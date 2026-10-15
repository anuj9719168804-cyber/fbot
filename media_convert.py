"""On-demand conversion commands, ported from src's Akbots/convert.py. Reply to a video with:

  /tomp4       any container (mkv, ts, avi, flv, webm, wmv, ...) -> a real .mp4. Stream copy first; re-encode
               only if the codecs can't go into MP4 as they are. Same engine as the automatic conversion
               (mp4_convert.ensure_mp4), so the result is the same as a link you downloaded.
  /tovideo     a video that came in as a file -> sent back as a playable video. Non-mp4 is converted first.
  /todocument  a video -> sent back as a plain file (no re-encode, original bytes, no player).

Each job takes a slot from the per-user download queue (dl_queue), so conversions and downloads share the 2 slots.
"""
import logging
import os
import shutil

from pyrogram import filters
from pyrogram.enums import ParseMode

import dl_queue
import mp4_convert

logger = logging.getLogger("media_convert")
VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".3gp", ".flv", ".m4v", ".ts", ".m2ts", ".mts",
              ".wmv", ".mpg", ".mpeg", ".vob", ".ogv", ".asf", ".f4v", ".divx")
USAGE = {
    "tomp4": "Reply to a video (any container: mkv, ts, avi, flv, webm...) with <code>/tomp4</code> to get a real .mp4.",
    "tovideo": "Reply to a video sent as a file with <code>/tovideo</code> to send it back as a playable video.",
    "todocument": "Reply to a video with <code>/todocument</code> to send it back as a plain file (no re-encode).",
}
_ns = None


def _media(message):
    replied = message.reply_to_message
    if not replied:
        return None, None
    if replied.video:
        return replied.video, (replied.video.file_name or f"video_{replied.id}.mp4")
    if replied.document and (replied.document.file_name or "").lower().endswith(VIDEO_EXTS):
        return replied.document, replied.document.file_name
    return None, None


async def _to_mp4(in_path: str, status, name: str):
    """-> path of an .mp4 made from in_path, or None if the conversion didn't produce one."""
    ns = _ns
    await status.edit_text(ns.SC(f"<b>🔄 Converting to MP4...</b>\n<code>{name}</code>"), parse_mode=ParseMode.HTML)
    out = await mp4_convert.ensure_mp4(in_path, mp4_convert.status_progress(status, name, ns.SC))
    if out == in_path or not out.lower().endswith(".mp4") or not os.path.exists(out):
        return None
    return out


async def _job(client, message, mode: str):
    ns = _ns
    uid = message.from_user.id
    media, orig = _media(message)
    if not media:
        return await message.reply(ns.SC(f"<b>{USAGE[mode]}</b>"), parse_mode=ParseMode.HTML)
    if await ns.is_banned(uid):
        return await message.reply(ns.SC("🚫 You are banned from using this bot."), parse_mode=ParseMode.HTML)
    if mode == "tomp4" and orig.lower().endswith(".mp4"):
        return await message.reply(ns.SC("<b>⚠️ This is already an .mp4 file.</b>"), parse_mode=ParseMode.HTML)

    import url_uploader as uu
    replied = message.reply_to_message
    status = await message.reply(ns.SC("<b>📥 Downloading...</b>"), parse_mode=ParseMode.HTML)
    folder = os.path.join(ns.DOWNLOAD_DIR, f"conv_{uid}_{replied.id}")
    os.makedirs(folder, exist_ok=True)
    rec = None
    try:
        try:
            rec = await dl_queue.acquire(client, uid, message.chat.id, mode, orig)
        except dl_queue.QueueCancelled:
            return
        in_path = os.path.join(folder, os.path.basename(orig))
        await client.download_media(replied, file_name=in_path)

        if mode == "todocument":
            send_path, as_video = in_path, False
        elif orig.lower().endswith(".mp4"):
            send_path, as_video = in_path, True
        else:
            send_path = await _to_mp4(in_path, status, orig)
            if not send_path:
                return await status.edit_text(ns.SC(
                    "<b>❌ Conversion failed.</b> The file may be corrupt or use a codec MP4 can't hold."),
                    parse_mode=ParseMode.HTML)
            as_video = True

        size = os.path.getsize(send_path)
        label = {"tomp4": "MP4", "tovideo": "Video", "todocument": "Document"}[mode]
        await uu._upload(client, message, status, send_path, os.path.basename(send_path), size, as_video,
                         os.path.splitext(send_path)[1].lower(), "", 0, uid, quality_label=label,
                         force_document=(mode == "todocument"))
    except uu.URLError as e:
        await status.edit_text(ns.SC(f"<b>❌ {e}</b>"), parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.exception(f"{mode} failed")
        try:
            await status.edit_text(ns.SC(f"<b>❌ {mode} failed:</b> <code>{str(e)[:200]}</code>"),
                                   parse_mode=ParseMode.HTML)
        except Exception:
            pass
    finally:
        shutil.rmtree(folder, ignore_errors=True)
        await dl_queue.release(rec)


def setup(app, ns) -> None:
    global _ns
    _ns = ns

    @app.on_message(filters.command("tomp4") & filters.private)
    async def tomp4_cmd(client, message):
        await _job(client, message, "tomp4")

    @app.on_message(filters.command("tovideo") & filters.private)
    async def tovideo_cmd(client, message):
        await _job(client, message, "tovideo")

    @app.on_message(filters.command("todocument") & filters.private)
    async def todocument_cmd(client, message):
        await _job(client, message, "todocument")

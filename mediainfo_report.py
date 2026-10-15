"""MediaInfo report for uploaded videos, published as a Telegraph page.

After each video upload the bot replies to the uploaded message with a "📊 MediaInfo" link: codecs, resolution,
audio and subtitle tracks, plus the raw report. Raw text comes from the `mediainfo` CLI if it's installed, otherwise
from ffprobe (ffmpeg is already a requirement of fbot). Telegraph pages are anonymous and public-by-link.

Ported from mxdlbot-main's utils/mediainfo.py and services/telegraph.py (raw report on a Telegraph page). The
mxdlbot version needs pymediainfo and the mediainfo CLI; this one falls back to ffprobe, which fbot already ships.

Env: MEDIAINFO_REPORT=0 turns the automatic report off (the /mediainfo command still works).
"""
import asyncio
import json
import logging
import os
from pathlib import Path

import aiohttp
from pyrogram import filters
from pyrogram.enums import ParseMode

logger = logging.getLogger("mediainfo_report")
TELEGRAPH_API = "https://api.telegra.ph"
MAX_RAW_CHARS = 60000          # Telegraph pages stay small; the raw report is cut to fit
CMD_TIMEOUT = 60
ENABLED = os.environ.get("MEDIAINFO_REPORT", "1").strip().lower() not in ("0", "false", "no", "off")
_TOKEN = None


class MediaInfoError(Exception):
    pass


async def _run(cmd, timeout=CMD_TIMEOUT):
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return None
    return out if proc.returncode == 0 and out else None


async def ffprobe_json(path: str):
    out = await _run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path])
    try:
        return json.loads(out) if out else None
    except json.JSONDecodeError:
        return None


def _hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _size(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TiB"


def _lang(stream) -> str:
    tags = stream.get("tags") or {}
    return (tags.get("language") or "und").lower()


def format_ffprobe(data: dict, path: str) -> str:
    fmt = data.get("format") or {}
    lines = [f"File: {os.path.basename(path)}",
             f"Container: {fmt.get('format_long_name') or fmt.get('format_name') or '?'}",
             f"Duration: {_hms(float(fmt.get('duration') or 0))}",
             f"Size: {_size(fmt.get('size'))}",
             f"Overall bitrate: {fmt.get('bit_rate', '?')} bps", ""]
    for i, s in enumerate(data.get("streams") or []):
        kind = s.get("codec_type")
        if kind == "video":
            lines += [f"Video #{i}",
                      f"  Codec: {s.get('codec_long_name') or s.get('codec_name') or '?'}",
                      f"  Resolution: {s.get('width', '?')}x{s.get('height', '?')}",
                      f"  Frame rate: {s.get('avg_frame_rate', '?')}",
                      f"  Bitrate: {s.get('bit_rate', '?')} bps", ""]
        elif kind == "audio":
            lines += [f"Audio #{i}",
                      f"  Codec: {s.get('codec_long_name') or s.get('codec_name') or '?'}",
                      f"  Language: {_lang(s)}",
                      f"  Channels: {s.get('channels', '?')}",
                      f"  Sample rate: {s.get('sample_rate', '?')} Hz", ""]
        elif kind == "subtitle":
            lines += [f"Subtitle #{i}", f"  Codec: {s.get('codec_name') or '?'}",
                      f"  Language: {_lang(s)}", ""]
    return "\n".join(lines)


def summary_lines(data: dict) -> list:
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    out = [f"Duration: {_hms(float(fmt.get('duration') or 0))} · Size: {_size(fmt.get('size'))}"]
    if video:
        out.append(f"Video: {video.get('codec_name', '?')} · {video.get('width', '?')}x{video.get('height', '?')}")
    if audio:
        langs = ", ".join(_lang(a).upper() for a in audio)
        out.append(f"Audio: {len(audio)} track(s) ({langs})")
    if subs:
        out.append(f"Subtitles: {len(subs)} track(s)")
    return out


async def raw_text(path: str) -> str:
    try:
        out = await _run(["mediainfo", path])
        if out:
            return out.decode("utf-8", errors="ignore")
    except FileNotFoundError:
        pass  # mediainfo CLI not installed: use ffprobe below
    data = await ffprobe_json(path)
    return format_ffprobe(data, path) if data else "MediaInfo not available"


async def _account(session) -> str:
    global _TOKEN
    if _TOKEN:
        return _TOKEN
    async with session.post(f"{TELEGRAPH_API}/createAccount",
                            data={"short_name": "fbot", "author_name": "fbot"}) as r:
        res = await r.json(content_type=None)
    if not isinstance(res, dict) or not res.get("ok"):
        raise MediaInfoError("Telegraph account could not be created.")
    _TOKEN = res["result"]["access_token"]
    return _TOKEN


async def telegraph_page(title: str, path: str) -> str:
    raw = (await raw_text(path))[:MAX_RAW_CHARS]
    data = await ffprobe_json(path)
    nodes = [{"tag": "h4", "children": [f"MediaInfo - {title[:200]}"]}]
    if data:
        nodes += [{"tag": "p", "children": [line]} for line in summary_lines(data)]
    nodes.append({"tag": "pre", "children": [raw]})
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        token = await _account(session)
        form = {"access_token": token, "title": f"MediaInfo - {title[:50]}", "author_name": "fbot",
                "content": json.dumps(nodes), "return_content": "false"}
        async with session.post(f"{TELEGRAPH_API}/createPage", data=form) as r:
            res = await r.json(content_type=None)
    if not isinstance(res, dict) or not res.get("ok"):
        global _TOKEN
        _TOKEN = None  # a bad token is re-created on the next call
        raise MediaInfoError(f"Telegraph page failed: {str(res.get('error') if isinstance(res, dict) else res)[:100]}")
    return res["result"]["url"]


async def send_report(client, chat_id: int, reply_to_id, title: str, path: str) -> None:
    """Automatic report after an upload. Never raises: a failed report must not affect the upload."""
    if not ENABLED or not path or not os.path.exists(path):
        return
    try:
        url = await asyncio.wait_for(telegraph_page(title, path), 90)
    except Exception as e:
        logger.warning(f"mediainfo report skipped: {e}")
        return
    try:
        await client.send_message(chat_id, f"📊 <b>MediaInfo:</b> {url}", reply_to_message_id=reply_to_id,
                                  parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning(f"mediainfo link not sent: {e}")


def setup(app, ns) -> None:
    @app.on_message(filters.command("mediainfo") & filters.private)
    async def mediainfo_cmd(client, m):
        target = m.reply_to_message
        media = (target.video or target.document) if target else None
        if not media:
            return await m.reply(ns.SC("<b>📊 MediaInfo</b>\nReply to a video with <code>/mediainfo</code> to get its "
                                       "codecs, tracks and a Telegraph page with the full report."),
                                 parse_mode=ParseMode.HTML)
        status = await m.reply(ns.SC("<b>📊 Reading MediaInfo...</b>"), parse_mode=ParseMode.HTML)
        path = None
        try:
            path = await client.download_media(target)
            url = await asyncio.wait_for(telegraph_page(getattr(media, "file_name", None) or "video", path), 120)
            await status.edit_text(f"📊 <b>MediaInfo:</b> {url}", parse_mode=ParseMode.HTML)
        except Exception as e:
            await status.edit_text(ns.SC(f"<b>❌ MediaInfo failed:</b> <code>{str(e)[:200]}</code>"),
                                   parse_mode=ParseMode.HTML)
        finally:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

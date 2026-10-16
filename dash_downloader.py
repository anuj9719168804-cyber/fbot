"""DASH (.mpd) stream downloader for fbot  (/url <manifest.mpd> [-n name], or just paste the .mpd link).

yt-dlp reads the DASH manifest, downloads the video + audio fragments in parallel and ffmpeg merges them into one
.mp4. The result then goes through the same path as every other /url download: mp4 conversion check, custom caption
and thumbnail, 2 GB split / Gofile, auto-delete, backup/log channels, the user's dump chat, daily limit + referral
prompt for free users. /cancel stops it like any other download.

HLS (.m3u8) is NOT handled here - fbot already has its own HLS downloader (m3u8_downloader.py, with a quality menu).

Not supported: DRM-protected manifests (Widevine / PlayReady / ContentProtection). The bot says so instead of
producing a broken file.

Env: DASH_MAX_HEIGHT        highest video height to pick (default 1080, 0 = best available)
     DASH_TASK_TIMEOUT      seconds before a download is given up (default 3600)
     DASH_FRAGMENT_WORKERS  parallel fragment downloads (default 8)
"""
import asyncio
import logging
import os
import re
import shutil
import threading
import time
import uuid

from pyrogram import enums

import mp4_convert
import url_uploader as _uu
from url_uploader import URLError

logger = logging.getLogger("dash_downloader")

MAX_HEIGHT = int(os.environ.get("DASH_MAX_HEIGHT", "1080"))
TASK_TIMEOUT = int(os.environ.get("DASH_TASK_TIMEOUT", "3600"))
FRAGMENT_WORKERS = int(os.environ.get("DASH_FRAGMENT_WORKERS", "8"))

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_SKIP_SUFFIXES = (".part", ".ytdl", ".temp", ".tmp", ".json", ".jpg", ".jpeg", ".png", ".webp", ".vtt", ".srt")


def _format_selector() -> str:
    if MAX_HEIGHT > 0:
        h = f"[height<={MAX_HEIGHT}]"
        return f"bv*{h}+ba/b{h}/bv*+ba/b"
    return "bv*+ba/b"


def _explain(err: Exception) -> str:
    """yt-dlp's error text -> one line a user can act on."""
    msg = _ANSI_RE.sub("", str(err) or "").strip()
    msg = re.sub(r"^ERROR:\s*", "", msg)
    low = msg.lower()
    if "drm" in low or "widevine" in low or "playready" in low or "contentprotection" in low:
        return "This stream is DRM-protected (Widevine/PlayReady) - it can't be downloaded."
    if "unsupported url" in low or "unable to extract" in low:
        return "Couldn't read that .mpd manifest (expired link, login/token missing, or not a real DASH manifest)."
    if "http error 403" in low or "http error 401" in low:
        return "The server refused the stream (HTTP 401/403) - the link has probably expired or needs a login/referer."
    if "http error 404" in low:
        return "The stream was not found (HTTP 404) - the link has expired or was removed."
    return msg[:250] or "yt-dlp failed."


def _pick_output(folder: str):
    """The finished file = the biggest non-temporary file yt-dlp left in `folder`."""
    best, best_size = None, 0
    for fn in os.listdir(folder):
        fp = os.path.join(folder, fn)
        if not os.path.isfile(fp) or fn.lower().endswith(_SKIP_SUFFIXES) or ".part" in fn.lower():
            continue
        sz = os.path.getsize(fp)
        if sz > best_size:
            best, best_size = fp, sz
    return best


def download_sync(url: str, folder: str, base_name, on_hook, cancel: threading.Event) -> str:
    """Blocking yt-dlp download of a DASH manifest into `folder` (runs in a worker thread). Returns the file path.
    `base_name`: output file name without extension (None = yt-dlp's title). Raises URLError."""
    try:
        import yt_dlp
        from yt_dlp.utils import DownloadCancelled, DownloadError
    except Exception:
        raise URLError("yt-dlp isn't installed on the server (pip install yt-dlp).")

    def hook(d):
        if cancel.is_set():
            raise DownloadCancelled()
        on_hook(d)

    tmpl = (re.sub(r"[%\\/]", "_", base_name) if base_name else "%(title).80s") + ".%(ext)s"
    opts = {
        "outtmpl": os.path.join(folder, tmpl),
        "format": _format_selector(),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 10,
        "fragment_retries": 10,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": max(1, FRAGMENT_WORKERS),
        "progress_hooks": [hook],
        "http_headers": {"User-Agent": _uu.UA},
    }
    ff = shutil.which("ffmpeg")
    if ff:
        opts["ffmpeg_location"] = os.path.dirname(ff) or ff
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except DownloadCancelled:
        raise
    except DownloadError as e:
        raise URLError(_explain(e))
    except Exception as e:
        raise URLError(_explain(e))
    path = _pick_output(folder)
    if not path:
        raise URLError("The stream downloaded nothing (empty manifest or no playable video/audio).")
    return path


def setup(app, ns) -> None:
    """Called by main.py at startup. DASH has no inline menu of its own: /url and a pasted .mpd link start run() directly
    (through url_uploader.handle_url, which owns the daily limit and the download queue). So there are no callbacks to
    register here; this only keeps the startup sequence in main.py working."""
    return None


async def run(client, message, url: str, custom_name=None):
    """Called by url_uploader.handle_url (already inside the per-user download queue) for .mpd links."""
    ns = _uu._ns
    uid = message.from_user.id
    me_task = asyncio.current_task()
    if await ns.is_banned(uid):
        return await message.reply(ns.SC("🚫 You are banned from using this bot."))
    premium = await ns.get_effective_premium_status(uid)
    if not premium["is_premium"] and await ns.get_daily_count(uid) >= ns.DAILY_FREE_LIMIT:
        return await ns.send_referral_prompt(client, uid)

    ns.ACTIVE_TASKS[uid] = me_task
    status = await message.reply(ns.SC("<b>🔎 Reading the DASH stream...</b>"), parse_mode=enums.ParseMode.HTML)
    folder = os.path.join(ns.DOWNLOAD_DIR, f"dash_{uid}_{uuid.uuid4().hex[:8]}")
    cancel = threading.Event()
    try:
        await _uu._check_public(url)                       # no localhost / private-network manifests
        if not await ns.disk_guard.wait_for_disk(ns.DOWNLOAD_DIR, need_bytes=0, max_wait_s=90):
            raise URLError("Server disk is almost full right now - try again in a few minutes.")
        os.makedirs(folder, exist_ok=True)

        label = _uu._safe_name(os.path.splitext(custom_name or "")[0], "") or "DASH stream"
        tracker = ns.ProgressTracker(status, "Downloading", label)
        loop = asyncio.get_running_loop()
        last = [0.0]

        def on_hook(d):
            if d.get("status") != "downloading":
                return
            now = time.time()
            if now - last[0] < 1.5:                        # tracker throttles edits itself; don't flood the loop
                return
            last[0] = now
            done = int(d.get("downloaded_bytes") or 0)
            total = int(d.get("total_bytes") or d.get("total_bytes_estimate") or 0)
            asyncio.run_coroutine_threadsafe(tracker.update(done, total), loop)

        base = _uu._safe_name(os.path.splitext(custom_name)[0], "") if custom_name else None
        dl_start = time.time()
        try:
            path = await asyncio.wait_for(
                asyncio.to_thread(download_sync, url, folder, base, on_hook, cancel), timeout=TASK_TIMEOUT)
        except asyncio.TimeoutError:
            raise URLError(f"The stream took longer than {TASK_TIMEOUT // 60} minutes - giving up.")
        dl_seconds = time.time() - dl_start

        size = os.path.getsize(path)
        if size <= 0:
            raise URLError("The downloaded file is empty.")
        if size > _uu.MAX_BYTES:
            raise URLError(f"Stream is {ns.human_size(size)} - larger than the {ns.human_size(_uu.MAX_BYTES)} limit.")
        name = os.path.basename(path)

        if mp4_convert.needs_convert(path):                # e.g. yt-dlp left .mkv/.webm -> real .mp4
            await status.edit_text(ns.SC("<b>🎞 Converting to MP4...</b>\n<code>" + name + "</code>"),
                                   parse_mode=enums.ParseMode.HTML)
            path = await mp4_convert.ensure_mp4(path, mp4_convert.status_progress(status, name, ns.SC))
            name = os.path.basename(path)
            size = os.path.getsize(path)
        ext = os.path.splitext(name)[1].lower()
        short = ns.shorten_filename(name)                  # byte-capped file name (Telegram/OS limits)
        if short != name:
            new_path = os.path.join(os.path.dirname(path), short)
            os.replace(path, new_path)
            path, name = new_path, short
        is_video = ext in _uu.VIDEO_EXT or ext == ""

        await _uu._upload(client, message, status, path, name, size, is_video, ext or ".mp4", url, dl_seconds, uid)
    except asyncio.CancelledError:
        cancel.set()                                       # stops the yt-dlp thread at its next progress tick
        try:
            await status.edit_text(ns.SC("<b>🛑 Cancelled.</b>"), parse_mode=enums.ParseMode.HTML)
        except Exception:
            pass
        raise
    except URLError as e:
        try:
            await status.edit_text(ns.SC(f"<b>❌ {e}</b>"), parse_mode=enums.ParseMode.HTML)
        except Exception:
            pass
    except Exception as e:
        logger.exception(f"dash download failed for {url!r}: {e}")
        try:
            await status.edit_text(ns.SC(f"<b>❌ Download failed:</b>\n<code>{str(e)[:300]}</code>"),
                                   parse_mode=enums.ParseMode.HTML)
        except Exception:
            pass
    finally:
        cancel.set()
        if ns.ACTIVE_TASKS.get(uid) is me_task:
            ns.ACTIVE_TASKS.pop(uid, None)
        shutil.rmtree(folder, ignore_errors=True)

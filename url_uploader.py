"""Generic URL uploader for fbot  (/url <direct link> [-n name]).

Ported from src--tera_api's Akbots/urluploader.py and rewritten to run on fbot's own pipeline instead of that bot's
helper modules: it takes ANY direct file link (mp4/mkv/zip/pdf/...), downloads it and uploads it to the user's chat
with the same extras every other fbot download gets - daily limit / premium check, referral prompt, /cancel,
custom caption + custom thumbnail, 2 GB split for videos, auto-delete, backup/log channels and the user's dump chat.

Ways to use it
  /url https://host/file.mp4                      download + upload
  /url https://host/file.mp4 -n MyVideo           rename ("-n \"My Video\"" for names with spaces)
  /url https://host:8080/f.mkv|MyFile.mkv|user|pass   private link behind HTTP Basic Auth (name/credentials optional)
  or simply paste a direct file link: a link no other fbot downloader claims is probed, and if the server answers
  with a real file (not a web page) it is uploaded exactly like /url.

Safety: links that point at private/internal addresses (localhost, 10.x, 192.168.x, cloud metadata 169.254.x ...)
are refused, so the bot cannot be used to poke at the server's own network (set URL_UPLOADER_ALLOW_PRIVATE=1 only
if you really want that).

Env: URL_UPLOADER_MAX_BYTES (default 4 GB; bigger links are refused), URL_UPLOADER_ALLOW_PRIVATE.
"""
import asyncio
import ipaddress
import logging
import os
import re
import shutil
import socket
import time
import uuid
from urllib.parse import urlparse, unquote

import aiohttp
from pyrogram import filters, enums
from pyrogram.errors import MessageNotModified
import mp4_convert

logger = logging.getLogger("url_uploader")

_ns = None  # main.py's namespace (SC, ProgressTracker, build_caption, ...) - set by setup()

MAX_BYTES = int(os.environ.get("URL_UPLOADER_MAX_BYTES", str(4 * 1024 ** 3)))
ALLOW_PRIVATE = os.environ.get("URL_UPLOADER_ALLOW_PRIVATE", "0") == "1"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")

URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
CUSTOM_NAME_RE = re.compile(r'(?:^|\s)-n\s+(".+?"|\S+)', re.IGNORECASE)

VIDEO_EXT = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".flv", ".wmv", ".ts", ".m2ts", ".3gp", ".mpg", ".mpeg"}
AUDIO_EXT = {".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus", ".aac", ".wma"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
FILE_EXT = VIDEO_EXT | AUDIO_EXT | IMAGE_EXT | {
    ".gif", ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso", ".pdf", ".epub", ".apk", ".exe", ".msi",
    ".dmg", ".txt", ".csv", ".srt", ".ass", ".vtt", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".json",
}

# domains other fbot plugins already own - never treat those as a raw file
_EXCLUDED = ("t.me", "telegram.me", "youtube.com", "youtu.be", "magnet:", ".torrent", "mega.nz", "drive.google.com")


class URLError(Exception):
    """A problem worth showing to the user as-is."""


# ------------------------------------------------------------------ parsing ----
def normalize_direct_url(url: str) -> str:
    """Dropbox / OneDrive share links serve an HTML preview unless asked for the raw bytes."""
    low = url.lower()
    if "dropbox.com" in low:
        if "dl=0" in url:
            return url.replace("dl=0", "dl=1")
        if "dl=1" not in url:
            return url + ("&" if "?" in url else "?") + "dl=1"
    if ("1drv.ms" in low or "onedrive.live.com" in low) and "download=1" not in low:
        return url + ("&" if "?" in url else "?") + "download=1"
    return url


def parse_request(text: str):
    """-> (url, custom_name, user, password) or None.  Understands 'url -n name' and 'url|name[|user|pass]'."""
    text = (text or "").strip()
    if not text:
        return None
    token = text.split()[0]
    if "|" in token and token.lower().startswith("http"):
        parts = [p.strip() for p in token.split("|")]
        if len(parts) == 2:
            return normalize_direct_url(parts[0]), (parts[1] or None), None, None
        if len(parts) == 4:
            return normalize_direct_url(parts[0]), (parts[1] or None), (parts[2] or None), (parts[3] or None)
        return None
    m = URL_RE.search(text)
    if not m:
        return None
    url = m.group(0).rstrip(").,;'\"")
    if any(d in url.lower() for d in _EXCLUDED):
        return None
    nm = CUSTOM_NAME_RE.search(text)
    name = nm.group(1).strip().strip('"').strip() if nm else None
    return normalize_direct_url(url), (name or None), None, None


def _safe_name(name: str, fallback: str) -> str:
    name = unquote((name or "").strip()).strip("/\\")
    name = "".join(c for c in name if c not in '\\/:*?"<>|\r\n\t').strip().strip(".")
    return name or fallback


def _apply_custom_name(original: str, custom: str) -> str:
    _, orig_ext = os.path.splitext(original)
    base, ext = os.path.splitext(custom)
    return _safe_name(f"{base}{ext or orig_ext}", original)


def _has_file_ext(url: str) -> bool:
    return os.path.splitext(urlparse(url).path.lower())[1] in FILE_EXT


# ------------------------------------------------------------- URL safety ----
async def _check_public(url: str) -> None:
    if ALLOW_PRIVATE:
        return
    host = urlparse(url).hostname
    if not host:
        raise URLError("That link has no host name.")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except Exception:
        raise URLError("Couldn't resolve that host name - check the link.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise URLError("Links to private/internal addresses are not allowed.")


# ------------------------------------------------------------------ probing ----
def _filename_from_disposition(cd: str):
    if not cd:
        return None
    m = re.search(r"filename\*\s*=\s*[\w-]+''([^;]+)", cd, re.I)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
    return m.group(1).strip() if m else None


async def probe(url: str, user: str = None, password: str = None, timeout: int = 15) -> dict:
    """HEAD (then a 1-byte ranged GET) -> {final_url, ctype, size, filename, is_html, attachment}. Raises URLError."""
    await _check_public(url)
    p = urlparse(url)
    headers = {"User-Agent": UA, "Accept": "*/*", "Referer": f"{p.scheme}://{p.netloc}/"}
    auth = aiohttp.BasicAuth(user, password or "") if user else None
    last_status = None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            for method, extra in (("HEAD", {}), ("GET", {"Range": "bytes=0-0"})):
                async with s.request(method, url, headers={**headers, **extra}, auth=auth,
                                     allow_redirects=True, max_redirects=8) as r:
                    last_status = r.status
                    if r.status in (401, 403) and method == "HEAD":
                        continue
                    if r.status >= 400 and method == "HEAD":
                        continue                      # some servers refuse HEAD - retry as a ranged GET
                    if r.status == 401:
                        raise URLError("The server wants a login (HTTP 401). Use: <code>/url link|name|user|pass</code>")
                    if r.status == 403:
                        raise URLError("The server refused the request (HTTP 403) - the link may need cookies or has expired.")
                    if r.status in (404, 410):
                        raise URLError(f"File not found (HTTP {r.status}) - the link is wrong or has expired.")
                    if r.status >= 400:
                        raise URLError(f"The server answered HTTP {r.status}.")
                    final = str(r.url)
                    await _check_public(final)
                    ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                    size = None
                    cr = r.headers.get("Content-Range", "")
                    if "/" in cr and cr.rsplit("/", 1)[1].isdigit():
                        size = int(cr.rsplit("/", 1)[1])
                    elif r.status == 200 and (r.headers.get("Content-Length") or "").isdigit():
                        size = int(r.headers["Content-Length"])
                    cd = r.headers.get("Content-Disposition", "")
                    return {
                        "final_url": final, "ctype": ctype, "size": size,
                        "filename": _filename_from_disposition(cd),
                        "attachment": "attachment" in cd.lower(),
                        "is_html": ctype in ("text/html", "application/xhtml+xml"),
                    }
    except URLError:
        raise
    except asyncio.TimeoutError:
        raise URLError("The server took too long to answer.")
    except aiohttp.ClientError as e:
        raise URLError(f"Couldn't connect: {str(e)[:150]}")
    raise URLError(f"The server answered HTTP {last_status}.")


# --------------------------------------------------------------- downloaders ----
_SIZE_UNITS = {"B": 1, "KIB": 1024, "MIB": 1024 ** 2, "GIB": 1024 ** 3, "TIB": 1024 ** 4,
               "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3}
_ARIA_RE = re.compile(r"\[#\w+\s+([\d.]+)\s*([KMGT]?i?B)/([\d.]+)\s*([KMGT]?i?B)\((\d+)%\)", re.I)


def _to_bytes(num: str, unit: str) -> int:
    return int(float(num) * _SIZE_UNITS.get(unit.upper(), 1))


async def _download_aiohttp(url, dest, user, password, on_progress, total_hint=None):
    p = urlparse(url)
    headers = {"User-Agent": UA, "Accept": "*/*", "Referer": f"{p.scheme}://{p.netloc}/"}
    auth = aiohttp.BasicAuth(user, password or "") if user else None
    done = 0
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=90)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.get(url, headers=headers, auth=auth, allow_redirects=True, max_redirects=8) as r:
            if r.status >= 400:
                raise URLError(f"The server answered HTTP {r.status}.")
            if (r.headers.get("Content-Type") or "").lower().startswith("text/html"):
                raise URLError("The link returned a web page, not a file (expired link / login / wrong URL).")
            total = int(r.headers["Content-Length"]) if (r.headers.get("Content-Length") or "").isdigit() else total_hint
            with open(dest, "wb") as f:
                async for chunk in r.content.iter_chunked(1024 * 1024):
                    f.write(chunk)
                    done += len(chunk)
                    if done > MAX_BYTES:
                        raise URLError("The file is larger than this bot's limit.")
                    await on_progress(done, total or 0)
    return dest


async def _download_aria2c(url, folder, out_name, on_progress, total_hint=None, with_referer=True):
    cmd = ["aria2c", f"--dir={folder}", f"--out={out_name}", "--continue=true", "--max-tries=5", "--retry-wait=3",
           "--max-connection-per-server=16", "--split=16", "--min-split-size=1M", "--summary-interval=2",
           "--console-log-level=warn", "--auto-file-renaming=false", "--allow-overwrite=true",
           "--file-allocation=none", f"--user-agent={UA}"]
    if with_referer:
        p = urlparse(url)
        cmd.append(f"--referer={p.scheme}://{p.netloc}/")
    cmd.append(url)
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    tail = []
    try:
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "ignore").strip()
            if not line:
                continue
            tail = (tail + [line])[-12:]
            m = _ARIA_RE.search(line)
            if m:
                await on_progress(_to_bytes(m.group(1), m.group(2)), _to_bytes(m.group(3), m.group(4)) or (total_hint or 0))
        rc = await proc.wait()
    except asyncio.CancelledError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise
    if rc != 0:
        err = " | ".join(tail)
        if with_referer and "errorCode=22" in err:      # CDN that dislikes our same-origin Referer on a redirect
            return await _download_aria2c(url, folder, out_name, on_progress, total_hint, with_referer=False)
        raise URLError(f"aria2c failed: {err[-250:] or rc}")
    path = os.path.join(folder, out_name)
    if not os.path.exists(path):
        left = [f for f in os.listdir(folder) if not f.endswith(".aria2")]
        if not left:
            raise URLError("The download finished but no file was produced.")
        path = os.path.join(folder, left[0])
    return path


def _looks_like_html(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(2048).lstrip().lower()
    except Exception:
        return False
    return head.startswith((b"<!doctype html", b"<html", b"<head", b"<?xml version")) and b"<" in head[:5]


# ------------------------------------------------------------------- main flow ----
def _busy_message():
    return _ns.SC("<b>⏳ You already have a download running.</b>\nWait for it to finish, or send /cancel.")


def _is_m3u8(url: str) -> bool:
    return ".m3u8" in (url or "").lower().split("?", 1)[0]


async def handle_url(client, message, url: str, custom_name=None, user=None, password=None):
    """Queue wrapper: up to 2 downloads per user run at once, extra ones wait (see dl_queue.py)."""
    import dl_queue
    try:
        rec = await dl_queue.acquire(client, message.from_user.id, message.chat.id, "url", url)
    except dl_queue.QueueCancelled:
        return
    try:
        return await _handle_url_impl(client, message, url, custom_name, user, password)
    finally:
        await dl_queue.release(rec)


async def _handle_url_impl(client, message, url: str, custom_name=None, user=None, password=None):
    if _is_m3u8(url):
        # HLS playlist: hand it to the m3u8 downloader (quality/audio/compress menu) instead of a plain file download
        import m3u8_downloader as _m3
        return await _m3.start_flow(client, message, [url], custom_name, None)
    ns = _ns
    uid = message.from_user.id
    me_task = asyncio.current_task()
    if await ns.is_banned(uid):
        return await message.reply(ns.SC("🚫 You are banned from using this bot."))
    premium = await ns.get_effective_premium_status(uid)
    if not premium["is_premium"] and await ns.get_daily_count(uid) >= ns.DAILY_FREE_LIMIT:
        return await ns.send_referral_prompt(client, uid)

    ns.ACTIVE_TASKS[uid] = me_task
    status = await message.reply(ns.SC("<b>🔎 Checking link...</b>"), parse_mode=enums.ParseMode.HTML)
    folder = os.path.join(ns.DOWNLOAD_DIR, f"url_{uid}_{uuid.uuid4().hex[:8]}")
    try:
        info = await probe(url, user, password)
        if info["is_html"] and not info["attachment"]:
            raise URLError("That link opens a web page, not a file. Send the direct download link of the file.")
        size = info["size"]
        if size and size > MAX_BYTES:
            raise URLError(f"File is {ns.human_size(size)} - larger than the {ns.human_size(MAX_BYTES)} limit.")

        guess = _safe_name(os.path.basename(urlparse(info["final_url"]).path), "") or \
            _safe_name(os.path.basename(urlparse(url).path), "")
        filename = _safe_name(info["filename"], "") or guess or f"file_{uuid.uuid4().hex[:6]}"
        if not os.path.splitext(filename)[1]:
            filename += {"video/mp4": ".mp4", "video/x-matroska": ".mkv", "video/webm": ".webm", "application/zip": ".zip",
                         "application/pdf": ".pdf", "audio/mpeg": ".mp3", "image/jpeg": ".jpg", "image/png": ".png"}.get(info["ctype"], "")
        if custom_name:
            filename = _apply_custom_name(filename, custom_name)
        filename = ns.shorten_filename(filename)               # byte-capped (fname_utils)
        ext = os.path.splitext(filename)[1].lower()
        is_video = ext in VIDEO_EXT or info["ctype"].startswith("video/")

        if size and size > ns.MAX_FILE_SIZE and not is_video:
            import user_settings as _us
            if not await _us.get_gofile_token(uid):
                raise URLError(f"File is {ns.human_size(size)} - Telegram only takes 2 GB per file and only videos can be split. "
                               "Save a Gofile token in /settings to send big files as a link.")
        if not await ns.disk_guard.wait_for_disk(ns.DOWNLOAD_DIR, need_bytes=size or 0, max_wait_s=90):
            raise URLError("Server disk is almost full right now - try again in a few minutes.")

        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, filename)
        await status.edit_text(ns.SC(f"<b>📥 Starting download...</b>\n<code>{filename}</code>"), parse_mode=enums.ParseMode.HTML)
        tracker = ns.ProgressTracker(status, "Downloading", filename)

        async def on_progress(done, total):
            try:
                await tracker.update(done, total)
            except Exception:
                pass

        dl_start = time.time()
        use_aria = bool(shutil.which("aria2c")) and not user      # credentials never go on a command line
        try:
            if use_aria:
                path = await _download_aria2c(url, folder, filename, on_progress, size)
            else:
                await _download_aiohttp(url, path, user, password, on_progress, size)
        except URLError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if not use_aria:
                raise URLError(f"Download failed: {str(e)[:200]}")
            logger.warning(f"aria2c failed for {url}: {e} - retrying with the built-in downloader")
            path = os.path.join(folder, filename)
            await _download_aiohttp(url, path, user, password, on_progress, size)
        dl_seconds = time.time() - dl_start

        if _looks_like_html(path):
            raise URLError("The server sent a web page instead of the file (expired link / login wall).")
        upload_size = os.path.getsize(path)
        if upload_size <= 0:
            raise URLError("The downloaded file is empty.")
        name = os.path.basename(path)

        if mp4_convert.needs_convert(path):  # ts/mkv/avi/... -> real .mp4 so Telegram shows it as a playable video
            await status.edit_text(ns.SC("<b>🎞 Converting to MP4...</b>\n<code>" + name + "</code>"),
                                   parse_mode=enums.ParseMode.HTML)
            path = await mp4_convert.ensure_mp4(path, mp4_convert.status_progress(status, name, ns.SC))
            name = os.path.basename(path)
            upload_size = os.path.getsize(path)
            if path.lower().endswith(".mp4"):
                ext, is_video = ".mp4", True

        await _upload(client, message, status, path, name, upload_size, is_video, ext, url, dl_seconds, uid)
    except asyncio.CancelledError:
        try:
            await status.edit_text(ns.SC("<b>🛑 Cancelled.</b>"), parse_mode=enums.ParseMode.HTML)
        except Exception:
            pass
        raise
    except URLError as e:
        try:
            await status.edit_text(ns.SC(f"<b>❌ {e}</b>"), parse_mode=enums.ParseMode.HTML)
        except MessageNotModified:
            pass
        except Exception:
            pass
    except Exception as e:
        logger.exception(f"url upload failed for {url!r}: {e}")
        try:
            await status.edit_text(ns.SC(f"<b>❌ Upload failed:</b>\n<code>{str(e)[:300]}</code>"), parse_mode=enums.ParseMode.HTML)
        except Exception:
            pass
    finally:
        if ns.ACTIVE_TASKS.get(uid) is me_task:
            ns.ACTIVE_TASKS.pop(uid, None)
        shutil.rmtree(folder, ignore_errors=True)


async def _upload(client, message, status, path, name, size, is_video, ext, url, dl_seconds, uid,
                  quality_label="Original", extra_caption="", force_document=False):
    ns = _ns
    chat_id = message.chat.id
    user = message.from_user
    by_name = (user.first_name or "") + (f" {user.last_name}" if user.last_name else "")
    import user_settings as _us
    as_document = force_document or (await _us.get_upload_mode(uid)) == "document"   # /settings -> Upload mode
    gofile_token = await _us.get_gofile_token(uid) if size > ns.MAX_FILE_SIZE else ""
    gofile_link = None

    duration = width = height = 0
    thumb_path, got_thumb = path + "_thumb.jpg", False
    if is_video:
        duration, width, height = await asyncio.to_thread(ns.get_video_metadata, path)
    if is_video or ext in AUDIO_EXT or ext not in IMAGE_EXT:
        custom_id = await ns.get_thumbnail(uid)            # /set_thumb photo overrides everything
        if custom_id:
            try:
                await client.download_media(custom_id, file_name=thumb_path)
                got_thumb = os.path.exists(thumb_path)
            except Exception as e:
                logger.warning(f"custom thumb download failed: {e}")
        if not got_thumb and is_video:
            at = min(max(duration * 0.15, 3), 20) if duration else 3
            got_thumb = await asyncio.to_thread(ns.generate_thumbnail, path, thumb_path, at)
        if got_thumb:
            got_thumb = await asyncio.to_thread(ns.finalize_thumbnail, thumb_path)

    parts = [path]
    if gofile_token:
        # over 2 GB and the user has a Gofile token: upload there and post the link instead of splitting
        import gofile
        parts = []
        gtracker = ns.ProgressTracker(status, "Uploading", name)
        await status.edit_text(ns.SC(f"<b>☁️ Uploading to Gofile (over 2GB)...</b>\n<code>{name}</code>"),
                               parse_mode=enums.ParseMode.HTML)
        try:
            gofile_link = await gofile.upload(path, gofile_token, gtracker.update)
        except gofile.GofileError as e:
            raise URLError(str(e))
    elif size > ns.MAX_FILE_SIZE:
        await status.edit_text(ns.SC(f"<b>✂️ Splitting into parts (over 2GB)...</b>\n<code>{name}</code>"), parse_mode=enums.ParseMode.HTML)
        parts = await asyncio.to_thread(ns.split_upload.split_video_file, path, ns.DOWNLOAD_DIR,
                                        uuid.uuid4().hex[:10], ns.SPLIT_PART_TARGET_BYTES)
        if len(parts) <= 1:
            raise URLError("The video is over 2GB and couldn't be split automatically.")

    async def caption_for(ul_seconds, part_i=None, total=1):
        cap = await ns.build_caption(
            name=name, size_bytes=size, dl_seconds=dl_seconds, ul_seconds=ul_seconds, user_id=uid, source_link=url,
            quality_label=quality_label, duration_seconds=duration or 0,
            downloaded_by_username=user.username, downloaded_by_name=by_name,
        )
        if extra_caption:
            cap += "\n" + extra_caption
        if total > 1:
            cap += f"\n\n✂️ <b>Part {part_i}/{total}</b>"
        return ns.SC(cap)

    await status.edit_text(ns.SC("<b>⏳ Uploading to Telegram...</b>"), parse_mode=enums.ParseMode.HTML)
    ul_tracker = ns.ProgressTracker(status, "Uploading", name)
    ul_tracker.start_upload_wait_animation()
    ul_start = time.time()
    thumb = thumb_path if got_thumb else None
    sent = []
    total = len(parts)
    for i, part in enumerate(parts, start=1):
        cap = await caption_for(0, i, total)
        pdur, pw, ph = (duration, width, height)
        if total > 1:
            pdur, pw, ph = await asyncio.to_thread(ns.get_video_metadata, part)
        if is_video and not as_document:
            msg = await client.send_video(chat_id, part, caption=cap, parse_mode=enums.ParseMode.HTML, supports_streaming=True,
                                          thumb=thumb, duration=pdur or 0, width=pw or 0, height=ph or 0, progress=ul_tracker.update)
        elif ext in AUDIO_EXT:
            msg = await client.send_audio(chat_id, part, caption=cap, parse_mode=enums.ParseMode.HTML,
                                          title=os.path.splitext(name)[0], thumb=thumb, progress=ul_tracker.update)
        elif ext in IMAGE_EXT and size <= 10 * 1024 * 1024:
            msg = await client.send_photo(chat_id, part, caption=cap, parse_mode=enums.ParseMode.HTML, progress=ul_tracker.update)
        else:
            msg = await client.send_document(chat_id, part, caption=cap, parse_mode=enums.ParseMode.HTML,
                                             thumb=thumb, force_document=True, progress=ul_tracker.update)
        sent.append(msg)
        if total > 1:
            try:
                os.remove(part)
            except OSError:
                pass
    if gofile_link:
        glink = f'\n\n☁️ <a href="{gofile_link}">Download from Gofile</a>'
        gmsg = await client.send_message(chat_id, await caption_for(0, 1, 1) + glink,
                                         parse_mode=enums.ParseMode.HTML, disable_web_page_preview=True)
        sent.append(gmsg)
    ul_seconds = time.time() - ul_start
    for i, msg in enumerate(sent, start=1):               # real upload time into the caption, like the normal flow
        try:
            await msg.edit_caption(caption=await caption_for(ul_seconds, i, total), parse_mode=enums.ParseMode.HTML)
        except Exception:
            pass

    try:
        await status.delete()
    except Exception:
        pass
    if got_thumb:
        try:
            os.remove(thumb_path)
        except OSError:
            pass
    if is_video and sent:                                  # MediaInfo report, replied under the video
        import mediainfo_report as _mi
        await _mi.send_report(client, chat_id, sent[0].id, name, path)
    premium = await ns.get_effective_premium_status(uid)
    if not premium["is_premium"]:
        await ns.bump_daily_count(uid)
    await ns.bump_total_downloads(uid)
    for msg in sent:
        asyncio.create_task(ns.schedule_delete(client, chat_id, msg.id))
        asyncio.create_task(ns.backup_to_linked_channels(client, chat_id, msg.id))
        asyncio.create_task(ns.forward_to_dump_chat(client, chat_id, msg.id))
    try:
        await ns.log_event(client, f"🔗 <b>URL upload</b>\n👤 <code>{uid}</code> (@{user.username or '-'})\n"
                                    f"📄 {name[:80]} — {ns.human_size(size)}")
    except Exception:
        pass


# ------------------------------------------------------------- entry points ----
USAGE = ("<b>🔗 URL Uploader</b>\n\n"
         "Send any direct download link and I upload the file to Telegram.\n\n"
         "<b>Usage:</b> <code>/url &lt;direct link&gt; [-n name]</code>\n"
         "<b>Example:</b> <code>/url https://example.com/video.mp4 -n MyVideo</code>\n\n"
         "<b>Private link (HTTP login):</b>\n<code>/url https://host:8080/file.mkv|MyFile.mkv|user|pass</code>\n\n"
         "<i>You can also just paste a direct file link.</i>")


async def maybe_handle(client, message, text: str) -> bool:
    """Hook for fbot's link_handler: called for text no other downloader claimed. True = handled here."""
    req = parse_request(text)
    if not req:
        return False
    url, name, user, password = req
    if _is_m3u8(url):
        await handle_url(client, message, url, name, user, password)
        return True
    explicit = bool(name or user)
    if not (explicit or _has_file_ext(url)):
        # unknown shape (page, redirector, download.php?id=1): a quick probe tells a file from a web page
        try:
            info = await probe(url, timeout=8)
        except URLError:
            return False
        is_file = not info["is_html"] and (info["attachment"] or not info["ctype"].startswith("text/"))
        if not is_file:
            return False
    await handle_url(client, message, url, name, user, password)
    return True


def setup(app, ns) -> None:
    """Registers /url and /direct. `ns` is main.py's module (gives access to its helpers at call time)."""
    global _ns
    _ns = ns

    @app.on_message(filters.command(["url", "direct"]) & filters.private)
    async def url_command(client, message):
        parts = (message.text or "").split(None, 1)
        req = parse_request(parts[1]) if len(parts) > 1 else None
        if not req:
            return await message.reply(ns.SC(USAGE), parse_mode=enums.ParseMode.HTML)
        url, name, user, password = req
        await handle_url(client, message, url, name, user, password)

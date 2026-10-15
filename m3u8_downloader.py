"""M3U8 / HLS downloader for fbot  (/m3u8 <playlist link(s)>).

Ported from src--tera_api's Akbots/m3u8dl.py and rewritten for fbot (no extra pip packages: own playlist parser,
aiohttp for the downloads, `cryptography` for AES-128, ffmpeg for the final mux - all already in fbot).

What it does
  /m3u8 <url> [url2 url3 ...] [-n name] [-r referer]     (a bare pasted ....m3u8 link works too)
  1. reads the playlist and offers the qualities (or "All qualities"), then the audio track if the stream has several
     (⭐ marks Hindi), then "Compress to H.265?" (optional, slow);
  2. downloads every segment in parallel (AES-128 encrypted streams are decrypted, fMP4 init segments and byte-range
     segments are handled, failed segments are retried), joins them and muxes video + separate audio into one .mp4;
  3. uploads through fbot's normal upload path (url_uploader._upload): custom caption/thumbnail, 2 GB split,
     auto-delete, backup/log channels, dump chat, daily limit + referral prompt for free users.
  /cancel stops a running batch (same global command as every other download).

Not supported: DRM (SAMPLE-AES / Widevine) streams - the bot says so instead of producing a broken file.
Safety: playlist/segment/key hosts that resolve to private/internal addresses are refused (same guard as /url).

Env: M3U8_MAX_WORKERS (batches running at once across all users, default 3), M3U8_SEGMENT_WORKERS (parallel segment
downloads per stream, default 16), M3U8_TASK_TIMEOUT (seconds per stream, default 3600), M3U8_MUX_TIMEOUT (900),
M3U8_COMPRESS_TIMEOUT (1800), M3U8_MAX_BYTES (default 4 GB per stream), M3U8_MAX_ITEMS (streams per batch, 20).
"""
import asyncio
import logging
import os
import re
import shutil
import time
import uuid
from urllib.parse import urljoin, urlparse, unquote

import aiohttp
from pyrogram import filters, enums

import url_uploader as _uu
from url_uploader import URLError

logger = logging.getLogger("m3u8_downloader")

_ns = None

MAX_WORKERS = int(os.environ.get("M3U8_MAX_WORKERS", "3"))
SEGMENT_WORKERS = int(os.environ.get("M3U8_SEGMENT_WORKERS", "16"))
TASK_TIMEOUT = int(os.environ.get("M3U8_TASK_TIMEOUT", "3600"))
MUX_TIMEOUT = int(os.environ.get("M3U8_MUX_TIMEOUT", "900"))
COMPRESS_TIMEOUT = int(os.environ.get("M3U8_COMPRESS_TIMEOUT", "1800"))
MAX_BYTES = int(os.environ.get("M3U8_MAX_BYTES", str(4 * 1024 ** 3)))
MAX_ITEMS = int(os.environ.get("M3U8_MAX_ITEMS", "20"))
UA = _uu.UA

M3U8_AUTO_RE = re.compile(r"https?://\S+?\.m3u8(?:\?\S*)?", re.IGNORECASE)
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
FLAG_N = re.compile(r'(?:^|\s)-n\s+(".+?"|\S+)', re.IGNORECASE)
FLAG_R = re.compile(r"(?:^|\s)-r\s+(\S+)", re.IGNORECASE)

LANG_FLAGS = {
    "hi": "🇮🇳 Hindi", "hin": "🇮🇳 Hindi", "hindi": "🇮🇳 Hindi", "ja": "🇯🇵 Japanese", "jpn": "🇯🇵 Japanese",
    "japanese": "🇯🇵 Japanese", "en": "🇺🇸 English", "eng": "🇺🇸 English", "english": "🇺🇸 English",
    "ko": "🇰🇷 Korean", "kor": "🇰🇷 Korean", "ta": "🇮🇳 Tamil", "tam": "🇮🇳 Tamil", "te": "🇮🇳 Telugu",
    "tel": "🇮🇳 Telugu", "bn": "🇮🇳 Bengali", "ben": "🇮🇳 Bengali", "mr": "🇮🇳 Marathi", "ml": "🇮🇳 Malayalam",
    "kn": "🇮🇳 Kannada", "gu": "🇮🇳 Gujarati", "pa": "🇮🇳 Punjabi", "ur": "🇵🇰 Urdu", "zh": "🇨🇳 Chinese",
    "fr": "🇫🇷 French", "de": "🇩🇪 German", "es": "🇪🇸 Spanish", "pt": "🇵🇹 Portuguese", "ru": "🇷🇺 Russian",
    "ar": "🇸🇦 Arabic", "it": "🇮🇹 Italian", "th": "🇹🇭 Thai",
}


def lang_display(language: str, name: str) -> str:
    k = (language or "").lower().strip()
    if k in LANG_FLAGS:
        return LANG_FLAGS[k]
    n = (name or "").lower().strip()
    if n in LANG_FLAGS:
        return LANG_FLAGS[n]
    for key, val in LANG_FLAGS.items():
        if len(key) > 2 and key in n:
            return val
    if name:
        return f"🔊 {name}"
    return f"🔊 {language}" if language else "🔊 Unknown"


def is_hindi(track: dict) -> bool:
    l, n = (track.get("language") or "").lower(), (track.get("name") or "").lower()
    return l in ("hi", "hin", "hindi") or "hindi" in n


# --------------------------------------------------------------- playlist parser ----
_ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def _attrs(s: str) -> dict:
    return {k: v.strip().strip('"') for k, v in _ATTR_RE.findall(s)}


def parse_master(text: str, base: str) -> dict:
    """-> {"is_master": bool, "qualities": [...], "audio_tracks": [...]}.  A media playlist gives one 'default' quality."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines or not lines[0].startswith("#EXTM3U"):
        raise URLError("That link is not a valid M3U8 playlist (it doesn't start with #EXTM3U).")
    if not any(l.startswith("#EXT-X-STREAM-INF") for l in lines):
        return {"is_master": False, "audio_tracks": [],
                "qualities": [{"name": "default", "resolution": "Unknown", "bandwidth": 0, "url": base, "height": 0, "group": None}]}
    qualities, audio, seen_q, seen_a = [], [], set(), set()
    for i, l in enumerate(lines):
        if l.startswith("#EXT-X-MEDIA:"):
            a = _attrs(l.split(":", 1)[1])
            if a.get("TYPE") == "AUDIO" and a.get("URI"):
                url = urljoin(base, a["URI"])
                if url in seen_a:
                    continue
                seen_a.add(url)
                audio.append({"language": a.get("LANGUAGE", ""), "name": a.get("NAME", ""), "url": url,
                              "group": a.get("GROUP-ID"), "default": a.get("DEFAULT") == "YES",
                              "display": lang_display(a.get("LANGUAGE", ""), a.get("NAME", ""))})
        elif l.startswith("#EXT-X-STREAM-INF:"):
            a = _attrs(l.split(":", 1)[1])
            uri = next((x for x in lines[i + 1:i + 4] if not x.startswith("#")), None)
            if not uri:
                continue
            url = urljoin(base, uri)
            bw = int(a.get("BANDWIDTH") or a.get("AVERAGE-BANDWIDTH") or 0)
            res = a.get("RESOLUTION", "")
            m = re.match(r"(\d+)x(\d+)", res)
            height = int(m.group(2)) if m else 0
            key = (url, height)
            if key in seen_q:
                continue
            seen_q.add(key)
            qualities.append({"name": f"{height}p" if height else (f"{bw // 1000}kbps" if bw else "default"),
                              "resolution": res or "Unknown", "bandwidth": bw, "url": url, "height": height,
                              "group": a.get("AUDIO")})
    if not qualities:
        raise URLError("No playable qualities found in that playlist.")
    qualities.sort(key=lambda q: (q["height"], q["bandwidth"]))
    # same height twice (different bitrate/codec): keep the best bitrate of each height
    best = {}
    for q in qualities:
        best[q["height"] or q["bandwidth"]] = q
    return {"is_master": True, "qualities": sorted(best.values(), key=lambda q: (q["height"], q["bandwidth"])), "audio_tracks": audio}


def parse_media(text: str, base: str) -> dict:
    """-> {"segments": [{url, key, iv, seq, range}], "init": {url, range, key, iv} | None}.  Raises URLError for DRM."""
    segs, init = [], None
    seq = 0
    key = None                                    # (uri, iv_bytes|None) or None
    pending_range, last_end = None, {}
    for raw in text.splitlines():
        l = raw.strip()
        if not l:
            continue
        if l.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            seq = int(l.split(":", 1)[1] or 0)
        elif l.startswith("#EXT-X-KEY:"):
            a = _attrs(l.split(":", 1)[1])
            method = a.get("METHOD", "NONE").upper()
            if method == "NONE":
                key = None
            elif method == "AES-128":
                iv = a.get("IV")
                key = (urljoin(base, a["URI"]), bytes.fromhex(iv[2:].zfill(32)) if iv else None)
            else:
                raise URLError(f"This stream is DRM-protected ({method}) - it can't be downloaded.")
        elif l.startswith("#EXT-X-MAP:"):
            a = _attrs(l.split(":", 1)[1])
            if a.get("URI"):
                init = {"url": urljoin(base, a["URI"]), "range": _parse_range(a.get("BYTERANGE"), None, {}, urljoin(base, a["URI"])),
                        "key": key}
        elif l.startswith("#EXT-X-BYTERANGE:"):
            pending_range = l.split(":", 1)[1].strip()
        elif l.startswith("#"):
            continue
        else:
            url = urljoin(base, l)
            rng = _parse_range(pending_range, None, last_end, url) if pending_range else None
            pending_range = None
            segs.append({"url": url, "key": key, "seq": seq, "range": rng})
            seq += 1
    if not segs:
        raise URLError("The playlist has no segments.")
    return {"segments": segs, "init": init}


def _parse_range(spec, _unused, last_end: dict, url: str):
    if not spec:
        return None
    m = re.match(r"(\d+)(?:@(\d+))?", spec)
    if not m:
        return None
    length = int(m.group(1))
    start = int(m.group(2)) if m.group(2) is not None else last_end.get(url, 0)
    last_end[url] = start + length
    return (start, start + length - 1)


def video_name(url: str, idx: int = 0, total: int = 1) -> str:
    try:
        parts = unquote(urlparse(url).path).strip("/").split("/")
        skip = {"hls", "video", "stream", "media", "content", "master", "index", "playlist", "manifest", "0", "1", "2", "3"}
        for part in reversed(parts):
            name = re.sub(r"\.m3u8?$", "", part, flags=re.I)
            if name and len(name) > 3 and name.lower() not in skip:
                return _uu._safe_name(name, f"Video_{idx + 1}")[:40]
    except Exception:
        pass
    return f"Video_{idx + 1}" if total > 1 else "video"


# --------------------------------------------------------------------- network ----
class Fetcher:
    """One aiohttp session + SSRF host guard + retries; `fetch` is replaceable in tests."""

    def __init__(self, headers: dict, proxy: str | None = None):
        self.headers = {"User-Agent": UA, "Accept": "*/*", **headers}
        self.proxy = proxy          # optional proxy URL (MX Player: own MXPLAYER_PROXY or the free-proxy pool)
        self._ok_hosts = set()
        self._session = None

    async def __aenter__(self):
        conn = aiohttp.TCPConnector(limit=SEGMENT_WORKERS + 4)
        self._session = aiohttp.ClientSession(connector=conn, timeout=aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=60))
        return self

    async def __aexit__(self, *a):
        if self._session:
            await self._session.close()

    async def _guard(self, url: str):
        host = urlparse(url).hostname or ""
        if host in self._ok_hosts:
            return
        await _uu._check_public(url)
        self._ok_hosts.add(host)

    async def fetch(self, url: str, rng=None, retries: int = 3) -> bytes:
        await self._guard(url)
        headers = dict(self.headers)
        if rng:
            headers["Range"] = f"bytes={rng[0]}-{rng[1]}"
        last = None
        for attempt in range(retries):
            try:
                async with self._session.get(url, headers=headers, proxy=self.proxy,
                                             allow_redirects=True, max_redirects=6) as r:
                    await self._guard(str(r.url))
                    if r.status in (401, 403):
                        raise URLError(f"The server refused the request (HTTP {r.status}) - the link expired, or it needs a Referer (-r).")
                    if r.status in (404, 410):
                        raise URLError(f"Not found (HTTP {r.status}) - the link is wrong or expired.")
                    if r.status not in (200, 206):
                        raise RuntimeError(f"HTTP {r.status}")
                    return await r.read()
            except (URLError, asyncio.CancelledError):
                raise
            except Exception as e:
                last = e
                await asyncio.sleep(0.6 * (attempt + 1))
        raise RuntimeError(f"download failed: {str(last)[:120]}")

    async def text(self, url: str) -> str:
        data = await self.fetch(url)
        if len(data) > 8 * 1024 * 1024:
            raise URLError("That playlist is unreasonably large.")
        return data.decode("utf-8", "ignore")


def _decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    out = dec.update(data) + dec.finalize()
    if out:
        pad = out[-1]
        if 1 <= pad <= 16 and out.endswith(bytes([pad]) * pad):
            out = out[:-pad]
    return out


async def download_stream(fetcher: Fetcher, media_url: str, workdir: str, label: str, on_progress, deadline: float) -> str:
    """Downloads + (decrypts) + joins all segments of one media playlist -> path of the joined file."""
    os.makedirs(workdir, exist_ok=True)
    pl = parse_media(await fetcher.text(media_url), media_url)
    segs, init = pl["segments"], pl["init"]
    keys: dict = {}

    async def get_key(uri):
        if uri not in keys:
            keys[uri] = await fetcher.fetch(uri)
            if len(keys[uri]) != 16:
                raise URLError("The stream's AES key is not valid (needs a login/token?).")
        return keys[uri]

    async def prep(item, data):
        k = item.get("key")
        if not k:
            return data
        key = await get_key(k[0])
        iv = k[1] or item.get("seq", 0).to_bytes(16, "big")
        return await asyncio.to_thread(_decrypt, data, key, iv)

    init_path = None
    if init:
        init_path = os.path.join(workdir, "init.seg")
        data = await prep({"key": init["key"], "seq": 0}, await fetcher.fetch(init["url"], init["range"]))
        with open(init_path, "wb") as f:
            f.write(data)

    total = len(segs)
    state = {"done": 0, "bytes": 0}
    sem = asyncio.Semaphore(SEGMENT_WORKERS)
    paths = [os.path.join(workdir, f"{i:06d}.seg") for i in range(total)]

    async def one(i, item):
        async with sem:
            if time.time() > deadline:
                raise URLError(f"Timed out - the download took longer than {TASK_TIMEOUT // 60} minutes.")
            data = await prep(item, await fetcher.fetch(item["url"], item["range"]))
            with open(paths[i], "wb") as f:
                f.write(data)
            state["done"] += 1
            state["bytes"] += len(data)
            if state["bytes"] > MAX_BYTES:
                raise URLError("The stream is larger than this bot's limit.")
            est = int(state["bytes"] * total / state["done"])
            await on_progress(state["bytes"], max(est, state["bytes"]), state["done"], total)

    tasks = [asyncio.ensure_future(one(i, s)) for i, s in enumerate(segs)]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    out = os.path.join(workdir, f"{label}.joined")
    def _join():
        with open(out, "wb") as of:
            for p in ([init_path] if init_path else []) + paths:
                with open(p, "rb") as f:
                    shutil.copyfileobj(f, of, 1024 * 1024)
                os.remove(p)
        return out
    return await asyncio.to_thread(_join)


# ------------------------------------------------------------------------ ffmpeg ----
async def _ffmpeg(cmd: list, timeout: int) -> bool:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return False
    except asyncio.CancelledError:
        proc.kill()
        raise
    if proc.returncode != 0:
        logger.warning("ffmpeg failed: %s", (err or b"").decode("utf-8", "ignore")[-300:])
    return proc.returncode == 0


def _mp4_flags(path: str) -> list:
    """+faststart only makes sense for MP4; the MKV muxer rejects it."""
    return ["-movflags", "+faststart"] if path.lower().endswith(".mp4") else []


async def remux_mkv(path: str) -> str:
    """Stream-copy any container to .mkv (all tracks kept). On failure the original file is returned."""
    if path.lower().endswith(".mkv"):
        return path
    out = os.path.splitext(path)[0] + ".mkv"
    if await _ffmpeg(["ffmpeg", "-y", "-nostdin", "-loglevel", "error", "-i", path, "-map", "0",
                      "-c", "copy", out], MUX_TIMEOUT) and os.path.exists(out) and os.path.getsize(out) > 10000:
        try:
            os.remove(path)
        except OSError:
            pass
        return out
    return path


async def mux(video: str, audio, out_path: str) -> str:
    """Video (+ separate audio) -> mp4. Never falls back to a raw copy: a failed mux is an error."""
    def ok():
        return os.path.exists(out_path) and os.path.getsize(out_path) > 10000
    base = ["ffmpeg", "-y", "-nostdin", "-loglevel", "error", "-fflags", "+genpts"]
    if audio and os.path.exists(audio) and os.path.getsize(audio) > 500:
        for acodec in ("aac", "copy"):
            if await _ffmpeg(base + ["-i", video, "-i", audio, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", acodec,
                                     *_mp4_flags(out_path), "-shortest", out_path], MUX_TIMEOUT) and ok():
                return out_path
    if await _ffmpeg(base + ["-i", video, "-map", "0:v:0", "-map", "0:a?", "-c", "copy", *_mp4_flags(out_path), out_path], MUX_TIMEOUT) and ok():
        return out_path
    if await _ffmpeg(base + ["-i", video, "-map", "0:v:0", "-map", "0:a?", "-c:v", "copy", "-c:a", "aac", *_mp4_flags(out_path), out_path], MUX_TIMEOUT) and ok():
        return out_path
    raise URLError("Couldn't join the downloaded segments into a video (the stream may be damaged or unsupported).")


async def compress_hevc(path: str) -> str:
    out = os.path.splitext(path)[0] + "_hevc.mp4"
    if not await _ffmpeg(["ffmpeg", "-y", "-nostdin", "-loglevel", "error", "-i", path, "-c:v", "libx265", "-preset", "medium", "-crf", "23",
                          "-tag:v", "hvc1", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out], COMPRESS_TIMEOUT):
        try:
            os.remove(out)
        except OSError:
            pass
        raise RuntimeError("H.265 compression failed or timed out")
    if not (os.path.exists(out) and os.path.getsize(out) > 10000):
        raise RuntimeError("H.265 compression produced no usable output")
    return out


# ------------------------------------------------------------------- selection ----
def find_quality(qualities: list, height):
    if not qualities:
        return None
    if height is None:
        return qualities[-1]
    for q in qualities:
        if q["height"] == height:
            return q
    return min(qualities, key=lambda q: abs(q["height"] - height))


def find_audio(tracks: list, group, lang: str, name: str):
    if not tracks:
        return None
    pool = [t for t in tracks if group and t.get("group") == group] or tracks
    if lang or name:
        for t in pool:
            tl, tn = (t.get("language") or "").lower(), (t.get("name") or "").lower()
            if (lang and lang.lower() in (tl, tn)) or (name and name.lower() == tn):
                return t
    return next((t for t in pool if t.get("default")), pool[0])


# ------------------------------------------------------------------------ batch ----
_SESSIONS: dict = {}
_SEM = None
_WAITING = 0


def _sem():
    global _SEM
    if _SEM is None:
        _SEM = asyncio.Semaphore(MAX_WORKERS)
    return _SEM


async def process_batch(client, message, task: dict):
    global _WAITING
    ns = _ns
    uid, cid = message.from_user.id, message.chat.id
    import dl_queue
    try:
        rec = await dl_queue.acquire(client, uid, cid, "m3u8", (task.get("name") if isinstance(task, dict) else None) or "HLS stream")
    except dl_queue.QueueCancelled:
        return
    me = asyncio.current_task()
    ns.ACTIVE_TASKS[uid] = me
    queued_msg = None
    done_n = failed_n = skipped_n = 0
    t0 = time.time()
    folder = None
    status = None
    try:
        if _sem().locked():
            _WAITING += 1
            queued_msg = await client.send_message(cid, ns.SC(f"<b>⏳ Queued</b> — position {_WAITING}. I'll start when a slot is free."),
                                                   parse_mode=enums.ParseMode.HTML)
        try:
            await _sem().acquire()
        finally:
            if queued_msg is not None:
                _WAITING = max(0, _WAITING - 1)
                try:
                    await queued_msg.delete()
                except Exception:
                    pass
        try:
            urls = task["urls"]
            headers = {"Referer": task["referer"], "Origin": task["referer"].rstrip("/")} if task.get("referer") else {}
            async with Fetcher(headers) as fetcher:
                for ui, url in enumerate(urls):
                    tag = f"[{ui + 1}/{len(urls)}] " if len(urls) > 1 else ""
                    vname = task["name"] if (task.get("name") and len(urls) == 1) else video_name(url, ui, len(urls))
                    try:
                        info = parse_master(await fetcher.text(url), url)
                    except (URLError, RuntimeError) as e:
                        failed_n += 1
                        await client.send_message(cid, ns.SC(f"<b>❌ {tag}{vname}:</b> {e}"), parse_mode=enums.ParseMode.HTML)
                        continue
                    chosen = info["qualities"] if task["is_all"] else [find_quality(info["qualities"], task["sel_height"])]
                    for qi, q in enumerate(chosen):
                        if not await _within_limit(client, uid, ns):
                            skipped_n += (len(urls) - ui) * 0 + 1
                            raise _LimitReached()
                        qtag = f"[{q['name']}]" if len(chosen) > 1 else ""
                        folder = os.path.join(ns.DOWNLOAD_DIR, f"m3u8_{uid}_{uuid.uuid4().hex[:8]}")
                        os.makedirs(folder, exist_ok=True)
                        status = await client.send_message(cid, ns.SC(f"<b>📥 {tag}{vname} {qtag}</b>\n<i>Reading playlist... (/cancel to stop)</i>"),
                                                           parse_mode=enums.ParseMode.HTML)
                        try:
                            ok = await _one_stream(client, message, fetcher, status, folder, url, vname, q, info, task, uid)
                            done_n += 1 if ok else 0
                            failed_n += 0 if ok else 1
                        except URLError as e:
                            failed_n += 1
                            await _edit(status, ns.SC(f"<b>❌ {tag}{vname} {qtag}:</b> {e}"))
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            failed_n += 1
                            logger.exception("m3u8 stream failed: %s", e)
                            await _edit(status, ns.SC(f"<b>❌ {tag}{vname} {qtag} failed:</b>\n<code>{str(e)[:200]}</code>"))
                        finally:
                            shutil.rmtree(folder, ignore_errors=True)
                            folder = None
        finally:
            _sem().release()
        if len(urls) > 1 or failed_n or skipped_n:
            await client.send_message(cid, ns.SC(f"<b>🎉 M3U8 batch finished</b>\n✅ Uploaded: {done_n}\n❌ Failed: {failed_n}\n⏭ Skipped: {skipped_n}\n"
                                                 f"🕐 Time: {ns.human_time(time.time() - t0) if hasattr(ns, 'human_time') else int(time.time() - t0)}"),
                                       parse_mode=enums.ParseMode.HTML)
    except _LimitReached:
        pass
    except asyncio.CancelledError:
        if status is not None:
            await _edit(status, ns.SC("<b>🛑 Cancelled.</b>"))
        else:
            try:
                await client.send_message(cid, ns.SC("<b>🛑 Cancelled.</b>"), parse_mode=enums.ParseMode.HTML)
            except Exception:
                pass
        raise
    finally:
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
        if ns.ACTIVE_TASKS.get(uid) is me:
            ns.ACTIVE_TASKS.pop(uid, None)
        await dl_queue.release(rec)


class _LimitReached(Exception):
    pass


async def _within_limit(client, uid: int, ns) -> bool:
    premium = await ns.get_effective_premium_status(uid)
    if premium["is_premium"] or await ns.get_daily_count(uid) < ns.DAILY_FREE_LIMIT:
        return True
    await ns.send_referral_prompt(client, uid)
    return False


async def _edit(msg, text):
    try:
        await msg.edit_text(text, parse_mode=enums.ParseMode.HTML)
    except Exception:
        pass


async def _one_stream(client, message, fetcher, status, folder, playlist_url, vname, q, info, task, uid) -> bool:
    ns = _ns
    name = _uu._safe_name(f"{vname}_{q['name']}" if q["name"] != "default" else vname, "video")
    import user_settings as _us
    fmt = await _us.get_output_format(uid)                 # /settings -> Output format
    name = ns.shorten_filename(name + "." + fmt)
    tracker = ns.ProgressTracker(status, "Downloading", name, quality=q["name"])
    deadline = time.time() + TASK_TIMEOUT
    t0 = time.time()

    async def prog(done, total, segs_done, segs_total):
        await tracker.update(done, total)

    video = await download_stream(fetcher, q["url"], os.path.join(folder, "v"), "video", prog, deadline)
    audio = None
    audio_track = find_audio(info["audio_tracks"], q.get("group"), task["sel_lang"], task["sel_name"]) if info["audio_tracks"] else None
    if audio_track:
        try:
            audio = await download_stream(fetcher, audio_track["url"], os.path.join(folder, "a"), "audio", prog, deadline)
        except (URLError, asyncio.CancelledError):
            raise
        except Exception as e:
            logger.warning("audio track failed (%s) - continuing without it", e)
    dl_seconds = time.time() - t0

    await _edit(status, ns.SC(f"<b>🔧 Joining video{' + audio' if audio else ''}...</b>\n<code>{name}</code>"))
    out_path = await mux(video, audio, os.path.join(folder, name))
    for p in (video, audio):
        if p:
            try:
                os.remove(p)
            except OSError:
                pass
    if task.get("compress"):
        await _edit(status, ns.SC("<b>🗜 Compressing to H.265...</b>\n<i>this can take a long time (/cancel to stop)</i>"))
        try:
            hevc = await compress_hevc(out_path)
            os.remove(out_path)
            out_path = hevc
            if fmt == "mkv":
                out_path = await remux_mkv(out_path)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await client.send_message(message.chat.id, ns.SC(f"<b>⚠️ Compression skipped:</b> {e}\n<i>Uploading the original instead.</i>"),
                                      parse_mode=enums.ParseMode.HTML)
    size = os.path.getsize(out_path)
    if size > ns.MAX_FILE_SIZE and not shutil.which("ffmpeg"):
        raise URLError("The video is over 2 GB and ffmpeg is needed to split it.")
    await _uu._upload(client, message, status, out_path, os.path.basename(out_path), size, True,
                      os.path.splitext(out_path)[1].lower(), playlist_url, dl_seconds, uid, quality_label=q["name"])
    return True


# --------------------------------------------------------------------- UI / entry ----
def _kb_quality(ns, sid, qualities):
    rows = []
    if len(qualities) > 1:
        rows.append([ns.make_button(ns.SC(f"📥 All qualities ({len(qualities)})"), callback_data=f"m3qall:{sid}", style=ns.BTN_PRIMARY)])
    for i, q in enumerate(qualities):
        label = f"📺 {q['name']} • {q['resolution']} • {q['bandwidth'] // 1000}kbps" if q["name"] != "default" else "📺 Default"
        rows.append([ns.make_button(label, callback_data=f"m3q:{sid}:{i}", style=ns.BTN_PRIMARY)])
    rows.append([ns.make_button(ns.SC("❌ Cancel"), callback_data=f"m3cancel:{sid}", style=ns.BTN_DANGER)])
    return ns.InlineKeyboardMarkup(rows)


def _kb_audio(ns, sid, tracks):
    rows = [[ns.make_button(f"{'⭐' if is_hindi(t) else '🔊'} {t['display']}" + (f" ({t['name']})" if t.get("name") else ""),
                            callback_data=f"m3a:{sid}:{i}", style=ns.BTN_PRIMARY)] for i, t in enumerate(tracks)]
    rows.append([ns.make_button(ns.SC("❌ Cancel"), callback_data=f"m3cancel:{sid}", style=ns.BTN_DANGER)])
    return ns.InlineKeyboardMarkup(rows)


def _kb_compress(ns, sid):
    return ns.InlineKeyboardMarkup([
        [ns.make_button(ns.SC("⚡ Skip — original quality, faster"), callback_data=f"m3c:{sid}:no", style=ns.BTN_PRIMARY)],
        [ns.make_button(ns.SC("🗜 Compress (H.265, smaller/slower)"), callback_data=f"m3c:{sid}:yes", style=ns.BTN_PRIMARY)],
        [ns.make_button(ns.SC("❌ Cancel"), callback_data=f"m3cancel:{sid}", style=ns.BTN_DANGER)],
    ])


def _parse_args(text: str):
    """-> (urls, name, referer) from the text after /m3u8 (or a pasted message)."""
    name = None
    nm = FLAG_N.search(text or "")
    if nm:
        name = nm.group(1).strip().strip('"').strip() or None
    rf = FLAG_R.search(text or "")
    referer = rf.group(1) if rf else None
    clean = FLAG_R.sub(" ", FLAG_N.sub(" ", text or ""))
    urls = []
    for u in URL_RE.findall(clean):
        u = u.rstrip(").,;'\"")
        if u not in urls:
            urls.append(u)
    return urls, name, referer


async def start_flow(client, message, urls: list, name=None, referer=None):
    ns = _ns
    uid = message.from_user.id
    if await ns.is_banned(uid):
        return await message.reply(ns.SC("🚫 You are banned from using this bot."))
    urls = urls[:MAX_ITEMS]
    status = await message.reply(ns.SC(f"<b>🔎 Analyzing {len(urls)} link(s)...</b>"), parse_mode=enums.ParseMode.HTML)
    headers = {"Referer": referer, "Origin": referer.rstrip("/")} if referer else {}
    try:
        async with Fetcher(headers) as f:
            info = parse_master(await f.text(urls[0]), urls[0])
    except URLError as e:
        return await _edit(status, ns.SC(f"<b>❌ {e}</b>"))
    except Exception as e:
        return await _edit(status, ns.SC(f"<b>❌ Couldn't read the playlist:</b> <code>{str(e)[:200]}</code>"))
    if len(_SESSIONS) > 200:
        for k in sorted(_SESSIONS, key=lambda k: _SESSIONS[k]["ts"])[:50]:
            _SESSIONS.pop(k, None)
    sid = uuid.uuid4().hex[:10]
    _SESSIONS[sid] = {"urls": urls, "qualities": info["qualities"], "audio_tracks": info["audio_tracks"], "is_all": False,
                      "sel_height": None, "sel_lang": "", "sel_name": "", "compress": False, "message": message, "uid": uid,
                      "name": name, "referer": referer, "ts": time.time()}
    tracks = info["audio_tracks"]
    audio_info = (f"🔊 {len(tracks)} audio tracks — choose after quality.\n\n" if len(tracks) > 1
                  else f"🔊 Audio: {tracks[0]['display']}\n\n" if tracks else "🔊 Audio: muxed with video\n\n")
    await _edit_kb(status, ns.SC(f"<b>🚀 Step 1: Select quality</b>\n\n📦 {len(urls)} link(s)\n\n{audio_info}<i>Applies to the whole batch.</i>"),
                   _kb_quality(ns, sid, info["qualities"]))


async def _edit_kb(msg, text, kb):
    try:
        await msg.edit_text(text, reply_markup=kb, parse_mode=enums.ParseMode.HTML)
    except Exception:
        pass


async def maybe_handle(client, message, text: str) -> bool:
    """Hook for link_handler: a message containing a bare .m3u8 link starts the flow. True = handled."""
    if not M3U8_AUTO_RE.search(text or ""):
        return False
    urls, name, referer = _parse_args(text)
    urls = [u for u in urls if M3U8_AUTO_RE.match(u)] or urls
    if not urls:
        return False
    await start_flow(client, message, urls, name, referer)
    return True


def setup(app, ns) -> None:
    global _ns
    _ns = ns

    @app.on_message(filters.command(["m3u8", "hls"]) & filters.private)
    async def m3u8_cmd(client, message):
        parts = (message.text or "").split(None, 1)
        urls, name, referer = _parse_args(parts[1]) if len(parts) > 1 else ([], None, None)
        if not urls:
            return await message.reply(ns.SC(
                "<b>🎞 M3U8 Downloader</b>\n\n<b>Usage:</b>\n<code>/m3u8 &lt;playlist link&gt;</code>\n"
                "<code>/m3u8 link1 link2 link3</code> (batch)\n<code>/m3u8 &lt;link&gt; -n MyVideo</code> (rename, single link)\n"
                "<code>/m3u8 &lt;link&gt; -r https://site.com/</code> (stream needs a Referer)\n\n"
                "<i>You can also just paste a .m3u8 link. /cancel stops a running download.</i>"), parse_mode=enums.ParseMode.HTML)
        await start_flow(client, message, urls, name, referer)

    def _owned(cq, sid):
        s = _SESSIONS.get(sid)
        return s if s and s["uid"] == cq.from_user.id else None

    @app.on_callback_query(filters.regex(r"^m3cancel:([a-f0-9]+)$"))
    async def _cancel(client, cq):
        _SESSIONS.pop(cq.matches[0].group(1), None)
        await _edit(cq.message, ns.SC("❌ Cancelled."))
        await cq.answer()

    async def _after_quality(client, cq, sid, is_all, height):
        s = _owned(cq, sid)
        if not s:
            return await cq.answer("Expired - send the link again.", show_alert=True)
        s["is_all"], s["sel_height"] = is_all, height
        await cq.answer("Quality selected.")
        tracks = s["audio_tracks"]
        if len(tracks) > 1:
            return await _edit_kb(cq.message, ns.SC("<b>🚀 Step 2: Select audio track</b>\n\n⭐ marks Hindi tracks."), _kb_audio(ns, sid, tracks))
        if tracks:
            s["sel_lang"], s["sel_name"] = tracks[0]["language"], tracks[0]["name"]
        await _ask_compress(cq, sid)

    async def _ask_compress(cq, sid):
        await _edit_kb(cq.message, ns.SC(
            "<b>🚀 Step 3: Compress?</b>\n\n🗜 <b>Compress</b> re-encodes to H.265 — smaller file but much slower "
            f"(capped at {COMPRESS_TIMEOUT // 60} min, falls back to the original).\n⚡ <b>Skip</b> uploads the stream as-is — faster."),
            _kb_compress(ns, sid))

    @app.on_callback_query(filters.regex(r"^m3qall:([a-f0-9]+)$"))
    async def _q_all(client, cq):
        await _after_quality(client, cq, cq.matches[0].group(1), True, None)

    @app.on_callback_query(filters.regex(r"^m3q:([a-f0-9]+):(\d+)$"))
    async def _q_one(client, cq):
        sid, idx = cq.matches[0].group(1), int(cq.matches[0].group(2))
        s = _owned(cq, sid)
        if not s:
            return await cq.answer("Expired - send the link again.", show_alert=True)
        try:
            height = s["qualities"][idx]["height"]
        except IndexError:
            return await cq.answer("That button is stale - send the link again.", show_alert=True)
        await _after_quality(client, cq, sid, False, height)

    @app.on_callback_query(filters.regex(r"^m3a:([a-f0-9]+):(\d+)$"))
    async def _a_one(client, cq):
        sid, idx = cq.matches[0].group(1), int(cq.matches[0].group(2))
        s = _owned(cq, sid)
        if not s:
            return await cq.answer("Expired - send the link again.", show_alert=True)
        try:
            t = s["audio_tracks"][idx]
        except IndexError:
            return await cq.answer("That button is stale - send the link again.", show_alert=True)
        s["sel_lang"], s["sel_name"] = t["language"], t["name"]
        await cq.answer("Audio selected.")
        await _ask_compress(cq, sid)

    @app.on_callback_query(filters.regex(r"^m3c:([a-f0-9]+):(yes|no)$"))
    async def _c_pick(client, cq):
        sid, choice = cq.matches[0].group(1), cq.matches[0].group(2)
        s = _owned(cq, sid)
        if not s:
            return await cq.answer("Expired - send the link again.", show_alert=True)
        s["compress"] = choice == "yes"
        _SESSIONS.pop(sid, None)
        await cq.answer("Starting...")
        quality_txt = "ALL" if s["is_all"] else f"{s['sel_height']}p" if s["sel_height"] else "best"
        await _edit(cq.message, ns.SC(f"<b>✅ Quality: {quality_txt} · {'🗜 H.265' if s['compress'] else '⚡ Original'} · 📦 {len(s['urls'])} link(s)</b>\n\n<i>Starting...</i>"))
        asyncio.create_task(process_batch(client, s["message"], s))

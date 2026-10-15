"""Facebook fallback downloader (ported from the reference "src" project's Akbots/facebook.py).

Used by ytdlp_downloader.download_video ONLY when yt-dlp fails on a facebook.com / fb.watch link:
the page HTML is fetched directly, the DASH video + audio stream URLs are read out of it, both are
downloaded (with progress), merged with ffmpeg (-c copy, AAC re-encode fallback) and validated with ffprobe.
Private/friends-only videos need FB_COOKIES (Netscape cookies.txt), same as the yt-dlp path."""
import http.cookiejar
import json
import logging
import os
import re
import subprocess
import time

import requests

import config

logger = logging.getLogger(__name__)

FB_RE = re.compile(r"(^|//|\.)(facebook\.com|fb\.watch|fb\.com)(/|$)", re.I)
DASH_RE = re.compile(r'"dash_prefetch_experimental":\[(.*?)\]')
_FETCH_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}
_DL_HEADERS = {"user-agent": _FETCH_HEADERS["User-Agent"]}
_HTML_SIGS = (b"<!doctype", b"<html", b"<head", b"<body")


def is_facebook(url: str) -> bool:
    return bool(url and FB_RE.search(url))


def _cookies():
    path = getattr(config, "FB_COOKIES", "")
    if not path or not os.path.exists(path):
        return None
    try:
        raw = open(path, "r", encoding="utf-8", errors="ignore").read().replace("#HttpOnly_", "")
        tmp = path + ".parsed.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(raw)
        jar = http.cookiejar.MozillaCookieJar(tmp)
        jar.load(ignore_discard=True, ignore_expires=True)
        os.remove(tmp)
        return jar if len(jar) else None
    except Exception:
        return None


def _stream_links(link: str):
    cookies = _cookies()
    try:
        resp = requests.get(link, headers=_FETCH_HEADERS, cookies=cookies, timeout=30, allow_redirects=True)
    except Exception as e:
        raise RuntimeError(f"Facebook: failed to fetch page: {e}")
    html = resp.text
    found = DASH_RE.findall(html)
    if not found:
        hint = "" if cookies else " If it is private/friends-only, set FB_COOKIES to a cookies.txt file."
        raise RuntimeError("Facebook: no video stream data in the page (private, expired, or a login wall)." + hint)
    try:
        sources = json.loads(f"[{found[0].strip()}]")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Facebook: could not parse stream sources: {e}")
    if len(sources) < 2:
        raise RuntimeError(f"Facebook: expected video+audio sources, got {len(sources)}.")

    def pick(rep):
        try:
            return html.split(f'"representation_id":"{rep}"')[1].split('"base_url":"')[1].split('"')[0].replace("\\", "")
        except IndexError:
            raise RuntimeError("Facebook: could not extract a stream URL.")
    return pick(sources[0]), pick(sources[1])


def _fetch_file(url: str, dest: str, on_bytes) -> None:
    r = requests.get(url, headers=_DL_HEADERS, stream=True, timeout=60)
    r.raise_for_status()
    ctype = r.headers.get("Content-Type", "").lower()
    if "text/html" in ctype or "text/plain" in ctype:
        raise RuntimeError("Facebook served an HTML page instead of media (link expired / login wall).")
    total_len = int(r.headers.get("Content-Length") or 0)
    written, first = 0, None
    with open(dest, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            if first is None:
                first = chunk
            f.write(chunk)
            written += len(chunk)
            on_bytes(len(chunk))
    if written < 10 * 1024:
        raise RuntimeError(f"Facebook download too small ({written} bytes).")
    if first and any(first[:300].lstrip().lower().startswith(s) for s in _HTML_SIGS):
        raise RuntimeError("Facebook served an HTML page instead of media.")
    return total_len


def _ok_media(path: str) -> bool:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=30).stdout
        return "video" in out
    except Exception:
        return False


def _merge(v: str, a: str, out: str) -> None:
    for extra in (["-c", "copy"], ["-c:v", "copy", "-c:a", "aac", "-b:a", "128k"]):
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", v, "-i", a, *extra, "-y", out]
        try:
            r = subprocess.run(cmd, timeout=600, capture_output=True, text=True)
        except FileNotFoundError:
            raise RuntimeError("ffmpeg is not installed on this host.")
        if r.returncode == 0 and os.path.exists(out) and _ok_media(out):
            return
    raise RuntimeError("Facebook: ffmpeg merge failed.")


def download_video(video_url: str, out_path: str, on_progress=None) -> tuple[str, float]:
    """Returns (out_path, elapsed_seconds) - same shape as ytdlp_downloader.download_video."""
    start = time.time()
    out_dir = os.path.dirname(out_path) or "."
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(out_path)[0]
    vp, ap = base + ".fbv.tmp", base + ".fba.tmp"
    if on_progress:
        on_progress({"pct": 0, "downloaded_bytes": 0, "speed_bytes_s": 0, "eta_s": 0, "elapsed_s": 0,
                     "duration_s": None, "connecting": True})
    vurl, aurl = _stream_links(video_url)
    done = {"n": 0}

    def on_bytes(n):
        done["n"] += n
        if on_progress:
            el = max(time.time() - start, 0.001)
            on_progress({"pct": None, "downloaded_bytes": done["n"], "total_bytes": None,
                         "speed_bytes_s": done["n"] / el, "eta_s": None, "elapsed_s": el,
                         "duration_s": None, "connecting": False})
    try:
        _fetch_file(vurl, vp, on_bytes)
        _fetch_file(aurl, ap, on_bytes)
        _merge(vp, ap, out_path)
    finally:
        for p in (vp, ap):
            try:
                os.remove(p)
            except OSError:
                pass
    return out_path, time.time() - start

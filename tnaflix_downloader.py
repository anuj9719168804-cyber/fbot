"""
tnaflix.com video downloader engine.

Why this exists: tnaflix used to fall through to yt-dlp's generic path, which does not
resolve this site's current player. The page's own player loads its sources from an AJAX
endpoint instead:

    GET https://tnaflix.com/ajax/video-player/<id>   ->  {"html": "<video> <source src=... size=1080> ..."}

so this module does exactly what the standalone tnaflix script does, but with the same
contract as mat6tube_downloader / faphouse_downloader (get_available_qualities /
download_video / get_page_meta), so main.py needs no tnaflix-specific logic besides
picking this backend.
"""

import html as _html
import logging
import http.client
import os
import re
import time
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://www.tnaflix.com"
_AJAX_URL = "https://tnaflix.com/ajax/video-player/{id}"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
# The standalone script works with the bare "Mozilla/5.0" agent, so it is kept as a second try
# in case the full browser string is the one being rejected.
_UA_FALLBACKS = (_UA, "Mozilla/5.0")

_ID_RE = re.compile(r"video(\d+)", re.IGNORECASE)
_SOURCE_TAG_RE = re.compile(r"<source\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r'([a-zA-Z_:-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\')')
_URL_RE = re.compile(r"https?://\S+")


def is_tnaflix_link(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
        return host == "tnaflix.com" or host.endswith(".tnaflix.com")
    except Exception:
        return False


def _video_id_from_url(video_url: str) -> str | None:
    # Only the path counts, so a "video123" in a query string / site name can't be mistaken for an id.
    m = _ID_RE.search(urlparse(video_url).path)
    return m.group(1) if m else None


def extract_tnaflix_links(text: str) -> list[str]:
    """Same contract as faphouse_downloader.extract_faphouse_links() — video pages only (not listings)."""
    if not text:
        return []
    seen, out = set(), []
    for match in _URL_RE.findall(text):
        url = match.rstrip(").,!?>'\"")
        if is_tnaflix_link(url) and _video_id_from_url(url) and url not in seen:
            seen.add(url)
            out.append(url)
    return out


# ── AJAX payload cache (get_available_qualities() + get_page_meta() run back-to-back) ──────
_DATA_CACHE: dict = {}
_DATA_CACHE_TTL = 300


def _fetch_player_data(video_id: str, video_url: str, force: bool = False) -> dict:
    now = time.time()
    if not force:
        cached = _DATA_CACHE.get(video_id)
        if cached and now - cached[0] < _DATA_CACHE_TTL:
            return cached[1]
    last_err: Exception | None = None
    for ua in _UA_FALLBACKS:
        try:
            r = requests.get(
                _AJAX_URL.format(id=video_id), timeout=15,
                headers={"User-Agent": ua, "Referer": video_url, "X-Requested-With": "XMLHttpRequest",
                         "Accept": "application/json, text/plain, */*"},
            )
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and data.get("html"):
                _DATA_CACHE[video_id] = (now, data)
                if len(_DATA_CACHE) > 300:
                    for k in list(_DATA_CACHE.keys())[:50]:
                        _DATA_CACHE.pop(k, None)
                return data
            last_err = RuntimeError("tnaflix: player endpoint returned no HTML (video removed or private?)")
        except Exception as e:  # network / HTTP / JSON errors: try the next User-Agent
            last_err = e
    raise RuntimeError(f"tnaflix: couldn't load the player data for video {video_id}: {last_err}")


def _absolute(src: str) -> str:
    src = _html.unescape(src.strip())
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return BASE_URL + src
    return src


def _parse_sources(html_text: str) -> list[dict]:
    """<source src=".." type="video/mp4" size="1080"> in ANY attribute order -> [{label, height, url}] best first."""
    variants, seen = [], set()
    for tag in _SOURCE_TAG_RE.findall(html_text or ""):
        attrs = {k.lower(): (v1 or v2) for k, v1, v2 in _ATTR_RE.findall(tag)}
        src = attrs.get("src")
        if not src:
            continue
        typ = (attrs.get("type") or "video/mp4").lower()
        if "mp4" not in typ:
            continue  # skip HLS/other tags; this backend downloads progressive MP4 only
        url = _absolute(src)
        size_raw = re.sub(r"\D", "", attrs.get("size") or attrs.get("res") or attrs.get("label") or "")
        height = int(size_raw) if size_raw else None
        if (url, height) in seen:
            continue
        seen.add((url, height))
        variants.append({"label": f"{height}p" if height else "Best available", "height": height, "url": url})
    variants.sort(key=lambda v: v["height"] or 0, reverse=True)
    return variants


def get_available_qualities(video_url: str) -> list:
    """[{"label": "1080p", "height": 1080, "url": <direct mp4>}, ...], best first."""
    video_id = _video_id_from_url(video_url)
    if not video_id:
        raise RuntimeError(f"tnaflix: video id not found in {video_url!r} (expects '.../video12345')")
    variants = _parse_sources(_fetch_player_data(video_id, video_url).get("html", ""))
    if not variants:
        raise RuntimeError(f"tnaflix: no playable MP4 sources found for {video_url}")
    return variants


class _IncompleteDownload(Exception):
    pass


class _EmptyResponse:
    """Stand-in after a failed reconnect so the retry loop's next pass raises a retryable error and tries again."""
    status_code = 0

    def iter_content(self, chunk_size=0):
        raise _IncompleteDownload("reconnect failed")

    def close(self):
        pass


_MAX_RESUMES = 8
_RETRYABLE = (requests.exceptions.ChunkedEncodingError, requests.exceptions.ConnectionError,
              requests.exceptions.Timeout, _IncompleteDownload, http.client.IncompleteRead)


def download_video(video_url: str, out_path: str, on_progress=None, stream_url: str = None) -> tuple[str, float]:
    """Same contract as faphouse_downloader.download_video(): plain progressive-MP4 download.
    If the chosen link was already used up/expired (403/404/410), the links are re-resolved once."""
    video_id = _video_id_from_url(video_url)
    target = stream_url or get_available_qualities(video_url)[0]["url"]
    start_time = time.time()
    headers = {"User-Agent": _UA, "Referer": BASE_URL + "/"}

    def _open(url: str, offset: int = 0) -> requests.Response:
        h = dict(headers)
        if offset:
            h["Range"] = f"bytes={offset}-"
        return requests.get(url, stream=True, timeout=30, headers=h)

    def _refresh_target(stale: str) -> str | None:
        """The stored link was used up/expired (403/404/410): re-resolve once, keeping the same quality if offered."""
        if not video_id:
            return None
        old = _DATA_CACHE.get(video_id)
        old_height = next((v["height"] for v in _parse_sources(old[1].get("html", "")) if v["url"] == stale), None) if old else None
        fresh = _parse_sources(_fetch_player_data(video_id, video_url, force=True).get("html", ""))
        if not fresh:
            return None
        return next((v for v in fresh if old_height and v["height"] == old_height), fresh[0])["url"]

    r = _open(target)
    try:
        if r.status_code in (403, 404, 410):
            r.close()
            logger.info(f"tnaflix: HTTP {r.status_code} on the stored link — re-resolving once")
            new_target = _refresh_target(target)
            if new_target:
                target = new_target
                r = _open(target)
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        downloaded = 0
        attempt = 0
        last_pos = 0
        mode = "wb"
        while True:
            try:
                with open(out_path, mode) as f:
                    for chunk in r.iter_content(chunk_size=1024 * 256):
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        if on_progress:
                            elapsed = time.time() - start_time
                            on_progress({
                                "pct": (downloaded / total * 100) if total else None,
                                "downloaded_bytes": downloaded,
                                "speed_bytes_s": downloaded / elapsed if elapsed > 0 else 0,
                                "eta_s": ((total - downloaded) / (downloaded / elapsed)) if (total and downloaded and elapsed > 0) else None,
                                "elapsed_s": elapsed,
                                "duration_s": 0,
                                "total_bytes": total or None,
                            })
                if total and downloaded < total:        # server closed early without an error
                    raise _IncompleteDownload(f"{downloaded} of {total} bytes")
                break
            except _RETRYABLE as e:
                # The CDN dropped the connection mid-file (IncompleteRead / reset / timeout): resume from where we
                # stopped with a Range request instead of failing a 180 MB download at 90%.
                r.close()
                if downloaded > last_pos:           # it moved forward since the last break: only stalls count
                    attempt = 0
                last_pos = downloaded
                attempt += 1
                if attempt > _MAX_RESUMES:
                    raise RuntimeError(f"tnaflix: connection kept breaking ({downloaded}"
                                       f"{'/' + str(total) if total else ''} bytes downloaded, stalled {_MAX_RESUMES} times in a row): {e}")
                logger.warning(f"tnaflix: connection broken at {downloaded} bytes ({e}) — resuming "
                               f"(attempt {attempt}/{_MAX_RESUMES})")
                time.sleep(min(2 * attempt, 15))
                try:
                    r = _open(target, downloaded)
                    if r.status_code in (403, 404, 410):        # link expired meanwhile
                        r.close()
                        new_target = _refresh_target(target)
                        if not new_target:
                            raise RuntimeError("tnaflix: download link expired and couldn't be refreshed")
                        target = new_target
                        r = _open(target, downloaded)
                    if r.status_code == 206 and downloaded:
                        mode = "ab"                             # server honoured the Range -> append
                    elif r.status_code == 200:                  # Range ignored -> start over from byte 0
                        downloaded, mode = 0, "wb"
                        total = int(r.headers.get("Content-Length") or total or 0)
                    elif r.status_code == 416 and total and downloaded >= total:
                        r.close()
                        break                                   # already complete
                    else:
                        r.raise_for_status()
                        mode = "ab"
                except _RETRYABLE as e2:
                    # couldn't even reconnect right now; loop again (counts as another attempt)
                    logger.warning(f"tnaflix: reconnect failed ({e2})")
                    r = _EmptyResponse()
                    mode = "ab" if downloaded else "wb"        # never truncate what we already have
    finally:
        try:
            r.close()
        except Exception:
            pass

    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError("Download finished but the output file is missing/empty.")
    return out_path, time.time() - start_time


def _meta(html_text: str, prop: str) -> str | None:
    m = (re.search(r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]+content=["\']([^"\']*)["\']' % re.escape(prop), html_text, re.I)
         or re.search(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']%s["\']' % re.escape(prop), html_text, re.I))
    return _html.unescape(m.group(1)).strip() if m else None


def get_page_meta(video_url: str) -> dict:
    """Same contract as faphouse_downloader.get_page_meta(). Title/poster come from the watch page when it can be
    fetched; otherwise the title falls back to the URL slug (downloads never depend on this)."""
    meta = {"title": None, "author": None, "duration": None, "poster_url": None,
            "view_count": None, "like_count": None, "comment_count": None, "upload_date": None}
    try:
        r = requests.get(video_url, timeout=15, headers={"User-Agent": _UA, "Referer": BASE_URL + "/"})
        r.raise_for_status()
        page = r.text
        meta["title"] = _meta(page, "og:title")
        meta["poster_url"] = _meta(page, "og:image")
        dur = _meta(page, "video:duration")
        if dur and dur.isdigit():
            meta["duration"] = int(dur)
    except Exception as e:
        logger.warning(f"tnaflix get_page_meta failed for {video_url}: {e}")
    if not meta["title"]:
        parts = [p for p in urlparse(video_url).path.split("/") if p]
        slug = next((p for p in reversed(parts) if not _ID_RE.fullmatch(p)), "")
        meta["title"] = slug.replace("-", " ").strip().title() or None
    return meta

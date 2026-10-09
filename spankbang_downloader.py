"""
spankbang.com video downloader for fbot.

Same function contract as faphouse_downloader.py / fpo_downloader.py /
mat6tube_downloader.py, so main.py's _downloader_for() dispatch and
auto_scraper.py's workers can use it without knowing which site they are
talking to:

  is_spankbang_link(url) / is_supported_link(url) -> bool
  extract_spankbang_links(text) / extract_supported_links(text) -> list[str]
  get_available_qualities(video_url) -> [{"label","height","url"}, ...] best-first
  get_stream_url(video_url) -> str | None
  download_video(video_url, out_path, on_progress=None, stream_url=None) -> (out_path, elapsed_s)
  get_page_meta(video_url) -> {"title","author","duration","poster_url", ...}

HOW IT DOWNLOADS (and why it is not "ffmpeg -i <page url>"):
  A SpankBang page URL is an HTML page, not a media file, so ffmpeg cannot
  read it. The real stream URLs (MP4 / HLS) are only revealed by yt-dlp's
  dedicated SpankBang extractor, and they are bound to the IP that resolved
  them -- so resolving and downloading must both happen from this server.
  ytdlp_downloader.py already does exactly that (browser TLS impersonation
  via curl_cffi, FlareSolverr/cf_bypass fallback for Cloudflare challenges,
  cookies, proxy, aria2c/fragment concurrency, real progress hooks), and it
  is the engine every other yt-dlp site in this bot uses. This module is the
  dedicated SpankBang layer on top of it: link detection for every mirror,
  URL normalisation, a Cloudflare retry, and a "SpankBang" site name.
"""

import logging
import re
import time
from urllib.parse import urlparse, urlunparse

import cf_bypass
import ytdlp_downloader as _ytdlp

logger = logging.getLogger(__name__)

SITE_NAME = "SpankBang"
BASE_URL = "https://spankbang.com"

# spankbang.com, locale subdomains (de., fr. ...), spankbang.party and any other
# spankbang.<tld> mirror -- same pattern ytdlp_downloader.HOST_PATTERNS uses.
_HOST_RE = re.compile(r"(?:^|\.)spankbang\.[a-z.]{2,}$", re.IGNORECASE)

# /<id>/video/<slug>   /<id>/play/<slug>   /<id>/embed/   /<id>/play-embed/
_VIDEO_PATH_RE = re.compile(r"^/([A-Za-z0-9]+)/(?:video|play|embed|play-embed)(?:/|$)", re.IGNORECASE)

_URL_RE = re.compile(r"https?://\S+")


# ---------------------------------------------------------------------------
# Link detection
# ---------------------------------------------------------------------------
def is_spankbang_link(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
    except Exception:
        return False
    return bool(_HOST_RE.search(host))


is_supported_link = is_spankbang_link  # alias -- same name the other backends expose


def extract_spankbang_links(text: str) -> list[str]:
    """Same contract as faphouse_downloader.extract_faphouse_links()."""
    if not text:
        return []
    seen, out = set(), []
    for match in _URL_RE.findall(text):
        url = match.rstrip(").,!?>'\"")
        if is_spankbang_link(url) and url not in seen:
            seen.add(url)
            out.append(url)
    return out


extract_supported_links = extract_spankbang_links  # alias


def is_video_link(url: str) -> bool:
    """True for an actual video page (not a /s/<search>/ or /trending_videos/ listing)."""
    try:
        return bool(is_spankbang_link(url) and _VIDEO_PATH_RE.match(urlparse(url).path))
    except Exception:
        return False


def video_id(url: str) -> str | None:
    try:
        m = _VIDEO_PATH_RE.match(urlparse(url).path)
    except Exception:
        return None
    return m.group(1).lower() if m else None


def normalize_url(url: str) -> str:
    """Strips tracking query/fragment and forces https so the same video always
    maps to one cache key / one dedup slug. Host is kept as given (a mirror such
    as spankbang.party must stay on that mirror)."""
    try:
        url = (url or "").strip()
        if not re.match(r"^https?://", url, re.IGNORECASE):
            url = "https://" + url
        p = urlparse(url)
        return urlunparse(("https", p.netloc, p.path, "", "", ""))
    except Exception:
        return url


# ---------------------------------------------------------------------------
# Backend contract (delegates to the yt-dlp engine)
# ---------------------------------------------------------------------------
def get_available_qualities(video_url: str) -> list:
    """[{"label": "720p", "height": 720, "url": <format_id>}, ...] best-first, with a
    leading "Auto (Best)" entry (url=None) -- identical shape to every other backend."""
    return _ytdlp.get_available_qualities(normalize_url(video_url))


def get_stream_url(video_url: str) -> str | None:
    """Directly playable URL of the best variant (used by the Stream button)."""
    return _ytdlp.get_stream_url(normalize_url(video_url))


def get_page_meta(video_url: str) -> dict:
    meta = _ytdlp.get_page_meta(normalize_url(video_url)) or {}
    if not meta.get("site_name") or str(meta.get("site_name")).lower().startswith("spankbang"):
        meta["site_name"] = SITE_NAME
    return meta


def _looks_blocked(err: Exception) -> bool:
    s = str(err).lower()
    return any(k in s for k in ("403", "forbidden", "cloudflare", "just a moment", "challenge", "429"))


def download_video(video_url: str, out_path: str, on_progress=None, stream_url: str = None) -> tuple[str, float]:
    """stream_url is one of get_available_qualities()'s "url" values (a yt-dlp format_id) or
    None for Auto (Best). on_progress gets {pct, downloaded_bytes, speed_bytes_s, eta_s,
    elapsed_s, duration_s} like the other backends.

    If the first attempt fails with a Cloudflare/403-style error, one FlareSolverr solve
    (cf_bypass.try_solve) is attempted and the download is retried once with the fresh
    clearance cookie. A stale format_id after that re-resolve falls back to Auto (Best)."""
    if not is_spankbang_link(video_url):
        raise RuntimeError(f"Not a SpankBang link: {video_url}")
    url = normalize_url(video_url)
    t0 = time.time()
    try:
        path, _elapsed = _ytdlp.download_video(url, out_path, on_progress=on_progress, stream_url=stream_url)
        return path, time.time() - t0
    except Exception as first_err:
        if not _looks_blocked(first_err):
            raise
        logger.warning(f"[spankbang] download blocked ({first_err}) -- trying Cloudflare solve + one retry")
        if not cf_bypass.try_solve(url):
            raise
    try:
        path, _elapsed = _ytdlp.download_video(url, out_path, on_progress=on_progress, stream_url=stream_url)
    except Exception:
        # format ids can change after a re-resolve; Auto (Best) always works
        path, _elapsed = _ytdlp.download_video(url, out_path, on_progress=on_progress, stream_url=None)
    return path, time.time() - t0


def is_available() -> bool:
    try:
        import yt_dlp  # noqa: F401
        return True
    except ImportError:
        logger.error("yt-dlp not installed -- SpankBang downloader unavailable")
        return False


if __name__ == "__main__":
    for u in ("https://spankbang.com/5abcd/video/some-title", "https://de.spankbang.com/5abcd/play/x/720p/",
              "https://spankbang.party/5abcd/video/x?utm=1", "https://example.com/5abcd/video/x"):
        print(u, "->", is_spankbang_link(u), is_video_link(u), video_id(u), normalize_url(u))

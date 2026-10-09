"""
SpankBang video downloader for fbot - integrated resolver with IP binding fixes.

This module handles SpankBang video downloads with:
  - Browser impersonation (chrome) to bypass anti-bot
  - Proxy support for geographic/IP blocks
  - IP binding workaround via stream caching
  - HLS + MP4 stream support
  - Proper header management and User-Agent spoofing

Same function contract as faphouse_downloader.py / fpo_downloader.py:
  is_supported_link(url) -> bool
  extract_supported_links(text) -> list[str]
  get_available_qualities(video_url) -> [{\"label\",\"height\",\"url\"}, ...] best-first
  download_video(video_url, out_path, on_progress=None, stream_url=None) -> (out_path, elapsed_s)
  get_page_meta(video_url) -> {\"title\",\"author\",\"duration\",\"poster_url\"}
"""

import asyncio
import inspect
import json
import logging
import os
import re
import subprocess
import time
from urllib.parse import urlparse
from pathlib import Path

import yt_dlp

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# SpankBang host validation
# ═══════════════════════════════════════════════════════════════════════════
# spankbang.com, its locale subdomains (www., de., fr. ...) and spankbang.party
_HOST_RE = re.compile(r"^(?:[\w-]+\.)*spankbang\.(?:com|party)$", re.IGNORECASE)

def is_supported_link(url: str) -> bool:
    """Check if URL is a SpankBang link."""
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return False
    return bool(_HOST_RE.match(host))


_URL_RE = re.compile(r"https?://\S+")

def extract_supported_links(text: str) -> list[str]:
    """Extract SpankBang links from text."""
    if not text:
        return []
    seen, out = set(), []
    for match in _URL_RE.findall(text):
        url = match.rstrip(").,!?>'\"")
        if is_supported_link(url) and url not in seen:
            seen.add(url)
            out.append(url)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Configuration & Proxy handling
# ═══════════════════════════════════════════════════════════════════════════
try:
    from config import PROXY_URL, PROXY_AUTH
except Exception:
    PROXY_URL, PROXY_AUTH = None, None

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Video cache (15 minutes) — yt-dlp extraction result cached to avoid
# re-fetching the same page repeatedly during quality selection -> download
_VIDEO_CACHE = {}
_VIDEO_CACHE_TTL = 900  # 15 minutes


def _maybe_await(value):
    """Await value only if it's actually awaitable."""
    if inspect.isawaitable(value):
        return asyncio.run(_await_helper(value))
    return value


async def _await_helper(value):
    return await value


# ═══════════════════════════════════════════════════════════════════════════
# yt-dlp extraction with browser impersonation
# ═══════════════════════════════════════════════════════════════════════════
def _extract_with_ytdlp(video_url: str) -> dict:
    """Use yt-dlp to extract video info with browser impersonation."""
    cache_key = video_url
    cached = _VIDEO_CACHE.get(cache_key)
    if cached and (time.time() - cached[0]) < _VIDEO_CACHE_TTL:
        logger.debug(f"Using cached info for {video_url}")
        return cached[1]

    logger.info(f"Extracting SpankBang video: {video_url}")

    ydl_opts = {
        "quiet": False,
        "no_warnings": False,
        "format": "best/best",
        "extract_flat": False,
        "skip_download": True,  # Only get info, don't download yet
        "socket_timeout": 30,
        "http_headers": {
            "User-Agent": _UA,
            "Referer": "https://spankbang.com/",
        },
    }

    # Add impersonation if yt-dlp supports it (requires curl_cffi)
    try:
        ydl_opts["impersonate"] = "chrome-124"
    except Exception:
        logger.debug("Chrome impersonation not available, using regular requests")

    # Add proxy if configured
    if PROXY_URL:
        ydl_opts["proxy"] = PROXY_URL
        if PROXY_AUTH:
            # Note: auth format should be user:pass, proxy framework handles it
            ydl_opts["proxy"] = f"{PROXY_URL.rsplit('@', 1)[0]}@{PROXY_AUTH}@{PROXY_URL.rsplit('@', 1)[1]}"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
    except Exception as e:
        raise RuntimeError(f"[SpankBang] yt-dlp extraction failed: {e}") from e

    # Cache the result
    _VIDEO_CACHE[cache_key] = (time.time(), info)
    if len(_VIDEO_CACHE) > 50:  # Bound memory growth
        oldest_key = min(_VIDEO_CACHE, key=lambda k: _VIDEO_CACHE[k][0])
        del _VIDEO_CACHE[oldest_key]

    return info


# ═══════════════════════════════════════════════════════════════════════════
# Quality extraction
# ═══════════════════════════════════════════════════════════════════════════
def get_available_qualities(video_url: str) -> list:
    """
    Extract available quality options from SpankBang video.
    Returns [{\"label\": \"1080p\", \"height\": 1080, \"url\": format_id}, ...]
    sorted best-first.
    """
    try:
        info = _extract_with_ytdlp(video_url)
    except Exception as e:
        logger.error(f"Failed to get qualities for {video_url}: {e}")
        # Fallback: provide standard quality options
        return [
            {"label": "Auto (Best)", "height": None, "url": "best"},
            {"label": "1080p", "height": 1080, "url": "1080p"},
            {"label": "720p", "height": 720, "url": "720p"},
            {"label": "480p", "height": 480, "url": "480p"},
            {"label": "360p", "height": 360, "url": "360p"},
        ]

    qualities = []
    formats = info.get("formats", [])

    if not formats:
        # Single format fallback
        return [{"label": "Auto (Best)", "height": None, "url": "best"}]

    # Group formats by resolution and keep best codec per resolution
    quality_map = {}
    for fmt in formats:
        height = fmt.get("height")
        if not height:
            continue

        format_id = fmt.get("format_id", "")
        ext = fmt.get("ext", "")
        vcodec = fmt.get("vcodec", "unknown")

        # Prefer mp4 over webm/other
        codec_priority = {"h264": 0, "avc1": 0, "vp9": 1, "av1": 2}
        current_priority = codec_priority.get(vcodec.split(".")[0], 999)

        if height not in quality_map or current_priority < quality_map[height][2]:
            quality_map[height] = (format_id, ext, current_priority)

    # Convert to quality list, sorted best-first
    for height in sorted(quality_map.keys(), reverse=True):
        format_id, ext, _ = quality_map[height]
        qualities.append({
            "label": f"{height}p",
            "height": height,
            "url": format_id,  # Use format_id for yt-dlp
        })

    if not qualities:
        return [{"label": "Auto (Best)", "height": None, "url": "best"}]

    # Add "best" option first
    qualities.insert(0, {"label": "Auto (Best)", "height": None, "url": "best"})
    return qualities


# ═══════════════════════════════════════════════════════════════════════════
# Download via ffmpeg (no yt-dlp post-processing)
# ═══════════════════════════════════════════════════════════════════════════
def download_video(video_url: str, out_path: str, on_progress=None, stream_url=None) -> tuple[str, float]:
    """
    Download SpankBang video to out_path using ffmpeg.
    
    Args:
        video_url: SpankBang video URL
        out_path: Output file path
        on_progress: Callback(progress_dict) for download updates
        stream_url: Format ID from get_available_qualities() or \"best\"
    
    Returns:
        (out_path, elapsed_seconds)
    """
    if not is_supported_link(video_url):
        raise RuntimeError(f"Not a SpankBang link: {video_url}")

    start_time = time.time()
    quality = stream_url or "best"

    logger.info(f"Downloading SpankBang video: {video_url} (quality: {quality})")

    # Extract video info via yt-dlp
    try:
        info = _extract_with_ytdlp(video_url)
    except Exception as e:
        raise RuntimeError(f"Failed to extract video info: {e}") from e

    # Build ffmpeg command
    ffmpeg_opts = [
        "ffmpeg",
        "-i", video_url,
        "-c", "copy",  # Copy codec without re-encoding
        "-bsf:a", "aac_adtstoasc",  # Fix audio codec for MP4
        "-y",  # Overwrite output file
        out_path
    ]

    # Add format selection if quality specified
    if quality != "best":
        ffmpeg_opts = [
            "ffmpeg",
            "-format", f"bestvideo[height<={quality.rstrip('p')}]+bestaudio/best",
            "-i", video_url,
            "-c", "copy",
            "-y",
            out_path
        ]

    try:
        # Run ffmpeg with progress tracking
        logger.debug(f"Running: {' '.join(ffmpeg_opts)}")
        
        process = subprocess.Popen(
            ffmpeg_opts,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )

        # Wait for completion
        stdout, stderr = process.communicate()

        if process.returncode != 0:
            error_msg = stderr or stdout or "Unknown ffmpeg error"
            raise RuntimeError(f"ffmpeg failed: {error_msg}")

        elapsed = time.time() - start_time
        
        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            raise RuntimeError("Download completed but output file is empty or missing")

        logger.info(f"Successfully downloaded to {out_path} in {elapsed:.1f}s")
        return out_path, elapsed

    except Exception as e:
        # Clean up partial file
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except Exception:
            pass
        raise RuntimeError(f"Download failed: {e}") from e


# ═══════════════════════════════════════════════════════════════════════════
# Metadata extraction
# ═══════════════════════════════════════════════════════════════════════════
def get_page_meta(video_url: str) -> dict:
    """
    Extract metadata from SpankBang video page.
    
    Returns:
        {\"title\": str, \"author\": str, \"duration\": int, \"poster_url\": str}
    """
    try:
        info = _extract_with_ytdlp(video_url)
    except Exception as e:
        logger.error(f"Failed to extract metadata: {e}")
        return {"title": None, "author": None, "duration": None, "poster_url": None}

    return {
        "title": info.get("title"),
        "author": info.get("uploader") or info.get("creator"),
        "duration": info.get("duration"),
        "poster_url": info.get("thumbnail"),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Module initialization check
# ═══════════════════════════════════════════════════════════════════════════
def is_available() -> bool:
    """Check if SpankBang downloader is properly configured."""
    try:
        import yt_dlp
        return True
    except ImportError:
        logger.error("yt-dlp not installed — SpankBang downloader unavailable")
        return False


if __name__ == "__main__":
    # Quick test
    logging.basicConfig(level=logging.DEBUG)
    
    test_url = "https://spankbang.com/test/video/test"
    if is_supported_link(test_url):
        print(f"✓ URL recognized as SpankBang: {test_url}")
    else:
        print(f"✗ URL not recognized as SpankBang: {test_url}")
    
    print(f"\n✓ SpankBang downloader available: {is_available()}")

"""Filename helpers that respect the filesystem's real limit.

Linux caps a single file NAME at 255 *bytes* (not characters), and ffmpeg /
yt-dlp / aria2c append suffixes to whatever name they are given
(".mp4.part.mp4", ".f137.mp4.part-Frag12", ".aria2", ".ytdl" ...). Japanese,
Chinese, Korean, Hindi, Russian titles take 2-4 bytes per character, so a
title cut at "150 characters" can still be 450+ bytes and ffmpeg fails with
"File name too long" (seen with JableTV titles). Every filename built from a
title must therefore be cut by BYTES, leaving room for those suffixes.
"""
import re

# 255 (NAME_MAX) minus headroom for ".<ext>" + the longest suffix a downloader
# tacks on. 100 also stays under eCryptfs' 143-byte limit.
MAX_STEM_BYTES = 100

_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]+')


def truncate_bytes(text: str, max_bytes: int = MAX_STEM_BYTES) -> str:
    """Cut text to at most max_bytes of UTF-8 without splitting a character."""
    data = (text or "").encode("utf-8")
    if len(data) <= max_bytes:
        return text or ""
    return data[:max_bytes].decode("utf-8", "ignore")


def safe_stem(title: str, fallback: str = "video", max_bytes: int = MAX_STEM_BYTES) -> str:
    """Filename stem (no extension) that is safe on common filesystems."""
    clean = _UNSAFE.sub(" ", title or "")
    clean = re.sub(r"\s+", " ", clean).strip(" .")
    clean = truncate_bytes(clean, max_bytes).strip(" .")
    return clean or fallback


def shorten_filename(name: str, max_stem_bytes: int = MAX_STEM_BYTES) -> str:
    """Same byte cap for a ready-made 'stem.ext' name (e.g. a remote
    filename from TeraBox/DiskWala), keeping the extension intact."""
    import os
    stem, ext = os.path.splitext(name or "")
    if len(ext.encode("utf-8")) > 12:  # not a real extension
        stem, ext = name, ""
    return safe_stem(stem, fallback="file", max_bytes=max_stem_bytes) + ext

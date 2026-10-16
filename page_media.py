"""Pure page-scanning helpers for /extract: find playable media in any web page's HTML.

No network and no bot imports here, so it can be unit-tested on its own. page_extract.py does the fetching,
the browser fallback and the Telegram side.

What it finds, for ANY site (not a hard-coded list):
  * HLS (.m3u8) and DASH (.mpd) manifests, in attributes, inline <script> / JSON, or bare in the text
  * direct media files (.mp4 .webm .mkv .mp3 ...) and downloadable files (.zip .pdf ...)
  * relative paths too ("/media/720.mp4" inside a JS player config) - resolved against the page URL
  * embedded player pages (<iframe src>), so the caller can scan one level deeper
Escaped JSON (\\/ and \\u002F) is unescaped first, so "https:\\/\\/cdn.x\\/a.m3u8" is found as well.
"""
import html as html_lib
import re
from urllib.parse import urljoin, urlsplit

VIDEO_EXTS = {"mp4", "webm", "m4v", "mov", "mkv", "avi", "flv", "3gp"}
AUDIO_EXTS = {"mp3", "m4a", "flac", "ogg", "opus", "wav", "aac"}
FILE_EXTS = {"zip", "rar", "7z", "pdf", "apk", "exe", "iso"}
STREAM_EXTS = {"m3u8", "mpd"}
ALL_EXTS = STREAM_EXTS | VIDEO_EXTS | AUDIO_EXTS | FILE_EXTS
_EXT_ALT = "|".join(sorted(ALL_EXTS, key=len, reverse=True))

# Things that are never the page's media: ad networks and trackers. Same spirit as browser_sniff's list.
AD_HOST_PARTS = (
    "doubleclick", "googlesyndication", "google-analytics", "googletagmanager", "adnxs", "exoclick",
    "trafficjunky", "juicyads", "popads", "histats", "yandex", "facebook.net", "adsterra", "propellerads",
    "mgid", "taboola", "outbrain",
)

# Quoted strings (HTML attributes, JS, JSON) that end in a media extension. Relative or absolute.
_QUOTED_RE = re.compile(
    r"""["'`]([^"'`\s<>]{3,2048}?\.(?:%s)(?:\?[^"'`\s<>]*)?)["'`]""" % _EXT_ALT, re.I)
# Bare absolute URLs in running text.
_BARE_RE = re.compile(
    r"""https?://[^\s"'`<>\\()\[\]{}|^]{3,2048}?\.(?:%s)(?![A-Za-z0-9])(?:\?[^\s"'`<>\\()\[\]{}|^]*)?"""
    % _EXT_ALT, re.I)
_IFRAME_RE = re.compile(r"""<iframe\b[^>]*?\b(?:data-)?src\s*=\s*["']([^"']+)["']""", re.I)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)

_RES = {144, 240, 360, 480, 540, 720, 1080, 1440, 2160, 4320}
_RES_RE = re.compile(r"(?<![\d])(\d{3,4})p(?![A-Za-z])", re.I)
_LABEL_RE = re.compile(r"""(?:label|quality|res|height)["']?\s*[:=]\s*["']?(\d{3,4})p?""", re.I)

_KIND_RANK = {"hls": 0, "dash": 1, "video": 2, "audio": 3, "file": 4, "page": 5}


def _ext_of(url: str) -> str:
    last = urlsplit(url).path.rsplit("/", 1)[-1].lower()
    return last.rsplit(".", 1)[-1] if "." in last else ""


def kind_of(url: str) -> str:
    """'hls' | 'dash' | 'video' | 'audio' | 'file' | '' (not a media/download link)."""
    if not url or not url.startswith(("http://", "https://")):
        return ""
    ext = _ext_of(url)
    if ext == "m3u8":
        return "hls"
    if ext == "mpd":
        return "dash"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in FILE_EXTS:
        return "file"
    return ""


def key_of(url: str) -> str:
    """Dedupe key: the URL without query string or fragment (same file, different tokens)."""
    return url.split("#", 1)[0].split("?", 1)[0]


def _ok_url(url: str) -> bool:
    if not url.startswith(("http://", "https://")) or len(url) > 2000:
        return False
    low = url.lower()
    return not any(part in low for part in AD_HOST_PARTS)


def _height_near(url: str, window: str) -> int:
    for text in (url, window):
        for rx in (_RES_RE, _LABEL_RE):
            for m in rx.finditer(text or ""):
                h = int(m.group(1))
                if h in _RES:
                    return h
    return 0


def _unescape_source(raw: str) -> str:
    return (raw.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
               .replace("\\u0026", "&"))


def page_title(text: str):
    m = _TITLE_RE.search(text or "")
    if not m:
        return None
    t = re.sub(r"\s+", " ", html_lib.unescape(m.group(1))).strip()
    return t[:120] or None


def extract_candidates(text: str, page_url: str) -> list:
    """Scan one page's HTML/JS text. Returns unordered, de-duplicated dicts:
    {"url": absolute URL, "kind": hls|dash|video|audio|file|page, "height": int (0 if unknown)}.
    Embedded player pages come back as kind "page" (at most 6), so the caller can scan them one level down.
    """
    raw = _unescape_source(text or "")
    found = []                                     # (candidate text, start, end)
    for m in _QUOTED_RE.finditer(raw):
        found.append((m.group(1), m.start(), m.end()))
    for m in _BARE_RE.finditer(raw):
        found.append((m.group(0), m.start(), m.end()))
    found.sort(key=lambda t: t[1])

    out, by_key = [], {}
    for i, (cand, start, end) in enumerate(found):
        url = urljoin(page_url, html_lib.unescape(cand).strip())
        if not _ok_url(url):
            continue
        kind = kind_of(url)
        if not kind:
            continue
        # Height label that belongs to THIS link: text right after it, up to the next media link in the page
        # (e.g. {file:"a.m3u8", label:"720p"}). Looking before it would pick up the previous entry's label.
        next_start = found[i + 1][1] if i + 1 < len(found) else len(raw)
        window = raw[end:min(end + 120, next_start)]
        height = _height_near(url, window)
        key = key_of(url)
        if key in by_key:
            if height and not by_key[key]["height"]:
                by_key[key]["height"] = height
            continue
        item = {"url": url, "kind": kind, "height": height}
        by_key[key] = item
        out.append(item)

    pages = 0
    page_key = key_of(page_url)
    for m in _IFRAME_RE.finditer(raw):
        if pages >= 6:
            break
        src = html_lib.unescape(m.group(1)).strip()
        if src.startswith(("about:", "javascript:", "data:", "blob:")):
            continue
        url = urljoin(page_url, src)
        if not _ok_url(url) or key_of(url) == page_key or key_of(url) in by_key:
            continue
        by_key[key_of(url)] = {"url": url, "kind": "page", "height": 0}
        out.append(by_key[key_of(url)])
        pages += 1
    return out


def sort_candidates(items: list) -> list:
    """Best first: HLS/DASH/video by height (high first), then audio, files, embedded pages."""
    return sorted(items, key=lambda c: (_KIND_RANK.get(c["kind"], 9), -(c.get("height") or 0), c["url"]))


def merge(first: list, second: list) -> list:
    """first wins on duplicates (browser-read streams are the real player requests, so they go first)."""
    out, seen = [], set()
    for item in list(first) + list(second):
        k = key_of(item["url"])
        if k in seen:
            continue
        seen.add(k)
        out.append(item)
    return out


def display_label(item: dict) -> str:
    kind, h = item["kind"], item.get("height") or 0
    res = f" {h}p" if h else ""
    ext = _ext_of(item["url"]).upper() or "?"
    if kind == "hls":
        return f"📺 HLS{res}"
    if kind == "dash":
        return "📺 DASH"
    if kind == "video":
        return f"🎬 {ext}{res}"
    if kind == "audio":
        return f"🎵 Audio {ext}"
    if kind == "file":
        return f"🗂 File {ext}"
    return "🖼 Embedded player"


def short_name(url: str, limit: int = 40) -> str:
    parts = urlsplit(url)
    tail = parts.path.rstrip("/").rsplit("/", 1)[-1] or parts.path or "/"
    name = f"{parts.hostname or ''}/{tail}"
    return name if len(name) <= limit else name[:limit - 1] + "…"

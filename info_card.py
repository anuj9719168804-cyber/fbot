"""Info card for a link: poster + movie/video details, shown above the "Choose an action" menu.

    fetch_info_card(link) -> dict | None
        {"title", "thumb", "text"}  where "text" is the ready HTML caption (labels only where the page had them).

Sources, in order of trust:
  1. JSON-LD (<script type="application/ld+json">) with Movie / VideoObject / TVSeries type
  2. Open Graph / Twitter meta tags (og:title, og:image, og:description)
  3. "Label : value" lines in the page text (Starcast, Genres, Quality, Length, Release Date, Movie Story, ...)

Everything is best-effort: a failed fetch, a Cloudflare challenge page, or a page with no usable data
returns None, and the caller falls back to the plain link menu.
"""
import html as _html
import json
import re
from urllib.parse import urljoin

import requests

TIMEOUT = 8
MAX_STORY = 400
MAX_CAPTION = 800  # Telegram caption limit is 1024; the link line + menu text take the rest

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
}

# (label as shown on the page, key used internally, icon)
_LABELS = [
    (r"movie\s*name|video\s*title|title", "title_line", "🎬"),
    (r"star\s*cast|starcast|cast|actors?|performers?|stars", "cast", "⭐"),
    (r"genres?|categories|category|tags", "genre", "🎭"),
    (r"quality|resolution", "quality", "📺"),
    (r"length|duration|runtime|run\s*time", "length", "⏱"),
    (r"release\s*date|released|date", "release", "📅"),
    (r"movie\s*story|story|synopsis|plot|description|about", "story", "📖"),
]

_BAD_PAGE_RE = re.compile(r"just a moment|attention required|access denied|cloudflare|enable javascript", re.I)


# ---------- low-level helpers ----------

def _clean(s: str) -> str:
    s = _html.unescape(s or "")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


_PLACEHOLDER_RE = re.compile(r"^[\s\-–—:.]*(n\s*/\s*a|na|none|null|nil|unknown|not\s+available|not\s+found)?[\s\-–—:.]*$", re.I)


def _is_placeholder(val: str) -> bool:
    """True for empty / 'N/A' / 'None' / '-' style filler values."""
    return bool(_PLACEHOLDER_RE.match(val or ""))


def _meta(page: str, prop: str) -> str:
    pat = r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]*>' % re.escape(prop)
    m = re.search(pat, page, re.I)
    if not m:
        return ""
    c = re.search(r'content=["\']([^"\']*)["\']', m.group(0), re.I)
    return _clean(c.group(1)) if c else ""


def _title_tag(page: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
    return _clean(re.sub(r"<[^>]+>", "", m.group(1))) if m else ""


def _iso_duration(d: str) -> str:
    """'PT3H21M' -> '3 Hrs 21 Mins'. Anything else is returned as-is."""
    m = re.fullmatch(r"P(?:T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)", (d or "").strip().upper())
    if not m or not any(m.groups()):
        return _clean(d)
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    parts = []
    if h:
        parts.append(f"{h} Hrs")
    if mi:
        parts.append(f"{mi} Mins")
    if not h and not mi and s:
        parts.append(f"{s} Secs")
    return " ".join(parts)


def _names(v) -> str:
    """JSON-LD actor/genre values can be a string, a dict with name, or a list of either."""
    if isinstance(v, str):
        return _clean(v)
    if isinstance(v, dict):
        return _clean(v.get("name", ""))
    if isinstance(v, list):
        return ", ".join(x for x in (_names(i) for i in v) if x)
    return ""


_SKIP_IMG_RE = re.compile(r"logo|icon|sprite|counter|histats|statcounter|analytics|\.gif|\.svg|/templates/|avatar|banner-ad", re.I)
_PREFER_IMG_RE = re.compile(r"poster|cover|thumb|/images?/|/files/|/uploads?/|/movie|/video", re.I)


def _poster_from_img(page: str, base_url: str) -> str:
    """First plausible poster <img>: skips logos/icons/counters, prefers src that looks like a poster/thumbnail."""
    best = ""
    for tag in re.findall(r"<img[^>]*>", page, re.I):
        m = re.search(r'src=["\']([^"\']+)["\']', tag, re.I)
        if not m:
            continue
        src = _html.unescape(m.group(1)).strip()
        if not src or src.startswith("data:") or _SKIP_IMG_RE.search(src):
            continue
        absolute = urljoin(base_url, src) if base_url else src
        if _PREFER_IMG_RE.search(src):
            return absolute
        best = best or absolute
    return best


# ---------- source 1: JSON-LD ----------

_LD_TYPES = {"movie", "videoobject", "tvseries", "tvepisode", "creativework"}


def _walk_ld(obj):
    if isinstance(obj, list):
        for i in obj:
            yield from _walk_ld(i)
    elif isinstance(obj, dict):
        yield obj
        for v in obj.values():
            if isinstance(v, (dict, list)):
                yield from _walk_ld(v)


def _from_jsonld(page: str) -> dict:
    out = {}
    for raw in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.I | re.S):
        try:
            data = json.loads(raw.strip())
        except Exception:
            continue
        for node in _walk_ld(data):
            t = node.get("@type", "")
            types = {x.lower() for x in (t if isinstance(t, list) else [t]) if isinstance(x, str)}
            if not (types & _LD_TYPES):
                continue
            out.setdefault("title", _clean(node.get("name", "")))
            out.setdefault("cast", _names(node.get("actor") or node.get("actors") or ""))
            out.setdefault("genre", _names(node.get("genre") or ""))
            out.setdefault("length", _iso_duration(node.get("duration", "")))
            out.setdefault("release", _clean(str(node.get("datePublished") or node.get("uploadDate") or "")))
            out.setdefault("story", _clean(node.get("description", "")))
            img = node.get("thumbnailUrl") or node.get("image") or ""
            if isinstance(img, list):
                img = img[0] if img else ""
            if isinstance(img, dict):
                img = img.get("url", "")
            if img and isinstance(img, str):
                out.setdefault("thumb", img)
    return {k: v for k, v in out.items() if v}


# ---------- source 3: "Label : value" lines ----------

def _page_text(page: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", page)
    text = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h[1-6]|dd|dt)>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = _html.unescape(text)
    return "\n".join(_clean(line) for line in text.split("\n") if _clean(line))


def _from_labels(page: str) -> dict:
    out = {}
    lines = _page_text(page).split("\n")
    for line in lines:
        for label_re, key, _icon in _LABELS:
            if key in out:
                continue
            m = re.match(r"^\s*(?:%s)\s*[:\-–]\s*(.+)$" % label_re, line, re.I)
            if m:
                val = m.group(1).strip()
                if key == "story":
                    val = val[:MAX_STORY]
                if _is_placeholder(val):
                    continue
                if val and len(val) < 500:
                    out[key] = val
    return out


# ---------- building the caption ----------

_CAPTION_FIELDS = [
    ("cast", "⭐", "Starcast"),
    ("genre", "🎭", "Genres"),
    ("quality", "📺", "Quality"),
    # "Length" line intentionally removed from the caption / info card
    ("release", "📅", "Release Date"),
    # "Movie Story" line intentionally removed from the caption / info card
]


def _caption(info: dict) -> str:
    lines = []
    title = info.get("title") or info.get("title_line") or ""
    if title:
        lines.append(f"🎬 <b>{_html.escape(title)}</b>")
    for key, icon, label in _CAPTION_FIELDS:
        val = info.get(key)
        if not val:
            continue
        if key == "story" and len(val) > MAX_STORY:
            val = val[:MAX_STORY].rsplit(" ", 1)[0] + "…"
        lines.append(f"{icon} <b>{label}:</b> {_html.escape(val)}")
    text = "\n".join(lines)
    if len(text) > MAX_CAPTION:
        text = text[:MAX_CAPTION].rsplit("\n", 1)[0]
    # whole card in a quote block (the upload caption's details already sit inside one, so _details stays plain)
    return f"<blockquote>{text}</blockquote>" if text else text


DETAIL_STORY = 200  # shorter story for the upload caption (the caption also has the file info)


def _details(info: dict) -> str:
    """Cast / genre / quality / length / release / story lines, without the title, for the upload caption."""
    lines = []
    for key, icon, label in _CAPTION_FIELDS:
        val = info.get(key)
        if not val:
            continue
        if key == "story" and len(val) > DETAIL_STORY:
            val = val[:DETAIL_STORY].rsplit(" ", 1)[0] + "…"
        lines.append(f"{icon} <b>{label}:</b> {_html.escape(val)}")
    return "\n".join(lines)


# ---------- public API ----------

def parse_page(page: str, base_url: str = "") -> dict | None:
    """Pure function (no network): turn one page's HTML into {"title", "thumb", "text", ...} or None."""
    if not page or _BAD_PAGE_RE.search(_title_tag(page) + " " + _meta(page, "og:title")):
        return None

    info = {}
    info.update(_from_labels(page))            # lowest priority
    info.update(_from_jsonld(page))             # overrides labels where the page has structured data
    og_title = _meta(page, "og:title")
    og_desc = _meta(page, "og:description") or _meta(page, "description")
    og_img = _meta(page, "og:image") or _meta(page, "twitter:image")

    # title priority: JSON-LD name > the page's own "Movie Name:" line > og:title > <title>
    if not info.get("title"):
        info["title"] = info.get("title_line") or og_title or _title_tag(page)
    if not info.get("story") and og_desc:
        info["story"] = og_desc
    if not info.get("thumb") and og_img:
        info["thumb"] = og_img
    if not info.get("thumb"):
        info["thumb"] = _poster_from_img(page, base_url)
    if info.get("thumb"):
        info["thumb"] = urljoin(base_url, info["thumb"]) if base_url else info["thumb"]

    # A card needs something besides a title: a poster or at least one real detail (cast, quality, story, ...).
    details = [k for k in info if k not in ("title", "title_line", "thumb") and info.get(k)]
    if not details and not info.get("thumb"):
        return None
    info.pop("title_line", None)

    info["text"] = _caption(info)
    info["details"] = _details(info)
    return info


def fetch_info_card(link: str) -> dict | None:
    """Blocking (uses requests). Call it through asyncio.to_thread from async code."""
    if not link or not link.lower().startswith(("http://", "https://")):
        return None
    try:
        r = requests.get(link, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
    except Exception:
        return None
    if r.status_code != 200 or "text/html" not in r.headers.get("Content-Type", "").lower():
        return None
    try:
        return parse_page(r.text, base_url=r.url)
    except Exception:
        return None

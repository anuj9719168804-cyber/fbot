"""desiserial.su support (wired like filmyzilla_scan.py).

Flow seen in the HAR:
  1. Episode page  https://www.desiserial.su/<slug>/<id>/
       each player option is an <a class="stream-action"> to
       https://go.searchping.com/desiserial/getlink.php?v=...&part2=...   (label in .stream-quality, e.g. "720p HD Quality")
  2. getlink.php  -> HTML meta refresh -> go.showdetails.org/published?v=...  -> ...
  3. workers.showdetails.org/hls/play.php?v=<token>  -> 302 Location: https://s*.storage.yandex.net/rdisk/...  (the MP4)

The direct MP4 link is time-limited, so it is resolved right before the download, never stored.
"""
import html as _html
import re
from urllib.parse import quote_plus, urljoin, urlsplit

import requests

TIMEOUT = 20
MAX_HOPS = 10
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_SITE_RE = re.compile(r"^https?://(?:www\.)?desiserial\.\w+/", re.I)
GETLINK_RE = re.compile(r"go\.searchping\.com/desiserial/getlink\.php", re.I)
PLAY_RE = re.compile(r"workers\.showdetails\.org/hls/play\.php", re.I)
_REFRESH_RE = re.compile(r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+content=["\'][^"\']*url=([^"\']+)["\']', re.I)
_STREAM_RE = re.compile(r'<div class="stream-action">\s*<a[^>]+href="([^"]+)"', re.I)


def is_desiserial_page(url: str) -> bool:
    return bool(url and _SITE_RE.match(url))


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def list_options(page_url: str) -> list:
    """Episode page -> [{"label", "url"}] (one per player/quality). [] if the page has none."""
    r = requests.get(page_url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    page = r.text
    out, seen = [], set()
    for m in _STREAM_RE.finditer(page):
        href = _html.unescape(m.group(1)).strip()
        if not GETLINK_RE.search(href) or href in seen:
            continue
        seen.add(href)
        # quality: the nearest "stream-quality" text before this link
        before = page[max(0, m.start() - 1500):m.start()]
        q = re.findall(r'<div class="stream-quality">(.*?)</div>', before, re.I | re.S)
        t = re.findall(r'<div class="stream-title">(.*?)</div>', before, re.I | re.S)
        quality = _clean(q[-1]) if q else ""
        player = _clean(t[-1]).split("–")[0].split(" - ")[0].strip() if t else ""
        label = " · ".join(x for x in (player, quality.replace(" HD Quality", "")) if x) or "Option"
        out.append({"label": label, "url": href})
    return out


def episode_title(page_url: str) -> str:
    try:
        r = requests.get(page_url, headers=HEADERS, timeout=TIMEOUT)
        m = re.search(r"<h1[^>]*>(.*?)</h1>", r.text, re.I | re.S) or re.search(r"<title>(.*?)</title>", r.text, re.I | re.S)
        return _clean(m.group(1)).split(" | ")[0] if m else ""
    except Exception:
        return ""


def resolve_direct(start_url: str) -> str | None:
    """getlink / showdetails / play URL -> the direct MP4 URL (storage.yandex.net). None if the chain breaks."""
    url = start_url
    session = requests.Session()
    session.headers.update(HEADERS)
    for _ in range(MAX_HOPS):
        try:
            r = session.get(url, allow_redirects=False, timeout=TIMEOUT)
        except requests.RequestException:
            return None
        loc = r.headers.get("Location")
        if r.status_code in (301, 302, 303, 307, 308) and loc:
            nxt = urljoin(url, loc)
            if "storage.yandex" in urlsplit(nxt).netloc:
                return nxt  # the direct MP4
            url = nxt
            continue
        if r.status_code == 200:
            m = _REFRESH_RE.search(r.text)
            if m:
                url = urljoin(url, _html.unescape(m.group(1)).strip())
                continue
            return None
        return None
    return None


def resolve_option(option_url: str):
    """-> (filename, direct_url) for a quality option, or None."""
    direct = resolve_direct(option_url)
    if not direct:
        return None
    path = urlsplit(direct).path
    name = path.rsplit("/", 1)[-1] or "video.mp4"
    if not name.lower().endswith(".mp4"):
        name += ".mp4"
    return name, direct


_TILE_RE = re.compile(r'<a class="film-art" href="([^"]+)" aria-label="([^"]*)"', re.I)
BASE = "https://www.desiserial.su"


def search(query: str, limit: int = 200) -> list:
    """Site search (/search.php?s=...) -> [{"title", "url"}] of episode pages, newest first as the site lists them."""
    q = (query or "").strip()
    if not q:
        return []
    r = requests.get(f"{BASE}/search.php", params={"s": q}, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    out, seen = [], set()
    for m in _TILE_RE.finditer(r.text):
        url = urljoin(BASE, _html.unescape(m.group(1)))
        if url in seen:
            continue
        seen.add(url)
        title = _clean(m.group(2))
        if title.lower().startswith("watch "):
            title = title[6:]
        out.append({"title": title.split(" | ")[0], "url": url})
        if len(out) >= limit:
            break
    return out


_MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                        "september", "october", "november", "december"], start=1)}
_DATE_RE = re.compile(r"^(.*?)\s*(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})", re.I)


def episode_date(title: str):
    """'Yeh Rishta Kya Kehlata Hai 21st October 2026 Full Episode | Star Plus' -> (show, (2026, 10, 21), '21 Oct')."""
    m = _DATE_RE.search(title or "")
    if not m or m.group(3).lower() not in _MONTHS:
        return None
    show = m.group(1).strip(" -|:")
    day, month, year = int(m.group(2)), _MONTHS[m.group(3).lower()], int(m.group(4))
    short = f"{day} {m.group(3)[:3].title()}"
    return show, (year, month, day), short


def sort_newest_first(items: list) -> list:
    """Search results -> episodes with show, date and short label, newest first (21, 20, 19 ...)."""
    out = []
    for it in items:
        d = episode_date(it["title"])
        if d is None:
            continue
        show, date, short = d
        out.append({"show": show, "date": date, "label": short, "url": it["url"], "title": it["title"]})
    out.sort(key=lambda e: e["date"], reverse=True)
    return out

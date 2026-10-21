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


def _token(url: str):
    """The v= token of a getlink / showdetails / play URL, decoded the way play.php expects it."""
    from urllib.parse import parse_qs
    vals = parse_qs(urlsplit(url).query).get("v")
    return vals[0].replace(" ", "+") if vals else None


def _play_direct(session, token: str, tries: int = 3):
    """workers.showdetails.org/hls/play.php?v=<token> -> the storage.yandex.net MP4 (302 Location).
    play.php answers 0 / 200 now and then; a retry usually works."""
    from urllib.parse import quote
    import time
    url = f"https://workers.showdetails.org/hls/play.php?v={quote(token, safe='')}"
    for i in range(tries):
        try:
            r = session.get(url, allow_redirects=False, timeout=TIMEOUT)
            loc = r.headers.get("Location")
            if loc and "storage.yandex" in urlsplit(urljoin(url, loc)).netloc:
                return urljoin(url, loc)
        except requests.RequestException:
            pass
        time.sleep(1 + i)
    return None


def resolve_direct(start_url: str) -> str | None:
    """getlink / showdetails / play URL -> the direct MP4 URL (storage.yandex.net).

    The redirect chain is not the same every time: some tokens go through a decoy page (momentumprofitbreakout ->
    /a-diverted-... or /lotteries/...) that answers 200 without a redirect. The token (v=) is the same on every hop,
    so when the chain stops, play.php is asked directly with that token."""
    token = _token(start_url)
    url = start_url
    session = requests.Session()
    session.headers.update(HEADERS)
    for _ in range(MAX_HOPS):
        t = _token(url)
        token = t or token
        try:
            r = session.get(url, allow_redirects=False, timeout=TIMEOUT)
        except requests.RequestException:
            break
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
        break  # a page with no redirect (decoy / end of chain): fall back to play.php
    if token:
        return _play_direct(session, token)
    return None


def _extra_tokens(option_url: str) -> list:
    """Each player option carries up to three play tokens: v= (the player's own), part2= and part3= (other sources of
    the same episode). If the v= source fails, the others are tried."""
    from urllib.parse import parse_qs
    q = parse_qs(urlsplit(option_url).query)
    out = []
    for key in ("v", "part2", "part3"):
        if q.get(key):
            out.append(q[key][0].replace(" ", "+"))
    return out


def resolve_option(option_url: str):
    """-> (filename, direct_url) for a quality option, or None."""
    direct = resolve_direct(option_url)
    if not direct:
        session = requests.Session()
        session.headers.update(HEADERS)
        for tok in _extra_tokens(option_url):
            if tok == _token(option_url):
                continue  # already tried inside resolve_direct
            direct = _play_direct(session, tok)
            if direct:
                break
    if not direct:
        return None
    path = urlsplit(direct).path
    name = path.rsplit("/", 1)[-1] or "video.mp4"
    if not name.lower().endswith(".mp4"):
        name += ".mp4"
    return name, direct

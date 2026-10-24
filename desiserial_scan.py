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
from urllib.parse import quote, quote_plus, urljoin, urlsplit

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


def part_tokens(option_url: str) -> list:
    """The episode is split into parts: v= is Part 1, part2= Part 2, part3= Part 3 (the site's player has a "Video parts"
    switcher; each part is its own MP4). -> the tokens in playing order (a single-part option gives just [v])."""
    from urllib.parse import parse_qs
    q = parse_qs(urlsplit(option_url).query)
    out = []
    for key in ("v", "part2", "part3", "part4", "part5"):
        if q.get(key):
            out.append(q[key][0].replace(" ", "+"))
    return out


WORKERS_HLS = "https://workers.showdetails.org/hls/"
_EMBED_CACHE: dict = {}      # option url -> (time, embed_url): the article page the redirect chain ends on


def _chain_end(option_url: str, session):
    """Follow getlink.php -> (meta refresh) go.showdetails.org/published -> revenueebitda.../published -> article page.
    The page the chain ends on (HTTP 200, no further redirect) is the `embed_url` the player passes to media_meta.php.
    -> (embed_url without query | None, direct yandex url if the chain happened to redirect straight to it | None)"""
    url, last_ok = option_url, None
    session.headers["Referer"] = "https://www.desiserial.su/"
    for _ in range(MAX_HOPS):
        try:
            r = session.get(url, allow_redirects=False, timeout=TIMEOUT)
        except requests.RequestException:
            break
        loc = r.headers.get("Location")
        if r.status_code in (301, 302, 303, 307, 308) and loc:
            nxt = urljoin(url, loc)
            if "storage.yandex" in urlsplit(nxt).netloc:
                return None, nxt
            session.headers["Referer"] = url
            url = nxt
            continue
        if r.status_code == 200:
            m = _REFRESH_RE.search(r.text)
            if m:
                session.headers["Referer"] = url
                url = urljoin(url, _html.unescape(m.group(1)).strip())
                continue
            last_ok = url
        break
    if last_ok and urlsplit(last_ok).netloc.lower() != "go.searchping.com":
        u = urlsplit(last_ok)
        return f"{u.scheme}://{u.netloc}{u.path}", None
    return None, None


def _signed_play(session, token: str, embed_url: str):
    """media_meta.php (what the player calls) answers with source.file = play.php?v=..&exp=..&sig=.. - the site now
    refuses an unsigned play.php. That signed play.php 302s to the storage.yandex.net MP4.
    -> (direct url | None, title)"""
    import json
    meta = f"{WORKERS_HLS}media_meta.php?v={quote(token, safe='')}&embed_url={quote(embed_url, safe='')}&type=player"
    hdr = {"Referer": WORKERS_HLS, "Accept": "*/*"}
    try:
        r = session.get(meta, headers=hdr, timeout=TIMEOUT)
        data = r.json()
    except Exception as e:
        print(f"[-] desiserial media_meta failed: {str(e)[:120]}")
        return None, ""
    src = (data.get("source") or {}) if isinstance(data, dict) else {}
    f = src.get("file")
    if not f:
        print(f"[-] desiserial media_meta: no source.file ({str(data)[:160]})")
        return None, ""
    play = urljoin(WORKERS_HLS, f)
    for i in range(3):
        try:
            r = session.get(play, headers={"Range": "bytes=0-", "Referer": WORKERS_HLS}, allow_redirects=False, timeout=TIMEOUT)
            loc = r.headers.get("Location")
            if loc and "storage.yandex" in urlsplit(urljoin(play, loc)).netloc:
                return urljoin(play, loc), src.get("title") or ""
            print(f"[-] desiserial signed play.php: HTTP {r.status_code}, no yandex redirect")
        except requests.RequestException as e:
            print(f"[-] desiserial signed play.php failed: {str(e)[:100]}")
        import time
        time.sleep(1 + i)
    return None, ""


def resolve_part(option_url: str, idx: int):
    """Direct (storage.yandex.net) MP4 URL of part idx (0 = Part 1) of a player option, or None. The URL is signed and
    short-lived, so resolve each part right before downloading it.
    Order: (1) the browser's own way - follow the chain to the article page, media_meta.php -> signed play.php;
    (2) the old way - unsigned play.php with the token (kept as a fallback)."""
    import time
    toks = part_tokens(option_url)
    if idx >= len(toks):
        return None
    tok = toks[idx]
    session = _session()
    now = time.time()
    cached = _EMBED_CACHE.get(option_url)
    embed = cached[1] if cached and now - cached[0] < 240 else None
    if not embed:
        embed, direct = _chain_end(option_url, session)
        if direct and idx == 0:
            return direct
        if embed:
            _EMBED_CACHE[option_url] = (now, embed)
            if len(_EMBED_CACHE) > 50:
                _EMBED_CACHE.clear()
    if embed:
        direct, _title = _signed_play(_session(), tok, embed)
        if direct:
            return direct
    print(f"[*] desiserial: signed flow gave nothing for part {idx + 1}, trying plain play.php")
    return _play_direct(_session(), tok)


def _session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


# ---------------------------------------------------------------------------------------------------------------------
# Search by show name  (/desiserial <show>  ->  main.py: _ds.search(q) then _ds.sort_newest_first(items))
# Append this block to the END of desiserial_scan.py.
# ---------------------------------------------------------------------------------------------------------------------
SITE = "https://www.desiserial.su"
# episode page = https://www.desiserial.su/<slug>/<numeric id>/   (a show / category page has no numeric id)
_EP_URL_RE = re.compile(r"^https?://(?:www\.)?desiserial\.\w+/(?!page/)[^/?#]+/[0-9a-f]{8,40}/?$", re.I)
_A_RE = re.compile(r"<a\b([^>]*?)>(.*?)</a>", re.I | re.S)
_HREF_RE = re.compile(r'href\s*=\s*["\']([^"\']+)["\']', re.I)
_ATTR_TITLE_RE = re.compile(r'\btitle\s*=\s*["\']([^"\']+)["\']', re.I)
_IMG_ALT_RE = re.compile(r'<img[^>]+alt\s*=\s*["\']([^"\']+)["\']', re.I)
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_DATE_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*,?\s*"
                      r"((?:19|20)\d{2})\b", re.I)


def _title_date(title: str):
    """'Mannat 23rd October 2026 Full Episode | Zee TV' -> datetime.date(2026, 10, 23) | None"""
    import datetime
    m = _DATE_RE.search(title or "")
    if not m:
        return None
    try:
        return datetime.date(int(m.group(3)), _MONTHS[m.group(2)[:3].lower()], int(m.group(1)))
    except ValueError:
        return None


def _show_of(title: str) -> str:
    """'Yeh Rishta Kya Kehlata Hai 23rd October 2026 Full Episode | Star Plus' -> 'Yeh Rishta Kya Kehlata Hai'"""
    t = (title or "").split("|")[0]
    m = _DATE_RE.search(t)
    if m:
        t = t[:m.start()]
    t = re.sub(r"\b(?:full\s+)?episodes?\b.*$|\bwritten\s+update\b.*$", "", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip(" -:,") or (title or "").strip()


def _slug_title(url: str) -> str:
    """.../mannat-har-khushi-paane-ki-6th-october-2026-full-episode-colors-tv/<id>/ -> readable title"""
    parts = [x for x in urlsplit(url).path.split("/") if x]
    return re.sub(r"\s+", " ", parts[0].replace("-", " ")).strip().title() if parts else ""


def _search_page_links(page_html: str, base_url: str) -> dict:
    """All episode links on a page -> {url: best title}. An episode is usually linked 2-3 times (poster image, heading,
    'read more'); the longest text wins."""
    out = {}
    for m in _A_RE.finditer(page_html):
        attrs, inner = m.group(1), m.group(2)
        h = _HREF_RE.search(attrs)
        if not h:
            continue
        href = urljoin(base_url, _html.unescape(h.group(1)).strip()).split("#")[0].split("?")[0]
        if not _EP_URL_RE.match(href):
            continue
        text = _clean(inner)
        if not text:
            t = _ATTR_TITLE_RE.search(attrs)
            a = _IMG_ALT_RE.search(inner)
            text = _clean(t.group(1)) if t else (_clean(a.group(1)) if a else "")
        if len(text) > len(out.get(href, "")):
            out[href] = text
        else:
            out.setdefault(href, text)
    return out


def search(query: str, max_pages: int = 60) -> list:
    """Site search (WordPress-style /?s=<name>, then /page/N/?s=<name>) -> [{"title", "url"}, ...] for episode pages whose
    title contains the words of the query. [] when nothing matches or the site can't be reached."""
    from urllib.parse import quote_plus
    words = [w for w in re.findall(r"\w+", (query or "").lower()) if w]
    if not words:
        return []
    s = _session()
    s.headers["Referer"] = SITE + "/"
    found, seen = [], set()
    for page in range(1, max_pages + 1):
        url = f"{SITE}/search.php?s={quote_plus(query)}" + (f"&page={page}" if page > 1 else "")
        try:
            r = s.get(url, timeout=TIMEOUT)
        except requests.RequestException as e:
            print(f"[-] desiserial search {url}: {str(e)[:100]}")
            break
        links = _search_page_links(r.text, url) if r.status_code == 200 else {}
        print(f"[desiserial] search {url}: HTTP {r.status_code}, {len(links)} episode link(s)")
        if not links:
            break
        added = 0
        for href, title in links.items():
            key = href.rstrip("/").lower()
            low = (title or href).lower()
            # slug words count too (some themes only show an image link)
            hay = low + " " + re.sub(r"[-_]+", " ", urlsplit(href).path.lower())
            if key in seen or not all(w in hay for w in words):
                continue
            seen.add(key)
            found.append({"title": title if (title and _title_date(title)) else _slug_title(href), "url": href})
            added += 1
        if not added:
            break
    return found


def sort_newest_first(items: list) -> list:
    """search() result -> [{"label", "url", "title", "show"}] newest episode first (date taken from the title,
    e.g. '23rd October 2026'); episodes without a date go last, in the order the site gave them."""
    rows = []
    for pos, it in enumerate(items or []):
        title = it.get("title") or ""
        d = _title_date(title) or _title_date(_slug_title(it["url"]))
        label = f"{d.day} {d.strftime('%b')}" if d else (_show_of(title)[:10] or f"#{pos + 1}")
        rows.append((d, pos, {"label": label, "url": it["url"], "title": title, "show": _show_of(title)}))
    dated = sorted((r for r in rows if r[0]), key=lambda r: (r[0], -r[1]), reverse=True)
    undated = [r for r in rows if not r[0]]
    return [r[2] for r in dated + undated]

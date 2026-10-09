"""
spankbang.com auto-scraper backend for auto_scraper.py.

Function contract auto_scraper.py expects (same as mat6tube_scraper /
eporner_scraper):
    get_model_page_videos(term, page=1) -> (list[{slug,url,title}], total_pages)
    get_random_page_videos()            -> list[{slug,url,title}]
    get_latest_videos()                 -> list[{slug,url,title}]
plus get_trending_videos() for the /autouploadspankbang trending mode.

Every item's "slug" is "spankbang-<id>" (also what auto_scraper.extract_slug()
produces for the same URL), so the dedup check (is_video_uploaded) agrees no
matter whether a video arrived through a worker, the monitor or a pasted link.

WHY HTML SCRAPING (and not yt-dlp playlists):
  yt-dlp's SpankBang extractor only understands single videos and
  /<id>/playlist/ pages -- it cannot list search results or the new/trending
  pages, so those are read straight from the listing HTML instead.

NETWORK NOTES:
  * SpankBang sits behind Cloudflare. Requests go out through curl_cffi with
    Chrome TLS impersonation (same as ytdlp_downloader / mat6tube_downloader).
  * If a Cloudflare JS challenge still shows up, cf_bypass.try_solve()
    (FlareSolverr, optional) is asked for a clearance cookie once and the
    request is retried with it.
  * spankbang.party is tried as a mirror if spankbang.com fails.

URL patterns (SpankBang's own site structure):
    Video:      /<id>/video/<slug>
    New:        /new_videos/            /new_videos/<page>/
    Trending:   /trending_videos/       /trending_videos/<page>/
    Popular:    /most_popular/          /most_popular/<page>/
    Search:     /s/<query+with+plus>/   /s/<query>/<page>/
    Pornstar:   /<id>/pornstar/<name>/  /<id>/pornstar/<name>/<page>/
    Channel:    /<id>/channel/<name>/   /<id>/channel/<name>/<page>/

The term given to get_model_page_videos() may also be a full SpankBang listing URL (or just its path) of any of the
above -- see _url_listing_base().
"""

import asyncio
import html as _html
import http.cookiejar
import logging
import random
import re
from urllib.parse import quote_plus, unquote_plus, urlsplit

import requests

import cf_bypass

try:
    from curl_cffi.requests import Session as CurlSession
    _CURL_OK = True
except Exception:  # pragma: no cover - curl_cffi is in requirements.txt
    _CURL_OK = False

logger = logging.getLogger(__name__)

BASE_URL = "https://spankbang.com"
_MIRRORS = ("https://spankbang.com", "https://spankbang.party")
PER_PAGE = 40  # SpankBang shows ~40 videos per listing page

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Broad keywords for random mode
_BROAD_TERMS = [
    "amateur", "teen", "milf", "asian", "latina", "ebony", "blonde", "brunette",
    "anal", "lesbian", "hardcore", "big tits", "creampie", "pov", "public",
    "homemade", "solo", "threesome", "mature", "indian",
]
# Browse pages usable as "random" sources (path, max page worth picking)
_RANDOM_PATHS = (("/new_videos/", 15), ("/trending_videos/", 10), ("/most_popular/", 10))

# <a ... href="/<id>/video/<slug>" ...>  (absolute or relative href)
_VIDEO_A_RE = re.compile(
    r'<a\b[^>]*?href=["\'](?:https?://[^"\'/]+)?(/([A-Za-z0-9]+)/video/([^"\'?#\s]+))[^"\']*["\'][^>]*>',
    re.IGNORECASE,
)
_TITLE_ATTR_RE = re.compile(r'\btitle=["\']([^"\']+)["\']', re.IGNORECASE)
_CF_MARKERS = ("just a moment", "cf-browser-verification", "challenge-platform", "cf-chl", "attention required")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _cookies_from_bypass(url: str):
    """(cookies dict, user_agent) from a still-fresh cf_bypass solve for this domain, else ({}, _UA)."""
    try:
        opts = cf_bypass.get_bypass_opts(url)
        if not opts:
            return {}, _UA
        jar = http.cookiejar.MozillaCookieJar(opts["cookiefile"])
        jar.load(ignore_discard=True, ignore_expires=True)
        return {c.name: c.value for c in jar}, opts["http_headers"].get("User-Agent") or _UA
    except Exception:
        return {}, _UA


def _is_challenge(status: int, text: str) -> bool:
    if status in (403, 429, 503):
        return True
    head = (text or "")[:3000].lower()
    return any(m in head for m in _CF_MARKERS)


def _get_once(url: str, referer: str):
    cookies, ua = _cookies_from_bypass(url)
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.7",
        "Referer": referer,
    }
    px = cf_bypass.proxy_for(url)
    proxies = {"http": px, "https": px} if px else None
    if _CURL_OK:
        with CurlSession(impersonate="chrome") as sess:  # closed after every request (no leaked sessions)
            r = sess.get(url, headers=headers, cookies=cookies or None, timeout=25, allow_redirects=True, proxies=proxies)
            return r.status_code, r.text
    r = requests.get(url, headers=headers, cookies=cookies or None, timeout=25, proxies=proxies)
    return r.status_code, r.text


def _fetch(path: str) -> str | None:
    """Fetches BASE_URL+path (then the mirror). One FlareSolverr attempt per mirror if challenged."""
    for base in _MIRRORS:
        url = base + path
        for attempt in (1, 2):
            try:
                status, text = _get_once(url, base + "/")
            except Exception as e:
                logger.warning(f"spankbang fetch error for {url}: {e}")
                break
            if status == 200 and not _is_challenge(status, text):
                return text
            if _is_challenge(status, text) and attempt == 1 and cf_bypass.try_solve(url):
                continue  # retry once with the fresh clearance cookie
            logger.warning(f"spankbang fetch {url} -> HTTP {status} (challenge/blocked)")
            break
    return None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def _title_from_slug(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").strip().title() or slug


def _parse_videos(page_html: str) -> list[dict]:
    """[{slug, url, title}] from a listing page, de-duplicated by video id, in page order."""
    items, seen = [], {}
    for m in _VIDEO_A_RE.finditer(page_html or ""):
        path, vid, vslug = m.group(1), m.group(2).lower(), m.group(3)
        tm = _TITLE_ATTR_RE.search(m.group(0))
        real_title = _html.unescape(tm.group(1)).strip() if tm else ""
        if vid in seen:
            # a video usually has several links (thumbnail, name ...); only some carry title="" --
            # upgrade a slug-derived title if a later link has the real one
            it = seen[vid]
            if real_title and not it["_real"]:
                it["title"], it["_real"] = real_title, True
            continue
        it = {"slug": f"spankbang-{vid}", "url": f"{BASE_URL}{path}",
              "title": real_title or _title_from_slug(vslug), "_real": bool(real_title)}
        seen[vid] = it
        items.append(it)
    for it in items:
        it.pop("_real", None)
    return items


def _total_pages(page_html: str, base_path: str, page: int, got_items: bool) -> int:
    """Highest page number linked from the pagination bar for this listing, else page+1 when a
    'next' link exists, else the current page (= last page)."""
    best = page
    pat = re.compile(r'href=["\'](?:https?://[^"\'/]+)?' + re.escape(base_path) + r'(\d+)/?(?:\?[^"\']*)?["\']', re.IGNORECASE)
    for m in pat.finditer(page_html or ""):
        try:
            best = max(best, int(m.group(1)))
        except ValueError:
            pass
    if best == page and got_items and re.search(r'class=["\'][^"\']*\bnext\b', page_html or "", re.IGNORECASE):
        best = page + 1
    return best


# Keywords that browse a SpankBang section instead of searching: /autouploadspankbang trending
_BROWSE_ALIASES = {
    "trending": "/trending_videos/", "trend": "/trending_videos/",
    "new": "/new_videos/", "latest": "/new_videos/",
    "popular": "/most_popular/", "top": "/most_popular/",
}


_SB_URL_RE = re.compile(r"^(?:https?://)?(?:www\.)?spankbang\.[a-z.]{2,}(?=/|$)", re.IGNORECASE)
_BROWSE_SECTIONS = ("trending_videos", "new_videos", "most_popular")


def _url_listing_base(term: str):
    """Listing base path (always ends with '/') when `term` is a SpankBang listing URL / path, else None.

    https://spankbang.com/s/mandy+flores/            -> /s/mandy+flores/
    https://spankbang.com/b4/pornstar/mandy+flores/  -> /b4/pornstar/mandy+flores/
    https://spankbang.com/60/channel/puretaboo/3/    -> /60/channel/puretaboo/   (page number dropped)
    https://spankbang.com/trending_videos/           -> /trending_videos/
    Video links and anything else -> None (a plain keyword is then searched as before)."""
    t = (term or "").strip()
    if not t:
        return None
    if t.startswith("/"):
        path = t
    elif _SB_URL_RE.match(t):
        path = urlsplit(t if "://" in t else "https://" + t).path
    else:
        return None
    segs = [x for x in path.split("/") if x]
    if len(segs) >= 2 and segs[0].lower() == "s":
        return f"/s/{segs[1]}/"
    if len(segs) >= 3 and segs[1].lower() in ("pornstar", "channel"):
        return f"/{segs[0]}/{segs[1].lower()}/{segs[2]}/"
    if segs and segs[0].lower() in _BROWSE_SECTIONS:
        return f"/{segs[0].lower()}/"
    return None


_PORNSTAR_A_RE = re.compile(r'href=["\'](?:https?://[^"\'/]+)?/([A-Za-z0-9]+)/pornstar/([^"\'/?#]+)/?', re.IGNORECASE)
_SEEN_PAGES: dict = {}  # listing base -> {page: frozenset(video slugs)}; detects "page N is just page M again"
_PS_CACHE: dict = {}  # normalised name -> pornstar base path ("/b4/pornstar/mandy+flores/") or None (no such pornstar page)
_PS_VIDEO_PAGES_TO_TRY = 3


def _name_key(text: str) -> str:
    """'Mandy Flores' / 'mandy+flores' / 'mandy-flores' / 'Mandy%20Flores' -> 'mandy flores'."""
    return " ".join(re.findall(r"[a-z0-9]+", unquote_plus(text or "").lower()))


def _pornstar_link_for(page_html: str, key: str):
    """First /<id>/pornstar/<slug>/ link in `page_html` whose slug is exactly the performer `key`, as a base path."""
    for m in _PORNSTAR_A_RE.finditer(page_html or ""):
        if _name_key(m.group(2)) == key:
            return f"/{m.group(1)}/pornstar/{m.group(2)}/"
    return None


def _pornstar_base(term: str):
    """Pornstar page base path for a plain-name `term`, so "Mandy flores" behaves exactly like pasting her
    /<id>/pornstar/mandy+flores/ link. SpankBang's pornstar URLs contain an id that cannot be guessed, so it is
    read from the search results page (pornstar card) and, failing that, from the first few result video pages
    (their performer links). None -> no pornstar page found; the caller then falls back to the plain /s/ search.
    Positive and negative answers are cached for the life of the process (the worker asks once per listing page)."""
    key = _name_key(term)
    if not key:
        return None
    if key in _PS_CACHE:
        return _PS_CACHE[key]
    search_html = _fetch(f"/s/{quote_plus(key)}/")
    if not search_html:
        return None  # network / Cloudflare problem: don't cache, try again next time
    found = _pornstar_link_for(search_html, key)
    if not found:
        for it in _parse_videos(search_html)[:_PS_VIDEO_PAGES_TO_TRY]:
            vhtml = _fetch(urlsplit(it["url"]).path)
            found = _pornstar_link_for(vhtml, key) if vhtml else None
            if found:
                break
    _PS_CACHE[key] = found
    logger.info(f"spankbang: performer {term!r} -> {found or 'no pornstar page found, using search results'}")
    return found


def _browse_base(term: str):
    """Base path of the listing `term` points to (alias word or pasted URL), None for a plain keyword search."""
    t = (term or "").strip()
    return _BROWSE_ALIASES.get(t.lower()) or _url_listing_base(t)


def _search_path(term: str, page: int) -> str:
    base = _browse_base(term) or f"/s/{quote_plus(term.strip().lower())}/"
    return base if page <= 1 else f"{base}{page}/"


def _listing_path(base: str, page: int) -> str:
    return base if page <= 1 else f"{base}{page}/"


# ---------------------------------------------------------------------------
# Public contract
# ---------------------------------------------------------------------------
async def get_model_page_videos(term: str, page: int = 1) -> tuple[list, int]:
    """term: performer name, tag or any keyword (SpankBang search doubles as the
    performer/tag lookup), trending / new / popular to page through that section, or a full SpankBang
    search / pornstar / channel / trending / new / popular URL. A plain performer name ("Mandy flores") is looked
    up to her pornstar page, so it gives the same videos as pasting that page's URL. Returns (items, total_pages)."""
    def _run():
        # alias word / pasted URL -> that listing; plain name -> the performer's pornstar page (same as pasting its link);
        # anything else (tags, keywords, unknown names) -> /s/ search
        base = _browse_base(term) or _pornstar_base(term) or f"/s/{quote_plus(term.strip().lower())}/"
        page_html = _fetch(_listing_path(base, page))
        if not page_html:
            return [], 1
        items = _parse_videos(page_html)
        # Past the last page SpankBang may redirect back to an earlier page instead of erroring; the worker would then loop
        # over already-uploaded videos forever. A page whose videos exactly repeat another page of this listing = the end.
        ids = frozenset(i["slug"] for i in items)
        seen = _SEEN_PAGES.setdefault(base, {})
        if ids and any(pg != page and ids == prev for pg, prev in seen.items()):
            logger.info(f"spankbang: {base} page {page} repeats an earlier page -> end of listing")
            return [], page
        seen[page] = ids
        return items, _total_pages(page_html, base, page, bool(items))
    return await asyncio.to_thread(_run)


async def get_random_page_videos() -> list:
    """Random mode: a random browse page (new/trending/popular) or a random keyword search page."""
    def _run():
        if random.random() < 0.5:
            base, max_page = random.choice(_RANDOM_PATHS)
            path = _listing_path(base, random.randint(1, max_page))
        else:
            path = _search_path(random.choice(_BROAD_TERMS), random.randint(1, 10))
        page_html = _fetch(path)
        return _parse_videos(page_html) if page_html else []
    return await asyncio.to_thread(_run)


async def get_latest_videos() -> list:
    """Newest uploads (page 1 of /new_videos/) -- polled by spankbang_live_monitor."""
    def _run():
        page_html = _fetch("/new_videos/")
        items = _parse_videos(page_html) if page_html else []
        if not items:
            logger.warning("spankbang get_latest_videos: no videos parsed (blocked by Cloudflare or markup changed)")
        return items
    return await asyncio.to_thread(_run)


async def get_trending_videos(page: int = 1) -> list:
    def _run():
        page_html = _fetch(_listing_path("/trending_videos/", page))
        return _parse_videos(page_html) if page_html else []
    return await asyncio.to_thread(_run)


def is_valid_spankbang_url(url: str) -> bool:
    """Kept for compatibility with the earlier scraper draft."""
    import spankbang_downloader as _dl
    return _dl.is_spankbang_link(url)


if __name__ == "__main__":
    sample = ('<div class="video-item"><a href="/5abcd/video/some-cool-title" title="Some &amp; Cool Title" class="thumb"></a>'
              '<a href="https://spankbang.com/6efgh/video/another-one?x=1" class="n">x</a></div>'
              '<ul class="paginate-bar"><li><a href="/s/milf/2/">2</a></li><li><a href="/s/milf/7/">7</a></li>'
              '<li class="next"><a href="/s/milf/2/">next</a></li></ul>')
    vids = _parse_videos(sample)
    print(vids)
    print(_total_pages(sample, "/s/milf/", 1, True), _search_path("Big Tits", 3), _listing_path("/new_videos/", 1))
    for u in ("https://spankbang.com/s/mandy+flores/", "https://spankbang.com/b4/pornstar/mandy+flores/",
              "https://spankbang.com/trending_videos/", "https://spankbang.com/60/channel/puretaboo/3/", "mandy flores", "trending"):
        print(u, "->", _search_path(u, 1), _search_path(u, 2))

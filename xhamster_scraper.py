"""
xhamster auto-scraper backend for auto_scraper.py's xhamster_uploader_worker
— same role as pornhub_scraper.py/xvideos_scraper.py, just for xHamster.

FIX v3:
  + Multi-strategy get_latest_videos():
    1. xHamster JSON API (no yt-dlp, works even when Render IP is blocked)
    2. HTML scraping fallback (urllib + regex)
    3. yt-dlp as absolute last resort
  + /videos/recent + /newest 404 on Render IPs — API used instead
  + xhamster2.com added as fallback domain
"""

import asyncio
import json
import logging
import re
import urllib.error
import urllib.request
from urllib.parse import urlparse, quote

import yt_dlp

logger = logging.getLogger(__name__)

PER_PAGE = 30

_DOMAINS = [
    "https://xhamster.com",
    "https://xhamster18.com",
    "https://xhamster2.com",
    "https://xhamster.desi",
]

_MODEL_URL_PATTERNS = [
    "{domain}/users/{slug}/videos",
    "{domain}/creators/{slug}",
    "{domain}/creators/videos/{slug}",
]

_STUDIO_URL_PATTERN = "{domain}/channels/{slug}"

_UNKNOWN_TOTAL_PAGES = 10_000
_CHROME_TARGET = "chrome-124"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
}

# xHamster JSON API endpoints for latest videos
_API_LATEST_URLS = [
    "https://xhamster.com/api/front/v2/videos?page=1&sort=newest",
    "https://xhamster.com/api/front/v2/videos?sort=newest&page=1",
    "https://xhamster18.com/api/front/v2/videos?page=1&sort=newest",
]

# HTML listing pages as fallback
_HTML_LATEST_URLS = [
    "https://xhamster.com/newest",
    "https://xhamster.com/best/weekly",
    "https://xhamster.com/",
    "https://xhamster18.com/newest",
    "https://xhamster2.com/newest",
    "https://xhamster.com/videos",
]


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower())
    return slug.strip("-")


def _extract_domain_from_url(url: str) -> str | None:
    try:
        parsed = urlparse(url)
        domain = f"{parsed.scheme}://{parsed.netloc}"
        if "xhamster" in parsed.netloc:
            return domain
    except Exception:
        pass
    return None


def _proxy_for(url: str):
    try:
        import cf_bypass
        return cf_bypass.proxy_for(url)          # XVIDEOS_PROXY / XHAMSTER_PROXY, or the free pool once a block was seen
    except Exception:
        return None


def _switch_proxy(url: str, err) -> bool:
    try:
        import free_proxy
        return free_proxy.switch_on_block(url, err)
    except Exception:
        return False


def _get_impersonated(url: str, proxy, timeout: int) -> str:
    """GET with a real Chrome TLS/HTTP2 fingerprint (curl_cffi). Plain urllib gets 404 from xHamster for bot-looking
    requests even when the page exists in a browser; the other downloaders in this bot already use this route."""
    from curl_cffi.requests import Session
    headers = {"Accept-Language": "en-US,en;q=0.9"}
    with Session(impersonate="chrome") as s:
        r = s.get(url, headers=headers, timeout=timeout, allow_redirects=True,
                  proxies={"http": proxy, "https": proxy} if proxy else None)
    if r.status_code >= 400:
        raise urllib.error.HTTPError(url, r.status_code, f"HTTP Error {r.status_code}", None, None)
    return r.text


def _get_urllib(url: str, proxy, timeout: int) -> str:
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.build_opener(*handlers).open(req, timeout=timeout) as resp:   # TLS verified (default)
        return resp.read().decode("utf-8", errors="replace")


def _fetch_url(url: str, timeout: int = 15) -> str:
    """HTTP GET for xHamster pages. Browser-impersonated first; plain urllib only if curl_cffi is unavailable.
    Direct first; if the site blocks this IP (403/429/timeout) the request is retried through the site's proxy
    (see cf_bypass.proxy_for / free_proxy.py), up to twice."""
    for attempt in range(3):
        px = _proxy_for(url)
        try:
            try:
                return _get_impersonated(url, px, timeout)
            except ImportError:
                return _get_urllib(url, px, timeout)
        except Exception as e:
            if attempt < 2 and _switch_proxy(url, e):
                continue
            raise


def _canon(u: str) -> str:
    """xhaccess.<tld> is an xHamster mirror yt-dlp doesn't recognise: use xhamster.com (same site, same video id)."""
    return re.sub(r"^(https?://)(?:www\.)?(?:xhaccess\.[a-z]{2,10}|xhms\.pro|xhday\.com|xhvid\.com)", r"\1xhamster.com", u or "", flags=re.I)


def _items_from_xhamster_json(raw: str) -> list:
    """Parse xHamster API JSON response."""
    try:
        data = json.loads(raw)
    except Exception:
        return []
    # Try different response shapes
    videos = (data.get("videos") or {}).get("items") or \
             data.get("items") or \
             data.get("data") or []
    items = []
    for v in videos:
        vid_url = v.get("pageURL") or v.get("url") or v.get("link")
        if not vid_url or not vid_url.startswith("http"):
            continue
        vid_url = _canon(vid_url)
        vid_id = str(v.get("id") or vid_url)
        items.append({
            "slug": f"xhamster-{vid_id}",
            "url": vid_url,
            "title": v.get("title") or vid_id,
        })
    return items


# Bare-id video URLs (/videos/xhQyTtg, no title slug) are listed by xHamster but yt-dlp rejects them as
# "Unsupported URL". Follow the redirect to the full page URL (with slug) before downloading.
_SHORT_VIDEO_RE = re.compile(r"/videos/[A-Za-z0-9]{5,12}/?$")
_FULL_VIDEO_RE = re.compile(r"/videos/[A-Za-z0-9-]+-[A-Za-z0-9]+/?$")
_resolved_urls: dict = {}


def _resolve_short_video_url(url: str) -> str:
    if not _SHORT_VIDEO_RE.search(url):
        return url
    if url in _resolved_urls:
        return _resolved_urls[url]
    final = url
    try:
        from curl_cffi.requests import Session
        with Session(impersonate="chrome") as s:
            r = s.get(url, timeout=15, allow_redirects=True)
        if r.status_code < 400 and _FULL_VIDEO_RE.search(urlparse(r.url).path):
            final = _canon(r.url).split("?")[0]
    except Exception as e:
        logger.info(f"[xhamster] could not resolve short video URL {url}: {e}")
    _resolved_urls[url] = final
    return final


def _items_from_xhamster_html(html: str) -> list:
    """Scrape video URLs from xHamster listing HTML."""
    # xhamster embeds video data as JSON in script tags
    pattern = re.compile(r'videos\s*:\s*(\[.*?\])', re.DOTALL)
    match = pattern.search(html)
    if match:
        try:
            videos = json.loads(match.group(1))
            items = []
            for v in videos:
                vid_url = v.get("pageURL") or v.get("url")
                if not vid_url or not vid_url.startswith("http"):
                    continue
                vid_url = _resolve_short_video_url(_canon(vid_url))
                vid_id = str(v.get("id") or vid_url)
                items.append({
                    "slug": f"xhamster-{vid_id}",
                    "url": vid_url,
                    "title": v.get("title") or vid_id,
                })
            if items:
                return items
        except Exception:
            pass

    # Fallback: href regex (absolute or relative /videos/<slug>-<id> links)
    items = []
    seen = set()
    for m in re.finditer(r'href="((?:https?://(?:www\.)?(?:xhamster\d*|xhaccess)\.[a-z]{2,10})?/videos/[^"#?]+)"', html):
        u = _canon(m.group(1))
        if u.startswith("/"):
            u = "https://xhamster.com" + u
        u = _resolve_short_video_url(u)
        if u in seen:
            continue
        seen.add(u)
        vid_id = u.rstrip("/").rsplit("/", 1)[-1]
        items.append({"slug": f"xhamster-{vid_id}", "url": u, "title": vid_id.replace("-", " ")})
    return items


def _flat_entries_once(url: str, playlist_start: int, playlist_end: int):
    opts = {
        "quiet":              True,
        "no_warnings":        True,
        "extract_flat":       "in_playlist",
        "playliststart":      playlist_start,
        "playlistend":        playlist_end,
        "skip_download":      True,
        "socket_timeout":     20,
        "nocheckcertificate": True,
    }
    px = _proxy_for(url)
    if px:
        opts["proxy"] = px
        opts["nocheckcertificate"] = False       # never skip TLS verification through a (public) proxy
    last_err = None
    try:
        with yt_dlp.YoutubeDL({**opts, "impersonate": _CHROME_TARGET}) as ydl:
            info = ydl.extract_info(url, download=False)
        entries = (info or {}).get("entries") or []
        if entries:
            return entries, None
    except Exception as e:
        last_err = e
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return (info or {}).get("entries") or [], None
    except Exception as e:
        logger.debug(f"[xhamster] _flat_entries failed for {url}: {e}")
        return None, e


def _flat_entries(url: str, playlist_start: int, playlist_end: int) -> list:
    """Listing via yt-dlp. Direct first; if the site blocked this IP, retry through the site's proxy (up to twice)."""
    for attempt in range(3):
        entries, err = _flat_entries_once(url, playlist_start, playlist_end)
        if entries or err is None:
            return entries or []
        if attempt < 2 and _switch_proxy(url, err):
            continue
        return []
    return []


def _try_urls_with_fallback(urls: list, playlist_start: int, playlist_end: int) -> list:
    # FIX: this used to `return entries` on the FIRST url pattern that
    # returned ANYTHING non-empty — including just 1 entry. If yt-dlp's
    # extractor happens to treat one of the earlier url patterns (e.g.
    # "{domain}/users/{slug}/videos") as a single-video page rather than
    # a proper playlist listing for a given performer, that single entry
    # "succeeded" and the loop stopped right there, never trying the
    # other patterns (e.g. "{domain}/creators/{slug}") that might
    # correctly return the performer's full video list. This is exactly
    # what "auto-upload by actor name only finds 1 video" looks like.
    # Now every pattern gets tried and the one with the most entries
    # wins, so a single-video false-positive from one pattern can't
    # shadow a working multi-video result from another.
    best_entries: list = []
    best_url = None
    for url in urls:
        try:
            entries = _flat_entries(url, playlist_start, playlist_end)
        except Exception as e:
            logger.debug(f"[xhamster] failed {url}: {e}")
            continue
        if entries:
            logger.debug(f"[xhamster] got {len(entries)} entries from {url}")
        if len(entries) > len(best_entries):
            best_entries, best_url = entries, url
        # A pattern that already returns a full page is as good as it
        # gets — no need to keep hitting every remaining domain/pattern
        # combination once we've clearly found the real listing.
        if len(best_entries) >= (playlist_end - playlist_start + 1):
            break
    if best_entries:
        logger.debug(f"[xhamster] using {len(best_entries)} entries from {best_url}")
        return best_entries
    logger.warning(f"[xhamster] all URLs failed. Tried: {urls}")
    return []


def _entry_to_item(entry: dict) -> dict | None:
    video_url = entry.get("webpage_url") or entry.get("url")
    if not video_url or not video_url.startswith("http"):
        return None
    video_url = _resolve_short_video_url(video_url)
    video_id = entry.get("id") or video_url
    slug = f"xhamster-{video_id}"
    return {"slug": slug, "url": video_url, "title": entry.get("title") or slug}


def _term_to_slug(term: str, url_path_prefixes: tuple) -> str:
    if term.startswith("http"):
        path = urlparse(term).path.strip("/")
        segments = path.split("/")
        if len(segments) >= 3 and segments[0] == "creators" and segments[1] == "videos":
            return segments[2]  # /creators/videos/<x>
        if len(segments) >= 2 and segments[0] in url_path_prefixes:
            return segments[1]
        if len(segments) >= 1 and segments[0] in url_path_prefixes:
            return term.rstrip("/").rsplit("/", 1)[-1]
    return _slugify(term)


def _fetch_latest_items_sync() -> list:
    """
    Multi-strategy latest video fetch:
    1. xHamster JSON API (most reliable — no yt-dlp needed)
    2. HTML scraping fallback
    3. yt-dlp as last resort
    """
    # Strategy 1: JSON API
    for url in _API_LATEST_URLS:
        try:
            raw = _fetch_url(url)
            items = _items_from_xhamster_json(raw)
            if items:
                logger.info(f"[xhamster] ✅ latest via JSON API: {len(items)} items from {url}")
                return items
            else:
                logger.warning(f"[xhamster] JSON API returned empty from {url}")
        except Exception as e:
            logger.warning(f"[xhamster] JSON API failed {url}: {e}")

    # Strategy 2: HTML scrape
    for url in _HTML_LATEST_URLS:
        try:
            html = _fetch_url(url)
            items = _items_from_xhamster_html(html)
            if items:
                logger.info(f"[xhamster] ✅ latest via HTML scrape: {len(items)} items from {url}")
                return items
            else:
                logger.warning(f"[xhamster] HTML scrape returned empty from {url}")
        except Exception as e:
            logger.warning(f"[xhamster] HTML scrape failed {url}: {e}")

    logger.warning("[xhamster] all strategies failed (JSON API, HTML pages) — returning empty list. "
                   "If this persists, the listing pages changed; check the URLs in _HTML_LATEST_URLS.")
    return []


# Performer page shapes on xHamster (same idea as xvideos' _MODEL_URL_PREFIXES): try every one, yt-dlp first
# (it handles users/creators), then a plain HTML read of each shape. /pornstars/ is HTML-only (no yt-dlp extractor).
_MODEL_SHAPES = (
    "{domain}/pornstars/{slug}",
    "{domain}/creators/{slug}",
    "{domain}/users/{slug}/videos",
    "{domain}/{slug}",            # bare username
)


def _html_model_items(base: str, page: int) -> list:
    """Performer/model page read from its HTML, paged with ?page=N."""
    url = base if page == 1 else f"{base}?page={page}"
    try:
        return _items_from_xhamster_html(_fetch_url(url))
    except Exception as e:
        logger.debug(f"[xhamster] model page {url} not readable: {e}")
        return []


async def get_model_page_videos(term: str, page: int = 1) -> tuple[list, int]:
    slug  = _term_to_slug(term, ("users", "creators", "pornstars"))
    start = (page - 1) * PER_PAGE + 1
    end   = page * PER_PAGE
    custom_domain = _extract_domain_from_url(term)
    domains = [custom_domain] if custom_domain else _DOMAINS

    # A pasted link: its own URL is the first candidate for both yt-dlp and HTML.
    own = _canon(term).split("?")[0].rstrip("/") if term.startswith("http") else None

    # 1) yt-dlp — only the shapes its extractors understand (users, creators)
    yt_urls = [u for d in domains for u in (f"{d}/creators/{slug}", f"{d}/users/{slug}/videos")]
    if own and "/pornstars/" not in own:
        yt_urls.insert(0, own)
    try:
        entries = await asyncio.to_thread(_try_urls_with_fallback, yt_urls, start, end)
        items = [item for e in entries if (item := _entry_to_item(e)) is not None]
        if items:
            return items, _UNKNOWN_TOTAL_PAGES
    except Exception as e:
        logger.warning(f"[xhamster] get_model_page_videos yt-dlp path failed for {term!r}: {e}")

    # 2) HTML — every shape, like xvideos does; the pasted link (if any) goes first
    html_bases = [own] if own else []
    html_bases += [shape.format(domain=d, slug=slug) for d in domains for shape in _MODEL_SHAPES]
    seen = set()
    for base in html_bases:
        base = base.split("?")[0].rstrip("/")
        if base in seen:
            continue
        seen.add(base)
        items = await asyncio.to_thread(_html_model_items, base, page)
        if items:
            return items, _UNKNOWN_TOTAL_PAGES

    # 3) search fallback (page 1 only): the performer uses a different username than the typed name
    if page == 1:
        logger.info(f"[xhamster] no page found for {term!r}, trying search fallback...")
        for domain in domains:
            search_url = f"{domain}/search/videos?q={quote(term, safe='')}"
            try:
                entries = await asyncio.to_thread(_flat_entries, search_url, 1, PER_PAGE)
                items = [item for e in entries if (item := _entry_to_item(e)) is not None]
                if items:
                    return items, _UNKNOWN_TOTAL_PAGES
            except Exception as e:
                logger.debug(f"[xhamster] search fallback failed for {domain}: {e}")

    return [], 0


def url_kind(term: str) -> str:
    """'pornstar' (performer page), 'studio' (channels/<slug>), 'listing' (any other xHamster list page),
    or 'name' (plain performer/studio name, not a link)."""
    if not term or not term.startswith("http"):
        return "name"
    segs = [s for s in urlparse(_canon(term)).path.strip("/").split("/") if s]
    if segs and segs[0] in ("users", "creators", "pornstars"):
        return "pornstar"
    if len(segs) >= 2 and segs[0] == "channels":
        return "studio"
    return "listing"


async def get_listing_page_videos(url: str, page: int = 1) -> tuple[list, int]:
    """Any xHamster list page (best/weekly, categories/<x>, newest, ...) read through yt-dlp, paged by playlist range."""
    start = (page - 1) * PER_PAGE + 1
    end   = page * PER_PAGE
    target = _canon(url)
    try:
        entries = await asyncio.to_thread(_flat_entries, target, start, end)
    except Exception as e:
        logger.warning(f"[xhamster] get_listing_page_videos failed for {url!r}: {e}")
        return [], 0
    items = [item for e in entries if (item := _entry_to_item(e)) is not None]
    return items, _UNKNOWN_TOTAL_PAGES


async def get_studio_page_videos(term: str, page: int = 1) -> tuple[list, int]:
    slug  = _term_to_slug(term, ("channels",))
    start = (page - 1) * PER_PAGE + 1
    end   = page * PER_PAGE
    custom_domain  = _extract_domain_from_url(term)
    domains_to_try = [custom_domain] if custom_domain else _DOMAINS
    urls_to_try    = [
        _STUDIO_URL_PATTERN.format(domain=domain, slug=slug)
        for domain in domains_to_try
    ]
    try:
        entries = await asyncio.to_thread(_try_urls_with_fallback, urls_to_try, start, end)
    except Exception as e:
        logger.warning(f"[xhamster] get_studio_page_videos failed for {term!r}: {e}")
        return [], 0
    items = [item for e in entries if (item := _entry_to_item(e)) is not None]
    return items, _UNKNOWN_TOTAL_PAGES


async def get_latest_videos() -> list:
    """
    Multi-strategy latest video fetch — JSON API first, HTML scrape second,
    yt-dlp last resort. Handles Render/cloud IP blocks gracefully.
    """
    try:
        items = await asyncio.to_thread(_fetch_latest_items_sync)
        return items
    except Exception as e:
        logger.warning(f"[xhamster] get_latest_videos failed: {e}")
        return []


async def get_random_page_videos() -> list:
    return await get_latest_videos()

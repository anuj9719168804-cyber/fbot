"""
Free-proxy fallback for PeekVids / PlayVids, xVideos and xHamster (adapted from the TeraBox rotating-proxy resolver).

Why: the bot's server often can't reach peekvids.com directly (DNS failure, "Rate Limit Exceeded" / bot page).  This module
finds a public HTTP proxy that can actually load the site and hands it to cf_bypass.proxy_for(), which is the single place
yt-dlp, the legacy extractor, the Chrome fallback and FlareSolverr all take their proxy from - so every request of one
download leaves through the same IP.

How it behaves:
  * Proxy first (default): every PeekVids/PlayVids request goes through a free proxy; only when no working proxy can be
    found does it fall back to a direct connection.  A proxy that dies mid-request is dropped and replaced (ytdlp_downloader
    retries up to twice with a new one).  PEEKVIDS_FREE_PROXY_MODE=fallback switches to the old behaviour: direct first, proxy
    only after a direct attempt failed (ytdlp_downloader calls enable_mode()) and then for PROXY_MODE_SECONDS (15 min).
  * A candidate only counts as "working" if it returns the real PeekVids home page (HTTP 200, no rate-limit page).
  * The working proxy is cached; discovery is serialised (one thread searches, the rest wait for its answer) and a failed
    search is not repeated for 60 s.
  * PEEKVIDS_PROXY (a proper proxy you pay for) always wins; PEEKVIDS_FREE_PROXY=0 switches this module off.
  * FREE_PROXY_SOURCES=url1,url2 adds extra plain-text "ip:port" lists.

Per-site settings (site = PEEKVIDS | XVIDEOS | XHAMSTER):
  * <SITE>_PROXY (paid/own proxy) always wins; <SITE>_FREE_PROXY=0 switches the free pool off for that site.
  * <SITE>_FREE_PROXY_MODE=first|fallback. Default "first" for all three: every request goes through a free proxy; only when
    no working proxy can be found does it fall back to a direct connection. "fallback" = direct connection first, free pool
    only after a block (403/429/timeout/Cloudflare page), then for 15 min.
  * Every candidate must load the real site over verified TLS, so a proxy that tampers with HTTPS is rejected; the
    xVideos/xHamster callers also keep certificate verification ON while a proxy is in use.

Trade-off: public proxies are slow, die within minutes and can read/alter what goes through them - fine for a public
video page, but a paid PEEKVIDS_PROXY is the real fix if this keeps being needed.
"""
from __future__ import annotations

import concurrent.futures
import logging
import os
import re
import threading
import time
from typing import List, Optional

import requests

logger = logging.getLogger("faphouse_bot")

TEST_URL = os.environ.get("FREE_PROXY_TEST_URL", "https://www.peekvids.com/")   # PeekVids (kept for compatibility)
# site -> (env prefix, page that must load through the proxy, text that real page contains, default mode is proxy-first?)
_SITES = {
    "peekvids": ("PEEKVIDS", TEST_URL, "peekvids", True),
    "xvideos":  ("XVIDEOS", os.environ.get("XVIDEOS_TEST_URL", "https://www.xvideos.com/"), "xvideos", True),
    "xhamster": ("XHAMSTER", os.environ.get("XHAMSTER_TEST_URL", "https://xhamster.com/"), "xhamster", True),
    "spankbang": ("SPANKBANG", os.environ.get("SPANKBANG_TEST_URL", "https://spankbang.com/"), "spankbang", True),
}
_DEFAULT_SITE = "peekvids"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0.0.0 Safari/537.36")
_SOURCES = [
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=3000&country=all&ssl=yes&anonymity=elite",
    "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt",
] + [s.strip() for s in os.environ.get("FREE_PROXY_SOURCES", "").split(",") if s.strip()]

PROXY_MODE_SECONDS = 15 * 60
_POOL_TTL = 300            # re-download the proxy lists at most every 5 min
_WORKING_TTL = 10 * 60     # a found proxy is trusted for 10 min, then searched again
_NONE_TTL = 60             # after a search that found nothing, don't search again for a minute
_CANDIDATES = 30
_SEARCH_DEADLINE = 25      # seconds for one whole discovery

_lock = threading.Lock()
_pool: List[str] = []
_pool_ts = 0.0
_BAD_TTL = 10 * 60


class _SiteState:
    __slots__ = ("working", "working_ts", "mode_until", "none_until", "bad")

    def __init__(self):
        self.working: Optional[str] = None
        self.working_ts = 0.0
        self.mode_until = 0.0
        self.none_until = 0.0
        self.bad: dict = {}      # proxy -> time until which it is not picked again (passed the check, then failed for real)


_state = {name: _SiteState() for name in _SITES}


def site_for(url: str) -> Optional[str]:
    """'peekvids' | 'xvideos' | 'xhamster' for a link of that site family, else None."""
    u = (url or "").lower()
    if "peekvids" in u or "playvids" in u:
        return "peekvids"
    if re.search(r"xvideos\d*\.", u):
        return "xvideos"
    if re.search(r"xhamster(?:live)?\d*\.|xhaccess|xhms\.pro|xhday\.com|xhvid\.com", u):
        return "xhamster"
    if "spankbang" in u:
        return "spankbang"
    return None


def enabled(site: str = _DEFAULT_SITE) -> bool:
    pre = _SITES[site][0]
    return os.environ.get(f"{pre}_FREE_PROXY", "1").strip().lower() not in ("0", "false", "no", "off")


def proxy_first(site: str = _DEFAULT_SITE) -> bool:
    """Default for every site: through a free proxy first (direct only when none can be found).
    <SITE>_FREE_PROXY_MODE=fallback = direct first, proxy only after a block."""
    pre, _t, _n, first_default = _SITES[site]
    mode = os.environ.get(f"{pre}_FREE_PROXY_MODE", "first" if first_default else "fallback").strip().lower()
    return mode != "fallback"


def mode_active(site: str = _DEFAULT_SITE) -> bool:
    return proxy_first(site) or time.time() < _state[site].mode_until


def enable_mode(seconds: int = PROXY_MODE_SECONDS, site: str = _DEFAULT_SITE) -> bool:
    """Start routing this site through a free proxy. -> True if this switched it on just now (the caller should retry
    its request), False if it was already on."""
    st = _state[site]
    newly = not mode_active(site)
    st.mode_until = time.time() + seconds
    if newly:
        st.none_until = 0.0
        logger.info("🔀 free_proxy: direct connection to %s failed/blocked - switching to a free proxy for %d min.",
                    site, seconds // 60)
    return newly


def invalidate(site: str = _DEFAULT_SITE) -> bool:
    """The cached proxy didn't help (died / timed out mid-request): forget it, never pick it again for 10 min, so the
    next get_proxy() searches for a different one. -> True if there was a proxy to drop (a retry makes sense)."""
    st = _state[site]
    with _lock:
        dropped = st.working
        if dropped:
            logger.info("free_proxy[%s]: dropping %s", site, dropped)
            st.bad[dropped] = time.time() + _BAD_TTL
        st.working, st.working_ts = None, 0.0
    return bool(dropped)


_BLOCK_HINTS = ("403", "429", "forbidden", "too many", "rate limit", "timed out", "timeout", "connection", "unable to download",
                "cloudflare", "just a moment", "captcha", "blocked", "temporary failure", "name or service", "reset by peer",
                "remote end closed", "ssl", "proxy")


def looks_blocked(err) -> bool:
    """Does this error look like an IP block / network problem (worth a proxy retry) rather than a removed video?"""
    if err is None:
        return True
    t = str(err).lower()
    if any(w in t for w in ("video unavailable", "has been removed", "deleted", "not found", "404", "private video",
                            "premium", "members only", "age-verif")):
        return False
    return any(w in t for w in _BLOCK_HINTS)


def switch_on_block(url: str, err=None) -> bool:
    """A direct request to xVideos/xHamster (or PeekVids) just failed. -> True when the caller should retry right now
    (free proxy just switched on, or the one in use died and a different one will be picked); False when there is
    nothing new to try: other site, own paid proxy configured, free pool disabled, or the error isn't block-like."""
    site = site_for(url)
    if not site or not enabled(site) or not looks_blocked(err):
        return False
    try:
        import cf_bypass
        if cf_bypass.has_paid_proxy(site):
            return False
    except Exception:
        pass
    return enable_mode(site=site) or invalidate(site)


def _normalize(p: str) -> str:
    p = p.strip()
    return p if "://" in p else f"http://{p}"


def _fetch_pool() -> List[str]:
    global _pool, _pool_ts
    now = time.time()
    if _pool and now - _pool_ts < _POOL_TTL:
        return _pool
    found: List[str] = []
    for src in _SOURCES:
        try:
            r = requests.get(src, timeout=6)
            if r.status_code == 200:
                found.extend(_normalize(ln) for ln in r.text.splitlines()
                             if ln.strip() and ":" in ln and not ln.lstrip().startswith("#"))
                if len(found) >= 60:
                    break
        except Exception:
            continue
    _pool = list(dict.fromkeys(found))[:100]
    _pool_ts = now
    return _pool


def _check(proxy: str, site: str = _DEFAULT_SITE) -> bool:
    """Does this proxy load the real site (not a rate-limit / captcha / challenge / blank page) over VERIFIED TLS?"""
    _pre, test_url, needle, _f = _SITES[site]
    try:
        r = requests.get(test_url, proxies={"http": proxy, "https": proxy}, headers={"User-Agent": _UA},
                         timeout=(5, 8))                      # verify=True (default): a TLS-tampering proxy fails here
        head = r.text[:20000]
        return (r.status_code == 200 and "Rate Limit Exceeded" not in head and "Just a moment" not in head
                and needle in head.lower())
    except Exception:
        return False


def _discover(site: str = _DEFAULT_SITE) -> Optional[str]:
    st = _state[site]
    pool = _fetch_pool()
    if not pool:
        logger.info("free_proxy: no proxy lists reachable")
        return None
    now = time.time()
    for p in [p for p, until in st.bad.items() if until <= now]:
        st.bad.pop(p, None)
    candidates = [p for p in pool if p not in st.bad][:_CANDIDATES]
    if not candidates:
        return None
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=15)
    try:
        futs = {ex.submit(_check, p, site): p for p in candidates}
        try:
            for fut in concurrent.futures.as_completed(futs, timeout=_SEARCH_DEADLINE):
                try:
                    if fut.result():
                        return futs[fut]
                except Exception:
                    continue
        except concurrent.futures.TimeoutError:
            pass
    finally:
        ex.shutdown(wait=False, cancel_futures=True)   # don't sit through every slow candidate once one has won
    return None


def get_proxy(site: str = _DEFAULT_SITE) -> Optional[str]:
    """The proxy URL to use right now for this site, or None (direct)."""
    st = _state[site]
    if not enabled(site) or not mode_active(site):
        return None
    with _lock:
        now = time.time()
        if st.working and now - st.working_ts < _WORKING_TTL:
            return st.working
        if now < st.none_until:
            return None
        proxy = _discover(site)
        if proxy:
            st.working, st.working_ts = proxy, time.time()
            logger.info("✅ free_proxy[%s]: using %s", site, proxy)
            return proxy
        st.none_until = time.time() + _NONE_TTL
        logger.warning("free_proxy[%s]: no working free proxy found (will retry in %ds)", site, _NONE_TTL)
        return None

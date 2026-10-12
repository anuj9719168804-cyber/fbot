"""
Free-proxy fallback for PeekVids / PlayVids (adapted from the TeraBox rotating-proxy resolver).

Why: the bot's server often can't reach peekvids.com directly (DNS failure, "Rate Limit Exceeded" / bot page).  This module
finds a public HTTP proxy that can actually load the site and hands it to cf_bypass.proxy_for(), which is the single place
yt-dlp, the legacy extractor, the Chrome fallback and FlareSolverr all take their proxy from - so every request of one
download leaves through the same IP.

How it behaves:
  * Direct connection first.  The proxy is only used after a direct attempt failed (ytdlp_downloader calls enable_mode()),
    and then for PROXY_MODE_SECONDS (15 min).  No proxy mode -> get_proxy() returns None and nothing here costs anything.
  * A candidate only counts as "working" if it returns the real PeekVids home page (HTTP 200, no rate-limit page).
  * The working proxy is cached; discovery is serialised (one thread searches, the rest wait for its answer) and a failed
    search is not repeated for 60 s.
  * PEEKVIDS_PROXY (a proper proxy you pay for) always wins; PEEKVIDS_FREE_PROXY=0 switches this module off.
  * FREE_PROXY_SOURCES=url1,url2 adds extra plain-text "ip:port" lists.

Trade-off: public proxies are slow, die within minutes and can read/alter what goes through them - fine for a public
video page, but a paid PEEKVIDS_PROXY is the real fix if this keeps being needed.
"""
from __future__ import annotations

import concurrent.futures
import logging
import os
import threading
import time
from typing import List, Optional

import requests

logger = logging.getLogger("faphouse_bot")

TEST_URL = os.environ.get("FREE_PROXY_TEST_URL", "https://www.peekvids.com/")
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
_working: Optional[str] = None
_working_ts = 0.0
_mode_until = 0.0
_none_until = 0.0


def enabled() -> bool:
    return os.environ.get("PEEKVIDS_FREE_PROXY", "1").strip().lower() not in ("0", "false", "no", "off")


def mode_active() -> bool:
    return time.time() < _mode_until


def enable_mode(seconds: int = PROXY_MODE_SECONDS) -> bool:
    """Start routing PeekVids/PlayVids through a free proxy. -> True if this switched it on just now (the caller
    should retry its request), False if it was already on."""
    global _mode_until, _none_until
    newly = not mode_active()
    _mode_until = time.time() + seconds
    if newly:
        _none_until = 0.0
        logger.info("🔀 free_proxy: direct connection to PeekVids failed - switching to a free proxy for %d min.", seconds // 60)
    return newly


def invalidate() -> None:
    """The cached proxy didn't help: forget it so the next get_proxy() searches for another one."""
    global _working, _working_ts
    with _lock:
        if _working:
            logger.info("free_proxy: dropping %s", _working)
        _working, _working_ts = None, 0.0


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


def _check(proxy: str) -> bool:
    """Does this proxy load the real site (not a rate-limit / captcha / blank page)?"""
    try:
        r = requests.get(TEST_URL, proxies={"http": proxy, "https": proxy}, headers={"User-Agent": _UA},
                         timeout=(5, 8))
        head = r.text[:20000]
        return r.status_code == 200 and "Rate Limit Exceeded" not in head and "peekvids" in head.lower()
    except Exception:
        return False


def _discover() -> Optional[str]:
    pool = _fetch_pool()
    if not pool:
        logger.info("free_proxy: no proxy lists reachable")
        return None
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=15)
    try:
        futs = {ex.submit(_check, p): p for p in pool[:_CANDIDATES]}
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


def get_proxy() -> Optional[str]:
    """The proxy URL to use right now, or None (direct)."""
    global _working, _working_ts, _none_until
    if not enabled() or not mode_active():
        return None
    with _lock:
        now = time.time()
        if _working and now - _working_ts < _WORKING_TTL:
            return _working
        if now < _none_until:
            return None
        proxy = _discover()
        if proxy:
            _working, _working_ts = proxy, time.time()
            logger.info("✅ free_proxy: using %s", proxy)
            return proxy
        _none_until = time.time() + _NONE_TTL
        logger.warning("free_proxy: no working free proxy found (will retry in %ds)", _NONE_TTL)
        return None

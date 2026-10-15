"""
Cloudflare JS-challenge bypass via FlareSolverr — for links where the
site's own Cloudflare "checking your browser" challenge requires actually
running JavaScript, which curl_cffi's TLS/JA3 impersonation (used
everywhere else in ytdlp_downloader.py) can't do: impersonation only
copies a browser's network-level fingerprint, it doesn't execute a
challenge page's JS. Confirmed live on a spankbang.party mirror link:
curl_cffi with impersonate="chrome" still got back Cloudflare's
challenge-error-text page (a real JS challenge), not a 403 that
impersonation could talk its way past.

FlareSolverr (https://github.com/FlareSolverr/FlareSolverr) is a small
standalone HTTP service that runs a real headless browser, solves the
challenge, and hands back the resulting cf_clearance cookie + matching
User-Agent — which yt-dlp can then reuse for the actual extraction/
download like a normal browser session (no need to run a browser for
every single request, just once per site until the cookie expires).

THIS IS OPTIONAL INFRASTRUCTURE, not something this file can set up on
its own: FlareSolverr has to be running as its own service (see the
setup note at the bottom of this docstring). Every function here fails
gracefully if it isn't reachable — ytdlp_downloader.py falls back to
exactly the plain-403 behavior it had before this existed, it doesn't
break anything by being installed-but-unreachable.

Setup (once, on the VPS):
    docker run -d --name flaresolverr --restart unless-stopped \\
        -p 8191:8191 -e LOG_LEVEL=info ghcr.io/flaresolverr/flaresolverr:latest
No Docker? See https://github.com/FlareSolverr/FlareSolverr#installation
for a plain-binary install instead. Once it's running, this module finds
it automatically at http://localhost:8191/v1 (override with the
FLARESOLVERR_URL env var if it's running elsewhere) — nothing else to
configure.
"""

import http.cookiejar
import logging
import os
import tempfile
import threading
import time
from urllib.parse import urlparse

import requests

logger = logging.getLogger("faphouse_bot")

FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://localhost:8191/v1")
_SOLVE_TIMEOUT = 60  # was 25 -- a real challenge on a busy/small VPS often needs 30-50s; was: keep this well under the ~30s a genuine solve needs at
# most — if FlareSolverr isn't actually reachable/working (unverified live —
# see this file's own docstring), a caller shouldn't be stuck waiting a full
# 60s to find that out on every matching 403, on every site, before falling
# through to the plain error it would've gotten anyway.
_CACHE_TTL = 20 * 60  # cf_clearance cookies commonly last 30min-2h; 20min is a safe floor

# Optional proxy for SpankBang only (e.g. http://user:pass@host:port). A datacenter/VPS IP is often
# blocked outright by SpankBang's Cloudflare (plain HTTP 403) -- a residential/rotating proxy fixes that.
# Used by yt-dlp, the listing scraper AND FlareSolverr, so the cf_clearance cookie matches the IP.
SPANKBANG_PROXY = os.environ.get("SPANKBANG_PROXY", "").strip()
# Same idea for PeekVids / PlayVids (one site family): PEEKVIDS_PROXY=http://user:pass@host:port. Used by yt-dlp, the legacy
# extractor (its webpage requests), the Chrome fallback and FlareSolverr - so every request leaves through the same IP.
PEEKVIDS_PROXY = os.environ.get("PEEKVIDS_PROXY", "").strip()
# xVideos / xHamster: XVIDEOS_PROXY / XHAMSTER_PROXY=http://user:pass@host:port (a paid/own proxy always wins). Without one,
# free_proxy.py (public proxies) is used - by default for every request (direct only if no working proxy is found).
XVIDEOS_PROXY = os.environ.get("XVIDEOS_PROXY", "").strip()
XHAMSTER_PROXY = os.environ.get("XHAMSTER_PROXY", "").strip()
# MX Player: MXPLAYER_PROXY=http://user:pass@host:port (own/paid proxy wins). Without one, free_proxy.py is used.
MXPLAYER_PROXY = os.environ.get("MXPLAYER_PROXY", "").strip()

_cache: dict[str, dict] = {}  # domain -> {"cookiefile": path, "user_agent": str, "expires": ts}
_cache_lock = threading.Lock()
_unreachable_warned = False  # log the "not running" warning once, not on every call


def _domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
    except Exception:
        return url


def has_paid_proxy(site: str) -> bool:
    """Is an own/paid proxy configured for this site family ('peekvids' | 'xvideos' | 'xhamster' | 'spankbang')?"""
    return bool({"peekvids": PEEKVIDS_PROXY, "xvideos": XVIDEOS_PROXY, "xhamster": XHAMSTER_PROXY,
                 "spankbang": SPANKBANG_PROXY, "mxplayer": MXPLAYER_PROXY}.get(site))


def proxy_for(url: str) -> str | None:
    """Proxy URL to use for this URL's site, or None. SPANKBANG_PROXY / PEEKVIDS_PROXY / XVIDEOS_PROXY / XHAMSTER_PROXY
    when set; otherwise the free-proxy pool for PeekVids/PlayVids, xVideos and xHamster (when its mode is active)."""
    d = _domain_of(url)
    if SPANKBANG_PROXY and "spankbang" in d:
        return SPANKBANG_PROXY
    try:
        import free_proxy
        site = free_proxy.site_for(d)
    except Exception:
        return None
    if not site:
        return None
    paid = {"peekvids": PEEKVIDS_PROXY, "xvideos": XVIDEOS_PROXY, "xhamster": XHAMSTER_PROXY,
            "spankbang": SPANKBANG_PROXY, "mxplayer": MXPLAYER_PROXY}[site]
    if paid:
        return paid
    # no paid proxy configured: free_proxy hands out a public one (default: always; *_FREE_PROXY_MODE=fallback: after a block)
    try:
        return free_proxy.get_proxy(site)
    except Exception:
        return None


def get_bypass_opts(url: str) -> dict | None:
    """Non-blocking — just checks the cache. Returns
    {"cookiefile": path, "http_headers": {"User-Agent": ...}} for
    _base_opts() to merge in if a still-fresh solve exists for this
    URL's domain, else None. Never triggers a solve itself (that only
    happens from try_solve() below, on an actual 403)."""
    domain = _domain_of(url)
    with _cache_lock:
        entry = _cache.get(domain)
        if entry and time.time() < entry["expires"]:
            return {"cookiefile": entry["cookiefile"], "http_headers": {"User-Agent": entry["user_agent"]}}
    return None


_solve_locks: dict[str, threading.Lock] = {}


def try_solve(url: str) -> bool:
    """One solve at a time per domain (the monitor, scraper and downloads can all hit a 403 at once;
    FlareSolverr then times out on the pile-up). Whoever waits re-checks the cache once it gets the lock."""
    domain = _domain_of(url)
    with _cache_lock:
        lock = _solve_locks.setdefault(domain, threading.Lock())
    with lock:
        if get_bypass_opts(url):  # another thread just solved it
            return True
        if _try_solve_locked(url):
            return True
        # SpankBang: the direct (VPS-IP) solve failed -> switch the free-proxy pool on and retry ONCE, but only if a
        # working free proxy actually exists (otherwise a second identical direct attempt would just waste ~60-90 s).
        try:
            import free_proxy
            if (free_proxy.site_for(domain) == "spankbang" and free_proxy.switch_on_block(url, None)
                    and free_proxy.get_proxy("spankbang")):
                logger.info("[cf-bypass] direct solve for spankbang failed -- retrying through a free proxy.")
                return _try_solve_locked(url)
        except Exception as e:
            logger.debug(f"[cf-bypass] free-proxy fallback skipped: {e}")
        return False


def _try_solve_locked(url: str) -> bool:
    """Call FlareSolverr for this URL, cache the result for its domain on
    success. Returns True if a bypass is now cached and ready (caller
    should rebuild its yt-dlp opts via _base_opts() to pick it up),
    False on any failure — unreachable FlareSolverr, a genuine non-
    Cloudflare block it can't help with, timeout, etc. Never raises."""
    global _unreachable_warned
    domain = _domain_of(url)
    # FlareSolverr may still be installing/starting (flaresolver_bootstrap) -- wait for it instead of failing
    try:
        import flaresolver_bootstrap as _fb
        if _fb.is_starting():
            logger.info("[cf-bypass] FlareSolverr is still installing/starting -- waiting for it (up to 3 min)...")
            _fb.wait_ready(180)
    except Exception:
        pass
    try:
        payload = {"cmd": "request.get", "url": url, "maxTimeout": _SOLVE_TIMEOUT * 1000}
        px = proxy_for(url)
        if px:
            pu = urlparse(px)
            proxy = {"url": f"{pu.scheme}://{pu.hostname}" + (f":{pu.port}" if pu.port else "")}
            if pu.username:
                proxy["username"], proxy["password"] = pu.username, pu.password or ""
            payload["proxy"] = proxy
        resp = requests.post(
            FLARESOLVERR_URL,
            json=payload,
            timeout=_SOLVE_TIMEOUT + 20,
        )
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400:
            # FlareSolverr answers failures with HTTP 500 + a JSON "message" saying WHY -- log that, not just "500"
            msg = str(data.get("message") or resp.text[:300])
            hint = ""
            low = msg.lower()
            if "banned" in low or "blocked this request" in low:
                hint = "  -> this server's IP is banned by the site: set SPANKBANG_PROXY to a residential proxy."
            elif "timeout" in low:
                hint = "  -> challenge not solved in time (slow server or IP flagged)."
            logger.warning(f"[cf-bypass] FlareSolverr error for {domain} (HTTP {resp.status_code}): {msg}{hint}")
            return False
    except requests.exceptions.ConnectionError:
        if not _unreachable_warned:
            logger.warning(
                f"[cf-bypass] FlareSolverr isn't reachable at {FLARESOLVERR_URL} — "
                "Cloudflare-JS-challenge links (403 with no impersonation fix) will "
                "keep failing until it's running. See cf_bypass.py's docstring for setup."
            )
            _unreachable_warned = True
        return False
    except Exception as e:
        logger.warning(f"[cf-bypass] FlareSolverr request failed for {domain}: {e}")
        return False

    if data.get("status") != "ok":
        logger.warning(f"[cf-bypass] FlareSolverr couldn't solve {domain}: {data.get('message')}")
        return False

    solution = data.get("solution") or {}
    cookies = solution.get("cookies") or []
    user_agent = solution.get("userAgent")
    if not cookies or not user_agent:
        logger.warning(f"[cf-bypass] FlareSolverr returned no usable cookies/UA for {domain}.")
        return False

    try:
        cookiefile = _write_netscape_cookiefile(cookies, domain)
    except Exception as e:
        logger.warning(f"[cf-bypass] couldn't write cookiejar for {domain}: {e}")
        return False

    with _cache_lock:
        _cache[domain] = {"cookiefile": cookiefile, "user_agent": user_agent, "expires": time.time() + _CACHE_TTL}
    logger.info(f"[cf-bypass] ✅ solved Cloudflare challenge for {domain} — cached for {_CACHE_TTL // 60} min.")
    return True


def _write_netscape_cookiefile(cookies: list, domain: str) -> str:
    """yt-dlp's cookiefile option expects the Netscape/Mozilla cookies.txt
    format — build one from FlareSolverr's JSON cookie list. One file per
    domain, reused (overwritten) across solves rather than accumulating
    temp files forever."""
    path = os.path.join(tempfile.gettempdir(), f"cf_bypass_{domain.replace('.', '_')}.txt")
    jar = http.cookiejar.MozillaCookieJar(path)
    for c in cookies:
        try:
            jar.set_cookie(http.cookiejar.Cookie(
                version=0, name=c["name"], value=c["value"],
                port=None, port_specified=False,
                domain=c.get("domain", f".{domain}"),
                domain_specified=True, domain_initial_dot=c.get("domain", "").startswith("."),
                path=c.get("path", "/"), path_specified=True,
                secure=c.get("secure", False),
                expires=int(c["expiry"]) if c.get("expiry") else int(time.time()) + _CACHE_TTL,
                discard=False, comment=None, comment_url=None, rest={},
            ))
        except Exception:
            continue  # one malformed cookie shouldn't sink the whole batch
    jar.save(ignore_discard=True, ignore_expires=True)
    return path


# ═══════════════════════════════════════════════════════════════════════
#  Page-level solving — used by jav_sites.py (JableTV / MissAV / SupJav / Jav.guru)
# ═══════════════════════════════════════════════════════════════════════
# yt-dlp-style callers above only need the clearance cookie. jav_sites fetches the page itself with curl_cffi, so it
# wants (a) the page HTML straight from the browser that passed the challenge (Cloudflare often rejects a replayed
# cf_clearance from a different TLS fingerprint) and (b) the session (cookies + User-Agent) to reuse on later requests.
#
#   solve_via_service(url) -> SolvedResponse | None   the bundled cf-bypass service (cf-bypass/, mode "iuam")
#   solve_page(url)        -> SolvedResponse | None   FlareSolverr (request.get)
#   session_data(url)      -> {"user_agent": str, "cookies": {name: value}} | None   cached clearance for the domain
# Both solve_* raise RuntimeError with the reason when they fail, so the caller can log WHY (service unreachable,
# Chrome crashed, IP banned...). They return None only when the route is switched off.

import json as _json

_sessions: dict[str, dict] = {}  # domain -> {"user_agent", "cookies": {name: value}, "expires"}
_page_locks: dict[str, threading.Lock] = {}
_CHALLENGE_MARKERS = ("just a moment", "attention required", "cf-browser-verification", "cf_chl_")


class SolvedResponse:
    """The bits of a requests/curl_cffi response that jav_sites reads."""

    def __init__(self, url, text, status_code=200, headers=None, cookies=None):
        self.url = url
        self.status_code = status_code
        self.text = text or ""
        self.content = self.text.encode("utf-8", "ignore")
        self.headers = dict(headers or {})
        self.cookies = dict(cookies or {})
        self.encoding = "utf-8"
        self.ok = status_code < 400

    def json(self):
        return _json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _looks_like_challenge(html: str) -> bool:
    head = (html or "")[:4000].lower()
    return (not head.strip()) or any(m in head for m in _CHALLENGE_MARKERS)


def _remember_session(domain: str, user_agent, cookies) -> None:
    """cookies: list of {name, value, ...} (service / FlareSolverr format) or a {name: value} dict."""
    if isinstance(cookies, list):
        cookies = {c["name"]: c["value"] for c in cookies if isinstance(c, dict) and c.get("name")}
    if not user_agent or not cookies:
        return
    with _cache_lock:
        _sessions[domain] = {"user_agent": user_agent, "cookies": dict(cookies), "expires": time.time() + _CACHE_TTL}


def session_data(url: str):
    """Cached Cloudflare clearance for this URL's domain, or None. Never triggers a solve."""
    domain = _domain_of(url)
    with _cache_lock:
        entry = _sessions.get(domain)
        if entry and time.time() < entry["expires"]:
            return {"user_agent": entry["user_agent"], "cookies": dict(entry["cookies"])}
        if entry:
            _sessions.pop(domain, None)
    return None


def _page_lock(url: str) -> threading.Lock:
    domain = _domain_of(url)
    with _cache_lock:
        return _page_locks.setdefault(domain, threading.Lock())


def solve_via_service(url: str):
    """Load `url` in the bundled cf-bypass service's real Chrome (mode "iuam") and return the page it ends up on."""
    import turnstile_solver as ts
    if not ts.enabled():
        return None
    domain = _domain_of(url)
    payload = {"mode": "iuam", "domain": url}
    if ts.CF_BYPASS_AUTH:
        payload["authToken"] = ts.CF_BYPASS_AUTH
    with _page_lock(url):
        cached = session_data(url)  # another thread may have just solved this domain
        if cached:
            pass  # still go on: the caller wants the HTML, and the service caches iuam results itself
        try:
            r = ts._http.post(ts.CF_BYPASS_URL + "/cloudflare", json=payload, timeout=ts.CF_BYPASS_TIMEOUT + 20)
        except Exception as e:
            raise RuntimeError(f"cf-bypass service unreachable at {ts.CF_BYPASS_URL} ({str(e)[:120]})")
        try:
            j = r.json()
        except Exception:
            raise RuntimeError(f"cf-bypass answered HTTP {r.status_code} with non-JSON")
        if not isinstance(j, dict) or r.status_code >= 400 or j.get("code", 200) not in (200, None):
            raise RuntimeError(str((j or {}).get("message") if isinstance(j, dict) else r.text[:150])
                               or f"HTTP {r.status_code}")
        ua = j.get("user_agent")
        cookies = j.get("cookies") or ([{"name": "cf_clearance", "value": j["cf_clearance"]}]
                                       if j.get("cf_clearance") else [])
        _remember_session(domain, ua, cookies)
        html = j.get("html") or ""
        if _looks_like_challenge(html):
            return None  # solved, but this build returned no usable page -> caller replays the clearance
        logger.info(f"[cf-bypass] ✅ page-level solve for {domain} via service ({j.get('elapsed', '?')}).")
        return SolvedResponse(j.get("final_url") or url, html, 200, cookies=session_data(url)["cookies"]
                              if session_data(url) else {})


def solve_page(url: str):
    """FlareSolverr request.get: returns the page HTML its browser got after passing the challenge."""
    domain = _domain_of(url)
    with _page_lock(url):
        try:
            import flaresolver_bootstrap as _fb
            if _fb.is_starting():
                logger.info("[cf-bypass] FlareSolverr is still installing/starting -- waiting for it (up to 3 min)...")
                _fb.wait_ready(180)
        except Exception:
            pass
        payload = {"cmd": "request.get", "url": url, "maxTimeout": _SOLVE_TIMEOUT * 1000}
        px = proxy_for(url)
        if px:
            pu = urlparse(px)
            proxy = {"url": f"{pu.scheme}://{pu.hostname}" + (f":{pu.port}" if pu.port else "")}
            if pu.username:
                proxy["username"], proxy["password"] = pu.username, pu.password or ""
            payload["proxy"] = proxy
        try:
            r = requests.post(FLARESOLVERR_URL, json=payload, timeout=_SOLVE_TIMEOUT + 20)
        except requests.exceptions.ConnectionError:
            raise RuntimeError(f"FlareSolverr isn't reachable at {FLARESOLVERR_URL}")
        except Exception as e:
            raise RuntimeError(f"FlareSolverr request failed ({str(e)[:120]})")
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(f"FlareSolverr answered HTTP {r.status_code} with non-JSON")
        if data.get("status") != "ok":
            raise RuntimeError(str(data.get("message") or f"HTTP {r.status_code}")[:200])
        sol = data.get("solution") or {}
        _remember_session(domain, sol.get("userAgent"), sol.get("cookies") or [])
        html = sol.get("response") or ""
        if _looks_like_challenge(html):
            raise RuntimeError("FlareSolverr returned the challenge page, not the video page")
        logger.info(f"[cf-bypass] ✅ page-level solve for {domain} via FlareSolverr.")
        sd = session_data(url)
        return SolvedResponse(sol.get("url") or url, html, int(sol.get("status") or 200),
                              headers=sol.get("headers") or {}, cookies=(sd or {}).get("cookies"))

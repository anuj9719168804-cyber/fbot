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

_cache: dict[str, dict] = {}  # domain -> {"cookiefile": path, "user_agent": str, "expires": ts}
_cache_lock = threading.Lock()
_unreachable_warned = False  # log the "not running" warning once, not on every call


def _domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
    except Exception:
        return url


def proxy_for(url: str) -> str | None:
    """Proxy URL to use for this URL's site, or None (only SpankBang has one: SPANKBANG_PROXY)."""
    if SPANKBANG_PROXY and "spankbang" in _domain_of(url):
        return SPANKBANG_PROXY
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
        return _try_solve_locked(url)


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


# ---------------------------------------------------------------------------
# Page-level solving (used by jav_sites.py: SupJav / Jable / MissAV / Jav.guru)
#
# try_solve() above only caches a cf_clearance cookie for yt-dlp. jav_sites.py
# fetches pages itself (curl_cffi), so it needs two more things:
#   solve_page(url)      -> FlareSolverr's own rendered HTML as a response-like object. Using the HTML
#                           FlareSolverr already got avoids replaying the cookie from a different TLS
#                           fingerprint (the usual reason "solved" cookies still get 403 afterwards).
#   solve_clearance(url) -> second route: the bundled cf-bypass service (cf-bypass/, mode "iuam") returns a
#                           cf_clearance + the exact User-Agent it used; the caller replays both.
#   session_data(url)    -> cookies + UA from the newest solve of this domain (so later requests to the same
#                           site skip the challenge entirely until the cookie expires).
# ---------------------------------------------------------------------------
import json as _json

CF_BYPASS_URL = os.environ.get("CF_BYPASS_URL", "http://127.0.0.1:8742").strip().rstrip("/")
CF_BYPASS_AUTH = os.environ.get("CF_BYPASS_AUTH", "").strip()
_CHALLENGE_MARKERS = ("just a moment", "cf-browser-verification", "cf_chl_", "challenge-platform",
                      "attention required", "enable javascript and cookies")


class SolvedResponse:
    """Duck-types the bits of a requests/curl_cffi response that jav_sites.py reads."""

    def __init__(self, status_code, text, url, headers=None, cookies=None):
        self.status_code = int(status_code or 200)
        self.text = text or ""
        self.content = self.text.encode("utf-8")
        self.url = url
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.ok = self.status_code < 400
        self.solved_by = "flaresolverr"

    def json(self):
        return _json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code} for {self.url}")


def _still_challenge(html: str) -> bool:
    head = (html or "")[:4000].lower()
    return any(m in head for m in _CHALLENGE_MARKERS)


def _flaresolverr_solution(url: str):
    """POST request.get to FlareSolverr; the `solution` dict, or None (never raises)."""
    global _unreachable_warned
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
    domain = _domain_of(url)
    try:
        resp = requests.post(FLARESOLVERR_URL, json=payload, timeout=_SOLVE_TIMEOUT + 20)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400 or data.get("status") != "ok":
            msg = str(data.get("message") or resp.text[:300])
            low = msg.lower()
            hint = ""
            if "banned" in low or "blocked this request" in low or "error 1020" in low or "error 1015" in low:
                hint = "  -> this server's IP is banned by the site (FlareSolverr can't fix an IP ban): use a residential proxy."
            logger.warning(f"[cf-bypass] FlareSolverr couldn't load {domain}: {msg}{hint}")
            return None
        return data.get("solution") or None
    except requests.exceptions.ConnectionError:
        if not _unreachable_warned:
            logger.warning(f"[cf-bypass] FlareSolverr isn't reachable at {FLARESOLVERR_URL}.")
            _unreachable_warned = True
        return None
    except Exception as e:
        logger.warning(f"[cf-bypass] FlareSolverr request failed for {domain}: {e}")
        return None


def _remember(domain: str, cookies: list, user_agent: str) -> None:
    """Store a solve (FlareSolverr-style cookie dicts) in the shared per-domain cache."""
    if not cookies or not user_agent:
        return
    try:
        path = _write_netscape_cookiefile(cookies, domain)
    except Exception as e:
        logger.warning(f"[cf-bypass] couldn't write cookiejar for {domain}: {e}")
        return
    with _cache_lock:
        _cache[domain] = {"cookiefile": path, "user_agent": user_agent,
                          "cookies": {c["name"]: c["value"] for c in cookies if c.get("name")},
                          "expires": time.time() + _CACHE_TTL}


def session_data(url: str):
    """{"cookies": {name: value}, "user_agent": str} from a still-fresh solve of this URL's domain, else None."""
    domain = _domain_of(url)
    with _cache_lock:
        entry = _cache.get(domain)
        if not entry or time.time() >= entry["expires"]:
            return None
        cookies = entry.get("cookies")
        ua = entry["user_agent"]
        cookiefile = entry["cookiefile"]
    if cookies is None:  # entry created by try_solve() (yt-dlp path): read the cookies back from its file
        try:
            jar = http.cookiejar.MozillaCookieJar(cookiefile)
            jar.load(ignore_discard=True, ignore_expires=True)
            cookies = {c.name: c.value for c in jar}
        except Exception:
            cookies = {}
    return {"cookies": cookies, "user_agent": ua}


def solve_page(url: str):
    """Load `url` in FlareSolverr's real browser and return the page as a SolvedResponse, or None.
    One solve at a time per domain; also caches the clearance cookie + UA for session_data()."""
    domain = _domain_of(url)
    with _cache_lock:
        lock = _solve_locks.setdefault(domain, threading.Lock())
    with lock:
        sol = _flaresolverr_solution(url)
        if not sol:
            return None
        html = sol.get("response") or ""
        status = sol.get("status") or 200
        if not html or _still_challenge(html):
            logger.warning(f"[cf-bypass] FlareSolverr returned a challenge/empty page for {domain} (status {status}).")
            return None
        _remember(domain, sol.get("cookies") or [], sol.get("userAgent") or "")
        logger.info(f"[cf-bypass] ✅ FlareSolverr loaded {domain} (status {status}, {len(html)} bytes)")
        return SolvedResponse(status, html, sol.get("url") or url, cookies={c["name"]: c["value"] for c in (sol.get("cookies") or []) if c.get("name")})


def solve_clearance(url: str) -> bool:
    """Second route: ask the bundled cf-bypass service (mode "iuam") for a cf_clearance + User-Agent for this
    site and cache them. True when a usable clearance is now cached (read it back with session_data())."""
    if os.environ.get("CF_BYPASS_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    p = urlparse(url)
    if not p.scheme or not p.netloc:
        return False
    domain = _domain_of(url)
    body = {"mode": "iuam", "domain": f"{p.scheme}://{p.netloc}/", "ttl": _CACHE_TTL * 1000}
    if CF_BYPASS_AUTH:
        body["authToken"] = CF_BYPASS_AUTH
    try:
        s = requests.Session()
        s.trust_env = False  # local service: never via HTTP(S)_PROXY from the environment
        r = s.post(CF_BYPASS_URL + "/cloudflare", json=body, timeout=int(os.environ.get("CF_BYPASS_TIMEOUT", "90")))
        data = r.json()
    except Exception as e:
        logger.info(f"[cf-bypass] cf-bypass service not usable for {domain}: {e}")
        return False
    clearance, ua = data.get("cf_clearance"), data.get("user_agent")
    if not clearance or not ua:
        logger.info(f"[cf-bypass] cf-bypass service gave no clearance for {domain}: {data.get('message') or data}")
        return False
    _remember(domain, [{"name": "cf_clearance", "value": clearance, "domain": "." + domain.lstrip("."), "path": "/", "secure": True}], ua)
    logger.info(f"[cf-bypass] ✅ cf-bypass service cleared {domain}")
    return True

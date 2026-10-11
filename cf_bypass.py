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
# FlareSolverr drives ONE real Chrome: several challenges at once (jable + supjav + fpo login...) make every one of
# them time out. At most 2 solves in flight overall; the rest wait their turn.
_fs_gate = threading.BoundedSemaphore(int(os.environ.get("FLARESOLVERR_PARALLEL", "2")))


def _fs_post(payload: dict):
    with _fs_gate:
        return requests.post(FLARESOLVERR_URL, json=payload, timeout=_SOLVE_TIMEOUT + 20)


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
        resp = _fs_post(payload)
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
        resp = _fs_post(payload)
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


def solve_via_service(url: str):
    """Bundled cf-bypass service (mode "iuam"): its Chrome opens the FULL page URL, passes the challenge and
    returns the rendered HTML plus cf_clearance + User-Agent. Returns a SolvedResponse (HTML from that same
    browser -- nothing to replay with a different TLS fingerprint) or None. Faster than FlareSolverr (seconds)."""
    if os.environ.get("CF_BYPASS_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
        return None
    p = urlparse(url)
    if not p.scheme or not p.netloc:
        return None
    domain = _domain_of(url)
    body = {"mode": "iuam", "domain": url, "ttl": _CACHE_TTL * 1000}  # full URL: the service loads exactly this page
    if CF_BYPASS_AUTH:
        body["authToken"] = CF_BYPASS_AUTH
    with _cache_lock:
        lock = _solve_locks.setdefault("svc:" + domain, threading.Lock())
    with lock:
        try:
            s = requests.Session()
            s.trust_env = False  # local service: never via HTTP(S)_PROXY from the environment
            r = s.post(CF_BYPASS_URL + "/cloudflare", json=body, timeout=int(os.environ.get("CF_BYPASS_TIMEOUT", "90")))
            data = r.json()
        except Exception as e:
            logger.info(f"[cf-bypass] cf-bypass service not usable for {domain}: {e}")
            return None
        clearance, ua = data.get("cf_clearance"), data.get("user_agent")
        if not clearance or not ua:
            logger.info(f"[cf-bypass] cf-bypass service gave no clearance for {domain}: {data.get('message') or data}")
            return None
        cookies = data.get("cookies") or [{"name": "cf_clearance", "value": clearance,
                                           "domain": "." + domain.lstrip("."), "path": "/", "secure": True}]
        _remember(domain, cookies, ua)
        logger.info(f"[cf-bypass] ✅ cf-bypass service cleared {domain}")
        html = data.get("html") or ""
        if html and not _still_challenge(html):
            res = SolvedResponse(200, html, data.get("final_url") or url,
                                 cookies={c["name"]: c["value"] for c in cookies if c.get("name")})
            res.solved_by = "cf-bypass"
            return res
        return None  # clearance is cached; caller may replay it (older cf-bypass builds return no html)


def solve_clearance(url: str) -> bool:
    """True when the cf-bypass service produced a usable clearance (read it back with session_data())."""
    res = solve_via_service(url)
    return res is not None or session_data(url) is not None


# ---------------------------------------------------------------------------
# AUTO MODE -- Cloudflare sites are detected and cleared BEFORE the first request
#
# Until now the bypass only ran AFTER a request had already failed (403 / challenge page). ensure(url) flips that:
# for any link whose site sits behind a Cloudflare challenge, a fresh cf_clearance is obtained up front (cached per
# domain for _CACHE_TTL), so the normal path -- yt-dlp via _base_opts(), jav_sites via session_data() -- already
# carries the clearance cookie + matching User-Agent on its very first request.
#
#   is_cf_protected(url)  -> True if the domain is on the built-in list / CF_SITES, or a one-off probe sees a
#                            Cloudflare CHALLENGE (not merely Cloudflare's CDN, which half the web uses). The probe
#                            result is cached per domain.
#   ensure(url)           -> True when a fresh clearance exists for the domain after the call. Never raises.
#
# Env:  CF_AUTO=0            turn the whole auto mode off (the old on-403 behaviour stays)
#       CF_SITES=a.com,b.tv  extra domains to always treat as Cloudflare-protected (substring match)
#       CF_NOT_SITES=x.com   domains to never probe/solve
# ---------------------------------------------------------------------------
_KNOWN_CF_HOSTS = ("spankbang", "jable.tv", "fs1.app", "supjav", "missav", "jav.guru", "fpo.xxx", "faphouse")
_NEVER_CF = ("youtube.com", "youtu.be", "googlevideo.com", "facebook.com", "fb.watch", "instagram.com",
             "twitter.com", "x.com", "tiktok.com", "t.me", "telegram.org", "github.com")
_PROBE_TTL_YES = 6 * 3600   # a Cloudflare-challenged site stays one for a good while
_PROBE_TTL_NO = 30 * 60     # "not challenged right now" is re-checked sooner (the 403 fallback still exists)
_ENSURE_COOLDOWN = 120      # after a failed solve don't retry the same domain for this long (IP ban etc.)
_probe_cache: dict[str, tuple[bool, float]] = {}  # domain -> (is_cf, expires)
_ensure_fail: dict[str, float] = {}


def _auto_enabled() -> bool:
    return os.environ.get("CF_AUTO", "1").strip().lower() not in ("0", "false", "no", "off")


def _env_hosts(name: str) -> tuple:
    return tuple(h.strip().lower() for h in os.environ.get(name, "").split(",") if h.strip())


def _resp_is_cf_challenge(status: int, headers, body: bytes) -> bool:
    """A Cloudflare *challenge/block* page (what solving can fix) -- not merely a site served through Cloudflare."""
    try:
        hdrs = {str(k).lower(): str(v).lower() for k, v in (headers or {}).items()}
        if "challenge" in hdrs.get("cf-mitigated", ""):
            return True
        head = (body or b"")[:6000].lower()
        markers = any(m.encode() in head for m in _CHALLENGE_MARKERS)  # bodies only; real pages rarely contain these
        via_cf = "cloudflare" in hdrs.get("server", "") or "cf-ray" in hdrs
        if status in (403, 429, 503):
            return via_cf or markers
        # 200 OK with an interstitial instead of the real page
        return b"just a moment" in head or b"cf-browser-verification" in head or b"cf_chl_" in head
    except Exception:
        return False


def _probe_domain(url: str) -> bool:
    """One lightweight GET (browser TLS fingerprint when curl_cffi is there) to see whether the site answers with a
    Cloudflare challenge. Network errors count as 'not CF' (and are cached only briefly)."""
    px = proxy_for(url)
    proxies = {"http": px, "https": px} if px else None
    try:
        try:
            from curl_cffi import requests as _cffi
            r = _cffi.get(url, impersonate="chrome", timeout=12, allow_redirects=True, proxies=proxies)
        except ImportError:
            r = requests.get(url, timeout=12, allow_redirects=True, proxies=proxies,
                             headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                                                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"})
        return _resp_is_cf_challenge(r.status_code, r.headers, r.content or b"")
    except Exception as e:
        logger.info(f"[cf-bypass] probe of {_domain_of(url)} failed ({str(e)[:100]}) -- treating as not Cloudflare for now.")
        return False


def is_cf_protected(url: str) -> bool:
    """True if this URL's site is (or just proved to be) behind a Cloudflare challenge. Cached per domain."""
    if not _auto_enabled():
        return False
    domain = _domain_of(url)
    if not domain or not url.lower().startswith(("http://", "https://")):
        return False
    if any(n in domain for n in _NEVER_CF + _env_hosts("CF_NOT_SITES")):
        return False
    if any(k in domain for k in _KNOWN_CF_HOSTS + _env_hosts("CF_SITES")):
        return True
    with _cache_lock:
        hit = _probe_cache.get(domain)
        lock = _solve_locks.setdefault("probe:" + domain, threading.Lock())
    if hit and time.time() < hit[1]:
        return hit[0]
    with lock:  # several jobs on the same new site -> only one probe
        with _cache_lock:
            hit = _probe_cache.get(domain)
        if hit and time.time() < hit[1]:
            return hit[0]
        is_cf = _probe_domain(url)
        with _cache_lock:
            _probe_cache[domain] = (is_cf, time.time() + (_PROBE_TTL_YES if is_cf else _PROBE_TTL_NO))
        if is_cf:
            logger.info(f"[cf-bypass] {domain} is behind a Cloudflare challenge -- auto-bypass will be used for it.")
        return is_cf


def ensure(url: str) -> bool:
    """Make sure a fresh Cloudflare clearance is cached for this URL's domain (no-op for sites without a challenge).
    Order: bundled cf-bypass service (seconds) -> FlareSolverr (slower, independent browser). Blocking, but only
    once per domain per _CACHE_TTL; failures are put on a short cooldown. Returns True when a clearance is ready."""
    try:
        if get_bypass_opts(url):
            return True
        if not is_cf_protected(url):
            return False
        domain = _domain_of(url)
        if time.time() - _ensure_fail.get(domain, 0) < _ENSURE_COOLDOWN:
            return False
        with _cache_lock:
            lock = _solve_locks.setdefault("ensure:" + domain, threading.Lock())
        with lock:
            if get_bypass_opts(url):  # another thread solved it while we waited
                return True
            logger.info(f"[cf-bypass] auto-solving Cloudflare for {domain} before the first request...")
            ok = False
            try:
                ok = solve_clearance(url)
            except Exception as e:
                logger.info(f"[cf-bypass] service route failed for {domain}: {e}")
            if not ok:
                ok = try_solve(url)
            if not ok:
                _ensure_fail[domain] = time.time()
                logger.warning(f"[cf-bypass] couldn't clear Cloudflare on {domain} (will retry in {_ENSURE_COOLDOWN}s) "
                               "-- if this repeats, the server IP is banned: set a residential proxy.")
            return ok
    except Exception as e:
        logger.warning(f"[cf-bypass] ensure({url[:80]}) failed: {e}")
        return False


# ---------------------------------------------------------------------------
# UNIVERSAL LAYER -- any Cloudflare site, whatever protects it (challenge page, Turnstile token, sitekey form...)
#
# This is what fpo_downloader does for fpo.xxx's login (find the Turnstile sitekey -> token from the cf-bypass service ->
# POST it in cf-turnstile-response + g-recaptcha-response), made generic so every site gets the same treatment:
#
#   find_sitekey(html)                 -> the Turnstile sitekey of a page (data-sitekey / data-recaptcha-key / JS render())
#   get_turnstile_fields(url, key)     -> {"cf-turnstile-response": token, "g-recaptcha-response": token, ...}
#   cf_request(method, url, data=...)  -> a request that clears Cloudflare first, solves a challenge page if one comes
#                                         back, and for POSTs finds + attaches the Turnstile token by itself (also retries
#                                         once with a token when the answer says "captcha/turnstile required")
#   cf_submit_form(page_url, fields)   -> login / any form: loads the page, keeps its hidden inputs, adds your fields and
#                                         the Turnstile token, posts to the form's action. Returns (response, session).
#
# Env: CF_TURNSTILE=0 never fetches Turnstile tokens (clearance/challenge solving stays on).
# ---------------------------------------------------------------------------
import re as _re
from urllib.parse import urljoin

_UA_DEFAULT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/124.0.0.0 Safari/537.36")
_TOKEN_FIELDS = ("cf-turnstile-response", "g-recaptcha-response")
_SITEKEY_RES = (
    _re.compile(r"""data-(?:sitekey|site-key|recaptcha-key|turnstile-sitekey)\s*=\s*["'](0x[A-Za-z0-9_-]{10,})["']""", _re.I),
    _re.compile(r"""["']?site[-_]?key["']?\s*[:=]\s*["'](0x[A-Za-z0-9_-]{10,})["']""", _re.I),
    _re.compile(r"""turnstile\.render\([^)]*?["'](0x[A-Za-z0-9_-]{10,})["']""", _re.I | _re.S),
)
_NEEDS_TOKEN_WORDS = ("turnstile", "captcha", "cf-turnstile", "verify you are human", "verification failed",
                      "challenge failed", "missing-input-response", "timeout-or-duplicate", "invalid-input-response")


def find_sitekey(html: str) -> str | None:
    """Turnstile sitekey (0x4...) from a page's HTML, or None when the page has no Turnstile widget."""
    for rx in _SITEKEY_RES:
        m = rx.search(html or "")
        if m:
            return m.group(1)
    return None


def _token_field_names(html: str) -> list:
    """The standard pair plus any custom field the page's own widget/form declares for the token."""
    names = list(_TOKEN_FIELDS)
    for m in _re.finditer(r"""data-response-field-name\s*=\s*["']([^"']+)["']""", html or "", _re.I):
        if m.group(1) not in names:
            names.append(m.group(1))
    for m in _re.finditer(r"""name\s*=\s*["']([^"']*(?:turnstile|recaptcha)[^"']*response[^"']*)["']""", html or "", _re.I):
        if m.group(1) not in names:
            names.append(m.group(1))
    return names


def get_turnstile_fields(page_url: str, sitekey: str, html: str = "") -> dict:
    """Ask the bundled cf-bypass service for a Turnstile token valid for page_url's origin and return it under every
    field name the page may expect ({} when disabled / the service is unreachable). Never raises."""
    if os.environ.get("CF_TURNSTILE", "1").strip().lower() in ("0", "false", "no", "off"):
        return {}
    try:
        import turnstile_solver as ts
        if not ts.enabled():
            return {}
        try:  # first boot: the service may still be installing/starting
            import cfbypass_bootstrap as cb
            if cb.is_starting():
                logger.info("[cf-bypass] cf-bypass service is still being set up - waiting for it (up to 4 min)...")
                cb.wait_ready(240)
        except Exception:
            pass
        if not ts.wait_until_up(30):
            logger.info(f"[cf-bypass] Turnstile service not reachable at {ts.CF_BYPASS_URL}")
            return {}
        p = urlparse(page_url)
        origin = f"{p.scheme or 'https'}://{p.netloc}"
        for attempt in (1, 2):
            token, msg = ts.get_token(origin, sitekey)
            if token:
                logger.info(f"[cf-bypass] Turnstile token for {p.netloc} ready (attempt {attempt}).")
                return {name: token for name in _token_field_names(html)}
            logger.info(f"[cf-bypass] Turnstile attempt {attempt} for {p.netloc}: {msg}")
    except Exception as e:
        logger.info(f"[cf-bypass] Turnstile token failed: {e}")
    return {}


def _new_session(url: str):
    """Browser-fingerprint session (curl_cffi when installed) so cookies the site sets survive between GET and POST."""
    px = proxy_for(url)
    proxies = {"http": px, "https": px} if px else None
    try:
        from curl_cffi import requests as _cffi
        return _cffi.Session(impersonate="chrome", proxies=proxies)
    except Exception:
        s = requests.Session()
        if proxies:
            s.proxies.update(proxies)
        return s


def _needs_token(resp) -> bool:
    try:
        if resp.status_code not in (400, 401, 403, 422, 429):
            return False
        low = (resp.text or "")[:6000].lower()
        return any(w in low for w in _NEEDS_TOKEN_WORDS)
    except Exception:
        return False


def _looks_challenged(resp) -> bool:
    try:
        return _resp_is_cf_challenge(int(resp.status_code), resp.headers, (resp.text or "")[:6000].encode("utf-8", "ignore"))
    except Exception:
        return False


def cf_request(method: str, url: str, data=None, json=None, headers=None, session=None, page_url: str | None = None,
               turnstile="auto", timeout: int = 30):
    """requests-style call that copes with Cloudflare however it protects the site:
       1. clearance first (ensure) -> cf_clearance cookie + matching User-Agent are sent;
       2. a challenge page in the answer -> solved through cf-bypass service / FlareSolverr, request repeated
          (a GET gets the solved browser's HTML straight back);
       3. POST with form data: if the page (page_url, default = url) has a Turnstile widget, a token is fetched and put
          into the form fields up front (turnstile="auto"/True); False = never. If the site still answers
          'captcha/turnstile required', one more try with a fresh token.
       Returns the response object. Network errors propagate like with requests."""
    method = method.upper()
    s = session or _new_session(url)
    page_url = page_url or url
    ensure(url)

    def _send(body):
        sd = session_data(url)
        h = dict(headers or {})
        kw = {}
        if sd:
            h["User-Agent"] = sd["user_agent"]
            kw["cookies"] = sd["cookies"]
        else:
            h.setdefault("User-Agent", _UA_DEFAULT)
        if body is not None:
            kw["data"] = body
        if json is not None:
            kw["json"] = json
        return s.request(method, url, headers=h, timeout=timeout, allow_redirects=True, **kw)

    body = dict(data) if isinstance(data, dict) else data
    attached = False
    if method == "POST" and isinstance(body, dict) and turnstile is not False and not any(f in body for f in _TOKEN_FIELDS):
        try:
            page = cf_request("GET", page_url, session=s, turnstile=False, headers=headers, timeout=timeout)
            key = find_sitekey(page.text or "")
            if key:
                fields = get_turnstile_fields(page_url, key, page.text or "")
                if fields:
                    body.update(fields)
                    attached = True
        except Exception as e:
            logger.info(f"[cf-bypass] pre-fetching the Turnstile widget failed: {e}")

    resp = _send(body)
    if _looks_challenged(resp):
        solved = None
        try:
            solved = solve_via_service(url) or solve_page(url)
        except Exception as e:
            logger.info(f"[cf-bypass] challenge solve failed for {_domain_of(url)}: {e}")
        if solved is not None and method == "GET":
            return solved
        if solved is not None or session_data(url):
            resp = _send(body)  # clearance is cached now: repeat with the cookie + UA

    if (turnstile is not False and method == "POST" and isinstance(body, dict) and not attached and _needs_token(resp)):
        try:
            page = cf_request("GET", page_url, session=s, turnstile=False, headers=headers, timeout=timeout)
            key = find_sitekey(page.text or "")
            fields = get_turnstile_fields(page_url, key, page.text or "") if key else {}
            if fields:
                body.update(fields)
                resp = _send(body)
        except Exception as e:
            logger.info(f"[cf-bypass] Turnstile retry failed: {e}")
    return resp


def cf_submit_form(page_url: str, fields: dict, form_hint: str = "", session=None, headers=None, timeout: int = 30):
    """Login / any HTML form on a Cloudflare site, FPO-style but for every site. Loads page_url (clearing Cloudflare),
    picks the form (the one with a password input, or whose id/action contains `form_hint`), keeps its hidden inputs
    and defaults, overlays `fields` (e.g. {"username": u, "pass": p}), adds the Turnstile token when the page has the
    widget, and posts to the form's action. Returns (response, session) - the session now holds the login cookies."""
    from bs4 import BeautifulSoup
    s = session or _new_session(page_url)
    page = cf_request("GET", page_url, session=s, turnstile=False, headers=headers, timeout=timeout)
    html = page.text or ""
    soup = BeautifulSoup(html, "html.parser")
    forms = soup.find_all("form")
    form = None
    if form_hint:
        form = next((f for f in forms if form_hint.lower() in f"{f.get('id', '')} {f.get('action', '')} {f.get('class', '')}".lower()), None)
    form = form or next((f for f in forms if f.find("input", {"type": "password"})), None) or (forms[0] if forms else None)
    data: dict = {}
    action = page_url
    method = "POST"
    if form is not None:
        action = urljoin(page.url if isinstance(getattr(page, "url", None), str) else page_url, form.get("action") or page_url)
        method = (form.get("method") or "POST").upper()
        for inp in form.find_all(["input", "textarea", "select"]):
            name = inp.get("name")
            if not name or inp.get("type") in ("submit", "button", "image", "file"):
                continue
            if inp.get("type") in ("checkbox", "radio") and not inp.has_attr("checked"):
                continue
            data[name] = inp.get("value", "")
    data.update(fields or {})
    key = find_sitekey(html)
    if key and not any(data.get(f) for f in _TOKEN_FIELDS):
        data.update(get_turnstile_fields(page_url, key, html))
    h = dict(headers or {})
    h.setdefault("Referer", page_url)
    p = urlparse(page_url)
    h.setdefault("Origin", f"{p.scheme}://{p.netloc}")
    if method == "GET":
        return cf_request("GET", action, session=s, turnstile=False, headers=h, timeout=timeout), s
    # token already attached above; "auto" keeps the one-retry-with-fresh-token safety net when the site still refuses it
    return cf_request("POST", action, data=data, session=s, page_url=page_url,
                      turnstile="auto" if key else False, headers=h, timeout=timeout), s

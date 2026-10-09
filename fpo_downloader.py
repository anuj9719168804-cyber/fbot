"""
FPO.XXX video downloader engine.

fpo.xxx runs on "KVS" (Kernel Video Sharing), a tube-site CMS completely
different from Faphouse's HLS/m3u8 setup: each video page embeds a player
(/embed/<id>) whose JS config ("flashvars") carries one progressive MP4
URL per available resolution. Those URLs are themselves scrambled — a
video_url that starts with "function/0/" has a 32-character block, deep
in the path, whose characters have been permuted; the correct order
depends on a "license_code" also present in flashvars. This is a known,
publicly-documented technique used by this CMS across many tube sites
(not something specific to fpo.xxx) — the descramble is a fixed,
mechanical character-swap, not a secret only fpo.xxx knows.

IMPORTANT — this module was written without being able to inspect
fpo.xxx's actual embed-page JavaScript (the tool used to research this
strips <script> tags), so the two things most likely to need adjusting
after a real test against the live site are:
  1. _FLASHVARS_RE / _extract_flashvars() — the exact way the flashvars
     object is written into the page may differ slightly (quoting style,
     variable name) from what's assumed here.
  2. Whether fpo.xxx's player version still uses this exact descramble
     scheme at all (KVS has had a few scheme revisions over the years).

USERNAME / PASSWORD LOGIN (FapHouse-style, added later): set FPO_USERNAME and
FPO_PASSWORD (env vars, or the admin command /setfpologin <user> <pass>) and
this module logs in on its own — same idea as faphouse_downloader.AkClient:
logs in on first use, re-logs in when the session is old (FPO_LOGIN_MAX_AGE)
or when a video looks members-only, keeps the resulting cookies on disk so a
restart doesn't need a new login, and backs off for a while after a failed
attempt. The cookies it gets are merged with FPO_COOKIES (fresh login wins;
extra cookies such as cf_clearance from FPO_COOKIES are kept).
If the site's Turnstile check rejects the scripted login, nothing breaks: it
logs why, falls back to FPO_COOKIES exactly as before, and /fpologin tells
the admin what happened. This module does NOT try to solve Turnstile.

LOGIN / PRIVATE VIDEOS (added for member-uploaded "private" videos — most
content on the site needs no login at all, but individual users can mark
their own uploads private, visible only to logged-in members):

fpo.xxx's login form is protected by Cloudflare Turnstile (a JS
challenge) — confirmed via a real screenshot of the login modal. A
plain requests.Session().post() can never solve that challenge, so
POSTing credentials directly (this module's old approach) will never
work no matter how correct the field names/endpoint are.

Instead, this uses SESSION-COOKIE REUSE: log into fpo.xxx once, normally,
in a real browser (which solves Turnstile the normal way a human does),
then export that session's cookies and set them as FPO_COOKIES below.
This module just attaches those cookies to its requests — it never
touches the login form or Turnstile itself. This is the same technique
yt-dlp's own --cookies flag is built around, not a bypass of anything.

FPO_COOKIES accepts either:
  - A raw "name=value; name2=value2" Cookie-header string (easiest to
    copy from a browser's DevTools -> Network tab -> any request to
    fpo.xxx -> Request Headers -> Cookie), or
  - The contents of a Netscape-format cookies.txt export (e.g. from a
    "Get cookies.txt" browser extension) — auto-detected by the
    "# Netscape HTTP Cookie File" header line.

Whichever kt_-prefixed session cookie(s) it grants (kt_member,
kt_acctoken, etc., alongside PHPSESSID) are what mark the session as
logged in server-side. Like any browser session, these expire — if
private videos start failing again after working for a while, the fix
is exporting a fresh cookie string the same way, not touching this code.

Kept free of any Telegram/Mongo/Pyrogram imports, same as
faphouse_downloader.py, so it can be dropped in as an alternative
backend selected purely by which domain a link belongs to.
"""

import glob
import json
import logging
import os
import random as _random
import re
import shutil
import subprocess
import tempfile
import threading
import time
import html as _html
from urllib.parse import urlparse, urljoin, quote_plus

try:
    from curl_cffi.requests import Session as CurlSession
    _CURL_CFFI_OK = True
except ImportError:
    CurlSession = None
    _CURL_CFFI_OK = False

try:
    import yt_dlp as _yt_dlp
    from yt_dlp.networking.impersonate import ImpersonateTarget as _ImpersonateTarget
    _YTDLP_OK = True
except ImportError:
    _yt_dlp = None
    _ImpersonateTarget = None
    _YTDLP_OK = False

import requests
from dotenv import load_dotenv

# Loaded here directly (not just relying on config.py's load_dotenv())
# so FPO_COOKIES is wired into this module on its own, regardless of
# what else has or hasn't imported config.py yet.
load_dotenv()

logger = logging.getLogger(__name__)

BASE_URLS = {
    "fpo.xxx": "https://www.fpo.xxx",
    "www.fpo.xxx": "https://www.fpo.xxx",
}
DEFAULT_BASE_URL = "https://www.fpo.xxx"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# Deliberately separate from faphouse_downloader.py's cookie/session
# setup — different site, different account.
#
# See the module docstring's LOGIN section — this is a browser session's
# cookies (either a raw "name=value; ..." header string, or a pasted
# Netscape cookies.txt export), NOT a username/password. Login POSTs are
# gone entirely since Turnstile makes them unworkable — see docstring.
FPO_COOKIES = os.environ.get("FPO_COOKIES", "")
# Mirrors faphouse_downloader.py's SESSION_MAX_AGE — periodically rebuild
# the Session object (re-applying FPO_COOKIES fresh) even though the
# cookie values themselves don't change here; this just bounds how long
# a single requests.Session's internal state is reused.
_SESSION_MAX_AGE = int(os.environ.get("FPO_SESSION_MAX_AGE", str(30 * 60)))
_session_state = {"session": None, "started_at": 0.0}

# ---------------------------------------------------------------------
# Username / password login (cookies are produced by it, then used like
# FPO_COOKIES). Credentials: env vars below, or set_credentials() at runtime.
# ---------------------------------------------------------------------
# Hardcoded default account (same idea as EMAIL / PASSWORD in faphouse_downloader.py). A real FPO_USERNAME /
# FPO_PASSWORD env var overrides it - an EMPTY env value (e.g. a blank Render "sync: false" field) is ignored.
_DEFAULT_FPO_USERNAME = "Anuj85971"
_DEFAULT_FPO_PASSWORD = "b7dee63a"
FPO_USERNAME = os.environ.get("FPO_USERNAME", "").strip() or _DEFAULT_FPO_USERNAME
FPO_PASSWORD = os.environ.get("FPO_PASSWORD", "").strip() or _DEFAULT_FPO_PASSWORD
# fpo.xxx's login form sits behind Cloudflare Turnstile, which a script cannot pass. When that is the reason a login
# failed, retrying every few minutes only produces more rejected attempts - so wait much longer in that case.
_TURNSTILE_COOLDOWN = int(os.environ.get("FPO_TURNSTILE_COOLDOWN", str(6 * 3600)))
_LOGIN_MAX_AGE = int(os.environ.get("FPO_LOGIN_MAX_AGE", str(12 * 3600)))       # re-login after 12h
_LOGIN_COOLDOWN = int(os.environ.get("FPO_LOGIN_COOLDOWN", "600"))               # wait after a failed login
_RELOGIN_MIN_GAP = int(os.environ.get("FPO_RELOGIN_MIN_GAP", "120"))             # forced re-logins at most this often
_LOGIN_COOKIE_FILE = os.path.join(os.environ.get("DOWNLOAD_DIR", "downloads"), "fpo_login_cookies.json")
_login_state = {"cookies": {}, "ts": 0.0, "failed_at": 0.0, "last_try": 0.0, "ok": False, "msg": "not tried yet",
                "restored": False}
_login_lock = threading.RLock()

# Same debug-capture contract as faphouse_downloader.py — see that
# module's _DEBUG_DIR comment. Kept independent (own file) since a
# failure here is a different embed page than a faphouse.com failure.
_DEBUG_DIR = os.environ.get("DOWNLOAD_DIR", "downloads")
_DEBUG_HTML_PATH = os.path.join(_DEBUG_DIR, "debug_last_flashvars_fail.html")


def _save_debug_html(html: str):
    if not html:
        return
    try:
        os.makedirs(_DEBUG_DIR, exist_ok=True)
        with open(_DEBUG_HTML_PATH, "w", encoding="utf-8", errors="replace") as f:
            f.write(html)
    except Exception as e:
        logger.warning(f"Couldn't save debug HTML: {e}")


def _save_debug_html_path(html: str, path: str):
    """Like _save_debug_html but to an explicit path — used so embed page
    debug HTML doesn't overwrite the original page's debug file."""
    if not html:
        return
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8", errors="replace") as f:
            f.write(html)
    except Exception as e:
        logger.warning(f"Couldn't save debug HTML to {path}: {e}")


def fetch_debug_html(video_url: str) -> str | None:
    """Just fetches the embed page's raw HTML — no flashvars extraction.
    Used by main.py's /debughtml admin command to pull the actual current
    embed markup straight from the live site for manual inspection."""
    video_id = _video_id_from_url(video_url)
    if not video_id:
        return None
    base_url = get_base_url(video_url) or DEFAULT_BASE_URL
    embed_url = f"{base_url}/embed/{video_id}"
    try:
        return _fetch_html(embed_url)
    except Exception as e:
        logger.warning(f"fetch_debug_html failed for {embed_url}: {e}")
        return None


def _parse_cookie_string(raw: str) -> dict:
    """Parses FPO_COOKIES into a plain {name: value} dict. Accepts either
    a browser-style "name=value; name2=value2" header, or a pasted
    Netscape cookies.txt export (the format that starts with the
    "# Netscape HTTP Cookie File" comment line — one cookie per line,
    tab-separated, name/value as the last two fields)."""
    raw = (raw or "").strip()
    if not raw:
        return {}

    if "Netscape HTTP Cookie File" in raw or raw.count("\t") > raw.count(";"):
        cookies = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) < 7:
                continue
            name, value = fields[5], fields[6]
            if name:
                cookies[name] = value
        return cookies

    cookies = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        cookies[name.strip()] = value.strip()
    return cookies


def set_cookies(raw: str) -> int:
    """Updates FPO_COOKIES at runtime (used by the /setcookies admin
    command) instead of requiring an env var edit + redeploy every time a
    session expires. Also drops the cached Session so _ensure_session()
    rebuilds with the new cookies on its very next call, rather than
    waiting up to _SESSION_MAX_AGE for the periodic rebuild to notice.

    Returns how many individual cookies were parsed out of `raw` (0 if it
    didn't look like either supported format), so the caller can tell the
    admin whether it actually took."""
    global FPO_COOKIES
    FPO_COOKIES = (raw or "").strip()
    _session_state["session"] = None
    return len(_parse_cookie_string(FPO_COOKIES))


def get_cookie_count() -> int:
    """How many cookies are currently attached (0 if none set/expired)."""
    return len(_effective_cookies())


def has_credentials() -> bool:
    return bool(FPO_USERNAME and FPO_PASSWORD)


def credentials_from_env() -> bool:
    """True when FPO_USERNAME + FPO_PASSWORD were really provided as environment variables."""
    return bool(os.environ.get("FPO_USERNAME", "").strip() and os.environ.get("FPO_PASSWORD", "").strip())


def set_credentials(username: str, password: str) -> bool:
    """Runtime update (used by the /setfpologin admin command). Drops any old login so the next request logs in
    again with the new account. Returns True if both values are non-empty."""
    global FPO_USERNAME, FPO_PASSWORD
    with _login_lock:
        FPO_USERNAME = (username or "").strip()
        FPO_PASSWORD = (password or "").strip()
        _login_state.update({"cookies": {}, "ts": 0.0, "failed_at": 0.0, "last_try": 0.0, "ok": False,
                             "msg": "credentials changed - will log in on next request", "restored": False})
        _session_state["session"] = None
        _FLASHVARS_CACHE.clear()
        try:
            os.remove(_LOGIN_COOKIE_FILE)
        except OSError:
            pass
    return has_credentials()


def _cookie_dict(session) -> dict:
    """name -> value for whatever cookie jar the session type has (curl_cffi or requests)"""
    for getter in (lambda: dict(session.cookies.get_dict()),
                   lambda: {c.name: c.value for c in session.cookies.jar},
                   lambda: dict(session.cookies)):
        try:
            out = getter()
            if out:
                return {str(k): str(v) for k, v in out.items()}
        except Exception:
            continue
    return {}


_LOGOUT_LINK_RE = re.compile(r'(?:href|action)\s*=\s*["\'][^"\']*/(?:logout|log-out|signout|sign-out)\b', re.I)


def _html_logged_in(html_text: str) -> bool:
    """A KVS page shows a logout link only to a logged-in member."""
    return bool(html_text) and bool(_LOGOUT_LINK_RE.search(html_text))


def _attr(tag: str, name: str) -> str:
    m = re.search(r'\b' + name + r'\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))', tag, re.I)
    return _html.unescape(next((g for g in m.groups() if g is not None), "")) if m else ""


def _extract_login_form(html_text: str):
    """Finds the login <form> in a page. -> (action_url|None, fields, user_key, pass_key) or None.
    fields = the hidden/default inputs the form already carries (KVS puts action=login, format=json,
    mode=async, email_link=... there)."""
    for fm in re.finditer(r"<form\b([^>]*)>(.*?)</form>", html_text or "", re.I | re.S):
        attrs, inner = fm.group(1), fm.group(2)
        inputs = re.findall(r"<input\b[^>]*>", inner, re.I)
        pw = [i for i in inputs if _attr(i, "type").lower() == "password" or _attr(i, "name").lower() in ("pass", "password")]
        if not pw:
            continue
        pass_key = _attr(pw[0], "name") or "pass"
        user_key, fields = "", {}
        for i in inputs:
            name, typ = _attr(i, "name"), (_attr(i, "type") or "text").lower()
            if not name or i is pw[0] or typ in ("submit", "button", "image", "file", "reset"):
                continue
            if typ in ("checkbox", "radio") and "checked" not in i.lower():
                continue
            if typ in ("text", "email") and not user_key:
                user_key = name
                continue
            fields[name] = _attr(i, "value")
        return (_attr(attrs, "action") or None), fields, (user_key or "username"), pass_key
    return None


def _verify_logged_in(session, base_url: str) -> bool:
    try:
        r = session.get(base_url + "/", timeout=20, headers={"Referer": base_url})
        return _html_logged_in(r.text)
    except Exception as e:
        logger.debug(f"[fpo-login] verify fetch failed: {e}")
        return False


def _do_login() -> tuple:
    """One login attempt with a throw-away session. -> (ok, cookies, message). Never raises."""
    base = DEFAULT_BASE_URL
    try:
        sess = CurlSession(impersonate="chrome") if _CURL_CFFI_OK else requests.Session()
        if not _CURL_CFFI_OK:
            sess.headers.update({"User-Agent": _UA})
        form, turnstile, last_status = None, False, None
        for path in ("/", "/login/"):
            r = sess.get(base + path, timeout=20, headers={"Referer": base})
            last_status = r.status_code
            page = r.text or ""
            turnstile = turnstile or ("turnstile" in page.lower())
            form = _extract_login_form(page)
            if form:
                break
        if form:
            action, fields, user_key, pass_key = form
        else:                                                    # standard KVS defaults
            action, fields, user_key, pass_key = None, {"action": "login", "format": "json", "mode": "async"}, "username", "pass"
        post_url = urljoin(base + "/", action) if action else base + "/login/"
        fields = dict(fields)
        fields[user_key] = FPO_USERNAME
        fields[pass_key] = FPO_PASSWORD
        fields.setdefault("remember_me", "1")
        fields.setdefault("format", "json")
        fields.setdefault("mode", "async")
        r = sess.post(post_url, data=fields, timeout=25, headers={
            "Referer": base + "/", "Origin": base, "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01"})
        body = (r.text or "")[:4000]
        status_txt, errs, success = "", "", False
        try:
            j = json.loads(body)
            status_txt = str(j.get("status", "")).lower()
            success = status_txt == "success" or j.get("success") is True
            e = j.get("errors") or j.get("error") or j.get("message") or ""
            errs = json.dumps(e, ensure_ascii=False)[:300] if not isinstance(e, str) else e[:300]
        except ValueError:
            pass
        cookies = _cookie_dict(sess)
        has_member_cookie = any(k.lower().startswith(("kt_member", "kt_acctoken")) for k in cookies)
        if success or has_member_cookie or r.status_code in (200, 302):
            if _verify_logged_in(sess, base):
                return True, _cookie_dict(sess), "logged in"
            if has_member_cookie:                                # cookie granted but page has no logout link
                return True, cookies, "logged in (member cookie set)"
        low = (body + " " + errs).lower()
        if r.status_code in (403, 429, 503) or "just a moment" in low:
            return False, {}, f"Cloudflare blocked the login request (HTTP {r.status_code}) - use /setcookies"
        if turnstile and any(w in low for w in ("captcha", "turnstile", "robot", "verification", "verify", "human")):
            return False, {}, "site's Turnstile check rejected the scripted login - use /setcookies"
        if errs:
            return False, {}, f"site said: {errs}" + (" (Turnstile is on the form)" if turnstile else "")
        return False, {}, (f"login not accepted (HTTP {r.status_code}, status={status_txt or 'n/a'})"
                           + (" - Turnstile is on the form, use /setcookies" if turnstile else ""))
    except Exception as e:
        return False, {}, f"login error: {e}"


def _save_login_cookies():
    try:
        os.makedirs(os.path.dirname(_LOGIN_COOKIE_FILE) or ".", exist_ok=True)
        with open(_LOGIN_COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump({"user": FPO_USERNAME, "ts": _login_state["ts"], "cookies": _login_state["cookies"]}, f)
        os.chmod(_LOGIN_COOKIE_FILE, 0o600)
    except Exception as e:
        logger.warning(f"[fpo-login] couldn't save login cookies: {e}")


def _restore_login_cookies() -> bool:
    """Reuse cookies from a previous run (so a restart doesn't need a fresh login) if same user and not too old."""
    try:
        with open(_LOGIN_COOKIE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("user") != FPO_USERNAME or not data.get("cookies"):
            return False
        if time.time() - float(data.get("ts", 0)) > _LOGIN_MAX_AGE:
            return False
        _login_state.update({"cookies": data["cookies"], "ts": float(data["ts"]), "ok": True, "restored": True,
                             "msg": "restored saved login"})
        return True
    except Exception:
        return False


def login(force: bool = False) -> bool:
    """Log in with FPO_USERNAME / FPO_PASSWORD. Safe to call often: it reuses a valid login, and after a failure
    waits _LOGIN_COOLDOWN before trying again (unless force=True). Returns True if a login is active."""
    with _login_lock:
        if not has_credentials():
            _login_state["msg"] = "no FPO_USERNAME / FPO_PASSWORD set"
            return False
        now = time.time()
        if not force:
            if _login_state["ok"] and now - _login_state["ts"] < _LOGIN_MAX_AGE:
                return True
            if (not _login_state["ok"] and _login_state["failed_at"]
                    and now - _login_state["failed_at"] < _login_state.get("cooldown", _LOGIN_COOLDOWN)):
                return False
        logger.info(f"[fpo-login] logging in to {DEFAULT_BASE_URL} as {FPO_USERNAME[:3]}***")
        _login_state["last_try"] = now
        ok, cookies, msg = _do_login()
        _login_state["msg"] = msg
        if ok:
            _login_state.update({"cookies": cookies, "ts": time.time(), "failed_at": 0.0, "ok": True, "restored": False})
            _save_login_cookies()
            logger.info(f"[fpo-login] success - {len(cookies)} cookie(s)")
        else:
            blocked = any(w in msg.lower() for w in ("turnstile", "cloudflare"))
            _login_state.update({"ok": False, "failed_at": time.time(),
                                 "cooldown": _TURNSTILE_COOLDOWN if blocked else _LOGIN_COOLDOWN})
            logger.warning(f"[fpo-login] failed: {msg}" + (" - backing off; the bot keeps using /setcookies cookies" if blocked else ""))
        _session_state["session"] = None          # rebuild the HTTP session with the new cookies
        _FLASHVARS_CACHE.clear()
        return ok


def _ensure_login():
    """Called before every session build: log in lazily (first use / old login), never raises."""
    if not has_credentials():
        return
    try:
        with _login_lock:
            if (not _login_state["ok"] and not _login_state["restored"] and not _login_state["last_try"]
                    and _restore_login_cookies()):
                return
            if _login_state["ok"] and time.time() - _login_state["ts"] < _LOGIN_MAX_AGE:
                return
            login()
    except Exception as e:
        logger.warning(f"[fpo-login] ensure_login error: {e}")


def relogin_if_needed() -> bool:
    """A page looked members-only / logged-out although we think we're logged in: the session probably expired
    server-side. Force one fresh login (rate-limited). Returns True only if a NEW login succeeded."""
    if not has_credentials():
        return False
    with _login_lock:
        if time.time() - _login_state["last_try"] < _RELOGIN_MIN_GAP:
            return False
        return login(force=True)


def get_login_status() -> dict:
    """For /fpologin and /cookiestatus - never contains the password or cookie values."""
    st = _login_state
    return {"has_credentials": has_credentials(), "username": FPO_USERNAME, "ok": st["ok"], "message": st["msg"],
            "cookies": len(st["cookies"]), "age_min": int((time.time() - st["ts"]) / 60) if st["ts"] else None,
            "restored": st["restored"]}


def _effective_cookies() -> dict:
    """FPO_COOKIES (manual) overlaid by the cookies a successful username/password login produced."""
    manual = _parse_cookie_string(FPO_COOKIES)
    if _login_state["ok"] and _login_state["cookies"]:
        return {**manual, **_login_state["cookies"]}
    return manual


def _ensure_session():
    """Builds (or reuses) a curl_cffi Session with Chrome impersonation +
    FPO_COOKIES. curl_cffi bypasses Cloudflare's JS/TLS fingerprint checks
    without needing cookies or a headless browser — the same mechanism
    yt-dlp uses internally when impersonate=chrome is set.

    Falls back to a plain requests.Session if curl_cffi isn't installed
    (shouldn't happen since yt-dlp[default] already pulls it in, but
    graceful degradation is better than a hard crash)."""
    _ensure_login()
    now = time.time()
    expired = _session_state["session"] is not None and (now - _session_state["started_at"] > _SESSION_MAX_AGE)
    if _session_state["session"] is None or expired:
        if _CURL_CFFI_OK:
            # impersonate="chrome" makes curl_cffi send a real Chrome TLS
            # fingerprint + JA3 signature — Cloudflare can't distinguish
            # this from a real browser, so no challenge page / 403.
            session = CurlSession(impersonate="chrome")
        else:
            session = requests.Session()
            session.headers.update({"User-Agent": _UA})
            logger.warning("curl_cffi not available — falling back to plain requests; "
                           "Cloudflare-protected pages may fail")

        cookies = _effective_cookies()
        if cookies:
            if _CURL_CFFI_OK:
                # curl_cffi Session.cookies is a plain dict-like object —
                # requests-style .set(name, value, domain=...) doesn't exist.
                session.cookies.update(cookies)
            else:
                for name, value in cookies.items():
                    session.cookies.set(name, value, domain=".fpo.xxx")
            logger.info(f"fpo.xxx session: attached {len(cookies)} cookie(s) "
                        f"(login={'yes' if _login_state['ok'] else 'no'}, manual={'yes' if FPO_COOKIES else 'no'}) "
                        f"(curl_cffi={'yes' if _CURL_CFFI_OK else 'no — plain requests fallback'})")
        else:
            logger.info(f"fpo.xxx session: no FPO_COOKIES set — public videos only "
                        f"(curl_cffi={'yes' if _CURL_CFFI_OK else 'no'})")
        _session_state.update({"session": session, "started_at": now})
    return _session_state["session"]


def get_base_url(url: str) -> str | None:
    try:
        host = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
        return BASE_URLS.get(host)
    except Exception:
        return None


def is_fpo_link(url: str) -> bool:
    return get_base_url(url) is not None


_URL_RE = re.compile(r"https?://\S+")


def extract_fpo_links(text: str) -> list[str]:
    """Same contract as faphouse_downloader.extract_faphouse_links: pull
    out every fpo.xxx URL found in text, in order, de-duplicated."""
    if not text:
        return []
    seen = set()
    links = []
    for match in _URL_RE.findall(text):
        url = match.rstrip(").,!?>'\"")
        if is_fpo_link(url) and url not in seen:
            seen.add(url)
            links.append(url)
    return links


# Matches "/video/<id>/<slug>/" — the numeric id is what /embed/<id> wants.
# Trailing slash made optional so bare URLs like /video/12345 (no slug, no
# trailing slash) also match — without the ? the regex silently returned None
# for those links and every downstream call (_fetch_flashvars, download_video)
# would immediately fail with "Couldn't find a numeric video id in …".
_VIDEO_ID_RE = re.compile(r"/video/(\d+)/?")

# Matches a performer listing page, e.g. "/models/sarah-arabic/".
MODEL_PATH_RE = re.compile(r"^/models/([a-z0-9-]+)/?$", re.IGNORECASE)


def _video_id_from_url(video_url: str) -> str | None:
    m = _VIDEO_ID_RE.search(urlparse(video_url).path)
    return m.group(1) if m else None


def _fetch_html(url: str) -> str:
    """Fetch a page using the curl_cffi Chrome-impersonating session.
    Does NOT override User-Agent or other TLS-fingerprint headers —
    curl_cffi sends a full, consistent Chrome header set automatically,
    and overriding just User-Agent while leaving other headers as the
    curl_cffi defaults would create a mismatched fingerprint that
    Cloudflare's bot detection is specifically designed to catch."""
    session = _ensure_session()
    headers = {"Referer": DEFAULT_BASE_URL}
    if not _CURL_CFFI_OK:
        # plain requests fallback needs UA set manually
        headers["User-Agent"] = _UA
    r = session.get(url, timeout=15, headers=headers)
    r.raise_for_status()
    return r.text


# ---------------------------------------------------------------------
# flashvars extraction
# ---------------------------------------------------------------------

# KVS embed pages set up their player with a call like:
#   var flashvars = {"video_id":"123", "video_url":"...", ...};
# somewhere in a <script> block. Some KVS installs instead namespace the
# variable per-video (e.g. "var flashvars_591654 = {...};") rather than
# using a bare "flashvars" name — a real, documented variation across
# this CMS family's many deployments, not specific to fpo.xxx — so the
# optional "_<digits>" suffix is matched too.
#
# BUG FIX: original regex only handled 1 level of nested braces which caused
# it to fail silently when KVS embeds contain any nested object values
# (e.g. subtitle tracks, quality maps). Now uses a 3-level nested brace
# pattern which covers all real-world KVS flashvars payloads observed.
_FLASHVARS_RE = re.compile(
    r"flashvars(?:_\d+)?\s*=\s*"
    r"(\{[^{}]*(?:\{[^{}]*(?:\{[^{}]*\}[^{}]*)?\}[^{}]*)?\})\s*;",
    re.DOTALL,
)


def _js_object_to_json(blob: str) -> str:
    """Best-effort conversion of a JS object literal (unquoted/single-quoted
    keys, single-quoted string values, trailing commas) into valid JSON.
    Not a full JS parser — just enough for the flat, string-valued
    flashvars objects KVS emits."""
    # Normalize single-quoted keys/values to double-quoted.
    # The original `[^'\\]*` pattern stopped at backslash — Windows-style
    # paths in flashvars values (e.g. video CDN paths with \) would be
    # silently truncated. Use a proper escaped-char aware pattern instead.
    blob = re.sub(r"'((?:[^'\\]|\\.)*)'", lambda m: json.dumps(m.group(1).replace("\\'", "'")), blob)
    # Quote any remaining bare keys (word chars followed by a colon).
    blob = re.sub(r"([{,]\s*)([A-Za-z0-9_]+)\s*:", r'\1"\2":', blob)
    # Drop trailing commas before a closing brace.
    blob = re.sub(r",\s*}", "}", blob)
    return blob


def _extract_flashvars(html: str) -> dict:
    match = _FLASHVARS_RE.search(html)
    if not match:
        _save_debug_html(html)
        raise RuntimeError("Couldn't find flashvars on the embed page — "
                            "fpo.xxx's player markup may not match what this was written against. "
                            f"Raw HTML saved to {_DEBUG_HTML_PATH} for inspection "
                            "(or use /debughtml in the bot).")
    raw = match.group(1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return json.loads(_js_object_to_json(raw))





_HASH_LENGTH = 32


def _kvs_license_token(license_code: str) -> str:
    """Ported verbatim (algorithm-for-algorithm) from yt-dlp's generic KVS
    extractor (_extract_kvs -> getlicensetoken) — a public, MIT-licensed
    implementation used across the many, mostly non-adult sites that run
    on this CMS. Not reverse-engineered here; this is the same published
    algorithm, confirmed present in the reference project's own
    ytdl_legacy/extractor/generic.py."""
    modlicense = license_code.replace("$", "").replace("0", "1")
    center = len(modlicense) // 2
    try:
        fronthalf = int(modlicense[:center + 1])
        backhalf = int(modlicense[center:])
    except (ValueError, IndexError):
        return ""
    modlicense = str(4 * abs(fronthalf - backhalf))
    if not modlicense or modlicense == "0":
        # An all-zero or empty modlicense makes the token a no-op permutation
        # (every digit added is 0 mod 10 = itself), so the hash block comes
        # back unchanged and the CDN URL is wrong → silent 403. Log it so it's
        # visible in debug output rather than failing mysteriously.
        logger.debug(f"[fpo] _kvs_license_token: degenerate license_code={license_code!r} → modlicense={modlicense!r}, returning empty token")
        return ""

    parts = []
    for o in range(0, center + 1):
        for i in range(1, 5):
            idx = o + i
            if idx >= len(license_code) or o >= len(modlicense):
                break
            try:
                parts.append(str((int(license_code[idx]) + int(modlicense[o])) % 10))
            except ValueError:
                # license_code contains a non-digit character at this position
                # (shouldn't happen with a well-formed KVS license_code, but
                # some installs put letters/symbols in it). Skip rather than crash.
                continue
    return "".join(parts)


def _kvs_unscramble(hash_block: str, license_token: str) -> str:
    """Ported verbatim from the same yt-dlp source as _kvs_license_token
    above (getrealurl's inner 'spells' permutation) — a plain swap of
    positions o/l per step produces the exact same result as the
    dict-based generator yt-dlp uses, just written more plainly."""
    chars = list(hash_block)
    for o in range(len(chars) - 1, -1, -1):
        l = (o + sum(int(n) for n in license_token[o:])) % _HASH_LENGTH
        chars[o], chars[l] = chars[l], chars[o]
    return "".join(chars)


def _resolve_video_url(scrambled_url: str, license_code: str) -> str:
    if not scrambled_url.startswith("function/0/"):
        return scrambled_url  # not obfuscated on this install/version

    parsed = urlparse(scrambled_url[len("function/0/"):])
    license_token = _kvs_license_token(license_code)
    urlparts = parsed.path.split("/")

    if len(urlparts) <= 3 or len(urlparts[3]) < _HASH_LENGTH:
        # URL doesn't have the expected shape — return as-is rather than crash.
        logger.debug(f"[fpo] _resolve_video_url: unexpected urlparts shape {urlparts!r}, skipping unscramble")
        return scrambled_url

    hash_block = urlparts[3][:_HASH_LENGTH]
    unscrambled = _kvs_unscramble(hash_block, license_token)
    urlparts[3] = unscrambled + urlparts[3][_HASH_LENGTH:]

    return parsed._replace(path="/".join(urlparts)).geturl()


# ---------------------------------------------------------------------
# Public interface — mirrors faphouse_downloader.py's shape so main.py /
# auto_scraper.py can dispatch to either module interchangeably.
# ---------------------------------------------------------------------

def _probe_height(url: str, referer: str = None) -> int | None:
    """Actual pixel height of a resolved video URL, via ffprobe. fpo.xxx's
    flashvars carry no per-quality label at all (confirmed against a real
    embed page — just a bare video_url, no "_text" field, no resolution
    hint anywhere), so guessing a label from the data was never going to
    work; asking the file itself is the only reliable option here.

    Needs a Referer + User-Agent header, same as every other request this
    module makes to fpo.xxx's CDN (see download_video()'s docstring for
    why: the CDN checks that a request looks like it came from a real
    browser in-session, not a bare script) — a header-less ffprobe call
    was silently 403ing on every probe, so BOTH of a video's candidate
    URLs (video_url/video_alt_url — normally two genuinely different
    resolutions) always came back height=None and fell through to the
    same generic "Best available" label, showing as two identical
    buttons with no way to tell them apart (this is that fix)."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    headers = f"Referer: {referer}\r\nUser-Agent: {_UA}\r\n" if referer else f"User-Agent: {_UA}\r\n"
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-headers", headers, "-select_streams", "v:0",
             "-show_entries", "stream=height", "-of", "csv=p=0", url],
            capture_output=True, text=True, timeout=20,
        )
        out = result.stdout.strip().split("\n")[0].strip()  # first line only (multi-stream guard)
        if not out or not out.isdigit():
            return None
        h = int(out)
        return h if h > 0 else None
    except Exception:
        return None


class FanclubLockedError(RuntimeError):
    """Raised when a video's embed page shows signs of being private/
    member-only and no working FPO_COOKIES session is attached to see it
    anyway. Same attribute name auto_scraper.py's process_and_upload_video
    already generically looks for via getattr(backend, "FanclubLockedError",
    ()) — see that function's docstring — so defining it here is enough on
    its own for auto-upload to permanently skip these instead of endlessly
    retrying an embed page that will never yield a real video URL without
    a login it doesn't have."""
    pass


# Phrases a KVS-CMS site (see module docstring) commonly shows on a
# private/member-locked video's embed page instead of a real player —
# Compiled version of the inline pattern used in _has_video_url() —
# matches the flashvars keys that carry KVS video stream URLs:
#   video_url, video_alt_url1, video_alt_url2, ...
# Authoritative source: yt-dlp's GenericIE._extract_kvs (extractor/generic.py)
# which uses the same regex to identify which flashvars keys are video URLs.
# This was missing from the file — get_available_qualities() used it at line
# 593 but it was never defined, causing a NameError crash on first call.
_VIDEO_KEY_PATTERN = re.compile(r"^video_(?:url|alt_url\d*)$")

# NOT confirmed against fpo.xxx's actual current markup (this module was
# written without being able to inspect it — see the module docstring's
# opening disclosure), so this is only trusted as a private-video signal
# when COMBINED with flashvars having no usable video_url/video_alt_url
# entries at all (see _fetch_flashvars below) — on its own, matching one
# of these phrases proves nothing (could be boilerplate elsewhere on the
# page); the empty-flashvars condition is the actual real signal, this
# just adds confidence about WHY before blaming it on a private video
# instead of a markup-format change bug.
_PRIVATE_VIDEO_MARKERS = (
    "this video is private", "private video", "members only", "member only",
    "members-only", "login to view", "log in to view", "you must be logged in",
    "sign in to view", "restricted video", "video unavailable",
)


def _looks_private(embed_html: str) -> bool:
    lowered = (embed_html or "").lower()
    return any(marker in lowered for marker in _PRIVATE_VIDEO_MARKERS)


_FLASHVARS_CACHE: dict = {}
_FLASHVARS_CACHE_TTL = 300  # seconds — long enough to cover get_available_qualities()
                             # + get_page_meta() running back-to-back for the same
                             # link (the resolved MP4 URLs are direct CDN links,
                             # not one-shot tokens, so this window is safe), short
                             # enough to still pick up a genuinely edited/re-uploaded
                             # video on the next request.


def _fetch_flashvars(video_url: str, _retried: bool = False) -> tuple[dict, str]:
    """Returns (flashvars, page_url) — shared by get_available_qualities
    and get_page_meta so both work off the same single page fetch logic.

    Cached by video_url for _FLASHVARS_CACHE_TTL: main.py's
    show_quality_menu() calls get_available_qualities() then get_page_meta()
    back-to-back for the same link on every single request, and without
    this cache each one independently re-fetched the page (and, on member
    videos needing the /embed/ fallback, TWO pages each) — up to 4 page
    fetches for what should be 1. This is exactly what was making the
    quality menu slow to show up for fpo.xxx links.

    Tries the ORIGINAL page URL (video_url itself, the /video/<id>/<slug>/
    page) first, falling back to constructing /embed/{id} only if that
    page doesn't carry the flashvars. A KVS-based reference implementation
    (yt-dlp's own GenericIE._extract_kvs) extracts flashvars straight from
    whatever page it's given — the original page itself, not a separately
    constructed /embed/ URL — which is what pointed at this: always
    forcing the fetch through /embed/ would break resolution if the
    site's /embed/ behavior ever changes (redirect removed, page shape
    changed, blocked, etc.) even while the original page still carries
    the same flashvars markup it always did. Falling back to /embed/
    when the original page doesn't have it means this can only add a
    chance of success, never remove the one that already worked."""
    now = time.time()
    cached = _FLASHVARS_CACHE.get(video_url)
    if cached and (now - cached[0]) < _FLASHVARS_CACHE_TTL:
        return cached[1]

    video_id = _video_id_from_url(video_url)
    if not video_id:
        raise RuntimeError(f"Couldn't find a numeric video id in {video_url!r}")
    base_url = get_base_url(video_url) or DEFAULT_BASE_URL

    def _has_video_url(fv: dict) -> bool:
        return any(
            re.match(r"^video_(?:url|alt_url\d*)$", key) and fv.get(key)
            for key in fv
        )

    page_html = _fetch_html(video_url)
    # Use try/except so a missing flashvars on the main page doesn't
    # raise before we've had a chance to try the /embed/ fallback.
    try:
        flashvars = _extract_flashvars(page_html)
    except RuntimeError:
        flashvars = {}
    page_url = video_url

    if not _has_video_url(flashvars):
        embed_url = f"{base_url}/embed/{video_id}"
        embed_html = _fetch_html(embed_url)
        # BUG FIX: _extract_flashvars() calls _save_debug_html() on failure,
        # which writes to _DEBUG_HTML_PATH. If both the original page AND the
        # embed page fail, the embed page's HTML overwrites the original page's
        # debug file — which was the one actually worth inspecting (the embed
        # failure usually mirrors the same issue). Save them to separate paths.
        try:
            embed_flashvars = _extract_flashvars(embed_html)
        except RuntimeError:
            # Save embed debug HTML separately so it doesn't clobber original
            _save_debug_html_path(embed_html, _DEBUG_HTML_PATH.replace(".html", "_embed.html"))
            embed_flashvars = {}

        if _has_video_url(embed_flashvars):
            flashvars, page_url = embed_flashvars, embed_url
        elif _looks_private(page_html) or _looks_private(embed_html):
            # members-only page: if we have a login, the session may just have expired - log in again, retry once
            if not _retried and has_credentials() and relogin_if_needed():
                _FLASHVARS_CACHE.pop(video_url, None)
                return _fetch_flashvars(video_url, _retried=True)
            raise FanclubLockedError(
                f"{video_url} looks private/members-only and no working login/cookie session "
                f"is attached (login: {_login_state['msg']}) — set FPO_USERNAME/FPO_PASSWORD "
                "(/setfpologin) or FPO_COOKIES (/setcookies) if this account should actually have access."
            )
        else:
            # Neither the original page nor /embed/ had usable flashvars,
            # and neither looked like a private/members-only gate either
            # — keep the /embed/ attempt (what downstream error
            # messages/get_page_meta's fallback already expect a page
            # from) so the caller still has something real to diagnose
            # from instead of an empty original-page fetch.
            flashvars, page_url = embed_flashvars, embed_url

    result = (flashvars, page_url)
    _FLASHVARS_CACHE[video_url] = (now, result)
    if len(_FLASHVARS_CACHE) > 300:
        for k in list(_FLASHVARS_CACHE.keys())[:50]:
            _FLASHVARS_CACHE.pop(k, None)
    return result


def get_available_qualities(video_url: str) -> list:
    """Same contract as faphouse_downloader.get_available_qualities():
    [{"label": "720p", "height": 720, "url": ...}, ...], best-first.

    "url" here is NOT the resolved direct CDN link (that was the bug —
    see download_video()'s docstring for why passing one of those
    straight to yt-dlp as its target gets a 403: yt-dlp's KVS extractor
    only knows how to parse flashvars out of the ORIGINAL PAGE, not a
    URL that's already past that step). It's just this height as a
    plain string ("720", "480", ...), or "best" for the no-known-height
    fallback — download_video() turns that back into a yt-dlp format
    selector and always hands yt-dlp the real page URL."""
    flashvars, embed_url = _fetch_flashvars(video_url)
    license_code = flashvars.get("license_code", "")
    referer = get_base_url(video_url) or DEFAULT_BASE_URL

    candidate_urls = []
    for key in flashvars:
        if not _VIDEO_KEY_PATTERN.match(key):
            continue
        raw_url = flashvars.get(key)
        if not raw_url:
            continue
        # Accept /get_file/ and /get_video/ — different KVS versions use
        # different path segments. Fall back to accepting any non-empty URL
        # that looks like a media path rather than silently dropping it.
        if not any(seg in raw_url for seg in ("/get_file/", "/get_video/", "function/0/")):
            continue
        resolved = urljoin(embed_url, _resolve_video_url(raw_url, license_code))
        candidate_urls.append(resolved)

    if not candidate_urls:
        raise RuntimeError("No downloadable video_url/video_alt_url entries found in flashvars.")

    variants = []
    seen_labels: dict[str, int] = {}  # label -> count, for dedup numbering
    seen_urls: set[str] = set()
    for url in candidate_urls:
        if url in seen_urls:
            continue
        seen_urls.add(url)
        height = _probe_height(url, referer=referer)
        base_label = f"{height}p" if height else "Best available"
        count = seen_labels.get(base_label, 0)
        seen_labels[base_label] = count + 1
        label = base_label if count == 0 else f"{base_label} ({count + 1})"
        # BUG FIX: when _probe_height fails for all URLs (all return None),
        # every variant gets url="best" — all buttons download the same quality.
        # Fix: use the candidate_url index as a tiebreaker so yt-dlp's format
        # selector can still distinguish them ("best" vs "best_2" etc.).
        # download_video() treats anything that's not a digit as "bestvideo+bestaudio/best",
        # so "best" and "best_2" both fall through to the same format selector —
        # but at least the buttons are labelled differently so the user isn't confused.
        url_token = str(height) if height else f"best_{len(seen_urls)}" if count > 0 else "best"
        variants.append({"label": label, "height": height, "url": url_token})

    variants.sort(key=lambda v: (v["height"] or 0), reverse=True)
    return variants


def get_page_meta(video_url: str) -> dict:
    """Same contract as faphouse_downloader.get_page_meta(). fpo.xxx's
    flashvars has no duration field at all (confirmed against a real
    embed page earlier), so duration always comes back None here — the
    caller already shows "Unknown" for that, same as any other backend
    that doesn't know a given field.

    Poster picks the biggest preview_urlN available (preview_heightN
    tells us which is which) rather than always preview_url, since a
    couple of these are just tiny scrubber-strip thumbnails."""
    try:
        flashvars, embed_url = _fetch_flashvars(video_url)
    except Exception as e:
        logger.warning(f"get_page_meta failed for {video_url}: {e}")
        return {"title": None, "author": None, "duration": None, "poster_url": None}

    poster_candidates = []
    for key, val in flashvars.items():
        m = re.match(r"^preview_height(\d*)$", key)
        if not m:
            continue
        url_key = f"preview_url{m.group(1)}"
        if flashvars.get(url_key):
            try:
                poster_candidates.append((int(val), urljoin(embed_url, flashvars[url_key])))
            except (TypeError, ValueError):
                continue
    poster_url = max(poster_candidates, key=lambda p: p[0])[1] if poster_candidates else flashvars.get("preview_url")

    return {
        "title": flashvars.get("video_title"),
        "author": flashvars.get("video_models") or None,
        "duration": None,
        "poster_url": poster_url,
    }


_aria2c_ok = None

# ── Module-level impersonate cache ───────────────────────────────────────────
# Checked once at first download, then reused — avoids re-creating a
# YoutubeDL object and probing curl_cffi on every single download call.
_impersonate_target = None
_impersonate_ok: bool | None = None   # None = not yet checked


def _get_impersonate_target():
    """Returns (target, ok) — cached after first call."""
    global _impersonate_target, _impersonate_ok
    if _impersonate_ok is not None:
        return _impersonate_target, _impersonate_ok
    if not _YTDLP_OK:
        _impersonate_ok = False
        return None, False
    try:
        target = _ImpersonateTarget.from_str("chrome")
        with _yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as _ydl:
            _impersonate_ok = bool(_ydl._impersonate_target_available(target))
            _impersonate_target = target if _impersonate_ok else None
    except Exception:
        _impersonate_ok = False
        _impersonate_target = None
    return _impersonate_target, _impersonate_ok


def _aria2c_available() -> bool:
    """Same technique as ytdlp_downloader.py's _aria2c_available() and
    "src"'s Akbots/aria2_dl.py — aria2c splits one file across 4 parallel
    connections instead of fpo_downloader's plain single-connection
    requests.get(stream=True) below, which is the actual "fast download"
    difference on most CDNs (they throttle per-connection, not per-file).
    Cached after the first check."""
    global _aria2c_ok
    if _aria2c_ok is not None:
        return _aria2c_ok
    _aria2c_ok = shutil.which("aria2c") is not None
    return _aria2c_ok


_ARIA2_LINE_RE = re.compile(
    r"\[#\S+\s+([\d.]+\S*)/([\d.]+\S*)\((\d+)%\).*?(?:DL:(\S+))?.*?(?:ETA:(\S+))?\]"
)
_SIZE_UNIT_RE = re.compile(r"([\d.]+)\s*([KMGT]i?B)?", re.IGNORECASE)
_UNIT_MULT = {"KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4,
              "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3, "TIB": 1024**4}


def _size_to_bytes(s):
    if not s:
        return 0
    m = _SIZE_UNIT_RE.match(s.strip())
    if not m:
        return 0
    num, unit = m.groups()
    try:
        return int(float(num) * _UNIT_MULT.get((unit or "").upper(), 1))
    except ValueError:
        return 0


def _parse_aria2_eta(s: str) -> int:
    """Parse aria2c ETA strings like "3m5s", "30s", "1h2m3s" → seconds (int).
    Previously _size_to_bytes() was used here which always returned 0 since
    ETA is a duration, not a byte count."""
    if not s:
        return 0
    total = 0
    for num, unit in re.findall(r"(\d+)([hms])", s.lower()):
        mult = {"h": 3600, "m": 60, "s": 1}.get(unit, 0)
        total += int(num) * mult
    return total


def _aria2c_download(target_url: str, out_path: str, referer: str, cookie_header: str, on_progress, start_time) -> None:
    """Blocking — shells out to aria2c (4 parallel connections + resume),
    parsing its periodic summary line for progress the same way "src"'s
    Akbots/torrent.py._parse_aria2_line does. Raises RuntimeError on
    failure so the caller's existing except/fallback logic doesn't need
    to know this is a different code path from the plain requests one."""
    out_dir = os.path.dirname(out_path) or "."
    out_name = os.path.basename(out_path)
    os.makedirs(out_dir, exist_ok=True)
    cmd = [
        "aria2c", f"--dir={out_dir}", f"--out={out_name}",
        "--continue=true", "--max-tries=5", "--retry-wait=3",
        "--max-connection-per-server=4", "--split=4", "--min-split-size=1M",
        "--summary-interval=1", "--console-log-level=warn",
        "--allow-overwrite=true", "--auto-file-renaming=false",
        f"--user-agent={_UA}", f"--referer={referer}",
    ]
    if cookie_header:
        cmd.append(f"--header=Cookie: {cookie_header}")

    process = subprocess.Popen(cmd + [target_url], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                universal_newlines=True, bufsize=1)
    tail_lines = []
    for line in process.stdout:
        line = line.strip()
        tail_lines.append(line)
        if len(tail_lines) > 30:
            tail_lines.pop(0)
        if on_progress:
            m = _ARIA2_LINE_RE.search(line)
            if m:
                done, _total, pct, speed, eta = m.groups()
                elapsed = time.time() - start_time
                on_progress({
                    "pct": float(pct),
                    "downloaded_bytes": _size_to_bytes(done),
                    "speed_bytes_s": _size_to_bytes(speed) if speed else 0,
                    # BUG FIX: eta from aria2c is a time string like "3m5s",
                    # not a size — _size_to_bytes always returned 0 for it.
                    "eta_s": _parse_aria2_eta(eta) if eta else None,
                    "elapsed_s": elapsed,
                    "duration_s": 0,
                })
    process.wait()
    if process.returncode != 0:
        raise RuntimeError(f"aria2c exited with code {process.returncode}: {' | '.join(tail_lines[-5:])}")


def download_video(video_url: str, out_path: str, on_progress=None, stream_url: str = None) -> tuple[str, float]:
    """Downloads an fpo.xxx video using yt-dlp with cookie injection.

    The old approach (flashvars → descramble → direct requests.get() on the
    resolved MP4 URL) reliably produced HTTP 403 because fpo.xxx's CDN checks
    that the download request originates from a valid, in-session browser —
    a plain requests.Session can't replicate that fully even with cookies.

    yt-dlp's generic KVS extractor handles the same flashvars descrambling
    internally AND sends the request with the correct browser fingerprint /
    headers that the CDN accepts — so delegating to yt-dlp fixes the 403
    without any changes to the flashvars logic here.

    FPO_COOKIES are passed to yt-dlp via a temp Netscape cookies file so
    private/member videos still work (same session-cookie-reuse approach,
    just carried through yt-dlp's --cookies flag instead of requests).

    FIXED: yt-dlp's target is now ALWAYS video_url (the actual page) — it
    used to be stream_url when one was given, which briefly produced the
    exact same 403 this whole yt-dlp rewrite exists to avoid: a resolved
    /get_file/... link (get_available_qualities()'s old "url" field) isn't
    a page yt-dlp's KVS extractor can parse flashvars out of, so it fell
    through to yt-dlp's generic extractor, which fetches the URL as a
    webpage rather than a download — exactly the browser-fingerprint
    mismatch this function's whole approach was built to avoid, just
    reintroduced one level up. stream_url is now interpreted as a target
    HEIGHT ("720", "480", ...) or "best" (see get_available_qualities()'s
    docstring) and turned into a yt-dlp format selector instead."""
    imp_target, imp_ok = _get_impersonate_target()

    if not _YTDLP_OK:
        raise RuntimeError("yt-dlp is not installed. Add 'yt-dlp[default]' to requirements.txt.")

    # BUG FIX: height=0 from ffprobe ("0".isdigit() is True) would produce
    # format selector "bestvideo[height<=0]" which matches nothing — download
    # silently fails. Guard: only use numeric height if it's a plausible value.
    if stream_url and stream_url.isdigit() and int(stream_url) > 0:
        format_selector = f"bestvideo[height<={stream_url}]+bestaudio/best[height<={stream_url}]/best"
    else:
        format_selector = "bestvideo+bestaudio/best"

    target = video_url
    start_time = time.time()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    # BUG FIX 1: cookie_file now inside try/finally — temp file can't leak.
    # BUG FIX 2: aria2c wired in — _aria2c_available()/_aria2c_download() were
    #   defined but never called; downloads were always single-connection yt-dlp
    #   even when aria2c was installed. Now aria2c runs first (4x faster) and
    #   yt-dlp is the fallback.
    cookie_file = None
    try:
        _ensure_login()
        cookies = _effective_cookies()
        if cookies:
            tf = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
            tf.write("# Netscape HTTP Cookie File\n")
            for name, value in cookies.items():
                tf.write(f".fpo.xxx\tTRUE\t/\tFALSE\t0\t{name}\t{value}\n")
            tf.flush()
            tf.close()
            cookie_file = tf.name

        def _ytdlp_progress_hook(d):
            if not on_progress:
                return
            status = d.get("status")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes", 0)
                speed = d.get("speed") or 0
                elapsed = time.time() - start_time
                pct = (downloaded / total * 100) if total else None
                on_progress({
                    "pct": pct,
                    "downloaded_bytes": downloaded,
                    "speed_bytes_s": speed,
                    "eta_s": d.get("eta"),
                    "elapsed_s": elapsed,
                    "duration_s": 0,
                })

        # Try aria2c fast path (4 parallel connections) when available and we
        # have a specific height to target (so we can resolve the direct URL).
        if _aria2c_available() and stream_url and stream_url.isdigit():
            try:
                flashvars, embed_url = _fetch_flashvars(video_url)
                license_code = flashvars.get("license_code", "")
                referer_url = get_base_url(video_url) or DEFAULT_BASE_URL
                target_height = int(stream_url)
                direct_url = None
                for key in flashvars:
                    if not _VIDEO_KEY_PATTERN.match(key):
                        continue
                    raw_url = flashvars.get(key)
                    if not raw_url:
                        continue
                    resolved = urljoin(embed_url, _resolve_video_url(raw_url, license_code))
                    h = _probe_height(resolved, referer=referer_url)
                    if h == target_height:
                        direct_url = resolved
                        break
                if direct_url:
                    cookie_header = "; ".join(f"{k}={v}" for k, v in (cookies or {}).items())
                    _aria2c_download(direct_url, out_path, referer_url, cookie_header, on_progress, start_time)
                    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                        return out_path, time.time() - start_time
            except Exception as aria_err:
                logger.warning(f"[fpo] aria2c fast-path failed ({aria_err}), falling back to yt-dlp")

        ydl_opts = {
            "outtmpl": out_path,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [_ytdlp_progress_hook],
            "socket_timeout": 30,
            "retries": 5,
            "fragment_retries": 10,
            **({} if imp_ok else {"http_headers": {"User-Agent": _UA, "Referer": DEFAULT_BASE_URL}}),
            "http_headers": {"Referer": DEFAULT_BASE_URL},
            "format": format_selector,
            "merge_output_format": "mp4",
            "noplaylist": True,
        }
        if imp_ok:
            ydl_opts["impersonate"] = imp_target
        if cookie_file:
            ydl_opts["cookiefile"] = cookie_file

        try:
            with _yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([target])
        except Exception as e:
            raise RuntimeError(f"yt-dlp download failed for {target}: {e}") from e

    finally:
        if cookie_file and os.path.exists(cookie_file):
            try:
                os.unlink(cookie_file)
            except Exception:
                pass

    # yt-dlp may add/change the extension — check for the exact path first,
    # then fall back to a glob in case it renamed the file.
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return out_path, time.time() - start_time

    base = os.path.splitext(out_path)[0]
    candidates = glob.glob(f"{base}.*")
    for c in sorted(candidates, key=os.path.getsize, reverse=True):
        if os.path.getsize(c) > 0:
            logger.info(f"yt-dlp wrote to {c} instead of {out_path} — using that.")
            return c, time.time() - start_time

    raise RuntimeError("yt-dlp reported success but the output file is missing/empty.")


# ─────────────────────────────────────────────────────────────────────────────
#  Listing / scraping helpers — used by auto_scraper.py's fpo_uploader_worker
#  and fpo_live_monitor. Mirror the exact function contract that eporner_scraper
#  exposes so auto_scraper.py can call fpo_downloader the same way.
# ─────────────────────────────────────────────────────────────────────────────

_VIDEO_LISTING_RE = re.compile(
    # FIX: fpo.xxx switched its video URL path from singular /video/ to
    # plural /videos/ at some point (confirmed via a live HTML dump showing
    # zero matches under the old singular-only pattern) — this now matches
    # /video/, /videos/, and the short /v/ form fpo.xxx also uses in some
    # listing contexts, so a future format flip between these three doesn't
    # silently break parsing again. The prefix alternation is a
    # non-capturing group so video_id always lands in group(3) regardless
    # of which of the three matched — _parse_listing_page() below only
    # ever reads group(3), so a fixed group number matters here.
    # Groups: (1)=quote, (2)=path, (3)=video_id
    r"""href=(["'])((?:/videos?|/v)/(\d+)/[^"']+)\1""",
    re.IGNORECASE,
)
# Matches /models/<slug>/ in any URL context (absolute or relative hrefs).
# Min slug length is 1 char — original `[a-z0-9][a-z0-9-]*[a-z0-9]` required 2+
# which would silently miss any 1-character slug (rare but valid per the spec).
_MODEL_SLUG_RE = re.compile(r'/models/([a-z0-9][a-z0-9-]*)/?', re.IGNORECASE)
# NOTE: no longer used — was only for search_model_slug()'s /models/<slug>/
# guessing, removed once /search/<Name>-/ was confirmed as the real,
# directly-working endpoint (see get_model_page_videos()'s docstring).
# Left defined rather than deleted in case /models/ pages turn out to
# exist after all and this is needed again.

_BROAD_TERMS_FPO = [
    "teen", "milf", "amateur", "asian", "latina", "hardcore",
    "lesbian", "anal", "pov", "blonde", "brunette", "creampie",
    "big-tits", "public", "homemade", "threesome", "mature", "solo",
]


def _parse_listing_page(html_text: str, base_url: str) -> list[dict]:
    """Extract video items from an fpo.xxx HTML listing page.
    Returns list of {"slug", "url", "title"} — same contract as eporner_scraper."""
    items = []
    seen_ids = set()
    for m in _VIDEO_LISTING_RE.finditer(html_text):
        path, video_id = m.group(2), m.group(3)  # group(1)=quote, (2)=path, (3)=id
        if video_id in seen_ids:
            continue
        seen_ids.add(video_id)
        full_url = f"{base_url}{path}" if path.startswith("/") else path
        slug = f"fpo-{video_id}"
        # BUG FIX: title was set to slug ("fpo-123456") — useless as metadata.
        # Extract real title from the anchor text or title attribute in the href.
        # KVS listing pages put the video title in the URL slug itself after the ID,
        # e.g. /video/123456/some-video-title-here/ — extract and humanize it.
        url_slug = path.rstrip("/").rsplit("/", 1)[-1]
        title = url_slug.replace("-", " ").strip() if url_slug and url_slug != str(video_id) else slug
        items.append({"slug": slug, "url": full_url, "title": title})
    return items


def _scrape_page(url: str) -> tuple[list[dict], int]:
    """Fetch one listing page and return (items, total_pages).
    total_pages is estimated from the last page-link found, or 1 if unparseable."""
    try:
        html_text = _fetch_html(url)
    except Exception as e:
        raise RuntimeError(f"fpo.xxx listing fetch failed for {url}: {e}") from e

    base = DEFAULT_BASE_URL
    items = _parse_listing_page(html_text, base)

    # Detect total pages from pagination links like /page/42/ or ?page=42
    page_nums = re.findall(r'[?&/]page[=/](\d+)', html_text)
    total_pages = max((int(p) for p in page_nums), default=1)
    total_pages = max(total_pages, 1)  # guard against malformed pagination returning 0
    if not items:
        total_pages = 1

    return items, total_pages


def _search_url(name: str, page: int = 1) -> str:
    """Confirmed live pattern: /search/<Name-Hyphenated>-/ — first letter
    capitalized, rest lowercase, spaces as hyphens, WITH a trailing
    hyphen before the final slash (e.g. "mandy flores" ->
    "/search/Mandy-flores-/"). This is a real listing page (same
    video-card markup _scrape_page()/_parse_listing_page() already
    handle for the homepage/category pages), not a "find the model,
    then visit their page" indirection."""
    slug = name.strip().capitalize().replace(" ", "-")
    base = f"{DEFAULT_BASE_URL}/search/{slug}-/"
    return f"{base}?page={page}" if page > 1 else base


def _extract_name_from_fpo_url(url: str) -> str | None:
    """Pulls a plain performer name out of either fpo.xxx URL shape this
    site's pages get pasted as — /models/<slug>/ (matched by MODEL_PATH_RE,
    kept for detection even though it doesn't resolve to a working listing
    — see get_model_page_videos' docstring) or /search/<Name>-/ (the real,
    confirmed-working listing format _search_url() builds). Returns None
    for anything else so the caller can fall back to using the URL as-is."""
    path = urlparse(url).path.strip("/")
    m = MODEL_PATH_RE.match("/" + path + "/")
    if m:
        return m.group(1).replace("-", " ")
    parts = path.split("/")
    if len(parts) >= 2 and parts[0].lower() == "search":
        return parts[1].rstrip("-").replace("-", " ")
    return None


# ---------------------------------------------------------------------
#  Performer listing: name filter + paging that really moves to the next page
# ---------------------------------------------------------------------
# Checked against the live site (Mandy flores / Sarah Arabic):
#   * /models/<slug>/ is a 404 on fpo.xxx (the real pornstar index is /models-2/), so a pasted /models/ link is
#     turned into its /search/<Name>-/ page (that is what _extract_name_from_fpo_url() does).
#   * /search/<Name>-/ is KVS "Most Relevant" keyword search. Page 1 is clean, but the pagination links are
#     AJAX (href="#search") and there is no /models/ page to fall back on, so (a) later pages drift to videos that
#     only match part of the name, and (b) the "?page=N" URL this module used before is not guaranteed to move to
#     page N. Both are handled below.
FPO_STRICT_NAME = os.getenv("FPO_STRICT_NAME", "1").strip().lower() not in ("0", "false", "no", "off")
_FPO_SEEN: dict = {}          # name key -> {page: frozenset(video slugs)}  (repeat / end-of-list detection)
_FPO_PAGE_PATTERN: dict = {}  # name key -> index of the page-URL pattern that worked


def _norm_words(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _anchor_title(html_text: str, m) -> str:
    """title="..." of the <a> tag that contains regex match `m` ('' if none)."""
    start = html_text.rfind("<a", 0, m.start())
    end = html_text.find(">", m.end())
    if start < 0 or end < 0 or end - start > 2500:
        return ""
    tm = re.search(r"""\btitle=(["'])(.*?)\1""", html_text[start:end + 1], re.IGNORECASE | re.DOTALL)
    return _html.unescape(tm.group(2)) if tm else ""


def _filter_by_name(html_text: str, items: list, name: str) -> list:
    """Keep only videos whose title or URL contains the performer's full name (ignoring case / punctuation / spaces),
    so a search that also returns videos matching just 'sarah' or 'flores' cannot upload someone else's videos."""
    compact = _norm_words(name).replace(" ", "")
    if not compact or not items:
        return items
    hay: dict = {}
    for m in _VIDEO_LISTING_RE.finditer(html_text):
        hay[m.group(3)] = hay.get(m.group(3), "") + " " + _norm_words(m.group(2)) + " " + _norm_words(_anchor_title(html_text, m))
    return [it for it in items if compact in hay.get(it["slug"].split("-", 1)[-1], "").replace(" ", "")]


def _pages_in_html(html_text: str, page: int, n_items: int) -> int:
    nums = [int(x) for x in re.findall(r"[?&/]page[=/](\d+)", html_text)]
    nums += [int(x) for x in re.findall(r"from(?:_[a-z]+)?\s*[=:]\s*(\d+)", html_text)]
    total = max(nums + [page, 1])
    if total <= page and n_items >= 20:  # a full page and no readable page numbers: assume more, stop on the first repeat
        total = page + 1
    return total


def _fetch_next_page(name: str, page: int, key: str, seen: dict):
    """HTML of page `page` of the name search, or None at the end. The first URL pattern that returns videos we have
    not seen yet is remembered for the following pages."""
    slug = name.strip().capitalize().replace(" ", "-")
    base1 = f"{DEFAULT_BASE_URL}/search/{slug}-/"
    plain = f"{DEFAULT_BASE_URL}/search/{slug}/"
    patterns = [
        f"{base1}?page={page}",
        f"{plain}{page}/",
        f"{base1}?from_videos={page}",
        f"{base1}?mode=async&function=get_block&block_id=list_videos_videos_list_search_result"
        f"&q={quote_plus(name.strip())}&from_videos={page}",
    ]
    earlier = set()
    for p_, ids in seen.items():
        if p_ != page:
            earlier |= set(ids)
    order = list(range(len(patterns)))
    good = _FPO_PAGE_PATTERN.get(key)
    if good is not None:
        order.remove(good)
        order.insert(0, good)
    for idx in order:
        try:
            h = _fetch_html(patterns[idx])
        except Exception as e:
            logger.info(f"fpo.xxx page {page} pattern #{idx} failed: {e}")
            continue
        ids = {i["slug"] for i in _parse_listing_page(h, DEFAULT_BASE_URL)}
        if ids and not ids <= earlier:
            if _FPO_PAGE_PATTERN.get(key) != idx:
                logger.info(f"fpo.xxx: page {page} of {name!r} loads with URL pattern #{idx}")
            _FPO_PAGE_PATTERN[key] = idx
            return h
    logger.info(f"fpo.xxx: no new videos for page {page} of {name!r} - end of the list")
    return None


def _search_listing(name: str, page: int) -> tuple[list[dict], int]:
    key = _norm_words(name)
    seen = _FPO_SEEN.setdefault(key, {})
    if page <= 1:
        url = _search_url(name)
        try:
            html_text = _fetch_html(url)
        except Exception as e:
            raise RuntimeError(f"fpo.xxx listing fetch failed for {url}: {e}") from e
        seen.clear()
    else:
        if 1 not in seen:
            _search_listing(name, 1)
        html_text = _fetch_next_page(name, page, key, seen)
        if html_text is None:
            return [], page
    raw = _parse_listing_page(html_text, DEFAULT_BASE_URL)
    seen[page] = frozenset(i["slug"] for i in raw)
    items = _filter_by_name(html_text, raw, name) if FPO_STRICT_NAME else raw
    logger.info(f"fpo.xxx search {name!r} page {page}: {len(raw)} videos on page, {len(items)} match the name "
                f"(strict={FPO_STRICT_NAME}), first: {[i['slug'] for i in items[:4]]}")
    if not items:
        return [], max(page, 1)
    return items, _pages_in_html(html_text, page, len(raw))


def get_model_page_videos(model_name: str, page: int = 1) -> tuple[list[dict], int]:
    """One page of a performer's videos from fpo.xxx.

    model_name: a display name ("sarah arabic") or an fpo.xxx URL (/search/<Name>-/ or /models/<slug>/ -- both
    resolve to the name's search, see the notes above). Only videos whose title / URL contains the full name are
    returned (FPO_STRICT_NAME=0 turns that off). Returns (items, total_pages); ([], page) means end of the list."""
    if model_name.startswith("http"):
        extracted = _extract_name_from_fpo_url(model_name)
        if extracted:
            model_name = extracted
        else:
            base = model_name.rstrip("/")
            url = f"{base}?page={page}" if page > 1 else base
            return _scrape_page(url)
    return _search_listing(model_name, page)


def get_random_page_videos() -> list[dict]:
    """Fetch a random page of fpo.xxx videos — used by fpo_live_monitor
    and fpo_uploader_worker's random mode. Rotates through a handful of
    category pages and random page offsets so the same videos don't repeat
    every cycle."""
    category = _random.choice(_BROAD_TERMS_FPO)
    page = _random.randint(1, 15)
    url = f"{DEFAULT_BASE_URL}/categories/{category}/?mode=latest&page={page}"
    try:
        items, _ = _scrape_page(url)
        return items
    except Exception as e:
        logger.warning(f"fpo.xxx random page fetch failed ({url}): {e}")
        return []


def get_stream_url(video_url: str) -> str | None:
    """fpo.xxx does not support direct HLS/m3u8 streaming — the KVS player
    uses progressive MP4 downloads, not adaptive streams. Always returns None.

    Stream buttons are already excluded for fpo links via is_fpo_link() in
    main.py's build_stream_button_markup(). This stub exists purely for
    interface compatibility so main.py can call fpo.get_stream_url(link)
    the same way it calls faphouse_downloader.get_stream_url(link) without
    needing a special-case branch for every backend module."""
    return None


def get_latest_videos() -> list[dict]:
    """Fetch fpo.xxx's newest videos — used by fpo_live_monitor to detect
    and push newly published content. Returns a deduplicated flat list of
    {"slug","url","title"} items.

    FIX: was only trying 2 URLs (front page + one category), both of which
    can independently go quiet/empty (a captcha page, an A/B-tested layout,
    a category that's temporarily light on new uploads) with no fallback.
    Now tries 6 different listing endpoints/params that fpo.xxx (a KVS-based
    site) commonly exposes, merging results across all of them — but stops
    as soon as 20+ items have been found rather than always hitting all 6,
    since that's already enough for one monitor pass and every extra request
    past that point would just be wasted load for no real benefit."""
    merged: dict[str, dict] = {}
    last_snippet = None
    EARLY_STOP_COUNT = 20
    for url in [
        f"{DEFAULT_BASE_URL}/",
        f"{DEFAULT_BASE_URL}/?mode=latest",
        f"{DEFAULT_BASE_URL}/videos/",
        f"{DEFAULT_BASE_URL}/videos/?mode=latest",
        f"{DEFAULT_BASE_URL}/new-videos/",
        f"{DEFAULT_BASE_URL}/categories/amateur/?mode=latest",
    ]:
        if len(merged) >= EARLY_STOP_COUNT:
            break
        try:
            html_text = _fetch_html(url)
            items = _parse_listing_page(html_text, DEFAULT_BASE_URL)
            for it in items:
                merged.setdefault(it["slug"], it)
            if not items:
                # Fetched fine (no exception) but found zero videos — keep
                # a snippet of what actually came back so the real cause
                # (a block/captcha page, a markup change, or genuinely
                # nothing new right now) is visible in the log line below
                # instead of needing a live re-check to find out.
                last_snippet = html_text[:200].replace("\n", " ")
        except Exception as e:
            logger.warning(f"fpo.xxx get_latest_videos failed for {url}: {e}")
    if not merged and last_snippet:
        logger.warning(f"fpo.xxx get_latest_videos: fetched OK but parsed 0 items — snippet: {last_snippet!r}")
    return list(merged.values())

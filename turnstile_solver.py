"""
Client for the bundled cf-bypass service (cf-bypass/ - a Bun server that drives a real Chrome on Xvfb through
rebrowser-puppeteer and solves Cloudflare Turnstile). Used by fpo_downloader to get the Turnstile token that
fpo.xxx's login form demands (cf-turnstile-response / g-recaptcha-response).

How the service gets a token: it opens a page whose origin is the target site (the request for `domain` is answered
with a tiny HTML page holding only the widget for the given sitekey), so Cloudflare issues the token for that
hostname; the widget is ticked with human-like mouse movement. We then POST the login ourselves with that token.

Service API:  POST {CF_BYPASS_URL}/cloudflare  {"mode":"turnstile","domain":"https://www.fpo.xxx","siteKey":"0x4..."}
              -> {"token": "..."}   (errors: {"code": 500, "message": "..."})

Env:
  CF_BYPASS_URL      default http://127.0.0.1:8742   (the service started by entrypoint.sh; or any remote deployment)
  CF_BYPASS_AUTH     optional - sent as "authToken" when the service was started with authToken=...
  CF_BYPASS_ENABLED  0 = never use the service
  CF_BYPASS_TIMEOUT  seconds to wait for one token (default 90)
"""

import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

CF_BYPASS_URL = os.environ.get("CF_BYPASS_URL", "http://127.0.0.1:8742").strip().rstrip("/")
CF_BYPASS_AUTH = os.environ.get("CF_BYPASS_AUTH", "").strip()
CF_BYPASS_TIMEOUT = int(os.environ.get("CF_BYPASS_TIMEOUT", "90"))


def enabled() -> bool:
    return os.environ.get("CF_BYPASS_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")


def is_up(timeout: float = 3.0) -> bool:
    """True when something answers at CF_BYPASS_URL (the service replies 404 JSON to a plain GET - that is 'up')."""
    try:
        requests.get(CF_BYPASS_URL + "/", timeout=timeout)
        return True
    except Exception:
        return False


def wait_until_up(wait: float = 30.0) -> bool:
    """The service starts its Chrome in the background right at boot - give it a moment on a fresh container."""
    t0 = time.time()
    while True:
        if is_up(2.0):
            return True
        if time.time() - t0 >= wait:
            return False
        time.sleep(1.5)


def get_token(domain: str, sitekey: str, timeout: int = 0) -> tuple:
    """-> (token, message). token is '' on failure and message says why. Never raises."""
    timeout = timeout or CF_BYPASS_TIMEOUT
    payload = {"mode": "turnstile", "domain": domain.rstrip("/"), "siteKey": sitekey}
    if CF_BYPASS_AUTH:
        payload["authToken"] = CF_BYPASS_AUTH
    t0 = time.time()
    try:
        r = requests.post(CF_BYPASS_URL + "/cloudflare", json=payload, timeout=timeout + 15)
    except Exception as e:
        return "", f"cf-bypass service unreachable at {CF_BYPASS_URL} ({str(e)[:120]})"
    try:
        j = r.json()
    except Exception:
        return "", f"cf-bypass answered HTTP {r.status_code} with non-JSON"
    token = (j.get("token") or "").strip() if isinstance(j, dict) else ""
    if r.status_code == 200 and len(token) >= 20:
        logger.info(f"[cf-bypass] Turnstile token received in {time.time() - t0:.1f}s ({len(token)} chars)")
        return token, ""
    msg = (j.get("message") if isinstance(j, dict) else "") or f"HTTP {r.status_code}"
    return "", f"cf-bypass: {str(msg)[:160]}"

"""
Self-installing browser for fpo_downloader's username/password login (Turnstile needs a real browser).

After a plain `git pull` + restart nothing else has to be done by hand: at boot this module, in a background
thread, makes sure that
  1. the `playwright` Python package is importable      (pip install playwright, if missing)
  2. a Chromium is available                            (system chromium / chrome, else apt-get install
                                                         chromium xvfb when running as root, else
                                                         `playwright install chromium`)
It never blocks the bot and never raises: if something can't be installed it logs why and the login simply
falls back to FPO_COOKIES (/setcookies) exactly as before. Set BROWSER_BOOTSTRAP=0 to disable.

fpo_downloader calls wait_ready() before a browser login so a first login right after boot waits for the
install instead of failing and backing off.
"""

import logging
import os
import shutil
import subprocess
import sys
import threading

logger = logging.getLogger("faphouse_bot")

_ENABLED = os.environ.get("BROWSER_BOOTSTRAP", "1") != "0"
_ready = threading.Event()
_started = False
_lock = threading.Lock()
_status = {"playwright": False, "chrome": None, "msg": "not started"}


def is_starting() -> bool:
    return _started and not _ready.is_set()


def wait_ready(timeout: float = 240.0) -> bool:
    if not _started:
        return True
    return _ready.wait(timeout)


def status() -> dict:
    return dict(_status)


def _run(cmd, timeout=900):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr)[-400:]
    except Exception as e:
        return False, str(e)


def _have_playwright() -> bool:
    try:
        import importlib
        importlib.invalidate_caches()
        importlib.import_module("playwright.sync_api")
        return True
    except Exception:
        return False


def find_chrome():
    p = os.environ.get("FPO_CHROME_PATH", "")
    if p and os.path.exists(p):
        return p
    for n in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        w = shutil.which(n)
        if w:
            return w
    return None


def _playwright_has_own_chromium() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            return os.path.exists(pw.chromium.executable_path)
    except Exception:
        return False


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _ensure():
    # 1) playwright python package
    if not _have_playwright():
        logger.info("[browser-bootstrap] installing the playwright python package...")
        ok, out = _run([sys.executable, "-m", "pip", "install", "--quiet", "playwright"])
        if not ok:  # PEP 668 ("externally managed") systems
            ok, out = _run([sys.executable, "-m", "pip", "install", "--quiet", "--break-system-packages", "playwright"])
        if not ok or not _have_playwright():
            _status["msg"] = f"playwright pip install failed: {out[-200:]}"
            logger.warning(f"[browser-bootstrap] {_status['msg']}")
            return
    _status["playwright"] = True

    # 2) a Chromium
    chrome = find_chrome()
    if not chrome and _is_root() and shutil.which("apt-get"):
        logger.info("[browser-bootstrap] installing chromium + xvfb with apt-get...")
        _run(["apt-get", "update", "-qq"], 300)
        ok, out = _run(["apt-get", "install", "-y", "-qq", "--no-install-recommends",
                        "chromium", "xvfb", "fonts-liberation"], 900)
        if not ok:  # Ubuntu names it differently / chromium is a snap shim there
            _run(["apt-get", "install", "-y", "-qq", "--no-install-recommends", "xvfb", "fonts-liberation"], 600)
        chrome = find_chrome()
    if not chrome and not _playwright_has_own_chromium():
        logger.info("[browser-bootstrap] running `playwright install chromium`...")
        cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
        ok, out = _run(cmd + (["--with-deps"] if _is_root() else []), 1200)
        if not ok:
            ok, out = _run(cmd, 1200)
        if not ok:
            _status["msg"] = f"chromium install failed: {out[-200:]}"
            logger.warning(f"[browser-bootstrap] {_status['msg']}")
            return
    _status["chrome"] = chrome or "playwright-managed"
    _status["msg"] = "ready"
    logger.info(f"[browser-bootstrap] OK - browser login available ({_status['chrome']})")


def _worker():
    try:
        _ensure()
    except Exception as e:
        _status["msg"] = f"bootstrap error: {e}"
        logger.warning(f"[browser-bootstrap] {_status['msg']}")
    finally:
        _ready.set()


def start_background():
    """Non-blocking. Safe to call more than once."""
    global _started
    if not _ENABLED:
        _status["msg"] = "disabled (BROWSER_BOOTSTRAP=0)"
        return
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_worker, name="browser-bootstrap", daemon=True).start()

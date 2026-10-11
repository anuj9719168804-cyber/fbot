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
_status = {"playwright": False, "patchright": False, "chrome": None, "msg": "not started"}


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


def is_snap_shim(path: str) -> bool:
    """Ubuntu's `chromium` / `chromium-browser` apt package is only a stub that launches a *snap*. Snap-confined
    Chromium cannot create its profile under /tmp ("Failed to create socket directory"), so it dies at once and
    cf-bypass / the browser login only ever see `ECONNREFUSED 127.0.0.1:<port>`. Never use it."""
    try:
        real = os.path.realpath(path)
        if "/snap/" in real or os.path.basename(real) == "snap":
            return True
        if os.path.getsize(real) < 8192:                       # a tiny wrapper script, not the real binary
            with open(real, "rb") as f:
                head = f.read(8192).lower()
            # specific markers only: Debian's genuine /usr/bin/chromium is ALSO a small wrapper script
            return any(m in head for m in (b"/snap/bin", b"snap install", b"snap run", b"snapd"))
    except Exception:
        pass
    return False


def find_chrome():
    """A REAL Chrome/Chromium (never the Ubuntu snap shim). Google Chrome first."""
    p = os.environ.get("FPO_CHROME_PATH", "")
    if p and os.path.exists(p) and not is_snap_shim(p):
        return p
    for n in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser", "chrome"):
        w = shutil.which(n)
        if w and not is_snap_shim(w):
            return w
    for p in ("/opt/google/chrome/chrome", "/usr/bin/google-chrome-stable"):
        if os.path.exists(p):
            return p
    return None


def _apt(*pkgs, timeout=900):
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    try:
        r = subprocess.run(["apt-get", "install", "-y", "-qq", "--no-install-recommends", *pkgs],
                           env=env, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, ((r.stdout or "") + (r.stderr or ""))[-300:]
    except Exception as e:
        return False, str(e)[:300]


def _install_google_chrome() -> bool:
    """x86_64 Debian/Ubuntu: Google's own .deb (the apt `chromium` package is a snap shim on Ubuntu)."""
    import platform
    if platform.machine().lower() not in ("x86_64", "amd64"):
        return False
    deb = "/tmp/google-chrome-stable_current_amd64.deb"
    url = "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb"
    logger.info("[browser-bootstrap] installing Google Chrome (one time, ~110 MB)...")
    ok = False
    if shutil.which("wget"):
        ok, _ = _run(["wget", "-q", "-O", deb, url], 600)
    if not ok and shutil.which("curl"):
        ok, _ = _run(["curl", "-fsSL", "-o", deb, url], 600)
    if not ok:
        return False
    ok, out = _apt(deb)
    if not ok:
        logger.warning(f"[browser-bootstrap] Google Chrome install failed: {out}")
    try:
        os.remove(deb)
    except OSError:
        pass
    return ok


def _ensure_system_deps():
    """Everything the bot shells out to, installed with apt when we are root (a plain `git pull` + restart is enough):
    aria2 (multi-connection downloads - without it yt-dlp falls back to ~70 KiB/s single connection), ffmpeg
    (HLS -> mp4 for the JAV sites and merging), Xvfb (virtual screen for the headed Chrome), and a real Chrome."""
    if not (_is_root() and shutil.which("apt-get")):
        return
    missing = [pkg for pkg, exe in (("aria2", "aria2c"), ("ffmpeg", "ffmpeg"), ("xvfb", "Xvfb"),
                                    ("curl", "curl"), ("wget", "wget")) if not shutil.which(exe)]
    if missing:
        logger.info(f"[browser-bootstrap] installing missing system packages with apt: {', '.join(missing)}")
        _run(["apt-get", "update", "-qq"], 300)
        ok, out = _apt(*missing, "fonts-liberation")
        if not ok:
            logger.warning(f"[browser-bootstrap] apt install {missing} failed: {out}")
    if not find_chrome():
        if not _install_google_chrome():
            _run(["apt-get", "update", "-qq"], 300)
            _apt("chromium", "fonts-liberation")   # Debian: real package; Ubuntu: snap shim (ignored by find_chrome)
    c = find_chrome()
    if c:
        os.environ.setdefault("CHROME_PATH", c)
        os.environ.setdefault("CHROME_BIN", c)
    else:
        logger.warning("[browser-bootstrap] no usable (non-snap) Chrome could be installed - see the lines above.")


def _playwright_has_own_chromium() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            return os.path.exists(pw.chromium.executable_path)
    except Exception:
        return False


def _have_patchright() -> bool:
    try:
        import importlib
        importlib.invalidate_caches()
        importlib.import_module("patchright.sync_api")
        return True
    except Exception:
        return False


_patch_lock = threading.Lock()


def ensure_patchright() -> bool:
    """Makes sure `patchright` (Playwright fork the FPO login prefers) is importable, installing it with pip on the
    server if missing - so a plain `git pull` + restart is enough. Tries the normal install, then PEP-668 and
    --user variants. Safe to call repeatedly / from several threads. Never raises. -> True if importable."""
    with _patch_lock:
        if _have_patchright():
            _status["patchright"] = True
            return True
        logger.info("[browser-bootstrap] patchright missing - installing with pip...")
        last = ""
        for extra in ([], ["--break-system-packages"], ["--user", "--break-system-packages"]):
            ok, out = _run([sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", "patchright"] + extra, 600)
            last = out
            if ok and _have_patchright():
                _status["patchright"] = True
                logger.info("[browser-bootstrap] patchright installed OK")
                return True
        _status["patchright"] = False
        logger.warning(f"[browser-bootstrap] patchright install failed - FPO login falls back to plain playwright. pip said: {last[-250:]}")
        return False


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _ensure():
    _ensure_system_deps()
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
    ensure_patchright()

    # 2) a Chromium
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

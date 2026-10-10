"""
Starts the bundled cf-bypass service (cf-bypass/ -> Turnstile token for fpo.xxx's login, see turnstile_solver.py)
on hosts that do NOT run the Dockerfile/entrypoint.sh - a plain VPS (/root/fbot ...), Render native, etc. Docker
deployments already start it from entrypoint.sh; there this module just sees it listening and does nothing.

start_background() (non-blocking, runs in a thread), in order:
  1. something already answers on CF_BYPASS_URL (entrypoint.sh / your own deployment)        -> use it
  2. CF_BYPASS_URL points to another machine                                                -> idle (nothing to start)
  3. find Bun (PATH, ~/.bun/bin); if missing install it (official installer, else `npm i -g bun`)
  4. `bun install` inside cf-bypass/ (first run only)
  5. make sure a Chromium + Xvfb exist (same helpers FlareSolverr / the fpo browser login use)
  6. run `bun src/index.ts` (PORT=8742, CHROME_PATH set), wait for the port, then watch it and restart if it dies.
Log: cf-bypass/cf-bypass.log.   Env: CF_BYPASS_AUTO=0 disables all of this; CF_BYPASS_PORT (default 8742).
"""

import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from urllib.parse import urlparse

logger = logging.getLogger("faphouse_bot")

_HERE = os.path.dirname(os.path.abspath(__file__))
_DIR = os.path.join(_HERE, "cf-bypass")
_LOG = os.path.join(_DIR, "cf-bypass.log")
_PORT = int(os.environ.get("CF_BYPASS_PORT", "8742"))

_ready = threading.Event()
_state = "idle"          # idle | installing | starting | ready | failed | disabled
_proc = None
_msg = "not started"


def is_ready() -> bool:
    return _ready.is_set()


def is_starting() -> bool:
    return _state in ("installing", "starting")


def status() -> str:
    return f"{_state}: {_msg}"


def wait_ready(timeout: float = 240.0) -> bool:
    """Block while an install/start is in progress. True when the service is up."""
    if _ready.is_set():
        return True
    if not is_starting():
        return False
    return _ready.wait(timeout)


def _port_open(port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _run(cmd, cwd=None, timeout=900, env=None):
    try:
        r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, ((r.stdout or "") + (r.stderr or ""))[-400:]
    except Exception as e:
        return False, str(e)[:300]


def _find_bun():
    w = shutil.which("bun")
    if w:
        return w
    for p in (os.path.expanduser("~/.bun/bin/bun"), "/root/.bun/bin/bun", "/usr/local/bin/bun"):
        if os.path.exists(p):
            return p
    return None


def _install_bun():
    global _msg
    _msg = "installing Bun"
    logger.info("[cf-bypass] Bun not found - installing it (one time)...")
    if shutil.which("curl") and shutil.which("bash"):
        ok, out = _run(["bash", "-c", "curl -fsSL https://bun.sh/install | bash"], timeout=300)
        if ok and _find_bun():
            return _find_bun()
        logger.info(f"[cf-bypass] official Bun installer failed: {out[-160:]}")
    if shutil.which("npm"):
        ok, out = _run(["npm", "install", "-g", "bun"], timeout=300)
        if ok and _find_bun():
            return _find_bun()
        logger.info(f"[cf-bypass] npm install -g bun failed: {out[-160:]}")
    return None


def _chrome_path():
    try:
        import browser_bootstrap
        c = browser_bootstrap.find_chrome()
        if c:
            return c
        if browser_bootstrap.is_starting():
            browser_bootstrap.wait_ready(240)          # it apt-installs chromium + xvfb when we are root
            return browser_bootstrap.find_chrome()
    except Exception:
        pass
    for n in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        w = shutil.which(n)
        if w:
            return w
    return None


def _launch(bun: str, chrome: str) -> bool:
    global _proc, _state, _msg
    _state, _msg = "starting", "launching service"
    env = os.environ.copy()
    env["PORT"] = str(_PORT)
    env["CHROME_PATH"] = chrome
    env.setdefault("DISPLAY", ":99")                    # fallback display if the service's own Xvfb can't start
    env["PATH"] = os.path.dirname(bun) + os.pathsep + env.get("PATH", "")
    try:
        logf = open(_LOG, "ab")
        _proc = subprocess.Popen([bun, "src/index.ts"], cwd=_DIR, env=env, stdout=logf, stderr=subprocess.STDOUT)
    except Exception as e:
        _msg = f"failed to start: {e}"
        logger.warning(f"[cf-bypass] {_msg}")
        return False
    for _ in range(60):
        if _port_open(_PORT):
            return True
        if _proc.poll() is not None:
            tail = ""
            try:
                with open(_LOG, "rb") as f:
                    tail = f.read()[-500:].decode("utf-8", "replace")
            except Exception:
                pass
            _msg = f"exited early (code {_proc.returncode})"
            logger.warning(f"[cf-bypass] process {_msg}. Last log:\n{tail}")
            return False
        time.sleep(1)
    _msg = "did not open its port within 60s"
    logger.warning(f"[cf-bypass] {_msg}")
    return False


def _setup_and_start():
    global _state, _msg
    if os.environ.get("CF_BYPASS_AUTO", "1").strip().lower() in ("0", "false", "no", "off"):
        _state, _msg = "disabled", "CF_BYPASS_AUTO=0"
        return
    try:
        import turnstile_solver as ts
        host = urlparse(ts.CF_BYPASS_URL).hostname or ""
        if not ts.enabled():
            _state, _msg = "disabled", "CF_BYPASS_ENABLED=0"
            return
        if host not in ("127.0.0.1", "localhost", "::1"):
            _state, _msg = "disabled", f"CF_BYPASS_URL points to {host} - nothing to start locally"
            logger.info(f"[cf-bypass] {_msg}")
            return
    except Exception:
        pass
    if _port_open(_PORT):                                # entrypoint.sh (Docker) or a manual start already runs it
        _state, _msg = "ready", "already listening"
        _ready.set()
        logger.info(f"[cf-bypass] already running on :{_PORT}")
        return
    if not os.path.isdir(_DIR):
        _state, _msg = "failed", "cf-bypass/ folder is missing next to main.py"
        logger.warning(f"[cf-bypass] {_msg}")
        return

    _state, _msg = "installing", "checking Bun / dependencies"
    bun = _find_bun() or _install_bun()
    if not bun:
        _state, _msg = "failed", "Bun is not installed and could not be auto-installed (curl -fsSL https://bun.sh/install | bash)"
        logger.warning(f"[cf-bypass] {_msg}")
        return
    if not os.path.isdir(os.path.join(_DIR, "node_modules")):
        _msg = "bun install"
        logger.info("[cf-bypass] installing dependencies (bun install, one time)...")
        ok, out = _run([bun, "install"], cwd=_DIR, timeout=600)
        if not ok:
            _state, _msg = "failed", f"bun install failed: {out[-200:]}"
            logger.warning(f"[cf-bypass] {_msg}")
            return
    try:
        import flaresolver_bootstrap as fb
        fb._ensure_xvfb()                                # Xvfb for the headed Chrome (apt when root)
    except Exception:
        pass
    chrome = _chrome_path()
    if not chrome:
        _state, _msg = "failed", "no Chromium/Chrome found (apt install chromium)"
        logger.warning(f"[cf-bypass] {_msg}")
        return

    restarts = 0
    while restarts <= 5:
        if _launch(bun, chrome):
            _state, _msg = "ready", f"up on :{_PORT}"
            _ready.set()
            logger.info(f"[cf-bypass] ✅ Turnstile token service is up on :{_PORT}")
            while _proc is not None and _proc.poll() is None:
                time.sleep(5)
            _ready.clear()
            logger.warning("[cf-bypass] process died - restarting")
        restarts += 1
        _state = "starting"
        time.sleep(10)
    _state, _msg = "failed", "gave up after 5 restarts - see cf-bypass/cf-bypass.log"
    logger.warning(f"[cf-bypass] {_msg}")


def start_background():
    """Call once at bot startup (main.py). Non-blocking."""
    threading.Thread(target=_setup_and_start, name="cfbypass-setup", daemon=True).start()

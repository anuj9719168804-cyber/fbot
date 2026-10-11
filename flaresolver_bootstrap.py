"""
Gets FlareSolverr (https://github.com/FlareSolverr/FlareSolverr) running by itself, on any host --
a VPS, Render (Docker) or a plain machine -- so cf_bypass.py can solve Cloudflare challenges
(SpankBang etc.) with ZERO manual setup. cf_bypass.py talks to http://localhost:8191/v1.

What start_background() does, in order (all in a background thread, never blocks the bot):
  1. Something already listens on :8191 (your own docker run / earlier start)  -> use it.
  2. Docker-image install at /opt/flaresolverr (source + venv, see Dockerfile)   -> start it.
  3. Official standalone release (Chrome bundled, no Docker / no pip needed):
       download flaresolverr_linux_x64.tar.gz once, unpack, start it.
     Install dir: $FLARESOLVERR_HOME, else /opt/flaresolverr-bin, else <bot folder>/.flaresolverr.
     If we are root on Debian/Ubuntu and Xvfb is missing, it is apt-installed.
  A watchdog restarts FlareSolverr if it dies. Logs: <install dir>/flaresolverr.log.

Env: FLARESOLVERR_AUTO=0 (disable all of this) | FLARESOLVERR_VERSION (default v3.5.2) |
     FLARESOLVERR_HOME | FLARESOLVERR_URL (cf_bypass: use a FlareSolverr that runs elsewhere -> this module stays idle).

Needs ~1 GB free disk for the first install and a few hundred MB of RAM while a challenge is being solved.
cf_bypass.try_solve() calls wait_ready() so a job that arrives while the install is still running waits for it
instead of failing. Every step is wrapped: on failure is_ready() stays False and cf_bypass falls back to a plain 403.
"""

import logging
import os
import platform
import shutil
import socket
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request

logger = logging.getLogger("faphouse_bot")

FS_VERSION = os.environ.get("FLARESOLVERR_VERSION", "v3.5.2").strip() or "v3.5.2"
FS_TARBALL_URL = f"https://github.com/FlareSolverr/FlareSolverr/releases/download/{FS_VERSION}/flaresolverr_linux_x64.tar.gz"

# (2) Docker-image layout (source checkout + its own venv), built by the Dockerfile
FLARESOLVERR_HOME = "/opt/flaresolverr"
FLARESOLVERR_VENV_PY = os.path.join(FLARESOLVERR_HOME, "venv", "bin", "python")
FLARESOLVERR_ENTRY = os.path.join(FLARESOLVERR_HOME, "src", "flaresolverr.py")

FLARESOLVERR_PORT = 8191
XVFB_DISPLAY = ":99"

_ready = threading.Event()
_state = "idle"  # idle | installing | starting | ready | failed | disabled
_xvfb_process: subprocess.Popen | None = None
_fs_process: subprocess.Popen | None = None
_log_path: str | None = None


def is_ready() -> bool:
    """True once FlareSolverr accepts connections on 127.0.0.1:8191."""
    return _ready.is_set()


def is_starting() -> bool:
    return _state in ("installing", "starting")


def wait_ready(timeout: float = 120.0) -> bool:
    """Block (up to `timeout` s) while an install/start is in progress. True if FlareSolverr is up."""
    if _ready.is_set():
        return True
    if not is_starting():
        return False
    return _ready.wait(timeout)


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


# ---------------------------------------------------------------------------
# install of the standalone release
# ---------------------------------------------------------------------------
def _install_dir() -> str | None:
    cands = []
    if os.environ.get("FLARESOLVERR_HOME"):
        cands.append(os.environ["FLARESOLVERR_HOME"])
    cands += ["/opt/flaresolverr-bin", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".flaresolverr"),
              os.path.join(tempfile.gettempdir(), "flaresolverr-bin")]
    for d in cands:
        try:
            os.makedirs(d, exist_ok=True)
            if os.access(d, os.W_OK):
                return d
        except OSError:
            continue
    return None


def _ensure_xvfb():
    """Chrome needs *some* display. Install Xvfb with apt when we are allowed to (root on Debian/Ubuntu)."""
    if shutil.which("Xvfb"):
        return
    if not (_is_root() and shutil.which("apt-get")):
        logger.warning("[flaresolverr] Xvfb is missing and can't be auto-installed here (not root / no apt) -- "
                       "install it (apt install xvfb) if FlareSolverr fails to start.")
        return
    logger.info("[flaresolverr] installing Xvfb (apt-get)...")
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    try:
        subprocess.run(["apt-get", "update", "-qq"], env=env, timeout=180, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["apt-get", "install", "-y", "-qq", "--no-install-recommends", "xvfb", "fonts-liberation"],
                       env=env, timeout=300, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        logger.warning(f"[flaresolverr] apt install xvfb failed: {e}")


def _download(url: str, dest: str):
    logger.info(f"[flaresolverr] downloading {url} (~260 MB, one time)...")
    req = urllib.request.Request(url, headers={"User-Agent": "fbot-flaresolverr-installer"})
    last_log, done = time.time(), 0
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:  # 60s socket stall timeout
        total = int(r.headers.get("Content-Length") or 0)
        while True:
            chunk = r.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if time.time() - last_log > 20:
                last_log = time.time()
                logger.info(f"[flaresolverr] download {done // 1048576} / {total // 1048576 or '?'} MB")
    if total and done < total:
        raise RuntimeError(f"download cut short ({done} of {total} bytes)")


def _install_release(base: str) -> str | None:
    """Download + unpack the standalone release into base/. Returns the flaresolverr binary path, or None."""
    binary = os.path.join(base, "flaresolverr", "flaresolverr")
    marker = os.path.join(base, "VERSION")
    try:
        if os.path.exists(binary) and open(marker).read().strip() == FS_VERSION:
            return binary
    except OSError:
        pass
    if platform.machine().lower() not in ("x86_64", "amd64"):
        logger.warning(f"[flaresolverr] no standalone build for {platform.machine()} -- run FlareSolverr via Docker "
                       "(docker run -d -p 8191:8191 ghcr.io/flaresolverr/flaresolverr) or set FLARESOLVERR_URL.")
        return None
    if shutil.disk_usage(base).free < 1_300_000_000:
        logger.warning("[flaresolverr] less than ~1.3 GB free disk in %s -- not enough to install FlareSolverr.", base)
        return None
    tgz = os.path.join(base, "flaresolverr.tar.gz.part")
    staging = os.path.join(base, "_staging")
    try:
        shutil.rmtree(staging, ignore_errors=True)
        _download(FS_TARBALL_URL, tgz)
        logger.info("[flaresolverr] unpacking...")
        os.makedirs(staging)
        with tarfile.open(tgz, "r:gz") as t:
            try:
                t.extractall(staging, filter="data")
            except TypeError:  # Python < 3.12 has no extraction filters
                t.extractall(staging)
        os.remove(tgz)
        new_bin = os.path.join(staging, "flaresolverr", "flaresolverr")
        if not os.path.exists(new_bin):
            raise RuntimeError("archive layout unexpected (flaresolverr/flaresolverr not found)")
        os.chmod(new_bin, 0o755)
        chrome = os.path.join(staging, "flaresolverr", "_internal", "chrome", "chrome")
        if os.path.exists(chrome):
            os.chmod(chrome, 0o755)
        shutil.rmtree(os.path.join(base, "flaresolverr"), ignore_errors=True)
        os.replace(os.path.join(staging, "flaresolverr"), os.path.join(base, "flaresolverr"))
        shutil.rmtree(staging, ignore_errors=True)
        with open(marker, "w") as f:
            f.write(FS_VERSION)
        logger.info(f"[flaresolverr] installed {FS_VERSION} into {base}")
        return binary
    except Exception as e:
        logger.warning(f"[flaresolverr] install failed: {e}")
        for p in (tgz,):
            try:
                os.remove(p)
            except OSError:
                pass
        shutil.rmtree(staging, ignore_errors=True)
        return None


# ---------------------------------------------------------------------------
# start / watch
# ---------------------------------------------------------------------------
def _start_xvfb():
    global _xvfb_process
    if _xvfb_process is not None and _xvfb_process.poll() is None:
        return
    if not shutil.which("Xvfb"):
        return
    try:
        _xvfb_process = subprocess.Popen(
            ["Xvfb", XVFB_DISPLAY, "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(1)
    except Exception as e:
        logger.warning(f"[flaresolverr] Xvfb failed to start: {e}")


def _launch(cmd: list, cwd: str, extra_env: dict | None = None) -> bool:
    """Start FlareSolverr and wait (<=90 s) for the port. True when it is up."""
    global _fs_process, _state
    _state = "starting"
    _start_xvfb()
    env = os.environ.copy()
    env["DISPLAY"] = XVFB_DISPLAY
    env.setdefault("HOST", "127.0.0.1")
    env.setdefault("PORT", str(FLARESOLVERR_PORT))
    env.setdefault("LOG_LEVEL", "info")
    env.update(extra_env or {})
    logf = open(_log_path, "ab") if _log_path else subprocess.DEVNULL
    logger.info(f"[flaresolverr] starting on 127.0.0.1:{FLARESOLVERR_PORT}...")
    try:
        _fs_process = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=logf, stderr=subprocess.STDOUT)
    except Exception as e:
        logger.warning(f"[flaresolverr] failed to start process: {e}")
        return False
    for _ in range(90):
        if _port_open("127.0.0.1", FLARESOLVERR_PORT):
            return True
        if _fs_process.poll() is not None:
            tail = ""
            try:
                with open(_log_path, "rb") as f:
                    tail = f.read()[-600:].decode("utf-8", "replace")
            except Exception:
                pass
            logger.warning(f"[flaresolverr] process exited early (code {_fs_process.returncode}). Last log:\n{tail}")
            return False
        time.sleep(1)
    logger.warning("[flaresolverr] didn't come up within 90s.")
    return False


def _setup_and_start():
    global _state, _log_path
    if os.environ.get("FLARESOLVERR_AUTO", "1").strip().lower() in ("0", "false", "no", "off"):
        _state = "disabled"
        return
    if os.environ.get("FLARESOLVERR_URL") and "localhost" not in os.environ["FLARESOLVERR_URL"] \
            and "127.0.0.1" not in os.environ["FLARESOLVERR_URL"]:
        _state = "disabled"  # user points cf_bypass at a remote FlareSolverr
        logger.info("[flaresolverr] FLARESOLVERR_URL points elsewhere -- not starting a local one.")
        return

    _state = "installing"
    if _port_open("127.0.0.1", FLARESOLVERR_PORT):
        logger.info(f"[flaresolverr] something's already listening on :{FLARESOLVERR_PORT}, assuming it's ready.")
        _state = "ready"
        _ready.set()
        return

    def _cmd_for_layout():
        """(cmd, cwd, env) for whichever install exists / can be made, else None."""
        global _log_path
        # (2) Docker-image layout
        if os.path.exists(FLARESOLVERR_VENV_PY) and os.path.exists(FLARESOLVERR_ENTRY):
            _log_path = os.path.join(tempfile.gettempdir(), "flaresolverr.log")
            return ([FLARESOLVERR_VENV_PY, FLARESOLVERR_ENTRY], os.path.join(FLARESOLVERR_HOME, "src"),
                    {"BROWSER_EXECUTABLE_PATH": os.environ.get("BROWSER_EXECUTABLE_PATH", "/usr/bin/chromium")})
        # (3) standalone release, auto-installed
        base = _install_dir()
        if not base:
            logger.warning("[flaresolverr] no writable folder to install into -- set FLARESOLVERR_HOME.")
            return None
        _log_path = os.path.join(base, "flaresolverr.log")
        _ensure_xvfb()
        binary = _install_release(base)
        if not binary:
            return None
        return ([binary], os.path.dirname(binary), {})

    spec = None
    try:
        spec = _cmd_for_layout()
    except Exception as e:
        logger.warning(f"[flaresolverr] setup failed: {e}")
    if not spec:
        _state = "failed"
        return

    cmd, cwd, extra = spec
    restarts = 0
    while True:
        if _launch(cmd, cwd, extra):
            _state = "ready"
            _ready.set()
            logger.info("[flaresolverr] ✅ up — Cloudflare-protected links can now be bypassed automatically.")
            # watchdog: restart if it dies later
            while _fs_process is not None and _fs_process.poll() is None:
                time.sleep(5)
            _ready.clear()
            logger.warning("[flaresolverr] process died.")
        restarts += 1
        if restarts > 5:
            _state = "failed"
            logger.warning("[flaresolverr] giving up after 5 restarts -- see the log file above.")
            return
        _state = "starting"
        time.sleep(10)


def start_background():
    """Call once at bot startup (main.py). Non-blocking."""
    threading.Thread(target=_setup_and_start, name="flaresolverr-setup", daemon=True).start()

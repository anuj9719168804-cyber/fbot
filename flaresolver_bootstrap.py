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
# missing shared libraries of the bundled Chrome ("error while loading shared libraries: libwayland-server.so.0")
# ---------------------------------------------------------------------------
# shared-library name -> Debian/Ubuntu package ("a|b" = try a, then b: newer Ubuntu renamed many to *t64)
_LIB_PKGS = {
    "libwayland-server.so.0": "libwayland-server0", "libwayland-client.so.0": "libwayland-client0",
    "libwayland-egl.so.1": "libwayland-egl1", "libwayland-cursor.so.0": "libwayland-cursor0",
    "libgbm.so.1": "libgbm1", "libdrm.so.2": "libdrm2", "libxkbcommon.so.0": "libxkbcommon0",
    "libnss3.so": "libnss3", "libnssutil3.so": "libnss3", "libsmime3.so": "libnss3", "libnspr4.so": "libnspr4",
    "libatk-1.0.so.0": "libatk1.0-0t64|libatk1.0-0", "libatk-bridge-2.0.so.0": "libatk-bridge2.0-0t64|libatk-bridge2.0-0",
    "libatspi.so.0": "libatspi2.0-0t64|libatspi2.0-0", "libcups.so.2": "libcups2t64|libcups2",
    "libasound.so.2": "libasound2t64|libasound2", "libgtk-3.so.0": "libgtk-3-0t64|libgtk-3-0",
    "libglib-2.0.so.0": "libglib2.0-0t64|libglib2.0-0", "libgobject-2.0.so.0": "libglib2.0-0t64|libglib2.0-0",
    "libgio-2.0.so.0": "libglib2.0-0t64|libglib2.0-0", "libdbus-1.so.3": "libdbus-1-3", "libexpat.so.1": "libexpat1",
    "libpango-1.0.so.0": "libpango-1.0-0", "libpangocairo-1.0.so.0": "libpango-1.0-0", "libcairo.so.2": "libcairo2",
    "libX11.so.6": "libx11-6", "libX11-xcb.so.1": "libx11-xcb1", "libxcb.so.1": "libxcb1", "libXext.so.6": "libxext6",
    "libXcomposite.so.1": "libxcomposite1", "libXdamage.so.1": "libxdamage1", "libXfixes.so.3": "libxfixes3",
    "libXrandr.so.2": "libxrandr2", "libXi.so.6": "libxi6", "libXtst.so.6": "libxtst6", "libXrender.so.1": "libxrender1",
    "libxshmfence.so.1": "libxshmfence1", "libfontconfig.so.1": "libfontconfig1", "libfreetype.so.6": "libfreetype6",
}
_bundled_chrome: str = ""


_extra_lib_dirs: list = []   # user-space lib dirs (unpacked .debs) for hosts where apt-get install isn't allowed


def _lib_env(env: dict | None = None) -> dict:
    env = dict(env if env is not None else os.environ)
    if _extra_lib_dirs:
        env["LD_LIBRARY_PATH"] = ":".join(_extra_lib_dirs + ([env["LD_LIBRARY_PATH"]] if env.get("LD_LIBRARY_PATH") else []))
    return env


def _register_userland_dir(base: str) -> None:
    root = os.path.join(base, "syslibs")
    for sub in ("usr/lib/x86_64-linux-gnu", "lib/x86_64-linux-gnu", "usr/lib"):
        d = os.path.join(root, sub)
        if os.path.isdir(d) and d not in _extra_lib_dirs:
            _extra_lib_dirs.append(d)


def _os_release() -> dict:
    d = {}
    try:
        with open("/etc/os-release") as f:
            for ln in f:
                if "=" in ln:
                    k, v = ln.strip().split("=", 1)
                    d[k] = v.strip('"')
    except OSError:
        pass
    return d


def _mirror_indexes() -> list:
    """[(mirror base url, 'dists/<pocket>/<component>')] to search for a package, newest pocket first."""
    osr = _os_release()
    ids = f"{osr.get('ID', '')} {osr.get('ID_LIKE', '')}".lower()
    if "ubuntu" in ids:
        base, code = "http://archive.ubuntu.com/ubuntu", osr.get("UBUNTU_CODENAME") or osr.get("VERSION_CODENAME", "")
        pockets = [f"{code}-updates", f"{code}-security", code]
        comps = ("main", "universe")
    elif "debian" in ids:
        base, code = "http://deb.debian.org/debian", osr.get("DEBIAN_CODENAME") or osr.get("VERSION_CODENAME", "")
        pockets = [f"{code}-updates", code]
        comps = ("main",)
    else:
        return []
    if not code:
        return []
    return [(base, f"dists/{pk}/{c}") for pk in pockets for c in comps]


def _mirror_download_deb(pkg: str, dest_dir: str) -> bool:
    """No apt lists / no apt at all: read the distro mirror's Packages index (plain HTTP), find `pkg`, download its .deb."""
    import gzip
    import lzma
    import re
    for base, comp in _mirror_indexes():
        text = None
        for ext, opener in ((".xz", lzma.decompress), (".gz", gzip.decompress)):
            try:
                with urllib.request.urlopen(f"{base}/{comp}/binary-amd64/Packages{ext}", timeout=60) as r:
                    text = opener(r.read()).decode("utf-8", "replace")
                break
            except Exception:
                continue
        if not text:
            continue
        for para in text.split("\n\n"):
            if f"Package: {pkg}\n" not in para and not para.startswith(f"Package: {pkg}\n"):
                continue
            name = re.search(r"^Package: (.+)$", para, re.M)
            fn = re.search(r"^Filename: (.+)$", para, re.M)
            if not name or name.group(1).strip() != pkg or not fn:
                continue
            url = f"{base}/{fn.group(1).strip()}"
            out = os.path.join(dest_dir, f"{pkg}_mirror_amd64.deb")
            try:
                with urllib.request.urlopen(url, timeout=120) as r, open(out, "wb") as f:
                    shutil.copyfileobj(r, f)
                logger.info(f"[flaresolverr] downloaded {pkg} from the {base.split('//')[1].split('/')[0]} mirror")
                return True
            except Exception as e:
                logger.info(f"[flaresolverr] mirror download of {pkg} failed: {e}")
                return False
    return False


def _userland_install(base: str, pkgs: list) -> None:
    """No root / sudo (or apt can't install): get the .deb files WITHOUT touching the system -- `apt-get download` when
    apt works for normal users, else straight from the distro mirror -- unpack them with dpkg-deb into <base>/syslibs and
    point LD_LIBRARY_PATH there."""
    if not shutil.which("dpkg-deb"):
        return
    dl = os.path.join(base, "_debs")
    os.makedirs(dl, exist_ok=True)

    def _have(pkg):
        return any(n.startswith(pkg + "_") and n.endswith(".deb") for n in os.listdir(dl))

    for entry in dict.fromkeys(pkgs):
        for pkg in entry.split("|"):
            if _have(pkg):
                break
            if shutil.which("apt-get"):
                try:
                    r = subprocess.run(["apt-get", "download", pkg], cwd=dl, capture_output=True, text=True, timeout=120)
                    if r.returncode != 0:
                        logger.info(f"[flaresolverr] apt-get download {pkg} failed: {(r.stderr or r.stdout)[-160:].strip()}")
                except Exception as e:
                    logger.info(f"[flaresolverr] apt-get download {pkg} failed: {e}")
            if not _have(pkg):
                _mirror_download_deb(pkg, dl)
            if _have(pkg):
                break
    dest = os.path.join(base, "syslibs")
    os.makedirs(dest, exist_ok=True)
    for name in os.listdir(dl):
        if name.endswith(".deb"):
            try:
                subprocess.run(["dpkg-deb", "-x", os.path.join(dl, name), dest], capture_output=True, timeout=60)
            except Exception as e:
                logger.info(f"[flaresolverr] unpacking {name} failed: {e}")
    _register_userland_dir(base)


def _missing_libs(binary: str) -> list:
    try:
        out = subprocess.run(["ldd", binary], capture_output=True, text=True, timeout=30, env=_lib_env()).stdout
    except Exception:
        return []
    return sorted({ln.split("=>")[0].strip() for ln in out.splitlines() if "not found" in ln})


def _apt_prefix():
    if _is_root():
        return []
    if shutil.which("sudo"):
        try:  # passwordless sudo only -- never hang on a password prompt
            if subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=10).returncode == 0:
                return ["sudo", "-n"]
        except Exception:
            pass
    return None


def _ensure_chrome_libs(binary: str) -> bool:
    """FlareSolverr's bundled Chrome needs system libraries the host may not have (seen: libwayland-server.so.0 on a
    minimal Ubuntu). Find them with ldd and apt-install the matching packages (root / passwordless sudo). True when
    nothing is missing afterwards."""
    if not binary or not os.path.exists(binary) or not shutil.which("ldd"):
        return True
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(binary))))  # <base>/flaresolverr/_internal/chrome/chrome
    _register_userland_dir(base)      # libraries unpacked by an earlier run
    missing = _missing_libs(binary)
    if not missing:
        return True
    logger.warning(f"[flaresolverr] Chrome is missing system libraries: {', '.join(missing)}")
    pkgs, unknown = [], []
    for lib in missing:
        pkg = _LIB_PKGS.get(lib)
        (pkgs if pkg else unknown).append(pkg or lib)
    prefix = _apt_prefix()
    if prefix is None or not shutil.which("apt-get"):
        logger.warning("[flaresolverr] no root/sudo -> fetching the packages into the bot folder instead "
                       "(apt-get download or the distro mirror + dpkg-deb, no system change)...")
        _userland_install(base, pkgs)
        left = _missing_libs(binary)
        if not left:
            logger.info("[flaresolverr] Chrome's system libraries are complete now (user-space copy).")
            return True
        logger.warning(f"[flaresolverr] still missing: {', '.join(left)}. Run once on the host: "
                       f"sudo apt-get install -y {' '.join(p.split('|')[-1] for p in pkgs) or '<the libraries above>'}")
        return False
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    try:
        subprocess.run(prefix + ["apt-get", "update", "-qq"], env=env, timeout=180, capture_output=True)
    except Exception as e:
        logger.warning(f"[flaresolverr] apt-get update failed: {e}")
    for entry in dict.fromkeys(pkgs):
        for pkg in entry.split("|"):
            try:
                r = subprocess.run(prefix + ["apt-get", "install", "-y", "-qq", "--no-install-recommends", pkg],
                                   env=env, timeout=300, capture_output=True, text=True)
                if r.returncode == 0:
                    logger.info(f"[flaresolverr] installed {pkg}")
                    break
            except Exception as e:
                logger.warning(f"[flaresolverr] apt install {pkg} failed: {e}")
    left = _missing_libs(binary)
    if left:
        logger.info("[flaresolverr] apt didn't cover everything -- trying the user-space route for the rest...")
        _userland_install(base, pkgs)
        left = _missing_libs(binary)
        if not left:
            logger.info("[flaresolverr] Chrome's system libraries are complete now.")
            return True
    if left:
        logger.warning(f"[flaresolverr] still missing after apt: {', '.join(left)}"
                       + (f" (no known package for: {', '.join(unknown)})" if unknown else ""))
        return False
    logger.info("[flaresolverr] Chrome's system libraries are complete now.")
    return True


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
    env = _lib_env(os.environ.copy())
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
        global _bundled_chrome
        _bundled_chrome = os.path.join(os.path.dirname(binary), "_internal", "chrome", "chrome")
        _ensure_chrome_libs(_bundled_chrome)
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
        if restarts and _bundled_chrome:
            _ensure_chrome_libs(_bundled_chrome)  # a restart after an early exit: libraries may be what is missing
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

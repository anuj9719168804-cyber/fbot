"""Disk-space guard + parallel-download cap.

Why: with several sites auto-uploading at once, 600-800 MB downloads (plus HLS temp segments and split parts)
pile up and the disk fills ("ENOSPC: no space left on device"). Chrome (FlareSolverr / cf-bypass) then can't write
its profile, solves time out, and every Cloudflare-protected site looks "blocked".

  free_bytes(path)            free space on the filesystem holding `path`
  purge_stale(root, age_s)    delete work dirs/files under `root` untouched for `age_s` seconds (+ leftover
                              jav_hls_* temp dirs) -- never touches anything a running download is still writing
  wait_for_disk(root)         async: purge stale, then wait (up to DISK_WAIT_MAX_S) until MIN_FREE_DISK_MB is free
  download_slots()            asyncio.Semaphore capping simultaneous downloads across ALL sites (MAX_PARALLEL_DOWNLOADS)

Env: MIN_FREE_DISK_MB (default 2500) | MAX_PARALLEL_DOWNLOADS (default 3) | STALE_DOWNLOAD_MIN (default 30)
     DISK_WAIT_MAX_S (default 1200)
"""
import asyncio
import glob
import logging
import os
import shutil
import tempfile
import time

logger = logging.getLogger("faphouse_bot")

MIN_FREE_BYTES = int(os.getenv("MIN_FREE_DISK_MB", "2500")) * 1024 * 1024
MAX_PARALLEL = max(1, int(os.getenv("MAX_PARALLEL_DOWNLOADS", "3")))
STALE_SECONDS = max(60, int(os.getenv("STALE_DOWNLOAD_MIN", "30")) * 60)
DISK_WAIT_MAX_S = int(os.getenv("DISK_WAIT_MAX_S", "1200"))

_sem = None


def download_slots() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(MAX_PARALLEL)
    return _sem


def free_bytes(path: str) -> int:
    try:
        os.makedirs(path, exist_ok=True)
        return shutil.disk_usage(path).free
    except Exception:
        return 1 << 62  # can't tell -> don't block anything


def _newest_mtime(path: str, cap: int = 4000) -> float:
    newest = 0.0
    seen = 0
    try:
        newest = os.path.getmtime(path)
        if os.path.isdir(path):
            for dp, _dn, fns in os.walk(path):
                for fn in fns:
                    try:
                        newest = max(newest, os.path.getmtime(os.path.join(dp, fn)))
                    except OSError:
                        pass
                    seen += 1
                    if seen >= cap:
                        return newest
    except OSError:
        pass
    return newest


def _size(path: str) -> int:
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for dp, _dn, fns in os.walk(path):
        for fn in fns:
            try:
                total += os.path.getsize(os.path.join(dp, fn))
            except OSError:
                pass
    return total


def purge_stale(root: str, age_s: int = None) -> int:
    """Remove leftovers nobody has written to for age_s seconds. Returns bytes freed."""
    age_s = STALE_SECONDS if age_s is None else age_s
    now, freed = time.time(), 0
    targets = []
    try:
        targets += [e.path for e in os.scandir(root)]
    except OSError:
        pass
    targets += glob.glob(os.path.join(tempfile.gettempdir(), "jav_hls_*"))
    for path in targets:
        if now - _newest_mtime(path) < age_s:
            continue  # still being written: a live download
        size = _size(path)
        try:
            shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
            freed += size
        except OSError:
            continue
    if freed:
        logger.info(f"[disk] purged {freed / 1e6:.0f} MB of stale downloads from {root}")
    return freed


def start_janitor(root: str, every_s: int = 600) -> None:
    """Daemon thread: purge stale leftovers every few minutes so a crashed/aborted download can't slowly eat the disk."""
    import threading

    def _loop():
        while True:
            time.sleep(every_s)
            try:
                purge_stale(root)
            except Exception as e:
                logger.debug(f"[disk] janitor error: {e}")

    threading.Thread(target=_loop, name="disk-janitor", daemon=True).start()


async def wait_for_disk(root: str, need_bytes: int = 0, max_wait_s: int = None) -> bool:
    """True once enough space is free (purging stale leftovers first). Waits instead of starting a download that
    would fill the disk -- other sites' running downloads free space as they finish."""
    need = max(MIN_FREE_BYTES, need_bytes)
    deadline = time.time() + (DISK_WAIT_MAX_S if max_wait_s is None else max_wait_s)
    warned = False
    while True:
        if free_bytes(root) >= need:
            return True
        await asyncio.to_thread(purge_stale, root)
        if free_bytes(root) >= need:
            return True
        # nothing stale left: only running downloads hold space -> try progressively fresher leftovers once
        if time.time() >= deadline:
            logger.error(f"[disk] only {free_bytes(root) / 1e6:.0f} MB free (< {need / 1e6:.0f} MB) after "
                         f"{DISK_WAIT_MAX_S if max_wait_s is None else max_wait_s}s -- not starting another download")
            return False
        if not warned:
            logger.warning(f"[disk] low space ({free_bytes(root) / 1e6:.0f} MB free, need {need / 1e6:.0f} MB) "
                           f"-- waiting for running downloads to finish...")
            warned = True
        await asyncio.sleep(20)

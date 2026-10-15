"""Keeps the bot running. Start the bot with:  python run_forever.py

It starts main.py as a child process and restarts it whenever it exits for any reason (crash, abort, out of memory,
an unhandled error). A native-code crash like "double free or corruption" kills the whole Python process, so nothing
inside it can recover; only a parent process like this one can bring the bot back.

On every crash it:
  * writes crash_logs/crash_<time>.log with the exit reason and the last 300 lines of output (including the
    faulthandler stack trace, which shows the Python code that was running),
  * sends a short message to OWNER_ID on Telegram (if the bot token is set),
  * restarts after a short delay. Repeated fast crashes back off up to CRASH_MAX_WAIT seconds.

Ctrl+C or SIGTERM stops everything for good (no restart).

Env:
  CRASH_MIN_WAIT   seconds before the first restart (default 3)
  CRASH_MAX_WAIT   longest wait between restarts (default 60)
  BOT_ENTRY        script to run (default main.py)
"""
import collections
import datetime
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ENTRY = os.path.join(HERE, os.environ.get("BOT_ENTRY", "main.py"))
LOG_DIR = os.path.join(HERE, "crash_logs")
MIN_WAIT = float(os.environ.get("CRASH_MIN_WAIT", "3"))
MAX_WAIT = float(os.environ.get("CRASH_MAX_WAIT", "60"))
STABLE_AFTER = 600          # running this long = healthy, backoff resets
TAIL_LINES = 300

_stop = threading.Event()
_child = None


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str):
    print(f"[watchdog {_now()}] {msg}", flush=True)


def _telegram_notify(text: str):
    """Best-effort message to the owner. Reads BOT_TOKEN / OWNER_ID from env or config.py; silent if unavailable."""
    token = os.environ.get("BOT_TOKEN")
    owner = os.environ.get("OWNER_ID")
    if not (token and owner):
        try:
            sys.path.insert(0, HERE)
            import config  # noqa: E402
            token = token or getattr(config, "BOT_TOKEN", None)
            owner = owner or getattr(config, "OWNER_ID", None)
        except Exception:
            pass
    if not (token and owner):
        return
    try:
        data = urllib.parse.urlencode({"chat_id": owner, "text": text[:3900]}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=15).read()
    except Exception as e:
        _log(f"telegram notify failed: {e}")


def _describe_exit(code: int) -> str:
    if code is None:
        return "still running"
    if code < 0:
        try:
            import signal as _s
            return f"killed by signal {_s.Signals(-code).name} ({-code})"
        except Exception:
            return f"killed by signal {-code}"
    if code == 134:
        return "aborted (SIGABRT, e.g. 'double free or corruption')"
    if code == 139:
        return "segmentation fault (SIGSEGV)"
    return f"exited with code {code}"


def _pump(stream, buf: collections.deque):
    """Copy child output to our console and keep the last lines for the crash log."""
    for raw in iter(stream.readline, b""):
        line = raw.decode("utf-8", errors="replace").rstrip("\n")
        buf.append(line)
        print(line, flush=True)
    stream.close()


def _write_crash_log(code: int, lines, uptime: float) -> str:
    os.makedirs(LOG_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(LOG_DIR, f"crash_{stamp}.log")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"time: {_now()}\nreason: {_describe_exit(code)}\nexit code: {code}\n"
                f"uptime: {uptime:.0f}s\nentry: {ENTRY}\n{'=' * 60}\n")
        f.write("\n".join(lines))
    return path


def _run_once() -> tuple:
    """Run the bot until it exits. Returns (exit_code, last_lines, uptime_seconds)."""
    global _child
    buf = collections.deque(maxlen=TAIL_LINES)
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    cmd = [sys.executable, "-X", "faulthandler", ENTRY]
    started = time.time()
    _child = subprocess.Popen(cmd, cwd=HERE, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    reader = threading.Thread(target=_pump, args=(_child.stdout, buf), daemon=True)
    reader.start()
    code = _child.wait()
    reader.join(timeout=5)
    _child = None
    return code, list(buf), time.time() - started


def _forward_stop(signum, frame):
    _stop.set()
    _log(f"signal {signum} received - stopping the bot, no restart")
    if _child and _child.poll() is None:
        try:
            _child.send_signal(signal.SIGTERM)
        except Exception:
            pass


def main():
    signal.signal(signal.SIGINT, _forward_stop)
    signal.signal(signal.SIGTERM, _forward_stop)
    _log(f"starting {ENTRY} (auto-restart on crash)")
    wait = MIN_WAIT
    crashes = 0
    while not _stop.is_set():
        code, lines, uptime = _run_once()
        if _stop.is_set():
            _log("bot stopped on request. bye.")
            break
        crashes += 1
        reason = _describe_exit(code)
        path = _write_crash_log(code, lines, uptime)
        _log(f"bot {reason} after {uptime:.0f}s. crash log: {path}. restarting in {wait:.0f}s")
        _telegram_notify(f"⚠️ Bot crashed ({reason}) after {uptime:.0f}s. Restarting in {wait:.0f}s.\n"
                         f"Crash #{crashes}, log: {os.path.basename(path)}")
        if uptime >= STABLE_AFTER:
            wait = MIN_WAIT                     # it ran fine for a while: start over
        else:
            wait = min(max(wait * 2, MIN_WAIT), MAX_WAIT)
        # interruptible sleep so Ctrl+C is honoured during the wait
        if _stop.wait(wait):
            break
        if not _stop.is_set():
            _telegram_notify("🔄 Restarting the bot now.")
    sys.exit(0)


if __name__ == "__main__":
    main()

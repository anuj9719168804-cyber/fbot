#!/bin/sh
# Local/VPS runner: keeps the bot alive after a crash and prints the native crash trace (segfault) if one happens.
# The Dockerfile / Render run Python 3.12; the native packages (curl_cffi, tgcrypto, greenlet/playwright) are built and
# tested there. Python 3.14 is too new for them and can segfault - use a 3.12 venv:
#     python3.12 -m venv venv && . venv/bin/activate && pip install -r requirements.txt
cd "$(dirname "$0")" || exit 1
PYV=$(python3 -c 'import sys;print("%d.%d" % sys.version_info[:2])')
[ "$PYV" != "3.12" ] && echo "[run_local] WARNING: Python $PYV in use - the bot is built for 3.12 (a segfault was seen on 3.14)."
export PYTHONFAULTHANDLER=1          # a segfault now prints which Python line / thread was running
while true; do
    python3 -X faulthandler main.py
    code=$?
    echo "[run_local] bot exited with code $code - restarting in 5s (Ctrl-C twice to stop)"
    sleep 5
done

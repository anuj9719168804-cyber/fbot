#!/bin/sh
# Starts the bgutil PO-token HTTP server (built at image-build time — see
# Dockerfile) in the background, waits a moment, then prints whether it's
# actually reachable before starting the bot — so "is it working" is
# answered by the container logs on every boot instead of staying a
# silent guess. Replaces the old approach of building+starting this from
# inside pot_provider.py at bot runtime with Deno — see the Dockerfile's
# own comment for why that was fragile (an unnecessary "canvas" native
# build) and pot_provider.py's docstring for what it does now instead
# (a Node.js-based fallback for non-Docker hosts where this script never
# runs, e.g. Render's native Python buildpack).
node /opt/bgutil-pot/server/build/main.js > /tmp/bgutil-pot.log 2>&1 &
BGUTIL_PID=$!

# Poll for up to ~15s instead of a single fixed sleep — on slower/cold
# instances the Node server can take longer than a couple seconds to
# bind its port.
BGUTIL_UP=0
for i in $(seq 1 15); do
    if wget -q -O- http://127.0.0.1:4416/ping >/dev/null 2>&1; then
        BGUTIL_UP=1
        break
    fi
    sleep 1
done

if [ "$BGUTIL_UP" = "1" ] && kill -0 "$BGUTIL_PID" 2>/dev/null; then
    echo "[bgutil-pot] OK — PO token server is up on :4416 (YouTube 'web' client should get the full quality ladder)"
else
    echo "[bgutil-pot] WARNING — PO token server did not come up after 15s. Last log lines:"
    tail -n 20 /tmp/bgutil-pot.log 2>/dev/null
    echo "[bgutil-pot] Bot will still run — YouTube just falls back to tv_embedded/android's lower-res formats."
fi

# ytnode — a cookie-free ytdl-core-based fallback YouTube downloader
# (ytnode_client.py) for when the PO-token path above still comes back
# degraded. Same start-in-background-then-poll pattern as bgutil-pot
# above; a failure here is non-fatal the same way — ytdlp_downloader.py's
# fallback is already written to just skip it (ytnode_client.is_ready()
# returning False) and use whatever yt-dlp's own result was.
node /app/ytnode/server.js > /tmp/ytnode.log 2>&1 &
YTNODE_PID=$!

YTNODE_UP=0
for i in $(seq 1 15); do
    if wget -q -O- http://127.0.0.1:4417/health >/dev/null 2>&1; then
        YTNODE_UP=1
        break
    fi
    sleep 1
done

if [ "$YTNODE_UP" = "1" ] && kill -0 "$YTNODE_PID" 2>/dev/null; then
    echo "[ytnode] OK — cookie-free YouTube fallback is up on :4417"
else
    echo "[ytnode] WARNING — did not come up after 15s. Last log lines:"
    tail -n 20 /tmp/ytnode.log 2>/dev/null
    echo "[ytnode] Bot will still run — YouTube quality just relies on the PO-token path alone."
fi

# cf-bypass — Turnstile token service for fpo.xxx's login (turnstile_solver.py). Own Chrome + Xvfb, port 8742.
# Failure is non-fatal: fpo_downloader falls back to its older login routes.
#
# Memory preflight: FlareSolverr's Chrome + cf-bypass's headed Chrome + big downloads in one small container get
# OOM-killed ("renderer disconnected" / "ECONNREFUSED 127.0.0.1:<random port>"). If less than CF_BYPASS_MIN_MB (default
# 1500) of RAM is available we do NOT start the second Chrome at boot. Override: CF_BYPASS_FORCE=1.
mem_avail_mb() {
    LIM=""
    for f in /sys/fs/cgroup/memory.max /sys/fs/cgroup/memory/memory.limit_in_bytes; do
        [ -r "$f" ] && LIM=$(cat "$f" 2>/dev/null) && break
    done
    HOST=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo 2>/dev/null)
    if [ -n "$LIM" ] && [ "$LIM" != "max" ] && [ "$LIM" -lt 1099511627776 ] 2>/dev/null; then
        LIM_MB=$((LIM/1024/1024))
        if [ -n "$HOST" ] && [ "$HOST" -lt "$LIM_MB" ]; then echo "$HOST"; else echo "$LIM_MB"; fi
    else
        echo "${HOST:-0}"
    fi
}

if [ "${CF_BYPASS_ENABLED:-1}" != "0" ] && [ -d /app/cf-bypass ] && command -v bun >/dev/null 2>&1; then
    MEM_MB=$(mem_avail_mb)
    MIN_MB=${CF_BYPASS_MIN_MB:-1500}
    echo "[cf-bypass] memory available to this container: ${MEM_MB} MB (need >= ${MIN_MB} MB)"
    if [ "$MEM_MB" -lt "$MIN_MB" ] && [ "${CF_BYPASS_FORCE:-0}" != "1" ]; then
        echo "[cf-bypass] SKIPPED — not enough RAM for a second Chrome next to FlareSolverr (it would get OOM-killed and take FlareSolverr's Chrome down with it)."
        echo "[cf-bypass] Raise the instance RAM to >= 2 GB, or set CF_BYPASS_FORCE=1 to start it anyway."
    else
        # Supervisor: if the service (or its Chrome) dies, restart it instead of leaving :8742 dead for the rest of the run.
        (
            n=0
            while [ "$n" -lt 20 ]; do
                (cd /app/cf-bypass && PORT=8742 bun src/index.ts) >> /tmp/cf-bypass.log 2>&1
                n=$((n+1))
                echo "[cf-bypass] process exited — restart $n/20 in 15s" >> /tmp/cf-bypass.log
                sleep 15
            done
        ) &
        CFB_UP=0
        for i in $(seq 1 20); do
            if wget -S -q -O- http://127.0.0.1:8742/ 2>&1 | grep -q "HTTP/"; then
                CFB_UP=1
                break
            fi
            sleep 1
        done
        if [ "$CFB_UP" = "1" ]; then
            echo "[cf-bypass] OK — Turnstile token service is up on :8742"
        else
            echo "[cf-bypass] WARNING — did not come up after 20s. Last log lines:"
            tail -n 20 /tmp/cf-bypass.log 2>/dev/null
        fi
    fi
fi

exec python3 main.py

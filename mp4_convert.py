"""
Any video container -> real .mp4 (so Telegram shows it as a playable video instead of a plain document).

  .ts .m2ts .mts .mkv .avi .flv .webm .wmv .mpg .mpeg .mov .m4v .3gp .vob .ogv .asf .f4v .divx  ->  .mp4

How (fastest + lossless first, heavy re-encode only when it has to be):
  1. remux          stream copy, video + audio untouched                (seconds, no quality loss)
  2. remux + aac    video copied, audio re-encoded to AAC               (when the audio can't live in mp4)
  3. re-encode      libx264 + aac, yuv420p                              (VP9/AV1/MPEG-2/WMV/10-bit... or when 1-2 fail)
Plan 1-2 are only tried when the video codec is mp4-safe (h264 / hevc / mpeg4, 8-bit); otherwise it goes straight to 3.
Every result is sanity-checked (size + duration) and if a plan produced a short/broken file the next plan is tried.

On any failure the ORIGINAL file is returned untouched, so the upload still happens exactly like before.

Env:  MP4_REENCODE=0          never re-encode (remux only - for weak servers)
      MP4_CONVERT_TIMEOUT=N   seconds per conversion before giving up (default 7200)
"""

import asyncio
import html
import json
import logging
import os
import shutil
import time

logger = logging.getLogger("faphouse_bot")

CONVERT_EXTS = (
    ".ts", ".m2ts", ".mts", ".mkv", ".avi", ".flv", ".webm", ".wmv", ".mpg", ".mpeg",
    ".mov", ".m4v", ".3gp", ".vob", ".ogv", ".asf", ".f4v", ".divx",
)
MP4_REENCODE = os.environ.get("MP4_REENCODE", "1") != "0"
CONVERT_TIMEOUT = int(os.environ.get("MP4_CONVERT_TIMEOUT", "7200"))
_SAFE_VIDEO = {"h264", "hevc", "mpeg4"}
_SAFE_AUDIO = {"aac", "mp3"}
_SAFE_PIX = {None, "yuv420p", "yuvj420p"}


def needs_convert(path: str) -> bool:
    return (path or "").lower().endswith(CONVERT_EXTS) and bool(shutil.which("ffmpeg"))


async def _probe(path: str) -> dict:
    """-> {"dur": float, "vcodec": str|None, "pix": str|None, "acodec": str|None, "has_video": bool}"""
    info = {"dur": 0.0, "vcodec": None, "pix": None, "acodec": None, "has_video": True}
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,pix_fmt:format=duration",
            "-of", "json", path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        data = json.loads(out or b"{}")
        try:
            info["dur"] = float((data.get("format") or {}).get("duration") or 0)
        except (TypeError, ValueError):
            pass
        streams = data.get("streams") or []
        v = next((s for s in streams if s.get("codec_type") == "video"), None)
        a = next((s for s in streams if s.get("codec_type") == "audio"), None)
        info["has_video"] = v is not None
        if v:
            info["vcodec"], info["pix"] = v.get("codec_name"), v.get("pix_fmt")
        if a:
            info["acodec"] = a.get("codec_name")
    except Exception as e:
        logger.warning(f"[mp4] ffprobe failed for {path}: {e}")
    return info


def _plans(info: dict) -> list:
    vc, ac, pix = info.get("vcodec"), info.get("acodec"), info.get("pix")
    probe_failed = vc is None and info.get("has_video", True)
    video_copy_ok = probe_failed or (vc in _SAFE_VIDEO and pix in _SAFE_PIX)
    plans = []
    if video_copy_ok:
        vtag = ["-tag:v", "hvc1"] if vc == "hevc" else []
        if ac is None or ac in _SAFE_AUDIO or probe_failed:
            bsf = ["-bsf:a", "aac_adtstoasc"] if ac == "aac" else []
            plans.append(("remux", ["-c", "copy", *vtag, *bsf]))
        plans.append(("remux + aac", ["-c:v", "copy", *vtag, "-c:a", "aac", "-b:a", "128k"]))
    if MP4_REENCODE:
        plans.append(("re-encode", ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                                    "-c:a", "aac", "-b:a", "128k"]))
    return plans


def _rm(fp: str):
    try:
        os.remove(fp)
    except OSError:
        pass


async def ensure_mp4(path: str, on_progress=None) -> str:
    """Convert `path` to .mp4 if it is another video container. Returns the new path (original removed), or the
    original path if no conversion was needed / possible. on_progress(frac 0..1, plan_name) is awaited, if given.
    asyncio cancellation kills ffmpeg and removes the partial output."""
    if not needs_convert(path) or not os.path.exists(path):
        return path
    info = await _probe(path)
    if not info["has_video"]:
        return path                                    # audio-only file: leave it alone
    base = os.path.splitext(path)[0]
    final, tmp = base + ".mp4", base + ".converting.mp4"
    insize = os.path.getsize(path)

    for plan_name, args in _plans(info):
        _rm(tmp)
        if on_progress:
            await on_progress(0.0, plan_name)
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-progress", "pipe:1", "-nostats",
               "-fflags", "+genpts", "-i", path, "-map", "0:v:0", "-map", "0:a?", "-sn", "-dn",
               "-max_muxing_queue_size", "1024", *args, "-movflags", "+faststart", "-f", "mp4", tmp]
        t0 = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except Exception as e:
            logger.warning(f"[mp4] ffmpeg spawn failed: {e}")
            return path
        err_task = asyncio.create_task(proc.stderr.read())
        try:
            while True:
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=3)
                except asyncio.TimeoutError:
                    line = None
                if line == b"":
                    break
                if time.time() - t0 > CONVERT_TIMEOUT:
                    logger.warning(f"[mp4] {plan_name} timed out after {CONVERT_TIMEOUT}s for {path}")
                    proc.kill()
                    break
                if line and on_progress:
                    txt = line.decode(errors="ignore").strip()
                    if txt.startswith(("out_time_us=", "out_time_ms=")):
                        try:
                            sec = int(txt.split("=", 1)[1]) / 1e6
                        except ValueError:
                            continue
                        if info["dur"] > 0:
                            await on_progress(min(sec / info["dur"], 0.99), plan_name)
                        elif insize and os.path.exists(tmp):          # unknown duration: output grows ~ input size
                            await on_progress(min(os.path.getsize(tmp) / insize, 0.99), plan_name)
            await proc.wait()
        except BaseException:                          # task cancelled / error -> stop ffmpeg, drop partial output
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            err_task.cancel()
            _rm(tmp)
            raise
        try:
            err = (await err_task).decode(errors="ignore").strip().splitlines()
        except Exception:
            err = []

        if proc.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 10 * 1024:
            out_info = await _probe(tmp)
            if info["dur"] > 0 and out_info["dur"] > 0 and out_info["dur"] < info["dur"] * 0.85:
                logger.warning(f"[mp4] {plan_name} gave a short file ({out_info['dur']:.0f}s of {info['dur']:.0f}s) - trying next plan")
                _rm(tmp)
                continue
            try:
                os.replace(tmp, final)
            except OSError as e:
                logger.warning(f"[mp4] couldn't move result into place: {e}")
                _rm(tmp)
                return path
            if final != path:
                _rm(path)
            if on_progress:
                await on_progress(1.0, plan_name)
            logger.info(f"[mp4] {os.path.basename(path)} -> mp4 via {plan_name} in {time.time() - t0:.0f}s")
            return final
        logger.warning(f"[mp4] {plan_name} failed (exit {proc.returncode}): {(err[-1] if err else '')[:200]}")
        _rm(tmp)
    logger.warning(f"[mp4] could not convert {path} - sending the original")
    return path


def status_progress(status, name: str, sc=None, interval: float = 4.0):
    """on_progress callback that edits a Telegram status message (throttled; edit errors are ignored)."""
    from pyrogram.enums import ParseMode
    last = [0.0]

    async def cb(frac: float, plan: str):
        now = time.time()
        if frac not in (0.0, 1.0) and now - last[0] < interval:
            return
        last[0] = now
        pct = max(0.0, min(frac * 100, 100.0))
        filled = min(10, int(10 * pct / 100))
        bar = "⬢" * filled + "⬡" * (10 - filled)             # same look as the download progress bar in main.py
        txt = (f"<b>🔄 Converting to MP4 ({plan})...</b>\n"
               f"[{bar}] {pct:.0f}%\n"
               f"<code>{html.escape((name or '')[:60])}</code>")
        try:
            await status.edit_text(sc(txt) if sc else txt, parse_mode=ParseMode.HTML)
        except Exception:
            pass
    return cb

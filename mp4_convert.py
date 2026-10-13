"""Convert any video container (ts, mkv, avi, webm, mov, flv, wmv, 3gp, ...) to a real .mp4 before upload.

Why: Telegram only shows a playable video for .mp4 files. Anything else arrives as a plain document.

Strategy (same idea as ak-vip's remux_to_mp4, with an extra fallback):
  1. stream copy (no re-encode, no quality loss) + AAC audio bitstream fix
  2. stream copy video, re-encode audio to AAC
  3. full re-encode to H.264 + AAC (last resort for codecs MP4 can't hold, e.g. WMV / old FLV)
If every attempt fails, the original file is returned untouched so the upload still goes through.
"""
import asyncio
import logging
import os
import shutil

logger = logging.getLogger("mp4_convert")

# Extensions that must be converted/remuxed to .mp4. .mp4 itself is never touched.
NEEDS_MP4 = (
    ".ts", ".m2ts", ".mts", ".mkv", ".avi", ".flv", ".f4v", ".3gp", ".webm", ".wmv",
    ".mpg", ".mpeg", ".mov", ".m4v", ".vob", ".asf", ".ogv", ".divx", ".rmvb", ".rm",
)

_TIMEOUT_S = 6 * 3600  # hard ceiling for one ffmpeg run


def needs_mp4(path: str) -> bool:
    return path.lower().endswith(NEEDS_MP4)


def mp4_path_for(path: str) -> str:
    dst = os.path.splitext(path)[0] + ".mp4"
    if dst == path:
        dst = path + ".mp4"
    return dst


def _ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg"))


async def _run_ffmpeg(src: str, dst: str, args: list) -> bool:
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", src, *args,
           "-movflags", "+faststart", dst]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.warning("ffmpeg timed out converting %s", src)
        return False
    except BaseException:  # cancelled by the user / task cancel -> stop ffmpeg
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    if proc.returncode != 0:
        logger.info("ffmpeg attempt failed for %s: %s", src,
                    (err or b"").decode("utf-8", "ignore")[-200:])
        return False
    return True


async def convert_to_mp4(path: str) -> str:
    """Return the path of the MP4 to upload. On any failure returns `path` unchanged."""
    if not needs_mp4(path) or not _ffmpeg_available():
        return path

    dst = mp4_path_for(path)
    attempts = [
        # 1) lossless remux
        ["-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-bsf:a", "aac_adtstoasc"],
        # 2) keep video, re-encode audio only
        ["-map", "0:v:0", "-map", "0:a?", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k"],
        # 3) full transcode for codecs MP4 cannot hold as-is
        ["-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k"],
    ]

    for args in attempts:
        ok = await _run_ffmpeg(path, dst, args)
        if ok and os.path.exists(dst) and os.path.getsize(dst) > 50 * 1024:
            try:
                os.remove(path)
            except OSError:
                pass
            return dst
        if os.path.exists(dst):
            try:
                os.remove(dst)
            except OSError:
                pass

    logger.warning("could not convert %s to mp4 - uploading original", path)
    return path

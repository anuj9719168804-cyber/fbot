"""Gofile.io upload, used for files over Telegram's 2 GB limit when the user has a Gofile token (/settings).

Ported from mxdlbot-main's services/uploader.py (GofileUploader). Endpoints: GET api.gofile.io/servers,
then multipart POST to https://<server>.gofile.io/contents/uploadfile with the optional token field.
"""
import os

import aiohttp

BASE_URL = "https://api.gofile.io"


class GofileError(Exception):
    """A problem worth showing to the user as-is."""


async def _best_server() -> str:
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(f"{BASE_URL}/servers") as r:
            data = await r.json(content_type=None)
    if not isinstance(data, dict) or data.get("status") != "ok":
        raise GofileError("Gofile servers are not responding right now.")
    servers = (data.get("data") or {}).get("servers") or []
    if not servers:
        raise GofileError("Gofile returned no upload server.")
    return servers[0]["name"]


async def upload(path: str, token: str = "", progress=None) -> str:
    """Upload one file. progress(done, total) is awaited per 1 MB chunk. Returns the Gofile download page URL."""
    server = await _best_server()
    url = f"https://{server}.gofile.io/contents/uploadfile"
    size = os.path.getsize(path)
    done = 0

    async def sender():
        nonlocal done
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                done += len(chunk)
                if progress:
                    await progress(done, size)
                yield chunk

    form = aiohttp.FormData()
    if token:
        form.add_field("token", token)
    form.add_field("file", sender(), filename=os.path.basename(path), content_type="application/octet-stream")
    timeout = aiohttp.ClientTimeout(total=None, sock_read=600)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, data=form) as r:
            if r.status != 200:
                raise GofileError(f"Gofile upload failed (HTTP {r.status}).")
            res = await r.json(content_type=None)
    if not isinstance(res, dict) or res.get("status") != "ok":
        raise GofileError("Gofile rejected the upload. Check the token in /settings.")
    link = (res.get("data") or {}).get("downloadPage")
    if not link:
        raise GofileError("Gofile did not return a download link.")
    return link

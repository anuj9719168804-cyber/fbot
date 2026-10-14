"""MX Player downloader for fbot  (/mxplayer <link>  or just paste an mxplayer.in link).

Ported from src--tera_api's Akbots/mxplayer.py + mx_direct_api.py and rewritten the way fbot's other downloaders work
(no extra pip packages, no third-party resolver / API key).

What it does
  1. finds the content id in the mxplayer.in link and asks MX Player's own web API for the title + stream links;
  2. hands the HLS (.m3u8) stream to m3u8_downloader.start_flow(), so the user gets the SAME menus as /m3u8:
     quality -> audio track (Hindi marked) -> optional H.265 compress -> segment download -> mux -> fbot's normal upload
     path (custom caption / thumbnail, 2 GB split, auto-delete, backup/log channels, daily limit).
  /cancel stops a running download (global command, same as every other download).

Not supported (the bot says so instead of producing a broken file):
  * DRM-protected titles (MX Player flags them with `drmProtect`; they need a Widevine licence). Nothing here tries to
    get around DRM.
  * DASH-only titles (only HLS is downloaded) and series landing pages (send the link of ONE episode).
  * Titles MX Player blocks for this server's IP / region (HTTP 403) - run the bot from an Indian IP for those.

Env: MX_MAX_ITEMS (links per message, default 10), MX_API_TIMEOUT (seconds, default 20).
"""
import asyncio
import html as _html
import logging
import os
import re
from urllib.parse import urlparse, unquote

import aiohttp
from pyrogram import filters, enums

logger = logging.getLogger("mxplayer_downloader")

_ns = None

SITE = "https://www.mxplayer.in/"
API_BASE = "https://api.mxplayer.in/v1/web"
CDN_BASE = "https://d3sgzbosmwirao.cloudfront.net/"
THUMB_BASE = "https://qqcdnpictest.mxplay.com/"
_PARAMS = ("&platform=com.mxplay.desktop&device-density=2&kids-mode-enabled=false"
           "&content-languages=hi,en,ta,te,bn,ml,kn,mr,pa,gu,bho")
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.mxplayer.in",
    "Referer": SITE,
}

MAX_ITEMS = int(os.environ.get("MX_MAX_ITEMS", "10"))
API_TIMEOUT = int(os.environ.get("MX_API_TIMEOUT", "20"))

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_ID_RE = re.compile(r"^[a-f0-9]{8,}$")


class MXError(Exception):
    """A problem worth showing to the user as-is."""


# ------------------------------------------------------------------------------------------- links ----
def is_mxplayer_link(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
        return host == "mxplayer.in" or host.endswith(".mxplayer.in")
    except Exception:
        return False


def parse_link(url: str):
    """-> (content_id, api_type) or (None, None).  api_type is 'movie' or 'episode' (MX's own names).
    The id is the hex block after the last '-' of the last path segment (or the whole segment)."""
    try:
        parts = [p for p in unquote(urlparse(url).path).split("/") if p]
    except Exception:
        return None, None
    if not parts:
        return None, None
    cid = parts[-1].rsplit("-", 1)[-1].lower()
    if not _ID_RE.match(cid):
        return None, None
    return cid, ("movie" if parts[0].lower() == "movie" else "episode")


def extract_mxplayer_links(text: str) -> list:
    """Same contract as tnaflix.extract_tnaflix_links(): playable-looking MX Player links only, de-duplicated."""
    if not text:
        return []
    seen, out = set(), []
    for match in _URL_RE.findall(text):
        url = match.rstrip(").,!?>'\"")
        if is_mxplayer_link(url) and parse_link(url)[0] and url not in seen:
            seen.add(url)
            out.append(url)
    return out


# -------------------------------------------------------------------------------------------- API -----
async def _fetch(session: aiohttp.ClientSession, cid: str, api_type: str):
    url = f"{API_BASE}/detail/video?type={api_type}&id={cid}{_PARAMS}"
    async with session.get(url, headers=_HEADERS) as r:
        if r.status != 200:
            return None, r.status
        return await r.json(content_type=None), r.status


def _d(x) -> dict:
    return x if isinstance(x, dict) else {}


def _absolute(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return ""
    return u if u.startswith("http") else CDN_BASE + u.lstrip("/")


def parse_detail(data: dict) -> dict:
    """MX 'detail/video' JSON -> {title, seo_title, season, description, thumbnail, hls_url, dash_url, drm}."""
    data = _d(data)
    title = data.get("title")
    if not title:
        raise MXError("MX Player returned no title for this link (wrong link, or the title was removed).")
    stream = _d(data.get("stream"))
    if not stream:
        raise MXError("No stream info for this link. If this is a series page, send the link of ONE episode.")

    provider = stream.get("provider") or ""
    if provider == "thirdParty":
        hls = _d(stream.get("thirdParty")).get("webHlsUrl") or stream.get("hlsUrl") or ""
        dash = ""
    else:
        prov = _d(stream.get(provider))
        h = _d(prov.get("hls")) or _d(stream.get("hls"))
        d = _d(prov.get("dash")) or _d(stream.get("dash"))
        hls = h.get("high") or h.get("base") or h.get("main") or ""
        dash = d.get("high") or d.get("base") or d.get("main") or ""

    image = _d((data.get("imageInfo") or [{}])[0] if isinstance(data.get("imageInfo"), list) and data.get("imageInfo") else {})
    thumb = image.get("url") or ""
    return {
        "title": str(title),
        "seo_title": str(data.get("seoTitle") or title),
        "season": _d(data.get("season")).get("seasonNo") or "",
        "description": str(data.get("description") or ""),
        "thumbnail": thumb if thumb.startswith("http") else (THUMB_BASE + thumb.lstrip("/") if thumb else ""),
        "hls_url": _absolute(hls),
        "dash_url": _absolute(dash),
        "drm": bool(stream.get("drmProtect")),
    }


async def resolve(link: str) -> dict:
    """Link -> parse_detail() dict with a usable hls_url, or raises MXError with a user-facing reason."""
    cid, api_type = parse_link(link)
    if not cid:
        raise MXError("That doesn't look like an MX Player title link (no content id in it).")
    other = "movie" if api_type == "episode" else "episode"
    timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            data, status = await _fetch(session, cid, api_type)
            if data is None and status in (403, 404):
                # MX answers 403/404 when the id is asked under the wrong type - the URL can misclassify
                # movie vs show, so try the other type once before treating it as a real block.
                data, status = await _fetch(session, cid, other)
    except asyncio.TimeoutError:
        raise MXError("MX Player's API didn't answer in time - try again in a moment.")
    except aiohttp.ClientError as e:
        raise MXError(f"Couldn't reach MX Player's API ({str(e)[:100]}).")
    if data is None:
        if status == 403:
            raise MXError("MX Player refused the request (HTTP 403) - it may block this server's IP/region for this title.")
        raise MXError(f"MX Player's API error: HTTP {status}.")

    info = parse_detail(data)
    if info["drm"]:
        raise MXError(f"“{info['title']}” is DRM-protected (Widevine) - it can't be downloaded.")
    if not info["hls_url"]:
        if info["dash_url"]:
            raise MXError(f"“{info['title']}” is only offered as DASH, and only HLS streams are supported.")
        raise MXError(f"No downloadable stream found for “{info['title']}”.")
    return info


def _file_name(info: dict) -> str:
    name = info["title"]
    season = str(info.get("season") or "")
    if season.isdigit():
        name = f"{name} S{int(season):02d}"
    try:
        import url_uploader as _uu
        return _uu._safe_name(name, "MX_Video")[:60]
    except Exception:
        return re.sub(r"[^\w .()\-]+", "", name).strip()[:60] or "MX_Video"


# ------------------------------------------------------------------------------------------ flow ------
async def handle(client, message, links: list):
    ns = _ns
    uid = message.from_user.id
    if await ns.is_banned(uid):
        return await message.reply(ns.SC("🚫 You are banned from using this bot."))
    prev = ns.ACTIVE_TASKS.get(uid)
    if prev and not prev.done():
        import url_uploader as _uu
        return await message.reply(_uu._busy_message(), parse_mode=enums.ParseMode.HTML)

    links = links[:MAX_ITEMS]
    status = await message.reply(ns.SC(f"<b>🔎 Fetching MX Player details ({len(links)} link(s))...</b>"),
                                 parse_mode=enums.ParseMode.HTML)
    urls, infos, errors = [], [], []
    for link in links:
        try:
            info = await resolve(link)
        except MXError as e:
            errors.append(str(e))
        except Exception as e:
            logger.exception(f"mxplayer resolve failed for {link}")
            errors.append(f"Unexpected error: {str(e)[:120]}")
        else:
            urls.append(info["hls_url"])
            infos.append(info)

    problems = "".join(f"\n• {_html.escape(e)}" for e in errors)
    if not urls:
        return await _edit(status, ns.SC(f"<b>❌ Couldn't get a stream.</b>{problems}"))

    first = infos[0]
    desc = _html.escape(first["description"][:220] + ("…" if len(first["description"]) > 220 else ""))
    card = (f"<b>🎬 {_html.escape(first['title'])}</b>"
            + (f" <i>(+{len(infos) - 1} more)</i>" if len(infos) > 1 else "")
            + (f"\n\n<i>{desc}</i>" if desc and len(infos) == 1 else "")
            + (f"\n\n<b>⚠️ Skipped:</b>{problems}" if errors else ""))
    await _edit(status, ns.SC(card))

    import m3u8_downloader as m3
    # the quality / audio / compress menus, download, mux and upload are all m3u8_downloader's
    await m3.start_flow(client, message, urls, name=_file_name(first) if len(urls) == 1 else None, referer=SITE)


async def _edit(msg, text):
    try:
        await msg.edit_text(text, parse_mode=enums.ParseMode.HTML)
    except Exception:
        pass


async def maybe_handle(client, message, text: str) -> bool:
    """Hook for link_handler: a message containing an MX Player title link starts the flow. True = handled."""
    links = extract_mxplayer_links(text)
    if not links:
        return False
    await handle(client, message, links)
    return True


def setup(app, ns) -> None:
    global _ns
    _ns = ns

    @app.on_message(filters.command(["mxplayer", "mx"]) & filters.private)
    async def mxplayer_cmd(client, message):
        parts = (message.text or "").split(None, 1)
        links = extract_mxplayer_links(parts[1]) if len(parts) > 1 else []
        if not links:
            return await message.reply(ns.SC(
                "<b>🎬 MX Player Downloader</b>\n\n<b>Usage:</b>\n<code>/mxplayer &lt;mxplayer.in link&gt;</code>\n"
                "<code>/mxplayer link1 link2</code> (batch)\n\n"
                "Pick quality, audio language and (optionally) H.265 compression, then I upload the file.\n"
                "<i>You can also just paste the link. DRM-protected titles can't be downloaded. "
                "/cancel stops a running download.</i>"), parse_mode=enums.ParseMode.HTML)
        await handle(client, message, links)

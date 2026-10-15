"""MX Player downloader for fbot  (/mxplayer <link>  or just paste an mxplayer.in link).

Ported from src--tera_api's Akbots/mxplayer.py + mx_direct_api.py. The menus, downloads and uploads are MX-specific
here, but the heavy lifting (playlist parser, segment downloader, ffmpeg mux, quality/audio helpers, fbot's upload
path) is reused from m3u8_downloader and url_uploader, so limits, caption, backup/dump channels and daily count
behave exactly like every other fbot download.

What it does
  1. Resolves each link to a stream, trying in order:
       a) optional key-based resolver (MXPLAYER_API_KEY env, same proxy src uses);
       b) MX's own web API, asking for BOTH movie and episode types;
       c) last resort: the public title page, scanned for an .m3u8 URL.
  2. Menus (same as src):
       quality  ->  audio: several tracks can be ticked and muxed into ONE file (Done / use default)
       or  🎵 Audio only  ->  MP3 (one or several tracks mixed, or extracted from the video when MX has no separate audio)
  3. Downloads segments in parallel, muxes, uploads via url_uploader._upload with a caption that has season,
     episode and audio language, and records the download in history.
  /cancel stops a running download (global command, same as every other download).

Not supported (the bot says so instead of producing a broken file):
  * DRM-protected titles (MX Player flags them with `drmProtect`). Nothing here tries to get around DRM.
  * DASH-only titles (only HLS is downloaded) and series landing pages (send the link of ONE episode).
  * Titles MX Player blocks for this server's IP / region (HTTP 403) - run the bot from an Indian IP for those.

Env: MX_MAX_ITEMS (links per message, default 10), MX_API_TIMEOUT (seconds, default 20),
     MXPLAYER_API_KEY (optional, enables the key-based resolver).
     Proxy: MXPLAYER_PROXY=http://user:pass@host:port (own proxy, wins). Otherwise the free-proxy pool (free_proxy.py)
     is used for every MX request (API, title page, show page, segments). MXPLAYER_FREE_PROXY=0 turns the pool off,
     MXPLAYER_FREE_PROXY_MODE=fallback = direct first, proxy only after a block.
"""
import asyncio
import html as _html
import json
import logging
import os
import re
from urllib.parse import urlparse, unquote

import shutil
import time
import uuid

import aiohttp
from pyrogram import filters, enums

from mx_show_browser import is_show_link

logger = logging.getLogger("mxplayer_downloader")

_ns = None

SITE = "https://www.mxplayer.in/"
API_BASE = "https://api.mxplayer.in/v1/web"
CDN_BASE = "https://d3sgzbosmwirao.cloudfront.net/"
THUMB_BASE = "https://qqcdnpictest.mxplay.com/"
KEY_API = "https://ott.dkbotzpro.in/mxplayer"  # unofficial third-party resolver (src's primary); optional
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
MXPLAYER_API_KEY = os.environ.get("MXPLAYER_API_KEY", "").strip()
MX_DEBUG = os.environ.get("MX_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")
MX_DEBUG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mx_debug")

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_ID_RE = re.compile(r"^[a-f0-9]{8,}$")
_M3U8_RE = re.compile(r"https?://[^\s\"'<>\\]+?\.m3u8[^\s\"'<>\\]*", re.IGNORECASE)


class MXError(Exception):
    """A problem worth showing to the user as-is."""


# ------------------------------------------------------------------------------------------- links ----
def is_mxplayer_link(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
        return host == "mxplayer.in" or host.endswith(".mxplayer.in") or host == "mxplay.com" or host.endswith(".mxplay.com")
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
    kinds = [p.lower() for p in parts[:-1]]
    return cid, ("movie" if "movie" in kinds else "episode")


def extract_mxplayer_links(text: str) -> list:
    """Same contract as tnaflix.extract_tnaflix_links(): playable-looking MX Player links only, de-duplicated."""
    if not text:
        return []
    seen, out = set(), []
    for match in _URL_RE.findall(text):
        url = match.rstrip(").,!?>'\"")
        if is_mxplayer_link(url) and (parse_link(url)[0] or is_show_link(url)) and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _slug_title(url: str):
    """Best-effort title from the URL slug (used only if the API gives none)."""
    try:
        path = url.split("?")[0].split("#")[0]
        slug = [s for s in path.split("/") if s][-1]
        slug = re.sub(r"-[0-9a-fA-F]{20,}$", "", slug)
        stop = {"watch", "movie", "online", "free", "hd", "stream", "streaming", "web", "series", "webseries",
                "episode", "full", "official", "trailer", "video", "in", "on", "the", "a", "an"}
        words = [w for w in slug.replace("-", " ").replace("_", " ").split() if w.lower() not in stop]
        title = " ".join(words).title()
        return title if len(title) >= 3 else None
    except Exception:
        return None


def _parse_season_episode(seo_title: str):
    """seo_title is a sentence like 'Arafta Season 02 Episode 03' - pull the numbers out of it."""
    season = episode = ""
    if not seo_title:
        return season, episode
    m = re.search(r"season\s*0*(\d+)", seo_title, re.IGNORECASE)
    if m:
        season = m.group(1)
    m = re.search(r"episode\s*0*(\d+)", seo_title, re.IGNORECASE)
    if m:
        episode = m.group(1)
    return season, episode


# ----------------------------------------------------------------------------------- normalise ----
def _d(x) -> dict:
    return x if isinstance(x, dict) else {}


def _absolute(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return ""
    return u if u.startswith("http") else CDN_BASE + u.lstrip("/")


def _info(title, seo_title, season, episode, description, thumbnail, hls, dash, drm) -> dict:
    return {
        "title": str(title or "MX Video"),
        "seo_title": str(seo_title or title or ""),
        "season": str(season or ""),
        "episode": str(episode or ""),
        "description": str(description or ""),
        "thumbnail": thumbnail or "",
        "hls_url": _absolute(hls),
        "dash_url": _absolute(dash),
        "drm": bool(drm),
    }


def parse_detail(data: dict) -> dict:
    """MX 'detail/video' JSON -> normalised info dict. Raises MXError on a payload with no usable title/stream."""
    data = _d(data)
    # some responses wrap the payload once more
    if "title" not in data and isinstance(data.get("data"), dict):
        data = data["data"]
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

    images = data.get("imageInfo")
    image = _d(images[0]) if isinstance(images, list) and images else {}
    thumb = image.get("url") or ""
    thumb = thumb if thumb.startswith("http") else (THUMB_BASE + thumb.lstrip("/") if thumb else "")
    season = _d(data.get("season")).get("seasonNo") or ""
    s_no, e_no = _parse_season_episode(str(data.get("seoTitle") or ""))
    return _info(title, data.get("seoTitle"), season or s_no, data.get("episodeNo") or e_no,
                 data.get("description"), thumb, hls, dash, stream.get("drmProtect"))


# ------------------------------------------------------------------------------- resolvers ----
async def _fetch(session: aiohttp.ClientSession, cid: str, api_type: str, px: str | None = None):
    url = f"{API_BASE}/detail/video?type={api_type}&id={cid}{_PARAMS}"
    async with session.get(url, headers=_HEADERS, proxy=px) as r:
        if r.status != 200:
            return None, r.status
        return await r.json(content_type=None), r.status


def _dump_debug(cid: str, api_type: str, data) -> None:
    """MX_DEBUG=1: save the raw API answer so a link that fails can be inspected."""
    try:
        os.makedirs(MX_DEBUG_DIR, exist_ok=True)
        path = os.path.join(MX_DEBUG_DIR, f"{cid}_{api_type}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        logger.warning(f"mxplayer debug: raw {api_type} response saved to {path}")
    except Exception as e:
        logger.warning(f"mxplayer debug dump failed: {e}")


async def _resolve_api(link: str) -> dict:
    """MX's own web API, trying both movie and episode types. Raises MXError with the real reason."""
    cid, api_type = parse_link(link)
    if not cid:
        raise MXError("That doesn't look like an MX Player title link (no content id in it).")
    other = "movie" if api_type == "episode" else "episode"
    timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)
    reasons, status = [], None
    for attempt in range(3):
        px = _px()
        blocked = False
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for t in (api_type, other):
                    data, status = await _fetch(session, cid, t, px)
                    if data is None:
                        reasons.append(f"{t}: HTTP {status}")
                        blocked = blocked or status in (403, 429)
                        continue
                    try:
                        info = parse_detail(data)
                    except MXError as e:
                        reasons.append(f"{t}: {e}")
                        if MX_DEBUG:
                            _dump_debug(cid, t, data)
                        continue
                    if info["drm"] or info["hls_url"] or info["dash_url"]:
                        return info
                    reasons.append(f"{t}: no HLS stream")
        except asyncio.TimeoutError:
            if attempt < 2 and _px_switch(link, "timeout"):
                reasons.clear()
                continue
            raise MXError("MX Player's API didn't answer in time - try again in a moment.")
        except aiohttp.ClientError as e:
            if attempt < 2 and _px_switch(link, e):
                reasons.clear()
                continue
            raise MXError(f"Couldn't reach MX Player's API ({str(e)[:100]}).")
        if blocked and attempt < 2 and _px_switch(link, f"HTTP {status}"):
            reasons.clear()
            continue          # this IP/proxy is refused -> try again through another free proxy
        break
    raise MXError("MX API: " + "; ".join(reasons))


async def _resolve_key(link: str) -> dict:
    """Key-based third-party resolver (src's primary). Only runs if MXPLAYER_API_KEY is set."""
    if not MXPLAYER_API_KEY:
        raise MXError("key resolver not configured")
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for _ in range(2):
            try:
                async with session.get(KEY_API, params={"url": link, "api_key": MXPLAYER_API_KEY}) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json(content_type=None)
            except Exception:
                continue
            if not data or not data.get("status"):
                raise MXError((data or {}).get("message") or "key resolver returned no data")
            stream = data.get("m3u8_url") or data.get("mpd_url") or ""
            if not stream:
                raise MXError("key resolver returned no stream")
            s_no, e_no = _parse_season_episode(data.get("seo_title") or "")
            return _info(data.get("show_title") or data.get("seo_title") or _slug_title(link),
                         data.get("seo_title"), data.get("season") or s_no,
                         data.get("episode") or e_no, data.get("description"), data.get("thumbnail"),
                         data.get("m3u8_url") or "", data.get("mpd_url") or "", data.get("is_drm"))
    raise MXError("key resolver is not responding")


async def _resolve_page(link: str) -> dict:
    """Last resort: read the public title page and look for an HLS URL in it."""
    timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(link, headers={"User-Agent": _HEADERS["User-Agent"], "Referer": SITE},
                               proxy=_px()) as r:
            if r.status != 200:
                raise MXError(f"title page HTTP {r.status}")
            page = await r.text()
    page = page.replace("\\/", "/")
    m = _M3U8_RE.search(page)
    if not m:
        raise MXError("no HLS link on the title page")
    t = re.search(r'property="og:title"\s+content="([^"]+)"', page)
    title = _html.unescape(t.group(1)) if t else _slug_title(link)
    s_no, e_no = _parse_season_episode(title or "")
    return _info(title, title, s_no, e_no, "", "", m.group(0), "", False)


async def resolve(link: str) -> dict:
    """Link -> normalised info dict with a usable hls_url, or raises MXError with a user-facing reason."""
    reasons = []
    resolvers = [("key", _resolve_key)] if MXPLAYER_API_KEY else []
    resolvers += [("api", _resolve_api), ("page", _resolve_page)]
    info = None
    for name, fn in resolvers:
        try:
            info = await fn(link)
        except MXError as e:
            reasons.append(f"{name}: {e}")
            if name == "api" and "DRM" in str(e):
                break
            continue
        except Exception as e:
            logger.warning(f"mxplayer {name} resolver failed for {link}: {e}")
            reasons.append(f"{name}: {str(e)[:100]}")
            continue
        if info["drm"]:
            raise MXError(f"“{info['title']}” is DRM-protected (Widevine) - it can't be downloaded.")
        if info["hls_url"]:
            return info
        if info["dash_url"]:
            raise MXError(f"“{info['title']}” is only offered as DASH, and only HLS streams are supported.")
    raise MXError("No downloadable stream found. " + " | ".join(reasons))


def _px() -> str | None:
    """Proxy for MX Player requests (own MXPLAYER_PROXY, else a working free proxy), None = direct."""
    try:
        import cf_bypass
        return cf_bypass.proxy_for(SITE)
    except Exception:
        return None


def _px_switch(link: str, err) -> bool:
    """A request through the proxy just failed. True = retry now (a new proxy was picked)."""
    try:
        import free_proxy
        return free_proxy.switch_on_block(link, err)
    except Exception:
        return False


def _file_name(info: dict) -> str:
    name = info["title"]
    season = str(info.get("season") or "")
    episode = str(info.get("episode") or "")
    if season.isdigit():
        name = f"{name} S{int(season):02d}"
        if episode.isdigit():
            name += f"E{int(episode):02d}"
    try:
        import url_uploader as _uu
        return _uu._safe_name(name, "MX_Video")[:60]
    except Exception:
        return re.sub(r"[^\w .()\-]+", "", name).strip()[:60] or "MX_Video"


# ------------------------------------------------------------------- shared with m3u8 / uploader ----
def _m3():
    import m3u8_downloader as m3
    return m3


def _uu():
    import url_uploader as uu
    return uu


_SESSIONS: dict = {}


def _akey(track: dict):
    return (track.get("language") or "", track.get("name") or "")


def _pick_audio(tracks: list, group, sel: set) -> list:
    """The tracks to download for one link: the ticked ones (matched by language+name), else the default track."""
    if not tracks:
        return []
    m3 = _m3()
    if sel:
        picked = [t for t in tracks if _akey(t) in sel]
        if picked:
            return picked
    return [m3.find_audio(tracks, group, "", "")]


def _audio_to_mp3(paths: list, out_path: str):
    async def run():
        m3 = _m3()
        cmd = ["ffmpeg", "-y", "-nostdin", "-loglevel", "error"]
        for p in paths:
            cmd += ["-i", p]
        if len(paths) > 1:
            fc = "".join(f"[{i}:a]" for i in range(len(paths))) + f"amix=inputs={len(paths)}:duration=longest[a]"
            cmd += ["-filter_complex", fc, "-map", "[a]"]
        else:
            cmd += ["-map", "0:a:0"]
        cmd += ["-vn", "-c:a", "libmp3lame", "-b:a", "192k", out_path]
        if not (await m3._ffmpeg(cmd, m3.MUX_TIMEOUT) and os.path.exists(out_path) and os.path.getsize(out_path) > 10000):
            raise _uu().URLError("Couldn't extract audio from that stream.")
        return out_path
    return run()


def _kb_quality(sid, s):
    ns = _ns
    rows = []
    for i, q in enumerate(s["qualities"]):
        label = f"📺 {q['name']}" + (f" • {q['resolution']}" if q.get("resolution") not in (None, "Unknown") else "")
        rows.append([ns.make_button(ns.SC(label), callback_data=f"mxq:{sid}:{i}", style=ns.BTN_PRIMARY)])
    rows.append([ns.make_button(ns.SC("🎵 Audio only (MP3)"), callback_data=f"mxqa:{sid}", style=ns.BTN_PRIMARY)])
    rows.append([ns.make_button(ns.SC("❌ Cancel"), callback_data=f"mxcancel:{sid}", style=ns.BTN_DANGER)])
    return ns.InlineKeyboardMarkup(rows)


def _kb_audio(sid, s):
    ns = _ns
    m3 = _m3()
    rows = []
    for i, t in enumerate(s["audio"]):
        mark = "✅ " if _akey(t) in s["sel_audio"] else ""
        rows.append([ns.make_button(ns.SC(f"{mark}{t['display']}" + (f" ({t['name']})" if t.get("name") else "")),
                                    callback_data=f"mxt:{sid}:{i}", style=ns.BTN_PRIMARY)])
    rows.append([ns.make_button(ns.SC("🚀 Done"), callback_data=f"mxtdone:{sid}", style=ns.BTN_PRIMARY)])
    rows.append([ns.make_button(ns.SC("⏭ Use default track"), callback_data=f"mxtskip:{sid}", style=ns.BTN_PRIMARY)])
    rows.append([ns.make_button(ns.SC("❌ Cancel"), callback_data=f"mxcancel:{sid}", style=ns.BTN_DANGER)])
    return ns.InlineKeyboardMarkup(rows)


async def _edit_any(msg, text, kb=None):
    """Edit a text or photo message (photo cards keep their caption)."""
    try:
        if getattr(msg, "photo", None):
            await msg.edit_caption(_ns.SC(text), reply_markup=kb, parse_mode=enums.ParseMode.HTML)
        else:
            await msg.edit_text(_ns.SC(text), reply_markup=kb, parse_mode=enums.ParseMode.HTML)
    except Exception:
        pass


async def _delete(msg):
    try:
        await msg.delete()
    except Exception:
        pass


# ------------------------------------------------------------------------------------------ flow ------
async def handle(client, message, links: list):
    ns = _ns
    uid = message.from_user.id
    if await ns.is_banned(uid):
        return await message.reply(ns.SC("🚫 You are banned from using this bot."))
    shows = [l for l in links if is_show_link(l)]
    links = [l for l in links if not is_show_link(l)]
    if shows:
        import mx_show_browser
        await mx_show_browser.open_show(client, message, shows[0])
        if not links:
            return

    links = links[:MAX_ITEMS]
    status = await message.reply(ns.SC(f"<b>🔎 Fetching MX Player details ({len(links)} link(s))...</b>"),
                                 parse_mode=enums.ParseMode.HTML)
    items, errors = [], []
    for link in links:
        try:
            info = await resolve(link)
        except MXError as e:
            errors.append(str(e))
        except Exception as e:
            logger.exception(f"mxplayer resolve failed for {link}")
            errors.append(f"Unexpected error: {str(e)[:120]}")
        else:
            items.append({"link": link, "hls_url": info["hls_url"], "info": info})

    problems = "".join(f"\n• {_html.escape(e)}" for e in errors)
    if not items:
        return await _edit_any(status, f"<b>❌ Couldn't get a stream.</b>{problems}")

    # the quality and audio menus come from the first link's playlist
    m3 = _m3()
    try:
        async with m3.Fetcher({"Referer": SITE, "Origin": SITE.rstrip("/")}, proxy=_px()) as f:
            master0 = m3.parse_master(await f.text(items[0]["hls_url"]), items[0]["hls_url"])
    except Exception as e:
        return await _edit_any(status, f"<b>❌ Couldn't read the playlist:</b> <code>{_html.escape(str(e)[:200])}</code>")

    first = items[0]["info"]
    sid = uuid.uuid4().hex[:10]
    s = {"uid": uid, "message": message, "chat_id": message.chat.id, "items": items,
         "qualities": master0["qualities"], "audio": master0["audio_tracks"], "sel_audio": set(),
         "sel_height": None, "audio_only": False, "compress": False, "started": False}
    _SESSIONS[sid] = s
    if len(_SESSIONS) > 200:
        for k in list(_SESSIONS)[:50]:
            _SESSIONS.pop(k, None)

    desc = _html.escape(first["description"][:220] + ("…" if len(first["description"]) > 220 else ""))
    ep = ""
    if first.get("season") or first.get("episode"):
        ep = f"\n<i>Season {first.get('season') or '?'} · Episode {first.get('episode') or '?'}</i>"
    card = (f"<b>🎬 {_html.escape(first['title'])}</b>"
            + (f" <i>(+{len(items) - 1} more)</i>" if len(items) > 1 else "")
            + ep
            + (f"\n\n<i>{desc}</i>" if desc and len(items) == 1 else "")
            + (f"\n\n<b>⚠️ Skipped:</b>{problems}" if errors else "")
            + "\n\n<b>🚀 Step 1: Select quality</b> (or audio only)")
    kb = _kb_quality(sid, s)
    thumb = first.get("thumbnail") if len(items) == 1 else ""
    if thumb:
        try:
            await message.reply_photo(thumb, caption=ns.SC(card), reply_markup=kb, parse_mode=enums.ParseMode.HTML)
            return await _delete(status)
        except Exception:
            pass
    await _edit_any(status, card, kb)


async def _after_choice(client, cq, sid, s):
    """After a quality or audio-only pick: ask for audio tracks if there is a choice, else go on."""
    if s["audio"] and len(s["audio"]) > 1:
        text = ("<b>🚀 Step 2: Select audio track(s)</b>\n\n"
                "Tick one or more tracks, then tap Done. They're merged into one file.")
        return await _edit_any(cq.message, text, _kb_audio(sid, s))
    await _next_step(client, cq, sid, s)


async def _next_step(client, cq, sid, s):
    """Video: ask whether to compress to H.265 first. Audio only: nothing left to ask, start."""
    if s["audio_only"]:
        return await _start(client, cq, sid, s)
    text = ("<b>🚀 Step 3: Compress?</b>\n\n"
            "🗜 <b>Compress</b> re-encodes to H.265: smaller file, much slower "
            f"(capped at {_m3().COMPRESS_TIMEOUT // 60} min, falls back to the original).\n"
            "⚡ <b>Skip</b> uploads the stream as-is, faster.")
    ns = _ns
    kb = ns.InlineKeyboardMarkup([
        [ns.make_button(ns.SC("⚡ Skip, original quality (faster)"), callback_data=f"mxc:{sid}:no", style=ns.BTN_PRIMARY)],
        [ns.make_button(ns.SC("🗜 Compress (H.265, smaller)"), callback_data=f"mxc:{sid}:yes", style=ns.BTN_PRIMARY)],
        [ns.make_button(ns.SC("❌ Cancel"), callback_data=f"mxcancel:{sid}", style=ns.BTN_DANGER)],
    ])
    await _edit_any(cq.message, text, kb)


async def _start(client, cq, sid, s):
    ns = _ns
    if s["started"]:
        return
    s["started"] = True
    await _edit_any(cq.message, "<b>⏳ Queued...</b>\n<i>/cancel stops it.</i>", None)
    asyncio.create_task(_run(client, sid))


async def _run(client, sid):
    ns = _ns
    s = _SESSIONS.get(sid)
    if not s:
        return
    m3 = _m3()
    uid, chat_id = s["uid"], s["chat_id"]
    import dl_queue
    try:
        rec = await dl_queue.acquire(client, uid, chat_id, "mxplayer", s["items"][0]["info"]["title"])
    except dl_queue.QueueCancelled:
        _SESSIONS.pop(sid, None)
        return
    task = asyncio.current_task()
    ns.ACTIVE_TASKS[uid] = task
    try:
        async with m3._sem():
            total = len(s["items"])
            for li, item in enumerate(s["items"], start=1):
                if not await m3._within_limit(client, uid, ns):
                    break
                tag = f"[{li}/{total}] " if total > 1 else ""
                status = await client.send_message(
                    chat_id, ns.SC(f"<b>📥 {tag}{_html.escape(item['info']['title'])}</b>\n"
                                   f"<i>Reading playlist... (/cancel to stop)</i>"),
                    parse_mode=enums.ParseMode.HTML)
                folder = os.path.join(ns.DOWNLOAD_DIR, f"mx_{uid}_{uuid.uuid4().hex[:8]}")
                os.makedirs(folder, exist_ok=True)
                try:
                    await _one_retry(client, status, folder, s, item)
                except asyncio.CancelledError:
                    raise
                except _uu().URLError as e:
                    await _edit_any(status, f"<b>❌ {tag}{_html.escape(item['info']['title'])}:</b> {_html.escape(str(e))}")
                except Exception as e:
                    logger.exception("mxplayer download failed")
                    await _edit_any(status, f"<b>❌ {tag}failed:</b>\n<code>{_html.escape(str(e)[:200])}</code>")
                finally:
                    shutil.rmtree(folder, ignore_errors=True)
    except asyncio.CancelledError:
        try:
            await client.send_message(chat_id, ns.SC("<b>🛑 Cancelled.</b>"), parse_mode=enums.ParseMode.HTML)
        except Exception:
            pass
        raise
    finally:
        if ns.ACTIVE_TASKS.get(uid) is task:
            ns.ACTIVE_TASKS.pop(uid, None)
        _SESSIONS.pop(sid, None)
        await dl_queue.release(rec)


async def _one_retry(client, status, folder, s, item):
    """_one through the free proxy; if the proxy dies or gets blocked mid-download, switch proxy and redo the item."""
    for attempt in range(3):
        try:
            return await _one(client, status, folder, s, item)
        except (asyncio.CancelledError, _uu().URLError):
            raise
        except Exception as e:
            if attempt < 2 and _px_switch(item["link"], e):
                logger.warning(f"mxplayer: proxy failed during download ({str(e)[:100]}) - retrying through another")
                continue
            raise


async def _one(client, status, folder, s, item):
    """Download, mux/convert and upload ONE link with the user's choices."""
    ns = _ns
    m3 = _m3()
    uu = _uu()
    info = item["info"]
    uid, message = s["uid"], s["message"]
    deadline = time.time() + m3.TASK_TIMEOUT
    t0 = time.time()
    base = _file_name(info)
    extra = []
    if info.get("season") or info.get("episode"):
        extra.append(f"📦 <b>Season:</b> {info.get('season') or 'N/A'} · 📺 <b>Episode:</b> {info.get('episode') or 'N/A'}")

    async with m3.Fetcher({"Referer": SITE, "Origin": SITE.rstrip("/")}, proxy=_px()) as f:
        master = m3.parse_master(await f.text(item["hls_url"]), item["hls_url"])
        tracks = master["audio_tracks"]

        async def prog_for(label):
            tracker = ns.ProgressTracker(status, "Downloading", base, quality=label)

            async def prog(done, total, segs_done, segs_total):
                await tracker.update(done, total)
            return prog

        if s["audio_only"]:
            chosen = _pick_audio(tracks, None, s["sel_audio"])
            if chosen:
                paths = []
                for t in chosen:
                    t["_lang3"] = (t.get("language") or "und")[:3].lower() or "und"
                    t["_label"] = t.get("display") or "Audio"
                    p = await m3.download_stream(f, t["url"], os.path.join(folder, f"a{len(paths)}"), "audio",
                                                 await prog_for("MP3"), deadline)
                    paths.append(p)
            else:
                # MX gave no separate audio rendition: take the lowest quality and extract its audio
                q = master["qualities"][0]
                paths = [await m3.download_stream(f, q["url"], os.path.join(folder, "v"), "video",
                                                  await prog_for("MP3"), deadline)]
            out_name = base + ".mp3"
            out_path = os.path.join(folder, out_name)
            await _edit_any(status, f"<b>🔧 Converting to MP3...</b>\n<code>{_html.escape(out_name)}</code>")
            await _audio_to_mp3(paths, out_path)
            ext, is_video, quality_label = ".mp3", False, "MP3 (Audio)"
            audio_names = [t.get("display") for t in chosen] if chosen else []
        else:
            q = m3.find_quality(master["qualities"], s["sel_height"])
            video = await m3.download_stream(f, q["url"], os.path.join(folder, "v"), "video",
                                             await prog_for(q["name"]), deadline)
            chosen = _pick_audio(tracks, q.get("group"), s["sel_audio"]) if tracks else []
            audios = []
            for t in chosen:
                t["_lang3"] = (t.get("language") or "und")[:3].lower() or "und"
                t["_label"] = t.get("display") or "Audio"
                try:
                    audios.append({**t, "_path": await m3.download_stream(
                        f, t["url"], os.path.join(folder, f"a{len(audios)}"), "audio", await prog_for(q["name"]), deadline)})
                except (uu.URLError, asyncio.CancelledError):
                    raise
                except Exception as e:
                    logger.warning("audio track failed (%s) - continuing without it", e)
            import user_settings as _us
            fmt = await _us.get_output_format(uid)          # /settings -> Output format
            out_name = base + "." + fmt
            out_path = os.path.join(folder, out_name)
            await _edit_any(status, f"<b>🔧 Joining video{' + audio' if audios else ''}...</b>\n<code>{_html.escape(out_name)}</code>")
            await _mux_videos(video, audios, out_path)
            ext, is_video, quality_label = "." + fmt, True, q["name"]
            audio_names = [a.get("display") for a in audios]
            for p in [video] + [a["_path"] for a in audios]:
                try:
                    os.remove(p)
                except OSError:
                    pass
            if s.get("compress"):
                await _edit_any(status, f"<b>🗜 Compressing to H.265...</b>\n<i>this can take a long time (/cancel to stop)</i>")
                try:
                    hevc = await m3.compress_hevc(out_path)
                    os.remove(out_path)
                    out_path = hevc
                    if fmt == "mkv":
                        out_path = await m3.remux_mkv(out_path)
                    quality_label = f"{quality_label} • H.265"
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    await client.send_message(s["chat_id"], ns.SC(
                        f"<b>⚠️ Compression skipped:</b> {_html.escape(str(e))}\n<i>Uploading the original instead.</i>"),
                        parse_mode=enums.ParseMode.HTML)

    if audio_names:
        extra.append("🔊 <b>Audio:</b> " + ", ".join(n for n in audio_names if n))

    size = os.path.getsize(out_path)

    dl_seconds = time.time() - t0
    await uu._upload(client, message, status, out_path, os.path.basename(out_path), size, is_video, ext,
                     item["link"], dl_seconds, uid,
                     quality_label=quality_label, extra_caption="\n".join(extra))

    try:
        from database import add_download_history
        await add_download_history(uid, {
            "title": info["title"], "season": info.get("season") or "", "episode": info.get("episode") or "",
            "url": item["link"], "type": "audio" if not is_video else "video",
            "quality": quality_label, "audio": audio_names,
        })
    except Exception as e:
        logger.warning(f"download history not saved: {e}")


async def _mux_videos(video: str, audios: list, out_path: str):
    """Video + audio renditions (dicts with _path, _lang3, _label) -> mp4, one audio stream per track."""
    m3 = _m3()
    if len(audios) <= 1:
        return await m3.mux(video, audios[0]["_path"] if audios else None, out_path)
    cmd = ["ffmpeg", "-y", "-nostdin", "-loglevel", "error", "-fflags", "+genpts", "-i", video]
    for a in audios:
        cmd += ["-i", a["_path"]]
    cmd += ["-map", "0:v:0"]
    for i in range(len(audios)):
        cmd += ["-map", f"{i + 1}:a:0"]
    for i, a in enumerate(audios):
        cmd += [f"-metadata:s:a:{i}", f"language={a['_lang3']}", f"-metadata:s:a:{i}", f"title={a['_label']}"]
    cmd += ["-c:v", "copy", "-c:a", "aac"] + (["-movflags", "+faststart"] if out_path.lower().endswith(".mp4") else []) + [out_path]
    if await m3._ffmpeg(cmd, m3.MUX_TIMEOUT) and os.path.exists(out_path) and os.path.getsize(out_path) > 10000:
        return out_path
    # multi-audio mux failed: fall back to the first track so the user still gets a file
    return await m3.mux(video, audios[0]["_path"], out_path)


async def _edit(msg, text):
    await _edit_any(msg, text)


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

    import mx_show_browser
    mx_show_browser.setup(app, ns)

    def _owned(cq, sid):
        s = _SESSIONS.get(sid)
        return s if s and s["uid"] == cq.from_user.id else None

    @app.on_message(filters.command(["mxplayer", "mx"]) & filters.private)
    async def mxplayer_cmd(client, message):
        parts = (message.text or "").split(None, 1)
        links = extract_mxplayer_links(parts[1]) if len(parts) > 1 else []
        if not links:
            return await message.reply(ns.SC(
                "<b>🎬 MX Player Downloader</b>\n\n<b>Usage:</b>\n<code>/mxplayer &lt;mxplayer.in link&gt;</code>\n"
                "<code>/mxplayer link1 link2</code> (batch)\n"
                "<code>/mxplayer &lt;show page link&gt;</code> (pick episodes from a show)\n\n"
                "Pick quality or audio only, tick the audio track(s), optionally compress to H.265, then I upload the file.\n"
                "<i>You can also just paste the link. DRM-protected titles can't be downloaded. "
                "/cancel stops a running download.</i>"), parse_mode=enums.ParseMode.HTML)
        await handle(client, message, links)

    @app.on_callback_query(filters.regex(r"^mxq:([a-f0-9]+):(\d+)$"))
    async def _q(client, cq):
        sid, idx = cq.matches[0].group(1), int(cq.matches[0].group(2))
        s = _owned(cq, sid)
        if not s:
            return await cq.answer("Expired - send the link again.", show_alert=True)
        try:
            s["sel_height"] = s["qualities"][idx]["height"]
        except IndexError:
            return await cq.answer("That button is stale - send the link again.", show_alert=True)
        s["audio_only"] = False
        await cq.answer("Quality selected.")
        await _after_choice(client, cq, sid, s)

    @app.on_callback_query(filters.regex(r"^mxqa:([a-f0-9]+)$"))
    async def _qa(client, cq):
        sid = cq.matches[0].group(1)
        s = _owned(cq, sid)
        if not s:
            return await cq.answer("Expired - send the link again.", show_alert=True)
        s["audio_only"] = True
        s["sel_height"] = None
        await cq.answer("Audio only.")
        await _after_choice(client, cq, sid, s)

    @app.on_callback_query(filters.regex(r"^mxt:([a-f0-9]+):(\d+)$"))
    async def _t(client, cq):
        sid, idx = cq.matches[0].group(1), int(cq.matches[0].group(2))
        s = _owned(cq, sid)
        if not s:
            return await cq.answer("Expired - send the link again.", show_alert=True)
        try:
            key = _akey(s["audio"][idx])
        except IndexError:
            return await cq.answer("That button is stale - send the link again.", show_alert=True)
        (s["sel_audio"].discard if key in s["sel_audio"] else s["sel_audio"].add)(key)
        await cq.answer("Track toggled.")
        try:
            await cq.message.edit_reply_markup(_kb_audio(sid, s))
        except Exception:
            pass

    @app.on_callback_query(filters.regex(r"^mxtdone:([a-f0-9]+)$"))
    async def _tdone(client, cq):
        sid = cq.matches[0].group(1)
        s = _owned(cq, sid)
        if not s:
            return await cq.answer("Expired - send the link again.", show_alert=True)
        await cq.answer("Next...")
        await _next_step(client, cq, sid, s)

    @app.on_callback_query(filters.regex(r"^mxtskip:([a-f0-9]+)$"))
    async def _tskip(client, cq):
        sid = cq.matches[0].group(1)
        s = _owned(cq, sid)
        if not s:
            return await cq.answer("Expired - send the link again.", show_alert=True)
        s["sel_audio"] = set()
        await cq.answer("Using default track.")
        await _next_step(client, cq, sid, s)

    @app.on_callback_query(filters.regex(r"^mxc:([a-f0-9]+):(yes|no)$"))
    async def _compress_pick(client, cq):
        sid, choice = cq.matches[0].group(1), cq.matches[0].group(2)
        s = _owned(cq, sid)
        if not s:
            return await cq.answer("Expired - send the link again.", show_alert=True)
        s["compress"] = choice == "yes"
        await cq.answer("Compression on." if s["compress"] else "Original quality.")
        await _start(client, cq, sid, s)

    @app.on_callback_query(filters.regex(r"^mxcancel:([a-f0-9]+)$"))
    async def _cancel(client, cq):
        _SESSIONS.pop(cq.matches[0].group(1), None)
        await _edit_any(cq.message, "❌ Cancelled.", None)
        await cq.answer()

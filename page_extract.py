"""Auto-detect media on a pasted web page link.

When someone pastes a link that no dedicated downloader and no yt-dlp extractor claims, and that link is a web page
rather than a file, the bot scans the page and shows what it found as buttons:

  1. The HTML and inline scripts are scanned (page_media.py): HLS (.m3u8), DASH (.mpd), direct video / audio / file
     links, relative paths inside JS player configs, and embedded players (<iframe>).
  2. If the HTML has no playable stream but the page looks like it has a player (a <video>, an <iframe>, a JS
     player library, a stream URL in the scripts), the page is opened in a real Chrome through browser_sniff, and the
     streams its own player requests are added. This is generic, not tied to particular sites. BROWSER_SNIFF=0 turns
     the browser step off.
  3. Tapping a button downloads that link through the same pipeline as /url and pasted links (daily limit, queue,
     /cancel, upload settings). HLS opens the quality menu, DASH is merged with ffmpeg, and an embedded player is
     scanned one level deeper (at most 2 levels).

If a page has no media at all, maybe_handle() returns False and the caller keeps its usual behaviour, so nothing is
shown for plain articles. Only the person who pasted the link can use its buttons. A list expires after 30 minutes.
"""
import asyncio
import html
import logging
import os
import re
import time
import uuid
from urllib.parse import urlsplit

import aiohttp
from pyrogram import enums, filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import InlineKeyboardMarkup

import browser_sniff
import page_media as pm
import url_uploader

logger = logging.getLogger("page_extract")

_ns = None                                              # main.py's namespace (SC, make_button, ...)

SCAN_TIMEOUT = 150                                      # whole scan, browser included
BROWSER_TIMEOUT = int(os.environ.get("PAGE_EXTRACT_BROWSER_TIMEOUT", "40") or 40)
MAX_PAGE_BYTES = 6 * 1024 * 1024
MAX_LINKS = 40                                          # kept after sorting
MAX_BUTTONS = 20                                        # shown as buttons
MAX_DEPTH = 2                                           # embedded player -> embedded player, at most 2 levels
STORE_TTL = 1800

_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"]+")

_STORE: dict = {}                                       # token -> {uid, orig, page, links, depth, ts}


# ------------------------------------------------------------------ fetching ----

class _FetchBlocked(Exception):
    """Plain HTTP could not get the page (HTTP error, bot wall, timeout, connection refused). A real browser may still."""


async def _fetch_page(url: str):
    """-> (final_url, content_type, text). Raises _FetchBlocked when plain HTTP fails, URLError when the link is refused."""
    await url_uploader._check_public(url)           # private / internal addresses: refused, never sent to a browser
    headers = {
        "User-Agent": url_uploader.UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.8",
    }
    timeout = aiohttp.ClientTimeout(total=25, sock_connect=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(url, headers=headers, allow_redirects=True, max_redirects=8) as r:
                if r.status >= 400:
                    raise _FetchBlocked(f"The page answered HTTP {r.status}.")
                final = str(r.url)
                await url_uploader._check_public(final)
                ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                buf = bytearray()
                async for chunk in r.content.iter_chunked(64 * 1024):
                    buf.extend(chunk)
                    if len(buf) >= MAX_PAGE_BYTES:
                        break
    except (url_uploader.URLError, _FetchBlocked):
        raise
    except asyncio.TimeoutError:
        raise _FetchBlocked("The page took too long to answer.")
    except aiohttp.ClientError as e:
        raise _FetchBlocked(f"Couldn't open the page: {str(e)[:150]}")
    return final, ctype, bytes(buf).decode("utf-8", errors="replace")


# ------------------------------------------------------------------- scanning ----

async def _browser_items(url: str):
    """Open the page in Chrome; -> (streams its own player requested, note-or-None)."""
    if not browser_sniff.enabled():
        return [], None
    try:
        data = await asyncio.get_running_loop().run_in_executor(
            None, lambda: browser_sniff.sniff_media(url, timeout=BROWSER_TIMEOUT))
    except Exception as e:
        logger.info(f"[auto-detect] browser scan failed for {url}: {e}")
        return [], str(e)[:160]
    hdrs = {"Referer": url}
    if data.get("ua"):
        hdrs["User-Agent"] = data["ua"]
    if data.get("cookie"):
        hdrs["Cookie"] = data["cookie"]
    items = [{
        "url": s["url"],
        "kind": "hls" if s["kind"] == "hls" else "video",
        "height": int(s.get("height") or 0),
        "headers": hdrs,
    } for s in data.get("streams", [])]
    return items, None


async def _scan(url: str) -> dict:
    """-> {"direct": url} for a file link, else {"page", "title", "links", "browser_used", "browser_note"}.
    Raises url_uploader.URLError when neither plain HTTP nor the browser could get the page."""
    fetch_err = None
    try:
        final, ctype, text = await _fetch_page(url)
    except _FetchBlocked as e:
        fetch_err, final, ctype, text = str(e), url, "", ""

    if fetch_err is None and ctype and not ("html" in ctype or "xml" in ctype or ctype.startswith("text/")):
        return {"direct": final}

    items = pm.extract_candidates(text, final) if text else []
    browser_used, browser_note = False, None
    if not any(i["kind"] in ("hls", "dash", "video") for i in items):
        b_items, browser_note = await _browser_items(final)     # the page's own player, in a real Chrome
        if b_items:
            browser_used = True
            items = pm.merge(b_items, items)                    # real player requests first

    if fetch_err and not browser_used:
        raise url_uploader.URLError(fetch_err + (f" Browser: {browser_note}" if browser_note else ""))

    for it in items:
        it.setdefault("headers", {"Referer": final})
    return {
        "page": final,
        "title": pm.page_title(text),
        "links": pm.sort_candidates(items)[:MAX_LINKS],
        "browser_used": browser_used,
        "browser_note": browser_note,
    }


# ------------------------------------------------------------------ UI / state ----

def _put(entry: dict) -> str:
    now = time.time()
    for k in [k for k, v in _STORE.items() if now - v["ts"] > STORE_TTL]:
        _STORE.pop(k, None)
    if len(_STORE) > 300:
        for k in list(_STORE)[:100]:
            _STORE.pop(k, None)
    token = uuid.uuid4().hex[:10]
    _STORE[token] = {**entry, "ts": now}
    return token


def _render(token: str, res: dict, depth: int):
    links = res["links"]
    shown = links[:MAX_BUTTONS]
    host = html.escape(urlsplit(res["page"]).hostname or "")
    lines = [f"🔍 <b>Found {len(links)} media link(s)</b>", f"🌐 <code>{host}</code>"]
    if res.get("title"):
        lines.append(f"📝 {html.escape(res['title'])}")
    if res.get("browser_used"):
        lines.append("🧭 <i>Streams read from the page's own player (browser)</i>")
    if depth:
        lines.append(f"↳ <i>embedded player, level {depth}</i>")
    lines.append("")
    for i, c in enumerate(shown, 1):
        lines.append(f"<b>{i}.</b> {html.escape(pm.display_label(c))} · "
                     f"<code>{html.escape(pm.short_name(c['url']))}</code>")
    if len(links) > len(shown):
        lines.append(f"<i>…and {len(links) - len(shown)} more (showing the best {len(shown)}).</i>")
    lines.append("\nTap a number to download it.")

    ns = _ns
    rows, row = [], []
    for i, c in enumerate(shown):
        row.append(ns.make_button(f"{i + 1}. {pm.display_label(c)[:22]}",
                                  callback_data=f"pex|{token}|{i}", style=ns.BTN_PRIMARY))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([ns.make_button("❌ Close", callback_data=f"pex|{token}|x", style=ns.BTN_DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def _safe_edit(msg, text: str, markup=None):
    try:
        await msg.edit(text, parse_mode=enums.ParseMode.HTML, reply_markup=markup)
    except MessageNotModified:
        pass
    except Exception as e:
        logger.warning(f"[auto-detect] couldn't edit status: {e}")


async def _show_scan(client, orig, status, url: str, depth: int) -> bool:
    """Scan `url` and show what was found on `status`. Always answers, so a pasted link never gets silence."""
    try:
        res = await asyncio.wait_for(_scan(url), SCAN_TIMEOUT)
    except url_uploader.URLError as e:
        await _safe_edit(status, f"❌ {html.escape(str(e))}")
        return True
    except asyncio.TimeoutError:
        await _safe_edit(status, "❌ The scan took too long. Try again, or send the video link itself.")
        return True
    except Exception as e:
        logger.exception(f"[auto-detect] scan failed for {url!r}: {e}")
        await _safe_edit(status, f"❌ Scan failed: <code>{html.escape(str(e)[:200])}</code>")
        return True

    if res.get("direct"):
        await _safe_edit(status, "📎 That link is already a file, downloading it…")
        await url_uploader.handle_url(client, orig, res["direct"], http_headers={"Referer": url})
        return True

    if not res["links"]:
        msg = "❌ No playable media found on this page."
        if res.get("browser_note"):
            msg += f"\n<i>Browser scan: {html.escape(res['browser_note'])}</i>"
        await _safe_edit(status, msg)
        return True

    token = _put({"uid": orig.from_user.id, "orig": orig, "page": res["page"],
                  "links": res["links"], "depth": depth})
    text, markup = _render(token, res, depth)
    await _safe_edit(status, text, markup)
    return True


async def _pick(client, ent: dict, link: dict, status):
    if link["kind"] == "page":
        if ent["depth"] >= MAX_DEPTH:
            return await _safe_edit(status, "⚠️ Too many nested players. Open that embed link in your browser instead.")
        return await _show_scan(client, ent["orig"], status, link["url"], ent["depth"] + 1)

    await _safe_edit(status, "⬇️ Starting download…")
    # url_uploader routes HLS to the quality menu, DASH to the ffmpeg merge, everything else to the normal
    # download + upload path, all with the page's referer (and cookies / UA when the browser read them).
    await url_uploader.handle_url(client, ent["orig"], link["url"], http_headers=dict(link.get("headers") or {}))


# ------------------------------------------------------------------ entry points ----

async def fallback_scan(client, orig, status, url: str) -> bool:
    """Used by main when a generic link's own downloader failed: scan the page instead, same as a pasted link."""
    return await _show_scan(client, orig, status, url, 0)


async def maybe_handle(client, message, text: str) -> bool:
    """Called by main's link_handler for text that no dedicated downloader claimed. True = handled here.

    A link that is already a media/file URL (by its extension) is left to url_uploader, which downloads it."""
    found = _URL_IN_TEXT.search(text or "")
    if not found:
        return False
    url = found.group(0).rstrip(".,;)")
    if pm.kind_of(url):
        return False
    status = await message.reply("🔍 <b>Scanning this page for media…</b>",
                                 parse_mode=enums.ParseMode.HTML)
    return await _show_scan(client, message, status, url, 0)


def setup(app, ns) -> None:
    """Registers the button callback. `ns` is main.py's module (gives make_button / BTN_* )."""
    global _ns
    _ns = ns

    @app.on_callback_query(filters.regex(r"^pex\|"))
    async def extract_pick(client, cb):
        parts = (cb.data or "").split("|")
        if len(parts) != 3:
            return await cb.answer("Invalid choice.", show_alert=True)
        ent = _STORE.get(parts[1])
        if not ent or time.time() - ent["ts"] > STORE_TTL:
            _STORE.pop(parts[1], None)
            return await cb.answer("This list has expired. Send the link again.", show_alert=True)
        if cb.from_user.id != ent["uid"]:
            return await cb.answer("Only the person who sent this link can pick from this list.", show_alert=True)
        if cb.message is None:
            return await cb.answer("Send the link again.", show_alert=True)
        if parts[2] == "x":
            _STORE.pop(parts[1], None)
            await cb.answer()
            return await _safe_edit(cb.message, "❌ Closed.")
        try:
            link = ent["links"][int(parts[2])]
        except (ValueError, IndexError):
            return await cb.answer("This link is no longer in the list.", show_alert=True)
        await cb.answer("Starting…")
        await _pick(client, ent, link, cb.message)

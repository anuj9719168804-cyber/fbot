"""MX Player show browser for fbot.

Paste a show page (https://www.mxplayer.in/show/<slug>, no episode in it) and the bot lists its seasons and episodes.
Tick the episodes you want (Select all / Clear, paged), tap Download, and they go through the SAME quality / audio /
compress menus and download pipeline as a single MX link (mxplayer_downloader.handle), applied to the whole selection.

Ported from mxdlbot-main's services/mx_scraper.py (season/episode extraction) and plugins/browse.py (the UI).
The extraction is heuristic like mxdlbot's: it reads the page's __NEXT_DATA__ JSON if present, otherwise the
/show/<slug>/season-N/... episode links in the HTML. It could not be checked against live MX pages from here.

Limits: at most MX_MAX_ITEMS episodes per batch (the rest of a larger selection is dropped and the user is told).
"""
import json
import logging
import os
import re
from urllib.parse import urljoin, urlparse, unquote

import aiohttp
from pyrogram import filters, enums

logger = logging.getLogger("mx_show_browser")

SITE = "https://www.mxplayer.in/"
SEASONS_PER_PAGE = 8      # buttons on the season screen (2 per row)
EPISODES_PER_PAGE = 6     # episode buttons per page
MAX_BATCH = int(os.environ.get("MX_MAX_ITEMS", "10"))
_HEX_RE = re.compile(r"^[a-f0-9]{8,}$")
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": SITE,
}

_ns = None
_STATE: dict = {}   # user_id -> browser state


class ShowError(Exception):
    """A problem worth showing to the user as-is."""


# ----------------------------------------------------------------------------- link checks ----
def _path_parts(url: str) -> list:
    try:
        return [p for p in unquote(urlparse(url).path).split("/") if p]
    except Exception:
        return []


def _host_ok(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
    except Exception:
        return False
    return host == "mxplayer.in" or host.endswith(".mxplayer.in") or host == "mxplay.com" or host.endswith(".mxplay.com")


def is_show_link(url: str) -> bool:
    """/show/<slug> (no episode id at the end of the path) -> a show page to browse."""
    if not _host_ok(url):
        return False
    parts = _path_parts(url)
    if len(parts) < 2 or parts[0].lower() != "show":
        return False
    last = parts[-1].rsplit("-", 1)[-1].lower()
    return not _HEX_RE.match(last)


def show_slug(url: str) -> str:
    parts = _path_parts(url)
    return parts[1] if len(parts) >= 2 else ""


# ----------------------------------------------------------------------------- scraping ----
async def _fetch_html(url: str) -> str:
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=_HEADERS) as r:
            if r.status != 200:
                raise ShowError(f"The show page returned HTTP {r.status}.")
            return await r.text()


def _find_episode_dicts(node, out: list, season=None):
    """Walk any JSON and collect episode dicts (those with episodeNumber), each tagged with the season
    number of its nearest parent season (episodes usually don't carry their own season number)."""
    if isinstance(node, dict):
        if "episodeNumber" in node:
            ep = dict(node)
            if season is not None and not ep.get("seasonNumber"):
                ep["seasonNumber"] = season
            out.append(ep)
            return
        here = season
        if isinstance(node.get("seasonNumber"), (int, str)):
            here = node.get("seasonNumber")
        elif isinstance(node.get("season"), dict) and node["season"].get("seasonNo") is not None:
            here = node["season"].get("seasonNo")
        for v in node.values():
            _find_episode_dicts(v, out, here)
    elif isinstance(node, list):
        for v in node:
            _find_episode_dicts(v, out, season)


def _episode_link(slug: str, season: int, ep: dict):
    url = ep.get("url") or ""
    if url:
        full = url if url.startswith("http") else urljoin(SITE, url.lstrip("/"))
        if _host_ok(full):
            return full
    cid = str(ep.get("id") or ep.get("contentId") or "").lower()
    if _HEX_RE.match(cid) and slug:
        # the same shape as a real MX episode link, so mxplayer_downloader.parse_link can read the id
        return f"{SITE}show/{slug}/season-{season}/episode-{ep.get('episodeNumber')}-{cid}"
    return None


def _from_next_data(html: str, slug: str) -> list:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    eps: list = []
    _find_episode_dicts(data, eps)
    seasons: dict = {}
    for ep in eps:
        try:
            num = int(ep.get("episodeNumber") or 0)
            season_obj = ep.get("season") if isinstance(ep.get("season"), dict) else {}
            sno = int(ep.get("seasonNumber") or season_obj.get("seasonNo") or 1)
        except (TypeError, ValueError):
            continue
        link = _episode_link(slug, sno, ep)
        if not link or num <= 0:
            continue
        bucket = seasons.setdefault(sno, {})
        bucket.setdefault(num, {"number": num, "title": str(ep.get("title") or ep.get("name") or f"Episode {num}"),
                                "link": link})
    return _to_list(seasons)


def _from_html(html: str, slug: str) -> list:
    pat = r'href="((?:https?://(?:www\.)?mxplayer\.in)?/show/[^"]+/season-(\d+)/[^"]*?-([a-f0-9]{8,}))"'
    seasons: dict = {}
    for href, sno, _cid in re.findall(pat, html):
        link = href if href.startswith("http") else urljoin(SITE, href)
        sno = int(sno)
        lst = seasons.setdefault(sno, {})
        if link not in {e["link"] for e in lst.values()}:
            num = len(lst) + 1
            lst[num] = {"number": num, "title": f"Episode {num}", "link": link}
    return _to_list(seasons)


def _to_list(seasons: dict) -> list:
    return [{"number": sno, "episodes": [eps[k] for k in sorted(eps)]} for sno, eps in sorted(seasons.items()) if eps]


async def fetch_show(url: str) -> dict:
    """-> {"title", "image", "seasons": [{"number", "episodes": [{"number", "title", "link"}]}]}"""
    html = await _fetch_html(url)
    slug = show_slug(url)
    t = re.search(r'property="og:title"\s+content="([^"]+)"', html)
    im = re.search(r'property="og:image"\s+content="([^"]+)"', html)
    title = (t.group(1) if t else slug.replace("-", " ").title()) or "MX Show"
    seasons = _from_next_data(html, slug) or _from_html(html, slug)
    if not seasons:
        raise ShowError("Couldn't find the episodes on that show page. Try sending one episode's link instead.")
    return {"title": title, "image": im.group(1) if im else "", "seasons": seasons}


# ----------------------------------------------------------------------------- UI ----
def _kb(rows):
    return _ns.InlineKeyboardMarkup(rows)


def _btn(text, cb, danger=False):
    return _ns.make_button(_ns.SC(text), callback_data=cb, style=(_ns.BTN_DANGER if danger else _ns.BTN_PRIMARY))


def _total_selected(st) -> int:
    return sum(len(v) for v in st["selected"].values())


def _season_screen(uid, st):
    seasons = st["seasons"]
    page = st["seasons_page"]
    chunk = seasons[page * SEASONS_PER_PAGE:(page + 1) * SEASONS_PER_PAGE]
    rows, row = [], []
    for s in chunk:
        sel = len(st["selected"].get(s["number"], set()))
        label = f"S{s['number']} ({len(s['episodes'])} ep" + (f", {sel} ✓)" if sel else ")")
        row.append(_btn(label, f"sbs:{s['number']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    nav = []
    if page > 0:
        nav.append(_btn("⬅️ Prev", f"sbsp:{page - 1}"))
    if (page + 1) * SEASONS_PER_PAGE < len(seasons):
        nav.append(_btn("Next ➡️", f"sbsp:{page + 1}"))
    if nav:
        rows.append(nav)
    if _total_selected(st):
        rows.append([_btn(f"⬇️ Download selected ({_total_selected(st)})", "sbdl")])
    rows.append([_btn("❌ Cancel", "sbcancel", danger=True)])
    n_eps = sum(len(s["episodes"]) for s in seasons)
    text = (f"📺 <b>{_html_esc(st['title'])}</b>\n"
            f"Seasons: {len(seasons)} · Episodes: {n_eps}\n\n"
            "Select a season to pick episodes.")
    return text, _kb(rows)


def _episode_screen(uid, st):
    snum = st["season"]
    season = next(s for s in st["seasons"] if s["number"] == snum)
    eps = season["episodes"]
    page = st["ep_page"]
    chunk = eps[page * EPISODES_PER_PAGE:(page + 1) * EPISODES_PER_PAGE]
    selected = st["selected"].get(snum, set())
    rows = []
    for e in chunk:
        check = "✓ " if e["number"] in selected else ""
        rows.append([_btn(f"{check}Ep {e['number']}", f"sbe:{snum}:{e['number']}")])
    rows.append([_btn("Select All", f"sbsa:{snum}"), _btn("Clear", f"sbcl:{snum}")])
    pages = (len(eps) + EPISODES_PER_PAGE - 1) // EPISODES_PER_PAGE
    nav = []
    if page > 0:
        nav.append(_btn("⬅️ Prev", f"sbep:{snum}:{page - 1}"))
    nav.append(_btn(f"📄 {page + 1}/{pages}", "sbnoop"))
    if (page + 1) * EPISODES_PER_PAGE < len(eps):
        nav.append(_btn("Next ➡️", f"sbep:{snum}:{page + 1}"))
    rows.append(nav)
    action = [_btn("⬅️ Seasons", "sbback")]
    if _total_selected(st):
        action.append(_btn(f"⬇️ Download selected ({_total_selected(st)})", "sbdl"))
    rows.append(action)
    rows.append([_btn("❌ Cancel", "sbcancel", danger=True)])
    text = (f"📺 <b>{_html_esc(st['title'])}</b>\n📂 <b>Season {snum}</b> · {len(eps)} episodes\n\n"
            "Tap episodes to select, then Download.")
    return text, _kb(rows)


def _html_esc(s: str) -> str:
    import html as _h
    return _h.escape(s or "")


async def _render(msg, uid):
    st = _STATE.get(uid)
    if not st:
        return
    text, kb = _episode_screen(uid, st) if st["screen"] == "episodes" else _season_screen(uid, st)
    try:
        if getattr(msg, "photo", None):
            await msg.edit_caption(_ns.SC(text), reply_markup=kb, parse_mode=enums.ParseMode.HTML)
        else:
            await msg.edit_text(_ns.SC(text), reply_markup=kb, parse_mode=enums.ParseMode.HTML)
    except Exception:
        pass


async def open_show(client, message, url: str):
    """Entry point: a show link was pasted. Shows the season picker."""
    ns = _ns
    uid = message.from_user.id
    status = await message.reply(ns.SC("<b>🔎 Fetching the show...</b>"), parse_mode=enums.ParseMode.HTML)
    try:
        show = await fetch_show(url)
    except ShowError as e:
        return await _safe_edit(status, f"<b>❌ {_html_esc(str(e))}</b>")
    except Exception as e:
        logger.exception("show fetch failed")
        return await _safe_edit(status, f"<b>❌ Couldn't read the show page:</b> <code>{_html_esc(str(e)[:150])}</code>")

    _STATE[uid] = {"title": show["title"], "seasons": show["seasons"], "selected": {}, "screen": "seasons",
                   "season": None, "seasons_page": 0, "ep_page": 0, "message": message}
    text, kb = _season_screen(uid, _STATE[uid])
    if show.get("image"):
        try:
            sent = await message.reply_photo(show["image"], caption=ns.SC(text), reply_markup=kb,
                                             parse_mode=enums.ParseMode.HTML)
            await _delete(status)
            _STATE[uid]["msg"] = sent
            return
        except Exception:
            pass
    await _safe_edit(status, text, kb)
    _STATE[uid]["msg"] = status


async def _safe_edit(msg, text, kb=None):
    try:
        await msg.edit_text(_ns.SC(text), reply_markup=kb, parse_mode=enums.ParseMode.HTML)
    except Exception:
        pass


async def _delete(msg):
    try:
        await msg.delete()
    except Exception:
        pass


def _owned(cq):
    st = _STATE.get(cq.from_user.id)
    return st


def setup(app, ns) -> None:
    global _ns
    _ns = ns

    @app.on_callback_query(filters.regex(r"^sbs:(\d+)$"))
    async def _season(client, cq):
        st = _owned(cq)
        if not st:
            return await cq.answer("Session expired. Send the show link again.", show_alert=True)
        snum = int(cq.matches[0].group(1))
        if not any(s["number"] == snum for s in st["seasons"]):
            return await cq.answer("Season not found.", show_alert=True)
        st["season"], st["ep_page"], st["screen"] = snum, 0, "episodes"
        await cq.answer()
        await _render(cq.message, cq.from_user.id)

    @app.on_callback_query(filters.regex(r"^sbsp:(\d+)$"))
    async def _seasons_page(client, cq):
        st = _owned(cq)
        if not st:
            return await cq.answer("Session expired.", show_alert=True)
        st["seasons_page"] = int(cq.matches[0].group(1))
        st["screen"] = "seasons"
        await cq.answer()
        await _render(cq.message, cq.from_user.id)

    @app.on_callback_query(filters.regex(r"^sbep:(\d+):(\d+)$"))
    async def _ep_page(client, cq):
        st = _owned(cq)
        if not st:
            return await cq.answer("Session expired.", show_alert=True)
        st["season"], st["ep_page"], st["screen"] = int(cq.matches[0].group(1)), int(cq.matches[0].group(2)), "episodes"
        await cq.answer()
        await _render(cq.message, cq.from_user.id)

    @app.on_callback_query(filters.regex(r"^sbe:(\d+):(\d+)$"))
    async def _toggle(client, cq):
        st = _owned(cq)
        if not st:
            return await cq.answer("Session expired.", show_alert=True)
        snum, num = int(cq.matches[0].group(1)), int(cq.matches[0].group(2))
        sel = st["selected"].setdefault(snum, set())
        (sel.discard if num in sel else sel.add)(num)
        await cq.answer("Selected." if num in sel else "Removed.")
        await _render(cq.message, cq.from_user.id)

    @app.on_callback_query(filters.regex(r"^sbsa:(\d+)$"))
    async def _select_all(client, cq):
        st = _owned(cq)
        if not st:
            return await cq.answer("Session expired.", show_alert=True)
        snum = int(cq.matches[0].group(1))
        season = next((s for s in st["seasons"] if s["number"] == snum), None)
        if season:
            st["selected"][snum] = {e["number"] for e in season["episodes"]}
        await cq.answer("All selected.")
        await _render(cq.message, cq.from_user.id)

    @app.on_callback_query(filters.regex(r"^sbcl:(\d+)$"))
    async def _clear(client, cq):
        st = _owned(cq)
        if not st:
            return await cq.answer("Session expired.", show_alert=True)
        st["selected"].pop(int(cq.matches[0].group(1)), None)
        await cq.answer("Cleared.")
        await _render(cq.message, cq.from_user.id)

    @app.on_callback_query(filters.regex(r"^sbback$"))
    async def _back(client, cq):
        st = _owned(cq)
        if not st:
            return await cq.answer("Session expired.", show_alert=True)
        st["screen"] = "seasons"
        await cq.answer()
        await _render(cq.message, cq.from_user.id)

    @app.on_callback_query(filters.regex(r"^sbnoop$"))
    async def _noop(client, cq):
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^sbdl$"))
    async def _download(client, cq):
        st = _owned(cq)
        if not st:
            return await cq.answer("Session expired. Send the show link again.", show_alert=True)
        picks = []
        for s in st["seasons"]:
            for e in s["episodes"]:
                if e["number"] in st["selected"].get(s["number"], set()):
                    picks.append((s["number"], e))
        if not picks:
            return await cq.answer("Select at least one episode.", show_alert=True)
        links = [e["link"] for _, e in picks]
        dropped = len(links) - MAX_BATCH
        links = links[:MAX_BATCH]
        await cq.answer(f"Starting {len(links)} episode(s)...")
        user_msg = st["message"]
        _STATE.pop(cq.from_user.id, None)
        await _delete(cq.message)
        if dropped > 0:
            await user_msg.reply(_ns.SC(f"<b>ℹ️ Only the first {MAX_BATCH} episodes go in one batch.</b>\n"
                                        f"{dropped} more: select them again after this batch."),
                                 parse_mode=enums.ParseMode.HTML)
        import mxplayer_downloader as mx
        await mx.handle(client, user_msg, links)

    @app.on_callback_query(filters.regex(r"^sbcancel$"))
    async def _cancel(client, cq):
        _STATE.pop(cq.from_user.id, None)
        await _safe_edit(cq.message, "❌ Cancelled.")
        await cq.answer()

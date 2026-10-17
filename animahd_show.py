"""AnimaHD show page -> seasons + episode list.

A show link  (https://animahd.com/demon-slayer-kimetsu-no-yaiba-2/)  has no stream of its own; it lists the episodes,
each of which is an episode link  (https://animahd.com/player/?file_id=...&show_id=983&ep=18)  that the bot already
downloads. This module reads the show page and returns the seasons and their episodes so the bot can show them as
buttons (main.py: animahd_autodetect -> _animahd_show_open, callbacks fzs| / fzq|).

I could not open animahd.com while writing this (no network where it was built), so the page is read defensively:
  * every attribute of every tag, and the raw page text (JSON / script blobs), is searched for player / eid links;
  * links hidden behind a base64 parameter (?p= / ?target= / ?url=) are decoded;
  * the season comes from the link itself, the label ("S2E5", "Season 2"), season tabs / panes, or a "Season N" heading;
  * when nothing says which season it is, different `show_id` values become different seasons, else everything is S1.
If a page still gives no episodes, ShowError carries the raw HTML (`.html`) so the bot can send it to the admin as a file.
"""
import base64
import html as _html
import logging
import re
from urllib.parse import urljoin, urlparse, parse_qs, unquote

import requests

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover - bs4 is in requirements.txt
    BeautifulSoup = None

logger = logging.getLogger("animahd_show")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")
MAX_EPISODES = 600
EPISODES_PER_PAGE = 30

_RESERVED = {
    "filter", "anime-schedule", "schedule", "dmca", "terms", "about", "contact", "player", "genre", "genres", "tag",
    "category", "page", "search", "feed", "login", "register", "home", "az-list", "a-z", "latest", "new", "popular",
    "movies", "movie", "wp-content", "wp-admin", "wp-json", "privacy-policy", "request", "news", "blog",
}
_SHOW_PREFIXES = {"anime", "series", "show", "shows", "watch", "tv"}


class ShowError(Exception):
    """A problem worth showing to the user. `.html` = the page that was read (if any), for debugging."""

    def __init__(self, msg, html=None):
        super().__init__(msg)
        self.html = html


# ------------------------------------------------------------------------------------------------ links ----
def is_show_link(url: str) -> bool:
    """animahd.* link to a show's own page (not a player / episode / listing link)."""
    try:
        p = urlparse(url if "://" in url else "https://" + url)
    except Exception:
        return False
    if "animahd" not in (p.hostname or "").lower():
        return False
    if p.query and re.search(r"(?:^|&)(?:eid|file_id|id|p|target|gate|s)=", p.query):
        return False                                   # episode / gate / search links
    parts = [x for x in p.path.split("/") if x]
    if len(parts) == 2 and parts[0].lower() in _SHOW_PREFIXES:
        slug = parts[1]
    elif len(parts) == 1:
        slug = parts[0]
    else:
        return False
    low = slug.lower()
    return (low not in _RESERVED and not re.search(r"\.\w{2,4}$", low)
            and not re.search(r"(?:^|-)(?:episode|ep)-?\d+$", low))


# --------------------------------------------------------------------------------------------- fetching ----
def _fetch(url: str):
    last = None
    for _ in range(2):
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                                           "Accept-Language": "en-US,en;q=0.9", "Referer": "https://animahd.com/"},
                             timeout=25, allow_redirects=True)
        except Exception as e:
            last = str(e)[:120]
            continue
        if r.status_code == 200 and r.text:
            return r.text, r.url
        last = f"HTTP {r.status_code}"
    raise ShowError(f"Couldn't open the show page ({last}). Cloudflare may be blocking this server.")


# ------------------------------------------------------------------------------------- link extraction ----
_PLAYER_ABS_RE = re.compile(r"https?://[^\s\"'<>\\]+?/player/?\?[^\s\"'<>\\]*", re.I)
_PLAYER_REL_RE = re.compile(r"(?<=[\"'(=])/player/?\?[^\s\"'<>\\]*", re.I)
_WRAP_KEYS = ("p", "target", "url", "u", "link", "go", "gate", "href")
_LINK_ATTRS = ("href", "data-href", "data-url", "data-link", "data-src", "data-player", "data-file", "data-target-url",
               "onclick")


def _b64_text(s: str):
    s = unquote((s or "").strip())
    if len(s) < 12 or not re.fullmatch(r"[A-Za-z0-9+/_\-=]+", s):
        return None
    s += "=" * (-len(s) % 4)
    for dec in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            out = dec(s).decode("utf-8", "ignore")
        except Exception:
            continue
        if out.startswith("http") or "/player" in out or "file_id=" in out or "eid=" in out:
            return out
    return None


def _is_episode_url(u: str) -> bool:
    q = parse_qs(urlparse(u).query)
    if "/player" in urlparse(u).path.lower() and ("file_id" in q or "show_id" in q or "ep" in q or "id" in q):
        return True
    return "eid" in q and "animahd" in (urlparse(u).hostname or "").lower()


def _episode_url(value: str, base: str, depth: int = 0):
    """Attribute value -> episode (player / eid) URL, decoding ?p=<base64> style wrappers. None if it isn't one."""
    value = _html.unescape(value or "").strip()
    if not value or value.startswith(("#", "javascript:", "mailto:")):
        m = _PLAYER_ABS_RE.search(value) or _PLAYER_REL_RE.search(value)   # onclick="location='...'" case
        return urljoin(base, m.group(0)) if m else None
    full = urljoin(base, value)
    if _is_episode_url(full):
        return full
    if depth < 2:
        q = parse_qs(urlparse(full).query)
        for key in _WRAP_KEYS:
            for v in q.get(key, []):
                inner = _b64_text(v) or (unquote(v) if v.startswith(("http", "%3A", "%3a")) else None)
                if inner:
                    got = _episode_url(inner, base, depth + 1)
                    if got:
                        return got
    m = _PLAYER_ABS_RE.search(value) or _PLAYER_REL_RE.search(value)
    return urljoin(base, m.group(0)) if m else None


# ------------------------------------------------------------------------------- numbers / season hints ----
_SEASON_RE = re.compile(r"\bseason\s*0*(\d{1,2})\b", re.I)
_SE_RE = re.compile(r"\bS0*(\d{1,2})\s*[-_. ]?\s*E(?:p(?:isode)?)?\.?\s*0*(\d{1,4})\b", re.I)
_EP_RE = re.compile(r"(?<![A-Za-z])(?:e|ep|episode)\.?\s*0*(\d{1,4})\b", re.I)
_JUNK_RE = re.compile(r"\b(?:thumb\s*hd|thumbhd|thumb)\b", re.I)
_HEAD_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "strong", "b", "legend", "caption", "summary", "dt", "th", "p"}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", _JUNK_RE.sub(" ", _html.unescape(text or ""))).strip(" -|·•")


def _ep_from_label(label: str):
    m = _SE_RE.search(label)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _EP_RE.search(label)
    if m:
        return None, int(m.group(1))
    if re.fullmatch(r"0*\d{1,4}", label.strip()):
        return None, int(label.strip())
    return None, None


def _id_season(tokens):
    for t in tokens:
        t = (t or "").strip().lower()
        m = re.fullmatch(r"(?:season|ssn|se)[-_]?0*(\d{1,2})", t) or re.fullmatch(r"s0*(\d{1,2})", t) \
            or re.search(r"season[-_]?0*(\d{1,2})(?!\d)", t)
        if m:
            return int(m.group(1))
    return None


def _tab_map(soup) -> dict:
    """pane id -> season number, from tab buttons ('Season 2' pointing at #pane / data-tab / aria-controls)."""
    out = {}
    for el in soup.find_all(True):
        target = None
        for attr in ("href", "data-target", "data-bs-target", "data-tab", "aria-controls", "data-season-tab"):
            v = el.get(attr)
            if isinstance(v, str) and v:
                target = v.lstrip("#")
                if target and "/" not in target and " " not in target:
                    break
                target = None
        if not target:
            continue
        txt = _clean(el.get_text(" ", strip=True)) or _clean(el.get("title") or "")
        m = _SEASON_RE.search(txt) or re.fullmatch(r"S\s*0*(\d{1,2})", txt, re.I)
        if m:
            out[target] = int(m.group(1))
    return out


def _context_season(node, tabs: dict):
    cur, depth = node, 0
    while cur is not None and getattr(cur, "name", None) and depth < 10:
        cid = cur.get("id") if hasattr(cur, "get") else None
        if isinstance(cid, str) and cid in tabs:
            return tabs[cid]
        classes = cur.get("class") or [] if hasattr(cur, "get") else []
        classes = classes if isinstance(classes, list) else [classes]
        for attr in ("data-season", "data-season-number", "data-tab"):
            v = cur.get(attr) if hasattr(cur, "get") else None
            if isinstance(v, str) and re.fullmatch(r"0*\d{1,2}", v.strip()):
                return int(v.strip())
        s = _id_season([cid or ""] + list(classes))
        if s:
            return s
        cur, depth = cur.parent, depth + 1
    if not tabs:    # a "Season N" heading just before the list - but never a tab button (it would match every pane)
        try:
            for prev in node.find_all_previous(string=_SEASON_RE, limit=4):
                par = prev.parent
                if par is not None and par.name in _HEAD_TAGS:
                    return int(_SEASON_RE.search(prev).group(1))
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------------------------- parsing ----
def _label_for(node) -> str:
    txt = _clean(node.get_text(" ", strip=True))
    if not txt:
        txt = _clean(node.get("title") or node.get("aria-label") or "")
    if not txt and node.parent is not None:
        txt = _clean(node.parent.get_text(" ", strip=True))[:120]
    return txt


def parse_show(html_text: str, base: str) -> dict:
    """-> {"title","poster","seasons":[{"no","label","episodes":[{"ep","label","url"}]}]} (may have no seasons)."""
    if BeautifulSoup is None:
        raise ShowError("BeautifulSoup (bs4) isn't installed on this bot.")
    soup = BeautifulSoup(html_text, "html.parser")
    tabs = _tab_map(soup)
    found = []          # (node or None, url)
    for el in soup.find_all(True):
        for attr in _LINK_ATTRS:
            v = el.get(attr)
            if isinstance(v, str) and v:
                u = _episode_url(v, base)
                if u:
                    found.append((el, u))
                    break
    seen_urls = {u for _, u in found}
    blob = _html.unescape(html_text).replace("\\/", "/")
    for m in list(_PLAYER_ABS_RE.finditer(blob)) + list(_PLAYER_REL_RE.finditer(blob)):   # JSON / script blobs
        u = urljoin(base, m.group(0))
        if _is_episode_url(u) and u not in seen_urls:
            seen_urls.add(u)
            found.append((None, u))

    rows = []
    for order, (node, url) in enumerate(found):
        q = parse_qs(urlparse(url).query)
        label = _label_for(node) if node is not None else ""
        s_lbl, e_lbl = _ep_from_label(label)
        ep = e_lbl
        if (q.get("ep") or [""])[0].isdigit():
            ep = int(q["ep"][0])
        season = None
        for key in ("season", "sn", "season_no"):
            if (q.get(key) or [""])[0].isdigit():
                season = int(q[key][0])
        if season is None:
            season = s_lbl
        if season is None and node is not None:
            season = _context_season(node, tabs)
        rows.append({"order": order, "url": url, "ep": ep, "season": season, "label": label,
                     "show_id": (q.get("show_id") or [""])[0]})

    if not rows:
        return {"title": _title(soup), "poster": _poster(soup, base), "seasons": []}

    if all(r["season"] is None for r in rows):
        ids = list(dict.fromkeys(r["show_id"] for r in rows if r["show_id"]))
        if len(ids) > 1:                                 # one show_id per season
            for r in rows:
                r["season"] = ids.index(r["show_id"]) + 1 if r["show_id"] in ids else 1
    last = None
    for r in sorted(rows, key=lambda r: r["order"]):      # unknown season -> same as the previous entry, else 1
        if r["season"] is None:
            r["season"] = last or 1
        last = r["season"]

    by_season: dict = {}
    for r in rows:
        by_season.setdefault(r["season"], []).append(r)
    seasons = []
    for no in sorted(by_season):
        eps, used = [], set()
        for r in sorted(by_season[no], key=lambda r: r["order"]):
            if r["url"] in used:
                continue
            key = r["ep"]
            if key is not None and key in used:           # same episode twice (e.g. two qualities): keep the first
                continue
            used.add(r["url"])
            if key is not None:
                used.add(key)
            eps.append(dict(r))
        n = max([e["ep"] for e in eps if e["ep"] is not None] or [0])
        for e in eps:                                     # no number anywhere -> keep page order after the known ones
            if e["ep"] is None:
                n += 1
                e["ep"] = n
        eps.sort(key=lambda e: (e["ep"], e["order"]))
        seasons.append({"no": no, "label": f"Season {no}",
                        "episodes": [{"ep": e["ep"], "label": f"E{e['ep']:02d}", "url": e["url"]} for e in eps]})
    total = 0
    for s in seasons:
        s["episodes"] = s["episodes"][:max(MAX_EPISODES - total, 0)]
        total += len(s["episodes"])
    return {"title": _title(soup), "poster": _poster(soup, base), "seasons": [s for s in seasons if s["episodes"]]}


def _title(soup) -> str:
    for sel in (("meta", {"property": "og:title"}), ("meta", {"name": "twitter:title"})):
        t = soup.find(*sel)
        if t and t.get("content"):
            return _site_suffix(t["content"])
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return _site_suffix(h1.get_text(" ", strip=True))
    if soup.title and soup.title.string:
        return _site_suffix(soup.title.string)
    return ""


def _site_suffix(t: str) -> str:
    t = re.sub(r"\s*[-|–—:]\s*anima\s*hd.*$", "", _html.unescape(t or ""), flags=re.I)
    t = re.sub(r"^\s*watch\s+", "", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip()[:90]


def _poster(soup, base: str) -> str:
    t = soup.find("meta", {"property": "og:image"}) or soup.find("meta", {"name": "twitter:image"})
    return urljoin(base, t["content"]) if t and t.get("content") else ""


def fetch_show(url: str) -> dict:
    """Blocking (run it in a thread). Raises ShowError with a readable reason when no episodes can be found."""
    raw, final = _fetch(url)
    show = parse_show(raw, final)
    if not show["seasons"]:
        raise ShowError("No episode links found on that show page (the site may load them with JavaScript, or its "
                        "layout changed). Send one episode's link instead.", html=raw)
    return show


# --------------------------------------------------------------------------------- screens (for the bot) ----
def screen(show: dict, title: str, pick_id: str, sidx, page: int = 0):
    """-> (html_text, rows). rows = [[(button_text, callback_data, kind)]], kind in {"primary" blue, "success" green, "danger" red, "plain" uncoloured}.
    sidx None = season list; an int = that season's episode page. Episode buttons use fzq|<pick>|<global index>."""
    from html import escape
    seasons = show["seasons"]
    n_eps = sum(len(s["episodes"]) for s in seasons)
    head = f"<b>🎬 {escape(title)}</b>\n<i>{len(seasons)} season(s) · {n_eps} episode(s)</i>"
    cancel = ("❌ Cancel", f"fzq|{pick_id}|x", "danger")
    if sidx is None:
        btns = [(f"{s['label']} · {len(s['episodes'])} eps", f"fzs|{pick_id}|{i}|0", "primary") for i, s in enumerate(seasons)]
        rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
        rows.append([cancel])
        return f"{head}\n\n<i>Choose a season:</i>", rows
    s = seasons[sidx]
    eps = s["episodes"]
    pages = max(1, -(-len(eps) // EPISODES_PER_PAGE))
    page = max(0, min(page, pages - 1))
    chunk = eps[page * EPISODES_PER_PAGE:(page + 1) * EPISODES_PER_PAGE]
    btns = [(e["label"], f"fzq|{pick_id}|{e['idx']}", "primary") for e in chunk]
    rows = [btns[i:i + 5] for i in range(0, len(btns), 5)]
    nav = []
    if page > 0:
        nav.append(("◀", f"fzs|{pick_id}|{sidx}|{page - 1}", "success"))
    if pages > 1:
        nav.append((f"{page + 1}/{pages}", f"fzs|{pick_id}|{sidx}|{page}", "primary"))
    if page < pages - 1:
        nav.append(("▶", f"fzs|{pick_id}|{sidx}|{page + 1}", "success"))
    if nav:
        rows.append(nav)
    bottom = [("⬅ Seasons", f"fzs|{pick_id}|b", "danger")] if len(seasons) > 1 else []
    bottom.append(cancel)
    rows.append(bottom)
    first, last = chunk[0]["label"], chunk[-1]["label"]
    return f"{head}\n<b>{escape(s['label'])}</b> · {first}–{last}\n\n<i>Tap an episode:</i>", rows

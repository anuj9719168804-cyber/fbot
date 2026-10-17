"""FilmyZilla link resolver + category / full-site scanner (ported from src's Akbots/filmyzilla.py).

  resolve_single(url)                  -> (filename, download_url)          (one /movie/ or /server/ page)
  scan_category(url)                   -> [(filename, download_url), ...]   (one /category/ page)
  scan_full_site(base, max_cats, max_movies, progress_cb) -> dict           (homepage -> categories -> movies)
  chunk_lines(files)                   -> [html_text, ...]                  (Telegram-sized pieces, nothing dropped)

Uses the mirror helpers and workers.dev link finder from downloader.py; blocking `requests` calls run in a
small thread pool, so no new dependency is needed. The commands themselves (admin-only) live in main.py."""
import html as _html
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlsplit

import requests

from downloader import (
    HEADERS, BeautifulSoup, _FZ_MIRROR_RE, _WORKERS_FILE_RE, _fz_mirrors, _fz_on_mirror, get_quality_score,
)

MAX_MOVIES = 30          # per category page
CONCURRENCY = 5          # parallel movie/server fetches
TIMEOUT = 20
SCAN_MAX_CATEGORIES = 5
SCAN_MAX_MOVIES = 10

_ID_RE = re.compile(r"/movie/(\d+)/")


def _safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name or "").strip(" ._")
    return name[:150] or "movie_file"


def _base(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def _fetch(url: str):
    """(html, final_url) from the first mirror that answers. Relative links must be built from final_url."""
    last = None
    for mirror in _fz_mirrors():
        cand = _fz_on_mirror(url, mirror) if _FZ_MIRROR_RE.search(url) else url
        try:
            r = requests.get(cand, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
            if r.status_code == 200 and len(r.text) > 200:
                return r.text, r.url
            last = f"HTTP {r.status_code}"
        except Exception as e:
            last = e
    raise ValueError(f"page unreachable ({str(last)[:120]})")


def _movie_links(html: str, base: str) -> list:
    """Movie tiles on a category page. Falls back to any /movie/<id>/ link if the tile CSS classes change."""
    soup = BeautifulSoup(html, "html.parser")

    def collect(anchors):
        out, seen = [], set()
        for a in anchors:
            href = a.get("href")
            if href and "/movie/" in href:
                full = urljoin(base, href)
                if full not in seen:
                    seen.add(full)
                    out.append(full)
        return out

    out = collect(soup.select("a.d-flex.flex-row"))
    if not out:
        out = [u for u in collect(soup.select('a[href*="/movie/"]')) if _ID_RE.search(u)]
    return out


def _category_links(html: str, base: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.select('a[href*="/category/"]'):
        href = a.get("href")
        if href:
            full = urljoin(base, href)
            if full not in seen:
                seen.add(full)
                out.append(full)
    return out


def _server_links(html: str, base: str) -> list:
    """Every /server/ or server_ link on a movie page (the quality options): [(text, parent_text, url)]."""
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "server_" in href or "/server/" in href:
            full = urljoin(base, href)
            if full not in seen:
                seen.add(full)
                out.append((a.get_text(" ", strip=True), a.parent.get_text(" ", strip=True) if a.parent else "", full))
    return out


def _download_link(html: str):
    """workers.dev file URL on a server page: an <a> first, then the raw HTML (JavaScript button).
    Must be a real .mkv/.mp4 file URL, not an intermediate '/server/...mkv.html' page."""
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if "workers.dev" in h and re.search(r"\.(mkv|mp4)(\?|$)", h, re.IGNORECASE):
            return h
    m = _WORKERS_FILE_RE.search(html)
    return m.group(0) if m else None


def _quality_info(text: str, size_text: str) -> tuple:
    q = re.search(r"(\d+p|HEVC|HD|HDTC)", text, re.IGNORECASE)
    f = re.search(r"\.(mkv|mp4|avi)", text, re.IGNORECASE)
    s = re.search(r"(\d+(?:\.\d+)?)\s*(GB|MB)", size_text, re.IGNORECASE)
    return (q.group(1) if q else "Unknown", f.group(1).lower() if f else "mkv", s.group(0) if s else "Unknown")


def _file_name(dl: str) -> str:
    return _safe_name(dl.split("/")[-1].split("?")[0])


def _resolve_movie(movie_url: str):
    """movie page -> best-quality server -> download link. Returns (filename, download_url) or None."""
    try:
        html, final = _fetch(movie_url)
        servers = _server_links(html, _base(final))
        if not servers:
            return None
        servers.sort(key=lambda s: get_quality_score(s[0]), reverse=True)
        # try servers best-first until one yields a real file link (the top one is not always resolvable)
        for _, _, surl in servers[:3]:
            try:
                server_html, _ = _fetch(surl)
            except Exception:
                continue
            dl = _download_link(server_html)
            if dl:
                return _file_name(dl), dl
        return None
    except Exception:
        return None


_RES_RE = re.compile(r"\b(\d{3,4}p)\b", re.I)
_SRC_RE = re.compile(r"\b(Web-?DL|WEB-?Rip|HDRip|HDTC|BluRay|HDTS|DVDRip|CAMRip)\b", re.I)
_SIZE_RE = re.compile(r"\(([\d.]+\s*(?:MB|GB))\)", re.I)


def list_movie_servers(url: str) -> list:
    """A /movie/ page -> [{"label", "url", "size"}, ...], one entry per download option (480p, 720p, 1080p, HDRip, ...).

    Each option is a /server/ link on the page; the label is short ("1080p Web-DL", "480p HDRip (Reloaded)")
    so it fits on a Telegram button. Returns [] if the page has no /server/ links."""
    if BeautifulSoup is None:
        raise ValueError("BeautifulSoup (bs4) isn't installed on this bot.")
    html, final = _fetch(url)
    soup = BeautifulSoup(html, "html.parser")
    base = _base(final)
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"])
        if "/server/" not in href or href in seen:
            continue
        seen.add(href)
        text = " ".join(a.get_text(" ", strip=True).split()).lstrip("» ").strip()
        # the file size is usually plain text right after the link: "... 480p Web-DL.mp4 (829.94 MB)"
        after = a.next_sibling
        size_src = (str(after) if after is not None else "") + " " + text
        m_size = _SIZE_RE.search(size_src)
        res = _RES_RE.search(text)
        src = _SRC_RE.search(text)
        if res and src:
            label = f"{res.group(1)} {src.group(1)}"
            if "reloaded" in text.lower():
                label += " (Reloaded)"
        else:
            label = re.sub(r"\.mp4$", "", text, flags=re.I)[-40:].strip() or "Option"
        out.append({"label": label, "url": href, "size": m_size.group(1) if m_size else ""})
    return out


_EP_TEXT_RE = re.compile(r"\bS\s*(\d{1,2})\s*[-_ ]?\s*E\s*(\d{1,4})\b|\bE\s*(\d{1,4})\b|\bEp(?:isode)?\.?\s*(\d{1,4})\b", re.I)
_EP_HREF_RE = re.compile(r"(?:episode|ep)[-_/]?(\d{1,4})\b", re.I)
MAX_EPISODES = 500


def list_episodes(url: str) -> list:
    """A TV-show page -> [{"season", "ep", "label", "url"}, ...] sorted by season and episode.

    Only same-site links whose text (E01, S6E10, Episode 10, Ep 10) or URL (episode-10) looks like an episode are
    taken. Label is "E10" when the page has a single season, "S6E10" when it has several. Returns [] if none."""
    if BeautifulSoup is None:
        raise ValueError("BeautifulSoup (bs4) isn't installed on this bot.")
    html, final = _fetch(url)
    soup = BeautifulSoup(html, "html.parser")
    base = _base(final)
    host = urlsplit(base).netloc
    found = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"])
        if not href.startswith(("http://", "https://")) or urlsplit(href).netloc != host:
            continue
        text = " ".join(a.get_text(" ", strip=True).split())
        m = _EP_TEXT_RE.search(text)
        if m:
            if m.group(1) is not None:
                season, ep = int(m.group(1)), int(m.group(2))
            else:
                season, ep = 1, int(m.group(3) or m.group(4))
        else:
            mh = _EP_HREF_RE.search(urlsplit(href).path)
            if not mh:
                continue
            season, ep = 1, int(mh.group(1))
        if ep <= 0:
            continue
        found.setdefault((season, ep), href)
        if len(found) >= MAX_EPISODES:
            break
    several = len({s for s, _ in found}) > 1
    out = []
    for (season, ep), u in sorted(found.items()):
        label = f"S{season}E{ep:02d}" if several else f"E{ep:02d}"
        out.append({"season": season, "ep": ep, "label": label, "url": u})
    return out


def resolve_single(url: str) -> tuple:
    """A single /server/ or /movie/ page -> (filename, download_url). Raises ValueError with a readable reason."""
    if BeautifulSoup is None:
        raise ValueError("BeautifulSoup (bs4) isn't installed on this bot.")
    if "/server/" in url:
        html, _ = _fetch(url)
        dl = _download_link(html)
        if not dl:
            raise ValueError("Reached the server page but found no direct workers.dev link on it "
                             "(layout changed, or this server has no direct link).")
        return _file_name(dl), dl
    res = _resolve_movie(url)
    if not res:
        raise ValueError("Couldn't resolve a download link from that movie page.")
    return res


def scan_category(url: str, max_movies: int = MAX_MOVIES) -> list:
    """All resolvable (filename, download_url) pairs on one /category/ page."""
    if BeautifulSoup is None:
        raise ValueError("BeautifulSoup (bs4) isn't installed on this bot.")
    html, final = _fetch(url)
    movies = _movie_links(html, _base(final))[:max_movies]
    if not movies:
        raise ValueError("No movies found on that category page - the page structure may have changed.")
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        results = list(ex.map(_resolve_movie, movies))
    return [r for r in results if r]


def _movie_record(movie_url: str, category: str):
    """Full record for one movie: title/year/poster and every server option with its direct link."""
    try:
        html, final = _fetch(movie_url)
        base = _base(final)
        soup = BeautifulSoup(html, "html.parser")
        t = soup.select_one('a[href*="/movie/"]')
        title = t.get_text(strip=True) if t else ""
        if not title or title.lower() == "filmyzilla.com":
            st = soup.select_one('a[href*="/server/"]')
            m = re.match(r"^(.+?)\s+\d+p", st.get_text(strip=True), re.IGNORECASE) if st else None
            title = m.group(1).strip() if m else title
        yr = re.search(r"(\d{4})", title)
        mid = _ID_RE.search(movie_url)
        thumb = soup.select_one('img[src*="poster"]')
        desc = soup.select_one('meta[name="description"]')
        links = []
        for stext, sparent, surl in _server_links(html, base):
            q, fmt, size = _quality_info(stext, sparent)
            try:
                sh, _ = _fetch(surl)
                dl = _download_link(sh) or "NOT_FOUND"
            except Exception:
                dl = "ERROR"
            links.append({"quality": q, "format": fmt, "size": size, "serverUrl": surl, "downloadUrl": dl})
        return {
            "id": mid.group(1) if mid else "", "title": title, "url": movie_url,
            "year": yr.group(1) if yr else "", "category": category,
            "thumbnail": thumb.get("src", "") if thumb else "",
            "description": desc.get("content", "") if desc else "",
            "links": links,
        }
    except Exception:
        return None


def scan_full_site(base: str, max_categories: int, max_movies: int, progress_cb=None) -> dict:
    """Homepage -> up to max_categories categories -> up to max_movies movies each.
    progress_cb(done_cats, total_cats, movies_so_far) is called from the worker thread."""
    if BeautifulSoup is None:
        raise ValueError("BeautifulSoup (bs4) isn't installed on this bot.")
    home, final = _fetch(base)
    root = _base(final)
    cats = _category_links(home, root)[:max_categories]
    if not cats:
        raise ValueError("No categories found on the homepage - the site layout may have changed.")
    movies = []
    for i, cat in enumerate(cats, 1):
        try:
            chtml, cfinal = _fetch(cat)
        except Exception:
            continue  # one dead category shouldn't sink the whole scan
        cname = cat.rstrip("/").split("/")[-1]
        urls = _movie_links(chtml, _base(cfinal))[:max_movies]
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
            recs = list(ex.map(lambda u: _movie_record(u, cname), urls))
        movies.extend(r for r in recs if r)
        if progress_cb:
            try:
                progress_cb(i, len(cats), len(movies))
            except Exception:
                pass
    total_links = sum(len(m["links"]) for m in movies)
    return {
        "generatedAt": int(time.time()),
        "totalMovies": len(movies),
        "totalCategories": len({m["category"] for m in movies}),
        "totalLinks": total_links,
        "categories": sorted({m["category"] for m in movies}),
        "movies": movies,
    }


# ---- category browser (main.py: pasted /category/ link -> movie buttons, page by page) ----------------------------------
_PAGE_RE = re.compile(r"^(.*?)/default/(\d+)\.html$", re.I)
_NEXT_TXT = {"next", "next »", "»", "›", ">", "next page", "older"}
_PREV_TXT = {"prev", "previous", "« prev", "«", "‹", "<", "previous page", "newer"}
CATEGORY_MAX_ITEMS = 40          # buttons per category page (Telegram allows 100 per message)


def _slug_title(url: str, fallback: str = "Movie") -> str:
    """.../movie/22409/Jeet-ki-zid-(2026)-hindi-movie.html -> 'Jeet Ki Zid (2026) Hindi'"""
    from urllib.parse import unquote
    slug = unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
    slug = re.sub(r"\.(?:html?|php)$", "", slug, flags=re.I)
    t = re.sub(r"[-_+]+", " ", slug).strip()
    t = re.sub(r"\s+(?:full\s+)?(?:movie|series|web series)(?:\s+download)?$", "", t, flags=re.I)
    return " ".join(w[:1].upper() + w[1:] for w in t.split()) or fallback


def _category_title(url: str) -> str:
    """/category/405/2026-latest-bollywood-movies/default/1.html -> '2026 Latest Bollywood Movies'"""
    from urllib.parse import unquote
    parts = [p for p in unquote(urlsplit(url).path).split("/") if p]
    parts = [p for p in parts if p.lower() != "category"]
    if parts and re.fullmatch(r"\d+", parts[0]) and len(parts) > 1:
        parts = parts[1:]                                        # drop the numeric id
    if parts and parts[0].lower() == "default":
        return "Category"
    slug = re.sub(r"\.(?:html?|php)$", "", parts[0] if parts else "", flags=re.I)
    return " ".join(w[:1].upper() + w[1:] for w in re.sub(r"[-_+]+", " ", slug).split()) or "Category"


def list_category(url: str) -> dict:
    """Category page -> {"title","url","page","movies":[{"title","url"}],"subcats":[{"title","url"}],"prev","next"}.

    `movies` are the /movie/ tiles; when a page lists none (e.g. a TV-shows index), `subcats` has the /category/ tiles
    instead. prev/next are page URLs (or None). Raises ValueError with a readable reason when the page can't be read."""
    if BeautifulSoup is None:
        raise ValueError("BeautifulSoup (bs4) isn't installed on this bot.")
    html, final = _fetch(url)
    base = _base(final)
    soup = BeautifulSoup(html, "html.parser")
    movies = [{"title": _slug_title(u), "url": u} for u in _movie_links(html, base)][:CATEGORY_MAX_ITEMS]

    subcats, seen = [], {final.rstrip("/")}
    if not movies:
        for a in soup.select("a.d-flex.flex-row") or soup.select('a[href*="/category/"]'):
            href = a.get("href") or ""
            full = urljoin(base, href)
            pm = _PAGE_RE.match(urlsplit(full).path)      # a sub-category's own link ends in /default/1.html; /default/2+ is a pager
            if "/category/" in href and full.rstrip("/") not in seen and not (pm and int(pm.group(2)) != 1):
                seen.add(full.rstrip("/"))
                subcats.append({"title": _category_title(full), "url": full})
        subcats = subcats[:CATEGORY_MAX_ITEMS]

    m = _PAGE_RE.match(urlsplit(final).path)
    prev_url = next_url = None
    cur = int(m.group(2)) if m else 1
    anchors = {}                                                  # page number -> url, from the page's own pager
    for a in soup.find_all("a", href=True):
        full = urljoin(base, a["href"])
        mm = _PAGE_RE.match(urlsplit(full).path)
        if m and mm and mm.group(1) == m.group(1):
            anchors[int(mm.group(2))] = full
        elif not m:
            txt = a.get_text(" ", strip=True).lower()
            num = re.search(r"(?:page[=/-]|/|-)(\d+)(?:\.html)?/?$", urlsplit(full).path + ("?" + urlsplit(full).query if urlsplit(full).query else ""))
            if txt.isdigit() and num and int(num.group(1)) == int(txt) and urlsplit(full).path.startswith(urlsplit(final).path.rsplit(".", 1)[0]):
                anchors[int(txt)] = full
            elif txt in _NEXT_TXT and full.rstrip("/") != final.rstrip("/"):
                next_url = next_url or full
            elif txt in _PREV_TXT and full.rstrip("/") != final.rstrip("/"):
                prev_url = prev_url or full
    if m:
        stem = f"{base}{m.group(1)}"                              # .../category/405/<slug>
        if cur > 1:
            prev_url = f"{stem}/default/{cur - 1}.html"
        if (cur + 1) in anchors or not anchors:                   # no pager at all -> assume there may be more
            next_url = f"{stem}/default/{cur + 1}.html"
    else:
        next_url = anchors.get(cur + 1) or next_url
        prev_url = anchors.get(cur - 1) or prev_url if cur > 1 else prev_url

    if not movies and not subcats:
        raise ValueError("No movies found on that category page (the layout may have changed).")
    return {"title": _category_title(final), "url": final, "page": cur, "movies": movies, "subcats": subcats,
            "prev": prev_url, "next": next_url}


def category_screen(cat: dict, pick_id: str, can_back: bool = False):
    """-> (html_text, rows); rows = [[(button_text, callback_data, kind)]], kind in {"primary" blue, "success" green, "danger" red, "plain" uncoloured}.
    Callbacks: fzc|<pick>|m|<i> open movie, c|<i> open sub-category, n / p next / previous page, b back, x cancel."""
    from html import escape
    items = cat["movies"] or cat["subcats"]
    is_movies = bool(cat["movies"])
    head = (f"<b>📂 {escape(cat['title'])}</b>\n<i>Page {cat['page']} · {len(items)} "
            f"{'movie(s)' if is_movies else 'categories'}</i>")
    kind_cb = "m" if is_movies else "c"
    icon = "🎬 " if is_movies else "📂 "
    rows = [[((icon + it["title"])[:60], f"fzc|{pick_id}|{kind_cb}|{i}", "primary")] for i, it in enumerate(items)]
    nav = []
    if cat.get("prev"):
        nav.append(("◀ Prev", f"fzc|{pick_id}|p", "success"))
    nav.append((f"Page {cat['page']}", f"fzc|{pick_id}|k", "primary"))
    if cat.get("next"):
        nav.append(("Next ▶", f"fzc|{pick_id}|n", "success"))
    rows.append(nav)
    bottom = [("⬅ Back", f"fzc|{pick_id}|b", "danger")] if can_back else []
    bottom.append(("❌ Cancel", f"fzc|{pick_id}|x", "danger"))
    rows.append(bottom)
    return f"{head}\n\n<i>Choose a {'movie' if is_movies else 'category'}:</i>", rows


def chunk_lines(files: list, limit: int = 3800) -> list:
    """Splits 'filename + link' entries into Telegram-sized HTML chunks (nothing dropped)."""
    chunks, cur = [], ""
    for name, link in files:
        entry = f"<b>{_html.escape(name)}</b>\n<code>{_html.escape(link)}</code>\n\n"
        if len(cur) + len(entry) > limit and cur:
            chunks.append(cur)
            cur = ""
        cur += entry
    if cur:
        chunks.append(cur)
    return chunks

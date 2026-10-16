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

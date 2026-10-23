"""AnimeSalt (animesalt.cam) support for fbot - same flow as AnimaHD.

Ported from the Anime-Salt Cloudflare worker (src/parsers/servers.js, src/decryptors/*.js, src/api/cam.js).
The worker returns JSON for a web player; here the same logic ends in a local file the bot can upload.

    is_supported(url)       pasted episode / movie page (animesalt.*/episode/<slug>/ or /movies/<slug>/)?
    is_show_link(url)       a show's own page (animesalt.*/tv/<slug>/)? -> seasons + episode buttons in the bot
    fetch_show(url)         blocking, -> {"title","poster","seasons":[{"no","label","episodes":[{"ep","label","url"}]}]}
                            (same shape as animahd_show.fetch_show, so main.py reuses the season / episode screens)
    download_from_page(url, state)                      -> local path | None   (tries every server, best language first)
    search_and_download(query, original, state, title_match)
                                                        -> local path | None   (WordPress search -> show -> episode)

Flow of one episode page: servers (language tabs, data-embed buttons) -> embed host -> raw stream:
    as-cdn*        POST player/index.php?do=getVideo           -> HLS master
    megaplay       getSourcesNew -> AES-CBC "enc"              -> HLS master (needs Referer/Origin megaplay.buzz)
    abyssplayer    const datas -> enc-dec.app/api/dec-abyss    -> signed MP4 (needs the full player page as Referer)
    vidmoly        JWPlayer config (maybe p.a.c.k.e.d)         -> m3u8 / mp4
    anything else  iframe / plain m3u8 on the page
HLS masters are read here: best variant + the wanted audio language (Hindi by default) -> one ffmpeg stream copy.
Referers follow the worker's proxy/media.js: the page-level referer first, then its origin, then the known player hosts
(megaplay.buzz, as-cdn26..30, animesalt, abyssplayer, playhydrax), the target's own origin, finally none - the first one
that gets a 2xx is used for the whole download (playlist + first segment are both checked).

Env: ANIMESALT_DOMAIN        site root (default https://animesalt.cam) - change it when the site moves
     ANIMESALT_LANG          language tried first, matched against the tab name ("Hindi Dub"); "" = site order
     ANIMESALT_AUDIO         audio track wanted inside an HLS master (default: the server's language, else ANIMESALT_LANG)
     ANIMESALT_MAX_HEIGHT    0 = best quality (default); e.g. 720 caps the picked HLS variant
     ANIMESALT_MAX_SOURCES   how many servers to try per page (default 6)
     ANIMESALT_FFMPEG_TIMEOUT  seconds for one HLS download (default 3600)

Written without network access: the parsers follow the worker's verified markup (asnt theme), but nothing here could be
run against the live site. A series page whose layout differs raises ShowError carrying the raw HTML, which the bot
sends to the admin as a file, the same as for AnimaHD.
"""
import base64
import html as _html
import json
import logging
import os
import re
import subprocess
import time
from urllib.parse import urljoin, urlparse, quote_plus

import requests

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover - bs4 is in requirements.txt
    BeautifulSoup = None

import animahd_show
from animahd_show import ShowError

logger = logging.getLogger("animesalt")

UPSTREAM = os.getenv("ANIMESALT_DOMAIN", "https://animesalt.cam").rstrip("/")
SITE_REFERER = UPSTREAM + "/"
PREF_LANG = os.getenv("ANIMESALT_LANG", "hindi").strip().lower()
PREF_AUDIO = os.getenv("ANIMESALT_AUDIO", "").strip().lower()
MAX_HEIGHT = int(os.getenv("ANIMESALT_MAX_HEIGHT", "0") or 0)
MAX_SOURCES = int(os.getenv("ANIMESALT_MAX_SOURCES", "6"))
FFMPEG_TIMEOUT = int(os.getenv("ANIMESALT_FFMPEG_TIMEOUT", "3600"))
OUT_PATH = "temp_animesalt.mp4"
suggested_names: dict = {}        # local temp path -> "<Title>.mp4"; downloader merges this into SUGGESTED_NAMES

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/137.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


class FetchError(Exception):
    pass


# ------------------------------------------------------------------------------------------------ http ----
def _get(url: str, referer=None, headers=None, timeout: int = 25):
    """GET with one retry; on a Cloudflare-style 403/429/503 (or a connection error) one more try with curl_cffi's
    Chrome TLS fingerprint when it's installed. Returns a response object (.text .url .json()). Raises FetchError."""
    h = dict(HEADERS)
    if referer:
        h["Referer"] = referer
    h.update(headers or {})
    last, retry_cffi = None, True
    for _ in range(2):
        try:
            r = requests.get(url, headers=h, timeout=timeout, allow_redirects=True)
        except Exception as e:
            last = str(e)[:120]
            continue
        if r.status_code == 200 and r.text:
            return r
        last = f"HTTP {r.status_code}"
        if r.status_code not in (403, 429, 503):
            retry_cffi = False
            break
    if retry_cffi:
        try:
            from curl_cffi import requests as cr
            r = cr.get(url, headers=h, impersonate="chrome", timeout=timeout, allow_redirects=True)
            if r.status_code == 200 and r.text:
                return r
            last = f"HTTP {r.status_code}"
        except ImportError:
            pass
        except Exception as e:
            last = str(e)[:120]
    raise FetchError(last or "no response")


# ------------------------------------------------------------------- referer rules (worker proxy/media.js) ----
_REFERER_FALLBACKS = ("https://megaplay.buzz/", "https://megaplay.buzz", "https://as-cdn26.top/", "https://as-cdn27.top/",
                      "https://as-cdn28.top/", "https://as-cdn29.top/", "https://as-cdn30.top/", SITE_REFERER,
                      "https://abyssplayer.com/", "https://playhydrax.com/")


def _hdrs(referer=None, extra=None) -> dict:
    """Headers for a media request. Origin is sent ONLY with an origin-level referer (a plain <video> load carries no
    Origin, and some anti-hotlink CDNs 403 when it is there) - same rule as the worker's proxy."""
    h = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}
    if referer:
        h["Referer"] = referer
        if urlparse(referer).path in ("", "/"):
            h["Origin"] = referer.rstrip("/")
    h.update(extra or {})
    return h


def _referer_candidates(target: str, referer) -> list:
    """Page-level referer first (sssrr.org / hydrax CDNs validate the whole player page URL), then its origin, the known
    player hosts, the target's own origin, and finally no referer at all."""
    ref_origin = _origin(referer) if referer and referer.startswith("http") else None
    tgt_origin = _origin(target) if (target or "").startswith("http") else None
    out = []
    for c in (referer, ref_origin, *_REFERER_FALLBACKS, tgt_origin, ""):
        if c is not None and c not in out:
            out.append(c)
    return out


def _probe(url: str, referer, limit: int = 4096):
    """Try the referer candidates in order until one answers 2xx (an HTML 200 is a block page, not media).
    -> (working_referer, content_type, first bytes) | None. Reads at most `limit` bytes, and only more than 4 KB when the
    body is an HLS playlist, so a big mp4 is never pulled down by a probe."""
    for cand in _referer_candidates(url, referer):
        try:
            with requests.get(url, headers=_hdrs(cand), stream=True, timeout=(8, 20), allow_redirects=True) as r:
                ctype = r.headers.get("Content-Type", "").lower()
                if not (200 <= r.status_code < 300) or "text/html" in ctype:
                    continue
                buf = b""
                for chunk in r.iter_content(65536):
                    buf += chunk
                    if len(buf) >= limit or (len(buf) >= 4096 and not buf.lstrip().startswith(b"#EXTM3U")):
                        break
                return cand, ctype, buf[:limit]
        except Exception:
            continue
    return None


def _is_hls(url: str, ctype: str, head: bytes) -> bool:
    """The worker decides by extension OR Content-Type (mpegurl / manifest); sniffing #EXTM3U covers the rest
    (as-cdn's securedLink does not always end in .m3u8)."""
    return bool(re.search(r"\.m3u8(\?|$)", url or "", re.I) or re.search(r"mpegurl|manifest", ctype or "", re.I)
                or (head or b"").lstrip()[:7] == b"#EXTM3U")


# ------------------------------------------------------------------------------------------------ links ----
_PATH_RE = re.compile(r"^/(tv|series|movies|movie|episode)/([^/?#]+)", re.I)


def _host_ok(url: str) -> bool:
    try:
        return "animesalt" in (urlparse(url if "://" in url else "https://" + url).hostname or "").lower()
    except Exception:
        return False


def _kind_slug(url: str):
    try:
        p = urlparse(url if "://" in url else "https://" + url)
    except Exception:
        return None, None
    if "animesalt" not in (p.hostname or "").lower():
        return None, None
    m = _PATH_RE.match(p.path)
    return (m.group(1).lower(), m.group(2)) if m else (None, None)


def is_supported(url: str) -> bool:
    """An episode or movie page: it hosts the player, so it can be downloaded directly."""
    return _kind_slug(url)[0] in ("episode", "movies", "movie")


def is_show_link(url: str) -> bool:
    """A show's own page (/tv/<slug>/): no stream, only the episode list."""
    return _kind_slug(url)[0] in ("tv", "series")


# --------------------------------------------------------------------------------- show page (seasons) ----
_SLUG_SE = (
    re.compile(r"-season-0*(\d{1,2})-episode-0*(\d{1,4})/?$", re.I),
    re.compile(r"-s0*(\d{1,2})e0*(\d{1,4})/?$", re.I),
    re.compile(r"-0*(\d{1,2})x0*(\d{1,4})/?$", re.I),
)
_SLUG_EP = re.compile(r"-episode-0*(\d{1,4})/?$", re.I)


def _se_from_slug(slug: str):
    for rx in _SLUG_SE:
        m = rx.search(slug)
        if m:
            return int(m.group(1)), int(m.group(2))
    m = _SLUG_EP.search(slug)
    return (None, int(m.group(1))) if m else (None, None)


def _site_suffix(t: str) -> str:
    t = re.sub(r"\s*[-|–—:]\s*anime\s*salt.*$", "", _html.unescape(t or ""), flags=re.I)
    t = re.sub(r"^\s*watch\s+", "", t, flags=re.I)
    t = re.sub(r"\s+(?:all\s+)?(?:episodes?|season\s*\d+).*$", "", t, flags=re.I) if len(t) > 90 else t
    return re.sub(r"\s+", " ", t).strip()[:90]


def _meta(soup, *names):
    for n in names:
        t = soup.find("meta", {"property": n}) or soup.find("meta", {"name": n})
        if t and t.get("content"):
            return t["content"]
    return ""


def _build_seasons(rows):
    """rows: [{"url","ep","season","order"}] -> seasons list (same shape as animahd_show.parse_show)."""
    if not rows:
        return []
    last = None
    for r in sorted(rows, key=lambda r: r["order"]):          # unknown season -> same as the previous entry, else 1
        if r["season"] is None:
            r["season"] = last or 1
        last = r["season"]
    by_season: dict = {}
    for r in rows:
        by_season.setdefault(r["season"], []).append(r)
    seasons, total = [], 0
    for no in sorted(by_season):
        eps, used = [], set()
        for r in sorted(by_season[no], key=lambda r: r["order"]):
            if r["url"] in used:
                continue
            key = r["ep"]
            if key is not None and ("ep", key) in used:
                continue
            used.add(r["url"])
            if key is not None:
                used.add(("ep", key))
            eps.append(dict(r))
        n = max([e["ep"] for e in eps if e["ep"] is not None] or [0])
        for e in eps:                                          # no number anywhere -> page order after the known ones
            if e["ep"] is None:
                n += 1
                e["ep"] = n
        eps.sort(key=lambda e: (e["ep"], e["order"]))
        eps = eps[:max(animahd_show.MAX_EPISODES - total, 0)]
        total += len(eps)
        if eps:
            seasons.append({"no": no, "label": f"Season {no}",
                            "episodes": [{"ep": e["ep"], "label": f"E{e['ep']:02d}", "url": e["url"]} for e in eps]})
    return seasons


def parse_show(page_html: str, base: str, extra_rows=None) -> dict:
    """extra_rows: rows from the WordPress REST lookup, merged in (page rows win: they carry the tab's season)."""
    if BeautifulSoup is None:
        raise ShowError("BeautifulSoup (bs4) isn't installed on this bot.")
    soup = BeautifulSoup(page_html, "html.parser")
    tabs = animahd_show._tab_map(soup)
    rows, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = urljoin(base, _html.unescape(a["href"])).split("#")[0].split("?")[0]
        kind, slug = _kind_slug(href)
        if kind != "episode" or href.rstrip("/") in seen:
            continue
        seen.add(href.rstrip("/"))
        s_slug, e_slug = _se_from_slug(slug)
        label = animahd_show._label_for(a)
        s_lbl, e_lbl = animahd_show._ep_from_label(label)
        season = s_slug or s_lbl
        if season is None:
            season = animahd_show._context_season(a, tabs)
        rows.append({"order": len(rows), "url": href, "ep": e_slug if e_slug is not None else e_lbl, "season": season})
    for x in extra_rows or []:                                 # episodes only the REST API knows about
        if x["url"].rstrip("/") not in seen:
            seen.add(x["url"].rstrip("/"))
            rows.append({**x, "order": len(rows)})
    h1 = soup.find("h1")
    title = _site_suffix(_meta(soup, "og:title", "twitter:title") or (h1.get_text(" ", strip=True) if h1 else ""))
    poster = _meta(soup, "og:image", "twitter:image")
    return {"title": title, "poster": urljoin(base, poster) if poster else "", "seasons": _build_seasons(rows)}


_TYPES_CACHE = {"t": 0.0, "v": {}}


def _wp_types() -> dict:
    """WordPress post types -> {"series": rest_base, "movies": ..., "episodes": ...} (worker's discoverTypes). Cached 6 h;
    a failure is remembered for 5 min so a blocked REST API doesn't cost a request per call."""
    age = time.time() - _TYPES_CACHE["t"]
    if _TYPES_CACHE["t"] and age < (6 * 3600 if _TYPES_CACHE["v"] else 300):
        return _TYPES_CACHE["v"]
    out = {}
    try:
        types = _get(f"{UPSTREAM}/wp-json/wp/v2/types", referer=SITE_REFERER).json() or {}
        for key, t in types.items():
            rb = (t or {}).get("rest_base") or key
            for kind, rx in (("series", r"tv|series|tv-?shows?|tv_show|anime"), ("movies", r"movies?|films?"),
                             ("episodes", r"episodes?")):
                if kind not in out and (re.fullmatch(rx, key, re.I) or re.fullmatch(rx, rb, re.I)):
                    out[kind] = rb
    except Exception as e:
        logger.info(f"animesalt REST types failed: {e}")
    _TYPES_CACHE.update(t=time.time(), v=out)
    return out


def _episode_slug_rx(slug: str):
    """<slug>-episode-N, -season-S-episode-N, -2nd-season-episode-N, -sSeN, -SxN. Stricter than a plain prefix, which
    would also let "naruto" swallow every "naruto-shippuden-episode-N"."""
    return re.compile(rf"^{re.escape(slug)}-(?:(?:\d+(?:st|nd|rd|th)-season|season-\d+|part-\d+|cour-\d+)-)?"
                      rf"(?:episode-\d+|s\d+e\d+|\d+x\d+)$", re.I)


def _rest_episodes(slug: str):
    """Episodes of a show from WordPress REST (the worker's handleEpisodes; it merges these with the series page, and so
    does fetch_show). -> rows for _build_seasons (may be [])."""
    base = _wp_types().get("episodes")
    if not base:
        return []
    rx = _episode_slug_rx(slug)
    rows = []
    for page in range(1, 11):
        try:
            data = _get(f"{UPSTREAM}/wp-json/wp/v2/{base}?search={quote_plus(slug.replace('-', ' '))}"
                        f"&per_page=100&page={page}&orderby=date&order=asc", referer=SITE_REFERER).json()
        except Exception:
            break
        for p in data or []:
            link = (p.get("link") or "").split("?")[0]
            ep_slug = p.get("slug") or link.rstrip("/").rsplit("/", 1)[-1]
            if "/episode/" in link and rx.match(ep_slug):
                s, e = _se_from_slug(ep_slug)
                rows.append({"order": len(rows), "url": link, "ep": e, "season": s})
        if len(data or []) < 100:
            break
    return rows


def fetch_show(url: str) -> dict:
    """Blocking (run it in a thread). Raises animahd_show.ShowError with a readable reason when nothing is found.
    Episode links on the page + the REST list are merged (the page may show only one season tab / the latest few)."""
    try:
        r = _get(url, referer=SITE_REFERER)
    except FetchError as e:
        raise ShowError(f"Couldn't open the AnimeSalt page ({e}). Cloudflare may be blocking this server.")
    slug = _kind_slug(r.url)[1] or _kind_slug(url)[1] or ""
    show = parse_show(r.text, r.url, _rest_episodes(slug) if slug else None)
    if not show["seasons"]:
        raise ShowError("No episode links found on that AnimeSalt show page (the site may load them with JavaScript, "
                        "or its layout changed). Send one episode's link instead.", html=r.text)
    return show


# ------------------------------------------------------------------------------------ servers (one page) ----
def _expand_multi(server: dict):
    """Multi-language embed: ...?data=<base64 JSON [{language, link}]> -> one server per language."""
    m = re.search(r"data=([A-Za-z0-9+/=_-]{20,})", server["embed"])
    if not m:                                                  # (worker: also tries any data=<20+ chars>, not only multi / short.icu)
        return [server]
    try:
        raw = m.group(1).replace("-", "+").replace("_", "/")
        items = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "ignore"))
    except Exception:
        return [server]
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        link = str(it.get("link") or "").replace("\\/", "/")
        if link:
            out.append({**server, "embed": link, "language": str(it.get("language") or server["language"]),
                        "lang_key": str(it.get("language") or server["lang_key"]).lower()})
    return out or [server]


def parse_servers(page_html: str) -> list:
    """-> [{"name","embed","language","lang_key"}]. Strategy 0 is the verified asnt-theme markup:
       <button class="asnt-lang-tab" data-lang-index="1" data-lang-key="hindi-dub">Hindi Dub</button>
       <div data-lang-panel="1"> <button data-embed="https://vidmoly.org/embed-x.html" data-server="Server 1">"""
    if BeautifulSoup is None:
        return []
    soup = BeautifulSoup(page_html, "html.parser")
    servers = []

    langs = {}
    for t in soup.find_all(attrs={"data-lang-index": True}):
        key = t.get("data-lang-key") or ""
        langs[str(t["data-lang-index"])] = (t.get_text(" ", strip=True) or key, key)
    for panel in soup.find_all(attrs={"data-lang-panel": True}):
        idx = str(panel["data-lang-panel"])
        lang, key = langs.get(idx, (f"Lang {idx}", f"lang-{idx}"))
        for b in panel.find_all(attrs={"data-embed": True}):
            embed = _html.unescape(b["data-embed"]).strip()
            if embed:
                servers.append({"name": b.get("data-server") or b.get_text(" ", strip=True) or f"Server {len(servers) + 1}",
                                "embed": embed, "language": lang, "lang_key": key})
    if not servers:                                            # data-embed buttons outside a language panel
        for b in soup.find_all(attrs={"data-embed": True}):
            embed = _html.unescape(b["data-embed"]).strip()
            if embed:
                servers.append({"name": b.get("data-server") or f"Server {len(servers) + 1}", "embed": embed,
                                "language": "", "lang_key": ""})
    if not servers:                                            # worker strategy 1: var servers = [{url|link|src, name|label}]
        jm = re.search(r"var\s+servers\s*=\s*(\[[\s\S]*?\]);", page_html, re.I)
        if jm:
            try:
                for i, srv in enumerate(json.loads(jm.group(1))):
                    embed = str(srv.get("url") or srv.get("link") or srv.get("src") or "").replace("\\/", "/").strip()
                    if embed:
                        servers.append({"name": srv.get("name") or srv.get("label") or f"Server {i + 1}", "embed": embed,
                                        "language": "", "lang_key": ""})
            except Exception:
                servers = []
    if not servers:                                            # worker strategy 3: <a data-server href=...>
        for a in soup.find_all("a", attrs={"data-server": True}, href=True):
            href = _html.unescape(a["href"]).strip()
            if href.startswith("//"):
                href = "https:" + href
            if href.startswith("http"):
                servers.append({"name": a.get_text(" ", strip=True) or f"Server {len(servers) + 1}", "embed": href,
                                "language": "", "lang_key": ""})
    if not servers:                                            # iframes (lazy ones keep the url in data-src)
        for f in soup.find_all("iframe"):
            src = f.get("src") or f.get("data-src") or f.get("data-lazy-src") or ""
            if src.startswith("//"):
                src = "https:" + src
            if src.startswith("http") and not _host_ok(src):
                servers.append({"name": f"Server {len(servers) + 1}", "embed": _html.unescape(src), "language": "",
                                "lang_key": ""})
    if not servers:                                            # last resort: known host names anywhere in the page
        blob = _html.unescape(page_html).replace("\\/", "/")
        for m in re.finditer(r"https?://[^\s\"'<>\\]*(?:as-cdn|megaplay|vidmoly|abyssplayer|short\.icu)[^\s\"'<>\\]*",
                             blob, re.I):
            if m.group(0) not in {s["embed"] for s in servers}:
                servers.append({"name": f"Server {len(servers) + 1}", "embed": m.group(0), "language": "", "lang_key": ""})
            if len(servers) >= 6:
                break
    out = []
    for s in servers:
        out.extend(_expand_multi(s))
    return out


def _lang_rank(s: dict) -> int:
    if not PREF_LANG:
        return 0
    hay = f"{s.get('language', '')} {s.get('lang_key', '')}".lower()
    return 0 if PREF_LANG in hay or (PREF_LANG == "hindi" and re.search(r"\bhin\b|\bhi\b", hay)) else 1


_AUDIO_WORDS = {"hindi": ("hi", "hin"), "english": ("en", "eng"), "japanese": ("ja", "jpn"), "tamil": ("ta", "tam"),
                "telugu": ("te", "tel"), "malayalam": ("ml", "mal"), "bengali": ("bn", "ben"), "kannada": ("kn", "kan")}


def _norm_audio(v: str) -> str:
    """Worker's normAudio: "Hindi" / "hi" / "hi-IN" / "hin" -> "hin" (so LANGUAGE= and NAME= compare equal)."""
    s = (v or "").strip().lower()
    if not s:
        return ""
    if s in _AUDIO_WORDS:
        return _AUDIO_WORDS[s][1]
    base = re.split(r"[-_]", s)[0]
    for _, (c2, c3) in _AUDIO_WORDS.items():
        if base in (c2, c3):
            return c3
    return s[:3]


def _audio_for(server: dict) -> str:
    if PREF_AUDIO:
        return PREF_AUDIO
    hay = f"{server.get('language', '')} {server.get('lang_key', '')}".lower()
    for w in _AUDIO_WORDS:
        if w in hay:
            return w
    return PREF_LANG


# -------------------------------------------------------------------------------------------- resolvers ----
_REWRITES = [(re.compile(r"^https?://short\.icu/", re.I), "https://player.abyssplayer.com/"),
             (re.compile(r"^https?://(?:www\.)?abyss\.to/", re.I), "https://player.abyssplayer.com/"),
             (re.compile(r"^https?://(?:www\.)?playhydrax\.com/", re.I), "https://player.abyssplayer.com/")]


def _rewrite(url: str) -> str:
    url = (url or "").replace("\\/", "/").strip()
    if url.startswith("//"):
        url = "https:" + url
    for rx, rep in _REWRITES:
        if rx.match(url):
            return rx.sub(rep, url)
    return url


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/"


def _unpack(src: str):
    """Dean Edwards p.a.c.k.e.r unpacker (vidmoly hides its JWPlayer config in one)."""
    m = re.search(r"}\('(.*?)',\s*(\d+),\s*(\d+),\s*'(.*?)'\.split\('\|'\)", src, re.S)
    if not m:
        return None
    payload, radix, count, words = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4).split("|")
    payload = payload.replace("\\\\", "\\").replace("\\'", "'")
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"

    def enc(n):
        head = "" if n < radix else enc(n // radix)
        n %= radix
        return head + (chr(n + 29) if n > 35 else digits[n])

    table = {}
    for c in range(count - 1, -1, -1):
        table[enc(c)] = (words[c] if c < len(words) else "") or enc(c)
    return re.sub(r"\b\w+\b", lambda mo: table.get(mo.group(0), mo.group(0)), payload)


def _find_media(text: str):
    t = (text or "").replace("\\/", "/")
    m = re.search(r"https?://[^\"'\s<>\\]+\.m3u8[^\"'\s<>\\]*", t, re.I)
    if m:
        return m.group(0)
    m = re.search(r"https?://[^\"'\s<>\\]+\.mp4[^\"'\s<>\\]*", t, re.I)
    return m.group(0) if m else None


def _r_ascdn(embed: str):
    m = re.search(r"/video/([a-f0-9]+)", embed, re.I)
    if not m:
        return None
    vid, root = m.group(1), _origin(embed)
    try:                                                       # only a fallback for the m3u8 + subtitles; the API call below
        page = _get(embed, referer=root).text                  # doesn't need it (the worker also carries on when it fails)
    except FetchError as e:
        logger.info(f"animesalt as-cdn page failed: {e}")
        page = ""
    media = None
    try:
        r = requests.post(f"{root}player/index.php?data={vid}&do=getVideo",
                          headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded",
                                   "X-Requested-With": "XMLHttpRequest", "Referer": embed},
                          data={"hash": vid, "r": ""}, timeout=25)
        if r.ok:
            j = r.json()
            media = j.get("videoSource") or j.get("securedLink")
    except Exception as e:
        logger.info(f"animesalt as-cdn getVideo failed: {e}")
    media = media or _find_media(page)
    return {"src": str(media).replace("\\/", "/"), "referer": root} if media else None


MEGAPLAY_KEY = os.getenv("ANIMESALT_MEGAPLAY_KEY", "i?LMTAx0Q6,:}50U")


def _megaplay_decrypt(enc: str) -> str:
    """AES-256-CBC: first 16 bytes = IV, PKCS7, URL-safe base64, key = the 16 char secret zero-padded to 32 bytes."""
    from Crypto.Cipher import AES
    b64 = enc.replace("-", "+").replace("_", "/")
    raw = base64.b64decode(b64 + "=" * (-len(b64) % 4))
    key = MEGAPLAY_KEY.encode()[:32].ljust(32, b"\0")
    plain = AES.new(key, AES.MODE_CBC, raw[:16]).decrypt(raw[16:])
    try:
        pad = plain[-1]
        if 1 <= pad <= 16:
            plain = plain[:-pad]
    except Exception:
        pass
    text = plain.decode("utf-8", "ignore").replace("\\/", "/")
    m = re.search(r"https?://[^\s\"'}\\]+\.m3u8[^\s\"'}\\]*", text, re.I)
    if m:
        return m.group(0)
    m = re.search(r"/{1,2}([^\s\"'}\\]+\.m3u8[^\s\"'}\\]*)", text, re.I)      # "/fetch.host/path/master.m3u8"
    return "https://" + m.group(1) if m else ""


def _r_megaplay(embed: str):
    page = _get(embed, referer=SITE_REFERER).text
    m = re.search(r'data-id="(\d+)"', page)
    if not m:
        return None
    r = requests.get(f"https://megaplay.buzz/stream/getSourcesNew?id={m.group(1)}",
                     headers={**HEADERS, "Referer": embed, "X-Requested-With": "XMLHttpRequest",
                              "Accept": "application/json, text/plain, */*"}, timeout=25)
    if not r.ok:
        return None
    enc = (r.json() or {}).get("enc")
    media = _megaplay_decrypt(enc) if enc else ""
    return {"src": media, "referer": "https://megaplay.buzz/"} if media else None


def _r_abyss(embed: str):
    hdr = {"User-Agent": UA, "Origin": "https://playhydrax.com", "Referer": "https://playhydrax.com/"}
    r = requests.get(embed, headers=hdr, timeout=25, allow_redirects=True)
    if r.status_code != 200:
        return None
    m = re.search(r'const\s+datas\s*=\s*"([^"]+)"', r.text)
    if not m:
        return None
    d = requests.post("https://enc-dec.app/api/dec-abyss", headers={**hdr, "Content-Type": "application/json",
                                                                   "Accept": "application/json"},
                      data=json.dumps({"text": m.group(1)}), timeout=30)
    if not d.ok:
        return None
    j = d.json() or {}
    sources = (j.get("result") or {}).get("sources") or j.get("sources") or []
    usable = [s for s in sources if s and s.get("url") and s.get("status") is not False]
    if not usable:
        return None
    h264 = [s for s in usable if re.search(r"h264", s.get("codec") or "", re.I)]
    best = (next((s for s in h264 if re.search(r"1080p", s.get("type") or "", re.I)), None)
            or next((s for s in h264 if re.search(r"720p", s.get("type") or "", re.I)), None)
            or sorted(usable, key=lambda s: s.get("size") or 0, reverse=True)[0])
    return {"src": best["url"], "referer": r.url or embed}             # the FULL player page: the CDN checks it


def _r_vidmoly(embed: str):
    page = _get(embed, referer=SITE_REFERER).text
    media = _find_media(page)
    if not media:
        unpacked = _unpack(page)
        media = _find_media(unpacked) if unpacked else None
    return {"src": media, "referer": _origin(embed)} if media else None


_KNOWN_HOSTS = re.compile(r"as-cdn|megaplay|abyssplayer|abyss\.to|playhydrax|vidmoly", re.I)


def _r_generic(embed: str, depth: int = 0):
    """Unknown host / redirector: follow redirects and dispatch on where it landed (worker's followAndResolve), else look
    for a plain m3u8 / mp4 or an iframe on the page."""
    r = _get(embed, referer=SITE_REFERER)
    landed = _rewrite(getattr(r, "url", "") or embed)
    if depth < 2 and landed != embed and _KNOWN_HOSTS.search(urlparse(landed).hostname or ""):
        return resolve_embed(landed, depth + 1)
    page = r.text
    media = _find_media(page)
    if media:
        return {"src": media, "referer": _origin(landed)}
    if depth < 2 and BeautifulSoup is not None:
        f = BeautifulSoup(page, "html.parser").find("iframe")
        inner = _rewrite((f.get("src") or f.get("data-src") or "") if f else "")
        if inner.startswith("http"):
            res = resolve_embed(inner, depth + 1)
            if not res and not _KNOWN_HOSTS.search(urlparse(inner).hostname or ""):
                try:                                           # worker tries the abyss page pattern on unknown inner iframes
                    res = _r_abyss(inner)
                except Exception:
                    res = None
            return res
    return None


def resolve_embed(embed: str, depth: int = 0):
    """Embed URL -> {"src": m3u8/mp4 url, "referer": ...} | None."""
    embed = _rewrite(_html.unescape(embed or ""))
    host = (urlparse(embed).hostname or "").lower()
    if not host:
        return None
    if "as-cdn" in host:
        return _r_ascdn(embed)
    if "megaplay" in host:
        return _r_megaplay(embed)
    if re.search(r"abyssplayer|abyss\.to|playhydrax", host):
        return _r_abyss(embed)
    if "vidmoly" in host:
        return _r_vidmoly(embed)
    return _r_generic(embed, depth)


# ------------------------------------------------------------------------------------------ downloading ----
def _attrs(line: str) -> dict:
    return {k: v.strip('"') for k, v in re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line)}


def _pick_hls(master_url: str, text: str, want_audio: str):
    """Master playlist text -> (video_url, audio_url|None, height). A plain media playlist (or unreadable text) ->
    (master_url, None, 0), and ffmpeg opens it directly."""
    if "#EXT-X-STREAM-INF" not in (text or ""):
        return master_url, None, 0
    lines = [ln.strip() for ln in text.splitlines()]
    variants, audios = [], []
    for i, ln in enumerate(lines):
        if ln.startswith("#EXT-X-MEDIA:"):
            a = _attrs(ln)
            if a.get("TYPE") == "AUDIO" and a.get("URI"):
                audios.append({"group": a.get("GROUP-ID", ""), "lang": (a.get("LANGUAGE") or "").lower(),
                               "name": (a.get("NAME") or "").lower(), "default": a.get("DEFAULT", "").upper() == "YES",
                               "url": urljoin(master_url, a["URI"])})
        elif ln.startswith("#EXT-X-STREAM-INF:"):
            a = _attrs(ln)
            nxt = next((x for x in lines[i + 1:] if x and not x.startswith("#")), "")
            if nxt:
                res = re.match(r"(\d+)x(\d+)", a.get("RESOLUTION", ""))
                variants.append({"bw": int(a.get("BANDWIDTH") or 0), "h": int(res.group(2)) if res else 0,
                                 "audio": a.get("AUDIO", ""), "url": urljoin(master_url, nxt)})
    if not variants:
        return master_url, None, 0
    pool = [v for v in variants if not MAX_HEIGHT or not v["h"] or v["h"] <= MAX_HEIGHT] or variants
    best = max(pool, key=lambda v: (v["h"], v["bw"]))
    cands = [a for a in audios if not best["audio"] or a["group"] == best["audio"]] or audios
    audio = None
    if cands:
        want = _norm_audio(want_audio)
        audio = (next((a for a in cands if want and (_norm_audio(a["lang"]) == want or _norm_audio(a["name"]) == want
                                                     or want_audio in a["name"])), None)
                 or next((a for a in cands if a["default"]), None) or cands[0])
    return best["url"], (audio["url"] if audio else None), best["h"]


def _first_segment(playlist_text: str, base: str):
    for ln in (playlist_text or "").splitlines():
        ln = ln.strip()
        m = re.match(r'#EXT-X-MAP:.*URI="([^"]+)"', ln, re.I)
        if m:
            return urljoin(base, m.group(1))
        if ln and not ln.startswith("#"):
            return urljoin(base, ln)
    return None


def _settle_referer(playlist_url: str, referer: str) -> str:
    """The playlist and its segments can be guarded differently (segments often live on another host): find the referer
    that opens the playlist, then make sure it opens the first segment too (or switch to the one that does)."""
    hit = _probe(playlist_url, referer, 2_000_000)
    if not hit:
        return referer
    ref, _, body = hit
    seg = _first_segment(body.decode("utf-8", "ignore"), playlist_url)
    if seg:
        seg_hit = _probe(seg, ref, 2048)
        if seg_hit:
            ref = seg_hit[0]
    return ref


def _download_hls(master_url: str, referer, out: str, state, want_audio: str, master_text=None) -> bool:
    """ffmpeg stream copy of the best variant (+ the wanted audio track) into one mp4. Segment requests carry the same
    Referer (and an Origin for origin-level referers), which is what the CDNs check."""
    if state is not None:
        state["stage"] = "hls"                   # no byte counter for an ffmpeg HLS pull
    if master_text is None:
        hit = _probe(master_url, referer or SITE_REFERER, 2_000_000)
        master_text, referer = (hit[2].decode("utf-8", "ignore"), hit[0]) if hit else ("", referer)
    video, audio, height = _pick_hls(master_url, master_text, want_audio)
    referer = _settle_referer(video, referer or SITE_REFERER)
    hdr = "".join(f"{k}: {v}\r\n" for k, v in _hdrs(referer).items() if k != "Accept")
    logger.info(f"animesalt hls: {height or '?'}p, audio={'separate' if audio else 'in stream'} <- {video[:90]}")

    def run(vid, aud, bsf):
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
        for u in [vid] + ([aud] if aud else []):
            cmd += ["-headers", hdr, "-allowed_extensions", "ALL",
                    "-protocol_whitelist", "file,http,https,tcp,tls,crypto", "-i", u]
        if aud:
            cmd += ["-map", "0:v:0", "-map", "1:a:0"]
        cmd += ["-c", "copy"] + (["-bsf:a", "aac_adtstoasc"] if bsf else []) + ["-movflags", "+faststart", out]
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=FFMPEG_TIMEOUT)
        except Exception as e:
            logger.warning(f"animesalt hls failed: {e}")
            return False
        if p.returncode != 0:
            logger.warning(f"animesalt hls ffmpeg exit {p.returncode}: {p.stderr.decode(errors='ignore')[-200:]}")
            return False
        return os.path.exists(out) and os.path.getsize(out) > 100 * 1024

    attempts = []
    for pair in ((video, audio), (video, None), (master_url, None)):
        if pair not in attempts:
            attempts.append(pair)
    for vid, aud in attempts:
        for bsf in (True, False):                # the ADTS->ASC filter errors on non-AAC audio: retry without it
            if run(vid, aud, bsf):
                return True
    return False


def _download_direct(url: str, referer, out: str, state) -> bool:
    try:
        r = requests.get(url, headers=_hdrs(referer), stream=True, timeout=(15, 60), allow_redirects=True)
        ctype = r.headers.get("Content-Type", "").lower()
        if r.status_code not in (200, 206) or "text/html" in ctype:
            logger.warning(f"animesalt direct: HTTP {r.status_code} {ctype} for {url[:90]}")
            return False
        total = int(r.headers.get("Content-Length") or 0)
        if state is not None:
            state["total"], state["dl_t0"] = total, time.time()
        done = 0
        with open(out, "wb") as f:
            for chunk in r.iter_content(chunk_size=2 * 1024 * 1024):
                if chunk:
                    f.write(chunk)
                    done += len(chunk)
                    if state is not None:
                        state["done"] = done
        return done > 100 * 1024 and (not total or done >= total)
    except Exception as e:
        logger.warning(f"animesalt direct failed: {e}")
        return False


def _download_media(src: str, referer, out: str, state, want_audio: str) -> bool:
    """HLS or mp4, decided like the worker's proxy (extension / Content-Type / #EXTM3U), with its referer fallback chain."""
    hit = _probe(src, referer, 2_000_000)
    if hit:
        used, ctype, head = hit
        if _is_hls(src, ctype, head):
            return _download_hls(src, used, out, state, want_audio, head.decode("utf-8", "ignore"))
        return _download_direct(src, used, out, state)
    logger.info(f"animesalt: no referer opened {src[:90]}; trying with the resolver's own")
    return (_download_hls(src, referer, out, state, want_audio, "") if ".m3u8" in src.lower()
            else _download_direct(src, referer, out, state))


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\r\n]+', " ", name or "").strip()[:120] or "Episode"


def _page_title(page_html: str) -> str:
    if BeautifulSoup is None:
        return "AnimeSalt"
    soup = BeautifulSoup(page_html, "html.parser")
    h1 = soup.find("h1")
    t = h1.get_text(" ", strip=True) if h1 else _meta(soup, "og:title")
    return _safe(re.sub(r"\s+", " ", _site_suffix(t)))


def download_from_page(url: str, state=None):
    """Blocking. Episode / movie page -> local file, trying the servers in language order until one downloads."""
    try:
        r = _get(url, referer=SITE_REFERER)
    except FetchError as e:
        logger.warning(f"animesalt page failed for {url}: {e}")
        return None
    servers = parse_servers(r.text)
    if not servers:
        logger.info(f"animesalt: no servers on {url}")
        return None
    servers.sort(key=_lang_rank)                  # stable: Hindi first, the site's own order inside a language
    title = _page_title(r.text)
    for s in servers[:MAX_SOURCES]:
        logger.info(f"animesalt: trying {s['name']} [{s['language'] or '-'}] {s['embed'][:80]}")
        try:
            res = resolve_embed(s["embed"])
        except Exception as e:
            logger.info(f"animesalt resolve failed ({s['embed'][:60]}): {e}")
            continue
        if not res or not res.get("src"):
            continue
        try:
            if os.path.exists(OUT_PATH):
                os.remove(OUT_PATH)
        except OSError:
            pass
        src, ref = res["src"], res.get("referer")
        ok = _download_media(src, ref, OUT_PATH, state, _audio_for(s))
        if ok:
            tag = f" [{s['language']}]" if s["language"] else ""
            suggested_names[OUT_PATH] = f"{_safe(title + tag)}.mp4"
            return OUT_PATH
    return None


# ------------------------------------------------------------------------------------------------ search ----
def _parse_se(text: str):
    m = re.search(r"\bs0*(\d{1,2})\s*e0*(\d{1,3})\b", text or "", re.I) or re.search(r"\b(\d{1,2})x(\d{1,3})\b", text or "")
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"\b(?:e|ep|episode)\s*0*(\d{1,3})\b", text or "", re.I)
    return (None, int(m.group(1))) if m else (None, None)


def _search_hits(title: str):
    """Name -> [(title, url)] of shows and movies. Same order as the worker's /api/search: per-type REST search
    (/wp/v2/<series>?search=, /wp/v2/<movies>?search=) first - /wp/v2/search mixes in every episode post and its 10 results
    get crowded out - then /wp/v2/search, then the site's own ?s= page."""
    hits, seen = [], set()

    def add(text, u):
        u = (u or "").split("?")[0].split("#")[0]
        if u and u.rstrip("/") not in seen and _kind_slug(u)[0] in ("tv", "series", "movies", "movie"):
            seen.add(u.rstrip("/"))
            hits.append((_html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()
                         or _kind_slug(u)[1].replace("-", " "), u))

    types = _wp_types()
    for base in [types.get("series"), types.get("movies")]:
        if not base:
            continue
        try:
            for it in _get(f"{UPSTREAM}/wp-json/wp/v2/{base}?search={quote_plus(title)}&per_page=20",
                           referer=SITE_REFERER).json() or []:
                add((it.get("title") or {}).get("rendered"), it.get("link"))
        except Exception as e:
            logger.info(f"animesalt {base} search failed: {e}")
    if hits:
        return hits
    try:
        for it in _get(f"{UPSTREAM}/wp-json/wp/v2/search?search={quote_plus(title)}&per_page=10",
                       referer=SITE_REFERER).json() or []:
            add(it.get("title"), it.get("url"))
    except Exception as e:
        logger.info(f"animesalt wp search failed: {e}")
    if hits or BeautifulSoup is None:
        return hits
    try:
        soup = BeautifulSoup(_get(f"{UPSTREAM}/?s={quote_plus(title)}", referer=SITE_REFERER).text, "html.parser")
        for a in soup.find_all("a", href=True):
            add(a.get_text(" ", strip=True), urljoin(UPSTREAM + "/", a["href"]))
    except Exception as e:
        logger.info(f"animesalt html search failed: {e}")
    return hits


def find_episode_page(query: str, original: str, title_match):
    """Name -> a playable page: a movie page, or the wanted S/E (default 1x1) of the matching show."""
    season, episode = _parse_se(original or query)
    base_title = re.sub(r"\s*(?:\bs\d{1,2}\s*e\d{1,3}\b|\b\d{1,2}x\d{1,3}\b|\b(?:e|ep|episode)\s*\d{1,3}\b).*$", "",
                        original or query, flags=re.I).strip() or query
    for text, href in _search_hits(base_title):
        slug_words = _kind_slug(href)[1].replace("-", " ")
        try:
            if not title_match(base_title, f"{text} {slug_words}")[0]:
                continue
        except Exception:
            continue
        if _kind_slug(href)[0] in ("movies", "movie"):
            if episode is None:
                return href
            continue
        try:
            show = fetch_show(href)
        except Exception as e:
            logger.info(f"animesalt show {href} unreadable: {e}")
            continue
        want_s, want_e = season or 1, episode or 1
        for se in show["seasons"]:
            if se["no"] == want_s:
                for ep in se["episodes"]:
                    if ep["ep"] == want_e:
                        return ep["url"]
    return None


def search_and_download(query: str, original: str, state, title_match):
    page = find_episode_page(query, original, title_match)
    if not page:
        logger.info(f"animesalt: no page found for {query!r}")
        return None
    logger.info(f"animesalt: page {page}")
    return download_from_page(page, state)

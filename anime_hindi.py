"""Hindi-dub anime fallback for the movie/anime download chain (find_movie in downloader.py).

The scraping itself is ported from src--tera_api (Akbots/anime_hindi_scraper.py + anime_hindi_extractors.py, same code
as /animehindi there): Toonstream, AnimeDekho, HindiSubAnime, HindiAnimeVerse and WatchAnimeWorld episode
pages -> provider embeds -> raw stream link (mp4 / m3u8) + the Referer it needs.

This module is the glue for fbot (blocking, no Telegram, so downloader.find_movie can call it through to_thread):

    is_supported(url)                              pasted episode page of one of the sites above?
    download_from_page(url, state)                 -> local path | None   (tries every resolved source, best first)
    search_and_download(query, original, state, title_match)
                                                    -> local path | None   (best effort: WordPress search -> episode page)

A downloaded file's own name is stored in downloader-style SUGGESTED_NAMES (see `suggested_names`) so the uploader can
name it "<Title>.mp4".

Env: ANIME_HINDI_SITES  comma separated site roots used for the name search (default: toonstream.us, animedekho.app, hindisubanime.co, hindianimeverse.org, watchanimeworld.ro)
     ANIME_HINDI_MAX_SOURCES  how many resolved sources to try per page (default 6)
     ANIME_HINDI_FFMPEG_TIMEOUT  seconds for one HLS download (default 3600)
"""
import logging
import os
import re
import subprocess
import time
from urllib.parse import urljoin, urlparse, quote_plus

import requests

try:
    from anime_hindi_scraper import Scraper, validate_url, SUPPORTED_DOMAINS
except Exception as _e:                       # bs4 missing etc. - the fallback just stays off
    Scraper = None
    SUPPORTED_DOMAINS = []

    def validate_url(_u):
        return False
    logging.getLogger(__name__).warning(f"anime_hindi unavailable: {_e}")

logger = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
SEARCH_SITES = [s.strip().rstrip("/") for s in os.getenv(
    "ANIME_HINDI_SITES",
    "https://toonstream.us,https://animedekho.app,https://hindisubanime.co,https://hindianimeverse.org,https://watchanimeworld.ro",
).split(",") if s.strip()]
MAX_SOURCES = int(os.getenv("ANIME_HINDI_MAX_SOURCES", "6"))
FFMPEG_TIMEOUT = int(os.getenv("ANIME_HINDI_FFMPEG_TIMEOUT", "3600"))
OUT_BASE = "temp_hindi_anime"
suggested_names: dict = {}                    # local temp path -> "<Title>.mp4"; downloader merges this into SUGGESTED_NAMES

_EP_RE = re.compile(r"(?:-(\d{1,2})x(\d{1,3})|-s(\d{1,2})e(\d{1,3})|/(\d{1,2})x(\d{1,3})|-season-(\d{1,2})-episode-(\d{1,4}))/?$", re.I)


def is_supported(url: str) -> bool:
    return bool(Scraper) and validate_url(url)


def is_series_page(url: str) -> bool:
    """A show's own page (e.g. hindisubanime.co/serie/<name>/): it has no stream, only the list of episodes."""
    return bool(re.search(r"/(?:serie|series|anime|cartoon|show)s?/[^/]+/?$", urlparse(url).path, re.I))


# ---------- downloading one resolved stream ----------

def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\r\n]+', " ", name or "").strip()[:120] or "Episode"


def _download_direct(url: str, referer, out: str, state) -> bool:
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    try:
        r = requests.get(url, headers=headers, stream=True, timeout=(15, 60), allow_redirects=True)
        ctype = r.headers.get("Content-Type", "").lower()
        if r.status_code not in (200, 206) or "text/html" in ctype:
            logger.warning(f"anime_hindi direct: HTTP {r.status_code} {ctype} for {url[:90]}")
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
        logger.warning(f"anime_hindi direct failed: {e}")
        return False


def _download_hls(url: str, referer, out: str, state) -> bool:
    """ffmpeg reads the playlist (with the Referer header) and stream-copies everything into one mp4."""
    hdr = f"User-Agent: {UA}\r\n" + (f"Referer: {referer}\r\n" if referer else "")
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-headers", hdr,
           "-allowed_extensions", "ALL", "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
           "-i", url, "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", out]
    if state is not None:
        state["stage"] = "hls"                # no byte counter for an ffmpeg HLS pull
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=FFMPEG_TIMEOUT)
        if p.returncode != 0:
            logger.warning(f"anime_hindi hls ffmpeg exit {p.returncode}: {p.stderr.decode(errors='ignore')[-200:]}")
            return False
        return os.path.exists(out) and os.path.getsize(out) > 100 * 1024
    except Exception as e:
        logger.warning(f"anime_hindi hls failed: {e}")
        return False


def _source_rank(item: dict) -> int:
    src = ((item.get("Streaming Links") or {}).get("source") or "").lower()
    if not src:
        return 99
    return 1 if ".m3u8" in src else 0         # plain files first (resumable, no ffmpeg), HLS second


def download_from_page(url: str, state=None):
    """Blocking. Scrape an episode page, try each resolved stream (best first) until one downloads. -> path | None"""
    if not Scraper:
        return None
    try:
        data = Scraper().get_episode_data(url)
    except Exception as e:
        logger.warning(f"anime_hindi scrape failed for {url}: {e}")
        return None
    if not data or not data.get("Details"):
        logger.info(f"anime_hindi: no sources on {url}")
        return None
    title = _safe(data.get("Title") or "Episode")
    items = [i for i in data["Details"] if isinstance(i.get("Streaming Links"), dict) and i["Streaming Links"].get("source")]
    items.sort(key=_source_rank)
    for item in items[:MAX_SOURCES]:
        sl = item["Streaming Links"]
        src, ref = sl["source"], sl.get("referer")
        is_hls = ".m3u8" in src.lower()
        out = f"{OUT_BASE}.mp4"
        try:
            if os.path.exists(out):
                os.remove(out)
        except OSError:
            pass
        logger.info(f"anime_hindi: trying {item.get('Provider')} ({'hls' if is_hls else 'file'}) for {title}")
        ok = _download_hls(src, ref, out, state) if is_hls else _download_direct(src, ref, out, state)
        if ok:
            suggested_names[out] = f"{title}.mp4"
            return out
    return None


# ---------- name search (best effort) ----------

def _parse_se(text: str):
    """'Shinchan S02E05' / '2x5' / 'episode 5' -> (season|None, episode|None)"""
    m = re.search(r"\bs(\d{1,2})\s*e(\d{1,3})\b", text, re.I) or re.search(r"\b(\d{1,2})x(\d{1,3})\b", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"\b(?:e|ep|episode)\s*(\d{1,3})\b", text, re.I)
    return (None, int(m.group(1))) if m else (None, None)


def _episode_se(href: str):
    m = _EP_RE.search(href)
    if not m:
        return None
    g = [x for x in m.groups() if x is not None]
    return int(g[0]), int(g[1])


def _links(sess, url, site):
    try:
        from bs4 import BeautifulSoup
        r = sess.get(url, timeout=15, headers={"User-Agent": UA})
        if r.status_code != 200:
            return []
        host = urlparse(site).hostname or ""
        out = []
        for a in BeautifulSoup(r.text, "html.parser").find_all("a", href=True):
            href = urljoin(url, a["href"])
            if (urlparse(href).hostname or "").endswith(host):
                out.append((a.get_text(" ", strip=True) + " " + (a.get("title") or ""), href))
        return out
    except Exception as e:
        logger.debug(f"anime_hindi search fetch failed {url}: {e}")
        return []


def find_episode_page(query: str, original: str, title_match):
    """WordPress `?s=` search on each known site -> an episode page URL for the requested season/episode (default 1x1).
    Best effort and unverified against the live sites: any miss just returns None so the next fallback runs."""
    if not Scraper:
        return None
    season, episode = _parse_se(original or query)
    want = (season or 1, episode or 1)
    base_title = re.sub(r"\s*(?:\bs\d{1,2}\s*e\d{1,3}\b|\b\d{1,2}x\d{1,3}\b|\b(?:e|ep|episode)\s*\d{1,3}\b).*$", "",
                        original or query, flags=re.I).strip() or query
    sess = requests.Session()
    for site in SEARCH_SITES:
        found = _links(sess, f"{site}/?s={quote_plus(base_title)}", site)
        episodes, series = [], []
        for text, href in found:
            if not title_match(base_title, text + " " + href.rsplit("/", 1)[-1].replace("-", " "))[0]:
                continue
            (episodes if _episode_se(href) else series).append(href)
        for href in episodes:
            if _episode_se(href) == want:
                return href
        for sp in dict.fromkeys(series):          # a series page: look for its episode list
            for _t, href in _links(sess, sp, site):
                if _episode_se(href) == want:
                    return href
        if episodes:                               # right title, but not the wanted episode in the results
            base = episodes[0]
            m = _EP_RE.search(base)
            if m and m.group(1):                   # '<slug>-1x5' style: swap in the wanted numbers
                return re.sub(r"-\d{1,2}x\d{1,3}/?$", f"-{want[0]}x{want[1]}", base)
    return None


def search_and_download(query: str, original: str, state, title_match):
    page = find_episode_page(query, original, title_match)
    if not page:
        logger.info(f"anime_hindi: no episode page found for {query!r}")
        return None
    logger.info(f"anime_hindi: episode page {page}")
    return download_from_page(page, state)


def list_episodes(url: str, limit: int = 120):
    """Blocking. Series page -> [(label 'S1E5', episode_url)] sorted by season/episode. [] when the page lists none
    in its HTML (some sites load the season lists by AJAX)."""
    if not Scraper:
        return []
    try:
        soup = Scraper().fetch_page(url)
    except Exception:
        soup = None
    if not soup:
        return []
    host = urlparse(url).hostname or ""
    seen, eps = set(), []
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"]).split("#")[0]
        if href in seen or not (urlparse(href).hostname or "").endswith(host) or href.rstrip("/") == url.rstrip("/"):
            continue
        se = _episode_se(href)
        if se:
            seen.add(href)
            eps.append((se, href))
    eps.sort(key=lambda x: x[0])
    return [(f"S{se[0]}E{se[1]}", href) for se, href in eps[:limit]]

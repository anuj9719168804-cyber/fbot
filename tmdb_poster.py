"""TMDB poster lookup for the Filmyzilla info card + the uploaded video's thumbnail.

Ported from src's Akbots/movieinfo.py (same search + image-picking logic), rewritten with `requests` because
info_card.fetch_info_card() is blocking and is called through asyncio.to_thread.

    poster_for_title(title)  -> image URL | ""      (title may be a messy Filmyzilla page title / URL slug)

Only the picture comes from TMDB; every other field of the info card stays exactly what Filmyzilla's page says.
Everything is best effort: no key, no match or any network error returns "" and the caller keeps Filmyzilla's own image.
"""
import logging
import re
from functools import lru_cache

import requests

try:
    from config import TMDB_API_KEY
except Exception:  # config unavailable (tests / standalone use)
    import os
    TMDB_API_KEY = os.getenv("TMDB_API_KEY", "61e2290429798c561450eb56b26de19b")

logger = logging.getLogger(__name__)

BASE_URL = "https://api.themoviedb.org/3"
TIMEOUT = 8
MIN_TITLE_SCORE = 0.85   # fuzzy title match needed before a TMDB result is trusted

# Filmyzilla page titles look like:
#   "Kgf Chapter 2 (2022) South Indian Hindi Dubbed Movie Download 480p 720p 1080p"
# Everything from the first of these junk words onward is not part of the movie name.
_JUNK_RE = re.compile(
    r"\b(?:web\s+series|series|season\s*\d+|s\d{1,2}(?:e\d{1,3})?|full\s+movie|movie\s+download|free\s+download|download|south\s+indian|hindi|dubbed|dual\s+audio|"
    r"bollywood|hollywood|tamil|telugu|malayalam|kannada|punjabi|bengali|marathi|bhojpuri|gujarati|"
    r"web[\s-]?dl|web[\s-]?rip|hd[\s-]?rip|hdtv|hdts|hdcam|cam[\s-]?rip|dvd[\s-]?rip|dvd[\s-]?scr|pre[\s-]?dvd|"
    r"blu[\s-]?ray|brrip|bdrip|x264|x265|hevc|esub|4k|2160p|1080p|720p|480p|360p|300mb|org|filmyzilla)\b",
    re.I,
)
_SERIES_RE = re.compile(r"\b(?:web\s+series|series|season\s*\d+|s\d{1,2}(?:e\d{1,3})?)\b", re.I)
_YEAR_RE = re.compile(r"[\(\[]?\b((?:19|20)\d{2})\b[\)\]]?")


def clean_title(raw: str):
    """Messy Filmyzilla title / URL slug -> (name, year|None).  'Kgf Chapter 2 (2022) Hindi Dubbed' -> ('Kgf Chapter 2', '2022')"""
    t = (raw or "").strip()
    t = re.sub(r"\.(?:html?|php)$", "", t, flags=re.I)
    t = re.sub(r"[-_+.]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    # the release year: prefer one in brackets "(2019)", else the last 4-digit year; the name is what comes before it
    years = list(_YEAR_RE.finditer(t))
    year = None
    if years:
        pick = next((y for y in years if y.group(0)[0] in "([" ), years[-1])
        if pick.start() > 0:                       # "1917 Hindi Dubbed": a year at the very start IS the name, keep it
            year, t = pick.group(1), t[:pick.start()]
    j = _JUNK_RE.search(t)
    if j:
        t = t[:j.start()]
    t = re.sub(r"[\(\[\]\)\-:|,]+", " ", t)
    return re.sub(r"\s+", " ", t).strip(), year


def _get(path: str, **params) -> dict:
    params["api_key"] = TMDB_API_KEY
    r = requests.get(f"{BASE_URL}{path}", params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (t or "").lower())


def _similarity(a: str, b: str) -> float:
    """1 - normalized Levenshtein distance (spelling variants like 'Ishq Ishqa' / 'Ishqa Ishqa' stay close)."""
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return 1 - prev[-1] / max(len(a), len(b))


def _best_match(results: list, name: str, media_type: str, year, exact_only: bool):
    """Pick the result whose title really matches `name`. TMDB's results[0] is only 'closest by its own ranking'
    ('Ishq Ishqa' -> 'Ishqa'n De Lekhe'), so every candidate is checked against the wanted title."""
    best, best_score = None, 0.0
    for r in results[:10]:
        titles = [r.get("title") or r.get("name"), r.get("original_title") or r.get("original_name")]
        score = max((_similarity(name, t) for t in titles if t), default=0.0)
        if exact_only and score < 0.999:
            continue
        date = r.get("release_date") if media_type == "movie" else r.get("first_air_date")
        if year and date and date[:4].isdigit() and abs(int(date[:4]) - int(year)) <= 1:
            score += 0.05                       # same year (+-1) wins ties between same-named titles
        if score > best_score:
            best, best_score = r, score
    return best if best_score >= MIN_TITLE_SCORE else None


def _search(name: str, year, media_type: str, exact_only: bool = False):
    p = {"query": name}
    if year:
        p["year" if media_type == "movie" else "first_air_date_year"] = year
    res = _get(f"/search/{media_type}", **p).get("results") or []
    return _best_match(res, name, media_type, year, exact_only)


def _image_url(media_id, media_type: str) -> str:
    """Same pick order as src's get_poster_url: Hindi backdrop > English backdrop > poster > any backdrop."""
    try:
        data = _get(f"/{media_type}/{media_id}/images", include_image_language="hi,en,null")
    except Exception:
        return ""
    backdrops, posters = data.get("backdrops") or [], data.get("posters") or []
    for lang in ("hi", "en"):
        for b in backdrops:
            if b.get("iso_639_1") == lang and b.get("file_path"):
                return f"https://media.themoviedb.org/t/p/w1000_and_h563_face{b['file_path']}"
    if posters and posters[0].get("file_path"):
        return f"https://image.tmdb.org/t/p/original{posters[0]['file_path']}"
    if backdrops and backdrops[0].get("file_path"):
        return f"https://media.themoviedb.org/t/p/w1000_and_h563_face{backdrops[0]['file_path']}"
    return ""


@lru_cache(maxsize=256)
def _lookup(name: str, year, is_series: bool = False) -> str:
    # 'S01 / Series' titles check TV first, others movie first. With the year first (most exact); the no-year retry
    # (Filmyzilla's year is sometimes off) is only accepted for an EXACT title match, so a different film that
    # merely looks similar is never used.
    types = ("tv", "movie") if is_series else ("movie", "tv")
    for y in ((year, None) if year else (None,)):
        for media_type in types:
            hit = _search(name, y, media_type, exact_only=(y is None and bool(year)))
            if hit:
                url = _image_url(hit["id"], media_type)
                if not url and hit.get("poster_path"):
                    url = f"https://image.tmdb.org/t/p/original{hit['poster_path']}"
                if url:
                    return url
    return ""


def poster_for_title(title: str) -> str:
    """Blocking. Returns a TMDB image URL for a (messy) movie title, or '' when nothing was found."""
    if not TMDB_API_KEY:
        return ""
    name, year = clean_title(title)
    if not name:
        return ""
    is_series = bool(_SERIES_RE.search(title or ""))
    try:
        return _lookup(name, year, is_series)
    except Exception as e:
        logger.warning(f"TMDB poster lookup failed for {title!r}: {e}")
        return ""

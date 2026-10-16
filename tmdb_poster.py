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
    TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")

logger = logging.getLogger(__name__)

BASE_URL = "https://api.themoviedb.org/3"
TIMEOUT = 8

# Filmyzilla page titles look like:
#   "Kgf Chapter 2 (2022) South Indian Hindi Dubbed Movie Download 480p 720p 1080p"
# Everything from the first of these junk words onward is not part of the movie name.
_JUNK_RE = re.compile(
    r"\b(?:full\s+movie|movie\s+download|free\s+download|download|south\s+indian|hindi|dubbed|dual\s+audio|"
    r"bollywood|hollywood|tamil|telugu|malayalam|kannada|punjabi|bengali|marathi|bhojpuri|gujarati|"
    r"web[\s-]?dl|web[\s-]?rip|hd[\s-]?rip|hdtv|hdts|hdcam|cam[\s-]?rip|dvd[\s-]?rip|dvd[\s-]?scr|pre[\s-]?dvd|"
    r"blu[\s-]?ray|brrip|bdrip|x264|x265|hevc|esub|4k|2160p|1080p|720p|480p|360p|300mb|org|filmyzilla)\b",
    re.I,
)
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


def _search(name: str, year, media_type: str):
    p = {"query": name}
    if year:
        p["year" if media_type == "movie" else "first_air_date_year"] = year
    res = _get(f"/search/{media_type}", **p).get("results") or []
    return res[0] if res else None


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
def _lookup(name: str, year) -> str:
    # movie first, then TV; with the year first (most exact), then without it (Filmyzilla's year is sometimes off)
    for y in ((year, None) if year else (None,)):
        for media_type in ("movie", "tv"):
            hit = _search(name, y, media_type)
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
    try:
        return _lookup(name, year)
    except Exception as e:
        logger.warning(f"TMDB poster lookup failed for {title!r}: {e}")
        return ""

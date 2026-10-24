import os
import sys
import re
import json
import time
import asyncio
import subprocess
import requests
import base64
import unicodedata
import difflib
import socket

from urllib.parse import urljoin, quote, quote_plus, unquote, urlparse, urlsplit, parse_qs, urlencode
from telethon import TelegramClient
from telethon.sessions import StringSession

try:
    import anime_hindi          # Hindi-dub anime fallback (ported from src: Toonstream / AnimeDekho / HindiSubAnime / ...)
except Exception as _e:         # never let the optional fallback stop the bot from starting
    anime_hindi = None
    print(f"[-] anime_hindi fallback unavailable: {_e}")
try:
    import meowly_dl           # Meowly / Vidly (TMDB id -> HLS), see meowly_dl.py
except Exception:
    meowly_dl = None
try:
    import animesalt            # AnimeSalt (animesalt.cam), ported from the Anime-Salt worker - same flow as AnimaHD
except Exception as _e:         # optional source: never let it stop the bot from starting
    animesalt = None
    print(f"[-] animesalt source unavailable: {_e}")
try:  # Google Drive sync/upload only - a missing package must not stop the whole bot from booting
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    GOOGLE_API_OK = True
except ImportError:
    Credentials = build = MediaFileUpload = None
    GOOGLE_API_OK = False

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

# Every setting comes ONLY from fbot's config.py (single source of truth - no copies or fallbacks here)
from config import (
    API_ID, API_HASH, SESSION,
    MOVIE_NAME, RUN_MODE, ACTIVE_TELEGRAM_BOTS, FILMYZILLA_DOMAIN, ANIMAHD_DOMAIN, ANIMAHD_GATE_DOMAIN, DOWNLOADER_SOCKET_TIMEOUT,
    GCP_CLIENT_ID, GCP_CLIENT_SECRET, GCP_REFRESH_TOKEN, GOOGLE_TOKEN_URI, DRIVE_FOLDER_ID,
)

socket.setdefaulttimeout(DOWNLOADER_SOCKET_TIMEOUT)

SESSION_STRING = SESSION
ACTIVE_BOTS = list(ACTIVE_TELEGRAM_BOTS)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

STOP_WORDS = {"the", "a", "an", "of", "in", "on", "and", "or", "to", "for", "with", "at", "by", "hindi", "dubbed", "english", "movie", "series"}

# =========================================================
# SMART QUERY VARIATION ENGINE
# =========================================================
def generate_search_variations(raw_query):
    variations = []
    
    def add_var(v):
        if v and v not in variations:
            variations.append(v)
            
    # 1. Original Exact Query
    add_var(raw_query)
    
    # 2. Normalized Unicode (e.g., Naruto Shippūden -> Naruto Shippuden)
    nfkd_form = unicodedata.normalize('NFKD', raw_query)
    ascii_str = "".join([c for c in nfkd_form if not unicodedata.combining(c)])
    add_var(ascii_str)
    
    # 3. Base Title Stripped of Season/Episode Tags safely
    base_title = re.sub(r'\s*[sS](?:eason)?\s*\d{1,2}\s*[eE](?:p|pisode)?\s*\d{1,3}.*', '', ascii_str, flags=re.IGNORECASE).strip()
    base_title = re.sub(r'\s*[eE](?:p|pisode)?\s*\d{1,3}.*', '', base_title, flags=re.IGNORECASE).strip()
    add_var(base_title)
    
    # 4. Punctuation Replaced with Spaces (Spider-Man -> Spider Man)
    spaced = re.sub(r'[^a-zA-Z0-9\s]', ' ', base_title)
    spaced = re.sub(r'\s+', ' ', spaced).strip()
    add_var(spaced)
    
    # 5. Completely Compacted (Spider-Man -> Spiderman)
    compacted = re.sub(r'[^a-zA-Z0-9]', '', base_title)
    add_var(compacted)
    
    return variations


def set_github_output(name, value):
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        try:
            with open(output_file, "a", encoding="utf-8") as f:
                f.write(f"{name}={value}\n")
        except Exception:
            pass

def clean_text(text):
    if not text:
        return ""
    normalized = unicodedata.normalize('NFKD', str(text)).encode('ascii', 'ignore').decode('utf-8')
    return re.sub(r'[^a-zA-Z0-9]+', ' ', normalized).lower().strip()

def compact_text(text):
    if not text:
        return ""
    normalized = unicodedata.normalize('NFKD', str(text)).encode('ascii', 'ignore').decode('utf-8')
    return re.sub(r'[^a-zA-Z0-9]', '', normalized).lower().strip()

def sanitize_title(title):
    clean = unicodedata.normalize('NFKD', title).encode('ascii', 'ignore').decode('utf-8')
    se_match = re.search(r'\b(?:s|season)\s*(\d{1,2})\s*(?:e|ep|episode)\s*(\d{1,2})\b', clean, re.IGNORECASE)
    es_match = re.search(r'\b(?:e|ep|episode)\s*(\d{1,2})\s*(?:s|season)\s*(\d{1,2})\b', clean, re.IGNORECASE)
    e_match = re.search(r'\b(?:e|ep|episode)\s*(\d{1,2})\b', clean, re.IGNORECASE)
    
    if se_match:
        base = re.sub(r'\b(?:s|season)\s*\d{1,2}\s*(?:e|ep|episode)\s*\d{1,2}\b', '', clean, flags=re.IGNORECASE).strip()
        return f"{base.title()} S{int(se_match.group(1)):02d}E{int(se_match.group(2)):02d}"
    elif es_match:
        base = re.sub(r'\b(?:e|ep|episode)\s*\d{1,2}\s*(?:s|season)\s*\d{1,2}\b', '', clean, flags=re.IGNORECASE).strip()
        return f"{base.title()} S{int(es_match.group(2)):02d}E{int(es_match.group(1)):02d}"
    elif e_match:
        base = re.sub(r'\b(?:e|ep|episode)\s*\d{1,2}\b', '', clean, flags=re.IGNORECASE).strip()
        return f"{base.title()} S01E{int(e_match.group(1)):02d}"
    else:
        return clean.title()

def get_quality_score(text):
    t = str(text).lower()
    if re.search(r'\b(1080p|1080|fhd|full\s*hd)\b', t): return 100
    if re.search(r'\b(720p|720)\b', t): return 90
    if re.search(r'\b(hd|high\s*def)\b', t): return 80
    if re.search(r'\b(2160p|4k|uhd)\b', t): return -50
    if re.search(r'\b(480p|360p|240p|sd|dvdrip)\b', t): return 30
    if re.search(r'\b(camrip|cam|ts)\b', t): return 20
    return 40

def is_title_match(target_title, candidate_title):
    t_clean = clean_text(target_title)
    c_clean = clean_text(candidate_title)
    t_compact = compact_text(target_title)
    c_compact = compact_text(candidate_title)
    
    if t_compact and c_compact:
        if t_compact in c_compact or c_compact in t_compact:
            return True, 1.0

    t_words = [w for w in t_clean.split() if w not in STOP_WORDS]
    if not t_words:
        t_words = t_clean.split()
        
    c_words = set(c_clean.split())
    if not t_words or not c_words:
        return False, 0.0

    matched = [w for w in t_words if w in c_words]
    ratio = len(matched) / len(t_words)

    if ratio >= 0.50:
        return True, ratio
    return False, ratio

def get_drive_service():
    if not GOOGLE_API_OK:
        raise RuntimeError("Google Drive libraries are missing - run: pip install google-api-python-client google-auth google-auth-httplib2")
    creds = Credentials(
        token=None,
        refresh_token=GCP_REFRESH_TOKEN,
        token_uri=GOOGLE_TOKEN_URI,
        client_id=GCP_CLIENT_ID,
        client_secret=GCP_CLIENT_SECRET
    )
    return build('drive', 'v3', credentials=creds)

def sync_movies_json():
    folder_id = DRIVE_FOLDER_ID
    service = get_drive_service()
    query = f"'{folder_id}' in parents and trashed = false"
    results = service.files().list(q=query, pageSize=200, fields="files(id, name, webViewLink, webContentLink)").execute(num_retries=3)
    
    movie_data = [{"id": f.get("id"), "name": f.get("name"), "webViewLink": f.get("webViewLink"), "webContentLink": f.get("webContentLink", "#")} for f in results.get('files', [])]
    with open("movies.json", "w", encoding="utf-8") as f:
        json.dump(movie_data, f, indent=4)
    print("[*] movies.json synced successfully.")

def install_browser_engine():
    try:
        import playwright
    except ImportError:
        print("[*] Installing headless browser engine...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright"])
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
        print("[+] Headless browser ready.")

def decode_base64_string(encoded_str):
    try:
        encoded_str = encoded_str.replace('-', '+').replace('_', '/')
        encoded_str += "=" * ((4 - len(encoded_str) % 4) % 4)
        return base64.b64decode(encoded_str).decode('utf-8')
    except Exception:
        return None

# =========================================================
# SCRAPERS
# =========================================================
def _fz_host() -> str:
    return urlsplit(FILMYZILLA_DOMAIN).netloc or "www.filmyzilla74.com"


def _unwrap_search_href(href: str) -> str:
    """Google /url?q=..., DuckDuckGo uddg=... and Bing ck/a u=a1<base64> redirect links -> the real URL."""
    if href.startswith("//"):
        href = "https:" + href
    parts = urlsplit(href)
    qs = parse_qs(parts.query)
    if href.startswith("/url?") or (parts.netloc.endswith("google.com") and parts.path == "/url"):
        return (qs.get("q") or qs.get("url") or [""])[0]
    if "duckduckgo.com" in parts.netloc and qs.get("uddg"):
        return qs["uddg"][0]
    if "bing.com" in parts.netloc and parts.path.startswith("/ck/") and qs.get("u"):
        u = qs["u"][0]
        if u.startswith("a1"):
            dec = decode_base64_string(u[2:])
            if dec:
                return dec
    return href


# (name, url, params builder). Filmyzilla has no search of its own - its search box just opens Google with
# "<title> site:<host>" - so the bot does the same, and falls back to Bing / DuckDuckGo if Google blocks the request.
# Each builder gets (title, host). The first two are the same Google search two ways: "<title> site:<host>" and the
# exact mobile URL Filmyzilla's own search box opens (google.com/m/search?q=<title>&as_sitesearch=<host>).
_FZ_SEARCH_ENGINES = (
    ("Google", "https://www.google.com/search", lambda t, h: {"q": f"{t} site:{h}", "num": "20", "hl": "en"}),
    ("Google (as_sitesearch)", "https://www.google.com/m/search", lambda t, h: {"q": t, "as_sitesearch": h}),
    ("Bing", "https://www.bing.com/search", lambda t, h: {"q": f"{t} site:{h}", "count": "20", "setlang": "en"}),
    ("DuckDuckGo", "https://html.duckduckgo.com/html/", lambda t, h: {"q": f"{t} site:{h}"}),
)


def _fz_parse_results(html: str):
    """Filmyzilla links (movies, categories) out of a search-engine results page."""
    soup = BeautifulSoup(html or "", "html.parser")
    movies, cats, seen = [], [], set()
    for a in soup.find_all("a", href=True):
        href = _unwrap_search_href(a["href"]).split("#", 1)[0]
        parts = urlsplit(href)
        if "filmyzilla" not in parts.netloc.lower() or href in seen:
            continue
        path = parts.path
        slug = path.rsplit("/", 1)[-1].replace(".html", "").replace("-", " ")
        text = f"{a.get_text(' ', strip=True)} {slug}".strip()
        if any(seg in path for seg in ("/movie/", "/series/", "/files/")):
            seen.add(href)
            movies.append((text, href))
        elif "/category/" in path:
            seen.add(href)
            cats.append((a.get_text(" ", strip=True) or slug, href))
    return movies, cats


def _fz_search_proxy() -> str:
    """SEARCH_PROXY=http://user:pass@host:port - proxy used ONLY for the Filmyzilla web search (Google/Bing/DDG
    and the headless Chrome Google stage). Empty = direct. Use an IP Google trusts (residential/mobile), not a
    random free proxy, which Google usually flags."""
    return os.environ.get("SEARCH_PROXY", "").strip()


def _fz_search_requests(query: str) -> dict:
    """Plain-HTTP engines (Google, Google m/search, Bing, DuckDuckGo)."""
    """Same as typing '<query> site:www.filmyzilla74.com' into Google. Returns
    {"movies": [(text, url)], "categories": [(text, url)]} - only links on the Filmyzilla host.
    The text includes the URL slug so titles still match when the link text is generic."""
    empty = {"movies": [], "categories": []}
    if not BeautifulSoup or not query:
        return empty
    host = _fz_host()
    for name, url, params in _FZ_SEARCH_ENGINES:
        try:
            sp = _fz_search_proxy()
            r = requests.get(url, params=params(query, host), headers={**HEADERS, "Accept-Language": "en-US,en;q=0.9"},
                             timeout=15, proxies={"http": sp, "https": sp} if sp else None)
        except Exception as e:
            print(f"[-] {name} search error: {e}")
            continue
        if r.status_code != 200:
            print(f"[-] {name} search returned HTTP {r.status_code} (may be rate-limited / blocked).")
            continue
        movies, cats = _fz_parse_results(r.text)
        print(f"[*] {name} 'site:' search: {len(movies)} movie page(s), {len(cats)} category page(s).")
        if movies or cats:
            return {"movies": movies, "categories": cats}
    return empty


_FZ_SEARCH_CACHE = {}          # query -> (timestamp, result); 30 min, so repeated searches don't hit Google
_FZ_CACHE_TTL = 1800


def _fz_browser_search(query: str) -> dict:
    """Last resort when plain HTTP is blocked: Google serves a 'enablejs' page to non-browser clients, so this runs
    the same 'site:' search in a real headless Chrome (patchright, else playwright) - the same way the phone's
    browser gets results. Uses the real Chrome browser_bootstrap finds (never the Ubuntu snap shim)."""
    empty = {"movies": [], "categories": []}
    if not query:
        return empty
    try:
        import browser_bootstrap as bb
        chrome = bb.find_chrome()
    except Exception:
        chrome = None
    try:
        from patchright.sync_api import sync_playwright as _pw
    except Exception:
        try:
            from playwright.sync_api import sync_playwright as _pw
        except Exception:
            print("[-] Browser search skipped: patchright/playwright is not installed.")
            return empty
    host = _fz_host()
    url = "https://www.google.com/search?" + urlencode({"q": f"{query} site:{host}", "num": "20", "hl": "en"})
    html = ""
    try:
        with _pw() as pw:
            kw = {"headless": not bool(os.environ.get("DISPLAY")),
                  "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"]}
            if chrome:
                kw["executable_path"] = chrome
            sp = _fz_search_proxy()
            if sp:
                pu = urlsplit(sp)
                px = {"server": f"{pu.scheme}://{pu.hostname}:{pu.port}"}
                if pu.username:
                    px["username"] = unquote(pu.username)
                    px["password"] = unquote(pu.password or "")
                kw["proxy"] = px
            browser = pw.chromium.launch(**kw)
            try:
                ctx = browser.new_context(user_agent=HEADERS.get("User-Agent"), locale="en-US",
                                          viewport={"width": 1280, "height": 900})
                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                for sel in ('button:has-text("Accept all")', 'button:has-text("I agree")', 'button:has-text("Accept")'):
                    try:
                        page.locator(sel).first.click(timeout=2000)   # EU / consent interstitial, if shown
                        break
                    except Exception:
                        pass
                try:
                    page.wait_for_selector("div#search, div#rso, div.g, a h3", timeout=12000)
                except Exception:
                    pass
                html = page.content()
                final_url = page.url
                title = page.title()
            finally:
                browser.close()
    except Exception as e:
        print(f"[-] Browser Google search error: {str(e)[:200]}")
        return empty
    low = (html or "").lower()
    if "/sorry/" in final_url or "unusual traffic" in low or "captcha" in low or "recaptcha" in low:
        print("[-] Browser Google search: Google showed a CAPTCHA / 'unusual traffic' page - this server is blocked.")
        return empty
    movies, cats = _fz_parse_results(html)
    print(f"[*] Browser Google search: {len(movies)} movie page(s), {len(cats)} category page(s).")
    if not movies and not cats:
        print(f"[*] Browser Google search diagnostics: title={title!r} url={final_url[:120]}")
    return {"movies": movies, "categories": cats}


def _fz_cse_search(query: str) -> dict:
    """Google Programmable Search (Custom Search JSON API). Needs GOOGLE_CSE_KEY (API key) and GOOGLE_CSE_ID (cx).
    Free tier = 100 queries/day, no captcha. Skipped silently when the key is not set."""
    empty = {"movies": [], "categories": []}
    key = os.environ.get("GOOGLE_CSE_KEY", "").strip()
    cx = os.environ.get("GOOGLE_CSE_ID", "").strip()
    if not key or not cx or not query:
        return empty
    try:
        r = requests.get(
            "https://www.googleapis.com/customsearch/v1",
            params={"key": key, "cx": cx, "q": f"{query} site:{_fz_host()}", "num": 10},
            timeout=15,
        )
        if r.status_code != 200:
            print(f"[-] Google CSE returned HTTP {r.status_code}: {r.text[:160]}")
            return empty
        items = r.json().get("items", []) or []
    except Exception as e:
        print(f"[-] Google CSE error: {str(e)[:160]}")
        return empty
    from html import escape as _esc
    fake = "".join(f'<a href="{_esc(it.get("link", ""))}">{_esc(it.get("title", ""))}</a>' for it in items)
    movies, cats = _fz_parse_results(fake)
    print(f"[*] Google CSE: {len(movies)} movie page(s), {len(cats)} category page(s).")
    return {"movies": movies, "categories": cats}


def _fz_web_search(query: str) -> dict:
    """Same as typing '<query> site:<filmyzilla host>' into Google. Order: cache -> plain HTTP engines ->
    headless Chrome Google search (when HTTP is blocked). Returns {"movies": [...], "categories": [...]}."""
    key = (query or "").strip().lower()
    hit = _FZ_SEARCH_CACHE.get(key)
    if hit and time.time() - hit[0] < _FZ_CACHE_TTL:
        return hit[1]
    res = _fz_cse_search(query)
    if not res["movies"] and not res["categories"]:
        res = _fz_search_requests(query)
    if not res["movies"] and not res["categories"]:
        print("[*] Plain HTTP search found nothing - trying headless Chrome Google search...")
        res = _fz_browser_search(query)
    if res["movies"] or res["categories"]:
        _FZ_SEARCH_CACHE[key] = (time.time(), res)
    return res


def _google_filmyzilla_links(query):
    """Kept for older callers: the movie pages from the web 'site:' search."""
    return _fz_web_search(query)["movies"]


_FZ_NUMWORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
                "eight": "8", "nine": "9", "ten": "10", "ii": "2", "iii": "3", "iv": "4", "vi": "6", "vii": "7",
                "viii": "8", "ix": "9"}
_FZ_ARTICLES = {"the", "a", "an"}


def _fz_norm_word(w: str) -> str:
    return _FZ_NUMWORDS.get(w, w)


def _fz_word_eq(a: str, b: str) -> bool:
    """Same word, allowing small spelling differences (bahubali / baahubali, pushpa / pushpaa) for words of 5+ letters."""
    if a == b:
        return True
    if len(a) >= 5 and len(b) >= 5 and not a.isdigit() and not b.isdigit():
        return difflib.SequenceMatcher(None, a, b).ratio() >= 0.85
    return False


def _fz_title_match(target: str, candidate: str):
    """is_title_match() for Filmyzilla only, tolerant of related wording: 'kgf 2' -> 'KGF Chapter 2',
    'kgf chapter two' -> 'KGF Chapter 2', 'bahubali 2' -> 'Baahubali 2: The Conclusion'.
    The words that are not numbers (kgf, pushpa, ...) must really be in the title, so 'kgf 2' never matches 'Raja 2'."""
    ok, ratio = is_title_match(target, candidate)
    if ok and ratio >= 1.0:
        return True, 1.0
    t_words = [_fz_norm_word(w) for w in clean_text(target).split() if w not in STOP_WORDS]
    if not t_words:
        t_words = [_fz_norm_word(w) for w in clean_text(target).split()]
    c_words = [_fz_norm_word(w) for w in clean_text(candidate).split()]
    if not t_words or not c_words:
        return ok, ratio
    hits = [any(_fz_word_eq(w, c) for c in c_words) for w in t_words]
    ratio2 = sum(hits) / len(t_words)
    core = [h for w, h in zip(t_words, hits) if not w.isdigit()]
    if core:
        need = len(core) if len(core) <= 2 else -(-len(core) * 2 // 3)   # all of 1-2 words, else two thirds
        if sum(core) < need:
            return False, ratio2
    return (ratio2 >= 0.5), max(ratio2, ratio if ok else 0.0)


def _fz_letters(title: str) -> list:
    """Letters whose alphabet index may list the title: its first letter, and the first letter after a leading
    'The' / 'A' / 'An' (the site may file 'The Kashmir Files' under K)."""
    words = [w for w in clean_text(title).split() if w]
    out = []
    for w in words[:2]:
        if w[0].isalpha() and w[0] not in out:
            out.append(w[0])
        if w not in _FZ_ARTICLES:
            break
    if not out:
        c = next((c.lower() for c in (title or "") if c.isascii() and c.isalpha()), "")
        out = [c] if c else []
    return out


_FZ_NON_MOVIE_RE = re.compile(r"trailer|teaser|\bclip|promo|\bsample\b|\bsong\b|\bscene|behind[\s-]+the[\s-]+scenes|\bmaking\b|interview|\breview", re.I)
_FZ_JUNK_WORDS = {"hindi", "dubbed", "dub", "movie", "download", "south", "indian", "full", "hd", "web", "dl",
                  "tv", "serial", "series", "season", "free", "org", "bluray", "hdrip", "720p", "1080p", "480p"}


def _fz_clean_match_title(title: str) -> str:
    t = re.sub(r"[\(\[]?\b(?:19|20)\d{2}\b[\)\]]?", " ", title or "")
    words = [w for w in re.split(r"[^A-Za-z0-9]+", t) if w and w.lower() not in _FZ_JUNK_WORDS]
    return " ".join(words) or (title or "").strip()


_FZ_CATEGORY_BASE_RE = re.compile(r"(/category/\d+/[^/\"'<>?#\s]+)", re.I)


def _fz_category_bases(max_n: int = 8) -> list:
    """Category base URLs (/category/<id>/<name>) linked from the Filmyzilla home page."""
    try:
        r = requests.get(FILMYZILLA_DOMAIN.rstrip("/") + "/", headers=HEADERS, timeout=12)
        if r.status_code != 200:
            return []
    except Exception:
        return []
    found = []
    for m in _FZ_CATEGORY_BASE_RE.finditer(r.text):
        b = urljoin(FILMYZILLA_DOMAIN, m.group(1))
        if b not in found:
            found.append(b)
    # South / Hindi-dubbed categories first: that is where titles like RRR live. The URL below is confirmed in the HAR.
    seed = [urljoin(FILMYZILLA_DOMAIN, "/category/60/Latest-south-indian-hindi-dubbed-movies-2022")]
    ordered = seed + sorted(found, key=lambda b: not re.search(r"dubbed|south", b, re.I))
    bases = []
    for b in ordered:
        if b not in bases:
            bases.append(b)
        if len(bases) >= max_n:
            break
    return bases


def _fz_alphabet_soup(title: str, max_categories: int = 25, max_pages: int = 10):
    """Walk each category's alphabet index for the title's first letter until a page lists a matching movie.
    -> BeautifulSoup of that page (anchors are matched by the caller), or None."""
    if not BeautifulSoup or not title:
        return None
    letters = _fz_letters(title)
    if not letters:
        return None
    for base in _fz_category_bases(max_categories):
        for letter in letters:
            for n in range(1, max_pages + 1):
                try:
                    r = requests.get(f"{base}/alphabet/{letter}/{n}.html", headers=HEADERS, timeout=12)
                except Exception:
                    break
                if r.status_code != 200 or "/movie/" not in r.text:
                    break
                soup = BeautifulSoup(r.text, "html.parser")
                for a in soup.find_all("a", href=True):
                    if "/movie/" in a["href"] and _fz_title_match(title, a.get_text(strip=True))[0]:
                        return soup
    return None


def _fz_score_soup(page_soup, match_title):
    """(ratio, quality, text, url) for every movie / series link on a page whose text matches the title."""
    out = []
    for a in page_soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        if _FZ_NON_MOVIE_RE.search(text) or _FZ_NON_MOVIE_RE.search(href):
            continue  # trailers / clips / samples / songs share the movie's title: never the movie itself
        if any(seg in href for seg in ["/movie/", "/files/", "/series/", "/file/"]):
            matched, ratio = _fz_title_match(match_title, text)
            if matched:
                out.append((ratio, get_quality_score(text), text, urljoin(FILMYZILLA_DOMAIN, href)))
    return out


def _fz_category_find(cat_url: str, title: str, max_pages: int = 15, shallow: bool = False) -> list:
    """The web search gave a category page: look for the movie inside that category. Order: the page itself,
    the category's alphabet index for the title's first letter, then its plain listing pages."""
    m = _FZ_CATEGORY_BASE_RE.search(cat_url)
    base = urljoin(FILMYZILLA_DOMAIN, m.group(1)) if m else ""
    groups = [[cat_url]]
    if shallow:   # only the exact page the web search returned (1 request)
        base = ""
    if base:
        # e.g. /category/60/Latest-south-indian-hindi-dubbed-movies-2022/alphabet/r/2.html  (RRR sits on page 2)
        for letter in _fz_letters(title):
            groups.append([f"{base}/alphabet/{letter}.html"])
            groups.append([f"{base}/alphabet/{letter}/{n}.html" for n in range(1, max_pages + 1)])
    if base:
        groups.append([f"{base}/default/{n}.html" for n in range(1, max_pages + 1)])
    for pages in groups:
        for url in pages:
            try:
                r = requests.get(url, headers=HEADERS, timeout=12)
            except Exception:
                break
            if r.status_code != 200 or "/movie/" not in r.text:
                break  # ran past the last page of this listing
            found = _fz_score_soup(BeautifulSoup(r.text, "html.parser"), title)
            if found:
                return found
    return []


def try_filmyzilla_scrape(search_query, original_query, state=None):
    if not BeautifulSoup: return None
    print(f"\n[Source: Filmyzilla] Searching {FILMYZILLA_DOMAIN} for: '{search_query}'...")
    session = requests.Session()
    session.headers.update(HEADERS)
    
    # Extract episode requirements from original query
    episode_match = re.search(r'\b(?:e|ep|episode)\s*(\d{1,2})\b', original_query, re.IGNORECASE)
    if not episode_match:
        episode_match = re.search(r'\bs\d{1,2}e(\d{1,2})\b', original_query, re.IGNORECASE)
    
    ascii_orig = "".join([c for c in unicodedata.normalize('NFKD', original_query) if not unicodedata.combining(c)])
    base_match_title = re.sub(r'\s*[sS](?:eason)?\s*\d{1,2}\s*[eE](?:p|pisode)?\s*\d{1,3}.*', '', ascii_orig).strip()
    if not base_match_title: base_match_title = ascii_orig

    slug = re.sub(r'[^a-zA-Z0-9]+', '-', search_query).strip('-').lower()
    search_urls = [
        f"{FILMYZILLA_DOMAIN}/search/{slug}.html",
        f"{FILMYZILLA_DOMAIN}/search.php?q={quote_plus(search_query)}"
    ]
    
    soup = None
    for s_url in search_urls:
        try:
            r = session.get(s_url, timeout=12)
            if r.status_code == 200 and len(r.text) > 800:
                temp_soup = BeautifulSoup(r.text, "html.parser")
                found = [a for a in temp_soup.find_all("a", href=True) if any(k in a['href'] for k in ["/movie/", "/files/", "/file/"])]
                if found:
                    soup = temp_soup
                    print(f"[*] Search matched using query: '{search_query}'")
                    break
        except Exception:
            continue

    # Title used for matching: year and junk words removed ("RRR 2022 Hindi dubbed" -> "RRR").
    match_title = _fz_clean_match_title(base_match_title)

    def _score(page_soup):
        return _fz_score_soup(page_soup, match_title)

    # A search page only counts if it actually lists the wanted title (the old /search URLs return unrelated pages).
    scored_candidates = _score(soup) if soup else []
    site_search_ok = bool(scored_candidates)
    if not site_search_ok:
        # Filmyzilla has no working search (its search box opens Google): do the same "<title> site:<host>" search.
        print(f"[-] Filmyzilla's own search found nothing. Web search: '{match_title} site:{_fz_host()}' ...")
        web = _fz_web_search(match_title)
        for text, href in web["movies"]:
            if _FZ_NON_MOVIE_RE.search(text) or _FZ_NON_MOVIE_RE.search(href):
                continue
            matched, ratio = _fz_title_match(match_title, text)
            if matched:
                scored_candidates.append((ratio, get_quality_score(text), text, href))
        if not scored_candidates:
            # 1) the exact pages the search returned, in every category (1 request each): "Pushpa" can sit in the
            #    2021 category (The Rise) and the 2024 one (The Rule), and the newest should win below.
            _seen_urls = set()
            for _cat_text, cat_url in web["categories"][:8]:
                for cand in _fz_category_find(cat_url, match_title, shallow=True):
                    if cand[3] not in _seen_urls:
                        _seen_urls.add(cand[3])
                        scored_candidates.append(cand)
            if scored_candidates:
                print(f"[*] Found {len(scored_candidates)} match(es) for '{match_title}' on the pages the web search returned.")
        if not scored_candidates:
            # 2) not on those exact pages: walk each category's alphabet index + listing pages
            _deadline = time.time() + 90   # don't spend minutes walking categories that don't have the movie
            for _cat_text, cat_url in web["categories"][:5]:
                if time.time() > _deadline:
                    print("[-] Category walk time budget used up.")
                    break
                found = _fz_category_find(cat_url, match_title)
                if found:
                    scored_candidates = found
                    print(f"[*] Found '{match_title}' inside category {cat_url}")
                    break
        site_search_ok = bool(scored_candidates)
    if not site_search_ok:
        # last resort: walk the category alphabet indexes (slow)
        alpha = _fz_alphabet_soup(match_title)
        if alpha is not None:
            scored_candidates = _score(alpha)
            if scored_candidates:
                print(f"[*] Filmyzilla alphabet index matched '{match_title}'.")

    if not scored_candidates:
        print(f"[-] No valid candidates matched '{match_title}' on Filmyzilla.")
        return None

    _mt = compact_text(match_title)
    _mt_word = re.compile(r"\b" + re.escape(match_title) + r"\b", re.I)   # "Lekh" must not pick "Lekhak"
    _wy = re.search(r"\b(?:19|20)\d{2}\b", original_query or "")
    _want_year = _wy.group(0) if _wy else ""

    def _year_of(text_url):
        ys = re.findall(r"\b((?:19|20)\d{2})\b", text_url)
        return int(ys[-1]) if ys else 0

    scored_candidates.sort(key=lambda x: (x[0], bool(_want_year and _want_year in x[2]), bool(_mt_word.search(x[2])),
                                          compact_text(x[2]).startswith(_mt), _year_of(x[2]), x[1], -len(x[2])),
                           reverse=True)
    target_page = scored_candidates[0][3]
    print(f"[+] Selected title: '{scored_candidates[0][2]}' -> {target_page}")
    
    try:
        r2 = session.get(target_page, timeout=12)
        soup2 = BeautifulSoup(r2.text, "html.parser")
        tier_candidates = []

        for a in soup2.find_all("a", href=True):
            if any(k in a['href'] for k in ["/server/", "/file/", "/download/"]):
                href = urljoin(FILMYZILLA_DOMAIN, a['href'])
                txt = a.get_text(strip=True)
                score = get_quality_score(txt)
                tier_candidates.append((score, txt, href))

        if not tier_candidates: return None

        selected_tier = None
        if episode_match:
            ep_num = int(episode_match.group(1))
            ep_matching = [t for t in tier_candidates if re.search(rf'\b(ep|episode|e)[-_\s]*0?{ep_num}\b', t[1], re.IGNORECASE)]
            if ep_matching:
                ep_matching.sort(key=lambda x: x[0], reverse=True)
                selected_tier = (ep_matching[0][1], ep_matching[0][2])
        
        if not selected_tier:
            tier_candidates.sort(key=lambda x: x[0], reverse=True)
            selected_tier = (tier_candidates[0][1], tier_candidates[0][2])

        print(f"[*] Selected download tier: '{selected_tier[0]}'")

        r3 = session.get(selected_tier[1], timeout=12)
        soup3 = BeautifulSoup(r3.text, "html.parser")
        server_links = [
            urljoin(FILMYZILLA_DOMAIN, a['href']) 
            for a in soup3.find_all("a", href=True) 
            if any(k in a['href'] for k in ["/verified/", "/dload/", "download="])
        ]
        
        if not server_links:
            if "download" in r3.url or ".mp4" in r3.url or ".mkv" in r3.url:
                server_links = [r3.url]
            else:
                return None

        print(f"[+] Streaming video from: {server_links[0]}")
        res = session.get(server_links[0], stream=True, allow_redirects=True, timeout=20)
        if res.status_code == 200:
            local_dl = "temp_raw_stream.mkv"
            done = 0
            if state is not None:
                state["total"] = int(res.headers.get("Content-Length") or 0)
                state["dl_t0"] = time.time()
                state["stage"] = "download"
            with open(local_dl, "wb") as f:
                for chunk in res.iter_content(chunk_size=2*1024*1024):
                    if chunk:
                        f.write(chunk)
                        done += len(chunk)
                        if state is not None:
                            state["done"] = done
            return local_dl
    except Exception as e:
        print(f"[-] Filmyzilla stream error: {e}")
    return None

def _fz_follow(session, start_url, referer, max_hops=5):
    """Follow Filmyzilla's download chain (server page -> /dl/<id>/<srv>/?token=... -> /verified/...) until
    the response is not an HTML page. Each hop sends the previous page as Referer, like the browser does.
    Returns the final requests.Response (streamed if it is a file)."""
    url, ref = start_url, referer
    r = None
    for _ in range(max_hops):
        r = session.get(url, headers={"Referer": ref}, stream=True, timeout=30, allow_redirects=True)
        if "text/html" not in r.headers.get("Content-Type", ""):
            return r
        soup = BeautifulSoup(r.text, "html.parser")
        nxt = None
        for a in soup.find_all("a", href=True):
            if any(k in a["href"] for k in ["/dl/", "/verified/", "/dload/", "download="]):
                nxt = urljoin(r.url, a["href"])
                break
        if not nxt:
            m = re.search(r"""(?:href|location\.href\s*=)\s*['"]([^'"]+\.(?:mkv|mp4)[^'"]*)['"]""", r.text, re.I)
            if m:
                nxt = urljoin(r.url, m.group(1))
        if not nxt or nxt == url:
            return r
        print(f"[*] Filmyzilla hop: {url[:90]} -> {nxt[:90]}")
        ref, url = r.url, nxt
    return r


# Mirrors tried in order (ported from src filmyzilla.py). The first entry is FILMYZILLA_DOMAIN from config;
# the rest are fallbacks for when a mirror is dead. Any filmyzilla link is rewritten onto the mirror that works.
_FZ_MIRROR_RE = re.compile(r"(https?://)(www\.)?filmyzilla\d*\.\w+", re.IGNORECASE)
_FZ_EXTRA_MIRRORS = ["https://www.filmyzilla52.com"]
_WORKERS_FILE_RE = re.compile(r"https://[A-Za-z0-9\.\-]+\.workers\.dev/\S*?\.(?:mkv|mp4)(?:\?[^\"'\s<)]*)?", re.IGNORECASE)


def _fz_mirrors():
    out = []
    for m in [FILMYZILLA_DOMAIN] + _FZ_EXTRA_MIRRORS:
        m = (m or "").rstrip("/")
        if m and m not in out:
            out.append(m)
    return out


def _fz_on_mirror(url, mirror):
    return _FZ_MIRROR_RE.sub(lambda m: mirror, url, count=1)


def try_filmyzilla_direct(link, state=None):
    """Download straight from a Filmyzilla link, no search.
    Accepts a /server/<id>/... link (that quality) or a /movie/... page (its best-quality server is used).
    Returns the local file path or None."""
    if not BeautifulSoup:
        return None
    session = requests.Session()
    session.headers.update(HEADERS)
    raw_link = urljoin(FILMYZILLA_DOMAIN, link.strip())
    r, page_url, last_err = None, raw_link, None
    for mirror in _fz_mirrors():
        candidate = _fz_on_mirror(raw_link, mirror) if _FZ_MIRROR_RE.search(raw_link) else raw_link
        try:
            r = session.get(candidate, timeout=15)
            page_url = candidate
            break
        except Exception as e:
            last_err = e
            print(f"[-] Filmyzilla mirror {mirror} failed ({str(e)[:80]}), trying next...")
    if r is None:
        print(f"[-] Filmyzilla link open error: {last_err}")
        return None
    soup = BeautifulSoup(r.text, "html.parser")

    if "/movie/" in urlparse(page_url).path:
        tiers = []
        for a in soup.find_all("a", href=True):
            if "/server/" in a["href"]:
                tiers.append((get_quality_score(a.get_text(strip=True)), urljoin(page_url, a["href"])))
        if not tiers:
            print("[-] No server links found on the Filmyzilla movie page.")
            return None
        tiers.sort(key=lambda t: t[0], reverse=True)
        print(f"[*] Movie page: picking best server {tiers[0][1]}")
        try:
            r = session.get(tiers[0][1], timeout=15)
        except Exception as e:
            print(f"[-] Filmyzilla server page error: {e}")
            return None
        soup = BeautifulSoup(r.text, "html.parser")

    start = None
    for a in soup.find_all("a", href=True):
        if any(k in a["href"] for k in ["/dl/", "/verified/", "/dload/", "download="]):
            start = urljoin(page_url, a["href"])
            break
    if not start:
        for a in soup.find_all("a", href=True):
            if "workers.dev" in a["href"] and re.search(r"\.(mkv|mp4)(\?|$)", a["href"], re.IGNORECASE):
                start = a["href"]
                break
    if not start:
        # JS-driven button: the workers.dev file URL is in a <script>/data attribute, not an <a> tag
        m = _WORKERS_FILE_RE.search(r.text)
        if m:
            start = m.group(0)
            print("[*] Download URL found in page source (JS button).")
    if not start:
        if re.search(r"\.(mkv|mp4)(\?|$)", r.url, re.I):
            start = r.url
        else:
            print("[-] No download link found on the Filmyzilla server page.")
            return None

    print(f"[+] Following download chain from: {start}")
    try:
        res = _fz_follow(session, start, r.url)
    except Exception as e:
        print(f"[-] Filmyzilla download error: {e}")
        return None
    ctype = res.headers.get("Content-Type", "")
    if res.status_code != 200 or "text/html" in ctype:
        print(f"[-] Filmyzilla download refused (HTTP {res.status_code}, {ctype}).")
        return None
    local_dl = "temp_raw_stream.mkv"
    total = int(res.headers.get("Content-Length") or 0)
    if state is not None:
        state["total"] = total
        state["dl_t0"] = time.time()
    done = 0
    with open(local_dl, "wb") as f:
        for chunk in res.iter_content(chunk_size=2 * 1024 * 1024):
            if chunk:
                f.write(chunk)
                done += len(chunk)
                if state is not None:
                    state["done"] = done
    return local_dl


# ---- AnimaHD, plain-HTTP path (no browser) -------------------------------------------------------------------------
# Flow seen in a HAR capture of the real site:
#   episode link (?eid=...)  ->  <stream host>/?eid=...  (HTML)  ->  `window.proxyDownloadUrl = '<workers.dev url>'`
#   ->  GET that url with Referer = <stream host>/   ->  200 video/x-matroska, Content-Disposition: attachment; filename=...
# The page and the file URL are fetched back to back (the token inside the URL is generated per page load).
ANIMAHD_STREAM_HOST = os.getenv("ANIMAHD_STREAM_HOST", "https://animefansviewingzone.animahd.fun").rstrip("/")
_AH_UA = ("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/139.0.0.0 Mobile Safari/537.36")
_AH_PROXY_RE = re.compile(r"""proxyDownloadUrl\s*=\s*['"]([^'"]+)['"]""")
_AH_SOURCE_RE = re.compile(r"""<source[^>]+src=["']([^"']*workers\.dev[^"']*)["']""", re.I)
SUGGESTED_NAMES: dict = {}     # local temp path -> the site's own file name (from Content-Disposition), for the uploader


def _animahd_resolve(link: str):
    """Episode/stream link -> (file_url, referer) or (None, None). Tries the link itself, then the stream host."""
    import html as _html
    qs = parse_qs(urlparse(link).query)
    eid = (qs.get("eid") or [""])[0]
    fid = (qs.get("id") or qs.get("file_id") or [""])[0]
    cands = [link]
    if eid:
        cands.append(f"{ANIMAHD_STREAM_HOST}/?eid={eid}")
    if fid:
        cands.append(f"{ANIMAHD_STREAM_HOST}/?id={fid}")
    sess = requests.Session()
    for url in dict.fromkeys(cands):
        try:
            r = sess.get(url, headers={"User-Agent": _AH_UA, "Referer": "https://animahd.com/",
                                       "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
                         timeout=25, allow_redirects=True)
        except Exception as e:
            print(f"[-] AnimaHD page fetch failed ({url[:80]}): {e}")
            continue
        m = _AH_PROXY_RE.search(r.text) or _AH_SOURCE_RE.search(r.text)
        if r.status_code == 200 and m:
            u = urlparse(r.url)
            return _html.unescape(m.group(1)), f"{u.scheme}://{u.netloc}/"
        print(f"[-] AnimaHD: no file url on {url[:80]} (HTTP {r.status_code}, {len(r.text)} bytes)")
    return None, None


def _animahd_http_fetch(link: str, state=None):
    """Blocking. Resolve + stream the file to disk with progress in `state`. Returns the local path or None."""
    from urllib.parse import unquote
    file_url, referer = _animahd_resolve(link)
    if not file_url:
        return None
    print(f"[*] AnimaHD file url found: {file_url.split('?')[0]}")
    try:
        res = requests.get(file_url, headers={"User-Agent": _AH_UA, "Referer": referer}, stream=True,
                           allow_redirects=True, timeout=(15, 60))
        ctype = res.headers.get("Content-Type", "").lower()
        if res.status_code not in (200, 206) or "text/html" in ctype:
            print(f"[-] AnimaHD file request failed: HTTP {res.status_code} {ctype}")
            return None
        cd = res.headers.get("Content-Disposition", "")
        mm = re.search(r"filename\*=UTF-8''([^;]+)", cd, re.I) or re.search(r'filename="?([^";]+)"?', cd, re.I)
        fname = unquote(mm.group(1)).strip() if mm else ""
        ext = os.path.splitext(fname)[1].lower() if fname else (".mp4" if "mp4" in ctype else ".mkv")
        if ext not in (".mkv", ".mp4", ".avi", ".webm", ".ts", ".mov"):
            ext = ".mp4" if "mp4" in ctype else ".mkv"
        local_dl = "temp_animahd_stream" + ext
        if state is not None:
            state["total"] = int(res.headers.get("Content-Length") or 0)
            state["dl_t0"] = time.time()
        done = 0
        with open(local_dl, "wb") as f:
            for chunk in res.iter_content(chunk_size=2 * 1024 * 1024):
                if chunk:
                    f.write(chunk)
                    done += len(chunk)
                    if state is not None:
                        state["done"] = done
        total = int(res.headers.get("Content-Length") or 0)
        if done <= 0 or (total and done < total):
            print(f"[-] AnimaHD download incomplete: {done}/{total} bytes")
            return None
        if fname:
            SUGGESTED_NAMES[local_dl] = fname
        return local_dl
    except Exception as e:
        print(f"[-] AnimaHD http download error: {e}")
        return None


def _hindi_page_download(url: str, state=None):
    """Blocking. Pasted Hindi-anime episode page -> local file (see anime_hindi.py)."""
    if not anime_hindi:
        return None
    path = anime_hindi.download_from_page(url, state)
    if path:
        SUGGESTED_NAMES.update(anime_hindi.suggested_names)
    return path


def _hindi_search_download(query: str, original: str, state=None):
    """Blocking. Name search on the Hindi anime sites -> local file. Last web fallback before the Telegram bots."""
    if not anime_hindi:
        return None
    path = anime_hindi.search_and_download(query, original, state, is_title_match)
    if path:
        SUGGESTED_NAMES.update(anime_hindi.suggested_names)
    return path


def _salt_page_download(url: str, state=None):
    """Blocking. Pasted AnimeSalt episode / movie page -> local file (see animesalt.py)."""
    if not animesalt:
        return None
    path = animesalt.download_from_page(url, state)
    if path:
        SUGGESTED_NAMES.update(animesalt.suggested_names)
    return path


def _salt_search_download(query: str, original: str, state=None):
    """Blocking. Name search on AnimeSalt -> local file (a movie, or the wanted S/E of the matching show)."""
    if not animesalt:
        return None
    path = animesalt.search_and_download(query, original, state, is_title_match)
    if path:
        SUGGESTED_NAMES.update(animesalt.suggested_names)
    return path


def _salt_show_for(title: str):
    """Best AnimeSalt show/movie link for a title (WordPress search), or None."""
    if not animesalt or not title:
        return None
    best, best_ratio = None, -1.0
    for text, href in animesalt._search_hits(title):
        slug_words = animesalt._kind_slug(href)[1].replace("-", " ")
        try:
            matched, ratio = is_title_match(title, f"{text} {slug_words}")
        except Exception:
            continue
        if matched and ratio > best_ratio:
            best_ratio, best = ratio, href
    return best


def _salt_episode_listing(show_url: str) -> str:
    """Season/episode summary of an AnimeSalt show, as text. Pick one with: /download_movie <title> S01E05 anime"""
    if animesalt.is_supported(show_url):
        return f"Ye movie mili: {show_url}\nIse download karne ke liye ye link /download_movie ko bhejiye."
    try:
        show = animesalt.fetch_show(show_url)
    except Exception as e:
        return f"AnimeSalt show page error: {str(e)[:160]}"
    lines = [f"Ye show AnimeSalt par mila: {show_url}",
             "Episode chunne ke liye likhiye: /download_movie <title> S01E05 anime"]
    for se in show["seasons"]:
        eps = [e["ep"] for e in se["episodes"]]
        lines.append(f"Season {se['no']}: E{min(eps):02d} to E{max(eps):02d} ({len(eps)} episodes)")
    return "\n".join(lines)[:3500]


async def _animahd_browser_fetch(target_ep_link, state=None):
    """Episode/gate link -> headless browser walks the AnimaHD gate (base64 target -> Download Episode -> Final Step)
    and saves the file. Returns the local path or None. Shared by the name search and by pasted episode links."""
    try:
        print(f"[*] Dispatching headless browser to retrieve streams...")
        install_browser_engine()
        from playwright.async_api import async_playwright

        download_url = None
        final_url = None
        captured_urls = []

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
            context = await browser.new_context(user_agent=HEADERS["User-Agent"], accept_downloads=True)
            page = await context.new_page()

            loop = asyncio.get_running_loop()
            download_future = loop.create_future()

            def _on_download(d):
                if not download_future.done(): download_future.set_result(d)

            page.on("download", _on_download)
            context.on("page", lambda new_page: new_page.on("download", _on_download))

            await page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "font"] else route.continue_())
            page.on("request", lambda req: captured_urls.append(req.url))

            try:
                await page.goto(target_ep_link, timeout=30000)
                await page.wait_for_url(re.compile(re.escape(ANIMAHD_GATE_DOMAIN) + r"|target=|gate="), timeout=12000)
            except Exception: pass

            current_url = page.url
            if "target=" in current_url:
                parsed = urlparse(current_url)
                q_params = parse_qs(parsed.query)
                if "target" in q_params:
                    decoded = decode_base64_string(q_params["target"][0])
                    if decoded: final_url = decoded

            if final_url:
                if "eid=" in final_url and "passed=" not in final_url:
                    final_url += "&passed=1" if "?" in final_url else "?passed=1"
                try:
                    await page.goto(final_url, timeout=20000)
                except Exception: pass

            click_sequence = [r"Download Episode", r"Click Again to Continue", r"Final Step"]
            for step_regex in click_sequence:
                try:
                    btn = page.get_by_text(re.compile(step_regex, re.IGNORECASE)).first
                    if await btn.is_visible(timeout=6000):
                        await btn.click(force=True)
                        await page.wait_for_timeout(2000)
                except Exception: pass

            local_dl = "temp_animahd_stream.mkv"
            if state is not None:
                state["stage"] = "browser"   # browser download: no byte counter available
            try:
                download_obj = await asyncio.wait_for(asyncio.shield(download_future), timeout=12.0)
                await download_obj.save_as(local_dl)
                await browser.close()
                return local_dl
            except asyncio.TimeoutError: pass

            scored_captured = []
            for url in captured_urls:
                if re.search(r'\.mkv|\.mp4|workers\.dev|download=true', url, re.IGNORECASE):
                    if url != page.url and "latestanimeepisodes" not in url:
                        score = get_quality_score(url)
                        scored_captured.append((score, url))

            if scored_captured:
                scored_captured.sort(key=lambda x: x[0], reverse=True)
                download_url = scored_captured[0][1]

            if not download_url:
                links = await page.query_selector_all("a")
                found_links = []
                for link in links:
                    text = (await link.inner_text()).lower()
                    href = await link.get_attribute("href")
                    if href and ("download" in text or "url?id=" in href or ".mkv" in href or ".mp4" in href):
                        full_h = urljoin(page.url, href)
                        score = get_quality_score(f"{text} {full_h}")
                        found_links.append((score, full_h))
                if found_links:
                    found_links.sort(key=lambda x: x[0], reverse=True)
                    download_url = found_links[0][1]

            if download_url:
                try:
                    async with page.expect_download(timeout=90000) as dl_info:
                        await page.evaluate("url => window.location.href = url", download_url)
                    download = await dl_info.value
                    await download.save_as(local_dl)
                    await browser.close()
                    return local_dl
                except Exception: pass

            await browser.close()
        return None
    except Exception as e:
        print(f"[-] AnimaHD error: {e}")
        return None


async def try_animahd_direct(link, state=None):
    """A pasted AnimaHD episode link (e.g. https://latestanimeepisodesonline.animahd.online/?eid=...)."""
    print(f"\n[Source: AnimaHD] Direct episode link: {link}")
    path = await asyncio.to_thread(_animahd_http_fetch, link, state)      # fast path: plain HTTP, no browser
    if path:
        return path
    print("[*] AnimaHD plain-HTTP path failed, trying the headless browser...")
    return await _animahd_browser_fetch(link, state)


async def try_animahd_scrape(search_query, original_query, state=None):
    if not BeautifulSoup: return None
    print(f"\n[Source: AnimaHD] Searching {ANIMAHD_DOMAIN} for: '{search_query}'...")
    session = requests.Session()
    session.headers.update(HEADERS)
    
    episode_match = re.search(r'\b(?:e|ep|episode)\s*(\d{1,2})\b', original_query, re.IGNORECASE)
    if not episode_match:
        episode_match = re.search(r'\bs\d{1,2}e(\d{1,2})\b', original_query, re.IGNORECASE)
    
    ascii_orig = "".join([c for c in unicodedata.normalize('NFKD', original_query) if not unicodedata.combining(c)])
    base_match_title = re.sub(r'\s*[sS](?:eason)?\s*\d{1,2}\s*[eE](?:p|pisode)?\s*\d{1,3}.*', '', ascii_orig).strip()
    if not base_match_title: base_match_title = ascii_orig

    try:
        # The site's own search is the WordPress REST API (the browser calls /wp-json/wp/v2/posts?search=...).
        # Its JSON gives every post's link and title; the old /?s= HTML page is kept only as a fallback.
        anime_page_url = None
        try:
            rj = session.get(f"{ANIMAHD_DOMAIN.rstrip('/')}/wp-json/wp/v2/posts",
                             params={"search": search_query, "per_page": 10}, timeout=12)
            posts = rj.json() if rj.status_code == 200 else []
        except Exception:
            posts = []
        best_ratio = -1.0
        for post in posts if isinstance(posts, list) else []:
            link = (post.get("link") or "").strip()
            title = re.sub(r"<[^>]+>", "", (post.get("title") or {}).get("rendered", "") or "")
            title = unicodedata.normalize("NFKD", title)
            if not (link and title):
                continue
            matched, ratio = is_title_match(base_match_title, title)
            if matched and ratio > best_ratio:      # closest title wins ("Naruto" -> "Naruto Shippuden", not "Boruto Naruto ...")
                best_ratio, anime_page_url = ratio, link

        if not anime_page_url:
            r = session.get(f"{ANIMAHD_DOMAIN}/?s={quote(search_query)}", timeout=12)
            if r.status_code != 200: return None
            soup = BeautifulSoup(r.text, "html.parser")

            ignore_links = ["/filter/", "/anime-schedule/", "/dmca/", "/terms/", "/about/", "/contact/"]
            for a in soup.find_all("a", href=True):
                href = a['href']
                text = a.get_text()
                if ANIMAHD_DOMAIN in href and not any(ig in href.lower() for ig in ignore_links) and href.strip('/') != ANIMAHD_DOMAIN.strip('/'):
                    matched, _ = is_title_match(base_match_title, text)
                    if matched:
                        anime_page_url = href
                        break

        if not anime_page_url:
            print(f"[-] Not found on AnimaHD.")
            return None

        print(f"[*] Found Series Page: {anime_page_url}")
        r2 = session.get(anime_page_url, timeout=12)
        soup2 = BeautifulSoup(r2.text, "html.parser")
        
        episode_links = []
        for a in soup2.find_all("a", href=True):
            txt = a.get_text(strip=True)
            t_low = txt.lower()
            if re.search(r'(?:e|ep|episode|s\d+e)\s*\d+', t_low) or ".mkv" in t_low or ".mp4" in t_low:
                href = urljoin(anime_page_url, a['href'])
                score = get_quality_score(txt)
                episode_links.append((score, txt, href))
                
        target_ep_link = None
        ep_num = int(episode_match.group(1)) if episode_match else 1
        ep_matches = [
            item for item in episode_links 
            if re.search(rf'\b(?:e|ep|episode)\s*0?{ep_num}\b', item[1], re.IGNORECASE) or re.search(rf's\d+e0?{ep_num}\b', item[1], re.IGNORECASE)
        ]
        
        if ep_matches:
            ep_matches.sort(key=lambda x: x[0], reverse=True)
            target_ep_link = ep_matches[0][2]
        elif episode_links:
            episode_links.sort(key=lambda x: x[0], reverse=True)
            target_ep_link = episode_links[0][2]
            
        if not target_ep_link:
            print("[-] No matching episode links on series page.")
            return None
            
        return await _animahd_browser_fetch(target_ep_link, state)

    except Exception as e:
        print(f"[-] AnimaHD error: {e}")
        return None

async def try_telegram_bots(search_query, original_query, state=None):
    if not SESSION_STRING:
        print("[-] TG_SESSION is not set.")
        return None

    print(f"\n[Source: Telegram] Engaging bots for: '{search_query}'...")
    client = None
    try:
        client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
        await client.connect()
        
        if not await client.is_user_authorized():
            print("[-] Telegram session invalid.")
            return None

        for bot in ACTIVE_BOTS:
            print(f"[*] Querying bot: {bot}...")
            try:
                sent_msg = await client.send_message(bot, search_query)
                out_id = sent_msg.id
            except Exception as e:
                print(f"[-] Could not send to {bot}: {e}")
                continue

            start_time = time.time()
            media_messages = []

            while time.time() - start_time < 35:
                async for msg in client.iter_messages(bot, min_id=out_id, limit=25):
                    if msg.buttons:
                        for row in msg.buttons:
                            for btn in row:
                                matched, _ = is_title_match(original_query, btn.text)
                                score = get_quality_score(btn.text)
                                if matched or score >= 80:
                                    try:
                                        await btn.click()
                                        await asyncio.sleep(2)
                                    except Exception: pass

                    if (msg.media or msg.document or msg.video) and msg.id not in [m.id for m in media_messages]:
                        media_messages.append(msg)
                
                if media_messages:
                    best_current_score = max([get_quality_score(f"{getattr(m.file, 'name', '')} {m.text or ''}") for m in media_messages])
                    if best_current_score >= 80: break
                        
                if len(media_messages) >= 2 and (time.time() - start_time > 12): break
                await asyncio.sleep(2)

            if media_messages:
                scored_msgs = []
                for m in media_messages:
                    f_name = getattr(m.file, 'name', '') or ''
                    caption = m.text or ''
                    full_desc = f"{f_name} {caption}"
                    matched, ratio = is_title_match(original_query, full_desc)
                    score = get_quality_score(full_desc)
                    if matched or ratio >= 0.4:
                        scored_msgs.append((score, m, f_name))

                if scored_msgs:
                    scored_msgs.sort(key=lambda x: x[0], reverse=True)
                    best_score, best_msg, best_fname = scored_msgs[0]
                    print(f"[+] Selected Telegram file: '{best_fname}' (Quality: {best_score})")

                    final_name = best_fname or "video.mp4"
                    def _tg_progress(current, total):
                        if state is not None:
                            state["done"], state["total"] = current, total or 0
                    if state is not None:
                        state["dl_t0"] = time.time()
                        state["stage"] = "download"
                    dl_path = await best_msg.download_media(file=f"temp_{final_name}", progress_callback=_tg_progress)
                    return dl_path

    except Exception as e:
        print(f"[-] Telegram error: {e}")
    finally:
        if client and client.is_connected():
            await client.disconnect()
            print("[*] Telegram disconnected cleanly.")
    return None

def probe_file_streams(filepath):
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=codec_type,codec_name,pix_fmt",
        "-of", "json", filepath
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        info = json.loads(proc.stdout)
        video_codec, pix_fmt, audio_codec = None, None, None
        
        for s in info.get("streams", []):
            c_type = s.get("codec_type")
            if c_type == "video" and not video_codec:
                video_codec = s.get("codec_name", "").lower()
                pix_fmt = s.get("pix_fmt", "").lower()
            elif c_type == "audio" and not audio_codec:
                audio_codec = s.get("codec_name", "").lower()
                
        return video_codec, pix_fmt, audio_codec
    except Exception as e:
        print(f"[-] ffprobe inspection error: {e}")
        return None, None, None

def transcode_and_upload(source_file, title_label):
    clean_title = sanitize_title(title_label)
    final_output = f"{clean_title}.mp4"
    
    v_codec, pix_fmt, a_codec = probe_file_streams(source_file)
    print(f"[*] Detected streams: Video={v_codec} ({pix_fmt}), Audio={a_codec}")
    
    is_v_safe = (v_codec == "h264") and (pix_fmt == "yuv420p")
    is_a_safe = a_codec in ["aac", "mp3"]
    
    if is_v_safe and is_a_safe:
        print("[+] TV compliant streams detected. Remuxing with +faststart header...")
        ffmpeg_cmd = ["ffmpeg", "-y", "-i", source_file, "-c", "copy", "-movflags", "+faststart", final_output]
    elif is_v_safe and not is_a_safe:
        print("[*] Video is compatible; converting audio to stereo AAC with +faststart...")
        ffmpeg_cmd = ["ffmpeg", "-y", "-i", source_file, "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-movflags", "+faststart", final_output]
    else:
        print("[!] Re-encoding to universal Sony TV H.264 profile...")
        ffmpeg_cmd = ["ffmpeg", "-y", "-i", source_file, "-vf", "scale='min(1920,iw)':-2", "-c:v", "libx264", "-profile:v", "high", "-level", "4.1", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-movflags", "+faststart", final_output]
    
    proc = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
    upload_target = final_output if proc.returncode == 0 and os.path.exists(final_output) else source_file
    
    service = get_drive_service()
    folder_id = DRIVE_FOLDER_ID
    metadata = {'name': clean_title + ".mp4", 'parents': [folder_id]}
    
    print(f"[*] Uploading '{upload_target}' to Google Drive...")
    media = MediaFileUpload(upload_target, mimetype='video/mp4', resumable=True)
    
    uploaded_file = service.files().create(body=metadata, media_body=media, fields='id').execute(num_retries=5)
    file_id = uploaded_file.get('id')
    print(f"[+] Upload complete! File ID: {file_id}")
    
    try:
        service.permissions().create(fileId=file_id, body={"type": "anyone", "role": "reader"}).execute(num_retries=5)
        print("[+] Public view permission enabled.")
    except Exception as e:
        print(f"[-] Permission warning: {e}")
    
    for f in [source_file, final_output]:
        if os.path.exists(f):
            try: os.remove(f)
            except Exception: pass

# =========================================================
# MAIN ORCHESTRATOR
# =========================================================
def _ds_join_parts(parts: list, out: str):
    """Join the downloaded parts (same codec, they are cuts of one MP4) into one file with ffmpeg's concat demuxer."""
    lst = out + ".parts.txt"
    try:
        with open(lst, "w") as f:
            for p in parts:
                f.write("file '" + os.path.abspath(p).replace("'", "'\\''") + "'\n")
        r = subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst,
                            "-c", "copy", "-movflags", "+faststart", out], capture_output=True, timeout=1800)
        if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) <= 0:
            print(f"[-] desiserial concat failed: {r.stderr.decode(errors='ignore')[-300:]}")
            return None
        return out
    except Exception as e:
        print(f"[-] desiserial concat error: {e}")
        return None
    finally:
        for p in parts + [lst]:
            try:
                os.remove(p)
            except OSError:
                pass


def _fz_download_url(dl_url: str, state=None, out_name: str = None) -> str:
    """workers.dev ya direct file URL se file download karo. Returns local path ya None."""
    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        res = session.get(dl_url, stream=True, allow_redirects=True, timeout=30)
        if res.status_code != 200:
            print(f"[-] _fz_download_url HTTP {res.status_code}")
            return None
        ctype = res.headers.get("Content-Type", "")
        if "text/html" in ctype:
            print(f"[-] _fz_download_url got HTML instead of file")
            return None
        # Extension detect karo
        ext = ".mkv"
        if ".mp4" in dl_url.lower():
            ext = ".mp4"
        local_dl = out_name or f"temp_raw_stream{ext}"
        total = int(res.headers.get("Content-Length") or 0)
        if state is not None:
            state["total"] = total
            state["dl_t0"] = time.time()
        done = 0
        with open(local_dl, "wb") as f:
            for chunk in res.iter_content(chunk_size=2 * 1024 * 1024):
                if chunk:
                    f.write(chunk)
                    done += len(chunk)
                    if state is not None:
                        state["done"] = done
        return local_dl if done > 0 else None
    except Exception as e:
        print(f"[-] _fz_download_url error: {e}")
        return None


def drive_configured() -> bool:
    """Everything transcode_and_upload() needs to reach Google Drive (all values come from config.py)."""
    return bool(GCP_CLIENT_ID and GCP_CLIENT_SECRET and GCP_REFRESH_TOKEN and DRIVE_FOLDER_ID)


_EP_TOKEN_RE = re.compile(r"\b(?:e|ep|episode)\s*\d{1,3}\b|\bs\d{1,2}e\d{1,3}\b", re.IGNORECASE)


def _animahd_show_for(title: str):
    """Best AnimaHD show/post link for a title, via the site's WordPress search (no Google)."""
    if not BeautifulSoup or not title:
        return None
    sess = requests.Session()
    sess.headers.update(HEADERS)
    best, best_ratio = None, -1.0
    try:
        rj = sess.get(f"{ANIMAHD_DOMAIN.rstrip('/')}/wp-json/wp/v2/posts",
                      params={"search": title, "per_page": 10}, timeout=12)
        posts = rj.json() if rj.status_code == 200 else []
    except Exception as e:
        print(f"[-] AnimaHD search request failed: {str(e)[:120]}")
        posts = []
    for post in posts if isinstance(posts, list) else []:
        link = (post.get("link") or "").strip()
        t = re.sub(r"<[^>]+>", "", (post.get("title") or {}).get("rendered", "") or "")
        t = unicodedata.normalize("NFKD", t)
        if not (link and t):
            continue
        matched, ratio = is_title_match(title, t)
        if matched and ratio > best_ratio:
            best_ratio, best = ratio, link
    return best


def _animahd_episode_listing(show_url: str) -> str:
    """Season/episode list from an AnimaHD show page, as text the user can pick from.
    Pick one with: /download_movie <title> S01E05 anime"""
    try:
        r = requests.get(show_url, headers={**HEADERS, "Referer": ANIMAHD_DOMAIN}, timeout=15)
        if r.status_code != 200:
            return f"AnimaHD show page open nahi hua (HTTP {r.status_code}): {show_url}"
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        return f"AnimaHD show page error: {str(e)[:120]}"

    seasons = {}
    season = "1"
    for el in soup.find_all(True):
        txt = el.get_text(" ", strip=True) if el.name in ("h1", "h2", "h3", "h4", "h5", "button", "div", "span") else ""
        if el.name in ("h2", "h3", "h4", "button", "div", "span") and txt and len(txt) < 40:
            m = re.search(r"season\s*(\d{1,2})", txt, re.IGNORECASE)
            if m:
                season = str(int(m.group(1)))
        if el.name == "a" and el.get("href"):
            href = urljoin(show_url, el["href"])
            label = el.get_text(" ", strip=True)
            if ANIMAHD_DOMAIN.split("//")[-1].split("/")[0] not in urlparse(href).netloc and "animahd" not in urlparse(href).netloc:
                continue
            m = re.search(r"\b(?:episode|ep)\s*(\d{1,3})\b", label, re.IGNORECASE) or re.search(r"[?&]ep(?:isode)?=(\d{1,3})", href)
            if m:
                seasons.setdefault(season, {})[int(m.group(1))] = label[:40]
    if not seasons:
        return f"Is show page par episode list nahi mili. Link: {show_url}"
    lines = [f"Ye show mila: {show_url}", "Episode chunne ke liye likhiye: /download_movie <title> S01E05 anime"]
    for sn in sorted(seasons, key=lambda x: int(x)):
        eps = sorted(seasons[sn])
        lines.append(f"Season {sn}: E{eps[0]:02d} to E{eps[-1]:02d} ({len(eps)} episodes)")
    return "\n".join(lines)[:3500]


async def _find_movie_any(movie_name: str, run_mode: str = None, state: dict = None):
    """Search + download ONLY (no conversion, no Drive). Returns (path_or_None, detail).

    Used by the bot's /download_movie, which then converts the file to mp4 and sends it straight to the Telegram chat.
    The blocking requests-based scraper runs in a worker thread so the bot's event loop stays responsive."""
    movie_name = (movie_name or "").strip()
    mode = (run_mode or RUN_MODE or "all").strip().lower()
    if not movie_name:
        return None, "No movie name given."

    # desiserial.su: an episode page, or one of its getlink / play links (see desiserial_scan.py)
    try:
        import desiserial_scan as _ds
        _ds_links = []
        if _ds.GETLINK_RE.search(movie_name) or _ds.PLAY_RE.search(movie_name):
            _ds_links = [movie_name]
        elif _ds.is_desiserial_page(movie_name):
            _opts = await asyncio.to_thread(_ds.list_options, movie_name)
            if not _opts:
                return None, "desiserial: no stream options found on that page."
            # 720p players first, then the rest in page order: if one player fails the next is tried
            _opts = sorted(_opts, key=lambda o: "720" not in o["label"])
            _ds_links = [o["url"] for o in _opts]
        _last = "desiserial: could not resolve a direct MP4 for this link."
        _ds_tag = __import__("uuid").uuid4().hex[:8]   # unique temp names -> several desiserial jobs can run at once
        for _ds_link in _ds_links:
            # An episode is split into Part 1 / 2 / 3 (v=, part2=, part3=): all parts are downloaded and joined.
            _n = max(1, len(_ds.part_tokens(_ds_link)))
            _parts, _fail = [], None
            for _i in range(_n):
                _direct = await asyncio.to_thread(_ds.resolve_part, _ds_link, _i)
                if not _direct:
                    _fail = f"desiserial: could not resolve part {_i + 1}/{_n}."
                    break
                _pp = await asyncio.to_thread(_fz_download_url, _direct, state, f"temp_ds_{_ds_tag}_part{_i + 1}.mp4")
                if not _pp:
                    _fail = f"desiserial: part {_i + 1}/{_n} download failed."
                    break
                _parts.append(_pp)
            if _fail:
                _last = _fail
                for _pp in _parts:
                    try:
                        os.remove(_pp)
                    except OSError:
                        pass
                continue
            if len(_parts) == 1:
                _path = f"temp_ds_{_ds_tag}.mp4"
                os.replace(_parts[0], _path)
            else:
                _path = await asyncio.to_thread(_ds_join_parts, _parts, f"temp_ds_{_ds_tag}.mp4")
                if not _path:
                    _last = "desiserial: joining the parts failed."
                    continue
            return _path, f"desiserial: {_n} part(s)"
        if _ds_links:
            return None, _last
    except Exception as _ds_err:
        print(f"[-] desiserial download failed: {_ds_err}")
        return None, f"desiserial: {str(_ds_err)[:160]}"

    # Meowly / Vidly link (meowly.qzz.io/watch/tv/<tmdb>?s=1&e=2, vidly.pro/tv/<tmdb>/1/2, .../movie/<tmdb>)
    if meowly_dl and meowly_dl.is_supported(movie_name.strip().split()[0] if movie_name.strip() else ""):
        _mw_link = movie_name.strip().split()[0]
        if meowly_dl.is_show_link(_mw_link):
            return None, "Meowly: that is a show link - open it in the bot to pick an episode (or send .../watch/tv/<id>?s=1&e=2)."
        _mw = await asyncio.to_thread(meowly_dl.download, _mw_link, state)
        if _mw:
            SUGGESTED_NAMES.update(meowly_dl.SUGGESTED_NAMES)
            return _mw, "Downloaded from Meowly."
        return None, "Meowly: could not download that title - " + (meowly_dl.last_error or "unknown reason") + "."

    # A pasted link (or several lines with links) is downloaded directly, no search.
    links = re.findall(r"https?://\S+", movie_name)
    if links:
        fz_links = [l for l in links if "filmyzilla" in urlparse(l).netloc.lower()]
        ah_links = [l for l in links if "animahd" in urlparse(l).netloc.lower()]
        hi_links = [l for l in links if anime_hindi and anime_hindi.is_supported(l)]
        as_links = [l for l in links if animesalt and animesalt.is_supported(l)]
        if not fz_links and not ah_links and not hi_links and not as_links:
            return None, "Only Filmyzilla, AnimaHD, AnimeSalt and Hindi-anime (Toonstream, AnimeDekho, HindiSubAnime, HindiAnimeVerse, WatchAnimeWorld) links are supported as direct links."
        for link in ah_links:
            downloaded = await try_animahd_direct(link, state)
            if downloaded:
                return downloaded, f"Downloaded from {link}."
        for link in as_links:
            downloaded = await asyncio.to_thread(_salt_page_download, link, state)
            if downloaded:
                return downloaded, f"Downloaded from {link}."
        for link in hi_links:
            downloaded = await asyncio.to_thread(_hindi_page_download, link, state)
            if downloaded:
                return downloaded, f"Downloaded from {link}."
        if not fz_links:
            _why = (getattr(animesalt, "last_report", "") or "")[:700] if (as_links and animesalt) else ""
            return None, "Could not download from the given link(s)." + (f"\n{_why}" if _why else "")
        for link in fz_links:
            # Step 1: filmyzilla_scan.resolve_single se direct workers.dev link nikalo
            # (movie page -> best server -> workers.dev file URL)
            try:
                import filmyzilla_scan
                fname, dl_url = await asyncio.to_thread(filmyzilla_scan.resolve_single, link)
                if dl_url and dl_url != "NOT_FOUND":
                    print(f"[+] resolve_single found: {fname} -> {dl_url}")
                    # workers.dev URL se direct download karo
                    downloaded = await asyncio.to_thread(_fz_download_url, dl_url, state)
                    if downloaded:
                        print(f"[+] Downloaded from direct link (scan): {link}")
                        return downloaded, f"Downloaded from {link}."
            except Exception as e:
                print(f"[-] resolve_single failed ({e}), falling back to try_filmyzilla_direct...")

            # Step 2: Fallback — purana method
            downloaded = await asyncio.to_thread(try_filmyzilla_direct, link, state)
            if downloaded:
                print(f"[+] Downloaded from direct link: {link}")
                return downloaded, f"Downloaded from {link}."
        return None, "Could not download from the given Filmyzilla link(s)."

    # "<title> anime": go straight to AnimaHD (no Filmyzilla/Google search).
    # Without an episode -> return the show's season/episode list. With an episode -> download it.
    if re.search(r"\banime\b", movie_name, re.IGNORECASE):
        core = re.sub(r"\banime\b", " ", movie_name, flags=re.IGNORECASE)
        core = re.sub(r"\s+", " ", core).strip(" -")
        base = _EP_TOKEN_RE.sub(" ", core)
        base = re.sub(r"\s+", " ", base).strip(" -")
        show = await asyncio.to_thread(_animahd_show_for, base)
        if not show:         # not on AnimaHD: try AnimeSalt before giving up
            salt = await asyncio.to_thread(_salt_show_for, base)
            if not salt:
                return None, f"AnimaHD / AnimeSalt par '{base}' nahi mila."
            if not _EP_TOKEN_RE.search(core):
                return None, await asyncio.to_thread(_salt_episode_listing, salt)
            downloaded = await asyncio.to_thread(_salt_search_download, base, core, state)
            if downloaded:
                return downloaded, f"Found on AnimeSalt: {salt}"
            return None, f"AnimeSalt par episode nahi mila: {core}"
        if not _EP_TOKEN_RE.search(core):
            return None, await asyncio.to_thread(_animahd_episode_listing, show)
        downloaded = await try_animahd_scrape(base, core, state)
        if downloaded:
            return downloaded, f"Found on AnimaHD: {show}"
        downloaded = await asyncio.to_thread(_salt_search_download, base, core, state)     # AnimaHD missed it
        if downloaded:
            return downloaded, f"Found on AnimeSalt: {base}"
        return None, f"AnimaHD par episode nahi mila: {core}"

    print(f"[*] Starting universal search and scrape for: '{movie_name}'")
    # Let the engine automatically generate fallback queries
    search_variations = generate_search_variations(movie_name)
    print(f"[*] Generated search variations string to test: {search_variations}")

    downloaded = None
    # Loop through each generated variant until one finally hits gold
    for query_variant in search_variations:
        print(f"\n=======================================================")
        print(f"[*] ATTEMPTING SCRAPE WITH VARIANT: '{query_variant}'")
        print(f"=======================================================")

        if mode in ["web", "all"]:
            downloaded = await asyncio.to_thread(try_filmyzilla_scrape, query_variant, movie_name, state)
            if not downloaded:
                downloaded = await try_animahd_scrape(query_variant, movie_name, state)
            if not downloaded:      # AnimeSalt (animesalt.cam)
                downloaded = await asyncio.to_thread(_salt_search_download, query_variant, movie_name, state)
            if not downloaded:      # fallback: the Hindi-dub anime sites ported from src (Toonstream, AnimeDekho, ...)
                downloaded = await asyncio.to_thread(_hindi_search_download, query_variant, movie_name, state)

        if not downloaded and mode in ["telegram", "all"]:
            downloaded = await try_telegram_bots(query_variant, movie_name, state)

        if downloaded:
            print(f"[+] Successfully found and downloaded file using query variation: '{query_variant}'")
            return downloaded, f"Found with the search '{query_variant}'."

    print("[-] All search variations and sources exhausted. File could not be found.")
    return None, "Not found on any source."


MIN_MOVIE_SECONDS = int(os.environ.get("MIN_MOVIE_SECONDS", "1200"))  # shorter than this = trailer/teaser, not the movie


def _probe_duration(path: str) -> float:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=nw=1:nk=1", path], capture_output=True, text=True, timeout=60)
        return float((out.stdout or "0").strip() or 0)
    except Exception:
        return 0.0


async def find_movie(movie_name: str, run_mode: str = None, state: dict = None):
    """Search + download, then refuse a clip: a trailer/teaser from any source is not the movie the user asked for.
    Episodes (E01 / S01E01 in the name) are exempt, since they are short on purpose."""
    downloaded, detail = await _find_movie_any(movie_name, run_mode, state)
    if not downloaded or re.search(r"animesalt[^\s/]*/episode/", movie_name or "", re.I) or re.search(r"\b(?:e|ep|episode)\s*\d{1,3}\b|\bs\d{1,2}e\d{1,3}\b", movie_name or "", re.I):
        return downloaded, detail
    dur = await asyncio.to_thread(_probe_duration, downloaded)
    if dur and dur < MIN_MOVIE_SECONDS:
        try:
            os.remove(downloaded)
        except OSError:
            pass
        return None, (f"Only a {int(dur // 60)} min clip (trailer/teaser) was found for this title, not the full movie. "
                      f"Paste the movie page link instead.")
    return downloaded, detail


async def find_and_process(movie_name: str, run_mode: str = None):
    """Search -> download -> convert -> upload to Google Drive -> sync movies.json (the original standalone flow).
    Returns (ok, detail); no env vars, no sys.exit."""
    if not GOOGLE_API_OK:
        return False, "Google Drive libraries are missing - run: pip install google-api-python-client google-auth google-auth-httplib2"
    if not drive_configured():
        return False, "Google Drive is not configured - set GCP_CLIENT_ID, GCP_CLIENT_SECRET, GCP_REFRESH_TOKEN and DRIVE_FOLDER_ID in config.py / .env."
    downloaded, detail = await find_movie(movie_name, run_mode)
    if not downloaded:
        return False, detail
    await asyncio.to_thread(transcode_and_upload, downloaded, movie_name.strip())
    await asyncio.to_thread(sync_movies_json)
    print("[+] Processing and sync completed successfully.")
    return True, "Uploaded to Google Drive."


async def main():
    """Standalone / GitHub-Actions entry point: the title comes from config.MOVIE_NAME (env MOVIE_NAME)."""
    if not MOVIE_NAME:
        sys.exit(1)
    ok, detail = await find_and_process(MOVIE_NAME)
    set_github_output("downloaded", "true" if ok else "false")
    if not ok:
        print(f"[-] {detail}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())

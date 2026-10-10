"""
jav_sites.py — JableTV, MissAV, SupJav and Jav.guru support.

Ported from the standalone "jav-downloader" project's site adapters
(sites/jabletv.py, missav.py, supjav.py, javguru.py + the M3U8Crawler in
sites/base.py), trimmed down to what this bot needs and reshaped into the same
backend contract every other *_downloader.py here follows:

    is_jav_link(url) / extract_jav_links(text)
    get_available_qualities(url) -> [{"label", "height", "url"}, ...] best-first
    download_video(url, out_path, on_progress=None, stream_url=None) -> (path, seconds)
    get_page_meta(url) -> {"title", "poster_url", ...}

How it works
------------
1. The video page is fetched (curl_cffi "chrome" impersonation — all four sites
   sit behind Cloudflare) and the real stream is resolved:
     * JableTV  : og tags + the .m3u8 URL inside the page
     * MissAV   : the .m3u8 URL hidden in a Dean-Edwards packed script
     * SupJav   : server buttons -> FST (HLS, packed script) / Streamtape
                  (progressive MP4) / TV (HLS with fake-PNG-header segments)
     * Jav.guru : STREAM buttons -> gateway -> embed host (javclan, turbovid,
                  VOE, Lulu/StreamHG, DoodStream) -> HLS or MP4
2. HLS streams are downloaded segment-by-segment in parallel (AES-128 aware,
   fake-PNG header stripping for SupJav/TV), and piped straight into ffmpeg
   (-c copy) so the result is a normal .mp4 without needing 2-3x the disk.
3. Progressive MP4 streams use parallel HTTP Range requests when the host
   allows it.

When a page offers several sources (SupJav, Jav.guru) the next one is tried
automatically if one fails.
"""

import base64
import html as html_lib
import json
import logging
import os
import random
import re
import shutil
import string
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

import requests

try:
    from curl_cffi import requests as _cffi
except Exception:  # pragma: no cover - curl_cffi is in requirements.txt
    _cffi = None

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover
    BeautifulSoup = None

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

MIRRORS = {
    "missav": ["missav.ai", "missav.ws", "missav123.com", "missav.live"],
    "jable": ["jable.tv", "fs1.app"],
    "supjav": ["supjav.com"],
    "javguru": ["jav.guru", "www.jav.guru"],
}

_MISSAV_HOSTS = r"(?:www\.)?(?:missav\.(?:ai|ws|live)|missav123\.com)"
_PATTERNS = {
    "jable": re.compile(r"^https?://(?:www\.)?(?:jable\.tv|fs1\.app)/videos/[^/?#]+/?", re.I),
    # video pages only — category pages (/dm278/chinese-subtitle) have no "<letters>-<digits>" code
    "missav": re.compile(
        rf"^https?://{_MISSAV_HOSTS}/(?:dm\d+/)?(?:(?:cn|en|ja|ko|ms|th)/)?"
        r"[a-zA-Z0-9][a-zA-Z0-9\-_]*[-_]\d[a-zA-Z0-9\-_]*", re.I),
    "supjav": re.compile(r"^https?://(?:www\.)?supjav\.com/(?:(?:zh|ja)/)?\d+\.html", re.I),
    "javguru": re.compile(r"^https?://(?:www\.)?jav\.guru/\d+/[^?#]+", re.I),
}
_URL_RE = re.compile(r"https?://\S+")


def site_of(url: str):
    """'jable' / 'missav' / 'supjav' / 'javguru', or None."""
    url = (url or "").strip()
    for name, pat in _PATTERNS.items():
        if pat.match(url):
            return name
    return None


def is_jav_link(url: str) -> bool:
    return site_of(url) is not None


def extract_jav_links(text: str) -> list[str]:
    if not text:
        return []
    seen, out = set(), []
    for match in _URL_RE.findall(text):
        url = match.rstrip(").,!?>'\"")
        if is_jav_link(url) and url not in seen:
            seen.add(url)
            out.append(url)
    return out


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
_tls = threading.local()


class BlockedError(RuntimeError):
    """Cloudflare (or similar) is blocking this server's IP."""


_BLOCKED_MSG = ("The site is blocking this server (Cloudflare). "
                "Try again later or from a different network/IP.")


def _session():
    """One impersonating session per thread (sessions aren't thread-safe)."""
    s = getattr(_tls, "session", None)
    if s is None:
        if _cffi is not None:
            s = _cffi.Session(impersonate="chrome")
        else:
            s = requests.Session()
            s.headers["User-Agent"] = _UA
        _tls.session = s
    return s


def _get(url, headers=None, timeout=30, **kw):
    hdrs = dict(headers or {})
    if _cffi is None:
        hdrs.setdefault("User-Agent", _UA)
    return _session().get(url, headers=hdrs, timeout=timeout, allow_redirects=True, **kw)


def _post(url, data=None, headers=None, timeout=30):
    return _session().post(url, data=data, headers=dict(headers or {}), timeout=timeout,
                           allow_redirects=True)


def _is_cf_interstitial(resp) -> bool:
    if resp.status_code in (403, 429, 503):
        return True
    if "challenge" in str(resp.headers.get("cf-mitigated", "")).lower():
        return True
    head = (resp.content or b"")[:3000].lower()
    return b"just a moment" in head or b"cf-browser-verification" in head or b"cf_chl_" in head


def _fetch_page(url, site_key, validate, headers_factory=None, timeout=30):
    """GET `url`, rotating across the site's mirror domains. Returns (resp, host)."""
    mirrors = MIRRORS.get(site_key) or [urlsplit(url).netloc]
    orig = urlsplit(url).netloc
    order = []
    for h in [orig] + list(mirrors):
        if h in mirrors and h not in order:
            order.append(h)
    saw_real = False
    for host in order:
        p = urlsplit(url)
        target = urlunsplit((p.scheme or "https", host, p.path, p.query, p.fragment))
        hdrs = dict(headers_factory(host) if headers_factory else {})
        for _ in range(2):
            try:
                resp = _get(target, hdrs, timeout=timeout)
            except Exception as e:
                logger.debug(f"jav_sites: {target} transport error: {e}")
                continue
            if _is_cf_interstitial(resp):
                break
            saw_real = True
            try:
                if validate(resp):
                    return resp, host
            except Exception:
                pass
            break
    if saw_real:
        raise RuntimeError("Page could not be parsed (layout changed, or the video no longer exists).")
    raise BlockedError(_BLOCKED_MSG)


def _og(text, prop):
    m = re.search(rf'{prop}"\s+content="([^"]+)"', text) or \
        re.search(rf'content="([^"]+)"\s+(?:property|name)="{prop}"', text)
    return html_lib.unescape(m.group(1)).strip() if m else None


def _origin(url):
    p = urlsplit(str(url or ""))
    return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else None


def _hdrs_for_origin(origin):
    origin = (origin or "").rstrip("/")
    return {"Referer": origin + "/", "Origin": origin}


# ---------------------------------------------------------------------------
# Packers / decoders shared by the site adapters
# ---------------------------------------------------------------------------

def _to_base(n, base):
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if n == 0:
        return "0"
    s = ""
    while n:
        s = digits[n % base] + s
        n //= base
    return s


def _unpack_js_eval(script_text):
    """Decode a Dean Edwards p,a,c,k,e,d packed script (None when not packed)."""
    m = re.search(
        r"eval\(function\(p,a,c,k,e,d\)\{.*?\}\('(.*?)',\s*(\d+),\s*(\d+),\s*'([^']*)'\s*\.split\('\|'\)",
        script_text, re.DOTALL)
    if not m:
        return None
    packed, a, c, keys = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4).split("|")
    if a <= 1 or c < 0 or c > 200000:
        return None
    lookup = {_to_base(i, a): (keys[i] if i < len(keys) and keys[i] else _to_base(i, a)) for i in range(c)}
    return re.sub(r"\b(\w+)\b", lambda mm: lookup.get(mm.group(0), mm.group(0)), packed)


def _packed_m3u8(html_text):
    for script in re.findall(r"<script[^>]*>(.*?)</script>", html_text or "", re.DOTALL):
        if "eval(function" not in script or "m3u8" not in script:
            continue
        unpacked = _unpack_js_eval(script)
        if not unpacked:
            continue
        m = re.search(r"source\s*=\s*[\\']*(https?://[^'\\;\s]+\.m3u8[^'\\;\s]*)", unpacked) or \
            re.search(r"https?://[^'\"\\;\s]+\.m3u8[^'\"\\;\s]*", unpacked)
        if m:
            return m.group(1) if m.groups() else m.group(0)
    return None


def _strip_fake_header(data: bytes) -> bytes:
    """SupJav / Jav.guru-TV segments are MPEG-TS hidden behind a fake PNG header."""
    if data[:1] == b"\x47":
        return data
    limit = min(len(data) - 188 * 4 - 1, 8000)
    i = 0
    while 0 <= i <= limit:
        j = data.find(b"\x47", i)
        if j < 0 or j > limit:
            break
        if all(data[j + 188 * n] == 0x47 for n in range(5)):
            return data[j:]
        i = j + 1
    return b""


# ---------------------------------------------------------------------------
# Source model
# ---------------------------------------------------------------------------

class Source:
    """One downloadable stream: kind is 'hls' or 'mp4'."""

    def __init__(self, kind, url, headers=None, fake_header=False, label=""):
        self.kind = kind
        self.url = url
        self.headers = dict(headers or {})
        self.fake_header = fake_header
        self.label = label


class Resolved:
    def __init__(self, title, thumb, sources_iter, page_url, meta=None):
        self.title = title
        self.thumb = thumb
        self._iter = sources_iter          # lazy generator of Source
        self._cache: list[Source] = []
        self.page_url = page_url
        self.meta = meta or {}
        self.errors: list[str] = []

    def sources(self):
        """Yield every source in priority order, resolving lazily and remembering them."""
        i = 0
        while True:
            if i < len(self._cache):
                yield self._cache[i]
                i += 1
                continue
            try:
                nxt = next(self._iter)
            except StopIteration:
                return
            self._cache.append(nxt)


# ---------------------------------------------------------------------------
# Site: JableTV
# ---------------------------------------------------------------------------

def _resolve_jable(url):
    def validate(r):
        return "og:title" in r.text and "m3u8" in r.text
    resp, host = _fetch_page(url, "jable", validate)
    text = resp.text
    title = _og(text, "og:title")
    thumb = _og(text, "og:image")
    m = re.search(r'https://[^\s"\']+\.m3u8', text)
    if not (title and m):
        raise RuntimeError("Could not find the stream on the JableTV page.")
    hdrs = _hdrs_for_origin(f"https://{host}")

    def gen():
        yield Source("hls", m.group(0), hdrs, label="JableTV")
    return Resolved(title, thumb, gen(), url)


# ---------------------------------------------------------------------------
# Site: MissAV
# ---------------------------------------------------------------------------

def _resolve_missav(url):
    hf = lambda host: {"Referer": f"https://{host}/", "Origin": f"https://{host}"}

    def validate(r):
        return "og:title" in r.text and ("m3u8" in r.text or "eval(function(p,a,c,k,e,d)" in r.text)
    resp, host = _fetch_page(url, "missav", validate, headers_factory=hf)
    text = resp.text
    m3u8 = _packed_m3u8(text)
    if not m3u8:
        m = re.search(r'https://[^\s"\'\\]+\.m3u8', text)
        m3u8 = m.group(0) if m else None
    if not m3u8:
        raise RuntimeError("Could not find the stream on the MissAV page.")

    def gen():
        yield Source("hls", m3u8, hf(host), label="MissAV")
    return Resolved(_og(text, "og:title"), _og(text, "og:image"), gen(), url)


# ---------------------------------------------------------------------------
# Site: SupJav
# ---------------------------------------------------------------------------

_SUPREMEJAV = "https://lk1.supremejav.com/supjav.php?c={}"


def _supjav_servers(html_text):
    soup = BeautifulSoup(html_text, "html.parser")
    out = {}
    for a in soup.select("a.btn-server[data-link]"):
        name = a.get_text(strip=True).upper()
        link = a.get("data-link", "")
        if name and link and name not in out:
            out[name] = link
    return out


def _streamtape_direct_url(html_text):
    m = re.search(
        r"getElementById\(\s*['\"]robotlink['\"]\s*\)\.innerHTML\s*=\s*"
        r"['\"]([^'\"]*)['\"]\s*\+\s*(?:['\"]{2}\s*\+\s*)?"
        r"\(\s*['\"]([^'\"]*)['\"]\s*\)((?:\.substring\(\s*\d+\s*\))+)", html_text)
    if not m:
        return None
    prefix, suffix, subs = m.group(1), m.group(2), m.group(3)
    for off in re.findall(r"substring\(\s*(\d+)\s*\)", subs):
        suffix = suffix[int(off):]
    link = (prefix + suffix).lstrip("/")
    return "https://" + link if "get_video" in link else None


def _first_m3u8(body):
    body = (body or "").replace("\\/", "/")
    m = re.search(r"urlPlay[\s=:'\"]+(?P<u>https?://[^\s'\"\\]+\.m3u8[^\s'\"\\]*)", body)
    if m:
        return m.group("u")
    m = re.search(r"https?://[^\s'\"\\]+\.m3u8[^\s'\"\\]*", body)
    return m.group(0) if m else None


def _resolve_supjav(url):
    resp, _host = _fetch_page(url, "supjav", lambda r: "data-link" in r.text)
    servers = _supjav_servers(resp.text)
    if not servers:
        raise RuntimeError("SupJav: no server sources found on the page (layout changed?).")
    soup = BeautifulSoup(resp.content, "html.parser")
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else (soup.title.get_text(strip=True) if soup.title else "")
    thumb = _og(resp.text, "og:image")
    page_ref = {"Referer": "https://supjav.com/"}

    def embed(name):
        return _get(_SUPREMEJAV.format(servers[name][::-1]), page_ref, timeout=25)

    def gen():
        # 1) FST — HLS with real 480/720/1080 variants
        if "FST" in servers:
            try:
                r = embed("FST")
                m3u8 = _packed_m3u8(r.text)
                if m3u8:
                    hdrs = {"Referer": str(r.url)}
                    o = _origin(str(r.url))
                    if o:
                        hdrs["Origin"] = o
                    yield Source("hls", m3u8, hdrs, label="SupJav FST")
            except BlockedError:
                raise
            except Exception as e:
                logger.info(f"SupJav FST failed: {e}")
        # 2) Streamtape — progressive MP4
        if "ST" in servers:
            try:
                r = embed("ST")
                direct = _streamtape_direct_url(r.text)
                if direct:
                    yield Source("mp4", direct, {"Referer": str(r.url) or "https://streamtape.com/"},
                                 label="SupJav Streamtape")
            except Exception as e:
                logger.info(f"SupJav Streamtape failed: {e}")
        # 3) TV — HLS with fake-header segments (often rate limited)
        if "TV" in servers:
            try:
                r = embed("TV")
                if r.status_code in (403, 429, 503):
                    raise BlockedError(_BLOCKED_MSG)
                m3u8 = _first_m3u8(r.text)
                if m3u8:
                    yield Source("hls", m3u8, page_ref, fake_header=True, label="SupJav TV")
            except BlockedError:
                raise
            except Exception as e:
                logger.info(f"SupJav TV failed: {e}")
    return Resolved(html_lib.unescape(title), thumb, gen(), url)


# ---------------------------------------------------------------------------
# Site: Jav.guru
# ---------------------------------------------------------------------------

_SERVER_PRIORITY = ("SB", "TV", "VO", "LU", "DD", "JK", "EA")
_SKIP_SERVERS = {"AV"}
_JAVCLAN = {"javclan.com", "www.javclan.com"}
_TURBOVID = {"turbovidhls.com", "www.turbovidhls.com"}
_LULU = {"maxstream.org", "www.maxstream.org", "streamhihi.com", "www.streamhihi.com",
         "lulustream.com", "www.lulustream.com", "luluvdo.com", "www.luluvdo.com",
         "luluvdoo.com", "www.luluvdoo.com"}
_DOOD_MARKERS = ("playmogo.com", "doodstream.com", "dood.", "dooood.")
_TITLE_SUFFIX_RE = re.compile(
    r"\s*[⋆✦•·|]\s*Jav\s*Guru\s*[⋆✦•·|]?\s*(?:Japanese\s+porn\s+Tube)?\s*$", re.I)
_SITE_SUFFIX_RE = re.compile(r"\s*[|⋆✦•·-]\s*(?:Jav\s*Guru|Japanese\s+porn\s+Tube)\s*$", re.I)


def _guru_servers(html_text):
    soup = BeautifulSoup(html_text, "html.parser")
    found, order = {}, []
    for a in soup.select("a.wp-btn-iframe__shortcode[data-localize]"):
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True).upper())
        if not text.startswith("STREAM "):
            continue
        label = text.split(" ", 1)[1].strip()
        token = str(a.get("data-localize") or "").strip()
        if not label or label in _SKIP_SERVERS or not token or label in found:
            continue
        found[label] = token
        order.append(label)
    servers = {k: found[k] for k in _SERVER_PRIORITY if k in found}
    for k in order:
        servers.setdefault(k, found[k])
    return servers


def _guru_gateway(html_text, token):
    m = re.search(rf"var\s+{re.escape(token)}\s*=\s*(\{{.*?\}})\s*;", html_text, re.S)
    if not m:
        return None
    try:
        raw = str(json.loads(m.group(1)).get("iframe_url") or "").strip()
        return base64.b64decode(raw).decode("utf-8") if raw else None
    except Exception:
        return None


def _guru_redirect(gateway):
    q = parse_qs(urlsplit(gateway).query)
    for key in q:
        if len(key) == 2 and key[1] == "d" and q[key]:
            return f"https://jav.guru/searcho/?{key[0]}r={q[key][0][::-1]}"
    return None


def _extract_m3u8_text(text):
    m = re.search(r'https://[^\s"\'\\]+\.m3u8[^\s"\'\\]*', text or "")
    if m:
        return m.group(0).replace("\\/", "/")
    m = re.search(r'https://[^\s"\'\\]+master\.txt[^\s"\'\\]*', text or "")
    return m.group(0).replace("\\/", "/") if m else None


def _unpack_jw(script_text):
    m = re.search(
        r"eval\(function\(p,a,c,k,e,d\)\{while\(c--\)if\(k\[c\]\)p=p\.replace\("
        r"new RegExp\('\\\\b'\+c\.toString\(a\)\+'\\\\b','g'\),k\[c\]\);return p\}\("
        r"'(.*?)',\s*(\d+),\s*(\d+),\s*'(.*?)'\.split\('\|'\)", script_text, re.S)
    if not m:
        return _unpack_js_eval(script_text)
    packed, base, count, keys = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4).split("|")
    if base <= 1 or count < 0 or count > 200000:
        return None
    lookup = {_to_base(i, base): (keys[i] if i < len(keys) and keys[i] else _to_base(i, base))
              for i in range(count)}
    return re.sub(r"\b\w+\b", lambda mm: lookup.get(mm.group(0), mm.group(0)), packed)


def _playlist_from_html(html_text):
    pl = _extract_m3u8_text(html_text)
    if pl:
        return pl
    for script in re.findall(r"<script[^>]*>(.*?)</script>", html_text or "", re.S):
        if "eval(function(p,a,c,k,e,d)" not in script:
            continue
        unpacked = _unpack_jw(script)
        if not unpacked:
            continue
        for key in ("hls4", "hls3", "hls2"):
            m = re.search(rf'"{key}"\s*:\s*"([^"]+)"', unpacked)
            if m:
                return m.group(1).replace("\\/", "/")
        pl = _extract_m3u8_text(unpacked)
        if pl:
            return pl
    return None


def _abs_stream_url(url, base):
    t = str(url or "").strip().replace("\\/", "/")
    if t.startswith("/"):
        o = _origin(base)
        return f"{o}{t}" if o else t
    return t


def _voe_rot13(v):
    out = []
    for ch in v:
        if "A" <= ch <= "Z":
            out.append(chr((ord(ch) - 65 + 13) % 26 + 65))
        elif "a" <= ch <= "z":
            out.append(chr((ord(ch) - 97 + 13) % 26 + 97))
        else:
            out.append(ch)
    return "".join(out)


def _voe_decrypt(encoded):
    p = _voe_rot13(encoded)
    for pat in ("@$", "^^", "~@", "%?", "*~", "!!", "#&"):
        p = p.replace(pat, "_")
    p = p.replace("_", "")
    p = base64.b64decode(p + "=" * (-len(p) % 4))
    p = "".join(chr(ord(c) - 3) for c in p.decode("latin1"))[::-1]
    return json.loads(base64.b64decode(p + "=" * (-len(p) % 4)))


def _fetch_playlist_text(url, headers):
    try:
        r = _get(url, headers, timeout=20)
    except Exception:
        return None
    if r.status_code != 200 or "#EXTM3U" not in (r.text or ""):
        return None
    return r.text


def _normalize_voe_playlist(source, headers):
    source = str(source or "").replace("\\/", "/").strip()
    if not source:
        return None
    if source.endswith(".m3u8") or source.endswith(".txt"):
        return source
    for cand in (source.rstrip("/") + "/master.m3u8", source.rstrip("/") + "/master.txt",
                 source.rstrip("/") + ".m3u8"):
        if _fetch_playlist_text(cand, headers):
            return cand
    return None


def _resolve_voe(embed_url):
    origin = _origin(embed_url)
    if not origin:
        return None
    hdrs = _hdrs_for_origin(origin)
    r = _get(embed_url, hdrs)
    if _is_cf_interstitial(r):
        raise BlockedError(_BLOCKED_MSG)
    text = r.text
    hdrs = _hdrs_for_origin(_origin(str(r.url)) or origin)
    red = re.search(r"window\.location\.href\s*=\s*'([^']+)';", text)
    if red:
        r = _get(red.group(1), hdrs)
        if _is_cf_interstitial(r):
            raise BlockedError(_BLOCKED_MSG)
        text = r.text
        hdrs = _hdrs_for_origin(_origin(str(r.url)) or origin)
    scripts = re.findall(r"<script[^>]+type=['\"]application/json['\"][^>]*>(.*?)</script>", text, re.S)
    if not scripts:
        return None
    try:
        payload = _voe_decrypt(scripts[0].strip().split('["', 1)[-1].rsplit('"]', 1)[0])
    except Exception:
        return None
    pl = _normalize_voe_playlist(payload.get("source") or payload.get("direct_access_url"), hdrs)
    return ("hls", pl, hdrs) if pl else None


def _resolve_doodstream(embed_url):
    origin = _origin(embed_url)
    code = urlsplit(embed_url).path.rstrip("/").rsplit("/", 1)[-1]
    if not origin or not code:
        return None
    page = re.sub(r"/d/", "/e/", embed_url, count=1) if "/d/" in urlsplit(embed_url).path else embed_url
    r = _get(page, _hdrs_for_origin(origin))
    if _is_cf_interstitial(r):
        return None
    text = r.text
    if "no_video" in text or "not found" in text.lower():
        return None
    host = _origin(str(r.url)) or origin
    m = re.search(r"/pass_md5/[^'\"\s<>]+", text)
    if not m:
        return None
    md5_url = host + m.group(0)
    pr = _get(md5_url, {"Referer": str(r.url)})
    prefix = (pr.text or "").strip()
    if pr.status_code != 200 or not prefix.startswith("http"):
        return None
    token = md5_url.rsplit("/", 1)[-1]
    suffix = "".join(random.choices(string.ascii_letters + string.digits, k=10))
    return ("mp4", f"{prefix}{suffix}?token={token}", _hdrs_for_origin(host))


def _resolve_lulu(embed_url, page_referer):
    origin = _origin(embed_url)
    code = urlsplit(embed_url).path.rstrip("/").rsplit("/", 1)[-1]
    if not origin or not code:
        return None
    r = _post(f"{origin}/dl",
              data={"op": "embed", "file_code": code, "auto": "1", "referer": page_referer or f"{origin}/"},
              headers={"Referer": embed_url, "Origin": origin})
    if _is_cf_interstitial(r):
        return None
    low = (r.text or "").lower()
    if any(x in low for x in ("no longer available", "expired", "embed disabled", "not found")):
        return None
    pl = _playlist_from_html(r.text or "")
    return ("hls", pl, _hdrs_for_origin(origin)) if pl else None


def _resolve_javclan(embed_url):
    origin = _origin(embed_url) or "https://javclan.com"
    r = _get(embed_url, _hdrs_for_origin(origin))
    if _is_cf_interstitial(r):
        raise BlockedError(_BLOCKED_MSG)
    final = str(r.url or embed_url)
    pl = _abs_stream_url(_playlist_from_html(r.text), final)
    return ("hls", pl, _hdrs_for_origin(_origin(final) or origin)) if pl else None


def _resolve_turbovid(embed_url):
    origin = _origin(embed_url) or "https://turbovidhls.com"
    r = _get(embed_url, _hdrs_for_origin(origin))
    if _is_cf_interstitial(r):
        raise BlockedError(_BLOCKED_MSG)
    pl = _extract_m3u8_text(r.text)
    return ("hls", pl, _hdrs_for_origin(_origin(str(r.url or embed_url)) or origin)) if pl else None


def _resolve_embed(embed_url, page_referer):
    host = (urlsplit(embed_url).hostname or "").lower()
    attempts = []
    if host in _JAVCLAN:
        attempts.append(("javclan", lambda: _resolve_javclan(embed_url)))
    if host in _TURBOVID:
        attempts.append(("turbovid", lambda: _resolve_turbovid(embed_url)))
    attempts.append(("voe", lambda: _resolve_voe(embed_url)))
    if host in _LULU:
        attempts.append(("lulu", lambda: _resolve_lulu(embed_url, page_referer)))
    if any(mk in host for mk in _DOOD_MARKERS):
        attempts.append(("dood", lambda: _resolve_doodstream(embed_url)))
    attempts.append(("lulu2", lambda: _resolve_lulu(embed_url, page_referer)))
    attempts.append(("javclan2", lambda: _resolve_javclan(embed_url)))
    attempts.append(("turbovid2", lambda: _resolve_turbovid(embed_url)))
    for _name, fn in attempts:
        try:
            res = fn()
        except BlockedError:
            raise
        except Exception:
            continue
        if res:
            return res
    return None


def _looks_hls(url):
    low = str(url or "").lower()
    return any(x in low for x in (".m3u8", ".txt", "/master.", "/index-"))


def _hls_candidates(url):
    out = [url]
    if "/" in url:
        base, name = url.rsplit("/", 1)
        if name.endswith(".txt"):
            out += [f"{base}/master.m3u8", url[:-4] + ".m3u8"]
        elif not name.endswith(".m3u8"):
            root = url.rstrip("/")
            out += [f"{root}/master.m3u8", f"{root}/master.txt", f"{root}.m3u8"]
    seen, res = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            res.append(c)
    return res


def _probe_hls(url, headers):
    """First candidate that really is a playlist with media segments."""
    for cand in _hls_candidates(url):
        text = _fetch_playlist_text(cand, headers)
        if not text:
            continue
        if "#EXTINF" in text:
            return cand
        if "#EXT-X-STREAM-INF" in text:
            for v in _parse_master(text, cand)[:4]:
                vt = _fetch_playlist_text(v["url"], headers)
                if vt and "#EXTINF" in vt:
                    return cand
    return None


def _guru_title(raw):
    t = html_lib.unescape(str(raw or "")).strip()
    t = _TITLE_SUFFIX_RE.sub("", t).strip()
    return _SITE_SUFFIX_RE.sub("", t).strip()


def _resolve_javguru(url):
    resp, _host = _fetch_page(url, "javguru",
                              lambda r: "data-localize" in r.text and "wp-btn-iframe" in r.text)
    text = resp.text
    servers = _guru_servers(text)
    if not servers:
        raise RuntimeError("Jav.guru: no STREAM sources found on the page (layout changed?).")
    title = _guru_title(_og(text, "og:title") or "")
    thumb = _og(text, "og:image")

    def gen():
        for label, token in servers.items():
            try:
                gateway = _guru_gateway(text, token)
                redirect = _guru_redirect(gateway) if gateway else None
                if not redirect:
                    continue
                er = _get(redirect, {"Referer": url})
                if _is_cf_interstitial(er):
                    continue
                embed_url = str(er.url or "")
                if not embed_url.startswith("http"):
                    continue
                resolved = _resolve_embed(embed_url, url)
                if not resolved:
                    continue
                kind, stream, extra = resolved
                stream = _abs_stream_url(stream, embed_url)
                if kind == "mp4" or (kind == "hls" and not _looks_hls(stream)):
                    r = _session().head(stream, headers=dict(extra), timeout=20, allow_redirects=True)
                    if r.status_code not in (200, 206):
                        continue
                    yield Source("mp4", stream, extra, label=f"Jav.guru STREAM {label}")
                elif stream.startswith("http"):
                    probed = _probe_hls(stream, extra)
                    if probed:
                        # TV/VO style hosts serve fake-PNG-header segments; harmless for plain TS
                        yield Source("hls", probed, extra, fake_header=True,
                                     label=f"Jav.guru STREAM {label}")
            except BlockedError:
                raise
            except Exception as e:
                logger.info(f"Jav.guru STREAM {label} failed: {e}")
    return Resolved(title, thumb, gen(), url)


_RESOLVERS = {
    "jable": _resolve_jable,
    "missav": _resolve_missav,
    "supjav": _resolve_supjav,
    "javguru": _resolve_javguru,
}

_PAGE_CACHE: dict[str, tuple[float, Resolved]] = {}
_PAGE_CACHE_TTL = 300
_cache_lock = threading.Lock()


def _resolve(url: str, force: bool = False) -> Resolved:
    site = site_of(url)
    if not site:
        raise RuntimeError(f"Not a supported JAV site link: {url}")
    now = time.time()
    with _cache_lock:
        hit = _PAGE_CACHE.get(url)
        if hit and not force and now - hit[0] < _PAGE_CACHE_TTL:
            return hit[1]
    resolved = _RESOLVERS[site](url)
    with _cache_lock:
        if len(_PAGE_CACHE) > 50:
            _PAGE_CACHE.clear()
        _PAGE_CACHE[url] = (now, resolved)
    return resolved


# ---------------------------------------------------------------------------
# HLS handling
# ---------------------------------------------------------------------------

def _parse_attrs(s):
    out = {}
    for m in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', s):
        out[m.group(1)] = m.group(2).strip('"')
    return out


def _parse_master(text, base_url):
    """[{url, height, bandwidth}] best-first. Empty when `text` is a media playlist."""
    variants, lines = [], text.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        attrs = _parse_attrs(line.split(":", 1)[1])
        uri = next((l.strip() for l in lines[i + 1:] if l.strip() and not l.startswith("#")), None)
        if not uri:
            continue
        height = None
        rm = re.match(r"(\d+)x(\d+)", attrs.get("RESOLUTION", ""))
        if rm:
            height = int(rm.group(2))
        try:
            bw = int(attrs.get("BANDWIDTH", 0))
        except ValueError:
            bw = 0
        variants.append({"url": urljoin(base_url, uri), "height": height, "bandwidth": bw})
    variants.sort(key=lambda v: (v["height"] or 0, v["bandwidth"]), reverse=True)
    return variants


def _parse_media(text, base_url):
    """-> dict(segments=[(url, duration)], key=dict|None, map_url, media_seq, ...)"""
    segs, key, map_url, media_seq = [], None, None, 0
    dur = 0.0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                media_seq = int(line.split(":", 1)[1])
            except ValueError:
                pass
        elif line.startswith("#EXT-X-KEY:"):
            attrs = _parse_attrs(line.split(":", 1)[1])
            if attrs.get("METHOD", "NONE").upper() == "NONE":
                key = None
            elif key is None:  # first key only (same as the reference downloader)
                key = {"method": attrs.get("METHOD"), "uri": urljoin(base_url, attrs.get("URI", "")),
                       "iv": attrs.get("IV")}
        elif line.startswith("#EXT-X-MAP:"):
            attrs = _parse_attrs(line.split(":", 1)[1])
            if attrs.get("URI"):
                map_url = urljoin(base_url, attrs["URI"])
        elif line.startswith("#EXTINF:"):
            try:
                dur = float(line.split(":", 1)[1].split(",")[0])
            except ValueError:
                dur = 0.0
        elif not line.startswith("#"):
            segs.append((urljoin(base_url, line), dur))
            dur = 0.0
    return {"segments": segs, "key": key, "map_url": map_url, "media_seq": media_seq}


def _aes_decrypt(data, key, iv_hex, seq):
    try:
        from Crypto.Cipher import AES  # pycryptodome (optional)
        def dec(k, iv, d):
            return AES.new(k, AES.MODE_CBC, iv).decrypt(d)
    except Exception:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        def dec(k, iv, d):
            c = Cipher(algorithms.AES(k), modes.CBC(iv)).decryptor()
            return c.update(d) + c.finalize()
    if iv_hex:
        iv = bytes.fromhex(iv_hex.replace("0x", "").replace("0X", "").zfill(32))
    else:
        iv = seq.to_bytes(16, "big")
    cut = len(data) - (len(data) % 16)
    out = dec(key, iv, data[:cut]) if cut else b""
    if out:  # PKCS7 padding on the last block
        pad = out[-1]
        if 1 <= pad <= 16:
            out = out[:-pad]
    return out


def _seg_headers(src: Source, playlist_url: str):
    h = {"User-Agent": _UA, **src.headers}
    ref = _origin(playlist_url)
    if ref:
        h["Referer"] = ref + "/"
    return h


def _hls_load(src: Source, height: int | None):
    """Fetch master/media playlists. -> (media_playlist_url, parsed media, variants)"""
    r = _get(src.url, src.headers, timeout=25)
    if r.status_code != 200:
        if _is_cf_interstitial(r):
            raise BlockedError(_BLOCKED_MSG)
        raise RuntimeError(f"Playlist request failed (HTTP {r.status_code})")
    text = r.text or ""
    if "#EXTM3U" not in text:
        raise RuntimeError("Playlist response isn't an m3u8.")
    variants = _parse_master(text, src.url)
    media_url = src.url
    if variants:
        pick = variants[0]
        if height:
            exact = [v for v in variants if v["height"] == height]
            lower = [v for v in variants if v["height"] and v["height"] <= height]
            pick = (exact or lower or variants)[0]
        media_url = pick["url"]
        mr = _get(media_url, src.headers, timeout=25)
        if mr.status_code != 200:
            raise RuntimeError(f"Variant playlist request failed (HTTP {mr.status_code})")
        text = mr.text
    return media_url, _parse_media(text, media_url), variants


def _find_ffmpeg():
    return shutil.which("ffmpeg") or "ffmpeg"


def _download_hls(src: Source, out_path: str, height, on_progress, start_time, workers):
    media_url, media, _variants = _hls_load(src, height)
    segs = media["segments"]
    if not segs:
        raise RuntimeError("Playlist has no segments.")
    hdrs = _seg_headers(src, media_url)
    key_bytes = None
    if media["key"] and media["key"].get("uri"):
        kr = _get(media["key"]["uri"], hdrs, timeout=20)
        if kr.status_code != 200:
            raise RuntimeError("Could not fetch the AES key.")
        key_bytes = kr.content
    n = len(segs)
    tmp = tempfile.mkdtemp(prefix="jav_hls_", dir=os.path.dirname(out_path) or None)
    lock = threading.Lock()
    stats = {"bytes": 0, "done": 0}
    stop = threading.Event()

    def report():
        if not on_progress:
            return
        elapsed = time.time() - start_time
        done, got = stats["done"], stats["bytes"]
        total_est = int(got / done * n) if done else None
        speed = got / elapsed if elapsed > 0 else 0
        on_progress({
            "pct": done / n * 100,
            "downloaded_bytes": got,
            "total_bytes": total_est,
            "speed_bytes_s": speed,
            "eta_s": ((total_est - got) / speed) if (total_est and speed > 0) else None,
            "elapsed_s": elapsed,
            "duration_s": None,
            "connecting": False,
        })

    def fetch(i):
        path = os.path.join(tmp, f"{i:06d}.seg")
        last = None
        for attempt in range(6):
            if stop.is_set():
                return
            try:
                r = _get(segs[i][0], hdrs, timeout=60)
                if r.status_code != 200 or not r.content:
                    raise RuntimeError(f"HTTP {r.status_code}")
                data = r.content
                if src.fake_header:
                    data = _strip_fake_header(data) or data
                if key_bytes:
                    data = _aes_decrypt(data, key_bytes, media["key"].get("iv"), i + media["media_seq"])
                if not media["map_url"] and data[:1] != b"\x47":
                    raise RuntimeError("segment isn't valid MPEG-TS")
                with open(path, "wb") as f:
                    f.write(data)
                with lock:
                    stats["bytes"] += len(data)
                    stats["done"] += 1
                report()
                return
            except Exception as e:
                last = e
                time.sleep(min(1.5 * (attempt + 1), 8))
        raise RuntimeError(f"Segment {i + 1}/{n} failed after retries: {last}")

    ff = None
    try:
        init_bytes = b""
        if media["map_url"]:
            ir = _get(media["map_url"], hdrs, timeout=30)
            if ir.status_code != 200:
                raise RuntimeError("Could not fetch the fMP4 init segment.")
            init_bytes = ir.content

        part = out_path + ".part.mp4"
        cmd = [_find_ffmpeg(), "-y", "-loglevel", "error"]
        if not media["map_url"]:
            cmd += ["-f", "mpegts"]
        cmd += ["-i", "pipe:0", "-c", "copy", "-bsf:a", "aac_adtstoasc",
                "-movflags", "+faststart", "-f", "mp4", part]
        ff = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        if init_bytes:
            ff.stdin.write(init_bytes)

        ex = ThreadPoolExecutor(max_workers=workers)
        futures = [ex.submit(fetch, i) for i in range(n)]
        try:
            # feed ffmpeg in order as soon as each segment lands, deleting it afterwards
            for i in range(n):
                path = os.path.join(tmp, f"{i:06d}.seg")
                while not os.path.exists(path):
                    if futures[i].done() and futures[i].exception():
                        raise futures[i].exception()
                    if ff.poll() is not None:
                        raise RuntimeError("ffmpeg stopped unexpectedly: "
                                           + (ff.stderr.read().decode("utf-8", "ignore")[-300:]))
                    time.sleep(0.05)
                futures[i].result()
                with open(path, "rb") as f:
                    shutil.copyfileobj(f, ff.stdin, 1 << 20)
                os.remove(path)
        except BaseException:
            stop.set()
            for fu in futures:
                fu.cancel()
            raise
        finally:
            ex.shutdown(wait=True)
        ff.stdin.close()
        rc = ff.wait()
        if rc != 0:
            raise RuntimeError("ffmpeg failed: " + ff.stderr.read().decode("utf-8", "ignore")[-300:])
        os.replace(part, out_path)
    except BaseException:
        stop.set()
        if ff and ff.poll() is None:
            ff.kill()
        try:
            os.remove(out_path + ".part.mp4")
        except OSError:
            pass
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Progressive MP4
# ---------------------------------------------------------------------------

_RANGE_WORKERS = 8


def _content_range(value):
    m = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+)", str(value or "").strip(), re.I)
    if not m:
        return None
    a, b, t = (int(x) for x in m.groups())
    return (a, b, t) if a <= b < t else None


def _download_mp4(src: Source, out_path: str, on_progress, start_time):
    hdrs = {"User-Agent": _UA, **src.headers}
    state = {"done": 0}
    lock = threading.Lock()

    def report(total):
        if not on_progress:
            return
        elapsed = time.time() - start_time
        done = state["done"]
        speed = done / elapsed if elapsed > 0 else 0
        on_progress({
            "pct": (done / total * 100) if total else None,
            "downloaded_bytes": done,
            "total_bytes": total or None,
            "speed_bytes_s": speed,
            "eta_s": ((total - done) / speed) if (total and speed > 0) else None,
            "elapsed_s": elapsed,
            "duration_s": None,
            "connecting": False,
        })

    total = 0
    try:
        probe = _session().get(src.url, headers={**hdrs, "Range": "bytes=0-0"}, timeout=40, stream=True)
        info = _content_range(probe.headers.get("content-range"))
        ok_range = probe.status_code == 206 and info and info[:2] == (0, 0)
        if ok_range:
            total = info[2]
        elif probe.status_code == 200:
            total = int(probe.headers.get("content-length") or 0)
        try:
            probe.close()
        except Exception:
            pass
    except Exception:
        ok_range = False

    part = out_path + ".part"
    if ok_range and total > 4 * 1024 * 1024:
        chunk, rem = divmod(total, _RANGE_WORKERS)
        ranges, start = [], 0
        for i in range(_RANGE_WORKERS):
            size = chunk + (1 if i < rem else 0)
            ranges.append((start, start + size - 1))
            start += size
        with open(part, "wb") as f:
            f.truncate(total)
        stop = threading.Event()

        def fetch(bounds):
            lo, hi = bounds
            written, retries = 0, 0
            while lo + written <= hi and not stop.is_set():
                cur = lo + written
                try:
                    r = _session().get(src.url, headers={**hdrs, "Range": f"bytes={cur}-{hi}"},
                                       timeout=60, stream=True)
                    if r.status_code != 206:
                        raise RuntimeError(f"HTTP {r.status_code}")
                    with open(part, "r+b", buffering=0) as f:
                        f.seek(cur)
                        for data in r.iter_content(chunk_size=262144):
                            if stop.is_set():
                                return
                            if not data:
                                continue
                            f.write(data)
                            written += len(data)
                            cur = lo + written
                            with lock:
                                state["done"] += len(data)
                            report(total)
                    try:
                        r.close()
                    except Exception:
                        pass
                except Exception as e:
                    retries += 1
                    if retries > 5:
                        raise RuntimeError(f"MP4 range download failed: {e}")
                    time.sleep(retries)
            return

        ex = ThreadPoolExecutor(max_workers=len(ranges))
        futs = [ex.submit(fetch, b) for b in ranges]
        try:
            for fu in as_completed(futs):
                fu.result()
        except BaseException:
            stop.set()
            raise
        finally:
            ex.shutdown(wait=True)
        if state["done"] < total:
            raise RuntimeError("MP4 download incomplete.")
    else:
        r = _session().get(src.url, headers=hdrs, timeout=60, stream=True)
        if r.status_code not in (200, 206):
            raise RuntimeError(f"HTTP {r.status_code}")
        total = total or int(r.headers.get("content-length") or 0)
        with open(part, "wb") as f:
            for data in r.iter_content(chunk_size=262144):
                if not data:
                    continue
                f.write(data)
                with lock:
                    state["done"] += len(data)
                report(total)
    os.replace(part, out_path)


# ---------------------------------------------------------------------------
# Public API (same contract as the other *_downloader.py modules)
# ---------------------------------------------------------------------------

def _quality_variants(url: str):
    """-> list of {"label","height","url"} from the first working source."""
    res = _resolve(url)
    last = None
    for src in res.sources():
        if src.kind == "hls":
            try:
                r = _get(src.url, src.headers, timeout=25)
                if r.status_code != 200 or "#EXTM3U" not in (r.text or ""):
                    if _is_cf_interstitial(r):
                        raise BlockedError(_BLOCKED_MSG)
                    raise RuntimeError(f"HTTP {r.status_code}")
                variants = _parse_master(r.text, src.url)
            except BlockedError:
                raise
            except Exception as e:
                last = e
                continue
            out, seen = [], set()
            for v in variants:
                h = v["height"]
                if h and h not in seen:
                    seen.add(h)
                    out.append({"label": f"{h}p", "height": h, "url": f"{url}#q={h}"})
            return out or [{"label": "Best available", "height": None, "url": url}]
        return [{"label": "Best available", "height": None, "url": url}]
    raise RuntimeError(str(last) if last else "No downloadable stream was found for this video.")


def get_available_qualities(video_url: str) -> list:
    video_url = video_url.split("#", 1)[0]
    return _quality_variants(video_url)


def get_page_meta(video_url: str) -> dict:
    video_url = video_url.split("#", 1)[0]
    try:
        res = _resolve(video_url)
    except Exception as e:
        logger.warning(f"jav_sites.get_page_meta failed for {video_url}: {e}")
        return {"title": None, "author": None, "duration": None, "poster_url": None,
                "view_count": None, "like_count": None, "comment_count": None, "upload_date": None}
    return {"title": res.title, "author": None, "duration": None, "poster_url": res.thumb,
            "view_count": None, "like_count": None, "comment_count": None, "upload_date": None}


def download_video(video_url: str, out_path: str, on_progress=None, stream_url: str = None):
    """Download to out_path (.mp4). `stream_url` may carry '#q=<height>' from the quality menu."""
    height = None
    if stream_url and "#q=" in stream_url:
        try:
            height = int(stream_url.split("#q=", 1)[1])
        except ValueError:
            height = None
    video_url = video_url.split("#", 1)[0]
    start = time.time()
    if on_progress:
        on_progress({"pct": 0, "downloaded_bytes": 0, "speed_bytes_s": 0, "eta_s": 0,
                     "elapsed_s": 0, "duration_s": None, "connecting": True})
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    site = site_of(video_url)
    workers = 6 if site == "missav" else 16   # MissAV's CDN throttles hard under many connections
    res = _resolve(video_url)
    errors = []
    tried = 0
    for src in res.sources():
        tried += 1
        try:
            if src.kind == "hls":
                _download_hls(src, out_path, height, on_progress, start, workers)
            else:
                _download_mp4(src, out_path, on_progress, start)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                return out_path, time.time() - start
            raise RuntimeError("output file is empty")
        except BlockedError:
            raise
        except Exception as e:
            logger.warning(f"jav_sites: source '{src.label}' failed for {video_url}: {e}")
            errors.append(f"{src.label}: {e}")
            try:
                os.remove(out_path)
            except OSError:
                pass
            # stale tokens: let the next attempt re-resolve fresh sources if we ran out
    if not tried:
        raise RuntimeError("No downloadable stream was found for this video.")
    raise RuntimeError("All sources failed — " + " | ".join(errors)[:600])

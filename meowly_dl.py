"""Meowly / Vidly downloader (movies + TV episodes by TMDB id).

How the site plays a title (seen in a HAR of meowly.qzz.io):
  meowly.qzz.io/watch/tv/<tmdb>?s=1&e=2  ->  iframe  embed.filmu.in/tv/<tmdb>/1/2  -> 301 ->  vidly.pro/tv/<tmdb>/1/2
  The vidly page is server-rendered: it carries   <link rel="preload" href="https://proxy.streamrip.fun/m/<token>?f=.m3u8">
  (the HLS playlist) and the title data ({"meta": {"title", "subtitle", "year", ...}}).  The playlist is a plain VOD list of
  /s/<token> segments.  Those are ordinary MPEG-TS packets that the server labels "image/jpeg".  Every request needs the
  headers  Referer: https://vidly.pro/  +  Origin: https://vidly.pro.
So: fetch the vidly page -> playlist -> download the segments (threads) -> ffmpeg remux to .mp4.

Public API (used by downloader.py / main.py):
  is_supported(url) -> bool           is_show_link(url) -> bool (TV id without season/episode)
  parse(url) -> {"kind","tmdb","season","episode"} | None
  download(url, state=None) -> local .mp4 path | None      (unique temp name: several can run at once)
  list_episodes(url) -> [{"season","episode","name","air","label","url"}]   (TMDB, for a show link)
  SUGGESTED_NAMES: local path -> nice file name
"""
import html as _html
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qs, urljoin, urlsplit

import requests

UA = ("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/139.0.0.0 Mobile Safari/537.36")
VIDLY = "https://vidly.pro"
MEOWLY_REFERER = "https://meowly.qzz.io/"
TIMEOUT = 30
WORKERS = max(1, int(os.getenv("MEOWLY_SEG_THREADS", "8")))
SEG_TRIES = 5

SUGGESTED_NAMES: dict = {}

_HOST_RE = re.compile(r"^(?:www\.)?(?:meowly\.[a-z.]+|vidly\.pro|embed\.filmu\.in)$", re.I)
_PATH_RE = re.compile(r"(?:^|/)(tv|movie)/(\d+)(?:/(\d+)/(\d+))?/?$", re.I)


def parse(url: str):
    """Meowly / Vidly link -> {"kind": "tv"|"movie", "tmdb": "281761", "season": 1|None, "episode": 2|None} | None."""
    u = (url or "").strip()
    if not u:
        return None
    if "://" not in u:
        u = "https://" + u
    try:
        sp = urlsplit(u)
    except ValueError:
        return None
    if not _HOST_RE.match(sp.netloc.split(":")[0]):
        return None
    m = _PATH_RE.search(sp.path.replace("/watch/", "/"))
    if not m:
        return None
    kind, tmdb, s, e = m.group(1).lower(), m.group(2), m.group(3), m.group(4)
    q = parse_qs(sp.query)
    s = (q.get("s") or q.get("season") or [s])[0]
    e = (q.get("e") or q.get("episode") or [e])[0]
    try:
        season = int(s) if s not in (None, "") else None
        episode = int(e) if e not in (None, "") else None
    except ValueError:
        season = episode = None
    return {"kind": kind, "tmdb": tmdb, "season": season, "episode": episode}


def is_supported(url: str) -> bool:
    return parse(url) is not None


def is_show_link(url: str) -> bool:
    p = parse(url)
    return bool(p and p["kind"] == "tv" and (p["season"] is None or p["episode"] is None))


def vidly_url(p: dict) -> str:
    if p["kind"] == "movie":
        return f"{VIDLY}/movie/{p['tmdb']}"
    return f"{VIDLY}/tv/{p['tmdb']}/{p['season']}/{p['episode']}"


def _headers(referer: str = MEOWLY_REFERER) -> dict:
    return {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9", "Referer": referer}


def _seg_headers() -> dict:
    h = _headers(VIDLY + "/")
    h["Origin"] = VIDLY
    return h


# ---------------------------------------------------------------------------------------------------------------------
# page -> playlist url + meta
# ---------------------------------------------------------------------------------------------------------------------
_M3U8_RE = re.compile(r'(https?://[^"\'\\\s<>]+?[?&]f=\.m3u8)')
_M3U8_ALT_RE = re.compile(r'(https?://[^"\'\\\s<>]+\.m3u8[^"\'\\\s<>]*)')


def _get_page(url: str) -> str:
    """The vidly page. Plain requests first; Cloudflare-style 403/503 -> curl_cffi (Chrome TLS) / cloudscraper."""
    last = ""
    try:
        r = requests.get(url, headers=_headers(), timeout=TIMEOUT)
        if r.status_code == 200 and r.text:
            return r.text
        last = f"HTTP {r.status_code}"
    except requests.RequestException as e:
        last = str(e)[:100]
    try:
        from curl_cffi import requests as _cr
        r = _cr.get(url, headers=_headers(), impersonate="chrome", timeout=TIMEOUT)
        if r.status_code == 200 and r.text:
            return r.text
        last = f"curl_cffi HTTP {r.status_code}"
    except Exception as e:
        last = f"{last}; curl_cffi {str(e)[:80]}"
    try:
        import cloudscraper
        r = cloudscraper.create_scraper().get(url, headers=_headers(), timeout=TIMEOUT)
        if r.status_code == 200 and r.text:
            return r.text
        last = f"cloudscraper HTTP {r.status_code}"
    except Exception as e:
        last = f"{last}; cloudscraper {str(e)[:80]}"
    raise RuntimeError(f"vidly page not reachable ({last})")


def _unescape_json(s: str) -> str:
    return s.replace('\\"', '"').replace("\\/", "/").replace("\\u0026", "&")


def page_info(page: str) -> dict:
    """-> {"m3u8": url | "", "title", "subtitle", "year", "poster", "runtime"} read from the server-rendered page."""
    text = _unescape_json(page)
    out = {"m3u8": "", "title": "", "subtitle": "", "year": "", "poster": "", "runtime": 0}
    m = _M3U8_RE.search(text) or _M3U8_ALT_RE.search(text)
    if m:
        out["m3u8"] = _html.unescape(m.group(1))
    meta = re.search(r'"meta":\{(.*?)"overview"', text, re.S)
    if meta:
        blk = meta.group(1)
        for key in ("title", "subtitle", "year", "poster"):
            mm = re.search(r'"%s":"([^"]*)"' % key, blk)
            if mm:
                out[key] = _html.unescape(mm.group(1))
        rt = re.search(r'"runtime":(\d+)', text)
        out["runtime"] = int(rt.group(1)) if rt else 0
    return out


# ---------------------------------------------------------------------------------------------------------------------
# HLS
# ---------------------------------------------------------------------------------------------------------------------
def _fetch_text(url: str, session) -> str:
    r = session.get(url, headers=_seg_headers(), timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def parse_playlist(text: str, base: str) -> dict:
    """Media playlist -> {"segments": [absolute urls], "key": (uri, iv|None) | None}.  A master playlist is NOT handled
    here (see resolve_media)."""
    segs, key = [], None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-KEY"):
            if "METHOD=NONE" in line:
                key = None
                continue
            u = re.search(r'URI="([^"]+)"', line)
            iv = re.search(r"IV=0x([0-9A-Fa-f]+)", line)
            if u and "AES-128" in line:
                key = (urljoin(base, u.group(1)), iv.group(1) if iv else None)
            continue
        if line.startswith("#"):
            continue
        segs.append(urljoin(base, line))
    return {"segments": segs, "key": key}


def resolve_media(playlist_url: str, session) -> tuple:
    """-> (media playlist url, its text).  If the url is a master playlist, the highest-bandwidth variant is used."""
    text = _fetch_text(playlist_url, session)
    if "#EXT-X-STREAM-INF" in text:
        best, best_bw, lines = None, -1, text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                bw = re.search(r"BANDWIDTH=(\d+)", line)
                bw = int(bw.group(1)) if bw else 0
                nxt = next((l.strip() for l in lines[i + 1:] if l.strip() and not l.startswith("#")), None)
                if nxt and bw > best_bw:
                    best, best_bw = urljoin(playlist_url, nxt), bw
        if best:
            return best, _fetch_text(best, session)
    return playlist_url, text


def _aes_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    from Crypto.Cipher import AES
    out = AES.new(key, AES.MODE_CBC, iv).decrypt(data)
    if out:
        pad = out[-1]
        if 1 <= pad <= 16 and out.endswith(bytes([pad]) * pad):
            out = out[:-pad]
    return out


def _download_segments(segs: list, key, workdir: str, state, session) -> list:
    """Fetch every segment (threads) into workdir/NNNNN.ts.  Raises on a segment that fails SEG_TRIES times."""
    key_bytes = None
    iv_hex = None
    if key:
        r = session.get(key[0], headers=_seg_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        key_bytes, iv_hex = r.content, key[1]
    total_n = len(segs)
    lock = threading.Lock()
    stat = {"bytes": 0, "n": 0}
    paths = [os.path.join(workdir, f"{i:05d}.ts") for i in range(total_n)]
    if state is not None:
        state["dl_t0"] = time.time()
        state["done"] = 0
        state["total"] = 0

    def one(i: int):
        last = None
        for attempt in range(SEG_TRIES):
            try:
                r = session.get(segs[i], headers=_seg_headers(), timeout=TIMEOUT)
                if r.status_code != 200 or not r.content:
                    raise RuntimeError(f"HTTP {r.status_code}")
                data = r.content
                if key_bytes:
                    iv = bytes.fromhex(iv_hex.zfill(32)) if iv_hex else i.to_bytes(16, "big")
                    data = _aes_decrypt(data, key_bytes, iv)
                with open(paths[i], "wb") as f:
                    f.write(data)
                with lock:
                    stat["bytes"] += len(data)
                    stat["n"] += 1
                    if state is not None:
                        state["done"] = stat["bytes"]
                        state["total"] = int(stat["bytes"] / stat["n"] * total_n)   # running estimate of the final size
                return
            except Exception as e:
                last = e
                time.sleep(min(1.5 * (attempt + 1), 6))
        raise RuntimeError(f"segment {i + 1}/{total_n} failed: {str(last)[:100]}")

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(one, i) for i in range(total_n)]
        try:
            for f in as_completed(futs):
                f.result()
        except BaseException:
            for f in futs:
                f.cancel()
            raise
    return paths


def _remux(paths: list, out: str) -> bool:
    ts = out + ".all.ts"
    try:
        with open(ts, "wb") as w:
            for p in paths:
                with open(p, "rb") as r:
                    shutil.copyfileobj(r, w, 4 * 1024 * 1024)
        cmd = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", ts, "-map", "0", "-c", "copy",
               "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", out]
        r = subprocess.run(cmd, capture_output=True, timeout=3600)
        if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) <= 0:
            print(f"[-] meowly remux failed: {r.stderr.decode(errors='ignore')[-300:]}")
            # some streams carry data/subtitle tracks mp4 can't hold: retry with video + audio only
            cmd = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", ts, "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
                   "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", out]
            r = subprocess.run(cmd, capture_output=True, timeout=3600)
            return r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0
        return True
    except Exception as e:
        print(f"[-] meowly remux error: {e}")
        return False
    finally:
        try:
            os.remove(ts)
        except OSError:
            pass


def _clean_name(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|]+', " ", s or "")).strip()


def file_name(p: dict, info: dict) -> str:
    title = _clean_name(info.get("title")) or f"Meowly {p['tmdb']}"
    if p["kind"] == "movie":
        yr = info.get("year")
        return f"{title} ({yr})" if yr else title
    base = f"{title} S{p['season']:02d}E{p['episode']:02d}"
    sub = _clean_name(info.get("subtitle", "").split("·")[-1]) if info.get("subtitle") else ""
    return f"{base} - {sub}" if sub else base


def download(url: str, state=None):
    """Meowly / Vidly link -> local .mp4 (unique temp name) or None."""
    p = parse(url)
    if not p:
        return None
    if p["kind"] == "tv" and (p["season"] is None or p["episode"] is None):
        print("[-] meowly: show link without season/episode")
        return None
    tag = uuid.uuid4().hex[:8]
    out = f"temp_mw_{tag}.mp4"
    workdir = tempfile.mkdtemp(prefix=f"temp_mw_{tag}_", dir=".")
    try:
        page = _get_page(vidly_url(p))
        info = page_info(page)
        if not info["m3u8"]:
            print("[-] meowly: no playlist link on the vidly page (title not available?)")
            return None
        session = requests.Session()
        murl, mtext = resolve_media(info["m3u8"], session)
        pl = parse_playlist(mtext, murl)
        if not pl["segments"]:
            print("[-] meowly: playlist has no segments")
            return None
        print(f"[meowly] {file_name(p, info)}: {len(pl['segments'])} segment(s)")
        paths = _download_segments(pl["segments"], pl["key"], workdir, state, session)
        if not _remux(paths, out):
            return None
        SUGGESTED_NAMES[out] = file_name(p, info) + ".mp4"
        return out
    except Exception as e:
        print(f"[-] meowly download failed: {e}")
        try:
            os.remove(out)
        except OSError:
            pass
        return None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------------------------------------------------
# TV show -> episode list (TMDB)
# ---------------------------------------------------------------------------------------------------------------------
def _tmdb_key() -> str:
    try:
        from config import TMDB_API_KEY
        return TMDB_API_KEY
    except Exception:
        return os.getenv("TMDB_API_KEY", "61e2290429798c561450eb56b26de19b")


def show_title(tmdb: str) -> str:
    try:
        r = requests.get(f"https://api.themoviedb.org/3/tv/{tmdb}", params={"api_key": _tmdb_key()}, timeout=20)
        return (r.json() or {}).get("name") or ""
    except Exception:
        return ""


def list_episodes(url: str) -> list:
    """TV show link -> every episode of every season (specials excluded), in air order:
    [{"season", "episode", "name", "air", "label": "S1 E2", "url": vidly url}]"""
    p = parse(url)
    if not p or p["kind"] != "tv":
        return []
    key = _tmdb_key()
    r = requests.get(f"https://api.themoviedb.org/3/tv/{p['tmdb']}", params={"api_key": key}, timeout=20)
    r.raise_for_status()
    out = []
    for s in (r.json() or {}).get("seasons") or []:
        n = s.get("season_number")
        if not n:
            continue                                           # 0 = specials
        rs = requests.get(f"https://api.themoviedb.org/3/tv/{p['tmdb']}/season/{n}", params={"api_key": key}, timeout=20)
        if rs.status_code != 200:
            continue
        for ep in (rs.json() or {}).get("episodes") or []:
            e = ep.get("episode_number")
            if e is None:
                continue
            out.append({"season": n, "episode": e, "name": ep.get("name") or "", "air": ep.get("air_date") or "",
                        "label": f"S{n} E{e}", "url": f"{VIDLY}/tv/{p['tmdb']}/{n}/{e}"})
    return out


def search(query: str, limit: int = 8) -> list:
    """Title search on TMDB (movies + TV) -> [{"type": "tv"|"movie", "tmdb", "title", "year", "link"}], best match first."""
    q = (query or "").strip()
    if not q:
        return []
    r = requests.get("https://api.themoviedb.org/3/search/multi", params={"api_key": _tmdb_key(), "query": q}, timeout=20)
    r.raise_for_status()
    out = []
    for it in (r.json() or {}).get("results") or []:
        t = it.get("media_type")
        if t not in ("tv", "movie"):
            continue
        title = it.get("name") or it.get("title") or ""
        year = (it.get("first_air_date") or it.get("release_date") or "")[:4]
        out.append({"type": t, "tmdb": str(it["id"]), "title": title, "year": year,
                    "link": f"{VIDLY}/{t}/{it['id']}"})
        if len(out) >= limit:
            break
    return out

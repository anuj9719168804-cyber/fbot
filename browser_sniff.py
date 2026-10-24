# coding: utf-8
"""
browser_sniff.py - read the stream a page's OWN player loads, in a real Chrome.

When a site's player page answers a plain HTTP client with an error (BeemTube's /embed/<id>/ gives HTTP 500 to scripts
while a browser iframe works), or hides the stream behind JavaScript, no regex over HTML will find it. This opens the page
in Chrome (Playwright / patchright - the same stack fbot's FPO login and the beeg fallback use), lets the site's own
JavaScript run, nudges the player to start, and records what the page really requests:

  * media requests / responses  (.mp4 .webm .m4v .mov, .m3u8, or a video/* / mpegurl content-type)
  * JSON / text answers that list sources ({"sources":[{"file":..., "label":"720p"}]}) or contain media URLs
  * the <video>/<source> elements' currentSrc in every frame

Nothing depends on a URL path the site can rename. Chrome runs headed on Xvfb when a display exists (sites treat headless
Chrome worse), else headless; a stale X socket falls back to headless. One browser at a time (RAM); a result is cached for
15 minutes (quality menu, page meta and download all ask for the same link). BROWSER_SNIFF=0 switches it off.

    sniff_media(url, referer=None, timeout=45) ->
        {"title", "ua", "cookie", "referer", "streams": [{"url", "kind": "mp4"|"hls", "height", "label", "size"}]}
    raises RuntimeError with the reason.
"""

import json
import os
import re
import shutil
import threading
import time
from urllib.parse import urljoin, urlsplit

_CACHE = {}
_CACHE_LOCK = threading.Lock()
_RUN_LOCK = threading.Lock()          # one Chrome at a time
_TTL = 900

_MP4_EXT = re.compile(r"\.(?:mp4|webm|m4v|mov)$", re.I)
_M3U8_EXT = re.compile(r"\.m3u8$", re.I)
_URL_IN_TEXT = re.compile(
    r"""["'(]((?:https?:)?//[^"'\s\\()]+?\.(?:mp4|m3u8|webm|m4v)(?:\?[^"'\s\\()]*)?)["')]""", re.I)
_PLAY_SELECTORS = (
    ".vjs-big-play-button", ".plyr__control--overlaid", ".jw-icon-display", ".jw-display-icon-container",
    "button[aria-label*='lay' i]", "[class*='big-play' i]", "[class*='play-button' i]", "[class*='BigPlay' i]",
    "[class*='playBtn' i]", ".video-js", "video",
)
_GATE_SELECTORS = (
    "button:has-text(\"I am 18\")", "button:has-text(\"I'm 18\")", "button:has-text(\"Enter\")",
    "button:has-text(\"I Agree\")", "button:has-text(\"Agree\")", "a:has-text(\"Enter\")", "button:has-text(\"Continue\")",
)
_SKIP_HOST_PARTS = ("doubleclick", "googlesyndication", "google-analytics", "googletagmanager", "adnxs", "exoclick",
                    "trafficjunky", "juicyads", "popads", "histats", "yandex", "facebook.net")


def enabled() -> bool:
    return os.environ.get("BROWSER_SNIFF", "1").strip().lower() not in ("0", "false", "no", "off")


def _find_chrome():
    p = os.environ.get("SNIFF_CHROME_PATH", "") or os.environ.get("BEEG_CHROME_PATH", "")
    if p and os.path.exists(p):
        return p
    try:
        import browser_bootstrap as bb          # fbot: real Google Chrome, never the Ubuntu snap stub
        c = bb.find_chrome()
        if c:
            return c
    except Exception:
        pass
    for n in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        w = shutil.which(n)
        if w:
            return w
    return None


def _display() -> str:
    d = os.environ.get("DISPLAY")
    if d:
        return d
    if os.path.exists("/tmp/.X11-unix/X99"):
        return ":99"
    try:                                          # fbot's FlareSolverr bootstrap can start Xvfb :99 for us
        import flaresolver_bootstrap as fb
        fb._ensure_xvfb()
        fb._start_xvfb()
        for _ in range(20):
            if os.path.exists("/tmp/.X11-unix/X99"):
                return ":99"
            time.sleep(0.5)
    except Exception:
        pass
    return ""


def _playwright():
    if os.environ.get("SNIFF_ENGINE", "auto") != "playwright":
        try:
            from patchright.sync_api import sync_playwright
            return sync_playwright
        except Exception:
            pass
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except Exception:
        raise RuntimeError("Playwright is not installed (pip install playwright) - the browser fallback needs it")


def _strip(u: str) -> str:
    return u.split("?", 1)[0].split("#", 1)[0]


def _kind_of(url: str, ctype: str = "") -> str:
    p = _strip(url)
    ct = (ctype or "").lower()
    if _M3U8_EXT.search(p) or "mpegurl" in ct:
        return "hls"
    if _MP4_EXT.search(p) or ct.startswith("video/"):
        return "mp4"
    return ""


def _height_of(*texts) -> int:
    for t in texts:
        if not t:
            continue
        m = re.search(r"(\d{3,4})\s*p\b", str(t), re.I) or re.search(r"[/_\-.](\d{3,4})(?:p)?[/_\-.]", str(t))
        if m and 140 <= int(m.group(1)) <= 4320:
            return int(m.group(1))
    return 0


def _walk_sources(obj, out):
    """Collect {"file"|"src"|"url": <media url>, "label"|"quality"|"res"|"height": ...} dicts anywhere in a JSON document."""
    if isinstance(obj, dict):
        f = obj.get("file") or obj.get("src") or obj.get("url") or obj.get("link")
        if isinstance(f, str) and _kind_of(f):
            label = obj.get("label") or obj.get("quality") or obj.get("res") or obj.get("height") or obj.get("name") or ""
            out.append((f, str(label)))
        for v in obj.values():
            _walk_sources(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_sources(v, out)


def sniff_media(url: str, referer: str = None, timeout: int = 45, min_mp4_bytes: int = 1500000,
                cookies: list = None, user_agent: str = None) -> dict:
    """cookies / user_agent: a Cloudflare clearance from cf_bypass (cookies = Playwright cookie dicts)."""
    if not enabled():
        raise RuntimeError("browser fallback is disabled (BROWSER_SNIFF=0)")
    key = url.split("#", 1)[0]
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < _TTL:
            return hit[1]
    with _RUN_LOCK:
        with _CACHE_LOCK:                          # another thread may have filled it while we waited for the lock
            hit = _CACHE.get(key)
            if hit and time.time() - hit[0] < _TTL:
                return hit[1]
        data = _sniff_once(url, referer, timeout, min_mp4_bytes, cookies=cookies, user_agent=user_agent)
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), data)
    return data


def _sniff_once(url, referer, timeout, min_mp4_bytes, cookies=None, user_agent=None):
    sync_playwright = _playwright()
    display = _display()
    env = dict(os.environ)
    if display:
        env["DISPLAY"] = display

    observed = {}            # path-without-query -> {"url","kind","size","height","label"}   (requests / responses)
    json_resps = []          # candidate JSON/text responses, parsed from the main thread

    def note(u, kind, size=None, label="", height=0):
        if not u.startswith("http") or any(s in u.lower() for s in _SKIP_HOST_PARTS):
            return
        p = _strip(u)
        cur = observed.get(p)
        if cur is None:
            observed[p] = {"url": u, "kind": kind, "size": size, "height": height or _height_of(label, u), "label": label}
        else:
            if size and not cur.get("size"):
                cur["size"] = size
            if height and not cur.get("height"):
                cur["height"] = height

    with sync_playwright() as pw:
        kw = {"headless": not display, "env": env, "ignore_default_args": ["--enable-automation"],
              "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled",
                       "--autoplay-policy=no-user-gesture-required", "--mute-audio", "--window-size=1280,800", "--lang=en-US"]}
        chrome = _find_chrome()
        if chrome:
            kw["executable_path"] = chrome
        try:
            browser = pw.chromium.launch(**kw)
        except Exception:
            if kw["headless"]:
                raise
            # a stale X socket (/tmp/.X11-unix/X99 left behind by a crashed Xvfb) looks like a display but isn't one
            kw["headless"] = True
            kw["env"] = {k: v for k, v in env.items() if k != "DISPLAY"}
            browser = pw.chromium.launch(**kw)
        try:
            ctx_kw = {"locale": "en-US", "viewport": {"width": 1280, "height": 720}}
            if user_agent:
                ctx_kw["user_agent"] = user_agent
            ctx = browser.new_context(**ctx_kw)
            if cookies:
                try:
                    ctx.add_cookies(cookies)
                except Exception as _ce:
                    print(f"[browser_sniff] cookies not added: {str(_ce)[:120]}")
            page = ctx.new_page()
            page.set_default_timeout(15000)

            def on_request(req):
                try:
                    k = _kind_of(req.url)
                    if k:
                        note(req.url, k)
                except Exception:
                    pass

            def on_response(resp):
                try:
                    ct = (resp.headers.get("content-type") or "").lower()
                    k = _kind_of(resp.url, ct)
                    if k and resp.status in (200, 206):
                        size = None
                        cr = resp.headers.get("content-range") or ""
                        m = re.search(r"/(\d+)$", cr)
                        if m:
                            size = int(m.group(1))
                        elif resp.headers.get("content-length"):
                            size = int(resp.headers.get("content-length"))
                        note(resp.url, k, size=size)
                    elif resp.status == 200 and ("json" in ct or "javascript" in ct or "text/" in ct) and len(json_resps) < 80:
                        json_resps.append(resp)
                except Exception:
                    pass

            page.on("request", on_request)
            page.on("response", on_response)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000, **({"referer": referer} if referer else {}))
            except Exception as e:
                if not observed and not json_resps:
                    raise RuntimeError("could not open the page: %s" % str(e)[:150])

            listed = []                              # (url, label) from JSON "sources" lists / urls inside text bodies
            t0 = time.time()
            last_poke = 0.0
            seen_n = 0
            while time.time() - t0 < timeout:
                while seen_n < len(json_resps):
                    r = json_resps[seen_n]
                    seen_n += 1
                    try:
                        body = r.text()
                    except Exception:
                        continue
                    if not body or len(body) > 3000000:
                        continue
                    body = body.replace("\\/", "/")
                    if body.lstrip()[:1] in "{[":
                        try:
                            _walk_sources(json.loads(body), listed)
                        except Exception:
                            pass
                    for m in _URL_IN_TEXT.finditer(body):
                        cand = m.group(1)
                        cand = ("https:" + cand) if cand.startswith("//") else cand
                        listed.append((cand, ""))
                big = [o for o in observed.values() if o["kind"] == "hls" or (o["size"] or 0) >= min_mp4_bytes]
                if (big or listed) and time.time() - t0 > (4 if big else 8):
                    break
                if time.time() - last_poke > 4:      # age gate / consent / start playback, in the page and every frame
                    last_poke = time.time()
                    frames = list(page.frames)
                    for fr in frames:
                        for sel in _GATE_SELECTORS:
                            try:
                                el = fr.locator(sel).first
                                if el.is_visible(timeout=250):
                                    el.click(timeout=700)
                                    break
                            except Exception:
                                pass
                        try:
                            fr.evaluate("document.querySelectorAll('video').forEach(function(v){v.muted=true;"
                                        "var p=v.play();if(p&&p.catch)p.catch(function(){});})")
                        except Exception:
                            pass
                        for sel in _PLAY_SELECTORS:
                            try:
                                el = fr.locator(sel).first
                                if el.is_visible(timeout=250):
                                    el.click(timeout=700)
                                    break
                            except Exception:
                                pass
                page.wait_for_timeout(400)

            # <video>/<source> currentSrc in every frame (direct-file players)
            for fr in list(page.frames):
                try:
                    srcs = fr.evaluate("Array.from(document.querySelectorAll('video,source')).map(function(e){"
                                       "return [e.currentSrc||e.src||'', e.getAttribute('label')||e.getAttribute('title')||'',"
                                       "e.videoHeight||0]})")
                except Exception:
                    srcs = []
                for s, label, vh in srcs or []:
                    k = _kind_of(s)
                    if k and s.startswith("http"):
                        note(s, k, label=str(label), height=int(vh or 0))
            try:
                title = page.evaluate("(document.querySelector('meta[property=\"og:title\"]')||{}).content||document.title")
            except Exception:
                title = None
            try:
                ua = page.evaluate("navigator.userAgent")
            except Exception:
                ua = None
            try:
                cookies = ctx.cookies()
            except Exception:
                cookies = []
        finally:
            try:
                browser.close()
            except Exception:
                pass

    streams, seen = [], set()

    def add(u, kind, size, height, label):
        p = _strip(u)
        if p in seen:
            return
        seen.add(p)
        streams.append({"url": u, "kind": kind, "height": height or 0, "label": label or None, "size": size})

    # 1) streams the page itself listed (labelled sources) - exact tokens, qualities
    for u, label in listed:
        u = urljoin(url, u)
        k = _kind_of(u)
        if k and u.startswith("http") and not any(s in u.lower() for s in _SKIP_HOST_PARTS):
            obs = observed.get(_strip(u)) or {}
            add(u, k, obs.get("size"), _height_of(label, u) or obs.get("height") or 0, label)
    # 2) what the player really requested; small mp4s (previews, ads) only if nothing else exists
    for o in sorted(observed.values(), key=lambda o: -(o["size"] or 0)):
        if o["kind"] == "mp4" and o["size"] is not None and o["size"] < min_mp4_bytes:
            continue
        add(o["url"], o["kind"], o["size"], o["height"], o["label"])
    if not streams:
        for o in observed.values():
            add(o["url"], o["kind"], o["size"], o["height"], o["label"])
    if not streams:
        raise RuntimeError("the page's player requested no video stream (video removed, blocked, or it needs a login)")
    streams.sort(key=lambda s: (-(s["height"] or 0), s["kind"] != "mp4"))

    host = urlsplit(streams[0]["url"]).hostname or ""
    cookie = "; ".join("%s=%s" % (c["name"], c["value"]) for c in cookies
                       if c.get("domain") and host.endswith(c["domain"].lstrip(".")))
    return {"title": (title or "").strip() or None, "ua": ua, "cookie": cookie or None,
            "referer": url, "streams": streams}

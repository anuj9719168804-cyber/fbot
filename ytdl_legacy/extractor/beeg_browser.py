# coding: utf-8
"""
Browser fallback for beeg.com (used by BeegIE when beeg's own API answers 400/500).

Why: beeg switched off the JSON endpoint extractors used (store.externulls.com/facts/file/<id>: HTTP 500 since 2025, HTTP 400
now - yt-dlp issues #14037 / #17680), so any extractor that hard-codes an endpoint breaks again whenever beeg moves it. The
page's own JavaScript always knows the current endpoint, so this opens the page in a real Chrome and records what the PAGE
requests: the JSON that carries "hls_resources" and the .m3u8 playlists the player loads. Nothing here depends on a URL path
that beeg can rename.

Needs Playwright (or patchright) + Chrome - the same stack fbot's FPO login uses (browser_bootstrap.py installs it). Headed on
Xvfb when a display exists (sites treat headless Chrome worse), else headless. One browser at a time (RAM), results cached
for 15 minutes (the quality menu, page meta and the download all ask for the same link). BEEG_BROWSER=0 switches it off.
"""

from __future__ import unicode_literals

import json
import os
import re
import shutil
import threading
import time

_CACHE = {}
_CACHE_LOCK = threading.Lock()
_RUN_LOCK = threading.Lock()          # one Chrome at a time
_TTL = 900

CDN_URL = 'https://video.beeg.com/'

_M3U8_RE = re.compile(r'\.m3u8(?:[?#]|$)', re.I)
_M3U8_IN_TEXT = re.compile(r'["\']((?:https?:)?//[^"\'\s\\]+?\.m3u8[^"\'\s\\]*|[^"\'\s\\]+?\.m3u8[^"\'\s\\]*)["\']', re.I)
_PLAY_SELECTORS = (
    '.vjs-big-play-button', '.plyr__control--overlaid', 'button[aria-label*="lay" i]', '[class*="play" i][class*="btn" i]',
    '[class*="BigPlay" i]', '[class*="play-button" i]',
)
_GATE_SELECTORS = (
    'button:has-text("I am 18")', 'button:has-text("I\'m 18")', 'button:has-text("Enter")', 'button:has-text("I Agree")',
    'button:has-text("Agree")', 'a:has-text("Enter")',
)


def enabled():
    return os.environ.get('BEEG_BROWSER', '1').strip().lower() not in ('0', 'false', 'no', 'off')


def _find_chrome():
    p = os.environ.get('BEEG_CHROME_PATH', '')
    if p and os.path.exists(p):
        return p
    try:
        import browser_bootstrap as bb          # fbot: real Google Chrome, never the Ubuntu snap stub
        c = bb.find_chrome()
        if c:
            return c
    except Exception:
        pass
    for n in ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser'):
        w = shutil.which(n)
        if w:
            return w
    return None


def _display():
    d = os.environ.get('DISPLAY')
    if d:
        return d
    if os.path.exists('/tmp/.X11-unix/X99'):
        return ':99'
    try:                                          # fbot's FlareSolverr bootstrap can start Xvfb :99 for us
        import flaresolver_bootstrap as fb
        fb._ensure_xvfb()
        fb._start_xvfb()
        for _ in range(20):
            if os.path.exists('/tmp/.X11-unix/X99'):
                return ':99'
            time.sleep(0.5)
    except Exception:
        pass
    return ''


def _playwright():
    if os.environ.get('BEEG_ENGINE', 'auto') != 'playwright':
        try:
            from patchright.sync_api import sync_playwright
            return sync_playwright
        except Exception:
            pass
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except Exception:
        raise RuntimeError('Playwright is not installed (pip install playwright) - the beeg browser fallback needs it')


def _walk(obj, found):
    """Collect every {"fl_cdn_720": "<uri>", ...} style dict ('hls_resources') anywhere in a JSON document."""
    if isinstance(obj, dict):
        hr = obj.get('hls_resources')
        if isinstance(hr, dict) and hr:
            found.append(hr)
        for v in obj.values():
            _walk(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, found)


_STD_HEIGHTS = (144, 240, 360, 480, 540, 576, 720, 1080, 1440, 2160, 4320)


def _std(n):
    """A number only counts as a video height when it is a real resolution (ids/hashes in urls are not)."""
    try:
        n = int(n)
    except Exception:
        return None
    return n if n in _STD_HEIGHTS else None


def _height(key):
    m = re.search(r'(\d{3,4})', key or '')
    return _std(m.group(1)) if m else None


def _height_from_url(path):
    """720 from '.../fl_cdn_720/...', '.../720p.m3u8' or '..._720.m3u8'; None when the url has no real height."""
    for rx in (r'fl_cdn_(\d{3,4})', r'(?<!\d)(\d{3,4})p(?![a-z0-9])', r'[_/.-](\d{3,4})(?=\.m3u8)'):
        m = re.search(rx, path or '', re.I)
        if m and _std(m.group(1)):
            return _std(m.group(1))
    return None


def _strip(u):
    return u.split('?')[0].split('#')[0]


def sniff(url, video_id, timeout=50, cdn_url=CDN_URL):
    """Open `url` in Chrome and return what the page loads:
        {'title', 'duration', 'thumbnail', 'streams': [{'url', 'height'|None, 'id'}], 'ua', 'cookie', 'referer'}
    'streams' are absolute .m3u8 URLs (observed requests first, then the page's own hls_resources). Raises RuntimeError."""
    if not enabled():
        raise RuntimeError('beeg browser fallback is disabled (BEEG_BROWSER=0)')
    with _CACHE_LOCK:
        hit = _CACHE.get(video_id)
        if hit and time.time() - hit[0] < _TTL:
            return hit[1]
    with _RUN_LOCK:
        with _CACHE_LOCK:                          # another thread may have filled it while we waited for the lock
            hit = _CACHE.get(video_id)
            if hit and time.time() - hit[0] < _TTL:
                return hit[1]
        data = _sniff_once(url, timeout, cdn_url)
    with _CACHE_LOCK:
        _CACHE[video_id] = (time.time(), data)
    return data


def _sniff_once(url, timeout, cdn_url):
    sync_playwright = _playwright()
    display = _display()
    env = dict(os.environ)
    if display:
        env['DISPLAY'] = display
    observed = []            # .m3u8 request URLs the player made, in order
    json_resps = []          # candidate JSON responses (parsed after the loop, from the main thread)
    with sync_playwright() as pw:
        kw = {'headless': not display, 'env': env, 'ignore_default_args': ['--enable-automation'],
              'args': ['--no-sandbox', '--disable-dev-shm-usage', '--disable-blink-features=AutomationControlled',
                       '--autoplay-policy=no-user-gesture-required', '--mute-audio', '--window-size=1280,800', '--lang=en-US']}
        chrome = _find_chrome()
        if chrome:
            kw['executable_path'] = chrome
        try:
            browser = pw.chromium.launch(**kw)
        except Exception:
            if kw['headless']:
                raise
            # a stale X socket (/tmp/.X11-unix/X99 left behind by a crashed Xvfb) looks like a display but isn't one:
            # fall back to headless instead of failing the whole extraction
            kw['headless'] = True
            kw['env'] = {k: v for k, v in env.items() if k != 'DISPLAY'}
            browser = pw.chromium.launch(**kw)
        try:
            ctx = browser.new_context(locale='en-US', viewport={'width': 1280, 'height': 720})
            page = ctx.new_page()
            page.set_default_timeout(15000)

            def on_request(req):
                u = req.url
                if _M3U8_RE.search(u) and u not in observed:
                    observed.append(u)

            def on_response(resp):
                try:
                    ct = (resp.headers.get('content-type') or '').lower()
                    if resp.status == 200 and ('json' in ct or 'javascript' in ct or 'text/plain' in ct):
                        if len(json_resps) < 60:
                            json_resps.append(resp)
                except Exception:
                    pass

            page.on('request', on_request)
            page.on('response', on_response)
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=45000)
            except Exception as e:
                if not observed and not json_resps:
                    raise RuntimeError('could not open the beeg page: %s' % str(e)[:150])

            hls_dicts = []
            t0 = time.time()
            last_poke = 0.0
            seen_n = 0
            while time.time() - t0 < timeout:
                # parse any new JSON responses for "hls_resources" / .m3u8 strings
                while seen_n < len(json_resps):
                    r = json_resps[seen_n]
                    seen_n += 1
                    try:
                        body = r.text()
                    except Exception:
                        continue
                    if not body or len(body) > 3000000:
                        continue
                    if 'hls_resources' in body:
                        try:
                            _walk(json.loads(body), hls_dicts)
                        except Exception:
                            pass
                    for m in _M3U8_IN_TEXT.finditer(body):
                        cand = m.group(1).replace('\\/', '/')
                        if cand.startswith('//'):
                            cand = 'https:' + cand
                        if cand.startswith('http') and cand not in observed:
                            observed.append(cand)
                # done once we know the streams (resources JSON or playlist requests); give the player a few extra
                # seconds so every quality's playlist shows up
                if (hls_dicts or observed) and time.time() - t0 > (3 if hls_dicts else 8):
                    break
                if time.time() - last_poke > 4:      # age gate / consent / start playback
                    last_poke = time.time()
                    for sel in _GATE_SELECTORS:
                        try:
                            el = page.locator(sel).first
                            if el.is_visible(timeout=300):
                                el.click(timeout=800)
                                break
                        except Exception:
                            pass
                    try:
                        page.evaluate("document.querySelectorAll('video').forEach(function(v){v.muted=true;"
                                      "var p=v.play();if(p&&p.catch)p.catch(function(){});})")
                    except Exception:
                        pass
                    for sel in _PLAY_SELECTORS:
                        try:
                            el = page.locator(sel).first
                            if el.is_visible(timeout=300):
                                el.click(timeout=800)
                                break
                        except Exception:
                            pass
                page.wait_for_timeout(400)

            title = duration = None
            try:
                title = page.evaluate("(document.querySelector('meta[property=\"og:title\"]')||{}).content||document.title")
            except Exception:
                pass
            try:
                duration = page.evaluate("(document.querySelector('video')||{}).duration")
            except Exception:
                pass
            thumbnail = None
            try:  # the page's own poster: og:image / twitter:image / <video poster>
                thumbnail = page.evaluate(
                    "(function(){var q=function(s,a){var e=document.querySelector(s);return e&&e.getAttribute(a)};"
                    "return q('meta[property=\"og:image\"]','content')||q('meta[name=\"twitter:image\"]','content')"
                    "||q('link[rel=\"image_src\"]','href')||q('video','poster')||null})()")
                if thumbnail:
                    thumbnail = page.evaluate("(u)=>new URL(u, location.href).href", thumbnail)
            except Exception:
                pass
            try:
                ua = page.evaluate('navigator.userAgent')
            except Exception:
                ua = None
            cookies = []
            try:
                cookies = ctx.cookies()
            except Exception:
                pass
        finally:
            try:
                browser.close()
            except Exception:
                pass

    streams, seen_paths = [], set()
    # 1) what the page's player really requested (absolute, exact tokens)
    for u in observed:
        p = _strip(u)
        if p in seen_paths:
            continue
        seen_paths.add(p)
        streams.append({'url': u, 'height': _height_from_url(p), 'id': None})
    # 2) qualities the page's JSON lists (relative uris live on the CDN host); an observed URL for the same path wins
    for hr in hls_dicts:
        for key, uri in hr.items():
            if not uri or not isinstance(uri, str):
                continue
            if re.match(r'https?://', uri):
                full = uri
            elif uri.startswith('//'):
                full = 'https:' + uri
            else:
                full = cdn_url + uri.lstrip('/')
            p = _strip(full)
            if p in seen_paths:
                for s in streams:
                    if _strip(s['url']) == p:
                        s['height'] = s['height'] or _height(key)
                        s['id'] = key
                continue
            seen_paths.add(p)
            streams.append({'url': full, 'height': _height(key), 'id': key})
    if not streams:
        raise RuntimeError('no .m3u8 stream was requested by the beeg page (video removed, blocked, or the player needs a login)')

    host = re.sub(r'^https?://([^/]+)/.*$', r'\1', streams[0]['url'])
    cookie = '; '.join('%s=%s' % (c['name'], c['value']) for c in cookies
                       if c.get('domain') and host.endswith(c['domain'].lstrip('.')))
    try:
        duration = int(duration) if duration and duration == duration and duration != float('inf') else None
    except Exception:
        duration = None
    return {'title': (title or '').strip() or None, 'duration': duration, 'streams': streams, 'ua': ua,
            'cookie': cookie or None, 'referer': 'https://beeg.com/', 'thumbnail': thumbnail or None}

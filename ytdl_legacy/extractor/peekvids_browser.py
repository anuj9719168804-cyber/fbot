# coding: utf-8
"""
Browser fallback for peekvids.com / playvids.com (used by PeekVidsIE when the plain page fetch has no <video data-id>).

Why: PeekVidsIE reads the short video id out of the page HTML (<video data-id=...>) and then asks /v-alt/<id> for the source
list. When the site changes that markup, or serves the bare script-fetched page a stripped version (bot/rate-limit/consent
page), the id cannot be found and the whole extraction fails ("Unable to extract short video ID"). A real Chrome sees the
page the way a visitor does, so this opens it and records what the PAGE itself loads: the /v-alt/ JSON response, the <video>
element's sources and any .mp4/.m3u8 request the player makes.

Same stack and helpers as beeg_browser.py (Playwright/patchright + Chrome, headed on Xvfb when a display exists, one Chrome
at a time - the lock is shared with beeg). PEEKVIDS_BROWSER=0 switches it off. Results are cached for 15 minutes.
"""
from __future__ import unicode_literals

import json
import re
import threading
import time
from urllib.parse import urlparse

from . import beeg_browser as _bb        # shared helpers: Chrome / display / playwright discovery, the one-Chrome lock

_CACHE = {}
_CACHE_LOCK = threading.Lock()
_TTL = 900

_MEDIA_RE = re.compile(r'\.(?:mp4|m3u8|webm)(?:[?#]|$)', re.I)
_SKIP_MEDIA = re.compile(r'(?:preview|thumb|trailer|/ads?/|sprite)', re.I)
_PLAY_SELECTORS = _bb._PLAY_SELECTORS
_GATE_SELECTORS = _bb._GATE_SELECTORS

_VIDEO_JS = """() => {
  const out = {srcs: {}, media: []};
  document.querySelectorAll('video').forEach(v => {
    for (const a of v.attributes) { if (/^data-src\\d{3,}$/.test(a.name) && a.value) out.srcs[a.name] = a.value; }
    if (v.currentSrc) out.media.push(v.currentSrc);
    if (v.getAttribute('src')) out.media.push(v.getAttribute('src'));
    v.querySelectorAll('source[src]').forEach(s => out.media.push(s.getAttribute('src')));
  });
  return out;
}"""


def enabled():
    import os
    return os.environ.get('PEEKVIDS_BROWSER', '1').strip().lower() not in ('0', 'false', 'no', 'off')


def sniff(url, cache_key, timeout=45):
    """-> {'srcs': {'data-src720': url, ...}, 'media': [urls], 'title', 'thumbnail', 'duration', 'ua', 'cookie', 'referer'}.
    Raises RuntimeError when the browser could not find anything playable."""
    if not enabled():
        raise RuntimeError('peekvids browser fallback is disabled (PEEKVIDS_BROWSER=0)')
    with _CACHE_LOCK:
        hit = _CACHE.get(cache_key)
        if hit and time.time() - hit[0] < _TTL:
            return hit[1]
    with _bb._RUN_LOCK:                           # one Chrome at a time (shared with beeg)
        with _CACHE_LOCK:
            hit = _CACHE.get(cache_key)
            if hit and time.time() - hit[0] < _TTL:
                return hit[1]
        data = _sniff_once(url, timeout)
    with _CACHE_LOCK:
        _CACHE[cache_key] = (time.time(), data)
    return data


def _sniff_once(url, timeout):
    import os
    sync_playwright = _bb._playwright()
    display = _bb._display()
    env = dict(os.environ)
    if display:
        env['DISPLAY'] = display
    media, valt_resps = [], []
    with sync_playwright() as pw:
        kw = {'headless': not display, 'env': env, 'ignore_default_args': ['--enable-automation'],
              'args': ['--no-sandbox', '--disable-dev-shm-usage', '--disable-blink-features=AutomationControlled',
                       '--autoplay-policy=no-user-gesture-required', '--mute-audio', '--window-size=1280,800', '--lang=en-US']}
        chrome = _bb._find_chrome()
        if chrome:
            kw['executable_path'] = chrome
        try:
            browser = pw.chromium.launch(**kw)
        except Exception:
            if kw['headless']:
                raise
            kw['headless'] = True
            kw['env'] = {k: v for k, v in env.items() if k != 'DISPLAY'}
            browser = pw.chromium.launch(**kw)
        try:
            ctx = browser.new_context(locale='en-US', viewport={'width': 1280, 'height': 720})
            page = ctx.new_page()
            page.set_default_timeout(15000)

            def on_request(req):
                u = req.url
                if _MEDIA_RE.search(u) and not _SKIP_MEDIA.search(u) and u not in media:
                    media.append(u)

            def on_response(resp):
                try:
                    if '/v-alt/' in resp.url and resp.status == 200 and len(valt_resps) < 5:
                        valt_resps.append(resp)
                except Exception:
                    pass

            page.on('request', on_request)
            page.on('response', on_response)
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=45000)
            except Exception as e:
                if not media and not valt_resps:
                    raise RuntimeError('could not open the page: %s' % str(e)[:150])

            srcs = {}
            t0, last_poke = time.time(), 0.0
            while time.time() - t0 < timeout:
                for r in list(valt_resps):
                    try:
                        j = json.loads(r.text())
                        for k, v in (j.items() if isinstance(j, dict) else []):
                            if v and isinstance(v, str) and re.match(r'^data-src\d{3,}$', k):
                                srcs[k] = v
                    except Exception:
                        pass
                try:
                    dom = page.evaluate(_VIDEO_JS) or {}
                    for k, v in (dom.get('srcs') or {}).items():
                        srcs.setdefault(k, v)
                    for m in (dom.get('media') or []):
                        if m and m.startswith('http') and not m.startswith('blob:') and m not in media:
                            media.append(m)
                except Exception:
                    pass
                if srcs or (media and time.time() - t0 > 6):
                    break
                if time.time() - last_poke > 4:          # age gate / consent / start playback
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

            title = thumb = duration = ua = None
            try:
                title = page.evaluate("(document.querySelector('meta[property=\"og:title\"]')||{}).content||document.title")
                thumb = page.evaluate("(function(){var e=document.querySelector('meta[property=\"og:image\"]');"
                                      "return e?new URL(e.content,location.href).href:null})()")
                duration = page.evaluate("(document.querySelector('video')||{}).duration")
                ua = page.evaluate('navigator.userAgent')
            except Exception:
                pass
            try:
                cookies = ctx.cookies()
            except Exception:
                cookies = []
        finally:
            try:
                browser.close()
            except Exception:
                pass

    if not srcs and not media:
        raise RuntimeError('the page never produced a video source (removed video, bot/consent wall, or a login is needed)')
    host = urlparse(url).netloc
    first = (list(srcs.values()) + media)[0]
    fhost = urlparse(first).netloc
    cookie = '; '.join('%s=%s' % (c['name'], c['value']) for c in cookies
                       if c.get('domain') and (fhost.endswith(c['domain'].lstrip('.')) or host.endswith(c['domain'].lstrip('.'))))
    try:
        duration = int(duration) if duration and duration == duration and duration != float('inf') else None
    except Exception:
        duration = None
    return {'srcs': srcs, 'media': media, 'title': (title or '').strip() or None, 'thumbnail': thumb or None,
            'duration': duration, 'ua': ua, 'cookie': cookie or None, 'referer': 'https://%s/' % host}

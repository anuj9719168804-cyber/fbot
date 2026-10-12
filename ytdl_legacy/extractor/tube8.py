# coding: utf-8
from __future__ import unicode_literals

import re

from .keezmovies import KeezMoviesIE
from ..compat import compat_urlparse
from ..utils import (
    determine_ext,
    ExtractorError,
    int_or_none,
    parse_duration,
    str_to_int,
    strip_or_none,
    try_get,
    unescapeHTML,
    urljoin,
)


class Tube8IE(KeezMoviesIE):
    # Old pages:  /<category>/[<sub>/]<slug>/<id>/
    # New pages:  /porn-video/<id>/      (also tube8.fr / .es / ... and optional trailing slug)
    _VALID_URL = (
        r'https?://(?:[^/?#]+\.)?tube8\.[a-z]{2,}/(?:[^/?#]+/)*?'
        r'(?:(?P<display_id>[^/?#]+)/)?(?P<id>\d+)(?:[/?#]|$)')
    _TESTS = [{
        'url': 'http://www.tube8.com/teen/kasia-music-video/229795/',
        'only_matching': True,
    }, {
        'url': 'https://www.tube8.com/porn-video/227617961/',
        'only_matching': True,
    }, {
        'url': 'https://www.tube8.fr/porn-video/257916991/',
        'only_matching': True,
    }, {
        'url': 'http://www.tube8.com/shemale/teen/blonde-cd-gets-kidnapped-by-two-blacks-and-punished-for-being-a-slutty-girl/19569151/',
        'only_matching': True,
    }]

    _NOT_A_SLUG = ('porn-video', 'video', 'videos', 'embed', 'watch')
    _MEDIA_EXTS = ('mp4', 'm4v', 'webm', 'mov')

    @staticmethod
    def _extract_urls(webpage):
        return re.findall(
            r'<iframe[^>]+\bsrc=["\']((?:https?:)?//(?:www\.)?tube8\.[a-z]{2,}/embed/(?:[^/]+/)*\d+)',
            webpage)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _clean_url(u):
        """URLs live inside JSON / JS strings: undo \\/ , \\u0026 , &amp; ..."""
        u = (u or '').strip().strip('"\'')
        u = u.replace('\\/', '/').replace('\\u0026', '&').replace('\\u003d', '=').replace('\\u002F', '/')
        u = unescapeHTML(u)
        return u.replace('\\', '')

    @staticmethod
    def _balanced(text, start, open_ch='[', close_ch=']'):
        """text[start] == open_ch -> the balanced slice (string-aware), or None."""
        depth, i, n, in_str, quote = 0, start, len(text), False, ''
        while i < n:
            c = text[i]
            if in_str:
                if c == '\\':
                    i += 1
                elif c == quote:
                    in_str = False
            elif c in '"\'':
                in_str, quote = True, c
            elif c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
            i += 1
        return None

    def _json_values_after(self, webpage, key, video_id):
        """Every JSON array that follows `"<key>":` in the page (works on raw and HTML-escaped text)."""
        out = []
        for src in (webpage, webpage.replace('&quot;', '"')):
            for m in re.finditer(r'["\']%s["\']\s*:\s*\[' % re.escape(key), src):
                raw = self._balanced(src, m.end() - 1)
                if not raw:
                    continue
                data = self._parse_json(raw, video_id, fatal=False)
                if isinstance(data, list):
                    out.append(data)
            if out:
                break
        return out

    def _add_media(self, formats, seen, media_url, base_url, video_id, hint=None, quality=None, headers=None):
        media_url = self._clean_url(media_url)
        if not media_url or media_url.startswith(('data:', 'blob:', 'javascript:')):
            return
        if media_url.startswith('//'):
            media_url = 'https:' + media_url
        elif media_url.startswith('/'):
            media_url = urljoin(base_url, media_url)
        if not media_url.startswith('http') or media_url in seen:
            return
        ext = determine_ext(media_url, default_ext='')
        hint = (hint or '').lower()
        if ext in ('svg', 'jpg', 'jpeg', 'png', 'gif', 'webp', 'ico', 'js', 'css', 'json', 'html', 'ts', 'vtt'):
            return
        seen.add(media_url)
        height = int_or_none(quality) if quality is not None and not isinstance(quality, (list, tuple)) else \
            int_or_none(try_get(quality, lambda x: x[0]))
        if ext == 'm3u8' or hint in ('hls', 'm3u8'):
            formats.extend(self._extract_m3u8_formats(
                media_url, video_id, 'mp4', entry_protocol='m3u8_native',
                m3u8_id='hls', fatal=False, headers=headers or {}))
        elif ext in self._MEDIA_EXTS or hint == 'mp4':
            if not height:
                height = int_or_none(self._search_regex(
                    r'(?<![\d])(\d{3,4})[pP](?![a-zA-Z])', media_url, 'height', default=None))
            tbr = int_or_none(self._search_regex(
                r'[/_](\d{3,5})[kK][/_.]', media_url, 'tbr', default=None))
            formats.append({
                'url': media_url,
                'format_id': 'mp4-%sp' % height if height else 'mp4',
                'height': height,
                'tbr': tbr,
                'ext': 'mp4',
                'http_headers': headers or {},
            })

    # ------------------------------------------------------------------ main
    def _real_extract(self, url):
        mobj = re.match(self._VALID_URL, url)
        video_id = mobj.group('id')
        display_id = mobj.group('display_id')
        if not display_id or display_id in self._NOT_A_SLUG:
            display_id = video_id

        parsed = compat_urlparse.urlparse(url)
        host = parsed.netloc.split(':')[0]
        base_url = '%s://%s' % (parsed.scheme or 'https', parsed.netloc)
        # age gate: MindGeek sites honour this cookie both as header and in the jar
        self._set_cookie('.' + (host[4:] if host.startswith('www.') else host), 'age_verified', '1')
        headers = {'Cookie': 'age_verified=1', 'Referer': base_url + '/'}

        webpage = self._download_webpage(url, display_id, headers=headers)
        media_headers = {'Referer': base_url + '/'}

        formats, seen = [], set()

        # 1) mediaDefinitions: [{"format": "hls"|"mp4", "quality": "720", "videoUrl": "...", "remote": true}, ...]
        for definitions in self._json_values_after(webpage, 'mediaDefinitions', video_id):
            for d in definitions:
                if not isinstance(d, dict):
                    continue
                vurl = d.get('videoUrl') or d.get('video_url') or d.get('url')
                if not vurl:
                    continue
                vurl = self._clean_url(vurl)
                is_indirect = d.get('remote') or (
                    vurl.startswith('/') and not determine_ext(vurl, default_ext='') in ('m3u8',) + self._MEDIA_EXTS)
                if is_indirect:
                    # RedTube/YouPorn-style indirection: the URL returns a JSON list of the real streams
                    sub = self._download_json(
                        urljoin(base_url, vurl), video_id, 'Downloading %s media list' % (d.get('format') or 'video'),
                        fatal=False, headers=headers)
                    for s in (sub if isinstance(sub, list) else []):
                        if isinstance(s, dict):
                            self._add_media(
                                formats, seen, s.get('videoUrl') or s.get('url'), base_url, video_id,
                                hint=s.get('format') or d.get('format'), quality=s.get('quality'),
                                headers=media_headers)
                else:
                    self._add_media(formats, seen, vurl, base_url, video_id, hint=d.get('format'),
                                    quality=d.get('quality'), headers=media_headers)

        # 2) any HLS / MP4 URL sitting in the page (player config, inline JSON, JW Player setup(), data-* ...)
        if not formats:
            for m in re.finditer(
                    r'(https?:(?:\\?/){2}[^"\'\s<>]+?\.m3u8[^"\'\s<>]*)', webpage):
                self._add_media(formats, seen, m.group(1), base_url, video_id, hint='hls', headers=media_headers)
        if not formats:
            for m in re.finditer(
                    r'''(?:["']?(?:file|src|hls|hlsUrl|hls_url|videoUrl|video_url|contentUrl|source|stream)["']?\s*[:=]\s*|<source[^>]+src=|<video[^>]+src=)["'](https?:(?:\\?/){2}[^"']+)["']''',
                    webpage):
                self._add_media(formats, seen, m.group(1), base_url, video_id, headers=media_headers)
        if not formats:
            for prop in ('og:video', 'og:video:url', 'og:video:secure_url', 'twitter:player:stream'):
                self._add_media(formats, seen, self._og_search_property(prop, webpage, default=None) or
                                self._html_search_meta(prop, webpage, default=None),
                                base_url, video_id, headers=media_headers)

        # 3) pre-redesign pages (flashvars) through the shared KeezMovies code
        if not formats:
            try:
                _, old = self._extract_info(url, fatal=False)
                formats.extend(old.get('formats') or [])
            except Exception:
                pass

        if not formats:
            raise ExtractorError(
                'Unable to find a video stream on the Tube8 page (the site layout may have changed, '
                'or it is age-/region-blocked for this server)', expected=True)
        self._sort_formats(formats)

        # ---- metadata (all optional)
        title = strip_or_none(self._og_search_title(webpage, default=None)) \
            or strip_or_none(self._html_search_regex(r'(?s)<title[^>]*>(.+?)</title>', webpage, 'title', default=None)) \
            or strip_or_none(self._search_regex(r'videoTitle\s*=\s*"([^"]+)', webpage, 'title', default=None)) \
            or display_id
        title = re.sub(r'\s*(?:Porn\s+Videos?)?\s*[-–|]\s*Tube8\s*$', '', unescapeHTML(title), flags=re.I).strip() or display_id

        json_ld = self._search_json_ld(webpage, video_id, expected_type='VideoObject', default={}) or {}
        thumbnail = self._og_search_thumbnail(webpage, default=None) or json_ld.get('thumbnail')
        if thumbnail and thumbnail.split('?')[0].lower().endswith('.svg'):
            thumbnail = json_ld.get('thumbnail') if not str(json_ld.get('thumbnail') or '').endswith('.svg') else None
        duration = (json_ld.get('duration')
                    or int_or_none(self._html_search_meta('video:duration', webpage, default=None))
                    or parse_duration(self._search_regex(
                        r'["\']duration["\']\s*:\s*["\']?([\d:PTHMS.]+)', webpage, 'duration', default=None)))
        view_count = str_to_int(self._search_regex(
            r'Views:\s*</dt>\s*<dd>([\d,\.]+)', webpage, 'view count', default=None)) or json_ld.get('view_count')

        return {
            'id': video_id,
            'display_id': display_id,
            'title': title,
            'description': self._og_search_description(webpage, default=None) or json_ld.get('description'),
            'thumbnail': thumbnail,
            'duration': duration,
            'uploader': json_ld.get('uploader') or self._search_regex(
                r'<span class="username">\s*(.+?)\s*<', webpage, 'uploader', default=None),
            'view_count': view_count,
            'age_limit': 18,
            'formats': formats,
        }

# coding: utf-8
from __future__ import unicode_literals

import re
import time

from .common import InfoExtractor
from ..utils import (
    ExtractorError,
    get_element_by_class,
    int_or_none,
    merge_dicts,
    url_or_none,
)


# URLs that are teasers/hover-previews/thumbnails, never the full video
_SKIP_URL = re.compile(r'(?:preview|thumb|trailer|teaser|hover|sprite|/ads?/|[_/-]prev(?:[_./-]|$)|\bclip\b)', re.I)
_URL_TOKEN = re.compile(r'https?://[^\s"\'<>\\]+', re.I)
_STREAM_EXT = re.compile(r'\.(?:m3u8|mp4)(?:[?#,]|$)', re.I)


class PeekVidsIE(InfoExtractor):
    _VALID_URL = r'''(?x)
        https?://(?:www\.)?peekvids\.com/
        (?:(?:[^/?#]+/){2}|embed/?\?(?:[^#]*&)?v=)
        (?P<id>[^/?&#]*)
    '''
    _TESTS = [{
        'url': 'https://peekvids.com/pc/dane-jones-cute-redhead-with-perfect-tits-with-mini-vamp/BSyLMbN0YCd',
        'md5': '2ff6a357a9717dc9dc9894b51307e9a2',
        'info_dict': {
            'id': '1262717',
            'display_id': 'BSyLMbN0YCd',
            'title': ' Dane Jones - Cute redhead with perfect tits with Mini Vamp',
            'ext': 'mp4',
            'thumbnail': r're:^https?://.*\.jpg$',
            'description': 'md5:0a61df3620de26c0af8963b1a730cd69',
            'timestamp': 1642579329,
            'upload_date': '20220119',
            'duration': 416,
            'view_count': int,
            'age_limit': 18,
            'uploader': 'SEXYhub.com',
            'categories': list,
            'tags': list,
        },
    }]
    _DOMAIN = 'www.peekvids.com'

    def _get_detail(self, html):
        return get_element_by_class('detail-video-block', html)

    def _formats_from_player_html(self, webpage, display_id, page_url):
        """The full-length stream URL (usually a .../master.m3u8 urlset) sits in the player markup BEFORE the <h1> title.
        Only that area is searched (not the related-video cards below it, which carry hover-preview clips)."""
        if not webpage:
            return []
        m = re.search(r'(?i)<h1\b', webpage)
        area = webpage[:m.start()] if m else webpage
        urls = []
        for u in _URL_TOKEN.findall(area):
            u = u.replace('&amp;', '&')
            if not _STREAM_EXT.search(u) or _SKIP_URL.search(u) or u in urls:
                continue
            urls.append(u)
        if not urls:
            return []
        headers = {'Referer': page_url}
        formats = []
        for u in urls:
            if re.search(r'\.m3u8(?:[?#]|$)', u, re.I):
                for f in self._extract_m3u8_formats(u, display_id, 'mp4', entry_protocol='m3u8_native', fatal=False, headers=headers):
                    f['http_headers'] = dict(headers)
                    formats.append(f)
        if formats:                      # an HLS master = the full-length adaptive stream: use it alone
            return formats
        for u in urls:
            mh = re.search(r'(?<!\d)(\d{3,4})p?(?=[^\d]|$)', u.rsplit('/', 1)[-1])
            h = int(mh.group(1)) if mh and int(mh.group(1)) in (240, 360, 480, 540, 720, 1080, 1440, 2160) else None
            formats.append({'url': u, 'format_id': str(h or 'http'), 'height': h, 'http_headers': dict(headers)})
        return formats

    def _formats_from_browser(self, url, display_id):
        try:
            from . import peekvids_browser
            data = peekvids_browser.sniff(url, display_id)
        except Exception as e:
            raise ExtractorError(
                'Unable to extract short video ID, and reading the page with a browser failed too: %s '
                '(if this server cannot reach the site at all, set PEEKVIDS_PROXY to a working proxy)' % str(e)[:300],
                expected=True)
        headers = {'Referer': data.get('referer') or 'https://%s/' % self._DOMAIN}
        if data.get('ua'):
            headers['User-Agent'] = data['ua']
        if data.get('cookie'):
            headers['Cookie'] = data['cookie']
        formats, seen = [], set()
        for key, u in data.get('srcs', {}).items():          # data-src720 -> 720p
            m = re.match(r'^data-src(\d{3,})$', key)
            if url_or_none(u) and u not in seen:
                seen.add(u)
                formats.append({'url': u, 'format_id': m.group(1) if m else key, 'height': int_or_none(m.group(1)) if m else None,
                                'http_headers': dict(headers)})
        for u in data.get('media', []):
            if u in seen or not url_or_none(u):
                continue
            seen.add(u)
            if re.search(r'\.m3u8(?:[?#]|$)', u, re.I):
                for f in self._extract_m3u8_formats(u, display_id, 'mp4', entry_protocol='m3u8_native', fatal=False, headers=headers):
                    f['http_headers'] = dict(headers)
                    formats.append(f)
            else:
                mh = re.search(r'(?<!\d)(\d{3,4})p?(?=[^\d]|$)', u.rsplit('/', 1)[-1])
                h = int(mh.group(1)) if mh and int(mh.group(1)) in (240, 360, 480, 540, 720, 1080, 1440, 2160) else None
                formats.append({'url': u, 'format_id': str(h or 'http'), 'height': h, 'http_headers': dict(headers)})
        if not formats:
            raise ExtractorError('Could not extract any playable format from the page', expected=True)
        return formats, data

    def _real_extract(self, url):
        video_id = self._match_id(url)
        fetch_err = None
        webpage = ''
        for attempt in range(3):
            try:
                webpage = self._download_webpage(url, video_id, expected_status=429)
                fetch_err = None
                break
            except ExtractorError as e:   # timeout / 403 for the plain (non-browser) request -> a real Chrome may still get in
                fetch_err, webpage = e, ''
                # flaky DNS / timeouts (e.g. "No address associated with hostname") usually clear within a second or two
                transient = re.search(r'No address associated|Name or service not known|Temporary failure in name|'
                                      r'timed out|Errno -[235]\b|Connection reset', str(e), re.I)
                if not transient or attempt == 2:
                    break
                time.sleep(1.5 * (attempt + 1))
        if fetch_err is not None:
            self.report_warning('%s: the plain page request failed (%s) - trying a browser' % (video_id, str(fetch_err)[:120]))
        rate_limited = '>Rate Limit Exceeded' in webpage
        if rate_limited:
            # the bare scripted request got the rate-limit page; a real Chrome often still gets the real page,
            # so don't give up here - drop the page and let the browser fallback below try
            self.report_warning('%s: rate-limit page for the plain request - trying a browser' % video_id)
            webpage = ''

        title = self._html_search_regex(r'(?s)<h1\b[^>]*>(.+?)</h1>', webpage, 'title', default=None) if webpage else None
        if not title and webpage:
            raise ExtractorError('Unable to extract title', expected=True)

        display_id = video_id
        formats, browser_info = [], {}

        # 0) the full-length stream URL straight from the player markup (skips hover-preview clips)
        if webpage:
            formats = self._formats_from_player_html(webpage, display_id, url)
            if formats:
                self.to_screen('%s: using the stream found in the player markup (%d format(s))' % (display_id, len(formats)))

        # 1) plain page: the short id sits in <video data-id=...>; a few alternative spots as a safety net
        short_id = self._search_regex(
            (r'(?s)<video\b[^>]+\bdata-id\s*=\s*["\']?([\w-]+)',
             r'(?s)<video\b[^>]+\bdata-video-id\s*=\s*["\']?([\w-]+)',
             r'/v-alt/([\w-]+)',
             r'["\']short_?[iI]d["\']\s*:\s*["\']([\w-]+)'),
            webpage, 'short video ID', default=None)
        if short_id and not formats:
            video_id = short_id
            srcs = self._download_json(
                'https://%s/v-alt/%s' % (self._DOMAIN, video_id), video_id,
                note='Downloading list of source files', fatal=False)
            if isinstance(srcs, dict):
                formats = [{
                    'url': f_url,
                    'format_id': f_id,
                    'height': int_or_none(f_id),
                } for f_url, f_id in (
                    (url_or_none(f_v), f_match.group(1))
                    for f_v, f_match in (
                        (v, re.match(r'^data-src(\d{3,})$', k))
                        for k, v in srcs.items() if v) if f_match)
                    if f_url
                ]
                if not formats:
                    formats = [{'url': u} for u in (url_or_none(v) for k, v in srcs.items()
                                                    if v and not _SKIP_URL.search(k) and not _SKIP_URL.search(str(v))) if u]

        # 2) nothing usable from the plain page -> let a real Chrome open it and report what the page loads
        if not formats:
            self.report_warning('%s: no video source in the plain page - reading it in a browser instead' % display_id)
            try:
                formats, browser_info = self._formats_from_browser(url, display_id)
            except ExtractorError as e:
                if rate_limited:
                    raise ExtractorError(
                        '[%s] %s: You are suspected as a bot (rate limit) and the browser fallback failed too: %s. '
                        'Wait a few minutes, set PEEKVIDS_PROXY, or provide --cookies.' % (self.IE_NAME, display_id, str(e)[:200]),
                        expected=True)
                if fetch_err is not None:      # nothing worked: report the original network error first
                    raise ExtractorError('%s | browser: %s' % (str(fetch_err)[:200], str(e)[:200]), expected=True)
                raise
            if browser_info.get('title') and not title:
                title = browser_info['title']
            video_id = short_id or display_id
        if len(formats) > 1:
            _full = [f for f in formats if not _SKIP_URL.search(f.get('url', ''))]
            formats = _full or formats
        self._sort_formats(formats)

        info = self._search_json_ld(webpage, video_id, expected_type='VideoObject', default={})
        info.pop('url', None)
        # may not have found the thumbnail if it was in a list in the ld+json
        info.setdefault('thumbnail', self._og_search_thumbnail(webpage, default=None) or browser_info.get('thumbnail'))
        detail = self._get_detail(webpage) or ''
        info['description'] = self._html_search_regex(
            r'(?s)(.+?)(?:%s\s*<|<ul\b)' % (re.escape(info.get('description', '')), ),
            detail, 'description', default=None) or None
        info['title'] = re.sub(r'\s*[,-][^,-]+$', '', info.get('title') or title) or self._generic_title(url)

        def cat_tags(name, html):
            l = self._html_search_regex(
                r'(?s)<span\b[^>]*>\s*%s\s*:\s*</span>(.+?)</li>' % (re.escape(name), ),
                html, name, default='')
            return [x for x in re.split(r'\s+', l) if x]

        return merge_dicts({
            'duration': browser_info.get('duration'),
            'id': video_id,
            'display_id': display_id,
            'age_limit': 18,
            'formats': formats,
            'categories': cat_tags('Categories', detail),
            'tags': cat_tags('Tags', detail),
            'uploader': self._html_search_regex(r'[Uu]ploaded\s+by\s(.+?)"', webpage, 'uploader', default=None),
        }, info)


class PlayVidsIE(PeekVidsIE):
    _VALID_URL = r'https?://(?:www\.)?playvids\.com/(?:embed/|\w\w?/)?(?P<id>[^/?#]*)'
    _TESTS = [{
        'url': 'https://www.playvids.com/U3pBrYhsjXM/pc/dane-jones-cute-redhead-with-perfect-tits-with-mini-vamp',
        'md5': '2f12e50213dd65f142175da633c4564c',
        'info_dict': {
            'id': '1978030',
            'display_id': 'U3pBrYhsjXM',
            'title': ' Dane Jones - Cute redhead with perfect tits with Mini Vamp',
            'ext': 'mp4',
            'thumbnail': r're:^https?://.*\.jpg$',
            'description': 'md5:0a61df3620de26c0af8963b1a730cd69',
            'timestamp': 1640435839,
            'upload_date': '20211225',
            'duration': 416,
            'view_count': int,
            'age_limit': 18,
            'uploader': 'SEXYhub.com',
            'categories': list,
            'tags': list,
        },
    }, {
        'url': 'https://www.playvids.com/es/U3pBrYhsjXM/pc/dane-jones-cute-redhead-with-perfect-tits-with-mini-vamp',
        'only_matching': True,
    }, {
        'url': 'https://www.playvids.com/embed/U3pBrYhsjXM',
        'only_matching': True,
    }, {
        'url': 'https://www.playvids.com/bKmGLe3IwjZ/sv/brazzers-800-phone-sex-madison-ivy-always-on-the-line',
        'md5': 'e783986e596cafbf46411a174ab42ba6',
        'info_dict': {
            'id': '762385',
            'display_id': 'bKmGLe3IwjZ',
            'ext': 'mp4',
            'title': 'Brazzers - 1 800 Phone Sex: Madison Ivy Always On The Line 6',
            'description': 'md5:bdcd2db2b8ad85831a491d7c8605dcef',
            'timestamp': 1516958544,
            'upload_date': '20180126',
            'thumbnail': r're:^https?://.*\.jpg$',
            'duration': 480,
            'uploader': 'Brazzers',
            'age_limit': 18,
            'view_count': int,
            'age_limit': 18,
            'categories': list,
            'tags': list,
        },
    }, {
        'url': 'https://www.playvids.com/v/47iUho33toY',
        'md5': 'b056b5049d34b648c1e86497cf4febce',
        'info_dict': {
            'id': '700621',
            'display_id': '47iUho33toY',
            'ext': 'mp4',
            'title': 'KATEE OWEN STRIPTIASE IN SEXY RED LINGERIE',
            'description': None,
            'timestamp': 1507052209,
            'upload_date': '20171003',
            'thumbnail': r're:^https?://.*\.jpg$',
            'duration': 332,
            'uploader': 'Cacerenele',
            'age_limit': 18,
            'view_count': int,
            'categories': list,
            'tags': list,
        }
    }, {
        'url': 'https://www.playvids.com/z3_7iwWCmqt/sexy-teen-filipina-striptease-beautiful-pinay-bargirl-strips-and-dances',
        'md5': 'efa09be9f031314b7b7e3bc6510cd0df',
        'info_dict': {
            'id': '1523518',
            'display_id': 'z3_7iwWCmqt',
            'ext': 'mp4',
            'title': 'SEXY TEEN FILIPINA STRIPTEASE - Beautiful Pinay Bargirl Strips and Dances',
            'description': None,
            'timestamp': 1607470323,
            'upload_date': '20201208',
            'thumbnail': r're:^https?://.*\.jpg$',
            'duration': 593,
            'uploader': 'yorours',
            'age_limit': 18,
            'view_count': int,
            'categories': list,
            'tags': list,
        },
    }]
    _DOMAIN = 'www.playvids.com'

    def _get_detail(self, html):
        return get_element_by_class('detail-block', html)

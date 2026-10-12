from __future__ import unicode_literals

import re

from .common import InfoExtractor
from ..compat import compat_str
from ..utils import (
    ExtractorError,
    int_or_none,
    try_get,
    unified_timestamp,
)


class BeegIE(InfoExtractor):
    # beeg's current URLs carry a leading "-" before the id (https://beeg.com/-0331654622832684); the API id is the digits
    # only. The old pattern required a digit straight after the slash, so these links never matched. The old numeric
    # links (beeg.com/5416503, beeg.porn/video/5416503) still match too.
    _VALID_URL = r'https?://(?:www\.)?beeg\.(?:com(?:/video)?|porn(?:/video)?)/-?(?P<id>\d+)'
    _TESTS = [{
        'url': 'https://beeg.com/-0983946056129650',
        'only_matching': True,
    }, {
        'url': 'https://beeg.com/-0599050563103750?t=4-861',
        'only_matching': True,
    }, {
        'url': 'https://beeg.com/0331654622832684',
        'only_matching': True,
    }, {
        'url': 'https://beeg.porn/video/5416503',
        'only_matching': True,
    }]

    # The old api/v6/<beeg_version>/video/<id> endpoint is gone; beeg's pages now load everything from this store API
    # (file info + HLS playlists) and the playlists from video.beeg.com.
    _API_URL = 'https://store.externulls.com/facts/file/%s'
    _CDN_URL = 'https://video.beeg.com/'
    _REFERER = 'https://beeg.com/'

    def _real_extract(self, url):
        video_id = self._match_id(url)

        video = self._download_json(
            self._API_URL % video_id, video_id, 'Downloading video JSON',
            headers={'Referer': self._REFERER, 'Origin': 'https://beeg.com', 'Accept': 'application/json'})
        if not isinstance(video, dict):
            raise ExtractorError('Unexpected response from the beeg API', expected=True)

        file_info = video.get('file') if isinstance(video.get('file'), dict) else {}
        facts = [f for f in (video.get('fc_facts') or []) if isinstance(f, dict)]

        # The lowest-id fact is the video's own record (later ones are clips cut from it).
        first_fact = {}
        for fact in facts:
            try:
                if not first_fact or fact['id'] < first_fact['id']:
                    first_fact = fact
            except (KeyError, TypeError):
                first_fact = first_fact or fact

        resources = (
            file_info.get('hls_resources')
            or first_fact.get('hls_resources')
            or next((f['hls_resources'] for f in facts if f.get('hls_resources')), None))
        if not isinstance(resources, dict) or not resources:
            raise ExtractorError(
                'No video streams in the beeg API response (removed video, or beeg changed its API again)', expected=True)

        formats = []
        for format_id, uri in resources.items():
            if not uri or not isinstance(uri, compat_str):
                continue
            # keys look like "fl_cdn_720" -> 720p
            m = re.search(r'(\d{3,4})', format_id)
            height = int_or_none(m.group(1)) if m else None
            if re.match(r'https?://', uri):
                m3u8_url = uri
            elif uri.startswith('//'):
                m3u8_url = 'https:' + uri
            else:
                m3u8_url = self._CDN_URL + uri.lstrip('/')
            m3u8_formats = self._extract_m3u8_formats(
                m3u8_url, video_id, 'mp4', entry_protocol='m3u8_native',
                m3u8_id=compat_str(height or format_id), fatal=False,
                headers={'Referer': self._REFERER})
            for f in m3u8_formats:
                if height:
                    f['height'] = height
                f.setdefault('http_headers', {})['Referer'] = self._REFERER
            formats.extend(m3u8_formats)
        if not formats:
            raise ExtractorError('Could not extract any playable format from beeg', expected=True)
        self._sort_formats(formats)

        tags = [
            t['tg_name'].strip() for t in (video.get('tags') or [])
            if isinstance(t, dict) and isinstance(t.get('tg_name'), compat_str) and t['tg_name'].strip()] or None

        return {
            'id': video_id,
            'display_id': compat_str(first_fact['id']) if first_fact.get('id') is not None else None,
            'title': try_get(file_info, lambda x: x['stuff']['sf_name'], compat_str) or 'beeg-%s' % video_id,
            'description': try_get(file_info, lambda x: x['stuff']['sf_story'], compat_str),
            'timestamp': unified_timestamp(first_fact.get('fc_created')),
            'duration': int_or_none(file_info.get('fl_duration')),
            'tags': tags,
            'formats': formats,
            'age_limit': 18,
        }

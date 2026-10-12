# coding: utf-8
from __future__ import unicode_literals

# Trimmed ytdl_legacy: ONLY adult-site extractors are shipped (see extractors.py). There is deliberately no
# GenericIE (it would pull in hundreds of unrelated extractors) -- a URL that none of these match simply fails
# with "Unsupported URL", which the caller treats as "legacy has nothing for this site".
_LAZY_LOADER = False

from .extractors import *  # noqa: F401,F403

_ALL_CLASSES = [
    klass
    for name, klass in list(globals().items())
    if name.endswith('IE') and name not in ('GenericIE',) and 'Base' not in name
]


def gen_extractor_classes():
    """List of supported extractors; the first one matching a URL handles it."""
    return _ALL_CLASSES


def gen_extractors():
    return [klass() for klass in gen_extractor_classes()]


def list_extractors(age_limit):
    return sorted(
        filter(lambda ie: ie.is_suitable(age_limit), gen_extractors()),
        key=lambda ie: ie.IE_NAME.lower())


def get_info_extractor(ie_name):
    """Returns the info extractor class with the given ie_name"""
    return globals()[ie_name + 'IE']

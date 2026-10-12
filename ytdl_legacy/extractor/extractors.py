# coding: utf-8
from __future__ import unicode_literals

# ADULT-SITE extractors only, MINUS the sites fbot already handles itself (eporner, pornhub, xhamster, xnxx, xvideos,
# spankbang, beeg, tnaflix.com). YouPorn is kept on purpose: fbot downloads it through yt-dlp, and this is its fallback.
# Order = match order.

from .alphaporno import (
    AlphaPornoIE,
)
from .behindkink import (
    BehindKinkIE,
)
from .bongacams import (
    BongaCamsIE,
)
from .cammodels import (
    CamModelsIE,
)
from .camtube import (
    CamTubeIE,
)
from .camwithher import (
    CamWithHerIE,
)
from .chaturbate import (
    ChaturbateIE,
)
from .cliphunter import (
    CliphunterIE,
)
from .drtuber import (
    DrTuberIE,
)
from .eroprofile import (
    EroProfileIE,
)
from .extremetube import (
    ExtremeTubeIE,
)
from .fourtube import (
    FourTubeIE,
    FuxIE,
    PornTubeIE,
    PornerBrosIE,
)
from .foxgay import (
    FoxgayIE,
)
from .goshgay import (
    GoshgayIE,
)
from .hellporno import (
    HellPornoIE,
)
from .hentaistigma import (
    HentaiStigmaIE,
)
from .hornbunny import (
    HornBunnyIE,
)
from .iwara import (
    IwaraIE,
)
from .keezmovies import (
    KeezMoviesIE,
)
from .lovehomeporn import (
    LoveHomePornIE,
)
from .mofosex import (
    MofosexIE,
    MofosexEmbedIE,
)
from .motherless import (
    MotherlessIE,
    MotherlessGroupIE,
)
from .myvidster import (
    MyVidsterIE,
)
from .nonktube import (
    NonkTubeIE,
)
from .nuvid import (
    NuvidIE,
)
from .peekvids import (
    PeekVidsIE,
    PlayVidsIE,
)
from .playvid import (
    PlayvidIE,
)
from .porn91 import (
    Porn91IE,
)
from .porncom import (
    PornComIE,
)
from .pornhd import (
    PornHdIE,
)
from .pornotube import (
    PornotubeIE,
)
from .pornovoisines import (
    PornoVoisinesIE,
)
from .pornoxo import (
    PornoXOIE,
)
from .redtube import (
    RedTubeIE,
)
from .tnaflix import (
    EMPFlixIE,
    MovieFapIE,
)
from .tube8 import (
    Tube8IE,
)
from .watchindianporn import (
    WatchIndianPornIE,
)
from .xbef import (
    XBefIE,
)
from .xtube import (
    XTubeIE,
    XTubeUserIE,
)
from .xxxymovies import (
    XXXYMoviesIE,
)
from .youjizz import (
    YouJizzIE,
)
from .youporn import (
    YouPornIE,
    YouPornCategoryIE,
    YouPornChannelIE,
    YouPornCollectionIE,
    YouPornTagIE,
    YouPornStarIE,
    YouPornVideosIE,
)
from .yourporn import (
    YourPornIE,
)

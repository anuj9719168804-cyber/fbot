"""Stream-button resolver for links that have no dedicated stream code (the 'generic link' case).

send_stream_link() only knew Terabox / DiskWala / SpankBang / the fixed yt-dlp hosts / fpo and then fell through to
FapHouse's get_m3u8_url() - so for ANY other link (a direct .mp4/.m3u8 file, a random video page, TnaFlix, ...) the
Stream Link button always ended in "No stream URL found". resolve() tries, in order:

  1. the link itself, when it already is a playable file / HLS playlist (by extension)
  2. the sites with their own resolvers (mat6tube, tnaflix)
  3. yt-dlp (dedicated, legacy AND Generic extractor)  (ytdlp.get_stream_url -> best variant's real URL)
  4. page scan (page_extract._scan: HTML + inline player config + real-Chrome sniff), one level into <iframe> players

Everything site-specific is injected, so this module imports nothing heavy and is testable on its own.
"""
import asyncio
import logging

logger = logging.getLogger("faphouse_bot")

_PLAYABLE = ("hls", "video")   # what an "Open Stream" link can actually play (audio / files / DASH can't)


async def _first_playable(scan_result: dict, kind_of=lambda u: ""):
    direct = scan_result.get("direct")
    if direct:
        # the server answered with a non-HTML body: a video endpoint without an extension is fine, but a zip / pdf /
        # mp3 / DASH manifest is not something "Open Stream" can play
        return None if kind_of(direct) in ("file", "audio", "dash") else direct
    for it in scan_result.get("links") or []:
        if it.get("kind") in _PLAYABLE and it.get("url"):
            return it["url"]
    return None


async def resolve(link: str, *, scan, kind_of, mat6tube=None, tnaflix=None, ytdlp=None, timeout: int = 75):
    """-> (stream_url, how). stream_url is None when nothing playable was found; how = short source label / reason."""
    # 1) already a playable URL
    if kind_of(link) in _PLAYABLE:
        return link, "direct link"

    # 2) sites with a dedicated resolver
    for mod, name, check in ((mat6tube, "mat6tube", "is_mat6tube_link"), (tnaflix, "tnaflix", "is_tnaflix_link")):
        try:
            if mod is not None and getattr(mod, check)(link):
                variants = await asyncio.to_thread(mod.get_available_qualities, link)
                url = (variants or [{}])[0].get("url")
                if url:
                    return url, name
        except Exception as e:
            logger.info(f"[generic-stream] {name} resolver failed for {link}: {e}")

    # 3) yt-dlp: dedicated / legacy extractors AND its Generic extractor. The links that get a Stream button here were
    #    found by ytdlp.generic_links() - i.e. yt-dlp's Generic extractor already read a video from that page - and
    #    is_generically_supported() is False for exactly those, so it must not be used as a gate. The extraction is
    #    cached, so this is usually instant after the menu probe; a plain article just fails fast and falls through.
    if ytdlp is not None:
        try:
            url = await asyncio.wait_for(asyncio.to_thread(ytdlp.get_stream_url, link), timeout=timeout)
            if url:
                return url, "yt-dlp"
        except asyncio.TimeoutError:
            logger.info(f"[generic-stream] yt-dlp timed out for {link}")
        except Exception as e:
            logger.info(f"[generic-stream] yt-dlp had nothing for {link}: {str(e)[:150]}")

    # 4) scan the page (and up to two embedded players one level deeper)
    try:
        res = await asyncio.wait_for(scan(link), timeout=timeout)
    except asyncio.TimeoutError:
        return None, "page scan timed out"
    except Exception as e:
        return None, str(e)[:200] or "page scan failed"
    url = await _first_playable(res, kind_of)
    if url:
        return url, "page scan"
    for emb in [i for i in (res.get("links") or []) if i.get("kind") == "page"][:2]:
        try:
            res2 = await asyncio.wait_for(scan(emb["url"]), timeout=timeout)
        except Exception:
            continue
        url = await _first_playable(res2, kind_of)
        if url:
            return url, "embedded player"
    return None, "no playable stream on the page"

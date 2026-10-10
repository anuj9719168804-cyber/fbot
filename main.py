import os
import asyncio
import html
import inspect
import json
import logging
import random
import re
import shutil
import time
import uuid
import requests
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, urlparse

from pyrogram import Client, filters, idle
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, BotCommand, BotCommandScopeChat, LinkPreviewOptions
from pyrogram.enums import ParseMode, ChatMemberStatus
from pyrogram.errors import (FloodWait, InputUserDeactivated, UserIsBlocked, PeerIdInvalid, ButtonUrlInvalid, MessageNotModified,
                             ChannelInvalid, ChannelPrivate, ChatAdminRequired, ChatWriteForbidden)

try:
    from pyrogram.enums import ButtonStyle
    BUTTON_STYLE_SUPPORTED = True
except ImportError:
    BUTTON_STYLE_SUPPORTED = False

from config import (
    API_ID, API_HASH, BOT_TOKEN, OWNER_ID, TG_BOT_WORKERS,
    DOWNLOAD_DIR, MAX_CONCURRENT_DOWNLOADS, ADMINS, START_PHOTO_URL,
    DAILY_FREE_LIMIT, AUTO_DELETE_SECONDS, LOG_CHANNEL_ID, BACKUP_CHANNEL_IDS,
    DEFAULT_CHANNEL, CACHE_CHANNEL_ID, MAX_FILE_SIZE, SPLIT_PART_TARGET_BYTES,
)
import faphouse_downloader as faphouse
import fpo_downloader as fpo
import porn_fetch_downloader as pf
import ytdlp_downloader as ytdlp
import pot_provider
import ytnode_client
import flaresolver_bootstrap
import browser_bootstrap
import cfbypass_bootstrap
import ytsearch  # handlers registered via ytsearch.register(app) below — see end of file
import terabox_downloader as terabox
import mat6tube_downloader as mat6tube
import spankbang_downloader as spankbang
import jav_sites
import spankbang_scraper
import diskwala
import auto_scraper
import split_upload
from keep_alive import keep_alive, register_stream_proxy, get_stream_public_url
import titanium
from database import (
    register_user_if_new, is_banned, set_banned,
    get_premium_status, set_premium, remove_premium,
    get_daily_count, bump_daily_count, bump_total_downloads,
    get_user_total_downloads,
    set_caption, get_caption, del_caption,
    set_thumbnail, get_thumbnail, del_thumbnail,
    set_dump_chat, get_dump_chat,
    get_stats_summary, all_chat_ids, delete_user, get_all_users_full,
    get_cached_file, set_cached_file, delete_cached_file,
    get_cached_description, set_cached_description,
    add_channel, remove_channel, remove_all_channels, get_channels,
    ensure_indexes,
    add_pending_delete, remove_pending_delete, get_all_pending_deletes,
    add_active_download, remove_active_download, get_all_active_downloads,
    set_chat_scraper_state, get_chat_scraper_state,
    set_referrer, increment_referral_count, get_referral_count,
    get_referral_rewards_claimed, mark_referral_reward_claimed,
    grant_referral_premium_days,
    get_skipped_size_limit_videos, get_failed_videos, filter_by_actor,
    set_bot_setting, get_bot_setting, delete_bot_setting,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("faphouse_bot")

# FIX: process_and_upload_video()'s per-video work_dir only gets cleaned
# up by its own `finally: shutil.rmtree(...)` when that function returns
# normally — if the whole process dies mid-download instead (a Render
# redeploy, an OOM kill, any ungraceful restart), that in-progress
# work_dir's partial video is orphaned on disk forever, since nothing
# ever runs that finally block. These accumulate across crashes/redeploys
# until the disk fills up (confirmed cause of a real "[Errno 28] No space
# left on device" failure during /autoupload). Anything already sitting
# in DOWNLOAD_DIR at this exact point in startup can only be leftover
# garbage from a previous run — no download is running yet — so it's
# always safe to wipe the whole directory here before recreating it.
shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

app = Client(
    "faphouse_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=TG_BOT_WORKERS,
    sleep_threshold=60,          # FloodWait errors se bachata hai, upload hang nahi hota
    max_concurrent_transmissions=4,  # parallel upload chunks — speed boost
    # FIX: sqlite3.OperationalError: database is locked
    # Bots don't need a persistent SQLite session — bot_token is provided on
    # every start, so Pyrogram re-authenticates automatically each time anyway.
    # in_memory=True switches to MemoryStorage (no .session file, no locking)
    # which completely eliminates the "database is locked" crash that happens
    # when a previous bot process still holds the SQLite WAL lock during restart.
    in_memory=True,
)

download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
ACTIVE_TASKS: dict = {}  # user_id -> asyncio.Task, tracks the in-flight download/upload so /cancel can stop it

# Maps a short id (used in callback_data, which has a size limit) -> the
# original link. Entries are created when a link is received and cleaned
# up lazily; they aren't meant to survive a bot restart.
LINK_CACHE: dict[str, str] = {}
QUALITY_CACHE: dict[str, list] = {}
# link_id -> {"site_name", "title", "description"} — backs the "📄 Full
# Description" button attached after upload. Only populated for sites
# whose get_page_meta() actually returns a "description" (currently
# ytdlp_downloaderrr.py's sites) — faphouse/fpo/porn_fetch don't have one
# to show, so no button gets attached for those at all rather than
# showing an empty one.
DESC_CACHE: dict[str, dict] = {}

# ---------------------------------------------------------------------
# Safe, colored InlineKeyboardButton builder
# ---------------------------------------------------------------------
# Uses Telegram's colored inline-button style (blue "primary" for normal
# actions, red "danger" for cancel/close/destructive actions) when the
# installed pyrogram build supports it. Falls back to a plain button
# automatically on older pyrogram versions, so this never breaks the bot.

def make_button(text: str, callback_data: str = None, url: str = None,
                 style: "ButtonStyle" = None) -> InlineKeyboardButton:
    kwargs = {"text": text}
    if callback_data:
        kwargs["callback_data"] = callback_data
    if url:
        kwargs["url"] = url
    if BUTTON_STYLE_SUPPORTED and style is not None:
        kwargs["style"] = style
    return InlineKeyboardButton(**kwargs)


# Shorthand style constants (None on unsupported pyrogram builds, so
# passing them into make_button() is always safe).
BTN_PRIMARY = ButtonStyle.PRIMARY if BUTTON_STYLE_SUPPORTED else None
BTN_DANGER = ButtonStyle.DANGER if BUTTON_STYLE_SUPPORTED else None


def make_reply_button(text: str, style: "ButtonStyle" = None):
    """Same idea as make_button() but for the reply keyboard (the one
    pinned above the message box, not the inline buttons under a
    message). Falls back to a plain string button — which is exactly
    what Pyrogram expects for an unstyled reply-keyboard button — if
    the installed pyrogram build's KeyboardButton doesn't accept a
    style kwarg yet."""
    if BUTTON_STYLE_SUPPORTED and style is not None:
        try:
            return KeyboardButton(text=text, style=style)
        except TypeError:
            pass
    return text


# ---------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------

async def get_effective_premium_status(user_id: int) -> dict:
    """Same shape as database.get_premium_status(), but bot admins (see
    ADMINS in config.py) are always treated as Lifetime Premium — they
    never need to buy/be granted premium separately, and are never
    subject to the daily free-download limit."""
    if user_id in ADMINS:
        return {"is_premium": True, "lifetime": True, "expires_at": None}
    return await get_premium_status(user_id)


def human_size(n: float) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"


def human_speed(n: float) -> str:
    for unit in ["B/s", "KB/s", "MB/s", "GB/s"]:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB/s"


def human_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def hms(seconds: float) -> str:
    """Format seconds as H:MM:SS (e.g. 0:06:54)."""
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


_SMALLCAPS_MAP = str.maketrans(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "ᴀʙᴄᴅᴇғɢʜɪᴊᴋʟᴍɴᴏᴘǫʀsᴛᴜᴠᴡxʏᴢᴀʙᴄᴅᴇғɢʜɪᴊᴋʟᴍɴᴏᴘǫʀsᴛᴜᴠᴡxʏᴢ",
)


def smallcaps(text: str) -> str:
    return text.translate(_SMALLCAPS_MAP)


# Matches a Telegram-style @username/@bot mention (letters/digits/
# underscores, must start with a letter, 4-32 chars after the @).
_TAG_OR_MENTION_RE = re.compile(r"(<[^>]+>|@[A-Za-z][A-Za-z0-9_]{3,31})")


def smallcaps_html(text):
    """Small-caps every plain-text word in a message, but leaves HTML tags
    (<b>, <a href="...">, <blockquote>, etc.), the contents of
    <code>...</code>, and @username/@bot mentions completely untouched —
    so commands, UPI IDs, chat IDs, {placeholder} examples, and bot
    usernames the user needs to copy-paste or tap stay exactly as typed,
    while everything else gets the small-caps look. Mentions specifically
    have to stay literal ASCII, not just readable: Telegram only
    auto-links a plain @username as a tappable mention when the text is
    an exact match, so small-caps glyphs there would silently turn a
    clickable bot link into dead text.
    Safe to call on non-strings (e.g. None) — passes them through as-is."""
    if not isinstance(text, str):
        return text
    parts = _TAG_OR_MENTION_RE.split(text)
    out = []
    in_code = 0
    for part in parts:
        if part.startswith("<") and part.endswith(">"):
            lower = part.lower()
            if lower.startswith("<code"):
                in_code += 1
            elif lower.startswith("</code"):
                in_code = max(0, in_code - 1)
            out.append(part)
        elif part.startswith("@"):
            out.append(part)
        else:
            out.append(part if in_code else part.translate(_SMALLCAPS_MAP))
    return "".join(out)


SC = smallcaps_html

# ANSI escape code stripper — yt-dlp prints colored errors to stderr even
# with quiet=True, and they end up in exception messages as literal escape
# sequences like "\x1b[0;31mERROR:\x1b[0m". Strip them before showing to user.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mABCDEFGHJKSTfinsulh]")

def _strip_ansi(text: str) -> str:
    """Remove ANSI terminal color/style escape sequences from a string."""
    if not text:
        return text
    return _ANSI_RE.sub("", text)

POWERED_BY = "Anuj Kumar"  # change this to whatever name/credit you want shown
POWERED_BY_URL = "https://t.me/anujedits76"  # change this to the profile/channel to link to


def _guess_site_name_from_link(link: str) -> str:
    """Display name for the "🔗 Source" caption line — a few known
    hosts get a nicer name, everything else falls back to titlecasing
    the domain's main label (e.g. "xvideos.com" -> "Xvideos"). Only used
    when the backend itself didn't already supply a name (ytdlp_downloaderrr
    sites provide page_meta's "site_name" straight from yt-dlp, which is
    preferred over this guess — see build_caption's caller)."""
    _KNOWN_NAMES = {
        "faphouse": "Faphouse", "faphouse2": "Faphouse",
        "fpo": "FPO.XXX",
        "spankbang": "SpankBang",
        "xfreehd": "XFreeHD",
        "jable": "JableTV", "fs1": "JableTV", "missav": "MissAV", "missav123": "MissAV",
        "supjav": "SupJav", "jav": "Jav.guru",
    }
    try:
        host = urlparse(link).netloc.lower().split("@")[-1].split(":")[0]
        labels = host.split(".")
        # Strip common non-identifying subdomain prefixes (www, beta,
        # m, mobile) so "beta.xfreehd.com" still resolves to "XFreeHD"
        # rather than "Beta" — matches the same beta./www. normalization
        # porn_fetch_downloader.py already does for xfreehd specifically,
        # just generalized here for the caption label across any site.
        while len(labels) > 2 and labels[0] in ("www", "beta", "m", "mobile"):
            labels = labels[1:]
        label = labels[0]
        return _KNOWN_NAMES.get(label, label.replace("-", " ").title())
    except Exception:
        return "Source"


async def build_caption(name: str, size_bytes: int, dl_seconds: float, ul_seconds: float,
                         user_id: int, source_link: str, quality_label: str = "Auto (Best)",
                         duration_seconds: float = 0, suppress_auto_delete_note: bool = False,
                         views: int = None, upload_date: str = None,
                         likes: int = None, comments: int = None,
                         author: str = None, author_url: str = None, category: str = None,
                         downloaded_by_username: str = None, source_site_name: str = None,
                         title: str = None, downloaded_by_name: str = None) -> str:
    auto_delete_note = ""
    if AUTO_DELETE_SECONDS > 0 and not suppress_auto_delete_note:
        auto_delete_note = (
            f"⚠️ This file will auto-delete from here in {human_time(AUTO_DELETE_SECONDS)}.\n"
            "📤 Please forward it to any other chat to save it permanently.\n"
        )

    custom_caption = await get_caption(user_id)
    if custom_caption:
        text = (
            custom_caption
            .replace("{filename}", name)
            .replace("{size}", human_size(size_bytes))
            .replace("{quality}", quality_label)
            .replace("{source}", source_link)
            .replace("{duration}", hms(duration_seconds) if duration_seconds else "Unknown")
            .replace("{views}", f"{views:,}" if views else "Unknown")
            .replace("{upload_date}", upload_date or "Unknown")
            .replace("{likes}", f"{likes:,}" if likes else "Unknown")
            .replace("{comments}", f"{comments:,}" if comments else "Unknown")
            .replace("{author}", author or "Unknown")
            .replace("{category}", category or "Unknown")
            .replace("{title}", title or "Unknown")
        )
        if auto_delete_note:
            text += f"\n\n{auto_delete_note}"
        return text

    powered_text = smallcaps(POWERED_BY)
    powered_html = f'<a href="{POWERED_BY_URL}">{powered_text}</a>' if POWERED_BY_URL else powered_text

    source_text = smallcaps(f"{source_site_name or _guess_site_name_from_link(source_link)} Link")
    source_html = f'<a href="{source_link}">{source_text}</a>' if source_link else source_text

    # BUG FIX: the video's own title used to only ever appear via the
    # separate "📄 Full Description" button — which only exists at all for
    # ytdlp_downloaderrr.py-backed sites (faphouse.py/fpo.py's get_page_meta
    # don't return a "description"), and even then only after the person
    # taps it. Every other case (faphouse/fpo links, cache hits, anything
    # where that button didn't get attached) showed no title/description
    # anywhere in the caption at all. Putting the title directly in the
    # caption itself — when the caller has one — means it's visible
    # unconditionally, the same way the reference bot shows it inline
    # rather than behind a button.
    title_line = f"🎬 <b>{smallcaps(title)}</b>\n\n" if title else ""

    # Every one of these six is None on most sites (faphouse.com/
    # fpo.xxx don't expose them at all — see ytdlp_downloaderrr.py's
    # get_page_meta for the sites that do, via yt-dlp's own extracted
    # info) — each line is skipped entirely rather than printed as
    # "Unknown", so the caption doesn't grow six guaranteed-empty lines
    # for every video regardless of source site.
    author_line = ""
    if author:
        author_text = f'<a href="{author_url}">{smallcaps(author)}</a>' if author_url else smallcaps(author)
        author_line = f"👤 {smallcaps('By')}: {author_text}\n"
    stats_lines = ""
    if views:
        stats_lines += f"👁️ {smallcaps('Views')}: {views:,}\n"
    if likes:
        stats_lines += f"👍 {smallcaps('Likes')}: {likes:,}\n"
    if comments:
        stats_lines += f"💬 {smallcaps('Comments')}: {comments:,}\n"
    if category:
        stats_lines += f"🏷️ {smallcaps('Category')}: {smallcaps(category)}\n"
    if upload_date:
        stats_lines += f"📅 {smallcaps('Uploaded on')}: {upload_date}\n"

    # Same "named link" style as ⚡ Powered by, instead of raw "@username"
    # plain text — a proper display name reads better than a handle, and
    # matching the Powered By treatment is what was actually asked for.
    # Falls back name -> @username -> numeric user_id, same order of
    # preference as before, just styled consistently now.
    if downloaded_by_name:
        downloaded_by_html = f'<a href="tg://user?id={user_id}">{smallcaps(downloaded_by_name)}</a>'
    elif downloaded_by_username:
        downloaded_by_html = f'<a href="https://t.me/{downloaded_by_username}">{smallcaps(downloaded_by_username)}</a>'
    else:
        downloaded_by_html = str(user_id)

    return (
        f"{title_line}"
        "<blockquote>"
        f"📄 {smallcaps('File Name')}: {smallcaps(name)}\n"
        f"{author_line}"
        f"📦 {smallcaps('Size')}: {human_size(size_bytes)}\n"
        f"🎞️ {smallcaps('Quality')}: {smallcaps(quality_label)}\n"
        f"⏱️ {smallcaps('Duration')}: {hms(duration_seconds) if duration_seconds else smallcaps('Unknown')}\n"
        f"{stats_lines}"
        f"⬇️ {smallcaps('Downloaded in')}: {hms(dl_seconds)} sec\n"
        f"⬆️ {smallcaps('Uploaded in')}: {hms(ul_seconds)} sec\n"
        f"🙋 {smallcaps('Downloaded by')}: {downloaded_by_html}\n"
        f"🔗 {smallcaps('Source')}: {source_html}\n"
        f"{auto_delete_note}"
        "</blockquote>\n\n"
        f"⚡ {smallcaps('Powered by')} {powered_html}"
    )


def progress_bar(pct: float, width: int = 10) -> str:
    filled = min(width, int(width * pct / 100))
    return "⬢" * filled + "⬡" * (width - filled)


class ProgressTracker:
    """Throttled Telegram status-message updater for download/upload progress."""

    def __init__(self, status_msg: Message, label: str, name: str,
                 interval: float = 4.0, quality: str = None, duration: str = None):
        self.status_msg = status_msg
        self.label = label
        self.name = name
        self.interval = interval
        self.quality = quality
        self.duration = duration
        self.start_time = time.time()
        self.last_edit_time = 0.0
        self._first_update = False        # pehla progress callback aaya ya nahi
        self._waiting_task: asyncio.Task | None = None

    def start_upload_wait_animation(self):
        """Upload se pehle animated dots dikhata hai jab tak Pyrogram
        file buffer nahi kar leta aur pehla progress callback nahi aata."""
        self._waiting_task = asyncio.create_task(self._upload_wait_loop())

    async def _upload_wait_loop(self):
        frames = ["⏳", "⌛"]
        dots   = ["", ".", "..", "..."]
        i = 0
        wait_start = time.time()
        while not self._first_update:
            # FIX: was a fixed 1.2s sleep the entire time this loop runs.
            # For a large file with a genuinely slow upload-connection
            # setup, this could keep firing edit_text every 1.2s for
            # several minutes straight (a 5-minute wait = ~250 edits on
            # one message) — Telegram's edit-rate limit doesn't allow
            # anywhere near that, so a long stall risked a FloodWait on
            # the status message itself. Backing off the interval as the
            # wait drags on keeps the animation responsive at first while
            # keeping total edits bounded even if this runs for minutes.
            elapsed_before_sleep = time.time() - wait_start
            if elapsed_before_sleep < 10:
                sleep_for = 1.2
            elif elapsed_before_sleep < 30:
                sleep_for = 3.0
            else:
                sleep_for = 6.0
            await asyncio.sleep(sleep_for)
            if self._first_update:
                break
            if not auto_scraper.chat_edit_allowed(getattr(getattr(self.status_msg, "chat", None), "id", None)):
                continue
            try:
                icon = frames[i % 2]
                dot  = dots[i % 4]
                elapsed = time.time() - wait_start
                # Elapsed time is the actual fix for the underlying
                # complaint here: the code has no way to make Telegram's
                # own upload-connection setup faster (that's network/file-
                # size dependent, not something this loop controls), but
                # showing how long it's genuinely been waiting lets the
                # person tell "this is just slow" apart from "this is
                # stuck" — a bare unchanging "Preparing..." line can't.
                await self.status_msg.edit_text(
                    SC(
                        f"{icon} <b>Uploading to Telegram{dot}</b>\n\n"
                        "╭━━━━❰Please Wait❱━➣\n"
                        f"┣⪼ 🎬 File: <code>{self.name}</code>\n"
                        "┣⪼ ⚙️ Preparing upload stream...\n"
                        f"┣⪼ ⏱ Elapsed: {hms(int(elapsed))}\n"
                        "╰━━━━━━━━━━━━━━━➣"
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            i += 1

    def _stop_wait_animation(self):
        if self._waiting_task and not self._waiting_task.done():
            self._waiting_task.cancel()
            self._waiting_task = None

    async def update(self, current: int, total: int):
        if not self._first_update:
            self._first_update = True
            self._stop_wait_animation()
        now = time.time()
        is_done = total and current >= total
        if not is_done and (now - self.last_edit_time) < self.interval:
            return
        # shared per-chat gate (same one auto_scraper's trackers use) so parallel
        # progress bars in one chat can't add up to a Telegram edit FloodWait
        if not auto_scraper.chat_edit_allowed(getattr(getattr(self.status_msg, "chat", None), "id", None), final=bool(is_done)):
            return
        self.last_edit_time = now

        elapsed = now - self.start_time
        speed = current / elapsed if elapsed > 0 else 0
        pct = (current / total * 100) if total else 0
        eta = (total - current) / speed if speed > 0 and total else 0

        is_download = "download" in self.label.lower()
        emoji = "📥" if is_download else "📤"
        title_verb = "Downloading" if is_download else "Uploading"
        connections_word = "download" if is_download else "upload"
        duration_line = f"┣⪼ ⏱ Duration: {self.duration}\n" if self.duration else ""
        quality_line = f"┣⪼ 🎞 Quality: {self.quality}\n" if self.quality else ""

        try:
            await self.status_msg.edit_text(
                SC(f"{emoji} <b>Fast {title_verb} via Main Engine</b>\n\n"
                "╭━━━━❰Progress❱━➣\n"
                f"┣⪼ 🎬 File: <code>{self.name}</code>\n"
                f"{duration_line}"
                f"{quality_line}"
                f"┣⪼ [{progress_bar(pct)}]\n"
                f"┣⪼ ✅ {pct:.1f}%\n"
                f"┣⪼ 💾 {human_size(current)} / {human_size(total)}\n"
                f"┣⪼ ⚡ {human_speed(speed)}\n"
                f"┣⪼ 🕐 Elapsed: {human_time(elapsed)}\n"
                f"┣⪼ ⏳ ETA: {human_time(eta)}\n"
                "╰━━━━━━━━━━━━━━━➣\n\n"
                f"⚡ Hyper {connections_word} connections active"),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------
# Menus / keyboards
# ---------------------------------------------------------------------

MAIN_MENU_KB = ReplyKeyboardMarkup(
    [
        [make_reply_button("💎 ᴘʟᴀɴs", style=BTN_PRIMARY), make_reply_button("📊 ᴍʏ sᴛᴀᴛᴜs", style=BTN_PRIMARY)],
        [make_reply_button("❓ ʜᴇʟᴘ", style=BTN_PRIMARY), make_reply_button("☎️ sᴜᴘᴘᴏʀᴛ", style=BTN_PRIMARY)],
    ],
    resize_keyboard=True,
)

MENU_BUTTON_TEXTS = {"💎 ᴘʟᴀɴs", "📊 ᴍʏ sᴛᴀᴛᴜs", "❓ ʜᴇʟᴘ", "☎️ sᴜᴘᴘᴏʀᴛ"}


# ---------------------------------------------------------------------
# Menu button matching — robust to variation-selector drift
# ---------------------------------------------------------------------
# Some Telegram clients don't echo back the invisible U+FE0F "variation
# selector" byte the same way it was sent in a button label (☎️/❓ are
# emoji-presentation sequences that use it). That means a plain
# `filters.regex(r"^☎️ sᴜᴘᴘᴏʀᴛ$")` exact match can silently fail on some
# devices even though the button visually looks identical — the tap then
# falls through to the generic link handler and looks like the button
# "does nothing".
#
# FIX: this used to be a custom `filters.create(async_func)` filter that
# normalized both sides in Python before comparing. That's correct in
# theory, but gave a worse failure mode in practice — if the custom
# callback ever silently doesn't match for any reason (async/sync
# handling quirks across pyrogram/kurigram builds, etc.), the message
# matches NO handler anywhere at all: not the button handler, and not
# the fallback link handler either (since the same custom filter also
# drives that exclusion), so nothing replies whatsoever — worse than the
# original bug, which at least fell through to a visible fallback reply.
# Rebuilt as a plain `filters.regex()` pattern instead — pyrogram's own
# native, thoroughly-tested primitive — where the FE0F byte is simply
# made optional after every character, rather than relying on any custom
# callback matching correctly at all.
def _tolerant_button_pattern(expected: str) -> str:
    base = expected.replace("\ufe0f", "")
    return "^" + r"\ufe0f?".join(re.escape(ch) for ch in base) + r"\ufe0f?$"


def menu_text_filter(expected: str):
    """Filter that matches a message/caption equal to `expected`,
    tolerating variation-selector (U+FE0F) differences."""
    return filters.regex(_tolerant_button_pattern(expected))


_MENU_BUTTON_REGEX_PATTERN = "|".join(f"(?:{_tolerant_button_pattern(t)})" for t in MENU_BUTTON_TEXTS)
NOT_MENU_BUTTON_FILTER = ~filters.regex(_MENU_BUTTON_REGEX_PATTERN)

# ---------------------------------------------------------------------
# Premium plans — informational only, there's no payment gateway wired
# up here. A plan tap tells the user how to contact the admin to get it
# activated manually via /addpremium.
# ---------------------------------------------------------------------
# (price, total days granted, bonus days already included in that total) -- same plans as ak-vip
_PLAN_DEFS = [
    (99, 18, 3),    # 15 + 3
    (199, 35, 5),   # 30 + 5
    (299, 50, 5),   # 45 + 5
    (399, 68, 8),   # 60 + 8
    (599, 100, 10), # 90 + 10
    (999, 165, 15), # 150 + 15
]


def _plan_label(days: int, extra: int) -> str:
    return f"{days - extra} Days + {extra} Extra Days 🎁" if extra else f"{days} Days"


PLANS = [(price, _plan_label(days, extra)) for price, days, extra in _PLAN_DEFS]  # [(price, label)]
PLAN_DAYS = {price: days for price, days, _ in _PLAN_DEFS}  # price -> total days to grant with /addpremium

PLANS_PHOTO_URL = "https://iili.io/nHyIqox.jpg"

PLANS_TEXT = (
    "💎 <b>ᴘʀᴇᴍɪᴜᴍ ᴍᴇᴍʙᴇʀsʜɪᴘ ᴘʟᴀɴs</b>\n"
    "✨ Unlock Unlimited Access & Advanced Features!\n\n"
    + "\n".join(f"• ₹{p} → {l}" for p, l in PLANS) + "\n\n"
    "🔒 <b>sᴇᴄᴜʀᴇ ᴘᴀʏᴍᴇɴᴛ:</b>\n"
    "⚡️ ᴜᴘɪ ɪᴅ: <code>971916880@ybl</code>\n"
    "🔗 ǫʀ ᴄᴏᴅᴇ: <a href=\"https://iili.io/nHyIqox.jpg\">Scan to Pay</a>\n"
    "💡 After Payment: Send Screenshot to Admin for Instant Activation.\n\n"
    "👇 Plan pe tap karo — shuru ho jao!"
)


def plans_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for price, label in PLANS:
        rows.append([make_button(SC(f"💎 ₹{price} - {label}"), callback_data=f"plan_{price}", style=BTN_PRIMARY)])
    rows.append([make_button(SC("📸 Send Payment Proof"), url=POWERED_BY_URL, style=BTN_PRIMARY)])
    rows.append([make_button(SC("⬅️ Back"), callback_data="plans_back", style=BTN_DANGER)])
    return InlineKeyboardMarkup(rows)


@app.on_callback_query(filters.regex(r"^plans_back$"))
async def plans_back_cb(client: Client, query):
    try:
        await query.message.delete()
    except Exception:
        pass
    await query.answer()


async def send_plans_message(m: Message):
    """Sends the QR/payment photo with the plans text as its caption,
    falling back to plain text if the photo can't be fetched/sent."""
    try:
        await m.reply_photo(
            PLANS_PHOTO_URL,
            caption=SC(PLANS_TEXT),
            reply_markup=plans_keyboard(),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.warning(f"plans photo failed, falling back to text: {e}")
        await m.reply(SC(PLANS_TEXT), reply_markup=plans_keyboard(), parse_mode=ParseMode.HTML)


def status_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [make_button(SC("💎 View Plans"), callback_data="show_plans", style=BTN_PRIMARY)],
        [make_button(SC("📞 Contact Admin"), url=POWERED_BY_URL, style=BTN_PRIMARY)],
    ])


# ---------------------------------------------------------------------
# Referral system — shown when a free user hits the daily download limit
# (which is also what happens the moment their premium expires, since
# they fall back to the same free-limit gating at that point).
# ---------------------------------------------------------------------

REFERRAL_PHOTO_URL = "https://iili.io/n2jHRQn.jpg"
# (referral-count threshold, days of premium granted when that threshold
# is first reached). Rewards stack: reaching 10 grants a *further* 1 day
# on top of the 1 already granted at 5, for 2 days total.
REFERRAL_REWARDS = [(5, 1), (10, 1)]
REFERRAL_GOAL = REFERRAL_REWARDS[-1][0]


async def get_bot_username(client: Client) -> str:
    cached = getattr(client, "_cached_username", None)
    if cached:
        return cached
    me = await client.get_me()
    client._cached_username = me.username
    return me.username or ""


def referral_link(bot_username: str, user_id: int) -> str:
    return f"https://t.me/{bot_username}?start=ref_{user_id}"


def referral_text(referral_count: int) -> str:
    return (
        "🎁 ʀᴇғᴇʀ & ᴇᴀʀɴ ᴘʀᴇᴍɪᴜᴍ\n\n"
        "⚡ ʏᴏᴜʀ ᴅᴀɪʟʏ ᴅᴏᴡɴʟᴏᴀᴅ ʟɪᴍɪᴛ ʜᴀs ʙᴇᴇɴ ʀᴇᴀᴄʜᴇᴅ\n"
        "ᴏʀ ʏᴏᴜʀ ᴘʀᴇᴍɪᴜᴍ ᴘʟᴀɴ ʜᴀs ᴇxᴘɪʀᴇᴅ.\n\n"
        "💎 ᴡᴀɴᴛ ᴛᴏ ɢᴇᴛ ᴘʀᴇᴍɪᴜᴍ ғᴏʀ ғʀᴇᴇ?\n\n"
        "👥 ɪɴᴠɪᴛᴇ ғʀɪᴇɴᴅs ᴜsɪɴɢ ʏᴏᴜʀ ᴜɴɪǫᴜᴇ ʀᴇғᴇʀʀᴀʟ ʟɪɴᴋ.\n\n"
        "🏆 ʀᴇᴡᴀʀᴅs\n\n"
        "🎯 5 ʀᴇғᴇʀʀᴀʟs → 💎 1 ᴅᴀʏ ᴘʀᴇᴍɪᴜᴍ\n"
        "🎯 10 ʀᴇғᴇʀʀᴀʟs → 💎 2 ᴅᴀʏs ᴘʀᴇᴍɪᴜᴍ\n\n"
        f"📊 ʏᴏᴜʀ ʀᴇғᴇʀʀᴀʟs: {referral_count}/{REFERRAL_GOAL}\n\n"
        "🔗 sʜᴀʀᴇ ʏᴏᴜʀ ʟɪɴᴋ & ᴇᴀʀɴ ᴘʀᴇᴍɪᴜᴍ!"
    )


def referral_keyboard(bot_username: str, user_id: int, referral_count: int) -> InlineKeyboardMarkup:
    link = referral_link(bot_username, user_id)
    share_text = quote("🎬 Download faphouse videos free via Telegram — try this bot!")
    share_url = f"https://t.me/share/url?url={quote(link, safe='')}&text={share_text}"
    return InlineKeyboardMarkup([
        [make_button(SC("🔗 Get Referral Link"), callback_data="ref_getlink", style=BTN_PRIMARY)],
        [make_button(SC("📤 Share Referral Link"), url=share_url, style=BTN_PRIMARY)],
        [make_button(SC(f"👥 Referrals: {referral_count}"), callback_data="ref_count", style=BTN_PRIMARY)],
        [make_button(SC("💎 Premium Rewards"), callback_data="ref_rewards", style=BTN_PRIMARY)],
        [make_button(SC("📞 Contact Admin"), url="https://t.me/anujedits97", style=BTN_PRIMARY)],
    ])


async def send_referral_prompt(client: Client, chat_id: int):
    """Shown in place of a plain 'limit reached' message — offers the
    referral photo/text/buttons instead so a blocked user has an
    immediate free path back to downloading."""
    bot_username = await get_bot_username(client)
    referral_count = await get_referral_count(chat_id)
    keyboard = referral_keyboard(bot_username, chat_id, referral_count)
    try:
        await client.send_photo(
            chat_id, REFERRAL_PHOTO_URL,
            caption=SC(referral_text(referral_count)),
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.warning(f"referral photo failed, falling back to text: {e}")
        await client.send_message(
            chat_id, SC(referral_text(referral_count)),
            reply_markup=keyboard, parse_mode=ParseMode.HTML,
        )


@app.on_message(filters.command("referral") & filters.private)
async def referral_cmd(client: Client, m: Message):
    await send_referral_prompt(client, m.from_user.id)


async def _autoupload_target(client: Client, m: Message) -> int | None:
    """Where an auto-upload / retry command posts: DEFAULT_CHANNEL if set, else the chat the command was sent in.

    Checks up front that the bot can really post there. Without this check the worker started anyway, every send
    failed quietly (PEER_ID_INVALID / CHAT_WRITE_FORBIDDEN), each video burned its retries and was then recorded as
    "failed" - so the command looked like it did nothing. Returns None (after telling the admin why) when the target
    cannot be used."""
    target = DEFAULT_CHANNEL or m.chat.id
    if target == m.chat.id:
        return target
    try:
        chat = await client.get_chat(target)
        member = await client.get_chat_member(target, "me")
        is_admin_here = member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
        can_post = getattr(getattr(member, "privileges", None), "can_post_messages", None)
        if str(getattr(chat, "type", "")).lower().endswith("channel"):
            if not is_admin_here or can_post is False:
                raise PermissionError("bot is not an admin with 'Post messages' permission in this channel")
        elif member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER, ChatMemberStatus.MEMBER):
            raise PermissionError("bot is not a member of this group")
        return target
    except Exception as e:
        logger.error(f"[autoupload] target chat {target} is not usable: {e}")
        await m.reply(SC(
            "⚠️ <b>Auto-upload not started - can't post to the upload channel.</b>\n\n"
            f"📍 Channel: <code>{target}</code>\n"
            f"❗ Reason: <code>{html.escape(_strip_ansi(str(e))[:200])}</code>\n\n"
            "• Add the bot to that channel as <b>admin</b> (with <i>Post messages</i>)\n"
            "• Make sure the ID is right - or set the <code>DEFAULT_CHANNEL</code> env variable\n"
            "• Then send the command again (use /retryfailed for videos that already failed)"
        ), parse_mode=ParseMode.HTML)
        return None


async def _scraper_reset_state(target_chat: int, mode: str | None,
                                display_key: str | None = None, display_value: str | None = None) -> dict:
    """Builds the dict to pass to set_chat_scraper_state() when
    (re)starting an auto-upload job for target_chat.

    Always resets this run's *_total_scraped counters — a session's
    "Total uploaded: N" should count from 0 each time it starts. But the
    *_current_page fields are only reset when this is genuinely a NEW
    target (different actor/category/studio, or switching in/out of
    plain full-site mode). If it's the SAME target as whatever's already
    saved for this chat — e.g. resuming after a bot restart by resending
    the exact /autoupload <name> the restart notice told them to —
    page fields are left OUT of the returned dict entirely.
    set_chat_scraper_state() does a Mongo $set (merge), not a full
    replace, so omitting a key here leaves whatever's already saved in
    place, which is exactly what actor_uploader_worker/the category and
    studio workers' own resuming_same_* checks expect to find.

    BUG this fixes: every one of these command handlers used to
    unconditionally reset current_page (and every other site's
    *_current_page) back to 1 on EVERY call, including a resume — which
    stomped the saved position before the worker's own resume-detection
    logic ever got a chance to see it. That's why "the bot restarted, I
    resent the same /autoupload command like the restart notice said to,
    and it started over from page 1 instead of continuing."

    mode=None means "plain full-site scrape" (chat_uploader_worker,
    which has no actor/category/studio target of its own) — pass
    display_key/display_value only when mode is "actor"/"category"/
    "studio"."""
    existing = await get_chat_scraper_state(target_chat)
    if mode is None:
        same_target = not existing.get("mode")
    else:
        same_target = existing.get("mode") == mode and existing.get(display_key) == display_value

    state = {
        "is_running": True,
        "total_scraped": 0, "eporner_total_scraped": 0,
        "pornhub_total_scraped": 0, "xhamster_total_scraped": 0,
        "xvideos_total_scraped": 0, "fpo_total_scraped": 0,
    }
    if not same_target:
        state.update({
            "current_page": 1, "eporner_current_page": 1,
            "pornhub_current_page": 1, "ph_model_current_page": 1,
            "ph_studio_current_page": 1, "xhamster_current_page": 1,
            "xh_model_current_page": 1, "xh_studio_current_page": 1,
            "xvideos_current_page": 1, "xv_model_current_page": 1,
            "xv_studio_current_page": 1, "fpo_current_page": 1,
        })
    return state


@app.on_message(filters.command("autoupload"))
async def autoupload_cmd(client: Client, m: Message):
    """Admin-only: starts scraping faphouse.com's /videos listing and
    bulk-uploading every not-yet-seen video into the configured channel.

    If an actor/actress name is given after the command
    (e.g. "/autoupload Comatozze"), it uploads every video on every page
    of that performer's own page instead of the whole site."""
    if not m.from_user or m.from_user.id not in ADMINS:
        return

    target_chat = await _autoupload_target(client, m)
    if target_chat is None:
        return
    parts = (m.text or "").split(maxsplit=1)
    actor_name = parts[1].strip() if len(parts) > 1 else ""

    await auto_scraper.stop_worker_task(target_chat, auto_scraper.FAPHOUSE_FAMILY)

    if actor_name:
        # ── Discovery PEHLE karo (command chat mein reply milega) ──
        status_msg = await m.reply(
            SC(f"🔍 <b>Looking up \"{html.escape(actor_name)}\" on faphouse...</b>"),
            parse_mode=ParseMode.HTML
        )
        try:
            # discover_actor_paths() has its own per-request timeouts, but
            # those only bound EACH network call — an unlucky pileup, a
            # slow DNS resolution, or genuinely bad luck could still add
            # up to a long wait with no feedback. This wall-clock timeout
            # is the outer safety net: past 90s, tell the user instead of
            # leaving "Looking up..." sitting there with no way to tell a
            # slow lookup apart from a stuck one.
            try:
                actor_paths = await asyncio.wait_for(
                    auto_scraper.discover_actor_paths(actor_name), timeout=90,
                )
            except asyncio.TimeoutError:
                await status_msg.edit_text(SC(
                    f"⚠️ <b>Lookup for \"{html.escape(actor_name)}\" timed out.</b>\n\n"
                    "faphouse (or a mirror) may be slow/unreachable right now — try again in a bit."
                ), parse_mode=ParseMode.HTML)
                return

            if not actor_paths:
                # Command chat mein error reply karo (DM/group jahan command diya)
                await status_msg.edit_text(SC(
                    f"⚠️ <b>Performer \"{html.escape(actor_name)}\" not found.</b>\n\n"
                    "• Check spelling (e.g. exact stage name)\n"
                    "• Try a different variation of the name\n"
                    "• The performer may not have a page on faphouse yet"
                ), parse_mode=ParseMode.HTML)
                return

            # Found — paths dikhao aur worker start karo
            sites_found = ", ".join(actor_paths.keys())
            paths_found = ", ".join(f"{b}{p}" for b, p in actor_paths.items())
            logger.info(f"[autoupload] actor paths found: {paths_found}")

            await auto_scraper.begin_site_run(
                target_chat, "actor",
                await _scraper_reset_state(target_chat, "actor", "actor_display", actor_name),
            )
            started = auto_scraper.start_actor_worker_task(
                client, target_chat, m.from_user.id, actor_name,
                is_admin=(m.from_user.id in ADMINS),
                prefound_paths=actor_paths,
            )
            if started:
                await status_msg.edit_text(SC(
                    f"🚀 <b>Auto-upload started for \"{html.escape(actor_name)}\".</b>\n\n"
                    f"📍 Found on: <code>{sites_found}</code>\n\n"
                    "Every video on this performer's page (all pages) is scraped and "
                    "uploaded to the configured channel/group as it's found.\n"
                    "Use /stopupload to stop."
                ), parse_mode=ParseMode.HTML)
            else:
                await status_msg.edit_text(SC("⚠️ <b>Couldn't start — try again in a moment.</b>"), parse_mode=ParseMode.HTML)
        except Exception as e:
            # Catch-all: whatever broke (DB write, worker start, an
            # unexpected scraper error) still gets a reply instead of
            # leaving the "Looking up..." message stuck forever with no
            # sign anything went wrong — this was the actual bug: any
            # exception in this block used to propagate up and get
            # swallowed by pyrogram's dispatcher with no user-facing reply.
            logger.error(f"[autoupload] actor lookup/start failed for \"{actor_name}\": {e}", exc_info=True)
            try:
                await status_msg.edit_text(SC(
                    "⚠️ <b>Something went wrong looking that up.</b> Try again in a moment."
                ), parse_mode=ParseMode.HTML)
            except Exception:
                pass
        return

    await auto_scraper.begin_site_run(
                target_chat, "faphouse",
                await _scraper_reset_state(target_chat, None),
            )
    started = auto_scraper.start_chat_worker_task(
        client, target_chat, m.from_user.id, is_admin=(m.from_user.id in ADMINS),
    )
    if started:
        await m.reply(SC(
            "🚀 <b>Auto-upload started.</b>\n\n"
            "Videos are scraped page by page and uploaded to the configured channel/group as they're found. "
            "Use /stopupload to stop, or /pending to see what's queued up."
        ), parse_mode=ParseMode.HTML)
    else:
        await m.reply(SC("⚠️ <b>Couldn't start — try again in a moment.</b>"), parse_mode=ParseMode.HTML)


def _eporner_term_from_input(text: str) -> str:
    """Accepts either a plain search term/model name OR a link — a
    pornstar page (eporner.com/pornstar/riley-reid/), a video page, or
    really any URL — and returns something usable as a search keyword
    either way. For a URL, this takes the last non-empty path segment
    (the model/video slug) and turns its hyphens/underscores into spaces
    ("riley-reid" -> "riley reid") since that reads as a name/keyword the
    same official search API (eporner_scraper.get_model_page_videos)
    already uses for plain typed names — no separate link-specific
    lookup needed, just feeding it a cleaner query."""
    text = text.strip()
    if not text.lower().startswith(("http://", "https://")):
        return text
    from urllib.parse import urlparse
    path = urlparse(text).path.strip("/")
    segments = [s for s in path.split("/") if s]
    if not segments:
        return text
    # For an eporner.com video URL (/video-<id>/<title-slug>/), the id
    # segment isn't a useful search term — prefer the slug after it.
    term = segments[-1]
    if re.fullmatch(r"video-[A-Za-z0-9]+", segments[0], re.IGNORECASE) and len(segments) > 1:
        term = segments[-1]
    return term.replace("-", " ").replace("_", " ").strip()


@app.on_message(filters.command("autouploadfpo"))
async def autouploadfpo_cmd(client: Client, m: Message):
    """Admin-only: same idea as /autouploadeporner, sourced from fpo.xxx
    instead (see fpo_uploader_worker in auto_scraper.py and
    fpo_scraper.py). Unlike eporner's official search API, fpo.xxx has no
    keyword search at all — the argument here has to be an actual
    performer name/page (it maps to /models/<slug>/, same as
    fpo_downloader.MODEL_PATH_RE's own link-detection), not an arbitrary
    tag or keyword the way eporner's is. No studio command either — the
    site has no separate studio/channel concept (see fpo_scraper.py's
    docstring).

    "/autouploadfpo" alone: continuously uploads random fpo.xxx videos.
    No natural end — runs until /stopupload.
    "/autouploadfpo <performer name or model page link>": pages through
    that performer's videos in order until there are no more."""
    if not m.from_user or m.from_user.id not in ADMINS:
        return

    target_chat = await _autoupload_target(client, m)
    if target_chat is None:
        return
    parts = (m.text or "").split(maxsplit=1)
    term = parts[1].strip() if len(parts) > 1 else ""

    await auto_scraper.stop_worker_task(target_chat, "fpo")
    await auto_scraper.begin_site_run(target_chat, "fpo")

    started = auto_scraper.start_fpo_worker_task(
        client, target_chat, m.from_user.id, term or None,
        is_admin=(m.from_user.id in ADMINS),
    )
    if not started:
        await m.reply(SC("⚠️ <b>Couldn't start — try again in a moment.</b>"), parse_mode=ParseMode.HTML)
        return

    if term:
        await m.reply(SC(
            f"🚀 <b>FPO auto-upload started for \"{term}\".</b>\n\n"
            "Every video on this performer's page (all pages) is scraped and "
            "uploaded to the configured channel/group as it's found.\nUse /stopupload to stop."
        ), parse_mode=ParseMode.HTML)
    else:
        await m.reply(SC(
            "🚀 <b>FPO auto-upload started (random mode).</b>\n\n"
            "Random videos are continuously scraped and uploaded to the "
            "configured channel/group — this has no natural stopping point "
            "since each batch is independently random.\nUse /stopupload to stop."
        ), parse_mode=ParseMode.HTML)


@app.on_message(filters.command("autouploadeporner"))
async def autouploadeporner_cmd(client: Client, m: Message):
    """Admin-only: same idea as /autoupload, sourced from eporner.com
    instead of faphouse (see eporner_uploader_worker in auto_scraper.py
    and eporner_scraper.py). eporner's official search API has no
    separate tag/studio endpoint — just keyword search — so a performer
    name, a tag, or any other keyword all work the same way here. A link
    (a pornstar page, a video page, any eporner URL) works too —
    _eporner_term_from_input() pulls a usable search term out of it.

    "/autouploadeporner" alone: continuously uploads random eporner
    videos. No natural end (each cycle is independently random by
    design) — runs until /stopupload.
    "/autouploadeporner <term or link>": pages through that keyword's
    search results in order until there are no more, same as a faphouse
    actor page under plain /autoupload."""
    if not m.from_user or m.from_user.id not in ADMINS:
        return

    target_chat = await _autoupload_target(client, m)
    if target_chat is None:
        return
    parts = (m.text or "").split(maxsplit=1)
    raw_input = parts[1].strip() if len(parts) > 1 else ""
    term = _eporner_term_from_input(raw_input) if raw_input else ""

    await auto_scraper.stop_worker_task(target_chat, "eporner")
    await auto_scraper.begin_site_run(target_chat, "eporner")

    started = auto_scraper.start_eporner_worker_task(
        client, target_chat, m.from_user.id, term or None,
        is_admin=(m.from_user.id in ADMINS),
    )
    if not started:
        await m.reply(SC("⚠️ <b>Couldn't start — try again in a moment.</b>"), parse_mode=ParseMode.HTML)
        return

    if term:
        await m.reply(SC(
            f"🚀 <b>Eporner auto-upload started for \"{term}\".</b>\n\n"
            "Every matching video (all pages) is scraped and uploaded to the "
            "configured channel/group as it's found.\nUse /stopupload to stop."
        ), parse_mode=ParseMode.HTML)
    else:
        await m.reply(SC(
            "🚀 <b>Eporner auto-upload started (random mode).</b>\n\n"
            "Random videos are continuously scraped and uploaded to the "
            "configured channel/group — this has no natural stopping point "
            "since each batch is independently random.\nUse /stopupload to stop."
        ), parse_mode=ParseMode.HTML)


@app.on_message(filters.command("autouploadmat6tube"))
async def autouploadmat6tube_cmd(client: Client, m: Message):
    """Admin-only: auto-upload from mat6tube.com.

    "/autouploadmat6tube" alone: continuously uploads random mat6tube videos.
    "/autouploadmat6tube <model or keyword>": pages through search results.
    Download backend: mat6tube_downloader (direct MP4 — no yt-dlp needed).
    """
    if not m.from_user or m.from_user.id not in ADMINS:
        return

    target_chat = await _autoupload_target(client, m)
    if target_chat is None:
        return
    parts = (m.text or "").split(maxsplit=1)
    term = parts[1].strip() if len(parts) > 1 else ""

    await auto_scraper.stop_worker_task(target_chat, "mat6tube")
    await auto_scraper.begin_site_run(target_chat, "mat6tube")

    started = auto_scraper.start_mat6tube_worker_task(
        client, target_chat, m.from_user.id, term or None,
        is_admin=(m.from_user.id in ADMINS),
    )
    if not started:
        await m.reply(SC("⚠️ <b>Couldn't start — try again in a moment.</b>"), parse_mode=ParseMode.HTML)
        return

    if term:
        await m.reply(SC(
            f"🚀 <b>Mat6tube auto-upload started for \"{term}\".</b>\n\n"
            "Every matching video (all pages) will be scraped and uploaded.\n"
            "Use /stopupload to stop."
        ), parse_mode=ParseMode.HTML)
    else:
        await m.reply(SC(
            "🚀 <b>Mat6tube auto-upload started (random mode).</b>\n\n"
            "Random mat6tube videos will be continuously uploaded.\n"
            "Use /stopupload to stop."
        ), parse_mode=ParseMode.HTML)


@app.on_message(filters.command("autouploadspankbang"))
async def autouploadspankbang_cmd(client: Client, m: Message):
    """Admin-only: auto-upload from spankbang.com -- works like /autoupload <actor> does for FapHouse.

    "/autouploadspankbang <performer name>": looks the performer up FIRST ("Looking up ..."), then uploads every video
    on every page of her pornstar page; if no such performer page exists it says so and starts nothing.
    "/autouploadspankbang <spankbang.com URL>": a search (/s/...), pornstar (/<id>/pornstar/...), channel
    (/<id>/channel/...) or trending / new_videos / most_popular page link -- pages through that listing.
    "/autouploadspankbang trending" (or new / popular): pages through that SpankBang section.
    "/autouploadspankbang search <keyword>": pages through that keyword's search results.
    "/autouploadspankbang" alone: continuously uploads random SpankBang videos.
    Download backend: spankbang_downloader (dedicated layer over the yt-dlp engine).
    The 24/7 new-uploads monitor is off unless SPANKBANG_MONITOR=1.
    """
    if not m.from_user or m.from_user.id not in ADMINS:
        return

    target_chat = await _autoupload_target(client, m)
    if target_chat is None:
        return
    parts = (m.text or "").split(maxsplit=1)
    term = parts[1].strip() if len(parts) > 1 else ""

    await auto_scraper.stop_worker_task(target_chat, "spankbang")

    status_msg = None
    found_path = ""
    if term and not spankbang_scraper.is_direct_term(term):
        # Performer name: discovery FIRST (reply goes to the command chat), exactly like /autoupload <actor>
        status_msg = await m.reply(
            SC(f"🔍 <b>Looking up \"{html.escape(term)}\" on SpankBang...</b>\n"
               "<i>(can take 1-2 min when Cloudflare asks for a check)</i>"), parse_mode=ParseMode.HTML)
        try:
            try:
                found_path = await asyncio.wait_for(spankbang_scraper.find_performer_page(term), timeout=240)
            except asyncio.TimeoutError:
                await status_msg.edit_text(SC(
                    f"⚠️ <b>Lookup for \"{html.escape(term)}\" timed out.</b>\n\n"
                    "SpankBang may be slow/unreachable right now — try again in a minute (a lookup that already finished is remembered, so the retry is usually instant)."
                ), parse_mode=ParseMode.HTML)
                return
            if not found_path:
                await status_msg.edit_text(SC(
                    f"⚠️ <b>Performer \"{html.escape(term)}\" not found.</b>\n\n"
                    "• Check spelling (exact stage name)\n"
                    "• Or paste her SpankBang pornstar page link instead\n"
                    "• For a keyword search use <code>/autouploadspankbang search &lt;keyword&gt;</code>\n"
                    "• SpankBang may also be blocking the server — see the logs"
                ), parse_mode=ParseMode.HTML)
                return
        except Exception as e:
            logger.error(f"[autouploadspankbang] performer lookup failed for {term!r}: {e}", exc_info=True)
            try:
                await status_msg.edit_text(SC("⚠️ <b>Something went wrong looking that up.</b> Try again in a moment."), parse_mode=ParseMode.HTML)
            except Exception:
                pass
            return

    await auto_scraper.begin_site_run(target_chat, "spankbang")
    started = auto_scraper.start_spankbang_worker_task(
        client, target_chat, m.from_user.id, term or None,
        is_admin=(m.from_user.id in ADMINS),
    )
    reply = status_msg.edit_text if status_msg else m.reply
    if not started:
        await reply(SC("⚠️ <b>Couldn't start — try again in a moment.</b>"), parse_mode=ParseMode.HTML)
        return

    if found_path:
        await reply(SC(
            f"🚀 <b>Auto-upload started for \"{html.escape(term)}\".</b>\n\n"
            f"📍 Found on: <code>spankbang.com{html.escape(found_path)}</code>\n\n"
            "Every video on this performer's page (all pages) is scraped and uploaded as it's found.\n"
            "Use /stopupload spankbang to stop."
        ), parse_mode=ParseMode.HTML)
    elif term:
        await reply(SC(
            f"🚀 <b>SpankBang auto-upload started for \"{html.escape(term)}\".</b>\n\n"
            "Every video on that listing (all pages) will be scraped and uploaded.\n"
            "Use /stopupload spankbang to stop."
        ), parse_mode=ParseMode.HTML)
    else:
        await reply(SC(
            "🚀 <b>SpankBang auto-upload started (random mode).</b>\n\n"
            "Random SpankBang videos will be continuously uploaded.\n"
            "Use /stopupload spankbang to stop."
        ), parse_mode=ParseMode.HTML)


@app.on_message(filters.command("autouploadpornhub"))
async def autouploadpornhub_cmd(client: Client, m: Message):
    """Admin-only: same idea as /autouploadeporner, sourced from
    pornhub.com instead (see pornhub_uploader_worker in auto_scraper.py
    and pornhub_scraper.py). Unlike eporner, PornHub has no public search
    API — pornhub_scraper.py lists a pornstar/model's videos via yt-dlp's
    own PornHub playlist support instead, so a name or a pornstar page
    link both work as the argument the same way.

    "/autouploadpornhub" alone: continuously uploads random pornhub
    videos (from PornHub's own most-viewed/top-rated/most-recent
    listings). No natural end — runs until /stopupload.
    "/autouploadpornhub <name or link>": pages through that
    pornstar/model's videos in order until there are no more."""
    if not m.from_user or m.from_user.id not in ADMINS:
        return

    target_chat = await _autoupload_target(client, m)
    if target_chat is None:
        return
    parts = (m.text or "").split(maxsplit=1)
    term = parts[1].strip() if len(parts) > 1 else ""

    await auto_scraper.stop_worker_task(target_chat, "pornhub")
    await auto_scraper.begin_site_run(target_chat, "pornhub")

    started = auto_scraper.start_pornhub_worker_task(
        client, target_chat, m.from_user.id, term or None,
        is_admin=(m.from_user.id in ADMINS),
    )
    if not started:
        await m.reply(SC("⚠️ <b>Couldn't start — try again in a moment.</b>"), parse_mode=ParseMode.HTML)
        return

    if term:
        await m.reply(SC(
            f"🚀 <b>PornHub auto-upload started for \"{term}\".</b>\n\n"
            "Every video on this performer's page (all pages) is scraped and "
            "uploaded to the configured channel/group as it's found.\nUse /stopupload to stop."
        ), parse_mode=ParseMode.HTML)
    else:
        await m.reply(SC(
            "🚀 <b>PornHub auto-upload started (random mode).</b>\n\n"
            "Random videos are continuously scraped and uploaded to the "
            "configured channel/group — this has no natural stopping point "
            "since each batch is independently random.\nUse /stopupload to stop."
        ), parse_mode=ParseMode.HTML)


@app.on_message(filters.command("autouploadxvideos"))
async def autouploadxvideos_cmd(client: Client, m: Message):
    """Admin-only: same idea as /autouploadxhamster, sourced from
    xvideos.com instead (see xvideos_uploader_worker in auto_scraper.py
    and xvideos_scraper.py). XVideos calls a performer's own page a
    "profile" (xvideos.com/profiles/<slug>) rather than pornhub/xhamster's
    "pornstar"/"user" naming, but it's the same kind of page — a name or
    a profile page link both work as the argument the same way.

    "/autouploadxvideos" alone: continuously uploads random xvideos
    videos (from XVideos' own new/best/most-recent listings). No natural
    end — runs until /stopupload.
    "/autouploadxvideos <name or link>": pages through that performer's
    videos in order until there are no more."""
    if not m.from_user or m.from_user.id not in ADMINS:
        return

    target_chat = await _autoupload_target(client, m)
    if target_chat is None:
        return
    parts = (m.text or "").split(maxsplit=1)
    term = parts[1].strip() if len(parts) > 1 else ""

    await auto_scraper.stop_worker_task(target_chat, "xvideos")
    await auto_scraper.begin_site_run(target_chat, "xvideos")

    started = auto_scraper.start_xvideos_worker_task(
        client, target_chat, m.from_user.id, term or None,
        is_admin=(m.from_user.id in ADMINS),
    )
    if not started:
        await m.reply(SC("⚠️ <b>Couldn't start — try again in a moment.</b>"), parse_mode=ParseMode.HTML)
        return

    if term:
        await m.reply(SC(
            f"🚀 <b>XVideos auto-upload started for \"{term}\".</b>\n\n"
            "Every video on this performer's page (all pages) is scraped and "
            "uploaded to the configured channel/group as it's found.\nUse /stopupload to stop."
        ), parse_mode=ParseMode.HTML)
    else:
        await m.reply(SC(
            "🚀 <b>XVideos auto-upload started (random mode).</b>\n\n"
            "Random videos are continuously scraped and uploaded to the "
            "configured channel/group — this has no natural stopping point "
            "since each batch is independently random.\nUse /stopupload to stop."
        ), parse_mode=ParseMode.HTML)


@app.on_message(filters.command("autouploadxvideosstudio"))
async def autouploadxvideosstudio_cmd(client: Client, m: Message):
    """Admin-only: xvideos.com equivalent of /autouploadxhamsterstudio —
    e.g. "/autouploadxvideosstudio SomeStudio". XVideos has separate
    channel pages for studios/networks (xvideos.com/channels/<slug>)
    distinct from a performer's own /profiles/<slug> page —
    xvideos_scraper.get_studio_page_videos() targets that real listing."""
    if not m.from_user or m.from_user.id not in ADMINS:
        return
    target_chat = await _autoupload_target(client, m)
    if target_chat is None:
        return
    parts = (m.text or "").split(maxsplit=1)
    studio_name = parts[1].strip() if len(parts) > 1 else ""
    if not studio_name:
        await m.reply(SC("⚠️ <b>Usage:</b> <code>/autouploadxvideosstudio SomeStudio</code>"), parse_mode=ParseMode.HTML)
        return

    await auto_scraper.stop_worker_task(target_chat, "xvideos")
    await auto_scraper.begin_site_run(target_chat, "xvideos")

    started = auto_scraper.start_xvideos_worker_task(
        client, target_chat, m.from_user.id, studio_name,
        is_admin=(m.from_user.id in ADMINS), mode="studio",
    )
    if started:
        await m.reply(SC(
            f"🚀 <b>XVideos auto-upload started for studio \"{studio_name}\".</b>\n\n"
            "Every video from this studio/channel (all pages) is scraped and "
            "uploaded to the configured channel/group as it's found.\nUse /stopupload to stop."
        ), parse_mode=ParseMode.HTML)
    else:
        await m.reply(SC("⚠️ <b>Couldn't start — try again in a moment.</b>"), parse_mode=ParseMode.HTML)


@app.on_message(filters.command("autouploadxhamster"))
async def autouploadxhamster_cmd(client: Client, m: Message):
    """Admin-only: same idea as /autouploadpornhub, sourced from
    xhamster.com instead (see xhamster_uploader_worker in auto_scraper.py
    and xhamster_scraper.py). Like PornHub, xHamster has no public search
    API — xhamster_scraper.py lists a performer's videos via yt-dlp's own
    XHamster playlist support instead, so a name or a performer page link
    both work as the argument the same way.

    "/autouploadxhamster" alone: continuously uploads random xhamster
    videos (from xHamster's own newest/most-popular/top-rated listings).
    No natural end — runs until /stopupload.
    "/autouploadxhamster <name or link>": pages through that
    performer's videos in order until there are no more."""
    if not m.from_user or m.from_user.id not in ADMINS:
        return

    target_chat = await _autoupload_target(client, m)
    if target_chat is None:
        return
    parts = (m.text or "").split(maxsplit=1)
    term = parts[1].strip() if len(parts) > 1 else ""

    await auto_scraper.stop_worker_task(target_chat, "xhamster")
    await auto_scraper.begin_site_run(target_chat, "xhamster")

    started = auto_scraper.start_xhamster_worker_task(
        client, target_chat, m.from_user.id, term or None,
        is_admin=(m.from_user.id in ADMINS),
    )
    if not started:
        await m.reply(SC("⚠️ <b>Couldn't start — try again in a moment.</b>"), parse_mode=ParseMode.HTML)
        return

    if term:
        await m.reply(SC(
            f"🚀 <b>xHamster auto-upload started for \"{term}\".</b>\n\n"
            "Every video on this performer's page (all pages) is scraped and "
            "uploaded to the configured channel/group as it's found.\nUse /stopupload to stop."
        ), parse_mode=ParseMode.HTML)
    else:
        await m.reply(SC(
            "🚀 <b>xHamster auto-upload started (random mode).</b>\n\n"
            "Random videos are continuously scraped and uploaded to the "
            "configured channel/group — this has no natural stopping point "
            "since each batch is independently random.\nUse /stopupload to stop."
        ), parse_mode=ParseMode.HTML)


@app.on_message(filters.command("autouploadxhamsterstudio"))
async def autouploadxhamsterstudio_cmd(client: Client, m: Message):
    """Admin-only: xhamster.com equivalent of /autouploadpornhubstudio —
    e.g. "/autouploadxhamsterstudio SomeStudio". xHamster has separate
    channel pages for studios/networks (xhamster.com/channels/<slug>)
    distinct from a performer's own /users/<slug>/videos page —
    xhamster_scraper.get_studio_page_videos() targets that real listing."""
    if not m.from_user or m.from_user.id not in ADMINS:
        return
    target_chat = await _autoupload_target(client, m)
    if target_chat is None:
        return
    parts = (m.text or "").split(maxsplit=1)
    studio_name = parts[1].strip() if len(parts) > 1 else ""
    if not studio_name:
        await m.reply(SC("⚠️ <b>Usage:</b> <code>/autouploadxhamsterstudio SomeStudio</code>"), parse_mode=ParseMode.HTML)
        return

    await auto_scraper.stop_worker_task(target_chat, "xhamster")
    await auto_scraper.begin_site_run(target_chat, "xhamster")

    started = auto_scraper.start_xhamster_worker_task(
        client, target_chat, m.from_user.id, studio_name,
        is_admin=(m.from_user.id in ADMINS), mode="studio",
    )
    if started:
        await m.reply(SC(
            f"🚀 <b>xHamster auto-upload started for studio \"{studio_name}\".</b>\n\n"
            "Every video from this studio/channel (all pages) is scraped and "
            "uploaded to the configured channel/group as it's found.\nUse /stopupload to stop."
        ), parse_mode=ParseMode.HTML)
    else:
        await m.reply(SC("⚠️ <b>Couldn't start — try again in a moment.</b>"), parse_mode=ParseMode.HTML)


@app.on_message(filters.command("autouploadpornhubstudio"))
async def autouploadpornhubstudio_cmd(client: Client, m: Message):
    """Admin-only: pornhub.com equivalent of /autouploadstudio — e.g.
    "/autouploadpornhubstudio Brazzers". Unlike /autouploadepornerstudio
    (which is really just a keyword search under studio-flavored
    messaging, since eporner has no separate studio endpoint), PornHub
    genuinely has separate channel pages for studios/networks
    (pornhub.com/channels/<slug>/videos) distinct from a performer's own
    page — pornhub_scraper.get_studio_page_videos() targets that real
    listing, same as faphouse's own /studios/<slug> discovery."""
    if not m.from_user or m.from_user.id not in ADMINS:
        return
    target_chat = await _autoupload_target(client, m)
    if target_chat is None:
        return
    parts = (m.text or "").split(maxsplit=1)
    studio_name = parts[1].strip() if len(parts) > 1 else ""
    if not studio_name:
        await m.reply(SC("⚠️ <b>Usage:</b> <code>/autouploadpornhubstudio Brazzers</code>"), parse_mode=ParseMode.HTML)
        return

    await auto_scraper.stop_worker_task(target_chat, "pornhub")
    await auto_scraper.begin_site_run(target_chat, "pornhub")

    started = auto_scraper.start_pornhub_worker_task(
        client, target_chat, m.from_user.id, studio_name,
        is_admin=(m.from_user.id in ADMINS), mode="studio",
    )
    if started:
        await m.reply(SC(
            f"🚀 <b>PornHub auto-upload started for studio \"{studio_name}\".</b>\n\n"
            "Every video from this studio/channel (all pages) is scraped and "
            "uploaded to the configured channel/group as it's found.\nUse /stopupload to stop."
        ), parse_mode=ParseMode.HTML)
    else:
        await m.reply(SC("⚠️ <b>Couldn't start — try again in a moment.</b>"), parse_mode=ParseMode.HTML)


@app.on_message(filters.command("autouploadepornerstudio"))
async def autouploadepornerstudio_cmd(client: Client, m: Message):
    """Admin-only: eporner.com equivalent of "/autouploadstudio <name>"
    — e.g. "/autouploadepornerstudio Brazzers".

    Honest note on what this actually does, unlike faphouse's version:
    eporner's official API has no dedicated studio/production-company
    listing at all — its docs list exactly three methods (search/id/
    removed), no navigation-by-studio endpoint the way faphouse's own
    site structure has actual /studios/<slug> pages to discover and
    page through (see discover_studio_paths). So this is really the
    same keyword search /autouploadeporner already does, with the
    studio's name as the search term and studio-flavored messaging —
    not a genuinely different, more-precise lookup the way the faphouse
    version is. Kept as its own command anyway for the same one-command-
    per-concept UX faphouse has (/autoupload vs /autouploadtag vs
    /autouploadstudio), even though under the hood it's one shared path."""
    if not m.from_user or m.from_user.id not in ADMINS:
        return
    target_chat = await _autoupload_target(client, m)
    if target_chat is None:
        return
    parts = (m.text or "").split(maxsplit=1)
    raw_input = parts[1].strip() if len(parts) > 1 else ""
    if not raw_input:
        await m.reply(SC("⚠️ <b>Usage:</b> <code>/autouploadepornerstudio Brazzers</code>"), parse_mode=ParseMode.HTML)
        return
    studio_name = _eporner_term_from_input(raw_input)

    await auto_scraper.stop_worker_task(target_chat, "eporner")
    await auto_scraper.begin_site_run(target_chat, "eporner")

    started = auto_scraper.start_eporner_worker_task(
        client, target_chat, m.from_user.id, studio_name, is_admin=(m.from_user.id in ADMINS),
    )
    if started:
        await m.reply(SC(
            f"🚀 <b>Eporner auto-upload started for studio \"{studio_name}\".</b>\n\n"
            "Every matching video (all pages) is scraped and uploaded to the "
            "configured channel/group as it's found.\nUse /stopupload to stop.\n\n"
            "<i>Note: eporner has no dedicated studio listing like faphouse does — "
            "this searches the studio name as a keyword, same as /autouploadeporner.</i>"
        ), parse_mode=ParseMode.HTML)
    else:
        await m.reply(SC("⚠️ <b>Couldn't start — try again in a moment.</b>"), parse_mode=ParseMode.HTML)


@app.on_message(filters.command("stopupload"))
async def stopupload_cmd(client: Client, m: Message):
    if not m.from_user or m.from_user.id not in ADMINS:
        return
    # Must resolve to the SAME chat_id the worker was actually started
    # under (see autoupload_cmd/autouploadtag_cmd) or this looks up the
    # wrong scraper-state record and silently fails to stop anything.
    target_chat = DEFAULT_CHANNEL or m.chat.id
    # "/stopupload" stops every site; "/stopupload eporner" (or xhamster,
    # xvideos, pornhub, mat6tube, spankbang, fpo, faphouse) stops only that one.
    arg = ((m.text or "").split(maxsplit=1) + [""])[1].strip().lower()
    aliases = {"faphouse": auto_scraper.FAPHOUSE_FAMILY, "fap": auto_scraper.FAPHOUSE_FAMILY,
               "eporner": ("eporner",), "mat6tube": ("mat6tube",), "spankbang": ("spankbang",), "sb": ("spankbang",), "pornhub": ("pornhub",),
               "ph": ("pornhub",), "xhamster": ("xhamster",), "xh": ("xhamster",),
               "xvideos": ("xvideos",), "xv": ("xvideos",), "fpo": ("fpo",)}
    only = aliases.get(arg) if arg else None
    if arg and only is None:
        await m.reply(SC("⚠️ <b>Unknown site.</b> Use: faphouse, eporner, mat6tube, spankbang, pornhub, xhamster, xvideos, fpo — or just /stopupload for all."), parse_mode=ParseMode.HTML)
        return
    status_msg = await m.reply(SC("🛑 <b>Stopping auto-upload...</b>"), parse_mode=ParseMode.HTML)
    # Waits for the worker to actually exit (not just flags it to stop),
    # so by the time this replies, /autoupload can be started again right
    # away instead of racing a worker that's still mid-video.
    await auto_scraper.stop_worker_task(target_chat, only)
    await status_msg.edit_text(SC(f"🛑 <b>Auto-upload stopped{(' for ' + arg) if arg else ''}.</b>"), parse_mode=ParseMode.HTML)


@app.on_message(filters.command("autouploadtag"))
async def autouploadtag_cmd(client: Client, m: Message):
    """Admin-only: same idea as "/autoupload <actor name>", but for a
    category/tag page instead of a performer page — e.g. "/autouploadtag
    MILF" or "/autouploadtag Anal" scrapes every page of that category's
    listing and uploads every not-yet-seen video into this chat."""
    if not m.from_user or m.from_user.id not in ADMINS:
        return
    # Same fixed-destination fix as /autoupload — see comment there.
    target_chat = await _autoupload_target(client, m)
    if target_chat is None:
        return
    parts = (m.text or "").split(maxsplit=1)
    tag_name = parts[1].strip() if len(parts) > 1 else ""
    if not tag_name:
        await m.reply(SC("⚠️ <b>Usage:</b> <code>/autouploadtag MILF</code>"), parse_mode=ParseMode.HTML)
        return

    # Same "new command wins over old" behavior as /autoupload — stop
    # whatever's already running for this target first instead of
    # refusing to start.
    await auto_scraper.stop_worker_task(target_chat, auto_scraper.FAPHOUSE_FAMILY)

    await auto_scraper.begin_site_run(
                target_chat, "tag",
                await _scraper_reset_state(target_chat, "category", "category_display", tag_name),
            )
    status_msg = await m.reply(SC(f"🔍 <b>Looking up \"{tag_name}\"...</b>"), parse_mode=ParseMode.HTML)
    started = auto_scraper.start_category_worker_task(
        client, target_chat, m.from_user.id, tag_name, is_admin=(m.from_user.id in ADMINS),
    )
    if started:
        await status_msg.edit_text(SC(
            f"🚀 <b>Auto-upload started for \"{tag_name}\".</b>\n\n"
            "Every video in this category/tag (all pages) is scraped and "
            "uploaded to the configured channel/group as it's found. Use /stopupload to stop."
        ), parse_mode=ParseMode.HTML)
    else:
        await status_msg.edit_text(SC("⚠️ <b>Couldn't start — try again in a moment.</b>"), parse_mode=ParseMode.HTML)


@app.on_message(filters.command("autouploadstudio"))
async def autouploadstudio_cmd(client: Client, m: Message):
    """Admin-only: same idea as "/autouploadtag <tag>", but for a studio/
    production-company page — e.g. "/autouploadstudio PureTaboo" scrapes
    every page of that studio's listing and uploads every not-yet-seen
    video into this chat."""
    if not m.from_user or m.from_user.id not in ADMINS:
        return
    target_chat = await _autoupload_target(client, m)
    if target_chat is None:
        return
    parts = (m.text or "").split(maxsplit=1)
    studio_name = parts[1].strip() if len(parts) > 1 else ""
    if not studio_name:
        await m.reply(SC("⚠️ <b>Usage:</b> <code>/autouploadstudio PureTaboo</code>"), parse_mode=ParseMode.HTML)
        return

    await auto_scraper.stop_worker_task(target_chat, auto_scraper.FAPHOUSE_FAMILY)

    await auto_scraper.begin_site_run(
                target_chat, "studio",
                await _scraper_reset_state(target_chat, "studio", "studio_display", studio_name),
            )
    status_msg = await m.reply(SC(f"🔍 <b>Looking up \"{studio_name}\"...</b>"), parse_mode=ParseMode.HTML)
    started = auto_scraper.start_studio_worker_task(
        client, target_chat, m.from_user.id, studio_name, is_admin=(m.from_user.id in ADMINS),
    )
    if started:
        await status_msg.edit_text(SC(
            f"🚀 <b>Auto-upload started for \"{studio_name}\".</b>\n\n"
            "Every video from this studio (all pages) is scraped and "
            "uploaded to the configured channel/group as it's found. Use /stopupload to stop."
        ), parse_mode=ParseMode.HTML)
    else:
        await status_msg.edit_text(SC("⚠️ <b>Couldn't start — try again in a moment.</b>"), parse_mode=ParseMode.HTML)


@app.on_message(filters.command("debughtml"))
async def debughtml_cmd(client: Client, m: Message):
    """Admin-only diagnostic: fetches a link's raw page HTML through the
    bot's own live session (same one used for real downloads) and sends
    it back as a file. For fpo.xxx links this fetches the /embed/<id>
    page (what flashvars extraction actually parses); for faphouse links
    it fetches the video page itself (what M3U8 extraction parses).

    Exists because extraction can start failing when a site changes its
    player markup, and guessing at a fix without seeing the actual
    current HTML is unreliable — this pulls real evidence straight from
    the live site (which this bot can reach) instead."""
    if not m.from_user or m.from_user.id not in ADMINS:
        return
    parts = (m.text or "").split(maxsplit=1)
    url = parts[1].strip() if len(parts) > 1 else ""
    if not url:
        await m.reply(SC("⚠️ <b>Usage:</b> <code>/debughtml &lt;video url&gt;</code>"), parse_mode=ParseMode.HTML)
        return

    status = await m.reply(SC("🔍 <b>Fetching raw HTML...</b>"), parse_mode=ParseMode.HTML)
    if fpo.is_fpo_link(url):
        html = await asyncio.to_thread(fpo.fetch_debug_html, url)
    elif faphouse.is_faphouse_link(url):
        html = await asyncio.to_thread(faphouse.fetch_debug_html, url)
    else:
        await status.edit_text(SC("⚠️ <b>Not a recognized faphouse/fpo.xxx link.</b>"), parse_mode=ParseMode.HTML)
        return

    if not html:
        await status.edit_text(SC("❌ <b>Fetch failed</b> — nothing came back. Check the logs for the underlying error."), parse_mode=ParseMode.HTML)
        return

    debug_path = os.path.join(DOWNLOAD_DIR, "debughtml_requested.html")
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    with open(debug_path, "w", encoding="utf-8", errors="replace") as f:
        f.write(html)
    await status.delete()
    await m.reply_document(debug_path, caption=SC(f"📄 <b>{len(html)} chars</b> — raw HTML for:\n<code>{url}</code>"), parse_mode=ParseMode.HTML)


@app.on_message(filters.command("getdebughtml"))
async def getdebughtml_cmd(client: Client, m: Message):
    """Admin-only: sends whichever auto-captured failing-page HTML exists
    on disk — faphouse_downloader.py and fpo_downloader.py each save the
    last page that failed extraction automatically (see their
    _DEBUG_HTML_PATH), so this retrieves that without needing to
    reproduce the failure live via /debughtml first."""
    if not m.from_user or m.from_user.id not in ADMINS:
        return
    candidates = [
        ("Faphouse M3U8 extraction", faphouse._DEBUG_HTML_PATH),
        ("fpo.xxx flashvars extraction", fpo._DEBUG_HTML_PATH),
        ("fpo.xxx listing/get_latest_videos", fpo._LISTING_DEBUG_HTML_PATH),
    ]
    sent_any = False
    for label, path in candidates:
        if os.path.exists(path):
            await m.reply_document(path, caption=SC(f"📄 <b>Last capture — {label}</b>"), parse_mode=ParseMode.HTML)
            sent_any = True
    if not sent_any:
        await m.reply(SC("✅ <b>No captured failures on disk right now.</b>"), parse_mode=ParseMode.HTML)


@app.on_message(filters.command("setcookies"))
async def setcookies_cmd(client: Client, m: Message):
    """Admin-only: sets the fpo.xxx session cookies used to reach
    member-only/private videos (see fpo_downloader.py's module docstring
    — this is browser session-cookie reuse, not a username/password,
    since fpo.xxx's login form is behind Cloudflare Turnstile).

    Accepts the cookie string three ways, in this priority order:
      1. Inline, right after the command: /setcookies name=value; ...
      2. As a reply to a text message containing the cookie string (handy
         when it's long enough that typing it after /setcookies directly
         is awkward).
      3. As a reply to an uploaded .txt document — a Netscape-format
         cookies.txt export (e.g. from a "Get cookies.txt" browser
         extension).
    Either the raw "name=value; name2=value2" header form or a full
    Netscape cookies.txt export works — fpo_downloader.set_cookies()
    auto-detects which.

    Takes effect immediately (no restart needed) and is saved to the
    database so it survives one — main.py reloads it into fpo_downloader
    on startup, see the bottom of this file."""
    if not m.from_user or m.from_user.id not in ADMINS:
        return

    parts = (m.text or "").split(maxsplit=1)
    raw = parts[1].strip() if len(parts) > 1 else ""
    had_inline_secret = bool(raw)  # cookies were typed directly into this command message

    reply = m.reply_to_message
    if not raw and reply and reply.document:
        status = await m.reply(SC("📥 <b>Reading cookies file...</b>"), parse_mode=ParseMode.HTML)
        doc_path = await client.download_media(reply.document, file_name=os.path.join(DOWNLOAD_DIR, "setcookies_upload.txt"))
        try:
            with open(doc_path, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read()
        finally:
            try:
                os.remove(doc_path)
            except OSError:
                pass
        await status.delete()
    elif not raw and reply and reply.text:
        raw = reply.text.strip()

    if not raw:
        await m.reply(
            SC(
                "⚠️ <b>Usage:</b>\n"
                "<code>/setcookies name=value; name2=value2</code>\n\n"
                "Or reply to a text message / uploaded .txt file containing "
                "the cookie string with just <code>/setcookies</code>.\n\n"
                "ℹ️ fpo.xxx cookies are tied to the IP they were created from (kt_acctoken). Cookies from your "
                "phone/PC usually won't unlock private videos on a server with a different IP - "
                "prefer <code>/setfpologin</code> (logs in from the server itself)."
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    count = fpo.set_cookies(raw)
    await set_bot_setting("fpo_cookies", raw)

    if had_inline_secret:
        # The cookie string is a live session token — don't leave it
        # sitting in plain chat history any longer than necessary.
        try:
            await m.delete()
        except Exception:
            pass

    if count == 0:
        await m.reply(
            SC(
                "⚠️ <b>Saved, but 0 cookies were parsed out of that.</b>\n"
                "Double-check it's either a <code>name=value; ...</code> header string "
                "or a full Netscape cookies.txt export — private videos won't work until this parses correctly."
            ),
            parse_mode=ParseMode.HTML,
        )
    else:
        await m.reply(
            SC(f"✅ <b>fpo.xxx cookies updated</b> — {count} cookie(s) attached. Takes effect immediately."),
            parse_mode=ParseMode.HTML,
        )


@app.on_message(filters.command("setfpologin"))
async def setfpologin_cmd(client: Client, m: Message):
    """Admin-only: /setfpologin <username> <password> - the fpo.xxx account the bot logs in with (like the
    FapHouse EMAIL/PASSWORD login). Saved in the database, applied immediately, and the message holding the
    password is deleted. Alternatively set the FPO_USERNAME / FPO_PASSWORD env vars."""
    if not m.from_user or m.from_user.id not in ADMINS:
        return
    parts = (m.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].strip() or not parts[2].strip():
        await m.reply(SC("⚠️ <b>Usage:</b> <code>/setfpologin username password</code>"), parse_mode=ParseMode.HTML)
        return
    user, pw = parts[1].strip(), parts[2].strip()
    fpo.set_credentials(user, pw)
    await set_bot_setting("fpo_login", json.dumps({"u": user, "p": pw}))
    try:
        await m.delete()                       # don't leave the password in chat history
    except Exception:
        pass
    status = await m.reply(SC("🔐 <b>Saved. Logging in to fpo.xxx...</b>"), parse_mode=ParseMode.HTML)
    ok = await asyncio.get_event_loop().run_in_executor(None, lambda: fpo.login(force=True))
    st = fpo.get_login_status()
    if ok:
        await status.edit_text(SC(f"✅ <b>fpo.xxx login works</b> as <code>{st['username']}</code> — "
                                  f"{st['cookies']} cookie(s) active."), parse_mode=ParseMode.HTML)
    else:
        await status.edit_text(SC(f"⚠️ <b>Credentials saved, but login failed:</b> {st['message']}\n"
                                  "The bot keeps using /setcookies cookies if you set them, and retries the login later."),
                               parse_mode=ParseMode.HTML)


@app.on_message(filters.command("fpologin"))
async def fpologin_cmd(client: Client, m: Message):
    """Admin-only: forces a fresh fpo.xxx login right now and reports the result (never prints the password)."""
    if not m.from_user or m.from_user.id not in ADMINS:
        return
    if not fpo.has_credentials():
        await m.reply(SC("⚠️ <b>No fpo.xxx login set.</b> Use <code>/setfpologin username password</code> "
                         "(or the FPO_USERNAME / FPO_PASSWORD env vars)."), parse_mode=ParseMode.HTML)
        return
    status = await m.reply(SC("🔐 <b>Logging in to fpo.xxx...</b>"), parse_mode=ParseMode.HTML)
    ok = await asyncio.get_event_loop().run_in_executor(None, lambda: fpo.login(force=True))
    st = fpo.get_login_status()
    if ok:
        await status.edit_text(SC(f"✅ <b>Logged in</b> as <code>{st['username']}</code> — {st['cookies']} cookie(s) active."),
                               parse_mode=ParseMode.HTML)
    else:
        await status.edit_text(SC(f"❌ <b>Login failed:</b> {st['message']}"), parse_mode=ParseMode.HTML)


@app.on_message(filters.command("fpotoken"))
async def fpotoken_cmd(client: Client, m: Message):
    """Admin-only: /fpotoken <token> - logs the bot in to fpo.xxx using a Turnstile token YOU solved in a real browser
    (for when Cloudflare refuses the server's own browser). The bot does the login POST from this server, so the
    resulting cookies are valid for the server's IP."""
    if not m.from_user or m.from_user.id not in ADMINS:
        return
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await m.reply(
            SC("🔑 <b>Login with a token you solve yourself</b>\n\n"
               "1. On your phone/PC open <code>https://www.fpo.xxx</code> and tap <b>Log in</b>. Type your username/password "
               "and wait until the captcha shows its tick.\n"
               "2. In the address bar type <code>javascript:</code> and right after it paste: "
               "<code>prompt('t',document.querySelector('[name=cf-turnstile-response]').value)</code> then Go "
               "(type the word <code>javascript:</code> yourself - browsers drop it when pasted).\n"
               "3. Copy the long text from the box and send it within 5 minutes:\n"
               "<code>/fpotoken THE_TOKEN</code>\n\n"
               "Do NOT press Log in on the page afterwards - a token works once."),
            parse_mode=ParseMode.HTML)
        return
    token = parts[1].strip()
    try:
        await m.delete()
    except Exception:
        pass
    if not fpo.has_credentials():
        await m.reply(SC("⚠️ <b>No fpo.xxx login set.</b> Use <code>/setfpologin username password</code> first."),
                      parse_mode=ParseMode.HTML)
        return
    status = await m.reply(SC("🔐 <b>Logging in to fpo.xxx with your token...</b>"), parse_mode=ParseMode.HTML)
    ok, msg = await asyncio.get_event_loop().run_in_executor(None, lambda: fpo.login_with_token(token))
    st = fpo.get_login_status()
    if ok:
        await status.edit_text(SC(f"✅ <b>Logged in</b> as <code>{st['username']}</code> — {st['cookies']} cookie(s) active."),
                               parse_mode=ParseMode.HTML)
    else:
        await status.edit_text(SC(f"❌ <b>Token login failed:</b> {html.escape(msg)}"), parse_mode=ParseMode.HTML)


@app.on_message(filters.command("cookiestatus"))
async def cookiestatus_cmd(client: Client, m: Message):
    """Admin-only: reports how many fpo.xxx cookies are currently active,
    without ever printing the cookie values themselves in chat (they're
    live session tokens — logging them out would be a real credential
    leak, not just a debugging convenience)."""
    if not m.from_user or m.from_user.id not in ADMINS:
        return
    count = fpo.get_cookie_count()
    st = fpo.get_login_status()
    if st["has_credentials"]:
        age = f", {st['age_min']} min old" if st["age_min"] is not None else ""
        login_line = (f"🔐 Login <code>{st['username']}</code>: " +
                      (f"✅ active{age}" if st["ok"] else f"❌ {st['message']}"))
    else:
        login_line = "🔐 Login: not set (/setfpologin username password)"
    if count == 0:
        await m.reply(
            SC("⚠️ <b>No fpo.xxx cookies active</b> — private/member-only videos aren't reachable.\n"
               f"{login_line}\nUse /setfpologin (best - cookies get the server's IP) or /setcookies."),
            parse_mode=ParseMode.HTML,
        )
    else:
        await m.reply(SC(f"✅ <b>{count} fpo.xxx cookie(s) currently active.</b>\n{login_line}"), parse_mode=ParseMode.HTML)


@app.on_message(filters.command(["delcookies", "clearcookies"]))
async def delcookies_cmd(client: Client, m: Message):
    """Admin-only: deletes every stored fpo.xxx cookie - the /setcookies ones (memory + database) and the cookies of
    the last username/password login (memory + saved file). Takes effect immediately, no restart needed."""
    if not m.from_user or m.from_user.id not in ADMINS:
        return
    res = fpo.clear_cookies()
    try:
        await delete_bot_setting("fpo_cookies")
    except Exception as e:
        logger.warning(f"/delcookies: couldn't remove the saved cookies from the database: {e}")
        await m.reply(SC("⚠️ <b>Cleared from memory, but the database copy could not be removed</b> — "
                         "they would come back after a restart. Try /delcookies again."), parse_mode=ParseMode.HTML)
        return
    lines = [f"🗑 <b>fpo.xxx cookies deleted</b>",
             f"• /setcookies cookies: {res['manual']}",
             f"• Login-session cookies: {res['login']}"]
    if os.environ.get("FPO_COOKIES", "").strip():
        lines.append("\n⚠️ The <code>FPO_COOKIES</code> environment variable is still set - it will load again after a "
                     "restart. Remove it from your host settings too.")
    lines.append("\nPrivate/member-only videos won't work until you set fresh cookies with /setcookies.")
    await m.reply(SC("\n".join(lines)), parse_mode=ParseMode.HTML)


@app.on_message(filters.command("pending"))
async def pending_cmd(client: Client, m: Message):
    """Admin-only: /pending scans the site-wide listing by default.
    /pending <actor name> instead scopes the scan to just that performer's
    page — e.g. "/pending Mia Khalifa" reports how many of HER videos are
    still un-uploaded, not the whole site's."""
    if not m.from_user or m.from_user.id not in ADMINS:
        return
    parts = (m.text or "").split(maxsplit=1)
    actor_name = parts[1].strip() if len(parts) > 1 else ""

    if actor_name:
        status_msg = await m.reply(SC(f"🔍 <b>Looking up \"{actor_name}\"...</b>"), parse_mode=ParseMode.HTML)
        actor_paths = await auto_scraper.discover_actor_paths(actor_name)
        if not actor_paths:
            await status_msg.edit_text(SC(
                f"⚠️ <b>Couldn't find a page for \"{actor_name}\".</b>\n"
                "Double-check the spelling — or the site may have no matching performer page."
            ), parse_mode=ParseMode.HTML)
            return
        summary = await auto_scraper.get_pending_videos_summary(paths=actor_paths)
        await status_msg.edit_text(
            SC(f"📊 <b>Scan Summary — \"{actor_name}\"</b>\n\n"
               f"🔎 Scanned: {summary['scanned_pages']} page(s), {summary['total_scanned']} video(s)\n"
               f"🆕 Pending (not yet uploaded): {summary['pending_count']}"),
            parse_mode=ParseMode.HTML,
        )
        return

    status_msg = await m.reply(SC("🔍 <b>Scanning faphouse.com's listing...</b>"), parse_mode=ParseMode.HTML)
    summary = await auto_scraper.get_pending_videos_summary()
    await status_msg.edit_text(
        SC(f"📊 <b>Scan Summary</b>\n\n"
           f"🔎 Scanned: {summary['scanned_pages']} page(s), {summary['total_scanned']} video(s)\n"
           f"🆕 Pending (not yet uploaded): {summary['pending_count']}"),
        parse_mode=ParseMode.HTML,
    )


@app.on_message(filters.command("retryskipped"))
async def retryskipped_cmd(client: Client, m: Message):
    """Admin-only: re-attempts every video previously marked
    "skipped_size_limit" — mostly ones skipped before split_upload.py
    existed, back when anything over 2GB was permanently given up on.
    Uploads whatever succeeds into THIS chat, same as /autoupload."""
    if not m.from_user or m.from_user.id not in ADMINS:
        return
    # Same fixed-destination fix as /autoupload — see comment there.
    # (status_msg itself stays in the admin's chat so they can see progress.)
    target_chat = await _autoupload_target(client, m)
    if target_chat is None:
        return
    actor = " ".join(m.command[1:]).strip()
    skipped = await get_skipped_size_limit_videos()
    if actor:
        skipped = filter_by_actor(skipped, actor)
    if not skipped:
        await m.reply(SC(f"✅ <b>Nothing to retry</b> — no skipped videos{f' for {html.escape(actor)}' if actor else ''}."), parse_mode=ParseMode.HTML)
        return

    status_msg = await m.reply(
        SC(f"🔄 <b>Found {len(skipped)} skipped video(s){f' for {html.escape(actor)}' if actor else ''}.</b> Retrying now — this can take a while, "
           f"progress updates below as each one is checked."),
        parse_mode=ParseMode.HTML,
    )
    started = auto_scraper.start_retry_skipped_task(client, target_chat, status_msg, user_id=m.from_user.id, actor=actor)
    if not started:
        await status_msg.edit_text(SC("⚠️ <b>A retry is already running.</b> Wait for it to finish first."), parse_mode=ParseMode.HTML)


@app.on_message(filters.command("retryfailed"))
async def retryfailed_cmd(client: Client, m: Message):
    """Admin-only: re-attempts every video marked status="failed" — ones
    where every download attempt was exhausted (see
    auto_scraper._process_with_retries) and gave up specifically so
    live_site_monitor stops retrying the same broken link every
    MONITOR_INTERVAL forever. Use this after whatever actually broke it
    is believed fixed (e.g. after a faphouse_downloader.py extraction fix) —
    it won't help on its own if the underlying cause is still there."""
    if not m.from_user or m.from_user.id not in ADMINS:
        return
    target_chat = await _autoupload_target(client, m)
    if target_chat is None:
        return
    actor = " ".join(m.command[1:]).strip()
    failed = await get_failed_videos()
    if actor:
        failed = filter_by_actor(failed, actor)
    if not failed:
        await m.reply(SC(f"✅ <b>Nothing to retry</b> — no failed videos{f' for {html.escape(actor)}' if actor else ''}."), parse_mode=ParseMode.HTML)
        return

    status_msg = await m.reply(
        SC(f"🔄 <b>Found {len(failed)} failed video(s){f' for {html.escape(actor)}' if actor else ''}.</b> Retrying now — this can take a while, "
           f"progress updates below as each one is checked."),
        parse_mode=ParseMode.HTML,
    )
    started = auto_scraper.start_retry_failed_task(client, target_chat, status_msg, user_id=m.from_user.id, actor=actor)
    if not started:
        await status_msg.edit_text(SC("⚠️ <b>A retry is already running.</b> Wait for it to finish first."), parse_mode=ParseMode.HTML)


@app.on_callback_query(filters.regex(r"^vstream\|"))
async def video_stream_cb(client: Client, query):
    """Stream button under an auto-uploaded video's caption (see
    build_stream_button_markup). Kept as its own handler/prefix rather
    than reusing the manual flow's stream| callback because that one
    edit_text()s the message it's on — fine for the manual flow's plain
    text menu, but Telegram rejects editMessageText on a message that has
    media (the video itself). Only the BUTTONS are edited here (editMessageReplyMarkup
    works on media messages): Stream -> Open Stream + Back (see video_stream_back_cb).

    Was previously hardcoded to faphouse.client.get_m3u8_url() no matter
    which site the link was actually from — fine for a faphouse-only
    autoupload, but auto_scraper now also handles fpo.xxx and the
    porn_fetch_downloader sites (pornhub, xnxx, etc.), so this needs the
    same per-site branching send_stream_link() (the manual flow's
    equivalent) already does, or it errors/returns nonsense for anything
    that isn't faphouse."""
    link_id = query.data.split("|", 1)[1]
    link = LINK_CACHE.get(link_id)
    if not link:
        # LINK_CACHE lives in memory only (lost on restart) - the video's caption still carries the source link
        try:
            for ent in (query.message.caption_entities or []):
                u = getattr(ent, "url", None)
                if u and faphouse.is_faphouse_link(u):
                    link = u
                    FORCE_STREAM_IDS.add(link_id)
                    break
                if u and _stream_kind(u) not in (None, "other"):
                    link = u
                    break
        except Exception:
            link = None
    if not link:
        await query.answer(SC("⏱ Link expired — try /download again for a fresh one."), show_alert=True)
        return
    await query.answer(SC("Fetching stream link..."))

    async def _notify(text: str):
        # A callback can only be answered once; the "Fetching..." answer above
        # already used it, so later problems go out as a normal message.
        try:
            await client.send_message(query.message.chat.id, SC(text), parse_mode=ParseMode.HTML)
        except Exception:
            pass

    kind = "faphouse" if link_id in FORCE_STREAM_IDS else _stream_kind(link)
    if kind is None:
        # porn_fetch-only sites never expose a raw playable URL.
        await _notify("Stream link isn't available for this site — the file itself was already sent above.")
        return

    fpo_direct = None
    try:
        if kind == "fpo":
            fpo_direct, stream_url = await _fpo_stream_urls(link)
            stream_url = stream_url or fpo_direct
        elif kind == "mat6tube":
            variants = await asyncio.to_thread(mat6tube.get_available_qualities, link)
            stream_url = variants[0]["url"]
        elif kind == "spankbang":
            stream_url = await asyncio.to_thread(spankbang.get_stream_url, link)
            stream_url = player_url(link) or stream_url
        elif kind == "ytdlp":
            # eporner / pornhub / xhamster / xvideos / generic yt-dlp sites:
            # best variant's real playable URL (not a format_id).
            stream_url = await asyncio.to_thread(ytdlp.get_stream_url, link)
            stream_url = player_url(link) or stream_url
        else:
            stream_url = await asyncio.to_thread(faphouse.client.get_m3u8_url, link)

        if not stream_url:
            await _notify("No stream URL found")
            return
        # Turn the post's own Stream button into Open Stream + Back, right where the user tapped (a new message at
        # the bottom of the chat was easy to miss - e.g. it landed under a running progress message).
        try:
            await query.message.edit_reply_markup(InlineKeyboardMarkup(_open_stream_rows(stream_url, link_id)))
            return
        except MessageNotModified:
            return  # tapped twice - the buttons already show Open Stream
        except Exception as e:
            # URL too long for a button (BUTTON_URL_INVALID), or this copy of the post can't be edited (forwarded /
            # backup copy, no edit right): fall back to the old behaviour - a fresh message with the link.
            logger.info(f"[stream-button] in-place edit not possible ({type(e).__name__}: {e}) - sending a new message")
        await _send_message_safe(client, 
            query.message.chat.id,
            SC("<b>Stream Link Ready</b>"),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [make_button(SC("🔗 Open Stream"), url=stream_url, style=BTN_PRIMARY)],
            ]),
        )
    except Exception as e:
        logger.error(f"Stream error (video button): {e}")
        await _notify("Stream link failed")


@app.on_callback_query(filters.regex(r"^ref_getlink$"))
async def ref_getlink_cb(client: Client, query):
    bot_username = await get_bot_username(client)
    link = referral_link(bot_username, query.from_user.id)
    await query.answer()
    await client.send_message(
        query.from_user.id,
        SC(f"🔗 <b>Your referral link:</b>\n\n<code>{link}</code>\n\n"
           "sʜᴀʀᴇ ᴛʜɪs ᴡɪᴛʜ ꜰʀɪᴇɴᴅs — ᴡʜᴇɴ ᴛʜᴇʏ ꜱᴛᴀʀᴛ ᴛʜᴇ ʙᴏᴛ ᴜsɪɴɢ ɪᴛ, ʏᴏᴜ ɢᴇᴛ ᴄʀᴇᴅɪᴛ."),
        parse_mode=ParseMode.HTML,
    )


@app.on_callback_query(filters.regex(r"^ref_count$"))
async def ref_count_cb(client: Client, query):
    count = await get_referral_count(query.from_user.id)
    await query.answer(f"👥 Your referrals: {count}/{REFERRAL_GOAL}", show_alert=True)


@app.on_callback_query(filters.regex(r"^ref_rewards$"))
async def ref_rewards_cb(client: Client, query):
    await query.answer(
        "🎯 5 referrals → 1 day premium\n🎯 10 referrals → 2 days premium",
        show_alert=True,
    )


def link_menu_markup(link_id: str, link: str = None) -> InlineKeyboardMarkup:
    rows = [[make_button(SC("🔽 Download"), callback_data=f"dlq|{link_id}", style=BTN_PRIMARY)]]
    # Hide Stream button for yt-dlp and porn_fetch backends — none of them
    # expose a raw playable URL, only a download() method or a yt-dlp
    # format_id. This includes both HOST_PATTERNS sites (eporner, pornhub
    # etc.) AND generically-supported ones like mat6tube.com — both go
    # through yt-dlp's download() which has no "get raw playable URL" step.
    _is_ytdlp_link = link and not faphouse.is_faphouse_link(link) and (
        ytdlp.is_supported_link(link) or ytdlp.is_generically_supported(link)
    )
    # SpankBang has its own downloader that resolves a real playable URL (spankbang.get_stream_url), so a single SpankBang
    # link gets BOTH Download and Stream even though yt-dlp also matches the host.
    _is_spankbang = bool(link) and spankbang.is_spankbang_link(link)
    if bool(link) and jav_sites.is_jav_link(link):
        pass  # JableTV/MissAV/SupJav/Jav.guru: download only, no raw playable URL
    elif _is_spankbang or not (link and ((pf.is_supported_link(link) and not faphouse.is_faphouse_link(link)) or _is_ytdlp_link)):
        rows.append([make_button(SC("🔗 Stream Link"), callback_data=f"stream|{link_id}", style=BTN_PRIMARY)])
    rows.append([make_button(SC("❌ Cancel"), callback_data=f"cancel|{link_id}", style=BTN_DANGER)])
    return InlineKeyboardMarkup(rows)


FORCE_STREAM_IDS: set = set()  # link ids whose Stream button must work even if the host is not recognised as faphouse


@app.on_callback_query(filters.regex(r"^vback\|"))
async def video_stream_back_cb(client: Client, query):
    """⬅️ Back under an Open Stream button: restores the original 🔗 Stream button on the post."""
    link_id = query.data.split("|", 1)[1]
    try:
        await query.message.edit_reply_markup(InlineKeyboardMarkup(_stream_button_rows(link_id)))
    except MessageNotModified:
        pass
    except Exception as e:
        logger.warning(f"[stream-button] Back could not restore the Stream button: {e}")
    try:
        await query.answer()
    except Exception:
        pass


async def _fpo_stream_urls(link: str):
    """-> (direct_url, proxy_url) for an fpo.xxx video, or (None, None) if it could not be resolved.
    direct_url = the real CDN /get_file/ MP4 link. Its token is tied to the SERVER's IP/session, so it answers
    "HTTP ERROR 403" on the user's own device - never show it as the main button. proxy_url = the same file served
    through the bot's own /stream proxy (adds the Referer/cookies the CDN needs, and fetches a fresh link if the CDN
    ever refuses the old one). Registration lives in fpo_downloader.get_stream_proxy()."""
    info = await asyncio.to_thread(fpo.get_stream_proxy, link)
    if not info:
        return None, None
    return info["url"], info["proxy"]


async def _fpo_stream_url(link: str):
    """The proxied stream URL only (kept for older callers). None if it could not be resolved."""
    return (await _fpo_stream_urls(link))[1]


def _fpo_stream_markup(direct: str, proxy: str) -> InlineKeyboardMarkup:
    """One "Open Stream" button. It points at the bot-server proxy link whenever there is one: the raw CDN /get_file/
    link carries a token (v-acctoken) tied to the SERVER's IP, so on the user's own phone / PC it answers
    "HTTP ERROR 403 - Access denied". The proxy fetches the file from the server (right IP, Referer and cookies) and
    hands it over. The raw link is only used when no proxy link could be made."""
    url = proxy or direct
    rows = []
    if url:
        rows.append([make_button(SC("🔗 Open Stream"), url=url, style=BTN_PRIMARY)])
    return InlineKeyboardMarkup(rows)


def _stream_kind(link: str):
    """Which resolver can turn this page link into something playable.
    Returns "faphouse" / "fpo" / "mat6tube" / "spankbang" / "ytdlp" / "other", or None when
    the site only works through porn_fetch_downloader (no raw playable URL —
    a Stream button would always fail there). Used for BOTH deciding whether
    an auto-uploaded video gets a Stream button and for resolving the click,
    so the two can never disagree (that mismatch is why only FapHouse used to
    get the button)."""
    if faphouse.is_faphouse_link(link):
        return "faphouse"
    if fpo.is_fpo_link(link):
        return "fpo"
    if mat6tube.is_mat6tube_link(link):
        return "mat6tube"
    if spankbang.is_spankbang_link(link):
        return "spankbang"
    if jav_sites.is_jav_link(link):
        return None  # HLS needs site headers/segment handling - no browser-playable raw URL
    if ytdlp.is_supported_link(link) or ytdlp.is_generically_supported(link):
        return "ytdlp"
    if pf.is_supported_link(link):
        return None
    return "other"


def player_url(link: str):
    """Optional web-player wrapper for a stream URL. No hosted player is
    configured in this project, so callers fall back to the raw URL (this
    name was referenced by send_stream_link() but never defined, which made
    the yt-dlp branch there raise NameError)."""
    return None


def build_stream_button_markup(link: str, force: bool = False) -> InlineKeyboardMarkup | None:
    """The 'Stream' button attached under an auto-uploaded video's caption.
    Caches the page URL under a short id (LINK_CACHE) the same way the
    manual /download flow does — Telegram caps callback_data at 64 bytes,
    too small for a full URL — and points at the vstream| callback below
    rather than the manual flow's stream| one, since that one edit_text()s
    the message it's attached to, which fails on a video's caption.

    Returns None for porn_fetch_downloader-backed sites (xnxx, xvideos,
    etc.) and eporner.com/pornhub.com — none of those expose a raw
    playable URL, only a download() method or (for eporner/pornhub) a
    yt-dlp format_id (same reason send_stream_link() shows "Stream link
    isn't available" for them in the manual flow) — so there's nothing a
    Stream button could ever do for them; better to just not show it
    than show one that always fails."""
    # faphouse has its own downloader (faphouse.client.get_m3u8_url gives a real playable stream), so it ALWAYS gets the
    # button. It used to be checked after the yt-dlp test: yt-dlp's "generic" extractor matching also swallowed
    # faphouse links, so the button silently disappeared from every post.
    # force=True: the caller (auto-upload) already knows this link was handled by the faphouse backend
    if not force and _stream_kind(link) is None:
        logger.info(f"[stream-button] no stream button for {link[:80]} (site has no raw playable url)")
        return None

    # No stream button for TeraBox folder links — streaming a folder
    # is meaningless and the resolved URL would be invalid anyway.
    if terabox.is_terabox_link(link):
        try:
            probe = terabox.probe_terabox_share(link)
            if probe and probe.get("is_folder"):
                return None
        except Exception:
            pass

    link_id = uuid.uuid4().hex[:10]
    if len(LINK_CACHE) >= 1000:
        # Auto-upload can add one entry per video over a long bulk run —
        # LINK_CACHE has no expiry elsewhere in the manual flow either, so
        # bound it here to stop unbounded growth on a 24/7 autoupload/
        # monitor session. Oldest-first is a fine approximation since
        # dict insertion order is preserved and old auto-upload links are
        # the least likely to still be clicked.
        LINK_CACHE.pop(next(iter(LINK_CACHE)))
    LINK_CACHE[link_id] = link
    if force:
        if len(FORCE_STREAM_IDS) >= 2000:
            FORCE_STREAM_IDS.clear()
        FORCE_STREAM_IDS.add(link_id)
    return InlineKeyboardMarkup(_stream_button_rows(link_id))


def _stream_button_rows(link_id: str) -> list:
    """Idle state of the button under an auto-uploaded video: a single 🔗 Stream button."""
    return [[make_button(SC("🔗 Stream"), callback_data=f"vstream|{link_id}", style=BTN_PRIMARY)]]


def _open_stream_rows(url: str, link_id: str) -> list:
    """State after Stream was tapped: the SAME button turns into 🔗 Open Stream (the link) with ⬅️ Back below it;
    Back (vback|) puts the plain Stream button back."""
    return [
        [make_button(SC("🔗 Open Stream"), url=url, style=BTN_PRIMARY)],
        [make_button(SC("⬅️ Back"), callback_data=f"vback|{link_id}", style=BTN_DANGER)],
    ]


# ---------------------------------------------------------------------
# Misc helpers: auto-delete, backup channels, admin log
# ---------------------------------------------------------------------

DELETE_NOTICE_PHOTO = "https://iili.io/n2iBQ8F.jpg"
DELETE_NOTICE_TEXT = (
    "Your video / file has been deleted due to restriction.\n\n"
    "if you want to see it again please re download and save.\n\n"
    "आपका विडियो / फाइल डिलीट कर दी गयी है आपको फिर से देखनी है तो फिर से डाउनलोड कर सकते है धन्यवाद!"
)


async def schedule_delete(client: Client, chat_id: int, message_id: int, delete_at: datetime = None):
    """Delete a delivered file after AUTO_DELETE_SECONDS and drop a notice in its place.

    The deadline is persisted in MongoDB (add_pending_delete) before the
    sleep starts. Without this, the deletion lived only in an in-memory
    asyncio.sleep() task — if the bot process restarted for any reason
    (redeploy, crash, Render free-tier spin-down) before the hour was up,
    that task just vanished and the video sat there undeleted forever.
    Now _resume_pending_deletes() replays anything still owed on startup.
    """
    if AUTO_DELETE_SECONDS <= 0:
        return
    if delete_at is None:
        delete_at = datetime.now(timezone.utc) + timedelta(seconds=AUTO_DELETE_SECONDS)
        await add_pending_delete(chat_id, message_id, delete_at)

    if delete_at.tzinfo is None:
        # MongoDB (motor) returns naive datetimes - they are stored as UTC; comparing them with an aware "now"
        # raised "can't subtract offset-naive and offset-aware datetimes" and killed every restored delete task.
        delete_at = delete_at.replace(tzinfo=timezone.utc)
    remaining = (delete_at - datetime.now(timezone.utc)).total_seconds()
    if remaining > 0:
        await asyncio.sleep(remaining)

    try:
        await client.delete_messages(chat_id, message_id)
    except Exception as e:
        # Message may already be gone (user deleted it, chat cleared, etc.) —
        # nothing to notify about in that case.
        logger.warning(f"Auto-delete failed for {chat_id}/{message_id}: {e}")
        await remove_pending_delete(chat_id, message_id)
        return

    await remove_pending_delete(chat_id, message_id)
    try:
        await asyncio.wait_for(
            client.send_photo(
                chat_id=chat_id,
                photo=DELETE_NOTICE_PHOTO,
                caption=DELETE_NOTICE_TEXT,
                parse_mode=ParseMode.HTML,
            ),
            timeout=10,
        )
    except Exception as e:
        logger.warning(f"Couldn't send delete-notice photo to {chat_id}, falling back to text: {e}")
        try:
            await client.send_message(chat_id, SC(DELETE_NOTICE_TEXT), parse_mode=ParseMode.HTML)
        except Exception:
            pass


async def _resume_pending_deletes(client: Client):
    """Called once on startup: replays every auto-delete that was still
    owed when the process last stopped. Anything already past its
    deadline is deleted immediately instead of waiting."""
    if AUTO_DELETE_SECONDS <= 0:
        return
    try:
        pending = await get_all_pending_deletes()
    except Exception as e:
        logger.warning(f"Couldn't load pending auto-deletes: {e}")
        return
    for doc in pending:
        asyncio.create_task(schedule_delete(client, doc["chat_id"], doc["message_id"], doc["delete_at"]))
    if pending:
        logger.info(f"Resumed {len(pending)} pending auto-delete(s) from before restart.")


class _ResumedUser:
    def __init__(self, user_id: int):
        self.id = user_id


class _ResumedQuery:
    """Minimal stand-in for a Pyrogram CallbackQuery, built from the doc
    saved in active_downloads. download_video()/_download_faphouse_video_inner()
    only ever touch query.message and query.from_user.id, so this is enough
    to re-enter that same code path after a restart without a real tap on
    an inline button to hand it."""
    def __init__(self, message, user_id: int):
        self.message = message
        self.from_user = _ResumedUser(user_id)


async def _resume_single_download(client: Client, doc: dict):
    chat_id = doc["chat_id"]
    link = doc["link"]
    quality_url = doc.get("quality_url")
    quality_label = doc.get("quality_label", "Auto (Best)")
    resume_text = SC("<b>🔄 Bot restarted — resuming your download...</b>")

    status_msg = None
    try:
        status_msg = await client.get_messages(chat_id, doc["message_id"])
        if status_msg is None or getattr(status_msg, "empty", False):
            raise ValueError("original status message no longer exists")
        await status_msg.edit_text(resume_text, parse_mode=ParseMode.HTML)
    except Exception:
        # Original status message is gone (deleted, chat cleared, etc.) —
        # send a fresh one rather than silently dropping a download the
        # user is still waiting on.
        try:
            status_msg = await client.send_message(chat_id, resume_text, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.warning(f"Couldn't notify {chat_id} about resumed download, giving up on it: {e}")
            await remove_active_download(chat_id, link, quality_label)
            return

    query = _ResumedQuery(status_msg, chat_id)
    await download_video(client, query, link, quality_url=quality_url, quality_label=quality_label)


async def _resume_active_downloads(client: Client):
    """Called once on startup: automatically restarts every download that
    was still running when the process last stopped (crash, redeploy, host
    restart) instead of leaving the user's "Downloading..." message stuck
    forever with no way to know it silently died."""
    try:
        pending = await get_all_active_downloads()
    except Exception as e:
        logger.warning(f"Couldn't load active downloads: {e}")
        return
    for doc in pending:
        asyncio.create_task(_resume_single_download(client, doc))
    if pending:
        logger.info(f"Resuming {len(pending)} interrupted download(s) from before restart.")


# A channel the sending bot can't reach (CHANNEL_INVALID / not a member / not admin) is skipped for this long, so
# one bad link doesn't produce an error + wasted API call for every delivered file.
BACKUP_RETRY_AFTER = 1800
_backup_bad: dict = {}     # (bot id, channel id) -> time.time() after which we try again
_BACKUP_UNREACHABLE = (ChannelInvalid, ChannelPrivate, ChatAdminRequired, ChatWriteForbidden, PeerIdInvalid)


async def backup_to_linked_channels(client: Client, chat_id: int, message_id: int):
    """Best-effort copy of a delivered file into every linked backup channel —
    both the static ones from config (BACKUP_CHANNEL_IDS) and the ones admins
    have linked dynamically via /set_channel_id. Failures (bot not admin
    there, channel deleted, etc.) are logged and otherwise ignored; this must
    never break the user-facing download flow.

    The copy is made by the bot that delivered the file (`client`: the main bot or a Titanium clone), because the
    message only exists in that bot's chat with the user. So THAT bot must be an admin of the backup channel."""
    try:
        dynamic_ids = await get_channels()
    except Exception as e:
        logger.warning(f"Backup: couldn't read linked channels: {e}")
        dynamic_ids = []
    all_ids = set(BACKUP_CHANNEL_IDS) | set(dynamic_ids)
    me = getattr(client, "me", None)
    bot_id = getattr(me, "id", 0)
    bot_name = f"@{me.username}" if getattr(me, "username", None) else "this bot"
    for channel_id in all_ids:
        key = (bot_id, channel_id)
        if _backup_bad.get(key, 0) > time.time():
            continue
        try:
            await client.copy_message(chat_id=channel_id, from_chat_id=chat_id, message_id=message_id)
            _backup_bad.pop(key, None)
        except FloodWait as e:
            await asyncio.sleep(min(e.value, 60))
            try:
                await client.copy_message(chat_id=channel_id, from_chat_id=chat_id, message_id=message_id)
            except Exception as e2:
                logger.warning(f"Backup to {channel_id} failed after flood wait: {e2}")
        except _BACKUP_UNREACHABLE as e:
            _backup_bad[key] = time.time() + BACKUP_RETRY_AFTER
            logger.warning(
                f"Backup to {channel_id} skipped: {bot_name} can't reach it ({type(e).__name__}). Add {bot_name} to that "
                f"channel as an ADMIN (post messages), or fix/remove the id (/del_channel_id {channel_id}; "
                f"BACKUP_CHANNEL_IDS in the env). Will retry in {BACKUP_RETRY_AFTER // 60} min."
            )
        except Exception as e:
            logger.warning(f"Backup to {channel_id} failed: {e}")


async def forward_to_dump_chat(client: Client, chat_id: int, message_id: int):
    """Best-effort copy of a delivered file into this user's own personal
    dump chat (set via /setchat), if they have one configured."""
    dump_chat = await get_dump_chat(chat_id)
    if not dump_chat:
        return
    try:
        await client.copy_message(chat_id=dump_chat, from_chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logger.warning(f"Dump-chat forward to {dump_chat} for user {chat_id} failed: {e}")


async def log_event(client: Client, text: str):
    if not LOG_CHANNEL_ID:
        return
    try:
        await client.send_message(LOG_CHANNEL_ID, text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning(f"Log event failed: {e}")


CACHE_CHANNEL_HEALTH_CHECK_INTERVAL = 30 * 60  # seconds
_cache_channel_last_ok = True  # tracks state so we alert once on failure, once on recovery


async def _alert_admins(client: Client, text: str):
    """Best-effort DM to every admin, on top of the log channel — cache
    breakage is the kind of thing an admin should notice even if they
    don't have LOG_CHANNEL_ID open."""
    await log_event(client, text)
    for admin_id in ADMINS:
        try:
            await client.send_message(admin_id, text, parse_mode=ParseMode.HTML)
        except Exception:
            pass


async def check_cache_channel_health(client: Client) -> bool:
    """Verify the bot can still post into CACHE_CHANNEL_ID (admin rights
    intact, channel still exists). Previously a removed/deleted cache
    channel would only surface as a per-request warning in the logs —
    this makes the failure visible to admins instead of silent."""
    global _cache_channel_last_ok
    if not CACHE_CHANNEL_ID:
        return True

    ok = False
    reason = ""
    try:
        member = await client.get_chat_member(CACHE_CHANNEL_ID, "me")
        if member.status == ChatMemberStatus.ADMINISTRATOR:
            ok = member.privileges is None or getattr(member.privileges, "can_post_messages", True)
            if not ok:
                reason = "bot is admin in the cache channel but lacks post-message permission"
        elif member.status == ChatMemberStatus.OWNER:
            ok = True
        else:
            reason = f"bot is no longer an admin in the cache channel (status: {member.status})"
    except Exception as e:
        reason = f"cache channel unreachable ({e})"

    if ok and not _cache_channel_last_ok:
        await _alert_admins(client, "✅ <b>Cache channel recovered</b> — caching is back to normal.")
    elif not ok and _cache_channel_last_ok:
        await _alert_admins(
            client,
            "🚨 <b>Cache channel broken!</b>\n\n"
            f"Reason: {reason}\n\n"
            "Every cache write/hit that needs a fresh file_reference will fail "
            "silently until this is fixed (bot removed as admin, or the "
            "channel was deleted). Re-add the bot as admin, or update "
            "CACHE_CHANNEL_ID and restart.",
        )
    _cache_channel_last_ok = ok
    return ok


async def cache_channel_health_check_loop(client: Client):
    if not CACHE_CHANNEL_ID:
        return
    while True:
        try:
            await check_cache_channel_health(client)
        except Exception as e:
            logger.warning(f"Cache-channel health check loop error: {e}")
        await asyncio.sleep(CACHE_CHANNEL_HEALTH_CHECK_INTERVAL)


# ---------------------------------------------------------------------
# Random reaction on /start or any command
# ---------------------------------------------------------------------

REACTIONS = [
    # ── Telegram Official Reactions ──────────────────────
    "👍", "👎", "❤️", "🔥", "🥰", "👏", "😁", "🤔",
    "🤯", "😱", "🤬", "😢", "🎉", "🤩",
    "🙏", "👌", "🕊", "🤡", "🥱", "🥴", "😍", "🐳",
    "❤️‍🔥", "🌚", "🌭", "💯", "🤣", "⚡", "🍌", "🏆",
    "💔", "🤨", "😐", "🍓", "🍾", "💋", "😈", "😴",
    "😭", "🤓", "👻", "👨‍💻", "👀", "🎃", "🙈", "😇",
    "😨", "🤝", "✍", "🤗", "🫡", "🎅", "🎄", "☃",
    "💅", "🤪", "🗿", "🆒", "💘", "🙉", "🦄", "😘",
    "💊", "🙊", "😎", "👾", "🤷‍♂️", "🤷‍♀️", "😡",
    # ── Premium / Money / Diamond vibes ──────────────────
    "💎", "👑", "💰", "🪙", "💵", "💴", "💶", "💷",
    "💸", "💳", "🏦", "🤑", "💹", "📈", "🏅", "🥇",
    "🎖", "⚜️", "🔱", "♾️",
    # ── Fire / Energy / Power ────────────────────────────
    "🌟", "✨", "💫", "🌠", "☄️", "💥", "⭐", "🌙",
    "🌈", "🪄", "🎯", "🛡", "🚀", "⚔️", "🗡", "🔥",
    # ── Cute / Fun ───────────────────────────────────────
    "🥹", "🫶", "🫠", "🫣", "🥺", "🤭", "🫢", "🤌",
    "🤙", "🤞", "🫰", "🤟", "🫵", "✌️", "🤘",
    # ── Hearts ───────────────────────────────────────────
    "🧡", "💛", "💚", "💙", "💜", "🖤", "🤍", "🤎",
    "💝", "💖", "💗", "💓", "💞", "💕", "💟", "❣️",
]


def _is_command_message(_, __, m: Message) -> bool:
    """True for any message that starts with a bot command, e.g. /start, /help."""
    return bool(m.text and m.text.startswith("/"))


# group=-1 so this runs *before* the normal handlers (group 0) below, and
# since it doesn't call stop_propagation(), every matching command still
# reaches its real handler afterwards as usual.
@app.on_message(filters.create(_is_command_message) & filters.private, group=-1)
async def react_to_any_command(client: Client, m: Message):
    try:
        await client.send_reaction(
            chat_id=m.chat.id,
            message_id=m.id,
            emoji=random.choice(REACTIONS),
        )
    except Exception as e:
        # Reactions can fail (e.g. emoji not supported in this chat/region,
        # or rate limits) — never let that break the actual command.
        logger.debug(f"send_reaction failed for {m.command}: {e}")


# ---------------------------------------------------------------------
# /start, /help, menu buttons
# ---------------------------------------------------------------------

@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, m: Message):
    if m.from_user and m.from_user.id in ADMINS:
        asyncio.create_task(set_admin_commands_menu(client, m.from_user.id))  # admin sees the full menu
    display_name = smallcaps(m.from_user.first_name or "there")
    powered_link = f'<a href="{POWERED_BY_URL}">{smallcaps(POWERED_BY)}</a>'
    me = await client.get_me()
    bot_username = me.username or ""
    bot_name = me.first_name or "Faphouse Bot"
    start_txt = (
        f"<b>👋 Hello {display_name},</b>\n"
        f"<b>🤖 I am <a href=https://t.me/{bot_username}>{bot_name}</a></b>\n\n"
    )
    caption = (
        f"{start_txt}"
        "⚡ ɪ'ᴍ ᴀ ᴠᴇʀʏ ᴘᴏᴡᴇʀꜰᴜʟ faphouse ᴅᴏᴡɴʟᴏᴀᴅᴇʀ ʙᴏᴛ.\n\n"
        "📥 ꜱɪᴍᴘʟʏ ꜱᴇɴᴅ ᴍᴇ ᴀɴʏ faphouse ᴜʀʟ, ᴀɴᴅ ɪ'ʟʟ ꜰᴇᴛᴄʜ ᴛʜᴇ ᴅɪʀᴇᴄᴛ ᴠɪᴅᴇᴏ ꜰᴏʀ ʏᴏᴜ ɪɴ ꜱᴇᴄᴏɴᴅꜱ.\n\n"
        "🚀 ᴜʟᴛʀᴀ-ꜰᴀꜱᴛ ᴘʀᴏᴄᴇꜱꜱɪɴɢ\n"
        "🎬 ɪɴꜱᴛᴀɴᴛ ᴠɪᴅᴇᴏ ᴇxᴛʀᴀᴄᴛɪᴏɴ\n"
        "⚡ ʟɪɢʜᴛɴɪɴɢ-ꜱᴘᴇᴇᴅ ᴅᴏᴡɴʟᴏᴀᴅꜱ\n"
        "🛡️ ʀᴇʟɪᴀʙʟᴇ & ꜱᴛᴀʙʟᴇ ꜱᴇʀᴠɪᴄᴇ\n"
        "💎 ᴘʀᴇᴍɪᴜᴍ ᴘʟᴀɴꜱ ᴀᴠᴀɪʟᴀʙʟᴇ\n"
        "🔗 ᴊᴜꜱᴛ ᴘᴀꜱᴛᴇ ʏᴏᴜʀ faphouse ʟɪɴᴋ ʙᴇʟᴏᴡ ᴀɴᴅ ʟᴇᴛ ᴛʜᴇ ᴍᴀɢɪᴄ ʙᴇɢɪɴ!\n\n"
        "✅ ʏᴇ ʟɪɴᴋꜱ ꜱᴜᴘᴘᴏʀᴛᴇᴅ ʜᴀɪ:\n"
        "• faphouse.com / faphouse2.com \n"
        "ᴡᴀᴀʟᴇ ʟɪɴᴋꜱ\n\n"
        "━━━━━━━━━━━━━━━ \n"
        f"👑 ᴘᴏᴡᴇʀᴇᴅ ʙʏ {powered_link}\n"
        "⚡ ꜱᴘᴇᴇᴅ • ᴘᴇʀꜰᴏʀᴍᴀɴᴄᴇ • ʀᴇʟɪᴀʙɪʟɪᴛʏ\n"
        "━━━━━━━━━━━━━━━"
    )
    try:
        await m.reply_photo(START_PHOTO_URL, caption=SC(caption), reply_markup=fallback_keyboard(), parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning(f"start photo failed, falling back to text: {e}")
        await m.reply(SC(caption), reply_markup=fallback_keyboard(), parse_mode=ParseMode.HTML)

    await m.reply(SC(FALLBACK_TEXT), reply_markup=MAIN_MENU_KB)

    is_new = await register_user_if_new(m.from_user.id)
    if is_new:
        uname = f"@{m.from_user.username}" if m.from_user.username else "(no username)"
        await log_event(
            client,
            "🆕 <b>New User</b>\n\n"
            f"👤 Name: {m.from_user.first_name}\n"
            f"🔗 Username: {uname}\n"
            f"🆔 ID: <code>{m.from_user.id}</code>",
        )

        referrer_id = None
        if len(m.command) > 1 and m.command[1].startswith("ref_"):
            try:
                referrer_id = int(m.command[1][4:])
            except ValueError:
                referrer_id = None

        if referrer_id and referrer_id != m.from_user.id:
            credited = await set_referrer(m.from_user.id, referrer_id)
            if credited:
                new_count = await increment_referral_count(referrer_id)
                try:
                    await client.send_message(
                        referrer_id,
                        SC(f"🎁 sᴏᴍᴇᴏɴᴇ ᴊᴏɪɴᴇᴅ ᴜsɪɴɢ ʏᴏᴜʀ ʀᴇғᴇʀʀᴀʟ ʟɪɴᴋ!\n"
                           f"👥 ᴛᴏᴛᴀʟ ʀᴇғᴇʀʀᴀʟs: {new_count}/{REFERRAL_GOAL}"),
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass

                claimed = await get_referral_rewards_claimed(referrer_id)
                for threshold, days in REFERRAL_REWARDS:
                    if new_count >= threshold and threshold not in claimed:
                        await grant_referral_premium_days(referrer_id, days)
                        await mark_referral_reward_claimed(referrer_id, threshold)
                        try:
                            await client.send_message(
                                referrer_id,
                                SC(f"🎉 ʀᴇғᴇʀʀᴀʟ ʀᴇᴡᴀʀᴅ!\n\n"
                                   f"👥 ʏᴏᴜ ʀᴇᴀᴄʜᴇᴅ {threshold} ʀᴇғᴇʀʀᴀʟs — 💎 {days} ᴅᴀʏ(s) ᴘʀᴇᴍɪᴜᴍ ᴀᴅᴅᴇᴅ!"),
                                parse_mode=ParseMode.HTML,
                            )
                        except Exception:
                            pass


@app.on_message((filters.command("help") | menu_text_filter("❓ ʜᴇʟᴘ")) & filters.private)
async def help_handler(client: Client, m: Message):
    await m.reply(
        SC("ℹ️ <b>ʜᴏᴡ ᴛᴏ ᴜsᴇ</b>\n\n"
        "🔹 <b>ᴊᴜsᴛ sᴇɴᴅ ᴛʜᴇ ʟɪɴᴋ:</b>\n"
        "ᴘᴀsᴛᴇ ᴀɴʏ ꜰᴀᴘʜᴏᴜsᴇ ᴜʀʟ ᴅɪʀᴇᴄᴛʟʏ ɪɴ ᴛʜᴇ ᴄʜᴀᴛ.\n\n"
        "🔹 <b>sᴜᴘᴘᴏʀᴛᴇᴅ ᴜʀʟ ꜰᴏʀᴍᴀᴛs:</b>\n"
        "<code>faphouse.com</code>\n"
        "<code>faphouse2.com</code>\n\n"
        "📌 <b>ᴇxᴀᴍᴘʟᴇ:</b>\n"
        "<code>https://faphouse.com/videos/young-wife-cheated-husband-c2J8jp#dmVwPU1haW4gcGFnZSZ2ZWI9Rmlyc3QgNjAgb24gbWFpbg==</code>\n"
        "<code>https://faphouse2.com/videos/sharing-hotel-room-stepsister-Al1F04#dmVwPVZpZGVvIHBhZ2UmdmViPVJlbGF0ZWQmcmVsYXRlZF90eXBlPW1s</code>\n\n"
        "💡 <b>ᴛɪᴘs:</b>\n"
        "• ꜰɪʟᴇs ᴜᴘ ᴛᴏ 2 ɢʙ ᴀʀᴇ ᴜᴘʟᴏᴀᴅᴇᴅ ᴅɪʀᴇᴄᴛʟʏ — ʙɪɢɢᴇʀ ᴏɴᴇs ᴀʀᴇ sᴘʟɪᴛ ɪɴᴛᴏ ᴘᴀʀᴛs ᴀᴜᴛᴏᴍᴀᴛɪᴄᴀʟʟʏ\n"
        "• ᴘʀᴇᴠɪᴏᴜsʟʏ ᴅᴏᴡɴʟᴏᴀᴅᴇᴅ ʟɪɴᴋs ᴀʀᴇ ᴄᴀᴄʜᴇᴅ — ɪɴsᴛᴀɴᴛ!\n"
        "• ɪꜰ ᴅᴏᴡɴʟᴏᴀᴅ ꜰᴀɪʟs, ᴜsᴇ ᴛʜᴇ ʀᴇᴛʀʏ ʙᴜᴛᴛᴏɴ\n"
        "• ᴜsᴇ <code>/cancel</code> ᴛᴏ sᴛᴏᴘ ᴀɴ ᴀᴄᴛɪᴠᴇ ᴅᴏᴡɴʟᴏᴀᴅ\n\n"
        "ʜᴀᴠɪɴɢ ᴛʀᴏᴜʙʟᴇ? ᴍᴀᴋᴇ sᴜʀᴇ ʏᴏᴜ'ʀᴇ sᴇɴᴅɪɴɢ ᴀ ᴠᴀʟɪᴅ ꜰᴀᴘʜᴏᴜsᴇ ʟɪɴᴋ."),
        parse_mode=ParseMode.HTML,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


ABOUT_TEXT = (
    f"💠 {smallcaps('About This Bot')} 💠\n\n"
    f"╭────[ ✨ {smallcaps('Anuj')} ]────⍟\n"
    f"├⍟ 🚀 {smallcaps('Bot Name')}  : <a href=\"https://t.me/faphouse_downloaderr_bot\">{smallcaps('faphouse Downloader Bot')}</a>\n"
    f"├⍟ 👨‍💻 {smallcaps('Developer')}  : <a href=\"https://t.me/anujedits97\">{smallcaps('Anuj Kumar')}</a>\n"
    f"├⍟ 🔗 {smallcaps('Library')}  : <a href=\"https://docs.pyrogram.org/\">{smallcaps('Pyrogram Async')}</a>\n"
    f"├⍟ ⚡️ {smallcaps('Language')}  : <a href=\"https://www.python.org/\">{smallcaps('Python')} 3.11+</a>\n"
    f"├⍟ ⚙️ {smallcaps('Database')}  : <a href=\"https://www.mongodb.com/\">{smallcaps('MongoDB')}</a>\n"
    f"├⍟ ⭐️ {smallcaps('Hosting')}  :  {smallcaps('Dedicated High-Speed VPS')}\n"
    "╰───────────────⍟"
)


def about_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[make_button(SC("❌ Close"), callback_data="about_close", style=BTN_DANGER)]])


@app.on_message(filters.command("about") & filters.private)
async def about_cmd(client: Client, m: Message):
    await m.reply(SC(ABOUT_TEXT), reply_markup=about_keyboard(), parse_mode=ParseMode.HTML)


@app.on_callback_query(filters.regex(r"^about_close$"))
async def about_close_cb(client: Client, query):
    try:
        await query.message.delete()
    except Exception:
        pass
    await query.answer()


@app.on_message(filters.command("potstatus") & filters.private)
async def potstatus_cmd(client: Client, m: Message):
    """BUG FIX: pot_provider.py's own docstring/get_status() comments
    claimed this command already existed ("wired into /potstatus in
    main.py") — it never actually did. Without it there was no way to
    tell, from the bot itself, WHY YouTube quality was capped low (the
    "web" client — the only one with the real 144p-4K ladder — silently
    gets a degraded/throttled format list from YouTube when no working
    PO-token is attached; see pot_provider.py's module docstring), short
    of digging through raw server logs for "[pot-provider]" lines."""
    if m.from_user.id not in ADMINS:
        return await m.reply(SC("🚫 Owner/admin only."), parse_mode=ParseMode.HTML)
    ready = pot_provider.is_ready()
    status = pot_provider.get_status()
    icon = "🟢" if ready else "🔴"
    node_ready = ytnode_client.is_ready()
    node_icon = "🟢" if node_ready else "🔴"
    await m.reply(SC(
        f"{icon} <b>PO-token server:</b> {'ready' if ready else 'NOT ready'}\n"
        f"📋 <b>Status:</b> <code>{status}</code>\n\n"
        + ("YouTube's \"web\" client (the only one with the full 144p-4K quality "
           "ladder) requests a token from this local server on every YouTube link. "
           if ready else
           "Without this, YouTube's \"web\" client gets a degraded/throttled format "
           "list from YouTube itself — this is almost certainly why quality is "
           "capped low on every YouTube link right now. Check the server's raw logs "
           "for lines starting with <code>[pot-provider]</code> around this status "
           "message for exactly which setup step (git/node/npm availability, clone, "
           "build) failed.")
        + f"\n\n{node_icon} <b>ytnode fallback:</b> {'ready' if node_ready else 'NOT ready'}\n"
        + ("Kicks in automatically when the PO-token path above still returns fewer "
           "than 3 quality options for a YouTube link — no cookies/PO-token needed "
           "for this one, but it's a separate library with its own reliability. "
           if node_ready else
           "Check the raw logs for lines starting with <code>[ytnode]</code> for why "
           "it didn't come up.")
    ), parse_mode=ParseMode.HTML)


@app.on_message(filters.command("cancel") & filters.private)
async def cancel_cmd(client: Client, m: Message):
    task = ACTIVE_TASKS.get(m.from_user.id)
    if task and not task.done():
        task.cancel()
        await m.reply(SC("<b>🛑 Cancelling your active download...</b>"), parse_mode=ParseMode.HTML)
    else:
        await m.reply(SC("<b>⚠️ No active download to cancel.</b>"), parse_mode=ParseMode.HTML)


FALLBACK_TEXT = "👇 Apna Faphouse link bhejo boss!"

NOT_A_LINK_TEXT = (
    "🤨 <b>Bhai ye kaunsa link hai? Faphouse ka toh nahi lagta!</b>\n\n"
    "Agar lagta hai Faphouse ka hai aur error aa rha, toh screenshot ke saath "
    "idhar report kro 👉 <a href=\"https://t.me/anujedits76\">Anuj Kumar</a>\n\n"
    "📌 <b>Example:</b>\n"
    "<code>https://faphouse2.com/videos/sharing-hotel-room-stepsister-Al1F04</code>"
)


def fallback_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [make_button(SC("📥 Download"), callback_data="fallback_download", style=BTN_PRIMARY),
         make_button(SC("📊 Status"), callback_data="fallback_status", style=BTN_PRIMARY)],
    ])


@app.on_callback_query(filters.regex(r"^fallback_download$"))
async def fallback_download_cb(client: Client, query):
    await query.answer(SC("👇 Apna Faphouse link bhejo boss!"), show_alert=True)


@app.on_callback_query(filters.regex(r"^fallback_status$"))
async def fallback_status_cb(client: Client, query):
    text, kb = await build_status_text(query.from_user.id)
    await query.message.reply(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    await query.answer()


@app.on_message(filters.private & filters.text & menu_text_filter("☎️ sᴜᴘᴘᴏʀᴛ"))
async def support_handler(client: Client, m: Message):
    await m.reply(
        SC("📞 <b>Support</b>\n\n"
        "Koi problem? Idhar baat karo:\n\n"
        f"👤 Admin: <a href=\"{POWERED_BY_URL}\">Anuj Kumar</a>\n\n"
        "⏰ 24 ghante ke andar reply, pakka!"),
        parse_mode=ParseMode.HTML,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


@app.on_message(filters.private & filters.text & menu_text_filter("💎 ᴘʟᴀɴs"))
async def plans_menu_handler(client: Client, m: Message):
    await send_plans_message(m)


@app.on_message(filters.command(["plans", "premium"]) & filters.private)
async def plans_cmd(client: Client, m: Message):
    await send_plans_message(m)


# ---------------------------------------------------------------------
# Custom caption
# ---------------------------------------------------------------------

@app.on_message(filters.command("set_caption") & filters.private)
async def set_caption_cmd(client: Client, m: Message):
    if len(m.command) < 2:
        return await m.reply(
            SC("⚠️ <b>Usage Error</b>\n\n"
            "Please provide the caption text after the command.\n\n"
            "<b>Correct Format:</b>\n"
            "<code>/set_caption Your Caption Here</code>\n\n"
            "<b>Supported Placeholders:</b>\n"
            "• <code>{filename}</code> : File name\n"
            "• <code>{size}</code> : File size\n"
            "• <code>{quality}</code> : Quality label\n"
            "• <code>{source}</code> : Source Faphouse link\n\n"
            "<i>Example:</i> <code>/set_caption File: {filename} | Size: {size}</code>"),
            parse_mode=ParseMode.HTML,
        )
    caption = m.text.split(" ", 1)[1].strip()
    await set_caption(m.from_user.id, caption)
    await m.reply(
        SC("✅ <b>Custom Caption Saved!</b>\n\n"
        f"<b>Preview:</b>\n<code>{caption}</code>\n\n"
        "<i>This caption will be applied to your future downloads.</i>"),
        parse_mode=ParseMode.HTML,
    )


@app.on_message(filters.command("see_caption") & filters.private)
async def see_caption_cmd(client: Client, m: Message):
    caption = await get_caption(m.from_user.id)
    if caption:
        await m.reply(
            SC("📝 <b>Your Custom Caption</b>\n\n"
            f"<code>{caption}</code>\n\n"
            "<i>To remove this, use /del_caption</i>"),
            parse_mode=ParseMode.HTML,
        )
    else:
        await m.reply(
            SC("❌ <b>No Caption Set</b>\n\n"
            "You are currently using the default bot caption.\n"
            "<i>Use /set_caption to customize it.</i>"),
            parse_mode=ParseMode.HTML,
        )


@app.on_message(filters.command("del_caption") & filters.private)
async def del_caption_cmd(client: Client, m: Message):
    caption = await get_caption(m.from_user.id)
    if not caption:
        return await m.reply(
            SC("⚠️ <b>No Caption Found</b>\n\nYou don't have a custom caption set."),
            parse_mode=ParseMode.HTML,
        )
    await del_caption(m.from_user.id)
    await m.reply(
        SC("🗑 <b>Custom Caption Removed</b>\n\n<i>Your uploads will now use the default bot caption.</i>"),
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------
# Custom thumbnail
# ---------------------------------------------------------------------

@app.on_message(filters.command("set_thumb") & filters.private)
async def set_thumb_cmd(client: Client, m: Message):
    reply = m.reply_to_message
    if not reply or not reply.photo:
        return await m.reply(
            SC("🖼 <b>Set Custom Thumbnail</b>\n\n"
            "<i>Reply to any photo with /set_thumb to use it as your default thumbnail.</i>\n\n"
            "<b>Usage:</b> Reply to a photo → <code>/set_thumb</code>"),
            parse_mode=ParseMode.HTML,
        )
    file_id = reply.photo.file_id
    await set_thumbnail(m.from_user.id, file_id)
    await m.reply_photo(
        file_id,
        caption=(
            SC("✅ <b>Custom Thumbnail Set Successfully!</b>\n\n"
            "<i>This thumbnail will be used for all your future uploads.</i>\n"
            "<i>Use /view_thumb to preview • /del_thumb to remove</i>")
        ),
        parse_mode=ParseMode.HTML,
    )


@app.on_message(filters.command(["view_thumb", "see_thumb"]) & filters.private)
async def view_thumb_cmd(client: Client, m: Message):
    thumb_id = await get_thumbnail(m.from_user.id)
    if not thumb_id:
        return await m.reply(
            SC("❌ <b>No Custom Thumbnail Found</b>\n\n"
            "<i>Reply to a photo with /set_thumb to add one.</i>"),
            parse_mode=ParseMode.HTML,
        )
    try:
        await m.reply_photo(
            thumb_id,
            caption=(
                SC("🖼 <b>Your Current Custom Thumbnail</b>\n\n"
                "<i>This is applied to all uploads.</i>\n"
                "<i>To delete, use /del_thumb</i>")
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await m.reply(SC(f"❌ Error loading thumbnail: {e}\nPlease set a new one."))


@app.on_message(filters.command(["del_thumb", "delete_thumb"]) & filters.private)
async def del_thumb_cmd(client: Client, m: Message):
    thumb_id = await get_thumbnail(m.from_user.id)
    if not thumb_id:
        return await m.reply(SC("ℹ️ You don't have a custom thumbnail set."))
    await del_thumbnail(m.from_user.id)
    await m.reply(
        SC("🗑 <b>Custom Thumbnail Deleted</b>\n\n"
        "<i>Your uploads will now use the default video thumbnail.</i>"),
        parse_mode=ParseMode.HTML,
    )


@app.on_message(filters.command("thumb_mode") & filters.private)
async def thumb_mode_cmd(client: Client, m: Message):
    thumb_id = await get_thumbnail(m.from_user.id)
    if thumb_id:
        status, extra = "🟢 Custom Thumbnail Active", "<i>Use /view_thumb to preview</i>"
    else:
        status, extra = "🔴 No Custom Thumbnail", "<i>Use /set_thumb (reply to photo) to enable</i>"
    await m.reply(SC(f"🖼 <b>Thumbnail Status</b>\n\n{status}\n{extra}"), parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------
# Per-user dump chat
# ---------------------------------------------------------------------

@app.on_message(filters.command("setchat") & filters.private)
async def setchat_cmd(client: Client, m: Message):
    if len(m.command) < 2:
        return await m.reply(
            SC("🗑 <b>Set Dump Chat</b>\n\n"
            "<b>Usage:</b>\n"
            "<code>/setchat &lt;chat_id&gt;</code> → every file you download also gets copied here\n"
            "<code>/setchat clear</code> → remove it\n\n"
            "<i>Example: /setchat -1001234567890</i>\n"
            "ℹ️ I must already be an admin in that channel/group."),
            parse_mode=ParseMode.HTML,
        )
    arg = m.command[1].strip().lower()
    if arg == "clear":
        await set_dump_chat(m.from_user.id, None)
        return await m.reply(SC("✅ <b>Dump Chat Cleared.</b>"), parse_mode=ParseMode.HTML)
    try:
        chat_id = int(m.command[1].strip())
    except ValueError:
        return await m.reply(
            SC("❌ <b>Invalid Chat ID</b>\n\n<i>Must be a number (e.g., -1001234567890)</i>"),
            parse_mode=ParseMode.HTML,
        )
    try:
        chat = await client.get_chat(chat_id)
        chat_title = chat.title or "Private Chat"
    except Exception as e:
        return await m.reply(SC(f"❌ <b>Unable to Access Chat</b>\n<i>{e}</i>"), parse_mode=ParseMode.HTML)
    await set_dump_chat(m.from_user.id, chat_id)
    await m.reply(
        SC(f"✅ <b>Dump Chat Set Successfully</b>\n\n"
        f"<b>Forward To:</b> <code>{chat_id}</code>\n"
        f"<b>Title:</b> {chat_title}"),
        parse_mode=ParseMode.HTML,
    )


@app.on_message(filters.command("set_dump") & filters.private & filters.user(ADMINS))
async def admin_set_dump_cmd(client: Client, m: Message):
    if len(m.command) < 3:
        return await m.reply(
            SC("⚠️ <b>Usage:</b> <code>/set_dump &lt;user_id&gt; &lt;chat_id&gt;</code>\n"
            "<code>/set_dump &lt;user_id&gt; clear</code> → remove it"),
            parse_mode=ParseMode.HTML,
        )
    try:
        target_id = int(m.command[1])
    except ValueError:
        return await m.reply(SC("⚠️ user_id must be a number."))

    arg = m.command[2].strip().lower()
    if arg == "clear":
        await set_dump_chat(target_id, None)
        return await m.reply(SC(f"✅ Dump chat cleared for <code>{target_id}</code>."), parse_mode=ParseMode.HTML)

    try:
        chat_id = int(m.command[2].strip())
    except ValueError:
        return await m.reply(SC("⚠️ chat_id must be a number."))

    try:
        chat = await client.get_chat(chat_id)
        chat_title = chat.title or "Private Chat"
    except Exception as e:
        return await m.reply(SC(f"❌ <b>Unable to Access Chat</b>\n<i>{e}</i>"), parse_mode=ParseMode.HTML)

    await set_dump_chat(target_id, chat_id)
    await m.reply(
        SC(f"✅ Dump chat set for <code>{target_id}</code> → <code>{chat_id}</code> ({chat_title})"),
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------
# /settings — one menu tying caption/thumbnail/dump-chat/stats together
# ---------------------------------------------------------------------

def settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [make_button(SC("📊 My Usage Stats"), callback_data="settings_stats", style=BTN_PRIMARY)],
        [make_button(SC("🗑 Dump Chat"), callback_data="settings_dump", style=BTN_PRIMARY)],
        [
            make_button(SC("🖼 Thumbnail"), callback_data="settings_thumb", style=BTN_PRIMARY),
            make_button(SC("📝 Caption"), callback_data="settings_caption", style=BTN_PRIMARY),
        ],
        [make_button(SC("⚡ Titanium Clone Mode"), callback_data="titanium_status", style=BTN_PRIMARY)],
        [make_button(SC("❌ Close"), callback_data="settings_close", style=BTN_DANGER)],
    ])


def settings_back_close_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [make_button(SC("⬅️ Back"), callback_data="settings_back", style=BTN_PRIMARY),
         make_button(SC("❌ Close"), callback_data="settings_close", style=BTN_DANGER)],
    ])


async def settings_text(user_id: int) -> str:
    premium = await get_effective_premium_status(user_id)
    badge = "💎 Premium Member" if premium["is_premium"] else "👤 Free User"
    return (
        "⚙️ <b>Settings Panel</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>Account:</b> {badge}\n"
        f"<b>User ID:</b> <code>{user_id}</code>\n\n"
        "<i>Select an option below to customize your experience.</i>"
    )


@app.on_message(filters.command("settings") & filters.private)
async def settings_cmd(client: Client, m: Message):
    try:
        await m.reply(SC(await settings_text(m.from_user.id)), reply_markup=settings_keyboard(), parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning(f"/settings command failed for user {m.from_user.id}: {e}")
        try:
            await m.reply(SC("⚠️ Couldn't load settings, try again."), parse_mode=ParseMode.HTML)
        except Exception:
            pass


@app.on_callback_query(filters.regex(r"^settings_(stats|dump|thumb|caption|back|close)$"))
async def settings_callbacks(client: Client, query):
    try:
        await _settings_callbacks_impl(client, query)
    except Exception as e:
        logger.warning(f"Settings sub-menu '{query.data}' failed for user {query.from_user.id}: {e}")
        try:
            await query.answer("⚠️ Something went wrong, try again.", show_alert=True)
        except Exception:
            pass


async def _settings_callbacks_impl(client: Client, query):
    data = query.data
    user_id = query.from_user.id

    if data == "settings_stats":
        text, _ = await build_status_text(user_id)
        await query.message.edit_text(text, reply_markup=settings_back_close_keyboard(), parse_mode=ParseMode.HTML)

    elif data == "settings_dump":
        current = await get_dump_chat(user_id)
        if current:
            try:
                chat = await client.get_chat(current)
                title = chat.title or "Private Chat"
            except Exception:
                title = "Unknown (Inaccessible)"
            text = (
                "🗑 <b>Current Dump Chat</b>\n\n"
                f"<b>Chat ID:</b> <code>{current}</code>\n"
                f"<b>Title:</b> {title}\n\n"
                "<i>All your delivered files are copied here.</i>\n"
                "<i>Use /setchat to change or clear.</i>"
            )
        else:
            text = (
                "🗑 <b>No Dump Chat Set</b>\n\n"
                "<i>Delivered files only appear in this chat.</i>\n"
                "<i>Use /setchat &lt;chat_id&gt; to enable forwarding.</i>"
            )
        await query.message.edit_text(text, reply_markup=settings_back_close_keyboard(), parse_mode=ParseMode.HTML)

    elif data == "settings_thumb":
        thumb_id = await get_thumbnail(user_id)
        if thumb_id:
            await query.message.reply_photo(
                thumb_id,
                caption=SC("🖼 <b>Your Current Custom Thumbnail</b>\n\n<i>Use /set_thumb (reply to photo) to update, /del_thumb to remove</i>"),
                parse_mode=ParseMode.HTML,
            )
            await query.answer(SC("Thumbnail preview sent below 👇"))
            return
        else:
            await query.message.edit_text(
                SC("🖼 <b>No Custom Thumbnail Set</b>\n\n"
                "<i>Reply to a photo with /set_thumb to add one.</i>"),
                reply_markup=settings_back_close_keyboard(),
                parse_mode=ParseMode.HTML,
            )

    elif data == "settings_caption":
        caption = await get_caption(user_id)
        if caption:
            text = (
                "📝 <b>Current Custom Caption</b>\n\n"
                f"<code>{caption}</code>\n\n"
                "<i>Placeholders: {filename}, {size}, {quality}, {source}</i>\n"
                "<i>/set_caption &lt;text&gt; to change • /del_caption to remove</i>"
            )
        else:
            text = (
                "📝 <b>No Custom Caption Set</b>\n\n"
                "<i>Use /set_caption &lt;text&gt; to set one.</i>"
            )
        await query.message.edit_text(text, reply_markup=settings_back_close_keyboard(), parse_mode=ParseMode.HTML)

    elif data == "settings_back":
        await query.message.edit_text(
            SC(await settings_text(user_id)), reply_markup=settings_keyboard(), parse_mode=ParseMode.HTML
        )

    elif data == "settings_close":
        try:
            await query.message.delete()
        except Exception:
            pass

    await query.answer()


@app.on_message(filters.private & filters.text & menu_text_filter("📊 ᴍʏ sᴛᴀᴛᴜs"))
async def status_menu_handler(client: Client, m: Message):
    await show_my_status(client, m)


@app.on_message(filters.command("myplan") & filters.private)
async def myplan_cmd(client: Client, m: Message):
    await show_my_status(client, m)


async def build_status_text(chat_id: int):
    """Returns (text, keyboard_or_None) describing chat_id's plan/usage."""
    premium = await get_effective_premium_status(chat_id)
    if premium["lifetime"]:
        type_line = "💎 Premium (Lifetime ♾️)"
    elif premium["is_premium"]:
        _exp = premium["expires_at"]
        if _exp.tzinfo is None:                      # Mongo hands back naive UTC datetimes
            _exp = _exp.replace(tzinfo=timezone.utc)
        days_left = (_exp - datetime.now(timezone.utc)).days + 1
        type_line = f"💎 Premium ({days_left} day{'s' if days_left != 1 else ''} left)"
    else:
        type_line = "Free"

    total_downloads = await get_user_total_downloads(chat_id)

    text = (
        "<b>📊 Your Status</b>\n\n"
        f"User ID: <code>{chat_id}</code>\n"
        f"Plan: <code>{type_line}</code>\n"
        f"Total Downloads: <code>{total_downloads}</code>\n"
    )
    if not premium["is_premium"]:
        used_today = await get_daily_count(chat_id)
        remaining = max(0, DAILY_FREE_LIMIT - used_today)
        text += f"Today's downloads: {used_today}/{DAILY_FREE_LIMIT} ({remaining} left)\n\n"
        text += "💎 Premium lo — unlimited downloads ka maza lo!"
        return text, status_keyboard()
    return text, status_keyboard()


async def show_my_status(client: Client, m: Message):
    text, kb = await build_status_text(m.from_user.id)
    await m.reply(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@app.on_callback_query(filters.regex(r"^show_plans$"))
async def show_plans_cb(client: Client, query):
    await send_plans_message(query.message)
    await query.answer()


PLAN_LABELS = dict(PLANS)


def payment_keyboard(price: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [make_button(SC("✅ I've Paid"), callback_data=f"paid_{price}", style=BTN_PRIMARY)],
        [make_button(SC("📸 Send Payment Proof"), url=POWERED_BY_URL, style=BTN_PRIMARY)],
        [make_button(SC("⬅️ Back"), callback_data="plans_back", style=BTN_DANGER)],
    ])


@app.on_callback_query(filters.regex(r"^plan_\d+$"))
async def plan_selected_cb(client: Client, query):
    price = int(query.data.split("_", 1)[1])
    label = PLAN_LABELS.get(price, "")
    text = (
        f"💳 <b>{label}</b> ke liye Payment\n\n"
        f"Amount: ₹{price}\n\n"
        "📱 QR scan karo kisi bhi UPI app se:\n"
        "• PhonePe\n"
        "• GPay\n"
        "• Paytm\n"
        "• Koi bhi UPI app\n\n"
        "⏰ Time limit: 15 minutes\n\n"
        "Payment ke baad 'I've Paid' dabao — verify instant! ⚡"
    )
    try:
        await query.message.reply_photo(
            PLANS_PHOTO_URL,
            caption=text,
            reply_markup=payment_keyboard(price),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.warning(f"payment photo failed, falling back to text: {e}")
        await query.message.reply(text, reply_markup=payment_keyboard(price), parse_mode=ParseMode.HTML)
    await query.answer()


@app.on_callback_query(filters.regex(r"^paid_\d+$"))
async def paid_cb(client: Client, query):
    price = int(query.data.split("_", 1)[1])
    label = PLAN_LABELS.get(price, "")
    user = query.from_user
    display_name = f"@{user.username}" if user.username else (user.first_name or str(user.id))
    uname = f'<a href="tg://user?id={user.id}"><code>{html.escape(display_name)}</code></a>'

    for admin_id in ADMINS:
        try:
            await client.send_message(
                admin_id,
                SC("🔔 <b>Payment Claim</b>\n\n"
                f"User: {uname} (<code>{user.id}</code>)\n"
                f"Plan: ₹{price} - {label}\n\n"
                f"Verify the screenshot, then run:\n<code>/addpremium {user.id} {PLAN_DAYS.get(price, '&lt;days&gt;')}</code>"),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.warning(f"failed to notify admin {admin_id}: {e}")

    await query.answer(SC("✅ Admin ko notify kar diya!"), show_alert=True)
    await query.message.reply(
        SC("✅ Aapka payment claim admin ko bhej diya gaya hai.\n"
        f"Jaldi verification ke liye screenshot bhi bhej do: <a href=\"{POWERED_BY_URL}\">Anuj Kumar</a>"),
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------
# Admin-only premium/ban management
# ---------------------------------------------------------------------

@app.on_message(filters.command("addpremium") & filters.private & filters.user(ADMINS))
async def addpremium_cmd(client: Client, m: Message):
    args = m.command[1:]
    if len(args) < 2:
        return await m.reply(
            SC("⚠️ <b>Usage:</b> <code>/addpremium &lt;user_id&gt; &lt;days|lifetime&gt;</code>"),
            parse_mode=ParseMode.HTML,
        )
    try:
        target_id = int(args[0])
    except ValueError:
        return await m.reply(SC("⚠️ user_id must be a number."))

    if args[1].lower() == "lifetime":
        await set_premium(target_id, None)
        note = "Lifetime ♾️"
    else:
        try:
            days = int(args[1])
        except ValueError:
            return await m.reply(SC("⚠️ days must be a number, or 'lifetime'."))
        if days < 1:
            return await m.reply(SC("⚠️ days must be at least 1."))
        await set_premium(target_id, days)
        note = f"{days} day{'s' if days != 1 else ''}"

    await m.reply(SC(f"✅ Premium granted to <code>{target_id}</code> — {note}."), parse_mode=ParseMode.HTML)
    try:
        await client.send_message(target_id, SC(f"🎉 You've been given Premium ({note}) by the admin!"))
    except Exception as e:
        logger.warning(f"Couldn't notify {target_id} about premium grant: {e}")


@app.on_message(filters.command("removepremium") & filters.private & filters.user(ADMINS))
async def removepremium_cmd(client: Client, m: Message):
    args = m.command[1:]
    if len(args) < 1:
        return await m.reply(
            SC("⚠️ <b>Usage:</b> <code>/removepremium &lt;user_id&gt;</code>"),
            parse_mode=ParseMode.HTML,
        )
    try:
        target_id = int(args[0])
    except ValueError:
        return await m.reply(SC("⚠️ user_id must be a number."))

    await remove_premium(target_id)
    await m.reply(SC(f"✅ Premium removed for <code>{target_id}</code>."), parse_mode=ParseMode.HTML)


@app.on_message(filters.command("ban") & filters.private & filters.user(ADMINS))
async def ban_cmd(client: Client, m: Message):
    args = m.command[1:]
    if len(args) < 1:
        return await m.reply(SC("⚠️ <b>Usage:</b> <code>/ban &lt;user_id&gt;</code>"), parse_mode=ParseMode.HTML)
    try:
        target_id = int(args[0])
    except ValueError:
        return await m.reply(SC("⚠️ user_id must be a number."))
    if target_id in ADMINS:
        return await m.reply(SC("⚠️ Can't ban an admin."))

    await set_banned(target_id, True)
    await m.reply(SC(f"🚫 <code>{target_id}</code> has been banned."), parse_mode=ParseMode.HTML)
    try:
        await client.send_message(target_id, SC("🚫 You've been banned from using this bot."))
    except Exception as e:
        logger.warning(f"Couldn't notify {target_id} about ban: {e}")


@app.on_message(filters.command("unban") & filters.private & filters.user(ADMINS))
async def unban_cmd(client: Client, m: Message):
    args = m.command[1:]
    if len(args) < 1:
        return await m.reply(SC("⚠️ <b>Usage:</b> <code>/unban &lt;user_id&gt;</code>"), parse_mode=ParseMode.HTML)
    try:
        target_id = int(args[0])
    except ValueError:
        return await m.reply(SC("⚠️ user_id must be a number."))

    await set_banned(target_id, False)
    await m.reply(SC(f"✅ <code>{target_id}</code> has been unbanned."), parse_mode=ParseMode.HTML)
    try:
        await client.send_message(target_id, SC("✅ You've been unbanned — you can use the bot again."))
    except Exception as e:
        logger.warning(f"Couldn't notify {target_id} about unban: {e}")


@app.on_message(filters.command("stats") & filters.private & filters.user(ADMINS))
async def stats_cmd(client: Client, m: Message):
    s = await get_stats_summary()
    if not s:
        return await m.reply(SC("❌ Couldn't fetch stats."))
    text = (
        "📊 <b>Bot Stats</b>\n\n"
        f"👥 <b>Total Users:</b> {s['total_users']}\n"
        f"💎 <b>Premium Users:</b> {s['premium_count']}\n"
        f"🚫 <b>Banned Users:</b> {s['banned_count']}\n\n"
        f"📦 <b>Total Downloads:</b> {s['total_downloads']}\n"
        f"🗂️ <b>Unique Files Cached:</b> {s['total_files_cached']}"
    )
    await m.reply(text, parse_mode=ParseMode.HTML)


async def _broadcast_one(client: Client, cid: int, broadcast_text, reply, from_chat_id: int):
    try:
        if broadcast_text is not None:
            await client.send_message(cid, broadcast_text)
        else:
            await client.copy_message(chat_id=cid, from_chat_id=from_chat_id, message_id=reply.id)
        return "success"
    except FloodWait as e:
        await asyncio.sleep(e.value)
        return await _broadcast_one(client, cid, broadcast_text, reply, from_chat_id)
    except (InputUserDeactivated, UserIsBlocked, PeerIdInvalid):
        await delete_user(cid)
        return "removed"
    except Exception as e:
        logger.warning(f"broadcast failed for {cid}: {e}")
        return "failed"


@app.on_message(filters.command("broadcast") & filters.private & filters.user(ADMINS))
async def broadcast_cmd(client: Client, m: Message):
    reply = m.reply_to_message
    broadcast_text = None
    if len(m.command) >= 2:
        broadcast_text = m.text.split(None, 1)[1]
    elif not reply:
        return await m.reply(
            SC("⚠️ <b>Usage:</b> <code>/broadcast &lt;message&gt;</code>\n"
            "(or reply to a message with just <code>/broadcast</code> to forward that)"),
            parse_mode=ParseMode.HTML,
        )

    chat_ids = await all_chat_ids()
    total = len(chat_ids)
    status_msg = await m.reply(SC(f"📣 Broadcasting to {total} users..."))

    done = success = removed = failed = 0
    for cid in chat_ids:
        result = await _broadcast_one(client, cid, broadcast_text, reply, m.chat.id)
        if result == "success":
            success += 1
        elif result == "removed":
            removed += 1
        else:
            failed += 1
        done += 1

        if done % 20 == 0 or done == total:
            try:
                await status_msg.edit_text(
                    SC("📣 <b>Broadcast in progress...</b>\n\n"
                    f"👥 Total: {total}\n"
                    f"💫 Done: {done}/{total}\n"
                    f"✅ Success: {success}\n"
                    f"🚫 Removed (blocked/deleted): {removed}\n"
                    f"❌ Failed: {failed}"),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
        await asyncio.sleep(0.05)

    await status_msg.edit_text(
        SC("📣 <b>Broadcast done.</b>\n\n"
        f"✅ Success: {success}\n"
        f"🚫 Removed (blocked/deleted): {removed}\n"
        f"❌ Failed: {failed}"),
        parse_mode=ParseMode.HTML,
    )


@app.on_message(filters.command("users") & filters.private & filters.user(ADMINS))
async def users_export_cmd(client: Client, m: Message):
    status = await m.reply(SC("⏳ Gathering user data..."))
    users = await get_all_users_full()

    tmp_path = f"/tmp/faphouse_users_{m.chat.id}.json"
    export = [
        {
            "id": u.get("_id"),
            "is_banned": u.get("is_banned", False),
            "is_premium": bool(u.get("premium_lifetime") or u.get("premium_until")),
            "first_seen": u.get("first_seen").isoformat() if u.get("first_seen") else None,
        }
        for u in users
    ]
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(export, f, indent=2, ensure_ascii=False)
        await status.edit_text(SC(f"👥 <b>Total Users:</b> {len(export)}"), parse_mode=ParseMode.HTML)
        await m.reply_document(tmp_path, caption=SC(f"📄 {len(export)} users exported."))
    except Exception as e:
        await status.edit_text(SC(f"⚠️ Error exporting users: {e}"))
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


# ---------------------------------------------------------------------
# Admin-only dynamic backup-channel linking (no redeploy/env change needed)
# ---------------------------------------------------------------------

@app.on_message(filters.command("set_channel_id") & filters.private & filters.user(ADMINS))
async def set_channel_id_cmd(client: Client, m: Message):
    args = m.command[1:]
    if len(args) < 1:
        return await m.reply(
            SC("⚠️ <b>Usage:</b> <code>/set_channel_id -100xxxxxxxxxx</code>\n"
            "<b>Example:</b> <code>/set_channel_id -1001234567890</code>\n\n"
            "ℹ️ The ID must start with <code>-100</code>, and I must already be an admin "
            "in that channel/group. You can get a channel's ID by forwarding any message "
            "from it to @MissRose_bot.\n\n"
            "You can link more than one channel/group — just run this command again with "
            "a different ID. Use /channel_id to see everything linked, and /del_channel_id "
            "&lt;id&gt; to unlink one (or with no id to unlink all)."),
            parse_mode=ParseMode.HTML,
        )

    raw = args[0]
    if not raw.startswith("-100") or not raw.lstrip("-").isdigit():
        return await m.reply(
            SC("⚠️ The ID must start with <code>-100</code>, e.g. <code>-1001234567890</code>."),
            parse_mode=ParseMode.HTML,
        )

    channel_id = int(raw)
    try:
        chat = await client.get_chat(channel_id)
        member = await client.get_chat_member(channel_id, "me")
        if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            return await m.reply(SC("⚠️ I'm in that chat but I'm not an admin there. Please promote me first."))
    except Exception as e:
        return await m.reply(
            SC(f"⚠️ Couldn't verify that chat — make sure I've already been added there.\n<code>{_strip_ansi(str(e))[:300]}</code>"),
            parse_mode=ParseMode.HTML,
        )

    is_new = await add_channel(channel_id)
    title = getattr(chat, "title", None) or str(channel_id)
    note = "Linked" if is_new else "Already linked"
    await m.reply(SC(f"✅ {note}: <b>{title}</b> (<code>{channel_id}</code>)."), parse_mode=ParseMode.HTML)


@app.on_message(filters.command("channel_id") & filters.private & filters.user(ADMINS))
async def channel_id_cmd(client: Client, m: Message):
    dynamic_ids = await get_channels()
    static_ids = [c for c in BACKUP_CHANNEL_IDS if c not in dynamic_ids]
    if not dynamic_ids and not static_ids:
        return await m.reply(
            SC("❌ No backup channels linked yet.\n\n"
            "To link one: <code>/set_channel_id -100xxxxxxxxxx</code>"),
            parse_mode=ParseMode.HTML,
        )

    lines = []
    for cid in dynamic_ids:
        try:
            chat = await client.get_chat(cid)
            title = getattr(chat, "title", None) or str(cid)
            member = await client.get_chat_member(cid, "me")
            if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
                title += " ⚠️ (bot is not admin)"
        except Exception:
            title = "❌ unreachable (bot not in the channel, or wrong/deleted id)"
        lines.append(f"• <b>{title}</b> — <code>{cid}</code>")
    for cid in static_ids:
        try:
            chat = await client.get_chat(cid)
            status = f"<b>{getattr(chat, 'title', None) or cid}</b>"
        except Exception:
            status = "❌ unreachable (bot not in the channel, or wrong/deleted id)"
        lines.append(f"• {status} — <code>{cid}</code> — from config (not removable via /del_channel_id)")

    await m.reply(SC("🔗 <b>Linked Backup Channels</b>\n\n" + "\n".join(lines)), parse_mode=ParseMode.HTML)


@app.on_message(filters.command("del_channel_id") & filters.private & filters.user(ADMINS))
async def del_channel_id_cmd(client: Client, m: Message):
    args = m.command[1:]
    if not args:
        count = await remove_all_channels()
        return await m.reply(SC(f"🗑️ Unlinked all dynamically-added channels ({count} removed)."))

    try:
        channel_id = int(args[0])
    except ValueError:
        return await m.reply(
            SC("⚠️ Channel ID must be a number, e.g. <code>-1001234567890</code>."),
            parse_mode=ParseMode.HTML,
        )

    removed = await remove_channel(channel_id)
    if removed:
        await m.reply(SC(f"🗑️ Unlinked <code>{channel_id}</code>."), parse_mode=ParseMode.HTML)
    else:
        await m.reply(
            SC(f"⚠️ <code>{channel_id}</code> wasn't linked (or it's set via config, not removable here)."),
            parse_mode=ParseMode.HTML,
        )


# ---------------------------------------------------------------------
# Link handling
# ---------------------------------------------------------------------

LINK_HANDLER_FILTER = (
    filters.private & ~filters.service & ~filters.me
    & ~filters.command(["start", "help", "myplan", "plans", "premium", "addpremium",
                                          "removepremium", "ban", "unban", "stats", "broadcast", "users",
                                          "set_channel_id", "channel_id", "del_channel_id",
                                          "set_caption", "see_caption", "del_caption",
                                          "set_thumb", "view_thumb", "see_thumb",
                                          "del_thumb", "delete_thumb", "thumb_mode",
                                          "setchat", "settings", "set_dump", "cancel",
                                          "addbot", "delbot", "titanium", "potstatus", "fpotest"])
    & NOT_MENU_BUTTON_FILTER
)

# Matches a @BotFather-issued bot token (e.g. 8504787296:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx).
# Telegram's "Managed Bots" auto-create flow (see titanium.py's /addbot ->
# Auto-Create button) delivers its own creation confirmation as a real
# message in this chat, separately from our own "Bot added successfully!"
# reply — and that confirmation isn't a command or a Faphouse link, so it
# used to fall through to the generic "not a link" reply below, making
# every auto-created clone show BOTH messages together. A token-shaped
# message is never something the user is asking us to download, so it's
# safe (and much less confusing) to just ignore it silently here instead.
_BOTFATHER_TOKEN_RE = re.compile(r"\d[0-9]{8,10}:[0-9A-Za-z_-]{35}")

# Matches the path of a performer LISTING page (e.g. "/models/comatozze",
# "/pornstars/mia-khalifa") — the same shapes auto_scraper.ACTOR_PATH_PATTERNS
# looks for when "/autoupload <name>" resolves a typed name to a real page.
# Pasting the listing page's URL directly should behave the same way (bulk
# -scrape that performer's whole catalog), not fall through to the normal
# single-video Download/Stream menu, which only makes sense for an actual
# /videos/<slug> link and has nothing to download on a listing page.
_MODEL_PAGE_RE = re.compile(r"^/(?:pornstars?|models?|actors?)/([a-z0-9-]+)/?$", re.IGNORECASE)

# fpo.xxx's ONE confirmed-working performer-listing endpoint is
# /search/<Name-Hyphenated>-/ (see fpo_downloader._search_url's docstring) —
# not /models/ or /pornstars/ like the other sites above, and its own
# /models/<slug>/ path (matched generically by _MODEL_PAGE_RE) doesn't
# actually exist on the live site. Bug fix: pasting fpo.xxx's real listing
# URL (https://www.fpo.xxx/search/Mandy-flores-/) used to match NEITHER
# regex here, so it fell straight through to the single-video menu and
# failed outright — this is scoped to fpo.xxx hosts specifically so it
# doesn't affect other sites' own /search/ pages (which are real keyword
# search, not a performer-page shape).
_FPO_SEARCH_PAGE_RE = re.compile(r"^/search/([a-z0-9-]+?)-?/?$", re.IGNORECASE)

# Matches the path of a studio/production-company page (e.g.
# "/studios/puretaboo") — same idea as _MODEL_PAGE_RE above, but for
# STUDIO_PATH_PATTERNS' shapes instead of ACTOR_PATH_PATTERNS'.
_STUDIO_PAGE_RE = re.compile(r"^/(?:studios?|channels?|networks?|producers?)/([a-z0-9-]+)/?$", re.IGNORECASE)


def _downloader_for(link: str):
    """Picks which backend module actually understands this link — each
    site's structurally unrelated to the others (HLS/m3u8 vs KVS's
    scrambled progressive-MP4 links vs the pornhub_api/etc. package
    interface vs yt-dlp), so everything downstream (quality menu,
    download, stream link) needs to go through the right one.

    eporner.com/pornhub.com are checked ahead of porn_fetch_downloader's
    own is_supported_link() (which also used to register both) — this
    yt-dlp-backed module is the one actually meant to handle them now;
    see ytdlp_downloaderrr.py's module docstring for why.

    fpo.xxx was briefly moved to yt-dlp's GenericIE (on the theory that
    its real _extract_kvs KVS-player extractor would beat
    fpo_downloader.py's hand-rolled version) and reverted — confirmed in
    production that GenericIE doesn't reliably recognize fpo.xxx's pages
    (specifically /embed/ URLs) as KVS at all, raising UnsupportedError
    instead of ever reaching _extract_kvs, which is worse than
    fpo_downloader.py's known single-stream-key limitation. Back to
    fpo_downloader.py (checked below) until yt-dlp's detection actually
    works for this site.

    is_generically_supported() is the final fallback before giving up on
    faphouse by default — any of yt-dlp's ~1800 other dedicated
    extractors, not just the HOST_PATTERNS sites above."""
    if terabox.is_terabox_link(link):
        return terabox
    if diskwala.is_diskwala_link(link):
        return diskwala
    if mat6tube.is_mat6tube_link(link):
        return mat6tube
    if spankbang.is_spankbang_link(link):
        return spankbang
    if jav_sites.is_jav_link(link):
        return jav_sites  # JableTV / MissAV / SupJav / Jav.guru
    if ytdlp.is_supported_link(link):
        return ytdlp
    if fpo.is_fpo_link(link):
        return fpo
    if pf.is_supported_link(link):
        return pf
    if faphouse.is_faphouse_link(link):
        return faphouse
    if ytdlp.is_generically_supported(link):
        return ytdlp
    return faphouse


@app.on_message(filters.command("fpotest") & filters.private)
async def fpotest_cmd(client: Client, m: Message):
    """Admin-only: /fpotest <fpo.xxx listing link> - opens the page from THIS server (the same way the scraper does)
    and says what it found: videos on page 1, how many pages, and whether page 2 loads. Nothing is uploaded.
    If page 1 has no videos, the page the bot received is sent back as a file."""
    if not m.from_user or m.from_user.id not in ADMINS:
        return
    parts = (m.text or "").split(maxsplit=1)
    url = parts[1].strip() if len(parts) > 1 else ""
    if not url or not fpo.is_fpo_link(url):
        return await m.reply(SC("⚠️ <b>Usage:</b> <code>/fpotest https://www.fpo.xxx/sites/name/</code>"),
                             parse_mode=ParseMode.HTML)
    status = await m.reply(SC("🔎 <b>Testing...</b>"), parse_mode=ParseMode.HTML)

    def _run() -> list:
        out = []
        if not fpo.is_listing_link(url):
            out.append("This is not a site / channel / category / tag listing link "
                       "(a video link is downloaded the normal way).")
            return out
        base = fpo.listing_base_url(url)
        fpo._FPO_LISTING_STATE.pop(base, None)
        fpo._FPO_LISTING_ALIAS.pop(base, None)
        try:
            items, total = fpo.get_listing_page_videos(url, 1)
        except Exception as e:
            return [f"Page 1 could not be opened: {_strip_ansi(str(e))[:300]}"]
        out.append(f"Page 1: {len(items)} video(s), about {total} page(s).")
        out.append("Found via: " + str(fpo._FPO_LISTING_STATE.get(base, {}).get("via", "direct")))
        if not items:
            out.append("No video cards were found on page 1 - the page the bot got is attached if it was saved.")
            return out
        out.append("First ids: " + ", ".join(i["slug"].replace("fpo-", "") for i in items[:5]))
        try:
            items2, _ = fpo.get_listing_page_videos(url, 2)
            style = {0: "/2/", 1: "?page=2", 2: "?from=2"}.get(fpo._FPO_LISTING_STATE.get(base, {}).get("pattern"))
            if fpo._FPO_LISTING_STATE.get(base, {}).get("search"):
                style = "name search"
            out.append(f"Page 2: {len(items2)} new video(s)" + (f" (paging style {style})" if items2 else
                       " - none, so this listing has a single page or the next page could not be reached."))
        except Exception as e:
            out.append(f"Page 2 failed: {_strip_ansi(str(e))[:200]}")
        return out

    try:
        lines = await asyncio.to_thread(_run)
    except Exception as e:
        lines = [f"Test crashed: {_strip_ansi(str(e))[:300]}"]
    await status.edit_text(SC("🔎 <b>fpo.xxx test</b>\n<code>" + url + "</code>\n\n" + "\n".join(lines)),
                           parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True))
    if os.path.exists(fpo._LISTING_DEBUG_HTML_PATH) and any("No video cards" in l for l in lines):
        await m.reply_document(fpo._LISTING_DEBUG_HTML_PATH, caption=SC("📄 <b>Page the bot received</b>"),
                               parse_mode=ParseMode.HTML)


async def _maybe_start_fpo_listing_from_link(client: Client, m: Message, link: str) -> bool:
    """Admin pastes an fpo.xxx /sites/<slug>/ (or channel / category / tag) page: every video on it, all pages, is
    uploaded - the same job as "/autouploadfpo <link>". For anyone else this returns False (normal link flow)."""
    if not m.from_user or m.from_user.id not in ADMINS or not fpo.is_listing_link(link):
        return False
    target_chat = await _autoupload_target(client, m)
    if target_chat is None:
        return True
    term = fpo.listing_base_url(link) + "/"
    await auto_scraper.stop_worker_task(target_chat, "fpo")
    await auto_scraper.begin_site_run(target_chat, "fpo")
    started = auto_scraper.start_fpo_worker_task(client, target_chat, m.from_user.id, term, is_admin=True)
    if started:
        await m.reply(SC(
            f"🚀 <b>FPO auto-upload started</b> for <code>{term}</code>\n\n"
            "Every video on this page (all pages) is scraped and uploaded to the configured channel/group as it's found. "
            "Use /stopupload to stop."), parse_mode=ParseMode.HTML)
    else:
        await m.reply(SC("⚠️ <b>Couldn't start — try again in a moment.</b>"), parse_mode=ParseMode.HTML)
    return True


async def _maybe_start_actor_bulk_from_link(client: Client, m: Message, link: str) -> bool:
    """If `link` is a performer listing-page URL, starts the same bulk
    "/autoupload <name>" flow for it and returns True. Admin-only, same as
    /autoupload itself — for anyone else this just returns False so the
    link falls through to the normal single-video menu as before."""
    if not m.from_user or m.from_user.id not in ADMINS:
        return False
    path = urlparse(link).path
    match = _MODEL_PAGE_RE.match(path)
    if not match and fpo.is_fpo_link(link):
        match = _FPO_SEARCH_PAGE_RE.match(path)
    if not match:
        return False

    actor_name = match.group(1).replace("-", " ").replace("_", " ").title()
    target_chat = await _autoupload_target(client, m)
    if target_chat is None:
        return True

    await auto_scraper.stop_worker_task(target_chat, auto_scraper.FAPHOUSE_FAMILY)
    await auto_scraper.begin_site_run(target_chat, "actor")
    status_msg = await m.reply(SC(f"🔍 <b>Looking up \"{actor_name}\"...</b>"), parse_mode=ParseMode.HTML)
    started = auto_scraper.start_actor_worker_task(
        client, target_chat, m.from_user.id, actor_name, is_admin=(m.from_user.id in ADMINS),
    )
    if started:
        await status_msg.edit_text(SC(
            f"🚀 <b>Auto-upload started for \"{actor_name}\".</b>\n\n"
            "Every video on this performer's page (all pages) is scraped and "
            "uploaded to the configured channel/group as it's found. Use /stopupload to stop."
        ), parse_mode=ParseMode.HTML)
    else:
        await status_msg.edit_text(SC("⚠️ <b>Couldn't start — try again in a moment.</b>"), parse_mode=ParseMode.HTML)
    return True


async def _maybe_start_studio_bulk_from_link(client: Client, m: Message, link: str) -> bool:
    """Same idea as _maybe_start_actor_bulk_from_link, but for a studio/
    production-company page URL."""
    if not m.from_user or m.from_user.id not in ADMINS:
        return False
    path = urlparse(link).path
    match = _STUDIO_PAGE_RE.match(path)
    if not match:
        return False

    studio_name = match.group(1).replace("-", " ").replace("_", " ").title()
    target_chat = await _autoupload_target(client, m)
    if target_chat is None:
        return True

    await auto_scraper.stop_worker_task(target_chat, auto_scraper.FAPHOUSE_FAMILY)
    await auto_scraper.begin_site_run(target_chat, "studio")
    status_msg = await m.reply(SC(f"🔍 <b>Looking up \"{studio_name}\"...</b>"), parse_mode=ParseMode.HTML)
    started = auto_scraper.start_studio_worker_task(
        client, target_chat, m.from_user.id, studio_name, is_admin=(m.from_user.id in ADMINS),
    )
    if started:
        await status_msg.edit_text(SC(
            f"🚀 <b>Auto-upload started for \"{studio_name}\".</b>\n\n"
            "Every video from this studio (all pages) is scraped and "
            "uploaded to the configured channel/group as it's found. Use /stopupload to stop."
        ), parse_mode=ParseMode.HTML)
    else:
        await status_msg.edit_text(SC("⚠️ <b>Couldn't start — try again in a moment.</b>"), parse_mode=ParseMode.HTML)
    return True


@app.on_message(LINK_HANDLER_FILTER)
async def link_handler(client: Client, m: Message):
    if await is_banned(m.from_user.id):
        return await m.reply(SC("🚫 You are banned from using this bot."))

    text = m.text or m.caption or ""
    combined = (terabox.extract_terabox_links(text) + faphouse.extract_faphouse_links(text)
                + fpo.extract_fpo_links(text)
        + mat6tube.extract_mat6tube_links(text)
        + spankbang.extract_spankbang_links(text)
        + jav_sites.extract_jav_links(text)
                + diskwala.extract_diskwala_links(text)
                + ytdlp.extract_supported_links(text) + pf.extract_supported_links(text))
    seen = set()
    links = [link for link in combined if not (link in seen or seen.add(link))]
    # SpankBang search / category / trending pages are listings, not videos -- yt-dlp can't download them
    skipped_sb = [l for l in links if spankbang.is_spankbang_link(l) and not spankbang.is_video_link(l)]
    if skipped_sb:
        links = [l for l in links if l not in skipped_sb]
        if not links:
            if m.from_user.id in ADMINS:
                # Admin pasted a SpankBang listing link (search / pornstar / channel / trending ...): same as
                # /autouploadspankbang <that link> -- start uploading the whole listing instead of refusing.
                m.text = f"/autouploadspankbang {skipped_sb[0]}"
                return await autouploadspankbang_cmd(client, m)
            return await m.reply(SC(
                "<b>⚠️ That's a SpankBang listing page, not a video.</b>\n"
                "Send a video link (<code>spankbang.com/&lt;id&gt;/video/...</code>)."
                + ("\nAdmins: <code>/autouploadspankbang &lt;performer name | listing link | trending | new | popular | search keyword&gt;</code> uploads a whole listing." if m.from_user.id in ADMINS else "")
            ), parse_mode=ParseMode.HTML)
    if not links:
        # Last resort: any of yt-dlp's ~1800 dedicated site extractors,
        # not just the 8 hardcoded above — only tried once everything
        # more specific has already had a chance to claim this link, so
        # this doesn't add the extractor-list cost to every message.
        links = ytdlp.extract_generic_links(text)
    if not links:
        if text.startswith("/"):
            return await m.reply(
                SC("<b>❓ Unknown command.</b>\nType /help to see available commands."),
                parse_mode=ParseMode.HTML,
            )
        if _BOTFATHER_TOKEN_RE.search(text):
            return  # leftover bot-token/creation text, not a real query — ignore silently
        # Bug fix: a plain phrase like "lofi song" (no URL, no trigger word)
        # used to fall straight through to the generic "not a link" message
        # below — YouTube search only ever fired for text containing an
        # explicit "yt"/"search" keyword. Since this point is only reached
        # once every dedicated extractor has already found nothing, any
        # non-URL text here is exactly what "search YouTube for it" is for.
        if await ytsearch.try_plain_text_search(client, m, make_button=make_button, BTN_PRIMARY=BTN_PRIMARY):
            return
        # An unsupported *link* gets no reply at all - the "Bhai ye kaunsa link hai?" message is only for plain text.
        if re.search(r"https?://\S+|www\.\S+", text, re.IGNORECASE):
            logger.info(f"[link_handler] ignoring unsupported link from {m.from_user.id}: {text[:200]!r}")
            return
        return await m.reply(NOT_A_LINK_TEXT, parse_mode=ParseMode.HTML)
    for i, link in enumerate(links):
        if await _maybe_start_fpo_listing_from_link(client, m, link):
            continue
        if await _maybe_start_actor_bulk_from_link(client, m, link):
            continue
        if await _maybe_start_studio_bulk_from_link(client, m, link):
            continue
        tag = f"[{i+1}/{len(links)}]" if len(links) > 1 else ""
        await process_link(client, m, link, tag)


async def _send_terabox_folder(client: Client, chat_id: int, user_id: int, link: str, files: list):
    """Downloads and sends every file in a TeraBox folder share, one at a
    time — this is what makes 'paste a folder link' actually deliver the
    whole folder instead of just ever reaching the same first file the
    old quality-menu path was stuck on (see get_all_folder_files's
    docstring in terabox_downloader.py). Not video-only: whatever
    extension each file actually is decides which send_* method it goes
    through, since a TeraBox share can be an archive/doc/image just as
    easily as a video.

    Takes chat_id/user_id directly (rather than a Message to .reply()
    off of) so it can be called both from the normal message flow
    (process_link) and from the download-failure fallback inside the
    Download-button callback flow (download_video), which only has a
    CallbackQuery — not a fresh Message — to work with."""
    status = await client.send_message(
        chat_id,
        SC(f"📁 <b>Folder detected — {len(files)} file(s) found.</b>\nDownloading and sending each one..."),
        parse_mode=ParseMode.HTML,
    )
    sent, failed = 0, 0
    for idx, f in enumerate(files, start=1):
        name = f["name"]
        work_dir = os.path.join(DOWNLOAD_DIR, uuid.uuid4().hex[:12])
        try:
            os.makedirs(work_dir, exist_ok=True)
            try:
                await status.edit_text(
                    SC(f"📥 <b>({idx}/{len(files)}) Downloading:</b>\n<code>{name}</code>"),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

            out_path = os.path.join(work_dir, name)
            final_path = await asyncio.to_thread(
                # f["url"] is already the flowvideoplayer CDN link resolved by
                # get_all_folder_files — pass it as stream_url so download_video
                # uses it directly (tier 1) instead of re-resolving from scratch.
                terabox.download_video, link, out_path, None, f["url"],
            )

            caption = await build_caption(
                name=os.path.basename(final_path),
                size_bytes=os.path.getsize(final_path),
                dl_seconds=0, ul_seconds=0,
                user_id=user_id, source_link=link, quality_label="Original",
            )
            ext = os.path.splitext(final_path)[1].lower()
            if ext in (".mp4", ".mkv", ".mov", ".webm", ".avi"):
                await client.send_video(chat_id, final_path, caption=SC(caption), parse_mode=ParseMode.HTML)
            elif ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
                await client.send_photo(chat_id, final_path, caption=SC(caption), parse_mode=ParseMode.HTML)
            else:
                await client.send_document(chat_id, final_path, caption=SC(caption), parse_mode=ParseMode.HTML)
            sent += 1
        except Exception as e:
            logger.error(f"terabox folder: failed on {name!r}: {e}")
            failed += 1
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    try:
        await status.edit_text(
            SC(f"✅ <b>Folder complete.</b>\nSent: {sent} | Failed: {failed}"),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


MAX_PLAYLIST_ENTRIES = 50
PLAYLIST_STALL_TIMEOUT = 60
PLAYLIST_HARD_TIMEOUT = 20 * 60
# BUG FIX: the download phase below has both a hard timeout and stall
# detection (manual polling of _progress["downloaded"]) — the upload
# phase had neither, just a bare `await client.send_video(...)`. A single
# stuck/slow upload (network hiccup, a Telegram API stall, anything)
# therefore blocked the ENTIRE playlist forever with no error, no
# progress, and no way to move on to the next video — looking exactly
# like "video downloads fine, then nothing ever uploads and the whole
# playlist run just goes silent". This bounds it the same way the
# download side already is.
PLAYLIST_UPLOAD_TIMEOUT = 20 * 60


async def _send_youtube_playlist(client: Client, m: Message, link: str):
    """Bulk-downloads every video in a YouTube playlist link, one at a
    time, each forced to the closest-to-720p quality available (per your
    request — playlist videos always aim for 720p rather than each one
    picking its own "Auto/Best", which could vary wildly across a
    playlist and eat far more bandwidth/storage than intended for a bulk
    job). Same download→progress→upload shape as auto_scraper.py's other
    bulk-downloader (stall/hard timeout watchdog, live progress, split
    upload for >2GB files) — this is the same kind of loop, just for a
    YouTube playlist instead of a site listing."""
    ACTIVE_TASKS[m.from_user.id] = asyncio.current_task()
    status_msg = await m.reply(SC("<b>🔎 Reading playlist...</b>"), parse_mode=ParseMode.HTML)

    try:
        entries = await asyncio.to_thread(ytdlp.get_playlist_entries, link, MAX_PLAYLIST_ENTRIES)
    except Exception as e:
        logger.warning(f"[playlist] couldn't read entries for {link}: {e}")
        await status_msg.edit_text(
            SC(f"<b>❌ Couldn't read this playlist:</b>\n<code>{html.escape(str(e))}</code>"),
            parse_mode=ParseMode.HTML,
        )
        return

    if not entries:
        await status_msg.edit_text(SC("<b>⚠️ No videos found in this playlist.</b>"), parse_mode=ParseMode.HTML)
        return

    total_n = len(entries)
    await status_msg.edit_text(
        SC(f"<b>📃 Found {total_n} video(s) — downloading at 720p (closest available), one at a time.</b>\n"
           "Use /cancel to stop."),
        parse_mode=ParseMode.HTML,
    )

    done_n = 0
    failed_n = 0
    failure_samples = []  # (idx, title, error) — surfaced in the final summary
    for idx, entry in enumerate(entries, start=1):
        video_url = entry["url"]
        raw_title = entry.get("title") or f"video_{idx}"
        safe_title = re.sub(r'[\\/:*?"<>|\r\n]+', " ", raw_title).strip()
        safe_title = re.sub(r"\s+", " ", safe_title)[:150].strip() or f"video_{idx}"
        name = f"{safe_title}.mp4"
        work_dir = os.path.join(DOWNLOAD_DIR, f"playlist_{uuid.uuid4().hex[:10]}")

        try:
            os.makedirs(work_dir, exist_ok=True)
            out_path = os.path.join(work_dir, name)
            await status_msg.edit_text(
                SC(f"<b>🔎 [{idx}/{total_n}] Resolving:</b>\n<code>{html.escape(safe_title)}</code>"),
                parse_mode=ParseMode.HTML,
            )
            # FIX: removed ytdlp.pick_quality_near() call — yt-dlp handles
            # quality selection internally; the extra round-trip was wasted
            # time and its stream_url was silently ignored by download_video()
            # when yt-dlp already knew better formats to use.

            _progress = {"pct": None, "downloaded": 0, "total_bytes": None, "done": False,
                         "error": None, "connecting": True,
                         "actual_path": None}  # FIX: capture real output path

            def _on_progress(info, _p=_progress):
                _p["pct"] = info.get("pct")
                _p["downloaded"] = info.get("downloaded_bytes", 0)
                _p["total_bytes"] = info.get("total_bytes")
                _p["connecting"] = info.get("connecting", False)

            def _blocking_dl(_vu=video_url, _op=out_path):
                try:
                    actual_path, _elapsed = ytdlp.download_video(
                        _vu, _op, on_progress=_on_progress,
                        stream_url="bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]/best",
                    )
                    if actual_path:
                        _progress["actual_path"] = actual_path
                except Exception as exc:
                    logger.warning(f"[playlist] _blocking_dl failed for {_vu}: {exc}")
                    _progress["error"] = exc
                finally:
                    _progress["done"] = True

            dl_tracker = ProgressTracker(status_msg, "Downloading", f"[{idx}/{total_n}] {name}", quality="~720p")
            dl_task = asyncio.create_task(asyncio.to_thread(_blocking_dl))
            dl_start = time.time()
            last_bytes = 0
            last_bytes_ts = dl_start
            smoothed_total = 0

            while not _progress["done"]:
                now_t = time.time()
                if now_t - dl_start > PLAYLIST_HARD_TIMEOUT:
                    dl_task.cancel()
                    _progress["done"] = True
                    _progress["error"] = RuntimeError(f"Timed out after {int(now_t - dl_start)}s.")
                    break
                cur_bytes = _progress["downloaded"]
                if cur_bytes > last_bytes:
                    last_bytes, last_bytes_ts = cur_bytes, now_t
                elif not _progress["connecting"] and (now_t - last_bytes_ts) > PLAYLIST_STALL_TIMEOUT:
                    dl_task.cancel()
                    _progress["done"] = True
                    _progress["error"] = RuntimeError(f"Stalled ({PLAYLIST_STALL_TIMEOUT}s no data).")
                    break

                pct = _progress["pct"]
                real_total = _progress.get("total_bytes")
                if real_total:
                    smoothed_total = real_total
                elif pct and pct > 2 and cur_bytes > 0:
                    raw_total = int(cur_bytes / (pct / 100))
                    smoothed_total = raw_total if not smoothed_total else int(smoothed_total * 0.8 + raw_total * 0.2)
                await dl_tracker.update(cur_bytes, smoothed_total)
                await asyncio.sleep(1)

            try:
                await dl_task
            except asyncio.CancelledError:
                pass

            if _progress["error"]:
                raise _progress["error"]

            # FIX: use the actual path returned by yt-dlp (sanitized filename
            # + real extension). Fall back to out_path only if yt-dlp didn't
            # return a path (shouldn't happen, but be safe).
            actual_path = _progress.get("actual_path") or out_path

            # Try glob fallback if neither path exists yet
            if not os.path.exists(actual_path):
                base = os.path.splitext(out_path)[0]
                import glob as _glob
                candidates = _glob.glob(base + ".*")
                if candidates:
                    actual_path = candidates[0]

            if not os.path.exists(actual_path) or os.path.getsize(actual_path) < 1024:
                raise RuntimeError("Downloaded file is empty or missing.")

            file_size = os.path.getsize(actual_path)
            ul_tracker = ProgressTracker(status_msg, "Uploading", f"[{idx}/{total_n}] {name}", quality="~720p")

            # BUG FIX: caption here used to be just the title + index — no
            # size, duration, or quality, unlike the single-video path's
            # full build_caption() (size/duration/quality/source/downloaded-
            # by, plus the auto-delete note and the user's custom caption
            # template if they've set one via /set_caption). Reusing that
            # same builder here — the only reasons it needed a video_url).
            vid_duration, vid_width, vid_height = await asyncio.to_thread(get_video_metadata, actual_path)
            caption = await build_caption(
                name=name,
                size_bytes=file_size,
                dl_seconds=time.time() - dl_start,
                ul_seconds=0,  # filled in per-part below via edit_caption, same as the single-file path
                user_id=m.from_user.id,
                source_link=video_url,
                quality_label="~720p",
                duration_seconds=vid_duration,
                title=safe_title,
                downloaded_by_username=m.from_user.username,
                downloaded_by_name=(m.from_user.first_name or "") + (f" {m.from_user.last_name}" if m.from_user.last_name else ""),
            )
            caption += f"\n📃 {idx}/{total_n}"

            # BUG FIX: this path never generated a thumbnail at all — no
            # `thumb=` was ever passed to send_video() below, unlike the
            # single-video download path (which runs generate_thumbnail +
            # finalize_thumbnail and passes the result). Without one,
            # whether a video showed a proper preview or a blank white one
            # in Telegram came down to pure luck: only if that particular
            # file happened to carry its own embedded thumbnail (e.g. one
            # yt-dlp pulled from the source and muxed in) would Telegram
            # have anything to show — exactly why one video in a playlist
            # could look fine and the very next one show nothing. Grab a
            # real frame ourselves so every video in the playlist gets a
            # consistent thumbnail, same as the single-video path.
            thumb_path = actual_path + "_thumb.jpg"
            got_thumb = await asyncio.to_thread(generate_thumbnail, actual_path, thumb_path)
            if got_thumb:
                got_thumb = await asyncio.to_thread(finalize_thumbnail, thumb_path)

            parts = [actual_path]
            if file_size > MAX_FILE_SIZE:
                # NOTE: split_upload has no upload_split() function — the
                # two other call sites in this codebase referencing that
                # name (main.py's single-video path and auto_scraper.py's
                # other bulk-downloader) would both hit an AttributeError
                # the moment a file over MAX_FILE_SIZE actually reached
                # them; pre-existing bugs, not something this playlist
                # feature should copy. split_video_file() is the real,
                # working function — same one the single-video path above
                # ultimately uses — so that's what this calls directly.
                parts = await asyncio.to_thread(
                    split_upload.split_video_file, actual_path, DOWNLOAD_DIR, uuid.uuid4().hex[:10], SPLIT_PART_TARGET_BYTES,
                )
                if len(parts) <= 1:
                    raise RuntimeError("File is over 2GB and couldn't be split automatically.")

            for p_idx, part_path in enumerate(parts, start=1):
                part_caption = caption if len(parts) == 1 else f"{caption}\n✂️ Part {p_idx}/{len(parts)}"
                try:
                    await asyncio.wait_for(
                        client.send_video(
                            chat_id=m.chat.id, video=part_path, caption=part_caption,
                            parse_mode=ParseMode.HTML, supports_streaming=True,
                            thumb=thumb_path if got_thumb else None,
                            duration=vid_duration or 0,
                            width=vid_width or 0,
                            height=vid_height or 0,
                            # BUG FIX: this used to be
                            # `lambda cur, tot: asyncio.ensure_future(ul_tracker.update(cur, tot))`
                            # — Pyrogram's upload progress callback isn't
                            # always invoked from the main event-loop
                            # thread (confirmed live: "RuntimeError: There
                            # is no current event loop in thread
                            # 'Handler_0'" on every single playlist
                            # upload), and asyncio.ensure_future() requires
                            # a running loop in whatever thread it's
                            # called from — that RuntimeError propagated
                            # straight up through send_video(), so EVERY
                            # video in EVERY playlist finished downloading
                            # and then failed at the upload step, 100% of
                            # the time. ul_tracker.update is already an
                            # async function — passing it directly is the
                            # same safe pattern the single-video upload
                            # path (main.py's non-playlist download_video)
                            # already uses; Pyrogram awaits it properly
                            # itself instead of us trying to schedule it
                            # by hand.
                            progress=ul_tracker.update,
                        ),
                        timeout=PLAYLIST_UPLOAD_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    raise RuntimeError(
                        f"Upload stalled for over {PLAYLIST_UPLOAD_TIMEOUT // 60} minutes — skipping this video."
                    )
            done_n += 1
        except asyncio.CancelledError:
            shutil.rmtree(work_dir, ignore_errors=True)
            await status_msg.edit_text(
                SC(f"<b>🛑 Cancelled.</b> {done_n}/{total_n} uploaded before stopping."),
                parse_mode=ParseMode.HTML,
            )
            return
        except Exception as e:
            failed_n += 1
            logger.warning(f"[playlist] {video_url} failed: {type(e).__name__}: {e}")
            if len(failure_samples) < 3:
                failure_samples.append((idx, safe_title, f"{type(e).__name__}: {str(e)[:120]}"))
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    summary = f"<b>✅ Playlist done — {done_n}/{total_n} uploaded.</b>"
    if failed_n:
        summary += f"\n⚠️ {failed_n} video(s) failed and were skipped."
        for f_idx, f_title, f_err in failure_samples:
            summary += f"\n  [{f_idx}] {html.escape(f_title)}: <code>{html.escape(f_err)}</code>"
        if failed_n > len(failure_samples):
            summary += f"\n  ...and {failed_n - len(failure_samples)} more (see server logs)."
    await status_msg.edit_text(SC(summary), parse_mode=ParseMode.HTML)


async def process_link(client: Client, m: Message, link: str, tag: str):
    if ytdlp.is_youtube_playlist_link(link):
        # Same "detect the bulk case before the normal single-item menu"
        # shape as the terabox-folder check right below — a playlist
        # link never makes sense as a single Download/Stream choice.
        await _send_youtube_playlist(client, m, link)
        return

    if terabox.is_terabox_link(link):
        # Check folder-ness FIRST (cheap: shorturlinfo + share/list, no
        # per-file downloads yet) so a real folder link never falls
        # through to the normal Download/Stream quality menu — that menu
        # only makes sense for a single video and errors out
        # ("flowvideoplayer: no download_url in response") when handed a
        # folder. Previously we only discovered it was a folder AFTER
        # trying to fully resolve every file, so any resolve hiccup sent
        # it straight to the broken single-file menu, Stream button and
        # all. Now: confirmed folder -> always the folder path (send the
        # files, or a clear "couldn't fetch it right now" message) and
        # return, never the quality menu. A single-file share (or a
        # domain this Baidu-PCS tier can't read at all) falls through to
        # the normal menu below unchanged.
        try:
            probe = await asyncio.to_thread(terabox.probe_terabox_share, link)
        except Exception as e:
            logger.warning(f"terabox folder probe failed for {link}: {e}")
            probe = None

        if probe and probe.get("is_folder"):
            try:
                files = await asyncio.to_thread(terabox.get_all_folder_files, link)
            except Exception as e:
                logger.warning(f"terabox get_all_folder_files failed for {link}: {e}")
                files = None
            if files:
                await _send_terabox_folder(client, m.chat.id, m.from_user.id, link, files)
            else:
                await m.reply(
                    SC(
                        "📁 <b>This looks like a folder link</b>, but its files "
                        "couldn't be fetched right now.\nTry again in a bit, or "
                        "send a direct file link instead."
                    ),
                    parse_mode=ParseMode.HTML,
                )
            return

    link_id = uuid.uuid4().hex[:10]
    if len(LINK_CACHE) > 800:
        for k in list(LINK_CACHE.keys())[:200]:
            LINK_CACHE.pop(k, None)
    LINK_CACHE[link_id] = link
    await m.reply(
        SC(f"<b>Link received {tag}</b>\n<code>{link}</code>\n\nChoose an action:"),
        reply_markup=link_menu_markup(link_id, link),
        parse_mode=ParseMode.HTML,
    )


CALLBACK_HANDLER_FILTER = filters.regex(r"^(dlq|qpage|q|qall|stream|cancel|desc|back)\||^descclose$")


def _format_duration(seconds) -> str:
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "Unknown"
    if seconds <= 0:
        return "Unknown"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


async def show_quality_menu(client: Client, query, link_id: str, link: str, page: int = 0):
    """Shown after tapping Download — resolves the available qualities
    (like a YouTube downloader) so the person picks a resolution before
    anything downloads, instead of always getting whatever "auto" is.

    page: which page of *resolution* options to render (0-indexed) —
    only relevant when there are more than PAGE_SIZE of them, e.g. a
    terabox folder link where every file in the folder comes back as its
    own "option" (see terabox_downloader.get_available_qualities) rather
    than an actual quality distinction. Re-rendering a different page of
    an already-fetched list (via the qpage| callback) skips the whole
    fetch above and jumps straight to the QUALITY_CACHE branch below."""
    cached_variants = QUALITY_CACHE.get(link_id)
    if cached_variants is not None:
        variants = cached_variants
    else:
        try:
            await query.message.edit_text(SC("<b>🔍 Fetching available qualities...</b>"), parse_mode=ParseMode.HTML)
        except Exception:
            pass

        # Live ticker so the user sees movement during the 10-30s quality
        # resolve (some backends do several sequential requests before
        # returning anything) instead of the message just sitting frozen.
        resolve_start = time.time()

        async def _tick_quality():
            while True:
                await asyncio.sleep(3)
                elapsed = int(time.time() - resolve_start)
                try:
                    await query.message.edit_text(
                        SC(f"<b>🔍 Fetching available qualities... ({elapsed}s)</b>\n"
                           "<i>Resolving stream sources...</i>"),
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass

        tick_task = asyncio.create_task(_tick_quality())

        backend = _downloader_for(link)
        # FanclubLockedError is faphouse-specific — getattr with empty-tuple
        # fallback so the except clause below is a no-op for other backends.
        # Use the CURRENT backend's own class (fpo_downloader/faphouse_downloader each define
        # one) - looking only at faphouse's meant an fpo "private video" error fell into the
        # generic branch below.
        _fanclub_err_cls = getattr(backend, "FanclubLockedError", ())
        try:
            variants = await asyncio.to_thread(backend.get_available_qualities, link)
        except _fanclub_err_cls:
            tick_task.cancel()
            await query.message.edit_text(
                SC("<b>This video needs a separate per-studio Fanclub subscription.</b>\n\n"
                   "The page only has a short trailer — no full video stream available.\n"
                   "Try a different link from the same site.")
                if backend is faphouse else
                SC("<b>This video is private / members-only.</b>\n\n"
                   "The account the bot is logged in with can't watch it.\n"
                   "Try a different link from the same site."),
                parse_mode=ParseMode.HTML,
            )
            return
        except Exception as e:
            # No try/except here before meant a failure on this call (quite
            # possible for the porn_fetch_downloader-backed sites — their
            # underlying packages change often) left the message stuck on
            # "Checking available qualities..." forever, since the exception
            # just crashed the callback handler silently instead of ever
            # reaching the edit_text below. Logging the real exception here
            # (not just a generic message) is what makes the next one of
            # these actually diagnosable instead of a repeat of "it just
            # hangs" with no trace to go on.
            logger.error(f"get_available_qualities failed for {link}: {e}", exc_info=True)
            await query.message.edit_text(
                SC(f"<b>Couldn't fetch qualities for this link.</b>\n<code>{html.escape(_strip_ansi(str(e))[:500])}</code>\n\n"
                   "Try again in a bit, or a different link from the same site."),
                parse_mode=ParseMode.HTML,
            )
            return
        finally:
            tick_task.cancel()

        if not variants:
            await query.message.edit_text(
                SC("<b>No downloadable quality found for this link.</b>"),
                parse_mode=ParseMode.HTML,
            )
            return

        if len(variants) == 1:
            # Only one option to pick from (e.g. flowvideoplayer's terabox
            # response — a single download_url, no per-quality breakdown) —
            # showing a menu with exactly one button just adds a pointless
            # extra tap, so go straight to download_video() instead, the same
            # call the "q" callback below makes once a quality IS picked.
            only = variants[0]
            await download_video(client, query, link, quality_url=only.get("url"), quality_label=only["label"])
            return

        QUALITY_CACHE[link_id] = variants
        # Cleanup to prevent unbounded growth across a long-running process —
        # these are only ever added to, never individually expired, so a busy
        # bot would otherwise accumulate entries forever.
        if len(QUALITY_CACHE) > 500:
            for k in list(QUALITY_CACHE.keys())[:100]:
                QUALITY_CACHE.pop(k, None)

    # Same title/author/duration panel across every site, not just the
    # ones that happen to have a get_page_meta() — sites without one (or
    # where the fetch fails) just show "Unknown", same as the ones that
    # have it but don't know a given field. Skipped on page > 0 re-renders
    # to avoid an extra fetch for something already shown once.
    title, author, duration_str = "Video", "Unknown", "Unknown"
    page_meta = {}
    if page == 0:
        backend = _downloader_for(link)
        if hasattr(backend, "get_page_meta"):
            try:
                page_meta = await asyncio.to_thread(backend.get_page_meta, link) or {}
            except Exception as e:
                logger.warning(f"get_page_meta failed for {link}: {e}")
        title = page_meta.get("title") or urlparse_path_name(link).replace("-", " ").replace("_", " ").title() or "Video"
        author = page_meta.get("author") or "Unknown"
        duration_str = _format_duration(page_meta.get("duration"))

    resolution_variants = [(i, v) for i, v in enumerate(variants) if v["label"] != "Auto (Best)"]
    auto_entries = [(i, v) for i, v in enumerate(variants) if v["label"] == "Auto (Best)"]

    # Pagination only kicks in once there's more than one page's worth —
    # a normal 2-4-quality video never sees a Prev/Next row at all, only
    # something like a terabox folder full of files does.
    PAGE_SIZE = 15
    total_pages = max(1, (len(resolution_variants) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    page_slice = resolution_variants[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    # Auto (Best), if any, only shown on the first page — repeating it on
    # every page would just be clutter with no new information.
    shown_auto_entries = auto_entries if page == 0 else []

    quality_lines = "\n".join(f"✅ {v['label']}" for _, v in page_slice + shown_auto_entries)
    page_note = f"\n\n📄 Page {page + 1}/{total_pages}" if total_pages > 1 else ""
    caption = (
        f"🚀 <b>{title}</b>\n\n"
        f"👤 <b>BY:</b> {author}\n"
        f"⏱ <b>DURATION:</b> {duration_str}\n\n"
        f"<b>AVAILABLE QUALITIES:</b>\n{quality_lines}{page_note}\n\n"
        "<i>Tap a quality below to download:</i>"
    )

    # One full-width button per quality (not a 2-per-row grid) — easier
    # to read the "label - label mp4" text at a glance, matches the
    # layout every site now shares.
    buttons = []
    for i, v in page_slice:
        buttons.append([make_button(SC(f"🎬 {v['label']} - {v['label']} mp4"), callback_data=f"q|{link_id}|{i}", style=BTN_PRIMARY)])
    for i, v in shown_auto_entries:
        buttons.append([make_button(SC(f"⚡ {v['label']}"), callback_data=f"q|{link_id}|{i}", style=BTN_PRIMARY)])

    # MP3 audio-only options — YouTube links only, since this is built on
    # yt-dlp's own "bestaudio" + FFmpegExtractAudio postprocessor, which
    # needs a real audio stream to extract from. Every other backend here
    # (faphouse/terabox/etc.) only ever hands back muxed video URLs, so
    # offering this for them would either silently re-download the whole
    # video just to throw the picture away, or fail outright — neither of
    # which is what tapping "MP3" should do. Appended to the SAME
    # `variants` list (not a separate menu/callback) so the existing
    # q|{link_id}|{i} handler and QUALITY_CACHE indexing both work
    # completely unchanged — download_video()/ytdlp_downloader.py are the
    # only two places that need to know "mp3:<bitrate>" is special (see
    # their own comments on this).
    mp3_buttons = []
    if ytdlp._is_youtube(link):
        mp3_start_index = len(variants)
        for bitrate in (64, 128, 320):
            variants.append({"label": f"MP3 {bitrate}kbps", "height": None, "url": f"mp3:{bitrate}"})
        QUALITY_CACHE[link_id] = variants
        mp3_buttons = [
            make_button(SC(f"🎵 MP3 {b}kbps"), callback_data=f"q|{link_id}|{mp3_start_index + n}", style=BTN_PRIMARY)
            for n, b in enumerate((64, 128, 320))
        ]

    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(make_button(SC("◀️ Prev"), callback_data=f"qpage|{link_id}|{page - 1}", style=BTN_PRIMARY))
        if page < total_pages - 1:
            nav_row.append(make_button(SC("Next ▶️"), callback_data=f"qpage|{link_id}|{page + 1}", style=BTN_PRIMARY))
        if nav_row:
            buttons.append(nav_row)

    # MP3 row(s) shown on every page (small/fixed list, not worth its own
    # pagination) but only once — page 0 only, same "don't repeat on
    # every page" reasoning as Auto (Best) above.
    if mp3_buttons and page == 0:
        buttons.append(mp3_buttons[:2])
        buttons.append(mp3_buttons[2:])

    # "Select All" only makes sense for a terabox folder link (every
    # entry here is a DIFFERENT FILE, not a quality tier of the same
    # video — see terabox_downloader.get_available_qualities' docstring)
    # — showing it for a normal multi-quality video (720p/480p/etc. of
    # the SAME video) would be actively wrong, downloading the same
    # video three times over instead of what "select all" implies.
    if _downloader_for(link) is terabox and len(resolution_variants) > 1:
        buttons.append([make_button(
            SC(f"✅ Select All ({len(resolution_variants)} files)"),
            callback_data=f"qall|{link_id}", style=BTN_PRIMARY,
        )])

    buttons.append([
        make_button(SC("⬅️ Back"), callback_data=f"back|{link_id}", style=BTN_PRIMARY),
        make_button(SC("❌ Cancel"), callback_data=f"cancel|{link_id}", style=BTN_DANGER),
    ])
    markup = InlineKeyboardMarkup(buttons)

    poster_url = page_meta.get("poster_url")
    if poster_url:
        # Telegram can't edit a text message into a photo message (or
        # vice versa) — has to be a new message. Sent first, then the old
        # text one is removed, so there's never a moment with both or
        # neither visible if the photo send fails partway through.
        try:
            await client.send_photo(
                query.message.chat.id, photo=poster_url,
                caption=SC(caption), parse_mode=ParseMode.HTML, reply_markup=markup,
            )
            await query.message.delete()
            return
        except Exception as e:
            logger.warning(f"send_photo with poster_url failed for {link}, falling back to text: {e}")

    await query.message.edit_text(
        SC(caption),
        reply_markup=markup,
        parse_mode=ParseMode.HTML,
    )


@app.on_callback_query(CALLBACK_HANDLER_FILTER)
async def callback_handler(client: Client, query):
    parts = query.data.split("|")
    action = parts[0]
    # "descclose" (see CALLBACK_HANDLER_FILTER's second alternative,
    # ^descclose$) deliberately has no "|id" part — parts[1] used to be
    # accessed unconditionally here regardless, so every tap of the
    # description's Back button crashed with an IndexError before ever
    # reaching the "descclose" branch below: the button looked completely
    # unresponsive because the crash happened before any reply was sent.
    link_id = parts[1] if len(parts) > 1 else None
    link = LINK_CACHE.get(link_id) if link_id else None

    if action == "cancel":
        await query.message.edit_text(SC("<b>Cancelled.</b>"), parse_mode=ParseMode.HTML)
        LINK_CACHE.pop(link_id, None)
        QUALITY_CACHE.pop(link_id, None)
        if len(LINK_CACHE) > 800:
            for k in list(LINK_CACHE.keys())[:200]:
                LINK_CACHE.pop(k, None)
        return

    if action == "back":
        # Returns from the quality menu to the original Download/Stream/
        # Cancel menu. link_id/LINK_CACHE is unaffected by show_quality_menu
        # (only QUALITY_CACHE gets written there), so the same link is
        # still resolvable here — no re-fetch needed, just redraw the menu.
        if not link:
            await query.message.edit_text(SC("<b>Link expired, please resend it.</b>"), parse_mode=ParseMode.HTML)
            return
        markup = link_menu_markup(link_id, link)
        text = SC(f"<b>Link received</b>\n<code>{link}</code>\n\nChoose an action:")
        try:
            await query.message.edit_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
        except Exception:
            # The quality menu sent a photo message (poster_url case) —
            # Telegram can't edit a photo message into a text one, so send
            # fresh and drop the old one, same pattern show_quality_menu
            # itself uses for the reverse direction.
            await client.send_message(query.message.chat.id, text, reply_markup=markup, parse_mode=ParseMode.HTML)
            try:
                await query.message.delete()
            except Exception:
                pass
        return

    if action == "desc":
        # Own id namespace (DESC_CACHE, not LINK_CACHE) — falls through
        # to the "Link expired" check below otherwise, which is the
        # wrong message for this button.
        desc_id = parts[1]
        entry = DESC_CACHE.get(desc_id)
        if not entry:
            # In-memory dict doesn't survive a restart — this is the
            # normal case for any video sent before the bot's last
            # restart, not actually "gone". Fall back to the durable copy.
            try:
                entry = await get_cached_description(desc_id)
            except Exception as e:
                logger.warning(f"get_cached_description failed for {desc_id}: {e}")
                entry = None
            if entry:
                DESC_CACHE[desc_id] = entry  # warm the fast path for next time
        if not entry:
            await query.answer(SC("Description no longer available."), show_alert=True)
            return
        site_line = f"🌐 <b>{html.escape(entry['site_name'])}</b>\n\n" if entry.get("site_name") else ""
        title_line = f"<b>{html.escape(entry['title'])}</b>\n\n" if entry.get("title") else ""
        # FIX: this reply had no reply_markup at all — once opened, there
        # was nothing to tap to get rid of it; it just sat there as a
        # dead-end message forever. This is a standalone reply (not an
        # edit of an existing menu — the button that opens it lives on
        # the already-uploaded video message, which is unaffected either
        # way), so "back" here just means a Back button that deletes it.
        await query.message.reply(
            SC(f"{site_line}{title_line}{html.escape(entry.get('description') or '')}")[:4096],
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[make_button(SC("🔙 Back"), callback_data="descclose", style=BTN_PRIMARY)]]),
        )
        await query.answer()
        return

    if action == "descclose":
        try:
            await query.message.delete()
        except Exception:
            pass
        await query.answer()
        return

    if not link:
        await query.answer(SC("Link expired, please resend it."), show_alert=True)
        return

    if await is_banned(query.from_user.id):
        await query.answer(SC("You are banned from using this bot."), show_alert=True)
        return

    if action == "dlq":
        await show_quality_menu(client, query, link_id, link)
    elif action == "qpage":
        try:
            requested_page = int(parts[2])
        except (IndexError, ValueError):
            requested_page = 0
        await show_quality_menu(client, query, link_id, link, page=requested_page)
    elif action == "q":
        variants = QUALITY_CACHE.get(link_id)
        try:
            chosen = variants[int(parts[2])]
        except (TypeError, IndexError, ValueError):
            await query.answer(SC("Expired, please resend the link."), show_alert=True)
            return
        # quality_url / _format_id select the exact quality
        await download_video(
            client, query, link,
            quality_url=chosen.get("url"),
            quality_label=chosen["label"],
            _format_id=chosen.get("_format_id"),
        )
    elif action == "qall":
        variants = QUALITY_CACHE.get(link_id)
        if not variants:
            await query.answer(SC("Expired, please resend the link."), show_alert=True)
            return
        downloadable = [v for v in variants if v["label"] != "Auto (Best)" and v.get("_type") != "info_card"]
        if not downloadable:
            await query.answer(SC("Nothing downloadable here."), show_alert=True)
            return
        await query.answer(SC(f"Starting {len(downloadable)} downloads…"))
        for i, v in enumerate(downloadable, start=1):
            # Sequential, not concurrent — download_video() already only
            # lets one download run at a time per chat via ACTIVE_TASKS/
            # download_semaphore, and hammering a terabox folder's files
            # back-to-back with no gap at all risks the same rate-
            # limiting/slow-link behavior fixed elsewhere in this file for
            # single downloads. Each file still gets its own upload
            # message once done; only the status text (edited in place by
            # download_video) shows the running "file i of N" position.
            try:
                await query.message.edit_text(
                    SC(f"<b>📦 File {i}/{len(downloadable)}</b>\n<code>{v['label']}</code>"),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            await download_video(
                client, query, link,
                quality_url=v.get("url"), quality_label=v["label"], _format_id=v.get("_format_id"),
            )
            if i < len(downloadable):
                await asyncio.sleep(2)
    elif action == "stream":
        await send_stream_link(client, query, link)


# ---------------------------------------------------------------------
# The "/" command menu (BotFather-style) shown in Telegram's chat UI.
# Kept as one reusable constant so the main bot AND every Titanium clone
# set the exact same menu — see set_bot_commands_list() below and
# titanium.py's _get_clone_client(), which applies this same list to
# each clone right after it starts.
# ---------------------------------------------------------------------
BOT_COMMANDS_LIST = [
    BotCommand("start",           "🚀 Start the bot"),
    BotCommand("help",            "❓ How to use the bot"),
    BotCommand("about",           "ℹ️ About this bot"),
    BotCommand("cancel",          "🚫 Cancel current active download"),
    BotCommand("plans",           "💎 View premium plans"),
    BotCommand("premium",         "💎 View premium plans"),
    BotCommand("myplan",          "📋 Check your plan & usage"),
    BotCommand("referral",        "🎁 Get your referral link & rewards"),
    BotCommand("autoupload",      "🚀 [Admin] Start auto-scraping (add a name for one actor's page)"),
    BotCommand("autouploadeporner", "🚀 [Admin] Auto-scrape eporner.com (add a keyword, or leave blank for random)"),
    BotCommand("autouploadfpo", "🚀 [Admin] Auto-scrape fpo.xxx (add a performer name, or leave blank for random)"),
    BotCommand("autouploadmat6tube", "🚀 [Admin] Auto-scrape mat6tube.com (add a performer name, or leave blank for random)"),
    BotCommand("autouploadspankbang", "🚀 [Admin] Auto-scrape spankbang.com (performer/keyword, trending/new/popular, or blank for random)"),
    BotCommand("autouploadpornhub", "🚀 [Admin] Auto-scrape pornhub.com (add a pornstar name, or leave blank for random)"),
    BotCommand("autouploadpornhubstudio", "🚀 [Admin] Auto-scrape one pornhub.com studio/channel (e.g. Brazzers)"),
    BotCommand("autouploadxhamster", "🚀 [Admin] Auto-scrape xhamster.com (add a performer name, or leave blank for random)"),
    BotCommand("autouploadxhamsterstudio", "🚀 [Admin] Auto-scrape one xhamster.com studio/channel"),
    BotCommand("autouploadxvideos", "🚀 [Admin] Auto-scrape xvideos.com (add a performer name, or leave blank for random)"),
    BotCommand("autouploadxvideosstudio", "🚀 [Admin] Auto-scrape one xvideos.com studio/channel"),
    BotCommand("autouploadepornerstudio", "🚀 [Admin] Auto-scrape one eporner.com studio (e.g. Brazzers)"),
    BotCommand("autouploadtag",   "🚀 [Admin] Auto-scrape one category/tag (e.g. MILF, Anal)"),
    BotCommand("autouploadstudio", "🚀 [Admin] Auto-scrape one studio (e.g. PureTaboo)"),
    BotCommand("stopupload",      "🛑 [Admin] Stop auto-upload in this chat"),
    BotCommand("pending",         "📊 [Admin] Scan for un-uploaded videos (add a name for one actor)"),
    BotCommand("debughtml",       "🩺 [Admin] Fetch a link's raw page HTML for debugging"),
    BotCommand("getdebughtml",    "🩺 [Admin] Get the last auto-captured extraction-failure HTML"),
    BotCommand("setcookies",      "🍪 [Admin] Set fpo.xxx session cookies for private videos"),
    BotCommand("cookiestatus",    "🍪 [Admin] Check how many fpo.xxx cookies are active"),
    BotCommand("delcookies",      "🗑 [Admin] Delete all saved fpo.xxx cookies"),
    BotCommand("setfpologin",     "🔐 [Admin] Set the fpo.xxx username + password to log in with"),
    BotCommand("fpologin",        "🔐 [Admin] Log in to fpo.xxx now and show the result"),
    BotCommand("fpotoken",        "🔑 [Admin] Log in to fpo.xxx with a captcha token you solved yourself"),
    BotCommand("retryskipped",    "🔄 [Admin] Retry skipped videos — optional: /retryskipped <actor>"),
    BotCommand("retryfailed",     "🔄 [Admin] Retry failed videos — optional: /retryfailed <actor>"),
    BotCommand("settings",        "⚙️ Open your settings menu"),
    BotCommand("addbot",          "⚡ Connect a Titanium clone bot"),
    BotCommand("delbot",          "⚡ Disconnect a Titanium clone bot"),
    BotCommand("titanium",        "⚡ Titanium Clone Mode panel"),
    BotCommand("set_caption",     "✏️ Set a custom caption"),
    BotCommand("see_caption",     "📄 View your custom caption"),
    BotCommand("del_caption",     "❌ Delete your custom caption"),
    BotCommand("set_thumb",       "🖼️ Set a custom thumbnail (reply to photo)"),
    BotCommand("view_thumb",      "👁️ View your custom thumbnail"),
    BotCommand("see_thumb",       "👁️ View your custom thumbnail"),
    BotCommand("del_thumb",       "🗑️ Delete your custom thumbnail"),
    BotCommand("delete_thumb",    "🗑️ Delete your custom thumbnail"),
    BotCommand("thumb_mode",      "🖼️ Check thumbnail status"),
    BotCommand("setchat",         "💬 Set/clear your personal dump chat"),
    BotCommand("set_dump",        "💬 Set global dump chat (admin only)"),
    BotCommand("set_channel_id",  "📡 Link a backup channel/group (admin)"),
    BotCommand("channel_id",      "📋 List linked backup channels (admin)"),
    BotCommand("del_channel_id",  "🗑 Unlink a backup channel (admin)"),
    BotCommand("addpremium",      "👑 Grant premium to a user (admin)"),
    BotCommand("removepremium",   "💔 Remove premium from a user (admin)"),
    BotCommand("ban",             "🔨 Ban a user (admin)"),
    BotCommand("unban",           "✅ Unban a user (admin)"),
    BotCommand("stats",           "📊 Bot-wide stats (admin)"),
    BotCommand("broadcast",       "📢 Broadcast a message to all users (admin)"),
    BotCommand("users",           "👥 Export all users as JSON (admin)"),
][:100]


def _is_admin_command(c: BotCommand) -> bool:
    d = (c.description or "").lower()
    return "[admin]" in d or "(admin" in d  # "[Admin] ...", "(admin)", "(admin only)"


# What EVERYONE sees in the "/" menu: the same list minus every admin command. Admins get the full BOT_COMMANDS_LIST
# through a per-chat scope (see set_bot_commands_list). Hiding a command from the menu only hides it -- each admin
# handler still checks ADMINS itself.
USER_COMMANDS_LIST = [c for c in BOT_COMMANDS_LIST if not _is_admin_command(c)]


# ---------------------------------------------------------------------
# Titanium Clone Mode — wire the main bot's own download handlers onto
# any clone bot a user connects. See titanium.py for the full picture.
#
# The clone reuses these exact handler functions (not copies) so its
# /start screen, menu buttons and inline buttons behave identically to
# the main bot's — same photo, same caption, same Plans/Status/Help/
# Support flow.
# ---------------------------------------------------------------------

TITANIUM_MENU_MESSAGE_HANDLERS = [
    (help_handler, filters.command("help") | menu_text_filter("❓ ʜᴇʟᴘ")),
    (plans_menu_handler, filters.text & menu_text_filter("💎 ᴘʟᴀɴs")),
    (plans_cmd, filters.command(["plans", "premium"])),
    (status_menu_handler, filters.text & menu_text_filter("📊 ᴍʏ sᴛᴀᴛᴜs")),
    (myplan_cmd, filters.command("myplan")),
    (support_handler, filters.text & menu_text_filter("☎️ sᴜᴘᴘᴏʀᴛ")),
]

TITANIUM_MENU_CALLBACK_HANDLERS = [
    (fallback_download_cb, filters.regex(r"^fallback_download$")),
    (fallback_status_cb, filters.regex(r"^fallback_status$")),
    (show_plans_cb, filters.regex(r"^show_plans$")),
    (plan_selected_cb, filters.regex(r"^plan_\d+$")),
    (paid_cb, filters.regex(r"^paid_\d+$")),
    (plans_back_cb, filters.regex(r"^plans_back$")),
]

titanium.register_titanium_handlers(
    app,
    make_button=make_button,
    BTN_PRIMARY=BTN_PRIMARY,
    BTN_DANGER=BTN_DANGER,
    SC=SC,
    ParseMode=ParseMode,
    link_handler=link_handler,
    callback_handler=callback_handler,
    link_filter=LINK_HANDLER_FILTER,
    callback_filter=CALLBACK_HANDLER_FILTER,
    cancel_handler=cancel_cmd,
    start_handler=start_handler,
    get_username=lambda: getattr(app, "_cached_username", None),
    smallcaps=smallcaps,
    start_photo_url=START_PHOTO_URL,
    fallback_keyboard=fallback_keyboard,
    fallback_text=FALLBACK_TEXT,
    main_menu_kb=MAIN_MENU_KB,
    powered_by=POWERED_BY,
    powered_by_url=POWERED_BY_URL,
    menu_message_handlers=TITANIUM_MENU_MESSAGE_HANDLERS,
    menu_callback_handlers=TITANIUM_MENU_CALLBACK_HANDLERS,
    bot_commands=USER_COMMANDS_LIST,
)


# ---------------------------------------------------------------------
# Thumbnails / transcoding helpers
# ---------------------------------------------------------------------

def generate_thumbnail(video_path: str, thumb_path: str, seek_seconds: float = 3.0) -> bool:
    """Extract a frame from the video as a fallback thumbnail using ffmpeg.
    Tries seek_seconds first, then falls back to 1s if that fails (e.g.
    the clip is shorter than seek_seconds)."""
    import subprocess
    for ts in (seek_seconds, 1.0):
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-y", "-ss", str(ts), "-i", video_path,
                    "-vframes", "1",
                    "-vf", "scale=320:-1",
                    thumb_path,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
            if result.returncode == 0 and os.path.exists(thumb_path):
                return True
        except Exception as e:
            logger.warning(f"ffmpeg thumbnail generation failed at {ts}s: {e}")
    return False


def download_thumb(thumb_url: str, thumb_path: str) -> bool:
    """Download a thumbnail image from a URL."""
    try:
        r = requests.get(thumb_url, timeout=30)
        r.raise_for_status()
        with open(thumb_path, "wb") as f:
            f.write(r.content)
        return True
    except Exception as e:
        logger.warning(f"Thumbnail download failed: {e}")
        return False


def finalize_thumbnail(thumb_path: str) -> bool:
    """Normalize whatever image ended up at thumb_path (custom photo, site
    poster, or ffmpeg frame grab) to what Telegram's `thumb` param actually
    accepts: JPEG, <=320x320, <200KB.

    Telegram enforces this limit server-side — anything bigger gets
    silently rejected or mangled, which is what caused inconsistent
    thumbnails (crisp sometimes, blurry/missing other times) depending on
    how big the source poster happened to be. This makes every thumbnail
    hit the best quality that limit allows, every time. Downscale-only;
    never upscales a small source image. Returns False if the file can't
    be read as an image at all, so callers can fall back to the next
    source in the chain.
    """
    import subprocess
    tmp_path = thumb_path + ".fix.jpg"
    try:
        for q in (2, 4, 8, 12, 16, 20, 24, 28, 31):  # ffmpeg mjpeg scale: lower = better quality
            result = subprocess.run(
                [
                    "ffmpeg", "-y", "-i", thumb_path,
                    "-vf", "scale='min(320,iw)':'min(320,ih)':force_original_aspect_ratio=decrease",
                    "-vframes", "1",
                    "-q:v", str(q),
                    tmp_path,
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
            )
            if result.returncode != 0 or not os.path.exists(tmp_path):
                return False
            if os.path.getsize(tmp_path) <= 200 * 1024:
                os.replace(tmp_path, thumb_path)
                return True
        # Every quality step still over 200KB (very rare) — use the
        # smallest one we produced rather than give up entirely.
        os.replace(tmp_path, thumb_path)
        return os.path.getsize(thumb_path) > 0
    except Exception as e:
        logger.warning(f"Thumbnail finalize failed: {e}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return False


def get_video_metadata(video_path: str):
    """Return (duration_seconds, width, height) using ffprobe, or (0, None, None) on failure."""
    try:
        import subprocess, json
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height:format=duration",
                "-of", "json",
                video_path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(result.stdout)
        stream = data["streams"][0]
        width = int(stream.get("width") or 0) or None
        height = int(stream.get("height") or 0) or None
        duration = int(float(data["format"]["duration"]))
        return duration, width, height
    except Exception as e:
        logger.warning(f"ffprobe metadata failed: {e}")
        return 0, None, None


def transcode_video(src_path: str, dst_path: str, target_height: int) -> bool:
    """Downscale a video to target_height using ffmpeg (blocking, kept for reference)."""
    try:
        import subprocess
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", src_path,
                "-vf", f"scale=-2:{target_height}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                dst_path,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3600,
        )
        return result.returncode == 0 and os.path.exists(dst_path)
    except Exception as e:
        logger.warning(f"ffmpeg transcode failed: {e}")
        return False


async def transcode_video_async(
    src_path: str, dst_path: str, target_height: int,
    total_duration: int, status_msg: Message, name: str,
) -> bool:
    """Downscale a video to target_height using ffmpeg WITHOUT blocking the event loop.

    Runs ffmpeg as a real async subprocess and parses its `-progress` output
    to update the Telegram status message with a live progress bar, so the
    bot stays responsive to other commands/users while a conversion runs.
    """
    cmd = [
        "ffmpeg", "-y", "-i", src_path,
        "-vf", f"scale=-2:{target_height}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats",
        dst_path,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception as e:
        logger.warning(f"ffmpeg spawn failed: {e}")
        return False

    start_time = time.time()
    last_edit_time = 0.0
    out_time_secs = 0

    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            line = line.decode(errors="ignore").strip()

            if line.startswith("out_time_ms="):
                try:
                    out_time_secs = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    pass
            elif line.startswith("out_time="):
                # HH:MM:SS.microseconds fallback if out_time_ms is unavailable
                try:
                    h, m, s = line.split("=", 1)[1].split(":")
                    out_time_secs = int(h) * 3600 + int(m) * 60 + float(s)
                except ValueError:
                    pass

            now = time.time()
            if now - last_edit_time >= 6.0 and auto_scraper.chat_edit_allowed(getattr(getattr(status_msg, "chat", None), "id", None)):
                last_edit_time = now
                pct = min(99.0, (out_time_secs / total_duration * 100)) if total_duration else 0
                elapsed = now - start_time
                speed_x = (out_time_secs / elapsed) if elapsed > 0 else 0
                try:
                    await status_msg.edit_text(
                        SC(f"🎞️ <b>Converting to {target_height}p...</b>\n"
                        f"<code>{name}</code>\n\n"
                        "╭━━━━❰Progress❱━➣\n"
                        f"┣⪼ [{progress_bar(pct)}]\n"
                        f"┣⪼ ✅ Done: {pct:.1f}%\n"
                        f"┣⪼ ⏱️ Processed: {human_time(out_time_secs)} / {human_time(total_duration)}\n"
                        f"┣⪼ ⚡ Speed: {speed_x:.2f}x\n"
                        "╰━━━━━━━━━━━━━━━➣"),
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass

        returncode = await proc.wait()
        return returncode == 0 and os.path.exists(dst_path)
    except Exception as e:
        logger.warning(f"ffmpeg transcode (async) failed: {e}")
        try:
            proc.kill()
        except Exception:
            pass
        return False


# ---------------------------------------------------------------------
# Download / cache / upload
# ---------------------------------------------------------------------

async def _send_cached_video_message(client: Client, chat_id: int, cached: dict, caption: str) -> "Message | None":
    """Send the cached video. Prefers copy_message() from its CACHE_CHANNEL_ID
    copy (gives Telegram a fresh file_reference for the recipient — this is
    what fixes cache hits failing/redownloading when a *different* user
    requests a link someone else already downloaded). Falls back to the raw
    file_id if there's no cache-channel copy on record or the copy fails.
    Returns None if both attempts fail."""
    sent_msg = None
    if CACHE_CHANNEL_ID and cached.get("cache_chat_id") and cached.get("cache_message_id"):
        try:
            sent_msg = await client.copy_message(
                chat_id=chat_id,
                from_chat_id=cached["cache_chat_id"],
                message_id=cached["cache_message_id"],
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.warning(f"Cache-channel copy failed, falling back to file_id: {e}")
            sent_msg = None

    if sent_msg is None and cached.get("file_id"):
        try:
            sent_msg = await client.send_video(
                chat_id, cached["file_id"],
                caption=caption,
                parse_mode=ParseMode.HTML,
                supports_streaming=True,
                duration=cached.get("duration", 0),
            )
        except Exception as e:
            logger.warning(f"Cached file_id send failed: {e}")
            sent_msg = None

    return sent_msg


async def send_cached_video(client: Client, query, status_msg, link: str, cached: dict) -> bool:
    """Try to resend a previously uploaded video instantly from the cache.
    Returns True on success, False if the cache entry is stale and needs a fresh download."""
    await status_msg.edit_text(SC("<b>⚡ Found in cache, sending instantly...</b>"), parse_mode=ParseMode.HTML)
    chat_id = query.from_user.id
    caption = await build_caption(
        name=cached["name"],
        size_bytes=cached["size"],
        dl_seconds=0,
        ul_seconds=0,
        user_id=chat_id,
        source_link=link,
        quality_label=cached["quality_label"],
        duration_seconds=cached.get("duration", 0),
        downloaded_by_username=query.from_user.username,
            downloaded_by_name=(query.from_user.first_name or "") + (f" {query.from_user.last_name}" if query.from_user.last_name else ""),
    )
    sent_msg = await _send_cached_video_message(client, chat_id, cached, caption)
    if sent_msg is None:
        logger.warning("Cache hit failed to send — treating cache entry as stale.")
        return False

    try:
        await status_msg.delete()
        premium = await get_effective_premium_status(chat_id)
        if not premium["is_premium"]:
            await bump_daily_count(chat_id)
        await bump_total_downloads(chat_id)
        asyncio.create_task(schedule_delete(client, chat_id, sent_msg.id))
        asyncio.create_task(backup_to_linked_channels(client, chat_id, sent_msg.id))
        asyncio.create_task(forward_to_dump_chat(client, chat_id, sent_msg.id))
        return True
    except Exception as e:
        # The video itself already sent successfully at this point — only
        # the bookkeeping below failed. Don't report False here, or the
        # caller will delete a perfectly good cache entry and force a
        # pointless redownload for a file the user already received.
        logger.warning(f"Cache-hit post-send bookkeeping failed (delivery itself succeeded): {e}")
        return True


async def download_video(client: Client, query, link: str, quality_url: str = None, quality_label: str = "Auto (Best)", _format_id: str = None):
    async with download_semaphore:
        status_msg = query.message
        chat_id = query.from_user.id
        ACTIVE_TASKS[chat_id] = asyncio.current_task()
        try:
            await _download_faphouse_video_inner(client, query, link, status_msg, chat_id, quality_url, quality_label, _format_id=_format_id)
        except asyncio.CancelledError:
            try:
                await status_msg.edit_text(SC("<b>❌ Cancelled.</b>"), parse_mode=ParseMode.HTML)
            except Exception:
                pass
        except Exception as e:
            # Without this, any failure inside the download/upload pipeline
            # (bad proxy URL, expired link, corrupt file, etc.) crashed the
            # callback silently — the status message stayed stuck on
            # "Downloading..." forever with no indication anything went
            # wrong. Same failure class already fixed for
            # get_available_qualities above; this closes the same gap here.
            logger.error(f"download_video failed for {link}: {e}", exc_info=True)

            # Last-resort folder fallback: process_link's own
            # probe_terabox_share() check runs BEFORE this, but it relies
            # on the same Baidu-PCS shorturlinfo/share/list calls that can
            # themselves fail transiently (or for a share this Baidu PCS
            # guest session just can't read yet) — letting a real folder
            # share slip through as "not a folder" and hit the normal
            # single-file quality menu, where every resolver tier
            # (flowvideoplayer, terabox.beer, ...) hands back some link
            # that isn't the actual video (a tiny error/redirect page),
            # failing every candidate with "file too small". Before
            # showing that as a final error, retry the Baidu-PCS folder
            # path once — if it succeeds this time and finds 2+ files,
            # it really was a folder and we can still deliver it instead
            # of just reporting failure.
            if terabox.is_terabox_link(link):
                try:
                    folder_files = await asyncio.to_thread(terabox.get_all_folder_files, link)
                except Exception as folder_err:
                    logger.warning(f"terabox folder fallback also failed for {link}: {folder_err}")
                    folder_files = None
                if folder_files and len(folder_files) > 1:
                    try:
                        await status_msg.delete()
                    except Exception:
                        pass
                    await _send_terabox_folder(client, chat_id, query.from_user.id, link, folder_files)
                    return

            try:
                await status_msg.edit_text(
                    SC(f"<b>❌ Download failed.</b>\n<code>{_strip_ansi(str(e))[:500]}</code>\n\n"
                       "The link may be expired or invalid. Try resending it."),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
        finally:
            ACTIVE_TASKS.pop(chat_id, None)


async def _download_faphouse_video_inner(client: Client, query, link: str, status_msg, chat_id: int,
                                          quality_url: str = None, quality_label: str = "Auto (Best)", _format_id: str = None):
    """Faphouse.com/faphouse2.com flow: resolve the page link to its m3u8
    stream (faphouse_downloader.AkClient) and pull it down via ffmpeg, then
    run it through the thumbnail/duration/caption/upload pipeline below.
    """
    # A distinct cache slot per quality — otherwise a 1080p pick could get
    # served a previously-cached 360p file (or vice versa) for the same link.
    cache_key = re.sub(r"\W+", "_", quality_label.lower()).strip("_") or "auto"

    premium = await get_effective_premium_status(chat_id)
    if not premium["is_premium"]:
        used_today = await get_daily_count(chat_id)
        if used_today >= DAILY_FREE_LIMIT:
            try:
                await status_msg.delete()
            except Exception:
                pass
            await send_referral_prompt(client, chat_id)
            return

    cached = await get_cached_file(link, cache_key)
    if cached:
        ok = await send_cached_video(client, query, status_msg, link, cached)
        if ok:
            return
        await delete_cached_file(link, cache_key)

    await status_msg.edit_text(SC("<b>Starting download...</b>"), parse_mode=ParseMode.HTML)
    # Record this as an in-flight download *before* the real work starts —
    # if the bot process dies partway through, this doc is what lets
    # _resume_active_downloads() kick it off again on the next startup.
    await add_active_download(chat_id, status_msg.id, link, quality_url, quality_label)
    out_path = None
    try:
        # Fetch page metadata (title, duration, thumbnail, etc.) up front,
        # BEFORE computing `name`/`out_path` below — previously this ran
        # further down, by which point `name` was already locked in from
        # urlparse_path_name(link) alone. That's fine for sites whose URL
        # itself carries a readable slug (faphouse2.com/videos/some-cool-
        # title-123), but Diskwala/Flezen/VidBunker share links are just
        # an opaque hash id in the path — so the "📄 File Name" line in
        # the final caption showed something like
        # "6aa480fa06ba7ea03d2dee47.mp4" instead of the actual video
        # title the caption's own 🎬 line (from this same page_meta,
        # already fetched) displays right above it. Fetching page_meta
        # first lets the name below prefer that real title when there is
        # one, only falling back to the URL slug when there isn't.
        page_meta = {}
        backend_for_meta = _downloader_for(link)
        if hasattr(backend_for_meta, "get_page_meta"):
            try:
                page_meta = await asyncio.to_thread(backend_for_meta.get_page_meta, link) or {}
            except Exception:
                pass

        slug = urlparse_path_name(link)
        # MP3 selections (quality_url="mp3:<bitrate>" — see show_quality_menu's
        # comment) produce an audio file, not a video — .mp4 here would be
        # flat wrong (Telegram/players would treat a real mp3 as a broken
        # video file) regardless of what page_meta's own extension guess is.
        if quality_url and quality_url.startswith("mp3:"):
            ext = "mp3"
        else:
            ext = (page_meta.get("extension") or ".mp4").lstrip(".") or "mp4"
        title_for_name = page_meta.get("title")
        name = None
        if title_for_name:
            # Strip characters unsafe in a filename on common filesystems,
            # collapse whitespace, and cap the length so an unusually
            # long title can't blow past filesystem path limits.
            safe_title = re.sub(r'[\\/:*?"<>|\r\n]+', " ", title_for_name).strip()
            safe_title = re.sub(r"\s+", " ", safe_title)[:150].strip()
            if safe_title:
                name = f"{safe_title}.{ext}"
        if not name:
            name = f"{slug}.{ext}" if slug else f"faphouse_{uuid.uuid4().hex[:8]}.{ext}"
        out_path = os.path.join(DOWNLOAD_DIR, name)

        await status_msg.edit_text(SC(f"<b>📥 Starting download...</b>\n<code>{name}</code>"), parse_mode=ParseMode.HTML)

        # faphouse_downloader.download_video() is fully synchronous (requests +
        # a blocking ffmpeg subprocess) — run it in a thread so it can't
        # freeze the shared event loop, same reasoning as the Faphouse
        # blocking download above.
        dl_start = time.time()
        _progress = {"pct": None, "downloaded": 0, "total": 0, "total_bytes": None, "done": False, "error": None,
                     "duration": 0.0, "corrected_path": None, "connecting": True}

        def _on_progress(info):
            _progress["pct"] = info.get("pct")
            _progress["downloaded"] = info.get("downloaded_bytes", 0)
            # FIX: this used to discard the hook's real total_bytes
            # entirely and always fall back to reconstructing an estimated
            # total from pct — fine for faphouse's ffmpeg-based progress
            # (which genuinely has no byte total, only % of duration), but
            # for ytdlp downloads yt-dlp/aria2c DOES hand back a real
            # total_bytes — throwing it away and re-deriving a fuzzy
            # estimate that only kicks in once pct > 2% is why a real
            # download could sit at a frozen "0.0% / 0.00 B / 0.00 B" even
            # while bytes were genuinely arriving. None here (faphouse's
            # ffmpeg path) keeps the existing estimate-from-pct behavior
            # in the polling loop below unchanged.
            _progress["total_bytes"] = info.get("total_bytes")
            _progress["duration"] = info.get("duration_s", 0.0)
            _progress["connecting"] = info.get("connecting", False)

        def _blocking_download():
            try:
                backend = _downloader_for(link)
                # Pass _format_id so exact quality is picked
                dl_kwargs = {"on_progress": _on_progress, "stream_url": quality_url}
                if _format_id and hasattr(backend, "download_video"):
                    import inspect as _inspect
                    if "_format_id" in _inspect.signature(backend.download_video).parameters:
                        dl_kwargs["_format_id"] = _format_id
                result = backend.download_video(link, out_path, **dl_kwargs)
                if backend is terabox and isinstance(result, str) and result and result != out_path:
                    _progress["corrected_path"] = result
            except Exception as exc:
                _progress["error"] = exc
            finally:
                _progress["done"] = True

        duration_str = _format_duration(page_meta.get("duration")) if page_meta.get("duration") else None

        dl_tracker = ProgressTracker(status_msg, "Downloading", name, quality=quality_label, duration=duration_str)
        dl_task = asyncio.create_task(asyncio.to_thread(_blocking_download))
        smoothed_total = 0
        while not _progress["done"]:
            # ffmpeg reports progress as % of stream duration, not bytes —
            # back an estimated "total bytes" out of downloaded/pct so this
            # can reuse the same downloaded/total ProgressTracker UI. The
            # video's bitrate isn't constant scene to scene, so this raw
            # ratio swings noticeably between updates (e.g. 2.39GB one
            # second, 2.44GB the next) — smoothing it with an EMA instead
            # of showing the instantaneous value keeps the displayed total
            # from visibly jumping around while still converging on an
            # accurate number as more of the video downloads.
            pct = _progress["pct"]
            real_total = _progress.get("total_bytes")
            if real_total:
                # A real total_bytes came straight from the downloader
                # (yt-dlp/aria2c) — use it directly instead of the
                # estimate below, which exists only for backends (like
                # faphouse's ffmpeg progress) that never report one.
                smoothed_total = real_total
            elif pct and pct > 2:  # skip the first couple % — early estimates are the noisiest
                raw_total = int(_progress["downloaded"] / (pct / 100))
                smoothed_total = raw_total if not smoothed_total else int(smoothed_total * 0.8 + raw_total * 0.2)
            await dl_tracker.update(_progress["downloaded"], smoothed_total)
            await asyncio.sleep(1)
        await dl_task

        if _progress["error"] is not None:
            raise _progress["error"]

        if _progress["corrected_path"]:
            out_path = _progress["corrected_path"]
            name = os.path.basename(out_path)

        dl_seconds = time.time() - dl_start

        if not os.path.exists(out_path):
            await status_msg.edit_text(SC("<b>❌ Download failed — no file produced.</b>"), parse_mode=ParseMode.HTML)
            return

        duration, vid_width, vid_height = await asyncio.to_thread(get_video_metadata, out_path)
        upload_size = os.path.getsize(out_path)

        thumb_path = out_path + "_thumb.jpg"
        got_thumb = False
        custom_thumb_id = await get_thumbnail(chat_id)
        if custom_thumb_id:
            try:
                await client.download_media(custom_thumb_id, file_name=thumb_path)
                got_thumb = os.path.exists(thumb_path)
            except Exception as e:
                logger.warning(f"custom thumb download failed: {e}")
        if not got_thumb:
            # The page's own og:image is the exact poster faphouse.com/
            # faphouse2.com shows for this video — try that before falling
            # back to an ffmpeg frame grab (which can't guarantee matching
            # the site's own thumbnail).
            backend = _downloader_for(link)
            page_meta = await asyncio.to_thread(backend.get_page_meta, link) if hasattr(backend, "get_page_meta") else {}
            poster_url = page_meta.get("poster_url")
            if poster_url:
                got_thumb = await asyncio.to_thread(download_thumb, poster_url, thumb_path)
        if not got_thumb:
            # A fixed 1-second mark almost always lands on faphouse's own
            # black intro/logo bumper — a real frame further in (but still
            # early, in case of short clips) actually shows the video.
            thumb_at = min(max(duration * 0.15, 3), 20) if duration else 3
            got_thumb = await asyncio.to_thread(generate_thumbnail, out_path, thumb_path, thumb_at)

        if got_thumb:
            # Whatever source it came from, force it to Telegram's actual
            # thumb limits (<=320x320, <200KB JPEG) so it never gets
            # silently rejected in favor of a blurry/no thumbnail.
            got_thumb = await asyncio.to_thread(finalize_thumbnail, thumb_path)

        await status_msg.edit_text(SC("<b>⏳ Uploading to Telegram...</b>"), parse_mode=ParseMode.HTML)
        ul_tracker = ProgressTracker(status_msg, "Uploading", name, quality=quality_label, duration=duration_str)
        ul_tracker.start_upload_wait_animation()   # animated dots jab tak pehla progress callback na aaye
        ul_start = time.time()

        parts = [out_path]
        if upload_size > MAX_FILE_SIZE:
            await status_msg.edit_text(
                SC(f"<b>✂️ Splitting into parts (over 2GB)...</b>\n<code>{name}</code>"),
                parse_mode=ParseMode.HTML,
            )
            # Removes out_path itself once split successfully — the
            # thumbnail above was already generated from it, so nothing
            # downstream needs it to still exist.
            parts = await asyncio.to_thread(
                split_upload.split_video_file, out_path, DOWNLOAD_DIR, uuid.uuid4().hex[:10], SPLIT_PART_TARGET_BYTES,
            )
            if len(parts) <= 1:
                await status_msg.edit_text(
                    SC(f"<b>❌ File is over 2GB and couldn't be split automatically.</b>\n<code>{name}</code>"),
                    parse_mode=ParseMode.HTML,
                )
                return

        sent_msgs = []
        base_caption = await build_caption(
            name=name,
            size_bytes=upload_size,
            dl_seconds=dl_seconds,
            ul_seconds=0,  # filled in per-part below via edit_caption, same as the single-file path
            user_id=chat_id,
            source_link=link,
            quality_label=quality_label,
            duration_seconds=duration,
            views=page_meta.get("views"),
            upload_date=page_meta.get("upload_date"),
            likes=page_meta.get("likes"),
            comments=page_meta.get("comments"),
            author=page_meta.get("author"),
            author_url=page_meta.get("author_url"),
            category=page_meta.get("category"),
            downloaded_by_username=query.from_user.username,
            downloaded_by_name=(query.from_user.first_name or "") + (f" {query.from_user.last_name}" if query.from_user.last_name else ""),
            source_site_name=page_meta.get("site_name"),
            title=page_meta.get("title"),
        )
        total_parts = len(parts)
        for i, part_path in enumerate(parts, start=1):
            if total_parts > 1:
                await status_msg.edit_text(
                    SC(f"<b>Uploading Part {i}/{total_parts}...</b>\n<code>{name}</code>"),
                    parse_mode=ParseMode.HTML,
                )
                part_duration, part_width, part_height = await asyncio.to_thread(get_video_metadata, part_path)
                part_caption = f"{base_caption}\n\n✂️ <b>Part {i}/{total_parts}</b>"
            else:
                part_duration, part_width, part_height = duration, vid_width, vid_height
                part_caption = base_caption
            is_mp3 = bool(quality_url and quality_url.startswith("mp3:"))
            if is_mp3:
                part_msg = await client.send_audio(
                    chat_id, part_path,
                    caption=part_caption,
                    parse_mode=ParseMode.HTML,
                    duration=part_duration or 0,
                    title=title_for_name or name,
                    performer=page_meta.get("author"),
                    thumb=thumb_path if got_thumb else None,
                    progress=ul_tracker.update,
                )
            else:
                part_msg = await client.send_video(
                    chat_id, part_path,
                    caption=part_caption,
                    parse_mode=ParseMode.HTML,
                    supports_streaming=True,
                    thumb=thumb_path if got_thumb else None,
                    duration=part_duration or 0,
                    width=part_width or 0,
                    height=part_height or 0,
                    progress=ul_tracker.update,
                )
            sent_msgs.append(part_msg)
            if total_parts > 1:
                try:
                    os.remove(part_path)
                except OSError:
                    pass
        sent_msg = sent_msgs[0]
        ul_seconds = time.time() - ul_start

        # "📄 Full Description" button — only when this site's get_page_meta
        # actually returned one (currently ytdlp_downloaderrr.py's sites;
        # see DESC_CACHE's comment above), and only on a single-part
        # upload (a split video has no one message to attach it to that
        # represents "the whole video").
        if total_parts == 1 and page_meta.get("description"):
            desc_id = uuid.uuid4().hex[:10]
            if len(DESC_CACHE) >= 1000:
                DESC_CACHE.pop(next(iter(DESC_CACHE)))
            desc_entry = {
                "site_name": page_meta.get("site_name"),
                "title": page_meta.get("title"),
                "description": page_meta.get("description"),
            }
            DESC_CACHE[desc_id] = desc_entry
            try:
                await set_cached_description(desc_id, **desc_entry)
            except Exception as e:
                # DESC_CACHE (in-memory) still has it for this boot even
                # if the DB write failed — just means "Description no
                # longer available" becomes possible again after the next
                # restart instead of guaranteed. Not worth failing the
                # whole upload over.
                logger.warning(f"Couldn't persist description to DB: {e}")
            try:
                await sent_msg.edit_reply_markup(
                    InlineKeyboardMarkup([[make_button(SC("📄 Full Description"), callback_data=f"desc|{desc_id}", style=BTN_PRIMARY)]])
                )
            except Exception as e:
                logger.warning(f"Couldn't attach Full Description button: {e}")

        if len(parts) == 1 and sent_msg.video:
            # Cache-channel copy + set_cached_file only makes sense for a
            # single-message video — a split upload has no one file_id
            # that alone represents "this video", so multi-part results
            # simply aren't cached (each part still gets its own
            # schedule_delete/backup/forward below, same as any upload).
            cache_chat_id = None
            cache_message_id = None
            if CACHE_CHANNEL_ID:
                try:
                    cache_copy = await client.copy_message(
                        chat_id=CACHE_CHANNEL_ID,
                        from_chat_id=chat_id,
                        message_id=sent_msg.id,
                    )
                    cache_chat_id = CACHE_CHANNEL_ID
                    cache_message_id = cache_copy.id
                except Exception as e:
                    logger.warning(f"Cache-channel copy after fresh upload failed: {e}")
                    # Don't wait for the next scheduled health check — a
                    # copy failure right here is the strongest possible
                    # signal something's wrong with the cache channel, so
                    # verify (and alert if so) now.
                    asyncio.create_task(check_cache_channel_health(client))

            await set_cached_file(
                link, cache_key,
                file_id=sent_msg.video.file_id,
                name=name,
                size=upload_size,
                quality_label=quality_label,
                duration=duration,
                cache_chat_id=cache_chat_id,
                cache_message_id=cache_message_id,
            )

        final_caption = await build_caption(
            name=name,
            size_bytes=upload_size,
            dl_seconds=dl_seconds,
            ul_seconds=ul_seconds,
            user_id=chat_id,
            source_link=link,
            quality_label=quality_label,
            duration_seconds=duration,
            views=page_meta.get("views"),
            upload_date=page_meta.get("upload_date"),
            likes=page_meta.get("likes"),
            comments=page_meta.get("comments"),
            author=page_meta.get("author"),
            author_url=page_meta.get("author_url"),
            category=page_meta.get("category"),
            downloaded_by_username=query.from_user.username,
            downloaded_by_name=(query.from_user.first_name or "") + (f" {query.from_user.last_name}" if query.from_user.last_name else ""),
            source_site_name=page_meta.get("site_name"),
            title=page_meta.get("title"),
        )
        for i, part_msg in enumerate(sent_msgs, start=1):
            part_final_caption = f"{final_caption}\n\n✂️ <b>Part {i}/{total_parts}</b>" if total_parts > 1 else final_caption
            try:
                await part_msg.edit_caption(caption=SC(part_final_caption), parse_mode=ParseMode.HTML)
            except Exception:
                pass

        await status_msg.delete()
        try:
            os.remove(out_path)
        except Exception:
            pass
        if got_thumb:
            try:
                os.remove(thumb_path)
            except Exception:
                pass

        if not premium["is_premium"]:
            await bump_daily_count(chat_id)
        await bump_total_downloads(chat_id)
        for part_msg in sent_msgs:
            asyncio.create_task(schedule_delete(client, chat_id, part_msg.id))
            asyncio.create_task(backup_to_linked_channels(client, chat_id, part_msg.id))
            asyncio.create_task(forward_to_dump_chat(client, chat_id, part_msg.id))
    except RuntimeError as e:
        await status_msg.edit_text(SC(f"<b>❌ Failed:</b> {e}"), parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.exception("Unexpected error during faphouse download/upload")
        await status_msg.edit_text(SC(f"<b>❌ Unexpected error:</b> {_strip_ansi(str(e))[:500]}"), parse_mode=ParseMode.HTML)
    finally:
        # Reached on a normal finish (success or a handled failure above)
        # AND on /cancel (CancelledError skips the except clauses above but
        # still runs finally) — the attempt is over one way or another, so
        # stop tracking it. A hard process crash never reaches this line,
        # which is exactly what leaves the doc behind for
        # _resume_active_downloads() to pick up on the next startup.
        await remove_active_download(chat_id, link, quality_label)
        # Normal success path already removes out_path right after upload;
        # this only mops up leftovers from an error that happened partway.
        if out_path and os.path.exists(out_path):
            try:
                os.remove(out_path)
            except OSError:
                pass


def urlparse_path_name(url: str) -> str:
    """Best-effort filename slug from a faphouse video page URL, e.g.
    https://faphouse2.com/videos/some-cool-title-123 -> 'some-cool-title-123'."""
    try:
        from urllib.parse import urlparse
        path = urlparse(url).path.strip("/")
        slug = path.split("/")[-1] if path else ""
        slug = "".join(c for c in slug if c.isalnum() or c in " ._-")
        return slug[:100]
    except Exception:
        return ""


def _markup_urls(markup) -> list:
    out = []
    try:
        for row in markup.inline_keyboard:
            for b in row:
                if getattr(b, "url", None):
                    out.append(b.url)
    except Exception:
        pass
    return out


async def _url_fallback_text(message, text: str, urls: list) -> str:
    """Telegram refused the URL button (BUTTON_URL_INVALID: link too long - signed CDN/m3u8 URLs can pass 2 KB - or
    containing characters Telegram rejects). Show the link as copyable text instead; if even that would not fit in one
    message, send it as a .txt file."""
    body = "\n".join(f"<code>{html.escape(u)}</code>" for u in urls)
    full = f"{text}\n\n{body}"
    if len(full) <= 3900:
        return full
    try:
        import io
        bio = io.BytesIO("\n".join(urls).encode("utf-8"))
        bio.name = "stream_link.txt"
        await message.reply_document(bio, caption="Stream link (too long for a button)")
    except Exception as e:
        logger.warning(f"Couldn't send long stream link as file: {e}")
    return text


async def _edit_text_safe(message, text, **kw):
    """message.edit_text that survives BUTTON_URL_INVALID by showing the URL(s) as text instead of a button."""
    try:
        return await message.edit_text(text, **kw)
    except MessageNotModified:
        return message  # button tapped twice / same content already shown - nothing to do (was an unhandled callback error)
    except ButtonUrlInvalid:
        urls = _markup_urls(kw.get("reply_markup"))
        if not urls:
            raise
        kw.pop("reply_markup", None)
        logger.warning(f"BUTTON_URL_INVALID for a stream link ({len(urls[0])} chars) - sending it as text")
        try:
            return await message.edit_text(await _url_fallback_text(message, text, urls), **kw)
        except MessageNotModified:
            return message  # same text already shown - nothing left to do (was an unhandled callback error)


async def _send_message_safe(client, chat_id, text, **kw):
    try:
        return await client.send_message(chat_id, text, **kw)
    except ButtonUrlInvalid:
        urls = _markup_urls(kw.get("reply_markup"))
        if not urls:
            raise
        kw.pop("reply_markup", None)
        logger.warning(f"BUTTON_URL_INVALID for a stream link ({len(urls[0])} chars) - sending it as text")
        return await client.send_message(chat_id, f"{text}\n\n" + "\n".join(f"<code>{html.escape(u)}</code>" for u in urls)[:3800], **kw)



async def send_stream_link(client: Client, query, link: str):
    if terabox.is_terabox_link(link):
        try:
            stream_url = await asyncio.to_thread(terabox.get_stream_url, link)
        except Exception as e:
            logger.error(f"Terabox stream error: {e}")
            stream_url = None
        if not stream_url:
            await _edit_text_safe(query.message, SC("<b>No stream URL found for this Terabox link.</b>"), parse_mode=ParseMode.HTML)
            return
        await _edit_text_safe(query.message, 
            SC("<b>Terabox Stream Link Ready</b>"),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [make_button(SC("🔗 Open Stream"), url=stream_url, style=BTN_PRIMARY)],
            ]),
        )
        return

    # ── Diskwala / Flezen / VidBunker ────────────────────────────────────────
    if diskwala.is_diskwala_link(link):
        try:
            video_info = await asyncio.to_thread(diskwala.get_available_qualities, link)
            cdn_url = name = None
            size = 0
            if isinstance(video_info, list) and video_info:
                first = video_info[0]
                cdn_url = first.get("url") or first.get("stream_url") or first.get("download_url")
                name    = first.get("name", "video.mp4")
                size    = first.get("size", 0)
            elif isinstance(video_info, dict):
                cdn_url = video_info.get("streamUrl") or video_info.get("downloadUrl")
                name    = video_info.get("name", "video.mp4")
                size    = video_info.get("size", 0)
            if not cdn_url:
                cdn_url = await asyncio.to_thread(diskwala.get_stream_url, link)
            if not cdn_url:
                await _edit_text_safe(query.message, SC("<b>No stream URL found for this link.</b>"), parse_mode=ParseMode.HTML)
                return
            name = name or "video.mp4"
            if not any(name.lower().endswith(e) for e in (".mp4",".mkv",".webm",".mov",".avi",".m4v",".ts",".flv")):
                name += ".mp4"
            import hashlib as _hl
            code = _hl.sha1(cdn_url.encode()).hexdigest()[:12]
            register_stream_proxy(code, cdn_url, name, size)
            stream_url = get_stream_public_url(code)
            size_str = human_size(size) if size else "Unknown"
            await _edit_text_safe(query.message, 
                SC(f"<b>Stream Link Ready</b>\n\nName: <code>{name}</code>\nSize: <code>{size_str}</code>"),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [make_button(SC("▶️ Stream / Play"), url=stream_url, style=BTN_PRIMARY)],
                ]),
            )
        except Exception as e:
            logger.error(f"Diskwala stream error: {e}")
            await _edit_text_safe(query.message, 
                SC(f"<b>Stream link failed</b>\n<code>{_strip_ansi(str(e))[:500]}</code>"),
                parse_mode=ParseMode.HTML,
            )
        return

    # ── SpankBang: dedicated resolver (same one the auto-upload Stream button uses) ──────────────
    if spankbang.is_spankbang_link(link):
        try:
            stream_url = await asyncio.to_thread(spankbang.get_stream_url, link)
        except Exception as e:
            logger.error(f"SpankBang stream error: {e}")
            await _edit_text_safe(query.message, 
                SC(f"<b>Stream link failed</b>\n<code>{_strip_ansi(str(e))[:500]}</code>"),
                parse_mode=ParseMode.HTML,
            )
            return
        if not stream_url:
            await _edit_text_safe(query.message, 
                SC("<b>No stream URL found</b>\nUse 🔽 Download instead — the file gets sent here."),
                parse_mode=ParseMode.HTML,
            )
            return
        await _edit_text_safe(query.message, 
            SC("<b>Stream Link Ready</b>"),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [make_button(SC("🔗 Open Stream"), url=player_url(link) or stream_url, style=BTN_PRIMARY)],
            ]),
        )
        return

    if pf.is_supported_link(link):
        # porn_fetch_downloader's "quality" values are ints (heights),
        # not real URLs — these packages only resolve the actual CDN
        # link internally inside their own download() call, with no
        # public property exposing it separately, so there's nothing to
        # open/stream directly here the way the other backends allow.
        await _edit_text_safe(query.message, 
            SC("<b>Stream link isn't available for this site</b>\n"
               "Use ⬇️ Download instead — the file gets sent here."),
            parse_mode=ParseMode.HTML,
        )
        return

    if ytdlp.is_supported_link(link):
        try:
            stream_url = await asyncio.to_thread(ytdlp.get_stream_url, link)
        except Exception as e:
            logger.error(f"ytdlp stream error: {e}")
            stream_url = None
        if not stream_url:
            await _edit_text_safe(query.message, SC("<b>No stream URL found</b>"))
            return
        watch_url = player_url(link) or stream_url
        await _edit_text_safe(query.message, 
            SC("<b>Stream Link Ready</b>"),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [make_button(SC("▶️ Watch Now"), url=watch_url, style=BTN_PRIMARY)],
            ]),
        )
        return

    if fpo.is_fpo_link(link):
        try:
            fpo_direct, fpo_proxy = await _fpo_stream_urls(link)
            best_url = fpo_direct or fpo_proxy
            if not best_url:
                await _edit_text_safe(query.message,
                    SC("<b>No stream URL found</b>\nUse 🔽 Download instead — the file gets sent here."),
                    parse_mode=ParseMode.HTML,
                )
                return
        except Exception as e:
            logger.error(f"fpo.xxx stream error: {e}")
            await _edit_text_safe(query.message, 
                SC(f"<b>Stream link failed</b>\n<code>{_strip_ansi(str(e))[:500]}</code>"),
                parse_mode=ParseMode.HTML,
            )
            return
        await _edit_text_safe(query.message, 
            SC("<b>Stream Link Ready</b>"),
            parse_mode=ParseMode.HTML,
            reply_markup=_fpo_stream_markup(fpo_direct, fpo_proxy),
        )
        return

    try:
        m3u8_url = await asyncio.to_thread(faphouse.client.get_m3u8_url, link)
        if not m3u8_url:
            await _edit_text_safe(query.message, SC("<b>No stream URL found</b>"))
            return

        await _edit_text_safe(query.message, 
            SC("<b>Stream Link Ready</b>"),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [make_button(SC("🔗 Open Stream"), url=m3u8_url, style=BTN_PRIMARY)],
            ]),
        )
    except Exception as e:
        logger.error(f"Faphouse stream error: {e}")
        await _edit_text_safe(query.message, 
            SC(f"<b>Stream link failed</b>\n<code>{_strip_ansi(str(e))[:500]}</code>"),
            parse_mode=ParseMode.HTML,
        )


async def set_admin_commands_menu(client: Client, admin_id: int) -> None:
    """Full command menu (admin commands included) for ONE admin's chat with the bot. Best-effort: Telegram refuses
    a chat scope until that admin has opened the bot, so this is retried from /start and /help."""
    try:
        await client.set_bot_commands(BOT_COMMANDS_LIST, scope=BotCommandScopeChat(chat_id=admin_id))
    except Exception as e:
        logger.info(f"admin command menu for {admin_id} not set yet: {e}")


async def set_bot_commands_list():
    # default scope (everyone): user commands only -- no admin commands visible
    await app.set_bot_commands(USER_COMMANDS_LIST)
    # each admin: the full list
    for admin_id in ADMINS:
        await set_admin_commands_menu(app, admin_id)


async def _startup_log():
    try:
        me = await app.get_me()
        app._cached_username = me.username
        stats = await get_stats_summary()
        ist_time = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime("%I:%M %p IST")
        await log_event(
            app,
            "🚀 <b>Bot successfully started!</b>\n\n"
            f"⭐ Bot: @{me.username}\n"
            f"👥 Users: {stats['total_users']}\n"
            f"⏳ Time: {ist_time}\n\n"
            f"👑 Developed by {POWERED_BY_URL.replace('https://t.me/', '@')}",
        )
    except Exception as e:
        logger.warning(f"Startup log failed: {e}")


def _run_maybe_async(value, loop: asyncio.AbstractEventLoop):
    """Run the result of a library call that may or may not be a
    coroutine, depending on the installed pyrogram/kurigram version.

    Newer kurigram releases have, at times, changed Client.start()/
    stop() to already run to completion synchronously (returning None)
    instead of returning a coroutine — which makes a bare
    `loop.run_until_complete(app.start())` crash with:
    'TypeError: An asyncio.Future, a coroutine or an awaitable is
    required', even though everything actually started up fine. This
    checks which behavior we got and only hands the loop a real
    awaitable."""
    if inspect.isawaitable(value):
        return loop.run_until_complete(value)
    return value


if __name__ == "__main__":
    logger.info("Starting Faphouse Bot...")
    keep_alive()
    pot_provider.start_background()  # non-blocking — see pot_provider.py
    flaresolver_bootstrap.start_background()  # non-blocking — see flaresolverr_bootstrap.py
    browser_bootstrap.start_background()  # non-blocking — installs playwright + chromium for fpo browser login
    cfbypass_bootstrap.start_background()  # non-blocking — starts cf-bypass/ (Turnstile token service) when entrypoint.sh didn't
    auto_scraper.configure_caption_builder(build_caption, schedule_delete, build_stream_button_markup)
    loop = asyncio.get_event_loop()
    loop.run_until_complete(ensure_indexes())
    saved_cookies = loop.run_until_complete(get_bot_setting("fpo_cookies"))
    if saved_cookies:
        n = fpo.set_cookies(saved_cookies)
        logger.info(f"Loaded {n} fpo.xxx cookie(s) from database (set via /setcookies).")
    saved_login = loop.run_until_complete(get_bot_setting("fpo_login"))
    # The zip is the source of truth for the fpo.xxx login (FPO_USERNAME/FPO_PASSWORD env, else the account
    # hardcoded in fpo_downloader.py) - nothing has to be typed into the bot after a deploy. An old
    # /setfpologin value left in the database no longer overrides it, unless FPO_USE_DB_LOGIN=1.
    if saved_login and os.getenv("FPO_USE_DB_LOGIN", "0").strip().lower() not in ("1", "true", "yes", "on"):
        logger.info("Ignoring fpo.xxx login saved in the database (/setfpologin) - using the login from the code/env. "
                    "Set FPO_USE_DB_LOGIN=1 to prefer the saved one.")
        saved_login = None
    if saved_login and not fpo.credentials_from_env():     # real env vars (FPO_USERNAME/PASSWORD) win; /setfpologin beats the hardcoded default
        try:
            _cred = json.loads(saved_login)
            fpo.set_credentials(_cred.get("u", ""), _cred.get("p", ""))
            logger.info("Loaded fpo.xxx login from database (set via /setfpologin).")
        except Exception as _e:
            logger.warning(f"Couldn't load saved fpo.xxx login: {_e}")
    _run_maybe_async(app.start(), loop)
    # Log in to fpo.xxx right at boot (background thread, never blocks the bot): the first video/monitor request then
    # already has a valid session and nobody has to run /fpologin or /setfpologin after deploying.
    if fpo.has_credentials():
        fpo.start_login_keeper()   # background thread: logs in now, retries after a failure, refreshes before expiry
    # BUG FIX: ytsearch handlers MUST be registered on the app instance
    # (not at import time via @Client.on_* class decorators) — class-level
    # decorators only fire when Pyrogram's plugin system is active.
    # register() is called here, after app.start(), so all callbacks
    # (ytsr:N:key, ytsrpg:N:key, ytsr:noop:x) are bound to the running app.
    ytsearch.register(app, make_button, BTN_PRIMARY, LINK_CACHE, show_quality_menu)
    loop.run_until_complete(set_bot_commands_list())
    loop.run_until_complete(_startup_log())
    loop.run_until_complete(_resume_pending_deletes(app))
    loop.run_until_complete(_resume_active_downloads(app))
    loop.create_task(cache_channel_health_check_loop(app))
    loop.run_until_complete(auto_scraper.notify_server_restart(app))
    if DEFAULT_CHANNEL:
        asyncio.ensure_future(auto_scraper.live_site_monitor(app, DEFAULT_CHANNEL), loop=loop)
        asyncio.ensure_future(auto_scraper.eporner_live_monitor(app, DEFAULT_CHANNEL), loop=loop)
        asyncio.ensure_future(auto_scraper.xhamster_live_monitor(app, DEFAULT_CHANNEL), loop=loop)
        asyncio.ensure_future(auto_scraper.xvideos_live_monitor(app, DEFAULT_CHANNEL), loop=loop)
        asyncio.ensure_future(auto_scraper.fpo_live_monitor(app, DEFAULT_CHANNEL), loop=loop)
        asyncio.ensure_future(auto_scraper.mat6tube_live_monitor(app, DEFAULT_CHANNEL), loop=loop)
        # SpankBang's 24/7 "latest uploads" monitor is OFF by default: it uploaded site-wide new videos on its own after every
        # restart and again after /stopupload. Set SPANKBANG_MONITOR=1 only if you want that. /autouploadspankbang <name> is unaffected.
        if os.getenv("SPANKBANG_MONITOR", "0").strip().lower() in ("1", "true", "yes", "on"):
            asyncio.ensure_future(auto_scraper.spankbang_live_monitor(app, DEFAULT_CHANNEL), loop=loop)
    loop.run_until_complete(titanium.boot_titanium_bots())
    idle()
    _run_maybe_async(app.stop(), loop)

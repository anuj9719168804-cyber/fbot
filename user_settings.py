"""Per-user upload settings, shown in /settings:
  * Upload mode: Video (default) or Document. Document sends the file as-is, without Telegram's video preview.
  * Gofile token: files over Telegram's 2 GB limit go to Gofile instead of being split, when a token is set.

The token is set with /gofile_token <token> (the message is deleted right after, so the token isn't left in
chat) and removed with /gofile_remove. It is never shown in full.
"""
import logging

from pyrogram import filters
from pyrogram.enums import ParseMode

import database

logger = logging.getLogger("user_settings")
_ns = None
MODES = {"video": "🎬 Video", "document": "📄 Document"}
FORMATS = {"mp4": "🎞 MP4", "mkv": "📦 MKV"}


async def get_output_format(uid: int) -> str:
    """Container for files fbot muxes itself (MX Player, /m3u8). Direct links keep their MP4 conversion."""
    fmt = (await database.get_user_settings(uid)).get("output_format", "mp4")
    return fmt if fmt in FORMATS else "mp4"


def _format_text(fmt: str) -> str:
    return ("<b>🎞 Output format</b>\n\n"
            "🎞 <b>MP4</b>: plays inline in Telegram. Default.\n"
            "📦 <b>MKV</b>: keeps every audio and subtitle track as-is, but Telegram shows it as a file, not a "
            "video player.\n\n"
            "Applies to MX Player and /m3u8 downloads. Direct links are still converted to MP4.\n\n"
            f"Current: <b>{FORMATS[fmt]}</b>")


def _format_kb(fmt: str):
    ns = _ns
    rows = [[ns.make_button(ns.SC(("✅ " if k == fmt else "") + label), callback_data=f"ust_fmt:{k}",
                            style=ns.BTN_PRIMARY)] for k, label in FORMATS.items()]
    rows.append([ns.make_button(ns.SC("⬅️ Back"), callback_data="settings_back", style=ns.BTN_PRIMARY)])
    return ns.InlineKeyboardMarkup(rows)


async def get_upload_mode(uid: int) -> str:
    mode = (await database.get_user_settings(uid)).get("upload_mode", "video")
    return mode if mode in MODES else "video"


async def get_gofile_token(uid: int) -> str:
    return (await database.get_user_settings(uid)).get("gofile_token", "") or ""


def _mask(token: str) -> str:
    return ("•" * 8 + token[-4:]) if len(token) > 4 else "••••"


def _upload_text(mode: str) -> str:
    return ("<b>📤 Upload mode</b>\n\n"
            "🎬 <b>Video</b>: sent as a playable video with a preview (streams in Telegram).\n"
            "📄 <b>Document</b>: sent as a file, no preview. Better for MKV and other formats Telegram "
            "won't play as video.\n\n"
            f"Current: <b>{MODES[mode]}</b>")


def _upload_kb(mode: str):
    ns = _ns
    rows = []
    for key, label in MODES.items():
        mark = "✅ " if key == mode else ""
        rows.append([ns.make_button(ns.SC(mark + label), callback_data=f"ust_mode:{key}", style=ns.BTN_PRIMARY)])
    rows.append([ns.make_button(ns.SC("⬅️ Back"), callback_data="settings_back", style=ns.BTN_PRIMARY)])
    return ns.InlineKeyboardMarkup(rows)


async def _gofile_view(uid: int):
    ns = _ns
    token = await get_gofile_token(uid)
    if token:
        text = ("<b>☁️ Gofile token</b>\n\n"
                f"Saved: <code>{_mask(token)}</code>\n\n"
                "Files over 2 GB will be uploaded to Gofile and you get a link.\n"
                "To change it, send <code>/gofile_token &lt;new token&gt;</code>.")
        rows = [[ns.make_button(ns.SC("🗑 Remove token"), callback_data="ust_gofile_rm", style=ns.BTN_DANGER)]]
    else:
        text = ("<b>☁️ Gofile token</b>\n\n"
                "No token saved. Without one, files over 2 GB are split into parts (videos only).\n\n"
                "Get a token at gofile.io (account settings), then send:\n"
                "<code>/gofile_token &lt;your token&gt;</code>")
        rows = []
    rows.append([ns.make_button(ns.SC("⬅️ Back"), callback_data="settings_back", style=ns.BTN_PRIMARY)])
    return text, ns.InlineKeyboardMarkup(rows)


def setup(app, ns) -> None:
    global _ns
    _ns = ns
    from pyrogram import filters as f

    @app.on_callback_query(f.regex(r"^ust_upload$"))
    async def ust_upload(client, cq):
        mode = await get_upload_mode(cq.from_user.id)
        await cq.message.edit_text(ns.SC(_upload_text(mode)), reply_markup=_upload_kb(mode),
                                   parse_mode=ParseMode.HTML)
        await cq.answer()

    @app.on_callback_query(f.regex(r"^ust_mode:(video|document)$"))
    async def ust_mode(client, cq):
        mode = cq.matches[0].group(1)
        await database.set_user_setting(cq.from_user.id, "upload_mode", mode)
        await cq.message.edit_text(ns.SC(_upload_text(mode)), reply_markup=_upload_kb(mode),
                                   parse_mode=ParseMode.HTML)
        await cq.answer(f"Upload mode: {MODES[mode]}")

    @app.on_callback_query(f.regex(r"^ust_format$"))
    async def ust_format(client, cq):
        fmt = await get_output_format(cq.from_user.id)
        await cq.message.edit_text(ns.SC(_format_text(fmt)), reply_markup=_format_kb(fmt), parse_mode=ParseMode.HTML)
        await cq.answer()

    @app.on_callback_query(f.regex(r"^ust_fmt:(mp4|mkv)$"))
    async def ust_fmt(client, cq):
        fmt = cq.matches[0].group(1)
        await database.set_user_setting(cq.from_user.id, "output_format", fmt)
        await cq.message.edit_text(ns.SC(_format_text(fmt)), reply_markup=_format_kb(fmt), parse_mode=ParseMode.HTML)
        await cq.answer(f"Output: {FORMATS[fmt]}")

    @app.on_callback_query(f.regex(r"^ust_gofile$"))
    async def ust_gofile(client, cq):
        text, kb = await _gofile_view(cq.from_user.id)
        await cq.message.edit_text(ns.SC(text), reply_markup=kb, parse_mode=ParseMode.HTML)
        await cq.answer()

    @app.on_callback_query(f.regex(r"^ust_gofile_rm$"))
    async def ust_gofile_rm(client, cq):
        await database.set_user_setting(cq.from_user.id, "gofile_token", None)
        text, kb = await _gofile_view(cq.from_user.id)
        await cq.message.edit_text(ns.SC(text), reply_markup=kb, parse_mode=ParseMode.HTML)
        await cq.answer("Token removed.")

    @app.on_message(f.command("gofile_token") & f.private)
    async def gofile_token_cmd(client, m):
        parts = (m.text or "").split(None, 1)
        if len(parts) < 2 or len(parts[1].strip()) < 8 or " " in parts[1].strip():
            return await m.reply(ns.SC("<b>Usage:</b> <code>/gofile_token &lt;your token&gt;</code>"),
                                 parse_mode=ParseMode.HTML)
        token = parts[1].strip()
        await database.set_user_setting(m.from_user.id, "gofile_token", token)
        try:  # keep the token out of the chat history
            await m.delete()
        except Exception:
            pass
        await m.reply(ns.SC(f"✅ Gofile token saved: <code>{_mask(token)}</code>\nFiles over 2 GB will go to Gofile."),
                      parse_mode=ParseMode.HTML)

    @app.on_message(f.command("gofile_remove") & f.private)
    async def gofile_remove_cmd(client, m):
        await database.set_user_setting(m.from_user.id, "gofile_token", None)
        await m.reply(ns.SC("🗑 Gofile token removed."), parse_mode=ParseMode.HTML)

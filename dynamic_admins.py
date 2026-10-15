"""Runtime-managed admins: /addadmin, /removeadmin (owner only) and /admins (any admin).

Admins from the ADMINS env var (config.py) are permanent. Ones added here are stored in MongoDB
(collection dynamic_admins), loaded at startup, and survive restarts. They are added to the same ADMINS list
object config.py exports, so every `m.from_user.id in ADMINS` check in the bot sees them at once.
"""
import logging

from pyrogram import filters
from pyrogram.enums import ParseMode

import database
from config import ADMINS, OWNER_ID

logger = logging.getLogger("dynamic_admins")

_ENV_ADMINS = set(ADMINS)  # snapshot before any runtime change: these can only be removed from the env var


def _is_admin(_, __, message) -> bool:
    return bool(message.from_user) and message.from_user.id in ADMINS


# Use this instead of filters.user(ADMINS) so admins added at runtime are recognised immediately.
ADMIN_FILTER = filters.create(_is_admin, name="AdminFilter")


async def load_admins() -> None:
    """Call once at startup, after the DB connection exists."""
    try:
        for uid in await database.get_dynamic_admins():
            if uid not in ADMINS:
                ADMINS.append(uid)
        logger.info(f"loaded {len(ADMINS)} admins ({len(_ENV_ADMINS)} from env)")
    except Exception as e:
        logger.warning(f"could not load dynamic admins: {e}")


def _parse_id(message):
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        return None
    return int(parts[1])


def setup(app) -> None:

    @app.on_message(filters.command("addadmin") & filters.private & filters.user(OWNER_ID))
    async def addadmin_cmd(client, message):
        uid = _parse_id(message)
        if uid is None:
            return await message.reply(
                "<b>Usage:</b> <code>/addadmin &lt;user_id&gt;</code>\nAdded admins stay until /removeadmin.",
                parse_mode=ParseMode.HTML)
        if uid in ADMINS:
            return await message.reply(f"<code>{uid}</code> is already an admin.", parse_mode=ParseMode.HTML)
        await database.add_dynamic_admin(uid)
        ADMINS.append(uid)
        await message.reply(f"✅ <code>{uid}</code> is now an admin (saved, survives restarts).",
                            parse_mode=ParseMode.HTML)

    @app.on_message(filters.command("removeadmin") & filters.private & filters.user(OWNER_ID))
    async def removeadmin_cmd(client, message):
        uid = _parse_id(message)
        if uid is None:
            return await message.reply("<b>Usage:</b> <code>/removeadmin &lt;user_id&gt;</code>",
                                       parse_mode=ParseMode.HTML)
        if uid == OWNER_ID:
            return await message.reply("The owner can't be removed.")
        if uid in _ENV_ADMINS:
            return await message.reply(
                f"<code>{uid}</code> is set in the ADMINS env var. Remove it there and restart to drop them.",
                parse_mode=ParseMode.HTML)
        if uid not in ADMINS:
            return await message.reply(f"<code>{uid}</code> is not an admin.", parse_mode=ParseMode.HTML)
        await database.remove_dynamic_admin(uid)
        ADMINS.remove(uid)
        await message.reply(f"🗑 <code>{uid}</code> is no longer an admin.", parse_mode=ParseMode.HTML)

    @app.on_message(filters.command("admins") & filters.private & ADMIN_FILTER)
    async def admins_cmd(client, message):
        lines = []
        for uid in ADMINS:
            tag = " 👑 owner" if uid == OWNER_ID else (" (env)" if uid in _ENV_ADMINS else " (added)")
            lines.append(f"• <code>{uid}</code>{tag}")
        await message.reply("<b>Admins</b>\n" + "\n".join(lines), parse_mode=ParseMode.HTML)

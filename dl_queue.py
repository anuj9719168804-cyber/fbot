"""Per-user download queue.

Up to MAX_DOWNLOADS_PER_USER downloads (default 2) run at the same time for one user. Extra ones wait in line
(first come, first served) and start as soon as a slot frees. Every download gets an ID (DL-XXXX) that shows in:
  /queue                 your running and queued downloads
  /canceltask DL-XXXX    stop one (running: cancelled now; queued: removed from the line)
  /cancelqueue           drop everything still waiting (running downloads keep going)
/cancel still stops your newest running download.

url_uploader.handle_url, m3u8_downloader.process_batch and mxplayer_downloader._run call acquire() before
they start and release() when they finish.
"""
import asyncio
import html
import logging
import os
import secrets
import time

from pyrogram import filters
from pyrogram.enums import ParseMode

logger = logging.getLogger("dl_queue")
MAX_PER_USER = max(1, int(os.environ.get("MAX_DOWNLOADS_PER_USER", "2")))
_TASKS: dict = {}          # id -> Rec, insertion order = arrival order
_cond = None
_ns = None


class QueueCancelled(Exception):
    """Raised by acquire() when the download was removed from the queue before it started."""


class Rec:
    __slots__ = ("id", "uid", "chat_id", "kind", "title", "state", "task", "cancel", "created")


def _get_cond():
    global _cond
    if _cond is None:
        _cond = asyncio.Condition()
    return _cond


def _mine(uid):
    return [r for r in _TASKS.values() if r.uid == uid]


def _new_id() -> str:
    while True:
        tid = "DL-" + secrets.token_hex(2).upper()
        if tid not in _TASKS:
            return tid


async def _notify(client, chat_id, text):
    try:
        await client.send_message(chat_id, text, parse_mode=ParseMode.HTML)
    except Exception:
        pass


async def acquire(client, uid: int, chat_id: int, kind: str, title: str) -> Rec:
    rec = Rec()
    rec.id, rec.uid, rec.chat_id, rec.kind = _new_id(), uid, chat_id, kind
    rec.title = (title or kind)[:70]
    rec.state, rec.task, rec.cancel, rec.created = "queued", None, False, time.time()
    _TASKS[rec.id] = rec
    cond = _get_cond()
    notified = False
    try:
        async with cond:
            while True:
                if rec.cancel:
                    raise QueueCancelled()
                mine = _mine(uid)
                running = sum(1 for r in mine if r.state == "running")
                waiting = [r for r in mine if r.state == "queued"]
                if running < MAX_PER_USER and waiting and waiting[0] is rec:
                    rec.state = "running"
                    rec.task = asyncio.current_task()
                    return rec
                if not notified:
                    notified = True
                    pos = waiting.index(rec) + 1 if rec in waiting else 1
                    await _notify(client, chat_id,
                                  f"⏳ <b>{rec.id}</b> is queued (#{pos}). It starts when one of your "
                                  f"downloads finishes. See /queue.")
                await cond.wait()
    except BaseException:
        _TASKS.pop(rec.id, None)       # cancelled or failed while waiting
        raise


async def release(rec) -> None:
    if rec is None:
        return
    _TASKS.pop(rec.id, None)
    cond = _get_cond()
    async with cond:
        cond.notify_all()


async def cancel(task_id: str, uid: int) -> str:
    """-> 'running' | 'queued' | 'notfound'"""
    rec = _TASKS.get(task_id.upper())
    if not rec or rec.uid != uid:
        return "notfound"
    if rec.state == "running":
        if rec.task and not rec.task.done():
            rec.task.cancel()
            return "running"
        return "notfound"
    rec.cancel = True
    cond = _get_cond()
    async with cond:
        cond.notify_all()
    return "queued"


def _label(rec) -> str:
    return html.escape(rec.title)


def setup(app, ns) -> None:
    global _ns
    _ns = ns

    @app.on_message(filters.command("queue") & filters.private)
    async def queue_cmd(client, m):
        mine = sorted(_mine(m.from_user.id), key=lambda r: r.created)
        if not mine:
            return await m.reply("📭 You have no downloads running or queued.")
        lines, pos = [], 0
        for r in mine:
            if r.state == "running":
                lines.append(f"▶️ <code>{r.id}</code> · running · {_label(r)}")
            else:
                pos += 1
                lines.append(f"⏳ <code>{r.id}</code> · queued #{pos} · {_label(r)}")
        await m.reply(f"<b>📋 Your downloads</b> (up to {MAX_PER_USER} run at once)\n\n" + "\n".join(lines)
                      + "\n\nStop one: <code>/canceltask DL-XXXX</code>", parse_mode=ParseMode.HTML)

    @app.on_message(filters.command("canceltask") & filters.private)
    async def canceltask_cmd(client, m):
        parts = (m.text or "").split()
        if len(parts) < 2:
            return await m.reply("<b>Usage:</b> <code>/canceltask DL-XXXX</code> (IDs from /queue)",
                                 parse_mode=ParseMode.HTML)
        res = await cancel(parts[1], m.from_user.id)
        text = {"running": f"🛑 <code>{parts[1].upper()}</code> is being cancelled.",
                "queued": f"🗑 <code>{parts[1].upper()}</code> removed from the queue.",
                "notfound": f"❓ No download <code>{html.escape(parts[1])}</code> of yours. See /queue."}[res]
        await m.reply(text, parse_mode=ParseMode.HTML)

    @app.on_message(filters.command("cancelqueue") & filters.private)
    async def cancelqueue_cmd(client, m):
        waiting = [r for r in _mine(m.from_user.id) if r.state == "queued"]
        for r in waiting:
            r.cancel = True
        if waiting:
            cond = _get_cond()
            async with cond:
                cond.notify_all()
        await m.reply(f"🗑 Removed {len(waiting)} queued download(s). Running ones keep going; use /cancel for those.")

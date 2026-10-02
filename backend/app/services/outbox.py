"""Transactional outbox: enqueue Telegram messages from any process (bot, API, scheduler).

Services call :func:`enqueue` *inside the same database transaction* as the state change, so a
notification is never lost and never sent for a change that rolled back. The bot worker
(``app.bot.delivery``) delivers the rows.
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.keyboards import Rows, inline
from app.models import Outbox, User
from app.timeutil import utcnow

log = logging.getLogger(__name__)


async def enqueue(
    db: AsyncSession,
    chat_id: int,
    text: str,
    *,
    buttons: Rows | None = None,
    dedupe_key: str | None = None,
    preview: bool = False,
    broadcast_id: int | None = None,
    delay_seconds: float = 0,
) -> int | None:
    """Queue a message. Returns the outbox id, or ``None`` if ``dedupe_key`` already exists."""
    values = {
        "chat_id": chat_id,
        "body": text,
        "parse_mode": "HTML",
        "reply_markup": inline(buttons),
        "disable_preview": not preview,
        "status": "queued",
        "next_attempt_at": utcnow() + dt.timedelta(seconds=delay_seconds),
        "dedupe_key": dedupe_key,
        "broadcast_id": broadcast_id,
    }
    stmt = pg_insert(Outbox).values(**values).returning(Outbox.id)
    if dedupe_key is not None:
        stmt = stmt.on_conflict_do_nothing(index_elements=[Outbox.dedupe_key])
    row = (await db.execute(stmt)).first()
    return row[0] if row else None


async def notify_user(db: AsyncSession, user: User, text: str, **kwargs) -> int | None:
    return await enqueue(db, user.telegram_id, text, **kwargs)


async def notify_admins(db: AsyncSession, text: str, *, dedupe_key: str | None = None, buttons: Rows | None = None) -> int:
    """Send to every Telegram admin listed in ``ADMIN_TELEGRAM_IDS``. Returns how many were queued."""
    admin_ids = get_settings().admin_telegram_ids
    if not admin_ids:
        log.warning("ADMIN_TELEGRAM_IDS is empty - admin notification skipped: %s", text[:80])
        return 0
    queued = 0
    for admin_id in admin_ids:
        key = f"{dedupe_key}:{admin_id}" if dedupe_key else None
        if await enqueue(db, admin_id, text, buttons=buttons, dedupe_key=key) is not None:
            queued += 1
    return queued

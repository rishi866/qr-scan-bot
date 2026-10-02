"""Outbox delivery: sends queued Telegram messages with retry, rate limiting and blocked-bot handling.

Claiming uses a short *lease* (``next_attempt_at`` pushed forward) instead of holding row locks while
talking to Telegram; a crash mid-send therefore re-delivers after the lease instead of losing the message.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut

from app.db import session_scope
from app.enums import OutboxStatus
from app.models import Outbox, User
from app.timeutil import utcnow

log = logging.getLogger(__name__)

LEASE_SECONDS = 120
MAX_ATTEMPTS = 6
SEND_INTERVAL = 0.04  # ~25 messages/second, below Telegram's 30/s global limit
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class _Job:
    id: int
    chat_id: int
    body: str
    parse_mode: str | None
    reply_markup: dict[str, Any] | None
    disable_preview: bool
    attempts: int


def build_markup(data: dict[str, Any] | None) -> InlineKeyboardMarkup | None:
    if not data:
        return None
    rows = []
    for row in data.get("inline_keyboard", []):
        rows.append([InlineKeyboardButton(text=b["text"], callback_data=b.get("callback_data"), url=b.get("url")) for b in row])
    return InlineKeyboardMarkup(rows) if rows else None


def strip_tags(text: str) -> str:
    return _TAG_RE.sub("", text).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


async def _claim(limit: int) -> list[_Job]:
    now = utcnow()
    async with session_scope() as db:
        rows = (
            await db.execute(
                select(Outbox)
                .where(Outbox.status == OutboxStatus.QUEUED.value, Outbox.next_attempt_at <= now)
                .order_by(Outbox.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        jobs = []
        for row in rows:
            row.attempts += 1
            row.next_attempt_at = now + dt.timedelta(seconds=LEASE_SECONDS)
            jobs.append(_Job(row.id, row.chat_id, row.body, row.parse_mode, row.reply_markup, row.disable_preview, row.attempts))
        return jobs


async def _mark_sent(job_id: int) -> None:
    async with session_scope() as db:
        await db.execute(
            update(Outbox).where(Outbox.id == job_id).values(status=OutboxStatus.SENT.value, sent_at=utcnow(), last_error=None)
        )


async def _mark_failed(job: _Job, error: str, *, blocked: bool = False) -> None:
    async with session_scope() as db:
        await db.execute(update(Outbox).where(Outbox.id == job.id).values(status=OutboxStatus.FAILED.value, last_error=error[:500]))
        if blocked:
            await db.execute(update(User).where(User.telegram_id == job.chat_id).values(bot_blocked=True))


async def _reschedule(job: _Job, delay: float, error: str) -> None:
    async with session_scope() as db:
        await db.execute(
            update(Outbox)
            .where(Outbox.id == job.id)
            .values(next_attempt_at=utcnow() + dt.timedelta(seconds=delay), last_error=error[:500])
        )


async def _send(bot: Bot, job: _Job, *, plain: bool = False) -> None:
    await bot.send_message(
        chat_id=job.chat_id,
        text=strip_tags(job.body) if plain else job.body,
        parse_mode=None if plain else job.parse_mode,
        reply_markup=build_markup(job.reply_markup),
        link_preview_options=LinkPreviewOptions(is_disabled=job.disable_preview),
    )


async def deliver_due(bot: Bot, limit: int = 30) -> int:
    """Send up to ``limit`` due messages; returns how many were delivered."""
    delivered = 0
    for job in await _claim(limit):
        try:
            try:
                await _send(bot, job)
            except BadRequest as exc:
                if "parse entities" in str(exc).lower():  # our HTML was rejected: degrade to plain text, never drop it
                    log.warning("outbox %s: HTML rejected (%s) - resending as plain text", job.id, exc)
                    await _send(bot, job, plain=True)
                else:
                    raise
            await _mark_sent(job.id)
            delivered += 1
        except Forbidden as exc:  # user blocked the bot / deactivated account
            await _mark_failed(job, f"Forbidden: {exc}", blocked=True)
        except RetryAfter as exc:
            await _reschedule(job, float(exc.retry_after) + 1, f"RetryAfter {exc.retry_after}")
        except BadRequest as exc:  # e.g. chat not found - retrying cannot help
            await _mark_failed(job, f"BadRequest: {exc}")
        except (TimedOut, NetworkError) as exc:
            if job.attempts >= MAX_ATTEMPTS:
                await _mark_failed(job, f"gave up after {job.attempts} attempts: {exc}")
            else:
                await _reschedule(job, min(5 * 2**job.attempts, 300), f"{type(exc).__name__}: {exc}")
        except TelegramError as exc:
            await _mark_failed(job, f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            log.exception("unexpected error delivering outbox %s", job.id)
            if job.attempts >= MAX_ATTEMPTS:
                await _mark_failed(job, f"unexpected: {exc}")
            else:
                await _reschedule(job, 30, f"unexpected: {exc}")
        await asyncio.sleep(SEND_INTERVAL)
    return delivered

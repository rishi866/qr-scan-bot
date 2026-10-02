"""Time-driven work. Every timer lives in the database (``expires_at``, ``deadline_at`` ...), so a
restart or a second worker never loses or duplicates anything.

Each ``tick_*`` function is idempotent: it finds what is due and processes each item in its *own*
transaction (re-checking the condition under a row lock), so one bad row cannot block the others.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import callbacks, states, texts, timeutil
from app.config import get_settings
from app.db import session_scope
from app.enums import OutboxStatus, Role, UserStatus
from app.models import Dispute, Outbox, ScannerSlot, SlotNotification, User
from app.services import disputes, outbox, sessions, settings_service
from app.timeutil import ensure_utc, utcnow

log = logging.getLogger(__name__)

Verifier = Callable[[bytes, str], Awaitable[dict[str, Any]]]


async def _each(ids: list[int], fn: Callable, label: str) -> int:
    done = 0
    for item_id in ids:
        try:
            async with session_scope() as db:
                if await fn(db, item_id):
                    done += 1
        except Exception:
            log.exception("%s %s failed", label, item_id)
    return done


# ── task sessions ───────────────────────────────────────────────────────────


async def tick_sessions(now: dt.datetime | None = None) -> dict[str, int]:
    now = ensure_utc(now or utcnow())
    async with session_scope() as db:
        due = await sessions.find_due(db, now)
    return {
        "expired": await _each(due["expire"], lambda db, i: sessions.process_expire(db, i, now), "expire"),
        "timed_out": await _each(due["task_timeout"], lambda db, i: sessions.process_task_timeout(db, i, now), "task_timeout"),
        "prompted": await _each(due["prompt"], lambda db, i: sessions.process_prompt(db, i, now), "prompt"),
        "auto_confirmed": await _each(due["autoconfirm"], lambda db, i: sessions.process_autoconfirm(db, i, now), "autoconfirm"),
    }


# ── housekeeping ────────────────────────────────────────────────────────────

RETENTION_DAYS = 30


async def tick_housekeeping(now: dt.datetime | None = None, days: int = RETENTION_DAYS) -> dict[str, int]:
    """Keep the busiest tables bounded: delivered / given-up messages and old slot reminders are not needed after a month.

    Broadcast deliveries are kept (the broadcast history counts them); money, tasks, disputes and the audit log are never touched.
    """
    cutoff = ensure_utc(now or utcnow()) - dt.timedelta(days=days)
    async with session_scope() as db:
        messages = await db.execute(
            delete(Outbox).where(
                Outbox.status.in_([OutboxStatus.SENT.value, OutboxStatus.FAILED.value]),
                Outbox.created_at < cutoff,
                Outbox.broadcast_id.is_(None),
            )
        )
        reminders = await db.execute(delete(SlotNotification).where(SlotNotification.occurrence_end < cutoff))
    return {"outbox": messages.rowcount or 0, "slot_reminders": reminders.rowcount or 0}


# ── disputes ────────────────────────────────────────────────────────────────


async def tick_disputes(now: dt.datetime | None = None) -> dict[str, int]:
    now = ensure_utc(now or utcnow())
    async with session_scope() as db:
        due = await disputes.due_proof_timeouts(db, now)
    return {"proof_timeouts": await _each(due, lambda db, i: disputes.process_proof_timeout(db, i, now), "proof_timeout")}


def proof_path(relative: str) -> Path:
    """Absolute path of a stored proof; refuses anything outside ``UPLOAD_DIR`` (path traversal guard)."""
    base = Path(get_settings().upload_dir).resolve()
    path = (base / relative).resolve()
    if base != path and base not in path.parents:
        raise ValueError("proof path escapes the upload directory")
    return path


def _media_type(path: Path) -> str:
    return {".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}.get(path.suffix.lower(), "image/jpeg")


async def reset_running_ai() -> None:
    """Startup recovery: a verification interrupted mid-call is simply queued again."""
    async with session_scope() as db:
        await db.execute(update(Dispute).where(Dispute.ai_status == "running").values(ai_status="pending"))


async def tick_ai(verify: Verifier, limit: int = 3) -> dict[str, int]:
    """Run the screenshot verifier for disputes whose proof just arrived (outside any DB transaction)."""
    claimed: list[tuple[int, str | None]] = []
    async with session_scope() as db:
        for dispute in await disputes.pending_ai(db, limit):
            dispute.ai_status = "running"
            claimed.append((dispute.id, dispute.proof_file))
    stats = {"auto_pay": 0, "auto_refund": 0, "escalated": 0}
    for dispute_id, proof_file in claimed:
        verdict: dict[str, Any] | None = None
        error: str | None = None
        try:
            if not proof_file:
                raise ValueError("no proof file")
            path = proof_path(proof_file)
            verdict = await verify(path.read_bytes(), _media_type(path))
        except Exception as exc:
            log.exception("AI verification failed for dispute %s", dispute_id)
            error = f"{type(exc).__name__}: {exc}"
        async with session_scope() as db:
            outcome = await disputes.apply_ai_result(db, dispute_id, verdict, error=error)
        stats[outcome] = stats.get(outcome, 0) + 1
    return stats


# ── pre-slot reminders ──────────────────────────────────────────────────────


async def tick_slot_notifications(now: dt.datetime | None = None) -> int:
    """Remind scanners ``pre_slot_notice_minutes`` before each slot starts (once per slot occurrence)."""
    now = ensure_utc(now or utcnow())
    sent = 0
    async with session_scope() as db:
        cfg = await settings_service.load(db)
        notice = dt.timedelta(minutes=cfg.pre_slot_notice_minutes)
        rows = (
            await db.execute(
                select(ScannerSlot, User)
                .join(User, User.user_id == ScannerSlot.user_id)
                .where(
                    ScannerSlot.is_active.is_(True),
                    User.role == Role.SCANNER.value,
                    User.status == UserStatus.APPROVED.value,
                    User.bot_blocked.is_(False),
                )
            )
        ).all()
        for slot, scanner in rows:
            try:
                start, end = timeutil.next_occurrence(slot.slot_start, slot.slot_end, slot.timezone, now)
            except ValueError:
                continue
            if start - now > notice:
                continue
            claimed = (
                await db.execute(
                    pg_insert(SlotNotification)
                    .values(slot_id=slot.id, user_id=scanner.user_id, occurrence_start=start, occurrence_end=end)
                    .on_conflict_do_nothing(constraint="uq_slot_notifications_occurrence")
                    .returning(SlotNotification.id)
                )
            ).first()
            if claimed is None:
                continue  # already reminded for this occurrence
            minutes = max(1, round((start - now).total_seconds() / 60))
            label = timeutil.slot_label(slot.slot_start, slot.slot_end)
            await outbox.notify_user(
                db,
                scanner,
                texts.pre_slot(label, slot.timezone, minutes),
                buttons=[[("✅ Ready", callbacks.ready(claimed[0])), ("❌ Busy", callbacks.busy(claimed[0]))]],
            )
            sent += 1
    return sent


# ── conversation states ─────────────────────────────────────────────────────


async def tick_states(now: dt.datetime | None = None) -> int:
    """Clear expired conversation states; tell the seller when the /send window closed."""
    now = ensure_utc(now or utcnow())
    cleared = 0
    async with session_scope() as db:
        expired = (
            await db.execute(
                select(User).where(User.state.is_not(None), User.state_expires_at.is_not(None), User.state_expires_at <= now).with_for_update(skip_locked=True)
            )
        ).scalars().all()
        for user in expired:
            was = user.state
            user.state = None
            user.state_data = None
            user.state_expires_at = None
            cleared += 1
            if was in states.ANNOUNCE_TIMEOUT:
                await outbox.notify_user(db, user, texts.SEND_TIMEOUT)
    return cleared

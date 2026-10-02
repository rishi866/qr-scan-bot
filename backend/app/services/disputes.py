"""Disputes: seller rejects a "Done" -> scanner uploads a screenshot -> AI and/or admin decide.

Outcomes
* ``pay_scanner``  - the dispute is *rejected*: the task counts as completed and the scanner is paid;
* ``refund_seller``- the dispute is *upheld* (status ``resolved``): the held funds go back to the seller.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import states, texts
from app.config import get_settings
from app.enums import DisputeResolution, DisputeStatus, SessionStatus, TxStatus
from app.models import Dispute, TaskSession, User
from app.services import outbox, settings_service, settlement, users
from app.services.settings_service import RuntimeSettings
from app.timeutil import utcnow

STATE_AWAITING_PROOF = states.AWAITING_PROOF
STATE_DISPUTE_NOTE = states.DISPUTE_NOTE
SELLER_NOTE_TTL_SECONDS = 600


class DisputeError(Exception):
    """Message is safe to show to the person who triggered it."""


async def _lock_session_of(db: AsyncSession, dispute_id: int) -> TaskSession:
    """Lock order is always session -> (scanner user) -> wallets, so resolve starts at the session."""
    session_id = (await db.execute(select(Dispute.session_id).where(Dispute.id == dispute_id))).scalar_one_or_none()
    if session_id is None:
        raise DisputeError(f"dispute {dispute_id} not found")
    return (
        await db.execute(
            select(TaskSession).where(TaskSession.session_id == session_id).with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _lock_dispute(db: AsyncSession, dispute_id: int) -> Dispute:
    return (
        await db.execute(select(Dispute).where(Dispute.id == dispute_id).with_for_update().execution_options(populate_existing=True))
    ).scalar_one()


def ai_is_configured() -> bool:
    return bool(get_settings().anthropic_api_key.get_secret_value())


async def open_dispute(
    db: AsyncSession,
    s: TaskSession,
    seller: User,
    scanner: User,
    cfg: RuntimeSettings,
    reason: str | None = None,
    *,
    now: dt.datetime | None = None,
) -> Dispute:
    now = now or utcnow()
    deadline = now + dt.timedelta(minutes=cfg.dispute_proof_timeout_minutes)
    dispute = Dispute(
        session_id=s.session_id,
        raised_by=seller.user_id,
        reason=(reason or "Rejected by the seller")[:2000],
        status=DisputeStatus.AWAITING_PROOF.value,
        proof_deadline_at=deadline,
    )
    db.add(dispute)
    await db.flush()
    await settlement.record_transaction(db, s, seller, scanner, TxStatus.DISPUTED)

    # the scanner's next photo is the proof; the seller may add one optional text note
    await users.set_state(
        db, scanner, STATE_AWAITING_PROOF, {"dispute_id": dispute.id}, cfg.dispute_proof_timeout_minutes * 60, now=now
    )
    await users.set_state(db, seller, STATE_DISPUTE_NOTE, {"dispute_id": dispute.id}, SELLER_NOTE_TTL_SECONDS, now=now)
    await outbox.notify_user(db, scanner, texts.scanner_dispute(s.session_id, cfg.dispute_proof_timeout_minutes))
    return dispute


async def add_seller_note(db: AsyncSession, dispute_id: int, seller_user_id: int, note: str) -> bool:
    dispute = await _lock_dispute(db, dispute_id)
    if dispute.raised_by != seller_user_id or dispute.status not in (
        DisputeStatus.AWAITING_PROOF.value,
        DisputeStatus.PENDING_REVIEW.value,
    ):
        return False
    base = (dispute.reason or "").strip()
    dispute.reason = (f"{base}\n\nSeller note: {note.strip()}")[:4000]
    return True


async def submit_proof(
    db: AsyncSession, dispute_id: int, scanner_user_id: int, *, proof_file: str, telegram_file_id: str | None
) -> Dispute:
    s = await _lock_session_of(db, dispute_id)
    dispute = await _lock_dispute(db, dispute_id)
    if s.scanner_id != scanner_user_id:
        raise DisputeError("this dispute is not yours")
    if dispute.status != DisputeStatus.AWAITING_PROOF.value:
        raise DisputeError("proof is no longer expected for this dispute")
    scanner = await users.require_user(db, scanner_user_id, lock=True)
    dispute.proof_file = proof_file
    dispute.proof_telegram_file_id = telegram_file_id
    dispute.status = DisputeStatus.PENDING_REVIEW.value
    if scanner.state == STATE_AWAITING_PROOF:
        await users.clear_state(db, scanner)
    await outbox.notify_user(db, scanner, texts.PROOF_RECEIVED)
    if ai_is_configured():
        dispute.ai_status = "pending"  # picked up by the scheduler
    else:
        dispute.ai_status = "skipped"
        await _notify_admins_for_review(db, dispute, None)
    return dispute


async def process_proof_timeout(db: AsyncSession, dispute_id: int, now: dt.datetime) -> bool:
    """Scanner never uploaded a screenshot: hand the case to the admin."""
    s = await _lock_session_of(db, dispute_id)
    dispute = await _lock_dispute(db, dispute_id)
    if dispute.status != DisputeStatus.AWAITING_PROOF.value or not dispute.proof_deadline_at or dispute.proof_deadline_at > now:
        return False
    dispute.status = DisputeStatus.PENDING_REVIEW.value
    dispute.ai_status = "skipped"
    scanner = await users.require_user(db, s.scanner_id, lock=True)
    if scanner.state == STATE_AWAITING_PROOF:
        await users.clear_state(db, scanner)
    await outbox.notify_user(db, scanner, texts.PROOF_TIMEOUT)
    await _notify_admins_for_review(db, dispute, "No screenshot was submitted in time.")
    return True


async def _notify_admins_for_review(db: AsyncSession, dispute: Dispute, ai_summary: str | None) -> None:
    await outbox.notify_admins(
        db, texts.admin_dispute_review(dispute.id, dispute.session_id, ai_summary), dedupe_key=f"dispute-review:{dispute.id}"
    )


# ── AI result handling ──────────────────────────────────────────────────────


def _summarise(verdict: dict[str, Any]) -> str:
    conf = verdict.get("confidence")
    conf_text = f" ({float(conf):.0%})" if isinstance(conf, (int, float)) else ""
    return f"{verdict.get('verdict', 'unclear')}{conf_text} - {verdict.get('reason', '')}".strip(" -")


async def apply_ai_result(db: AsyncSession, dispute_id: int, verdict: dict[str, Any] | None, *, error: str | None = None) -> str:
    """Store the AI verdict and either resolve automatically (clear cases only) or escalate.

    Returns ``"auto_pay"``, ``"auto_refund"`` or ``"escalated"``.
    """
    dispute = await _lock_dispute(db, dispute_id)
    if dispute.status != DisputeStatus.PENDING_REVIEW.value:
        return "escalated"
    cfg = await settings_service.load(db)
    if verdict is None:
        dispute.ai_status = "error"
        dispute.ai_verdict = {"error": (error or "AI verification failed")[:300]}
        await _notify_admins_for_review(db, dispute, "AI verification failed - manual review needed.")
        return "escalated"

    dispute.ai_status = "done"
    dispute.ai_verdict = verdict
    confidence = float(verdict.get("confidence") or 0)
    threshold = float(cfg.ai_confidence_threshold)
    kind = verdict.get("verdict")
    if kind == "completed" and confidence >= threshold and cfg.ai_auto_resolve:
        await resolve(db, dispute_id, DisputeResolution.PAY_SCANNER, by="ai", notes=_summarise(verdict))
        return "auto_pay"
    if kind in ("not_chatgpt", "not_completed") and confidence >= threshold and cfg.ai_auto_refund:
        await resolve(db, dispute_id, DisputeResolution.REFUND_SELLER, by="ai", notes=_summarise(verdict))
        return "auto_refund"
    await _notify_admins_for_review(db, dispute, _summarise(verdict))
    return "escalated"


# ── resolution ──────────────────────────────────────────────────────────────


async def resolve(
    db: AsyncSession, dispute_id: int, resolution: DisputeResolution, *, by: str, notes: str | None = None
) -> Dispute:
    s = await _lock_session_of(db, dispute_id)
    dispute = await _lock_dispute(db, dispute_id)
    if dispute.status not in (DisputeStatus.AWAITING_PROOF.value, DisputeStatus.PENDING_REVIEW.value):
        raise DisputeError("this dispute is already closed")
    if s.status != SessionStatus.DISPUTED.value:
        raise DisputeError(f"task #{s.session_id} is not in dispute (status {s.status})")

    scanner = await users.require_user(db, s.scanner_id, lock=True)
    seller = await users.require_user(db, s.seller_id)
    now = utcnow()

    if resolution == DisputeResolution.PAY_SCANNER:
        scanner_balance, _ = await settlement.pay(db, s)
        s.status = SessionStatus.CONFIRMED.value
        s.confirmed_at = now
        s.closed_reason = "dispute_rejected"
        await settlement.record_transaction(db, s, seller, scanner, TxStatus.COMPLETED)
        dispute.status = DisputeStatus.REJECTED.value
        await users.adjust_reputation(db, scanner, settlement.REP_DISPUTE_WON)
        await outbox.notify_user(db, scanner, texts.scanner_dispute_paid(s.session_id, s.amount))
        await outbox.notify_user(db, seller, texts.seller_dispute_rejected(s.session_id))
    else:
        await settlement.release(db, s, f"dispute #{dispute.id} refund")
        s.status = SessionStatus.REFUNDED.value
        s.closed_reason = "dispute_upheld"
        await settlement.record_transaction(db, s, seller, scanner, TxStatus.REFUNDED)
        dispute.status = DisputeStatus.RESOLVED.value
        await users.adjust_reputation(db, scanner, settlement.REP_DISPUTE_LOST)
        await outbox.notify_user(db, seller, texts.seller_dispute_refunded(s.session_id, settlement.session_cost(s)))
        await outbox.notify_user(db, scanner, texts.scanner_dispute_lost(s.session_id))

    s.deadline_at = None
    dispute.resolution = resolution.value
    dispute.resolved_by = by
    dispute.resolved_at = now
    if notes:
        dispute.admin_notes = notes[:4000]
    # no one is waiting for proof / note any more
    if scanner.state == STATE_AWAITING_PROOF:
        await users.clear_state(db, scanner)
    if seller.state == STATE_DISPUTE_NOTE:
        await users.clear_state(db, seller)
    return dispute


async def due_proof_timeouts(db: AsyncSession, now: dt.datetime) -> list[int]:
    return list(
        (
            await db.execute(
                select(Dispute.id).where(
                    Dispute.status == DisputeStatus.AWAITING_PROOF.value, Dispute.proof_deadline_at <= now
                )
            )
        ).scalars()
    )


async def pending_ai(db: AsyncSession, limit: int = 5) -> list[Dispute]:
    return list(
        (
            await db.execute(
                select(Dispute)
                .where(Dispute.status == DisputeStatus.PENDING_REVIEW.value, Dispute.ai_status == "pending")
                .order_by(Dispute.id)
                .limit(limit)
            )
        ).scalars()
    )

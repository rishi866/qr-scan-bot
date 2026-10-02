"""Task-session lifecycle (one URL travelling seller -> scanner).

    awaiting_scanner --accept--> accepted --done--> done --confirm--> confirmed (paid)
         |  \\--skip--> skipped      |                  \\--reject--> disputed --> confirmed | refunded
         \\--timeout--> expired      \\--timeout--> timed_out

Rules enforced here:
* the seller's funds (task amount + commission) are *held* when the URL is sent and released again
  on skip / expiry / timeout / refund - nothing is ever charged for work that did not happen;
* every transition is a compare-and-set under a row lock, so double taps and racing workers are safe;
* lock order is always: session -> scanner user -> wallets (see ``settlement``).
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import callbacks, texts
from app.enums import ACTIVE_SESSION_STATUSES, Role, SessionStatus, TxStatus, UserStatus
from app.models import Dispute, TaskSession, User
from app.services import disputes, matching, outbox, settings_service, settlement, users, wallet
from app.services.matching import Candidate
from app.timeutil import ensure_utc, utcnow


class SessionError(Exception):
    """Base class. ``str(exc)`` is safe to show to end users."""


class NotAllowed(SessionError):
    pass


class InvalidState(SessionError):
    def __init__(self, status: str, message: str | None = None):
        super().__init__(message or "This request is no longer active.")
        self.status = status


class ScannerUnavailable(SessionError):
    pass


class SellerBusy(SessionError):
    pass


async def lock_session(db: AsyncSession, session_id: int) -> TaskSession:
    s = (
        await db.execute(
            select(TaskSession).where(TaskSession.session_id == session_id).with_for_update().execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if s is None:
        raise SessionError("Task not found.")
    return s


async def get_session(db: AsyncSession, session_id: int) -> TaskSession | None:
    return (
        await db.execute(select(TaskSession).where(TaskSession.session_id == session_id).execution_options(populate_existing=True))
    ).scalar_one_or_none()


async def active_count_for_seller(db: AsyncSession, seller_id: int) -> int:
    return (
        await db.execute(
            select(func.count()).select_from(TaskSession).where(
                TaskSession.seller_id == seller_id, TaskSession.status.in_([x.value for x in ACTIVE_SESSION_STATUSES])
            )
        )
    ).scalar_one()


def _scanner_buttons(session_id: int):
    return [[("✅ Accept", callbacks.accept(session_id)), ("❌ Skip", callbacks.skip(session_id))]]


# ── create ──────────────────────────────────────────────────────────────────


async def preview(db: AsyncSession, seller: User, now: dt.datetime | None = None) -> Candidate | None:
    """The scanner that would get the URL right now (shown to the seller as ``user1`` only)."""
    return await matching.pick_scanner(db, now)


async def create_session(
    db: AsyncSession, seller: User, scanner_id: int, url: str, *, now: dt.datetime | None = None
) -> TaskSession:
    """Send ``url`` to ``scanner_id`` and hold the seller's funds.

    Raises :class:`ScannerUnavailable` (scanner no longer active / busy), :class:`SellerBusy`,
    :class:`wallet.InsufficientFunds` or :class:`NotAllowed`.
    """
    now = ensure_utc(now or utcnow())
    cfg = await settings_service.load(db)
    if seller.role != Role.SELLER.value or seller.status != UserStatus.APPROVED.value:
        raise NotAllowed("Your account is not allowed to send URLs.")

    # lock order: scanner user row -> seller wallet
    scanner = await users.get_user(db, scanner_id, lock=True)
    if scanner is None or scanner.role != Role.SCANNER.value:
        raise ScannerUnavailable("That user is no longer available.")
    seller_wallet = (await wallet.lock_wallets(db, seller.user_id))[seller.user_id]

    if await active_count_for_seller(db, seller.user_id) >= cfg.max_active_sessions_per_seller:
        raise SellerBusy(texts.SELLER_BUSY)

    candidate = await matching.candidate_for(db, scanner_id, now)
    if candidate is None:
        raise ScannerUnavailable("That user is no longer available.")

    cost = cfg.seller_cost
    if seller_wallet.balance < cost:
        raise wallet.InsufficientFunds(texts.low_balance(cost, seller_wallet.balance))

    s = TaskSession(
        seller_id=seller.user_id,
        scanner_id=scanner.user_id,
        url=url,
        status=SessionStatus.AWAITING_SCANNER.value,
        amount=cfg.task_amount,
        commission=cfg.commission_per_task,
        slot_id=candidate.slot.id,
        slot_label=candidate.slot_label,
        seller_timezone=seller.timezone,
        sent_at=now,
        expires_at=now + dt.timedelta(seconds=cfg.url_response_timeout_seconds),
    )
    db.add(s)
    await db.flush()
    await settlement.hold(db, s, seller_wallet)
    scanner.last_assigned_at = now
    await outbox.notify_user(
        db, scanner, texts.scanner_new_url(s.session_id, url, cfg.url_response_timeout_seconds), buttons=_scanner_buttons(s.session_id)
    )
    return s


# ── scanner actions ─────────────────────────────────────────────────────────


async def _owned_by_scanner(db: AsyncSession, session_id: int, scanner_user_id: int) -> TaskSession:
    s = await lock_session(db, session_id)
    if s.scanner_id != scanner_user_id:
        raise NotAllowed("This task is not yours.")
    return s


async def accept(db: AsyncSession, session_id: int, scanner_user_id: int, now: dt.datetime | None = None) -> TaskSession:
    now = ensure_utc(now or utcnow())
    s = await _owned_by_scanner(db, session_id, scanner_user_id)
    if s.status != SessionStatus.AWAITING_SCANNER.value:
        raise InvalidState(s.status)
    if s.expires_at and s.expires_at <= now:
        # the scheduler (next tick) performs the actual expiry + refund; accepting is no longer possible
        raise InvalidState(SessionStatus.EXPIRED.value, "⌛ This request has just expired.")
    cfg = await settings_service.load(db)
    s.status = SessionStatus.ACCEPTED.value
    s.accepted_at = now
    s.expires_at = None
    s.deadline_at = now + dt.timedelta(minutes=cfg.task_timeout_minutes)
    scanner = await users.require_user(db, s.scanner_id)
    seller = await users.require_user(db, s.seller_id)
    await outbox.notify_user(db, seller, texts.seller_accepted(scanner.alias or "user"))
    return s


async def skip(db: AsyncSession, session_id: int, scanner_user_id: int, now: dt.datetime | None = None) -> TaskSession:
    now = ensure_utc(now or utcnow())
    s = await _owned_by_scanner(db, session_id, scanner_user_id)
    if s.status != SessionStatus.AWAITING_SCANNER.value:
        raise InvalidState(s.status)
    scanner = await users.require_user(db, s.scanner_id, lock=True)
    seller = await users.require_user(db, s.seller_id)
    s.status = SessionStatus.SKIPPED.value
    s.closed_reason = "skipped_by_scanner"
    s.expires_at = None
    await settlement.release(db, s, f"task #{s.session_id} skipped")
    await outbox.notify_user(db, seller, texts.seller_skipped(scanner.alias or "user"))
    return s


async def mark_done(db: AsyncSession, session_id: int, scanner_user_id: int, now: dt.datetime | None = None) -> TaskSession:
    now = ensure_utc(now or utcnow())
    s = await _owned_by_scanner(db, session_id, scanner_user_id)
    if s.status != SessionStatus.ACCEPTED.value:
        raise InvalidState(s.status)
    cfg = await settings_service.load(db)
    s.status = SessionStatus.DONE.value
    s.done_at = now
    s.deadline_at = None
    s.prompt_at = now + dt.timedelta(seconds=cfg.seller_confirm_delay_seconds)
    if cfg.seller_confirm_delay_seconds == 0:
        await _prompt_seller(db, s, cfg, now)
    return s


# ── seller actions ──────────────────────────────────────────────────────────


async def _owned_by_seller(db: AsyncSession, session_id: int, seller_user_id: int) -> TaskSession:
    s = await lock_session(db, session_id)
    if s.seller_id != seller_user_id:
        raise NotAllowed("This task is not yours.")
    return s


async def _prompt_seller(db: AsyncSession, s: TaskSession, cfg, now: dt.datetime) -> None:
    scanner = await users.require_user(db, s.scanner_id)
    seller = await users.require_user(db, s.seller_id)
    s.prompted_at = now
    s.deadline_at = (
        now + dt.timedelta(minutes=cfg.seller_confirm_timeout_minutes) if cfg.seller_confirm_timeout_minutes > 0 else None
    )
    await outbox.notify_user(
        db,
        seller,
        texts.seller_report(scanner.alias or "user"),
        buttons=[[("✅ Confirm", callbacks.confirm(s.session_id)), ("❌ Reject", callbacks.reject(s.session_id))]],
    )


async def confirm(
    db: AsyncSession, session_id: int, seller_user_id: int | None, *, auto: bool = False, now: dt.datetime | None = None
) -> TaskSession:
    """Seller confirms (or the timeout does, ``auto=True``): pay the scanner."""
    now = ensure_utc(now or utcnow())
    s = await lock_session(db, session_id)
    if seller_user_id is not None and s.seller_id != seller_user_id:
        raise NotAllowed("This task is not yours.")
    if s.status != SessionStatus.DONE.value or s.prompted_at is None:
        raise InvalidState(s.status)
    scanner = await users.require_user(db, s.scanner_id, lock=True)
    seller = await users.require_user(db, s.seller_id)
    scanner_balance, seller_balance = await settlement.pay(db, s)
    s.status = SessionStatus.CONFIRMED.value
    s.confirmed_at = now
    s.deadline_at = None
    s.closed_reason = "auto_confirmed" if auto else "confirmed"
    await settlement.record_transaction(db, s, seller, scanner, TxStatus.COMPLETED, now)
    await users.adjust_reputation(db, scanner, settlement.REP_CONFIRMED)
    await outbox.notify_user(db, scanner, texts.scanner_paid(s.session_id, s.amount, scanner_balance))
    if auto:
        await outbox.notify_user(db, seller, texts.seller_confirmed(settlement.session_cost(s), seller_balance, auto=True))
    return s


async def reject(
    db: AsyncSession, session_id: int, seller_user_id: int, *, now: dt.datetime | None = None
) -> tuple[TaskSession, Dispute]:
    """Seller says the work was not done: freeze the funds and open a dispute."""
    now = ensure_utc(now or utcnow())
    s = await _owned_by_seller(db, session_id, seller_user_id)
    if s.status != SessionStatus.DONE.value or s.prompted_at is None:
        raise InvalidState(s.status)
    cfg = await settings_service.load(db)
    scanner = await users.require_user(db, s.scanner_id, lock=True)
    seller = await users.require_user(db, s.seller_id, lock=True)
    s.status = SessionStatus.DISPUTED.value
    s.deadline_at = None
    dispute = await disputes.open_dispute(db, s, seller, scanner, cfg, now=now)
    return s, dispute


# ── timers (called by the scheduler, one session per transaction) ────────────


async def _expire(db: AsyncSession, s: TaskSession, now: dt.datetime) -> None:
    scanner = await users.require_user(db, s.scanner_id, lock=True)
    seller = await users.require_user(db, s.seller_id)
    s.status = SessionStatus.EXPIRED.value
    s.closed_reason = "no_response"
    s.expires_at = None
    await settlement.release(db, s, f"task #{s.session_id} expired")
    await users.adjust_reputation(db, scanner, settlement.REP_EXPIRED)
    await outbox.notify_user(db, seller, texts.seller_expired(scanner.alias or "user"))
    await outbox.notify_user(db, scanner, texts.scanner_expired(s.session_id))


async def process_expire(db: AsyncSession, session_id: int, now: dt.datetime) -> bool:
    s = await lock_session(db, session_id)
    if s.status != SessionStatus.AWAITING_SCANNER.value or not s.expires_at or s.expires_at > now:
        return False
    await _expire(db, s, now)
    return True


async def process_task_timeout(db: AsyncSession, session_id: int, now: dt.datetime) -> bool:
    s = await lock_session(db, session_id)
    if s.status != SessionStatus.ACCEPTED.value or not s.deadline_at or s.deadline_at > now:
        return False
    scanner = await users.require_user(db, s.scanner_id, lock=True)
    seller = await users.require_user(db, s.seller_id)
    s.status = SessionStatus.TIMED_OUT.value
    s.closed_reason = "task_timeout"
    s.deadline_at = None
    await settlement.release(db, s, f"task #{s.session_id} timed out")
    await users.adjust_reputation(db, scanner, settlement.REP_TIMED_OUT)
    await outbox.notify_user(db, seller, texts.seller_timed_out(scanner.alias or "user"))
    await outbox.notify_user(db, scanner, texts.scanner_timed_out(s.session_id))
    return True


async def process_prompt(db: AsyncSession, session_id: int, now: dt.datetime) -> bool:
    s = await lock_session(db, session_id)
    if s.status != SessionStatus.DONE.value or s.prompted_at is not None or not s.prompt_at or s.prompt_at > now:
        return False
    cfg = await settings_service.load(db)
    await _prompt_seller(db, s, cfg, now)
    return True


async def process_autoconfirm(db: AsyncSession, session_id: int, now: dt.datetime) -> bool:
    s = await lock_session(db, session_id)
    if s.status != SessionStatus.DONE.value or s.prompted_at is None or not s.deadline_at or s.deadline_at > now:
        return False
    await confirm(db, session_id, None, auto=True, now=now)
    return True


async def find_due(db: AsyncSession, now: dt.datetime, limit: int = 100) -> dict[str, list[int]]:
    """Ids of sessions whose timer fired (cheap, lock-free; each id is re-checked under lock)."""

    async def ids(*conditions) -> list[int]:
        stmt = select(TaskSession.session_id).where(*conditions).order_by(TaskSession.session_id).limit(limit)
        return list((await db.execute(stmt)).scalars())

    return {
        "expire": await ids(TaskSession.status == SessionStatus.AWAITING_SCANNER.value, TaskSession.expires_at <= now),
        "task_timeout": await ids(TaskSession.status == SessionStatus.ACCEPTED.value, TaskSession.deadline_at <= now),
        "prompt": await ids(
            TaskSession.status == SessionStatus.DONE.value, TaskSession.prompted_at.is_(None), TaskSession.prompt_at <= now
        ),
        "autoconfirm": await ids(
            TaskSession.status == SessionStatus.DONE.value, TaskSession.prompted_at.is_not(None), TaskSession.deadline_at <= now
        ),
    }

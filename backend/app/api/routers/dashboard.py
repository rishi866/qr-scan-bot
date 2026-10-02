"""Dashboard numbers for the admin panel's home page."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter
from sqlalchemy import func, select

from app import timeutil
from app.api.common import money
from app.api.deps import CurrentAdmin, Db
from app.config import get_settings
from app.enums import (
    DepositStatus,
    DisputeStatus,
    Role,
    SessionStatus,
    TxStatus,
    UserStatus,
    WithdrawalStatus,
)
from app.models import (
    Deposit,
    Dispute,
    KVState,
    LedgerEntry,
    TaskSession,
    TransactionHistory,
    User,
    Withdrawal,
)
from app.services import matching, wallet

router = APIRouter(prefix="/api", tags=["dashboard"])

T = TransactionHistory
HEARTBEAT_KEY = "worker_heartbeat"


@router.get("/dashboard")
async def dashboard(_: CurrentAdmin, db: Db):
    now = timeutil.utcnow()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    counts = {
        (role, status): n
        for role, status, n in (await db.execute(select(User.role, User.status, func.count()).group_by(User.role, User.status))).all()
    }

    def total(role: str | None = None, status: str | None = None) -> int:
        return sum(n for (r, s), n in counts.items() if (role is None or r == role) and (status is None or s == status) and s != UserStatus.ONBOARDING.value)

    todays = (
        await db.execute(
            select(func.count(), func.coalesce(func.sum(T.amount), 0), func.coalesce(func.sum(T.commission), 0)).where(
                T.status == TxStatus.COMPLETED.value, T.confirmed_at >= today
            )
        )
    ).one()
    commission_total = (await db.execute(select(func.coalesce(func.sum(LedgerEntry.balance_delta), 0)).where(LedgerEntry.user_id.is_(None)))).scalar_one()

    open_disputes = dict(
        (await db.execute(select(Dispute.status, func.count()).where(Dispute.status.in_([DisputeStatus.AWAITING_PROOF.value, DisputeStatus.PENDING_REVIEW.value])).group_by(Dispute.status))).all()
    )
    pending_withdrawals = (
        await db.execute(
            select(func.count(), func.coalesce(func.sum(Withdrawal.amount), 0)).where(
                Withdrawal.status.in_([WithdrawalStatus.PENDING.value, WithdrawalStatus.APPROVED.value, WithdrawalStatus.PROCESSING.value, WithdrawalStatus.FAILED.value])
            )
        )
    ).one()
    deposits_to_review = (
        await db.execute(select(func.count()).select_from(Deposit).where(Deposit.status.in_([DepositStatus.PENDING.value, DepositStatus.BELOW_MIN.value])))
    ).scalar_one()
    live_sessions = (
        await db.execute(
            select(func.count()).select_from(TaskSession).where(
                TaskSession.status.in_([SessionStatus.AWAITING_SCANNER.value, SessionStatus.ACCEPTED.value, SessionStatus.DONE.value, SessionStatus.DISPUTED.value])
            )
        )
    ).scalar_one()
    needs_name = (
        await db.execute(
            select(func.count()).select_from(User).where(User.role == Role.SCANNER.value, User.status == UserStatus.APPROVED.value, User.alias.is_(None))
        )
    ).scalar_one()

    # last 14 days (UTC), including days without activity
    start = today - dt.timedelta(days=13)
    day = func.date_trunc("day", func.timezone("UTC", T.confirmed_at))
    by_day = {
        d.date(): (n, v, c)
        for d, n, v, c in (
            await db.execute(
                select(day, func.count(), func.coalesce(func.sum(T.amount), 0), func.coalesce(func.sum(T.commission), 0))
                .where(T.status == TxStatus.COMPLETED.value, T.confirmed_at >= start)
                .group_by(day)
            )
        ).all()
    }
    series = []
    for i in range(14):
        d = (start + dt.timedelta(days=i)).date()
        n, v, c = by_day.get(d, (0, 0, 0))
        series.append({"date": d.isoformat(), "transactions": n, "volume": money(v), "commission": money(c)})

    live = await matching.active_candidates(db, now)
    beat = await db.get(KVState, HEARTBEAT_KEY)
    beat_at = None
    if beat:
        try:
            beat_at = dt.datetime.fromisoformat(beat.value)
        except ValueError:
            beat_at = None
    settings = get_settings()
    report = await wallet.reconcile(db)
    return {
        "users": {
            "total": total(),
            "sellers": total(Role.SELLER.value),
            "scanners": total(Role.SCANNER.value),
            "approved": total(status=UserStatus.APPROVED.value),
            "pending": total(status=UserStatus.PENDING.value),
            "suspended": total(status=UserStatus.SUSPENDED.value),
        },
        "pending_approvals": total(status=UserStatus.PENDING.value),
        "scanners_needing_name": needs_name,
        "today": {"transactions": todays[0], "volume": money(todays[1]), "commission": money(todays[2])},
        "commission_total": money(commission_total),
        "disputes": {
            "open": sum(open_disputes.values()),
            "awaiting_proof": open_disputes.get(DisputeStatus.AWAITING_PROOF.value, 0),
            "pending_review": open_disputes.get(DisputeStatus.PENDING_REVIEW.value, 0),
        },
        "withdrawals": {"to_handle": pending_withdrawals[0], "amount": money(pending_withdrawals[1])},
        "deposits_to_review": deposits_to_review,
        "live_sessions": live_sessions,
        "active_scanners_now": [
            {"user_id": c.scanner.user_id, "alias": c.scanner.alias, "slot": c.slot_label, "country": c.scanner.country, "reputation": c.scanner.reputation} for c in live
        ],
        "series": series,
        "ledger": report.as_json(),
        "system": {
            "worker_online": bool(beat_at and (now - beat_at).total_seconds() < 90),
            "worker_last_seen": beat_at.isoformat() if beat_at else None,
            "ai_configured": bool(settings.anthropic_api_key.get_secret_value()),
            "deposits_configured": bool(settings.hd_xpub or settings.hd_mnemonic.get_secret_value()),
            "auto_payout": bool(settings.auto_payout_enabled and settings.payout_private_key.get_secret_value()),
            "binance_configured": bool(settings.binance_api_key.get_secret_value()),
            "telegram_admins": len(settings.admin_telegram_ids),
        },
        "generated_at": now.isoformat(),
    }


@router.get("/badges")
async def badges(_: CurrentAdmin, db: Db):
    """Cheap counters for the sidebar (polled every few seconds by the panel)."""

    async def count(stmt) -> int:
        return (await db.execute(stmt)).scalar_one()

    return {
        "pending_users": await count(select(func.count()).select_from(User).where(User.status == UserStatus.PENDING.value)),
        "open_disputes": await count(
            select(func.count()).select_from(Dispute).where(Dispute.status.in_([DisputeStatus.AWAITING_PROOF.value, DisputeStatus.PENDING_REVIEW.value]))
        ),
        "withdrawals_to_handle": await count(
            select(func.count()).select_from(Withdrawal).where(
                Withdrawal.status.in_([WithdrawalStatus.PENDING.value, WithdrawalStatus.APPROVED.value, WithdrawalStatus.FAILED.value])
            )
        ),
        "deposits_to_review": await count(
            select(func.count()).select_from(Deposit).where(Deposit.status.in_([DepositStatus.PENDING.value, DepositStatus.BELOW_MIN.value]))
        ),
        "scanners_needing_name": await count(
            select(func.count()).select_from(User).where(User.role == Role.SCANNER.value, User.status == UserStatus.APPROVED.value, User.alias.is_(None))
        ),
    }

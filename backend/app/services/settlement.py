"""Money movements for a task session: hold, release, pay, refund (+ the transaction record).

Callers must already hold the session row lock and the scanner's user-row lock (lock order:
session -> scanner user -> wallets, always).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import LedgerType, TxStatus
from app.models import TaskSession, TransactionHistory, User
from app.services import wallet
from app.timeutil import utcnow

# automatic reputation changes (applied only when the ``auto_reputation`` setting is on)
REP_CONFIRMED = 1
REP_EXPIRED = -1
REP_TIMED_OUT = -2
REP_DISPUTE_WON = 1
REP_DISPUTE_LOST = -5


def session_cost(s: TaskSession) -> Decimal:
    """What the seller is charged: scanner payout + commission."""
    return s.amount + s.commission


async def hold(db: AsyncSession, s: TaskSession, seller_wallet: wallet.Wallet) -> None:
    cost = session_cost(s)
    await wallet.post(
        db, seller_wallet, LedgerType.HOLD, balance_delta=-cost, pending_delta=cost,
        ref_type="session", ref_id=s.session_id, note=f"task #{s.session_id} reserved",
    )


async def release(db: AsyncSession, s: TaskSession, note: str) -> None:
    """The task did not happen: give the held funds back to the seller."""
    cost = session_cost(s)
    wallets = await wallet.lock_wallets(db, s.seller_id)
    await wallet.post(
        db, wallets[s.seller_id], LedgerType.RELEASE, balance_delta=cost, pending_delta=-cost,
        ref_type="session", ref_id=s.session_id, note=note,
    )


async def pay(db: AsyncSession, s: TaskSession) -> tuple[Decimal, Decimal]:
    """Consume the seller's hold: scanner earns ``amount``, the platform keeps ``commission``.

    Returns ``(scanner_balance_after, seller_balance_after)``.
    """
    cost = session_cost(s)
    wallets = await wallet.lock_wallets(db, s.seller_id, s.scanner_id)
    seller_w, scanner_w = wallets[s.seller_id], wallets[s.scanner_id]
    await wallet.post(
        db, seller_w, LedgerType.PAYMENT, pending_delta=-cost, spent=cost,
        ref_type="session", ref_id=s.session_id, note=f"task #{s.session_id} paid",
    )
    await wallet.post(
        db, scanner_w, LedgerType.EARNING, balance_delta=s.amount, earned=s.amount,
        ref_type="session", ref_id=s.session_id, note=f"task #{s.session_id} reward",
    )
    if s.commission > 0:
        await wallet.post(
            db, None, LedgerType.COMMISSION, balance_delta=s.commission,
            ref_type="session", ref_id=s.session_id, note=f"commission task #{s.session_id}",
        )
    return scanner_w.balance, seller_w.balance


async def get_transaction(db: AsyncSession, session_id: int) -> TransactionHistory | None:
    return (
        await db.execute(select(TransactionHistory).where(TransactionHistory.session_id == session_id).with_for_update())
    ).scalar_one_or_none()


async def record_transaction(
    db: AsyncSession, s: TaskSession, seller: User, scanner: User, status: TxStatus, now: dt.datetime | None = None
) -> TransactionHistory:
    """Create (or update the status of) the transaction record of a session."""
    tx = await get_transaction(db, s.session_id)
    now = now or utcnow()
    if tx is None:
        tx = TransactionHistory(
            session_id=s.session_id,
            seller_id=seller.user_id,
            scanner_id=scanner.user_id,
            scanner_name=scanner.alias,
            seller_name=seller.name,
            seller_country=seller.country,
            seller_timezone=seller.timezone,
            scanner_country=scanner.country,
            scanner_timezone=scanner.timezone,
            slot=s.slot_label,
            url=s.url,
            amount=s.amount,
            commission=s.commission,
            status=status.value,
            confirmed_at=now if status != TxStatus.DISPUTED else None,
        )
        db.add(tx)
    else:
        tx.status = status.value
        if status != TxStatus.DISPUTED:
            tx.confirmed_at = now
    await db.flush()
    return tx

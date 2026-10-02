"""Wallet ledger.

Every change to a balance goes through :func:`post`, which (a) refuses to overdraw, (b) updates the
wallet row and (c) appends an immutable :class:`~app.models.LedgerEntry`. Callers lock the wallets
they touch first with :func:`lock_wallets` (sorted by id -> no deadlocks between two transfers).

Conservation invariant (checked by :func:`reconcile`)::

    sum(balance+pending deltas of all non-adjustment entries)
        == credited deposits - completed withdrawals (net of fee)
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import DepositStatus, LedgerType, WithdrawalStatus
from app.models import Deposit, LedgerEntry, Wallet, Withdrawal
from app.money import ZERO


class WalletError(Exception):
    pass


class InsufficientFunds(WalletError):
    pass


async def ensure_wallets(db: AsyncSession, user_ids: Iterable[int]) -> None:
    ids = sorted(set(user_ids))
    if not ids:
        return
    stmt = pg_insert(Wallet).values([{"user_id": uid} for uid in ids]).on_conflict_do_nothing(index_elements=[Wallet.user_id])
    await db.execute(stmt)


async def lock_wallets(db: AsyncSession, *user_ids: int) -> dict[int, Wallet]:
    """Create-if-missing, then ``SELECT ... FOR UPDATE`` the wallets in ascending id order."""
    ids = sorted(set(user_ids))
    await ensure_wallets(db, ids)
    rows = (
        await db.execute(
            select(Wallet)
            .where(Wallet.user_id.in_(ids))
            .order_by(Wallet.user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    return {w.user_id: w for w in rows}


async def get_wallet(db: AsyncSession, user_id: int) -> Wallet:
    """Read-only access (no lock). Creates the wallet row on first use."""
    await ensure_wallets(db, [user_id])
    wallet = (
        await db.execute(select(Wallet).where(Wallet.user_id == user_id).execution_options(populate_existing=True))
    ).scalar_one()
    return wallet


async def post(
    db: AsyncSession,
    wallet: Wallet | None,
    entry_type: LedgerType,
    *,
    balance_delta: Decimal = ZERO,
    pending_delta: Decimal = ZERO,
    earned: Decimal = ZERO,
    spent: Decimal = ZERO,
    deposited: Decimal = ZERO,
    withdrawn: Decimal = ZERO,
    ref_type: str | None = None,
    ref_id: int | None = None,
    note: str | None = None,
) -> LedgerEntry:
    """Apply a delta to ``wallet`` (``None`` = the platform's own account) and record it."""
    balance_after = pending_after = None
    if wallet is not None:
        new_balance = wallet.balance + balance_delta
        new_pending = wallet.pending + pending_delta
        if new_balance < 0:
            raise InsufficientFunds(f"balance would become negative ({new_balance})")
        if new_pending < 0:
            raise InsufficientFunds(f"held funds would become negative ({new_pending})")
        wallet.balance = new_balance
        wallet.pending = new_pending
        wallet.total_earned += earned
        wallet.total_spent += spent
        wallet.total_deposited += deposited
        wallet.total_withdrawn += withdrawn
        balance_after, pending_after = new_balance, new_pending
    entry = LedgerEntry(
        user_id=wallet.user_id if wallet is not None else None,
        entry_type=entry_type.value,
        balance_delta=balance_delta,
        pending_delta=pending_delta,
        balance_after=balance_after,
        pending_after=pending_after,
        ref_type=ref_type,
        ref_id=ref_id,
        note=note,
    )
    db.add(entry)
    await db.flush()
    return entry


@dataclass
class ReconcileReport:
    ok: bool
    problems: list[str] = field(default_factory=list)
    ledger_total: Decimal = ZERO
    expected_total: Decimal = ZERO
    platform_balance: Decimal = ZERO
    user_total: Decimal = ZERO

    def as_json(self) -> dict:
        return {
            "ok": self.ok,
            "problems": self.problems,
            "ledger_total": str(self.ledger_total),
            "expected_total": str(self.expected_total),
            "platform_balance": str(self.platform_balance),
            "user_total": str(self.user_total),
        }


async def reconcile(db: AsyncSession) -> ReconcileReport:
    """Verify wallets against the ledger and the ledger against deposits/withdrawals."""
    problems: list[str] = []

    # 1. every wallet equals the sum of its ledger deltas
    sums = (
        await db.execute(
            select(
                LedgerEntry.user_id,
                func.coalesce(func.sum(LedgerEntry.balance_delta), 0),
                func.coalesce(func.sum(LedgerEntry.pending_delta), 0),
            )
            .where(LedgerEntry.user_id.is_not(None))
            .group_by(LedgerEntry.user_id)
        )
    ).all()
    ledger_by_user = {uid: (Decimal(b), Decimal(p)) for uid, b, p in sums}
    wallets = (await db.execute(select(Wallet))).scalars().all()
    user_total = ZERO
    for w in wallets:
        user_total += w.balance + w.pending
        lb, lp = ledger_by_user.get(w.user_id, (ZERO, ZERO))
        if lb != w.balance or lp != w.pending:
            problems.append(f"wallet {w.user_id}: stored {w.balance}/{w.pending} != ledger {lb}/{lp}")

    # 2. conservation across the whole system
    non_adj_total = Decimal(
        (
            await db.execute(
                select(func.coalesce(func.sum(LedgerEntry.balance_delta + LedgerEntry.pending_delta), 0)).where(
                    LedgerEntry.entry_type != LedgerType.ADJUSTMENT.value
                )
            )
        ).scalar_one()
    )
    all_total = Decimal(
        (
            await db.execute(select(func.coalesce(func.sum(LedgerEntry.balance_delta + LedgerEntry.pending_delta), 0)))
        ).scalar_one()
    )
    deposits = Decimal(
        (
            await db.execute(
                select(func.coalesce(func.sum(Deposit.amount), 0)).where(Deposit.status == DepositStatus.CREDITED.value)
            )
        ).scalar_one()
    )
    payouts = Decimal(
        (
            await db.execute(
                select(func.coalesce(func.sum(Withdrawal.amount - Withdrawal.fee), 0)).where(
                    Withdrawal.status == WithdrawalStatus.COMPLETED.value
                )
            )
        ).scalar_one()
    )
    expected = deposits - payouts
    if non_adj_total != expected:
        problems.append(f"conservation: ledger {non_adj_total} != deposits {deposits} - payouts {payouts}")

    platform = Decimal(
        (
            await db.execute(
                select(func.coalesce(func.sum(LedgerEntry.balance_delta), 0)).where(LedgerEntry.user_id.is_(None))
            )
        ).scalar_one()
    )
    if user_total + platform != all_total:
        problems.append(f"totals: users {user_total} + platform {platform} != ledger {all_total}")

    return ReconcileReport(
        ok=not problems,
        problems=problems,
        ledger_total=all_total,
        expected_total=expected,
        platform_balance=platform,
        user_total=user_total,
    )

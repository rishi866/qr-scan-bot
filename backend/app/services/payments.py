"""Deposits and withdrawals (database side; the blockchain side lives in ``app.chain``).

Deposit rules
* on-chain credits are idempotent - ``(method, tx_hash, log_index)`` is unique, a rescan can never
  double-credit;
* Binance deposits are *claims* (user submits amount + Binance Order ID); they are credited either by
  the Binance API poller or by an admin.

Withdrawal state machine::

    pending --approve--> approved --payout--> processing --receipt ok--> completed
       \\--reject--> rejected (funds returned)      \\--receipt failed / ambiguous--> failed --retry--> approved
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import texts
from app.chain.bsc import is_valid_address, to_checksum
from app.chain.hd import HDWallet
from app.enums import (
    DepositMethod,
    DepositStatus,
    LedgerType,
    Role,
    UserStatus,
    WithdrawalStatus,
)
from app.models import Deposit, DepositAddress, User, Wallet, Withdrawal
from app.money import q
from app.services import outbox, settings_service, users, wallet
from app.timeutil import utcnow

BINANCE_REF_RE = re.compile(r"^[A-Za-z0-9_\-]{6,64}$")
BINANCE_ID_RE = re.compile(r"^\d{5,20}$")
OPEN_WITHDRAWAL_STATUSES = (
    WithdrawalStatus.PENDING.value,
    WithdrawalStatus.APPROVED.value,
    WithdrawalStatus.PROCESSING.value,
)


class PaymentError(Exception):
    """``str(exc)`` is safe to show to the user / admin."""


# ── deposit addresses ───────────────────────────────────────────────────────


async def get_or_create_deposit_address(db: AsyncSession, user: User, hd: HDWallet) -> DepositAddress:
    existing = (await db.execute(select(DepositAddress).where(DepositAddress.user_id == user.user_id))).scalar_one_or_none()
    expected = hd.address(user.user_id)
    if existing is not None:
        if existing.address != expected:
            raise PaymentError("The deposit address configuration changed. Please contact the admin.")
        return existing
    await db.execute(
        pg_insert(DepositAddress)
        .values(user_id=user.user_id, address=expected, derivation_index=user.user_id)
        .on_conflict_do_nothing(index_elements=[DepositAddress.user_id])
    )
    return (await db.execute(select(DepositAddress).where(DepositAddress.user_id == user.user_id))).scalar_one()


async def verify_hd_consistency(db: AsyncSession, hd: HDWallet) -> list[str]:
    """Stored addresses must still derive from the configured xpub (guards against swapped seeds)."""
    problems = []
    for row in (await db.execute(select(DepositAddress))).scalars():
        if hd.address(row.derivation_index) != row.address:
            problems.append(f"user {row.user_id}: stored address {row.address} != derived address")
    return problems


# ── on-chain deposits ───────────────────────────────────────────────────────


async def _credit(db: AsyncSession, deposit: Deposit, now: dt.datetime) -> Wallet:
    wallets = await wallet.lock_wallets(db, deposit.user_id)
    w = wallets[deposit.user_id]
    await wallet.post(
        db, w, LedgerType.DEPOSIT, balance_delta=deposit.amount, deposited=deposit.amount,
        ref_type="deposit", ref_id=deposit.id, note=f"{deposit.method} deposit {deposit.tx_hash[:18]}",
    )
    deposit.status = DepositStatus.CREDITED.value
    deposit.credited_at = now
    return w


async def credit_onchain_deposit(
    db: AsyncSession,
    *,
    user_id: int,
    amount: Decimal,
    tx_hash: str,
    log_index: int,
    address: str,
    block_number: int | None,
    now: dt.datetime | None = None,
) -> Deposit | None:
    """Record + credit a confirmed BEP-20 transfer. Returns ``None`` when it was already processed."""
    now = now or utcnow()
    cfg = await settings_service.load(db)
    amount = q(amount)
    credit_now = amount >= cfg.min_deposit
    row = (
        await db.execute(
            pg_insert(Deposit)
            .values(
                user_id=user_id, amount=amount, method=DepositMethod.BEP20.value, tx_hash=tx_hash, log_index=log_index,
                address=address, block_number=block_number,
                status=DepositStatus.PENDING.value,  # flipped below, inside the same transaction
            )
            .on_conflict_do_nothing(constraint="uq_deposits_tx")
            .returning(Deposit.id)
        )
    ).first()
    if row is None:
        return None
    deposit = (await db.execute(select(Deposit).where(Deposit.id == row[0]).execution_options(populate_existing=True))).scalar_one()
    user = await users.require_user(db, user_id)
    if credit_now:
        w = await _credit(db, deposit, now)
        await outbox.notify_user(db, user, texts.deposit_credited(amount, w.balance))
    else:
        deposit.status = DepositStatus.BELOW_MIN.value
        deposit.note = "below minimum deposit"
        await outbox.notify_user(db, user, texts.deposit_below_min(amount, cfg.min_deposit, cfg.support_contact))
        await outbox.notify_admins(
            db, texts.admin_deposit_below_min(users.display_label(user), amount, tx_hash), dedupe_key=f"below-min:{deposit.id}"
        )
    return deposit


# ── Binance claims / admin deposit actions ──────────────────────────────────


async def create_binance_claim(db: AsyncSession, user: User, amount: Decimal, reference: str) -> Deposit:
    reference = reference.strip()
    if not BINANCE_REF_RE.match(reference):
        raise PaymentError(texts.DEPOSIT_CLAIM_BAD_REF)
    amount = q(amount)
    if amount <= 0:
        raise PaymentError("The amount must be greater than zero.")
    row = (
        await db.execute(
            pg_insert(Deposit)
            .values(
                user_id=user.user_id, amount=amount, method=DepositMethod.BINANCE.value, tx_hash=reference, log_index=0,
                status=DepositStatus.PENDING.value, note="awaiting verification",
            )
            .on_conflict_do_nothing(constraint="uq_deposits_tx")
            .returning(Deposit.id)
        )
    ).first()
    if row is None:
        raise PaymentError(texts.DEPOSIT_CLAIM_DUPLICATE)
    deposit = (await db.execute(select(Deposit).where(Deposit.id == row[0]).execution_options(populate_existing=True))).scalar_one()
    await outbox.notify_admins(
        db, texts.admin_deposit_claim(users.display_label(user), amount, reference), dedupe_key=f"binance-claim:{deposit.id}"
    )
    return deposit


async def _lock_deposit(db: AsyncSession, deposit_id: int) -> Deposit:
    dep = (
        await db.execute(select(Deposit).where(Deposit.id == deposit_id).with_for_update().execution_options(populate_existing=True))
    ).scalar_one_or_none()
    if dep is None:
        raise PaymentError("Deposit not found.")
    return dep


async def confirm_deposit(db: AsyncSession, deposit_id: int, by: str, *, amount: Decimal | None = None, note: str | None = None) -> Deposit:
    """Admin (or the Binance poller) credits a pending claim / below-minimum deposit."""
    dep = await _lock_deposit(db, deposit_id)
    if dep.status not in (DepositStatus.PENDING.value, DepositStatus.BELOW_MIN.value):
        raise PaymentError(f"Deposit is already {dep.status}.")
    if amount is not None:
        amount = q(amount)
        if amount <= 0:
            raise PaymentError("The amount must be greater than zero.")
        dep.amount = amount
    dep.note = (note or f"confirmed by {by}")[:255]
    w = await _credit(db, dep, utcnow())
    user = await users.require_user(db, dep.user_id)
    await outbox.notify_user(db, user, texts.deposit_credited(dep.amount, w.balance))
    return dep


async def reject_deposit(db: AsyncSession, deposit_id: int, by: str, note: str | None = None) -> Deposit:
    dep = await _lock_deposit(db, deposit_id)
    if dep.status not in (DepositStatus.PENDING.value, DepositStatus.BELOW_MIN.value):
        raise PaymentError(f"Deposit is already {dep.status}.")
    dep.status = DepositStatus.REJECTED.value
    dep.note = (note or f"rejected by {by}")[:255]
    cfg = await settings_service.load(db)
    user = await users.require_user(db, dep.user_id)
    await outbox.notify_user(db, user, texts.deposit_rejected(dep.amount, dep.tx_hash, cfg.support_contact))
    return dep


# ── payout addresses ────────────────────────────────────────────────────────


def validate_payout_address(method: str, address: str) -> str:
    address = (address or "").strip()
    if method == DepositMethod.BEP20.value:
        if not is_valid_address(address):
            raise PaymentError(texts.ADDRESS_INVALID_BEP20)
        return to_checksum(address)
    if method == DepositMethod.BINANCE.value:
        if not BINANCE_ID_RE.match(address):
            raise PaymentError(texts.ADDRESS_INVALID_BINANCE)
        return address
    raise PaymentError("Unknown payout method.")


async def set_payout_address(db: AsyncSession, user: User, method: str, address: str) -> str:
    clean = validate_payout_address(method, address)
    w = (await wallet.lock_wallets(db, user.user_id))[user.user_id]
    if method == DepositMethod.BEP20.value:
        w.bep20_address = clean
    else:
        w.binance_address = clean
    return clean


# ── withdrawals ─────────────────────────────────────────────────────────────


async def request_withdrawal(db: AsyncSession, user: User, method: str, address: str, amount: Decimal) -> Withdrawal:
    if user.role != Role.SCANNER.value or user.status != UserStatus.APPROVED.value:
        raise PaymentError("Only approved scanners can withdraw.")
    cfg = await settings_service.load(db)
    address = validate_payout_address(method, address)
    amount = q(amount)
    fee = q(cfg.withdrawal_fee)
    if amount < cfg.min_withdrawal:
        raise PaymentError(f"The minimum withdrawal is {texts.money(cfg.min_withdrawal)}.")
    if amount <= fee:
        raise PaymentError("The amount must be greater than the withdrawal fee.")
    w = (await wallet.lock_wallets(db, user.user_id))[user.user_id]
    if amount > w.balance:
        raise PaymentError(f"Insufficient balance (available {texts.money(w.balance)}).")
    open_count = (
        await db.execute(
            select(Withdrawal.id).where(Withdrawal.user_id == user.user_id, Withdrawal.status.in_(OPEN_WITHDRAWAL_STATUSES)).limit(1)
        )
    ).first()
    if open_count:
        raise PaymentError("You already have a withdrawal in progress. Please wait until it is paid.")
    wd = Withdrawal(user_id=user.user_id, amount=amount, fee=fee, method=method, address=address, status=WithdrawalStatus.PENDING.value)
    db.add(wd)
    await db.flush()
    await wallet.post(
        db, w, LedgerType.WITHDRAWAL_LOCK, balance_delta=-amount, pending_delta=amount,
        ref_type="withdrawal", ref_id=wd.id, note=f"withdrawal #{wd.id}",
    )
    await outbox.notify_admins(
        db, texts.admin_withdrawal_request(wd.id, users.display_label(user), amount, method, address), dedupe_key=f"withdrawal:{wd.id}"
    )
    return wd


async def lock_withdrawal(db: AsyncSession, withdrawal_id: int) -> Withdrawal:
    wd = (
        await db.execute(select(Withdrawal).where(Withdrawal.id == withdrawal_id).with_for_update().execution_options(populate_existing=True))
    ).scalar_one_or_none()
    if wd is None:
        raise PaymentError("Withdrawal not found.")
    return wd


async def approve_withdrawal(db: AsyncSession, withdrawal_id: int, by: str, note: str | None = None) -> Withdrawal:
    wd = await lock_withdrawal(db, withdrawal_id)
    if wd.status != WithdrawalStatus.PENDING.value:
        raise PaymentError(f"Withdrawal is already {wd.status}.")
    wd.status = WithdrawalStatus.APPROVED.value
    wd.processed_by = by
    if note:
        wd.admin_note = note[:255]
    return wd


async def reject_withdrawal(db: AsyncSession, withdrawal_id: int, by: str, note: str | None = None) -> Withdrawal:
    """Reject (or cancel a failed payout) and give the locked funds back."""
    wd = await lock_withdrawal(db, withdrawal_id)
    if wd.status not in (WithdrawalStatus.PENDING.value, WithdrawalStatus.APPROVED.value, WithdrawalStatus.FAILED.value):
        raise PaymentError(f"Withdrawal is {wd.status} and can no longer be rejected.")
    w = (await wallet.lock_wallets(db, wd.user_id))[wd.user_id]
    await wallet.post(
        db, w, LedgerType.WITHDRAWAL_UNLOCK, balance_delta=wd.amount, pending_delta=-wd.amount,
        ref_type="withdrawal", ref_id=wd.id, note=f"withdrawal #{wd.id} rejected",
    )
    wd.status = WithdrawalStatus.REJECTED.value
    wd.processed_by = by
    wd.processed_at = utcnow()
    wd.admin_note = (note or wd.admin_note or "")[:255] or None
    cfg = await settings_service.load(db)
    user = await users.require_user(db, wd.user_id)
    await outbox.notify_user(db, user, texts.withdraw_rejected(wd.id, note, cfg.support_contact))
    return wd


async def complete_withdrawal(db: AsyncSession, withdrawal_id: int, by: str, tx_hash: str | None) -> Withdrawal:
    """The payout happened (manually or on-chain): consume the locked funds, book the fee."""
    wd = await lock_withdrawal(db, withdrawal_id)
    if wd.status not in (
        WithdrawalStatus.PENDING.value,
        WithdrawalStatus.APPROVED.value,
        WithdrawalStatus.PROCESSING.value,
        WithdrawalStatus.FAILED.value,
    ):
        raise PaymentError(f"Withdrawal is already {wd.status}.")
    w = (await wallet.lock_wallets(db, wd.user_id))[wd.user_id]
    await wallet.post(
        db, w, LedgerType.WITHDRAWAL_PAID, pending_delta=-wd.amount, withdrawn=wd.amount,
        ref_type="withdrawal", ref_id=wd.id, note=f"withdrawal #{wd.id} paid",
    )
    if wd.fee > 0:
        await wallet.post(
            db, None, LedgerType.WITHDRAWAL_FEE, balance_delta=wd.fee,
            ref_type="withdrawal", ref_id=wd.id, note=f"fee withdrawal #{wd.id}",
        )
    wd.status = WithdrawalStatus.COMPLETED.value
    wd.tx_hash = tx_hash or wd.tx_hash or None
    if by != "system" or not wd.processed_by:  # keep the approving admin's name for automatic payouts
        wd.processed_by = by
    wd.processed_at = utcnow()
    user = await users.require_user(db, wd.user_id)
    await outbox.notify_user(db, user, texts.withdraw_completed(wd.id, wd.amount - wd.fee, wd.tx_hash))
    return wd


async def retry_withdrawal(db: AsyncSession, withdrawal_id: int, by: str) -> Withdrawal:
    wd = await lock_withdrawal(db, withdrawal_id)
    if wd.status != WithdrawalStatus.FAILED.value:
        raise PaymentError("Only failed withdrawals can be retried.")
    wd.status = WithdrawalStatus.APPROVED.value
    wd.tx_hash = None
    wd.processed_by = by
    return wd


async def claim_for_payout(db: AsyncSession, withdrawal_id: int) -> Withdrawal | None:
    """approved -> processing (commit this *before* broadcasting so a crash can never double-pay)."""
    wd = await lock_withdrawal(db, withdrawal_id)
    if wd.status != WithdrawalStatus.APPROVED.value:
        return None
    wd.status = WithdrawalStatus.PROCESSING.value
    wd.processed_at = utcnow()
    return wd


async def release_claim(db: AsyncSession, withdrawal_id: int, reason: str) -> None:
    """processing -> approved when the failure happened *before* anything was broadcast."""
    wd = await lock_withdrawal(db, withdrawal_id)
    if wd.status == WithdrawalStatus.PROCESSING.value and not wd.tx_hash:
        wd.status = WithdrawalStatus.APPROVED.value
        wd.admin_note = reason[:255]


async def mark_withdrawal_failed(db: AsyncSession, withdrawal_id: int, reason: str) -> Withdrawal:
    wd = await lock_withdrawal(db, withdrawal_id)
    if wd.status not in (WithdrawalStatus.PROCESSING.value, WithdrawalStatus.APPROVED.value):
        return wd
    wd.status = WithdrawalStatus.FAILED.value
    wd.admin_note = reason[:255]
    user = await users.require_user(db, wd.user_id)
    await outbox.notify_user(db, user, texts.withdraw_failed_user(wd.id))
    await outbox.notify_admins(db, texts.admin_withdrawal_failed(wd.id, reason), dedupe_key=f"withdrawal-failed:{wd.id}")
    return wd


# ── admin wallet adjustment ─────────────────────────────────────────────────


async def adjust_wallet(db: AsyncSession, user_id: int, delta: Decimal, reason: str, by: str) -> Wallet:
    """Manual correction (+/-) with a mandatory reason; recorded as an ``adjustment`` ledger entry."""
    if not reason or not reason.strip():
        raise PaymentError("A reason is required.")
    delta = q(delta)
    if delta == 0:
        raise PaymentError("The adjustment must not be zero.")
    await users.require_user(db, user_id)
    w = (await wallet.lock_wallets(db, user_id))[user_id]
    try:
        await wallet.post(
            db, w, LedgerType.ADJUSTMENT, balance_delta=delta, note=f"{by}: {reason.strip()}"[:255],
            ref_type="admin", ref_id=None,
        )
    except wallet.InsufficientFunds as exc:
        raise PaymentError("The adjustment would make the balance negative.") from exc
    return w

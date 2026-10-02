"""Automatic BEP-20 payouts for approved withdrawals (optional; ``AUTO_PAYOUT_ENABLED=true``).

Safety rules (money leaves the system here):
* only withdrawals an admin already **approved** are paid;
* the row is moved to ``processing`` and *committed before* anything is broadcast; if the process dies
  between broadcast and saving the tx hash the row stays ``processing`` without hash and is escalated
  as ``failed`` - it is never re-sent automatically;
* a failure that provably happened before broadcast (gas cap, node rejection, hot wallet empty)
  returns the row to ``approved`` and alerts the admin once;
* payouts above ``AUTO_PAYOUT_MAX_AMOUNT`` are left for a manual payout.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import select

from app import texts
from app.chain.bsc import AsyncBsc, PreBroadcastError
from app.config import get_settings
from app.db import session_scope
from app.enums import DepositMethod, WithdrawalStatus
from app.models import Withdrawal
from app.money import to_raw
from app.services import outbox, payments
from app.timeutil import utcnow

log = logging.getLogger(__name__)

AMBIGUOUS_AFTER_SECONDS = 120  # processing without a tx hash this long => crashed mid-send
_retry_after: dict[int, float] = {}  # withdrawal id -> monotonic time before which we do not retry
RETRY_COOLDOWN_SECONDS = 60


@dataclass
class PayoutResult:
    sent: list[int] = field(default_factory=list)
    completed: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)


def hot_wallet_key() -> str:
    return get_settings().payout_private_key.get_secret_value().strip()


def auto_payout_ready() -> bool:
    settings = get_settings()
    return bool(settings.auto_payout_enabled and hot_wallet_key())


async def _confirm_processing(chain: AsyncBsc, result: PayoutResult) -> None:
    settings = get_settings()
    async with session_scope() as db:
        rows = list(
            (
                await db.execute(
                    select(Withdrawal.id, Withdrawal.tx_hash, Withdrawal.processed_at).where(
                        Withdrawal.status == WithdrawalStatus.PROCESSING.value,
                        Withdrawal.method == DepositMethod.BEP20.value,
                    )
                )
            ).all()
        )
    if not rows:
        return
    latest = await chain.latest_block()
    now = utcnow()
    for wid, tx_hash, started in rows:
        if not tx_hash:
            if started and (now - started).total_seconds() > AMBIGUOUS_AFTER_SECONDS:
                async with session_scope() as db:
                    await payments.mark_withdrawal_failed(
                        db, wid, "Payout was interrupted before the transaction hash was saved - check the payout wallet on BscScan before retrying."
                    )
                result.failed.append(wid)
            continue
        receipt = await chain.receipt(tx_hash)
        if receipt is None:
            continue
        if receipt.status != 1:
            async with session_scope() as db:
                await payments.mark_withdrawal_failed(db, wid, f"Transaction {tx_hash} reverted on-chain.")
            result.failed.append(wid)
        elif latest - receipt.block_number + 1 >= settings.deposit_confirmations:
            async with session_scope() as db:
                await payments.complete_withdrawal(db, wid, "system", tx_hash)
            result.completed.append(wid)


async def _send_approved(chain: AsyncBsc, result: PayoutResult) -> None:
    settings = get_settings()
    async with session_scope() as db:
        approved = list(
            (
                await db.execute(
                    select(Withdrawal.id, Withdrawal.address, Withdrawal.amount, Withdrawal.fee)
                    .where(Withdrawal.status == WithdrawalStatus.APPROVED.value, Withdrawal.method == DepositMethod.BEP20.value)
                    .order_by(Withdrawal.id)
                    .limit(20)
                )
            ).all()
        )
    for wid, address, amount, fee in approved:
        net = amount - fee
        if net > settings.auto_payout_max_amount:
            result.skipped.append(wid)  # too large for automatic payout: admin pays manually
            continue
        if _retry_after.get(wid, 0) > time.monotonic():
            continue
        async with session_scope() as db:
            claimed = await payments.claim_for_payout(db, wid)
        if claimed is None:
            continue
        try:
            tx_hash = await chain.send_token(hot_wallet_key(), address, to_raw(net, chain.decimals))
        except PreBroadcastError as exc:
            log.warning("payout %s not broadcast: %s", wid, exc)
            _retry_after[wid] = time.monotonic() + RETRY_COOLDOWN_SECONDS
            async with session_scope() as db:
                await payments.release_claim(db, wid, f"auto payout postponed: {exc}")
                await outbox.notify_admins(
                    db, texts.admin_withdrawal_failed(wid, f"Automatic payout postponed: {exc}"), dedupe_key=f"payout-prebroadcast:{wid}"
                )
            continue
        except Exception as exc:  # ambiguous: maybe broadcast, maybe not
            log.exception("payout %s ambiguous failure", wid)
            async with session_scope() as db:
                await payments.mark_withdrawal_failed(
                    db, wid, f"Sending failed with an unclear outcome ({type(exc).__name__}). Check the payout wallet on BscScan before retrying."
                )
            result.failed.append(wid)
            continue
        async with session_scope() as db:
            wd = await payments.lock_withdrawal(db, wid)
            wd.tx_hash = tx_hash
        result.sent.append(wid)
        log.info("payout %s broadcast %s", wid, tx_hash)


async def run_payouts(chain: AsyncBsc) -> PayoutResult:
    """One scheduler tick: confirm what is in flight, then send newly approved withdrawals."""
    result = PayoutResult()
    await _confirm_processing(chain, result)
    if auto_payout_ready():
        await _send_approved(chain, result)
    return result


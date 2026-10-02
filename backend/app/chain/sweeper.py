"""Sweep deposit addresses into the treasury (cold) address.

Needs the HD *mnemonic* (to sign for each deposit address) and a small BNB-funded "gas" wallet that
tops up each deposit address just enough to pay for its token transfer. Intended to be run
occasionally from a trusted machine::

    python -m app.cli sweep --treasury 0xYourColdWallet --min 5 --dry-run
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from eth_account import Account
from sqlalchemy import select

from app.chain.bsc import AsyncBsc, ChainError, to_checksum
from app.chain.hd import HDWallet
from app.db import session_scope
from app.models import DepositAddress
from app.money import from_raw, to_raw

log = logging.getLogger(__name__)

TOKEN_TRANSFER_GAS = 100_000  # generous limit for one BEP-20 transfer (real usage ~35-60k)


@dataclass
class SweepAction:
    user_id: int
    address: str
    amount: Decimal
    status: str  # "planned" | "swept" | "skipped" | "failed"
    tx_hash: str | None = None
    gas_tx_hash: str | None = None
    detail: str = ""


async def sweep(
    chain: AsyncBsc,
    hd: HDWallet,
    treasury: str,
    gas_private_key: str,
    *,
    min_amount: Decimal = Decimal("1"),
    dry_run: bool = True,
) -> list[SweepAction]:
    if not hd.can_sign:
        raise ChainError("sweeping needs the HD mnemonic (HD_MNEMONIC); this wallet is watch-only")
    treasury = to_checksum(treasury)
    if int(treasury, 16) == 0:
        raise ChainError("refusing to sweep to the zero address")
    gas_address = Account.from_key(gas_private_key).address
    min_raw = to_raw(min_amount, chain.decimals)

    async with session_scope() as db:
        rows = list((await db.execute(select(DepositAddress).order_by(DepositAddress.user_id))).scalars())

    actions: list[SweepAction] = []
    for row in rows:
        balance = await chain.token_balance(row.address)
        if balance < min_raw or balance == 0:
            continue
        amount = from_raw(balance, chain.decimals)
        action = SweepAction(row.user_id, row.address, amount, "planned")
        actions.append(action)
        if dry_run:
            continue
        try:
            key = hd.private_key(row.derivation_index)
            if Account.from_key(key).address != row.address:
                raise ChainError("derived key does not match the stored address")  # wrong mnemonic
            # make sure the deposit address can pay for its own transfer
            cost = TOKEN_TRANSFER_GAS * await chain.gas_price()
            native = await chain.native_balance(row.address)
            if native < cost:
                gas_tx = await chain.send_native(gas_private_key, row.address, cost - native)
                receipt = await chain.wait_for_receipt(gas_tx)
                if receipt.status != 1:
                    raise ChainError("gas top-up transaction failed")
                action.gas_tx_hash = gas_tx
            tx_hash = await chain.send_token(key, treasury, balance, gas_limit=TOKEN_TRANSFER_GAS)
            receipt = await chain.wait_for_receipt(tx_hash)
            if receipt.status != 1:
                raise ChainError(f"sweep transaction {tx_hash} reverted")
            action.tx_hash = tx_hash
            action.status = "swept"
        except Exception as exc:
            log.exception("sweep of %s failed", row.address)
            action.status = "failed"
            action.detail = str(exc)[:300]
    log.info("sweep done (gas wallet %s): %s", gas_address, [(a.address, a.status) for a in actions])
    return actions

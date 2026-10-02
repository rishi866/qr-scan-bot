"""Deposit watcher: credits sellers when USDT arrives on their personal BSC address.

Works from a persisted block cursor and only looks at blocks that already have
``DEPOSIT_CONFIRMATIONS`` confirmations, so reorganisations cannot cause false credits. Crediting is
idempotent (unique ``(method, tx_hash, log_index)``), therefore restarts and re-scans are safe.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select

from app.chain.bsc import AsyncBsc
from app.config import get_settings
from app.db import session_scope
from app.models import DepositAddress, KVState
from app.money import from_raw, q_down
from app.services import payments

log = logging.getLogger(__name__)

CURSOR_KEY = "bsc_scan_cursor"


@dataclass
class ScanResult:
    from_block: int | None = None
    to_block: int | None = None
    credited: int = 0
    duplicates: int = 0
    dust: int = 0
    note: str = ""


async def get_cursor(db) -> int | None:
    row = (await db.execute(select(KVState).where(KVState.key == CURSOR_KEY))).scalar_one_or_none()
    return int(row.value) if row else None


async def set_cursor(db, value: int) -> None:
    row = (await db.execute(select(KVState).where(KVState.key == CURSOR_KEY).with_for_update())).scalar_one_or_none()
    if row is None:
        db.add(KVState(key=CURSOR_KEY, value=str(value)))
    else:
        row.value = str(value)


async def scan_deposits(chain: AsyncBsc, *, max_chunks: int = 20) -> ScanResult:
    settings = get_settings()
    result = ScanResult()
    latest = await chain.latest_block()
    confirmed = latest - settings.deposit_confirmations
    if confirmed < 0:
        result.note = "chain too short"
        return result

    async with session_scope() as db:
        cursor = await get_cursor(db)
        addresses = {
            a.address.lower(): a.user_id for a in (await db.execute(select(DepositAddress))).scalars()
        }
        if cursor is None:
            # first run: nothing can have been deposited before the first address existed
            start = settings.bsc_scan_start_block
            cursor = (start - 1) if start is not None else confirmed
            await set_cursor(db, cursor)
    if confirmed <= cursor:
        result.note = "up to date"
        return result

    address_list = [a for a in addresses]  # lower-case; checksum conversion happens in the client filter
    chunk = max(1, settings.bsc_scan_chunk)
    start = cursor + 1
    result.from_block = start
    chunks = 0
    while start <= confirmed and chunks < max_chunks:
        end = min(start + chunk - 1, confirmed)
        logs = await chain.get_transfers(start, end, address_list) if address_list else []
        async with session_scope() as db:
            for log_entry in logs:
                user_id = addresses.get(log_entry.to_address.lower())
                if user_id is None:
                    continue
                amount = q_down(from_raw(log_entry.value_raw, chain.decimals))
                if amount <= 0:
                    result.dust += 1
                    continue
                deposit = await payments.credit_onchain_deposit(
                    db,
                    user_id=user_id,
                    amount=amount,
                    tx_hash=log_entry.tx_hash,
                    log_index=log_entry.log_index,
                    address=log_entry.to_address,
                    block_number=log_entry.block_number,
                )
                if deposit is None:
                    result.duplicates += 1
                else:
                    result.credited += 1
                    log.info("deposit %s %s -> user %s (%s)", amount, log_entry.tx_hash, user_id, deposit.status)
            await set_cursor(db, end)
        result.to_block = end
        start = end + 1
        chunks += 1
    return result

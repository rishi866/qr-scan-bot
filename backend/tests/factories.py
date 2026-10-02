"""Helpers that build realistic data for tests."""

from __future__ import annotations

import itertools
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import DepositMethod, DepositStatus, LedgerType
from app.models import Deposit, ScannerSlot, User
from app.services import wallet

_tg = itertools.count(1000)
_tx = itertools.count(1)


async def make_user(
    db: AsyncSession,
    *,
    role: str = "seller",
    status: str = "approved",
    tz: str | None = "Asia/Kolkata",
    country: str | None = "IN",
    alias: str | None = None,
    name: str | None = None,
    reputation: int = 50,
    telegram_id: int | None = None,
) -> User:
    tg = telegram_id or next(_tg)
    user = User(
        telegram_id=tg,
        username=f"u{tg}",
        name=name or f"Person {tg}",
        role=role,
        status=status,
        timezone=tz,
        country=country,
        alias=alias,
        reputation=reputation,
    )
    db.add(user)
    await db.flush()
    await wallet.ensure_wallets(db, [user.user_id])
    return user


async def make_scanner(
    db: AsyncSession,
    *,
    alias: str | None = "user1",
    tz: str = "Asia/Kolkata",
    country: str = "IN",
    slots: list[tuple[int, int]] | None = None,
    status: str = "approved",
    reputation: int = 50,
    **kw,
) -> User:
    user = await make_user(db, role="scanner", status=status, tz=tz, country=country, alias=alias, reputation=reputation, **kw)
    for start, end in slots if slots is not None else [(0, 2), (2, 4), (4, 6), (6, 8), (8, 10), (10, 12), (12, 14), (14, 16), (16, 18), (18, 20), (20, 22), (22, 0)]:
        db.add(ScannerSlot(user_id=user.user_id, name=alias, slot_start=start, slot_end=end, timezone=tz, reputation=reputation))
    await db.flush()
    return user


async def fund(db: AsyncSession, user: User, amount: str | Decimal) -> None:
    """Credit a wallet the way a real deposit would (Deposit row + ledger entry)."""
    amount = Decimal(str(amount))
    wallets = await wallet.lock_wallets(db, user.user_id)
    db.add(
        Deposit(
            user_id=user.user_id,
            amount=amount,
            method=DepositMethod.BEP20.value,
            tx_hash=f"0xtest{next(_tx):060d}",
            log_index=0,
            status=DepositStatus.CREDITED.value,
        )
    )
    await wallet.post(db, wallets[user.user_id], LedgerType.DEPOSIT, balance_delta=amount, deposited=amount, note="test funding")
    await db.flush()

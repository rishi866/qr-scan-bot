"""Helpers for tests that talk to the admin API."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import httpx

from app.api import security
from app.db import session_scope
from app.models import Admin, TaskSession, TransactionHistory, User

PASSWORD = "Correct-Horse-42"
CSRF = {"X-Requested-With": "qr-admin"}


async def make_admin(username: str = "boss", password: str = PASSWORD) -> int:
    async with session_scope() as db:
        admin = Admin(username=username, password_hash=security.hash_password(password))
        db.add(admin)
        await db.flush()
        return admin.id


async def login(client: httpx.AsyncClient, username: str = "boss", password: str = PASSWORD) -> httpx.Response:
    return await client.post("/api/auth/login", json={"username": username, "password": password})


async def make_tx(
    db,
    seller: User,
    scanner: User,
    *,
    status: str = "completed",
    amount: str = "0.5",
    commission: str = "0.0005",
    confirmed_at: dt.datetime | None = None,
    url: str = "https://chatgpt.com/checkout/x",
    session_status: str | None = None,
) -> TransactionHistory:
    """Insert a finished task + its transaction record directly (for filter / report tests)."""
    confirmed_at = confirmed_at or dt.datetime.now(dt.UTC)
    session = TaskSession(
        seller_id=seller.user_id,
        scanner_id=scanner.user_id,
        url=url,
        status=session_status or {"completed": "confirmed", "refunded": "refunded", "disputed": "disputed"}[status],
        amount=Decimal(amount),
        commission=Decimal(commission),
        slot_label="08-10 Asia/Kolkata",
        sent_at=confirmed_at,
    )
    db.add(session)
    await db.flush()
    tx = TransactionHistory(
        session_id=session.session_id,
        seller_id=seller.user_id,
        scanner_id=scanner.user_id,
        scanner_name=scanner.alias,
        seller_name=seller.name,
        seller_country=seller.country,
        seller_timezone=seller.timezone,
        scanner_country=scanner.country,
        scanner_timezone=scanner.timezone,
        slot="08-10 Asia/Kolkata",
        url=url,
        amount=Decimal(amount),
        commission=Decimal(commission),
        status=status,
        confirmed_at=confirmed_at if status != "disputed" else None,
    )
    db.add(tx)
    await db.flush()
    return tx

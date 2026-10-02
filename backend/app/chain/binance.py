"""Binance integration (read-only API key, optional).

Sellers who deposit through Binance submit a *claim* (amount + the Order ID Binance shows them).
When an API key is configured this module looks the order up in the admin's Binance account and
credits the claim automatically if it matches; otherwise (or on any doubt) the claim stays
``pending`` for the admin to confirm in the panel.

Create the API key with **Enable Reading only** (never trading / withdrawals) and whitelist the
VPS IP. NOTE: the response shapes below follow Binance's public docs; they could not be exercised
against a live account during development, so every unexpected shape simply falls back to the manual
admin confirmation instead of crediting anything.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.enums import DepositMethod, DepositStatus
from app.models import Deposit, Wallet
from app.services import payments
from app.timeutil import utcnow

log = logging.getLogger(__name__)

CLAIM_MAX_AGE = dt.timedelta(hours=72)


class BinanceError(Exception):
    pass


@dataclass(frozen=True)
class IncomingPayment:
    reference: str
    amount: Decimal
    currency: str
    payer_id: str | None
    source: str  # "pay" or "deposit"


def _dec(value: Any) -> Decimal | None:
    try:
        d = Decimal(str(value))
        return d if d.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


class BinanceClient:
    def __init__(self, api_key: str, api_secret: str, base_url: str = "https://api.binance.com", client: httpx.AsyncClient | None = None):
        self._key = api_key
        self._secret = api_secret.encode()
        self._base = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=15)

    @classmethod
    def from_settings(cls) -> BinanceClient | None:
        s = get_settings()
        key, secret = s.binance_api_key.get_secret_value(), s.binance_api_secret.get_secret_value()
        if not key or not secret:
            return None
        return cls(key, secret, s.binance_base_url)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _signed_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query_params = {k: v for k, v in (params or {}).items() if v is not None}
        query_params["timestamp"] = int(time.time() * 1000)
        query_params["recvWindow"] = 10000
        query = urlencode(query_params)
        signature = hmac.new(self._secret, query.encode(), hashlib.sha256).hexdigest()
        response = await self._client.get(f"{self._base}{path}?{query}&signature={signature}", headers={"X-MBX-APIKEY": self._key})
        if response.status_code != 200:
            raise BinanceError(f"{path} -> HTTP {response.status_code}: {response.text[:200]}")
        return response.json()

    async def deposit_history(self, coin: str = "USDT", start: dt.datetime | None = None) -> list[dict[str, Any]]:
        data = await self._signed_get(
            "/sapi/v1/capital/deposit/hisrec",
            {"coin": coin, "status": 1, "startTime": int(start.timestamp() * 1000) if start else None, "limit": 1000},
        )
        return data if isinstance(data, list) else []

    async def pay_history(self, start: dt.datetime | None = None) -> list[dict[str, Any]]:
        data = await self._signed_get(
            "/sapi/v1/pay/transactions", {"startTimestamp": int(start.timestamp() * 1000) if start else None, "limit": 100}
        )
        items = data.get("data") if isinstance(data, dict) else None
        return items if isinstance(items, list) else []

    async def find_payment(self, reference: str, since: dt.datetime | None = None) -> IncomingPayment | None:
        """Look ``reference`` (Binance Pay order id / deposit tx id) up in the account's history."""
        ref = reference.strip().lower()
        for item in await self.pay_history(since):
            candidates = {str(item.get(k, "")).lower() for k in ("transactionId", "orderId", "prepayId")}
            if ref not in candidates:
                continue
            amount, currency = _dec(item.get("amount")), str(item.get("currency", "")).upper()
            if amount is None or amount <= 0:
                continue  # outgoing or malformed
            payer = (item.get("payerInfo") or {}).get("binanceId") or (item.get("payerInfo") or {}).get("accountId")
            return IncomingPayment(reference, amount, currency, str(payer) if payer else None, "pay")
        for item in await self.deposit_history("USDT", since):
            tx_id = str(item.get("txId", "")).lower()
            if tx_id and (tx_id == ref or tx_id.endswith(ref) or ref in tx_id):
                amount = _dec(item.get("amount"))
                if amount is None or amount <= 0:
                    continue
                return IncomingPayment(reference, amount, str(item.get("coin", "USDT")).upper(), None, "deposit")
        return None


async def poll_binance_claims(client: BinanceClient) -> dict[str, int]:
    """Try to auto-verify pending Binance claims. Anything doubtful stays pending for the admin."""
    stats = {"checked": 0, "credited": 0, "flagged": 0}
    cutoff = utcnow() - CLAIM_MAX_AGE
    async with session_scope() as db:
        claims = list(
            (
                await db.execute(
                    select(Deposit.id, Deposit.user_id, Deposit.amount, Deposit.tx_hash, Deposit.created_at).where(
                        Deposit.method == DepositMethod.BINANCE.value,
                        Deposit.status == DepositStatus.PENDING.value,
                        Deposit.created_at >= cutoff,
                    )
                )
            ).all()
        )
    for dep_id, user_id, amount, reference, created in claims:
        stats["checked"] += 1
        try:
            found = await client.find_payment(reference, created - dt.timedelta(hours=1))
        except (BinanceError, httpx.HTTPError) as exc:
            log.warning("binance lookup failed for claim %s: %s", dep_id, exc)
            continue
        if found is None:
            continue
        async with session_scope() as db:
            registered = (await db.execute(select(Wallet.binance_address).where(Wallet.user_id == user_id))).scalar_one_or_none()
            problems = []
            if found.currency != "USDT":
                problems.append(f"currency {found.currency}")
            if found.amount != amount:
                problems.append(f"amount {found.amount} != claimed {amount}")
            if found.payer_id and registered and found.payer_id != registered:
                problems.append("payer differs from the user's registered Binance ID")
            if problems:
                dep = await payments._lock_deposit(db, dep_id)
                dep.note = ("needs review: " + "; ".join(problems))[:255]
                stats["flagged"] += 1
                continue
            await payments.confirm_deposit(db, dep_id, "binance-api", note=f"verified via Binance {found.source} API")
            stats["credited"] += 1
    return stats

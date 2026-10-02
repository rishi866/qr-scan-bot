"""Wallet management: balances, ledger, manual adjustments, deposits, withdrawals and payout status."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from app import timeutil
from app.api.common import PageParams, iso, money, page_response, paginate
from app.api.deps import CurrentAdmin, Db, actor_label, audit
from app.chain import hd as hd_mod
from app.chain.bsc import async_client_from_settings
from app.chain.payout import auto_payout_ready, hot_wallet_key
from app.chain.watcher import get_cursor
from app.config import get_settings
from app.enums import DepositMethod, DepositStatus, Role, WithdrawalStatus
from app.models import Deposit, KVState, LedgerEntry, User, Wallet, Withdrawal
from app.money import from_raw, to_decimal
from app.services import payments, wallet

router = APIRouter(prefix="/api", tags=["wallets"])
Pages = Annotated[PageParams, Depends()]


def _who(u: User) -> dict:
    return {"user_id": u.user_id, "name": u.name, "alias": u.alias, "role": u.role, "telegram_id": u.telegram_id, "country": u.country, "username": u.username}


def wallet_json(w: Wallet, u: User) -> dict:
    return {
        **_who(u),
        "balance": money(w.balance),
        "pending": money(w.pending),
        "total_earned": money(w.total_earned),
        "total_spent": money(w.total_spent),
        "total_deposited": money(w.total_deposited),
        "total_withdrawn": money(w.total_withdrawn),
        "bep20_address": w.bep20_address,
        "binance_address": w.binance_address,
        "updated_at": iso(w.updated_at),
    }


@router.get("/wallets")
async def list_wallets(_: CurrentAdmin, db: Db, params: Pages, role: str | None = None, q: str | None = Query(None, max_length=100), non_zero: bool = False):
    stmt = select(Wallet, User).join(User, User.user_id == Wallet.user_id)
    if role in {r.value for r in Role}:
        stmt = stmt.where(User.role == role)
    if non_zero:
        stmt = stmt.where(or_(Wallet.balance > 0, Wallet.pending > 0))
    if q:
        needle = f"%{q.strip()}%"
        clauses = [User.name.ilike(needle), User.username.ilike(needle), User.alias.ilike(needle)]
        if q.strip().isdigit():
            clauses += [User.user_id == int(q.strip()), User.telegram_id == int(q.strip())]
        stmt = stmt.where(or_(*clauses))
    rows, total = await paginate(db, stmt.order_by((Wallet.balance + Wallet.pending).desc(), Wallet.user_id), params)
    return page_response([wallet_json(w, u) for w, u in rows], total, params)


@router.get("/wallets/summary")
async def wallets_summary(_: CurrentAdmin, db: Db):
    per_role = (
        await db.execute(
            select(User.role, func.coalesce(func.sum(Wallet.balance), 0), func.coalesce(func.sum(Wallet.pending), 0))
            .join(User, User.user_id == Wallet.user_id)
            .group_by(User.role)
        )
    ).all()
    platform = (await db.execute(select(func.coalesce(func.sum(LedgerEntry.balance_delta), 0)).where(LedgerEntry.user_id.is_(None)))).scalar_one()
    deposits = (await db.execute(select(func.coalesce(func.sum(Deposit.amount), 0)).where(Deposit.status == DepositStatus.CREDITED.value))).scalar_one()
    payouts = (
        await db.execute(select(func.coalesce(func.sum(Withdrawal.amount - Withdrawal.fee), 0)).where(Withdrawal.status == WithdrawalStatus.COMPLETED.value))
    ).scalar_one()
    report = await wallet.reconcile(db)
    return {
        "by_role": {role: {"balance": money(b), "pending": money(p)} for role, b, p in per_role},
        "platform_commission": money(platform),
        "deposits_credited": money(deposits),
        "withdrawals_paid": money(payouts),
        "ledger": report.as_json(),
    }


@router.get("/wallets/{user_id}")
async def wallet_detail(user_id: int, _: CurrentAdmin, db: Db, params: Pages):
    row = (await db.execute(select(Wallet, User).join(User, User.user_id == Wallet.user_id).where(Wallet.user_id == user_id))).first()
    if row is None:
        raise HTTPException(404, "Wallet not found")
    entries, total = await paginate(db, select(LedgerEntry).where(LedgerEntry.user_id == user_id).order_by(LedgerEntry.id.desc()), params)
    return {
        "wallet": wallet_json(*row),
        "ledger": page_response(
            [
                {
                    "id": e.id, "type": e.entry_type, "balance_delta": money(e.balance_delta), "pending_delta": money(e.pending_delta),
                    "balance_after": money(e.balance_after), "pending_after": money(e.pending_after),
                    "ref_type": e.ref_type, "ref_id": e.ref_id, "note": e.note, "created_at": iso(e.created_at),
                }
                for (e,) in entries
            ],
            total,
            params,
        ),
    }


class AdjustBody(BaseModel):
    amount: str = Field(min_length=1, max_length=40)  # signed decimal string, e.g. "5" or "-2.5"
    reason: str = Field(min_length=3, max_length=200)


@router.post("/wallets/{user_id}/adjust")
async def adjust_wallet(user_id: int, body: AdjustBody, request: Request, admin: CurrentAdmin, db: Db):
    try:
        delta = to_decimal(body.amount)
        w = await payments.adjust_wallet(db, user_id, delta, body.reason, actor_label(admin))
    except ValueError as exc:
        raise HTTPException(422, "amount must be a number") from exc
    except payments.PaymentError as exc:
        raise HTTPException(409, str(exc)) from exc
    await audit(db, admin, request, "wallet_adjusted", "user", user_id, {"amount": str(delta), "reason": body.reason})
    return {"balance": money(w.balance), "pending": money(w.pending)}


# ── deposits ────────────────────────────────────────────────────────────────


def deposit_json(d: Deposit, u: User) -> dict:
    return {
        "id": d.id, **_who(u), "amount": money(d.amount), "method": d.method, "tx_hash": d.tx_hash, "address": d.address,
        "status": d.status, "note": d.note, "block_number": d.block_number, "created_at": iso(d.created_at), "credited_at": iso(d.credited_at),
    }


@router.get("/deposits")
async def list_deposits(
    _: CurrentAdmin, db: Db, params: Pages, status: str | None = None, method: str | None = None, q: str | None = Query(None, max_length=100), needs_review: bool = False
):
    stmt = select(Deposit, User).join(User, User.user_id == Deposit.user_id)
    if needs_review:
        stmt = stmt.where(Deposit.status.in_([DepositStatus.PENDING.value, DepositStatus.BELOW_MIN.value]))
    elif status in {s.value for s in DepositStatus}:
        stmt = stmt.where(Deposit.status == status)
    if method in {m.value for m in DepositMethod}:
        stmt = stmt.where(Deposit.method == method)
    if q:
        needle = f"%{q.strip()}%"
        stmt = stmt.where(or_(Deposit.tx_hash.ilike(needle), User.name.ilike(needle), User.alias.ilike(needle), Deposit.address.ilike(needle)))
    rows, total = await paginate(db, stmt.order_by(Deposit.id.desc()), params)
    return page_response([deposit_json(d, u) for d, u in rows], total, params)


class ConfirmDepositBody(BaseModel):
    amount: str | None = Field(None, max_length=40)
    note: str | None = Field(None, max_length=200)


class NoteBody(BaseModel):
    note: str | None = Field(None, max_length=200)


@router.post("/deposits/{deposit_id}/confirm")
async def confirm_deposit(deposit_id: int, body: ConfirmDepositBody, request: Request, admin: CurrentAdmin, db: Db):
    try:
        amount = to_decimal(body.amount) if body.amount else None
        dep = await payments.confirm_deposit(db, deposit_id, actor_label(admin), amount=amount, note=body.note)
    except ValueError as exc:
        raise HTTPException(422, "amount must be a number") from exc
    except payments.PaymentError as exc:
        raise HTTPException(409, str(exc)) from exc
    await audit(db, admin, request, "deposit_confirmed", "deposit", deposit_id, {"amount": str(dep.amount)})
    user = await db.get(User, dep.user_id)
    return deposit_json(dep, user)


@router.post("/deposits/{deposit_id}/reject")
async def reject_deposit(deposit_id: int, body: NoteBody, request: Request, admin: CurrentAdmin, db: Db):
    try:
        dep = await payments.reject_deposit(db, deposit_id, actor_label(admin), body.note)
    except payments.PaymentError as exc:
        raise HTTPException(409, str(exc)) from exc
    await audit(db, admin, request, "deposit_rejected", "deposit", deposit_id, {"note": body.note})
    user = await db.get(User, dep.user_id)
    return deposit_json(dep, user)


# ── withdrawals ─────────────────────────────────────────────────────────────


def withdrawal_json(w: Withdrawal, u: User) -> dict:
    settings = get_settings()
    net = w.amount - w.fee
    return {
        "id": w.id, **_who(u), "amount": money(w.amount), "fee": money(w.fee), "net": money(net), "method": w.method, "address": w.address,
        "tx_hash": w.tx_hash, "status": w.status, "admin_note": w.admin_note, "processed_by": w.processed_by,
        "processed_at": iso(w.processed_at), "created_at": iso(w.created_at),
        "auto_payout": bool(
            auto_payout_ready() and w.method == DepositMethod.BEP20.value and w.status == WithdrawalStatus.APPROVED.value and net <= settings.auto_payout_max_amount
        ),
    }


@router.get("/withdrawals")
async def list_withdrawals(_: CurrentAdmin, db: Db, params: Pages, status: str | None = None, method: str | None = None, to_handle: bool = False):
    stmt = select(Withdrawal, User).join(User, User.user_id == Withdrawal.user_id)
    if to_handle:
        stmt = stmt.where(Withdrawal.status.in_([WithdrawalStatus.PENDING.value, WithdrawalStatus.APPROVED.value, WithdrawalStatus.PROCESSING.value, WithdrawalStatus.FAILED.value]))
    elif status in {s.value for s in WithdrawalStatus}:
        stmt = stmt.where(Withdrawal.status == status)
    if method in {m.value for m in DepositMethod}:
        stmt = stmt.where(Withdrawal.method == method)
    rows, total = await paginate(db, stmt.order_by(Withdrawal.id.desc()), params)
    return page_response([withdrawal_json(w, u) for w, u in rows], total, params)


class PaidBody(BaseModel):
    tx_hash: str | None = Field(None, max_length=100)


async def _withdrawal_action(request: Request, admin, db, withdrawal_id: int, action: str, fn, **details):
    try:
        wd = await fn()
    except payments.PaymentError as exc:
        raise HTTPException(409, str(exc)) from exc
    await audit(db, admin, request, action, "withdrawal", withdrawal_id, details or None)
    user = await db.get(User, wd.user_id)
    return withdrawal_json(wd, user)


@router.post("/withdrawals/{withdrawal_id}/approve")
async def approve_withdrawal(withdrawal_id: int, body: NoteBody, request: Request, admin: CurrentAdmin, db: Db):
    return await _withdrawal_action(request, admin, db, withdrawal_id, "withdrawal_approved", lambda: payments.approve_withdrawal(db, withdrawal_id, actor_label(admin), body.note))


@router.post("/withdrawals/{withdrawal_id}/reject")
async def reject_withdrawal(withdrawal_id: int, body: NoteBody, request: Request, admin: CurrentAdmin, db: Db):
    return await _withdrawal_action(
        request, admin, db, withdrawal_id, "withdrawal_rejected", lambda: payments.reject_withdrawal(db, withdrawal_id, actor_label(admin), body.note), note=body.note
    )


@router.post("/withdrawals/{withdrawal_id}/mark-paid")
async def mark_withdrawal_paid(withdrawal_id: int, body: PaidBody, request: Request, admin: CurrentAdmin, db: Db):
    """Manual payout: the admin sent the money (Binance Pay / own wallet) and records the reference."""
    return await _withdrawal_action(
        request, admin, db, withdrawal_id, "withdrawal_paid_manually",
        lambda: payments.complete_withdrawal(db, withdrawal_id, actor_label(admin), (body.tx_hash or "").strip() or None), tx_hash=body.tx_hash,
    )


@router.post("/withdrawals/{withdrawal_id}/retry")
async def retry_withdrawal(withdrawal_id: int, request: Request, admin: CurrentAdmin, db: Db):
    return await _withdrawal_action(request, admin, db, withdrawal_id, "withdrawal_retried", lambda: payments.retry_withdrawal(db, withdrawal_id, actor_label(admin)))


# ── payout / chain status ───────────────────────────────────────────────────


@router.get("/chain/status")
async def chain_status(_: CurrentAdmin, db: Db):
    settings = get_settings()
    result: dict = {
        "token": {"symbol": settings.token_symbol, "contract": settings.token_contract, "decimals": settings.token_decimals},
        "confirmations": settings.deposit_confirmations,
        "auto_payout": {"enabled": auto_payout_ready(), "max_amount": money(settings.auto_payout_max_amount)},
        "binance_configured": bool(settings.binance_api_key.get_secret_value() and settings.binance_api_secret.get_secret_value()),
        "ai_configured": bool(settings.anthropic_api_key.get_secret_value()),
        "deposits": {"configured": False},
        "hot_wallet": {"configured": False},
        "rpc": {"ok": False},
    }
    try:
        wallet_cfg = hd_mod.from_settings()
    except hd_mod.HDError as exc:
        result["deposits"] = {"configured": False, "error": str(exc)}
        wallet_cfg = None
    if wallet_cfg is not None:
        result["deposits"] = {"configured": True, "mode": "mnemonic" if wallet_cfg.can_sign else "watch-only", "xpub": wallet_cfg.xpub[:12] + "…"}
    result["last_scanned_block"] = await get_cursor(db)
    result["worker_heartbeat"] = None
    beat = await db.get(KVState, "worker_heartbeat")
    result["worker_heartbeat"] = beat.value if beat else None

    async def probe() -> None:
        chain = async_client_from_settings()
        chain_id = await chain.check_chain()
        latest = await chain.latest_block()
        result["rpc"] = {"ok": True, "chain_id": chain_id, "latest_block": latest}
        key = hot_wallet_key()
        if key:
            from eth_account import Account

            address = Account.from_key(key).address
            native = await chain.native_balance(address)
            token = await chain.token_balance(address)
            result["hot_wallet"] = {
                "configured": True,
                "address": address,
                "bnb": money(from_raw(native, 18)),
                "token": money(from_raw(token, settings.token_decimals)),
            }

    try:
        await asyncio.wait_for(probe(), timeout=8)
    except Exception as exc:
        result["rpc"] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    result["generated_at"] = timeutil.utcnow().isoformat()
    return result

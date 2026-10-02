"""Reports: daily / monthly, country-wise, scanner-wise, seller-wise and commission (JSON or CSV)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from sqlalchemy import func, select

from app import timeutil
from app.api.common import day_range, money, to_csv
from app.api.deps import CurrentAdmin, Db
from app.enums import LedgerType, TxStatus
from app.models import LedgerEntry, TransactionHistory

router = APIRouter(prefix="/api/reports", tags=["reports"])

T = TransactionHistory
MOMENT = func.coalesce(T.confirmed_at, T.created_at)


def _range(date_from: str | None, date_to: str | None):
    start, end = day_range(date_from, date_to)
    if start is None and end is None:  # default: the last 30 days
        end = (timeutil.utcnow() + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        start = end - dt.timedelta(days=30)
    return start, end


def _where(stmt, start, end, column=MOMENT):
    if start:
        stmt = stmt.where(column >= start)
    if end:
        stmt = stmt.where(column < end)
    return stmt


def _respond(rows: list[dict], header: list[str], fmt: str, name: str, extra: dict | None = None):
    if fmt == "csv":
        body = to_csv(header, ([r[h] for h in header] for r in rows))
        return Response(body, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})
    return {"items": rows, **(extra or {})}


def _period(group: str, column):
    if group not in ("day", "month"):
        raise HTTPException(422, "group must be 'day' or 'month'")
    return func.date_trunc(group, func.timezone("UTC", column))


def _label(period: dt.datetime, group: str) -> str:
    return period.strftime("%Y-%m" if group == "month" else "%Y-%m-%d")


@router.get("/summary")
async def summary(
    _: CurrentAdmin,
    db: Db,
    group: Literal["day", "month"] = "day",
    date_from: str | None = None,
    date_to: str | None = None,
    format: Literal["json", "csv"] = "json",
):
    start, end = _range(date_from, date_to)
    period = _period(group, MOMENT)
    stmt = _where(
        select(
            period.label("period"),
            func.count().filter(T.status == TxStatus.COMPLETED.value),
            func.coalesce(func.sum(T.amount).filter(T.status == TxStatus.COMPLETED.value), 0),
            func.coalesce(func.sum(T.commission).filter(T.status == TxStatus.COMPLETED.value), 0),
            func.count().filter(T.status == TxStatus.REFUNDED.value),
            func.count().filter(T.status == TxStatus.DISPUTED.value),
        ).group_by(period).order_by(period),
        start, end,
    )
    rows = [
        {"period": _label(p, group), "completed": c, "volume": money(v), "commission": money(m), "refunded": r, "disputed": d}
        for p, c, v, m, r, d in (await db.execute(stmt)).all()
    ]
    totals = {
        "completed": sum(r["completed"] for r in rows),
        "volume": money(sum((Decimal(r["volume"]) for r in rows), Decimal(0))),
        "commission": money(sum((Decimal(r["commission"]) for r in rows), Decimal(0))),
    }
    return _respond(rows, ["period", "completed", "volume", "commission", "refunded", "disputed"], format, f"summary-{group}", {"totals": totals})


@router.get("/by-country")
async def by_country(
    _: CurrentAdmin,
    db: Db,
    side: Literal["scanner", "seller"] = "scanner",
    date_from: str | None = None,
    date_to: str | None = None,
    format: Literal["json", "csv"] = "json",
):
    start, end = _range(date_from, date_to)
    column = T.scanner_country if side == "scanner" else T.seller_country
    stmt = _where(
        select(column, func.count(), func.coalesce(func.sum(T.amount), 0), func.coalesce(func.sum(T.commission), 0))
        .where(T.status == TxStatus.COMPLETED.value)
        .group_by(column)
        .order_by(func.count().desc()),
        start, end,
    )
    rows = [
        {"country": code or "?", "country_name": timeutil.country_name(code) if code else "Unknown", "tasks": n, "volume": money(v), "commission": money(m)}
        for code, n, v, m in (await db.execute(stmt)).all()
    ]
    return _respond(rows, ["country", "country_name", "tasks", "volume", "commission"], format, f"countries-{side}", {"side": side})


@router.get("/by-scanner")
async def by_scanner(_: CurrentAdmin, db: Db, date_from: str | None = None, date_to: str | None = None, format: Literal["json", "csv"] = "json"):
    start, end = _range(date_from, date_to)
    stmt = _where(
        select(
            T.scanner_id,
            func.max(T.scanner_name),
            func.max(T.scanner_country),
            func.count().filter(T.status == TxStatus.COMPLETED.value),
            func.coalesce(func.sum(T.amount).filter(T.status == TxStatus.COMPLETED.value), 0),
            func.count().filter(T.status == TxStatus.REFUNDED.value),
            func.count().filter(T.status == TxStatus.DISPUTED.value),
        ).group_by(T.scanner_id).order_by(func.count().filter(T.status == TxStatus.COMPLETED.value).desc()),
        start, end,
    )
    rows = [
        {"scanner_id": sid, "alias": alias or "-", "country": country or "?", "tasks": n, "earned": money(v), "refunded": r, "disputed": d}
        for sid, alias, country, n, v, r, d in (await db.execute(stmt)).all()
    ]
    return _respond(rows, ["scanner_id", "alias", "country", "tasks", "earned", "refunded", "disputed"], format, "by-scanner")


@router.get("/by-seller")
async def by_seller(_: CurrentAdmin, db: Db, date_from: str | None = None, date_to: str | None = None, format: Literal["json", "csv"] = "json"):
    start, end = _range(date_from, date_to)
    stmt = _where(
        select(
            T.seller_id,
            func.max(T.seller_name),
            func.max(T.seller_country),
            func.count().filter(T.status == TxStatus.COMPLETED.value),
            func.coalesce(func.sum(T.amount + T.commission).filter(T.status == TxStatus.COMPLETED.value), 0),
            func.count().filter(T.status == TxStatus.REFUNDED.value),
            func.count().filter(T.status == TxStatus.DISPUTED.value),
        ).group_by(T.seller_id).order_by(func.count().filter(T.status == TxStatus.COMPLETED.value).desc()),
        start, end,
    )
    rows = [
        {"seller_id": sid, "name": name or "-", "country": country or "?", "tasks": n, "spent": money(v), "refunded": r, "disputed": d}
        for sid, name, country, n, v, r, d in (await db.execute(stmt)).all()
    ]
    return _respond(rows, ["seller_id", "name", "country", "tasks", "spent", "refunded", "disputed"], format, "by-seller")


@router.get("/commission")
async def commission(
    _: CurrentAdmin,
    db: Db,
    group: Literal["day", "month"] = "day",
    date_from: str | None = None,
    date_to: str | None = None,
    format: Literal["json", "csv"] = "json",
):
    """Platform income from the ledger: task commissions plus withdrawal fees."""
    start, end = _range(date_from, date_to)
    period = _period(group, LedgerEntry.created_at)
    stmt = _where(
        select(
            period.label("period"),
            func.coalesce(func.sum(LedgerEntry.balance_delta).filter(LedgerEntry.entry_type == LedgerType.COMMISSION.value), 0),
            func.coalesce(func.sum(LedgerEntry.balance_delta).filter(LedgerEntry.entry_type == LedgerType.WITHDRAWAL_FEE.value), 0),
            func.coalesce(func.sum(LedgerEntry.balance_delta), 0),
        )
        .where(LedgerEntry.user_id.is_(None))
        .group_by(period)
        .order_by(period),
        start, end, LedgerEntry.created_at,
    )
    rows, running = [], Decimal(0)
    for p, task_commission, fees, total in (await db.execute(stmt)).all():
        running += Decimal(total)
        rows.append({"period": _label(p, group), "task_commission": money(task_commission), "withdrawal_fees": money(fees), "total": money(total), "cumulative": money(running)})
    all_time = (await db.execute(select(func.coalesce(func.sum(LedgerEntry.balance_delta), 0)).where(LedgerEntry.user_id.is_(None)))).scalar_one()
    return _respond(rows, ["period", "task_commission", "withdrawal_fees", "total", "cumulative"], format, f"commission-{group}", {"all_time_total": money(all_time)})


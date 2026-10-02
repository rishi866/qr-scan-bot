"""Shared helpers for the admin API: pagination, formatting, CSV."""

from __future__ import annotations

import csv
import datetime as dt
import io
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, Query
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession


def money(value: Decimal | int | float | str | None) -> str:
    """Exact decimal string (never scientific notation, never a float)."""
    if value is None:
        return "0"
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return format(d.normalize(), "f")  # 12.50000000 -> "12.5", 100 -> "100" (never scientific notation)


class PageParams:
    def __init__(self, page: int = Query(1, ge=1, le=100000), page_size: int = Query(25, ge=1, le=200)):
        self.page = page
        self.page_size = page_size

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


async def paginate(db: AsyncSession, stmt: Select, params: PageParams) -> tuple[Sequence[Any], int]:
    total = (await db.execute(select(func.count()).select_from(stmt.order_by(None).subquery()))).scalar_one()
    rows = (await db.execute(stmt.limit(params.page_size).offset(params.offset))).all()
    return rows, total


def page_response(items: list[Any], total: int, params: PageParams, **extra: Any) -> dict[str, Any]:
    return {"items": items, "total": total, "page": params.page, "page_size": params.page_size, **extra}


def parse_date(value: str | None, field: str) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{field} must be YYYY-MM-DD") from exc


def day_range(date_from: str | None, date_to: str | None) -> tuple[dt.datetime | None, dt.datetime | None]:
    """Inclusive UTC day range -> ``[start, end)`` datetimes."""
    start = parse_date(date_from, "date_from")
    end = parse_date(date_to, "date_to")
    return (
        dt.datetime.combine(start, dt.time(0), tzinfo=dt.UTC) if start else None,
        dt.datetime.combine(end + dt.timedelta(days=1), dt.time(0), tzinfo=dt.UTC) if end else None,
    )


def csv_cell(value: Any) -> str:
    """Neutralise spreadsheet formula injection (cells starting with = + - @ are prefixed with a quote)."""
    text = "" if value is None else str(value)
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def to_csv(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    for row in rows:
        writer.writerow([csv_cell(v) for v in row])
    return buffer.getvalue()


def iso(value: dt.datetime | None) -> str | None:
    return value.astimezone(dt.UTC).isoformat() if value else None

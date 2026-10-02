"""Transactions (with CSV export), live/all task sessions and disputes (with the proof viewer)."""

from __future__ import annotations

import mimetypes
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import aliased

from app import timeutil
from app.api.common import PageParams, day_range, iso, money, page_response, paginate, to_csv
from app.api.deps import CurrentAdmin, Db, actor_label, audit
from app.enums import DisputeResolution, DisputeStatus, SessionStatus, TxStatus
from app.models import Dispute, TaskSession, TransactionHistory, User
from app.services import disputes, scheduler

router = APIRouter(prefix="/api", tags=["transactions"])
Pages = Annotated[PageParams, Depends()]
CSV_ROW_LIMIT = 50_000


# ── transactions ────────────────────────────────────────────────────────────


def _filtered(
    stmt: Select,
    seller_id: int | None,
    scanner_id: int | None,
    status: str | None,
    date_from: str | None,
    date_to: str | None,
    q: str | None,
) -> Select:
    start, end = day_range(date_from, date_to)
    if seller_id:
        stmt = stmt.where(TransactionHistory.seller_id == seller_id)
    if scanner_id:
        stmt = stmt.where(TransactionHistory.scanner_id == scanner_id)
    if status in {s.value for s in TxStatus}:
        stmt = stmt.where(TransactionHistory.status == status)
    # a transaction belongs to the day it was decided; disputed ones (no decision yet) use their creation time
    moment = func.coalesce(TransactionHistory.confirmed_at, TransactionHistory.created_at)
    if start:
        stmt = stmt.where(moment >= start)
    if end:
        stmt = stmt.where(moment < end)
    if q:
        needle = f"%{q.strip()}%"
        clauses = [TransactionHistory.scanner_name.ilike(needle), TransactionHistory.seller_name.ilike(needle), TransactionHistory.url.ilike(needle)]
        if q.strip().isdigit():
            clauses += [TransactionHistory.session_id == int(q.strip()), TransactionHistory.id == int(q.strip())]
        stmt = stmt.where(or_(*clauses))
    return stmt


def tx_json(t: TransactionHistory) -> dict:
    return {
        "id": t.id,
        "session_id": t.session_id,
        "seller_id": t.seller_id,
        "seller_name": t.seller_name,
        "seller_country": t.seller_country,
        "seller_timezone": t.seller_timezone,
        "scanner_id": t.scanner_id,
        "scanner_name": t.scanner_name,
        "scanner_country": t.scanner_country,
        "scanner_timezone": t.scanner_timezone,
        "slot": t.slot,
        "url": t.url,
        "amount": money(t.amount),
        "commission": money(t.commission),
        "status": t.status,
        "confirmed_at": iso(t.confirmed_at),
        "created_at": iso(t.created_at),
    }


@router.get("/transactions")
async def list_transactions(
    _: CurrentAdmin,
    db: Db,
    params: Pages,
    seller_id: int | None = None,
    scanner_id: int | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = Query(None, max_length=200),
):
    base = _filtered(select(TransactionHistory), seller_id, scanner_id, status, date_from, date_to, q)
    rows, total = await paginate(db, base.order_by(TransactionHistory.id.desc()), params)
    totals_stmt = _filtered(
        select(func.count(), func.coalesce(func.sum(TransactionHistory.amount), 0), func.coalesce(func.sum(TransactionHistory.commission), 0)),
        seller_id, scanner_id, status, date_from, date_to, q,
    ).where(TransactionHistory.status == TxStatus.COMPLETED.value)
    count, amount, commission = (await db.execute(totals_stmt)).one()
    return page_response(
        [tx_json(r[0]) for r in rows], total, params,
        totals={"completed_count": count, "volume": money(amount), "commission": money(commission)},
    )


@router.get("/transactions/export.csv")
async def export_transactions(
    _: CurrentAdmin,
    db: Db,
    seller_id: int | None = None,
    scanner_id: int | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = Query(None, max_length=200),
):
    stmt = _filtered(select(TransactionHistory), seller_id, scanner_id, status, date_from, date_to, q).order_by(TransactionHistory.id).limit(CSV_ROW_LIMIT)
    rows = [r[0] for r in (await db.execute(stmt)).all()]
    header = [
        "id", "session_id", "status", "amount_usdt", "commission_usdt", "confirmed_at_utc", "slot",
        "seller_id", "seller_name", "seller_country", "seller_timezone",
        "scanner_id", "scanner_name", "scanner_country", "scanner_timezone", "url",
    ]
    body = to_csv(
        header,
        (
            [t.id, t.session_id, t.status, money(t.amount), money(t.commission), iso(t.confirmed_at), t.slot,
             t.seller_id, t.seller_name, t.seller_country, t.seller_timezone,
             t.scanner_id, t.scanner_name, t.scanner_country, t.scanner_timezone, t.url]
            for t in rows
        ),
    )
    stamp = timeutil.utcnow().strftime("%Y%m%d-%H%M%S")
    return Response(body, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="transactions-{stamp}.csv"'})


# ── sessions (monitoring) ───────────────────────────────────────────────────


@router.get("/sessions")
async def list_sessions(
    _: CurrentAdmin,
    db: Db,
    params: Pages,
    status: str | None = None,
    active: bool = False,
    seller_id: int | None = None,
    scanner_id: int | None = None,
):
    seller, scanner = aliased(User), aliased(User)
    stmt = (
        select(TaskSession, seller, scanner)
        .join(seller, seller.user_id == TaskSession.seller_id)
        .join(scanner, scanner.user_id == TaskSession.scanner_id)
    )
    if active:
        stmt = stmt.where(TaskSession.status.in_([s.value for s in (SessionStatus.AWAITING_SCANNER, SessionStatus.ACCEPTED, SessionStatus.DONE, SessionStatus.DISPUTED)]))
    elif status in {s.value for s in SessionStatus}:
        stmt = stmt.where(TaskSession.status == status)
    if seller_id:
        stmt = stmt.where(TaskSession.seller_id == seller_id)
    if scanner_id:
        stmt = stmt.where(TaskSession.scanner_id == scanner_id)
    rows, total = await paginate(db, stmt.order_by(TaskSession.session_id.desc()), params)
    items = [
        {
            "session_id": s.session_id,
            "status": s.status,
            "seller": {"user_id": sel.user_id, "name": sel.name, "telegram_id": sel.telegram_id},
            "scanner": {"user_id": sc.user_id, "alias": sc.alias, "name": sc.name, "telegram_id": sc.telegram_id},
            "url": s.url,
            "amount": money(s.amount),
            "commission": money(s.commission),
            "slot": s.slot_label,
            "sent_at": iso(s.sent_at),
            "accepted_at": iso(s.accepted_at),
            "done_at": iso(s.done_at),
            "confirmed_at": iso(s.confirmed_at),
            "expires_at": iso(s.expires_at),
            "deadline_at": iso(s.deadline_at),
            "closed_reason": s.closed_reason,
        }
        for s, sel, sc in rows
    ]
    return page_response(items, total, params)


# ── disputes ────────────────────────────────────────────────────────────────


def _dispute_row(d: Dispute, s: TaskSession, seller: User, scanner: User) -> dict:
    verdict = d.ai_verdict or {}
    return {
        "id": d.id,
        "session_id": d.session_id,
        "status": d.status,
        "resolution": d.resolution,
        "reason": d.reason,
        "admin_notes": d.admin_notes,
        "resolved_by": d.resolved_by,
        "resolved_at": iso(d.resolved_at),
        "created_at": iso(d.created_at),
        "proof_deadline_at": iso(d.proof_deadline_at),
        "has_proof": bool(d.proof_file),
        "ai_status": d.ai_status,
        "ai_verdict": verdict.get("verdict"),
        "ai_confidence": verdict.get("confidence"),
        "ai_reason": verdict.get("reason") or verdict.get("error"),
        "amount": money(s.amount),
        "commission": money(s.commission),
        "session_status": s.status,
        "url": s.url,
        "seller": {"user_id": seller.user_id, "name": seller.name, "username": seller.username, "telegram_id": seller.telegram_id, "country": seller.country},
        "scanner": {"user_id": scanner.user_id, "alias": scanner.alias, "name": scanner.name, "username": scanner.username, "telegram_id": scanner.telegram_id, "country": scanner.country},
        "timeline": {
            "sent_at": iso(s.sent_at),
            "accepted_at": iso(s.accepted_at),
            "done_at": iso(s.done_at),
            "prompted_at": iso(s.prompted_at),
        },
    }


@router.get("/disputes")
async def list_disputes(_: CurrentAdmin, db: Db, params: Pages, status: str | None = None):
    seller, scanner = aliased(User), aliased(User)
    stmt = (
        select(Dispute, TaskSession, seller, scanner)
        .join(TaskSession, TaskSession.session_id == Dispute.session_id)
        .join(seller, seller.user_id == TaskSession.seller_id)
        .join(scanner, scanner.user_id == TaskSession.scanner_id)
    )
    if status == "open":
        stmt = stmt.where(Dispute.status.in_([DisputeStatus.AWAITING_PROOF.value, DisputeStatus.PENDING_REVIEW.value]))
    elif status in {s.value for s in DisputeStatus}:
        stmt = stmt.where(Dispute.status == status)
    stmt = stmt.order_by((Dispute.status == DisputeStatus.PENDING_REVIEW.value).desc(), Dispute.id.desc())
    rows, total = await paginate(db, stmt, params)
    return page_response([_dispute_row(d, s, sel, sc) for d, s, sel, sc in rows], total, params)


async def _load_dispute(db, dispute_id: int):
    seller, scanner = aliased(User), aliased(User)
    row = (
        await db.execute(
            select(Dispute, TaskSession, seller, scanner)
            .join(TaskSession, TaskSession.session_id == Dispute.session_id)
            .join(seller, seller.user_id == TaskSession.seller_id)
            .join(scanner, scanner.user_id == TaskSession.scanner_id)
            .where(Dispute.id == dispute_id)
        )
    ).first()
    if row is None:
        raise HTTPException(404, "Dispute not found")
    return row


@router.get("/disputes/{dispute_id}")
async def dispute_detail(dispute_id: int, _: CurrentAdmin, db: Db):
    d, s, seller, scanner = await _load_dispute(db, dispute_id)
    return _dispute_row(d, s, seller, scanner)


@router.get("/disputes/{dispute_id}/proof")
async def dispute_proof(dispute_id: int, _: CurrentAdmin, db: Db):
    """The scanner's screenshot, served only to logged-in admins (never from a public folder)."""
    dispute = await db.get(Dispute, dispute_id)
    if dispute is None or not dispute.proof_file:
        raise HTTPException(404, "No proof uploaded")
    try:
        path = scheduler.proof_path(dispute.proof_file)
    except ValueError as exc:
        raise HTTPException(404, "No proof uploaded") from exc
    if not path.is_file():
        raise HTTPException(404, "The proof file is missing from the server")
    media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media, headers={"Cache-Control": "private, no-store", "Content-Disposition": "inline", "X-Content-Type-Options": "nosniff"})


class ResolveBody(BaseModel):
    resolution: DisputeResolution
    notes: str | None = Field(None, max_length=4000)


@router.post("/disputes/{dispute_id}/resolve")
async def resolve_dispute(dispute_id: int, body: ResolveBody, request: Request, admin: CurrentAdmin, db: Db):
    """``pay_scanner`` rejects the dispute (task counts as done); ``refund_seller`` upholds it."""
    try:
        await disputes.resolve(db, dispute_id, body.resolution, by=actor_label(admin), notes=body.notes)
    except disputes.DisputeError as exc:
        raise HTTPException(409, str(exc)) from exc
    await audit(db, admin, request, "dispute_resolved", "dispute", dispute_id, {"resolution": body.resolution.value, "notes": body.notes})
    d, s, seller, scanner = await _load_dispute(db, dispute_id)
    return _dispute_row(d, s, seller, scanner)

"""User management, scanner management (names, reputation, slots) and slot overview."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import case, func, or_, select

from app import timeutil
from app.api.common import PageParams, iso, money, page_response, paginate
from app.api.deps import CurrentAdmin, Db, actor_label, audit
from app.enums import Role, UserStatus
from app.models import Deposit, ScannerSlot, TaskSession, TransactionHistory, User, Wallet, Withdrawal
from app.services import matching, slots, users
from app.services.users import AliasTaken, UserError

router = APIRouter(prefix="/api", tags=["users"])

Pages = Annotated[PageParams, Depends()]


def user_json(u: User, w: Wallet | None = None, slot_count: int | None = None) -> dict:
    return {
        "user_id": u.user_id,
        "telegram_id": u.telegram_id,
        "username": u.username,
        "name": u.name,
        "role": u.role,
        "status": u.status,
        "country": u.country,
        "country_name": timeutil.country_name(u.country) if u.country else None,
        "timezone": u.timezone,
        "utc_offset": timeutil.format_offset(u.timezone) if u.timezone and timeutil.is_valid_timezone(u.timezone) else None,
        "alias": u.alias,
        "reputation": u.reputation if u.role == Role.SCANNER.value else None,
        "bot_blocked": u.bot_blocked,
        "admin_note": u.admin_note,
        "approved_at": iso(u.approved_at),
        "created_at": iso(u.created_at),
        "balance": money(w.balance) if w else None,
        "pending": money(w.pending) if w else None,
        "slot_count": slot_count,
    }


async def _slot_counts(db, user_ids: list[int]) -> dict[int, int]:
    if not user_ids:
        return {}
    rows = (
        await db.execute(
            select(ScannerSlot.user_id, func.count()).where(ScannerSlot.user_id.in_(user_ids)).group_by(ScannerSlot.user_id)
        )
    ).all()
    return dict(rows)


# ── users ───────────────────────────────────────────────────────────────────


@router.get("/users")
async def list_users(
    _: CurrentAdmin,
    db: Db,
    params: Pages,
    role: str | None = None,
    status: str | None = None,
    country: str | None = Query(None, min_length=2, max_length=2),
    q: str | None = Query(None, max_length=100),
    include_onboarding: bool = False,
):
    stmt = select(User, Wallet).join(Wallet, Wallet.user_id == User.user_id, isouter=True)
    if role in {r.value for r in Role}:
        stmt = stmt.where(User.role == role)
    if status in {s.value for s in UserStatus}:
        stmt = stmt.where(User.status == status)
    elif not include_onboarding:
        stmt = stmt.where(User.status != UserStatus.ONBOARDING.value)
    if country:
        stmt = stmt.where(User.country == country.upper())
    if q:
        needle = f"%{q.strip()}%"
        clauses = [User.name.ilike(needle), User.username.ilike(needle), User.alias.ilike(needle)]
        if q.strip().lstrip("-").isdigit():
            clauses += [User.telegram_id == int(q.strip()), User.user_id == int(q.strip())]
        stmt = stmt.where(or_(*clauses))
    # pending approvals first, then newest
    stmt = stmt.order_by(case((User.status == UserStatus.PENDING.value, 0), else_=1), User.user_id.desc())
    rows, total = await paginate(db, stmt, params)
    counts = await _slot_counts(db, [u.user_id for u, _w in rows if u.role == Role.SCANNER.value])
    return page_response([user_json(u, w, counts.get(u.user_id)) for u, w in rows], total, params)


@router.get("/users/countries")
async def user_countries(_: CurrentAdmin, db: Db):
    """Countries that registered users come from (for the filter drop-downs); declared before ``/users/{user_id}``."""
    rows = (
        await db.execute(
            select(User.country, func.count())
            .where(User.country.is_not(None), User.status != UserStatus.ONBOARDING.value)
            .group_by(User.country)
            .order_by(func.count().desc(), User.country)
        )
    ).all()
    return {"items": [{"code": code, "name": timeutil.country_name(code), "count": n} for code, n in rows]}


@router.get("/users/{user_id}")
async def user_detail(user_id: int, _: CurrentAdmin, db: Db):
    user = await users.get_user(db, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    wallet_row = await db.get(Wallet, user_id)
    seller_side = user.role == Role.SELLER.value
    column = TaskSession.seller_id if seller_side else TaskSession.scanner_id
    recent = (await db.execute(select(TaskSession).where(column == user_id).order_by(TaskSession.session_id.desc()).limit(10))).scalars().all()
    stats = (
        await db.execute(
            select(func.count(), func.coalesce(func.sum(TransactionHistory.amount), 0), func.coalesce(func.sum(TransactionHistory.commission), 0)).where(
                (TransactionHistory.seller_id if seller_side else TransactionHistory.scanner_id) == user_id,
                TransactionHistory.status == "completed",
            )
        )
    ).one()
    slot_rows = await slots.list_slots(db, user_id) if not seller_side else []
    data = user_json(user, wallet_row, len(slot_rows))
    data["wallet"] = (
        {
            "balance": money(wallet_row.balance),
            "pending": money(wallet_row.pending),
            "total_earned": money(wallet_row.total_earned),
            "total_spent": money(wallet_row.total_spent),
            "total_deposited": money(wallet_row.total_deposited),
            "total_withdrawn": money(wallet_row.total_withdrawn),
            "bep20_address": wallet_row.bep20_address,
            "binance_address": wallet_row.binance_address,
        }
        if wallet_row
        else None
    )
    data["stats"] = {"completed_tasks": stats[0], "volume": money(stats[1]), "commission": money(stats[2])}
    data["recent_sessions"] = [
        {"session_id": s.session_id, "status": s.status, "amount": money(s.amount), "commission": money(s.commission), "sent_at": iso(s.sent_at)}
        for s in recent
    ]
    data["slots"] = [slot_json(s) for s in slot_rows]
    data["deposits"] = (await db.execute(select(func.count()).select_from(Deposit).where(Deposit.user_id == user_id))).scalar_one()
    data["withdrawals"] = (await db.execute(select(func.count()).select_from(Withdrawal).where(Withdrawal.user_id == user_id))).scalar_one()
    return data


class SuspendBody(BaseModel):
    note: str | None = Field(None, max_length=2000)


class ProfileBody(BaseModel):
    country: str | None = Field(None, max_length=2)
    timezone: str | None = Field(None, max_length=64)
    note: str | None = Field(None, max_length=2000)


async def _user_action(request: Request, admin, db, user_id: int, action: str, fn, **details):
    try:
        user = await fn()
    except UserError as exc:
        raise HTTPException(409, str(exc)) from exc
    await audit(db, admin, request, action, "user", user_id, details or None)
    return user_json(user)


@router.post("/users/{user_id}/approve")
async def approve_user(user_id: int, request: Request, admin: CurrentAdmin, db: Db):
    return await _user_action(request, admin, db, user_id, "user_approved", lambda: users.approve(db, user_id, actor_label(admin)))


@router.post("/users/{user_id}/reject")
async def reject_user(user_id: int, request: Request, admin: CurrentAdmin, db: Db):
    return await _user_action(request, admin, db, user_id, "user_rejected", lambda: users.reject(db, user_id, actor_label(admin)))


@router.post("/users/{user_id}/suspend")
async def suspend_user(user_id: int, body: SuspendBody, request: Request, admin: CurrentAdmin, db: Db):
    return await _user_action(request, admin, db, user_id, "user_suspended", lambda: users.suspend(db, user_id, actor_label(admin), body.note), note=body.note)


@router.post("/users/{user_id}/reinstate")
async def reinstate_user(user_id: int, request: Request, admin: CurrentAdmin, db: Db):
    return await _user_action(request, admin, db, user_id, "user_reinstated", lambda: users.reinstate(db, user_id, actor_label(admin)))


@router.patch("/users/{user_id}")
async def update_user(user_id: int, body: ProfileBody, request: Request, admin: CurrentAdmin, db: Db):
    fields = body.model_dump(exclude_unset=True)
    return await _user_action(
        request, admin, db, user_id, "user_updated",
        lambda: users.update_profile(db, user_id, country=fields.get("country"), timezone=fields.get("timezone"), note=fields.get("note")),
        **fields,
    )


# ── scanners ────────────────────────────────────────────────────────────────


def slot_json(s: ScannerSlot, now=None) -> dict:
    try:
        window = timeutil.utc_window_text(s.slot_start, s.slot_end, s.timezone, now)
        active = timeutil.active_occurrence(s.slot_start, s.slot_end, s.timezone, now or timeutil.utcnow()) is not None
    except ValueError:
        window, active = "invalid time zone", False
    return {
        "id": s.id,
        "user_id": s.user_id,
        "alias": s.name,
        "label": timeutil.slot_label(s.slot_start, s.slot_end),
        "slot_start": s.slot_start,
        "slot_end": s.slot_end,
        "timezone": s.timezone,
        "tz_label": timeutil.tz_label(s.timezone) if timeutil.is_valid_timezone(s.timezone) else s.timezone,
        "utc_window": window,
        "is_active": s.is_active,
        "live_now": active and s.is_active,
        "reputation": s.reputation,
        "created_at": iso(s.created_at),
    }


@router.get("/scanners")
async def list_scanners(
    _: CurrentAdmin,
    db: Db,
    params: Pages,
    q: str | None = Query(None, max_length=100),
    status: str | None = None,
    country: str | None = Query(None, min_length=2, max_length=2),
    needs_name: bool = False,
    active_now: bool = False,
):
    stmt = select(User).where(User.role == Role.SCANNER.value, User.status != UserStatus.ONBOARDING.value)
    if status in {s.value for s in UserStatus}:
        stmt = stmt.where(User.status == status)
    if country:
        stmt = stmt.where(User.country == country.upper())
    if needs_name:
        stmt = stmt.where(User.alias.is_(None), User.status == UserStatus.APPROVED.value)
    if q:
        needle = f"%{q.strip()}%"
        stmt = stmt.where(or_(User.name.ilike(needle), User.username.ilike(needle), User.alias.ilike(needle)))
    now = timeutil.utcnow()
    live_ids: set[int] = set()
    if active_now:
        live_ids = {c.scanner.user_id for c in await matching.active_candidates(db, now)}
        stmt = stmt.where(User.user_id.in_(live_ids or {-1}))
    stmt = stmt.order_by(User.alias.is_(None).desc(), User.alias, User.user_id)
    rows, total = await paginate(db, stmt, params)
    scanner_list = [r[0] for r in rows]
    ids = [u.user_id for u in scanner_list]
    slot_rows = (await db.execute(select(ScannerSlot).where(ScannerSlot.user_id.in_(ids)).order_by(ScannerSlot.slot_start))).scalars().all() if ids else []
    by_user: dict[int, list[ScannerSlot]] = {}
    for s in slot_rows:
        by_user.setdefault(s.user_id, []).append(s)
    earned: dict[int, tuple[int, object]] = {}
    if ids:
        for sid, count, amount in (
            await db.execute(
                select(TransactionHistory.scanner_id, func.count(), func.coalesce(func.sum(TransactionHistory.amount), 0))
                .where(TransactionHistory.scanner_id.in_(ids), TransactionHistory.status == "completed")
                .group_by(TransactionHistory.scanner_id)
            )
        ).all():
            earned[sid] = (count, amount)
    live = live_ids or ({c.scanner.user_id for c in await matching.active_candidates(db, now)} if ids else set())
    items = []
    for u in scanner_list:
        data = user_json(u, None, len(by_user.get(u.user_id, [])))
        data["slots"] = [slot_json(s, now) for s in by_user.get(u.user_id, [])]
        data["slot_labels"] = [timeutil.slot_label(s.slot_start, s.slot_end) for s in by_user.get(u.user_id, [])]
        data["active_now"] = u.user_id in live
        data["completed_tasks"], data["total_earned"] = (earned[u.user_id][0], money(earned[u.user_id][1])) if u.user_id in earned else (0, "0")
        data["last_assigned_at"] = iso(u.last_assigned_at)
        items.append(data)
    return page_response(items, total, params)


class AliasBody(BaseModel):
    alias: str | None = Field(None, max_length=32)  # null/empty -> next free "userN"


class ReputationBody(BaseModel):
    value: int = Field(ge=0, le=100)


class SlotBlock(BaseModel):
    start: int = Field(ge=0, le=23)
    end: int = Field(ge=0, le=23)


class SlotsBody(BaseModel):
    slots: list[SlotBlock] = Field(max_length=48)


@router.get("/scanners/next-alias")
async def next_alias(_: CurrentAdmin, db: Db):
    return {"alias": await users.next_alias(db)}


@router.put("/scanners/{user_id}/alias")
async def set_alias(user_id: int, body: AliasBody, request: Request, admin: CurrentAdmin, db: Db):
    try:
        user = await users.set_alias(db, user_id, (body.alias or "").strip() or None)
    except AliasTaken as exc:
        raise HTTPException(409, str(exc)) from exc
    except UserError as exc:
        raise HTTPException(422, str(exc)) from exc
    await audit(db, admin, request, "scanner_named", "user", user_id, {"alias": user.alias})
    return user_json(user)


@router.put("/scanners/{user_id}/reputation")
async def set_reputation(user_id: int, body: ReputationBody, request: Request, admin: CurrentAdmin, db: Db):
    try:
        user = await users.set_reputation(db, user_id, body.value)
    except UserError as exc:
        raise HTTPException(422, str(exc)) from exc
    await audit(db, admin, request, "reputation_set", "user", user_id, {"value": body.value})
    return user_json(user)


@router.put("/scanners/{user_id}/slots")
async def replace_slots(user_id: int, body: SlotsBody, request: Request, admin: CurrentAdmin, db: Db):
    user = await users.get_user(db, user_id, lock=True)
    if user is None or user.role != Role.SCANNER.value:
        raise HTTPException(404, "Scanner not found")
    try:
        await slots.set_slots(db, user, [(b.start, b.end) for b in body.slots])
    except slots.SlotError as exc:
        raise HTTPException(422, str(exc)) from exc
    await db.flush()
    await audit(db, admin, request, "slots_replaced", "user", user_id, {"count": len(body.slots)})
    return {"slots": [slot_json(s) for s in await slots.list_slots(db, user_id)]}


# ── slots overview ──────────────────────────────────────────────────────────


@router.get("/slots")
async def list_slots(
    _: CurrentAdmin,
    db: Db,
    params: Pages,
    scanner_id: int | None = None,
    timezone: str | None = Query(None, max_length=64),
    live: bool = False,
    inactive: bool = False,
):
    stmt = select(ScannerSlot, User).join(User, User.user_id == ScannerSlot.user_id)
    if scanner_id:
        stmt = stmt.where(ScannerSlot.user_id == scanner_id)
    if timezone:
        stmt = stmt.where(ScannerSlot.timezone == timezone)
    if inactive:
        stmt = stmt.where(ScannerSlot.is_active.is_(False))
    stmt = stmt.order_by(ScannerSlot.slot_start, ScannerSlot.user_id)
    if live:
        now = timeutil.utcnow()
        rows = (await db.execute(stmt)).all()
        selected = [(s, u) for s, u in rows if s.is_active and timeutil.active_occurrence(s.slot_start, s.slot_end, s.timezone, now)]
        window = selected[params.offset : params.offset + params.page_size]
        return page_response([{**slot_json(s, now), "status": u.status} for s, u in window], len(selected), params)
    rows, total = await paginate(db, stmt, params)
    now = timeutil.utcnow()
    return page_response([{**slot_json(s, now), "status": u.status} for s, u in rows], total, params)


class SlotPatch(BaseModel):
    is_active: bool


@router.patch("/slots/{slot_id}")
async def patch_slot(slot_id: int, body: SlotPatch, request: Request, admin: CurrentAdmin, db: Db):
    slot = await db.get(ScannerSlot, slot_id)
    if slot is None:
        raise HTTPException(404, "Slot not found")
    slot.is_active = body.is_active
    await audit(db, admin, request, "slot_toggled", "slot", slot_id, {"is_active": body.is_active})
    return slot_json(slot)


@router.delete("/slots/{slot_id}")
async def delete_slot(slot_id: int, request: Request, admin: CurrentAdmin, db: Db):
    slot = await db.get(ScannerSlot, slot_id)
    if slot is None:
        raise HTTPException(404, "Slot not found")
    await db.delete(slot)
    await audit(db, admin, request, "slot_deleted", "slot", slot_id)
    return {"status": "ok"}


@router.get("/slots/coverage")
async def slot_coverage(_: CurrentAdmin, db: Db):
    """Which scanners cover each UTC hour of today (slot-wise scanner mapping)."""
    coverage = await matching.utc_coverage(db)
    now = timeutil.utcnow()
    return {
        "date": now.date().isoformat(),
        "current_hour": now.hour,
        "hours": [{"hour": h, "scanners": coverage[h]} for h in range(24)],
    }


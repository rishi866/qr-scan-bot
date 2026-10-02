"""Settings, broadcast messages and the audit log."""

from __future__ import annotations

import html
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.common import PageParams, iso, page_response, paginate
from app.api.deps import CurrentAdmin, Db, audit
from app.enums import BroadcastAudience, OutboxStatus, Role, UserStatus
from app.models import AuditLog, Broadcast, Outbox, User
from app.services import outbox, settings_service

router = APIRouter(prefix="/api", tags=["settings"])
Pages = Annotated[PageParams, Depends()]


@router.get("/settings")
async def get_settings_endpoint(_: CurrentAdmin, db: Db):
    cfg = await settings_service.load(db)
    return {"definitions": settings_service.describe(), "values": cfg.as_json()}


class SettingsBody(BaseModel):
    values: dict[str, object] = Field(min_length=1, max_length=60)


@router.put("/settings")
async def update_settings(body: SettingsBody, request: Request, admin: CurrentAdmin, db: Db):
    before = (await settings_service.load(db)).as_json()
    try:
        cfg = await settings_service.save(db, body.values, admin.username)
    except settings_service.SettingsError as exc:
        raise HTTPException(422, str(exc)) from exc
    after = cfg.as_json()
    changed = {k: {"from": before.get(k), "to": after.get(k)} for k in body.values if before.get(k) != after.get(k)}
    await audit(db, admin, request, "settings_updated", "settings", None, changed or None)
    return {"definitions": settings_service.describe(), "values": after, "changed": list(changed)}


# ── broadcast ───────────────────────────────────────────────────────────────


class BroadcastBody(BaseModel):
    message: str = Field(min_length=1, max_length=3500)
    audience: BroadcastAudience = BroadcastAudience.ALL
    confirm: Literal[True]  # the panel asks "send to N users?" first; the API refuses without it


@router.get("/broadcasts/preview")
async def broadcast_preview(_: CurrentAdmin, db: Db, audience: BroadcastAudience = BroadcastAudience.ALL):
    return {"recipients": await _recipient_ids_count(db, audience)}


async def _recipients(db, audience: BroadcastAudience) -> list[User]:
    stmt = select(User).where(User.status == UserStatus.APPROVED.value, User.bot_blocked.is_(False))
    if audience == BroadcastAudience.SELLERS:
        stmt = stmt.where(User.role == Role.SELLER.value)
    elif audience == BroadcastAudience.SCANNERS:
        stmt = stmt.where(User.role == Role.SCANNER.value)
    return list((await db.execute(stmt.order_by(User.user_id))).scalars())


async def _recipient_ids_count(db, audience: BroadcastAudience) -> int:
    return len(await _recipients(db, audience))


@router.post("/broadcasts", status_code=201)
async def send_broadcast(body: BroadcastBody, request: Request, admin: CurrentAdmin, db: Db):
    recipients = await _recipients(db, body.audience)
    if not recipients:
        raise HTTPException(409, "There is nobody to send this to")
    text = "📣 <b>Announcement</b>\n\n" + html.escape(body.message.strip(), quote=False)
    record = Broadcast(message=body.message.strip(), audience=body.audience.value, total=len(recipients), created_by=admin.username)
    db.add(record)
    await db.flush()
    for user in recipients:  # delivered by the bot worker at a safe rate (~25 messages/second)
        await outbox.enqueue(db, user.telegram_id, text, dedupe_key=f"bc:{record.id}:{user.user_id}", broadcast_id=record.id)
    await audit(db, admin, request, "broadcast_sent", "broadcast", record.id, {"audience": body.audience.value, "recipients": len(recipients)})
    return {"id": record.id, "total": len(recipients)}


@router.get("/broadcasts")
async def list_broadcasts(_: CurrentAdmin, db: Db, params: Pages):
    rows, total = await paginate(db, select(Broadcast).order_by(Broadcast.id.desc()), params)
    ids = [b.id for (b,) in rows]
    stats: dict[int, dict[str, int]] = {i: {} for i in ids}
    if ids:
        for bid, status, count in (
            await db.execute(select(Outbox.broadcast_id, Outbox.status, func.count()).where(Outbox.broadcast_id.in_(ids)).group_by(Outbox.broadcast_id, Outbox.status))
        ).all():
            stats[bid][status] = count
    items = [
        {
            "id": b.id, "message": b.message, "audience": b.audience, "total": b.total, "created_by": b.created_by, "created_at": iso(b.created_at),
            "sent": stats[b.id].get(OutboxStatus.SENT.value, 0), "failed": stats[b.id].get(OutboxStatus.FAILED.value, 0), "queued": stats[b.id].get(OutboxStatus.QUEUED.value, 0),
        }
        for (b,) in rows
    ]
    return page_response(items, total, params)


# ── audit log ───────────────────────────────────────────────────────────────


@router.get("/audit")
async def list_audit(_: CurrentAdmin, db: Db, params: Pages, action: str | None = None, admin: str | None = None):
    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action.icontains(action.strip(), autoescape=True))
    if admin:
        stmt = stmt.where(AuditLog.admin_username == admin)
    rows, total = await paginate(db, stmt.order_by(AuditLog.id.desc()), params)
    return page_response(
        [
            {"id": a.id, "admin": a.admin_username, "action": a.action, "target_type": a.target_type, "target_id": a.target_id, "details": a.details, "ip": a.ip, "created_at": iso(a.created_at)}
            for (a,) in rows
        ],
        total,
        params,
    )

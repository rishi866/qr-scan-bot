"""FastAPI dependencies: database session, authenticated admin, audit logging."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.security import SESSION_COOKIE, TOKEN_ACCESS, client_ip, decode_token
from app.db import session_scope
from app.models import Admin, AuditLog

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "qr-admin"


async def get_db() -> AsyncIterator[AsyncSession]:
    """One transaction per request: committed on success, rolled back on any error."""
    async with session_scope() as session:
        yield session


Db = Annotated[AsyncSession, Depends(get_db)]


async def current_admin(request: Request, db: Db) -> Admin:
    token = request.cookies.get(SESSION_COOKIE)
    payload = decode_token(token, TOKEN_ACCESS) if token else None
    if payload is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    # cookies are sent automatically by browsers, so state-changing calls must prove they come from our own
    # page: a cross-site form cannot set a custom header (CSRF defence on top of SameSite=Strict)
    if request.method in UNSAFE_METHODS and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(status_code=403, detail="Missing CSRF header")
    admin = await db.get(Admin, payload["aid"])
    if admin is None or admin.username != payload["sub"] or admin.token_version != payload["tv"]:
        raise HTTPException(status_code=401, detail="Session expired")
    return admin


CurrentAdmin = Annotated[Admin, Depends(current_admin)]


async def audit(
    db: AsyncSession,
    admin: Admin | str,
    request: Request | None,
    action: str,
    target_type: str | None = None,
    target_id: Any = None,
    details: dict[str, Any] | None = None,
) -> None:
    db.add(
        AuditLog(
            admin_username=admin if isinstance(admin, str) else admin.username,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            details=details,
            ip=client_ip(request) if request is not None else None,
        )
    )


def actor_label(admin: Admin) -> str:
    """Who is recorded as the decision maker in services (``processed_by`` / ``resolved_by`` ...)."""
    return f"admin:{admin.username}"

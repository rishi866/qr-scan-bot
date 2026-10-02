"""Login (password + optional TOTP 2FA), logout, password change, 2FA enrolment, admin accounts."""

from __future__ import annotations

import base64
import datetime as dt
import re

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api import security
from app.api.deps import CurrentAdmin, Db, audit
from app.config import get_settings
from app.models import Admin
from app.services import qr
from app.timeutil import utcnow

router = APIRouter(prefix="/api", tags=["auth"])

MAX_FAILED_ATTEMPTS = 5
LOCK_MINUTES = 15
USERNAME_RE = re.compile(r"^[a-z0-9_.-]{3,32}$")
# verifying against a real hash for unknown usernames keeps response times uniform
_DUMMY_HASH = security.hash_password("dummy-password-for-timing")


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class TwoFactorBody(BaseModel):
    challenge: str = Field(min_length=10, max_length=2000)
    code: str = Field(min_length=6, max_length=12)


class PasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class ReauthBody(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class EnableBody(BaseModel):
    code: str = Field(min_length=6, max_length=12)


class DisableBody(BaseModel):
    password: str = Field(min_length=1, max_length=256)
    code: str = Field(min_length=6, max_length=12)


class NewAdminBody(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=1, max_length=256)


def admin_json(admin: Admin) -> dict:
    return {
        "id": admin.id,
        "username": admin.username,
        "totp_enabled": admin.totp_enabled,
        "last_login_at": admin.last_login_at,
        "created_at": admin.created_at,
    }


def _set_session_cookie(response: Response, admin: Admin) -> None:
    ttl = security.session_ttl_seconds()
    token = security.create_token(admin.id, admin.username, admin.token_version, security.TOKEN_ACCESS, ttl)
    response.set_cookie(
        security.SESSION_COOKIE,
        token,
        max_age=ttl,
        httponly=True,
        secure=get_settings().cookie_secure,
        samesite="strict",
        path="/",
    )


async def _register_failure(db, admin: Admin | None, request: Request, action: str) -> None:
    """Persist the failed attempt *before* raising (the request transaction rolls back on errors)."""
    if admin is not None:
        admin.failed_attempts += 1
        if admin.failed_attempts >= MAX_FAILED_ATTEMPTS:
            admin.locked_until = utcnow() + dt.timedelta(minutes=LOCK_MINUTES)
            admin.failed_attempts = 0
    await audit(db, admin.username if admin else "?", request, action)
    await db.commit()


def _too_many() -> HTTPException:
    return HTTPException(status_code=429, detail="Too many attempts. Please try again later.")


@router.post("/auth/login")
async def login(body: LoginBody, request: Request, response: Response, db: Db):
    if not security.login_limiter.hit(f"ip:{security.client_ip(request)}"):
        raise _too_many()
    admin = (await db.execute(select(Admin).where(Admin.username == body.username.strip().lower()))).scalar_one_or_none()
    if admin is not None and admin.locked_until and admin.locked_until > utcnow():
        raise _too_many()
    valid = security.verify_password(admin.password_hash if admin else _DUMMY_HASH, body.password) and admin is not None
    if not valid:
        await _register_failure(db, admin, request, "login_failed")
        raise HTTPException(status_code=401, detail="Invalid username or password")
    assert admin is not None
    if admin.totp_enabled:
        challenge = security.create_token(admin.id, admin.username, admin.token_version, security.TOKEN_CHALLENGE, security.CHALLENGE_TTL_SECONDS)
        return {"status": "2fa_required", "challenge": challenge}
    admin.failed_attempts = 0
    admin.locked_until = None
    admin.last_login_at = utcnow()
    await audit(db, admin, request, "login")
    _set_session_cookie(response, admin)
    return {"status": "ok", "admin": admin_json(admin)}


@router.post("/auth/2fa")
async def verify_two_factor(body: TwoFactorBody, request: Request, response: Response, db: Db):
    if not security.login_limiter.hit(f"ip:{security.client_ip(request)}"):
        raise _too_many()
    payload = security.decode_token(body.challenge, security.TOKEN_CHALLENGE)
    if payload is None:
        raise HTTPException(status_code=401, detail="The login attempt expired. Please sign in again.")
    admin = await db.get(Admin, payload["aid"])
    if admin is None or admin.token_version != payload["tv"] or not admin.totp_enabled or not admin.totp_secret_enc:
        raise HTTPException(status_code=401, detail="The login attempt expired. Please sign in again.")
    if admin.locked_until and admin.locked_until > utcnow():
        raise _too_many()
    secret = security.decrypt_secret(admin.totp_secret_enc)
    step = security.verify_totp(secret, body.code, admin.last_totp_step) if secret else None
    if step is None:
        await _register_failure(db, admin, request, "2fa_failed")
        raise HTTPException(status_code=401, detail="Invalid or already used code")
    admin.last_totp_step = step
    admin.failed_attempts = 0
    admin.locked_until = None
    admin.last_login_at = utcnow()
    await audit(db, admin, request, "login_2fa")
    _set_session_cookie(response, admin)
    return {"status": "ok", "admin": admin_json(admin)}


@router.post("/auth/logout")
async def logout(request: Request, response: Response, db: Db):
    """Clears the cookie; if the session is valid it also revokes every session of that admin."""
    from app.api.deps import current_admin

    try:
        admin = await current_admin(request, db)
        admin.token_version += 1
        await audit(db, admin, request, "logout")
    except HTTPException:
        pass
    response.delete_cookie(security.SESSION_COOKIE, path="/")
    return {"status": "ok"}


@router.get("/auth/me")
async def me(admin: CurrentAdmin):
    return admin_json(admin)


@router.post("/auth/password")
async def change_password(body: PasswordBody, request: Request, response: Response, admin: CurrentAdmin, db: Db):
    if not security.verify_password(admin.password_hash, body.current_password):
        await _register_failure(db, admin, request, "password_change_failed")
        raise HTTPException(status_code=403, detail="The current password is wrong")
    try:
        security.validate_password_strength(body.new_password, admin.username)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    admin.password_hash = security.hash_password(body.new_password)
    admin.token_version += 1  # signs out every other session
    await audit(db, admin, request, "password_changed")
    _set_session_cookie(response, admin)
    return {"status": "ok"}


# ── two-factor enrolment ────────────────────────────────────────────────────


@router.post("/auth/2fa/setup")
async def two_factor_setup(body: ReauthBody, request: Request, admin: CurrentAdmin, db: Db):
    if admin.totp_enabled:
        raise HTTPException(status_code=409, detail="Two-factor authentication is already enabled")
    if not security.verify_password(admin.password_hash, body.password):
        await _register_failure(db, admin, request, "2fa_setup_failed")
        raise HTTPException(status_code=403, detail="Wrong password")
    secret = security.new_totp_secret()
    admin.totp_secret_enc = security.encrypt_secret(secret)  # pending until /enable proves the app works
    uri = security.totp_uri(secret, admin.username)
    qr_png = base64.b64encode(qr.qr_png(uri)).decode()
    return {"secret": secret, "otpauth_uri": uri, "qr_png_base64": qr_png}


@router.post("/auth/2fa/enable")
async def two_factor_enable(body: EnableBody, request: Request, admin: CurrentAdmin, db: Db):
    if admin.totp_enabled:
        raise HTTPException(status_code=409, detail="Two-factor authentication is already enabled")
    secret = security.decrypt_secret(admin.totp_secret_enc) if admin.totp_secret_enc else None
    if not secret:
        raise HTTPException(status_code=409, detail="Start the setup first")
    step = security.verify_totp(secret, body.code)
    if step is None:
        raise HTTPException(status_code=422, detail="That code is not valid. Check the time on your phone and try again.")
    admin.totp_enabled = True
    admin.last_totp_step = step
    await audit(db, admin, request, "2fa_enabled")
    return {"status": "ok", "totp_enabled": True}


@router.post("/auth/2fa/disable")
async def two_factor_disable(body: DisableBody, request: Request, admin: CurrentAdmin, db: Db):
    if not admin.totp_enabled or not admin.totp_secret_enc:
        raise HTTPException(status_code=409, detail="Two-factor authentication is not enabled")
    secret = security.decrypt_secret(admin.totp_secret_enc)
    if not security.verify_password(admin.password_hash, body.password) or not secret or security.verify_totp(secret, body.code, admin.last_totp_step) is None:
        await _register_failure(db, admin, request, "2fa_disable_failed")
        raise HTTPException(status_code=403, detail="Wrong password or code")
    admin.totp_enabled = False
    admin.totp_secret_enc = None
    admin.last_totp_step = None
    await audit(db, admin, request, "2fa_disabled")
    return {"status": "ok", "totp_enabled": False}


# ── admin accounts ──────────────────────────────────────────────────────────


@router.get("/admins")
async def list_admins(_: CurrentAdmin, db: Db):
    rows = (await db.execute(select(Admin).order_by(Admin.id))).scalars().all()
    return {"items": [admin_json(a) for a in rows]}


@router.post("/admins", status_code=201)
async def create_admin(body: NewAdminBody, request: Request, admin: CurrentAdmin, db: Db):
    username = body.username.strip().lower()
    if not USERNAME_RE.match(username):
        raise HTTPException(status_code=422, detail="Username: 3-32 characters, letters, digits, . _ -")
    try:
        security.validate_password_strength(body.password, username)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if (await db.execute(select(Admin.id).where(Admin.username == username))).first():
        raise HTTPException(status_code=409, detail="That username is taken")
    created = Admin(username=username, password_hash=security.hash_password(body.password))
    db.add(created)
    await db.flush()
    await audit(db, admin, request, "admin_created", "admin", created.id, {"username": username})
    return admin_json(created)


@router.delete("/admins/{admin_id}")
async def delete_admin(admin_id: int, request: Request, admin: CurrentAdmin, db: Db):
    if admin_id == admin.id:
        raise HTTPException(status_code=409, detail="You cannot delete your own account")
    target = await db.get(Admin, admin_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Admin not found")
    if (await db.execute(select(func.count()).select_from(Admin))).scalar_one() <= 1:
        raise HTTPException(status_code=409, detail="The last admin cannot be deleted")
    await db.delete(target)
    await audit(db, admin, request, "admin_deleted", "admin", admin_id, {"username": target.username})
    return {"status": "ok"}

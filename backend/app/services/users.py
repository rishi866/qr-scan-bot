"""User lifecycle: registration, location, admin approval, scanner alias / reputation, bot state."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import callbacks, texts, timeutil
from app.enums import Role, UserStatus
from app.models import ScannerSlot, User
from app.services import outbox, settings_service, wallet
from app.timeutil import utcnow

ALIAS_RE = re.compile(r"^[a-z0-9_]{2,24}$")


class UserError(Exception):
    """Message is safe to show to an admin."""


class AliasTaken(UserError):
    pass


@dataclass
class TgIdentity:
    """The few fields we read from Telegram's ``User`` object."""

    id: int
    first_name: str = ""
    last_name: str | None = None
    username: str | None = None
    language_code: str | None = None

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p).strip() or f"User {self.id}"


# ── lookups ─────────────────────────────────────────────────────────────────


async def get_by_telegram_id(db: AsyncSession, telegram_id: int, *, lock: bool = False) -> User | None:
    stmt = select(User).where(User.telegram_id == telegram_id).execution_options(populate_existing=True)
    if lock:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def get_user(db: AsyncSession, user_id: int, *, lock: bool = False) -> User | None:
    stmt = select(User).where(User.user_id == user_id).execution_options(populate_existing=True)
    if lock:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def require_user(db: AsyncSession, user_id: int, *, lock: bool = False) -> User:
    user = await get_user(db, user_id, lock=lock)
    if user is None:
        raise UserError(f"user {user_id} not found")
    return user


def display_label(user: User) -> str:
    return texts.admin_user_label(user.name, user.alias, user.telegram_id)


# ── registration ────────────────────────────────────────────────────────────


async def register_role(db: AsyncSession, ident: TgIdentity, role: Role) -> User:
    """Create the user (status ``onboarding``) or - while still onboarding - change the chosen role."""
    user = await get_by_telegram_id(db, ident.id, lock=True)
    if user is None:
        user = User(
            telegram_id=ident.id,
            username=ident.username,
            name=ident.full_name[:255],
            role=role.value,
            language_code=(ident.language_code or None),
            status=UserStatus.ONBOARDING.value,
        )
        db.add(user)
        await db.flush()
        return user
    user.username = ident.username
    user.name = ident.full_name[:255]
    if user.status == UserStatus.ONBOARDING.value:
        user.role = role.value
    return user


async def refresh_identity(db: AsyncSession, user: User, ident: TgIdentity) -> None:
    """Keep the admin-visible name / @username current."""
    if user.username != ident.username:
        user.username = ident.username
    if user.name != ident.full_name[:255]:
        user.name = ident.full_name[:255]


async def set_location(db: AsyncSession, user: User, tz: str, country: str | None) -> None:
    canonical = timeutil.normalize_timezone(tz)
    if canonical is None:
        raise UserError("invalid time zone")
    if country is not None:
        country = country.upper()
        if not timeutil.is_valid_country(country):
            raise UserError("invalid country")
    user.timezone = canonical
    user.country = country
    if user.role == Role.SCANNER.value:
        # slots are local-time windows: they follow the scanner's (new) time zone
        await db.execute(update(ScannerSlot).where(ScannerSlot.user_id == user.user_id).values(timezone=canonical))


async def submit_for_approval(db: AsyncSession, user: User) -> bool:
    """Finish onboarding: status -> pending and ping the admins. Returns False if already submitted."""
    if user.status != UserStatus.ONBOARDING.value:
        return False
    if not user.timezone:
        raise UserError("time zone missing")
    user.status = UserStatus.PENDING.value
    now = utcnow()
    await outbox.notify_admins(
        db,
        texts.admin_new_request(
            name=user.name,
            username=user.username,
            telegram_id=user.telegram_id,
            role=user.role,
            country=user.country,
            tz=user.timezone,
            at=now,
        ),
        buttons=[[("✅ Approve", callbacks.admin_approve(user.user_id)), ("❌ Reject", callbacks.admin_reject(user.user_id))]],
        dedupe_key=f"approval:{user.user_id}",
    )
    return True


# ── admin decisions ─────────────────────────────────────────────────────────


async def approve(db: AsyncSession, user_id: int, by: str) -> User:
    user = await require_user(db, user_id, lock=True)
    if user.status == UserStatus.ONBOARDING.value:
        raise UserError("the user has not finished onboarding yet")
    if user.status == UserStatus.APPROVED.value:
        return user
    user.status = UserStatus.APPROVED.value
    user.approved_at = utcnow()
    await wallet.ensure_wallets(db, [user.user_id])
    buttons = None
    if user.role == Role.SCANNER.value:
        buttons = [[("🕒 Choose slots", callbacks.SLOT_EDIT)]]
    await outbox.notify_user(db, user, texts.approved(user.role), buttons=buttons)
    return user


async def reject(db: AsyncSession, user_id: int, by: str) -> User:
    user = await require_user(db, user_id, lock=True)
    if user.status in (UserStatus.ONBOARDING.value, UserStatus.REJECTED.value):
        return user
    user.status = UserStatus.REJECTED.value
    cfg = await settings_service.load(db)
    await outbox.notify_user(db, user, texts.rejected(cfg.support_contact))
    return user


async def suspend(db: AsyncSession, user_id: int, by: str, note: str | None = None) -> User:
    user = await require_user(db, user_id, lock=True)
    if user.status == UserStatus.SUSPENDED.value:
        return user
    user.status = UserStatus.SUSPENDED.value
    if note:
        user.admin_note = note[:2000]
    cfg = await settings_service.load(db)
    await outbox.notify_user(db, user, texts.suspended(cfg.support_contact))
    return user


async def reinstate(db: AsyncSession, user_id: int, by: str) -> User:
    user = await require_user(db, user_id, lock=True)
    if user.status != UserStatus.SUSPENDED.value:
        return user
    user.status = UserStatus.APPROVED.value
    await outbox.notify_user(db, user, "✅ Your account has been reinstated.")
    return user


# ── scanner alias / reputation ──────────────────────────────────────────────


def normalize_alias(alias: str) -> str:
    alias = (alias or "").strip().lower()
    if not ALIAS_RE.match(alias):
        raise UserError("name must be 2-24 characters: letters, digits or underscore")
    return alias


async def next_alias(db: AsyncSession) -> str:
    """``user1``, ``user2`` ... the first unused number above the current maximum."""
    aliases = (await db.execute(select(User.alias).where(User.alias.like("user%")))).scalars().all()
    highest = 0
    for alias in aliases:
        m = re.fullmatch(r"user(\d+)", alias or "")
        if m:
            highest = max(highest, int(m.group(1)))
    return f"user{highest + 1}"


async def set_alias(db: AsyncSession, user_id: int, alias: str | None) -> User:
    user = await require_user(db, user_id, lock=True)
    if user.role != Role.SCANNER.value:
        raise UserError("only scanners have a name")
    new_alias = normalize_alias(alias) if alias else await next_alias(db)
    if new_alias == user.alias:
        return user
    try:
        async with db.begin_nested():
            user.alias = new_alias
            await db.flush()
    except IntegrityError as exc:
        user.alias = None
        raise AliasTaken(f"the name '{new_alias}' is already used") from exc
    await db.execute(update(ScannerSlot).where(ScannerSlot.user_id == user.user_id).values(name=new_alias))
    if user.status == UserStatus.APPROVED.value:
        await outbox.notify_user(db, user, texts.name_set(new_alias))
    return user


async def set_reputation(db: AsyncSession, user_id: int, value: int) -> User:
    if not 0 <= value <= 100:
        raise UserError("reputation must be between 0 and 100")
    user = await require_user(db, user_id, lock=True)
    if user.role != Role.SCANNER.value:
        raise UserError("only scanners have a reputation")
    user.reputation = value
    await db.execute(update(ScannerSlot).where(ScannerSlot.user_id == user.user_id).values(reputation=value))
    return user


async def adjust_reputation(db: AsyncSession, user: User, delta: int, *, respect_setting: bool = True) -> None:
    if respect_setting:
        cfg = await settings_service.load(db)
        if not cfg.auto_reputation:
            return
    new_value = max(0, min(100, user.reputation + delta))
    if new_value != user.reputation:
        user.reputation = new_value
        await db.execute(update(ScannerSlot).where(ScannerSlot.user_id == user.user_id).values(reputation=new_value))


async def update_profile(
    db: AsyncSession, user_id: int, *, country: str | None = None, timezone: str | None = None, note: str | None = None
) -> User:
    """Admin edit of country / time zone / note. ``None`` leaves a field unchanged; ``""`` clears the country."""
    user = await require_user(db, user_id, lock=True)
    if timezone or country is not None:
        tz = timezone or user.timezone
        if not tz:
            raise UserError("set a time zone first")
        new_country = user.country if country is None else (country or None)
        await set_location(db, user, tz, new_country)
    if note is not None:
        user.admin_note = note[:2000]
    return user


# ── conversation state (stored in the DB so restarts / several workers are safe) ──


async def set_state(
    db: AsyncSession,
    user: User,
    state: str | None,
    data: dict[str, Any] | None = None,
    ttl_seconds: int | None = None,
    *,
    now: dt.datetime | None = None,
) -> None:
    user.state = state
    user.state_data = data if state else None
    user.state_expires_at = (now or utcnow()) + dt.timedelta(seconds=ttl_seconds) if (state and ttl_seconds) else None


async def clear_state(db: AsyncSession, user: User) -> None:
    await set_state(db, user, None)


def state_is_expired(user: User, now: dt.datetime | None = None) -> bool:
    return bool(user.state and user.state_expires_at and user.state_expires_at <= (now or utcnow()))


async def count_by_role_status(db: AsyncSession) -> dict[tuple[str, str], int]:
    rows = (await db.execute(select(User.role, User.status, func.count()).group_by(User.role, User.status))).all()
    return {(r, s): n for r, s, n in rows}

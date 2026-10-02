"""Scanner slot management (daily windows in the scanner's local time)."""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import timeutil
from app.models import ScannerSlot, User


class SlotError(ValueError):
    pass


def blocks_for_duration(hours: int) -> list[tuple[int, int]]:
    """``2`` -> ``[(0, 2), (2, 4), ..., (22, 0)]`` (end hour 0 means midnight)."""
    if hours < 1 or 24 % hours != 0:
        raise SlotError("slot duration must divide 24")
    return [(start, (start + hours) % 24) for start in range(0, 24, hours)]


def validate_block(start: int, end: int) -> tuple[int, int]:
    if not (isinstance(start, int) and isinstance(end, int)) or not (0 <= start <= 23 and 0 <= end <= 23):
        raise SlotError("slot hours must be between 0 and 23")
    if start == end:
        raise SlotError("a slot must span at least one hour")
    return start, end


async def list_slots(db: AsyncSession, user_id: int, *, only_active: bool = False) -> list[ScannerSlot]:
    stmt = select(ScannerSlot).where(ScannerSlot.user_id == user_id)
    if only_active:
        stmt = stmt.where(ScannerSlot.is_active.is_(True))
    stmt = stmt.order_by(ScannerSlot.slot_start, ScannerSlot.slot_end)
    return list((await db.execute(stmt)).scalars().all())


def labels(slots: list[ScannerSlot]) -> list[str]:
    return [timeutil.slot_label(s.slot_start, s.slot_end) for s in slots]


async def add_slot(db: AsyncSession, user: User, start: int, end: int) -> bool:
    """Returns ``True`` if the slot was created, ``False`` if it already existed."""
    if user.role != "scanner":
        raise SlotError("only scanners have slots")
    if not user.timezone:
        raise SlotError("set your time zone first")
    validate_block(start, end)
    stmt = (
        pg_insert(ScannerSlot)
        .values(
            user_id=user.user_id,
            name=user.alias,
            slot_start=start,
            slot_end=end,
            timezone=user.timezone,
            is_active=True,
            reputation=user.reputation,
        )
        .on_conflict_do_nothing(constraint="uq_scanner_slots_user_window")
        .returning(ScannerSlot.id)
    )
    return (await db.execute(stmt)).first() is not None


async def remove_slot(db: AsyncSession, user: User, start: int, end: int) -> bool:
    result = await db.execute(
        delete(ScannerSlot).where(
            ScannerSlot.user_id == user.user_id, ScannerSlot.slot_start == start, ScannerSlot.slot_end == end
        )
    )
    return (result.rowcount or 0) > 0


async def toggle_slot(db: AsyncSession, user: User, start: int, end: int) -> bool:
    """Add the slot if missing, otherwise remove it. Returns whether it is selected afterwards."""
    if await add_slot(db, user, start, end):
        return True
    await remove_slot(db, user, start, end)
    return False


async def set_slots(db: AsyncSession, user: User, blocks: list[tuple[int, int]]) -> None:
    """Replace the scanner's slots with exactly ``blocks`` (used by the admin panel)."""
    wanted = {validate_block(s, e) for s, e in blocks}
    existing = {(s.slot_start, s.slot_end): s for s in await list_slots(db, user.user_id)}
    for key, slot in existing.items():
        if key not in wanted:
            await db.delete(slot)
    for start, end in sorted(wanted - set(existing)):
        await add_slot(db, user, start, end)

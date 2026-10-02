"""Find the scanner who is *active right now*.

A scanner is a candidate when
* they are approved, have an alias (``user1``) and at least one active slot that covers the current
  instant in the slot's own (local) time zone;
* they did not answer ``Busy`` to the pre-slot reminder of that very slot occurrence;
* they are below the concurrent-task limit.

The seller's own time zone plays no role: scanners and sellers can be in different countries, "now"
is a single UTC instant. Candidates are ranked by reputation, then by who has waited longest.
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import timeutil
from app.enums import SCANNER_BUSY_STATUSES, Role, SlotResponse, UserStatus
from app.models import ScannerSlot, SlotNotification, TaskSession, User
from app.services import settings_service
from app.timeutil import utcnow


@dataclass
class Candidate:
    scanner: User
    slot: ScannerSlot
    occurrence_start: dt.datetime
    occurrence_end: dt.datetime

    @property
    def slot_label(self) -> str:
        return f"{timeutil.slot_label(self.slot.slot_start, self.slot.slot_end)} {self.slot.timezone}"


async def _active_rows(db: AsyncSession, now: dt.datetime, scanner_id: int | None) -> list[tuple[ScannerSlot, User, tuple]]:
    stmt = (
        select(ScannerSlot, User)
        .join(User, User.user_id == ScannerSlot.user_id)
        .where(
            User.role == Role.SCANNER.value,
            User.status == UserStatus.APPROVED.value,
            User.alias.is_not(None),
            ScannerSlot.is_active.is_(True),
        )
    )
    if scanner_id is not None:
        stmt = stmt.where(User.user_id == scanner_id)
    rows = (await db.execute(stmt.execution_options(populate_existing=True))).all()
    active = []
    for slot, scanner in rows:
        try:
            occurrence = timeutil.active_occurrence(slot.slot_start, slot.slot_end, slot.timezone, now)
        except ValueError:  # corrupt time zone value - ignore the slot instead of failing every /send
            continue
        if occurrence:
            active.append((slot, scanner, occurrence))
    return active


async def active_candidates(
    db: AsyncSession, now: dt.datetime | None = None, *, scanner_id: int | None = None, exclude: tuple[int, ...] = ()
) -> list[Candidate]:
    """All currently available scanners, best first."""
    now = timeutil.ensure_utc(now or utcnow())
    cfg = await settings_service.load(db)
    rows = [r for r in await _active_rows(db, now, scanner_id) if r[1].user_id not in exclude]
    if not rows:
        return []

    # slot occurrences the scanner declared "Busy" for
    slot_ids = [slot.id for slot, _, _ in rows]
    busy = {
        (n.slot_id, n.occurrence_start)
        for n in (
            await db.execute(
                select(SlotNotification).where(
                    SlotNotification.slot_id.in_(slot_ids), SlotNotification.response == SlotResponse.BUSY.value
                )
            )
        ).scalars()
    }

    # scanners already at their concurrency limit
    loads = dict(
        (
            await db.execute(
                select(TaskSession.scanner_id, func.count())
                .where(
                    TaskSession.scanner_id.in_({s.user_id for _, s, _ in rows}),
                    TaskSession.status.in_([s.value for s in SCANNER_BUSY_STATUSES]),
                )
                .group_by(TaskSession.scanner_id)
            )
        ).all()
    )

    best: dict[int, Candidate] = {}
    for slot, scanner, (start, end) in rows:
        if (slot.id, start) in busy:
            continue
        if loads.get(scanner.user_id, 0) >= cfg.max_active_sessions_per_scanner:
            continue
        current = best.get(scanner.user_id)
        # keep the occurrence that lasts longest (the "current slot" from the scanner's viewpoint)
        if current is None or end > current.occurrence_end:
            best[scanner.user_id] = Candidate(scanner, slot, start, end)

    epoch = dt.datetime.min.replace(tzinfo=dt.UTC)
    ranked = list(best.values())
    random.shuffle(ranked)  # random tie-break; sort() is stable
    ranked.sort(key=lambda c: (-c.scanner.reputation, c.scanner.last_assigned_at or epoch))
    return ranked


async def pick_scanner(db: AsyncSession, now: dt.datetime | None = None, *, exclude: tuple[int, ...] = ()) -> Candidate | None:
    candidates = await active_candidates(db, now, exclude=exclude)
    return candidates[0] if candidates else None


async def candidate_for(db: AsyncSession, scanner_id: int, now: dt.datetime | None = None) -> Candidate | None:
    candidates = await active_candidates(db, now, scanner_id=scanner_id)
    return candidates[0] if candidates else None


async def utc_coverage(db: AsyncSession, day: dt.date | None = None) -> dict[int, list[dict]]:
    """For the admin panel: which scanners cover each UTC hour of ``day`` (sampled at :00 and :30)."""
    day = day or utcnow().date()
    base = dt.datetime.combine(day, dt.time(0), tzinfo=dt.UTC)
    stmt = (
        select(ScannerSlot, User)
        .join(User, User.user_id == ScannerSlot.user_id)
        .where(ScannerSlot.is_active.is_(True), User.role == Role.SCANNER.value, User.status == UserStatus.APPROVED.value)
    )
    rows = (await db.execute(stmt)).all()
    coverage: dict[int, list[dict]] = {h: [] for h in range(24)}
    for slot, scanner in rows:
        for hour in range(24):
            for minute in (0, 30):
                moment = base + dt.timedelta(hours=hour, minutes=minute)
                try:
                    hit = timeutil.active_occurrence(slot.slot_start, slot.slot_end, slot.timezone, moment)
                except ValueError:
                    hit = None
                if hit:
                    entry = {"user_id": scanner.user_id, "alias": scanner.alias, "slot": timeutil.slot_label(slot.slot_start, slot.slot_end), "timezone": slot.timezone}
                    if not any(c["user_id"] == scanner.user_id and c["slot"] == entry["slot"] for c in coverage[hour]):
                        coverage[hour].append(entry)
                    break
    return coverage

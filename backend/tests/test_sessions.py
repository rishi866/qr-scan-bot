"""Task lifecycle, matching, money invariants, races and anonymity - against real PostgreSQL."""

from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.db import session_scope
from app.enums import SessionStatus, SlotResponse
from app.models import LedgerEntry, Outbox, SlotNotification, TaskSession, TransactionHistory
from app.services import matching, sessions, users, wallet
from app.services.wallet import InsufficientFunds
from tests.factories import fund, make_scanner, make_user

UTC = dt.UTC
NOW = dt.datetime(2026, 10, 2, 3, 0, tzinfo=UTC)  # 08:30 IST -> slot 08-10 IST is live
URL = "https://chatgpt.com/checkout/openai_llc/cs_live_test123#fid"
COST = Decimal("0.5005")


async def messages(db, chat_id: int) -> list[Outbox]:
    return list((await db.execute(select(Outbox).where(Outbox.chat_id == chat_id).order_by(Outbox.id))).scalars())


async def balances(db, user) -> tuple[Decimal, Decimal]:
    user_id = user if isinstance(user, int) else user.user_id
    w = await wallet.get_wallet(db, user_id)
    return w.balance, w.pending


async def start_task(db, seller=None, scanner=None, funds="10"):
    seller = seller or await make_user(db, role="seller", tz="Europe/London", country="GB", name="SellerSecretName")
    scanner = scanner or await make_scanner(db, alias="user1", slots=[(8, 10)], name="ScannerSecretName")
    await fund(db, seller, funds)
    await db.commit()
    s = await sessions.create_session(db, seller, scanner.user_id, URL, now=NOW)
    await db.commit()
    return seller, scanner, s


# ── matching ────────────────────────────────────────────────────────────────


async def test_matching_finds_scanner_in_active_local_slot(db):
    scanner = await make_scanner(db, alias="user1", slots=[(8, 10)])
    await make_scanner(db, alias="user2", slots=[(10, 12)])  # not active at 08:30 IST
    await db.commit()
    cands = await matching.active_candidates(db, NOW)
    assert [c.scanner.alias for c in cands] == ["user1"]
    assert cands[0].slot_label == "08-10 Asia/Kolkata"
    # one minute before the slot starts (02:29Z == 07:59 IST): nobody
    assert await matching.active_candidates(db, dt.datetime(2026, 10, 2, 2, 29, tzinfo=UTC)) == []
    assert scanner.user_id == cands[0].scanner.user_id


async def test_matching_works_across_time_zones(db):
    # Pakistan scanner, slot 08-10 PKT (UTC+5) = 03:00-05:00Z; a London seller is irrelevant
    await make_scanner(db, alias="user5", tz="Asia/Karachi", country="PK", slots=[(8, 10)])
    await db.commit()
    assert [c.scanner.alias for c in await matching.active_candidates(db, NOW)] == ["user5"]


@pytest.mark.parametrize(
    "kwargs",
    [{"alias": None}, {"status": "pending"}, {"status": "suspended"}, {"status": "rejected"}],
)
async def test_matching_excludes_ineligible_scanners(db, kwargs):
    await make_scanner(db, slots=[(8, 10)], **kwargs)
    await db.commit()
    assert await matching.active_candidates(db, NOW) == []


async def test_matching_respects_busy_answer_and_capacity(db):
    s1 = await make_scanner(db, alias="user1", slots=[(8, 10)])
    s2 = await make_scanner(db, alias="user2", slots=[(8, 10)])
    await db.commit()
    c1 = (await matching.active_candidates(db, NOW, scanner_id=s1.user_id))[0]
    db.add(
        SlotNotification(
            slot_id=c1.slot.id, user_id=s1.user_id, occurrence_start=c1.occurrence_start,
            occurrence_end=c1.occurrence_end, response=SlotResponse.BUSY.value,
        )
    )
    await db.commit()
    assert [c.scanner.alias for c in await matching.active_candidates(db, NOW)] == ["user2"]

    seller = await make_user(db, role="seller")
    await fund(db, seller, "5")
    await db.commit()
    await sessions.create_session(db, seller, s2.user_id, URL, now=NOW)
    await db.commit()
    assert await matching.active_candidates(db, NOW) == []  # user1 busy, user2 at capacity


async def test_matching_ranks_by_reputation_then_longest_idle(db):
    low = await make_scanner(db, alias="user1", slots=[(8, 10)], reputation=40)
    high = await make_scanner(db, alias="user2", slots=[(8, 10)], reputation=90)
    mid_recent = await make_scanner(db, alias="user3", slots=[(8, 10)], reputation=60)
    mid_idle = await make_scanner(db, alias="user4", slots=[(8, 10)], reputation=60)
    mid_recent.last_assigned_at = NOW - dt.timedelta(minutes=1)
    mid_idle.last_assigned_at = NOW - dt.timedelta(hours=3)
    await db.commit()
    order = [c.scanner.alias for c in await matching.active_candidates(db, NOW)]
    assert order == ["user2", "user4", "user3", "user1"]
    assert low.user_id and high.user_id


async def test_coverage_for_admin_view(db):
    await make_scanner(db, alias="user1", slots=[(8, 10)])
    await db.commit()
    cov = await matching.utc_coverage(db, NOW.date())
    assert [c["alias"] for c in cov[3]] == ["user1"]  # 03:00-04:00Z is inside 02:30-04:30Z
    assert cov[5] == [] and cov[2][0]["alias"] == "user1"  # 02:30 sample hits


# ── happy path + money ──────────────────────────────────────────────────────


async def test_full_happy_path_moves_money_exactly(db):
    seller, scanner, s = await start_task(db)
    assert s.status == SessionStatus.AWAITING_SCANNER.value
    assert (s.amount, s.commission) == (Decimal("0.5"), Decimal("0.0005"))
    assert s.slot_label == "08-10 Asia/Kolkata" and s.seller_timezone == "Europe/London"
    assert await balances(db, seller) == (Decimal("10") - COST, COST)  # held

    await sessions.accept(db, s.session_id, scanner.user_id, now=NOW + dt.timedelta(seconds=30))
    await db.commit()
    s = await sessions.mark_done(db, s.session_id, scanner.user_id, now=NOW + dt.timedelta(minutes=5))
    await db.commit()
    assert s.status == SessionStatus.DONE.value and s.prompted_at is None

    # the seller prompt only goes out after the configured delay (2 min)
    assert not await sessions.process_prompt(db, s.session_id, NOW + dt.timedelta(minutes=6))
    assert await sessions.process_prompt(db, s.session_id, NOW + dt.timedelta(minutes=7, seconds=1))
    await db.commit()

    s = await sessions.confirm(db, s.session_id, seller.user_id, now=NOW + dt.timedelta(minutes=8))
    await db.commit()
    assert s.status == SessionStatus.CONFIRMED.value

    assert await balances(db, seller) == (Decimal("10") - COST, Decimal("0"))
    assert await balances(db, scanner) == (Decimal("0.5"), Decimal("0"))
    platform = (await db.execute(select(func.sum(LedgerEntry.balance_delta)).where(LedgerEntry.user_id.is_(None)))).scalar_one()
    assert platform == Decimal("0.0005")
    w = await wallet.get_wallet(db, scanner.user_id)
    assert w.total_earned == Decimal("0.5")
    assert (await wallet.get_wallet(db, seller.user_id)).total_spent == COST

    tx = (await db.execute(select(TransactionHistory))).scalar_one()
    assert tx.status == "completed" and tx.scanner_name == "user1" and tx.seller_country == "GB"
    assert tx.scanner_country == "IN" and tx.slot == "08-10 Asia/Kolkata" and tx.url == URL
    assert tx.confirmed_at is not None

    assert (await wallet.reconcile(db)).ok
    assert (await users.require_user(db, scanner.user_id)).reputation == 51  # +1 for a confirmed task


async def test_insufficient_funds_blocks_and_changes_nothing(db):
    seller = await make_user(db, role="seller")
    scanner = await make_scanner(db, slots=[(8, 10)])
    await fund(db, seller, "0.5")  # less than 0.5005
    await db.commit()
    seller_id, scanner_id = seller.user_id, scanner.user_id
    with pytest.raises(InsufficientFunds):
        await sessions.create_session(db, seller, scanner_id, URL, now=NOW)
    await db.rollback()
    assert (await db.execute(select(func.count()).select_from(TaskSession))).scalar_one() == 0
    assert await balances(db, seller_id) == (Decimal("0.5"), Decimal("0"))


async def test_scanner_no_longer_active_is_rejected(db):
    seller = await make_user(db, role="seller")
    scanner = await make_scanner(db, slots=[(10, 12)])  # not active at NOW
    await fund(db, seller, "5")
    await db.commit()
    seller_id, scanner_id = seller.user_id, scanner.user_id
    with pytest.raises(sessions.ScannerUnavailable):
        await sessions.create_session(db, seller, scanner_id, URL, now=NOW)
    await db.rollback()
    assert await balances(db, seller_id) == (Decimal("5"), Decimal("0"))


async def test_seller_can_only_have_one_task_in_flight(db):
    seller, scanner, _ = await start_task(db)
    other = await make_scanner(db, alias="user2", slots=[(8, 10)])
    await db.commit()
    with pytest.raises(sessions.SellerBusy):
        await sessions.create_session(db, seller, other.user_id, URL, now=NOW)
    await db.rollback()


async def test_unapproved_seller_cannot_send(db):
    seller = await make_user(db, role="seller", status="pending")
    scanner = await make_scanner(db, slots=[(8, 10)])
    await fund(db, seller, "5")
    await db.commit()
    with pytest.raises(sessions.NotAllowed):
        await sessions.create_session(db, seller, scanner.user_id, URL, now=NOW)
    await db.rollback()


# ── skip / expire / timeouts ────────────────────────────────────────────────


async def test_skip_releases_funds_and_tells_seller(db):
    seller, scanner, s = await start_task(db)
    await sessions.skip(db, s.session_id, scanner.user_id, now=NOW + dt.timedelta(seconds=20))
    await db.commit()
    assert await balances(db, seller) == (Decimal("10"), Decimal("0"))
    msgs = [m.body for m in await messages(db, seller.telegram_id)]
    assert any("skipped your URL" in m and "user1" in m for m in msgs)
    assert (await users.require_user(db, scanner.user_id)).reputation == 50  # skipping is not penalised
    assert (await wallet.reconcile(db)).ok


async def test_expiry_after_two_minutes_refunds_and_penalises(db):
    seller, scanner, s = await start_task(db)
    assert s.expires_at == NOW + dt.timedelta(seconds=120)
    assert not await sessions.process_expire(db, s.session_id, NOW + dt.timedelta(seconds=119))
    assert await sessions.process_expire(db, s.session_id, NOW + dt.timedelta(seconds=121))
    await db.commit()
    assert (await sessions.get_session(db, s.session_id)).status == SessionStatus.EXPIRED.value
    assert await balances(db, seller) == (Decimal("10"), Decimal("0"))
    assert (await users.require_user(db, scanner.user_id)).reputation == 49
    assert not await sessions.process_expire(db, s.session_id, NOW + dt.timedelta(hours=1))  # idempotent
    assert any("didn't respond in time" in m.body for m in await messages(db, seller.telegram_id))
    assert (await wallet.reconcile(db)).ok


async def test_accept_after_deadline_is_refused(db):
    _, scanner, s = await start_task(db)
    with pytest.raises(sessions.InvalidState):
        await sessions.accept(db, s.session_id, scanner.user_id, now=NOW + dt.timedelta(seconds=121))
    await db.rollback()


async def test_accepted_but_never_done_times_out_and_refunds(db):
    seller, scanner, s = await start_task(db)
    await sessions.accept(db, s.session_id, scanner.user_id, now=NOW + dt.timedelta(seconds=10))
    await db.commit()
    assert not await sessions.process_task_timeout(db, s.session_id, NOW + dt.timedelta(minutes=29))
    assert await sessions.process_task_timeout(db, s.session_id, NOW + dt.timedelta(minutes=31))
    await db.commit()
    assert (await sessions.get_session(db, s.session_id)).status == SessionStatus.TIMED_OUT.value
    assert await balances(db, seller) == (Decimal("10"), Decimal("0"))
    assert (await users.require_user(db, scanner.user_id)).reputation == 48
    assert (await wallet.reconcile(db)).ok


async def test_seller_silence_leads_to_auto_confirm(db):
    seller, scanner, s = await start_task(db)
    await sessions.accept(db, s.session_id, scanner.user_id, now=NOW)
    await sessions.mark_done(db, s.session_id, scanner.user_id, now=NOW)
    await db.commit()
    prompt_time = NOW + dt.timedelta(minutes=3)
    assert await sessions.process_prompt(db, s.session_id, prompt_time)
    await db.commit()
    assert not await sessions.process_autoconfirm(db, s.session_id, prompt_time + dt.timedelta(minutes=59))
    assert await sessions.process_autoconfirm(db, s.session_id, prompt_time + dt.timedelta(minutes=61))
    await db.commit()
    assert await balances(db, scanner) == (Decimal("0.5"), Decimal("0"))
    assert (await sessions.get_session(db, s.session_id)).closed_reason == "auto_confirmed"
    assert any("auto-confirmed" in m.body for m in await messages(db, seller.telegram_id))
    assert (await wallet.reconcile(db)).ok


async def test_due_finder_reports_the_right_sessions(db):
    _, scanner, s = await start_task(db)
    due = await sessions.find_due(db, NOW + dt.timedelta(minutes=5))
    assert due["expire"] == [s.session_id] and not due["prompt"]
    assert not any((await sessions.find_due(db, NOW + dt.timedelta(seconds=5))).values())


# ── transitions are guarded ─────────────────────────────────────────────────


async def test_wrong_person_or_state_is_rejected(db):
    seller, scanner, s = await start_task(db)
    intruder = await make_scanner(db, alias="user9", slots=[(8, 10)])
    await db.commit()
    sid, seller_id, scanner_id, intruder_id = s.session_id, seller.user_id, scanner.user_id, intruder.user_id
    with pytest.raises(sessions.NotAllowed):
        await sessions.accept(db, sid, intruder_id, now=NOW)
    await db.rollback()
    with pytest.raises(sessions.InvalidState):
        await sessions.mark_done(db, sid, scanner_id, now=NOW)  # not accepted yet
    await db.rollback()
    with pytest.raises(sessions.InvalidState):
        await sessions.confirm(db, sid, seller_id, now=NOW)  # not done yet
    await db.rollback()
    await sessions.accept(db, sid, scanner_id, now=NOW)
    await db.commit()
    with pytest.raises(sessions.InvalidState):
        await sessions.accept(db, sid, scanner_id, now=NOW)  # double tap
    await db.rollback()
    with pytest.raises(sessions.NotAllowed):
        await sessions.confirm(db, sid, intruder_id, now=NOW)
    await db.rollback()


async def test_seller_cannot_confirm_before_being_prompted(db):
    seller, scanner, s = await start_task(db)
    await sessions.accept(db, s.session_id, scanner.user_id, now=NOW)
    await sessions.mark_done(db, s.session_id, scanner.user_id, now=NOW)
    await db.commit()
    with pytest.raises(sessions.InvalidState):
        await sessions.confirm(db, s.session_id, seller.user_id, now=NOW)
    await db.rollback()


# ── races ───────────────────────────────────────────────────────────────────


async def test_two_sellers_cannot_both_grab_the_same_scanner():
    async with session_scope() as db:
        scanner = await make_scanner(db, alias="user1", slots=[(8, 10)])
        sellers = [await make_user(db, role="seller") for _ in range(2)]
        for seller in sellers:
            await fund(db, seller, "5")
        scanner_id, seller_ids = scanner.user_id, [s.user_id for s in sellers]

    async def attempt(seller_id: int):
        try:
            async with session_scope() as db:
                seller = await users.require_user(db, seller_id)
                await sessions.create_session(db, seller, scanner_id, URL, now=NOW)
            return "ok"
        except sessions.ScannerUnavailable:
            return "unavailable"

    results = await asyncio.gather(*(attempt(i) for i in seller_ids))
    assert sorted(results) == ["ok", "unavailable"]
    async with session_scope() as db:
        assert (await db.execute(select(func.count()).select_from(TaskSession))).scalar_one() == 1
        assert (await wallet.reconcile(db)).ok


async def test_double_confirm_race_pays_once():
    async with session_scope() as db:
        seller, scanner, s = await start_task(db)
        await sessions.accept(db, s.session_id, scanner.user_id, now=NOW)
        await sessions.mark_done(db, s.session_id, scanner.user_id, now=NOW)
        await sessions.process_prompt(db, s.session_id, NOW + dt.timedelta(minutes=3))
        sid, seller_id, scanner_id = s.session_id, seller.user_id, scanner.user_id

    async def attempt():
        try:
            async with session_scope() as db:
                await sessions.confirm(db, sid, seller_id, now=NOW + dt.timedelta(minutes=4))
            return "ok"
        except sessions.InvalidState:
            return "invalid"

    results = await asyncio.gather(*(attempt() for _ in range(4)))
    assert results.count("ok") == 1
    async with session_scope() as db:
        assert (await wallet.get_wallet(db, scanner_id)).balance == Decimal("0.5")
        assert (await wallet.reconcile(db)).ok


async def test_accept_vs_expire_race_never_double_handles():
    async with session_scope() as db:
        seller, scanner, s = await start_task(db)
        sid, scanner_id = s.session_id, scanner.user_id
    late = NOW + dt.timedelta(seconds=121)

    async def do_accept():
        try:
            async with session_scope() as db:
                await sessions.accept(db, sid, scanner_id, now=late)
            return "accepted"
        except sessions.InvalidState:
            return "refused"

    async def do_expire():
        async with session_scope() as db:
            return "expired" if await sessions.process_expire(db, sid, late) else "noop"

    results = await asyncio.gather(do_accept(), do_expire())
    assert results == ["refused", "expired"]
    async with session_scope() as db:
        assert (await sessions.get_session(db, sid)).status == SessionStatus.EXPIRED.value
        assert (await wallet.reconcile(db)).ok


# ── anonymity ───────────────────────────────────────────────────────────────


async def test_parties_never_see_each_others_identity(db):
    seller, scanner, s = await start_task(db)
    await sessions.accept(db, s.session_id, scanner.user_id, now=NOW)
    await sessions.mark_done(db, s.session_id, scanner.user_id, now=NOW)
    await sessions.process_prompt(db, s.session_id, NOW + dt.timedelta(minutes=3))
    await sessions.confirm(db, s.session_id, seller.user_id, now=NOW + dt.timedelta(minutes=4))
    await db.commit()

    to_seller = " ".join(m.body for m in await messages(db, seller.telegram_id))
    to_scanner = " ".join(m.body for m in await messages(db, scanner.telegram_id))
    assert to_seller and to_scanner
    assert "user1" in to_seller
    for secret in ("ScannerSecretName", str(scanner.telegram_id), "India", "Asia/Kolkata", "08-10", "IST", f"u{scanner.telegram_id}"):
        assert secret not in to_seller, secret
    for secret in ("SellerSecretName", str(seller.telegram_id), "United Kingdom", "Europe/London", f"u{seller.telegram_id}"):
        assert secret not in to_scanner, secret
    # the scanner only ever receives the URL itself, never who sent it
    assert URL.split("#")[0] in to_scanner

"""Admin API: users, scanners (names, reputation, slots), slots overview and dashboard."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from app import timeutil
from app.db import session_scope
from app.models import AuditLog, Outbox, ScannerSlot, User
from app.services import wallet
from tests.api_helpers import make_tx
from tests.factories import fund, make_scanner, make_user


def blocks_covering_now(tz: str) -> list[tuple[int, int]]:
    hour = timeutil.to_local(timeutil.utcnow(), tz).hour
    cur = hour // 2 * 2
    return [((cur + d) % 24, (cur + d + 2) % 24) for d in (-2, 0, 2)]


async def seed_users():
    async with session_scope() as db:
        pending = await make_user(db, role="seller", status="pending", name="Pat Pending", country="GB", tz="Europe/London")
        approved = await make_user(db, role="seller", status="approved", name="Alice Approved", country="IN")
        onboarding = await make_user(db, role="scanner", status="onboarding", name="Olga Onboarding", country=None, tz=None)
        scanner = await make_scanner(db, alias="user1", name="Sara Scanner", slots=[(8, 10)])
        await fund(db, approved, "12.5")
        return {"pending": pending.user_id, "approved": approved.user_id, "onboarding": onboarding.user_id, "scanner": scanner.user_id}


async def test_list_users_filters_search_and_pagination(api):
    ids = await seed_users()
    r = await api.get("/api/users")
    items = r.json()["items"]
    assert r.status_code == 200 and r.json()["total"] == 3  # the onboarding user is hidden by default
    assert items[0]["user_id"] == ids["pending"]  # pending approvals come first
    assert ids["onboarding"] not in {i["user_id"] for i in items}
    assert (await api.get("/api/users?include_onboarding=true")).json()["total"] == 4

    assert {i["user_id"] for i in (await api.get("/api/users?role=scanner")).json()["items"]} == {ids["scanner"]}
    assert (await api.get("/api/users?status=pending")).json()["total"] == 1
    assert (await api.get("/api/users?country=in")).json()["total"] == 2  # alice + sara (both IN)
    by_name = (await api.get("/api/users?q=alice")).json()["items"]
    assert [i["name"] for i in by_name] == ["Alice Approved"] and by_name[0]["balance"] == "12.5"
    assert (await api.get("/api/users?q=user1")).json()["items"][0]["alias"] == "user1"  # by scanner name
    tg = by_name[0]["telegram_id"]
    assert (await api.get(f"/api/users?q={tg}")).json()["total"] == 1  # by Telegram id
    page = (await api.get("/api/users?page=2&page_size=2")).json()
    assert len(page["items"]) == 1 and page["total"] == 3 and page["page"] == 2
    assert (await api.get("/api/users?page_size=1000")).status_code == 422  # bounded


async def test_user_detail_and_404(api):
    ids = await seed_users()
    detail = (await api.get(f"/api/users/{ids['approved']}")).json()
    assert detail["wallet"]["balance"] == "12.5" and detail["wallet"]["total_deposited"] == "12.5"
    assert detail["stats"] == {"completed_tasks": 0, "volume": "0", "commission": "0"}
    assert detail["deposits"] == 1 and detail["recent_sessions"] == []
    scanner = (await api.get(f"/api/users/{ids['scanner']}")).json()
    assert scanner["slots"][0]["label"] == "08-10" and scanner["slots"][0]["timezone"] == "Asia/Kolkata"
    assert (await api.get("/api/users/99999")).status_code == 404


async def test_approve_reject_suspend_reinstate_notify_the_user_and_are_audited(api):
    ids = await seed_users()
    uid = ids["pending"]
    approved = await api.post(f"/api/users/{uid}/approve")
    assert approved.status_code == 200 and approved.json()["status"] == "approved"
    assert (await api.post(f"/api/users/{uid}/approve")).json()["status"] == "approved"  # idempotent
    async with session_scope() as db:
        user = await db.get(User, uid)
        msgs = [m.body for m in (await db.execute(select(Outbox).where(Outbox.chat_id == user.telegram_id))).scalars()]
    assert msgs == ["✅ Approved. Use /send."]  # sent once

    suspended = await api.post(f"/api/users/{uid}/suspend", json={"note": "chargeback risk"})
    assert suspended.json()["status"] == "suspended" and suspended.json()["admin_note"] == "chargeback risk"
    assert (await api.post(f"/api/users/{uid}/reinstate")).json()["status"] == "approved"

    other = ids["approved"]
    assert (await api.post(f"/api/users/{other}/reject")).json()["status"] == "rejected"
    assert (await api.post(f"/api/users/{ids['onboarding']}/approve")).status_code == 409  # has not finished onboarding
    assert (await api.post("/api/users/99999/approve")).status_code == 409

    async with session_scope() as db:
        actions = [a.action for a in (await db.execute(select(AuditLog).order_by(AuditLog.id))).scalars() if a.action.startswith("user_")]
    assert actions == ["user_approved", "user_approved", "user_suspended", "user_reinstated", "user_rejected"]


async def test_admin_can_correct_country_timezone_and_note(api):
    ids = await seed_users()
    r = await api.patch(f"/api/users/{ids['approved']}", json={"country": "pk", "timezone": "Asia/Karachi", "note": "VIP"})
    assert r.status_code == 200 and (r.json()["country"], r.json()["timezone"], r.json()["admin_note"]) == ("PK", "Asia/Karachi", "VIP")
    assert (await api.patch(f"/api/users/{ids['approved']}", json={"timezone": "Mars/Base"})).status_code == 409
    assert (await api.patch(f"/api/users/{ids['approved']}", json={"country": "ZZ"})).status_code == 409


# ── scanners ────────────────────────────────────────────────────────────────


async def test_scanner_names_auto_manual_unique_and_mirrored_to_slots(api):
    async with session_scope() as db:
        a = await make_scanner(db, alias=None, name="Anna", slots=[(8, 10)])
        b = await make_scanner(db, alias=None, name="Ben", slots=[(10, 12)])
    assert (await api.get("/api/scanners/next-alias")).json() == {"alias": "user1"}
    named = await api.put(f"/api/scanners/{a.user_id}/alias", json={"alias": None})  # auto
    assert named.json()["alias"] == "user1"
    assert (await api.get("/api/scanners/next-alias")).json() == {"alias": "user2"}
    assert (await api.put(f"/api/scanners/{b.user_id}/alias", json={"alias": "USER1"})).status_code == 409  # unique, case-insensitive
    assert (await api.put(f"/api/scanners/{b.user_id}/alias", json={"alias": "bad name!"})).status_code == 422
    assert (await api.put(f"/api/scanners/{b.user_id}/alias", json={"alias": "Falcon_7"})).json()["alias"] == "falcon_7"
    async with session_scope() as db:
        mirrored = (await db.execute(select(ScannerSlot.name).order_by(ScannerSlot.user_id))).scalars().all()
        notes = [m.body for m in (await db.execute(select(Outbox).where(Outbox.body.like("%name is set%")))).scalars()]
    assert mirrored == ["user1", "falcon_7"]  # slot rows carry the name (spec schema)
    assert sorted(notes) == ["✅ Your name is set: <b>falcon_7</b>.", "✅ Your name is set: <b>user1</b>."]
    seller = await _a_seller()
    assert (await api.put(f"/api/scanners/{seller}/alias", json={"alias": "nope"})).status_code == 422


async def _a_seller() -> int:
    async with session_scope() as db:
        return (await make_user(db, role="seller")).user_id


async def test_reputation_is_bounded_and_mirrored(api):
    async with session_scope() as db:
        s = await make_scanner(db, alias="user1", slots=[(8, 10)])
    assert (await api.put(f"/api/scanners/{s.user_id}/reputation", json={"value": 87})).json()["reputation"] == 87
    for bad in (-1, 101):
        assert (await api.put(f"/api/scanners/{s.user_id}/reputation", json={"value": bad})).status_code == 422
    async with session_scope() as db:
        assert (await db.execute(select(ScannerSlot.reputation))).scalar_one() == 87


async def test_admin_edits_slots_of_a_scanner(api):
    async with session_scope() as db:
        s = await make_scanner(db, alias="user1", slots=[(8, 10)])
    r = await api.put(f"/api/scanners/{s.user_id}/slots", json={"slots": [{"start": 8, "end": 10}, {"start": 14, "end": 16}, {"start": 22, "end": 0}]})
    assert [x["label"] for x in r.json()["slots"]] == ["08-10", "14-16", "22-00"]
    r = await api.put(f"/api/scanners/{s.user_id}/slots", json={"slots": [{"start": 14, "end": 16}]})
    assert [x["label"] for x in r.json()["slots"]] == ["14-16"]
    assert (await api.put(f"/api/scanners/{s.user_id}/slots", json={"slots": [{"start": 5, "end": 5}]})).status_code == 422
    assert (await api.put(f"/api/scanners/{s.user_id}/slots", json={"slots": [{"start": 5, "end": 99}]})).status_code == 422
    assert (await api.put("/api/scanners/9999/slots", json={"slots": []})).status_code == 404


async def test_scanner_list_shows_names_slots_activity_and_earnings(api):
    async with session_scope() as db:
        seller = await make_user(db, role="seller", name="Sam")
        live = await make_scanner(db, alias="user1", name="Live One", slots=blocks_covering_now("Asia/Kolkata"))
        idle = await make_scanner(db, alias="user2", name="Idle Two", slots=[])
        unnamed = await make_scanner(db, alias=None, name="No Name", slots=[(8, 10)])
        await make_tx(db, seller, live, amount="0.5")
        await make_tx(db, seller, live, amount="0.5")
    everyone = (await api.get("/api/scanners")).json()["items"]
    assert [s["alias"] for s in everyone][:1] == [None]  # unnamed scanners are listed first (action needed)
    by_alias = {s["alias"]: s for s in everyone if s["alias"]}
    assert by_alias["user1"]["active_now"] is True and by_alias["user2"]["active_now"] is False
    assert by_alias["user1"]["completed_tasks"] == 2 and by_alias["user1"]["total_earned"] == "1"
    assert len(by_alias["user1"]["slot_labels"]) == 3 and by_alias["user2"]["slot_count"] == 0
    only_live = (await api.get("/api/scanners?active_now=true")).json()["items"]
    assert [s["alias"] for s in only_live] == ["user1"]
    needs = (await api.get("/api/scanners?needs_name=true")).json()["items"]
    assert [s["user_id"] for s in needs] == [unnamed.user_id]
    assert idle.user_id


# ── slots overview ──────────────────────────────────────────────────────────


async def test_slot_overview_filters_toggle_delete_and_utc_windows(api):
    async with session_scope() as db:
        s1 = await make_scanner(db, alias="user1", slots=[(8, 10)])
        await make_scanner(db, alias="user2", tz="Asia/Karachi", country="PK", slots=blocks_covering_now("Asia/Karachi"))
    rows = (await api.get("/api/slots")).json()
    assert rows["total"] == 4
    ist = next(r for r in rows["items"] if r["alias"] == "user1")
    assert ist["utc_window"] == "02:30-04:30 UTC" and ist["tz_label"] == "IST" and ist["label"] == "08-10"
    assert (await api.get("/api/slots?timezone=Asia/Karachi")).json()["total"] == 3
    assert (await api.get(f"/api/slots?scanner_id={s1.user_id}")).json()["total"] == 1
    live = (await api.get("/api/slots?live=true")).json()
    assert live["total"] >= 1 and all(r["live_now"] for r in live["items"]) and {r["alias"] for r in live["items"]} == {"user2"}

    slot_id = ist["id"]
    off = await api.patch(f"/api/slots/{slot_id}", json={"is_active": False})
    assert off.json()["is_active"] is False and (await api.get("/api/slots?inactive=true")).json()["total"] == 1
    assert (await api.delete(f"/api/slots/{slot_id}")).status_code == 200
    assert (await api.delete(f"/api/slots/{slot_id}")).status_code == 404
    assert (await api.patch("/api/slots/9999", json={"is_active": True})).status_code == 404


async def test_slot_coverage_maps_scanners_to_utc_hours(api):
    async with session_scope() as db:
        await make_scanner(db, alias="user1", slots=[(8, 10)])  # 02:30-04:30 UTC
    cov = (await api.get("/api/slots/coverage")).json()
    assert len(cov["hours"]) == 24 and 0 <= cov["current_hour"] <= 23
    covered = {h["hour"] for h in cov["hours"] if h["scanners"]}
    assert covered == {2, 3, 4}  # sampled at :00 and :30
    assert cov["hours"][3]["scanners"][0]["alias"] == "user1"


# ── dashboard ───────────────────────────────────────────────────────────────


async def test_dashboard_numbers(api):
    now = timeutil.utcnow()
    async with session_scope() as db:
        seller = await make_user(db, role="seller", name="Sam")
        await make_user(db, role="seller", status="pending")
        scanner = await make_scanner(db, alias="user1", slots=blocks_covering_now("Asia/Kolkata"))
        await make_scanner(db, alias=None, slots=[(8, 10)])  # approved but unnamed
        await make_tx(db, seller, scanner, amount="0.5", commission="0.0005", confirmed_at=now)
        await make_tx(db, seller, scanner, amount="0.5", commission="0.0005", confirmed_at=now - dt.timedelta(days=3))
        await make_tx(db, seller, scanner, status="refunded", confirmed_at=now)
    data = (await api.get("/api/dashboard")).json()
    assert data["users"] == {"total": 4, "sellers": 2, "scanners": 2, "approved": 3, "pending": 1, "suspended": 0}
    assert data["pending_approvals"] == 1 and data["scanners_needing_name"] == 1
    assert data["today"]["transactions"] == 1 and data["today"]["volume"] == "0.5"
    assert [d["transactions"] for d in data["series"]][-4:] == [1, 0, 0, 1]
    assert len(data["series"]) == 14 and data["series"][-1]["date"] == now.date().isoformat()
    assert data["active_scanners_now"][0]["alias"] == "user1"
    assert data["ledger"]["ok"] is True and data["system"]["worker_online"] is False
    assert data["system"]["ai_configured"] is False and data["system"]["telegram_admins"] == 2


async def test_dashboard_reports_a_broken_ledger_loudly(api):
    async with session_scope() as db:
        user = await make_user(db, role="seller")
        w = await wallet.get_wallet(db, user.user_id)
        w.balance = 5  # money appears without a ledger entry
    data = (await api.get("/api/dashboard")).json()
    assert data["ledger"]["ok"] is False and data["ledger"]["problems"]


async def test_sidebar_badges(api):
    async with session_scope() as db:
        await make_user(db, role="seller", status="pending")
        await make_scanner(db, alias=None, slots=[(8, 10)])
    assert (await api.get("/api/badges")).json() == {
        "pending_users": 1, "open_disputes": 0, "withdrawals_to_handle": 0, "deposits_to_review": 0, "scanners_needing_name": 1,
    }

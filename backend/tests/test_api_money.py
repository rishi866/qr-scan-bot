"""Admin API: transactions + CSV, disputes + proof viewer, wallets, deposits, withdrawals, reports, settings."""

from __future__ import annotations

import csv
import datetime as dt
import io
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.chain.bsc import AsyncBsc
from app.config import get_settings
from app.db import session_scope
from app.models import Outbox, Withdrawal
from app.services import disputes, payments, proofs, sessions, wallet
from tests.api_helpers import make_tx
from tests.chain_env import make_env
from tests.factories import fund, make_scanner, make_user
from tests.test_ai_proofs import png_bytes
from tests.test_disputes import rejected_task
from tests.test_sessions import NOW

D = dt.datetime


def at(y, m, d, h=12):
    return D(y, m, d, h, 0, tzinfo=dt.UTC)


async def seed_transactions() -> dict:
    async with session_scope() as db:
        sam = await make_user(db, role="seller", name="Sam Seller", country="GB", tz="Europe/London")
        ann = await make_user(db, role="seller", name="Ann Seller", country="US", tz="America/New_York")
        u1 = await make_scanner(db, alias="user1", country="IN", slots=[(8, 10)])
        u2 = await make_scanner(db, alias="user2", tz="Asia/Karachi", country="PK", slots=[(8, 10)])
        await make_tx(db, sam, u1, confirmed_at=at(2026, 9, 1))
        await make_tx(db, sam, u1, confirmed_at=at(2026, 9, 1, 20))
        await make_tx(db, ann, u1, amount="1", commission="0.001", confirmed_at=at(2026, 9, 15))
        await make_tx(db, ann, u2, confirmed_at=at(2026, 10, 2))
        await make_tx(db, sam, u2, status="refunded", confirmed_at=at(2026, 10, 2, 5))
        await make_tx(db, sam, u2, status="disputed")
        return {"sam": sam.user_id, "ann": ann.user_id, "u1": u1.user_id, "u2": u2.user_id}


# ── transactions ────────────────────────────────────────────────────────────


async def test_transactions_filters_totals_and_search(api):
    ids = await seed_transactions()
    allr = (await api.get("/api/transactions")).json()
    assert allr["total"] == 6
    assert allr["totals"] == {"completed_count": 4, "volume": "2.5", "commission": "0.0025"}
    assert (await api.get(f"/api/transactions?scanner_id={ids['u2']}")).json()["total"] == 3
    assert (await api.get(f"/api/transactions?seller_id={ids['ann']}")).json()["total"] == 2
    completed = (await api.get("/api/transactions?status=completed")).json()
    assert completed["total"] == 4 and all(t["status"] == "completed" for t in completed["items"])
    assert (await api.get("/api/transactions?status=refunded")).json()["total"] == 1
    # date range is inclusive of the end day (UTC)
    sept = (await api.get("/api/transactions?date_from=2026-09-01&date_to=2026-09-01")).json()
    assert sept["total"] == 2
    assert (await api.get("/api/transactions?date_from=2026-09-02&date_to=2026-09-30")).json()["total"] == 1
    assert (await api.get("/api/transactions?date_from=nonsense")).status_code == 422
    assert (await api.get("/api/transactions?q=user2")).json()["total"] == 3
    assert (await api.get("/api/transactions?q=Ann")).json()["total"] == 2
    row = completed["items"][0]
    assert {"seller_country", "scanner_country", "scanner_timezone", "slot", "url", "amount", "commission", "confirmed_at"} <= set(row)
    assert row["confirmed_at"].endswith("+00:00")  # UTC


async def test_csv_export_is_complete_and_safe_from_formula_injection(api):
    async with session_scope() as db:
        seller = await make_user(db, role="seller", name='=HYPERLINK("http://evil","x")', country="GB")
        scanner = await make_scanner(db, alias="user1")
        await make_tx(db, seller, scanner, url="https://chatgpt.com/x?a=1,2&b=\"q\"", confirmed_at=at(2026, 9, 1))
        other_seller = await make_user(db, role="seller", name="+cmd|' /C calc'!A0")
        await make_tx(db, other_seller, scanner, confirmed_at=at(2026, 9, 2))
    r = await api.get("/api/transactions/export.csv?date_from=2026-09-01&date_to=2026-09-30")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=\"transactions-" in r.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0][:5] == ["id", "session_id", "status", "amount_usdt", "commission_usdt"] and len(rows) == 3
    names = {row[8] for row in rows[1:]}
    assert names == {"'=HYPERLINK(\"http://evil\",\"x\")", "'+cmd|' /C calc'!A0"}  # neutralised with a leading quote
    assert rows[1][-1] == 'https://chatgpt.com/x?a=1,2&b="q"'  # commas / quotes survive CSV escaping
    assert rows[1][3] == "0.5" and rows[1][5].startswith("2026-09-01T12:00:00")
    filtered = await api.get("/api/transactions/export.csv?status=refunded")
    assert len(list(csv.reader(io.StringIO(filtered.text)))) == 1  # header only


async def test_sessions_monitor(api):
    async with session_scope() as db:
        sam = await make_user(db, role="seller", name="Sam")
        sc = await make_scanner(db, alias="user1")
        await make_tx(db, sam, sc)
        await make_tx(db, sam, sc, status="disputed")
    allr = (await api.get("/api/sessions")).json()
    assert allr["total"] == 2
    active = (await api.get("/api/sessions?active=true")).json()
    assert [s["status"] for s in active["items"]] == ["disputed"]
    item = active["items"][0]
    assert item["scanner"]["alias"] == "user1" and item["seller"]["name"] == "Sam"  # admins see both identities
    assert (await api.get("/api/sessions?status=confirmed")).json()["total"] == 1


# ── disputes ────────────────────────────────────────────────────────────────


async def dispute_with_proof(db, monkeypatch=None):
    seller, scanner, s, dispute = await rejected_task(db)
    path = (await _save_proof(dispute.id))
    await disputes.submit_proof(db, dispute.id, scanner.user_id, proof_file=path, telegram_file_id="f")
    await db.commit()
    return dispute.id, seller, scanner


async def _save_proof(dispute_id: int) -> str:
    return proofs.save_proof(png_bytes(), dispute_id)


async def test_dispute_list_detail_and_proof_viewer(api, db):
    dispute_id, seller, scanner = await dispute_with_proof(db)
    listing = (await api.get("/api/disputes")).json()
    assert listing["total"] == 1
    item = listing["items"][0]
    assert item["status"] == "pending_review" and item["has_proof"] and item["amount"] == "0.5" and item["commission"] == "0.0005"
    assert item["scanner"]["alias"] == "user1" and item["seller"]["name"] == "SellerSecretName"  # admins see both identities
    assert (await api.get("/api/disputes?status=open")).json()["total"] == 1
    assert (await api.get("/api/disputes?status=rejected")).json()["total"] == 0

    detail = (await api.get(f"/api/disputes/{dispute_id}")).json()
    assert detail["id"] == dispute_id and detail["timeline"]["done_at"]
    proof = await api.get(f"/api/disputes/{dispute_id}/proof")
    assert proof.status_code == 200 and proof.headers["content-type"] == "image/png" and proof.content.startswith(b"\x89PNG")
    assert proof.headers["cache-control"] == "private, no-store" and proof.headers["x-content-type-options"] == "nosniff"
    assert (await api.get("/api/disputes/999/proof")).status_code == 404


async def test_proof_viewer_refuses_paths_outside_the_upload_dir(api, db):
    dispute_id, _, _ = await dispute_with_proof(db)
    from app.models import Dispute

    for evil in ("../../../../etc/passwd", "/etc/passwd", "proofs/../../../secret"):
        async with session_scope() as other:
            (await other.execute(select(Dispute))).scalar_one().proof_file = evil
        assert (await api.get(f"/api/disputes/{dispute_id}/proof")).status_code == 404
    async with session_scope() as other:
        (await other.execute(select(Dispute))).scalar_one().proof_file = "proofs/deleted.png"
    assert (await api.get(f"/api/disputes/{dispute_id}/proof")).status_code == 404


async def test_resolve_dispute_both_ways_notes_and_audit(api, db):
    dispute_id, seller, scanner = await dispute_with_proof(db)
    seller_id, scanner_id = seller.user_id, scanner.user_id
    assert (await api.post(f"/api/disputes/{dispute_id}/resolve", json={"resolution": "maybe"})).status_code == 422
    r = await api.post(f"/api/disputes/{dispute_id}/resolve", json={"resolution": "pay_scanner", "notes": "proof looks genuine"})
    body = r.json()
    assert r.status_code == 200 and body["status"] == "rejected" and body["resolution"] == "pay_scanner"
    assert body["admin_notes"] == "proof looks genuine" and body["resolved_by"] == "admin:boss"
    assert (await api.post(f"/api/disputes/{dispute_id}/resolve", json={"resolution": "refund_seller"})).status_code == 409
    async with session_scope() as other:
        assert (await wallet.get_wallet(other, scanner_id)).balance == Decimal("0.5")
        assert (await wallet.get_wallet(other, seller_id)).pending == Decimal("0")
        assert (await wallet.reconcile(other)).ok


async def test_refund_resolution_returns_the_money(api, db):
    dispute_id, seller, scanner = await dispute_with_proof(db)
    seller_id = seller.user_id
    r = await api.post(f"/api/disputes/{dispute_id}/resolve", json={"resolution": "refund_seller"})
    assert r.json()["status"] == "resolved"
    async with session_scope() as other:
        assert (await wallet.get_wallet(other, seller_id)).balance == Decimal("10")


# ── wallets ─────────────────────────────────────────────────────────────────


async def test_wallet_list_detail_ledger_and_adjustments(api):
    async with session_scope() as db:
        rich = await make_user(db, role="seller", name="Rich")
        poor = await make_user(db, role="seller", name="Poor")
        await fund(db, rich, "20")
    wallets = (await api.get("/api/wallets")).json()
    assert [w["name"] for w in wallets["items"]] == ["Rich", "Poor"] and wallets["items"][0]["balance"] == "20"
    assert (await api.get("/api/wallets?non_zero=true")).json()["total"] == 1
    assert (await api.get("/api/wallets?role=scanner")).json()["total"] == 0
    detail = (await api.get(f"/api/wallets/{poor.user_id}")).json()
    assert detail["wallet"]["balance"] == "0" and detail["ledger"]["total"] == 0

    r = await api.post(f"/api/wallets/{poor.user_id}/adjust", json={"amount": "5.25", "reason": "goodwill credit"})
    assert r.json() == {"balance": "5.25", "pending": "0"}
    assert (await api.post(f"/api/wallets/{poor.user_id}/adjust", json={"amount": "-6", "reason": "oops"})).status_code == 409  # cannot overdraw
    assert (await api.post(f"/api/wallets/{poor.user_id}/adjust", json={"amount": "abc", "reason": "oops"})).status_code == 422
    assert (await api.post(f"/api/wallets/{poor.user_id}/adjust", json={"amount": "1", "reason": "x"})).status_code == 422  # reason too short
    ledger = (await api.get(f"/api/wallets/{poor.user_id}")).json()["ledger"]["items"]
    assert ledger[0]["type"] == "adjustment" and "boss" in ledger[0]["note"] and ledger[0]["balance_after"] == "5.25"
    summary = (await api.get("/api/wallets/summary")).json()
    assert summary["by_role"]["seller"]["balance"] == "25.25" and summary["ledger"]["ok"] is True


async def test_deposit_review_actions(api):
    async with session_scope() as db:
        user = await make_user(db, role="seller", name="Dee")
        claim = await payments.create_binance_claim(db, user, Decimal("10"), "M_P_1234567")
        low = await payments.credit_onchain_deposit(db, user_id=user.user_id, amount=Decimal("0.4"), tx_hash="0xlow", log_index=0, address="0xa", block_number=1)
        rejected = await payments.create_binance_claim(db, user, Decimal("3"), "M_P_7654321")
        ids = (claim.id, low.id, rejected.id, user.user_id)
    to_review = (await api.get("/api/deposits?needs_review=true")).json()
    assert to_review["total"] == 3
    assert (await api.get("/api/deposits?method=bep20")).json()["total"] == 1
    assert (await api.get("/api/deposits?q=0xlow")).json()["total"] == 1

    ok = await api.post(f"/api/deposits/{ids[0]}/confirm", json={"amount": "9.5", "note": "verified in Binance app"})
    assert ok.json()["status"] == "credited" and ok.json()["amount"] == "9.5"
    assert (await api.post(f"/api/deposits/{ids[0]}/confirm", json={})).status_code == 409  # not twice
    assert (await api.post(f"/api/deposits/{ids[1]}/confirm", json={})).json()["status"] == "credited"  # below-min, admin decides
    assert (await api.post(f"/api/deposits/{ids[2]}/reject", json={"note": "not found in Binance"})).json()["status"] == "rejected"
    assert (await api.post("/api/deposits/999/confirm", json={})).status_code == 409
    assert (await api.post(f"/api/deposits/{ids[1]}/confirm", json={"amount": "abc"})).status_code == 422
    async with session_scope() as db:
        assert (await wallet.get_wallet(db, ids[3])).balance == Decimal("9.9")
        assert (await wallet.reconcile(db)).ok


async def test_withdrawal_admin_workflow(api):
    async with session_scope() as db:
        scanner = await make_scanner(db, alias="user1", name="Sara")
        await fund(db, scanner, "20")
        w1 = await payments.request_withdrawal(db, scanner, "bep20", "0x70997970C51812dc3A010C7d01b50e0d17dc79C8", Decimal("5"))
        sid, w1_id = scanner.user_id, w1.id
    listing = (await api.get("/api/withdrawals?to_handle=true")).json()
    assert listing["total"] == 1 and listing["items"][0]["status"] == "pending" and listing["items"][0]["auto_payout"] is False
    assert listing["items"][0]["alias"] == "user1" and listing["items"][0]["net"] == "5"

    assert (await api.post(f"/api/withdrawals/{w1_id}/approve", json={})).json()["status"] == "approved"
    assert (await api.post(f"/api/withdrawals/{w1_id}/approve", json={})).status_code == 409
    paid = await api.post(f"/api/withdrawals/{w1_id}/mark-paid", json={"tx_hash": "0xmanual"})
    assert paid.json()["status"] == "completed" and paid.json()["tx_hash"] == "0xmanual" and paid.json()["processed_by"] == "admin:boss"
    assert (await api.post(f"/api/withdrawals/{w1_id}/reject", json={"note": "late"})).status_code == 409

    async with session_scope() as db:
        scanner = await make_user(db, role="scanner", alias="user9", name="Sven")
        await fund(db, scanner, "3")
        second = await payments.request_withdrawal(db, scanner, "binance", "12345678", Decimal("3"))
        second_id, second_uid = second.id, scanner.user_id
    rej = await api.post(f"/api/withdrawals/{second_id}/reject", json={"note": "unknown account"})
    assert rej.json()["status"] == "rejected"
    async with session_scope() as db:
        w = await wallet.get_wallet(db, second_uid)
        assert (w.balance, w.pending) == (Decimal("3"), Decimal("0"))  # funds returned
        assert (await wallet.get_wallet(db, sid)).total_withdrawn == Decimal("5")
        assert (await wallet.reconcile(db)).ok
        stuck = (await db.execute(select(Withdrawal).where(Withdrawal.id == second_id))).scalar_one()
        stuck.status = "failed"
    assert (await api.post(f"/api/withdrawals/{second_id}/retry")).json()["status"] == "approved"
    assert (await api.post(f"/api/withdrawals/{w1_id}/retry")).status_code == 409


async def test_chain_status_without_and_with_an_rpc(api, monkeypatch):
    from app.api.routers import wallets as wallets_router

    env = make_env()
    monkeypatch.setattr(wallets_router, "async_client_from_settings", lambda: env.chain)
    data = (await api.get("/api/chain/status")).json()
    assert data["rpc"]["ok"] is True and data["rpc"]["latest_block"] >= 5
    assert data["deposits"]["configured"] is False and data["hot_wallet"]["configured"] is False
    assert data["auto_payout"]["enabled"] is False and data["token"]["symbol"] == "USDT"

    class Broken(AsyncBsc):
        async def check_chain(self):
            raise ConnectionError("rpc down")

    monkeypatch.setattr(wallets_router, "async_client_from_settings", lambda: Broken(env.client))
    data = (await api.get("/api/chain/status")).json()
    assert data["rpc"]["ok"] is False and "rpc down" in data["rpc"]["error"]  # the page still renders


# ── reports ─────────────────────────────────────────────────────────────────


async def test_reports(api):
    await seed_transactions()
    q = "date_from=2026-09-01&date_to=2026-10-31"
    daily = (await api.get(f"/api/reports/summary?group=day&{q}")).json()
    assert [r["period"] for r in daily["items"]] == ["2026-09-01", "2026-09-15", "2026-10-02"]
    assert daily["items"][0]["completed"] == 2 and daily["items"][0]["volume"] == "1"
    assert daily["items"][2]["refunded"] == 1 and daily["totals"] == {"completed": 4, "volume": "2.5", "commission": "0.0025"}
    monthly = (await api.get(f"/api/reports/summary?group=month&{q}")).json()
    assert [r["period"] for r in monthly["items"]] == ["2026-09", "2026-10"] and monthly["items"][0]["completed"] == 3
    assert (await api.get("/api/reports/summary?group=week")).status_code == 422

    by_scanner_country = (await api.get(f"/api/reports/by-country?side=scanner&{q}")).json()["items"]
    assert [(r["country"], r["tasks"]) for r in by_scanner_country] == [("IN", 3), ("PK", 1)]
    assert by_scanner_country[0]["country_name"] == "India"
    by_seller_country = (await api.get(f"/api/reports/by-country?side=seller&{q}")).json()["items"]
    assert {r["country"] for r in by_seller_country} == {"GB", "US"}

    scanners = (await api.get(f"/api/reports/by-scanner?{q}")).json()["items"]
    assert [(r["alias"], r["tasks"], r["earned"]) for r in scanners][:2] == [("user1", 3, "2"), ("user2", 1, "0.5")]
    assert (scanners[1]["refunded"], scanners[1]["disputed"]) == (1, 1)
    sellers = (await api.get(f"/api/reports/by-seller?{q}")).json()["items"]
    assert sellers[0]["tasks"] >= 1 and {"spent", "refunded", "disputed"} <= set(sellers[0])
    commission = (await api.get("/api/reports/commission?group=day")).json()
    assert commission["items"] == [] and commission["all_time_total"] == "0"  # the ledger only has real settlements

    csv_resp = await api.get(f"/api/reports/summary?group=day&format=csv&{q}")
    rows = list(csv.reader(io.StringIO(csv_resp.text)))
    assert csv_resp.headers["content-type"].startswith("text/csv") and rows[0] == ["period", "completed", "volume", "commission", "refunded", "disputed"]
    assert len(rows) == 4


async def test_commission_report_uses_the_ledger(api, db):
    from tests.test_sessions import start_task

    seller, scanner, s = await start_task(db)
    await sessions.accept(db, s.session_id, scanner.user_id, now=NOW)
    await sessions.mark_done(db, s.session_id, scanner.user_id, now=NOW)
    await sessions.process_prompt(db, s.session_id, NOW + dt.timedelta(minutes=3))
    await sessions.confirm(db, s.session_id, seller.user_id, now=NOW + dt.timedelta(minutes=4))
    await db.commit()
    today = (await api.get("/api/reports/commission?group=month")).json()
    assert today["all_time_total"] == "0.0005" and today["items"][-1]["task_commission"] == "0.0005"
    assert today["items"][-1]["cumulative"] == "0.0005" and today["items"][-1]["withdrawal_fees"] == "0"


# ── settings, broadcast, audit ──────────────────────────────────────────────


async def test_settings_roundtrip_validation_and_audit(api):
    data = (await api.get("/api/settings")).json()
    keys = {d["key"] for d in data["definitions"]}
    assert {"commission_percent", "task_amount", "slot_duration_hours", "url_response_timeout_seconds", "allowed_url_domains"} <= keys
    assert data["values"]["commission_percent"] == "0.1" and data["values"]["slot_duration_hours"] == 2

    r = await api.put("/api/settings", json={"values": {"commission_percent": "0.25", "slot_duration_hours": 4, "support_contact": "@helpdesk"}})
    assert r.status_code == 200 and set(r.json()["changed"]) == {"commission_percent", "slot_duration_hours", "support_contact"}
    assert (await api.get("/api/settings")).json()["values"]["commission_percent"] == "0.25"
    assert (await api.put("/api/settings", json={"values": {"slot_duration_hours": 5}})).status_code == 422
    assert (await api.put("/api/settings", json={"values": {"commission_percent": "-1"}})).status_code == 422
    assert (await api.put("/api/settings", json={"values": {"nope": 1}})).status_code == 422
    assert (await api.put("/api/settings", json={"values": {}})).status_code == 422
    audit = (await api.get("/api/audit?action=settings_updated")).json()
    assert audit["total"] == 1 and audit["items"][0]["admin"] == "boss"
    assert audit["items"][0]["details"]["commission_percent"] == {"from": "0.1", "to": "0.25"}
    assert (await api.get("/api/audit?admin=ghost")).json()["total"] == 0


async def test_broadcast_goes_to_the_right_audience_once(api):
    async with session_scope() as db:
        s1 = await make_user(db, role="seller", name="S1")
        await make_user(db, role="seller", status="pending")  # not approved -> excluded
        blocked = await make_user(db, role="seller")
        blocked.bot_blocked = True
        sc = await make_scanner(db, alias="user1")
        ids = (s1.telegram_id, sc.telegram_id)
    assert (await api.get("/api/broadcasts/preview?audience=sellers")).json() == {"recipients": 1}
    assert (await api.get("/api/broadcasts/preview?audience=all")).json() == {"recipients": 2}
    body = {"message": "Maintenance <b>tonight</b> & more", "audience": "sellers", "confirm": True}
    assert (await api.post("/api/broadcasts", json={**body, "confirm": False})).status_code == 422  # must be confirmed
    r = await api.post("/api/broadcasts", json=body)
    assert r.status_code == 201 and r.json()["total"] == 1
    async with session_scope() as db:
        rows = (await db.execute(select(Outbox).where(Outbox.broadcast_id == r.json()["id"]))).scalars().all()
    assert [m.chat_id for m in rows] == [ids[0]]
    assert "Maintenance &lt;b&gt;tonight&lt;/b&gt; &amp; more" in rows[0].body  # admin text can never inject markup
    history = (await api.get("/api/broadcasts")).json()["items"][0]
    assert history["total"] == 1 and history["queued"] == 1 and history["sent"] == 0 and history["audience"] == "sellers"
    to_scanners = await api.post("/api/broadcasts", json={"message": "x", "audience": "scanners", "confirm": True})
    assert to_scanners.status_code == 201 and to_scanners.json()["total"] == 1
    async with session_scope() as db:
        chats = (await db.execute(select(Outbox.chat_id).where(Outbox.broadcast_id == to_scanners.json()["id"]))).scalars().all()
    assert chats == [ids[1]]


@pytest.fixture(autouse=True)
def _upload_dir_is_clean():
    yield
    root = get_settings().upload_dir
    import shutil

    shutil.rmtree(f"{root}/proofs", ignore_errors=True)

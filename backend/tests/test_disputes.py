from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.enums import DisputeResolution, DisputeStatus, SessionStatus
from app.models import Dispute, Outbox, TransactionHistory
from app.services import disputes, sessions, settings_service, users, wallet
from tests.test_sessions import COST, NOW, balances, messages, start_task

LATER = NOW + dt.timedelta(minutes=10)


async def rejected_task(db):
    """seller + scanner with a task the seller has rejected."""
    seller, scanner, s = await start_task(db)
    await sessions.accept(db, s.session_id, scanner.user_id, now=NOW)
    await sessions.mark_done(db, s.session_id, scanner.user_id, now=NOW)
    await sessions.process_prompt(db, s.session_id, NOW + dt.timedelta(minutes=3))
    await db.commit()
    s, dispute = await sessions.reject(db, s.session_id, seller.user_id, now=NOW + dt.timedelta(minutes=4))
    await db.commit()
    return seller, scanner, s, dispute


async def admin_messages(db) -> list[Outbox]:
    return list((await db.execute(select(Outbox).where(Outbox.chat_id.in_([9001, 9002])).order_by(Outbox.id))).scalars())


async def test_reject_opens_dispute_and_freezes_funds(db):
    seller, scanner, s, dispute = await rejected_task(db)
    assert s.status == SessionStatus.DISPUTED.value
    assert dispute.status == DisputeStatus.AWAITING_PROOF.value
    assert await balances(db, seller) == (Decimal("10") - COST, COST)  # still held, not refunded, not paid
    assert await balances(db, scanner) == (Decimal("0"), Decimal("0"))
    tx = (await db.execute(select(TransactionHistory))).scalar_one()
    assert tx.status == "disputed" and tx.confirmed_at is None

    # the scanner is asked for a screenshot (and only that - no seller identity), the bot waits for it
    scanner_msgs = [m.body for m in await messages(db, scanner.telegram_id)]
    assert any("screenshot" in m for m in scanner_msgs)
    scanner = await users.require_user(db, scanner.user_id)
    assert scanner.state == disputes.STATE_AWAITING_PROOF and scanner.state_data == {"dispute_id": dispute.id}
    seller = await users.require_user(db, seller.user_id)
    assert seller.state == disputes.STATE_DISPUTE_NOTE
    assert (await wallet.reconcile(db)).ok


async def test_proof_submission_escalates_to_admin_when_ai_is_off(db):
    seller, scanner, s, dispute = await rejected_task(db)
    d = await disputes.submit_proof(db, dispute.id, scanner.user_id, proof_file="proofs/1.jpg", telegram_file_id="abc")
    await db.commit()
    assert d.status == DisputeStatus.PENDING_REVIEW.value and d.ai_status == "skipped"
    assert d.proof_file == "proofs/1.jpg"
    msgs = await admin_messages(db)
    assert len(msgs) == 2 and all("needs your review" in m.body for m in msgs)  # both admins notified once
    assert (await users.require_user(db, scanner.user_id)).state is None  # no longer waiting for a photo
    # a second upload is refused
    with pytest.raises(disputes.DisputeError):
        await disputes.submit_proof(db, dispute.id, scanner.user_id, proof_file="proofs/2.jpg", telegram_file_id=None)
    await db.rollback()


async def test_only_the_scanner_of_the_task_can_upload_proof(db):
    seller, scanner, s, dispute = await rejected_task(db)
    with pytest.raises(disputes.DisputeError):
        await disputes.submit_proof(db, dispute.id, seller.user_id, proof_file="x.jpg", telegram_file_id=None)
    await db.rollback()


async def test_admin_pays_scanner_rejecting_the_dispute(db):
    seller, scanner, s, dispute = await rejected_task(db)
    await disputes.submit_proof(db, dispute.id, scanner.user_id, proof_file="p.jpg", telegram_file_id=None)
    await db.commit()
    d = await disputes.resolve(db, dispute.id, DisputeResolution.PAY_SCANNER, by="admin1", notes="screenshot is clear")
    await db.commit()
    assert d.status == DisputeStatus.REJECTED.value and d.resolution == "pay_scanner" and d.resolved_by == "admin1"
    assert d.admin_notes == "screenshot is clear"
    assert (await sessions.get_session(db, s.session_id)).status == SessionStatus.CONFIRMED.value
    assert await balances(db, scanner) == (Decimal("0.5"), Decimal("0"))
    assert await balances(db, seller) == (Decimal("10") - COST, Decimal("0"))
    tx = (await db.execute(select(TransactionHistory))).scalar_one()
    assert tx.status == "completed" and tx.confirmed_at is not None
    assert (await users.require_user(db, scanner.user_id)).reputation == 51
    assert any("payment stands" in m.body for m in await messages(db, seller.telegram_id))
    assert (await wallet.reconcile(db)).ok


async def test_admin_refunds_seller_upholding_the_dispute(db):
    seller, scanner, s, dispute = await rejected_task(db)
    d = await disputes.resolve(db, dispute.id, DisputeResolution.REFUND_SELLER, by="admin1")
    await db.commit()
    assert d.status == DisputeStatus.RESOLVED.value and d.resolution == "refund_seller"
    assert (await sessions.get_session(db, s.session_id)).status == SessionStatus.REFUNDED.value
    assert await balances(db, seller) == (Decimal("10"), Decimal("0"))
    assert await balances(db, scanner) == (Decimal("0"), Decimal("0"))
    assert (await db.execute(select(TransactionHistory))).scalar_one().status == "refunded"
    assert (await users.require_user(db, scanner.user_id)).reputation == 45
    assert any("resolved in your favour" in m.body for m in await messages(db, seller.telegram_id))
    assert (await wallet.reconcile(db)).ok


async def test_dispute_cannot_be_resolved_twice(db):
    _, _, _, dispute = await rejected_task(db)
    await disputes.resolve(db, dispute.id, DisputeResolution.REFUND_SELLER, by="a")
    await db.commit()
    with pytest.raises(disputes.DisputeError):
        await disputes.resolve(db, dispute.id, DisputeResolution.PAY_SCANNER, by="a")
    await db.rollback()


async def test_no_proof_in_time_goes_to_admin(db):
    seller, scanner, s, dispute = await rejected_task(db)
    assert await disputes.due_proof_timeouts(db, NOW + dt.timedelta(minutes=20)) == []
    due = await disputes.due_proof_timeouts(db, NOW + dt.timedelta(minutes=40))
    assert due == [dispute.id]
    assert not await disputes.process_proof_timeout(db, dispute.id, NOW + dt.timedelta(minutes=20))
    assert await disputes.process_proof_timeout(db, dispute.id, NOW + dt.timedelta(minutes=40))
    await db.commit()
    d = (await db.execute(select(Dispute))).scalar_one()
    assert d.status == DisputeStatus.PENDING_REVIEW.value and d.proof_file is None
    assert any("No screenshot" in m.body for m in await admin_messages(db))
    assert (await users.require_user(db, scanner.user_id)).state is None


async def test_seller_note_is_added_to_the_case_only_once_open(db):
    seller, _, _, dispute = await rejected_task(db)
    assert await disputes.add_seller_note(db, dispute.id, seller.user_id, "the page showed an error")
    await db.commit()
    d = (await db.execute(select(Dispute))).scalar_one()
    assert "the page showed an error" in d.reason
    assert not await disputes.add_seller_note(db, dispute.id, seller.user_id + 999, "hax")  # not the seller


async def _pending_review(db, monkeypatch):
    """A dispute whose proof arrived while the AI verifier is configured (admins are told only after the AI)."""
    monkeypatch.setattr(disputes, "ai_is_configured", lambda: True)
    seller, scanner, s, dispute = await rejected_task(db)
    d = await disputes.submit_proof(db, dispute.id, scanner.user_id, proof_file="p.jpg", telegram_file_id=None)
    assert d.ai_status == "pending"
    await db.commit()
    assert await admin_messages(db) == []
    return seller, scanner, s, dispute


async def test_ai_clear_positive_auto_pays_scanner(db, monkeypatch):
    _, scanner, s, dispute = await _pending_review(db, monkeypatch)
    verdict = {"verdict": "completed", "confidence": 0.95, "reason": "ChatGPT success page visible"}
    assert await disputes.apply_ai_result(db, dispute.id, verdict) == "auto_pay"
    await db.commit()
    d = (await db.execute(select(Dispute))).scalar_one()
    assert d.status == DisputeStatus.REJECTED.value and d.resolved_by == "ai" and d.ai_status == "done"
    assert await balances(db, scanner) == (Decimal("0.5"), Decimal("0"))
    assert (await wallet.reconcile(db)).ok


async def test_ai_unsure_or_low_confidence_escalates_to_admin(db, monkeypatch):
    _, scanner, s, dispute = await _pending_review(db, monkeypatch)
    assert await disputes.apply_ai_result(db, dispute.id, {"verdict": "completed", "confidence": 0.6, "reason": "blurry"}) == "escalated"
    await db.commit()
    d = (await db.execute(select(Dispute))).scalar_one()
    assert d.status == DisputeStatus.PENDING_REVIEW.value and d.ai_verdict["verdict"] == "completed"
    assert any("needs your review" in m.body and "blurry" in m.body for m in await admin_messages(db))
    assert await balances(db, scanner) == (Decimal("0"), Decimal("0"))


async def test_ai_negative_only_refunds_when_enabled(db, monkeypatch):
    seller, scanner, s, dispute = await _pending_review(db, monkeypatch)
    verdict = {"verdict": "not_chatgpt", "confidence": 0.97, "reason": "a photo of a cat"}
    # default: auto-refund is off -> admin decides
    assert await disputes.apply_ai_result(db, dispute.id, verdict) == "escalated"
    await db.commit()
    await settings_service.save(db, {"ai_auto_refund": True}, "t")
    await db.commit()
    d = (await db.execute(select(Dispute))).scalar_one()
    d.ai_status = "pending"
    await db.commit()
    assert await disputes.apply_ai_result(db, dispute.id, verdict) == "auto_refund"
    await db.commit()
    assert await balances(db, seller) == (Decimal("10"), Decimal("0"))
    assert (await wallet.reconcile(db)).ok


async def test_ai_failure_escalates(db, monkeypatch):
    _, _, _, dispute = await _pending_review(db, monkeypatch)
    assert await disputes.apply_ai_result(db, dispute.id, None, error="timeout") == "escalated"
    await db.commit()
    d = (await db.execute(select(Dispute))).scalar_one()
    assert d.ai_status == "error"
    assert any("manual review" in m.body for m in await admin_messages(db))

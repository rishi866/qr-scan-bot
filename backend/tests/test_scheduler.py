from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

from app import states
from app.bot import delivery
from app.config import get_settings
from app.db import session_scope
from app.enums import DisputeStatus, SessionStatus
from app.models import Dispute, Outbox, SlotNotification, User
from app.services import disputes, outbox, scheduler, sessions, wallet
from app.services import users as users_svc
from tests.factories import make_scanner, make_user
from tests.test_disputes import rejected_task
from tests.test_sessions import NOW, balances, messages, start_task

UTC = dt.UTC


def at(*a):
    return dt.datetime(*a, tzinfo=UTC)


# 08-10 IST starts at 02:30Z
SLOT_START = at(2026, 10, 2, 2, 30)


# ── pre-slot reminders ──────────────────────────────────────────────────────


async def test_pre_slot_reminder_is_sent_ten_minutes_before_once(db):
    scanner = await make_scanner(db, alias="user1", slots=[(8, 10)])
    await db.commit()
    assert await scheduler.tick_slot_notifications(SLOT_START - dt.timedelta(minutes=11)) == 0
    assert await scheduler.tick_slot_notifications(SLOT_START - dt.timedelta(minutes=9, seconds=30)) == 1
    assert await scheduler.tick_slot_notifications(SLOT_START - dt.timedelta(minutes=5)) == 0  # dedupe
    msgs = await messages(db, scanner.telegram_id)
    assert len(msgs) == 1
    body = msgs[0].body
    assert "starts in 10 minutes" in body and "08-10" in body and "IST" in body and "Be ready" in body
    buttons = [b["text"] for row in msgs[0].reply_markup["inline_keyboard"] for b in row]
    assert buttons == ["✅ Ready", "❌ Busy"]
    # next day's occurrence is reminded again
    assert await scheduler.tick_slot_notifications(SLOT_START + dt.timedelta(days=1, minutes=-8)) == 1


async def test_pre_slot_reminder_skips_inactive_and_unapproved(db):
    await make_scanner(db, alias="user1", slots=[(8, 10)], status="pending")
    await make_scanner(db, alias="user2", slots=[(8, 10)], status="suspended")
    await db.commit()
    assert await scheduler.tick_slot_notifications(SLOT_START - dt.timedelta(minutes=5)) == 0


async def test_each_slot_of_a_scanner_gets_its_own_reminder(db):
    scanner = await make_scanner(db, alias="user1", slots=[(8, 10), (10, 12)])
    await db.commit()
    assert await scheduler.tick_slot_notifications(SLOT_START - dt.timedelta(minutes=5)) == 1
    assert await scheduler.tick_slot_notifications(SLOT_START + dt.timedelta(hours=2) - dt.timedelta(minutes=5)) == 1
    rows = (await db.execute(select(SlotNotification))).scalars().all()
    assert len(rows) == 2 and {r.user_id for r in rows} == {scanner.user_id}


async def test_reminder_respects_the_configurable_notice_period(db):
    from app.services import settings_service

    await settings_service.save(db, {"pre_slot_notice_minutes": 30}, "t")
    scanner = await make_scanner(db, alias="user1", slots=[(8, 10)])
    await db.commit()
    assert await scheduler.tick_slot_notifications(SLOT_START - dt.timedelta(minutes=25)) == 1
    assert "starts in 25 minutes" in (await messages(db, scanner.telegram_id))[0].body


# ── conversation state expiry ───────────────────────────────────────────────


async def test_expired_send_window_is_cleared_and_announced(db):
    seller = await make_user(db, role="seller")
    other = await make_user(db, role="seller")
    await users_svc.set_state(db, seller, states.AWAITING_URL, None, 120, now=NOW)
    await users_svc.set_state(db, other, states.DISPUTE_NOTE, {"dispute_id": 1}, 120, now=NOW)
    await db.commit()
    assert await scheduler.tick_states(NOW + dt.timedelta(seconds=60)) == 0
    assert await scheduler.tick_states(NOW + dt.timedelta(seconds=121)) == 2
    seller_row = await users_svc.require_user(db, seller.user_id)
    assert seller_row.state is None
    assert any("Time's up" in m.body for m in await messages(db, seller.telegram_id))
    assert await messages(db, other.telegram_id) == []  # silent for non-/send states


# ── session timers through the scheduler ────────────────────────────────────


async def test_tick_sessions_runs_every_timer(db):
    seller, scanner, s = await start_task(db)
    sid = s.session_id
    result = await scheduler.tick_sessions(NOW + dt.timedelta(minutes=3))
    assert result["expired"] == 1
    assert (await sessions.get_session(db, sid)).status == SessionStatus.EXPIRED.value
    assert await balances(db, seller) == (Decimal("10"), Decimal("0"))
    # nothing left to do
    assert sum((await scheduler.tick_sessions(NOW + dt.timedelta(minutes=4))).values()) == 0


async def test_tick_sessions_prompts_then_auto_confirms(db):
    seller, scanner, s = await start_task(db)
    await sessions.accept(db, s.session_id, scanner.user_id, now=NOW)
    await sessions.mark_done(db, s.session_id, scanner.user_id, now=NOW)
    await db.commit()
    assert (await scheduler.tick_sessions(NOW + dt.timedelta(minutes=1)))["prompted"] == 0
    assert (await scheduler.tick_sessions(NOW + dt.timedelta(minutes=3)))["prompted"] == 1
    assert (await scheduler.tick_sessions(NOW + dt.timedelta(minutes=70)))["auto_confirmed"] == 1
    assert await balances(db, scanner) == (Decimal("0.5"), Decimal("0"))
    assert (await wallet.reconcile(db)).ok


async def test_tick_disputes_escalates_missing_proof(db):
    seller, scanner, s, dispute = await rejected_task(db)
    assert (await scheduler.tick_disputes(NOW + dt.timedelta(minutes=10)))["proof_timeouts"] == 0
    assert (await scheduler.tick_disputes(NOW + dt.timedelta(minutes=40)))["proof_timeouts"] == 1
    d = (await db.execute(select(Dispute).execution_options(populate_existing=True))).scalar_one()
    assert d.status == DisputeStatus.PENDING_REVIEW.value


# ── AI verification tick ────────────────────────────────────────────────────


def _store_proof(name: str = "proofs/d1.jpg") -> str:
    path = Path(get_settings().upload_dir) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8\xff fake jpeg bytes")
    return name


async def _pending_ai_dispute(db, monkeypatch):
    monkeypatch.setattr(disputes, "ai_is_configured", lambda: True)
    seller, scanner, s, dispute = await rejected_task(db)
    scanner_id, dispute_id = scanner.user_id, dispute.id
    await disputes.submit_proof(db, dispute_id, scanner_id, proof_file=_store_proof(), telegram_file_id="x")
    await db.commit()
    return scanner_id, dispute_id


async def test_ai_tick_pays_scanner_on_a_clear_positive(db, monkeypatch):
    scanner_id, dispute_id = await _pending_ai_dispute(db, monkeypatch)
    seen = {}

    async def verify(data: bytes, media_type: str):
        seen["bytes"], seen["type"] = data, media_type
        return {"verdict": "completed", "confidence": 0.97, "reason": "success page"}

    stats = await scheduler.tick_ai(verify)
    assert stats["auto_pay"] == 1 and seen["type"] == "image/jpeg" and seen["bytes"].startswith(b"\xff\xd8")
    d = (await db.execute(select(Dispute).execution_options(populate_existing=True))).scalar_one()
    assert d.status == DisputeStatus.REJECTED.value and d.resolved_by == "ai"
    assert await balances(db, scanner_id) == (Decimal("0.5"), Decimal("0"))
    assert (await scheduler.tick_ai(verify)) == {"auto_pay": 0, "auto_refund": 0, "escalated": 0}  # nothing pending any more


async def test_ai_tick_escalates_when_the_verifier_crashes(db, monkeypatch):
    _, dispute_id = await _pending_ai_dispute(db, monkeypatch)

    async def verify(data, media_type):
        raise RuntimeError("API down")

    stats = await scheduler.tick_ai(verify)
    assert stats["escalated"] == 1
    d = (await db.execute(select(Dispute).execution_options(populate_existing=True))).scalar_one()
    assert d.status == DisputeStatus.PENDING_REVIEW.value and d.ai_status == "error"
    assert "RuntimeError" in d.ai_verdict["error"]


async def test_interrupted_ai_runs_are_requeued(db, monkeypatch):
    _, dispute_id = await _pending_ai_dispute(db, monkeypatch)
    async with session_scope() as other:
        (await other.execute(select(Dispute))).scalar_one().ai_status = "running"
    await scheduler.reset_running_ai()
    d = (await db.execute(select(Dispute).execution_options(populate_existing=True))).scalar_one()
    assert d.ai_status == "pending"


def test_proof_path_cannot_escape_the_upload_dir():
    assert scheduler.proof_path("proofs/a.jpg").name == "a.jpg"
    for bad in ("../../etc/passwd", "/etc/passwd", "proofs/../../outside.jpg"):
        with pytest.raises(ValueError):
            scheduler.proof_path(bad)


# ── outbox delivery ─────────────────────────────────────────────────────────


class FakeBot:
    def __init__(self, behaviour=None):
        self.sent: list[dict] = []
        self.behaviour = behaviour or {}

    async def send_message(self, **kwargs):
        action = self.behaviour.get(kwargs["chat_id"])
        if callable(action):
            action = action(kwargs)
        if isinstance(action, Exception):
            raise action
        self.sent.append(kwargs)


@pytest.fixture(autouse=True)
def _fast_delivery(monkeypatch):
    monkeypatch.setattr(delivery, "SEND_INTERVAL", 0)


async def _queue(db, chat_id, text="hello", **kw):
    row_id = await outbox.enqueue(db, chat_id, text, **kw)
    await db.commit()
    return row_id


async def _row(db, row_id) -> Outbox:
    return (await db.execute(select(Outbox).where(Outbox.id == row_id).execution_options(populate_existing=True))).scalar_one()


async def test_delivery_sends_in_order_with_buttons_and_no_link_preview(db):
    first = await _queue(db, 111, "<b>one</b>", buttons=[[("Yes", "y:1"), ("Open", {"url": "https://example.com"})]])
    second = await _queue(db, 222, "two")
    bot = FakeBot()
    assert await delivery.deliver_due(bot) == 2
    assert [m["chat_id"] for m in bot.sent] == [111, 222]
    msg = bot.sent[0]
    assert msg["parse_mode"] == "HTML" and msg["link_preview_options"].is_disabled is True
    keyboard = msg["reply_markup"].inline_keyboard[0]
    assert keyboard[0].callback_data == "y:1" and keyboard[1].url == "https://example.com"
    assert (await _row(db, first)).status == "sent" and (await _row(db, second)).sent_at is not None
    assert await delivery.deliver_due(bot) == 0  # nothing is sent twice


async def test_dedupe_key_prevents_duplicate_messages(db):
    a = await _queue(db, 1, "x", dedupe_key="k1")
    b = await _queue(db, 1, "x", dedupe_key="k1")
    assert a is not None and b is None
    bot = FakeBot()
    assert await delivery.deliver_due(bot) == 1


async def test_blocked_user_marks_failed_and_flags_the_user(db):
    user = await make_user(db, role="seller")
    await db.commit()
    row_id = await _queue(db, user.telegram_id)
    await delivery.deliver_due(FakeBot({user.telegram_id: Forbidden("bot was blocked by the user")}))
    row = await _row(db, row_id)
    assert row.status == "failed" and "Forbidden" in row.last_error
    assert (await db.execute(select(User.bot_blocked).where(User.user_id == user.user_id))).scalar_one() is True


async def test_rate_limit_and_network_errors_are_retried_later(db):
    flood = await _queue(db, 1)
    net = await _queue(db, 2)
    bot = FakeBot({1: RetryAfter(7), 2: TimedOut("timeout")})
    assert await delivery.deliver_due(bot) == 0
    f, n = await _row(db, flood), await _row(db, net)
    assert f.status == "queued" and f.next_attempt_at > dt.datetime.now(UTC) + dt.timedelta(seconds=5)
    assert n.status == "queued" and n.attempts == 1 and n.next_attempt_at > dt.datetime.now(UTC)
    # not due yet -> not attempted again
    assert await delivery.deliver_due(FakeBot()) == 0


async def test_gives_up_after_max_attempts(db):
    row_id = await _queue(db, 5)
    for _ in range(delivery.MAX_ATTEMPTS):
        async with session_scope() as other:
            row = (await other.execute(select(Outbox).where(Outbox.id == row_id))).scalar_one()
            row.next_attempt_at = dt.datetime.now(UTC) - dt.timedelta(seconds=1)
        await delivery.deliver_due(FakeBot({5: NetworkError("down")}))
    row = await _row(db, row_id)
    assert row.status == "failed" and row.attempts == delivery.MAX_ATTEMPTS


async def test_html_parse_errors_degrade_to_plain_text_instead_of_losing_the_message(db):
    row_id = await _queue(db, 9, "<b>Bold</b> &amp; text")

    def behaviour(kwargs):
        return BadRequest("Can't parse entities: unsupported start tag") if kwargs["parse_mode"] else None

    bot = FakeBot({9: behaviour})
    assert await delivery.deliver_due(bot) == 1
    assert bot.sent[-1]["parse_mode"] is None and bot.sent[-1]["text"] == "Bold & text"
    assert (await _row(db, row_id)).status == "sent"


async def test_bad_request_is_final(db):
    row_id = await _queue(db, 3)
    await delivery.deliver_due(FakeBot({3: BadRequest("Chat not found")}))
    assert (await _row(db, row_id)).status == "failed"


async def test_admin_notifications_go_to_every_configured_admin(db):
    queued = await outbox.notify_admins(db, "hello admins", dedupe_key="x")
    await db.commit()
    assert queued == 2
    assert await outbox.notify_admins(db, "hello admins", dedupe_key="x") == 0  # deduped per admin
    rows = (await db.execute(select(Outbox.chat_id).order_by(Outbox.chat_id))).scalars().all()
    assert rows == [9001, 9002]

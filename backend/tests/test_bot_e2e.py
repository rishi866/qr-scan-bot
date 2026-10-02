"""End-to-end: the real bot application, the real services and a real database against a fake Telegram."""

from __future__ import annotations

import datetime as dt
import io
from decimal import Decimal

import pytest
from PIL import Image
from pydantic import SecretStr
from sqlalchemy import select

from app import timeutil
from app.bot import delivery
from app.bot.handlers import common
from app.config import get_settings
from app.db import session_scope
from app.enums import DisputeResolution
from app.models import Dispute, Outbox, TaskSession, TransactionHistory, User
from app.services import disputes, payments, scheduler, users, wallet
from tests.telegram_sim import Actor, Msg, Sim

URL = "https://chatgpt.com/checkout/openai_llc/cs_live_e2e_123#fidabc"
MNEMONIC = "test test test test test test test test test test test junk"


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(delivery, "SEND_INTERVAL", 0)
    common._hits.clear()  # the per-user rate limiter keeps module-level state


@pytest.fixture
async def sim():
    async with Sim() as s:
        yield s


# ── helpers ─────────────────────────────────────────────────────────────────


async def register(sim: Sim, who: Actor, role_label: str, tz: str, langs: list[str] | None = None) -> None:
    """/start -> role -> Mini App time zone -> confirm."""
    await who.say("/start")
    await who.press(role_label)
    await who.web_app({"tz": tz, "langs": langs or ["en"]})
    await who.press("Yes, correct")


def request_for(admin: Actor, telegram_id: int) -> Msg:
    return next(m for m in admin.inbox if "approval request" in m.text and str(telegram_id) in m.text)


async def user_id_of(telegram_id: int) -> int:
    async with session_scope() as db:
        return (await db.execute(select(User.user_id).where(User.telegram_id == telegram_id))).scalar_one()


def blocks_around_now(tz: str) -> list[str]:
    """Labels of the previous, current and next 2-hour block in ``tz`` (so a slot is live right now)."""
    hour = timeutil.to_local(timeutil.utcnow(), tz).hour
    cur = hour // 2 * 2
    return [timeutil.slot_label((cur + d) % 24, (cur + d + 2) % 24) for d in (-2, 0, 2)]


async def approved_pair(sim: Sim, *, scanner_tz: str = "Asia/Kolkata") -> tuple[Actor, Actor, Actor]:
    admin = sim.actor(9001, "Boss")
    seller = sim.actor(5001, "Sam", "Seller", username="sam_seller")
    scanner = sim.actor(6001, "Sara", "Scanner", username="sara_scanner")
    await register(sim, seller, "QR Sender", "Europe/London", ["en-GB"])
    await register(sim, scanner, "QR Scanner", scanner_tz, ["en-IN"])
    await admin.press("Approve", message=request_for(admin, 5001))
    await admin.press("Approve", message=request_for(admin, 6001))
    # the scanner chooses slots covering "now" through the real buttons
    await scanner.press("Choose slots")
    for label in blocks_around_now(scanner_tz):
        await scanner.press(f"⬜ {label}")
    await scanner.press("Done")
    # the admin names the scanner in the web panel
    async with session_scope() as db:
        await users.set_alias(db, await user_id_of(6001), None)
    await sim.deliver()
    return admin, seller, scanner


async def fund_seller(sim: Sim, amount: str = "10") -> None:
    async with session_scope() as db:
        await payments.credit_onchain_deposit(
            db, user_id=await user_id_of(5001), amount=Decimal(amount), tx_hash="0xe2e" + "0" * 61, log_index=0, address="0xabc", block_number=1
        )
    await sim.deliver()


def png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (320, 200), (10, 120, 60)).save(out, "PNG")
    return out.getvalue()


# ── onboarding ──────────────────────────────────────────────────────────────


async def test_onboarding_and_admin_approval_as_in_the_spec(sim):
    admin = sim.actor(9001, "Boss")
    seller = sim.actor(5001, "Sam", "Seller", username="sam_seller", language_code="en")

    msgs = await seller.say("/start")
    assert msgs[0].button_labels() == ["🏪 QR Sender", "📷 QR Scanner"]  # STEP 1: role question
    prompt = (await seller.press("QR Sender"))[-1]
    assert prompt.keyboard_buttons()[0]["web_app"]["url"] == "https://admin.example.test/tz.html"  # auto-detect via Mini App
    detected = (await seller.web_app({"tz": "Europe/London", "langs": ["en-GB", "en"]}))[-1]
    assert "United Kingdom" in detected.text and "Europe/London" in detected.text
    done = await seller.press("Yes, correct")
    assert any("Request sent" in m.text for m in done)

    # the admin gets: name, Telegram ID, role, country, timezone, timestamp + [Approve] [Reject]
    req = request_for(admin, 5001)
    for needle in ("Sam Seller", "@sam_seller", "5001", "QR Sender", "United Kingdom", "Europe/London", "UTC"):
        assert needle in req.text, needle
    assert req.button_labels() == ["✅ Approve", "❌ Reject"]

    # pending users cannot use the bot yet
    assert "waiting for admin approval" in (await seller.say("/send"))[-1].text
    assert "waiting for admin approval" in (await seller.say("/start"))[-1].text

    await admin.press("Approve", message=req)
    assert "Approved by" in req.text  # the buttons are replaced by the decision
    assert not req.buttons()
    assert seller.inbox[-1].text == "✅ Approved. Use /send."  # STEP 2 wording


async def test_scanner_approval_message_and_rejection(sim):
    admin = sim.actor(9001, "Boss")
    scanner = sim.actor(6001, "Sara", "Scanner")
    rejected = sim.actor(6002, "Rita", "Rejected")
    await register(sim, scanner, "QR Scanner", "Asia/Kolkata", ["en-IN"])
    await register(sim, rejected, "QR Scanner", "Asia/Karachi", ["en-PK"])
    await admin.press("Approve", message=request_for(admin, 6001))
    await admin.press("Reject", message=request_for(admin, 6002))
    assert scanner.inbox[-1].text == "✅ Approved. Choose your slots."
    assert scanner.inbox[-1].button_labels() == ["🕒 Choose slots"]
    assert rejected.inbox[-1].text == "❌ Rejected. Contact admin."
    assert "Rejected by" in request_for(admin, 6002).text
    # a rejected user stays out
    assert "Rejected" in (await rejected.say("/myslots"))[-1].text


async def test_manual_country_and_ambiguous_timezone_fallbacks(sim):
    admin = sim.actor(9001, "Boss")
    # 1. UTC has no country: the bot asks, the user types it, picks a time zone
    a = sim.actor(7001, "Ann", "Anywhere")
    await a.say("/start")
    await a.press("QR Scanner")
    out = await a.web_app({"tz": "UTC", "langs": ["en"]})
    assert "country" in out[-1].text.lower()
    assert "couldn't find" in (await a.say("Narnia"))[-1].text
    zones = (await a.say("indo"))[-1]  # an unambiguous prefix -> straight to Indonesia's time zones
    assert "time zone in" in zones.text and any("Jakarta" in b for b in zones.button_labels())
    await a.press("Jakarta")
    assert any("Request sent" in t for t in a.texts())
    async with session_scope() as db:
        row = (await db.execute(select(User).where(User.telegram_id == 7001))).scalar_one()
        assert (row.country, row.timezone, row.status) == ("ID", "Asia/Jakarta", "pending")
    # 2. typing an exact single-zone country goes straight through
    b = sim.actor(7002, "Bob", "Typed")
    await b.say("/start")
    await b.press("QR Sender")
    await b.web_app({"tz": "not/a-zone", "langs": []})  # garbage from the Mini App -> manual
    out = await b.say("india")
    assert any("Request sent" in m.text for m in out)
    async with session_scope() as db:
        row = (await db.execute(select(User).where(User.telegram_id == 7002))).scalar_one()
        assert (row.country, row.timezone) == ("IN", "Asia/Kolkata")
    assert request_for(admin, 7002)


async def test_ambiguous_country_text_offers_a_choice(sim):
    u = sim.actor(7005, "Eve", "Congo")
    await u.say("/start")
    await u.press("QR Scanner")
    picks = (await u.say("congo"))[-1]
    assert "one of these" in picks.text
    assert sorted(b for b in picks.button_labels()) == ["🇨🇩 DR Congo", "🇨🇬 Republic of the Congo"]
    await u.press("Republic of the Congo")
    assert any("Request sent" in t for t in u.texts())
    async with session_scope() as db:
        row = (await db.execute(select(User).where(User.telegram_id == 7005))).scalar_one()
        assert (row.country, row.timezone) == ("CG", "Africa/Brazzaville")


async def test_detected_timezone_can_be_corrected(sim):
    u = sim.actor(7003, "Cy", "Corrector")
    await u.say("/start")
    await u.press("QR Sender")
    await u.web_app({"tz": "Asia/Dhaka", "langs": ["bn-BD"]})
    await u.press("Change")
    assert "country" in u.inbox[-1].text.lower()
    out = await u.say("pakistan")
    assert any("Request sent" in m.text for m in out)
    async with session_scope() as db:
        assert (await db.execute(select(User.timezone).where(User.telegram_id == 7003))).scalar_one() == "Asia/Karachi"


async def test_role_cannot_be_changed_after_registration(sim):
    admin = sim.actor(9001, "Boss")
    u = sim.actor(7004, "Dee", "Done")
    await register(sim, u, "QR Sender", "Europe/London")
    stale_role_msg = u.inbox[0]  # the (already edited) role message; a fast double-tap replays its data
    await u.press_data("role:scanner", stale_role_msg)
    assert "already completed" in (sim.world.answers[-1].get("text") or "")
    async with session_scope() as db:
        assert (await db.execute(select(User.role).where(User.telegram_id == 7004))).scalar_one() == "seller"
    assert len([m for m in admin.inbox if "7004" in m.text]) == 1  # still exactly one request for this user


# ── slots ───────────────────────────────────────────────────────────────────


async def test_slot_commands_add_remove_and_confirm_text(sim):
    admin, seller, scanner = await approved_pair(sim)
    tz_label = timeutil.tz_label("Asia/Kolkata")
    saved = [t for t in scanner.texts() if "Slots saved" in t][-1]
    assert f"({tz_label})" in saved and "You can add/remove anytime." in saved

    info = (await scanner.say("/myslots"))[-1]
    assert "user1" in info.text and tz_label in info.text and info.button_labels() == ["✏️ Edit slots"]

    removable = (await scanner.say("/removeslot"))[-1]
    assert all(label.startswith("✅") for label in removable.button_labels()[:-1])  # only owned slots are offered
    first = removable.button_labels()[0].split(" ", 1)[1]
    await scanner.press(f"✅ {first}")
    assert first not in (await scanner.say("/myslots"))[-1].text

    addable = (await scanner.say("/addslot"))[-1]
    assert all(label.startswith("⬜") for label in addable.button_labels()[:-1])
    await scanner.press(f"⬜ {first}")
    assert first in (await scanner.say("/myslots"))[-1].text

    # wrong role
    assert "QR Scanners" in (await seller.say("/myslots"))[-1].text
    assert "QR Senders" in (await scanner.say("/send"))[-1].text


async def test_changing_time_zone_moves_the_slots_with_the_scanner(sim):
    admin, seller, scanner = await approved_pair(sim)
    await scanner.say("/timezone")
    await scanner.web_app({"tz": "Asia/Karachi", "langs": ["en-PK"]})
    await scanner.press("Yes, correct")
    assert "Time zone updated" in scanner.inbox[-1].text
    async with session_scope() as db:
        uid = await user_id_of(6001)
        tzs = set(await slots_tz(db, uid))
        assert tzs == {"Asia/Karachi"}


async def slots_tz(db, uid):
    from app.models import ScannerSlot

    return (await db.execute(select(ScannerSlot.timezone).where(ScannerSlot.user_id == uid))).scalars().all()


async def test_pre_slot_notification_ready_and_busy(sim):
    admin, seller, scanner = await approved_pair(sim)
    slot_a, slot_b = (await _slots_of(6001))[:2]
    now = timeutil.utcnow()
    start_a, _ = timeutil.next_occurrence(slot_a.slot_start, slot_a.slot_end, "Asia/Kolkata", now)
    start_b, _ = timeutil.next_occurrence(slot_b.slot_start, slot_b.slot_end, "Asia/Kolkata", now)

    # STEP 5: ten minutes before a slot the scanner gets "Be ready" with [Ready] [Busy]
    assert await scheduler.tick_slot_notifications(start_a - dt.timedelta(minutes=9)) == 1
    await sim.deliver()
    reminder = scanner.inbox[-1]
    assert reminder.text.startswith("🔔 Your slot starts in 9 minutes.") or reminder.text.startswith("🔔 Your slot starts in 10 minutes.")
    assert f"Slot: {timeutil.slot_label(slot_a.slot_start, slot_a.slot_end)} (IST)" in reminder.text and "Be ready." in reminder.text
    assert reminder.button_labels() == ["✅ Ready", "❌ Busy"]
    await scanner.press("Ready")
    assert reminder.text == "✅ Great - you're marked as ready."
    assert not any("marked Busy" in t for t in admin.texts())

    # Busy: the admin is notified (no backup scanner exists) and nobody is matched for that occurrence
    assert await scheduler.tick_slot_notifications(start_b - dt.timedelta(minutes=9)) == 1
    await sim.deliver()
    reminder_b = scanner.inbox[-1]
    callback_data = reminder_b.buttons()[1]["callback_data"]
    await scanner.press("Busy")
    assert "won't receive URLs" in reminder_b.text
    notice = next(t for t in admin.texts() if "marked Busy" in t)
    assert "user1" in notice and "IST" in notice
    from app.services import matching

    async with session_scope() as db:
        assert await matching.active_candidates(db, start_b + dt.timedelta(minutes=1)) == []
    # answering the same reminder twice is refused politely
    await scanner.press_data(callback_data, reminder_b)
    assert "Already answered" in sim.world.answers[-1]["text"] or "no longer active" in sim.world.answers[-1].get("text", "")


async def _slots_of(telegram_id: int):
    from app.models import ScannerSlot

    async with session_scope() as db:
        uid = await user_id_of(telegram_id)
        return list((await db.execute(select(ScannerSlot).where(ScannerSlot.user_id == uid).order_by(ScannerSlot.slot_start))).scalars().all())


# ── the main flow ───────────────────────────────────────────────────────────


async def test_seller_scanner_full_task_with_payment_and_anonymity(sim):
    admin, seller, scanner = await approved_pair(sim)
    assert any(t == "✅ Your name is set: user1." for t in scanner.texts())  # STEP 4

    # no balance yet -> told to deposit, nothing is asked
    assert "Insufficient balance" in (await seller.say("/send"))[-1].text
    await fund_seller(sim, "10")
    assert any("Deposit received" in t for t in seller.texts())

    # STEP 6: /send -> URL (2 min) -> validation -> only "user1 is active"
    assert "within 2 minutes" in (await seller.say("/send"))[-1].text
    assert "ChatGPT link" in (await seller.say("hello there"))[-1].text
    assert "official ChatGPT links" in (await seller.say("https://evil.example.com/x"))[-1].text
    preview = (await seller.say(URL))[-1]
    assert preview.text == "📊 Active user right now:\n✅ user1\n\nSend URL?"
    assert preview.button_labels() == ["✅ Yes", "❌ Cancel"]
    for leak in ("India", "Asia/Kolkata", "IST", "Sara", "6001", "slot", "08-", "10-"):
        assert leak not in preview.text

    sent = await seller.press("Yes")
    assert any("URL sent to user1" in m.text for m in sent)

    # STEP 7: the URL reaches only the scanner, with Accept / Skip, link preview disabled
    new_url = next(m for m in scanner.inbox if "New URL" in m.text)
    assert URL in new_url.text and "Respond within 2 minutes" in new_url.text
    assert new_url.button_labels() == ["✅ Accept", "❌ Skip"]
    assert new_url.link_preview_disabled is True
    assert not any(URL in m.text for m in admin.inbox + seller.inbox if m is not None and "New URL" in m.text)

    # STEP 8: Accept -> Done
    await scanner.press("Accept")
    assert "Accepted" in new_url.text and new_url.button_labels() == ["✅ Done"]
    assert any("user1 accepted your URL" in t for t in seller.texts())
    await scanner.press("Done")
    assert "marked as done" in new_url.text

    # STEP 9: two minutes later the seller is asked to confirm
    now = timeutil.utcnow()
    assert (await scheduler.tick_sessions(now + dt.timedelta(minutes=1)))["prompted"] == 0
    assert (await scheduler.tick_sessions(now + dt.timedelta(minutes=3)))["prompted"] == 1
    await sim.deliver()
    report = seller.inbox[-1]
    assert report.text == "🔔 URL report:\n✅ user1 → Done.\n\nDid it work?"
    assert report.button_labels() == ["✅ Confirm", "❌ Reject"]
    await seller.press("Confirm")
    assert "Confirmed" in report.text and "0.5005 USDT" in report.text

    # STEP 10: payment: scanner +0.5, platform commission 0.1% (0.0005), transaction saved in UTC
    assert any("+0.50 USDT" in t and "confirmed" in t for t in scanner.texts())
    assert "0.50 USDT" in (await scanner.say("/balance"))[-1].text
    assert "9.4995 USDT" in (await seller.say("/balance"))[-1].text
    async with session_scope() as db:
        tx = (await db.execute(select(TransactionHistory))).scalar_one()
        assert (tx.status, tx.amount, tx.commission, tx.scanner_name) == ("completed", Decimal("0.5"), Decimal("0.0005"), "user1")
        assert (tx.seller_country, tx.scanner_country, tx.scanner_timezone) == ("GB", "IN", "Asia/Kolkata")
        assert tx.confirmed_at.tzinfo is not None and tx.url == URL
        assert (await wallet.reconcile(db)).ok

    # anonymity: nothing about the other side ever crossed over
    seller_view, scanner_view = seller.all_text(), scanner.all_text()
    for secret in ("Sara", "sara_scanner", "6001", "India", "Asia/Kolkata", "IST"):
        assert secret not in seller_view, secret
    for secret in ("Sam", "sam_seller", "5001", "United Kingdom", "Europe/London", "GMT", "BST"):
        assert secret not in scanner_view, secret

    # history shows the alias to the seller and nothing about the seller to the scanner
    assert "user1" in (await seller.say("/history"))[-1].text
    assert "user" not in (await scanner.say("/history"))[-1].text.lower().replace("recent tasks", "")


async def test_skip_expiry_and_busy_scanner_paths(sim):
    admin, seller, scanner = await approved_pair(sim)
    await fund_seller(sim, "10")

    async def send_url():
        await seller.say("/send")
        await seller.say(URL)
        await seller.press("Yes")
        return next(m for m in reversed(scanner.inbox) if "New URL" in m.text)

    first = await send_url()
    await scanner.press("Skip", message=first)
    assert "skipped" in first.text
    assert any("skipped your URL" in t and "not charged" in t for t in seller.texts())
    assert "10.00 USDT" in (await seller.say("/balance"))[-1].text

    second = await send_url()
    assert (await scheduler.tick_sessions(timeutil.utcnow() + dt.timedelta(minutes=3)))["expired"] == 1
    await sim.deliver()
    assert any("didn't respond in time" in t for t in seller.texts())
    await scanner.press_data(second.markup["inline_keyboard"][0][0]["callback_data"], second)  # stale Accept
    assert "no longer active" in second.text or "expired" in second.text.lower()
    assert "10.00 USDT" in (await seller.say("/balance"))[-1].text


async def test_no_active_scanner_and_cancel_paths(sim):
    admin, seller, scanner = await approved_pair(sim)
    await fund_seller(sim, "10")
    # nobody is active: remove every slot
    async with session_scope() as db:
        from app.models import ScannerSlot

        for slot in (await db.execute(select(ScannerSlot))).scalars().all():
            await db.delete(slot)
    await seller.say("/send")
    out = await seller.say(URL)
    assert "No scanner is active right now" in out[-1].text
    # cancel at the prompt and at the confirmation step
    await seller.say("/send")
    assert "Cancelled" in (await seller.say("/cancel"))[-1].text
    assert "Nothing to cancel" in (await seller.say("/cancel"))[-1].text


async def test_send_window_times_out(sim):
    admin, seller, scanner = await approved_pair(sim)
    await fund_seller(sim, "10")
    await seller.say("/send")
    assert await scheduler.tick_states(timeutil.utcnow() + dt.timedelta(minutes=3)) == 1
    await sim.deliver()
    assert "Time's up" in seller.inbox[-1].text
    assert "Time's up" in (await seller.say(URL))[-1].text or "didn't understand" in seller.inbox[-1].text


async def test_confirm_screen_survives_a_scanner_change(sim):
    """If the shown scanner disappears before 'Yes', the seller is shown the new one instead of an error."""
    admin, seller, scanner = await approved_pair(sim)
    other = sim.actor(6002, "Omar", "Other", username="omar")
    await register(sim, other, "QR Scanner", "Asia/Kolkata", ["en-IN"])
    await admin.press("Approve", message=request_for(admin, 6002))
    await other.press("Choose slots")
    for label in blocks_around_now("Asia/Kolkata"):
        await other.press(f"⬜ {label}")
    await other.press("Done")
    async with session_scope() as db:
        await users.set_alias(db, await user_id_of(6002), None)
    await sim.deliver()
    await fund_seller(sim, "10")
    await seller.say("/send")
    preview = (await seller.say(URL))[-1]
    shown = "user1" if "user1" in preview.text else "user2"
    # the shown scanner goes offline (suspended) before the seller taps Yes
    async with session_scope() as db:
        uid = await user_id_of(6001 if shown == "user1" else 6002)
        await users.suspend(db, uid, "admin")
    await seller.press("Yes")
    changed = seller.inbox[-1]
    assert "active user changed" in changed.text
    assert ("user2" if shown == "user1" else "user1") in changed.text
    await seller.press("Yes")
    assert any("URL sent to" in m.text for m in seller.inbox)


# ── disputes ────────────────────────────────────────────────────────────────


async def test_reject_dispute_screenshot_and_admin_decision(sim):
    admin, seller, scanner = await approved_pair(sim)
    await fund_seller(sim, "10")
    await seller.say("/send")
    await seller.say(URL)
    await seller.press("Yes")
    await scanner.press("Accept")
    await scanner.press("Done")
    await scheduler.tick_sessions(timeutil.utcnow() + dt.timedelta(minutes=3))
    await sim.deliver()

    out = await seller.press("Reject")
    assert any("Rejection received" in m.text for m in out) or "Rejection received" in seller.texts()[-1]
    assert "describe what went wrong" in " ".join(seller.texts()[-2:])
    assert "note was added" in (await seller.say("The page showed an error"))[-1].text

    ask = next(m for m in scanner.inbox if "rejected task" in m.text)
    assert "screenshot" in ask.text and "Sam" not in ask.text and "sam_seller" not in ask.text  # seller stays anonymous
    assert "photo" in (await scanner.say("here you go"))[-1].text  # text is not proof
    assert any("Screenshot received" in t for t in (await scanner.send_photo(png())) and scanner.texts())

    async with session_scope() as db:
        dispute = (await db.execute(select(Dispute))).scalar_one()
        assert dispute.status == "pending_review" and dispute.proof_file.endswith(".png")
        assert "The page showed an error" in dispute.reason
        dispute_id = dispute.id
    review = next(m for m in admin.inbox if "needs your review" in m.text)
    assert f"#{dispute_id}" in review.text
    # funds are frozen meanwhile: seller shows 0.5005 on hold
    assert "On hold: 0.5005 USDT" in (await seller.say("/balance"))[-1].text

    async with session_scope() as db:
        await disputes.resolve(db, dispute_id, DisputeResolution.PAY_SCANNER, by="admin", notes="proof is clear")
    await sim.deliver()
    assert any("resolved in your favour" in t and "+0.50 USDT" in t for t in scanner.texts())
    assert any("payment stands" in t for t in seller.texts())
    async with session_scope() as db:
        assert (await wallet.reconcile(db)).ok
        assert (await db.execute(select(TransactionHistory.status))).scalar_one() == "completed"


async def test_dispute_refund_and_non_image_proof_is_refused(sim):
    admin, seller, scanner = await approved_pair(sim)
    await fund_seller(sim, "10")
    await seller.say("/send")
    await seller.say(URL)
    await seller.press("Yes")
    await scanner.press("Accept")
    await scanner.press("Done")
    await scheduler.tick_sessions(timeutil.utcnow() + dt.timedelta(minutes=3))
    await sim.deliver()
    await seller.press("Reject")
    out = await scanner.send_photo(b"this is not an image", as_document=True)
    assert any("valid image" in m.text for m in out)
    await scanner.send_photo(png(), as_document=True)  # a PNG sent as a file is fine
    async with session_scope() as db:
        dispute = (await db.execute(select(Dispute))).scalar_one()
        assert dispute.proof_file
        await disputes.resolve(db, dispute.id, DisputeResolution.REFUND_SELLER, by="admin")
    await sim.deliver()
    assert any("resolved in your favour" in t and "returned to your balance" in t for t in seller.texts())
    assert any("decided against you" in t for t in scanner.texts())
    assert "10.00 USDT" in (await seller.say("/balance"))[-1].text


# ── wallet ──────────────────────────────────────────────────────────────────


async def test_seller_deposit_screens(sim, monkeypatch):
    monkeypatch.setattr(get_settings(), "hd_mnemonic", SecretStr(MNEMONIC))
    admin, seller, scanner = await approved_pair(sim)
    menu = (await seller.say("/deposit"))[-1]
    assert menu.button_labels() == ["🔗 BEP-20 (USDT)", "🟡 Binance Pay"]

    # BEP-20: a personal address with a QR code; the same address every time
    photo = next(m for m in await seller.press("BEP-20") if m.photo)
    assert "personal address" in photo.text and "Minimum deposit" in photo.text and "BEP-20" in photo.text
    first_address = next(w for w in photo.text.split() if w.startswith("0x"))
    again = next(m for m in await seller.press("BEP-20", message=menu) if m.photo)
    assert first_address in again.text

    # Binance is unavailable until the admin configures a Pay ID
    assert "not set up" in (await seller.press("Binance", message=menu))[-1].text
    from app.services import settings_service

    async with session_scope() as db:
        await settings_service.save(db, {"binance_pay_id": "123456789"}, "admin")
    menu = (await seller.say("/deposit"))[-1]
    await seller.press("Binance", message=menu)
    assert "123456789" in menu.text and menu.button_labels() == ["✅ I've paid"]

    await seller.press("I've paid")
    assert "How much" in seller.inbox[-1].text
    assert "valid amount" in (await seller.say("lots"))[-1].text
    assert "Order ID" in (await seller.say("10"))[-1].text
    assert "valid Order ID" in (await seller.say("bad ref!"))[-1].text
    assert "claim received" in (await seller.say("M_P_777888"))[-1].text
    assert any("Binance deposit claim" in m.text for m in admin.inbox)
    async with session_scope() as db:
        claim = (await db.execute(select(payments.Deposit))).scalar_one()
        await payments.confirm_deposit(db, claim.id, "admin")
    await sim.deliver()
    assert any("Deposit received" in t and "10.00 USDT" in t for t in seller.texts())


async def test_scanner_withdrawal_flow(sim):
    admin, seller, scanner = await approved_pair(sim)
    await fund_seller(sim, "10")
    for _ in range(2):  # two paid tasks -> 1.00 USDT
        await seller.say("/send")
        await seller.say(URL)
        await seller.press("Yes")
        await scanner.press("Accept")
        await scanner.press("Done")
        await scheduler.tick_sessions(timeutil.utcnow() + dt.timedelta(minutes=3))
        await sim.deliver()
        await seller.press("Confirm")
    assert "Available: 1.00 USDT" in (await scanner.say("/balance"))[-1].text

    menu = (await scanner.say("/withdraw"))[-1]
    assert menu.button_labels() == ["🔗 BEP-20", "🟡 Binance Pay"]
    assert "BEP-20 wallet address" in (await scanner.press("BEP-20"))[-1].text
    assert "not a valid BEP-20 address" in (await scanner.say("0x123"))[-1].text
    good = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
    out = await scanner.say(good)
    assert any("Saved BEP-20 address" in m.text for m in out) and "How much" in out[-1].text
    assert "valid amount" in (await scanner.say("abc"))[-1].text
    assert "minimum withdrawal" in (await scanner.say("0.5"))[-1].text
    assert "only have" in (await scanner.say("5"))[-1].text
    confirm = (await scanner.say("all"))[-1]
    assert good in confirm.text and "Amount: 1.00 USDT" in confirm.text
    assert any("Withdrawal #1 requested" in m.text for m in await scanner.press("Confirm"))
    alert = next(m for m in admin.inbox if "withdrawal request" in m.text)
    assert good in alert.text and "1.00 USDT" in alert.text
    assert "On hold: 1.00 USDT" in (await scanner.say("/balance"))[-1].text
    assert "already have a withdrawal" in (await scanner.say("/withdraw"))[-1].text or "below the minimum" in scanner.inbox[-1].text

    async with session_scope() as db:
        await payments.approve_withdrawal(db, 1, "admin")
        await payments.complete_withdrawal(db, 1, "admin", "0xdeadbeef")
    await sim.deliver()
    assert any("Withdrawal #1 paid" in t and "0xdeadbeef" in t for t in scanner.texts())
    async with session_scope() as db:
        assert (await wallet.reconcile(db)).ok

    # saved address is reused next time (Binance ID flow, invalid then valid)
    wallet_view = (await scanner.say("/wallet"))[-1]
    assert good in wallet_view.text and "Binance ID: not set" in wallet_view.text
    await scanner.press("Set Binance ID")
    assert "number" in (await scanner.say("abc"))[-1].text
    assert "Saved Binance ID" in (await scanner.say("12345678"))[-1].text


# ── security / robustness ───────────────────────────────────────────────────


async def test_only_admins_can_press_admin_buttons(sim):
    admin = sim.actor(9001, "Boss")
    mallory = sim.actor(6666, "Mallory", "Evil")
    victim = sim.actor(6001, "Sara", "Scanner")
    await register(sim, victim, "QR Scanner", "Asia/Kolkata", ["en-IN"])
    req = request_for(admin, 6001)
    # mallory replays the admin's button data
    await mallory.press_data(req.buttons()[0]["callback_data"], req)
    assert sim.world.answers[-1]["text"] == "You are not allowed to do this."
    assert not any("Approved" in t for t in victim.texts())
    async with session_scope() as db:
        assert (await db.execute(select(User.status).where(User.telegram_id == 6001))).scalar_one() == "pending"
    # deciding twice is harmless
    approve_data = req.buttons()[0]["callback_data"]
    await admin.press("Approve", message=req)
    await admin.press_data(approve_data, req)  # a second tap on the same button
    assert sum("Approved. Choose your slots" in t for t in victim.texts()) == 1


async def test_stale_and_foreign_task_buttons_are_refused(sim):
    admin, seller, scanner = await approved_pair(sim)
    other = sim.actor(6002, "Omar", "Other")
    await register(sim, other, "QR Scanner", "Asia/Kolkata", ["en-IN"])
    await admin.press("Approve", message=request_for(admin, 6002))
    await fund_seller(sim, "10")
    await seller.say("/send")
    await seller.say(URL)
    await seller.press("Yes")
    new_url = next(m for m in scanner.inbox if "New URL" in m.text)
    # another scanner replays the button of a task that is not theirs
    await other.press_data(new_url.markup["inline_keyboard"][0][0]["callback_data"], new_url)
    assert "not yours" in sim.world.answers[-1]["text"]
    # the seller cannot accept on the scanner's behalf either
    await seller.press_data(new_url.markup["inline_keyboard"][0][0]["callback_data"], new_url)
    assert "QR Scanners" in seller.inbox[-1].text or "not" in str(sim.world.answers[-1])
    async with session_scope() as db:
        assert (await db.execute(select(TaskSession.status))).scalar_one() == "awaiting_scanner"


async def test_unknown_text_help_and_non_private_chats(sim):
    admin = sim.actor(9001, "Boss")
    nobody = sim.actor(8001, "Nobody")
    assert "send /start" in (await nobody.say("/help"))[-1].text.lower()
    assert "Please send /start" in (await nobody.say("hello"))[-1].text
    seller = sim.actor(5001, "Sam", "Seller")
    await register(sim, seller, "QR Sender", "Europe/London")
    await admin.press("Approve", message=request_for(admin, 5001))
    assert "didn't understand" in (await seller.say("blah blah"))[-1].text
    assert "/send" in (await seller.say("/help"))[-1].text and "Admin" not in seller.inbox[-1].text
    assert "Admin" in (await admin.say("/help"))[-1].text
    stats = (await admin.say("/admin"))[-1].text
    assert "Pending approvals" in stats and "Ledger check: ✅ OK" in stats
    assert not (await seller.say("/admin"))  # silently ignored for non-admins


async def test_every_delivered_message_is_valid_html_and_nothing_failed(sim):
    admin, seller, scanner = await approved_pair(sim)
    await fund_seller(sim, "10")
    await seller.say("/send")
    await seller.say(URL)
    await seller.press("Yes")
    await scanner.press("Accept")
    await scanner.press("Done")
    async with session_scope() as db:
        failed = (await db.execute(select(Outbox).where(Outbox.status == "failed"))).scalars().all()
        queued = (await db.execute(select(Outbox).where(Outbox.status == "queued"))).scalars().all()
    assert failed == [] and queued == []

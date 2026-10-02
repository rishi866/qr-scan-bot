"""All user-facing messages (English only). Telegram HTML parse mode - every dynamic value is escaped.

Anonymity rule: a message to a seller never contains anything about the scanner except the alias
(``user1``); a message to a scanner never contains anything about the seller.
"""

from __future__ import annotations

import datetime as dt
import html
from decimal import Decimal

from app import timeutil
from app.money import fmt_money

CURRENCY = "USDT"


def e(value: object) -> str:
    return html.escape(str(value), quote=False)


def money(value: object) -> str:
    return f"{fmt_money(value)} {CURRENCY}"


def role_label(role: str) -> str:
    return "🏪 QR Sender" if role == "seller" else "📷 QR Scanner"


def minutes_text(seconds: int) -> str:
    if seconds % 60 == 0:
        m = seconds // 60
        return f"{m} minute{'s' if m != 1 else ''}"
    return f"{seconds} seconds"


def contact_admin(support_contact: str = "") -> str:
    return f"Contact admin ({e(support_contact)})." if support_contact else "Contact admin."


# ── onboarding ──────────────────────────────────────────────────────────────

WELCOME = (
    "👋 <b>Welcome!</b>\n\n"
    "This bot connects <b>QR Senders</b> and <b>QR Scanners</b> anonymously.\n"
    "• 🏪 <b>QR Sender</b> - submit a ChatGPT URL and pay per completed task.\n"
    "• 📷 <b>QR Scanner</b> - complete URLs during your own time slots and earn USDT.\n\n"
    "Neither side ever sees the other's identity.\n\n"
    "Please choose your role:"
)

ASK_TIMEZONE_WEBAPP = (
    "🌍 <b>Let's detect your time zone</b>\n\n"
    "Tap the button below - it only reads your device's time zone (never your location). "
    "Slots and reports are shown in your local time."
)
ASK_TIMEZONE_MANUAL = "🌍 <b>Which country are you in?</b>\nType the country name, e.g. <i>India</i>."
ASK_COUNTRY_TEXT = "✏️ Type your <b>country</b> name (e.g. <i>India</i>, <i>Pakistan</i>, <i>Brazil</i>):"
COUNTRY_NOT_FOUND = "🤔 I couldn't find that country. Please try again (e.g. <i>India</i>) or send /cancel."
INVALID_TIMEZONE_DATA = "⚠️ I couldn't read a valid time zone. Please type your country instead."


def confirm_timezone(country: str | None, tz: str) -> str:
    where = f"{timeutil.country_flag(country)} <b>{e(timeutil.country_name(country))}</b>" if country else "🌐 <b>unknown country</b>"
    return (
        "📍 <b>Detected</b>\n"
        f"Country: {where}\n"
        f"Time zone: <b>{e(tz)}</b> ({timeutil.format_offset(tz)}, {e(timeutil.tz_label(tz))})\n\n"
        "Is this correct?"
    )


def choose_country_for_timezone(tz: str) -> str:
    return f"📍 Your time zone is <b>{e(tz)}</b>, which is used in several countries. Which one are you in?"


def choose_timezone_in_country(country: str) -> str:
    return f"🕒 Select your time zone in <b>{e(timeutil.country_name(country))}</b>:"


def pick_country(candidates: list[str]) -> str:
    return "Did you mean one of these?" if candidates else COUNTRY_NOT_FOUND


def request_sent(role: str) -> str:
    return (
        "✅ <b>Request sent!</b>\n"
        "An admin will review it shortly. You'll receive a message here as soon as it's decided."
    )


def already_pending() -> str:
    return "⏳ Your account is waiting for admin approval. You'll be notified here."


def not_registered() -> str:
    return "Please send /start first."


def suspended(support: str = "") -> str:
    return f"⛔ Your account is suspended. {contact_admin(support)}"


def rejected(support: str = "") -> str:
    return f"❌ Rejected. {contact_admin(support)}"


def approved(role: str) -> str:
    if role == "seller":
        return "✅ Approved. Use /send."
    return "✅ Approved. Choose your slots."


def admin_new_request(
    *, name: str, username: str | None, telegram_id: int, role: str, country: str | None, tz: str | None, at: dt.datetime
) -> str:
    handle = f" (@{e(username)})" if username else ""
    tz_text = f"{e(tz)} ({timeutil.format_offset(tz, at)})" if tz and timeutil.is_valid_timezone(tz) else "-"
    country_text = f"{timeutil.country_flag(country)} {e(timeutil.country_name(country))}" if country else "-"
    return (
        "🆕 <b>New approval request</b>\n\n"
        f"👤 Name: <b>{e(name)}</b>{handle}\n"
        f"🆔 Telegram ID: <code>{telegram_id}</code>\n"
        f"🎭 Role: {role_label(role)}\n"
        f"🌍 Country: {country_text}\n"
        f"🕒 Time zone: {tz_text}\n"
        f"📅 Requested: {at.strftime('%d %b %Y, %H:%M')} UTC"
    )


def admin_decision_suffix(approved_flag: bool, by: str) -> str:
    return f"\n\n{'✅ Approved' if approved_flag else '❌ Rejected'} by {e(by)}"


def name_set(alias: str) -> str:
    return f"✅ Your name is set: <b>{e(alias)}</b>."


def timezone_updated(country: str | None, tz: str) -> str:
    return (
        f"✅ Time zone updated: <b>{e(tz)}</b> ({e(timeutil.tz_label(tz))}). "
        "Your slots now follow this local time."
    )


# ── scanner slots ───────────────────────────────────────────────────────────


def slot_picker(tz: str, selected_count: int) -> str:
    return (
        f"🕒 <b>Choose your slots</b>\n"
        f"Times are in your local time (<b>{e(timeutil.tz_label(tz))}</b>, {timeutil.format_offset(tz)}).\n"
        "Tap to add ✅ or remove ⬜ a slot - there is no limit.\n\n"
        f"Selected: <b>{selected_count}</b>\n"
        "You can change this anytime with /myslots, /addslot, /removeslot."
    )


def slots_saved(labels: list[str], tz: str) -> str:
    if not labels:
        return "ℹ️ You have no slots yet. You won't receive any URLs. Use /addslot to add some."
    return (
        f"✅ Slots saved: <b>{e(', '.join(labels))}</b> ({e(timeutil.tz_label(tz))}).\n"
        "You can add/remove anytime."
    )


def my_slots(labels: list[str], tz: str, alias: str | None, reputation: int | None = None) -> str:
    name = f"Your name: <b>{e(alias)}</b>" if alias else "Your name: <i>not assigned yet</i> (the admin will set it)"
    lines = [f"🕒 <b>Your slots</b> ({e(timeutil.tz_label(tz))}, {timeutil.format_offset(tz)})"]
    lines.append(", ".join(labels) if labels else "<i>none yet</i>")
    lines.append("")
    lines.append(name)
    if reputation is not None:
        lines.append(f"⭐ Reputation: {reputation}/100")
    return "\n".join(lines)


def pre_slot(label: str, tz: str, minutes: int) -> str:
    return (
        f"🔔 <b>Your slot starts in {minutes} minutes.</b>\n"
        f"Slot: <b>{e(label)}</b> ({e(timeutil.tz_label(tz))})\n"
        "Be ready."
    )


READY_ACK = "✅ Great - you're marked as ready."
BUSY_ACK = "👍 Noted - you won't receive URLs during this slot."
NOTIFICATION_STALE = "This reminder is no longer active."


def admin_scanner_busy(alias: str | None, name: str, telegram_id: int, label: str, tz: str) -> str:
    who = f"<b>{e(alias)}</b>" if alias else f"<b>{e(name)}</b>"
    return (
        f"⚠️ {who} (ID <code>{telegram_id}</code>) marked <b>Busy</b> for slot "
        f"<b>{e(label)}</b> ({e(timeutil.tz_label(tz))}). They will not receive URLs for it."
    )


# ── seller: /send ───────────────────────────────────────────────────────────


def send_prompt(seconds: int) -> str:
    return (
        f"🔗 <b>Send me the ChatGPT URL</b> within {minutes_text(seconds)}.\n"
        "Send /cancel to abort."
    )


def send_invalid(reason: str) -> str:
    return f"⚠️ {e(reason)}\nPlease send a valid ChatGPT link, or /cancel."


SEND_TIMEOUT = "⌛ Time's up. Use /send to start again."
SEND_CANCELLED = "👍 Cancelled."
NOTHING_TO_CANCEL = "Nothing to cancel."


def low_balance(required: Decimal, available: Decimal) -> str:
    return (
        "💳 <b>Insufficient balance</b>\n"
        f"Each task costs <b>{money(required)}</b>.\n"
        f"Your available balance: <b>{money(available)}</b>\n\n"
        "Use /deposit to add funds."
    )


NO_ACTIVE_SCANNER = "😴 <b>No scanner is active right now.</b>\nPlease try again in a little while."
SELLER_BUSY = "⏳ You already have a task in progress. Please wait until it finishes."


def active_preview(alias: str) -> str:
    return f"📊 <b>Active user right now:</b>\n✅ <b>{e(alias)}</b>\n\nSend URL?"


def sent_ok(alias: str, seconds: int) -> str:
    return (
        f"📨 URL sent to <b>{e(alias)}</b>. They have {minutes_text(seconds)} to respond.\n"
        "I'll keep you posted."
    )


def seller_scanner_changed(alias: str) -> str:
    return f"ℹ️ The active user changed.\n\n{active_preview(alias)}"


def seller_accepted(alias: str) -> str:
    return f"👀 <b>{e(alias)}</b> accepted your URL and is working on it."


def seller_skipped(alias: str) -> str:
    return f"❌ <b>{e(alias)}</b> skipped your URL. You were not charged.\nUse /send to try again."


def seller_expired(alias: str) -> str:
    return f"⌛ <b>{e(alias)}</b> didn't respond in time. You were not charged.\nUse /send to try again."


def seller_timed_out(alias: str) -> str:
    return (
        f"⌛ <b>{e(alias)}</b> didn't finish in time. The task was cancelled and you were not charged.\n"
        "Use /send to try again."
    )


def seller_report(alias: str) -> str:
    return f"🔔 <b>URL report:</b>\n✅ <b>{e(alias)}</b> → Done.\n\nDid it work?"


def seller_confirmed(cost: Decimal, balance: Decimal, auto: bool = False) -> str:
    prefix = "⏱ No answer received, so the task was <b>auto-confirmed</b>.\n" if auto else "✅ <b>Confirmed.</b> Thank you!\n"
    return f"{prefix}Charged: {money(cost)} • Balance: {money(balance)}"


def seller_rejected(session_id: int) -> str:
    return (
        f"⚠️ <b>Rejection received</b> for task #{session_id}.\n"
        "The scanner has been asked for proof and an admin will review it. Your funds stay on hold meanwhile.\n\n"
        "If you like, describe what went wrong in <b>one message</b> now (optional)."
    )


DISPUTE_NOTE_SAVED = "📝 Thanks, your note was added to the case."


def seller_dispute_refunded(session_id: int, cost: Decimal) -> str:
    return f"✅ <b>Dispute #{session_id} resolved in your favour.</b>\n{money(cost)} was returned to your balance."


def seller_dispute_rejected(session_id: int) -> str:
    return (
        f"ℹ️ <b>Dispute for task #{session_id} was reviewed.</b>\n"
        "The admin found the task was completed, so the payment stands."
    )


# ── scanner: tasks ──────────────────────────────────────────────────────────


def scanner_new_url(session_id: int, url: str, seconds: int) -> str:
    return (
        f"🔔 <b>New URL!</b> (task #{session_id})\n"
        f"🔗 {e(url)}\n\n"
        f"Respond within {minutes_text(seconds)}."
    )


def scanner_accepted(session_id: int, url: str) -> str:
    return (
        f"✅ <b>Accepted</b> (task #{session_id})\n"
        f"🔗 {e(url)}\n\n"
        "Complete the task, then tap <b>Done</b>."
    )


def scanner_skipped(session_id: int) -> str:
    return f"⏭ Task #{session_id} skipped."


def scanner_expired(session_id: int) -> str:
    return f"⌛ Task #{session_id} expired - no response within the time limit."


def scanner_timed_out(session_id: int) -> str:
    return f"⌛ Task #{session_id} was cancelled because it wasn't marked Done in time."


def scanner_done_ack(session_id: int) -> str:
    return f"✅ Task #{session_id} marked as done. I'll tell you when the sender confirms."


def scanner_paid(session_id: int, amount: Decimal, balance: Decimal) -> str:
    return (
        f"💰 <b>Task #{session_id} confirmed</b>\n"
        f"+{money(amount)} • Balance: {money(balance)}"
    )


def scanner_dispute(session_id: int, minutes: int) -> str:
    return (
        f"⚠️ <b>The sender rejected task #{session_id}.</b>\n"
        f"Please send a <b>screenshot</b> of the ChatGPT page showing the completed task within {minutes} minutes. "
        "An admin will review it."
    )


PROOF_RECEIVED = "📎 Screenshot received. We're reviewing it and will update you here."
PROOF_NOT_IMAGE = "Please send the proof as a <b>photo</b> (or image file)."
PROOF_NOT_EXPECTED = "I'm not expecting a screenshot from you right now."
PROOF_TIMEOUT = "⌛ The proof window has closed. An admin will decide based on what is available."


def scanner_dispute_paid(session_id: int, amount: Decimal) -> str:
    return f"✅ <b>Dispute #{session_id} resolved in your favour.</b>\n+{money(amount)} credited."


def scanner_dispute_lost(session_id: int) -> str:
    return f"❌ <b>Dispute #{session_id} was decided against you.</b> No payment for this task."


# ── wallet ──────────────────────────────────────────────────────────────────


def wallet_summary(balance: Decimal, pending: Decimal, role: str, earned: Decimal, spent: Decimal) -> str:
    lines = [
        "💼 <b>Your wallet</b>",
        f"Available: <b>{money(balance)}</b>",
        f"On hold: {money(pending)}",
    ]
    if role == "scanner":
        lines.append(f"Total earned: {money(earned)}")
    else:
        lines.append(f"Total spent: {money(spent)}")
    return "\n".join(lines)


def deposit_menu() -> str:
    return "💳 <b>Deposit</b>\nChoose a method:"


def deposit_bep20(address: str, min_deposit: Decimal, confirmations: int, token: str = CURRENCY) -> str:
    return (
        f"🔗 <b>Deposit {e(token)} (BEP-20)</b>\n"
        f"Send <b>{e(token)}</b> on the <b>BNB Smart Chain (BEP-20)</b> network to your personal address:\n\n"
        f"<code>{e(address)}</code>\n\n"
        "⚠️ Only send USDT on <b>BEP-20</b>. Other tokens or networks cannot be recovered.\n"
        f"Minimum deposit: {money(min_deposit)}.\n"
        f"Your balance is credited automatically after {confirmations} network confirmations (about a minute)."
    )


def deposit_binance(pay_id: str) -> str:
    return (
        "🟡 <b>Deposit via Binance Pay</b>\n"
        "1. Open Binance → <b>Pay</b> → <b>Send</b>.\n"
        f"2. Send USDT to Binance Pay ID: <code>{e(pay_id)}</code>\n"
        "3. Come back here, tap <b>I've paid</b> and send me the amount and the <b>Order ID</b> shown by Binance.\n\n"
        "Your balance is credited as soon as the payment is verified."
    )


DEPOSIT_BINANCE_UNAVAILABLE = "🟡 Binance deposits are not set up yet. Please use BEP-20 or contact the admin."
DEPOSIT_UNAVAILABLE = "⚠️ Deposits are temporarily unavailable. Please try again later."
ASK_DEPOSIT_AMOUNT = "How much USDT did you send? (e.g. <code>10</code>)"
ASK_DEPOSIT_REFERENCE = "Now send the Binance <b>Order ID</b> / transaction ID of that payment."
DEPOSIT_CLAIM_DUPLICATE = "⚠️ That reference was already submitted."
DEPOSIT_CLAIM_BAD_REF = "That doesn't look like a valid Order ID. Please send it again (letters, digits, _ and - only)."


def deposit_claim_received(amount: Decimal, ref: str) -> str:
    return (
        f"📝 Payment claim received: {money(amount)} (ref <code>{e(ref)}</code>).\n"
        "We're verifying it - you'll get a message once it's credited."
    )


def deposit_credited(amount: Decimal, balance: Decimal) -> str:
    return f"✅ <b>Deposit received</b>\n+{money(amount)} • Balance: {money(balance)}"


def deposit_below_min(amount: Decimal, minimum: Decimal, support: str = "") -> str:
    return (
        f"⚠️ We received {money(amount)}, which is below the minimum deposit of {money(minimum)}, "
        f"so it was not credited automatically. {contact_admin(support)}"
    )


def deposit_rejected(amount: Decimal, ref: str, support: str = "") -> str:
    return f"❌ Your payment claim of {money(amount)} (ref <code>{e(ref)}</code>) could not be verified. {contact_admin(support)}"


def admin_deposit_claim(user_label: str, amount: Decimal, ref: str) -> str:
    return (
        "🟡 <b>Binance deposit claim</b>\n"
        f"User: {e(user_label)}\nAmount: {money(amount)}\nOrder ID: <code>{e(ref)}</code>\n"
        "Verify it in Binance, then confirm in the admin panel (Wallets → Deposits)."
    )


def admin_deposit_below_min(user_label: str, amount: Decimal, tx_hash: str) -> str:
    return (
        "⚠️ <b>Below-minimum deposit</b>\n"
        f"User: {e(user_label)}\nAmount: {money(amount)}\nTx: <code>{e(tx_hash)}</code>\n"
        "Credit it manually in the admin panel if appropriate."
    )


def ask_address(method: str) -> str:
    if method == "bep20":
        return "🔗 Send your <b>BEP-20 wallet address</b> (starts with 0x…). Double-check it - payouts cannot be reversed."
    return "🟡 Send your <b>Binance Pay ID / UID</b> (digits only)."


ADDRESS_INVALID_BEP20 = "⚠️ That is not a valid BEP-20 address. It must look like <code>0x…</code> (42 characters)."
ADDRESS_INVALID_BINANCE = "⚠️ A Binance Pay ID / UID is a number (5-20 digits). Please try again."


def address_saved(method: str, address: str) -> str:
    return f"✅ Saved {'BEP-20 address' if method == 'bep20' else 'Binance ID'}: <code>{e(address)}</code>"


def withdraw_menu(balance: Decimal, minimum: Decimal) -> str:
    return (
        f"🏧 <b>Withdraw</b>\nAvailable: <b>{money(balance)}</b> (minimum {money(minimum)}).\n"
        "Choose how you want to be paid:"
    )


def ask_withdraw_amount(balance: Decimal, minimum: Decimal) -> str:
    return (
        f"How much do you want to withdraw? Send an amount between {money(minimum)} and {money(balance)}, "
        "or <code>all</code>."
    )


WITHDRAW_BAD_AMOUNT = "⚠️ Please send a valid amount (e.g. <code>5</code> or <code>2.5</code>), or <code>all</code>."


def withdraw_confirm(method: str, address: str, amount: Decimal, fee: Decimal) -> str:
    net = amount - fee
    where = "BEP-20" if method == "bep20" else "Binance Pay"
    lines = [
        "🏧 <b>Confirm withdrawal</b>",
        f"Method: {where}",
        f"To: <code>{e(address)}</code>",
        f"Amount: {money(amount)}",
    ]
    if fee > 0:
        lines.append(f"Fee: {money(fee)}")
        lines.append(f"You receive: <b>{money(net)}</b>")
    return "\n".join(lines)


def withdraw_requested(withdrawal_id: int) -> str:
    return f"📨 Withdrawal #{withdrawal_id} requested. An admin will review it shortly."


def withdraw_completed(withdrawal_id: int, amount: Decimal, tx: str | None) -> str:
    ref = f"\nReference: <code>{e(tx)}</code>" if tx else ""
    return f"✅ <b>Withdrawal #{withdrawal_id} paid</b>\n{money(amount)} sent.{ref}"


def withdraw_rejected(withdrawal_id: int, note: str | None, support: str = "") -> str:
    reason = f"\nReason: {e(note)}" if note else ""
    return f"❌ Withdrawal #{withdrawal_id} was rejected and the funds were returned to your balance.{reason}\n{contact_admin(support)}"


def withdraw_failed_user(withdrawal_id: int) -> str:
    return f"⚠️ Withdrawal #{withdrawal_id} hit a problem and is being handled by the admin. Your funds are safe."


def admin_withdrawal_request(withdrawal_id: int, user_label: str, amount: Decimal, method: str, address: str) -> str:
    return (
        "🏧 <b>New withdrawal request</b>\n"
        f"#{withdrawal_id} • {e(user_label)}\n"
        f"Amount: {money(amount)} via {'BEP-20' if method == 'bep20' else 'Binance'}\n"
        f"To: <code>{e(address)}</code>\n"
        "Review it in the admin panel (Wallets → Withdrawals)."
    )


def admin_withdrawal_failed(withdrawal_id: int, reason: str) -> str:
    return f"🚨 <b>Withdrawal #{withdrawal_id} needs attention</b>\n{e(reason)}"


def history_line(when: str, status: str, amount: Decimal, who: str | None) -> str:
    who_text = f" • {e(who)}" if who else ""
    return f"{e(when)} • {e(status)} • {money(amount)}{who_text}"


# ── disputes / admin alerts ─────────────────────────────────────────────────


def admin_dispute_review(dispute_id: int, session_id: int, ai_summary: str | None) -> str:
    ai = f"\n🤖 AI: {e(ai_summary)}" if ai_summary else ""
    return (
        f"⚖️ <b>Dispute #{dispute_id}</b> (task #{session_id}) needs your review.{ai}\n"
        "Open the admin panel → Disputes."
    )


def admin_user_label(name: str, alias: str | None, telegram_id: int) -> str:
    return f"{name} ({alias}) [{telegram_id}]" if alias else f"{name} [{telegram_id}]"


# ── generic ─────────────────────────────────────────────────────────────────

HELP_SELLER = (
    "ℹ️ <b>Help - QR Sender</b>\n"
    "/send - submit a ChatGPT URL\n"
    "/deposit - add funds (BEP-20 / Binance)\n"
    "/balance - your wallet\n"
    "/history - recent tasks\n"
    "/timezone - change your time zone\n"
    "/cancel - cancel the current step"
)
HELP_SCANNER = (
    "ℹ️ <b>Help - QR Scanner</b>\n"
    "/myslots - view your slots\n"
    "/addslot - add slots\n"
    "/removeslot - remove slots\n"
    "/balance - your wallet\n"
    "/withdraw - cash out (BEP-20 / Binance)\n"
    "/wallet - save payout addresses\n"
    "/history - recent tasks\n"
    "/timezone - change your time zone\n"
    "/cancel - cancel the current step"
)
HELP_ADMIN = (
    "🛠 <b>Admin</b>\n"
    "Use the web admin panel for everything. This chat shows approval requests and alerts.\n"
    "/admin - quick stats"
)
UNKNOWN_TEXT = "I didn't understand that. Use /help to see what you can do."
SOMETHING_WENT_WRONG = "⚠️ Something went wrong. Please try again in a moment."
STALE_BUTTON = "This button is no longer active."
ONLY_SELLERS = "This command is for QR Senders."
ONLY_SCANNERS = "This command is for QR Scanners."
NEED_APPROVAL = "⏳ Your account must be approved by an admin first."
BLOCKED_WHILE_BUSY = "Please finish or /cancel the current step first."

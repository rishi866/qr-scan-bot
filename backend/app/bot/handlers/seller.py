"""Seller side: /send flow, Confirm / Reject, the optional dispute note.

What the seller may learn about the scanner: the alias (``user1``) - nothing else.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from app import callbacks, states, texts
from app.bot.context import Reply, ident_of, is_private_chat, send_replies
from app.bot.handlers.common import gate
from app.db import session_scope
from app.enums import Role
from app.models import User
from app.services import disputes, sessions, settings_service, urls, users, wallet

log = logging.getLogger(__name__)


def _yes_no_buttons():
    return [[("✅ Yes", callbacks.SEND_YES), ("❌ Cancel", callbacks.SEND_NO)]]


async def _seller_or_reply(db, update: Update, *, lock: bool = False) -> tuple[User | None, Reply | None]:
    ident = ident_of(update)
    user = await users.get_by_telegram_id(db, ident.id, lock=lock)
    problem = await gate(db, user, role=Role.SELLER)
    return (None, problem) if problem else (user, None)


# ── /send ───────────────────────────────────────────────────────────────────


async def send_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    async with session_scope() as db:
        user, problem = await _seller_or_reply(db, update, lock=True)
        if problem or user is None:
            reply = problem
        else:
            reply = await begin_send(db, user)
    await send_replies(update, context, reply)


async def begin_send(db, user: User) -> Reply:
    cfg = await settings_service.load(db)
    if await sessions.active_count_for_seller(db, user.user_id) >= cfg.max_active_sessions_per_seller:
        return Reply(texts.SELLER_BUSY)
    w = await wallet.get_wallet(db, user.user_id)
    if w.balance < cfg.seller_cost:
        return Reply(texts.low_balance(cfg.seller_cost, w.balance))
    await users.set_state(db, user, states.AWAITING_URL, None, cfg.url_input_timeout_seconds)
    return Reply(texts.send_prompt(cfg.url_input_timeout_seconds))


async def handle_url_text(db, user: User, text: str) -> Reply:
    """State ``awaiting_url``: validate the link, look up the active scanner, ask for confirmation."""
    cfg = await settings_service.load(db)
    if user.role != Role.SELLER.value:
        await users.clear_state(db, user)
        return Reply(texts.ONLY_SELLERS)
    try:
        url = urls.validate_chatgpt_url(text, cfg.allowed_url_domains)
    except urls.InvalidUrl as exc:
        return Reply(texts.send_invalid(str(exc)))  # stay in the state: the seller may retry within the window
    w = await wallet.get_wallet(db, user.user_id)
    if w.balance < cfg.seller_cost:
        await users.clear_state(db, user)
        return Reply(texts.low_balance(cfg.seller_cost, w.balance))
    candidate = await sessions.preview(db, user)
    if candidate is None:
        await users.clear_state(db, user)
        return Reply(texts.NO_ACTIVE_SCANNER)
    await users.set_state(db, user, states.CONFIRM_SEND, {"url": url, "scanner_id": candidate.scanner.user_id}, cfg.url_input_timeout_seconds)
    return Reply(texts.active_preview(candidate.scanner.alias or "user"), buttons=_yes_no_buttons())


async def on_send_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assert query is not None and query.data is not None
    yes = query.data == callbacks.SEND_YES
    async with session_scope() as db:
        user, problem = await _seller_or_reply(db, update, lock=True)
        if problem or user is None:
            reply = problem
        elif user.state != states.CONFIRM_SEND or users.state_is_expired(user) or not user.state_data:
            await users.clear_state(db, user)
            reply = Reply(texts.SEND_TIMEOUT, edit=True, toast=texts.STALE_BUTTON)
        elif not yes:
            await users.clear_state(db, user)
            reply = Reply(texts.SEND_CANCELLED, edit=True)
        else:
            reply = await _confirm_send(db, user)
    await send_replies(update, context, reply)


async def _confirm_send(db, user: User) -> Reply:
    cfg = await settings_service.load(db)
    data = user.state_data or {}
    url, scanner_id = data["url"], int(data["scanner_id"])
    try:
        # the session is created in a savepoint so a failed attempt leaves no half-done state behind
        async with db.begin_nested():
            s = await sessions.create_session(db, user, scanner_id, url)
    except sessions.ScannerUnavailable:
        candidate = await sessions.preview(db, user)
        if candidate is None:
            await users.clear_state(db, user)
            return Reply(texts.NO_ACTIVE_SCANNER, edit=True)
        await users.set_state(db, user, states.CONFIRM_SEND, {"url": url, "scanner_id": candidate.scanner.user_id}, cfg.url_input_timeout_seconds)
        return Reply(texts.seller_scanner_changed(candidate.scanner.alias or "user"), buttons=_yes_no_buttons(), edit=True)
    except wallet.InsufficientFunds:
        await users.clear_state(db, user)
        w = await wallet.get_wallet(db, user.user_id)
        return Reply(texts.low_balance(cfg.seller_cost, w.balance), edit=True)
    except sessions.SellerBusy:
        await users.clear_state(db, user)
        return Reply(texts.SELLER_BUSY, edit=True)
    except sessions.SessionError as exc:
        await users.clear_state(db, user)
        return Reply(f"⚠️ {texts.e(exc)}", edit=True)
    scanner = await users.require_user(db, s.scanner_id)
    await users.clear_state(db, user)
    return Reply(texts.sent_ok(scanner.alias or "user", cfg.url_response_timeout_seconds), edit=True)


# ── confirm / reject the scanner's report ───────────────────────────────────


async def on_report_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assert query is not None and query.data is not None
    action, raw_id = query.data.split(":")
    session_id = int(raw_id)
    async with session_scope() as db:
        user, problem = await _seller_or_reply(db, update)
        if problem or user is None:
            reply = problem
        else:
            try:
                if action == callbacks.CONFIRM:
                    s = await sessions.confirm(db, session_id, user.user_id)
                    w = await wallet.get_wallet(db, user.user_id)
                    reply = Reply(texts.seller_confirmed(s.amount + s.commission, w.balance), edit=True)
                else:
                    s, _ = await sessions.reject(db, session_id, user.user_id)
                    reply = Reply(texts.seller_rejected(s.session_id), edit=True)
            except sessions.InvalidState as exc:
                reply = Reply(f"ℹ️ {texts.e(exc)}", edit=True, toast=str(exc))
            except sessions.SessionError as exc:
                reply = Reply(toast=str(exc), alert=True)
    await send_replies(update, context, reply)


async def handle_dispute_note(db, user: User, text: str) -> Reply:
    """State ``dispute_note``: one optional free-text note that goes to the admin only."""
    data = user.state_data or {}
    dispute_id = int(data.get("dispute_id", 0))
    await users.clear_state(db, user)
    if dispute_id and await disputes.add_seller_note(db, dispute_id, user.user_id, text[:1000]):
        return Reply(texts.DISPUTE_NOTE_SAVED)
    return Reply(texts.UNKNOWN_TEXT)

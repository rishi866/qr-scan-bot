"""Routes free-text messages to the flow that is waiting for input (``users.state``)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from telegram import Update
from telegram.ext import ContextTypes

from app import states, texts
from app.bot.context import Reply, ident_of, is_private_chat, send_replies
from app.bot.handlers import seller, start, wallet
from app.db import session_scope
from app.models import User
from app.services import users

log = logging.getLogger(__name__)

TextHandler = Callable[..., Awaitable["Reply | list[Reply]"]]

STATE_HANDLERS: dict[str, TextHandler] = {
    states.ONB_COUNTRY_TEXT: start.handle_country_text,
    states.AWAITING_URL: seller.handle_url_text,
    states.DISPUTE_NOTE: seller.handle_dispute_note,
    states.AWAITING_ADDRESS: wallet.handle_address_text,
    states.AWAITING_WD_AMOUNT: wallet.handle_wd_amount_text,
    states.AWAITING_DEP_AMOUNT: wallet.handle_dep_amount_text,
    states.AWAITING_DEP_REF: wallet.handle_dep_ref_text,
}

# states that are driven by buttons (or photos), not by typed text
BUTTON_STATES = {states.ONB_CONFIRM, states.ONB_PICK, states.CONFIRM_SEND, states.CONFIRM_WD, states.AWAITING_PROOF}


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or message.text is None or not is_private_chat(update):
        return
    ident = ident_of(update)
    async with session_scope() as db:
        user = await users.get_by_telegram_id(db, ident.id, lock=True)
        replies = await _route(db, user, message.text)
    await send_replies(update, context, replies)


async def _route(db, user: User | None, text: str) -> Reply | list[Reply]:
    if user is None:
        return Reply(texts.not_registered())
    if user.state and users.state_is_expired(user):
        expired_state = user.state
        await users.clear_state(db, user)
        if expired_state in states.ANNOUNCE_TIMEOUT:
            return Reply(texts.SEND_TIMEOUT)
    handler = STATE_HANDLERS.get(user.state or "")
    if handler is not None:
        return await handler(db, user, text)
    if user.state == states.AWAITING_PROOF:
        return Reply(texts.PROOF_NOT_IMAGE)
    if user.state in BUTTON_STATES:
        return Reply("Please use the buttons above, or send /cancel.")
    return Reply(texts.UNKNOWN_TEXT)

"""Shared bits: access gate, /help, /cancel, rate limiting and the global error handler."""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ApplicationHandlerStop, ContextTypes

from app import texts
from app.bot.context import Reply, ident_of, is_admin, is_private_chat, send_replies
from app.db import session_scope
from app.enums import Role, UserStatus
from app.models import User
from app.services import settings_service, users

log = logging.getLogger(__name__)


async def gate(db, user: User | None, *, role: Role | None = None) -> Reply | None:
    """``None`` when the user may proceed, otherwise the message explaining why not."""
    if user is None:
        return Reply(texts.not_registered())
    cfg = await settings_service.load(db)
    if user.status == UserStatus.ONBOARDING.value:
        return Reply("Please finish the setup first: /start")
    if user.status == UserStatus.PENDING.value:
        return Reply(texts.already_pending())
    if user.status == UserStatus.REJECTED.value:
        return Reply(texts.rejected(cfg.support_contact))
    if user.status == UserStatus.SUSPENDED.value:
        return Reply(texts.suspended(cfg.support_contact))
    if role is not None and user.role != role.value:
        return Reply(texts.ONLY_SELLERS if role == Role.SELLER else texts.ONLY_SCANNERS)
    return None


# ── /help /cancel ───────────────────────────────────────────────────────────


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    ident = ident_of(update)
    async with session_scope() as db:
        user = await users.get_by_telegram_id(db, ident.id)
        if user is None:
            text = texts.WELCOME.split("\n\nPlease choose")[0] + "\n\nSend /start to begin."
        elif user.role == Role.SELLER.value:
            text = texts.HELP_SELLER
        else:
            text = texts.HELP_SCANNER
        if is_admin(ident.id):
            text += "\n\n" + texts.HELP_ADMIN
    await send_replies(update, context, Reply(text))


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    ident = ident_of(update)
    async with session_scope() as db:
        user = await users.get_by_telegram_id(db, ident.id, lock=True)
        if user is None or not user.state:
            reply = Reply(texts.NOTHING_TO_CANCEL)
        else:
            await users.clear_state(db, user)
            reply = Reply(texts.SEND_CANCELLED, remove_keyboard=True)
    await send_replies(update, context, reply)


# ── abuse protection ────────────────────────────────────────────────────────

_WINDOW_SECONDS = 10.0
_MAX_UPDATES = 30
_hits: dict[int, deque[float]] = defaultdict(deque)


def _rate_limited(user_id: int, now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    hits = _hits[user_id]
    while hits and now - hits[0] > _WINDOW_SECONDS:
        hits.popleft()
    if len(hits) >= _MAX_UPDATES:
        return True
    hits.append(now)
    if len(_hits) > 50_000:  # bound memory
        for key in [k for k, v in _hits.items() if not v or now - v[-1] > _WINDOW_SECONDS][:10_000]:
            _hits.pop(key, None)
    return False


async def rate_limit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or is_admin(user.id):
        return
    if _rate_limited(user.id):
        log.warning("rate limit hit by telegram id %s", user.id)
        if update.callback_query is not None:  # never leave a button spinner hanging
            try:
                await update.callback_query.answer("Too many requests - please slow down.")
            except TelegramError:
                pass
        raise ApplicationHandlerStop


# ── errors ──────────────────────────────────────────────────────────────────


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("unhandled error while processing an update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat is not None and update.effective_chat.type == "private":
        try:
            if update.callback_query is not None:
                await update.callback_query.answer(texts.SOMETHING_WENT_WRONG, show_alert=True)
            else:
                await context.bot.send_message(update.effective_chat.id, texts.SOMETHING_WENT_WRONG)
        except TelegramError:
            pass

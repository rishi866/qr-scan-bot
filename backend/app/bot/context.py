"""Glue between python-telegram-bot updates and the business services.

Handlers do their database work first and return :class:`Reply` objects; :func:`send_replies`
performs the Telegram I/O *after* the transaction has been committed (never hold row locks while
waiting for the network).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from telegram import (
    InlineKeyboardMarkup,
    KeyboardButton,
    LinkPreviewOptions,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from app.bot.delivery import build_markup, strip_tags
from app.config import get_settings
from app.keyboards import Rows, inline
from app.services.users import TgIdentity

log = logging.getLogger(__name__)


@dataclass
class Reply:
    text: str = ""
    buttons: Rows | None = None
    edit: bool = False  # edit the message that carried the pressed button instead of sending a new one
    toast: str | None = None  # answer shown on button press (callback queries only)
    alert: bool = False  # show ``toast`` as a modal alert
    photo: bytes | None = None  # send ``text`` as the caption of this image
    webapp_button: tuple[str, str] | None = None  # (label, url) reply-keyboard button that opens a Mini App
    keyboard_texts: list[str] = field(default_factory=list)  # extra plain reply-keyboard buttons
    remove_keyboard: bool = False
    preview: bool = False


def ident_of(update: Update) -> TgIdentity:
    user = update.effective_user
    assert user is not None
    return TgIdentity(
        id=user.id,
        first_name=user.first_name or "",
        last_name=user.last_name,
        username=user.username,
        language_code=user.language_code,
    )


def is_private_chat(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.type == "private"


def is_admin(telegram_id: int) -> bool:
    return telegram_id in get_settings().admin_telegram_ids


def webapp_url(path: str = "/tz.html") -> str | None:
    base = get_settings().public_base_url
    return f"{base}{path}" if base.startswith("https://") else None


def _reply_keyboard(reply: Reply) -> ReplyKeyboardMarkup | ReplyKeyboardRemove | None:
    rows: list[list[KeyboardButton]] = []
    if reply.webapp_button:
        label, url = reply.webapp_button
        rows.append([KeyboardButton(label, web_app=WebAppInfo(url=url))])
    for label in reply.keyboard_texts:
        rows.append([KeyboardButton(label)])
    if rows:
        return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)
    if reply.remove_keyboard:
        return ReplyKeyboardRemove()
    return None


async def send_replies(update: Update, context: ContextTypes.DEFAULT_TYPE, replies: Reply | Iterable[Reply] | None) -> None:
    if replies is None:
        return
    items = [replies] if isinstance(replies, Reply) else list(replies)
    query = update.callback_query
    answered = False
    for reply in items:
        if query is not None and not answered:
            answered = True
            try:
                await query.answer(text=reply.toast, show_alert=reply.alert)
            except TelegramError:  # stale query ids are harmless
                log.debug("could not answer callback query", exc_info=True)
            if not reply.text:
                continue
        if not reply.text:
            continue
        markup: Any = build_markup(inline(reply.buttons))
        chat = update.effective_chat
        assert chat is not None
        preview = LinkPreviewOptions(is_disabled=not reply.preview)
        if reply.edit and query is not None and query.message is not None:
            try:
                await query.edit_message_text(
                    reply.text, parse_mode=ParseMode.HTML, reply_markup=markup if isinstance(markup, InlineKeyboardMarkup) else None, link_preview_options=preview
                )
                continue
            except BadRequest as exc:
                if "not modified" in str(exc).lower():
                    continue
                log.debug("edit failed (%s) - sending a new message instead", exc)
        if reply.photo is not None:
            await context.bot.send_photo(chat.id, reply.photo, caption=reply.text, parse_mode=ParseMode.HTML, reply_markup=markup)
            continue
        kwargs: dict[str, Any] = {"parse_mode": ParseMode.HTML, "link_preview_options": preview}
        keyboard = _reply_keyboard(reply)
        kwargs["reply_markup"] = markup or keyboard
        try:
            await context.bot.send_message(chat.id, reply.text, **kwargs)
        except BadRequest as exc:
            if "parse entities" not in str(exc).lower():
                raise
            log.warning("HTML rejected, resending as plain text: %s", exc)
            kwargs["parse_mode"] = None
            await context.bot.send_message(chat.id, strip_tags(reply.text), **kwargs)

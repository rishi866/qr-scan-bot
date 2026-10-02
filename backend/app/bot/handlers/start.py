"""/start, role choice and time-zone / country detection.

Telegram does not tell bots a user's time zone or country, so we ask the *device*: a tiny Telegram
Mini App (``/tz.html``) reads ``Intl.DateTimeFormat().resolvedOptions().timeZone`` and sends it back.
Country comes from the time zone (+ locale region hints). Users can always type their country instead.
"""

from __future__ import annotations

import json
import logging

from telegram import Update
from telegram.ext import ContextTypes

from app import callbacks, states, texts, timeutil
from app.bot.context import Reply, ident_of, is_private_chat, send_replies, webapp_url
from app.db import session_scope
from app.enums import Role, UserStatus
from app.models import User
from app.services import settings_service, users
from app.timeutil import utcnow

log = logging.getLogger(__name__)

PURPOSE_ONBOARDING = "onboarding"
PURPOSE_CHANGE = "change"
MANUAL_LABEL = "✏️ Choose country manually"
STATE_TTL = 24 * 3600


def role_buttons():
    return [[("🏪 QR Sender", callbacks.role(Role.SELLER.value)), ("📷 QR Scanner", callbacks.role(Role.SCANNER.value))]]


def _confirm_buttons():
    return [[("✅ Yes, correct", callbacks.TZ_OK), ("✏️ Change", callbacks.TZ_CHANGE)]]


def _country_buttons(codes: list[str]):
    buttons = [(f"{timeutil.country_flag(c)} {timeutil.country_name(c)}", callbacks.country(c)) for c in codes]
    return [[b] for b in buttons]


def welcome_back(user: User) -> str:
    if user.role == Role.SELLER.value:
        return "👋 <b>Welcome back!</b>\nUse /send to submit a URL, /deposit to add funds, /balance for your wallet or /help for everything else."
    name = f"You are <b>{texts.e(user.alias)}</b>.\n" if user.alias else "The admin hasn't assigned your name yet.\n"
    return f"👋 <b>Welcome back!</b>\n{name}Use /myslots to manage your slots, /balance for your wallet or /help for everything else."


# ── location step ───────────────────────────────────────────────────────────


async def ask_location(db, user: User, purpose: str) -> list[Reply]:
    await users.set_state(db, user, states.ONB_COUNTRY_TEXT, {"purpose": purpose}, STATE_TTL)
    url = webapp_url()
    if url:
        return [
            Reply(
                texts.ASK_TIMEZONE_WEBAPP,
                webapp_button=("📍 Detect my time zone", url),
                keyboard_texts=[MANUAL_LABEL],
            )
        ]
    return [Reply(texts.ASK_TIMEZONE_MANUAL, remove_keyboard=True)]


async def finalize_location(db, user: User, tz: str, country: str | None, purpose: str) -> list[Reply]:
    await users.set_location(db, user, tz, country)
    await users.clear_state(db, user)
    if user.status == UserStatus.ONBOARDING.value:
        await users.submit_for_approval(db, user)
        return [Reply(texts.request_sent(user.role), remove_keyboard=True)]
    return [Reply(texts.timezone_updated(country, tz), remove_keyboard=True)]


async def propose_timezone(db, user: User, tz: str, languages: list[str] | None, purpose: str) -> list[Reply]:
    country, candidates = timeutil.guess_country(tz, timeutil.region_hints(languages))
    if country:
        await users.set_state(db, user, states.ONB_CONFIRM, {"tz": tz, "country": country, "purpose": purpose}, STATE_TTL)
        return [Reply(texts.confirm_timezone(country, tz), buttons=_confirm_buttons())]
    if candidates:
        await users.set_state(db, user, states.ONB_PICK, {"kind": "country", "options": candidates, "tz": tz, "purpose": purpose}, STATE_TTL)
        return [Reply(texts.choose_country_for_timezone(tz), buttons=_country_buttons(candidates))]
    await users.set_state(db, user, states.ONB_COUNTRY_TEXT, {"purpose": purpose}, STATE_TTL)
    return [Reply(texts.ASK_COUNTRY_TEXT, remove_keyboard=True)]


async def country_chosen(db, user: User, code: str, purpose: str) -> list[Reply]:
    zones = timeutil.timezones_for_country(code)
    if not zones:
        return [Reply(texts.COUNTRY_NOT_FOUND)]
    if len(zones) == 1:
        return await finalize_location(db, user, zones[0], code, purpose)
    now = utcnow()
    await users.set_state(db, user, states.ONB_PICK, {"kind": "tz", "options": zones, "country": code, "purpose": purpose}, STATE_TTL)
    buttons = [[(f"{z.split('/')[-1].replace('_', ' ')} ({timeutil.format_offset(z, now)})", callbacks.tz_select(i))] for i, z in enumerate(zones[:40])]
    return [Reply(texts.choose_timezone_in_country(code), buttons=buttons)]


# ── command + callback handlers ─────────────────────────────────────────────


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    ident = ident_of(update)
    async with session_scope() as db:
        user = await users.get_by_telegram_id(db, ident.id, lock=True)
        if user is not None:
            await users.refresh_identity(db, user, ident)
        replies = await _start_flow(db, user)
    await send_replies(update, context, replies)


async def _start_flow(db, user: User | None) -> list[Reply]:
    if user is None:
        return [Reply(texts.WELCOME, buttons=role_buttons())]
    cfg = await settings_service.load(db)
    if user.status == UserStatus.ONBOARDING.value:
        return await ask_location(db, user, PURPOSE_ONBOARDING)
    if user.status == UserStatus.PENDING.value:
        return [Reply(texts.already_pending())]
    if user.status == UserStatus.REJECTED.value:
        return [Reply(texts.rejected(cfg.support_contact))]
    if user.status == UserStatus.SUSPENDED.value:
        return [Reply(texts.suspended(cfg.support_contact))]
    return [Reply(welcome_back(user))]


async def on_role_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assert query is not None and query.data is not None
    role = Role(query.data.split(":", 1)[1])
    ident = ident_of(update)
    async with session_scope() as db:
        existing = await users.get_by_telegram_id(db, ident.id, lock=True)
        if existing is not None and existing.status != UserStatus.ONBOARDING.value:
            replies = [Reply(toast="You already completed registration.", alert=True)]
        else:
            user = await users.register_role(db, ident, role)
            replies = [Reply(f"✅ Role: <b>{texts.role_label(role.value)}</b>", edit=True)]
            replies += await ask_location(db, user, PURPOSE_ONBOARDING)
    await send_replies(update, context, replies)


async def timezone_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    ident = ident_of(update)
    async with session_scope() as db:
        user = await users.get_by_telegram_id(db, ident.id, lock=True)
        if user is None:
            replies = [Reply(texts.not_registered())]
        elif user.status in (UserStatus.ONBOARDING.value, UserStatus.APPROVED.value):
            purpose = PURPOSE_ONBOARDING if user.status == UserStatus.ONBOARDING.value else PURPOSE_CHANGE
            replies = await ask_location(db, user, purpose)
        else:
            replies = await _start_flow(db, user)
    await send_replies(update, context, replies)


async def on_web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or message.web_app_data is None or not is_private_chat(update):
        return
    try:
        payload = json.loads(message.web_app_data.data)
        raw_tz = payload.get("tz") if isinstance(payload, dict) else None
        langs = payload.get("langs") if isinstance(payload, dict) else None
        langs = [str(x) for x in langs][:10] if isinstance(langs, list) else None
    except (ValueError, TypeError):
        raw_tz, langs = None, None
    tz = timeutil.normalize_timezone(raw_tz if isinstance(raw_tz, str) else None)
    ident = ident_of(update)
    async with session_scope() as db:
        user = await users.get_by_telegram_id(db, ident.id, lock=True)
        if user is None or user.status not in (UserStatus.ONBOARDING.value, UserStatus.APPROVED.value):
            return
        purpose = (user.state_data or {}).get("purpose") or (
            PURPOSE_ONBOARDING if user.status == UserStatus.ONBOARDING.value else PURPOSE_CHANGE
        )
        if tz is None:
            await users.set_state(db, user, states.ONB_COUNTRY_TEXT, {"purpose": purpose}, STATE_TTL)
            replies = [Reply(texts.INVALID_TIMEZONE_DATA, remove_keyboard=True)]
        else:
            replies = await propose_timezone(db, user, tz, langs, purpose)
    await send_replies(update, context, replies)


async def on_timezone_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assert query is not None and query.data is not None
    ident = ident_of(update)
    async with session_scope() as db:
        user = await users.get_by_telegram_id(db, ident.id, lock=True)
        data = (user.state_data or {}) if user else {}
        if user is None or user.state != states.ONB_CONFIRM or users.state_is_expired(user) or "tz" not in data:
            replies = [Reply(toast=texts.STALE_BUTTON, alert=True)]
        elif query.data == callbacks.TZ_OK:
            replies = [Reply("✅ Time zone confirmed.", edit=True)]
            replies += await finalize_location(db, user, data["tz"], data.get("country"), data.get("purpose", PURPOSE_ONBOARDING))
        else:  # "Change"
            await users.set_state(db, user, states.ONB_COUNTRY_TEXT, {"purpose": data.get("purpose", PURPOSE_ONBOARDING)}, STATE_TTL)
            replies = [Reply(texts.ASK_COUNTRY_TEXT, edit=True)]
    await send_replies(update, context, replies)


async def on_country_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assert query is not None and query.data is not None
    code = query.data.split(":", 1)[1]
    ident = ident_of(update)
    async with session_scope() as db:
        user = await users.get_by_telegram_id(db, ident.id, lock=True)
        data = (user.state_data or {}) if user else {}
        if (
            user is None
            or user.state != states.ONB_PICK
            or users.state_is_expired(user)
            or data.get("kind") != "country"
            or code not in data.get("options", [])
        ):
            replies = [Reply(toast=texts.STALE_BUTTON, alert=True)]
        else:
            tz = data.get("tz")
            purpose = data.get("purpose", PURPOSE_ONBOARDING)
            if tz and timeutil.normalize_timezone(tz):  # ambiguous zone: the country was the only unknown
                replies = [Reply(f"🌍 Country: <b>{texts.e(timeutil.country_name(code))}</b>", edit=True)]
                replies += await finalize_location(db, user, tz, code, purpose)
            else:
                replies = [Reply(f"🌍 Country: <b>{texts.e(timeutil.country_name(code))}</b>", edit=True)]
                replies += await country_chosen(db, user, code, purpose)
    await send_replies(update, context, replies)


async def on_timezone_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assert query is not None and query.data is not None
    index = int(query.data.split(":", 1)[1])
    ident = ident_of(update)
    async with session_scope() as db:
        user = await users.get_by_telegram_id(db, ident.id, lock=True)
        data = (user.state_data or {}) if user else {}
        options = data.get("options", [])
        if (
            user is None
            or user.state != states.ONB_PICK
            or users.state_is_expired(user)
            or data.get("kind") != "tz"
            or not 0 <= index < len(options)
        ):
            replies = [Reply(toast=texts.STALE_BUTTON, alert=True)]
        else:
            replies = [Reply(f"🕒 Time zone: <b>{texts.e(options[index])}</b>", edit=True)]
            replies += await finalize_location(db, user, options[index], data.get("country"), data.get("purpose", PURPOSE_ONBOARDING))
    await send_replies(update, context, replies)


async def handle_country_text(db, user: User, text: str) -> list[Reply]:
    """State ``onb_country``: the user typed a country (or pressed the manual-entry button)."""
    purpose = (user.state_data or {}).get("purpose", PURPOSE_ONBOARDING)
    if text.strip() == MANUAL_LABEL:
        return [Reply(texts.ASK_COUNTRY_TEXT, remove_keyboard=True)]
    codes = timeutil.find_countries(text)
    if not codes:
        return [Reply(texts.COUNTRY_NOT_FOUND)]
    if len(codes) == 1:  # unambiguous (exact name / ISO code, or a single close match)
        return await country_chosen(db, user, codes[0], purpose)
    await users.set_state(db, user, states.ONB_PICK, {"kind": "country", "options": codes, "purpose": purpose}, STATE_TTL)
    return [Reply(texts.pick_country(codes), buttons=_country_buttons(codes))]


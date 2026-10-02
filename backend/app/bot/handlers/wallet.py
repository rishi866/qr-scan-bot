"""Wallet commands: /balance /history /deposit /withdraw /wallet and their button / text steps."""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import select
from telegram import Update
from telegram.ext import ContextTypes

from app import callbacks, states, texts, timeutil
from app.bot.context import Reply, ident_of, is_private_chat, send_replies
from app.bot.handlers.common import gate
from app.chain import hd as hd_mod
from app.config import get_settings
from app.db import session_scope
from app.enums import DepositMethod, Role
from app.models import TaskSession, User
from app.money import fmt_money, to_decimal
from app.services import payments, qr, settings_service, users, wallet

log = logging.getLogger(__name__)

FLOW_TTL = 15 * 60
STATUS_LABELS = {
    "awaiting_scanner": "⏳ waiting",
    "accepted": "🔧 in progress",
    "done": "🕑 awaiting confirmation",
    "confirmed": "✅ paid",
    "disputed": "⚖️ in dispute",
    "refunded": "↩️ refunded",
    "expired": "⌛ expired",
    "skipped": "⏭ skipped",
    "timed_out": "⌛ timed out",
    "cancelled": "🚫 cancelled",
}


async def _approved_or_reply(db, update: Update, *, role: Role | None = None, lock: bool = False) -> tuple[User | None, Reply | None]:
    ident = ident_of(update)
    user = await users.get_by_telegram_id(db, ident.id, lock=lock)
    problem = await gate(db, user, role=role)
    return (None, problem) if problem else (user, None)


# ── balance / history ───────────────────────────────────────────────────────


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    async with session_scope() as db:
        user, problem = await _approved_or_reply(db, update)
        if problem or user is None:
            reply = problem
        else:
            w = await wallet.get_wallet(db, user.user_id)
            reply = Reply(texts.wallet_summary(w.balance, w.pending, user.role, w.total_earned, w.total_spent))
    await send_replies(update, context, reply)


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    async with session_scope() as db:
        user, problem = await _approved_or_reply(db, update)
        if problem or user is None:
            reply = problem
        else:
            reply = await _history(db, user)
    await send_replies(update, context, reply)


async def _history(db, user: User) -> Reply:
    is_seller = user.role == Role.SELLER.value
    column = TaskSession.seller_id if is_seller else TaskSession.scanner_id
    rows = (await db.execute(select(TaskSession).where(column == user.user_id).order_by(TaskSession.session_id.desc()).limit(10))).scalars().all()
    if not rows:
        return Reply("📭 No tasks yet.")
    lines = ["🧾 <b>Recent tasks</b>"]
    for s in rows:
        when = timeutil.format_local(s.sent_at, user.timezone, "%d %b %H:%M")
        label = STATUS_LABELS.get(s.status, s.status)
        who = None
        if is_seller:
            scanner = await users.get_user(db, s.scanner_id)
            who = scanner.alias if scanner else None  # the alias is the only thing a seller may know
        amount = s.amount + s.commission if is_seller else s.amount
        lines.append(f"#{s.session_id} • " + texts.history_line(when, label, amount, who))
    return Reply("\n".join(lines))


# ── deposit (sellers) ───────────────────────────────────────────────────────


async def deposit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    async with session_scope() as db:
        user, problem = await _approved_or_reply(db, update, role=Role.SELLER)
        reply = problem if (problem or user is None) else Reply(
            texts.deposit_menu(),
            buttons=[[("🔗 BEP-20 (USDT)", callbacks.DEPOSIT_BEP20), ("🟡 Binance Pay", callbacks.DEPOSIT_BINANCE)]],
        )
    await send_replies(update, context, reply)


async def on_deposit_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assert query is not None and query.data is not None
    async with session_scope() as db:
        user, problem = await _approved_or_reply(db, update, role=Role.SELLER)
        if problem or user is None:
            reply = problem
        elif query.data == callbacks.DEPOSIT_BEP20:
            reply = await _deposit_bep20(db, user)
        else:
            cfg = await settings_service.load(db)
            if not cfg.binance_pay_id:
                reply = Reply(texts.DEPOSIT_BINANCE_UNAVAILABLE, edit=True)
            else:
                reply = Reply(texts.deposit_binance(cfg.binance_pay_id), buttons=[[("✅ I've paid", callbacks.DEPOSIT_CLAIM)]], edit=True)
    await send_replies(update, context, reply)


async def _deposit_bep20(db, user: User) -> Reply:
    try:
        hd = hd_mod.from_settings()
    except hd_mod.HDError:
        log.exception("HD wallet misconfigured")
        hd = None
    if hd is None:
        return Reply(texts.DEPOSIT_UNAVAILABLE, edit=True)
    try:
        row = await payments.get_or_create_deposit_address(db, user, hd)
    except payments.PaymentError as exc:
        return Reply(f"⚠️ {texts.e(exc)}", edit=True)
    cfg = await settings_service.load(db)
    settings = get_settings()
    caption = texts.deposit_bep20(row.address, cfg.min_deposit, settings.deposit_confirmations, settings.token_symbol)
    return Reply(caption, photo=qr.qr_png(row.address))


async def on_deposit_claim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with session_scope() as db:
        user, problem = await _approved_or_reply(db, update, role=Role.SELLER, lock=True)
        if problem or user is None:
            reply = problem
        else:
            await users.set_state(db, user, states.AWAITING_DEP_AMOUNT, None, FLOW_TTL)
            reply = Reply(texts.ASK_DEPOSIT_AMOUNT)
    await send_replies(update, context, reply)


def _parse_amount(text: str) -> Decimal | None:
    try:
        value = to_decimal(text)
    except ValueError:
        return None
    return value if value > 0 else None


async def handle_dep_amount_text(db, user: User, text: str) -> Reply:
    amount = _parse_amount(text)
    if amount is None:
        return Reply(texts.WITHDRAW_BAD_AMOUNT)
    await users.set_state(db, user, states.AWAITING_DEP_REF, {"amount": str(amount)}, FLOW_TTL)
    return Reply(texts.ASK_DEPOSIT_REFERENCE)


async def handle_dep_ref_text(db, user: User, text: str) -> Reply:
    amount = Decimal((user.state_data or {}).get("amount", "0"))
    try:
        async with db.begin_nested():
            claim = await payments.create_binance_claim(db, user, amount, text)
    except payments.PaymentError as exc:
        return Reply(f"⚠️ {texts.e(exc)}")
    await users.clear_state(db, user)
    return Reply(texts.deposit_claim_received(claim.amount, claim.tx_hash))


# ── payout addresses (scanners) ─────────────────────────────────────────────


async def wallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    async with session_scope() as db:
        user, problem = await _approved_or_reply(db, update)
        if problem or user is None:
            reply = problem
        else:
            w = await wallet.get_wallet(db, user.user_id)
            lines = [
                texts.wallet_summary(w.balance, w.pending, user.role, w.total_earned, w.total_spent),
                "",
                f"🔗 BEP-20 address: <code>{texts.e(w.bep20_address)}</code>" if w.bep20_address else "🔗 BEP-20 address: <i>not set</i>",
                f"🟡 Binance ID: <code>{texts.e(w.binance_address)}</code>" if w.binance_address else "🟡 Binance ID: <i>not set</i>",
            ]
            reply = Reply(
                "\n".join(lines),
                buttons=[[("🔗 Set BEP-20 address", callbacks.ADDRESS_BEP20)], [("🟡 Set Binance ID", callbacks.ADDRESS_BINANCE)]]
                if user.role == Role.SCANNER.value
                else None,
            )
    await send_replies(update, context, reply)


async def on_address_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assert query is not None and query.data is not None
    method = DepositMethod.BEP20.value if query.data == callbacks.ADDRESS_BEP20 else DepositMethod.BINANCE.value
    async with session_scope() as db:
        user, problem = await _approved_or_reply(db, update, role=Role.SCANNER, lock=True)
        if problem or user is None:
            reply = problem
        else:
            await users.set_state(db, user, states.AWAITING_ADDRESS, {"method": method}, FLOW_TTL)
            reply = Reply(texts.ask_address(method))
    await send_replies(update, context, reply)


async def handle_address_text(db, user: User, text: str) -> list[Reply] | Reply:
    data = user.state_data or {}
    method = data.get("method", DepositMethod.BEP20.value)
    try:
        address = await payments.set_payout_address(db, user, method, text)
    except payments.PaymentError as exc:
        return Reply(str(exc))  # texts already contain HTML-safe markup; stay in the state so the user can retry
    if data.get("then") == "withdraw":
        replies = [Reply(texts.address_saved(method, address))]
        replies.append(await _ask_amount(db, user, method, address))
        return replies
    await users.clear_state(db, user)
    return Reply(texts.address_saved(method, address))


# ── withdraw (scanners) ─────────────────────────────────────────────────────


async def withdraw_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    async with session_scope() as db:
        user, problem = await _approved_or_reply(db, update, role=Role.SCANNER)
        if problem or user is None:
            reply = problem
        else:
            cfg = await settings_service.load(db)
            w = await wallet.get_wallet(db, user.user_id)
            if w.balance < cfg.min_withdrawal:
                reply = Reply(
                    f"🏧 Your available balance ({texts.money(w.balance)}) is below the minimum withdrawal of {texts.money(cfg.min_withdrawal)}."
                )
            else:
                reply = Reply(
                    texts.withdraw_menu(w.balance, cfg.min_withdrawal),
                    buttons=[[("🔗 BEP-20", callbacks.WITHDRAW_BEP20), ("🟡 Binance Pay", callbacks.WITHDRAW_BINANCE)]],
                )
    await send_replies(update, context, reply)


async def _ask_amount(db, user: User, method: str, address: str) -> Reply:
    cfg = await settings_service.load(db)
    w = await wallet.get_wallet(db, user.user_id)
    await users.set_state(db, user, states.AWAITING_WD_AMOUNT, {"method": method, "address": address}, FLOW_TTL)
    return Reply(texts.ask_withdraw_amount(w.balance, cfg.min_withdrawal))


async def on_withdraw_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assert query is not None and query.data is not None
    method = DepositMethod.BEP20.value if query.data == callbacks.WITHDRAW_BEP20 else DepositMethod.BINANCE.value
    async with session_scope() as db:
        user, problem = await _approved_or_reply(db, update, role=Role.SCANNER, lock=True)
        if problem or user is None:
            reply = problem
        else:
            w = await wallet.get_wallet(db, user.user_id)
            saved = w.bep20_address if method == DepositMethod.BEP20.value else w.binance_address
            if saved:
                reply = await _ask_amount(db, user, method, saved)
                reply.edit = False
            else:
                await users.set_state(db, user, states.AWAITING_ADDRESS, {"method": method, "then": "withdraw"}, FLOW_TTL)
                reply = Reply(texts.ask_address(method))
    await send_replies(update, context, reply)


async def handle_wd_amount_text(db, user: User, text: str) -> Reply:
    data = user.state_data or {}
    cfg = await settings_service.load(db)
    w = await wallet.get_wallet(db, user.user_id)
    if text.strip().lower() == "all":
        amount = w.balance
    else:
        parsed = _parse_amount(text)
        if parsed is None:
            return Reply(texts.WITHDRAW_BAD_AMOUNT)
        amount = parsed
    if amount < cfg.min_withdrawal:
        return Reply(f"⚠️ The minimum withdrawal is {texts.money(cfg.min_withdrawal)}.")
    if amount > w.balance:
        return Reply(f"⚠️ You only have {texts.money(w.balance)} available.")
    if amount <= cfg.withdrawal_fee:
        return Reply("⚠️ The amount must be greater than the withdrawal fee.")
    await users.set_state(
        db, user, states.CONFIRM_WD, {"method": data["method"], "address": data["address"], "amount": fmt_money(amount, 2)}, FLOW_TTL
    )
    return Reply(
        texts.withdraw_confirm(data["method"], data["address"], amount, cfg.withdrawal_fee),
        buttons=[[("✅ Confirm", callbacks.WITHDRAW_YES), ("❌ Cancel", callbacks.WITHDRAW_NO)]],
    )


async def on_withdraw_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assert query is not None and query.data is not None
    async with session_scope() as db:
        user, problem = await _approved_or_reply(db, update, role=Role.SCANNER, lock=True)
        if problem or user is None:
            reply = problem
        elif user.state != states.CONFIRM_WD or users.state_is_expired(user) or not user.state_data:
            await users.clear_state(db, user)
            reply = Reply(texts.STALE_BUTTON, edit=True, toast=texts.STALE_BUTTON)
        elif query.data == callbacks.WITHDRAW_NO:
            await users.clear_state(db, user)
            reply = Reply(texts.SEND_CANCELLED, edit=True)
        else:
            data = user.state_data
            try:
                async with db.begin_nested():
                    wd = await payments.request_withdrawal(db, user, data["method"], data["address"], Decimal(data["amount"]))
                await users.clear_state(db, user)
                reply = Reply(texts.withdraw_requested(wd.id), edit=True)
            except payments.PaymentError as exc:
                await users.clear_state(db, user)
                reply = Reply(f"⚠️ {texts.e(exc)}", edit=True)
    await send_replies(update, context, reply)


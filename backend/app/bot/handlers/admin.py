"""Admin actions inside Telegram (approve / reject buttons on the approval request) and /admin."""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from telegram import Update
from telegram.ext import ContextTypes

from app import texts
from app.bot.context import Reply, is_admin, is_private_chat, send_replies
from app.config import get_settings
from app.db import session_scope
from app.enums import DisputeStatus, UserStatus, WithdrawalStatus
from app.models import Deposit, Dispute, User, Withdrawal
from app.services import users, wallet

log = logging.getLogger(__name__)


async def on_admin_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assert query is not None and query.data is not None
    admin_id = update.effective_user.id if update.effective_user else 0
    if not is_admin(admin_id):
        log.warning("non-admin %s pressed an admin button", admin_id)
        await send_replies(update, context, Reply(toast="You are not allowed to do this.", alert=True))
        return
    _, action, raw_id = query.data.split(":")  # adm:ap:<id> | adm:rj:<id>
    approve = action == "ap"
    original = query.message.text_html if query.message is not None and query.message.text_html else ""
    by = f"telegram:{admin_id}"
    async with session_scope() as db:
        user = await users.get_user(db, int(raw_id), lock=True)
        if user is None:
            reply = Reply(toast="User not found.", alert=True)
        elif user.status != UserStatus.PENDING.value:
            reply = Reply(
                original + f"\n\nℹ️ Already <b>{texts.e(user.status)}</b>.", edit=True, toast=f"Already {user.status}."
            )
        else:
            if approve:
                await users.approve(db, user.user_id, by)
            else:
                await users.reject(db, user.user_id, by)
            who = update.effective_user.full_name if update.effective_user else str(admin_id)
            reply = Reply(original + texts.admin_decision_suffix(approve, who), edit=True, toast="Approved" if approve else "Rejected")
    await send_replies(update, context, reply)


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update) or not is_admin(update.effective_user.id if update.effective_user else 0):
        return
    async with session_scope() as db:
        pending_users = (await db.execute(select(func.count()).select_from(User).where(User.status == UserStatus.PENDING.value))).scalar_one()
        disputes = (
            await db.execute(
                select(func.count()).select_from(Dispute).where(
                    Dispute.status.in_([DisputeStatus.PENDING_REVIEW.value, DisputeStatus.AWAITING_PROOF.value])
                )
            )
        ).scalar_one()
        withdrawals = (
            await db.execute(
                select(func.count()).select_from(Withdrawal).where(
                    Withdrawal.status.in_([WithdrawalStatus.PENDING.value, WithdrawalStatus.APPROVED.value, WithdrawalStatus.FAILED.value])
                )
            )
        ).scalar_one()
        claims = (await db.execute(select(func.count()).select_from(Deposit).where(Deposit.status.in_(["pending", "below_min"])))).scalar_one()
        report = await wallet.reconcile(db)
    base = get_settings().public_base_url
    panel = f"\n🔗 {texts.e(base)}" if base else ""
    text = (
        "🛠 <b>Admin overview</b>\n"
        f"⏳ Pending approvals: <b>{pending_users}</b>\n"
        f"⚖️ Open disputes: <b>{disputes}</b>\n"
        f"🏧 Withdrawals to handle: <b>{withdrawals}</b>\n"
        f"💳 Deposits to review: <b>{claims}</b>\n"
        f"🧮 Ledger check: {'✅ OK' if report.ok else '🚨 MISMATCH - see the panel'}"
        f"{panel}"
    )
    await send_replies(update, context, Reply(text))

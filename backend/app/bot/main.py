"""Telegram bot entry point: ``python -m app.bot.main``."""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.request import BaseRequest

from app.bot.handlers import admin, common, scanner, seller, start, wallet
from app.bot.handlers.router import on_text
from app.bot.worker import run_worker
from app.config import get_settings
from app.db import dispose_engine
from app.logging_conf import setup_logging

log = logging.getLogger(__name__)

WORKER_KEY = "worker_task"


def register_handlers(application: Application) -> None:
    add = application.add_handler
    add(TypeHandler(Update, common.rate_limit), group=-1)

    # commands
    commands = {
        "start": start.start_command,
        "help": common.help_command,
        "cancel": common.cancel_command,
        "timezone": start.timezone_command,
        "admin": admin.admin_command,
        "send": seller.send_command,
        "balance": wallet.balance_command,
        "history": wallet.history_command,
        "deposit": wallet.deposit_command,
        "wallet": wallet.wallet_command,
        "withdraw": wallet.withdraw_command,
        "myslots": scanner.myslots_command,
        "addslot": scanner.addslot_command,
        "removeslot": scanner.removeslot_command,
    }
    for name, fn in commands.items():
        add(CommandHandler(name, fn, filters=filters.ChatType.PRIVATE))

    # inline buttons
    callbacks = [
        (r"^role:(seller|scanner)$", start.on_role_chosen),
        (r"^tz:(ok|change)$", start.on_timezone_confirm),
        (r"^ctry:[A-Z]{2}$", start.on_country_pick),
        (r"^tzsel:\d{1,3}$", start.on_timezone_pick),
        (r"^adm:(ap|rj):\d+$", admin.on_admin_decision),
        (r"^sl:edit$", scanner.on_slot_edit),
        (r"^sl:t:\d{1,2}:\d{1,2}:[apm]$", scanner.on_slot_toggle),
        (r"^sl:done$", scanner.on_slot_done),
        (r"^(rdy|bsy):\d+$", scanner.on_ready_busy),
        (r"^(acc|skp|done):\d+$", scanner.on_task_action),
        (r"^send:(yes|no)$", seller.on_send_decision),
        (r"^(cfm|rej):\d+$", seller.on_report_decision),
        (r"^dep:(bep20|binance)$", wallet.on_deposit_method),
        (r"^dep:claim$", wallet.on_deposit_claim),
        (r"^addr:(bep20|binance)$", wallet.on_address_button),
        (r"^wd:(bep20|binance)$", wallet.on_withdraw_method),
        (r"^wd:(yes|no)$", wallet.on_withdraw_decision),
    ]
    for pattern, fn in callbacks:
        add(CallbackQueryHandler(fn, pattern=pattern))

    # messages
    add(MessageHandler(filters.StatusUpdate.WEB_APP_DATA & filters.ChatType.PRIVATE, start.on_web_app_data))
    add(MessageHandler((filters.PHOTO | filters.Document.IMAGE) & filters.ChatType.PRIVATE, scanner.on_proof))
    add(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_text))
    application.add_error_handler(common.on_error)


async def _post_init(application: Application) -> None:
    application.bot_data[WORKER_KEY] = asyncio.create_task(run_worker(application.bot), name="bot-worker")
    me = await application.bot.get_me()
    log.info("bot @%s started", me.username)


async def _post_shutdown(application: Application) -> None:
    task = application.bot_data.get(WORKER_KEY)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await dispose_engine()


def build_application(token: str | None = None, *, with_worker: bool = True, request: BaseRequest | None = None) -> Application:
    """``request`` lets tests plug in a fake Telegram transport."""
    token = token or get_settings().telegram_bot_token.get_secret_value()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    builder: ApplicationBuilder = Application.builder().token(token).concurrent_updates(8)
    if request is not None:
        builder = builder.request(request).get_updates_request(request)
    if with_worker:
        builder = builder.post_init(_post_init).post_shutdown(_post_shutdown)
    application = builder.build()
    register_handlers(application)
    return application


def main() -> None:
    setup_logging()
    application = build_application()
    log.info("starting long polling")
    application.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)


if __name__ == "__main__":
    main()

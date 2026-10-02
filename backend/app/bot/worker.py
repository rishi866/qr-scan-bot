"""Background loops of the bot process (outbox delivery, timers, blockchain, Binance, AI).

Everything is DB-driven and idempotent, so the loops can be restarted at any time. A failing loop
logs, backs off and keeps going - one broken dependency (RPC node, Binance, Anthropic) never takes
the rest of the bot down.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from telegram import Bot

from app.bot import delivery
from app.chain import binance as binance_mod
from app.chain import hd as hd_mod
from app.chain.bsc import AsyncBsc, async_client_from_settings
from app.chain.payout import run_payouts
from app.chain.watcher import scan_deposits
from app.config import get_settings
from app.db import session_scope
from app.services import payments, scheduler

log = logging.getLogger(__name__)


async def _loop(name: str, interval: float, fn: Callable[[], Awaitable[object]]) -> None:
    failures = 0
    while True:
        try:
            await fn()
            failures = 0
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception:
            failures += 1
            delay = min(interval * 2**min(failures, 5), 120)
            log.exception("worker loop %r failed (%d in a row) - retrying in %.0fs", name, failures, delay)
            await asyncio.sleep(delay)


async def _prepare_chain() -> AsyncBsc | None:
    settings = get_settings()
    try:
        hd = hd_mod.from_settings()
    except hd_mod.HDError as exc:
        log.error("HD wallet misconfigured, deposits disabled: %s", exc)
        return None
    if hd is None:
        log.warning("HD_XPUB / HD_MNEMONIC not set - BEP-20 deposits are disabled")
        return None
    async with session_scope() as db:
        problems = await payments.verify_hd_consistency(db, hd)
    if problems:
        log.error("HD configuration does not match stored deposit addresses, deposits disabled: %s", problems[:3])
        return None
    chain = async_client_from_settings()
    try:
        chain_id = await chain.check_chain()
        log.info("connected to chain %s via %s", chain_id, settings.bsc_rpc_url.split("//")[-1].split("/")[0])
    except Exception as exc:
        log.error("cannot verify the BSC RPC right now (%s) - will keep retrying", exc)
    return chain


async def run_worker(bot: Bot) -> None:
    """Runs until cancelled."""
    await scheduler.reset_running_ai()
    loops: list[Awaitable[None]] = [
        _loop("outbox", 1.0, lambda: delivery.deliver_due(bot)),
        _loop("sessions", 5.0, scheduler.tick_sessions),
        _loop("disputes", 15.0, scheduler.tick_disputes),
        _loop("slot-reminders", 20.0, scheduler.tick_slot_notifications),
        _loop("states", 10.0, scheduler.tick_states),
    ]

    from app.services import ai_verify  # local import: optional dependency on the anthropic SDK

    verifier = ai_verify.make_verifier()
    if verifier is not None:
        loops.append(_loop("ai-verification", 8.0, lambda: scheduler.tick_ai(verifier)))
    else:
        log.info("ANTHROPIC_API_KEY not set - disputes are reviewed by the admin only")

    chain = await _prepare_chain()
    if chain is not None:
        loops.append(_loop("deposits", 15.0, lambda: scan_deposits(chain)))
        loops.append(_loop("payouts", 20.0, lambda: run_payouts(chain)))
    binance = binance_mod.BinanceClient.from_settings()
    if binance is not None:
        loops.append(_loop("binance-claims", 60.0, lambda: binance_mod.poll_binance_claims(binance)))

    log.info("worker started with %d loops", len(loops))
    await asyncio.gather(*loops)

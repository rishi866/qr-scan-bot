"""Operations CLI: ``python -m app.cli <command>``.

    migrate        apply database migrations (alembic upgrade head); on Supabase also switches Row Level Security on
    create-admin   create (or reset the password of) a web-panel admin
    gen-wallet     generate a fresh HD wallet (mnemonic + xpub) for BEP-20 deposits
    check          verify the configuration (DB, Telegram, RPC, Binance key safety, ...)
    reconcile      verify wallets against the ledger
    rescan         re-scan the chain from a block (deposits are idempotent)
    sweep          move deposit-address balances to your treasury wallet
    seed-demo      fill a *development* database with realistic demo data
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select, text

from app.logging_conf import setup_logging


def _out(ok: bool | None, label: str, detail: str = "") -> None:
    mark = {True: "✅", False: "❌", None: "⚠️ "}[ok]
    print(f"{mark} {label}{(' - ' + detail) if detail else ''}")


# ── migrate ─────────────────────────────────────────────────────────────────


def cmd_migrate(args: argparse.Namespace) -> int:
    from alembic import command
    from alembic.config import Config

    from app import dbtools

    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    try:
        command.upgrade(cfg, "head")
    except dbtools.SchemaAccessError as exc:
        print(f"❌ {exc}")
        return 1
    print("database is up to date")
    try:
        asyncio.run(_harden())
    except Exception as exc:  # loud on purpose: on a hosted database an unprotected table may be reachable over HTTP
        print(f"❌ migrations are applied, but Row Level Security could not be switched on: {type(exc).__name__}: {str(exc)[:200]}")
        return 1
    return 0


async def _harden() -> None:
    """On Supabase-style databases: Row Level Security on every table of our schema (nothing happens elsewhere)."""
    from app import dbtools
    from app.db import dispose_engine, session_scope

    try:
        async with session_scope() as db:
            schema = await dbtools.effective_schema(db)
            changed = await dbtools.enable_row_level_security(db, schema)
    finally:
        await dispose_engine()
    if changed:
        print(f"Row Level Security switched on for {len(changed)} table(s) in schema '{schema}'")


# ── create-admin ────────────────────────────────────────────────────────────


async def _create_admin(username: str, password: str, reset: bool, disable_2fa: bool = False) -> int:
    from app.api import security
    from app.db import dispose_engine, session_scope
    from app.models import Admin

    username = username.strip().lower()
    try:
        security.validate_password_strength(password, username)
    except ValueError as exc:
        print(f"❌ {exc}")
        return 1
    async with session_scope() as db:
        existing = (await db.execute(select(Admin).where(Admin.username == username))).scalar_one_or_none()
        if existing and not reset:
            print(f"❌ '{username}' already exists (use --reset to set a new password)")
            return 1
        if existing:
            existing.password_hash = security.hash_password(password)
            existing.token_version += 1
            existing.failed_attempts = 0
            existing.locked_until = None
            print(f"✅ password of '{username}' was reset; all its sessions were signed out")
            if disable_2fa:
                existing.totp_enabled = False
                existing.totp_secret_enc = None
                existing.last_totp_step = None
                print("✅ two-factor authentication was switched off - enrol again under Settings → Security")
        else:
            db.add(Admin(username=username, password_hash=security.hash_password(password)))
            print(f"✅ admin '{username}' created")
    await dispose_engine()
    return 0


def cmd_create_admin(args: argparse.Namespace) -> int:
    password = args.password or os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Password (min 12 chars): ")
    if not args.password and not os.environ.get("ADMIN_PASSWORD") and getpass.getpass("Repeat password: ") != password:
        print("❌ passwords do not match")
        return 1
    if args.disable_2fa and not args.reset:
        print("❌ --disable-2fa only works together with --reset (it is the recovery path for a lost authenticator)")
        return 1
    return asyncio.run(_create_admin(args.username, password, args.reset, args.disable_2fa))


# ── helpers for the deployment scripts ─────────────────────────────────────


def cmd_admin_exists(args: argparse.Namespace) -> int:
    """Exit status 0 when at least one panel admin exists (used by deploy/install_vps.sh)."""

    async def run() -> bool:
        from app.db import dispose_engine, session_scope
        from app.models import Admin

        try:
            async with session_scope() as db:
                return (await db.execute(select(Admin.id).limit(1))).first() is not None
        finally:
            await dispose_engine()

    return 0 if asyncio.run(run()) else 1


def cmd_dump_url(args: argparse.Namespace) -> int:
    """Print DATABASE_URL in libpq form for pg_dump / psql. CONTAINS THE PASSWORD - only for the backup scripts."""
    from app import dbtools
    from app.config import get_settings

    print(dbtools.libpq_url(get_settings().database_url, os.environ.get("PGSSLROOTCERT") or None))
    return 0


# ── gen-wallet ──────────────────────────────────────────────────────────────


def cmd_gen_wallet(args: argparse.Namespace) -> int:
    from app.chain.hd import HDWallet, generate_mnemonic

    mnemonic = generate_mnemonic(args.words)
    wallet = HDWallet(mnemonic=mnemonic, account=args.account)
    print("=" * 78)
    print("NEW HD WALLET - write the words below on paper. Anyone who has them can take ALL funds.")
    print("Do NOT put the mnemonic on the VPS and do not paste it into chats or tickets.")
    print("=" * 78)
    print(f"\nMnemonic ({args.words} words):\n  {mnemonic}\n")
    print("Put ONLY this line into the server's .env (watch-only: it can derive deposit")
    print("addresses but can never spend):\n")
    print(f"  HD_XPUB={wallet.xpub}\n")
    print("First deposit addresses (user_id 1, 2, 3):")
    for i in (1, 2, 3):
        print(f"  index {i}: {wallet.address(i)}")
    print("\nTo sweep deposits later, run `python -m app.cli sweep` on a trusted machine that has the mnemonic.")
    return 0


# ── reconcile / rescan ──────────────────────────────────────────────────────


async def _reconcile() -> int:
    from app.db import dispose_engine, session_scope
    from app.services import wallet

    async with session_scope() as db:
        report = await wallet.reconcile(db)
    await dispose_engine()
    _out(report.ok, "ledger reconciliation", "" if report.ok else "; ".join(report.problems[:5]))
    print(f"   users hold {report.user_total}, platform commission {report.platform_balance}")
    return 0 if report.ok else 1


def cmd_reconcile(args: argparse.Namespace) -> int:
    return asyncio.run(_reconcile())


async def _rescan(from_block: int) -> int:
    from app.chain.watcher import set_cursor
    from app.db import dispose_engine, session_scope

    async with session_scope() as db:
        await set_cursor(db, max(from_block - 1, 0))
    await dispose_engine()
    print(f"✅ the next scan starts at block {from_block} (already credited deposits are skipped automatically)")
    return 0


def cmd_rescan(args: argparse.Namespace) -> int:
    return asyncio.run(_rescan(args.from_block))


# ── sweep ───────────────────────────────────────────────────────────────────


async def _sweep(args: argparse.Namespace) -> int:
    from app.chain import hd as hd_mod
    from app.chain.bsc import async_client_from_settings
    from app.chain.sweeper import sweep
    from app.config import get_settings
    from app.db import dispose_engine

    settings = get_settings()
    mnemonic = settings.hd_mnemonic.get_secret_value() or os.environ.get("HD_MNEMONIC") or getpass.getpass("HD mnemonic (hidden): ")
    hd = hd_mod.HDWallet(mnemonic=mnemonic, xpub=settings.hd_xpub.strip(), account=0)
    gas_key = os.environ.get("GAS_PRIVATE_KEY") or ("" if args.dry_run else getpass.getpass("Gas wallet private key (hidden, holds a little BNB): "))
    treasury = args.treasury or os.environ.get("TREASURY_ADDRESS", "")
    chain = async_client_from_settings()
    await chain.check_chain()
    actions = await sweep(chain, hd, treasury, gas_key or "0x" + "11" * 32, min_amount=Decimal(str(args.min)), dry_run=args.dry_run)
    for a in actions:
        print(f"  user {a.user_id:>5}  {a.address}  {a.amount:>14}  {a.status}  {a.tx_hash or ''} {a.detail}")
    total = sum((a.amount for a in actions if a.status in ("planned", "swept")), Decimal(0))
    print(f"{'would sweep' if args.dry_run else 'swept'} {total} across {len(actions)} address(es)")
    await dispose_engine()
    return 1 if any(a.status == "failed" for a in actions) else 0


def cmd_sweep(args: argparse.Namespace) -> int:
    if not args.dry_run and not args.yes:
        target = args.treasury or os.environ.get("TREASURY_ADDRESS", "?")
        print(f"This will move USDT from deposit addresses (>= {args.min}) to {target}.")
        if input("Type 'sweep' to continue: ").strip() != "sweep":
            print("aborted")
            return 1
    return asyncio.run(_sweep(args))


# ── check ───────────────────────────────────────────────────────────────────


def _alembic_head() -> str | None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    return ScriptDirectory.from_config(cfg).get_current_head()


async def _check() -> int:
    from app import dbtools
    from app.chain import hd as hd_mod
    from app.chain.bsc import async_client_from_settings
    from app.config import _INSECURE_DEFAULT_SECRET, get_settings
    from app.db import dispose_engine, session_scope

    settings = get_settings()
    problems = 0

    secret = settings.secret_key.get_secret_value()
    ok = secret != _INSECURE_DEFAULT_SECRET and len(secret) >= 32
    _out(ok, "SECRET_KEY", "random, >= 32 chars" if ok else "set a random value: openssl rand -hex 32")
    problems += not ok

    for status, label, detail in dbtools.hosted_db_advice(settings.database_url, settings.db_schema, settings.db_pool_size + settings.db_max_overflow):
        _out(status, label, detail)
        problems += status is False

    try:
        async with session_scope() as db:
            await db.execute(text("SELECT 1"))
            version = (await db.execute(text("SELECT version_num FROM alembic_version"))).scalar_one_or_none()
            schema = await dbtools.effective_schema(db)
            hosted = bool(await dbtools.api_roles_present(db))
            exposure = await dbtools.exposure_problems(db, schema)
        head = _alembic_head()
        current = version == head
        where = f", schema '{schema}'" if settings.db_schema else ""
        _out(current, "database", f"migration {version}{where}" + ("" if current else f" (latest is {head}: run `python -m app.cli migrate`)"))
        problems += not current
        for item in exposure:
            _out(False, "exposed over HTTP", item)
            problems += 1
        if hosted and not exposure:
            _out(True, "exposed over HTTP", f"no - schema '{schema}' is not reachable through Supabase's Data API and Row Level Security is on")
    except Exception as exc:
        _out(False, "database", f"{type(exc).__name__}: {str(exc)[:120]}")
        problems += 1

    token = settings.telegram_bot_token.get_secret_value()
    if not token:
        _out(False, "TELEGRAM_BOT_TOKEN", "not set")
        problems += 1
    else:
        try:
            from telegram import Bot

            async with Bot(token) as bot:
                me = await bot.get_me()
            _out(True, "Telegram bot", f"@{me.username}")
        except Exception as exc:
            _out(False, "Telegram bot", f"{type(exc).__name__}: {str(exc)[:100]}")
            problems += 1
    _out(bool(settings.admin_telegram_ids), "ADMIN_TELEGRAM_IDS", f"{len(settings.admin_telegram_ids)} admin(s)" if settings.admin_telegram_ids else "empty - nobody gets approval requests in Telegram")
    problems += not settings.admin_telegram_ids
    https = settings.public_base_url.startswith("https://")
    _out(https or None, "PUBLIC_BASE_URL", settings.public_base_url or "not set - the time-zone Mini App needs a public https URL (manual country entry still works)")

    try:
        hd = hd_mod.from_settings()
        if hd is None:
            _out(None, "BEP-20 deposits", "HD_XPUB / HD_MNEMONIC not set - deposits disabled")
        else:
            _out(True, "HD wallet", ("mnemonic on the server (can sweep)" if hd.can_sign else "watch-only xpub (recommended)"))
            if hd.can_sign:
                _out(None, "HD_MNEMONIC", "a mnemonic on the server is a risk - prefer HD_XPUB and sweep from your own machine")
    except hd_mod.HDError as exc:
        _out(False, "HD wallet", str(exc))
        problems += 1

    try:
        chain = async_client_from_settings()
        chain_id = await asyncio.wait_for(chain.check_chain(), 15)
        latest = await asyncio.wait_for(chain.latest_block(), 15)
        _out(True, "BSC RPC", f"chain {chain_id}, block {latest}")
    except Exception as exc:
        _out(False, "BSC RPC", f"{type(exc).__name__}: {str(exc)[:100]}")
        problems += 1

    pk = settings.payout_private_key.get_secret_value()
    if settings.auto_payout_enabled and not pk:
        _out(False, "auto payout", "AUTO_PAYOUT_ENABLED=true but PAYOUT_PRIVATE_KEY is empty")
        problems += 1
    elif settings.auto_payout_enabled:
        _out(None, "auto payout", f"ON (max {settings.auto_payout_max_amount} per payout) - keep only a limited float in that hot wallet")
    else:
        _out(True, "auto payout", "off (manual payouts)")

    if settings.binance_api_key.get_secret_value():
        try:
            from app.chain.binance import BinanceClient

            client = BinanceClient.from_settings()
            assert client is not None
            data = await client._signed_get("/sapi/v1/account/apiRestrictions")
            await client.aclose()
            if data.get("enableWithdrawals"):
                _out(False, "Binance API key", "WITHDRAWALS ARE ENABLED on this key - create a read-only key!")
                problems += 1
            else:
                _out(True, "Binance API key", "read-only" + ("" if data.get("ipRestrict") else " (add an IP whitelist!)"))
        except Exception as exc:
            _out(False, "Binance API key", f"{type(exc).__name__}: {str(exc)[:100]}")
            problems += 1
    else:
        _out(None, "Binance", "not configured - Binance deposits are confirmed manually in the panel")

    _out(True if settings.anthropic_api_key.get_secret_value() else None, "AI screenshot check", f"model {settings.ai_model}" if settings.anthropic_api_key.get_secret_value() else "ANTHROPIC_API_KEY not set - every dispute goes to the admin")
    _out(settings.cookie_secure or None, "COOKIE_SECURE", "true" if settings.cookie_secure else "false (only acceptable for local http development)")
    await dispose_engine()
    print("\nAll good." if not problems else f"\n{problems} problem(s) need attention.")
    return 1 if problems else 0


def cmd_check(args: argparse.Namespace) -> int:
    return asyncio.run(_check())


# ── seed-demo ───────────────────────────────────────────────────────────────


def cmd_seed_demo(args: argparse.Namespace) -> int:
    from app.demo import seed_demo

    return asyncio.run(seed_demo(purge_only=args.purge))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply database migrations").set_defaults(fn=cmd_migrate)

    p = sub.add_parser("create-admin", help="create a web-panel admin")
    p.add_argument("--username", default="admin")
    p.add_argument("--password", help="omit to be prompted (recommended)")
    p.add_argument("--reset", action="store_true", help="reset the password of an existing admin")
    p.add_argument("--disable-2fa", action="store_true", help="with --reset: also switch two-factor authentication off (lost authenticator)")
    p.set_defaults(fn=cmd_create_admin)

    p = sub.add_parser("gen-wallet", help="generate an HD wallet for deposits")
    p.add_argument("--words", type=int, choices=(12, 24), default=12)
    p.add_argument("--account", type=int, default=0)
    p.set_defaults(fn=cmd_gen_wallet)

    sub.add_parser("admin-exists", help="exit 0 if a panel admin exists (for scripts)").set_defaults(fn=cmd_admin_exists)
    sub.add_parser("dump-url", help="print DATABASE_URL in libpq form (contains the password; for the backup scripts)").set_defaults(fn=cmd_dump_url)
    sub.add_parser("check", help="verify configuration").set_defaults(fn=cmd_check)
    sub.add_parser("reconcile", help="verify wallets against the ledger").set_defaults(fn=cmd_reconcile)

    p = sub.add_parser("rescan", help="re-scan the chain from a block")
    p.add_argument("--from-block", type=int, required=True)
    p.set_defaults(fn=cmd_rescan)

    p = sub.add_parser("sweep", help="sweep deposit addresses to the treasury wallet")
    p.add_argument("--treasury", help="destination address (or TREASURY_ADDRESS env)")
    p.add_argument("--min", type=float, default=1.0, help="ignore deposit addresses holding less than this")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p.set_defaults(fn=cmd_sweep)

    p = sub.add_parser("seed-demo", help="fill a development database with demo data")
    p.add_argument("--purge", action="store_true", help="only remove previously seeded demo data")
    p.set_defaults(fn=cmd_seed_demo)

    args = parser.parse_args(argv)
    setup_logging()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

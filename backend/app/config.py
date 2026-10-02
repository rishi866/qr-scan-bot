"""Process-level configuration, read from environment variables / ``.env``.

Business rules that the admin changes at runtime (commission, timeouts, slot length,
allowed URL domains, ...) live in the database ``settings`` table instead - see
``app.services.settings_service``.
"""

from __future__ import annotations

import re
from decimal import Decimal
from functools import lru_cache
from typing import Annotated

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import make_url

# Binance-Peg USDT on BNB Smart Chain (BEP-20). 18 decimals on BSC.
USDT_BSC = "0x55d398326f99059fF775485246999027B3197955"

RESERVED_SCHEMAS = frozenset({"public", "information_schema", "extensions", "auth", "storage", "graphql", "graphql_public", "realtime", "vault"})

_INSECURE_DEFAULT_SECRET = "change-me-in-production-change-me-in-production"  # noqa: S105 - sentinel, rejected in production


def normalize_database_url(raw: str) -> str:
    """Accept the connection strings people actually paste and turn them into what SQLAlchemy + asyncpg need.

    * ``postgres://`` / ``postgresql://`` -> ``postgresql+asyncpg://``
    * libpq's ``sslmode=`` -> asyncpg's ``ssl=``; Prisma-style ``pgbouncer=true`` is dropped (asyncpg would reject it)
    * Supabase hosts get ``ssl=require`` unless a mode was chosen (Supabase accepts unencrypted connections otherwise)
    """
    url = make_url(raw)
    if url.drivername in ("postgres", "postgresql"):
        url = url.set(drivername="postgresql+asyncpg")
    query = dict(url.query)
    if "sslmode" in query:
        query.setdefault("ssl", query["sslmode"])
        del query["sslmode"]
    query.pop("pgbouncer", None)
    host = (url.host or "").lower()
    if "ssl" not in query and host.endswith((".supabase.co", ".supabase.com")):
        query["ssl"] = "require"
    return url.set(query=query).render_as_string(hide_password=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── core ────────────────────────────────────────────────────────────────
    environment: str = "production"  # production | development | test
    database_url: str = "postgresql+asyncpg://qrbot:qrbot@localhost:5432/qrbot"
    secret_key: SecretStr = SecretStr(_INSECURE_DEFAULT_SECRET)
    public_base_url: str = ""  # e.g. https://admin.example.com (needed for the timezone Mini App)
    log_level: str = "INFO"
    upload_dir: str = "./data/uploads"

    # ── database tuning (hosted Postgres such as Supabase) ─────────────────
    db_schema: str = ""  # keep every table in this schema (e.g. "qrbot"); empty = the database's default schema
    db_pool_size: int = Field(10, ge=1, le=100)  # per process (the API and the bot are separate processes)
    db_max_overflow: int = Field(10, ge=0, le=100)
    db_pool_recycle_seconds: int = Field(1800, ge=60)  # replace connections before a pooler / firewall drops them

    # ── telegram ───────────────────────────────────────────────────────────
    telegram_bot_token: SecretStr = SecretStr("")
    admin_telegram_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    # ── admin web panel / API ──────────────────────────────────────────────
    admin_bootstrap_username: str = "admin"
    admin_bootstrap_password: SecretStr = SecretStr("")  # creates the first admin if none exists
    cookie_secure: bool = True  # set false only for plain-http local development
    session_ttl_hours: int = 12
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)  # dev only
    admin_static_dir: str = ""  # serve the exported Next.js panel from the API process
    totp_issuer: str = "QR Exchange Admin"

    # ── BSC / BEP-20 ───────────────────────────────────────────────────────
    bsc_rpc_url: str = "https://bsc-dataseed.binance.org"
    bsc_chain_id: int = 56
    token_contract: str = USDT_BSC
    token_symbol: str = "USDT"  # noqa: S105 - ticker, not a secret
    token_decimals: int = 18
    deposit_confirmations: int = 15
    bsc_scan_chunk: int = 2000  # blocks per eth_getLogs call
    bsc_scan_start_block: int | None = None  # only used on the very first scan
    hd_xpub: str = ""  # watch-only account xpub (m/44'/60'/0')  - recommended
    hd_mnemonic: SecretStr = SecretStr("")  # only if the server should also be able to sweep
    payout_private_key: SecretStr = SecretStr("")  # hot wallet for automatic BEP-20 payouts
    auto_payout_enabled: bool = False
    auto_payout_max_amount: Decimal = Decimal("50")

    # ── Binance (read-only API key; never enable withdrawals on it) ────────
    binance_api_key: SecretStr = SecretStr("")
    binance_api_secret: SecretStr = SecretStr("")
    binance_base_url: str = "https://api.binance.com"

    # ── AI screenshot verification (optional) ──────────────────────────────
    anthropic_api_key: SecretStr = SecretStr("")
    ai_model: str = "claude-opus-5-5"  # any vision-capable Claude model id; cheaper options: claude-sonnet-5-5, claude-haiku-4-5

    # ── validators ─────────────────────────────────────────────────────────
    @field_validator("admin_telegram_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, v):
        if v is None or v == "":
            return []
        if isinstance(v, str):
            return [int(x) for x in v.replace(";", ",").split(",") if x.strip()]
        if isinstance(v, int):
            return [v]
        return v

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_origins(cls, v):
        if v is None or v == "":
            return []
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

    @field_validator("public_base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, v: str) -> str:
        return normalize_database_url(v)

    @field_validator("db_schema")
    @classmethod
    def _check_schema(cls, v: str) -> str:
        v = v.strip().lower()
        if not v:
            return ""
        # the name is spliced into `SET search_path`, so it must be a plain identifier
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", v) or v in RESERVED_SCHEMAS or v.startswith("pg_"):
            raise ValueError(
                "DB_SCHEMA must be a plain lower-case identifier (letters, digits, underscore) and not "
                "public / information_schema / pg_*; leave it empty to use the database's default schema"
            )
        return v

    @model_validator(mode="after")
    def _check_production_secrets(self):
        if self.environment == "production":
            key = self.secret_key.get_secret_value()
            if key == _INSECURE_DEFAULT_SECRET or len(key) < 32:
                raise ValueError(
                    "SECRET_KEY must be set to a random string of at least 32 characters "
                    "(e.g. `openssl rand -hex 32`) when ENVIRONMENT=production"
                )
            url = make_url(self.database_url)
            if url.host == "db" and not url.password:  # docker-compose composes this URL from POSTGRES_PASSWORD
                raise ValueError(
                    "DATABASE_URL points at the 'db' container but has no password: set POSTGRES_PASSWORD in deploy/.env "
                    "(own PostgreSQL container) or DATABASE_URL (a hosted database such as Supabase, see docs/SUPABASE.md)"
                )
        return self

    # ── helpers ────────────────────────────────────────────────────────────
    @property
    def is_test(self) -> bool:
        return self.environment == "test"

    @property
    def token_unit(self) -> int:
        return 10**self.token_decimals


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Used by tests that tweak environment variables."""
    get_settings.cache_clear()

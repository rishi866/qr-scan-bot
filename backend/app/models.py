"""ORM models.

The eight tables from the product spec (users, scanner_slots, sessions, wallets,
transaction_history, disputes, deposits, withdrawals) keep the requested column names; extra
columns/tables exist for production concerns (ledger, outbox, audit log, admin accounts, ...).

All timestamps are timezone-aware UTC.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.enums import (
    BroadcastAudience,
    DepositMethod,
    DepositStatus,
    DisputeStatus,
    LedgerType,
    OutboxStatus,
    Role,
    SessionStatus,
    SlotResponse,
    TxStatus,
    UserStatus,
    WithdrawalStatus,
    sql_in,
)
from app.timeutil import utcnow

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

MONEY = Numeric(20, 8)
ZERO = text("0")
JSON_TYPE = JSONB()
TS = DateTime(timezone=True)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _created() -> Mapped[dt.datetime]:
    return mapped_column(TS, default=utcnow, server_default=func.now(), nullable=False)


# ── users ───────────────────────────────────────────────────────────────────


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(sql_in("role", Role), name="role_valid"),
        CheckConstraint(sql_in("status", UserStatus), name="status_valid"),
        CheckConstraint("reputation BETWEEN 0 AND 100", name="reputation_range"),
        Index("ix_users_role_status", "role", "status"),
    )

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64))  # @handle, admin-only
    name: Mapped[str] = mapped_column(String(255), nullable=False)  # Telegram display name, admin-only
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    country: Mapped[str | None] = mapped_column(String(2))  # ISO 3166-1 alpha-2
    timezone: Mapped[str | None] = mapped_column(String(64))  # IANA name
    language_code: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(
        String(16), default=UserStatus.ONBOARDING.value, server_default=UserStatus.ONBOARDING.value, nullable=False
    )
    approved_at: Mapped[dt.datetime | None] = mapped_column(TS)
    created_at: Mapped[dt.datetime] = _created()

    # scanner-only ------------------------------------------------------------
    alias: Mapped[str | None] = mapped_column(String(32), unique=True)  # "user1" - the only name sellers see
    reputation: Mapped[int] = mapped_column(Integer, default=50, server_default=text("50"), nullable=False)
    last_assigned_at: Mapped[dt.datetime | None] = mapped_column(TS)  # fair rotation between scanners

    # bot plumbing ------------------------------------------------------------
    state: Mapped[str | None] = mapped_column(String(40))  # current conversation step
    state_data: Mapped[dict[str, Any] | None] = mapped_column(JSON_TYPE)
    state_expires_at: Mapped[dt.datetime | None] = mapped_column(TS)
    bot_blocked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    admin_note: Mapped[str | None] = mapped_column(Text)


class ScannerSlot(Base):
    """A recurring daily window in the scanner's *local* time, e.g. 08-10 ``Asia/Kolkata``."""

    __tablename__ = "scanner_slots"
    __table_args__ = (
        UniqueConstraint("user_id", "slot_start", "slot_end", name="uq_scanner_slots_user_window"),
        CheckConstraint("slot_start BETWEEN 0 AND 23", name="start_hour"),
        CheckConstraint("slot_end BETWEEN 0 AND 23", name="end_hour"),
        Index("ix_scanner_slots_active", "is_active", "timezone"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(32))  # mirror of users.alias (kept in sync by services.users)
    slot_start: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # local hour 0-23
    slot_end: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # local hour 0-23 (0 == midnight)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"), nullable=False)
    reputation: Mapped[int] = mapped_column(Integer, default=50, server_default=text("50"), nullable=False)  # mirror
    created_at: Mapped[dt.datetime] = _created()


class SlotNotification(Base):
    """One 10-minute pre-slot reminder (and the scanner's Ready / Busy answer) per slot occurrence."""

    __tablename__ = "slot_notifications"
    __table_args__ = (
        UniqueConstraint("slot_id", "occurrence_start", name="uq_slot_notifications_occurrence"),
        CheckConstraint("response IS NULL OR " + sql_in("response", SlotResponse), name="response_valid"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    slot_id: Mapped[int] = mapped_column(ForeignKey("scanner_slots.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    occurrence_start: Mapped[dt.datetime] = mapped_column(TS, nullable=False)
    occurrence_end: Mapped[dt.datetime] = mapped_column(TS, nullable=False)
    sent_at: Mapped[dt.datetime] = _created()
    response: Mapped[str | None] = mapped_column(String(8))
    responded_at: Mapped[dt.datetime | None] = mapped_column(TS)


# ── task sessions ───────────────────────────────────────────────────────────


class TaskSession(Base):
    """One URL handed from a seller to a scanner (table ``sessions``)."""

    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint(sql_in("status", SessionStatus), name="status_valid"),
        Index("ix_sessions_status_expires", "status", "expires_at"),
        Index("ix_sessions_status_deadline", "status", "deadline_at"),
        Index("ix_sessions_seller_status", "seller_id", "status"),
        Index("ix_sessions_scanner_status", "scanner_id", "status"),
    )

    session_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    seller_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"), nullable=False)
    scanner_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=SessionStatus.AWAITING_SCANNER.value)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)  # paid to the scanner
    commission: Mapped[Decimal] = mapped_column(MONEY, nullable=False)  # kept by the platform (charged on top)
    slot_id: Mapped[int | None] = mapped_column(ForeignKey("scanner_slots.id", ondelete="SET NULL"))
    slot_label: Mapped[str | None] = mapped_column(String(80))  # e.g. "08-10 Asia/Kolkata"
    seller_timezone: Mapped[str | None] = mapped_column(String(64))
    sent_at: Mapped[dt.datetime] = _created()
    accepted_at: Mapped[dt.datetime | None] = mapped_column(TS)
    done_at: Mapped[dt.datetime | None] = mapped_column(TS)
    confirmed_at: Mapped[dt.datetime | None] = mapped_column(TS)
    expires_at: Mapped[dt.datetime | None] = mapped_column(TS)  # scanner accept/skip deadline
    deadline_at: Mapped[dt.datetime | None] = mapped_column(TS)  # next hard deadline for the current status
    prompt_at: Mapped[dt.datetime | None] = mapped_column(TS)  # when to ask the seller to confirm
    prompted_at: Mapped[dt.datetime | None] = mapped_column(TS)
    closed_reason: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[dt.datetime] = _created()


class TransactionHistory(Base):
    """Immutable-ish financial record, written once the seller decides (confirm / reject)."""

    __tablename__ = "transaction_history"
    __table_args__ = (
        CheckConstraint(sql_in("status", TxStatus), name="status_valid"),
        Index("ix_transaction_history_confirmed_at", "confirmed_at"),
        Index("ix_transaction_history_seller", "seller_id"),
        Index("ix_transaction_history_scanner", "scanner_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.session_id"), unique=True, nullable=False)
    seller_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"), nullable=False)
    scanner_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"), nullable=False)
    scanner_name: Mapped[str | None] = mapped_column(String(32))  # alias at the time
    seller_name: Mapped[str | None] = mapped_column(String(255))
    seller_country: Mapped[str | None] = mapped_column(String(2))
    seller_timezone: Mapped[str | None] = mapped_column(String(64))
    scanner_country: Mapped[str | None] = mapped_column(String(2))
    scanner_timezone: Mapped[str | None] = mapped_column(String(64))
    slot: Mapped[str | None] = mapped_column(String(80))
    url: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    commission: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    confirmed_at: Mapped[dt.datetime | None] = mapped_column(TS)
    created_at: Mapped[dt.datetime] = _created()


class Dispute(Base):
    __tablename__ = "disputes"
    __table_args__ = (
        CheckConstraint(sql_in("status", DisputeStatus), name="status_valid"),
        Index("ix_disputes_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.session_id"), unique=True, nullable=False)
    raised_by: Mapped[int] = mapped_column(ForeignKey("users.user_id"), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    proof_file: Mapped[str | None] = mapped_column(String(255))  # relative to UPLOAD_DIR
    proof_telegram_file_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=DisputeStatus.AWAITING_PROOF.value)
    resolution: Mapped[str | None] = mapped_column(String(32))
    admin_notes: Mapped[str | None] = mapped_column(Text)
    resolved_by: Mapped[str | None] = mapped_column(String(64))  # admin username or "ai"
    resolved_at: Mapped[dt.datetime | None] = mapped_column(TS)
    proof_deadline_at: Mapped[dt.datetime | None] = mapped_column(TS)
    ai_status: Mapped[str | None] = mapped_column(String(12))  # pending | done | skipped | error
    ai_verdict: Mapped[dict[str, Any] | None] = mapped_column(JSON_TYPE)
    created_at: Mapped[dt.datetime] = _created()


# ── money ───────────────────────────────────────────────────────────────────


class Wallet(Base):
    __tablename__ = "wallets"
    __table_args__ = (
        CheckConstraint("balance >= 0", name="balance_non_negative"),
        CheckConstraint("pending >= 0", name="pending_non_negative"),
    )

    user_id: Mapped[int] = mapped_column(ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True)
    balance: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0), server_default=ZERO, nullable=False)
    pending: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0), server_default=ZERO, nullable=False)
    total_earned: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0), server_default=ZERO, nullable=False)
    total_spent: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0), server_default=ZERO, nullable=False)
    total_deposited: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0), server_default=ZERO, nullable=False)
    total_withdrawn: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0), server_default=ZERO, nullable=False)
    binance_address: Mapped[str | None] = mapped_column(String(64))  # Binance Pay ID / UID
    bep20_address: Mapped[str | None] = mapped_column(String(42))  # payout address on BSC
    updated_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow, server_default=func.now())


class LedgerEntry(Base):
    """Append-only record of every change to a wallet (``user_id`` NULL = the platform's own account)."""

    __tablename__ = "ledger_entries"
    __table_args__ = (
        CheckConstraint(sql_in("entry_type", LedgerType), name="type_valid"),
        Index("ix_ledger_entries_user", "user_id", "id"),
        Index("ix_ledger_entries_ref", "ref_type", "ref_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.user_id"))
    entry_type: Mapped[str] = mapped_column(String(24), nullable=False)
    balance_delta: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0), server_default=ZERO, nullable=False)
    pending_delta: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0), server_default=ZERO, nullable=False)
    balance_after: Mapped[Decimal | None] = mapped_column(MONEY)
    pending_after: Mapped[Decimal | None] = mapped_column(MONEY)
    ref_type: Mapped[str | None] = mapped_column(String(24))
    ref_id: Mapped[int | None] = mapped_column(BigInteger)
    note: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[dt.datetime] = _created()


class DepositAddress(Base):
    """Unique BEP-20 deposit address per seller. ``derivation_index`` == ``user_id`` (m/44'/60'/0'/0/i)."""

    __tablename__ = "deposit_addresses"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True)
    address: Mapped[str] = mapped_column(String(42), unique=True, nullable=False)
    derivation_index: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    created_at: Mapped[dt.datetime] = _created()


class Deposit(Base):
    __tablename__ = "deposits"
    __table_args__ = (
        UniqueConstraint("method", "tx_hash", "log_index", name="uq_deposits_tx"),
        CheckConstraint(sql_in("method", DepositMethod), name="method_valid"),
        CheckConstraint(sql_in("status", DepositStatus), name="status_valid"),
        Index("ix_deposits_user", "user_id"),
        Index("ix_deposits_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    tx_hash: Mapped[str] = mapped_column(String(100), nullable=False)  # BSC tx hash / Binance order id
    log_index: Mapped[int] = mapped_column(Integer, default=0, server_default=ZERO, nullable=False)
    address: Mapped[str | None] = mapped_column(String(64))  # receiving address (BEP-20)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    note: Mapped[str | None] = mapped_column(String(255))
    block_number: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[dt.datetime] = _created()
    credited_at: Mapped[dt.datetime | None] = mapped_column(TS)


class Withdrawal(Base):
    __tablename__ = "withdrawals"
    __table_args__ = (
        CheckConstraint(sql_in("method", DepositMethod), name="method_valid"),
        CheckConstraint(sql_in("status", WithdrawalStatus), name="status_valid"),
        Index("ix_withdrawals_status", "status"),
        Index("ix_withdrawals_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.user_id"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)  # debited from the scanner's wallet
    fee: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0), server_default=ZERO, nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    address: Mapped[str] = mapped_column(String(64), nullable=False)
    tx_hash: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(12), nullable=False, default=WithdrawalStatus.PENDING.value)
    admin_note: Mapped[str | None] = mapped_column(String(255))
    processed_by: Mapped[str | None] = mapped_column(String(64))
    processed_at: Mapped[dt.datetime | None] = mapped_column(TS)
    created_at: Mapped[dt.datetime] = _created()


# ── plumbing ────────────────────────────────────────────────────────────────


class Outbox(Base):
    """Transactional outbox: services enqueue Telegram messages in the same DB transaction as the
    state change; the bot worker delivers them (retry, rate limit, blocked-bot handling)."""

    __tablename__ = "outbox"
    __table_args__ = (
        CheckConstraint(sql_in("status", OutboxStatus), name="status_valid"),
        Index("ix_outbox_due", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)  # HTML message text
    parse_mode: Mapped[str | None] = mapped_column(String(10), default="HTML")
    reply_markup: Mapped[dict[str, Any] | None] = mapped_column(JSON_TYPE)
    disable_preview: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"), nullable=False)
    status: Mapped[str] = mapped_column(String(10), default=OutboxStatus.QUEUED.value, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=ZERO, nullable=False)
    next_attempt_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow, server_default=func.now(), nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str | None] = mapped_column(String(120), unique=True)
    broadcast_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[dt.datetime] = _created()
    sent_at: Mapped[dt.datetime | None] = mapped_column(TS)


class Broadcast(Base):
    __tablename__ = "broadcasts"
    __table_args__ = (CheckConstraint(sql_in("audience", BroadcastAudience), name="audience_valid"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    audience: Mapped[str] = mapped_column(String(10), nullable=False)
    total: Mapped[int] = mapped_column(Integer, default=0, server_default=ZERO, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[dt.datetime] = _created()


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON_TYPE, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow, server_default=func.now())
    updated_by: Mapped[str | None] = mapped_column(String(64))


class KVState(Base):
    """Small internal key/value store (e.g. the last scanned BSC block)."""

    __tablename__ = "kv_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(TS, default=utcnow, onupdate=utcnow, server_default=func.now())


class Admin(Base):
    """Web-panel administrator (separate from Telegram identities)."""

    __tablename__ = "admins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    totp_secret_enc: Mapped[str | None] = mapped_column(Text)  # Fernet-encrypted base32 secret
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    last_totp_step: Mapped[int | None] = mapped_column(BigInteger)  # replay protection: a code works once
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default=ZERO, nullable=False)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=ZERO, nullable=False)
    locked_until: Mapped[dt.datetime | None] = mapped_column(TS)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(TS)
    created_at: Mapped[dt.datetime] = _created()


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_created", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    admin_username: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON_TYPE)
    ip: Mapped[str | None] = mapped_column(String(45))
    created_at: Mapped[dt.datetime] = _created()

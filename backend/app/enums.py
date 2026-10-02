"""String enums shared by models, services, bot and API.

Values are persisted as plain strings (guarded by CHECK constraints) so adding a status
never needs a Postgres ``ALTER TYPE`` migration.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    SELLER = "seller"
    SCANNER = "scanner"


class UserStatus(StrEnum):
    ONBOARDING = "onboarding"  # picked a role, has not finished timezone/country yet
    PENDING = "pending"  # waiting for admin approval
    APPROVED = "approved"
    REJECTED = "rejected"
    SUSPENDED = "suspended"


class SessionStatus(StrEnum):
    AWAITING_SCANNER = "awaiting_scanner"  # URL sent, scanner has <= 2 min to accept / skip
    ACCEPTED = "accepted"  # scanner works on it
    DONE = "done"  # scanner tapped Done, waiting for the seller
    CONFIRMED = "confirmed"  # seller (or auto) confirmed -> paid
    DISPUTED = "disputed"  # seller rejected -> dispute open, funds frozen
    REFUNDED = "refunded"  # dispute upheld -> seller refunded
    EXPIRED = "expired"  # scanner did not respond in time
    SKIPPED = "skipped"  # scanner skipped
    TIMED_OUT = "timed_out"  # accepted but never marked Done
    CANCELLED = "cancelled"


# Sessions in these states hold the seller's funds and count against concurrency limits.
ACTIVE_SESSION_STATUSES = (
    SessionStatus.AWAITING_SCANNER,
    SessionStatus.ACCEPTED,
    SessionStatus.DONE,
    SessionStatus.DISPUTED,
)
# Sessions in these states keep a scanner busy (a dispute does not).
SCANNER_BUSY_STATUSES = (SessionStatus.AWAITING_SCANNER, SessionStatus.ACCEPTED)


class DisputeStatus(StrEnum):
    AWAITING_PROOF = "awaiting_proof"  # scanner must upload a screenshot
    PENDING_REVIEW = "pending_review"  # admin (or AI) has to decide
    RESOLVED = "resolved"  # dispute upheld  -> seller refunded
    REJECTED = "rejected"  # dispute rejected -> scanner paid


class DisputeResolution(StrEnum):
    REFUND_SELLER = "refund_seller"
    PAY_SCANNER = "pay_scanner"


class TxStatus(StrEnum):
    COMPLETED = "completed"
    DISPUTED = "disputed"
    REFUNDED = "refunded"


class DepositMethod(StrEnum):
    BEP20 = "bep20"
    BINANCE = "binance"


class DepositStatus(StrEnum):
    PENDING = "pending"  # Binance claim waiting for verification
    CREDITED = "credited"
    BELOW_MIN = "below_min"  # seen on-chain but under the minimum; admin decides
    REJECTED = "rejected"


class WithdrawalStatus(StrEnum):
    PENDING = "pending"  # requested, funds locked, waiting for admin
    APPROVED = "approved"  # admin approved, waiting for payout
    PROCESSING = "processing"  # on-chain transfer broadcast
    COMPLETED = "completed"
    REJECTED = "rejected"  # funds returned
    FAILED = "failed"  # payout failed / ambiguous; funds stay locked until admin decides


class LedgerType(StrEnum):
    DEPOSIT = "deposit"
    HOLD = "hold"  # seller balance -> pending (task in flight)
    RELEASE = "release"  # seller pending -> balance (task did not complete)
    PAYMENT = "payment"  # seller pending consumed (task paid)
    EARNING = "earning"  # scanner paid
    COMMISSION = "commission"  # platform (user_id NULL) paid
    WITHDRAWAL_LOCK = "withdrawal_lock"
    WITHDRAWAL_UNLOCK = "withdrawal_unlock"
    WITHDRAWAL_PAID = "withdrawal_paid"
    WITHDRAWAL_FEE = "withdrawal_fee"  # platform income
    ADJUSTMENT = "adjustment"  # manual admin correction


class OutboxStatus(StrEnum):
    QUEUED = "queued"
    SENT = "sent"
    FAILED = "failed"


class BroadcastAudience(StrEnum):
    ALL = "all"
    SELLERS = "sellers"
    SCANNERS = "scanners"


class SlotResponse(StrEnum):
    READY = "ready"
    BUSY = "busy"


class AiVerdict(StrEnum):
    COMPLETED = "completed"  # screenshot shows the ChatGPT task finished
    NOT_COMPLETED = "not_completed"  # ChatGPT UI visible, but shows failure / unfinished
    NOT_CHATGPT = "not_chatgpt"  # unrelated image
    UNCLEAR = "unclear"


def values(enum_cls: type[StrEnum]) -> list[str]:
    return [e.value for e in enum_cls]


def sql_in(column: str, enum_cls: type[StrEnum]) -> str:
    """``role IN ('seller','scanner')`` for CHECK constraints."""
    return f"{column} IN ({', '.join(repr(v) for v in values(enum_cls))})"

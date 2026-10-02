"""Admin-editable business settings, stored in the ``settings`` table.

Every key has a typed definition (default, bounds, label) which drives both validation and the
Settings page of the admin panel. Unknown / invalid stored values silently fall back to defaults so
a bad row can never take the bot down.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import money
from app.models import Setting


class SettingsError(ValueError):
    pass


@dataclass(frozen=True)
class SettingDef:
    key: str
    type: str  # decimal | int | bool | str | list
    default: Any
    label: str
    help: str = ""
    group: str = "General"
    min: float | None = None
    max: float | None = None
    choices: tuple[int, ...] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "type": self.type,
            "default": self.default,
            "label": self.label,
            "help": self.help,
            "group": self.group,
            "min": self.min,
            "max": self.max,
            "choices": list(self.choices) if self.choices else None,
        }


DEFAULT_URL_DOMAINS = ["chatgpt.com", "chat.openai.com", "pay.openai.com", "openai.com"]

SETTING_DEFS: list[SettingDef] = [
    # money ---------------------------------------------------------------
    SettingDef("task_amount", "decimal", "0.5", "Scanner payout per confirmed task (USDT)",
               "Paid to the scanner when the seller confirms.", "Money", 0.01, 1000),
    SettingDef("commission_percent", "decimal", "0.1", "Commission (%)",
               "Fixed platform commission, charged to the seller on top of the task amount.", "Money", 0, 50),
    SettingDef("min_deposit", "decimal", "1", "Minimum deposit (USDT)",
               "On-chain deposits below this are held for admin review.", "Money", 0, 100000),
    SettingDef("min_withdrawal", "decimal", "1", "Minimum withdrawal (USDT)", "", "Money", 0.01, 100000),
    SettingDef("withdrawal_fee", "decimal", "0", "Withdrawal fee (USDT)",
               "Deducted from the payout and kept by the platform.", "Money", 0, 1000),
    # timeouts ------------------------------------------------------------
    SettingDef("url_input_timeout_seconds", "int", 120, "Seller URL input window (seconds)",
               "How long the seller has to paste the URL after /send.", "Timeouts", 30, 900),
    SettingDef("url_response_timeout_seconds", "int", 120, "Scanner response window (seconds)",
               "Accept / Skip deadline once the URL is delivered to the scanner.", "Timeouts", 30, 900),
    SettingDef("seller_confirm_delay_seconds", "int", 120, "Delay before asking the seller to confirm (seconds)",
               "Time between the scanner tapping Done and the seller's Confirm / Reject prompt.", "Timeouts", 0, 3600),
    SettingDef("seller_confirm_timeout_minutes", "int", 60, "Seller confirmation timeout (minutes)",
               "If the seller never answers, the task is auto-confirmed (scanner paid). 0 = never auto-confirm.",
               "Timeouts", 0, 10080),
    SettingDef("task_timeout_minutes", "int", 30, "Scanner task timeout (minutes)",
               "Accepted but not marked Done within this time -> task cancelled and seller refunded.", "Timeouts", 2, 1440),
    SettingDef("dispute_proof_timeout_minutes", "int", 30, "Dispute proof window (minutes)",
               "How long the scanner has to upload a screenshot after the seller rejects.", "Timeouts", 2, 1440),
    SettingDef("pre_slot_notice_minutes", "int", 10, "Pre-slot notification (minutes)",
               "Scanners are reminded this long before a slot starts.", "Timeouts", 1, 120),
    # slots ---------------------------------------------------------------
    SettingDef("slot_duration_hours", "int", 2, "Slot duration (hours)",
               "Length of the slot buttons shown to scanners (must divide 24).", "Slots", None, None,
               choices=(1, 2, 3, 4, 6, 8, 12)),
    SettingDef("max_active_sessions_per_scanner", "int", 1, "Concurrent tasks per scanner", "", "Slots", 1, 20),
    SettingDef("max_active_sessions_per_seller", "int", 1, "Concurrent tasks per seller", "", "Slots", 1, 20),
    # urls ----------------------------------------------------------------
    SettingDef("allowed_url_domains", "list", DEFAULT_URL_DOMAINS, "Allowed URL domains",
               "Sellers may only submit https URLs on these domains (subdomains included).", "Validation"),
    # payments ------------------------------------------------------------
    SettingDef("binance_pay_id", "str", "", "Binance Pay ID (shown to depositors)",
               "Sellers who choose 'Binance' send USDT to this Binance Pay ID / UID.", "Payments"),
    SettingDef("support_contact", "str", "", "Support contact",
               "Shown in rejection messages, e.g. @your_username.", "Payments"),
    # reputation / AI -----------------------------------------------------
    SettingDef("auto_reputation", "bool", True, "Automatic reputation changes",
               "+1 per confirmed task, -1 per ignored URL, -2 per timed-out task, -5 per lost dispute.", "Reputation & AI"),
    SettingDef("ai_auto_resolve", "bool", True, "AI auto-resolve disputes",
               "When the AI is confident the screenshot shows a completed ChatGPT task, pay the scanner automatically.",
               "Reputation & AI"),
    SettingDef("ai_auto_refund", "bool", False, "AI auto-refund disputes",
               "When the AI is confident the screenshot is NOT valid proof, refund the seller automatically.",
               "Reputation & AI"),
    SettingDef("ai_confidence_threshold", "decimal", "0.85", "AI confidence threshold",
               "Minimum model confidence (0.5-1.0) for an automatic decision.", "Reputation & AI", 0.5, 1.0),
]

DEFS_BY_KEY: dict[str, SettingDef] = {d.key: d for d in SETTING_DEFS}


# ── parsing / validation ────────────────────────────────────────────────────


def _parse(defn: SettingDef, raw: Any) -> Any:
    """Coerce + validate ``raw`` for ``defn``; raises SettingsError."""
    try:
        if defn.type == "decimal":
            value = money.to_decimal(raw)
            _check_bounds(defn, float(value))
            return value
        if defn.type == "int":
            if isinstance(raw, bool):
                raise ValueError("expected a whole number")
            value = int(Decimal(str(raw)))
            if str(raw).strip() not in (str(value), f"{value}.0"):
                raise ValueError("expected a whole number")
            if defn.choices and value not in defn.choices:
                raise ValueError(f"must be one of {list(defn.choices)}")
            _check_bounds(defn, value)
            return value
        if defn.type == "bool":
            if isinstance(raw, bool):
                return raw
            if str(raw).lower() in ("true", "1", "yes", "on"):
                return True
            if str(raw).lower() in ("false", "0", "no", "off"):
                return False
            raise ValueError("expected true/false")
        if defn.type == "str":
            value = "" if raw is None else str(raw).strip()
            if len(value) > 200:
                raise ValueError("too long")
            return value
        if defn.type == "list":
            items = raw.replace("\n", ",").split(",") if isinstance(raw, str) else list(raw)
            cleaned = []
            for item in items:
                item = str(item).strip().lower()
                if item and item not in cleaned:
                    if not all(c.isalnum() or c in ".-" for c in item) or "." not in item:
                        raise ValueError(f"'{item}' is not a valid domain name")
                    cleaned.append(item)
            if not cleaned:
                raise ValueError("at least one domain is required")
            return cleaned
    except (ValueError, ArithmeticError) as exc:
        raise SettingsError(f"{defn.label}: {exc}") from exc
    raise SettingsError(f"{defn.key}: unsupported type {defn.type}")  # pragma: no cover


def _check_bounds(defn: SettingDef, value: float) -> None:
    if defn.min is not None and value < defn.min:
        raise ValueError(f"must be at least {defn.min:g}")
    if defn.max is not None and value > defn.max:
        raise ValueError(f"must be at most {defn.max:g}")


def _to_json(value: Any) -> Any:
    return str(value) if isinstance(value, Decimal) else value


# ── runtime view ────────────────────────────────────────────────────────────


class RuntimeSettings:
    """Typed, read-only snapshot of all settings (stored values merged over defaults)."""

    def __init__(self, values: dict[str, Any]):
        self._values = values

    def __getattr__(self, name: str) -> Any:
        try:
            return self.__dict__["_values"][name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def as_json(self) -> dict[str, Any]:
        return {k: _to_json(v) for k, v in self._values.items()}

    # derived money helpers ---------------------------------------------------
    @property
    def commission_per_task(self) -> Decimal:
        return money.percent_of(self.task_amount, self.commission_percent)

    @property
    def seller_cost(self) -> Decimal:
        """What a seller pays per task: scanner payout + commission."""
        return self.task_amount + self.commission_per_task


def defaults() -> dict[str, Any]:
    return {d.key: _parse(d, d.default) for d in SETTING_DEFS}


async def load(db: AsyncSession) -> RuntimeSettings:
    values = defaults()
    rows = (await db.execute(select(Setting))).scalars().all()
    for row in rows:
        defn = DEFS_BY_KEY.get(row.key)
        if defn is None:
            continue
        try:
            values[row.key] = _parse(defn, row.value)
        except SettingsError:
            continue  # keep the default
    return RuntimeSettings(values)


async def save(db: AsyncSession, updates: dict[str, Any], updated_by: str | None = None) -> RuntimeSettings:
    """Validate and persist ``updates`` atomically. Raises SettingsError listing every problem."""
    errors: dict[str, str] = {}
    parsed: dict[str, Any] = {}
    for key, raw in updates.items():
        defn = DEFS_BY_KEY.get(key)
        if defn is None:
            errors[key] = "unknown setting"
            continue
        try:
            parsed[key] = _parse(defn, raw)
        except SettingsError as exc:
            errors[key] = str(exc)
    if errors:
        raise SettingsError("; ".join(f"{k}: {v}" for k, v in errors.items()))
    for key, value in parsed.items():
        stmt = pg_insert(Setting).values(key=key, value=_to_json(value), updated_by=updated_by)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Setting.key], set_={"value": stmt.excluded.value, "updated_by": updated_by}
        )
        await db.execute(stmt)
    await db.flush()
    return await load(db)


def describe() -> list[dict[str, Any]]:
    return [d.public() for d in SETTING_DEFS]

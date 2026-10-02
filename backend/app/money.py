"""Decimal helpers. All money is ``Decimal`` with 8 decimal places (``NUMERIC(20, 8)``)."""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation

PLACES = 8
QUANT = Decimal(1).scaleb(-PLACES)  # 0.00000001
ZERO = Decimal(0)


def to_decimal(value: object) -> Decimal:
    """Parse user / JSON input into a finite Decimal; raises ``ValueError`` otherwise."""
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, bool):
        raise ValueError("not a number")
    else:
        try:
            result = Decimal(str(value).strip().replace(",", "."))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("not a number") from exc
    if not result.is_finite():
        raise ValueError("not a finite number")
    return result


def q(value: Decimal) -> Decimal:
    """Quantize to the ledger precision (round half up)."""
    return value.quantize(QUANT, rounding=ROUND_HALF_UP)


def q_down(value: Decimal) -> Decimal:
    return value.quantize(QUANT, rounding=ROUND_DOWN)


def percent_of(amount: Decimal, percent: Decimal) -> Decimal:
    return q(amount * percent / Decimal(100))


def fmt_money(value: object, min_dp: int = 2) -> str:
    """``0.5005``, ``10.00``, ``1234.5`` -> human friendly, never scientific notation."""
    d = q(to_decimal(value))
    text = format(d, "f")
    if "." not in text:
        return text + "." + "0" * min_dp
    whole, frac = text.split(".")
    frac = frac.rstrip("0").ljust(min_dp, "0")
    return f"{whole}.{frac}"


def from_raw(raw: int, decimals: int) -> Decimal:
    """Token base units -> Decimal (exact)."""
    return Decimal(raw).scaleb(-decimals)


def to_raw(amount: Decimal, decimals: int) -> int:
    """Decimal -> token base units (must be exactly representable)."""
    raw = amount.scaleb(decimals)
    if raw != raw.to_integral_value():
        raise ValueError("amount has more precision than the token supports")
    return int(raw)

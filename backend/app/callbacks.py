"""Callback-data builders and regex patterns for inline buttons (Telegram limit: 64 bytes).

Format: ``<prefix>:<arg>[:<arg>...]``. Handlers *always* re-validate ownership and state on the
server; the data is never trusted.
"""

from __future__ import annotations

# onboarding
ROLE_PREFIX = "role"
TZ_OK = "tz:ok"
TZ_CHANGE = "tz:change"
COUNTRY_PREFIX = "ctry"
TZ_SELECT_PREFIX = "tzsel"

# admin approvals
ADMIN_APPROVE = "adm:ap"
ADMIN_REJECT = "adm:rj"

# scanner slots
SLOT_TOGGLE = "sl:t"
SLOT_DONE = "sl:done"
SLOT_EDIT = "sl:edit"
SLOT_ADD_MODE = "sl:addm"
SLOT_REMOVE_MODE = "sl:rmm"
READY = "rdy"
BUSY = "bsy"

# seller flow
SEND_YES = "send:yes"
SEND_NO = "send:no"
CONFIRM = "cfm"
REJECT = "rej"

# scanner task flow
ACCEPT = "acc"
SKIP = "skp"
DONE = "done"

# wallet
DEPOSIT_BEP20 = "dep:bep20"
DEPOSIT_BINANCE = "dep:binance"
DEPOSIT_CLAIM = "dep:claim"
WITHDRAW_BEP20 = "wd:bep20"
WITHDRAW_BINANCE = "wd:binance"
WITHDRAW_YES = "wd:yes"
WITHDRAW_NO = "wd:no"
ADDRESS_BEP20 = "addr:bep20"
ADDRESS_BINANCE = "addr:binance"


def role(role_value: str) -> str:
    return f"{ROLE_PREFIX}:{role_value}"


def country(code: str) -> str:
    return f"{COUNTRY_PREFIX}:{code}"


def tz_select(index: int) -> str:
    return f"{TZ_SELECT_PREFIX}:{index}"


def admin_approve(user_id: int) -> str:
    return f"{ADMIN_APPROVE}:{user_id}"


def admin_reject(user_id: int) -> str:
    return f"{ADMIN_REJECT}:{user_id}"


def slot_toggle(start: int, end: int) -> str:
    return f"{SLOT_TOGGLE}:{start}:{end}"


def ready(notification_id: int) -> str:
    return f"{READY}:{notification_id}"


def busy(notification_id: int) -> str:
    return f"{BUSY}:{notification_id}"


def accept(session_id: int) -> str:
    return f"{ACCEPT}:{session_id}"


def skip(session_id: int) -> str:
    return f"{SKIP}:{session_id}"


def done(session_id: int) -> str:
    return f"{DONE}:{session_id}"


def confirm(session_id: int) -> str:
    return f"{CONFIRM}:{session_id}"


def reject(session_id: int) -> str:
    return f"{REJECT}:{session_id}"

"""Inline keyboards as plain JSON-able dicts (no Telegram library needed).

The same structure is stored in the outbox and converted to ``InlineKeyboardMarkup`` by the bot.
A button is ``(label, callback_data)`` or ``(label, {"url": "https://..."})``.
"""

from __future__ import annotations

from typing import Any

Button = tuple[str, Any]
Rows = list[list[Button]]


def inline(rows: Rows | None) -> dict[str, Any] | None:
    if not rows:
        return None
    keyboard: list[list[dict[str, str]]] = []
    for row in rows:
        out_row = []
        for label, action in row:
            if isinstance(action, dict):
                out_row.append({"text": label, "url": action["url"]})
            else:
                out_row.append({"text": label, "callback_data": str(action)})
        keyboard.append(out_row)
    return {"inline_keyboard": keyboard}


def chunk(buttons: list[Button], per_row: int) -> Rows:
    return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]

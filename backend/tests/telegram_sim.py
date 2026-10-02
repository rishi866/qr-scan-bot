"""A fake Telegram for end-to-end bot tests.

The *real* ``Application`` and handlers run against an in-memory "Bot API": every ``sendMessage``,
``editMessageText`` ... call lands in :class:`World`, and :class:`Actor` objects play users who type
messages and press the inline buttons that the bot actually produced.
"""

from __future__ import annotations

import itertools
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from telegram import Update
from telegram.request import BaseRequest

from app.bot import delivery
from app.bot.main import build_application

BOT_ID = 4242
TOKEN = "123456:TEST-TOKEN"


# ── HTML <-> plain text + entities (Telegram delivers messages to clients as text + entities) ──


class _Html(HTMLParser):
    TAGS = {"b": "bold", "strong": "bold", "i": "italic", "em": "italic", "code": "code", "pre": "pre", "a": "text_link"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = ""
        self.entities: list[dict[str, Any]] = []
        self._open: list[tuple[str, int, dict]] = []

    @staticmethod
    def _u16(s: str) -> int:
        return len(s.encode("utf-16-le")) // 2

    def handle_starttag(self, tag, attrs):
        if tag in self.TAGS:
            self._open.append((tag, self._u16(self.text), dict(attrs)))

    def handle_endtag(self, tag):
        for i in range(len(self._open) - 1, -1, -1):
            if self._open[i][0] == tag:
                _, start, attrs = self._open.pop(i)
                entity = {"type": self.TAGS[tag], "offset": start, "length": self._u16(self.text) - start}
                if tag == "a":
                    entity["url"] = attrs.get("href", "")
                if entity["length"]:
                    self.entities.append(entity)
                return

    def handle_data(self, data):
        self.text += data


def html_to_text(html: str) -> tuple[str, list[dict[str, Any]]]:
    parser = _Html()
    parser.feed(html)
    parser.close()
    return parser.text, parser.entities


# ── the fake Bot API ────────────────────────────────────────────────────────


@dataclass
class Msg:
    id: int
    chat_id: int
    html: str
    markup: dict[str, Any] | None = None
    photo: bool = False
    reply_keyboard: dict[str, Any] | None = None
    edits: int = 0
    link_preview_disabled: bool | None = None

    @property
    def text(self) -> str:
        return html_to_text(self.html)[0]

    def buttons(self) -> list[dict[str, Any]]:
        if not self.markup:
            return []
        return [b for row in self.markup.get("inline_keyboard", []) for b in row]

    def button_labels(self) -> list[str]:
        return [b["text"] for b in self.buttons()]

    def keyboard_buttons(self) -> list[dict[str, Any]]:
        if not self.reply_keyboard:
            return []
        return [b for row in self.reply_keyboard.get("keyboard", []) for b in row]


@dataclass
class World:
    messages: dict[int, list[Msg]] = field(default_factory=lambda: defaultdict(list))
    answers: list[dict[str, Any]] = field(default_factory=list)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    files: dict[str, bytes] = field(default_factory=dict)
    _ids: Any = field(default_factory=lambda: itertools.count(1))

    def find(self, chat_id: int, message_id: int) -> Msg | None:
        return next((m for m in self.messages[chat_id] if m.id == message_id), None)

    def message_dict(self, m: Msg) -> dict[str, Any]:
        text, entities = html_to_text(m.html)
        out: dict[str, Any] = {
            "message_id": m.id,
            "date": int(time.time()),
            "chat": {"id": m.chat_id, "type": "private", "first_name": "x"},
            "from": {"id": BOT_ID, "is_bot": True, "first_name": "TestBot", "username": "qr_test_bot"},
            "text": text,
        }
        if entities:
            out["entities"] = entities
        if m.markup:
            out["reply_markup"] = m.markup
        return out

    async def handle(self, url: str, method: str, request_data) -> tuple[int, bytes]:
        if method == "GET":  # file download: https://api.telegram.org/file/bot<token>/<file_path>
            file_id = url.rsplit("/", 1)[-1].removesuffix(".jpg").removesuffix(".png")
            return 200, self.files.get(file_id, b"")
        api = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data is not None else {}
        self.calls.append((api, params))
        result = getattr(self, f"_m_{api}", self._m_default)(params)
        return 200, json.dumps({"ok": True, "result": result}).encode()

    # -- API methods ---------------------------------------------------------
    def _m_default(self, params):
        return True

    def _m_getMe(self, params):
        return {"id": BOT_ID, "is_bot": True, "first_name": "TestBot", "username": "qr_test_bot", "can_join_groups": True,
                "can_read_all_group_messages": False, "supports_inline_queries": False}

    def _m_sendMessage(self, params):
        reply_markup = params.get("reply_markup")
        is_inline = isinstance(reply_markup, dict) and "inline_keyboard" in reply_markup
        preview = params.get("link_preview_options")
        msg = Msg(
            id=next(self._ids),
            chat_id=int(params["chat_id"]),
            html=params["text"] if params.get("parse_mode") == "HTML" else _escape(params["text"]),
            markup=reply_markup if is_inline else None,
            reply_keyboard=reply_markup if isinstance(reply_markup, dict) and "keyboard" in reply_markup else None,
            link_preview_disabled=bool(preview.get("is_disabled")) if isinstance(preview, dict) else None,
        )
        self.messages[msg.chat_id].append(msg)
        return self.message_dict(msg)

    def _m_sendPhoto(self, params):
        msg = Msg(id=next(self._ids), chat_id=int(params["chat_id"]), html=params.get("caption", ""), markup=params.get("reply_markup"), photo=True)
        self.messages[msg.chat_id].append(msg)
        return self.message_dict(msg)

    def _m_editMessageText(self, params):
        msg = self.find(int(params["chat_id"]), int(params["message_id"]))
        assert msg is not None, "edit of an unknown message"
        msg.html = params["text"] if params.get("parse_mode") == "HTML" else _escape(params["text"])
        msg.markup = params.get("reply_markup")
        msg.edits += 1
        return self.message_dict(msg)

    def _m_answerCallbackQuery(self, params):
        self.answers.append(params)
        return True

    def _m_getFile(self, params):
        file_id = params["file_id"]
        return {"file_id": file_id, "file_unique_id": f"u{file_id}", "file_size": len(self.files.get(file_id, b"")), "file_path": f"files/{file_id}.jpg"}


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class FakeTelegramRequest(BaseRequest):
    def __init__(self, world: World):
        self.world = world

    @property
    def read_timeout(self) -> float | None:
        return 5.0

    async def initialize(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def do_request(self, url, method, request_data=None, read_timeout=None, write_timeout=None, connect_timeout=None, pool_timeout=None):
        return await self.world.handle(url, method, request_data)


# ── simulation driver ───────────────────────────────────────────────────────


class Sim:
    def __init__(self):
        self.world = World()
        self.app = build_application(TOKEN, with_worker=False, request=FakeTelegramRequest(self.world))
        self._update_ids = itertools.count(1)
        self._query_ids = itertools.count(1)
        self.actors: dict[int, Actor] = {}

    async def __aenter__(self) -> Sim:
        await self.app.initialize()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.app.shutdown()

    def actor(self, tg_id: int, first_name: str, last_name: str | None = None, **kw) -> Actor:
        actor = Actor(self, tg_id, first_name, last_name, **kw)
        self.actors[tg_id] = actor
        return actor

    async def feed(self, payload: dict[str, Any]) -> None:
        update = Update.de_json({"update_id": next(self._update_ids), **payload}, self.app.bot)
        assert update is not None
        await self.app.process_update(update)

    async def deliver(self) -> int:
        """Pump the transactional outbox into the fake Telegram (the worker does this every second)."""
        total = 0
        while True:
            sent = await delivery.deliver_due(self.app.bot, limit=100)
            total += sent
            if sent == 0:
                return total


class Actor:
    def __init__(self, sim: Sim, tg_id: int, first_name: str, last_name: str | None = None, username: str | None = None, language_code: str = "en"):
        self.sim = sim
        self.id = tg_id
        self.first_name = first_name
        self.last_name = last_name
        self.username = username
        self.language_code = language_code

    # -- views ---------------------------------------------------------------
    @property
    def inbox(self) -> list[Msg]:
        return self.sim.world.messages[self.id]

    def mark(self) -> int:
        return len(self.inbox)

    def _snapshot(self) -> dict[int, int]:
        return {m.id: m.edits for m in self.inbox}

    def _changed(self, snapshot: dict[int, int]) -> list[Msg]:
        """Messages that are new or were edited since ``snapshot`` (what the user sees change on screen)."""
        return [m for m in self.inbox if m.id not in snapshot or m.edits > snapshot[m.id]]

    def texts(self, since: int = 0) -> list[str]:
        return [m.text for m in self.inbox[since:]]

    def last(self) -> Msg:
        assert self.inbox, f"{self.first_name} has received nothing"
        return self.inbox[-1]

    def all_text(self) -> str:
        return "\n".join(m.text + " " + " ".join(m.button_labels()) for m in self.inbox)

    # -- actions -------------------------------------------------------------
    def _from(self) -> dict[str, Any]:
        user: dict[str, Any] = {"id": self.id, "is_bot": False, "first_name": self.first_name, "language_code": self.language_code}
        if self.last_name:
            user["last_name"] = self.last_name
        if self.username:
            user["username"] = self.username
        return user

    def _message(self, **extra) -> dict[str, Any]:
        return {
            "message_id": next(self.sim.world._ids),
            "date": int(time.time()),
            "chat": {"id": self.id, "type": "private", "first_name": self.first_name},
            "from": self._from(),
            **extra,
        }

    async def say(self, text: str) -> list[Msg]:
        extra: dict[str, Any] = {"text": text}
        if text.startswith("/"):
            command = text.split()[0]
            extra["entities"] = [{"type": "bot_command", "offset": 0, "length": len(command)}]
        snap = self._snapshot()
        await self.sim.feed({"message": self._message(**extra)})
        await self.sim.deliver()
        return self._changed(snap)

    async def web_app(self, payload: dict[str, Any]) -> list[Msg]:
        snap = self._snapshot()
        await self.sim.feed({"message": self._message(web_app_data={"data": json.dumps(payload), "button_text": "Detect"})})
        await self.sim.deliver()
        return self._changed(snap)

    async def send_photo(self, data: bytes, *, as_document: bool = False, mime: str = "image/png") -> list[Msg]:
        file_id = f"file{next(self.sim.world._ids)}"
        self.sim.world.files[file_id] = data
        snap = self._snapshot()
        if as_document:
            doc = {"file_id": file_id, "file_unique_id": f"u{file_id}", "file_name": "shot.png", "mime_type": mime, "file_size": len(data)}
            await self.sim.feed({"message": self._message(document=doc)})
        else:
            photo = [{"file_id": file_id, "file_unique_id": f"u{file_id}", "width": 320, "height": 200, "file_size": len(data)}]
            await self.sim.feed({"message": self._message(photo=photo)})
        await self.sim.deliver()
        return self._changed(snap)

    async def press(self, label: str, *, message: Msg | None = None) -> list[Msg]:
        """Press the newest inline button whose label contains ``label`` (like tapping it in the chat)."""
        candidates = [message] if message else list(reversed(self.inbox))
        for msg in candidates:
            for button in msg.buttons():
                if label in button["text"] and "callback_data" in button:
                    return await self.press_data(button["callback_data"], msg)
        available = [m.button_labels() for m in self.inbox if m.buttons()]
        raise AssertionError(f"{self.first_name}: no button containing {label!r}. Buttons seen: {available}")

    async def press_data(self, data: str, msg: Msg) -> list[Msg]:
        snap = self._snapshot()
        message = self.sim.world.message_dict(msg)
        # a client can only press buttons in its *own* chat: replaying someone else's callback data
        # (a classic attack) therefore arrives as a callback coming from the presser's chat
        message["chat"] = {"id": self.id, "type": "private", "first_name": self.first_name}
        query = {
            "id": str(next(self.sim._query_ids)),
            "from": self._from(),
            "chat_instance": "ci",
            "message": message,
            "data": data,
        }
        await self.sim.feed({"callback_query": query})
        await self.sim.deliver()
        return self._changed(snap)

    def last_toast(self) -> dict[str, Any] | None:
        return self.sim.world.answers[-1] if self.sim.world.answers else None

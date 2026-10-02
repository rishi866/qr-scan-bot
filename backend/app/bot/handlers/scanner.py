"""Scanner side: slots (/myslots /addslot /removeslot), Ready/Busy, Accept/Skip/Done, dispute proof."""

from __future__ import annotations

import logging

from sqlalchemy import select
from telegram import Update
from telegram.ext import ContextTypes

from app import callbacks, states, texts, timeutil
from app.bot.context import Reply, ident_of, is_private_chat, send_replies
from app.bot.handlers.common import gate
from app.db import session_scope
from app.enums import Role, SlotResponse
from app.keyboards import chunk
from app.models import ScannerSlot, SlotNotification, User
from app.services import disputes, outbox, proofs, scheduler, sessions, settings_service, slots, users
from app.timeutil import utcnow

log = logging.getLogger(__name__)

MAX_PROOF_DOWNLOAD = proofs.MAX_BYTES


# ── slot editor ─────────────────────────────────────────────────────────────


def _grid(blocks: list[tuple[int, int]], selected: set[tuple[int, int]], mode: str):
    buttons = []
    for start, end in blocks:
        chosen = (start, end) in selected
        if mode == "p" and chosen:
            continue
        if mode == "m" and not chosen:
            continue
        label = f"{'✅' if chosen else '⬜'} {timeutil.slot_label(start, end)}"
        buttons.append((label, callbacks.slot_toggle(start, end, mode)))
    rows = chunk(buttons, 4 if len(blocks) > 12 else 3)
    rows.append([("✅ Done", callbacks.SLOT_DONE)])
    return rows


async def _editor(db, user: User, mode: str) -> Reply:
    cfg = await settings_service.load(db)
    current = await slots.list_slots(db, user.user_id)
    selected = {(s.slot_start, s.slot_end) for s in current}
    blocks = sorted(set(slots.blocks_for_duration(cfg.slot_duration_hours)) | selected)
    if mode == "p" and not (set(blocks) - selected):
        return Reply("You already have every slot selected. Use /removeslot to remove some.")
    if mode == "m" and not selected:
        return Reply("You have no slots yet. Use /addslot to add some.")
    return Reply(texts.slot_picker(user.timezone or "UTC", len(selected)), buttons=_grid(blocks, selected, mode))


async def _scanner_or_reply(db, update: Update, *, lock: bool = False) -> tuple[User | None, Reply | None]:
    ident = ident_of(update)
    user = await users.get_by_telegram_id(db, ident.id, lock=lock)
    problem = await gate(db, user, role=Role.SCANNER)
    return (None, problem) if problem else (user, None)


async def myslots_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    async with session_scope() as db:
        user, problem = await _scanner_or_reply(db, update)
        if problem or user is None:
            reply = problem
        else:
            current = await slots.list_slots(db, user.user_id)
            reply = Reply(
                texts.my_slots(slots.labels(current), user.timezone or "UTC", user.alias, user.reputation),
                buttons=[[("✏️ Edit slots", callbacks.SLOT_EDIT)]],
            )
    await send_replies(update, context, reply)


async def _editor_command(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str) -> None:
    if not is_private_chat(update):
        return
    async with session_scope() as db:
        user, problem = await _scanner_or_reply(db, update)
        reply = problem if (problem or user is None) else await _editor(db, user, mode)
    await send_replies(update, context, reply)


async def addslot_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _editor_command(update, context, "p")


async def removeslot_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _editor_command(update, context, "m")


async def on_slot_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with session_scope() as db:
        user, problem = await _scanner_or_reply(db, update)
        reply = problem if (problem or user is None) else await _editor(db, user, "a")
    await send_replies(update, context, reply)


async def on_slot_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assert query is not None and query.data is not None
    _, _, start_s, end_s, mode = query.data.split(":")
    async with session_scope() as db:
        user, problem = await _scanner_or_reply(db, update, lock=True)
        if problem or user is None:
            reply = problem
        else:
            try:
                await slots.toggle_slot(db, user, int(start_s), int(end_s))
                editor = await _editor(db, user, mode)
                if not editor.buttons:  # nothing left to show in add/remove mode
                    current = await slots.list_slots(db, user.user_id)
                    editor = Reply(texts.slots_saved(slots.labels(current), user.timezone or "UTC"))
                editor.edit = True
                reply = editor
            except slots.SlotError as exc:
                reply = Reply(toast=str(exc), alert=True)
    await send_replies(update, context, reply)


async def on_slot_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with session_scope() as db:
        user, problem = await _scanner_or_reply(db, update)
        if problem or user is None:
            reply = problem
        else:
            current = await slots.list_slots(db, user.user_id)
            reply = Reply(texts.slots_saved(slots.labels(current), user.timezone or "UTC"), edit=True)
    await send_replies(update, context, reply)


# ── Ready / Busy ────────────────────────────────────────────────────────────


async def on_ready_busy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assert query is not None and query.data is not None
    action, raw_id = query.data.split(":")
    answer = SlotResponse.READY if action == callbacks.READY else SlotResponse.BUSY
    async with session_scope() as db:
        user, problem = await _scanner_or_reply(db, update)
        if problem or user is None:
            reply = problem
        else:
            note = (
                await db.execute(
                    select(SlotNotification)
                    .where(SlotNotification.id == int(raw_id), SlotNotification.user_id == user.user_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            now = utcnow()
            if note is None or note.occurrence_end <= now:
                reply = Reply(texts.NOTIFICATION_STALE, edit=True, toast=texts.NOTIFICATION_STALE)
            elif note.response is not None:
                reply = Reply(toast=f"Already answered: {note.response}.")
            else:
                note.response = answer.value
                note.responded_at = now
                if answer == SlotResponse.BUSY:
                    slot = await db.get(ScannerSlot, note.slot_id)
                    label = timeutil.slot_label(slot.slot_start, slot.slot_end) if slot else "?"
                    tz = slot.timezone if slot else (user.timezone or "UTC")
                    await outbox.notify_admins(db, texts.admin_scanner_busy(user.alias, user.name, user.telegram_id, label, tz))
                reply = Reply(texts.READY_ACK if answer == SlotResponse.READY else texts.BUSY_ACK, edit=True)
    await send_replies(update, context, reply)


# ── tasks ───────────────────────────────────────────────────────────────────


async def on_task_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    assert query is not None and query.data is not None
    action, raw_id = query.data.split(":")
    session_id = int(raw_id)
    async with session_scope() as db:
        user, problem = await _scanner_or_reply(db, update)
        if problem or user is None:
            reply = problem
        else:
            try:
                if action == callbacks.ACCEPT:
                    s = await sessions.accept(db, session_id, user.user_id)
                    reply = Reply(texts.scanner_accepted(s.session_id, s.url), buttons=[[("✅ Done", callbacks.done(s.session_id))]], edit=True)
                elif action == callbacks.SKIP:
                    s = await sessions.skip(db, session_id, user.user_id)
                    reply = Reply(texts.scanner_skipped(s.session_id), edit=True)
                else:
                    s = await sessions.mark_done(db, session_id, user.user_id)
                    reply = Reply(texts.scanner_done_ack(s.session_id), edit=True)
            except sessions.InvalidState as exc:
                reply = Reply(f"ℹ️ {texts.e(exc)}", edit=True, toast=str(exc))
            except sessions.SessionError as exc:
                reply = Reply(toast=str(exc), alert=True)
    await send_replies(update, context, reply)


# ── dispute proof ───────────────────────────────────────────────────────────


async def on_proof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Photo / image document sent while the bot waits for a dispute screenshot."""
    message = update.effective_message
    if message is None or not is_private_chat(update):
        return
    ident = ident_of(update)
    async with session_scope() as db:
        user = await users.get_by_telegram_id(db, ident.id)
        waiting = (
            user is not None
            and user.role == Role.SCANNER.value
            and user.state == states.AWAITING_PROOF
            and not users.state_is_expired(user)
        )
        dispute_id = int((user.state_data or {}).get("dispute_id", 0)) if waiting and user else 0
    if not waiting or not dispute_id:
        await send_replies(update, context, Reply(texts.PROOF_NOT_EXPECTED))
        return

    if message.photo:
        file_ref = message.photo[-1]  # largest size
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        file_ref = message.document
    else:
        await send_replies(update, context, Reply(texts.PROOF_NOT_IMAGE))
        return
    if file_ref.file_size and file_ref.file_size > MAX_PROOF_DOWNLOAD:
        await send_replies(update, context, Reply("The image is too large (max 10 MB)."))
        return

    try:
        tg_file = await context.bot.get_file(file_ref.file_id)
        data = bytes(await tg_file.download_as_bytearray())
        relative = proofs.save_proof(data, dispute_id)
    except proofs.ProofError as exc:
        await send_replies(update, context, Reply(f"⚠️ {texts.e(exc)}"))
        return

    async with session_scope() as db:
        user = await users.get_by_telegram_id(db, ident.id)
        assert user is not None
        try:
            await disputes.submit_proof(db, dispute_id, user.user_id, proof_file=relative, telegram_file_id=file_ref.file_id)
            reply = None  # the confirmation is queued through the outbox by the service
        except disputes.DisputeError as exc:
            scheduler.proof_path(relative).unlink(missing_ok=True)  # do not keep an orphaned upload
            reply = Reply(f"ℹ️ {texts.e(exc)}")
    await send_replies(update, context, reply)

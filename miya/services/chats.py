"""Chat monitor registry (spec §6).

`chat_monitors` decides what the userbot is allowed to ingest. Defaults follow
the spec: private chats on, groups and channels off until the owner whitelists
them from `/chats`. A row is only ever created with those defaults — an
existing row's toggles are the owner's decision and are never overwritten by a
dialog re-sync.

Only `monitor_enabled` is decided here, because it depends on the dialog. The
fixed toggles (`vision_enabled`, `docs_enabled`) are deliberately *not* passed
on insert: their defaults live in the schema (`server_default`, owned by the
migrations — vision has been on since 0005, so receipts and payment screenshots
are read). Keeping a second copy of a default in this module is how vision stayed
off for every chat discovered after 0005: the migration flipped the column, and
the stale constant here overrode it on every insert.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.enums import ChatType
from miya.db.models import ChatMonitor

log = logging.getLogger(__name__)

TOGGLE_FIELDS = ("monitor_enabled", "vision_enabled", "docs_enabled")

# New groups, one tap (docs/owner-decisions.md): a group or channel that
# started switched off is offered to the owner in the bot, once. "Ha" turns
# it on and reads this many days back, so the conversation he just joined is
# not a blank page.
BACKFILL_DAYS = 7
# A backfill that keeps failing (the account lost access, Telegram flood
# limits) stops being retried after this many sweeps; the group stays on.
BACKFILL_MAX_ATTEMPTS = 3


@dataclass(slots=True)
class DialogInfo:
    """What the userbot knows about a dialog, independent of Telethon types."""

    tg_chat_id: int
    chat_type: ChatType
    title: str | None
    is_bot: bool = False


# Telegram's own service account: login codes and 2FA notifications arrive
# here. That text must never be stored or sent to an extraction API.
TELEGRAM_SERVICE_ID = 777000


def default_monitor_enabled(dialog: DialogInfo) -> bool:
    """Private chats are on by default (spec §6) — except bots and Telegram's
    service chat. A bot DM is not a business conversation (and MIYA's own
    assistant bot would be re-ingested and double-extracted); the service chat
    carries login codes. The owner can still enable a bot chat from /chats.
    """
    if dialog.tg_chat_id == TELEGRAM_SERVICE_ID or dialog.is_bot:
        return False
    return dialog.chat_type is ChatType.private


async def sync_dialogs(
    session: AsyncSession, dialogs: list[DialogInfo]
) -> tuple[int, int]:
    """Register new dialogs, refresh titles. Returns (created, renamed)."""
    if not dialogs:
        return 0, 0

    existing = {
        row.tg_chat_id: row
        for row in await session.scalars(
            sa.select(ChatMonitor).where(
                ChatMonitor.tg_chat_id.in_([d.tg_chat_id for d in dialogs])
            )
        )
    }

    created = renamed = 0
    for dialog in dialogs:
        monitor = existing.get(dialog.tg_chat_id)
        if monitor is None:
            session.add(
                ChatMonitor(
                    tg_chat_id=dialog.tg_chat_id,
                    chat_type=dialog.chat_type,
                    title=dialog.title,
                    monitor_enabled=default_monitor_enabled(dialog),
                    # vision/docs: schema defaults, see the module docstring.
                )
            )
            created += 1
            continue
        # Titles drift (groups get renamed); toggles are the owner's and stay.
        if dialog.title and monitor.title != dialog.title:
            monitor.title = dialog.title
            renamed += 1
        if monitor.chat_type != dialog.chat_type:
            monitor.chat_type = dialog.chat_type

    await session.flush()
    return created, renamed


async def get_monitor(session: AsyncSession, tg_chat_id: int) -> ChatMonitor | None:
    return await session.scalar(
        sa.select(ChatMonitor).where(ChatMonitor.tg_chat_id == tg_chat_id)
    )


async def ensure_monitor(session: AsyncSession, dialog: DialogInfo) -> ChatMonitor:
    """Fetch the monitor row for a chat, creating it with spec defaults.

    Two messages from a chat MIYA has never seen can be handled concurrently,
    and `tg_chat_id` is unique — so a plain check-then-insert loses the race
    with an IntegrityError that would drop a message. An upsert that does
    nothing on conflict, followed by a read, is safe from either side.
    """
    monitor = await get_monitor(session, dialog.tg_chat_id)
    if monitor is not None:
        return monitor

    await session.execute(
        insert(ChatMonitor)
        .values(
            tg_chat_id=dialog.tg_chat_id,
            chat_type=dialog.chat_type,
            title=dialog.title,
            monitor_enabled=default_monitor_enabled(dialog),
            # vision/docs: schema defaults, see the module docstring.
        )
        .on_conflict_do_nothing(index_elements=[ChatMonitor.tg_chat_id])
    )
    await session.flush()
    monitor = await get_monitor(session, dialog.tg_chat_id)
    if monitor is None:  # pragma: no cover - the upsert just guaranteed a row
        raise RuntimeError(f"chat monitor for {dialog.tg_chat_id} vanished")
    return monitor


async def list_monitors(
    session: AsyncSession, *, offset: int = 0, limit: int = 8
) -> tuple[list[ChatMonitor], int]:
    """One page of chats for `/chats`, monitored first, plus the total count."""
    total = await session.scalar(sa.select(sa.func.count()).select_from(ChatMonitor))
    rows = list(
        await session.scalars(
            sa.select(ChatMonitor)
            .order_by(
                ChatMonitor.monitor_enabled.desc(),
                ChatMonitor.chat_type,
                ChatMonitor.title.nulls_last(),
                ChatMonitor.id,
            )
            .offset(offset)
            .limit(limit)
        )
    )
    return rows, total or 0


async def toggle(
    session: AsyncSession,
    monitor_id: int,
    field: str,
    *,
    now: datetime | None = None,
) -> ChatMonitor | None:
    """Flip one boolean on one chat. Unknown fields are refused, not guessed."""
    if field not in TOGGLE_FIELDS:
        raise ValueError(f"unknown toggle field: {field!r}")
    monitor = await session.get(ChatMonitor, monitor_id)
    if monitor is None:
        return None
    setattr(monitor, field, not getattr(monitor, field))
    if field == "monitor_enabled":
        # Switching a chat on or off from /chats is the owner's answer to
        # "o'qiymi?", whether or not the question ever went out: a group he
        # deliberately turned off must not be asked about afterwards.
        monitor.asked_at = monitor.asked_at or _now(now)
    await session.flush()
    log.info(
        "chat %s (%s): %s -> %s",
        monitor.tg_chat_id,
        monitor.title,
        field,
        getattr(monitor, field),
    )
    return monitor


# --- new groups, one tap -----------------------------------------------------
#
# Three processes, none of which can call the others, share this: the userbot
# discovers the group and creates its row switched off; the worker asks the
# owner over the assistant bot and stamps ``asked_at``; the bot records his
# answer; the userbot — the only process with a Telegram user session — reads
# the backfill it asked for. The database is the only thing they have in
# common, exactly as with oversized media (services/approvals.py).


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(settings.tz)


async def awaiting_join_question(
    session: AsyncSession, *, limit: int = 5
) -> list[ChatMonitor]:
    """Groups and channels switched off that the owner was never asked about.

    Oldest first, a few per sweep: the first dialog sync after deployment can
    register every group he is in, and those are asked a handful at a time
    rather than as one burst of fifty messages.
    """
    return list(
        await session.scalars(
            sa.select(ChatMonitor)
            .where(ChatMonitor.chat_type.in_([ChatType.group, ChatType.channel]))
            .where(ChatMonitor.monitor_enabled.is_(False))
            .where(ChatMonitor.asked_at.is_(None))
            .order_by(ChatMonitor.id)
            .limit(limit)
        )
    )


def mark_asked(monitor: ChatMonitor, *, now: datetime | None = None) -> None:
    """The question went out (or is about to): never ask this chat again."""
    monitor.asked_at = _now(now)


async def _answerable(session: AsyncSession, monitor_id: int) -> ChatMonitor | None:
    """The row a Ha / Yo'q may act on: a group or channel the owner was asked
    about. The payload names a monitor id and nothing else, so anything else
    it could name — a private chat, a bot DM, the Telegram service chat, a
    group whose question never went out — is a stale or forged tap and must
    change nothing: switched on and backfilled is not what the owner said."""
    monitor = await session.get(ChatMonitor, monitor_id)
    if monitor is None or monitor.asked_at is None:
        return None
    if monitor.chat_type not in (ChatType.group, ChatType.channel):
        return None
    return monitor


async def accept_join(
    session: AsyncSession, monitor_id: int, *, now: datetime | None = None
) -> ChatMonitor | None:
    """ "Ha": switch the chat on and queue a BACKFILL_DAYS backfill for the
    userbot. None when the row is gone (purged between question and tap) or
    was never asked about; the bot then says the question is gone."""
    monitor = await _answerable(session, monitor_id)
    if monitor is None:
        return None
    now = _now(now)
    monitor.monitor_enabled = True
    if monitor.backfill_done_at is None:
        monitor.backfill_requested_at = now
        monitor.backfill_attempts = 0
    await session.flush()
    log.info(
        "chat %s (%s) switched on by the owner; backfill queued",
        monitor.tg_chat_id,
        monitor.title,
    )
    return monitor


async def decline_join(
    session: AsyncSession, monitor_id: int, *, now: datetime | None = None
) -> ChatMonitor | None:
    """ "Yo'q": stays off, and stays asked. The owner can still use /chats.
    None for a row that is gone or was never asked about, as with "Ha"."""
    monitor = await _answerable(session, monitor_id)
    if monitor is None:
        return None
    log.info("chat %s (%s) left off by the owner", monitor.tg_chat_id, monitor.title)
    return monitor


async def pending_backfills(
    session: AsyncSession, *, limit: int = 5
) -> list[ChatMonitor]:
    """Chats the owner said yes to that the userbot has not read back yet.

    Only chats still switched on: a group the owner turned off again from
    /chats in the meantime must not have a week of its history pulled in.
    """
    return list(
        await session.scalars(
            sa.select(ChatMonitor)
            .where(ChatMonitor.backfill_requested_at.isnot(None))
            .where(ChatMonitor.backfill_done_at.is_(None))
            .where(ChatMonitor.backfill_attempts < BACKFILL_MAX_ATTEMPTS)
            .where(ChatMonitor.monitor_enabled.is_(True))
            .order_by(ChatMonitor.backfill_requested_at, ChatMonitor.id)
            .limit(limit)
        )
    )


def mark_backfilled(monitor: ChatMonitor, *, now: datetime | None = None) -> None:
    monitor.backfill_done_at = _now(now)


def mark_backfill_failed(monitor: ChatMonitor) -> None:
    """One more failed sweep; at BACKFILL_MAX_ATTEMPTS it is left alone."""
    monitor.backfill_attempts += 1
    if monitor.backfill_attempts >= BACKFILL_MAX_ATTEMPTS:
        log.error(
            "backfill of chat %s (%s) gave up after %d attempt(s)",
            monitor.tg_chat_id,
            monitor.title,
            monitor.backfill_attempts,
        )

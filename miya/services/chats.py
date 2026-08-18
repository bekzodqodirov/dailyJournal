"""Chat monitor registry (spec §6).

`chat_monitors` decides what the userbot is allowed to ingest. Defaults follow
the spec: private chats on, groups and channels off until the owner whitelists
them from `/chats`. A row is only ever created with those defaults — an
existing row's toggles are the owner's decision and are never overwritten by a
dialog re-sync.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from miya.db.enums import ChatType
from miya.db.models import ChatMonitor

log = logging.getLogger(__name__)

TOGGLE_FIELDS = ("monitor_enabled", "vision_enabled", "docs_enabled")


@dataclass(slots=True)
class DialogInfo:
    """What the userbot knows about a dialog, independent of Telethon types."""

    tg_chat_id: int
    chat_type: ChatType
    title: str | None


def default_monitor_enabled(chat_type: ChatType) -> bool:
    return chat_type is ChatType.private


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
                    monitor_enabled=default_monitor_enabled(dialog.chat_type),
                    vision_enabled=False,
                    docs_enabled=True,
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
    """Fetch the monitor row for a chat, creating it with spec defaults."""
    monitor = await get_monitor(session, dialog.tg_chat_id)
    if monitor is not None:
        return monitor
    monitor = ChatMonitor(
        tg_chat_id=dialog.tg_chat_id,
        chat_type=dialog.chat_type,
        title=dialog.title,
        monitor_enabled=default_monitor_enabled(dialog.chat_type),
        vision_enabled=False,
        docs_enabled=True,
    )
    session.add(monitor)
    await session.flush()
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
    session: AsyncSession, monitor_id: int, field: str
) -> ChatMonitor | None:
    """Flip one boolean on one chat. Unknown fields are refused, not guessed."""
    if field not in TOGGLE_FIELDS:
        raise ValueError(f"unknown toggle field: {field!r}")
    monitor = await session.get(ChatMonitor, monitor_id)
    if monitor is None:
        return None
    setattr(monitor, field, not getattr(monitor, field))
    await session.flush()
    log.info(
        "chat %s (%s): %s -> %s",
        monitor.tg_chat_id,
        monitor.title,
        field,
        getattr(monitor, field),
    )
    return monitor

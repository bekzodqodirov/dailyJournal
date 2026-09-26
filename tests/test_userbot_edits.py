"""WP-74: an edited message keeps its earlier words as revisions."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import sqlalchemy as sa

from miya.bot import replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import WindowStatus
from miya.db.session import session_scope
from miya.services import passages, recall
from miya.userbot import main as userbot
from tests.test_userbot_catchup import CHAT, _message, _monitor

TZ = settings.tz
AT = datetime.now(TZ).replace(microsecond=0) - timedelta(hours=2)


def _edited(mid: int, text: str, *, at=AT):
    message = _message(mid, at=at, text=text)
    message.edit_date = at + timedelta(minutes=5)
    return message


async def _row(session, mid: int) -> m.Interaction:
    session.expire_all()
    return await session.scalar(
        sa.select(m.Interaction).where(
            m.Interaction.tg_chat_id == CHAT,
            m.Interaction.meta["tg_message_id"].astext == str(mid),
        )
    )


async def _stored(text: str, mid: int = 7) -> None:
    await _monitor(last_seen=0)
    assert await userbot.ingest_message(None, _message(mid, at=AT, text=text))


async def test_edit_keeps_the_old_text_in_meta_and_updates_raw_text(session):
    await _stored("narxi 5 mln")

    assert await userbot.record_edit(None, _edited(7, "narxi 6 mln"))

    row = await _row(session, 7)
    assert row.raw_text == "narxi 6 mln"
    [edit] = row.meta["edits"]
    assert edit["old"] == "narxi 5 mln"
    source = await recall.source_of(session, f"m{row.id}")
    text = replies.manba(source)
    assert "avvalgi matn: «narxi 5 mln»" in text and "narxi 6 mln" in text


async def test_edit_reindexes_passages(session):
    await _stored("narxi 5 mln")
    await passages.index_pending(session)
    await session.commit()

    await userbot.record_edit(None, _edited(7, "narxi 6 mln konteyner"))
    row = await _row(session, 7)
    assert row.search_indexed_at is None
    await passages.index_pending(session)

    bodies = list(
        await session.scalars(
            sa.select(m.Passage.body).where(m.Passage.interaction_id == row.id)
        )
    )
    assert bodies == ["narxi 6 mln konteyner"]


async def test_edit_of_an_unknown_message_ingests_it(session):
    await _monitor(last_seen=0)

    assert await userbot.record_edit(None, _edited(9, "yangi xabar"))

    row = await _row(session, 9)
    assert row.raw_text == "yangi xabar" and "edits" not in (row.meta or {})


async def _window(status: WindowStatus) -> int:
    async with session_scope() as session:
        row = await session.scalar(
            sa.select(m.Interaction).where(m.Interaction.tg_chat_id == CHAT)
        )
        window = m.ConversationWindow(
            tg_chat_id=CHAT,
            started_at=AT,
            ended_at=AT,
            message_count=1,
            char_count=10,
            text="[THEM (Akmal)] narxi 5 mln",
            status=status,
            custom_id=f"w-{uuid.uuid4().hex}",
        )
        session.add(window)
        await session.flush()
        row.window_id = window.id
        return window.id


async def test_edit_of_a_pending_window_rerenders_it(session):
    await _stored("narxi 5 mln")
    window_id = await _window(WindowStatus.pending)

    await userbot.record_edit(None, _edited(7, "narxi 6 mln"))

    session.expire_all()
    window = await session.get(m.ConversationWindow, window_id)
    assert "6 mln" in window.text and "5 mln" not in window.text


async def test_edit_after_extraction_is_flagged_not_reextracted(session):
    await _stored("narxi 5 mln")
    window_id = await _window(WindowStatus.applied)

    await userbot.record_edit(None, _edited(7, "narxi 6 mln"))

    row = await _row(session, 7)
    assert row.meta["edited_after_extraction"] is True
    window = await session.get(m.ConversationWindow, window_id)
    assert window.status is WindowStatus.applied and "5 mln" in window.text


async def test_reaction_only_edit_changes_nothing(session):
    await _stored("narxi 5 mln")

    assert not await userbot.record_edit(None, _edited(7, "narxi 5 mln"))

    row = await _row(session, 7)
    assert "edits" not in (row.meta or {})

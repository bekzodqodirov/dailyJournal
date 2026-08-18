"""Conversation windowing (spec §7B): the three flush triggers and claiming."""

from __future__ import annotations

from datetime import datetime, timedelta

import sqlalchemy as sa

from miya.config import settings
from miya.db import models as m
from miya.db.enums import Direction, InteractionSource, WindowStatus
from miya.services import windows
from miya.services.people import resolve_person

TZ = settings.tz
CHAT = -100123456789


async def _message(
    session,
    text: str,
    *,
    when: datetime,
    out: bool = False,
    person: m.Person | None = None,
    chat: int = CHAT,
    media: dict | None = None,
) -> m.Interaction:
    interaction = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.out if out else Direction.in_,
        person_id=person.id if person else None,
        tg_chat_id=chat,
        occurred_at=when,
        raw_text=text or None,
        media=media,
    )
    session.add(interaction)
    await session.flush()
    return interaction


async def test_nothing_flushes_while_a_chat_is_still_active(session):
    now = datetime.now(TZ)
    await _message(session, "salom", when=now - timedelta(minutes=2))
    await _message(session, "yaxshimisiz", when=now - timedelta(minutes=1))

    assert await windows.flush_ready_windows(session, now=now) == []


async def test_thirty_minutes_of_silence_flushes_the_backlog(session):
    now = datetime.now(TZ)
    person = await resolve_person(session, "Akmal")
    await _message(session, "yuk ketdi", when=now - timedelta(minutes=45), person=person)
    await _message(
        session, "rahmat", when=now - timedelta(minutes=40), out=True, person=person
    )

    [window] = await windows.flush_ready_windows(session, now=now)

    assert window.message_count == 2
    assert window.status is WindowStatus.pending
    assert window.person_id == person.id
    assert "[THEM (Akmal)] yuk ketdi" in window.text
    assert "[ME] rahmat" in window.text


async def test_the_message_count_trigger_fires_before_the_chat_goes_quiet(session):
    now = datetime.now(TZ)
    for i in range(settings.window_max_messages + 3):
        await _message(session, f"xabar {i}", when=now - timedelta(seconds=60 - i))

    created = await windows.flush_ready_windows(session, now=now)

    assert len(created) == 1
    assert created[0].message_count == settings.window_max_messages
    # The extra three are still unclaimed, waiting for their own trigger.
    left = await session.scalar(
        sa.select(sa.func.count())
        .select_from(m.Interaction)
        .where(m.Interaction.window_id.is_(None))
    )
    assert left == 3


async def test_a_long_message_run_flushes_on_the_character_limit(session):
    now = datetime.now(TZ)
    body = "x" * 1500
    for i in range(4):  # 6,000 chars — over the 4,000 limit, under 25 messages
        await _message(session, body, when=now - timedelta(seconds=30 - i))

    [window] = await windows.flush_ready_windows(session, now=now)

    assert window.char_count >= settings.window_max_chars
    assert window.message_count == 3  # cut right after crossing the limit


async def test_a_large_backlog_becomes_several_windows_in_one_pass(session):
    now = datetime.now(TZ)
    for i in range(settings.window_max_messages * 2):
        await _message(session, f"xabar {i}", when=now - timedelta(minutes=90 - i))

    created = await windows.flush_ready_windows(session, now=now)

    assert len(created) == 2
    assert all(w.message_count == settings.window_max_messages for w in created)


async def test_each_chat_gets_its_own_window(session):
    now = datetime.now(TZ)
    await _message(session, "birinchi", when=now - timedelta(hours=1), chat=-1)
    await _message(session, "ikkinchi", when=now - timedelta(hours=1), chat=-2)

    created = await windows.flush_ready_windows(session, now=now)

    assert {w.tg_chat_id for w in created} == {-1, -2}


async def test_windowed_messages_are_claimed_and_never_windowed_twice(session):
    now = datetime.now(TZ)
    await _message(session, "eski xabar", when=now - timedelta(hours=2))

    [window] = await windows.flush_ready_windows(session, now=now)
    claimed = await session.scalar(
        sa.select(m.Interaction.window_id).where(m.Interaction.window_id.isnot(None))
    )
    assert claimed == window.id

    assert await windows.flush_ready_windows(session, now=now) == []


async def test_media_without_text_still_carries_context_into_the_window(session):
    now = datetime.now(TZ)
    await _message(
        session,
        "",
        when=now - timedelta(hours=1),
        media={"type": "voice", "processed": True},
    )
    await _message(
        session,
        "",
        when=now - timedelta(hours=1),
        media={"type": "document", "filename": "hisob.pdf"},
    )

    [window] = await windows.flush_ready_windows(session, now=now)

    assert "[ovozli xabar]" in window.text
    assert "[hujjat: hisob.pdf]" in window.text


async def test_a_transcript_is_what_lands_in_the_window(session):
    now = datetime.now(TZ)
    interaction = await _message(
        session, "", when=now - timedelta(hours=1), media={"type": "voice"}
    )
    interaction.transcript = "Akmalga 5 mln berdim"
    await session.flush()

    [window] = await windows.flush_ready_windows(session, now=now)

    assert "Akmalga 5 mln berdim" in window.text

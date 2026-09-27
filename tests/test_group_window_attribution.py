"""WP-47: a group window is nobody's; its facts never land on the first speaker."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

import sqlalchemy as sa

from miya.config import settings
from miya.db import models as m
from miya.db.enums import ChatType, Direction, InteractionSource
from miya.services import extraction as ex
from miya.services import windows
from miya.services.persistence import apply_extraction

TZ = settings.tz
GROUP, PRIVATE = -1001, 5001
MIGRATION = (
    Path(__file__).resolve().parents[1] / "alembic/versions/0018_group_windows_unowned.py"
)


async def _person(session, name: str, **fields) -> m.Person:
    person = m.Person(display_name=name, aliases=[], **fields)
    session.add(person)
    await session.flush()
    return person


async def _chat(session, chat: int, chat_type: ChatType) -> None:
    session.add(m.ChatMonitor(tg_chat_id=chat, chat_type=chat_type, title=str(chat)))
    await session.flush()


async def _said(session, chat: int, person: m.Person, text: str, minutes_ago: int):
    session.add(
        m.Interaction(
            source=InteractionSource.telegram_userbot,
            direction=Direction.in_,
            person_id=person.id,
            tg_chat_id=chat,
            occurred_at=datetime.now(TZ) - timedelta(minutes=minutes_ago),
            raw_text=text,
        )
    )
    await session.flush()


async def test_group_window_has_no_person(session):
    sardor = await _person(session, "Sardor")
    await _chat(session, GROUP, ChatType.group)
    await _said(session, GROUP, sardor, "salom", 45)
    [window] = await windows.flush_ready_windows(session, now=datetime.now(TZ))
    assert window.person_id is None


async def test_private_window_keeps_the_peer(session):
    akmal = await _person(session, "Akmal")
    await _chat(session, PRIVATE, ChatType.private)
    await _said(session, PRIVATE, akmal, "salom", 45)
    [window] = await windows.flush_ready_windows(session, now=datetime.now(TZ))
    assert window.person_id == akmal.id


async def _window_row(session, chat: int, person_id=None) -> m.Interaction:
    row = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.na,
        tg_chat_id=chat,
        person_id=person_id,
        occurred_at=datetime.now(TZ),
        raw_text="window",
        meta={"kind": "window"},
    )
    session.add(row)
    await session.flush()
    return row


async def test_group_facts_go_to_the_single_person_written_or_nobody(session):
    await _person(session, "Sardor")
    akmal = await _person(session, "Akmal")
    await _chat(session, GROUP, ChatType.group)
    row = await _window_row(session, GROUP)
    await apply_extraction(
        session,
        row,
        ex.ExtractionResult(
            debts=[
                ex.ExtractedDebt(
                    direction="they_owe_me",
                    person="Akmal",
                    amount=1_000_000,
                    reason="konteyner",
                    asserted_by="me",
                )
            ],
            facts=["Akmal bilan konteyner kelishildi"],
        ),
    )
    [fact] = list(await session.scalars(sa.select(m.Memory)))
    assert fact.person_id == akmal.id


def _statements() -> tuple[str, ...]:
    spec = importlib.util.spec_from_file_location("m0018", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.STATEMENTS


async def test_data_fix_nulls_group_window_attribution(session):
    stamp = datetime.now(TZ) - timedelta(days=1)
    sardor = await _person(session, "Sardor", profile_updated_at=stamp)
    akmal = await _person(session, "Akmal", profile_updated_at=stamp)
    dilnoza = await _person(session, "Dilnoza", profile_updated_at=stamp)
    await _chat(session, GROUP, ChatType.group)
    await _chat(session, PRIVATE, ChatType.private)
    group_row = await _window_row(session, GROUP, sardor.id)
    private_row = await _window_row(session, PRIVATE, dilnoza.id)

    def memory(row, person, tags):
        return m.Memory(
            content="x",
            person_id=person.id,
            source_interaction_id=row.id,
            occurred_at=stamp,
            tags=tags,
        )

    guessed = memory(group_row, sardor, [])
    named = memory(group_row, akmal, ["person"])
    private = memory(private_row, dilnoza, [])
    session.add_all([guessed, named, private])
    await session.flush()

    for statement in _statements():
        await session.execute(sa.text(statement))
    for row in (guessed, named, private, sardor, akmal, dilnoza, group_row, private_row):
        await session.refresh(row)

    assert guessed.person_id is None and sardor.profile_updated_at is None
    assert named.person_id == akmal.id and akmal.profile_updated_at is not None
    assert private.person_id == dilnoza.id and dilnoza.profile_updated_at is not None
    assert group_row.person_id is None and private_row.person_id == dilnoza.id

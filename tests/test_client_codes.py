"""WP-30: who holds which client code."""

from __future__ import annotations

from datetime import datetime

import pytest
import sqlalchemy as sa

from miya.config import settings
from miya.db import models as m
from miya.db.enums import InteractionSource
from miya.services import codes


async def _person(session, name: str) -> m.Person:
    person = m.Person(display_name=name, aliases=[])
    session.add(person)
    await session.flush()
    return person


async def _active(session, code: str) -> list[m.ClientCode]:
    return list(
        await session.scalars(
            sa.select(m.ClientCode).where(
                m.ClientCode.code == code, m.ClientCode.status == "active"
            )
        )
    )


async def test_attach_twice_is_idempotent_and_a_second_holder_is_refused(session):
    akmal = await _person(session, "Akmal")
    vali = await _person(session, "Vali")
    first = await codes.attach(session, akmal, "GS367", source="command", by="command")
    again = await codes.attach(session, akmal, "GS367", source="command", by="command")
    assert first.id == again.id
    with pytest.raises(codes.CodeTaken) as taken:
        await codes.attach(session, vali, "GS367", source="command", by="command")
    assert taken.value.holder.id == akmal.id
    assert await codes.codes_of(session, akmal.id) == ["GS367"]


async def test_move_leaves_one_active_row_with_history(session):
    akmal = await _person(session, "Akmal")
    vali = await _person(session, "Vali")
    await codes.attach(session, akmal, "GS367", source="command", by="command")
    await codes.move(session, "GS367", vali, by="command")
    [row] = await _active(session, "GS367")
    assert row.person_id == vali.id and row.history[-1]["field"] == "person"
    old = await session.scalar(
        sa.select(m.ClientCode).where(m.ClientCode.person_id == akmal.id)
    )
    assert old.status == "detached" and old.history
    assert (await codes.holder(session, "GS367")).id == vali.id


async def test_detach_then_attach(session):
    akmal = await _person(session, "Akmal")
    vali = await _person(session, "Vali")
    await codes.attach(session, akmal, "GS367", source="command", by="command")
    assert (await codes.detach(session, "GS367", by="command")).id == akmal.id
    await codes.attach(session, vali, "GS367", source="command", by="command")
    assert (await codes.holder(session, "GS367")).id == vali.id


async def test_suggestions_are_refused_where_they_would_mislead(session):
    akmal = await _person(session, "Akmal")
    vali = await _person(session, "Vali")
    row = await codes.suggest(session, akmal, "GS1", source="tg_name")
    await codes.reject_suggestion(session, row.id, by="button")
    assert await codes.suggest(session, akmal, "GS1", source="tg_name") is None
    await codes.attach(session, vali, "GS2", source="command", by="command")
    assert await codes.suggest(session, akmal, "GS2", source="tg_name") is None
    assert await codes.pending_suggestion_count(session) == 0


async def test_accept_suggestion_refuses_a_code_taken_meanwhile(session):
    akmal = await _person(session, "Akmal")
    vali = await _person(session, "Vali")
    row = await codes.suggest(session, akmal, "GS9", source="tg_name")
    assert [s.id for s in await codes.pending_suggestions(session)] == [row.id]
    await codes.attach(session, vali, "GS9", source="command", by="command")
    with pytest.raises(codes.CodeTaken):
        await codes.accept_suggestion(session, row.id, by="button")


async def test_deleting_a_person_or_an_interaction_takes_its_rows(session):
    akmal = await _person(session, "Akmal")
    await codes.attach(session, akmal, "GS367", source="command", by="command")
    row = m.Interaction(
        source=InteractionSource.assistant_bot,
        occurred_at=datetime.now(settings.tz),
        raw_text="GS5",
    )
    session.add(row)
    await session.flush()
    vali = await _person(session, "Vali")
    await codes.attach(
        session, vali, "GS5", source="extraction", by="extraction", interaction_id=row.id
    )
    session.add(
        m.CodeMention(
            interaction_id=row.id, kind="client", code="GS5", occurred_at=row.occurred_at
        )
    )
    await session.flush()
    await session.delete(akmal)
    await session.delete(row)
    await session.flush()
    assert await session.scalar(sa.select(sa.func.count(m.ClientCode.id))) == 0
    assert await session.scalar(sa.select(sa.func.count(m.CodeMention.id))) == 0

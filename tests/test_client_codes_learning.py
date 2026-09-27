"""WP-35: codes learned from what the owner wrote; the rest only suggested."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import sqlalchemy as sa
from telethon.tl.types import User

from miya.config import settings
from miya.db import models as m
from miya.db.enums import ChatType, Direction, InteractionSource
from miya.services import codes, phone_events
from miya.services import extraction as ex
from miya.services.persistence import apply_extraction
from miya.userbot import main as userbot


async def _person(session, name: str, code: str | None = None, **fields) -> m.Person:
    person = m.Person(display_name=name, aliases=[], **fields)
    session.add(person)
    await session.flush()
    if code:
        await codes.attach(session, person, code, source="command", by="command")
    return person


async def _rows(session, code: str) -> list[m.ClientCode]:
    return list(
        await session.scalars(sa.select(m.ClientCode).where(m.ClientCode.code == code))
    )


def _dm(entity) -> tuple[SimpleNamespace, m.ChatMonitor]:
    async def get_chat():
        return entity

    message = SimpleNamespace(get_chat=get_chat, out=False)
    return message, m.ChatMonitor(tg_chat_id=entity.id, chat_type=ChatType.private)


async def test_a_saved_contact_attaches_its_code(session):
    entity = User(id=9001, first_name="GS367 Akmal", contact=True)
    person = await userbot._counterparty(session, *_dm(entity))
    assert person.display_name == "Akmal"
    [row] = await _rows(session, "GS367")
    assert (row.person_id, row.status, row.source) == (person.id, "active", "contact")


async def test_a_strangers_profile_name_only_suggests(session):
    entity = User(id=9002, first_name="GS368 Vali", contact=False)
    person = await userbot._counterparty(session, *_dm(entity))
    assert person.display_name == "Vali"
    [row] = await _rows(session, "GS368")
    assert (row.person_id, row.status, row.source) == (person.id, "suggested", "tg_name")
    assert await codes.holder(session, "GS368") is None


async def test_a_code_added_to_a_contact_later_is_picked_up(session):
    known = await _person(session, "Akmal", telegram_id=9003)
    entity = User(id=9003, first_name="Akmal GS412", contact=True)
    person = await userbot._counterparty(session, *_dm(entity))
    assert person.id == known.id
    assert (await codes.holder(session, "GS412")).id == known.id


async def test_a_phone_book_name_attaches_its_code(session):
    event = {
        "call_log_id": 1,
        "started_at": datetime.now(settings.tz).isoformat(),
        "duration_seconds": 30,
        "type": "incoming",
        "number": "+998907777777",
        "contact_name": "GS367 Akmal",
        "sim_slot": 0,
    }
    await phone_events.ingest_call_events(session, "dev-1", [event])
    holder = await codes.holder(session, "GS367")
    assert holder is not None and holder.display_name == "Akmal"
    [row] = await _rows(session, "GS367")
    assert row.source == "contact"


async def _interaction(session, *, source=InteractionSource.assistant_bot, meta=None):
    interaction = m.Interaction(
        source=source,
        direction=Direction.in_,
        occurred_at=datetime.now(settings.tz),
        raw_text="GS367 — Akmal",
        meta=meta,
    )
    session.add(interaction)
    await session.flush()
    return interaction


def _linked() -> ex.ExtractionResult:
    return ex.ExtractionResult(
        people=[ex.ExtractedPerson(name="Akmal", client_code="gs 367")]
    )


async def test_the_owners_own_note_attaches_the_code(session):
    akmal = await _person(session, "Akmal")
    interaction = await _interaction(session)
    applied = await apply_extraction(session, interaction, _linked())
    assert (await codes.holder(session, "GS367")).id == akmal.id
    assert applied.codes_learned == [("GS367", "Akmal")]


async def test_a_window_only_suggests_the_code(session):
    akmal = await _person(session, "Akmal")
    interaction = await _interaction(
        session, source=InteractionSource.telegram_userbot, meta={"window": 1}
    )
    applied = await apply_extraction(session, interaction, _linked())
    assert await codes.holder(session, "GS367") is None
    [row] = await _rows(session, "GS367")
    assert (row.person_id, row.status) == (akmal.id, "suggested")
    assert applied.codes_learned == []


async def test_a_code_someone_else_holds_is_a_conflict(session):
    await _person(session, "Akmal")
    sardor = await _person(session, "Sardor", "GS367")
    interaction = await _interaction(session)
    applied = await apply_extraction(session, interaction, _linked())
    assert applied.identity_conflicts == [("GS367", "Sardor", "Akmal")]
    assert interaction.needs_review is True
    assert (await codes.holder(session, "GS367")).id == sardor.id

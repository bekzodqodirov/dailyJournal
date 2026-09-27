"""WP-30: who holds which client code."""

from __future__ import annotations

from datetime import datetime

import pytest
import sqlalchemy as sa

from miya.config import settings
from miya.db import models as m
from miya.db.enums import InteractionSource
from miya.services import codes


async def _person(session, name: str, **fields) -> m.Person:
    person = m.Person(display_name=name, aliases=[], **fields)
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


# --- WP-31: codes first everywhere a person is looked up ---------------------

from miya.services import extraction as ex  # noqa: E402
from miya.services import people, persistence, phone_events  # noqa: E402


async def _held(session, name: str, code: str, **fields) -> m.Person:
    person = m.Person(display_name=name, aliases=[], **fields)
    session.add(person)
    await session.flush()
    await codes.attach(session, person, code, source="command", by="command")
    return person


async def _bot_interaction(session, text: str = "x") -> m.Interaction:
    interaction = m.Interaction(
        source=InteractionSource.assistant_bot,
        direction="in",
        occurred_at=datetime.now(settings.tz),
        raw_text=text,
    )
    session.add(interaction)
    await session.flush()
    return interaction


def _debt(person: str) -> ex.ExtractedDebt:
    return ex.ExtractedDebt(
        direction="they_owe_me",
        person=person,
        amount=1_000_000,
        currency="UZS",
        reason="yuk",
        asserted_by="me",
    )


async def test_a_held_code_resolves_to_its_holder_and_an_unknown_one_is_refused(
    session,
):
    akmal = await _held(session, "Akmal", "GS367")
    assert (await people.resolve_person(session, "GS367")).id == akmal.id
    assert (await people.resolve_person(session, "gs 367")).id == akmal.id
    with pytest.raises(people.UnknownCode) as unknown:
        await people.resolve_person(session, "GS999", strict=True)
    assert unknown.value.code == "GS999"
    # Non-strict callers get None and nobody named after a code.
    assert await people.resolve_person(session, "GS999") is None
    names = list(await session.scalars(sa.select(m.Person.display_name)))
    assert names == ["Akmal"]


async def test_write_debt_to_an_unknown_code_writes_nothing_and_flags(session):
    interaction = await _bot_interaction(session)
    applied = await persistence.apply_extraction(
        session, interaction, ex.ExtractionResult(debts=[_debt("GS999")])
    )
    assert applied.debts == []
    assert applied.unknown_codes == ["GS999"]
    assert not applied.is_empty()
    assert interaction.needs_review is True
    assert interaction.meta["identity"] == {"unknown_code": "GS999"}
    assert list(await session.scalars(sa.select(m.Debt))) == []


async def test_a_namesake_with_another_code_is_a_new_person(session):
    akmal = await _held(session, "Akmal", "GS367")
    other = await people.resolve_person(session, "Akmal GS368")
    assert other is not None and other.id != akmal.id
    assert other.display_name == "Akmal"
    assert await codes.codes_of(session, other.id) == ["GS368"]


async def test_a_codeless_namesake_gets_the_code_suggested_or_attached(session):
    akmal = await _person(session, "Akmal")
    got = await people.resolve_person(session, "Akmal GS368", code_policy="suggest")
    assert got.id == akmal.id
    assert await codes.codes_of(session, akmal.id) == []
    assert await codes.pending_suggestion_count(session) == 1

    vali = await _person(session, "Valijon")
    got = await people.resolve_person(session, "Valijon GS369", code_policy="attach")
    assert got.id == vali.id
    assert await codes.codes_of(session, vali.id) == ["GS369"]


async def test_a_code_held_by_someone_else_than_the_named_person_conflicts(session):
    akmal = await _held(session, "Akmal", "GS367")
    await _person(session, "Sardor")
    with pytest.raises(people.IdentityConflict) as conflict:
        await people.resolve_person(session, "Sardor GS367", strict=True)
    assert conflict.value.holder.id == akmal.id and conflict.value.named == "Sardor"

    interaction = await _bot_interaction(session)
    applied = await persistence.apply_extraction(
        session, interaction, ex.ExtractionResult(debts=[_debt("Sardor GS367")])
    )
    assert applied.debts == []
    assert applied.identity_conflicts == [("GS367", "Akmal", "Sardor")]
    assert interaction.needs_review is True
    assert list(await session.scalars(sa.select(m.Debt))) == []


async def test_phone_first_picks_the_namesake_who_holds_the_number(session):
    await _person(session, "Akmal", phone="+998901111111")
    second = await _person(session, "Akmal", phone="+998901234567")
    got = await people.resolve_person(session, "Akmal", phone="+998 90 123 45 67")
    assert got.id == second.id


async def test_a_telegram_user_named_after_a_code_nobody_holds_is_nobody(session):
    got = await people.resolve_person(session, "GS367", telegram_id=555)
    assert got is None
    assert list(await session.scalars(sa.select(m.Person))) == []


async def test_a_telegram_user_whose_code_is_someone_elses_keeps_only_the_name(
    session,
):
    akmal = await _held(session, "Akmal", "GS367")
    got = await people.resolve_person(session, "Sardor GS367", telegram_id=556)
    assert got is not None and got.id != akmal.id
    assert got.display_name == "Sardor"
    assert await codes.codes_of(session, got.id) == []
    assert (await codes.holder(session, "GS367")).id == akmal.id


async def test_persist_people_with_an_unknown_code_writes_nothing(session):
    interaction = await _bot_interaction(session)
    result = ex.ExtractionResult(
        people=[ex.ExtractedPerson(name="GS999", context="mashinasi oq")]
    )
    applied = await persistence.apply_extraction(session, interaction, result)
    assert list(await session.scalars(sa.select(m.Memory))) == []
    assert applied.unknown_codes == []


async def test_a_call_from_a_contact_named_after_an_unknown_code_is_stored(session):
    event = {
        "call_log_id": 1,
        "started_at": datetime.now(settings.tz).isoformat(),
        "duration_seconds": 30,
        "type": "incoming",
        "number": "+998907777777",
        "contact_name": "GS999",
        "sim_slot": 0,
    }
    outcome = await phone_events.ingest_call_events(session, "dev-1", [event])
    assert outcome.rejected == []
    [row] = list(await session.scalars(sa.select(m.Interaction)))
    assert row.person_id is None


async def test_find_person_goes_by_code_first(session):
    akmal = await _held(session, "Akmal", "GS367")
    match = await people.find_person(session, "gs-367")
    assert match.person.id == akmal.id and match.exact and match.via_code == "GS367"
    missing = await people.find_person(session, "GS999")
    assert missing.person is None and missing.unknown_code == "GS999"

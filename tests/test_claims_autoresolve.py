"""WP-43: claims that answer themselves — repeats, and what the owner wrote."""

from __future__ import annotations

from datetime import datetime, timedelta

import sqlalchemy as sa

from miya.config import settings
from miya.db import models as m
from miya.db.enums import Direction, InteractionSource
from miya.services import claims
from miya.services import extraction as ex
from miya.services.persistence import apply_extraction

NOW = datetime.now(settings.tz).replace(microsecond=0)


async def _interaction(session, source=InteractionSource.telegram_userbot):
    interaction = m.Interaction(
        source=source,
        direction=Direction.in_,
        occurred_at=NOW,
        raw_text="x",
    )
    session.add(interaction)
    await session.flush()
    return interaction


async def _person(session, name="Akmal") -> m.Person:
    person = m.Person(display_name=name, aliases=[])
    session.add(person)
    await session.flush()
    return person


def _debt(amount=5_000_000, *, by="them", person="Akmal") -> ex.ExtractedDebt:
    return ex.ExtractedDebt(
        direction="i_owe_them",
        person=person,
        amount=amount,
        currency="UZS",
        reason="yuk",
        asserted_by=by,
    )


async def _claim(session, amount=5_000_000, *, minutes_ago=0, person="Akmal"):
    return await claims.create(
        session,
        await _interaction(session),
        claims.KIND_DEBT,
        _debt(amount, person=person),
        now=NOW - timedelta(minutes=minutes_ago),
    )


async def _debts(session) -> list[m.Debt]:
    return list(await session.scalars(sa.select(m.Debt)))


async def test_repeated_claim_is_linked_not_asked(session):
    first = await _claim(session, minutes_ago=10)
    second = await _claim(session)
    assert second.duplicate_of == first.id
    assert [c.id for c in await claims.pending(session)] == [first.id]
    assert await claims.pending_count(session) == 1
    assert await claims.duplicate_counts(session, [first.id]) == {first.id: 1}


async def test_ha_on_primary_writes_one_debt_and_closes_the_duplicate(session):
    await _person(session)
    first = await _claim(session, minutes_ago=10)
    second = await _claim(session)
    await claims.accept(session, first.id, by=claims.BY_BUTTON, now=NOW)
    [debt] = await _debts(session)
    assert (second.state, second.answered_by) == (claims.AUTO, claims.BY_AUTO_DUPLICATE)
    assert (second.result_kind, second.result_id) == ("debt", debt.id)


async def test_yoq_on_primary_declines_duplicates(session):
    first = await _claim(session, minutes_ago=10)
    second = await _claim(session)
    await claims.decline(session, first.id, by=claims.BY_BUTTON, now=NOW)
    assert second.state == claims.DECLINED
    assert second.answered_by == claims.BY_AUTO_DUPLICATE
    assert await _debts(session) == []


async def test_ha_on_a_duplicate_answers_the_primary(session):
    await _person(session)
    first = await _claim(session, minutes_ago=10)
    second = await _claim(session)
    await claims.accept(session, second.id, by=claims.BY_BUTTON, now=NOW)
    assert first.state == claims.ACCEPTED
    assert second.state == claims.AUTO
    assert len(await _debts(session)) == 1


async def test_different_amount_is_not_a_duplicate(session):
    await _claim(session, minutes_ago=10)
    other = await _claim(session, 6_000_000)
    assert other.duplicate_of is None
    assert await claims.pending_count(session) == 2


async def test_owner_debt_first_then_claim_is_auto_own(session):
    await _person(session)
    note = await _interaction(session, InteractionSource.assistant_bot)
    await apply_extraction(session, note, ex.ExtractionResult(debts=[_debt(by="me")]))
    [debt] = await _debts(session)
    claim = await _claim(session)
    [resolved] = await claims.resolve_superseded(session, now=NOW)
    assert resolved.id == claim.id
    assert (claim.state, claim.answered_by) == (claims.AUTO, claims.BY_AUTO_OWN)
    assert (claim.result_kind, claim.result_id) == ("debt", debt.id)
    assert claim.history[-1]["new"] == f"d{debt.id}"
    assert len(await _debts(session)) == 1


async def test_claim_first_then_owner_writes_it(session):
    await _person(session)
    claim = await _claim(session)
    note = await _interaction(session, InteractionSource.assistant_bot)
    await apply_extraction(session, note, ex.ExtractionResult(debts=[_debt(by="me")]))
    assert claim.state == claims.AUTO and claim.answered_by == claims.BY_AUTO_OWN
    assert len(await _debts(session)) == 1


async def test_ambiguous_name_is_not_superseded(session):
    first = await _person(session, "Akmal")
    await _person(session, "Akmal")
    session.add(
        m.Debt(
            person_id=first.id,
            direction="i_owe_them",
            amount=5_000_000,
            currency="UZS",
        )
    )
    await session.flush()
    claim = await _claim(session)
    assert await claims.resolve_superseded(session, now=NOW) == []
    assert claim.state == claims.PENDING


async def test_a_debt_written_from_a_claim_never_supersedes_another_claim(session):
    await _person(session)
    first = await _claim(session, minutes_ago=10)
    await claims.accept(session, first.id, by=claims.BY_BUTTON, now=NOW)
    other = await claims.create(
        session,
        await _interaction(session),
        claims.KIND_DEBT,
        _debt(),
        now=NOW + timedelta(minutes=1),
    )
    assert await claims.resolve_superseded(session, now=NOW) == []
    assert other.state == claims.PENDING

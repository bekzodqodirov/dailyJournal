"""Integration tests that need a real PostgreSQL with pgvector and the schema applied.

Skipped automatically when the database is unreachable, so `make test` still
works on a laptop with nothing running:

    make up && make test
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
import sqlalchemy as sa

from miya.db import models as m
from miya.db.enums import (
    Currency,
    DebtDirection,
    Direction,
    InteractionSource,
    PromiseMadeBy,
)


@pytest.fixture
async def seeded(session):
    """One person and one interaction with a debt, promise and memory derived from it."""
    person = m.Person(display_name="Akmal aka", aliases=["Akmal GZ", "Akmal"])
    session.add(person)
    await session.flush()

    interaction = m.Interaction(
        source=InteractionSource.assistant_bot,
        direction=Direction.in_,
        person_id=person.id,
        occurred_at=datetime.now(UTC),
        raw_text="Akmalga 5 mln so'm berdim, 25-avgustgacha qaytaradi",
        media={"type": "voice", "processed": True},
        meta={"lang": "uz"},
    )
    session.add(interaction)
    await session.flush()

    debt = m.Debt(
        direction=DebtDirection.they_owe_me,
        person_id=person.id,
        amount=Decimal("5000000.00"),
        currency=Currency.UZS,
        reason="qarz berdim",
        source_interaction_id=interaction.id,
    )
    session.add_all(
        [
            debt,
            m.Promise(
                made_by=PromiseMadeBy.them,
                person_id=person.id,
                description="25-avgustda qaytaradi",
                source_interaction_id=interaction.id,
            ),
            m.Memory(
                content="Akmal aka — Guangzhou'dagi yetkazib beruvchi",
                embedding=[0.01] * 1024,
                occurred_at=datetime.now(UTC),
                tags=["supplier", "logistics"],
                source_interaction_id=interaction.id,
            ),
        ]
    )
    await session.flush()
    session.add(
        m.DebtPayment(
            debt_id=debt.id, amount=Decimal("1000000.00"), currency=Currency.UZS
        )
    )
    await session.commit()
    return {"person": person, "interaction": interaction, "debt": debt}


async def test_money_survives_as_exact_decimal(session, seeded):
    amount = await session.scalar(
        sa.select(m.Debt.amount).where(m.Debt.id == seeded["debt"].id)
    )
    assert isinstance(amount, Decimal)
    assert amount == Decimal("5000000.00")


async def test_direction_is_stored_as_in_not_in_underscore(session, seeded):
    label = await session.scalar(
        sa.text("SELECT direction::text FROM interactions WHERE id = :i").bindparams(
            i=seeded["interaction"].id
        )
    )
    assert label == "in"


async def test_cosine_search_finds_the_embedded_memory(session, seeded):
    hit = await session.scalar(
        sa.select(m.Memory.content).order_by(
            m.Memory.embedding.cosine_distance([0.01] * 1024)
        )
    )
    assert hit == "Akmal aka — Guangzhou'dagi yetkazib beruvchi"


async def test_purging_an_interaction_cascades_to_derived_rows(session, seeded):
    """Spec §10: `/unut` must be able to erase everything an interaction produced."""
    await session.execute(
        sa.delete(m.Interaction).where(m.Interaction.id == seeded["interaction"].id)
    )
    await session.commit()

    for model in (m.Debt, m.DebtPayment, m.Promise, m.Memory):
        remaining = await session.scalar(sa.select(sa.func.count()).select_from(model))
        assert remaining == 0, f"{model.__name__} kept {remaining} orphan row(s)"

    # The person outlives the interaction — only derived rows are purged.
    assert await session.scalar(sa.select(sa.func.count()).select_from(m.Person)) == 1


async def test_debt_amount_must_be_positive(session):
    person = m.Person(display_name="Test")
    session.add(person)
    await session.flush()
    session.add(
        m.Debt(
            direction=DebtDirection.i_owe_them,
            person_id=person.id,
            amount=Decimal("-1.00"),
            currency=Currency.USD,
        )
    )
    with pytest.raises(sa.exc.IntegrityError):
        await session.flush()
    await session.rollback()

"""End-to-end ingestion: an Uzbek note in, database rows and a reply out.

Only the Anthropic call is stubbed — the extraction schema, person resolution,
persistence, cost accounting and reply formatting are all the real thing.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest
import sqlalchemy as sa

from miya.bot import replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import Currency, DebtStatus, Direction, InteractionSource
from miya.services import extraction as ex
from miya.services import ingest
from miya.services.ingest import create_interaction, process_interaction


class _StubbedExtraction:
    """Replaces `extract()` with a canned outcome and records what it saw."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.seen: list[str] = []

    async def __call__(self, text, *, now=None):
        self.seen.append(text)
        return self.outcome


def _usage():
    return type(
        "Usage",
        (),
        {
            "input_tokens": 800,
            "output_tokens": 120,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    )()


@pytest.fixture
def stub_extract(monkeypatch):
    def _install(result=None, error=None):
        outcome = ex.ExtractionOutcome(
            result=result,
            error=error,
            model=settings.extract_model,
            usage=_usage(),
        )
        stub = _StubbedExtraction(outcome)
        monkeypatch.setattr(ingest, "extract", stub)
        return stub

    return _install


async def test_an_uzbek_note_becomes_debt_promise_and_a_confirmation(
    session, stub_extract
):
    stub_extract(
        ex.ExtractionResult(
            summary="Akmalga 5 mln so'm qarz berildi",
            language="uz",
            debts=[
                ex.ExtractedDebt(
                    direction="they_owe_me",
                    person="Akmal aka",
                    amount=5_000_000,
                    currency="UZS",
                    reason="qarz",
                    due_date="2026-08-25",
                )
            ],
            promises=[
                ex.ExtractedPromise(
                    made_by="them",
                    person="Akmal aka",
                    description="25-avgustda qaytaradi",
                    due_date="2026-08-25",
                )
            ],
            facts=["Akmal aka Guangzhou'da yetkazib beruvchi"],
            tags=["supplier", "finance"],
        )
    )

    interaction = await create_interaction(
        session,
        source=InteractionSource.assistant_bot,
        direction=Direction.in_,
        text="Akmal akaga 5 mln so'm berdim, 25-avgustgacha qaytaradi",
    )
    result = await process_interaction(session, interaction)
    await session.commit()

    assert result.ok
    assert interaction.processed is True
    assert interaction.needs_review is False
    assert interaction.summary == "Akmalga 5 mln so'm qarz berildi"

    debt = await session.scalar(sa.select(m.Debt))
    assert debt.amount == Decimal("5000000.00")
    assert debt.currency is Currency.UZS
    assert debt.status is DebtStatus.open
    assert debt.source_interaction_id == interaction.id

    person = await session.get(m.Person, debt.person_id)
    assert person.display_name == "Akmal aka"

    # The confirmation the owner actually receives.
    text = replies.confirmation(result.applied)
    assert "5 mln so'm" in text and "25-avg" in text

    # The call was billed.
    logged = await session.scalar(
        sa.select(m.UsageLog).where(m.UsageLog.operation == "extract")
    )
    assert logged.input_tokens == 800
    assert logged.cost_usd > 0


async def test_a_repayment_note_settles_the_existing_debt(session, stub_extract):
    stub_extract(
        ex.ExtractionResult(
            debts=[
                ex.ExtractedDebt(
                    direction="they_owe_me",
                    person="Akmal",
                    amount=5_000_000,
                    currency="UZS",
                )
            ]
        )
    )
    first = await create_interaction(
        session, source=InteractionSource.assistant_bot, text="Akmalga 5 mln berdim"
    )
    await process_interaction(session, first)

    stub_extract(
        ex.ExtractionResult(
            debt_settlements=[
                ex.ExtractedSettlement(
                    person="Akmal", amount=5_000_000, currency="UZS", note="naqd"
                )
            ]
        )
    )
    second = await create_interaction(
        session, source=InteractionSource.assistant_bot, text="Akmal 5 mln qaytardi"
    )
    result = await process_interaction(session, second)
    await session.commit()

    debt = await session.scalar(sa.select(m.Debt))
    assert debt.status is DebtStatus.settled
    assert len(result.applied.settlements) == 1
    assert "✅ To'lov" in replies.confirmation(result.applied)


async def test_a_failed_extraction_flags_review_and_keeps_the_raw_text(
    session, stub_extract
):
    """Spec §14: data is never silently dropped."""
    stub_extract(error="APIConnectionError: boom")

    interaction = await create_interaction(
        session,
        source=InteractionSource.assistant_bot,
        text="Akmalga 5 mln berdim",
    )
    result = await process_interaction(session, interaction)
    await session.commit()

    assert not result.ok
    assert interaction.needs_review is True
    assert interaction.processed is False
    assert interaction.raw_text == "Akmalga 5 mln berdim"
    assert await session.scalar(sa.select(sa.func.count()).select_from(m.Debt)) == 0


async def test_transcript_and_vision_are_both_fed_to_the_extractor(session, stub_extract):
    stub = stub_extract(ex.ExtractionResult())

    interaction = await create_interaction(
        session,
        source=InteractionSource.receipt_photo,
        text="chek",
        meta={"vision": "Jami: 145 000 so'm"},
    )
    interaction.transcript = "bugun tushlik qildim"
    await session.flush()

    await process_interaction(session, interaction)

    sent = stub.seen[0]
    assert "chek" in sent
    assert "bugun tushlik qildim" in sent
    assert "145 000" in sent


async def test_an_interaction_with_no_text_is_marked_processed_without_a_call(
    session, stub_extract
):
    stub = stub_extract(ex.ExtractionResult())

    interaction = await create_interaction(
        session, source=InteractionSource.assistant_bot, text=None
    )
    result = await process_interaction(session, interaction)

    assert result.ok
    assert interaction.processed is True
    assert stub.seen == []


async def test_interactions_carry_local_tashkent_time(session, stub_extract):
    stub_extract(ex.ExtractionResult())
    interaction = await create_interaction(
        session, source=InteractionSource.assistant_bot, text="salom"
    )
    await session.commit()

    assert interaction.occurred_at.tzinfo is not None
    local = interaction.occurred_at.astimezone(settings.tz)
    assert local.utcoffset().total_seconds() == 5 * 3600
    assert isinstance(local, datetime)

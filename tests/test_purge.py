"""Purge tooling (spec §10): plans are exact, and execution cascades."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa

from miya.config import settings
from miya.db import models as m
from miya.db.enums import (
    Currency,
    DebtDirection,
    Direction,
    InteractionSource,
    PromiseMadeBy,
)
from miya.services import purge
from miya.services.people import resolve_person

TZ = settings.tz


async def _interaction(
    session,
    *,
    person: m.Person | None = None,
    chat: int | None = None,
    when: datetime | None = None,
    media: dict | None = None,
) -> m.Interaction:
    interaction = m.Interaction(
        source=InteractionSource.telegram_userbot if chat else InteractionSource.manual,
        direction=Direction.in_,
        person_id=person.id if person else None,
        tg_chat_id=chat,
        occurred_at=when or datetime.now(TZ),
        raw_text="test",
        media=media,
    )
    session.add(interaction)
    await session.flush()
    return interaction


async def _full_person(session, name="Akmal"):
    """A person with a debt, a promise, a memory and a media interaction."""
    person = await resolve_person(session, name)
    interaction = await _interaction(session, person=person)
    session.add_all(
        [
            m.Debt(
                direction=DebtDirection.they_owe_me,
                person_id=person.id,
                amount=Decimal("5000000"),
                currency=Currency.UZS,
                source_interaction_id=interaction.id,
            ),
            m.Promise(
                made_by=PromiseMadeBy.them,
                person_id=person.id,
                description="ertaga to'laydi",
                source_interaction_id=interaction.id,
            ),
            m.Memory(
                content="Akmal GZ orqali yuk yuboradi",
                occurred_at=datetime.now(TZ),
                tags=[],
                source_interaction_id=interaction.id,
            ),
        ]
    )
    await session.flush()
    return person, interaction


# --- planning ----------------------------------------------------------------


async def test_a_person_plan_counts_everything_that_would_go(session):
    person, _ = await _full_person(session)

    plan = await purge.plan_person(session, person)

    assert plan.kind == "person"
    assert plan.label == "Akmal"
    assert plan.counts["interactions"] == 1
    assert plan.counts["debts"] == 1
    assert plan.counts["promises"] == 1
    assert plan.counts["memories"] == 1
    assert plan.is_empty() is False


async def test_a_plan_lists_media_files_without_touching_them(session, tmp_path):
    audio = tmp_path / "call.m4a"
    audio.write_bytes(b"audio")
    await _interaction(session, media={"type": "voice", "path": str(audio)})

    plan = await purge.plan_range(session, purge.today(), purge.today())

    assert str(audio) in plan.files
    assert audio.exists()  # planning never deletes


async def test_an_empty_plan_is_recognised(session):
    plan = await purge.plan_range(session, date(2020, 1, 1), date(2020, 1, 2))
    assert plan.is_empty() is True


# --- execution ---------------------------------------------------------------


async def test_purging_a_person_removes_every_derived_row(session):
    person, _ = await _full_person(session)
    plan = await purge.plan_person(session, person)

    result = await purge.execute(session, plan)
    await session.commit()

    assert result.person_deleted is True
    for model in (m.Person, m.Interaction, m.Debt, m.Promise, m.Memory):
        remaining = await session.scalar(sa.select(sa.func.count()).select_from(model))
        assert remaining == 0, f"{model.__name__} survived the purge"


async def test_purging_one_person_leaves_the_other_alone(session):
    akmal, _ = await _full_person(session, "Akmal")
    await _full_person(session, "Dilshod")

    await purge.execute(session, await purge.plan_person(session, akmal))
    await session.commit()

    people = list(await session.scalars(sa.select(m.Person.display_name)))
    assert people == ["Dilshod"]
    assert await session.scalar(sa.select(sa.func.count()).select_from(m.Debt)) == 1


async def test_purging_a_chat_removes_its_messages_and_windows(session):
    session.add(m.ChatMonitor(tg_chat_id=-500, chat_type="group", title="Ish guruhi"))
    now = datetime.now(TZ)
    await _interaction(session, chat=-500, when=now)
    await _interaction(session, chat=-501, when=now)
    session.add(
        m.ConversationWindow(
            tg_chat_id=-500,
            started_at=now,
            ended_at=now,
            message_count=1,
            char_count=10,
            text="[ME] salom",
            custom_id="w-purge-test",
        )
    )
    await session.flush()

    plan = await purge.plan_chat(session, -500)
    assert plan.label == "Ish guruhi"

    await purge.execute(session, plan)
    await session.commit()

    chats = list(await session.scalars(sa.select(m.Interaction.tg_chat_id)))
    assert chats == [-501]
    windows = await session.scalar(
        sa.select(sa.func.count()).select_from(m.ConversationWindow)
    )
    assert windows == 0


async def test_purging_a_date_range_spares_everything_outside_it(session):
    now = datetime.now(TZ)
    await _interaction(session, when=now - timedelta(days=10))
    keeper = await _interaction(session, when=now)

    start = (now - timedelta(days=11)).date()
    end = (now - timedelta(days=9)).date()
    await purge.execute(session, await purge.plan_range(session, start, end))
    await session.commit()

    survivors = list(await session.scalars(sa.select(m.Interaction.id)))
    assert survivors == [keeper.id]


async def test_purging_deletes_the_media_from_disk(session, tmp_path):
    audio = tmp_path / "call.m4a"
    audio.write_bytes(b"audio")
    converted = tmp_path / "call.mp3"
    converted.write_bytes(b"audio")
    await _interaction(
        session,
        media={"type": "voice", "path": str(audio), "audio_path": str(converted)},
    )

    plan = await purge.plan_range(session, purge.today(), purge.today())
    result = await purge.execute(session, plan)
    await session.commit()

    assert result.files_deleted == 2
    assert not audio.exists()
    assert not converted.exists()


async def test_a_missing_media_file_does_not_break_the_purge(session, tmp_path):
    await _interaction(
        session, media={"type": "voice", "path": str(tmp_path / "gone.m4a")}
    )

    result = await purge.execute(
        session, await purge.plan_range(session, purge.today(), purge.today())
    )

    assert result.interactions == 1
    assert result.files_deleted == 0


# --- argument parsing --------------------------------------------------------


def test_range_parsing_accepts_both_forms():
    assert purge.parse_range("2026-08-01..2026-08-15") == (
        date(2026, 8, 1),
        date(2026, 8, 15),
    )
    assert purge.parse_range("2026-08-01") == (date(2026, 8, 1), date(2026, 8, 1))
    # Reversed input is a typo, not an empty range.
    assert purge.parse_range("2026-08-15..2026-08-01") == (
        date(2026, 8, 1),
        date(2026, 8, 15),
    )


def test_a_name_is_not_mistaken_for_a_date():
    assert purge.parse_range("Akmal") is None
    assert purge.parse_range("chat GZ") is None

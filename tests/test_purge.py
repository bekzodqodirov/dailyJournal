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


# --- recaps and digests (WP-72) --------------------------------------------------


def _report(day: date, content: str, kind: str = "evening") -> m.DailyReport:
    return m.DailyReport(report_date=day, content=content, kind=kind, parts=[content])


def _digest(day: date, prose: str, *, person=None, chat=None, ids=()) -> m.RecapDigest:
    moment = datetime.combine(day, datetime.min.time(), tzinfo=TZ)
    return m.RecapDigest(
        digest_date=day,
        subject_key=f"k{len(prose)}{prose[:8]}",
        person_id=person.id if person else None,
        tg_chat_id=chat,
        window_start=moment,
        window_end=moment + timedelta(hours=20),
        input_hash="0" * 64,
        source_interaction_ids=list(ids),
        prose=prose,
        model="test",
    )


async def test_unut_person_removes_their_digests_and_the_recaps_of_those_days(session):
    person, interaction = await _full_person(session)
    day = interaction.occurred_at.astimezone(TZ).date()
    other_day = day - timedelta(days=5)
    session.add_all(
        [
            _report(day, "Akmal 5 mln qarz oldi"),
            _report(day + timedelta(days=1), "Kecha: Akmal …", kind="morning"),
            _report(other_day, "boshqa kun"),
            _digest(day, "Akmal bilan gaplashildi", person=person),
            _digest(other_day, "Akmal eslatildi", ids=[interaction.id]),
        ]
    )
    await session.flush()

    plan = await purge.plan_person(session, person)
    await purge.execute(session, plan)

    reports = list(await session.scalars(sa.select(m.DailyReport)))
    assert [r.content for r in reports] == ["boshqa kun"]
    assert await session.scalar(sa.select(sa.func.count(m.RecapDigest.id))) == 0


async def test_unut_range_removes_the_days_recaps(session):
    day = date(2026, 3, 10)
    session.add_all(
        [
            _report(day, "10-mart"),
            _report(date(2026, 3, 12), "12-mart"),
            _digest(day, "10-mart xulosa"),
        ]
    )
    await session.flush()

    plan = await purge.plan_range(session, day, day)
    assert plan.is_empty() is False
    await purge.execute(session, plan)

    reports = [r.content for r in await session.scalars(sa.select(m.DailyReport))]
    assert reports == ["12-mart"]
    assert await session.scalar(sa.select(sa.func.count(m.RecapDigest.id))) == 0


async def test_unut_chat_removes_group_digests(session):
    await _interaction(session, chat=-500)
    session.add_all(
        [
            _digest(purge.today(), "guruh", chat=-500),
            _digest(purge.today(), "boshqa", chat=-501),
        ]
    )
    await session.flush()

    plan = await purge.plan_chat(session, -500)
    await purge.execute(session, plan)

    left = [d.prose for d in await session.scalars(sa.select(m.RecapDigest))]
    assert left == ["boshqa"]


async def test_preview_counts_reports_and_digests(session):
    from miya.bot import replies

    person, interaction = await _full_person(session)
    day = interaction.occurred_at.astimezone(TZ).date()
    session.add_all([_report(day, "x"), _digest(day, "y", person=person)])
    await session.flush()

    text = replies.purge_preview(await purge.plan_person(session, person))

    assert "kunlik xulosa: 1 ta" in text and "AI xulosa: 1 ta" in text
    assert replies.PURGE_REPORTS_NOTE in text


async def test_recap_rows_of_untouched_days_survive(session):
    person, interaction = await _full_person(session)
    day = interaction.occurred_at.astimezone(TZ).date()
    session.add(_report(day - timedelta(days=3), "tinch kun"))
    await session.flush()

    await purge.execute(session, await purge.plan_person(session, person))

    assert [r.content for r in await session.scalars(sa.select(m.DailyReport))] == [
        "tinch kun"
    ]

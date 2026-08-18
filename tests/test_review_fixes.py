"""Regression tests for the adversarial-review findings.

Each test here reproduces a confirmed production bug from the Phase 1+2 review
and pins the fix.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import sqlalchemy as sa

from miya.bot import replies
from miya.bot.formatting import TELEGRAM_LIMIT, clip
from miya.config import settings
from miya.db import models as m
from miya.db.enums import (
    Currency,
    DebtDirection,
    DebtStatus,
    Direction,
    WindowStatus,
)
from miya.db.session import SessionLocal
from miya.services import extraction as ex
from miya.services import reminders
from miya.services.people import resolve_person
from miya.services.persistence import apply_extraction
from miya.services.transcription import ElevenLabsScribe, TranscriptionError
from tests.test_pipeline import _interaction, _open_debt

TZ = settings.tz


# --- settlement double-pay (autoflush=False made payments invisible) ---------


async def test_two_settlements_in_one_extraction_do_not_double_pay(session):
    """Repro from the review: debts A=40k, B=100k; settlements 70k then 50k.

    Before the fix, debt A collected 80k in payments against a 40k principal
    and B's balance came out wrong. Payments must land oldest-due-first with
    each settlement seeing the previous one's effect.
    """
    person = await resolve_person(session, "Akmal")
    interaction = await _interaction(session)
    debt_a = m.Debt(
        direction=DebtDirection.they_owe_me,
        person_id=person.id,
        amount=Decimal("40000"),
        currency=Currency.UZS,
        due_date=date(2026, 8, 1),
        source_interaction_id=interaction.id,
    )
    debt_b = m.Debt(
        direction=DebtDirection.they_owe_me,
        person_id=person.id,
        amount=Decimal("100000"),
        currency=Currency.UZS,
        due_date=date(2026, 9, 1),
        source_interaction_id=interaction.id,
    )
    session.add_all([debt_a, debt_b])
    await session.flush()

    await apply_extraction(
        session,
        await _interaction(session),
        ex.ExtractionResult(
            debt_settlements=[
                ex.ExtractedSettlement(person="Akmal", amount=70_000, currency="UZS"),
                ex.ExtractedSettlement(person="Akmal", amount=50_000, currency="UZS"),
            ]
        ),
    )
    await session.commit()

    paid_a = await session.scalar(
        sa.select(sa.func.coalesce(sa.func.sum(m.DebtPayment.amount), 0)).where(
            m.DebtPayment.debt_id == debt_a.id
        )
    )
    paid_b = await session.scalar(
        sa.select(sa.func.coalesce(sa.func.sum(m.DebtPayment.amount), 0)).where(
            m.DebtPayment.debt_id == debt_b.id
        )
    )
    # 120k total: 40k settles A, the remaining 80k goes to B.
    assert paid_a == Decimal("40000.00")
    assert paid_b == Decimal("80000.00")
    assert debt_a.status is DebtStatus.settled
    assert debt_b.status is DebtStatus.partially_paid


# --- resolve_person race (bot and worker are separate processes) -------------


async def test_concurrent_resolution_of_the_same_name_yields_one_person(session):
    """Two transactions resolving the same new name must not both insert."""

    async def resolve_in_new_session(delay: float) -> int:
        await asyncio.sleep(delay)
        async with SessionLocal() as s:
            person = await resolve_person(s, "Karim")
            await s.commit()
            return person.id

    ids = await asyncio.gather(resolve_in_new_session(0), resolve_in_new_session(0.01))

    assert ids[0] == ids[1]
    total = await session.scalar(
        sa.select(sa.func.count())
        .select_from(m.Person)
        .where(m.Person.display_name == "Karim")
    )
    assert total == 1


# --- reminder dedupe key must include direction ------------------------------


async def test_opposite_direction_debts_get_separate_reminders(session):
    person, _ = await _open_debt(session, due=date(2026, 1, 1))
    first = await reminders.collect_due(session)
    assert first.debts
    await reminders.mark_sent(session, first)
    await session.flush()

    # A debt in the opposite direction, same person and currency, now due.
    session.add(
        m.Debt(
            direction=DebtDirection.i_owe_them,
            person_id=person.id,
            amount=Decimal("1000000"),
            currency=Currency.UZS,
            due_date=date(2026, 1, 1),
        )
    )
    await session.flush()

    second = await reminders.collect_due(session)
    directions = {b.direction for b in second.debts}
    assert DebtDirection.i_owe_them in directions


# --- oversized replies are clipped, never rejected by Telegram ---------------


def test_clip_caps_a_message_under_the_telegram_limit():
    body = "\n".join(f"qator {i}: " + "x" * 80 for i in range(200))
    clipped = clip(body)
    assert len(clipped) <= TELEGRAM_LIMIT + 100  # marker fits in the margin
    assert clipped.endswith("<i>(qisqartirildi)</i>")


def test_clip_leaves_short_messages_alone():
    assert clip("salom") == "salom"


def test_a_huge_reminder_body_is_clipped():
    balances = [
        replies.DebtBalance(
            person=m.Person(display_name=f"Odam {i} " + "x" * 50, aliases=[]),
            direction=DebtDirection.they_owe_me,
            currency=Currency.UZS,
            outstanding=Decimal("1000000"),
            earliest_due=date(2026, 1, 1),
            count=1,
        )
        for i in range(200)
    ]
    body = replies.reminder(balances, [], [], [])
    assert len(body) <= TELEGRAM_LIMIT + 100


# --- /kim with HTML in the argument ------------------------------------------


def test_person_not_found_escapes_the_queried_name():
    body = replies.person_not_found("Ali <ukam>")
    assert "<ukam>" not in body
    assert "&lt;ukam&gt;" in body


# --- /tekshir surfaces flagged rows ------------------------------------------


async def test_review_report_lists_flagged_interactions(session):
    interaction = await _interaction(session, text="o'qilmagan xabar")
    interaction.needs_review = True
    await session.flush()

    from miya.services.queries import flagged_interactions

    flagged, total = await flagged_interactions(session)
    body = replies.review_report(flagged, total)

    assert total == 1
    assert "1 ta yozuv qayta ishlanmagan" in body
    assert "o'qilmagan xabar" in body


def test_review_report_when_everything_is_processed():
    assert "yo'q" in replies.review_report([], 0)


# --- Scribe returning a non-JSON 200 must not re-bill every sweep ------------


async def test_scribe_garbage_body_raises_transcription_error(monkeypatch, tmp_path):
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"xx")

    async def fake_post(self, url, **kwargs):
        return httpx.Response(200, text="<html>gateway error</html>")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    scribe = ElevenLabsScribe(api_key="k")

    with pytest.raises(TranscriptionError):
        await scribe.transcribe(audio)


# --- settlement direction (a repayment has a side) ---------------------------


async def _two_sided_debts(session):
    """The owner and Akmal owe each other — routine with a regular supplier."""
    person = await resolve_person(session, "Akmal")
    interaction = await _interaction(session)
    i_owe = m.Debt(
        direction=DebtDirection.i_owe_them,
        person_id=person.id,
        amount=Decimal("3000000"),
        currency=Currency.UZS,
        due_date=date(2026, 8, 20),  # due sooner, so it sorts first
        source_interaction_id=interaction.id,
    )
    they_owe = m.Debt(
        direction=DebtDirection.they_owe_me,
        person_id=person.id,
        amount=Decimal("5000000"),
        currency=Currency.UZS,
        due_date=date(2026, 9, 25),
        source_interaction_id=interaction.id,
    )
    session.add_all([i_owe, they_owe])
    await session.flush()
    return person, i_owe, they_owe


async def test_a_repayment_settles_the_side_it_was_made_on(session):
    """Repro: "Akmal 5 mln qaytardi" used to settle the owner's OWN debt."""
    _, i_owe, they_owe = await _two_sided_debts(session)

    await apply_extraction(
        session,
        await _interaction(session),
        ex.ExtractionResult(
            debt_settlements=[
                ex.ExtractedSettlement(
                    person="Akmal",
                    amount=5_000_000,
                    currency="UZS",
                    direction="they_owe_me",
                )
            ]
        ),
    )
    await session.flush()

    assert they_owe.status is DebtStatus.settled
    assert i_owe.status is DebtStatus.open  # the owner's own debt is untouched


async def test_the_owner_paying_back_settles_his_own_debt(session):
    _, i_owe, they_owe = await _two_sided_debts(session)

    await apply_extraction(
        session,
        await _interaction(session),
        ex.ExtractionResult(
            debt_settlements=[
                ex.ExtractedSettlement(
                    person="Akmal",
                    amount=3_000_000,
                    currency="UZS",
                    direction="i_owe_them",
                )
            ]
        ),
    )
    await session.flush()

    assert i_owe.status is DebtStatus.settled
    assert they_owe.status is DebtStatus.open


async def test_an_ambiguous_repayment_pays_nothing_and_asks(session):
    """With both sides open and no direction, guessing would invert the books."""
    _, i_owe, they_owe = await _two_sided_debts(session)

    applied = await apply_extraction(
        session,
        await _interaction(session),
        ex.ExtractionResult(
            debt_settlements=[
                ex.ExtractedSettlement(person="Akmal", amount=5_000_000, currency="UZS")
            ]
        ),
    )
    await session.flush()

    assert i_owe.status is DebtStatus.open
    assert they_owe.status is DebtStatus.open
    assert applied.settlements == []
    assert applied.ambiguous_settlements == [
        ("Akmal", Decimal("5000000.00"), Currency.UZS)
    ]
    body = replies.confirmation(applied)
    assert "kim to'laganini yozing" in body


async def test_a_one_sided_repayment_still_needs_no_direction(session):
    """Only one side open: the direction is not in doubt, so it just applies."""
    person, debt = await _open_debt(session, amount="4000000")

    applied = await apply_extraction(
        session,
        await _interaction(session),
        ex.ExtractionResult(
            debt_settlements=[
                ex.ExtractedSettlement(person="Akmal", amount=4_000_000, currency="UZS")
            ]
        ),
    )
    await session.flush()

    assert debt.status is DebtStatus.settled
    assert applied.ambiguous_settlements == []


# --- purge must not resurrect a half-deleted conversation --------------------


async def test_a_purged_window_never_resurrects_its_surviving_messages(session):
    """Repro: a conversation crossing midnight, purged for one of those days.

    The window row went with the second day; the FK nulled the first day's
    message window_id, and the flush job re-windowed it — re-billing the
    extraction and recreating the very rows the owner had just deleted.
    """
    from miya.db.enums import InteractionSource
    from miya.services import purge, windows

    now = datetime.now(TZ)
    base = (now - timedelta(days=2)).replace(hour=23, minute=50, second=0, microsecond=0)
    for index, when in enumerate([base, base + timedelta(minutes=15)]):
        session.add(
            m.Interaction(
                source=InteractionSource.telegram_userbot,
                direction=Direction.in_,
                tg_chat_id=-100999,
                occurred_at=when,
                raw_text=f"xabar {index}",
            )
        )
    await session.flush()

    [window] = await windows.flush_ready_windows(session, now=now)
    window.status = WindowStatus.applied
    await session.execute(
        sa.update(m.Interaction)
        .where(m.Interaction.window_id == window.id)
        .values(processed=True)
    )
    await session.flush()
    # The window ended on the day after its first message.
    assert (
        window.started_at.astimezone(TZ).date() != window.ended_at.astimezone(TZ).date()
    )

    day = window.ended_at.astimezone(TZ).date()
    await purge.execute(session, await purge.plan_range(session, day, day))
    await session.flush()

    survivor = await session.scalar(sa.select(m.Interaction))
    assert survivor is not None and survivor.window_id is None  # FK nulled it
    assert await windows.flush_ready_windows(session, now=now) == []


# --- every billed attempt is accounted for -----------------------------------


async def test_a_retried_extraction_reports_both_billed_calls(monkeypatch):
    """An attempt that hits max_tokens is charged; it must reach usage_log."""
    from types import SimpleNamespace

    def _usage(input_tokens, output_tokens):
        return SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )

    responses = [
        SimpleNamespace(
            stop_reason="max_tokens", usage=_usage(500, 4096), parsed_output=None
        ),
        SimpleNamespace(
            stop_reason="end_turn",
            usage=_usage(600, 200),
            parsed_output=ex.ExtractionResult(summary="ok"),
        ),
    ]

    class _Stub:
        def __init__(self):
            self.messages = self

        async def parse(self, **kwargs):
            return responses.pop(0)

    monkeypatch.setattr(ex, "get_client", lambda: _Stub())

    outcome = await ex.extract("Akmalga 5 mln berdim")

    assert outcome.ok
    assert outcome.usage.calls == 2
    assert outcome.usage.input_tokens == 1100  # both attempts, not just the last
    assert outcome.usage.output_tokens == 4296


async def test_two_failed_attempts_still_report_their_cost(monkeypatch):
    from types import SimpleNamespace

    def _response():
        return SimpleNamespace(
            stop_reason="max_tokens",
            usage=SimpleNamespace(
                input_tokens=500,
                output_tokens=4096,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
            parsed_output=None,
        )

    class _Stub:
        def __init__(self):
            self.messages = self

        async def parse(self, **kwargs):
            return _response()

    monkeypatch.setattr(ex, "get_client", lambda: _Stub())

    outcome = await ex.extract("juda uzun matn")

    assert not outcome.ok
    assert outcome.usage is not None and outcome.usage.calls == 2


# --- a brand-new chat seen twice at once -------------------------------------


async def test_two_messages_from_a_new_chat_do_not_collide(session):
    """Telethon can hand us two updates from an unknown chat concurrently."""
    from miya.db.enums import ChatType
    from miya.db.session import SessionLocal
    from miya.services.chats import DialogInfo, ensure_monitor

    dialog = DialogInfo(tg_chat_id=-100555, chat_type=ChatType.group, title="Yangi")

    async def register(delay: float) -> int:
        await asyncio.sleep(delay)
        async with SessionLocal() as s:
            monitor = await ensure_monitor(s, dialog)
            await s.commit()
            return monitor.id

    ids = await asyncio.gather(register(0), register(0.01))

    assert ids[0] == ids[1]
    total = await session.scalar(
        sa.select(sa.func.count())
        .select_from(m.ChatMonitor)
        .where(m.ChatMonitor.tg_chat_id == -100555)
    )
    assert total == 1


# --- untrusted text in owner-facing HTML -------------------------------------


async def _plain(value):
    return value


async def test_a_hostile_contact_name_cannot_break_the_daily_report(session, monkeypatch):
    """A Telegram contact picks their own name; it lands in an HTML message."""
    from miya.services import planner, reports

    # gather() builds tomorrow's plan; keep the model out of this test.
    monkeypatch.setattr(planner, "plan_for", lambda *a, **k: _plain("REJA"))

    person = await resolve_person(session, "<b>Akmal</b>")
    interaction = await _interaction(session)
    interaction.person_id = person.id
    session.add(
        m.Promise(
            made_by="them",
            person_id=person.id,
            description="<script>alert(1)</script> to'laydi",
            due_date=date(2026, 1, 1),
            source_interaction_id=interaction.id,
        )
    )
    await session.flush()

    data = await reports.gather(session, datetime.now(TZ).date())
    block = reports.render_data_block(data)

    assert "<b>Akmal</b>" not in block
    assert "&lt;b&gt;Akmal&lt;/b&gt;" in block
    assert "<script>" not in block


async def test_the_planner_escapes_event_titles_too(session):
    from miya.services import planner

    session.add(
        m.Event(
            title="<i>Bojxona</i>",
            start_at=datetime.now(TZ) + timedelta(days=1),
            location="<b>Toshkent</b>",
            source="extracted",
            status="planned",
        )
    )
    await session.flush()

    inputs = await planner.plan_inputs(
        session, (datetime.now(TZ) + timedelta(days=1)).date()
    )
    block = planner.render_inputs(inputs)

    assert "<i>Bojxona</i>" not in block
    assert "&lt;i&gt;Bojxona&lt;/i&gt;" in block


async def test_search_results_are_labelled_as_other_peoples_words(session):
    """Prompt-injection guard: a supplier's text must arrive quoted, not obeyed."""
    from miya.services import rag

    output = await rag._run_tool(session, None, "recent_interactions", {})
    payload = json.loads(output)

    assert payload["note"] == rag.UNTRUSTED_NOTE
    assert "results" in payload


# --- a clipped reminder must come back, not starve ---------------------------


def _balance(index: int):
    from miya.services.queries import DebtBalance

    return DebtBalance(
        person=m.Person(
            id=index, display_name=f"Qarzdor {index} " + "x" * 40, aliases=[]
        ),
        direction=DebtDirection.they_owe_me,
        currency=Currency.UZS,
        outstanding=Decimal("1000000"),
        earliest_due=date(2026, 1, 1),
        count=1,
    )


def test_only_what_fits_is_reported_as_rendered():
    debts = [_balance(i) for i in range(60)]
    tasks = [
        m.Task(id=i, description=f"vazifa {i} " + "y" * 60, due_date=date(2026, 1, 1))
        for i in range(30)
    ]

    body, counts = replies.reminder_with_counts(debts, [], tasks, [])

    assert len(body) <= replies.TELEGRAM_LIMIT
    assert counts["debt"] < 60 or counts["task"] < 30  # something had to give
    # The counts describe the body, not the input.
    assert body.count("Qarzdor") == counts["debt"]
    assert "va yana" in body  # the owner is told there is more


def test_a_meeting_is_never_the_item_that_gets_clipped():
    """Events only qualify within the hour; a dropped one is gone for good."""
    from datetime import datetime as dt

    debts = [_balance(i) for i in range(80)]
    events = [
        m.Event(id=1, title="Bojxona uchrashuvi", start_at=dt.now(TZ), source="extracted")
    ]

    body, counts = replies.reminder_with_counts(debts, [], [], events)

    assert counts["event"] == 1
    assert "Bojxona uchrashuvi" in body


async def test_clipped_items_are_not_marked_as_sent(session):
    """Repro: everything collected was logged, so the tail starved forever."""
    from miya.db.enums import PromiseMadeBy
    from miya.services import reminders

    person = await resolve_person(session, "Akmal")
    interaction = await _interaction(session)
    for index in range(40):
        session.add(
            m.Promise(
                made_by=PromiseMadeBy.them,
                person_id=person.id,
                description=f"va'da {index} " + "z" * 70,
                due_date=date(2026, 1, 1),
                source_interaction_id=interaction.id,
            )
        )
    await session.flush()

    bundle = await reminders.collect_due(session)
    body, rendered = replies.reminder_with_counts(
        bundle.debts, bundle.promises, bundle.tasks, bundle.events
    )
    assert rendered["promise"] < len(bundle.promises)  # the body could not hold them

    await reminders.mark_sent(session, bundle, rendered=rendered)
    await session.flush()

    logged = await session.scalar(sa.select(sa.func.count()).select_from(m.ReminderLog))
    assert logged == rendered["promise"]

    # The next sweep therefore offers the ones that were left out.
    second = await reminders.collect_due(session)
    assert len(second.promises) == len(bundle.promises) - rendered["promise"]

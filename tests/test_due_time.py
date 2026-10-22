"""JV-03: promise and task deadlines keep the time of day.

«Ertaga 14:00 gacha hisoblab beraman» is stored as tomorrow 14:00, shown with
its time and reminded PROMISE_REMIND_LEAD_MINUTES before it. A date-only
deadline reads, sorts and is reminded exactly as before.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from miya.bot import formatting as f
from miya.bot import handlers, replies
from miya.config import Settings, settings
from miya.db import models as m
from miya.db.enums import Currency, DebtDirection, PromiseMadeBy, TransactionType
from miya.services import claims, queries, rag, records, reminders
from miya.services import extraction as ex
from miya.services.people import resolve_person
from miya.services.persistence import apply_extraction
from miya.worker import main as worker
from tests.test_close_and_correct import (
    _command,
    _Message,
    _payloads,
    _reload,
    bound,  # noqa: F401 — fixture
)
from tests.test_pipeline import _interaction

TZ = settings.tz
# A Thursday; every pure test passes it in.
TODAY = date(2026, 10, 22)


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=TZ)


def _now() -> datetime:
    return datetime.now(TZ)


async def _timed_promise(
    session, description="hisob-kitob", *, due_at: datetime | None = None, made_by=None
) -> tuple[m.Person, m.Promise]:
    person = await resolve_person(session, "Akmal")
    interaction = await _interaction(session)
    promise = m.Promise(
        made_by=made_by or PromiseMadeBy.me,
        person_id=person.id,
        description=description,
        due_date=due_at.date() if due_at else None,
        due_time=due_at.timetz().replace(tzinfo=None) if due_at else None,
        source_interaction_id=interaction.id,
    )
    session.add(promise)
    await session.flush()
    return person, promise


# --- 1. extraction splits the day and the time --------------------------------


@pytest.mark.parametrize(
    ("value", "day", "at"),
    [
        ("2026-10-23T14:00", date(2026, 10, 23), time(14, 0)),
        ("2026-10-23 09:30", date(2026, 10, 23), time(9, 30)),
        ("2026-10-23T14:00:00", date(2026, 10, 23), time(14, 0)),
        ("2026-10-23T14:00:00.000", date(2026, 10, 23), time(14, 0)),
        ("2026-10-23T9:30", date(2026, 10, 23), time(9, 30)),
        # An offset or "Z" is read on the owner's clock (Asia/Tashkent, UTC+5).
        ("2026-10-23T14:00+05:00", date(2026, 10, 23), time(14, 0)),
        ("2026-10-23T14:00Z", date(2026, 10, 23), time(19, 0)),
        ("2026-10-23T21:30Z", date(2026, 10, 24), time(2, 30)),
        ("2026-10-23", date(2026, 10, 23), None),
        # Midnight is what a date-only value looks like after a datetime round trip.
        ("2026-10-23T00:00", date(2026, 10, 23), None),
        # A bad time keeps the day; a bad day drops the deadline.
        ("2026-10-23T25:00", date(2026, 10, 23), None),
        ("2026-13-01T14:00", None, None),
        ("ertaga", None, None),
        (None, None, None),
    ],
)
def test_parse_iso_due_splits_day_and_time(value, day, at):
    item = ex.ExtractedPromise(
        made_by="me", person="Akmal", description="x", due_date=value
    )
    assert (item.due, item.due_time) == (day, at)
    task = ex.ExtractedTask(description="x", due_date=value)
    assert (task.due, task.due_time) == (day, at)


def test_a_time_parse_iso_due_cannot_read_is_logged(caplog):
    with caplog.at_level("WARNING", logger="miya.services.extraction"):
        assert ex.parse_iso_due("2026-10-23 at two") == (date(2026, 10, 23), None)
    assert "due time dropped" in caplog.text


def test_the_prompt_asks_for_the_time_of_day():
    prompt = ex.EXTRACTION_SYSTEM_PROMPT
    assert "YYYY-MM-DDTHH:MM" in prompt and "14:00" in prompt


async def test_extraction_keeps_time(session):
    interaction = await _interaction(session, "Ertaga 14:00 gacha hisoblab beraman")
    tomorrow = _now().date() + timedelta(days=1)
    applied = await apply_extraction(
        session,
        interaction,
        ex.ExtractionResult(
            promises=[
                ex.ExtractedPromise(
                    made_by="me",
                    person="Akmal",
                    description="hisoblab beraman",
                    due_date=f"{tomorrow.isoformat()}T14:00",
                )
            ],
            tasks=[
                ex.ExtractedTask(
                    description="hisobni tayyorlash", due_date=f"{tomorrow}T09:30"
                )
            ],
        ),
    )
    [promise] = applied.promises
    [task] = applied.tasks
    await session.flush()
    stored = await session.get(m.Promise, promise.id)
    assert (stored.due_date, stored.due_time) == (tomorrow, time(14, 0))
    assert stored.due_at == _at(tomorrow, 14)
    assert (task.due_date, task.due_time) == (tomorrow, time(9, 30))
    # The owner sees the time was heard: «ertaga 14:00», not just the date.
    text = replies.confirmation(applied)
    assert "(ertaga 14:00)" in text and "(ertaga 09:30)" in text


async def test_a_promise_claim_keeps_its_time_through_correction_and_answer(session):
    interaction = await _interaction(session)
    applied = await apply_extraction(
        session,
        interaction,
        ex.ExtractionResult(
            promises=[
                ex.ExtractedPromise(
                    made_by="me",
                    person="Akmal",
                    description="hujjat yuborish",
                    due_date="2026-10-23",
                    asserted_by="them",
                )
            ]
        ),
    )
    [claim] = applied.claims
    edit = records.parse_edit("2026-10-23 14:00")
    await claims.edit(session, claim.id, edit, by=claims.BY_COMMAND)
    assert claim.payload["due_date"] == "2026-10-23T14:00"
    view = claims.view(claim)
    assert (view.due, view.due_time) == (date(2026, 10, 23), time(14, 0))
    assert f"({f.short_due(view.due, view.due_time)})" in f.claim_line(view)
    assert "14:00" in f.claim_line(view)

    await claims.accept(session, claim.id, by=claims.BY_COMMAND)
    promise = await session.scalar(sa.select(m.Promise))
    assert (promise.due_date, promise.due_time) == (date(2026, 10, 23), time(14, 0))


async def test_a_debt_claim_refuses_a_time_of_day(session):
    interaction = await _interaction(session)
    applied = await apply_extraction(
        session,
        interaction,
        ex.ExtractionResult(
            debts=[
                ex.ExtractedDebt(
                    direction="i_owe_them",
                    person="Akmal",
                    amount=5_000_000,
                    due_date="2026-10-23",
                    asserted_by="them",
                )
            ]
        ),
    )
    [claim] = applied.claims
    with pytest.raises(ValueError):
        await claims.edit(
            session, claim.id, records.parse_edit("ertaga 14:00"), by=claims.BY_COMMAND
        )
    assert claim.payload["due_date"] == "2026-10-23"


async def test_a_promise_claim_refuses_midnight_rather_than_dropping_it(session):
    interaction = await _interaction(session)
    applied = await apply_extraction(
        session,
        interaction,
        ex.ExtractionResult(
            promises=[
                ex.ExtractedPromise(
                    made_by="me",
                    person="Akmal",
                    description="hujjat yuborish",
                    due_date="2026-10-23",
                    asserted_by="them",
                )
            ]
        ),
    )
    [claim] = applied.claims
    edit = records.parse_edit("2026-10-24 00:00")
    assert edit.value == datetime(2026, 10, 24, 0, 0)
    with pytest.raises(ValueError):
        await claims.edit(session, claim.id, edit, by=claims.BY_COMMAND)
    assert claim.payload["due_date"] == "2026-10-23"


# --- 2. date-only deadlines are unchanged -----------------------------------


async def test_date_only_unchanged(session):
    tomorrow = _now().date() + timedelta(days=1)
    interaction = await _interaction(session)
    applied = await apply_extraction(
        session,
        interaction,
        ex.ExtractionResult(
            promises=[
                ex.ExtractedPromise(
                    made_by="me",
                    person="Akmal",
                    description="hujjat yuboraman",
                    due_date=tomorrow.isoformat(),
                )
            ]
        ),
    )
    [promise] = applied.promises
    assert (promise.due_date, promise.due_time, promise.due_at) == (tomorrow, None, None)
    # Rendered exactly as before JV-03.
    assert f.record_due(promise) == f.relative_day(tomorrow)
    assert f.record_short_due(promise) == f.short_date(tomorrow)
    assert f" ({f.short_date(tomorrow)})" in replies.confirmation(applied)
    person = await resolve_person(session, "Akmal")
    line = replies.promises_report([(promise, person)])
    assert f"hujjat yuboraman — Akmal · {f.relative_day(tomorrow)}" in line
    assert "⏰" not in line
    # Reminded on the morning of the day, by the hourly sweep, as before.
    morning = _at(TODAY, 8)
    assert reminders.decide("promise", [], due=TODAY, created_at=None, now=morning) == (
        reminders.PING
    )


def test_date_only_labels_are_relative_day():
    for delta in (-3, -1, 0, 1, 2, 5, 10):
        day = TODAY + timedelta(days=delta)
        assert f.due_label(day, today=TODAY) == f.relative_day(day, today=TODAY)


async def test_due_time_requires_date_check(session):
    person = await resolve_person(session, "Akmal")
    session.add(
        m.Promise(
            made_by=PromiseMadeBy.me,
            person_id=person.id,
            description="vaqt bor, kun yo'q",
            due_time=time(14, 0),
        )
    )
    with pytest.raises(IntegrityError) as caught:
        await session.flush()
    assert "ck_promises_due_time" in str(caught.value)
    await session.rollback()

    session.add(m.Task(description="vaqt bor, kun yo'q", due_time=time(14, 0)))
    with pytest.raises(IntegrityError) as caught:
        await session.flush()
    assert "ck_tasks_due_time" in str(caught.value)
    await session.rollback()


async def test_due_at_sql_matches_python(session):
    due = _at(TODAY, 14, 30)
    _, promise = await _timed_promise(session, due_at=due)
    _, undated = await _timed_promise(session, "muddatsiz")
    task = m.Task(description="t", due_date=TODAY)
    session.add(task)
    await session.flush()
    rows = dict((await session.execute(sa.select(m.Promise.id, m.Promise.due_at))).all())
    assert rows[promise.id] == due == promise.due_at
    assert rows[undated.id] is None
    assert await session.scalar(sa.select(m.Task.due_at)) is None


# --- 3. /tuzat accepts a time ------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ertaga 14:00", datetime(2026, 10, 23, 14, 0)),
        ("ertaga soat 3", datetime(2026, 10, 23, 15, 0)),
        ("juma kechki 6", datetime(2026, 10, 23, 18, 0)),
        ("dushanba 10:00", datetime(2026, 10, 26, 10, 0)),
        ("2026-10-25 09:30", datetime(2026, 10, 25, 9, 30)),
        ("2026-10-25T09:30", datetime(2026, 10, 25, 9, 30)),
        ("3 kun 14:00 gacha", datetime(2026, 10, 25, 14, 0)),
        # Unchanged forms.
        ("ertaga", date(2026, 10, 23)),
        ("2026-10-25", date(2026, 10, 25)),
        ("muddatsiz", None),
        # Not a date: a time with two readings, a bare number, junk, no day.
        ("ertaga soat 7", False),
        ("ertaga 5", False),
        ("ertaga 14:00 Sardor", False),
        ("14:00", False),
        ("muddatsiz 14:00", False),
        ("2026-10-25 25:00", False),
        # The ISO day reads its clock the way a day word does.
        ("2026-10-25 3:00", datetime(2026, 10, 25, 15, 0)),
        ("2026-10-25 03:00", datetime(2026, 10, 25, 3, 0)),
        ("2026-10-25 7:00", False),
        # A night hour after midnight with a day: that day's or the next night's.
        ("bugun kechasi 2", False),
        ("bugun kechasi 12", False),
        ("ertaga kechasi 1", False),
        ("ertaga kechasi 11", datetime(2026, 10, 23, 23, 0)),
    ],
)
def test_parse_date_with_time(text, expected):
    assert records.parse_date(text, today=TODAY) == expected


def test_parse_edit_reads_a_timed_due_and_leaves_amounts_alone():
    edit = records.parse_edit("ertaga 14:00", today=TODAY)
    assert edit == records.Edit("due", datetime(2026, 10, 23, 14, 0))
    assert records.parse_edit("muddat ertaga 14:00", today=TODAY) == edit
    assert records.parse_edit("6 mln", today=TODAY).field == "amount"


async def test_set_field_writes_the_time_and_its_history(session):
    _, promise = await _timed_promise(session, due_at=None)
    tomorrow = _now().date() + timedelta(days=1)
    at = datetime.combine(tomorrow, time(14, 0))

    change = await records.set_field(session, promise, "due", at, by=records.BY_COMMAND)

    assert (promise.due_date, promise.due_time) == (tomorrow, time(14, 0))
    assert change.field == "due" and change.new == tomorrow.isoformat()
    due_entry, time_entry = promise.history
    assert due_entry["field"] == "due" and due_entry["old"] is None
    assert time_entry["field"] == "due_time"
    assert (time_entry["old"], time_entry["new"], time_entry["by"]) == (
        None,
        "14:00",
        "command",
    )
    assert "ertaga 14:00" in replies.record_edited(change)

    # A new time on the same day is its own history entry.
    await records.set_field(
        session, promise, "due", at.replace(hour=16), by=records.BY_BUTTON
    )
    assert promise.history[-1]["field"] == "due_time"
    assert (promise.history[-1]["old"], promise.history[-1]["new"]) == ("14:00", "16:00")

    # A day alone is the whole new deadline: the time goes, and says so.
    await records.set_field(session, promise, "due", tomorrow, by=records.BY_COMMAND)
    assert promise.due_time is None
    assert promise.history[-1] | {"at": None} == {
        "at": None,
        "field": "due_time",
        "old": "16:00",
        "new": None,
        "by": "command",
    }

    # muddatsiz clears both, so the CHECK holds.
    await records.set_field(session, promise, "due", at, by=records.BY_COMMAND)
    await records.set_field(session, promise, "due", None, by=records.BY_COMMAND)
    await session.flush()
    assert (promise.due_date, promise.due_time) == (None, None)


async def test_a_date_only_correction_writes_no_time_entry(session):
    _, promise = await _timed_promise(session, due_at=None)
    await records.set_field(session, promise, "due", TODAY, by=records.BY_COMMAND)
    assert [e["field"] for e in promise.history] == ["due"]


async def test_cmd_tuzat_with_a_time(bound):  # noqa: F811
    _, promise = await _timed_promise(bound)
    message = _Message()

    await handlers.cmd_edit(message, _command("tuzat", f"p{promise.id} ertaga 14:00"))

    [(text, _)] = message.sent
    assert "Tuzatildi" in text and "ertaga 14:00" in text
    fresh = await _reload(bound, "promise", promise.id)
    tomorrow = _now().date() + timedelta(days=1)
    assert (fresh.due_date, fresh.due_time) == (tomorrow, time(14, 0))


async def test_a_debt_deadline_refuses_a_time(session):
    person = await resolve_person(session, "Akmal")
    debt = m.Debt(
        direction=DebtDirection.they_owe_me,
        person_id=person.id,
        amount=Decimal("5000000"),
        currency=Currency.UZS,
        due_date=TODAY,
    )
    session.add(debt)
    await session.flush()
    with pytest.raises(records.NotEditable):
        await records.set_field(
            session, debt, "due", datetime(2026, 10, 23, 14, 0), by=records.BY_COMMAND
        )
    assert debt.due_date == TODAY and debt.history in (None, [])


async def test_a_money_row_takes_the_day_and_the_time(session):
    txn = m.Transaction(
        type=TransactionType.expense,
        amount=Decimal("300"),
        currency=Currency.USD,
        occurred_at=_at(TODAY, 9),
    )
    session.add(txn)
    await session.flush()
    await records.set_field(
        session, txn, "due", datetime(2026, 10, 21, 14, 30), by=records.BY_COMMAND
    )
    assert txn.occurred_at == _at(date(2026, 10, 21), 14, 30)
    # A day alone keeps the clock time, as before.
    await records.set_field(session, txn, "due", date(2026, 10, 20), by="command")
    assert txn.occurred_at == _at(date(2026, 10, 20), 14, 30)


def test_the_tuzat_hint_teaches_a_time():
    assert "ertaga 14:00" in replies.tuzat_hint("promise", "p1")
    assert "ertaga 14:00" in replies.tuzat_hint("task", "t1")
    assert "ertaga 14:00" not in replies.tuzat_hint("debt", "d1")
    assert "ertaga 14:00" in replies.TUZAT_USAGE


# --- 4. rendering ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (TODAY, "bugun 14:00"),
        (TODAY + timedelta(days=1), "ertaga 14:00"),
        (TODAY + timedelta(days=2), "indinga 14:00"),
        (date(2026, 10, 26), "dushanba 14:00"),
        (TODAY - timedelta(days=1), "kecha 14:00"),
        (TODAY - timedelta(days=3), "3 kun kechikdi"),
        (date(2026, 11, 5), "5-noy 14:00"),
    ],
)
def test_due_label_renders_the_time(day, expected):
    assert f.due_label(day, time(14, 0), today=TODAY) == expected


async def test_lists_show_the_timed_line(session):
    due = _now().replace(second=0, microsecond=0) + timedelta(days=1)
    person, timed = await _timed_promise(session, due_at=due)
    _, plain = await _timed_promise(session, "hujjat", due_at=None)
    plain.due_date = due.date()
    await session.flush()
    label = f.record_due(timed)
    expected = f"⏰ {label} — Akmal: hisob-kitob <code>p{timed.id}</code>"

    report = replies.promises_report([(timed, person), (plain, person)])
    assert expected in report
    assert f"hujjat — Akmal · {f.relative_day(due.date())}" in report

    body, _ = replies.reminder_with_counts([], [(timed, person)], [], [])
    assert expected in body
    assert f.record_line("promise", timed, person).endswith(f"· {label}")


def test_timed_reminder_lines():
    person = m.Person(id=1, display_name="Akmal", aliases=[])
    due = date(2026, 10, 22)
    mine = m.Promise(
        id=7,
        made_by=PromiseMadeBy.me,
        person_id=1,
        description="hisob-kitob",
        due_date=due,
        due_time=time(14, 0),
    )
    theirs = m.Promise(
        id=8,
        made_by=PromiseMadeBy.them,
        person_id=1,
        description="pul yuboradi",
        due_date=due,
        due_time=time(14, 0),
    )
    task = m.Task(
        id=9, description="hujjat topshirish", due_date=due, due_time=time(9, 0)
    )
    vaqt = f.record_due(mine)
    assert replies.timed_reminder_line("promise", mine, person) == (
        f"⏰ {vaqt} da muddat: Akmalga va'da — hisob-kitob <code>p7</code>"
    )
    assert replies.timed_reminder_line("promise", theirs, person) == (
        f"⏰ {vaqt} da muddat: Akmalning va'dasi — pul yuboradi <code>p8</code>"
    )
    assert replies.timed_reminder_line("task", task) == (
        f"⏰ {f.record_due(task)} da muddat: hujjat topshirish <code>t9</code>"
    )


# --- 5. the reminder before the time --------------------------------------------


def test_timed_reminder_fires_before_due(monkeypatch):
    monkeypatch.setattr(settings, "promise_remind_lead_minutes", 60)
    due = TODAY
    at = time(14, 0)

    def decide(now, history=()):
        return reminders.decide(
            "promise", list(history), due=due, created_at=None, now=now, due_time=at
        )

    # Not on the morning of the day the way a date-only item is.
    assert decide(_at(TODAY, 8)) is None
    assert decide(_at(TODAY, 12, 59)) is None
    assert decide(_at(TODAY, 13, 0)) == reminders.LEAD
    assert decide(_at(TODAY, 13, 55)) == reminders.LEAD
    # Sent once: the next is the date-only ping a day later.
    sent = [("promise", _at(TODAY, 13, 5))]
    assert decide(_at(TODAY, 13, 30), sent) is None
    assert decide(_at(TODAY + timedelta(days=1), 8), sent) == reminders.PING
    # Missed (downtime): after the deadline the ordinary ping takes over.
    assert decide(_at(TODAY, 15)) == reminders.PING
    # Moved later the same day: a new lead before the new time.
    later = reminders.decide(
        "promise", sent, due=due, created_at=None, now=_at(TODAY, 17), due_time=time(18)
    )
    assert later == reminders.LEAD


def test_the_lead_follows_the_setting(monkeypatch):
    monkeypatch.setattr(settings, "promise_remind_lead_minutes", 15)
    assert reminders.timed_fire_at(_at(TODAY, 14)) == _at(TODAY, 13, 45)


def test_the_lead_is_never_shorter_than_the_sweep():
    # The timed sweep runs every five minutes: a shorter lead would fall
    # between two runs and the deadline would pass before it was sent.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, promise_remind_lead_minutes=4)
    assert Settings(_env_file=None, promise_remind_lead_minutes=5)


def test_a_lead_never_falls_inside_quiet_hours(monkeypatch):
    monkeypatch.setattr(settings, "promise_remind_lead_minutes", 60)
    monkeypatch.setattr(settings, "quiet_hours", "23:30-07:30")
    # 08:00 → 07:00 is quiet; the quiet hours end before the deadline.
    assert reminders.timed_fire_at(_at(TODAY, 8)) == _at(TODAY, 7, 30)
    # 01:00 is itself quiet: the lead moves before the quiet hours begin.
    assert reminders.timed_fire_at(_at(TODAY, 1)) == _at(
        TODAY - timedelta(days=1), 22, 30
    )
    # 07:30 exactly: the end is not before the deadline either.
    assert reminders.timed_fire_at(_at(TODAY, 7, 30)) == _at(
        TODAY - timedelta(days=1), 22, 30
    )
    # Outside quiet hours nothing moves.
    assert reminders.timed_fire_at(_at(TODAY, 14)) == _at(TODAY, 13)


async def test_the_timed_sweep_sends_once_and_the_hourly_one_never(session, monkeypatch):
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: False)
    monkeypatch.setattr(settings, "promise_remind_lead_minutes", 60)

    class _Bot:
        def __init__(self) -> None:
            self.sent: list[tuple[str, object]] = []

        async def send_message(self, chat_id, text, **kwargs):
            self.sent.append((text, kwargs.get("reply_markup")))

    soon = _now().replace(second=0, microsecond=0) + timedelta(minutes=30)
    later = soon + timedelta(hours=3)
    _, promise = await _timed_promise(session, due_at=soon)
    _, far = await _timed_promise(session, "keyinroq", due_at=later)
    task = m.Task(
        description="hujjat topshirish",
        due_date=soon.date(),
        due_time=soon.timetz().replace(tzinfo=None),
    )
    session.add(task)
    await session.commit()

    hourly = await reminders.collect_due(session)
    assert promise.id not in [p.id for p, _ in hourly.promises]
    assert task.id not in [t.id for t in hourly.tasks]

    bot = _Bot()
    await worker.timed_reminder_job(bot)

    [(text, markup)] = bot.sent
    vaqt = f.record_due(promise)
    assert f"⏰ {vaqt} da muddat: Akmalga va'da — hisob-kitob" in text
    assert "hujjat topshirish" in text and "keyinroq" not in text
    assert _payloads(markup)[0][0] == f"rec:d:p{promise.id}"

    bot.sent.clear()
    await worker.timed_reminder_job(bot)
    assert bot.sent == []
    hourly = await reminders.collect_due(session)
    assert promise.id not in [p.id for p, _ in hourly.promises]
    assert far.id not in [p.id for p, _ in hourly.promises]


async def test_one_bad_timed_item_never_stops_the_sweep(session, monkeypatch):
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: False)
    monkeypatch.setattr(settings, "promise_remind_lead_minutes", 60)

    class _Bot:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_message(self, chat_id, text, **kwargs):
            self.sent.append(text)

    soon = _now().replace(second=0, microsecond=0) + timedelta(minutes=30)
    _, good = await _timed_promise(session, "yaxshi", due_at=soon)
    _, undecidable = await _timed_promise(session, "qaror yo'q", due_at=soon)
    _, unrenderable = await _timed_promise(session, "chiqmaydi", due_at=soon)
    await session.commit()

    history = reminders._history

    async def _history(session, kind, ref):
        if ref == str(undecidable.id):
            raise RuntimeError("broken row")
        return await history(session, kind, ref)

    line = replies.timed_reminder_line

    def _line(kind, record, person=None):
        if record.id == unrenderable.id:
            raise KeyError("broken template")
        return line(kind, record, person)

    monkeypatch.setattr(reminders, "_history", _history)
    monkeypatch.setattr(replies, "timed_reminder_line", _line)

    bot = _Bot()
    await worker.timed_reminder_job(bot)

    [text] = bot.sent
    assert "yaxshi" in text
    assert "qaror yo'q" not in text and "chiqmaydi" not in text
    logged = set(
        await session.scalars(
            sa.select(m.ReminderLog.ref).where(m.ReminderLog.kind == "promise")
        )
    )
    assert logged == {str(good.id)}


async def test_timed_due_items_soonest_first(session):
    base = _now().replace(second=0, microsecond=0) + timedelta(hours=1)
    _, second = await _timed_promise(
        session, "ikkinchi", due_at=base + timedelta(hours=2)
    )
    _, first = await _timed_promise(session, "birinchi", due_at=base)
    _, plain = await _timed_promise(session, "sanasi bor", due_at=None)
    plain.due_date = base.date()
    await session.flush()
    now = _now()
    found = await queries.timed_due_items(
        session, after=now, until=base + timedelta(hours=3)
    )
    assert [p.id for p, _ in found["promises"]] == [first.id, second.id]
    found = await queries.timed_due_items(
        session, after=now, until=base + timedelta(minutes=30)
    )
    assert [p.id for p, _ in found["promises"]] == [first.id]
    # A deadline already past is not a lead any more: the hourly sweep has it.
    found = await queries.timed_due_items(
        session, after=base + timedelta(minutes=1), until=base + timedelta(hours=3)
    )
    assert [p.id for p, _ in found["promises"]] == [second.id]


async def test_a_timed_deadline_sorts_before_a_date_only_one_that_day(session):
    day = _now().date() + timedelta(days=1)
    _, plain = await _timed_promise(session, "sanasi bor", due_at=None)
    plain.due_date = day
    _, timed = await _timed_promise(session, "vaqti bor", due_at=_at(day, 18))
    await session.flush()
    rows = await queries.open_promises(session)
    assert [p.id for p, _ in rows] == [timed.id, plain.id]


async def test_the_tools_and_the_api_carry_the_time(session):
    due = _now().replace(second=0, microsecond=0) + timedelta(hours=2)
    _, promise = await _timed_promise(session, due_at=due)
    await session.flush()
    output = json.loads(await rag._run_tool(session, None, "due_items", {}))
    [row] = output["promises"]
    assert row["due_date"] == due.date().isoformat()
    assert row["due_time"] == due.strftime("%H:%M")


async def test_the_api_and_the_model_inputs_carry_the_time(session):
    from miya.api import main as api_main
    from miya.services import planner, profiles

    due = _now().replace(second=0, microsecond=0) + timedelta(hours=2)
    _, promise = await _timed_promise(session, due_at=due)
    _, plain = await _timed_promise(session, "sanasi bor", due_at=None)
    plain.due_date = due.date()
    await session.flush()
    body = await api_main.list_promises(session)
    by_id = {row["id"]: row for row in body["promises"]}
    assert by_id[promise.id]["due_time"] == due.strftime("%H:%M")
    assert by_id[plain.id]["due_time"] is None
    stamp = f"{due.date()} {due:%H:%M}"
    assert planner._due_text(promise) == stamp
    assert planner._due_text(plain) == f"{due.date()}"
    assert profiles._at(promise) == f" {due:%H:%M}" and profiles._at(plain) == ""

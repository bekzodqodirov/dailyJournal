"""Build step 2, part B: the open loops, surfaced — and the two rhythms fixed.

The engine (test_open_loops.py) finds what slipped the owner's attention;
these cover what he actually sees: the morning brief at 09:00, the two new
report sections, the half-hourly nudge with its two buttons, the instant
extraction path for what is addressed to him, and the one-tap question for a
new group. Telegram and Anthropic are stubbed; the database is real.
"""

from __future__ import annotations

import ast
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from miya.bot import handlers, keyboards, replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import (
    ChatType,
    Currency,
    DebtDirection,
    Direction,
    EventSource,
    EventStatus,
    WindowStatus,
)
from miya.services import batch, brief, chats, nudges, reports, windows
from miya.services import extraction as ex
from miya.tools import backfill as backfill_tool
from miya.userbot import main as userbot
from miya.worker import main as worker
from tests.test_close_and_correct import (
    _Callback,
    _Message,
    bound,  # noqa: F401 — fixture
)
from tests.test_open_loops import (
    _ago,
    _debt,
    _monitor,
    _msg,
    _now,
    _person,
    _promise,
)
from tests.test_userbot import HISTORY_CALLS, _calls_by_function

TZ = settings.tz
PRIVATE = 1001
GROUP = -1002


class _Bot:
    """Records what the owner would have received, with its keyboard."""

    def __init__(self, *, reachable: bool = True) -> None:
        self.sent: list[tuple[str, object]] = []
        self.reachable = reachable

    async def send_message(self, chat_id, text, **kwargs) -> None:
        if not self.reachable:
            raise RuntimeError("telegram is down")
        self.sent.append((text, kwargs.get("reply_markup")))

    @property
    def texts(self) -> list[str]:
        return [text for text, _ in self.sent]


def _usage() -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=500,
        output_tokens=80,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )


def _debt_result() -> ex.ExtractionResult:
    return ex.ExtractionResult(
        summary="Akmal 5 mln oldi",
        debts=[
            ex.ExtractedDebt(
                direction="they_owe_me", person="Akmal", amount=5_000_000, reason="yuk"
            )
        ],
    )


async def _extract_ok(text, *, now=None):
    return ex.ExtractionOutcome(
        result=_debt_result(), model=settings.extract_model, usage=_usage()
    )


async def _extract_fails(text, *, now=None):
    return ex.ExtractionOutcome(error="boom", model=settings.extract_model)


def _no_model(*args, **kwargs):  # pragma: no cover - must never run
    raise AssertionError("a model was called where none may be")


def _buttons(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


# --- ages ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(minutes=25), "25 daqiqa"),
        (timedelta(hours=3, minutes=20), "3 soat"),
        (timedelta(hours=23), "23 soat"),
        (timedelta(days=2, hours=5), "2 kun"),
        (timedelta(seconds=-5), "0 daqiqa"),
    ],
)
def test_age_label(delta, expected):
    assert replies.age_label(delta) == expected


# --- 1. the morning brief ----------------------------------------------------


async def _seed_loops(session):
    """A meeting today, a debt due today, an old question, a stale undated
    promise, and a supplier who went quiet with money open."""
    akmal = await _person(session, "Akmal")
    sardor = await _person(session, "Sardor")
    await _monitor(session)
    session.add(
        m.Event(
            title="Bojxona uchrashuvi",
            start_at=_now().replace(hour=15, minute=0, second=0, microsecond=0),
            location="Toshkent",
            source=EventSource.extracted,
            status=EventStatus.planned,
        )
    )
    due = await _debt(session, akmal, "5000000", due_date=_now().date())
    asked = await _msg(
        session, "konteyner qachon keladi?", at=_ago(hours=6), person=akmal
    )
    stale = await _promise(session, akmal, "invoice yuboradi", created_at=_ago(days=9))
    quiet_debt = await _debt(
        session,
        sardor,
        "1200",
        currency=Currency.USD,
        direction=DebtDirection.i_owe_them,
        created_at=_ago(days=40),
    )
    await session.flush()
    return SimpleNamespace(
        akmal=akmal, sardor=sardor, due=due, asked=asked, stale=stale, quiet=quiet_debt
    )


async def test_the_brief_says_everything_in_order_with_refs_and_ages(session):
    seed = await _seed_loops(session)

    data = await brief.gather(session)
    body = replies.morning_brief(data)

    # Meetings, then what is due, then the three kinds of open loop.
    sections = [
        replies.BRIEF_EVENTS,
        replies.BRIEF_DUE,
        replies.BRIEF_QUESTIONS,
        replies.BRIEF_STALE,
        replies.BRIEF_QUIET,
    ]
    positions = [body.index(label) for label in sections]
    assert positions == sorted(positions)

    assert "15:00 — Bojxona uchrashuvi (Toshkent)" in body
    assert f"Akmal: 5 mln so'm · bugun <code>d{seed.due.id}</code>" in body
    assert "<b>Akmal</b> · 6 soat: «konteyner qachon keladi?»" in body
    assert f"<code>p{seed.stale.id}</code> U — Akmal: invoice yuboradi" in body
    # The stale line says how long the row has been untouched.
    [stale_line] = [ln for ln in body.splitlines() if f"p{seed.stale.id}" in ln]
    assert "9 kun" in stale_line
    assert "<b>Sardor</b> · 40 kun jim: ← sen $1200" in body
    assert f"<code>d{seed.quiet.id}</code>" in body


async def test_the_briefs_buttons_are_the_reminders_rows(session):
    seed = await _seed_loops(session)
    data = await brief.gather(session)

    due, stale = replies.morning_brief_refs(data)
    markup = keyboards.brief_actions(due, stale)

    assert due == [("debt", seed.due.id)]
    # Sardor's 40-day undated debt is stale too — oldest first, then the promise.
    assert stale == [("debt", seed.quiet.id), ("promise", seed.stale.id)]
    payloads = _buttons(markup)
    # The due debt gets ✅ / ✏️ / 🔄; the undated promise Ha / ✅ / Yop.
    assert f"rec:d:d{seed.due.id}" in payloads and f"rec:f:d{seed.due.id}" in payloads
    assert f"rec:o:p{seed.stale.id}" in payloads and f"rec:c:p{seed.stale.id}" in payloads
    # Labelled: a brief carries many rows.
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any(f"d{seed.due.id}" in label for label in labels)


def test_the_brief_keyboard_caps_its_rows_and_drops_duplicates():
    due = [("task", i) for i in range(30)]
    markup = keyboards.brief_actions(due, [("task", 0)])
    assert len(markup.inline_keyboard) == keyboards.MAX_ROWS
    assert keyboards.brief_actions([], []) is None


async def test_an_empty_brief_says_all_clear(session):
    data = await brief.gather(session)
    assert data.is_empty()
    assert replies.BRIEF_ALL_CLEAR in replies.morning_brief(data)
    assert keyboards.brief_actions(*replies.morning_brief_refs(data)) is None


async def test_the_brief_job_sends_one_message_without_a_model_even_at_dawn(
    session, monkeypatch
):
    """Deterministic: Anthropic down and quiet hours on change nothing."""
    await _seed_loops(session)
    await session.commit()  # the job opens its own transaction
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: True)
    monkeypatch.setattr(ex, "get_client", _no_model)
    bot = _Bot()

    await worker.brief_job(bot)

    [(text, markup)] = bot.sent
    assert replies.BRIEF_HEADER in text
    assert "Javobsiz qolganlar" in text
    assert markup is not None and _buttons(markup)


async def test_ertalab_is_the_brief_on_demand(bound):  # noqa: F811
    await _seed_loops(bound)
    message = _Message()

    await handlers.cmd_brief(message)

    [(text, markup)] = message.sent
    assert replies.BRIEF_HEADER in text and "Jim bo'lib qolganlar" in text
    assert markup is not None


async def test_a_sent_brief_is_logged_and_a_missed_one_is_caught_up(session, monkeypatch):
    """The jobstore is in memory: a restart spanning 09:00 must not cost
    the brief. A delivered brief is logged by day; catch_up sends a missing
    one once today's brief time has passed, and never twice."""
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: False)
    monkeypatch.setattr(settings, "backup_age_recipient", "")
    monkeypatch.setattr(worker, "_missed_report_day", _none)
    today = _now().date()
    before = datetime.combine(today, settings.morning_brief_time_parsed, tzinfo=TZ)

    assert await worker._brief_is_missing(before - timedelta(minutes=1)) is False
    assert await worker._brief_is_missing(before + timedelta(minutes=1)) is True

    bot = _Bot()
    await worker.brief_job(bot)
    assert len(bot.sent) == 1
    log = await session.scalar(
        sa.select(m.ReminderLog).where(m.ReminderLog.kind == worker.BRIEF_KIND)
    )
    assert log.ref == today.isoformat()
    assert await worker._brief_is_missing(before + timedelta(hours=5)) is False

    # A failed send is not logged, so the next chance sends it again.
    await session.execute(sa.delete(m.ReminderLog))
    await session.commit()
    assert await worker.brief_job(_Bot(reachable=False)) is False
    assert await worker._brief_is_missing(before + timedelta(hours=5)) is True

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return before + timedelta(hours=2)

    monkeypatch.setattr(worker, "datetime", _Clock)
    bot = _Bot()
    await worker.catch_up(bot)
    await worker.catch_up(bot)
    assert len(bot.sent) == 1 and replies.BRIEF_HEADER in bot.texts[0]


async def _none(now):
    return None


def test_the_brief_is_in_help_and_scheduled_at_the_owners_time():
    assert "/ertalab" in replies.HELP
    assert settings.morning_brief_time == "09:00"
    assert settings.morning_brief_time_parsed.hour == 9
    assert "MORNING_BRIEF_TIME=09:00" in Path(".env.example").read_text()


# --- 2. the evening report ---------------------------------------------------


async def test_the_report_gains_the_two_sections_with_ages(session):
    seed = await _seed_loops(session)

    data = await reports.gather(session, _now().date())
    block = reports.render_data_block(data)

    unanswered = block[block.index("JAVOBSIZ QOLGANLAR") : block.index("JIM BO'LIB")]
    assert "Akmal, 6 soat javobsiz: «konteyner qachon keladi?»" in unanswered
    quiet = block[block.index("JIM BO'LIB QOLGANLAR") : block.index("ERTAGA")]
    assert "Sardor: 40 kun jim — $1200 (qarzingiz)" in quiet
    assert [q.interaction_id for q in data.questions] == [seed.asked.id]
    assert reports._stats_json(data)["unanswered"] == 1
    assert reports._stats_json(data)["quiet"] == 1


async def test_the_report_sections_say_none_and_the_prompt_lists_them(session):
    data = await reports.gather(session, _now().date())
    block = reports.render_data_block(data)
    assert "JAVOBSIZ QOLGANLAR:\n- yo'q" in block
    assert "JIM BO'LIB QOLGANLAR:\n- yo'q" in block
    assert reports._stats_json(data)["unanswered"] == 0
    prompt = reports.REPORT_SYSTEM_PROMPT
    assert "❓ Javobsiz qolganlar" in prompt and "🤫 Jim bo'lib qolganlar" in prompt
    assert prompt.index("Sizga murojaatlar") < prompt.index("Javobsiz qolganlar")
    assert prompt.index("Jim bo'lib qolganlar") < prompt.index("📅 Ertaga")


# --- 3. nudges ------------------------------------------------------------------


async def test_a_question_is_nudged_once_and_then_the_brief_carries_it(session):
    """One nudge per question: after it the morning brief lists the question
    until it is answered. "Ertaga" is the one exception — once the snooze
    runs out the question is nudged once more, and then never again."""
    akmal = await _person(session)
    await _monitor(session)
    asked = await _msg(session, "narxi qancha?", at=_ago(hours=6), person=akmal)

    [due] = await nudges.collect(session)
    assert due.interaction_id == asked.id

    nudges.mark_nudged(session, [due])
    await session.flush()
    assert await nudges.collect(session) == []

    # Days later it is still not nudged again; the brief carries it now.
    log = await session.scalar(sa.select(m.ReminderLog))
    assert log.kind == nudges.NUDGE_KIND and log.ref == str(asked.id)
    log.sent_at = _ago(days=3)
    await session.flush()
    assert await nudges.collect(session) == []
    assert [q.interaction_id for q in (await brief.gather(session)).loops.questions] == [
        asked.id
    ]

    # A snooze that has run out: the nudge before it is spent, so one more
    # goes out — and the one after it is the last.
    nudges.snooze(asked, until=_ago(hours=1))
    log.sent_at = _ago(hours=2)
    await session.flush()
    assert [q.interaction_id for q in await nudges.collect(session)] == [asked.id]
    log.sent_at = _ago(minutes=30)
    await session.flush()
    assert await nudges.collect(session) == []


async def test_javob_berdim_is_final_and_the_next_question_surfaces(session):
    akmal = await _person(session)
    await _monitor(session)
    first = await _msg(session, "narxi qancha?", at=_ago(hours=9), person=akmal)
    second = await _msg(session, "qachon yuborasiz?", at=_ago(hours=7), person=akmal)

    [loop] = await nudges.unanswered_questions(session)
    assert loop.interaction_id == first.id and loop.follow_ups == 1

    nudges.mark_answered(first)
    await session.flush()

    [loop] = await nudges.unanswered_questions(session)
    assert loop.interaction_id == second.id and loop.follow_ups == 0
    # And everywhere else at once: the brief, the report, the sweep.
    assert [q.interaction_id for q in (await brief.gather(session)).loops.questions] == [
        second.id
    ]
    data = await reports.gather(session, _now().date())
    assert [q.interaction_id for q in data.questions] == [second.id]
    assert [q.interaction_id for q in await nudges.collect(session)] == [second.id]


async def test_ertaga_snoozes_the_nudge_until_the_next_brief(session):
    akmal = await _person(session)
    await _monitor(session)
    asked = await _msg(session, "narxi qancha?", at=_ago(hours=6), person=akmal)
    now = _now()
    until = nudges.next_morning(now)
    # The next 09:00 after now: today's from the small hours (a tap at 00:30
    # means "at nine", not the day after tomorrow), tomorrow's otherwise.
    assert now < until <= now + timedelta(days=1)
    assert (until.hour, until.minute) == (9, 0)
    small_hours = datetime.combine(now.date(), time(0, 30), tzinfo=TZ)
    assert nudges.next_morning(small_hours).date() == now.date()
    mid_morning = datetime.combine(now.date(), time(10, 0), tzinfo=TZ)
    assert nudges.next_morning(mid_morning).date() == now.date() + timedelta(days=1)

    nudges.snooze(asked, until=until)
    await session.flush()

    assert await nudges.collect(session) == []
    # Snoozed, not answered: it still counts as open everywhere.
    assert len(await nudges.unanswered_questions(session)) == 1
    assert len(await nudges.collect(session, now=until + timedelta(minutes=1))) == 1


async def test_the_nudge_job_sends_one_per_question_capped_with_a_summary(
    session, monkeypatch
):
    await _monitor(session)
    people = [await _person(session, f"Odam {i}") for i in range(7)]
    for i, person in enumerate(people):
        await _monitor(session, 2000 + i)
        await _msg(
            session,
            f"savol {i} qachon?",
            at=_ago(hours=10 - i),
            chat_id=2000 + i,
            person=person,
        )
    await session.commit()
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: False)
    bot = _Bot()

    await worker.nudge_job(bot)

    assert len(bot.sent) == nudges.MAX_PER_SWEEP + 1
    first, markup = bot.sent[0]
    assert replies.NUDGE_HEADER in first
    assert "<b>Odam 0</b> · 10 soat oldin" in first and "«savol 0 qachon?»" in first
    assert [p[:7] for p in _buttons(markup)] == ["rec:qa:", "rec:qs:"]
    assert "yana 2 ta javobsiz savol" in bot.sent[-1][0]
    # Only the five that went out are logged; the two come next sweep.
    logged = await session.scalar(
        sa.select(sa.func.count())
        .select_from(m.ReminderLog)
        .where(m.ReminderLog.kind == nudges.NUDGE_KIND)
    )
    assert logged == nudges.MAX_PER_SWEEP
    bot.sent.clear()
    await worker.nudge_job(bot)
    assert len(bot.sent) == 2


async def test_quiet_hours_hold_the_nudge_and_never_drop_it(session, monkeypatch):
    akmal = await _person(session)
    await _monitor(session)
    await _msg(session, "narxi qancha?", at=_ago(hours=6), person=akmal)
    await session.commit()
    quiet = True
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: quiet)
    bot = _Bot()

    await worker.nudge_job(bot)
    assert bot.sent == []

    quiet = False
    await worker.nudge_job(bot)
    assert len(bot.sent) == 1


async def test_an_unreachable_owner_logs_nothing(session, monkeypatch):
    akmal = await _person(session)
    await _monitor(session)
    await _msg(session, "narxi qancha?", at=_ago(hours=6), person=akmal)
    await session.commit()
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: False)

    await worker.nudge_job(_Bot(reachable=False))

    assert (
        await session.scalar(sa.select(sa.func.count()).select_from(m.ReminderLog)) == 0
    )


async def test_the_two_nudge_buttons_through_the_handler(bound):  # noqa: F811
    akmal = await _person(bound)
    await _monitor(bound)
    asked = await _msg(bound, "narxi qancha?", at=_ago(hours=6), person=akmal)

    class _Editable(_Message):
        async def edit_text(self, text, reply_markup=None):
            self.sent.append((text, reply_markup))

    message = _Editable()
    await handlers.on_record_button(_Callback(f"rec:qs:q{asked.id}", message))
    until = nudges.snoozed_until(asked)
    assert until is not None
    assert message.sent[-1] == (replies.nudge_snoozed(until), None)
    assert not nudges.is_answered(asked)

    await handlers.on_record_button(_Callback(f"rec:qa:q{asked.id}", message))
    assert message.sent[-1] == (replies.NUDGE_ANSWERED, None)
    assert nudges.is_answered(asked)
    assert (asked.meta or {})[nudges.ANSWERED_KEY]["by"] == "button"

    await handlers.on_record_button(_Callback("rec:qa:q999999", message))
    assert message.sent[-1] == (replies.NUDGE_GONE, None)


def test_nudge_payloads_fit_telegrams_limit_and_never_read_as_records():
    markup = keyboards.nudge_actions(2_000_000_000)
    assert all(len(p.encode()) <= 64 for p in _buttons(markup))
    assert keyboards.parse_question_ref("q12") == 12
    assert keyboards.parse_question_ref("d12") is None
    assert keyboards.parse_question_ref("q") is None


# --- 4. the instant path -----------------------------------------------------


async def _raw(session, text, *, at, chat_id, to_me=False, out=False, person=None):
    return await _msg(
        session,
        text,
        at=at,
        chat_id=chat_id,
        person=person,
        to_me=to_me,
        direction=Direction.out if out else Direction.in_,
    )


def test_what_counts_as_addressed():
    to_me = m.Interaction(meta={"to_me": True})
    plain = m.Interaction(meta={"tg_message_id": 1})
    assert windows.wants_instant([plain], ChatType.private)
    assert windows.wants_instant([plain, to_me], ChatType.group)
    assert not windows.wants_instant([plain], ChatType.group)
    assert not windows.wants_instant([plain], None)
    assert windows.idle_minutes_for(True) == settings.window_idle_minutes_addressed == 5
    assert windows.idle_minutes_for(False) == settings.window_idle_minutes == 30
    assert "WINDOW_IDLE_MINUTES_ADDRESSED=5" in Path(".env.example").read_text()


async def test_a_private_chat_closes_after_five_minutes_and_is_instant(session):
    akmal = await _person(session)
    await _monitor(session, PRIVATE, ChatType.private)
    await _raw(session, "5 mln oldim", at=_ago(minutes=6), chat_id=PRIVATE, person=akmal)

    [window] = await windows.flush_ready_windows(session)

    assert window.instant is True and window.tg_chat_id == PRIVATE


async def test_group_traffic_keeps_thirty_minutes_unless_it_is_addressed(session):
    akmal = await _person(session)
    await _monitor(session, GROUP, ChatType.group)
    await _raw(
        session, "ular gaplashyapti", at=_ago(minutes=6), chat_id=GROUP, person=akmal
    )
    assert await windows.flush_ready_windows(session) == []

    await _raw(
        session,
        "Bekzod aka, konteyner qachon?",
        at=_ago(minutes=5, seconds=30),
        chat_id=GROUP,
        person=akmal,
        to_me=True,
    )
    [window] = await windows.flush_ready_windows(session)
    assert window.instant is True and window.message_count == 2

    await _raw(session, "yana gap", at=_ago(minutes=31), chat_id=-3003, person=akmal)
    [slow] = await windows.flush_ready_windows(session)
    assert slow.instant is False


async def test_the_batch_never_takes_an_instant_window(session, monkeypatch):
    akmal = await _person(session)
    await _monitor(session, PRIVATE, ChatType.private)
    await _raw(session, "5 mln oldim", at=_ago(minutes=6), chat_id=PRIVATE, person=akmal)
    [window] = await windows.flush_ready_windows(session)

    assert await windows.pending_windows(session) == []
    assert [w.id for w in await windows.pending_windows(session, instant=True)] == [
        window.id
    ]
    monkeypatch.setattr(batch, "get_client", _no_model)
    assert await batch.submit_pending(session) is None
    assert window.status is WindowStatus.pending


async def test_instant_extraction_lands_rows_bills_its_own_path_and_parks_the_receipt(
    session, monkeypatch
):
    akmal = await _person(session)
    await _monitor(session, PRIVATE, ChatType.private)
    await _raw(session, "5 mln oldim", at=_ago(minutes=6), chat_id=PRIVATE, person=akmal)
    [window] = await windows.flush_ready_windows(session)
    monkeypatch.setattr(batch, "extract", _extract_ok)

    outcome = await batch.extract_instant(session)

    assert outcome.applied == 1
    assert window.status is WindowStatus.applied
    assert await session.scalar(sa.select(sa.func.count()).select_from(m.Debt)) == 1
    [usage] = list(await session.scalars(sa.select(m.UsageLog)))
    assert usage.operation == "extract_window_instant" and usage.batch is False
    assert "extract_window_instant" in replies.OPERATION_LABEL
    [queued] = await batch.pending_notices(session)
    assert "5 mln so'm" in queued.text and "Akmal" in queued.text


async def test_a_failing_instant_window_is_retried_then_handed_to_the_batch(
    session, monkeypatch
):
    akmal = await _person(session)
    await _monitor(session, PRIVATE, ChatType.private)
    await _raw(session, "5 mln oldim", at=_ago(minutes=6), chat_id=PRIVATE, person=akmal)
    [window] = await windows.flush_ready_windows(session)
    monkeypatch.setattr(batch, "extract", _extract_fails)

    for attempt in range(1, settings.batch_max_attempts):
        outcome = await batch.extract_instant(session)
        assert outcome.applied == 0 and outcome.retried == 0
        fresh = await session.get(m.ConversationWindow, window.id)
        assert fresh.attempts == attempt and fresh.instant is True
        assert fresh.status is WindowStatus.pending

    outcome = await batch.extract_instant(session)
    assert outcome.retried == 1
    fresh = await session.get(m.ConversationWindow, window.id)
    assert fresh.instant is False and fresh.status is WindowStatus.pending
    assert fresh.attempts == 0
    # Now the batch's queue, with the same ladder as everything else in it.
    assert [w.id for w in await windows.pending_windows(session)] == [window.id]


async def test_an_exception_while_applying_counts_as_an_attempt(session, monkeypatch):
    akmal = await _person(session)
    await _monitor(session, PRIVATE, ChatType.private)
    await _raw(session, "5 mln oldim", at=_ago(minutes=6), chat_id=PRIVATE, person=akmal)
    [window] = await windows.flush_ready_windows(session)
    await session.commit()

    async def _explode(text, *, now=None):
        raise RuntimeError("persistence bug")

    monkeypatch.setattr(batch, "extract", _explode)
    outcome = await batch.extract_instant(session)

    assert outcome.applied == 0
    fresh = await session.get(m.ConversationWindow, window.id)
    assert fresh.attempts == 1 and fresh.instant is True


async def test_the_window_job_extracts_and_tells_the_owner_on_one_tick(
    session, monkeypatch
):
    akmal = await _person(session)
    await _monitor(session, PRIVATE, ChatType.private)
    await _raw(session, "5 mln oldim", at=_ago(minutes=6), chat_id=PRIVATE, person=akmal)
    await session.commit()
    monkeypatch.setattr(batch, "extract", _extract_ok)
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: False)
    bot = _Bot()

    await worker.window_job(bot)

    [text] = bot.texts
    assert "Telegramdan yozib olindi" in text and "5 mln so'm" in text
    assert await batch.pending_notices(session) == []


# --- 5. new groups, one tap --------------------------------------------------


def _dialog(chat_id, chat_type, title):
    return chats.DialogInfo(tg_chat_id=chat_id, chat_type=chat_type, title=title)


async def _asked_group(session, chat_id, title):
    """A group the worker has asked about: the only row a Ha / Yo'q acts on."""
    monitor = await chats.ensure_monitor(session, _dialog(chat_id, ChatType.group, title))
    chats.mark_asked(monitor)
    await session.flush()
    return monitor


async def test_only_switched_off_groups_and_channels_are_asked_and_only_once(session):
    await chats.sync_dialogs(
        session,
        [
            _dialog(111, ChatType.private, "Akmal"),
            _dialog(-222, ChatType.group, "GZ logistika"),
            _dialog(-333, ChatType.channel, "Bojxona"),
        ],
    )
    on = await chats.ensure_monitor(session, _dialog(-444, ChatType.group, "Yoqilgan"))
    on.monitor_enabled = True
    await session.flush()

    waiting = await chats.awaiting_join_question(session)
    assert [w.tg_chat_id for w in waiting] == [-222, -333]

    chats.mark_asked(waiting[0])
    await session.flush()
    assert [w.tg_chat_id for w in await chats.awaiting_join_question(session)] == [-333]
    assert len(await chats.awaiting_join_question(session, limit=0)) == 0


async def test_ha_switches_on_and_queues_a_week_of_backfill(session):
    monitor = await _asked_group(session, -222, "GZ")

    accepted = await chats.accept_join(session, monitor.id)

    assert accepted.monitor_enabled is True
    assert accepted.asked_at is not None
    assert accepted.backfill_requested_at is not None
    assert chats.BACKFILL_DAYS == 7
    assert [p.id for p in await chats.pending_backfills(session)] == [monitor.id]

    chats.mark_backfilled(accepted)
    await session.flush()
    assert await chats.pending_backfills(session) == []
    # A second "Ha" (a repeated tap) does not queue a second backfill.
    await chats.accept_join(session, monitor.id)
    assert await chats.pending_backfills(session) == []


async def test_yoq_leaves_it_off_and_asked(session):
    monitor = await _asked_group(session, -222, "GZ")
    declined = await chats.decline_join(session, monitor.id)
    assert declined.monitor_enabled is False and declined.asked_at is not None
    assert await chats.awaiting_join_question(session) == []
    assert await chats.pending_backfills(session) == []
    assert await chats.accept_join(session, 999_999) is None
    assert await chats.decline_join(session, 999_999) is None


async def test_a_backfill_stops_after_three_failures_or_when_switched_off(session):
    monitor = await _asked_group(session, -222, "GZ")
    await chats.accept_join(session, monitor.id)
    for _ in range(chats.BACKFILL_MAX_ATTEMPTS):
        assert len(await chats.pending_backfills(session)) == 1
        chats.mark_backfill_failed(monitor)
        await session.flush()
    assert await chats.pending_backfills(session) == []
    assert monitor.monitor_enabled is True  # the group stays on regardless

    other = await _asked_group(session, -333, "Ish")
    await chats.accept_join(session, other.id)
    other.monitor_enabled = False  # /chats, between the tap and the sweep
    await session.flush()
    assert await chats.pending_backfills(session) == []


async def test_the_worker_asks_once_with_two_buttons(session, monkeypatch):
    await chats.sync_dialogs(session, [_dialog(-222, ChatType.group, "GZ <logistika>")])
    await session.commit()
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: False)
    bot = _Bot()

    await worker.new_chat_ask_job(bot)
    await worker.new_chat_ask_job(bot)

    [(text, markup)] = bot.sent
    assert "Yangi guruh" in text and "GZ &lt;logistika&gt;" in text and "o'qiymi?" in text
    monitor = await chats.get_monitor(session, -222)
    await session.refresh(monitor)
    assert _buttons(markup) == [f"ng:y:{monitor.id}", f"ng:n:{monitor.id}"]
    assert monitor.asked_at is not None and monitor.monitor_enabled is False


async def test_the_worker_does_not_ask_at_night(session, monkeypatch):
    await chats.sync_dialogs(session, [_dialog(-222, ChatType.group, "GZ")])
    await session.commit()
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: True)
    bot = _Bot()
    await worker.new_chat_ask_job(bot)
    assert bot.sent == []
    assert len(await chats.awaiting_join_question(session)) == 1


async def test_the_two_group_buttons_through_the_handler(bound):  # noqa: F811
    monitor = await _asked_group(bound, -222, "GZ")

    class _Editable(_Message):
        async def edit_text(self, text, reply_markup=None):
            self.sent.append((text, reply_markup))

    message = _Editable()
    await handlers.on_new_group_button(_Callback(f"ng:n:{monitor.id}", message))
    assert message.sent[-1][0] == replies.new_group_declined("GZ", -222)
    assert monitor.monitor_enabled is False and monitor.asked_at is not None

    await handlers.on_new_group_button(_Callback(f"ng:y:{monitor.id}", message))
    assert message.sent[-1][0] == replies.new_group_accepted("GZ", -222, 7)
    assert "7 kun" in message.sent[-1][0]
    assert monitor.monitor_enabled is True and monitor.backfill_requested_at is not None

    await handlers.on_new_group_button(_Callback("ng:y:999999", message))
    assert message.sent[-1][0] == replies.NEW_GROUP_GONE


async def test_the_userbot_sweep_performs_the_backfill_the_bot_recorded(
    session, monkeypatch
):
    monitor = await _asked_group(session, -222, "GZ")
    await chats.accept_join(session, monitor.id)
    failing = await _asked_group(session, -333, "Ish")
    await chats.accept_join(session, failing.id)
    await session.flush()

    @asynccontextmanager
    async def _scope():
        yield session
        await session.flush()

    monkeypatch.setattr(userbot, "session_scope", _scope)
    calls: list[tuple] = []

    async def _fake_backfill(client, chat, days, *, limit=2000):
        calls.append((chat, days))
        if chat == -333:
            raise RuntimeError("no access")
        return 12

    monkeypatch.setattr(backfill_tool, "backfill_chat", _fake_backfill)

    done = await userbot.fetch_backfills(client=object())

    assert done == 1
    assert calls == [(-222, 7), (-333, 7)]
    assert monitor.backfill_done_at is not None
    assert failing.backfill_done_at is None and failing.backfill_attempts == 1


def test_the_userbot_reads_backfills_only_through_the_tool():
    """The userbot package still contains no history call of its own: its
    sweep delegates to miya.tools.backfill, which is the reviewable place."""
    source = Path(userbot.__file__).read_text()
    calls = _calls_by_function(source)
    assert "backfill_chat" in calls["fetch_backfills"]
    assert not (calls["fetch_backfills"] & HISTORY_CALLS)
    tool = _calls_by_function(Path(backfill_tool.__file__).read_text())
    assert "iter_messages" in tool["backfill_chat"]
    tree = ast.parse(source)
    assert not any(
        isinstance(node, ast.ImportFrom) and (node.module or "").startswith("miya.tools")
        for node in tree.body
    ), "the tool is imported lazily, inside the sweep, or the import is circular"


async def test_backfill_chat_stops_at_the_horizon_and_reuses_live_ingestion(
    monkeypatch,
):
    now = datetime.now(TZ)
    messages = [
        SimpleNamespace(id=3, date=now - timedelta(days=1)),
        SimpleNamespace(id=2, date=now - timedelta(days=6)),
        SimpleNamespace(id=1, date=now - timedelta(days=9)),  # past the week
    ]

    class _Client:
        async def get_entity(self, target):
            assert target == -222
            return SimpleNamespace(title="GZ")

        async def iter_messages(self, entity, limit):
            for message in messages:
                yield message

    ingested = []

    async def _ingest(client, message):
        ingested.append(message.id)
        return message.id != 2  # one already stored

    monkeypatch.setattr(userbot, "ingest_message", _ingest)

    stored = await backfill_tool.backfill_chat(_Client(), -222, 7)

    assert stored == 1
    assert ingested == [3, 2]
    assert backfill_tool._as_target("-222") == -222
    assert backfill_tool._as_target("@akmal") == "@akmal"

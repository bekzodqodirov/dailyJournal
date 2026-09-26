"""Build step 2, verifier findings on the loops side: A1–A10.

Each test here failed on the code the two adversarial reviews looked at.
The engine's own coverage is test_open_loops.py; these pin the corrections:
a payment is contact, the question scan is bounded, the owner's own words
are not addressed to him, the brief cannot sit inside quiet hours, a
question is nudged once (plus once after a snooze), "Ertaga" at night means
this morning, the answered mark lives in the engine, apostrophes fold in one
place, the question detector's false positives, and one knob for the weekly
cadence.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from miya.config import Settings, settings
from miya.db import models as m
from miya.db.enums import ChatType, Currency
from miya.services import loops, nudges, queries, reminders, text
from miya.userbot import main as userbot
from tests.test_open_loops import (
    _ago,
    _debt,
    _monitor,
    _msg,
    _now,
    _person,
    _promise,
)

TZ = settings.tz
ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


def _at(hour: int, minute: int = 0) -> datetime:
    """Today, by the real clock, at the given time: the same day ``_ago`` uses."""
    return _now().replace(hour=hour, minute=minute, second=0, microsecond=0)


# --- A1: a debt payment is contact -----------------------------------------------


async def test_a_payment_typed_into_the_bot_resets_the_quiet_clock(session):
    """A settlement produces only a DebtPayment row — no Transaction, and the
    bot's interaction carries no person — so the payment itself must count."""
    akmal = await _person(session)
    debt = await _debt(session, akmal, "5000000", created_at=_ago(days=20))
    await session.flush()
    assert [q.person_name for q in await loops.quiet_counterparties(session)] == ["Akmal"]

    session.add(
        m.DebtPayment(
            debt_id=debt.id,
            amount=Decimal("1000000"),
            currency=Currency.UZS,
            paid_at=_ago(days=3),
        )
    )
    await session.flush()

    assert await loops.quiet_counterparties(session) == []
    # The clock is the payment's own time: quiet again once that is old.
    [quiet] = await loops.quiet_counterparties(session, days=2)
    assert quiet.days_quiet == 3


# --- A2: the question scan is bounded on both ends --------------------------------


async def test_a_question_older_than_the_bound_is_not_a_loop(session):
    akmal = await _person(session)
    await _monitor(session, 1001)
    await _monitor(session, 1003)
    await _msg(session, "narxi qancha?", at=_ago(days=40), chat_id=1001, person=akmal)
    fresh = await _msg(
        session, "konteyner qachon?", at=_ago(days=2), chat_id=1003, person=akmal
    )

    [loop] = await loops.unanswered_questions(session)
    assert loop.ref == f"q{fresh.id}"


async def test_the_bound_is_a_setting_read_when_the_scan_runs(session, monkeypatch):
    akmal = await _person(session)
    await _monitor(session)
    await _msg(session, "narxi qancha?", at=_ago(days=10), person=akmal)

    assert len(await loops.unanswered_questions(session)) == 1
    monkeypatch.setattr(settings, "loop_question_max_days", 5)
    assert await loops.unanswered_questions(session) == []


def test_both_selects_carry_the_lower_bound_so_the_indexes_can_drive_them():
    sql = str(
        loops.question_candidates(_now() - timedelta(hours=4)).compile(
            dialect=postgresql.dialect()
        )
    )
    # The candidate rows and the per-chat last-outgoing subquery, one each.
    assert sql.count("interactions.occurred_at >= ") == 2


def test_the_bound_setting_is_documented_validated_and_read_from_env(monkeypatch):
    assert settings.loop_question_max_days == 30
    assert "LOOP_QUESTION_MAX_DAYS=30" in ENV_EXAMPLE.read_text()
    monkeypatch.setenv("LOOP_QUESTION_MAX_DAYS", "12")
    assert Settings(_env_file=None).loop_question_max_days == 12
    with pytest.raises(ValidationError):
        Settings(_env_file=None, loop_question_max_days=0)


# --- A3: the owner's own messages are never addressed to him ----------------------


def _message(text, *, out=False, mentioned=False):
    return SimpleNamespace(id=1, message=text, mentioned=mentioned, out=out)


def test_the_owner_writing_his_own_name_or_company_is_not_to_me(monkeypatch):
    monkeypatch.setattr(settings, "owner_aliases", "Bekzod, GSR Logistics")
    monitor = SimpleNamespace(chat_type=ChatType.group)

    theirs = _message("GSR Logistics ga yozing")
    assert userbot._message_meta(theirs, monitor) == {"tg_message_id": 1, "to_me": True}

    his = _message("GSR Logistics ga yozing", out=True)
    assert userbot._message_meta(his, monitor) == {"tg_message_id": 1}
    assert not userbot.addressed_to_owner(
        _message("Bekzod: ha", out=True), ChatType.group
    )
    # Not even a reply flag on his own row makes it aimed at him.
    assert not userbot.addressed_to_owner(
        _message("ha", out=True, mentioned=True), ChatType.group
    )


# --- A4: the brief never sits inside quiet hours ----------------------------------


@pytest.mark.parametrize(
    ("quiet", "brief", "ok"),
    [
        ("23:30-07:30", "09:00", True),  # the owner's decision, as shipped
        ("23:30-07:30", "07:00", False),  # inside the wrapped range
        ("23:30-07:30", "23:45", False),  # inside, after midnight's other side
        ("23:30-07:30", "23:30", False),  # the start belongs to the range
        ("23:30-07:30", "07:30", True),  # the end does not (half-open)
        ("13:00-15:00", "14:00", False),  # a range that does not wrap
        ("13:00-15:00", "16:00", True),
        ("13:00-15:00", "12:59", True),
    ],
)
def test_a_brief_inside_quiet_hours_is_refused_at_startup(quiet, brief, ok):
    build = lambda: Settings(  # noqa: E731
        _env_file=None, quiet_hours=quiet, morning_brief_time=brief
    )
    if ok:
        assert build().morning_brief_time == brief
    else:
        with pytest.raises(ValidationError, match="QUIET_HOURS"):
            build()


def test_the_config_check_agrees_with_the_reminder_sweep():
    """Same rule, spelled twice because config.py cannot import reminders:
    a brief time is refused exactly when the sweep would call it quiet."""
    for hour, minute in ((23, 29), (23, 30), (3, 0), (7, 29), (7, 30), (12, 0)):
        refused = False
        try:
            Settings(
                _env_file=None,
                quiet_hours="23:30-07:30",
                morning_brief_time=f"{hour:02d}:{minute:02d}",
            )
        except ValidationError:
            refused = True
        assert refused is reminders.in_quiet_hours(_at(hour, minute))


# --- A5: one nudge per question, one more after a snooze ---------------------------


async def _log_of(session, interaction_id: int) -> m.ReminderLog:
    return await session.scalar(
        sa.select(m.ReminderLog)
        .where(m.ReminderLog.kind == nudges.NUDGE_KIND)
        .where(m.ReminderLog.ref == str(interaction_id))
        .order_by(m.ReminderLog.id.desc())
        .limit(1)
    )


async def test_a_question_is_nudged_once_and_the_brief_carries_it_from_then_on(session):
    akmal = await _person(session)
    await _monitor(session)
    asked = await _msg(session, "narxi qancha?", at=_ago(hours=6), person=akmal)

    [due] = await nudges.collect(session)
    assert due.interaction_id == asked.id
    nudges.mark_nudged(session, [due])
    await session.flush()

    assert await nudges.collect(session) == []
    assert await nudges.collect(session, now=_now() + timedelta(hours=25)) == []
    assert await nudges.collect(session, now=_now() + timedelta(days=6)) == []
    # Still open everywhere the owner reads: only the nudge is spent.
    assert len(await nudges.unanswered_questions(session)) == 1
    assert not hasattr(nudges, "NUDGE_EVERY")


async def test_ertaga_earns_exactly_one_more_nudge_when_it_expires(session):
    akmal = await _person(session)
    await _monitor(session)
    asked = await _msg(session, "narxi qancha?", at=_ago(hours=6), person=akmal)
    [due] = await nudges.collect(session)
    nudges.mark_nudged(session, [due])
    await session.flush()
    assert await nudges.collect(session) == []

    until = nudges.next_morning(_at(15, 0))
    assert until == _at(9) + timedelta(days=1)
    nudges.snooze(asked, until=until)
    await session.flush()

    assert await nudges.collect(session, now=until - timedelta(minutes=1)) == []
    [again] = await nudges.collect(session, now=until + timedelta(minutes=1))
    assert again.interaction_id == asked.id
    # The second nudge goes out; from then on the question is the brief's.
    nudges.mark_nudged(session, [again])
    await session.flush()
    (await _log_of(session, asked.id)).sent_at = until + timedelta(minutes=2)
    await session.flush()
    assert await nudges.collect(session, now=until + timedelta(minutes=3)) == []
    assert await nudges.collect(session, now=until + timedelta(days=3)) == []


def test_the_nudge_rule_is_pure_and_explicit():
    now = _at(12, 0)
    assert nudges.is_due(None, None, now=now)
    assert not nudges.is_due(now - timedelta(days=5), None, now=now)
    snooze = now - timedelta(hours=1)
    assert not nudges.is_due(None, now + timedelta(hours=1), now=now)  # still snoozed
    assert nudges.is_due(snooze - timedelta(days=1), snooze, now=now)  # spent before
    assert not nudges.is_due(snooze + timedelta(minutes=5), snooze, now=now)  # after


# --- A6: "Ertaga" at night means this morning -------------------------------------


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (_at(0, 30), _at(9)),  # before the brief: today
        (_at(8, 59), _at(9)),
        (_at(9, 0), _at(9) + timedelta(days=1)),  # at it: tomorrow's
        (_at(15, 0), _at(9) + timedelta(days=1)),
    ],
)
def test_next_morning_is_the_next_brief_not_always_tomorrows(now, expected):
    assert nudges.next_morning(now) == expected


# --- A7: the engine knows the answered mark ---------------------------------------


async def test_the_engine_itself_honours_javob_berdim(session):
    akmal = await _person(session)
    await _monitor(session)
    first = await _msg(session, "narxi qancha?", at=_ago(hours=9), person=akmal)
    second = await _msg(session, "qachon yuborasiz?", at=_ago(hours=7), person=akmal)

    nudges.mark_answered(first)
    await session.flush()
    [loop] = await loops.unanswered_questions(session)
    assert loop.interaction_id == second.id and loop.follow_ups == 0
    assert [q.interaction_id for q in (await loops.open_loops(session)).questions] == [
        second.id
    ]

    # An answered row resets the chat: everything at or before it is handled.
    nudges.mark_answered(second)
    await session.flush()
    assert await loops.unanswered_questions(session) == []


async def test_nudges_delegates_to_the_engine_instead_of_repeating_the_walk(session):
    assert nudges.is_answered is loops.is_answered
    assert nudges.ANSWERED_KEY == loops.ANSWERED_KEY == "answered"
    for name in ("unanswered_questions", "open_loops"):
        source = inspect.getsource(getattr(nudges, name))
        assert "looks_like_question" not in source and "question_candidates" not in source
    akmal = await _person(session)
    await _monitor(session)
    asked = await _msg(session, "narxi qancha?", at=_ago(hours=9), person=akmal)
    now = _now()
    assert [q.ref for q in await nudges.unanswered_questions(session, now=now)] == [
        f"q{asked.id}"
    ]
    assert [q.ref for q in (await nudges.open_loops(session, now)).ordered] == [
        f"q{asked.id}"
    ]


# --- A8: one apostrophe table -----------------------------------------------------


def test_every_apostrophe_variant_folds_to_ascii_in_one_place():
    assert text.fold_apostrophes("oʻgʼ o‘g’ o`gʹ") == "o'g' o'g' o'g'"
    assert text.fold_apostrophes("Gʼani") == "G'ani"
    assert not hasattr(loops, "_APOSTROPHES") and not hasattr(userbot, "_APOSTROPHES")


def test_an_alias_typed_with_the_android_apostrophe_still_matches(monkeypatch):
    monkeypatch.setattr(settings, "owner_aliases", "G'ani")
    assert userbot.addressed_to_owner(_message("Gʼani keldi"), ChatType.group)
    assert userbot.addressed_to_owner(_message("Ғани келди"), ChatType.group)
    assert userbot.transliterate("Gʼani") == "ғани"
    assert loops.looks_like_question("boʼladimi")
    assert loops.looks_like_question("pulni toʼladingizmi")


# --- A9: the question detector's false positives ----------------------------------


@pytest.mark.parametrize(
    ("text_", "expected"),
    [
        # True positives kept.
        ("Konteyner qachon yetib keladi", True),
        ("bormi", True),
        ("Bo'ladimi", True),
        ("Pulni oldingizmi", True),
        ("hujjatlarni yubordingizmi", True),
        ("Yubordingizmi", True),  # capitalised, but an everyday form
        ("Hujjatlarni jo'natdingizmi", True),
        ("kim keldi?", True),  # bare "kim" counts with a "?"
        ("nima gap?", True),
        ("что там?", True),
        ("Пулни олдингизми", True),
        # False positives fixed.
        ("Rahimi keldi", False),
        ("Rahimi", False),  # a capitalised name in -mi is not a question
        ("Salom Rahimi", False),
        ("потому что так", False),
        ("nima bo'lsa ham qilaman", False),
        ("kim", False),
        ("нима гап", False),
        ("ким келди", False),
        ("что", False),
        ("Karimi va Hoshimi", False),
        # Possessive -i on a stem in -m is not the question particle.
        ("bu uning rasmi", False),
        ("ikkinchi qismi", False),
        ("bizning bo'limi", False),
        ("ҳужжатнинг қисми", False),
        ("yukning hajmi", False),
        # Nine lower-case words ending in -mi: too long for the particle rule.
        ("biz bugun ertalab soat sakkizda yukni omborga olib bordimi", False),
    ],
)
def test_looks_like_question_after_the_verified_false_positives(text_, expected):
    assert loops.looks_like_question(text_) is expected


def test_the_particle_rule_is_short_messages_only():
    eight = "bugun ertalab yukni omborga olib borib qo'ydingizmi"  # 7 words
    assert loops.looks_like_question(eight)
    assert loops.QUESTION_PARTICLE_MAX_WORDS == 8
    for word in ("nima", "нима", "kim", "ким", "что"):
        assert word not in loops.QUESTION_WORDS


# --- A10: one knob for the weekly cadence ------------------------------------------


def test_the_dated_re_ask_cadence_is_loop_undated_days_read_lazily(monkeypatch):
    assert not hasattr(reminders, "NUDGE_EVERY")
    assert reminders.nudge_every() == timedelta(days=settings.loop_undated_days)

    now = _at(12, 0)
    due = (now - timedelta(days=30)).date()
    history = [(reminders.ask_kind("promise"), now - timedelta(days=4))]
    decide = lambda: reminders.decide(  # noqa: E731
        "promise", history, due=due, created_at=None, now=now
    )
    assert decide() is None  # a week: asked four days ago, not yet
    monkeypatch.setattr(settings, "loop_undated_days", 3)
    assert reminders.nudge_every() == timedelta(days=3)
    assert decide() == reminders.ASK

    # "Ha, still open" runs on the same knob.
    acked = [*history, (reminders.ack_kind("promise"), now - timedelta(days=4))]
    assert (
        reminders.decide("promise", acked, due=due, created_at=None, now=now)
        == reminders.ASK
    )
    monkeypatch.setattr(settings, "loop_undated_days", 7)
    assert reminders.decide("promise", acked, due=due, created_at=None, now=now) is None


def test_the_dead_undated_query_is_gone_and_the_env_comment_says_so():
    assert not hasattr(queries, "undated_open")
    env = ENV_EXAMPLE.read_text()
    assert "LOOP_UNDATED_DAYS=7" in env
    # The comment above the key names both uses of the one knob.
    block = env[env.index("# --- Open loops") : env.index("LOOP_UNDATED_DAYS=7")]
    assert "Hali ochiqmi" in block and "still open" in block


async def test_the_undated_sweep_still_asks_after_a_week(session):
    """collect_due reads the undated selection through loops; nothing here
    depended on the deleted query."""
    akmal = await _person(session)
    await _promise(session, akmal, "invoice yuboradi", created_at=_ago(days=8))
    bundle = await reminders.collect_due(session)
    assert [q.kind for q in bundle.questions] == ["promise"]

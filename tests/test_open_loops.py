"""Build step 2, part A: the open-loops engine.

"Remind me of everything that slipped my attention" — a question nobody
answered, a promise with no date, a supplier who went quiet — found from
what the system already stores, with no model call, and ranked by money
first, then age. Plus the plain-text address: "Bekzod aka, …" typed in a
group counts as aimed at the owner, the same as an @-mention.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from miya.config import settings
from miya.db import models as m
from miya.db.enums import (
    ChatType,
    Currency,
    DebtDirection,
    DebtStatus,
    Direction,
    InteractionSource,
    PromiseMadeBy,
    PromiseStatus,
    TransactionType,
)
from miya.services import loops, reminders
from miya.userbot import main as userbot

TZ = settings.tz
_MESSAGE_IDS = itertools.count(1)
PRIVATE = 1001
GROUP = -1002
ALIASES = "Bekzod, Begi, Bekzod aka, Begika, Bega, GSR Logistics"


def _now() -> datetime:
    return datetime.now(TZ)


def _ago(**kwargs) -> datetime:
    return _now() - timedelta(**kwargs)


async def _person(session, name="Akmal") -> m.Person:
    person = m.Person(display_name=name, aliases=[])
    session.add(person)
    await session.flush()
    return person


async def _monitor(session, chat_id=PRIVATE, chat_type=ChatType.private, enabled=True):
    monitor = m.ChatMonitor(
        tg_chat_id=chat_id,
        chat_type=chat_type,
        title="Akmal" if chat_type is ChatType.private else "Yuk guruhi",
        monitor_enabled=enabled,
    )
    session.add(monitor)
    await session.flush()
    return monitor


async def _msg(
    session,
    text,
    *,
    at,
    chat_id=PRIVATE,
    direction=Direction.in_,
    person=None,
    to_me=False,
    transcript=None,
):
    # Unique per chat, as Telegram numbers them (ux_interactions_tg_message).
    meta = {"tg_message_id": next(_MESSAGE_IDS)}
    if to_me:
        meta["to_me"] = True
    row = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=direction,
        tg_chat_id=chat_id,
        person_id=person.id if person else None,
        occurred_at=at,
        raw_text=text,
        transcript=transcript,
        meta=meta,
    )
    session.add(row)
    await session.flush()
    return row


async def _debt(session, person, amount="5000000", currency=Currency.UZS, **kw):
    debt = m.Debt(
        direction=kw.pop("direction", DebtDirection.they_owe_me),
        person_id=person.id,
        amount=Decimal(amount),
        currency=currency,
        reason=kw.pop("reason", "yuk uchun"),
        **kw,
    )
    session.add(debt)
    await session.flush()
    return debt


async def _promise(session, person, text="invoice yuboradi", **kw):
    promise = m.Promise(
        made_by=kw.pop("made_by", PromiseMadeBy.them),
        person_id=person.id,
        description=text,
        **kw,
    )
    session.add(promise)
    await session.flush()
    return promise


# --- what a question looks like ----------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("konteyner qachon keladi?", True),
        ("Bekzod aka, konteyner qachon", True),
        ("Когда будет оплата", True),
        ("сколько осталось", True),
        ("hujjatlarni yubordingizmi", True),  # the -mi particle, no "?"
        ("bo‘ladimi", True),  # curly apostrophe
        ("Қачон тўлайсиз", True),
        ("salom, yuk ketdi", False),
        ("nimadir bo'ldi", False),  # "nima" only as a whole word
        ("kimyo zavodi", False),
        ("rasmiy xat", False),
        ("", False),
        (None, False),
    ],
)
def test_looks_like_question(text, expected):
    assert loops.looks_like_question(text) is expected


def test_the_question_words_live_in_one_extensible_list():
    assert "qachon" in loops.QUESTION_WORDS and "когда" in loops.QUESTION_WORDS


# --- 1. unanswered questions -------------------------------------------------


async def test_a_question_in_a_private_chat_with_no_reply_is_an_open_loop(session):
    akmal = await _person(session)
    await _monitor(session)
    await _msg(session, "salom", at=_ago(hours=7), person=akmal)
    asked = await _msg(
        session, "konteyner qachon keladi?", at=_ago(hours=6), person=akmal
    )
    await _msg(session, "javob kutyapman", at=_ago(hours=5), person=akmal)

    [loop] = await loops.unanswered_questions(session)

    assert loop.ref == f"q{asked.id}"
    assert loop.kind == loops.KIND_QUESTION
    assert loop.text == "konteyner qachon keladi?"
    assert loop.person is akmal and loop.person_name == "Akmal"
    assert loop.tg_chat_id == PRIVATE and loop.chat_title == "Akmal"
    assert loop.is_group is False
    assert loop.follow_ups == 1
    assert timedelta(hours=5, minutes=59) < loop.age < timedelta(hours=6, minutes=1)
    assert loop.stake_rank == 0


async def test_a_question_the_owner_answered_is_not_open(session):
    akmal = await _person(session)
    await _monitor(session)
    await _msg(session, "konteyner qachon?", at=_ago(hours=6), person=akmal)
    await _msg(session, "ertaga", at=_ago(hours=5), direction=Direction.out, person=akmal)

    assert await loops.unanswered_questions(session) == []


async def test_only_questions_after_the_owners_last_message_count(session):
    akmal = await _person(session)
    await _monitor(session)
    await _msg(session, "narxi qancha?", at=_ago(hours=30), person=akmal)
    await _msg(session, "500$", at=_ago(hours=29), direction=Direction.out, person=akmal)
    fresh = await _msg(session, "qachon jo'natasiz?", at=_ago(hours=8), person=akmal)

    [loop] = await loops.unanswered_questions(session)

    assert loop.ref == f"q{fresh.id}"


async def test_a_recent_question_waits_for_the_threshold(session):
    akmal = await _person(session)
    await _monitor(session)
    await _msg(session, "konteyner qachon?", at=_ago(hours=2), person=akmal)

    assert await loops.unanswered_questions(session) == []
    assert len(await loops.unanswered_questions(session, hours=1)) == 1


async def test_a_statement_with_no_reply_is_not_a_question(session):
    akmal = await _person(session)
    await _monitor(session)
    await _msg(session, "yuk ketdi, rahmat", at=_ago(hours=9), person=akmal)

    assert await loops.unanswered_questions(session) == []


async def test_a_voice_note_question_is_read_from_its_transcript(session):
    akmal = await _person(session)
    await _monitor(session)
    await _msg(
        session, None, at=_ago(hours=9), person=akmal, transcript="pul qachon tushadi?"
    )

    [loop] = await loops.unanswered_questions(session)
    assert loop.text == "pul qachon tushadi?"


async def test_in_a_group_only_messages_aimed_at_the_owner_count(session):
    akmal = await _person(session)
    await _monitor(session, GROUP, ChatType.group)
    await _msg(session, "kim keladi?", at=_ago(hours=9), chat_id=GROUP, person=akmal)
    assert await loops.unanswered_questions(session) == []

    mine = await _msg(
        session,
        "Bekzod aka, konteyner qachon?",
        at=_ago(hours=8),
        chat_id=GROUP,
        person=akmal,
        to_me=True,
    )
    [loop] = await loops.unanswered_questions(session)
    assert loop.ref == f"q{mine.id}" and loop.is_group and loop.chat_title == "Yuk guruhi"

    # The owner writing anything in that group afterwards is the answer.
    await _msg(
        session, "ertaga", at=_ago(hours=7), chat_id=GROUP, direction=Direction.out
    )
    assert await loops.unanswered_questions(session) == []


async def test_a_chat_switched_off_and_the_window_rows_are_ignored(session):
    akmal = await _person(session)
    await _monitor(session, enabled=False)
    await _msg(session, "qachon?", at=_ago(hours=9), person=akmal)
    assert await loops.unanswered_questions(session) == []

    await _monitor(session, 1003)
    await _msg(session, "qachon?", at=_ago(hours=9), chat_id=1003, person=akmal)
    # The window summary row (direction na) is the same text and must not
    # be mistaken for a message — nor count as the owner having answered.
    await _msg(
        session, "[THEM] qachon?", at=_ago(hours=8), chat_id=1003, direction=Direction.na
    )

    assert len(await loops.unanswered_questions(session)) == 1


# --- 2. plain-text address ----------------------------------------------------


def _group_message(text, mentioned=False):
    return SimpleNamespace(id=1, message=text, mentioned=mentioned)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Bekzod aka, konteyner qachon?", True),
        ("bekzod, salom", True),
        ("BEGA qachon kelasiz", True),
        ("(Begika) ko'rdingizmi", True),
        ("GSR Logistics ga yozing", True),
        ("Бекзод ака, контейнер қачон?", True),  # Cyrillic, from the Latin alias
        ("бега, ха", True),
        ("Begalar keldi", False),  # whole word only
        ("Bekzodjon aytdi", False),
        ("ular o'zaro gaplashyapti", False),
        ("", False),
        (None, False),
    ],
)
def test_an_alias_in_plain_text_addresses_the_owner(monkeypatch, text, expected):
    monkeypatch.setattr(settings, "owner_aliases", ALIASES)
    assert userbot.addressed_to_owner(_group_message(text), ChatType.group) is expected


def test_a_private_chat_is_never_flagged_and_a_mention_still_is(monkeypatch):
    monkeypatch.setattr(settings, "owner_aliases", ALIASES)
    assert not userbot.addressed_to_owner(_group_message("Bekzod aka?"), ChatType.private)
    assert userbot.addressed_to_owner(
        _group_message("salom", mentioned=True), ChatType.group
    )


def test_no_aliases_configured_means_mentions_only(monkeypatch):
    monkeypatch.setattr(settings, "owner_aliases", "")
    assert settings.owner_aliases_parsed == ()
    assert not userbot.addressed_to_owner(_group_message("Bekzod aka?"), ChatType.group)


def test_aliases_are_parsed_and_transliterated_once():
    monkey = ("Bekzod aka", "G'ani", "GSR Logistics")
    assert userbot.transliterate("Bekzod aka") == "бекзод ака"
    assert userbot.transliterate("G'ani") == "ғани"
    assert userbot.transliterate("Shohrux") == "шоҳрух"
    assert userbot.transliterate("Бекзод") == "бекзод"  # already Cyrillic: unchanged
    assert userbot.alias_pattern(monkey) is userbot.alias_pattern(monkey)  # cached
    assert userbot.alias_pattern(()) is None


def test_the_plain_text_address_lands_in_the_message_metadata(monkeypatch):
    """The end-to-end shape: what the userbot writes for a group message with
    the owner's name typed out, which queries.messages_to_me then finds."""
    monkeypatch.setattr(settings, "owner_aliases", ALIASES)
    monitor = SimpleNamespace(chat_type=ChatType.group)
    meta = userbot._message_meta(_group_message("Bekzod aka, konteyner qachon?"), monitor)
    assert meta == {"tg_message_id": 1, "to_me": True}
    assert userbot._message_meta(_group_message("ha, ketdi"), monitor) == {
        "tg_message_id": 1
    }


# --- 3. ageing undated commitments -------------------------------------------


def test_the_touch_kinds_agree_with_the_reminder_log():
    assert loops.log_kinds("debt") == (
        "debt",
        reminders.ask_kind("debt"),
        reminders.ack_kind("debt"),
    )


async def test_an_undated_promise_nobody_touched_for_a_week_is_stale(session):
    akmal = await _person(session)
    old = await _promise(session, akmal, "invoice yuboradi", created_at=_ago(days=8))
    await _promise(session, akmal, "yangi", created_at=_ago(days=2))
    await _promise(
        session, akmal, "muddatli", created_at=_ago(days=30), due_date=_now().date()
    )
    await _promise(
        session, akmal, "bajarilgan", created_at=_ago(days=30), status=PromiseStatus.done
    )

    [stale] = await loops.stale_undated(session)

    assert stale.ref == f"p{old.id}" and stale.record_kind == "promise"
    assert stale.kind == loops.KIND_UNDATED
    assert stale.record is old and stale.person is akmal
    assert stale.description == "invoice yuboradi"
    assert stale.outstanding is None and stale.currency is None and stale.stake_rank == 0
    assert stale.last_touched_at == old.created_at
    assert timedelta(days=7, hours=23) < stale.age < timedelta(days=8, hours=1)
    assert stale.untouched_for == stale.age


async def test_a_correction_or_a_reminder_counts_as_touching_it(session):
    akmal = await _person(session)
    corrected = await _promise(
        session,
        akmal,
        "tuzatilgan",
        created_at=_ago(days=20),
        history=[{"at": _ago(days=2).isoformat(), "field": "due", "by": "command"}],
    )
    asked = await _promise(session, akmal, "so'ralgan", created_at=_ago(days=20))
    session.add(
        m.ReminderLog(kind="ask:promise", ref=str(asked.id), sent_at=_ago(days=3))
    )
    long_ago = await _promise(session, akmal, "unutilgan", created_at=_ago(days=20))
    session.add(
        m.ReminderLog(kind="ask:promise", ref=str(long_ago.id), sent_at=_ago(days=9))
    )
    await session.flush()

    stale = await loops.stale_undated(session)

    assert [s.ref for s in stale] == [f"p{long_ago.id}"]
    assert corrected.id not in [s.record.id for s in stale]
    assert stale[0].untouched_for < timedelta(days=9, hours=1)
    assert stale[0].age > timedelta(days=19)


async def test_an_undated_debt_is_stale_with_its_outstanding_from_sql(session):
    akmal = await _person(session)
    debt = await _debt(session, akmal, "5000000", created_at=_ago(days=10))
    session.add(
        m.DebtPayment(debt_id=debt.id, amount=Decimal("2000000"), currency=Currency.UZS)
    )
    paid = await _debt(session, akmal, "1000000", created_at=_ago(days=10))
    session.add(
        m.DebtPayment(debt_id=paid.id, amount=Decimal("1000000"), currency=Currency.UZS)
    )
    await _debt(
        session,
        akmal,
        "700",
        currency=Currency.USD,
        created_at=_ago(days=10),
        status=DebtStatus.settled,
    )
    await _debt(
        session,
        akmal,
        "900",
        currency=Currency.USD,
        created_at=_ago(days=10),
        due_date=_now().date(),
    )
    await session.flush()

    [stale] = await loops.stale_undated(session)

    assert stale.ref == f"d{debt.id}" and stale.record_kind == "debt"
    assert stale.outstanding == Decimal("3000000") and stale.currency is Currency.UZS
    assert stale.stake_rank == Decimal("3000000")
    assert stale.description == "yuk uchun"


async def test_a_ping_on_the_debt_balance_touches_every_row_of_it(session):
    akmal = await _person(session)
    debt = await _debt(session, akmal, created_at=_ago(days=10))
    session.add(
        m.ReminderLog(
            kind="ack:debt",
            ref=reminders.debt_ref(akmal.id, debt.direction, debt.currency),
            sent_at=_ago(days=1),
        )
    )
    await session.flush()

    assert await loops.stale_undated(session) == []


async def test_kinds_narrow_the_selection_and_undated_tasks_are_in_it(session):
    akmal = await _person(session)
    await _debt(session, akmal, created_at=_ago(days=10))
    await _promise(session, akmal, created_at=_ago(days=10))
    session.add(m.Task(description="eski vazifa", created_at=_ago(days=10)))
    await session.flush()

    assert [s.record_kind for s in await loops.stale_undated(session)] == [
        "debt",
        "promise",
        "task",
    ]
    assert [
        s.record_kind
        for s in await loops.stale_undated(session, kinds=("promise", "task"))
    ] == ["promise", "task"]


async def test_the_threshold_is_configurable(session):
    akmal = await _person(session)
    await _promise(session, akmal, created_at=_ago(days=4))

    assert await loops.stale_undated(session) == []
    assert len(await loops.stale_undated(session, days=3)) == 1


async def test_the_weekly_question_comes_from_the_same_selection(session):
    """reminders.collect_due asks about undated promises through
    loops.stale_undated, so a promise the owner corrected yesterday is not
    asked about, and one nobody touched for a week is."""
    akmal = await _person(session)
    forgotten = await _promise(session, akmal, "unutilgan", created_at=_ago(days=8))
    await _promise(
        session,
        akmal,
        "tuzatilgan",
        created_at=_ago(days=8),
        history=[{"at": _ago(days=1).isoformat(), "field": "person", "by": "button"}],
    )
    await _debt(session, akmal, created_at=_ago(days=10))  # the brief's, not the sweep's

    bundle = await reminders.collect_due(session)

    assert [(q.kind, q.ref) for q in bundle.questions] == [("promise", str(forgotten.id))]
    assert bundle.questions[0].record is forgotten and bundle.questions[0].person is akmal


# --- 4. quiet counterparties ----------------------------------------------------


async def test_someone_with_an_open_debt_not_heard_from_in_two_weeks_is_quiet(session):
    akmal = await _person(session)
    debt = await _debt(
        session, akmal, "1200", currency=Currency.USD, created_at=_ago(days=40)
    )
    promise = await _promise(session, akmal, created_at=_ago(days=40))
    session.add(
        m.Interaction(
            source=InteractionSource.telegram_userbot,
            direction=Direction.out,
            person_id=akmal.id,
            occurred_at=_ago(days=20),
            raw_text="salom",
        )
    )
    await session.flush()

    [quiet] = await loops.quiet_counterparties(session)

    assert quiet.ref == f"k{akmal.id}" and quiet.kind == loops.KIND_QUIET
    assert quiet.person is akmal
    assert quiet.days_quiet == 20 and timedelta(days=20) <= quiet.age < timedelta(days=21)
    assert [(b.currency, b.outstanding, b.ids) for b in quiet.balances] == [
        (Currency.USD, Decimal("1200"), [debt.id])
    ]
    assert [p.id for p in quiet.promises] == [promise.id]
    assert quiet.stake_rank == Decimal("1200") * loops.RANK_WEIGHT_UZS[Currency.USD]


async def test_any_contact_resets_the_quiet_clock(session):
    akmal = await _person(session)
    await _debt(session, akmal, created_at=_ago(days=40))
    session.add(
        m.Transaction(
            type=TransactionType.income,
            amount=Decimal("100000"),
            currency=Currency.UZS,
            counterparty_person_id=akmal.id,
            occurred_at=_ago(days=3),
        )
    )
    dilnoza = await _person(session, "Dilnoza")
    await _promise(session, dilnoza, created_at=_ago(days=5), made_by=PromiseMadeBy.me)
    await session.flush()

    assert await loops.quiet_counterparties(session) == []


async def test_nothing_open_means_nobody_is_quiet(session):
    akmal = await _person(session)
    await _debt(session, akmal, created_at=_ago(days=40), status=DebtStatus.settled)
    await _promise(session, akmal, created_at=_ago(days=40), status=PromiseStatus.done)
    await session.flush()

    assert await loops.quiet_counterparties(session) == []
    assert len(await loops.quiet_counterparties(session, days=1)) == 0


async def test_quiet_people_are_listed_quietest_first(session):
    akmal = await _person(session, "Akmal")
    await _debt(session, akmal, created_at=_ago(days=15))
    dilnoza = await _person(session, "Dilnoza")
    await _promise(session, dilnoza, created_at=_ago(days=30))

    quiet = await loops.quiet_counterparties(session)

    assert [q.person_name for q in quiet] == ["Dilnoza", "Akmal"]


# --- 5. everything, by urgency --------------------------------------------------


async def test_open_loops_puts_money_first_then_age(session):
    akmal = await _person(session, "Akmal")
    await _monitor(session)
    await _msg(session, "konteyner qachon?", at=_ago(hours=10), person=akmal)
    await _promise(session, akmal, "invoice", created_at=_ago(days=9))
    small = await _debt(session, akmal, "2000000", created_at=_ago(days=8))
    session.add(
        m.Interaction(
            source=InteractionSource.assistant_bot,
            direction=Direction.in_,
            person_id=akmal.id,
            occurred_at=_ago(days=1),
            raw_text="Akmal bilan gaplashdim",
        )
    )
    supplier = await _person(session, "Li Wei")
    await _debt(
        session,
        supplier,
        "1000",
        currency=Currency.USD,
        created_at=_ago(days=20),
        direction=DebtDirection.i_owe_them,
        due_date=_now().date(),
    )
    await session.flush()

    now = _now()
    result = await loops.open_loops(session, now)

    assert result.now is now
    assert not result.is_empty()
    assert [loop.kind for loop in result.ordered] == [
        loops.KIND_QUIET,  # $1000 with the supplier: the most money at stake
        loops.KIND_UNDATED,  # d: 2 mln so'm, undated and untouched
        loops.KIND_UNDATED,  # p: no money, 9 days old
        loops.KIND_QUESTION,  # no money, 10 hours old
    ]
    assert result.ordered[0].person_name == "Li Wei"
    assert result.ordered[1].ref == f"d{small.id}"
    assert (
        len(result.questions) == 1 and len(result.stale) == 2 and len(result.quiet) == 1
    )
    # Every loop carries what the renderer needs: a ref, an age, a person.
    for loop in result.ordered:
        assert loop.ref and loop.age > timedelta(0)


async def test_an_empty_database_has_no_loops(session):
    result = await loops.open_loops(session, _now())
    assert result.is_empty() and result.ordered == []

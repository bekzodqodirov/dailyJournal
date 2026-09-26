"""WP-51: the recap's day, from SQL alone."""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta
from decimal import Decimal

import anthropic
import pytest
import sqlalchemy as sa

from miya.config import settings
from miya.db import models as m
from miya.db.enums import (
    ChatType,
    Currency,
    Direction,
    InteractionSource,
    TransactionType,
    WindowStatus,
)
from miya.services import extraction, phone_events, recaps
from miya.services.queries import day_bounds

TZ = settings.tz
DAY = datetime(2026, 9, 20, tzinfo=TZ).date()
START, END = day_bounds(DAY)
NOON = START + timedelta(hours=12)
AFTER = END + timedelta(hours=1)
PRIVATE, GROUP = 7001, -7002
_ids = itertools.count(1)


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("a recap's figures never come from a model")

    monkeypatch.setattr(extraction, "get_client", boom)
    monkeypatch.setattr(anthropic, "AsyncAnthropic", boom)


async def _gather(session):
    await session.flush()
    return await recaps.gather_activity(session, START, END, now=AFTER)


async def _person(session, name="Akmal") -> m.Person:
    person = m.Person(display_name=name, aliases=[])
    session.add(person)
    await session.flush()
    return person


async def _chats(session):
    session.add(m.ChatMonitor(tg_chat_id=PRIVATE, chat_type=ChatType.private, title="A"))
    session.add(
        m.ChatMonitor(
            tg_chat_id=GROUP, chat_type=ChatType.group, title="Yuk", monitor_enabled=True
        )
    )
    await session.flush()


async def _msg(
    session,
    *,
    person=None,
    chat=PRIVATE,
    out=False,
    at=NOON,
    text="salom",
    meta=None,
    window_id=None,
):
    row = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.out if out else Direction.in_,
        person_id=person.id if person else None,
        tg_chat_id=chat,
        occurred_at=at,
        raw_text=text,
        window_id=window_id,
        meta={"tg_message_id": next(_ids), **(meta or {})},
    )
    session.add(row)
    await session.flush()
    return row


def _txn(amount, currency=Currency.UZS, kind=TransactionType.expense, **fields):
    return m.Transaction(
        type=kind, amount=Decimal(amount), currency=currency, occurred_at=NOON, **fields
    )


async def test_money_is_per_currency_decimal_and_never_mixed(session):
    session.add_all([_txn("250000"), _txn("100"), _txn("50", Currency.USD)])
    session.add(_txn("1000", kind=TransactionType.income))
    day = await _gather(session)
    assert day.money.expense == {
        Currency.UZS: Decimal("250100.00"),
        Currency.USD: Decimal("50.00"),
    }
    assert day.money.income == {Currency.UZS: Decimal("1000.00")}
    assert all(isinstance(v, Decimal) for v in day.money.expense.values())


async def test_voided_and_internal_rows_are_not_money(session):
    session.add_all(
        [_txn("100"), _txn("200", voided_at=NOON), _txn("300", is_internal=True)]
    )
    day = await _gather(session)
    assert day.money.expense == {Currency.UZS: Decimal("100.00")}
    assert day.money.voided == 1


async def test_repayments_in_the_window_are_listed_with_the_debt(session):
    akmal = await _person(session)
    debt = m.Debt(
        person_id=akmal.id, direction="they_owe_me", amount=1_000_000, currency="UZS"
    )
    session.add(debt)
    await session.flush()
    session.add(
        m.DebtPayment(debt_id=debt.id, amount=400_000, currency="UZS", paid_at=NOON)
    )
    session.add(
        m.DebtPayment(
            debt_id=debt.id, amount=1, currency="UZS", paid_at=START - timedelta(1)
        )
    )
    day = await _gather(session)
    [line] = day.money.repayments
    assert (line.debt_id, line.person_name, line.amount) == (
        debt.id,
        "Akmal",
        Decimal("400000.00"),
    )
    assert day.people[0].repayment_debt_ids == [debt.id]


async def test_phone_rows_are_checkable_with_x_refs_and_others_are_not(session):
    session.add_all([_txn("100", channel="sms:payme"), _txn("200")])
    day = await _gather(session)
    assert [c.amount for c in day.money.checkable] == [Decimal("100.00")]
    assert day.money.checkable_total == 1


async def test_from_phone_merged_voided_and_review_counts(session):
    at = NOON.isoformat()
    body = "Платёж успешно проведён\nKORZINKA\n250 000 сум"
    for sms_id in (1, 2):
        await phone_events.ingest_sms(
            session,
            "dev",
            [
                {
                    "sms_id": sms_id,
                    "sender": "Payme",
                    "received_at": at,
                    "body": body + (" " * sms_id),
                    "sim_slot": 0,
                }
            ],
        )
    await phone_events.ingest_notifications(
        session, "dev", [{"package": "uz.dida.payme", "posted_at": at, "text": body}]
    )
    await phone_events.ingest_sms(
        session,
        "dev",
        [
            {
                "sms_id": 9,
                "sender": "Payme",
                "received_at": at,
                "body": "Оплата 5 000 сум отклонена",
                "sim_slot": 0,
            }
        ],
    )
    for row in await session.scalars(sa.select(m.Transaction)):
        row.created_at = NOON
    for row in await session.scalars(sa.select(m.TransactionEvidence)):
        row.created_at = NOON
    day = await _gather(session)
    assert day.money.from_phone == 1
    assert day.money.merged == 1
    assert day.money.review_pending == 1


async def test_ignored_counts_otp_and_adverts_but_not_repeats(session):
    at = NOON.isoformat()
    await phone_events.ingest_sms(
        session,
        "dev",
        [
            {
                "sms_id": 1,
                "sender": "Payme",
                "received_at": at,
                "body": "Kod: 123456. Hech kimga bermang",
                "sim_slot": 0,
            },
            {
                "sms_id": 2,
                "sender": "Click",
                "received_at": at,
                "sim_slot": 0,
                "body": "Click orqali barcha to'lovlar uchun 30 000 so'mgacha keshbek!",
            },
        ],
    )
    day = await _gather(session)
    assert day.money.ignored == 2


async def test_members_are_counted_not_the_window_row(session):
    await _chats(session)
    akmal = await _person(session)
    await _msg(session, person=akmal)
    await _msg(session, person=akmal, out=True)
    await _msg(session, person=akmal, meta={"kind": "window"})
    day = await _gather(session)
    [person] = day.people
    assert (person.messages_in, person.messages_out) == (1, 1)


async def test_group_chatter_goes_to_groups_and_addressed_lines_to_the_person(session):
    await _chats(session)
    akmal = await _person(session)
    await _msg(session, person=akmal, chat=GROUP)
    await _msg(session, person=akmal, chat=GROUP, meta={"to_me": True})
    day = await _gather(session)
    [person] = day.people
    assert (person.messages_in, person.group_mentions) == (0, 1)
    [group] = day.groups
    assert (group.messages, group.to_me) == (2, 1)


def _call(person, call_type, seconds, *, source="call_log"):
    media = {"type": source, "call_type": call_type, "duration_seconds": seconds}
    return m.Interaction(
        source=InteractionSource.phone_call,
        direction=Direction.in_,
        person_id=person.id if person else None,
        occurred_at=NOON,
        media=media,
    )


async def test_calls_come_from_the_call_log_with_minutes_and_missed(session):
    akmal = await _person(session)
    session.add_all(
        [
            _call(akmal, "incoming", 120),
            _call(akmal, "outgoing", 300),
            _call(akmal, "missed", 0),
            _call(akmal, None, 300, source="call_recording"),
        ]
    )
    day = await _gather(session)
    [person] = day.people
    assert (person.calls, person.call_seconds, person.missed) == (2, 420, 1)
    assert day.calls.total == 3


async def test_unknown_numbers_are_aggregated(session):
    session.add_all([_call(None, "incoming", 60), _call(None, "missed", 0)])
    day = await _gather(session)
    assert day.people == [] and day.calls.unknown == 2


async def test_questions_asked_today_are_marked_answered_or_not(session):
    await _chats(session)
    akmal = await _person(session)
    await _msg(session, person=akmal, text="Yuk qachon keladi?", at=NOON)
    await _msg(
        session, person=akmal, text="rahmat", out=True, at=NOON + timedelta(minutes=5)
    )
    await _msg(session, person=akmal, text="Narxi qancha?", at=NOON + timedelta(hours=1))
    day = await _gather(session)
    [person] = day.people
    assert [(q.text, q.answered) for q in person.questions] == [
        ("Narxi qancha?", False),
        ("Yuk qachon keladi?", True),
    ]


async def test_people_are_ranked_money_first(session):
    await _chats(session)
    chatty = await _person(session, "Vali")
    payer = await _person(session, "Akmal")
    for _ in range(20):
        await _msg(session, person=chatty)
    session.add(_txn("100", counterparty_person_id=payer.id))
    day = await _gather(session)
    assert [p.person.display_name for p in day.people] == ["Akmal", "Vali"]


async def test_window_is_half_open_and_local(session):
    session.add_all(
        [
            m.Transaction(
                type=TransactionType.expense,
                amount=1,
                currency=Currency.UZS,
                occurred_at=START,
            ),
            m.Transaction(
                type=TransactionType.expense,
                amount=10,
                currency=Currency.UZS,
                occurred_at=END,
            ),
        ]
    )
    day = await _gather(session)
    assert day.money.expense == {Currency.UZS: Decimal("1.00")}


async def test_a_group_with_an_unapplied_window_contributes_raw_lines(session):
    await _chats(session)
    window = m.ConversationWindow(
        tg_chat_id=GROUP,
        started_at=NOON,
        ended_at=NOON,
        message_count=1,
        char_count=5,
        text="x",
        status=WindowStatus.pending,
        custom_id="w-raw",
    )
    session.add(window)
    await session.flush()
    row = await _msg(session, chat=GROUP, window_id=window.id)
    day = await _gather(session)
    [group] = day.groups
    assert group.raw_ids == [row.id]

"""Closing the last review findings on build step 1.

The invariant behind most of them: a debt's payments never sum above its
amount, every mutation leaves a history entry, and a mis-tap is always
reversible.

 1  /tuzat amount resizes or removes the payment ✅ wrote; never below real money
 2  a currency change is refused while any payment exists
 3  ✅ twice is one payment: row locks, and "already done" the second time
 4  "300 krw" is 300 won, "300k" is 300 000 — all five currencies both ways
 5  a bare prefix ("kim") is usage, not a person called "kim"
 6  a status re-derived after an amount edit is a history entry
 7  /qaytar on a debt real payments cover changes nothing at all
 8  a date is the whole value: no trailing junk, no 8-digit form
 9  "Ha" on a balance with one settled and one open row keeps it open
10  /tuzat survives a newline or tab after the ref
11  the ✏️ hint only shows edits the kind accepts
12  only the questions actually shown are marked asked
13  ping dedupe is by calendar day, so D, D+1, D+3 are exactly those days
14  a debt settled by earlier payments gets settled_at
15  "invoice invoice" is one distinct token, not two
B3  an unknown name is asked about, not created; a keyword typo is usage
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from miya.bot import handlers, keyboards, replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import Currency, DebtDirection, DebtStatus, PromiseStatus
from miya.db.session import SessionLocal
from miya.services import extraction as ex
from miya.services import queries, records, reminders
from miya.services.people import resolve_person
from miya.services.persistence import apply_extraction
from tests.test_adversarial_fixes import _fulfilment
from tests.test_close_and_correct import (
    _Callback,
    _command,
    _logged,
    _Message,
    _payloads,
    _promise,
    _reload,
    _task,
    bound,  # noqa: F401 — fixture
)
from tests.test_pipeline import _interaction, _open_debt

TZ = settings.tz
TODAY = date(2026, 9, 15)


def _now() -> datetime:
    return datetime.now(TZ)


def _today() -> date:
    return _now().date()


async def _payments(session, debt_id):
    return list(
        await session.scalars(
            sa.select(m.DebtPayment)
            .where(m.DebtPayment.debt_id == debt_id)
            .order_by(m.DebtPayment.id)
        )
    )


async def _real_payment(session, debt, amount, note="naqd"):
    payment = m.DebtPayment(
        debt_id=debt.id, amount=Decimal(amount), currency=debt.currency, note=note
    )
    session.add(payment)
    await session.flush()
    return payment


def _fields(record):
    return [(e["field"], e["old"], e["new"]) for e in record.history]


class _Question(_Message):
    """A question message: the answer replaces it (edit_text) in place."""

    def __init__(self, reply_markup=None) -> None:
        super().__init__(reply_markup)
        self.edited_text = None

    async def edit_text(self, text, **kwargs):
        self.edited_text = text


# --- 1: the settling payment follows the amount ---------------------------------


async def test_tuzat_amount_after_a_tick_resizes_the_settling_payment(session):
    """After ✅ on 5 mln, "/tuzat d12 3 mln" must not leave a 5 mln
    'bajarildi' payment against a 3 mln debt."""
    _, debt = await _open_debt(session)
    await records.mark_done(session, debt, by=records.BY_BUTTON)
    [synthetic] = await _payments(session, debt.id)

    await records.set_field(
        session, debt, "amount", (Decimal("3000000"), None), by=records.BY_COMMAND
    )

    fresh = await _reload(session, "debt", debt.id)
    [payment] = await _payments(session, debt.id)
    assert payment.id == synthetic.id and payment.note == records.SETTLED_NOTE
    assert payment.amount == Decimal("3000000")  # never above the amount
    assert fresh.status is DebtStatus.settled and fresh.settled_at is not None
    assert _fields(fresh)[-2:] == [
        ("amount", "5000000.00", "3000000.00"),
        ("payment", "5000000.00", "3000000.00"),
    ]
    assert fresh.history[-1]["payment_id"] == synthetic.id
    # Up as well as down: the row stays settled and the books stay additive.
    await records.set_field(
        session, fresh, "amount", (Decimal("7000000"), None), by=records.BY_COMMAND
    )
    [payment] = await _payments(session, debt.id)
    assert payment.amount == Decimal("7000000")
    assert await queries.open_debts(session) == []


async def test_tuzat_amount_removes_the_settling_payment_real_money_makes_redundant(
    session,
):
    _, debt = await _open_debt(session)
    await _real_payment(session, debt, "2000000")
    await records.mark_done(session, debt, by=records.BY_BUTTON)
    [_, synthetic] = await _payments(session, debt.id)
    assert synthetic.amount == Decimal("3000000")

    # 2 mln was really paid: a 2 mln debt needs no settling payment.
    await records.set_field(
        session, debt, "amount", (Decimal("2000000"), None), by=records.BY_COMMAND
    )

    fresh = await _reload(session, "debt", debt.id)
    assert [p.note for p in await _payments(session, debt.id)] == ["naqd"]
    assert fresh.status is DebtStatus.settled
    assert ("payment", "3000000.00", None) in _fields(fresh)

    # 2.5 mln: the settling payment would be 500 000, but it was removed,
    # so the row is now partially paid — and says so in its history.
    await records.set_field(
        session, fresh, "amount", (Decimal("2500000"), None), by=records.BY_COMMAND
    )
    fresh = await _reload(session, "debt", debt.id)
    assert fresh.status is DebtStatus.partially_paid and fresh.settled_at is None
    assert _fields(fresh)[-1] == ("status", "settled", "partially_paid")
    [balance] = await queries.open_debts(session)
    assert balance.outstanding == Decimal("500000")


async def test_tuzat_amount_below_the_real_payments_is_refused_untouched(session):
    _, debt = await _open_debt(session)
    await _real_payment(session, debt, "2000000")
    await records.mark_done(session, debt, by=records.BY_BUTTON)
    before = (await _reload(session, "debt", debt.id)).history

    with pytest.raises(records.PaymentsExceed) as caught:
        await records.set_field(
            session, debt, "amount", (Decimal("1000000"), None), by=records.BY_COMMAND
        )

    assert (caught.value.paid, caught.value.amount) == (
        Decimal("2000000"),
        Decimal("1000000"),
    )
    fresh = await _reload(session, "debt", debt.id)
    assert fresh.amount == Decimal("5000000") and fresh.history == before
    assert [p.amount for p in await _payments(session, debt.id)] == [
        Decimal("2000000"),
        Decimal("3000000"),
    ]


async def test_cmd_tuzat_explains_the_refusal_and_points_at_qaytar(bound):  # noqa: F811
    _, debt = await _open_debt(bound)
    await _real_payment(bound, debt, "2000000")
    message = _Message()

    await handlers.cmd_edit(message, _command("tuzat", f"d{debt.id} 1 mln"))

    [(text, _)] = message.sent
    assert "2 mln so'm" in text and "1 mln so'm" in text
    assert f"/qaytar d{debt.id}" in text
    assert (await _reload(bound, "debt", debt.id)).amount == Decimal("5000000")


# --- 2: no currency change over payments ----------------------------------------


async def test_currency_change_is_refused_while_any_payment_exists(session):
    _, debt = await _open_debt(session)
    await _real_payment(session, debt, "1000000")

    with pytest.raises(records.PaymentsExist):
        await records.set_field(
            session, debt, "currency", Currency.USD, by=records.BY_COMMAND
        )
    with pytest.raises(records.PaymentsExist):
        await records.set_field(
            session, debt, "amount", (Decimal("300"), Currency.USD), by=records.BY_COMMAND
        )
    fresh = await _reload(session, "debt", debt.id)
    assert fresh.currency is Currency.UZS and fresh.amount == Decimal("5000000")
    assert fresh.history == []
    [payment] = await _payments(session, debt.id)
    assert payment.currency is Currency.UZS

    # The same currency is not a change; the amount edit goes through.
    await records.set_field(
        session, debt, "amount", (Decimal("6000000"), Currency.UZS), by=records.BY_COMMAND
    )
    assert (await _reload(session, "debt", debt.id)).amount == Decimal("6000000")


async def test_currency_change_is_allowed_with_no_payments(session):
    _, debt = await _open_debt(session)
    await records.set_field(
        session, debt, "currency", Currency.USD, by=records.BY_COMMAND
    )
    fresh = await _reload(session, "debt", debt.id)
    assert fresh.currency is Currency.USD
    assert _fields(fresh) == [("currency", "UZS", "USD")]


async def test_cmd_tuzat_currency_over_payments_says_reopen_first(bound):  # noqa: F811
    _, debt = await _open_debt(bound)
    await _real_payment(bound, debt, "1000000")
    message = _Message()
    await handlers.cmd_edit(message, _command("tuzat", f"d{debt.id} usd"))
    [(text, _)] = message.sent
    assert text == replies.DEBT_CURRENCY_LOCKED.format(ref=f"d{debt.id}")
    assert (await _reload(bound, "debt", debt.id)).currency is Currency.UZS


# --- 3: ✅ twice is one payment --------------------------------------------------


async def test_mark_done_twice_writes_one_payment_and_says_already_done(session):
    _, debt = await _open_debt(session)
    await records.mark_done(session, debt, by=records.BY_BUTTON)

    with pytest.raises(records.NotOpen):
        await records.mark_done(session, debt, by=records.BY_BUTTON)

    payments = await _payments(session, debt.id)
    assert [p.amount for p in payments] == [Decimal("5000000")]
    assert len([e for e in debt.history if e["field"] == "payment"]) == 1


async def test_settle_balance_twice_writes_one_payment_per_row(session):
    person, first = await _open_debt(session)
    second = m.Debt(
        direction=first.direction,
        person_id=person.id,
        amount=Decimal("1000000"),
        currency=Currency.UZS,
    )
    session.add(second)
    await session.flush()

    changes = await records.settle_balance(session, first, by=records.BY_BUTTON)
    assert [c.record.id for c in changes] == [first.id, second.id]
    with pytest.raises(records.NotOpen):
        await records.settle_balance(session, first, by=records.BY_BUTTON)

    paid = await session.scalar(sa.select(sa.func.sum(m.DebtPayment.amount)))
    assert paid == Decimal("6000000")


async def test_a_double_tap_on_the_tick_gets_already_closed(bound):  # noqa: F811
    _, debt = await _open_debt(bound)
    message = _Message(reply_markup=keyboards.record_actions([("debt", debt.id)]))

    await handlers.on_record_button(_Callback(f"rec:d:d{debt.id}", message))
    await handlers.on_record_button(_Callback(f"rec:d:d{debt.id}", message))

    assert [t for t, _ in message.sent][1] == replies.RECORD_ALREADY_CLOSED
    assert [p.amount for p in await _payments(bound, debt.id)] == [Decimal("5000000")]


async def test_a_second_session_waits_for_the_row_lock_and_then_sees_it_settled(
    session,
):
    """Two ✅ taps in two processes: the second load blocks on the first
    transaction's row lock, and when it proceeds the row is already settled
    — no second payment."""
    _, debt = await _open_debt(session)
    await session.commit()
    debt_id = debt.id

    async with SessionLocal() as first, SessionLocal() as second:
        locked = await records.load(first, "debt", debt_id)
        waiting = asyncio.create_task(records.load(second, "debt", debt_id))
        await asyncio.sleep(0.3)
        assert not waiting.done()  # blocked on FOR UPDATE

        await records.mark_done(first, locked, by=records.BY_BUTTON)
        await first.commit()

        late = await asyncio.wait_for(waiting, timeout=5)
        assert late.status is DebtStatus.settled
        with pytest.raises(records.NotOpen):
            await records.mark_done(second, late, by=records.BY_BUTTON)
        await second.rollback()

    assert [p.amount for p in await _payments(session, debt_id)] == [Decimal("5000000")]


async def test_a_stale_row_object_is_re_read_under_the_lock(session):
    """mark_done on an object read before another writer settled the row."""
    _, debt = await _open_debt(session)
    await session.commit()
    async with SessionLocal() as other:
        fresh = await records.load(other, "debt", debt.id)
        await records.mark_done(other, fresh, by=records.BY_COMMAND)
        await other.commit()

    assert debt.status is DebtStatus.open  # the stale in-memory copy
    with pytest.raises(records.NotOpen):
        await records.mark_done(session, debt, by=records.BY_BUTTON)
    assert [p.amount for p in await _payments(session, debt.id)] == [Decimal("5000000")]


# --- 4: amounts by code and symbol -----------------------------------------------


@pytest.mark.parametrize(
    ("text", "amount", "currency"),
    [
        ("300 krw", Decimal("300"), Currency.KRW),
        ("300 KRW", Decimal("300"), Currency.KRW),
        ("₩300", Decimal("300"), Currency.KRW),
        ("300 ₩", Decimal("300"), Currency.KRW),
        ("300k", Decimal("300000"), None),
        ("300 k", Decimal("300000"), None),
        ("$300k", Decimal("300000"), Currency.USD),
        ("300k so'm", Decimal("300000"), Currency.UZS),
        ("300 usd", Decimal("300"), Currency.USD),
        ("$300", Decimal("300"), Currency.USD),
        ("300$", Decimal("300"), Currency.USD),
        ("300 cny", Decimal("300"), Currency.CNY),
        ("¥300", Decimal("300"), Currency.CNY),
        ("300 ¥", Decimal("300"), Currency.CNY),
        ("300 rub", Decimal("300"), Currency.RUB),
        ("₽300", Decimal("300"), Currency.RUB),
        ("300 ₽", Decimal("300"), Currency.RUB),
        ("300 uzs", Decimal("300"), Currency.UZS),
        ("300 so'm", Decimal("300"), Currency.UZS),
        ("5 mln", Decimal("5000000"), None),
        ("5mln so'm", Decimal("5000000"), Currency.UZS),
        ("5 ming", Decimal("5000"), None),
    ],
)
def test_parse_amount_by_code_and_symbol(text, amount, currency):
    assert records.parse_amount(text) == (amount, currency)


@pytest.mark.parametrize("text", ["300 kr", "300krw$", "300k$", "300 mlnn"])
def test_parse_amount_rejects_a_scale_glued_to_junk(text):
    assert records.parse_amount(text) is None


# --- 5: a bare prefix is usage --------------------------------------------------


@pytest.mark.parametrize("text", ["kim", "odam", "KIM ", "summa", "muddat", "sana"])
def test_a_prefix_with_nothing_after_it_is_none(text):
    assert records.parse_edit(text, today=TODAY) is None


async def test_cmd_tuzat_kim_alone_creates_nobody(bound):  # noqa: F811
    _, debt = await _open_debt(bound)
    message = _Message()
    await handlers.cmd_edit(message, _command("tuzat", f"d{debt.id} kim"))
    assert message.sent[0][0] == replies.TUZAT_USAGE
    names = set(await bound.scalars(sa.select(m.Person.display_name)))
    assert names == {"Akmal"}


# --- 6: a re-derived status is a history entry -----------------------------------


async def test_restating_the_status_after_an_amount_edit_is_recorded(session):
    _, debt = await _open_debt(session)
    await _real_payment(session, debt, "2000000")
    debt.status = DebtStatus.partially_paid
    await session.flush()

    await records.set_field(
        session, debt, "amount", (Decimal("2000000"), None), by=records.BY_COMMAND
    )
    fresh = await _reload(session, "debt", debt.id)
    assert _fields(fresh) == [
        ("amount", "5000000.00", "2000000.00"),
        ("status", "partially_paid", "settled"),
    ]
    assert fresh.settled_at is not None

    await records.set_field(
        session, fresh, "amount", (Decimal("3000000"), None), by=records.BY_COMMAND
    )
    fresh = await _reload(session, "debt", debt.id)
    assert _fields(fresh)[-1] == ("status", "settled", "partially_paid")
    assert fresh.settled_at is None

    # An edit that leaves the status alone writes no status entry.
    await records.set_field(
        session, fresh, "amount", (Decimal("4000000"), None), by=records.BY_COMMAND
    )
    fresh = await _reload(session, "debt", debt.id)
    assert _fields(fresh)[-1] == ("amount", "3000000.00", "4000000.00")


# --- 7: a refused reopen leaves no trace -----------------------------------------


async def test_reopen_on_a_debt_real_payments_cover_deletes_nothing(session):
    """The settling payment ✅ wrote and a later real repayment both sit on
    the row; the real one covers it, so /qaytar is refused — and the old
    code had already deleted the settling payment and written history
    before it noticed."""
    _, debt = await _open_debt(session)
    await _real_payment(session, debt, "2000000")
    await records.mark_done(session, debt, by=records.BY_BUTTON)
    await _real_payment(session, debt, "3000000", note="keyin")
    before = list((await _reload(session, "debt", debt.id)).history)
    ids = [p.id for p in await _payments(session, debt.id)]

    with pytest.raises(records.NotReopenable):
        await records.reopen(session, debt, by=records.BY_COMMAND)

    fresh = await _reload(session, "debt", debt.id)
    assert fresh.status is DebtStatus.settled and fresh.history == before
    assert [p.id for p in await _payments(session, debt.id)] == ids


# --- 8: a date is the whole value --------------------------------------------------


@pytest.mark.parametrize(
    "text", ["2026-09-20xyz", "20260920", "2026-09-20 x", "2026-9-2"]
)
def test_parse_date_rejects_trailing_junk_and_the_8_digit_form(text):
    assert records.parse_date(text, today=TODAY) is False


def test_parse_date_still_reads_a_trimmed_iso_date():
    assert records.parse_date(" 2026-09-20 ", today=TODAY) == date(2026, 9, 20)
    edit = records.parse_edit("2026-09-20xyz", today=TODAY)
    assert edit is None or edit.field != "due"


# --- 9: "Ha" on a balance ---------------------------------------------------------


async def test_ha_on_a_balance_with_one_settled_and_one_open_row_keeps_it_open(
    bound,  # noqa: F811
):
    person, first = await _open_debt(bound)
    second = m.Debt(
        direction=first.direction,
        person_id=person.id,
        amount=Decimal("1000000"),
        currency=Currency.UZS,
    )
    bound.add(second)
    await bound.flush()
    await records.mark_done(bound, first, by=records.BY_BUTTON)
    message = _Message(reply_markup=keyboards.question_actions([("debt", first.id)]))

    await handlers.on_record_button(_Callback(f"rec:o:d{first.id}", message))

    [(text, _)] = message.sent
    assert text != replies.RECORD_ALREADY_CLOSED
    assert "Ochiq qoladi" in text and f"<code>d{second.id}</code>" in text
    assert f"<code>d{first.id}</code>" not in text  # the settled row is not shown
    logged = await bound.scalar(sa.select(m.ReminderLog))
    assert (logged.kind, logged.ref) == ("ack:debt", reminders.ref_for("debt", first))


async def test_ha_when_every_row_of_the_balance_is_settled_says_closed(bound):  # noqa: F811
    _, debt = await _open_debt(bound)
    await records.mark_done(bound, debt, by=records.BY_BUTTON)
    message = _Message(reply_markup=keyboards.question_actions([("debt", debt.id)]))
    await handlers.on_record_button(_Callback(f"rec:o:d{debt.id}", message))
    assert message.sent[0][0] == replies.RECORD_ALREADY_CLOSED
    assert await bound.scalar(sa.select(sa.func.count()).select_from(m.ReminderLog)) == 0


# --- 10: whitespace after the ref -------------------------------------------------


@pytest.mark.parametrize("sep", ["\n", "\t", "  ", " \n "])
async def test_cmd_tuzat_accepts_any_whitespace_after_the_ref(bound, sep):  # noqa: F811
    _, debt = await _open_debt(bound)
    message = _Message()
    await handlers.cmd_edit(message, _command("tuzat", f"d{debt.id}{sep}6 mln"))
    assert "Tuzatildi" in message.sent[0][0]
    assert (await _reload(bound, "debt", debt.id)).amount == Decimal("6000000")


# --- 11: the hint is per kind --------------------------------------------------------


@pytest.mark.parametrize("kind", ["debt", "promise", "task"])
def test_the_tuzat_hint_only_shows_edits_the_kind_accepts(kind):
    hint = replies.tuzat_hint(kind, "x1")
    examples = replies._TUZAT_EXAMPLES[kind]
    assert examples  # every kind has at least one
    for example in examples:
        assert f"/tuzat x1 {example}" in hint
        edit = records.parse_edit(example, today=TODAY)
        assert edit is not None and edit.field in records.EDITABLE[kind], example
    assert ("teskari" in hint) == (kind == "debt")
    assert ("Sardor" in hint) == (kind != "task")


async def test_the_tuzat_button_on_a_task_does_not_offer_a_person(bound):  # noqa: F811
    task = await _task(bound)
    message = _Message(reply_markup=keyboards.record_actions([("task", task.id)]))
    await handlers.on_record_button(_Callback(f"rec:e:t{task.id}", message))
    [(text, _)] = message.sent
    assert f"/tuzat t{task.id} ertaga" in text
    assert "Sardor" not in text and "teskari" not in text and "mln" not in text


# --- 12: only shown questions are marked asked ----------------------------------------


def test_the_question_body_counts_what_fit_and_announces_the_rest():
    questions = [
        reminders.Question(
            "task", str(i), [("task", i)], m.Task(id=i, description="x" * 300)
        )
        for i in range(30)
    ]
    body, shown = replies.still_open_question_with_count(questions)
    assert 0 < shown < 30
    assert len(body) <= replies.TELEGRAM_LIMIT
    assert f"va yana {30 - shown} ta" in body
    assert body.endswith(replies.STILL_OPEN_HINT)
    assert replies.still_open_question(questions) == body

    body, shown = replies.still_open_question_with_count(questions[:3])
    assert shown == 3 and "va yana" not in body


async def test_mark_asked_logs_only_the_rendered_questions(session):
    questions = [reminders.Question("task", str(i), [("task", i)]) for i in range(3)]
    await reminders.mark_asked(
        session, reminders.DueBundle(questions=questions), rendered=2
    )
    await session.flush()
    refs = sorted(await session.scalars(sa.select(m.ReminderLog.ref)))
    assert refs == ["0", "1"]


# --- 13: dedupe by calendar day ------------------------------------------------


async def test_a_ping_one_second_after_yesterdays_sweep_does_not_suppress_today(session):
    day = datetime.combine(TODAY, time(15, 0), tzinfo=TZ)
    await _logged(
        session, "debt", "1:they_owe_me:UZS", day - timedelta(days=1, seconds=-1)
    )
    assert not await reminders._already_sent(session, "debt", "1:they_owe_me:UZS", day)

    await _logged(session, "debt", "1:they_owe_me:UZS", day.replace(hour=8))
    assert await reminders._already_sent(session, "debt", "1:they_owe_me:UZS", day)
    # And a ping late yesterday is yesterday's, whatever the hour.
    await _logged(
        session, "task", "3", day.replace(hour=23, minute=59) - timedelta(days=1)
    )
    assert not await reminders._already_sent(session, "task", "3", day.replace(hour=0))


async def test_the_escalation_lands_on_d_d1_and_d3_at_the_first_sweep(session):
    """Sweeps at :00 every hour (outside quiet hours), the ping logged one
    second after each sweep: pings on exactly due, due+1, due+3 — and each
    at the day's first sweep. The rolling 24h window pinged the same days
    but slipped D+1 to the 09:00 sweep, one second short of a day."""
    due = _today()
    _, promise = await _promise(session, "pul beradi", due=due)
    pinged: list[datetime] = []
    for day in range(6):
        for hour in range(8, 23):
            now = datetime.combine(due + timedelta(days=day), time(hour), tzinfo=TZ)
            bundle = await reminders.collect_due(session, now=now)
            if bundle.promises:
                pinged.append(now)
                for _, ref in bundle.keys:
                    await _logged(session, "promise", ref, now + timedelta(seconds=1))
    assert [p.date() for p in pinged] == [
        due,
        due + timedelta(days=1),
        due + timedelta(days=3),
    ]
    assert [p.hour for p in pinged] == [8, 8, 8]


# --- 14: a debt settled by earlier payments gets settled_at -----------------------------


async def test_a_debt_already_covered_by_payments_is_dated_when_settled(session):
    person, debt = await _open_debt(session)
    await _real_payment(session, debt, "5000000")  # covered, but never restated
    assert debt.status is DebtStatus.open and debt.settled_at is None
    interaction = await _interaction(session)

    applied = await apply_extraction(
        session,
        interaction,
        ex.ExtractionResult(
            debt_settlements=[
                ex.ExtractedSettlement(
                    person="Akmal",
                    amount=1_000_000,
                    currency="UZS",
                    direction="they_owe_me",
                )
            ]
        ),
    )

    fresh = await _reload(session, "debt", debt.id)
    assert fresh.status is DebtStatus.settled and fresh.settled_at is not None
    assert [
        d.id for d in (await queries.completed_on(session, _today())).settled_debts
    ] == [debt.id]
    # The new money had nothing left to land on.
    assert applied.unmatched_settlements == [("Akmal", Decimal("1000000"), Currency.UZS)]


# --- 15: distinct tokens -------------------------------------------------------------


async def test_a_repeated_one_word_hint_closes_nothing(session):
    _, promise = await _promise(session, "invoice yuboradi")
    applied = await apply_extraction(
        session,
        await _interaction(session),
        ex.ExtractionResult(fulfilments=[_fulfilment("invoice invoice")]),
    )
    assert applied.fulfilled == []
    assert applied.unmatched_fulfilments == [("Akmal", "invoice invoice")]
    assert (await _reload(session, "promise", promise.id)).status is PromiseStatus.open


# --- B3: ask before creating a person; a keyword typo is usage --------------------------


@pytest.mark.parametrize(
    "text",
    [
        "teskarii",
        "ertga",
        "muddatsizz",
        "bugn",
        "indingа",
        "mlnn",
        "kunn",
        "haftа",
        "usdd",
        "so'mm",
    ],
)
def test_a_keyword_typo_is_never_a_person(text):
    assert records.parse_edit(text, today=TODAY) is None


@pytest.mark.parametrize("text", ["Sardor", "Akmal aka", "To'lqin", "Абдулла", "Hafsa"])
def test_a_real_name_is_still_a_person_with_or_without_kim(text):
    assert records.parse_edit(f"kim {text}", today=TODAY) == records.Edit("person", text)
    if not records.looks_like_keyword_typo(text):
        assert records.parse_edit(text, today=TODAY) == records.Edit("person", text)


def test_kim_prefix_bypasses_the_typo_check():
    # "Hafsa" is one letter from "hafta": bare, it is refused; with the
    # prefix the owner has said what it is.
    assert records.looks_like_keyword_typo("Hafsa")
    assert records.parse_edit("Hafsa", today=TODAY) is None
    assert records.parse_edit("kim Hafsa", today=TODAY) == records.Edit("person", "Hafsa")


async def test_an_unknown_name_is_asked_about_and_only_ha_creates(bound):  # noqa: F811
    _, debt = await _open_debt(bound)
    message = _Message()

    await handlers.cmd_edit(message, _command("tuzat", f"d{debt.id} Sardor"))

    [(text, markup)] = message.sent
    assert text == replies.new_person_question("Sardor")
    assert "Sardor" in text and "yaratilsinmi" in text
    fresh = await _reload(bound, "debt", debt.id)
    [entry] = fresh.history
    assert entry["field"] == records.PENDING_PERSON_FIELD and entry["name"] == "Sardor"
    index = len(fresh.history) - 1
    assert _payloads(markup) == [
        [f"rec:py:d{debt.id}:{index}", f"rec:pn:d{debt.id}:{index}"]
    ]
    assert [b.text for b in markup.inline_keyboard[0]] == ["Ha", "Yo'q"]
    names = set(await bound.scalars(sa.select(m.Person.display_name)))
    assert names == {"Akmal"}  # nothing created yet

    question = _Question(reply_markup=markup)
    await handlers.on_record_button(_Callback(f"rec:py:d{debt.id}:{index}", question))

    assert "Tuzatildi" in question.edited_text and "Sardor" in question.edited_text
    fresh = await _reload(bound, "debt", debt.id)
    assert fresh.person.display_name == "Sardor"
    assert fresh.history[-1]["field"] == "person" and fresh.history[-1]["by"] == "button"
    names = set(await bound.scalars(sa.select(m.Person.display_name)))
    assert names == {"Akmal", "Sardor"}


async def test_yoq_creates_nobody_and_changes_nothing(bound):  # noqa: F811
    _, promise = await _promise(bound)
    message = _Message()
    await handlers.cmd_edit(message, _command("tuzat", f"p{promise.id} Bobur"))
    [(_, markup)] = message.sent
    index = len((await _reload(bound, "promise", promise.id)).history) - 1

    question = _Question(reply_markup=markup)
    await handlers.on_record_button(_Callback(f"rec:pn:p{promise.id}:{index}", question))

    assert question.edited_text == replies.NEW_PERSON_DECLINED.format(name="Bobur")
    fresh = await _reload(bound, "promise", promise.id)
    assert fresh.person.display_name == "Akmal"
    assert [e["field"] for e in fresh.history] == [records.PENDING_PERSON_FIELD]
    names = set(await bound.scalars(sa.select(m.Person.display_name)))
    assert names == {"Akmal"}


async def test_a_stale_or_forged_new_person_button_does_nothing(bound):  # noqa: F811
    _, debt = await _open_debt(bound)
    message = _Question()
    await handlers.on_record_button(_Callback(f"rec:py:d{debt.id}:0", message))
    assert message.edited_text == replies.NEW_PERSON_EXPIRED
    forged = _Callback(f"rec:py:d{debt.id}:x", message)
    await handlers.on_record_button(forged)
    assert forged.answered and message.sent == []
    assert (await _reload(bound, "debt", debt.id)).person.display_name == "Akmal"
    names = set(await bound.scalars(sa.select(m.Person.display_name)))
    assert names == {"Akmal"}


def test_the_new_person_payload_fits_64_bytes_whatever_the_name():
    long_name = "Абдурахмон Абдуллаевич Мухаммаджонов"
    index = records.ask_new_person(m.Debt(history=[]), long_name, by=records.BY_COMMAND)
    markup = keyboards.new_person_question("d999999999", index)
    for button in markup.inline_keyboard[0]:
        assert len(button.callback_data.encode()) <= 64


async def test_a_known_name_still_attaches_without_a_question(bound):  # noqa: F811
    _, debt = await _open_debt(bound)
    await resolve_person(bound, "Sardor")
    await bound.flush()
    message = _Message()
    await handlers.cmd_edit(message, _command("tuzat", f"d{debt.id} Sardor aka"))
    [(text, markup)] = message.sent
    assert "Tuzatildi" in text and markup is None
    assert (await _reload(bound, "debt", debt.id)).person.display_name == "Sardor"


# --- the invariant, stated once -------------------------------------------------------


async def test_payments_never_sum_above_the_amount_through_every_path(session):
    _, debt = await _open_debt(session)
    await _real_payment(session, debt, "1000000")
    await records.mark_done(session, debt, by=records.BY_BUTTON)
    with pytest.raises(records.NotOpen):
        await records.mark_done(session, debt, by=records.BY_BUTTON)
    await records.set_field(
        session, debt, "amount", (Decimal("3000000"), None), by=records.BY_COMMAND
    )
    await records.reopen(session, debt, by=records.BY_COMMAND)
    await records.set_field(
        session, debt, "amount", (Decimal("2000000"), None), by=records.BY_COMMAND
    )
    await records.mark_done(session, debt, by=records.BY_COMMAND)
    fresh = await _reload(session, "debt", debt.id)
    assert (
        await records.paid_against(session, fresh) == fresh.amount == Decimal("2000000")
    )
    # Every mutation above left an entry, and all of them are readable.
    assert len(fresh.history) >= 8
    assert all({"at", "field", "old", "new", "by"} <= set(e) for e in fresh.history)
    assert fresh.direction is DebtDirection.they_owe_me

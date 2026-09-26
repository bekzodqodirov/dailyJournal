"""Build step 1: the owner can close and correct any record.

Refs on every listed line, the ✅ / ✏️ / 🔄 buttons, `/bajarildi`, `/yop`,
`/tuzat`, fulfilment from extraction, escalating-then-quiet reminders, and a
"Bajarilganlar" section that finally lists a kept promise.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from aiogram.filters import CommandObject

from miya.bot import formatting as f
from miya.bot import handlers, keyboards, replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import (
    Currency,
    DebtDirection,
    DebtStatus,
    PromiseMadeBy,
    PromiseStatus,
    TaskPriority,
    TaskStatus,
)
from miya.services import extraction as ex
from miya.services import queries, records, reminders, reports
from miya.services.people import resolve_person
from miya.services.persistence import Applied, apply_extraction
from miya.worker import main as worker
from tests.test_pipeline import _interaction, _open_debt

TZ = settings.tz


def _now() -> datetime:
    return datetime.now(TZ)


def _today() -> date:
    return _now().date()


# --- refs ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("d12", ("debt", 12)),
        ("P7", ("promise", 7)),
        (" #t3 ", ("task", 3)),
        ("x1", ("transaction", 1)),
        ("y1", None),
        ("d", None),
        ("12", None),
        ("", None),
    ],
)
def test_parse_ref_reads_the_handles_the_lists_show(text, expected):
    assert f.parse_ref(text) == expected


def test_ref_round_trips_and_hides_an_unsaved_row():
    assert f.ref("debt", 12) == "d12"
    assert f.parse_ref(f.ref("task", 3)) == ("task", 3)
    assert f.ref("promise", None) == ""
    assert f.tag("promise", None) == ""
    assert f.tags("debt", [12, 15]) == " <code>d12, d15</code>"


def test_every_list_line_carries_its_ref():
    person = m.Person(id=1, display_name="Akmal", aliases=[])
    balance = queries.DebtBalance(
        person=person,
        direction=DebtDirection.they_owe_me,
        currency=Currency.UZS,
        outstanding=Decimal("5000000"),
        earliest_due=_today(),
        count=2,
        ids=[12, 15],
    )
    promise = m.Promise(
        id=7,
        made_by=PromiseMadeBy.them,
        description="invoice yuboradi",
        due_date=_today(),
    )
    task = m.Task(id=3, description="Konteynerni tekshirish", due_date=_today())

    assert "d12, d15" in replies.debts_report([balance])
    assert "<code>p7</code>" in replies.promises_report([(promise, person)])
    reminder = replies.reminder([balance], [(promise, person)], [task], [])
    assert "d12, d15" in reminder and "<code>p7</code>" in reminder
    assert "<code>t3</code>" in reminder


def test_the_confirmation_shows_refs_and_knows_which_rows_its_buttons_hit():
    applied = Applied(
        debts=[
            m.Debt(
                id=12,
                direction=DebtDirection.they_owe_me,
                amount=Decimal("5000000"),
                currency=Currency.UZS,
            )
        ],
        promises=[m.Promise(id=7, made_by=PromiseMadeBy.me, description="pul beraman")],
        tasks=[m.Task(id=3, description="hujjat", priority=TaskPriority.med)],
    )
    text = replies.confirmation(applied)
    assert "<code>d12</code>" in text and "<code>p7</code>" in text
    assert "<code>t3</code>" in text
    assert replies.confirmation_refs(applied) == [
        ("debt", 12),
        ("promise", 7),
        ("task", 3),
    ]


def test_help_teaches_the_three_commands():
    assert "/bajarildi" in replies.HELP
    assert "/yop" in replies.HELP
    assert "/tuzat" in replies.HELP
    assert "d12" in replies.HELP


# --- keyboards -------------------------------------------------------------


def _payloads(markup) -> list[list[str]]:
    return [[b.callback_data for b in row] for row in markup.inline_keyboard]


def test_a_single_record_gets_plain_labels_and_a_debt_gets_teskari():
    markup = keyboards.record_actions([("debt", 12)])
    [row] = markup.inline_keyboard
    assert [b.text for b in row] == ["✅ Bajarildi", "✏️ Tuzat", "🔄 Teskari"]
    assert _payloads(markup) == [["rec:d:d12", "rec:e:d12", "rec:f:d12"]]


def test_several_records_are_labelled_and_only_debts_flip():
    markup = keyboards.record_actions([("promise", 7), ("task", 3)])
    texts = [[b.text for b in row] for row in markup.inline_keyboard]
    assert texts == [["✅ Bajarildi p7", "✏️ Tuzat p7"], ["✅ Bajarildi t3", "✏️ Tuzat t3"]]


def test_nothing_to_act_on_means_no_keyboard():
    assert keyboards.record_actions([]) is None
    assert keyboards.record_actions([("promise", None)]) is None


def test_the_question_row_offers_ha_bajarildi_yop():
    markup = keyboards.question_actions([("task", 3)])
    assert _payloads(markup) == [["rec:o:t3", "rec:d:t3", "rec:c:t3"]]


def test_a_finished_row_leaves_the_keyboard_and_the_rest_stay():
    markup = keyboards.record_actions([("debt", 12), ("promise", 7)])
    trimmed = keyboards.without(markup, "d12")
    assert _payloads(trimmed) == [["rec:d:p7", "rec:e:p7"]]
    assert keyboards.without(trimmed, "p7") is None


def test_every_callback_payload_fits_telegrams_64_bytes():
    markup = keyboards.record_actions([("debt", 999_999_999)])
    for row in markup.inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode()) <= 64


# --- /tuzat parsing --------------------------------------------------------

TODAY = date(2026, 9, 15)  # a Tuesday


@pytest.mark.parametrize(
    ("text", "field", "value"),
    [
        ("teskari", "direction", None),
        ("6 mln", "amount", (Decimal("6000000"), None)),
        ("5.5 mln so'm", "amount", (Decimal("5500000"), Currency.UZS)),
        ("300 $", "amount", (Decimal("300"), Currency.USD)),
        ("$300", "amount", (Decimal("300"), Currency.USD)),
        ("500 ming", "amount", (Decimal("500000"), None)),
        ("1200000", "amount", (Decimal("1200000"), None)),
        ("usd", "currency", Currency.USD),
        ("dollar", "currency", Currency.USD),
        ("2026-10-01", "due", date(2026, 10, 1)),
        ("ertaga", "due", date(2026, 9, 16)),
        ("indinga", "due", date(2026, 9, 17)),
        ("juma", "due", date(2026, 9, 18)),
        ("seshanba", "due", date(2026, 9, 22)),  # today is Tuesday: next one
        ("3 kun", "due", date(2026, 9, 18)),
        ("2 hafta", "due", date(2026, 9, 29)),
        ("muddatsiz", "due", None),
        ("Sardor", "person", "Sardor"),
        ("Akmal aka", "person", "Akmal aka"),
        ("kim Juma", "person", "Juma"),
        ("muddat juma", "due", date(2026, 9, 18)),
        ("summa 2 mln", "amount", (Decimal("2000000"), None)),
    ],
)
def test_parse_edit_reads_what_the_owner_types(text, field, value):
    edit = records.parse_edit(text, today=TODAY)
    assert edit is not None
    assert (edit.field, edit.value) == (field, value)


def test_parse_edit_rejects_nothing_and_nonsense_amounts():
    assert records.parse_edit("", today=TODAY) is None
    assert records.parse_edit("summa abc", today=TODAY) is None
    assert records.parse_amount("0") is None
    assert records.parse_amount("5 xyz") is None


# --- the record service ----------------------------------------------------


async def _promise(session, description="invoice yuboradi", *, due=None, made_by=None):
    person = await resolve_person(session, "Akmal")
    interaction = await _interaction(session)
    promise = m.Promise(
        made_by=made_by or PromiseMadeBy.them,
        person_id=person.id,
        description=description,
        due_date=due,
        source_interaction_id=interaction.id,
    )
    session.add(promise)
    await session.flush()
    return person, promise


async def _task(session, description="Konteynerni tekshirish", *, due=None):
    task = m.Task(description=description, due_date=due)
    session.add(task)
    await session.flush()
    return task


async def _reload(session, kind, record_id):
    """The row as the database has it now, with its person loaded."""
    current = await session.get(records.MODEL_OF[kind], record_id)
    if current is not None:
        session.expire(current)
    return await records.load(session, kind, record_id)


async def test_bajarildi_closes_a_promise_with_a_timestamp_and_a_history_entry(session):
    _, promise = await _promise(session)
    found = await records.find(session, f"p{promise.id}")
    assert found is not None

    change = await records.mark_done(session, found[1], by=records.BY_COMMAND)

    fresh = await _reload(session, "promise", promise.id)
    assert fresh.status is PromiseStatus.done
    assert fresh.completed_at is not None
    assert change.old == "open" and change.new == "done"
    [entry] = fresh.history
    assert entry["field"] == "status" and entry["by"] == "command"
    assert entry["old"] == "open" and entry["new"] == "done"


async def test_bajarildi_closes_a_task(session):
    task = await _task(session)
    await records.mark_done(session, task, by=records.BY_BUTTON)
    fresh = await _reload(session, "task", task.id)
    assert fresh.status is TaskStatus.done and fresh.completed_at is not None


async def test_bajarildi_settles_a_debt_in_full_with_a_payment_row(session):
    _, debt = await _open_debt(session)
    session.add(
        m.DebtPayment(debt_id=debt.id, amount=Decimal("2000000"), currency=Currency.UZS)
    )
    await session.flush()

    await records.mark_done(session, debt, by=records.BY_COMMAND)

    fresh = await _reload(session, "debt", debt.id)
    assert fresh.status is DebtStatus.settled and fresh.settled_at is not None
    paid = await session.scalar(
        sa.select(sa.func.sum(m.DebtPayment.amount)).where(
            m.DebtPayment.debt_id == debt.id
        )
    )
    assert paid == Decimal("5000000")  # the books stay additive
    assert await queries.open_debts(session) == []


async def test_a_closed_record_cannot_be_closed_twice(session):
    _, promise = await _promise(session)
    await records.mark_done(session, promise, by=records.BY_COMMAND)
    with pytest.raises(records.NotOpen):
        await records.mark_done(session, promise, by=records.BY_COMMAND)
    with pytest.raises(records.NotOpen):
        await records.close(session, promise, by=records.BY_COMMAND)


async def test_yop_closes_without_counting_as_kept(session):
    _, promise = await _promise(session)
    task = await _task(session)

    await records.close(session, promise, by=records.BY_COMMAND)
    await records.close(session, task, by=records.BY_BUTTON)

    promise = await _reload(session, "promise", promise.id)
    task = await _reload(session, "task", task.id)
    assert promise.status is PromiseStatus.cancelled and promise.completed_at is None
    assert task.status is TaskStatus.dropped and task.completed_at is None
    # Neither shows up as kept.
    done = await queries.completed_on(session, _today())
    assert done.done_promises == [] and done.done_tasks == []
    # And neither is open any more.
    assert await queries.open_promises(session) == []


async def test_yop_refuses_a_debt(session):
    _, debt = await _open_debt(session)
    with pytest.raises(records.NotClosable):
        await records.close(session, debt, by=records.BY_COMMAND)
    assert (await _reload(session, "debt", debt.id)).status is DebtStatus.open


async def test_teskari_flips_who_owes_whom_and_remembers_it(session):
    _, debt = await _open_debt(session)
    await records.flip(session, debt, by=records.BY_BUTTON)
    fresh = await _reload(session, "debt", debt.id)
    assert fresh.direction is DebtDirection.i_owe_them
    assert fresh.history[-1] == {
        **fresh.history[-1],
        "field": "direction",
        "old": "they_owe_me",
        "new": "i_owe_them",
        "by": "button",
    }
    await records.flip(session, fresh, by=records.BY_BUTTON)
    assert (
        await _reload(session, "debt", debt.id)
    ).direction is DebtDirection.they_owe_me


async def test_tuzat_amount_keeps_the_old_value_and_restates_the_status(session):
    _, debt = await _open_debt(session)
    session.add(
        m.DebtPayment(debt_id=debt.id, amount=Decimal("2000000"), currency=Currency.UZS)
    )
    await session.flush()

    change = await records.set_field(
        session, debt, "amount", (Decimal("2000000"), None), by=records.BY_COMMAND
    )
    fresh = await _reload(session, "debt", debt.id)
    assert fresh.amount == Decimal("2000000")
    assert fresh.status is DebtStatus.settled  # fully covered by what was paid
    assert change.old == "5000000.00" and change.new == "2000000.00"

    # The status change is itself a history entry, not a silent rewrite.
    fields = [entry["field"] for entry in fresh.history]
    assert fields == ["amount", "status"]

    # A currency change while a payment is on the books is refused: the
    # payment would keep the old currency and the balance sums without
    # converting.
    with pytest.raises(records.PaymentsExist):
        await records.set_field(
            session,
            fresh,
            "amount",
            (Decimal("300"), Currency.USD),
            by=records.BY_COMMAND,
        )
    fresh = await _reload(session, "debt", debt.id)
    assert fresh.currency is Currency.UZS and fresh.amount == Decimal("2000000")
    assert [entry["field"] for entry in fresh.history] == ["amount", "status"]


async def test_tuzat_person_uses_a_known_name_and_asks_before_creating_one(session):
    _, debt = await _open_debt(session)
    await resolve_person(session, "Sardor")
    await session.flush()

    await records.set_field(session, debt, "person", "Sardor aka", by=records.BY_COMMAND)
    fresh = await _reload(session, "debt", debt.id)
    assert fresh.person.display_name == "Sardor"

    # An unknown name is a question, not a new row (owner: ask first).
    with pytest.raises(records.UnknownPerson):
        await records.set_field(session, fresh, "person", "Bobur", by=records.BY_COMMAND)
    names = set(await session.scalars(sa.select(m.Person.display_name)))
    assert names == {"Akmal", "Sardor"}

    # "Ha" is the create.
    await records.set_field(
        session, fresh, "person", "Bobur", by=records.BY_BUTTON, create_person=True
    )
    fresh = await _reload(session, "debt", debt.id)
    assert fresh.person.display_name == "Bobur"
    names = set(await session.scalars(sa.select(m.Person.display_name)))
    assert names == {"Akmal", "Sardor", "Bobur"}


async def test_tuzat_due_date_on_every_kind_and_clearing_it(session):
    _, debt = await _open_debt(session, due=_today())
    _, promise = await _promise(session)
    task = await _task(session)
    tomorrow = _today() + timedelta(days=1)

    await records.set_field(session, debt, "due", tomorrow, by=records.BY_COMMAND)
    await records.set_field(session, promise, "due", tomorrow, by=records.BY_COMMAND)
    await records.set_field(session, task, "due", None, by=records.BY_COMMAND)

    assert (await _reload(session, "debt", debt.id)).due_date == tomorrow
    assert (await _reload(session, "promise", promise.id)).due_date == tomorrow
    assert (await _reload(session, "task", task.id)).due_date is None


async def test_a_task_has_no_person_and_a_promise_has_no_amount(session):
    _, promise = await _promise(session)
    task = await _task(session)
    with pytest.raises(records.NotEditable):
        await records.set_field(session, task, "person", "Akmal", by=records.BY_COMMAND)
    with pytest.raises(records.NotEditable):
        await records.set_field(
            session, promise, "amount", (Decimal("1"), None), by=records.BY_COMMAND
        )


async def test_an_unknown_ref_is_not_found(session):
    assert await records.find(session, "d999999") is None
    assert await records.find(session, "nonsense") is None


# --- the commands and buttons, through the handlers -------------------------


class _Message:
    """What the handler sends back, and the keyboard it edits."""

    def __init__(self, reply_markup=None) -> None:
        self.sent: list[tuple[str, object]] = []
        self.reply_markup = reply_markup
        self.edited_markup = "untouched"

    async def answer(self, text, **kwargs):
        self.sent.append((text, kwargs.get("reply_markup")))

    async def edit_reply_markup(self, reply_markup=None):
        self.edited_markup = reply_markup


class _Callback:
    def __init__(self, data: str, message: _Message) -> None:
        self.data = data
        self.message = message
        self.answered = False

    async def answer(self, *args, **kwargs):
        self.answered = True


def _command(name: str, args: str) -> CommandObject:
    return CommandObject(prefix="/", command=name, args=args or None)


@pytest.fixture
def bound(session, monkeypatch):
    """Handlers run inside the test session instead of opening their own."""

    @asynccontextmanager
    async def _scope():
        yield session
        await session.flush()

    monkeypatch.setattr(handlers, "session_scope", _scope)
    return session


async def test_cmd_bajarildi_replies_with_the_closed_line(bound):
    _, promise = await _promise(bound, "invoice yuboradi")
    message = _Message()

    await handlers.cmd_done(message, _command("bajarildi", f"p{promise.id}"))

    [(text, _)] = message.sent
    assert "Bajarildi" in text and "invoice yuboradi" in text
    assert f"<code>p{promise.id}</code>" in text
    assert (await _reload(bound, "promise", promise.id)).status is PromiseStatus.done


async def test_cmd_bajarildi_without_a_ref_explains_the_handles(bound):
    message = _Message()
    await handlers.cmd_done(message, _command("bajarildi", ""))
    [(text, _)] = message.sent
    assert "d12" in text and "p7" in text


async def test_cmd_yop_on_a_debt_points_to_the_right_commands(bound):
    _, debt = await _open_debt(bound)
    message = _Message()
    await handlers.cmd_close(message, _command("yop", f"d{debt.id}"))
    [(text, _)] = message.sent
    assert "/bajarildi" in text and "/tuzat" in text
    assert (await _reload(bound, "debt", debt.id)).status is DebtStatus.open


async def test_cmd_tuzat_edits_one_field_and_shows_the_corrected_line(bound):
    _, debt = await _open_debt(bound)
    message = _Message()

    await handlers.cmd_edit(message, _command("tuzat", f"d{debt.id} 6 mln"))

    [(text, _)] = message.sent
    assert "Tuzatildi" in text and "6 mln so'm" in text
    fresh = await _reload(bound, "debt", debt.id)
    assert fresh.amount == Decimal("6000000")
    assert fresh.history[-1]["old"] == "5000000.00"


async def test_cmd_tuzat_with_a_bad_ref_or_no_value_never_touches_anything(bound):
    _, debt = await _open_debt(bound)
    message = _Message()
    await handlers.cmd_edit(message, _command("tuzat", f"d{debt.id}"))
    await handlers.cmd_edit(message, _command("tuzat", "d999999 6 mln"))
    assert "/tuzat" in message.sent[0][0]
    assert "topilmadi" in message.sent[1][0]
    fresh = await _reload(bound, "debt", debt.id)
    assert fresh.amount == Decimal("5000000") and fresh.history == []


async def test_the_done_button_closes_the_row_and_drops_only_its_buttons(bound):
    _, debt = await _open_debt(bound)
    _, promise = await _promise(bound)
    markup = keyboards.record_actions([("debt", debt.id), ("promise", promise.id)])
    message = _Message(reply_markup=markup)
    callback = _Callback(f"rec:d:p{promise.id}", message)

    await handlers.on_record_button(callback)

    assert (await _reload(bound, "promise", promise.id)).status is PromiseStatus.done
    assert callback.answered
    [(text, _)] = message.sent
    assert "Bajarildi" in text
    assert _payloads(message.edited_markup) == [
        [f"rec:d:d{debt.id}", f"rec:e:d{debt.id}", f"rec:f:d{debt.id}"]
    ]


async def test_the_teskari_button_flips_and_keeps_the_buttons(bound):
    _, debt = await _open_debt(bound)
    message = _Message(reply_markup=keyboards.record_actions([("debt", debt.id)]))

    await handlers.on_record_button(_Callback(f"rec:f:d{debt.id}", message))

    assert (await _reload(bound, "debt", debt.id)).direction is DebtDirection.i_owe_them
    [(text, _)] = message.sent
    assert "← sen" in text
    assert message.edited_markup == "untouched"


async def test_the_tuzat_button_teaches_the_syntax_with_the_ref_filled_in(bound):
    _, debt = await _open_debt(bound)
    message = _Message(reply_markup=keyboards.record_actions([("debt", debt.id)]))
    await handlers.on_record_button(_Callback(f"rec:e:d{debt.id}", message))
    [(text, _)] = message.sent
    assert f"/tuzat d{debt.id} teskari" in text
    assert (await _reload(bound, "debt", debt.id)).history == []


async def test_a_stale_button_says_the_row_is_gone(bound):
    message = _Message(reply_markup=keyboards.record_actions([("task", 424242)]))
    await handlers.on_record_button(_Callback("rec:d:t424242", message))
    [(text, _)] = message.sent
    assert "topilmadi" in text


async def test_the_ha_button_acknowledges_and_a_note_gets_buttons(bound):
    task = await _task(bound)
    message = _Message(reply_markup=keyboards.question_actions([("task", task.id)]))

    await handlers.on_record_button(_Callback(f"rec:o:t{task.id}", message))

    assert (await _reload(bound, "task", task.id)).status is TaskStatus.todo
    logged = await bound.scalar(sa.select(m.ReminderLog))
    assert (logged.kind, logged.ref) == ("ack:task", str(task.id))
    assert message.edited_markup is None
    assert "bir hafta" in message.sent[0][0]


async def test_a_note_is_confirmed_with_its_buttons(bound, monkeypatch):
    _, promise = await _promise(bound)

    async def _process(session, interaction):
        return SimpleNamespace(ok=True, applied=Applied(promises=[promise]))

    monkeypatch.setattr(handlers, "process_interaction", _process)
    message = SimpleNamespace(
        text="Akmal ertaga invoice yuboradi",
        date=_now(),
        bot=SimpleNamespace(send_chat_action=None),
        chat=SimpleNamespace(id=1),
        sent=[],
    )

    async def _answer(text, **kwargs):
        message.sent.append((text, kwargs.get("reply_markup")))

    message.answer = _answer
    monkeypatch.setattr(handlers.rag, "looks_like_question", lambda text: False)

    await handlers.on_text(message)

    [(text, markup)] = message.sent
    assert f"<code>p{promise.id}</code>" in text
    assert _payloads(markup) == [[f"rec:d:p{promise.id}", f"rec:e:p{promise.id}"]]


# --- fulfilment from extraction -------------------------------------------


def test_the_extraction_schema_carries_fulfilments():
    result = ex.parse_extraction_json(
        '{"summary":"","fulfilments":'
        '[{"made_by":"them","person":"Akmal","description":"invoice yubordi"}]}'
    )
    assert result is not None
    assert result.fulfilments[0].person == "Akmal"
    assert not result.is_empty()
    schema = ex.extraction_output_config()["format"]["schema"]
    assert "fulfilments" in schema["properties"]
    assert "fulfilments" in ex.EXTRACTION_SYSTEM_PROMPT


async def test_a_fulfilment_closes_the_one_promise_it_clearly_matches(session):
    person, promise = await _promise(session, "invoice yuboradi")
    _, other = await _promise(session, "konteynerni jo'natadi")
    interaction = await _interaction(session, "Akmal invoice yubordi")

    applied = await apply_extraction(
        session,
        interaction,
        ex.ExtractionResult(
            fulfilments=[
                ex.ExtractedFulfilment(
                    made_by="them", person="Akmal", description="invoice yubordi"
                )
            ]
        ),
    )

    assert [p.id for p, _ in applied.fulfilled] == [promise.id]
    assert applied.unmatched_fulfilments == []
    assert (await _reload(session, "promise", promise.id)).status is PromiseStatus.done
    assert (await _reload(session, "promise", other.id)).status is PromiseStatus.open
    assert (await _reload(session, "promise", promise.id)).history[-1][
        "by"
    ] == "extraction"
    text = replies.confirmation(applied)
    assert "Va'da bajarildi" in text and "invoice yuboradi" in text


async def test_an_ambiguous_fulfilment_closes_nothing_and_says_so(session):
    # Two invoices promised: "invoice yubordi" fits both equally well.
    _, first = await _promise(session, "GZ invoice yuboradi")
    _, second = await _promise(session, "Shanghai invoice yuboradi")
    interaction = await _interaction(session)

    applied = await apply_extraction(
        session,
        interaction,
        ex.ExtractionResult(
            fulfilments=[
                ex.ExtractedFulfilment(
                    made_by="them", person="Akmal", description="invoice yubordi"
                )
            ]
        ),
    )

    assert applied.fulfilled == []
    assert applied.unmatched_fulfilments == [("Akmal", "invoice yubordi")]
    for promise in (first, second):
        assert (
            await _reload(session, "promise", promise.id)
        ).status is PromiseStatus.open
    assert "aniq emas" in replies.confirmation(applied)


async def test_a_fulfilment_for_an_unknown_person_creates_nobody(session):
    interaction = await _interaction(session)
    applied = await apply_extraction(
        session,
        interaction,
        ex.ExtractionResult(
            fulfilments=[
                ex.ExtractedFulfilment(
                    made_by="them", person="Nomalum", description="x qildi"
                )
            ]
        ),
    )
    assert applied.unmatched_fulfilments == [("Nomalum", "x qildi")]
    assert await session.scalar(sa.select(sa.func.count()).select_from(m.Person)) == 0


async def test_a_fulfilment_never_closes_the_promise_made_in_the_same_message(session):
    await resolve_person(session, "Akmal")
    interaction = await _interaction(session)
    applied = await apply_extraction(
        session,
        interaction,
        ex.ExtractionResult(
            promises=[
                ex.ExtractedPromise(
                    made_by="them", person="Akmal", description="invoice yuboradi"
                )
            ],
            fulfilments=[
                ex.ExtractedFulfilment(
                    made_by="them", person="Akmal", description="invoice yubordi"
                )
            ],
        ),
    )
    [promise] = applied.promises
    assert (await _reload(session, "promise", promise.id)).status is PromiseStatus.open


# --- reminders: escalate, ask once, go quiet ---------------------------------

DUE = date(2026, 9, 15)


def _at(day_offset: int, hour: int = 10) -> datetime:
    return datetime.combine(DUE, datetime.min.time(), tzinfo=TZ) + timedelta(
        days=day_offset, hours=hour
    )


def _decide(history, *, now, due=DUE, created_at=None):
    return reminders.decide("promise", history, due=due, created_at=created_at, now=now)


def test_a_dated_item_is_pinged_on_the_day_then_plus_one_then_plus_three():
    assert _decide([], now=_at(-1)) is None  # not yet due
    assert _decide([], now=_at(0)) == reminders.PING
    pinged = [("promise", _at(0))]
    assert _decide(pinged, now=_at(0, hour=15)) is None
    assert _decide(pinged, now=_at(1)) == reminders.PING
    pinged.append(("promise", _at(1)))
    assert _decide(pinged, now=_at(2)) is None
    assert _decide(pinged, now=_at(3)) == reminders.PING


def test_after_the_third_ping_it_is_asked_and_an_unanswered_question_repeats_weekly():
    """The owner's decision: re-remind after one week. A missed question is
    asked again, not buried — the old "quiet until answered" was silence
    forever."""
    pinged = [("promise", _at(0)), ("promise", _at(1)), ("promise", _at(3))]
    assert _decide(pinged, now=_at(4)) is None
    assert _decide(pinged, now=_at(7)) == reminders.ASK
    asked = [*pinged, ("ask:promise", _at(7))]
    assert _decide(asked, now=_at(8)) is None
    assert _decide(asked, now=_at(13)) is None  # quiet for the week
    assert _decide(asked, now=_at(14)) == reminders.ASK  # then asked again
    asked.append(("ask:promise", _at(14)))
    assert _decide(asked, now=_at(20)) is None
    assert _decide(asked, now=_at(21)) == reminders.ASK


def test_ha_keeps_it_open_and_asks_again_a_week_later():
    history = [
        ("promise", _at(0)),
        ("promise", _at(1)),
        ("promise", _at(3)),
        ("ask:promise", _at(7)),
        ("ack:promise", _at(8)),
    ]
    assert _decide(history, now=_at(14)) is None
    assert _decide(history, now=_at(15)) == reminders.ASK
    history.append(("ask:promise", _at(15)))
    assert _decide(history, now=_at(21)) is None
    assert _decide(history, now=_at(22)) == reminders.ASK  # unanswered: weekly


def test_pings_from_an_earlier_due_date_do_not_count_after_tuzat_moves_it():
    """/tuzat p7 ertaga restarts the schedule from the new date."""
    old_pings = [("promise", _at(-10)), ("promise", _at(-9)), ("promise", _at(-7))]
    assert _decide(old_pings, now=_at(0)) == reminders.PING


def test_an_undated_item_is_nudged_a_week_after_creation_then_weekly():
    created = _at(-7)
    assert _decide([], now=_at(-1), due=None, created_at=created) is None
    assert _decide([], now=_at(0), due=None, created_at=created) == reminders.ASK
    asked = [("ask:promise", _at(0))]
    assert _decide(asked, now=_at(6), due=None, created_at=created) is None
    assert _decide(asked, now=_at(7), due=None, created_at=created) == reminders.ASK
    acked = [*asked, ("ack:promise", _at(1))]
    assert _decide(acked, now=_at(7), due=None, created_at=created) is None
    assert _decide(acked, now=_at(8), due=None, created_at=created) == reminders.ASK


async def _logged(session, kind, ref, when):
    session.add(m.ReminderLog(kind=kind, ref=ref, sent_at=when))
    await session.flush()


async def test_the_sweep_turns_three_pings_into_a_question_with_buttons(session):
    due = _today() - timedelta(days=7)
    person, promise = await _promise(session, "invoice yuboradi", due=due)
    for offset in (0, 1, 3):
        await _logged(
            session,
            "promise",
            str(promise.id),
            datetime.combine(due, datetime.min.time(), tzinfo=TZ)
            + timedelta(days=offset, hours=10),
        )

    bundle = await reminders.collect_due(session)

    assert bundle.promises == []
    [question] = bundle.questions
    assert question.refs == [("promise", promise.id)]
    assert not bundle.is_empty()
    text = replies.still_open_question(bundle.questions)
    assert "Hali ochiqmi?" in text and "invoice yuboradi" in text

    await reminders.mark_asked(session, bundle)
    await session.flush()
    again = await reminders.collect_due(session)
    assert again.is_empty()  # quiet until answered

    # "Ha" a day later: nothing for six more days, then the question returns.
    await reminders.acknowledge(session, "promise", promise)
    await session.flush()
    assert (
        await reminders.collect_due(session, now=_now() + timedelta(days=6))
    ).is_empty()
    later = await reminders.collect_due(session, now=_now() + timedelta(days=8))
    assert [q.ref for q in later.questions] == [str(promise.id)]


async def test_an_overdue_promise_is_no_longer_pinged_every_day_forever(session):
    """Repro of the audit finding: with no writer for `done`, a dated promise
    was reminded daily until the end of time. Now: three pings, then one
    question a week — four in thirty days, not thirty pings."""
    due = _today() - timedelta(days=30)
    _, promise = await _promise(session, "pul beradi", due=due)
    pings = 0
    questions = 0
    for day in range(30):
        now = _now() + timedelta(days=day)
        bundle = await reminders.collect_due(session, now=now)
        if bundle.promises:
            pings += 1
            for _, ref in bundle.keys:
                await _logged(session, "promise", ref, now)
        if bundle.questions:
            questions += 1
            for q in bundle.questions:
                await _logged(session, "ask:promise", q.ref, now)
    assert (pings, questions) == (3, 4)


async def test_an_undated_task_is_nudged_weekly_and_a_fresh_one_is_left_alone(session):
    old = m.Task(description="eski vazifa", created_at=_now() - timedelta(days=8))
    fresh = m.Task(description="yangi vazifa")
    session.add_all([old, fresh])
    await session.flush()

    bundle = await reminders.collect_due(session)

    assert [q.refs for q in bundle.questions] == [[("task", old.id)]]


async def test_the_worker_attaches_buttons_to_pings_and_sends_the_question(
    session, monkeypatch
):
    # The sweep is silent inside quiet hours; the test must not depend on
    # the wall clock (it ran red between 23:30 and 07:30 Tashkent).
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: False)

    class _Bot:
        def __init__(self) -> None:
            self.sent: list[tuple[str, object]] = []

        async def send_message(self, chat_id, text, **kwargs):
            self.sent.append((text, kwargs.get("reply_markup")))

    _, debt = await _open_debt(session, due=_today())
    task = await _task(session, "hujjat topshirish", due=_today())
    stale = m.Promise(
        made_by=PromiseMadeBy.me,
        person_id=debt.person_id,
        description="javob yozaman",
        created_at=_now() - timedelta(days=9),
    )
    session.add(stale)
    await session.commit()
    bot = _Bot()

    await worker.reminder_job(bot)

    ping, question = bot.sent
    assert "Eslatma" in ping[0] and f"<code>d{debt.id}</code>" in ping[0]
    assert _payloads(ping[1]) == [
        [f"rec:d:d{debt.id}", f"rec:e:d{debt.id}", f"rec:f:d{debt.id}"],
        [f"rec:d:t{task.id}", f"rec:e:t{task.id}"],
    ]
    assert "Hali ochiqmi?" in question[0] and "javob yozaman" in question[0]
    assert _payloads(question[1]) == [
        [f"rec:o:p{stale.id}", f"rec:d:p{stale.id}", f"rec:c:p{stale.id}"]
    ]
    # Both went into the log: the next sweep has nothing to say.
    kinds = set(await session.scalars(sa.select(m.ReminderLog.kind)))
    assert kinds == {"debt", "task", "ask:promise"}
    bot.sent.clear()
    await worker.reminder_job(bot)
    assert bot.sent == []


# --- the evening report finally lists what was kept ---------------------------


async def test_bajarilganlar_lists_the_promise_closed_today_and_not_the_dropped_one(
    session,
):
    _, kept = await _promise(session, "invoice yuboradi")
    _, dropped = await _promise(session, "kelmaydi")
    task = await _task(session, "hujjat topshirish")
    await records.mark_done(session, kept, by=records.BY_COMMAND)
    await records.close(session, dropped, by=records.BY_COMMAND)
    await records.mark_done(session, task, by=records.BY_BUTTON)
    session.expire_all()

    completed = await queries.completed_on(session, _today())
    assert [p.id for p in completed.done_promises] == [kept.id]
    assert [t.id for t in completed.done_tasks] == [task.id]

    data = await reports.gather(session, _today())
    block = reports.render_data_block(data)
    section = block[block.index(reports.H_DONE) : block.index(reports.H_CHATS)]
    assert "va'da bajarildi: Akmal — invoice yuboradi" in section
    assert "vazifa bajarildi: hujjat topshirish" in section
    assert "kelmaydi" not in section


async def test_the_reports_open_items_carry_refs(session):
    _, debt = await _open_debt(session, due=_today())
    _, promise = await _promise(session, "pul beradi", due=_today())
    data = await reports.gather(session, _today())
    block = reports.render_data_block(data)
    assert f"[d{debt.id}]" in block and f"[p{promise.id}]" in block


async def test_bugun_lists_the_days_new_rows_by_ref(session):
    _, debt = await _open_debt(session)
    _, promise = await _promise(session, "pul beradi")
    summary = await queries.day_summary(session)
    text = replies.day_report(summary)
    assert f"<code>d{debt.id}</code>" in text and f"<code>p{promise.id}</code>" in text
    assert "Akmal" in text and "pul beradi" in text

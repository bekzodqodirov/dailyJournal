"""Closing the adversarial-review findings on build step 1.

B1  a missed "Hali ochiqmi?" is asked again a week later, not never;
B2  a fulfilment closes a promise only on a real, same-side, unambiguous match;
B3  a typo'd /tuzat value is refused instead of becoming a new person;
B4  a close can be reversed — ↩️ Qaytar after ✅, and /qaytar;
M1  a stale "Ha" on a closed row says so instead of acking it;
M2  ✅ on a debt question settles the whole balance the line shows;
M3  a debt question has no Yop button, because Yop can never work on a debt;
M4  a window that only closed a promise still has counts;
M5  asserted_by is required in both JSON schemas;
M6  the window-format description matches what the renderer emits;
M7  a receipt-rendering failure can never block the ledger.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from miya.bot import handlers, keyboards, notices, replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import (
    Currency,
    DebtDirection,
    DebtStatus,
    PromiseMadeBy,
    PromiseStatus,
    TaskStatus,
)
from miya.services import batch, persistence, queries, records, reminders
from miya.services import extraction as ex
from miya.services.people import resolve_person
from miya.services.persistence import Applied, apply_extraction
from miya.services.prompts import EXTRACTION_SYSTEM_PROMPT
from miya.worker import main as worker
from tests.test_batch import _window
from tests.test_chat_notices import _result
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


def _now() -> datetime:
    return datetime.now(TZ)


def _today() -> date:
    return _now().date()


def _fulfilment(description: str, made_by: str = "them", person: str = "Akmal"):
    return ex.ExtractedFulfilment(made_by=made_by, person=person, description=description)


# --- B1: a missed question comes back ----------------------------------------


async def test_a_missed_question_on_a_dated_item_is_asked_again_a_week_later(session):
    """The owner never answered: the debt must not vanish from his reminders."""
    due = _today() - timedelta(days=7)
    _, promise = await _promise(session, "invoice yuboradi", due=due)
    start = datetime.combine(due, datetime.min.time(), tzinfo=TZ)
    for offset in (0, 1, 3):
        await _logged(session, "promise", str(promise.id), start + timedelta(days=offset))
    await _logged(session, "ask:promise", str(promise.id), _now())

    quiet = await reminders.collect_due(session, now=_now() + timedelta(days=6))
    assert quiet.is_empty()

    again = await reminders.collect_due(session, now=_now() + timedelta(days=7))
    [question] = again.questions
    assert question.refs == [("promise", promise.id)]
    # The same buttons as the first time.
    assert _payloads(keyboards.question_keyboard(again.questions)) == [
        [f"rec:o:p{promise.id}", f"rec:d:p{promise.id}", f"rec:c:p{promise.id}"]
    ]


def test_a_missed_debt_question_repeats_weekly_too():
    due = date(2026, 9, 1)

    def at(days):
        return datetime.combine(due, datetime.min.time(), tzinfo=TZ) + timedelta(
            days=days, hours=10
        )

    history = [("debt", at(0)), ("debt", at(1)), ("debt", at(3)), ("ask:debt", at(7))]
    decide = lambda now: reminders.decide(  # noqa: E731
        "debt", history, due=due, created_at=None, now=now
    )
    assert decide(at(13)) is None
    assert decide(at(14)) == reminders.ASK
    assert decide(at(60)) == reminders.ASK


# --- B2: fulfilment matching ---------------------------------------------------


def test_a_short_hint_no_longer_scores_100_against_a_longer_promise():
    """partial_ratio said 93 here; token_set_ratio alone says 60."""
    score = persistence._fulfilment_score(
        "hujjat yubordi", "hujjat yuboradi va shartnoma imzolaydi"
    )
    assert score < persistence.FULFIL_MATCH


async def test_a_one_word_hint_closes_nothing_even_when_it_is_in_the_promise(session):
    _, promise = await _promise(session, "invoice yuboradi")
    interaction = await _interaction(session)

    applied = await apply_extraction(
        session, interaction, ex.ExtractionResult(fulfilments=[_fulfilment("invoice")])
    )

    assert applied.fulfilled == []
    assert applied.unmatched_fulfilments == [("Akmal", "invoice")]
    assert (await _reload(session, "promise", promise.id)).status is PromiseStatus.open


async def test_a_fulfilment_only_closes_promises_made_by_the_same_side(session):
    """Akmal sending an invoice ends Akmal's promise, never the owner's promise
    to Akmal that reads the same."""
    _, mine = await _promise(session, "invoice yuboraman", made_by=PromiseMadeBy.me)
    interaction = await _interaction(session)

    applied = await apply_extraction(
        session,
        interaction,
        ex.ExtractionResult(fulfilments=[_fulfilment("invoice yubordi", "them")]),
    )
    assert applied.fulfilled == []
    assert (await _reload(session, "promise", mine.id)).status is PromiseStatus.open

    # The owner saying he sent it closes his own.
    _, theirs = await _promise(session, "invoice yuboradi", made_by=PromiseMadeBy.them)
    applied = await apply_extraction(
        session,
        await _interaction(session),
        ex.ExtractionResult(fulfilments=[_fulfilment("invoice yubordim", "me")]),
    )
    assert [p.id for p, _ in applied.fulfilled] == [mine.id]
    assert (await _reload(session, "promise", theirs.id)).status is PromiseStatus.open


async def test_a_near_miss_pair_is_left_for_the_buttons(session):
    """Best 97, runner-up 88: above the threshold, but not clearly apart."""
    _, plain = await _promise(session, "invoice yuboradi")
    _, gz = await _promise(session, "GZ invoice yuboradi")
    assert (
        persistence._fulfilment_score("invoice yubordi", plain.description)
        - persistence._fulfilment_score("invoice yubordi", gz.description)
        < persistence.FULFIL_MARGIN
    )

    applied = await apply_extraction(
        session,
        await _interaction(session),
        ex.ExtractionResult(fulfilments=[_fulfilment("invoice yubordi")]),
    )

    assert applied.fulfilled == []
    assert applied.unmatched_fulfilments == [("Akmal", "invoice yubordi")]
    for promise in (plain, gz):
        assert (
            await _reload(session, "promise", promise.id)
        ).status is PromiseStatus.open


async def test_a_clear_winner_still_closes(session):
    _, plain = await _promise(session, "invoice yuboradi")
    _, other = await _promise(session, "konteynerni jo'natadi")
    applied = await apply_extraction(
        session,
        await _interaction(session),
        ex.ExtractionResult(fulfilments=[_fulfilment("invoice yubordi")]),
    )
    assert [p.id for p, _ in applied.fulfilled] == [plain.id]
    assert (await _reload(session, "promise", other.id)).status is PromiseStatus.open


def test_made_by_is_required_on_a_fulfilment_and_documented():
    assert (
        ex.parse_extraction_json(
            '{"fulfilments":[{"person":"Akmal","description":"invoice yubordi"}]}'
        )
        is None
    )
    schema = ex.extraction_output_config()["format"]["schema"]
    assert "made_by" in schema["$defs"]["ExtractedFulfilment"]["required"]
    assert '"made_by"' in EXTRACTION_SYSTEM_PROMPT
    bullet = EXTRACTION_SYSTEM_PROMPT.index("Fulfilments")
    assert "made_by" in EXTRACTION_SYSTEM_PROMPT[bullet:]


# --- B3: /tuzat refuses what is not a name ---------------------------------------

TODAY = date(2026, 9, 15)


@pytest.mark.parametrize("text", ["6 mlnn", "5 xyz", "Sardor 2", "$abc", "-", "3mln$"])
def test_parse_edit_refuses_values_that_are_neither_a_field_nor_a_name(text):
    edit = records.parse_edit(text, today=TODAY)
    assert edit is None or edit.field != "person", (text, edit)


@pytest.mark.parametrize("text", ["Sardor", "Akmal aka", "To'lqin", "Абдулла"])
def test_parse_edit_still_takes_a_name(text):
    assert records.parse_edit(text, today=TODAY) == records.Edit("person", text)


async def test_a_typo_in_tuzat_creates_nobody_and_shows_the_usage(bound):  # noqa: F811
    _, debt = await _open_debt(bound)
    message = _Message()

    await handlers.cmd_edit(message, _command("tuzat", f"d{debt.id} 6 mlnn"))

    [(text, _)] = message.sent
    assert text == replies.TUZAT_USAGE
    fresh = await _reload(bound, "debt", debt.id)
    assert fresh.person.display_name == "Akmal" and fresh.history == []
    names = set(await bound.scalars(sa.select(m.Person.display_name)))
    assert names == {"Akmal"}


async def test_tuzat_person_goes_through_resolve_person(session, monkeypatch):
    """The locked, alias-aware path — not a bare Person() construction."""
    _, debt = await _open_debt(session)
    sardor = await resolve_person(session, "Sardor")
    await session.flush()
    calls = []
    real = records.resolve_person

    async def spy(session_, name, **kwargs):
        calls.append(name)
        return await real(session_, name, **kwargs)

    monkeypatch.setattr(records, "resolve_person", spy)

    await records.set_field(session, debt, "person", "Sardor GZ", by=records.BY_COMMAND)

    assert calls == ["Sardor GZ"]
    fresh = await _reload(session, "debt", debt.id)
    assert fresh.person_id == sardor.id
    # Alias bookkeeping happened, as it does for every other writer: a new
    # spelling of a known name is kept.
    assert "Sardor GZ" in (await session.get(m.Person, sardor.id)).aliases


# --- B4: a close can be reversed -----------------------------------------------


async def test_reopen_puts_a_promise_and_a_task_back(session):
    _, promise = await _promise(session)
    task = await _task(session)
    await records.mark_done(session, promise, by=records.BY_BUTTON)
    await records.close(session, task, by=records.BY_COMMAND)

    change = await records.reopen(session, promise, by=records.BY_BUTTON)
    await records.reopen(session, task, by=records.BY_COMMAND)

    promise = await _reload(session, "promise", promise.id)
    task = await _reload(session, "task", task.id)
    assert promise.status is PromiseStatus.open and promise.completed_at is None
    assert task.status is TaskStatus.todo and task.completed_at is None
    assert (change.old, change.new) == ("done", "open")
    assert [e["new"] for e in promise.history] == ["done", "open"]
    assert [e["new"] for e in task.history] == ["dropped", "todo"]
    assert [p.id for p, _ in await queries.open_promises(session)] == [promise.id]
    assert (await queries.completed_on(session, _today())).done_promises == []


async def test_reopen_removes_only_the_settling_payment_of_a_debt(session):
    _, debt = await _open_debt(session)
    real = m.DebtPayment(
        debt_id=debt.id, amount=Decimal("2000000"), currency=Currency.UZS, note="naqd"
    )
    session.add(real)
    await session.flush()
    await records.mark_done(session, debt, by=records.BY_BUTTON)
    assert await queries.open_debts(session) == []

    await records.reopen(session, debt, by=records.BY_BUTTON)

    fresh = await _reload(session, "debt", debt.id)
    assert fresh.status is DebtStatus.partially_paid and fresh.settled_at is None
    payments = list(
        await session.scalars(
            sa.select(m.DebtPayment).where(m.DebtPayment.debt_id == debt.id)
        )
    )
    assert [(p.id, p.note) for p in payments] == [(real.id, "naqd")]
    [balance] = await queries.open_debts(session)
    assert balance.outstanding == Decimal("3000000")
    fields = [(e["field"], e["old"], e["new"]) for e in fresh.history]
    assert fields == [
        ("payment", None, "3000000.00"),
        ("status", "open", "settled"),
        ("payment", "3000000.00", None),
        ("status", "settled", "partially_paid"),
    ]


async def test_reopen_refuses_an_open_row_and_a_debt_real_payments_cover(session):
    _, promise = await _promise(session)
    with pytest.raises(records.AlreadyOpen):
        await records.reopen(session, promise, by=records.BY_COMMAND)

    _, debt = await _open_debt(session)
    session.add(
        m.DebtPayment(debt_id=debt.id, amount=Decimal("5000000"), currency=Currency.UZS)
    )
    debt.status = DebtStatus.settled
    await session.flush()
    with pytest.raises(records.NotReopenable):
        await records.reopen(session, debt, by=records.BY_COMMAND)
    assert (await _reload(session, "debt", debt.id)).status is DebtStatus.settled


async def test_the_done_outcome_carries_one_qaytar_button_that_reopens(bound):  # noqa: F811
    _, promise = await _promise(bound, "invoice yuboradi")
    message = _Message(reply_markup=keyboards.record_actions([("promise", promise.id)]))

    await handlers.on_record_button(_Callback(f"rec:d:p{promise.id}", message))

    [(text, markup)] = message.sent
    assert "Bajarildi" in text
    assert _payloads(markup) == [[f"rec:r:p{promise.id}"]]
    assert [b.text for row in markup.inline_keyboard for b in row] == ["↩️ Qaytar"]

    outcome = _Message(reply_markup=markup)
    await handlers.on_record_button(_Callback(f"rec:r:p{promise.id}", outcome))

    assert (await _reload(bound, "promise", promise.id)).status is PromiseStatus.open
    [(text, markup)] = outcome.sent
    assert "Qayta ochildi" in text and markup is None
    assert outcome.edited_markup is None  # the Qaytar button is gone


async def test_yop_outcome_carries_qaytar_too(bound):  # noqa: F811
    task = await _task(bound)
    message = _Message()
    await handlers.cmd_close(message, _command("yop", f"t{task.id}"))
    [(_, markup)] = message.sent
    assert _payloads(markup) == [[f"rec:r:t{task.id}"]]


async def test_cmd_bajarildi_and_cmd_qaytar(bound):  # noqa: F811
    _, debt = await _open_debt(bound)
    message = _Message()

    await handlers.cmd_done(message, _command("bajarildi", f"d{debt.id}"))
    assert _payloads(message.sent[-1][1]) == [[f"rec:r:d{debt.id}"]]
    assert (await _reload(bound, "debt", debt.id)).status is DebtStatus.settled

    await handlers.cmd_reopen(message, _command("qaytar", f"d{debt.id}"))
    assert "Qayta ochildi" in message.sent[-1][0]
    assert (await _reload(bound, "debt", debt.id)).status is DebtStatus.open

    await handlers.cmd_reopen(message, _command("qaytar", f"d{debt.id}"))
    assert message.sent[-1][0] == replies.RECORD_ALREADY_OPEN
    await handlers.cmd_reopen(message, _command("qaytar", ""))
    assert message.sent[-1][0] == replies.REF_USAGE


def test_help_teaches_qaytar():
    assert "/qaytar" in replies.HELP


# --- M1: a stale "Ha" ----------------------------------------------------------


async def test_ha_on_a_row_closed_since_says_so_and_logs_no_ack(bound):  # noqa: F811
    task = await _task(bound)
    await records.mark_done(bound, task, by=records.BY_COMMAND)
    message = _Message(reply_markup=keyboards.question_actions([("task", task.id)]))

    await handlers.on_record_button(_Callback(f"rec:o:t{task.id}", message))

    [(text, _)] = message.sent
    assert text == replies.RECORD_ALREADY_CLOSED
    assert message.edited_markup is None
    assert await bound.scalar(sa.select(sa.func.count()).select_from(m.ReminderLog)) == 0


# --- M2 / M3: a debt question is about a balance ---------------------------------


async def test_a_balance_question_has_one_row_and_its_tick_settles_every_debt(
    bound,  # noqa: F811
):
    person, first = await _open_debt(bound, due=_today() - timedelta(days=7))
    second = m.Debt(
        direction=DebtDirection.they_owe_me,
        person_id=person.id,
        amount=Decimal("1000000"),
        currency=Currency.UZS,
        due_date=_today() - timedelta(days=7),
    )
    bound.add(second)
    await bound.flush()
    ref = reminders.ref_for("debt", first)
    for offset in (0, 1, 3):
        await _logged(bound, "debt", ref, _now() - timedelta(days=7 - offset))

    bundle = await reminders.collect_due(bound)
    [question] = bundle.questions
    assert question.refs == [("debt", first.id), ("debt", second.id)]
    markup = keyboards.question_keyboard(bundle.questions)
    # One answer row for the balance, no Yop, ✅ keyed as a balance settle.
    assert _payloads(markup) == [[f"rec:o:d{first.id}", f"rec:b:d{first.id}"]]

    message = _Message(reply_markup=markup)
    await handlers.on_record_button(_Callback(f"rec:b:d{first.id}", message))

    for debt in (first, second):
        assert (await _reload(bound, "debt", debt.id)).status is DebtStatus.settled
    assert await queries.open_debts(bound) == []
    [(text, undo)] = message.sent
    assert f"<code>d{first.id}</code>" in text and f"<code>d{second.id}</code>" in text
    assert _payloads(undo) == [[f"rec:r:d{first.id}"], [f"rec:r:d{second.id}"]]
    assert message.edited_markup is None


def test_a_multi_row_balance_question_is_labelled_with_every_ref():
    questions = [
        reminders.Question("debt", "1:x:UZS", [("debt", 12), ("debt", 15)]),
        reminders.Question("task", "3", [("task", 3)]),
    ]
    markup = keyboards.question_keyboard(questions)
    texts = [[b.text for b in row] for row in markup.inline_keyboard]
    assert texts == [
        ["Ha d12, d15", "✅ Bajarildi d12, d15"],
        ["Ha t3", "✅ Bajarildi t3", "✖️ Yop t3"],
    ]
    assert _payloads(markup) == [
        ["rec:o:d12", "rec:b:d12"],
        ["rec:o:t3", "rec:d:t3", "rec:c:t3"],
    ]


def test_a_debt_question_row_never_offers_yop():
    assert _payloads(keyboards.question_actions([("debt", 12)])) == [
        ["rec:o:d12", "rec:b:d12"]
    ]
    for button in keyboards.question_actions([("debt", 12)]).inline_keyboard[0]:
        assert "Yop" not in button.text


async def test_the_worker_sends_the_balance_keyboard(session, monkeypatch):
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: False)

    class _Bot:
        def __init__(self) -> None:
            self.sent: list[tuple[str, object]] = []

        async def send_message(self, chat_id, text, **kwargs):
            self.sent.append((text, kwargs.get("reply_markup")))

    person, first = await _open_debt(session, due=_today() - timedelta(days=7))
    ref = reminders.ref_for("debt", first)
    for offset in (0, 1, 3):
        await _logged(session, "debt", ref, _now() - timedelta(days=7 - offset))
    await session.commit()
    bot = _Bot()

    await worker.reminder_job(bot)

    [(text, markup)] = bot.sent
    assert "Hali ochiqmi?" in text
    assert _payloads(markup) == [[f"rec:o:d{first.id}", f"rec:b:d{first.id}"]]


# --- M4: counts for fulfilments ----------------------------------------------------


def test_counts_cover_every_field_that_makes_a_receipt():
    """A window that only closed a promise is a receipt with counts."""
    promise = m.Promise(id=7, made_by=PromiseMadeBy.them, description="invoice")
    person = m.Person(id=1, display_name="Akmal")
    assert notices.counts_of(Applied(fulfilled=[(promise, person)])) == {"fulfilled": 1}
    assert notices.counts_of(Applied(unmatched_fulfilments=[("Akmal", "x y")])) == {
        "questions": 1
    }
    assert "fulfilled" in notices.COUNT_LABEL

    # Generic: no list field of Applied can be non-empty and uncounted.
    for name in Applied.__dataclass_fields__:
        if name == "facts":
            continue
        applied = Applied(**{name: [SimpleNamespace()]})
        assert not applied.is_empty()
        assert notices.counts_of(applied), name


# --- M5: asserted_by is required in the schema ---------------------------------------

_ASSERTING = (
    "ExtractedDebt",
    "ExtractedSettlement",
    "ExtractedPromise",
    "ExtractedTransaction",
    "ExtractedFulfilment",
)


def test_asserted_by_is_required_on_both_schema_paths_but_defaults_in_python():
    prompted = ex.extraction_system_block(with_schema=True)[0]["text"]
    prompted_schema = json.loads(prompted[prompted.index("{") :])
    grammar_schema = ex.extraction_output_config()["format"]["schema"]
    for schema in (prompted_schema, grammar_schema):
        for name in _ASSERTING:
            assert "asserted_by" in schema["$defs"][name]["required"], name
    # Old rows and callers that omit it keep their meaning.
    assert ex.ExtractedSettlement(person="X", amount=1).asserted_by == "me"
    omitted = '{"debts":[{"direction":"i_owe_them","person":"Akmal","amount":1}]}'
    assert ex.parse_extraction_json(omitted).debts[0].asserted_by == "me"


# --- M6: the format description is exact --------------------------------------------


def test_the_prompt_describes_bare_them_and_the_addressed_marker():
    assert "bare THEM" in EXTRACTION_SYSTEM_PROMPT
    assert '"[THEM (name) → ME]"' in EXTRACTION_SYSTEM_PROMPT
    assert '"[THEM → ME]"' in EXTRACTION_SYSTEM_PROMPT


# --- M7: a receipt failure never blocks the ledger ----------------------------------


async def test_a_failing_people_lookup_does_not_fail_the_apply(session, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("names are broken")

    monkeypatch.setattr(batch, "_people_named", boom)
    window = await _window(session)

    landed = await batch._apply_result(session, window, _result())
    await session.flush()

    assert window.status.value == "applied"
    assert [d.amount for d in landed.applied.debts] == [Decimal(5_000_000)]
    assert landed.notice is None and landed.people == []
    [balance] = await queries.open_debts(session)
    assert balance.outstanding == Decimal(5_000_000)

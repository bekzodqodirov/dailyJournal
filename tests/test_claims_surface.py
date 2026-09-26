"""Build step 3, the surface: how a counterparty's claim is asked and answered.

The question line, the Ha / Yo'q / Tuzat rows, the receipt that carries them,
the button handler, `/davolar`, `/tuzat c12 …`, the brief's section, the
notice counts and the report's one line. The core (the gate, the claims
table, accept / decline / edit) is tests/test_claims.py.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from miya.bot import formatting as f
from miya.bot import handlers, keyboards, notices, replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import Currency, DebtDirection, PromiseMadeBy, PromiseStatus
from miya.services import brief, claims, reports
from miya.services import extraction as ex
from miya.services import questions as questions_mod
from miya.services.persistence import Applied, apply_extraction
from tests.test_close_and_correct import _Callback, _command, _Message
from tests.test_pipeline import _interaction
from tests.test_step2_render import NOW, _question, _stale

TZ = settings.tz
TEN_DIGITS = 9_999_999_999


def _view(**kw) -> claims.ClaimView:
    fields = {
        "id": 12,
        "kind": claims.KIND_DEBT,
        "person_name": "Akmal",
        "amount": Decimal("5000000"),
        "currency": Currency.UZS,
        "direction": DebtDirection.they_owe_me,
        "description": "sabab",
        "due": None,
        "made_by": None,
        "state": claims.PENDING,
    }
    return claims.ClaimView(**{**fields, **kw})


def _claim(claim_id=12, kind=claims.KIND_DEBT, person_name="Akmal", **payload) -> m.Claim:
    """An unsaved Claim row, enough for rendering."""
    base = {
        claims.KIND_DEBT: {
            "direction": "they_owe_me",
            "amount": 5_000_000,
            "currency": "UZS",
            "reason": "sabab",
            "asserted_by": "them",
        },
        claims.KIND_SETTLEMENT: {"amount": 5_000_000, "currency": "UZS", "note": "izoh"},
        claims.KIND_TRANSACTION: {"type": "expense", "amount": 300, "currency": "USD"},
        claims.KIND_PROMISE: {"made_by": "me", "description": "hujjat yuborish"},
        claims.KIND_FULFILMENT: {
            "made_by": "them",
            "description": "hujjatlarni yubordim",
        },
    }[kind]
    return m.Claim(
        id=claim_id,
        interaction_id=1,
        kind=kind,
        person_name=person_name,
        payload={"person": person_name, **base, **payload},
        state=claims.PENDING,
        history=[],
    )


def _payloads(markup) -> list[list[str]]:
    return [[b.callback_data for b in row] for row in markup.inline_keyboard]


def _labels(markup) -> list[list[str]]:
    return [[b.text for b in row] for row in markup.inline_keyboard]


# --- the six shapes ----------------------------------------------------------


@pytest.mark.parametrize(
    ("view", "markup", "plain"),
    [
        (
            _view(),
            "❓ <code>c12</code> <b>Akmal</b> aytdi: u senga 5 mln so'm qarz («sabab»)"
            " — to'g'rimi?",
            "❓ c12 Akmal aytdi: u senga 5 mln so'm qarz («sabab») — to'g'rimi?",
        ),
        (
            _view(
                amount=Decimal("500"),
                currency=Currency.USD,
                direction=DebtDirection.i_owe_them,
            ),
            "❓ <code>c12</code> <b>Akmal</b> aytdi: sen unga $500 qarzsan («sabab»)"
            " — to'g'rimi?",
            "❓ c12 Akmal aytdi: sen unga $500 qarzsan («sabab») — to'g'rimi?",
        ),
        (
            _view(id=13, kind=claims.KIND_SETTLEMENT, direction=None, description="izoh"),
            "❓ <code>c13</code> <b>Akmal</b> aytdi: 5 mln so'm to'ladi («izoh»)"
            " — to'g'rimi?",
            "❓ c13 Akmal aytdi: 5 mln so'm to'ladi («izoh») — to'g'rimi?",
        ),
        (
            _view(
                id=14,
                kind=claims.KIND_TRANSACTION,
                amount=Decimal("300"),
                currency=Currency.USD,
                direction=None,
                description="tavsif",
                txn_type="expense",
            ),
            "❓ <code>c14</code> <b>Akmal</b> aytdi: $300 chiqim («tavsif»)"
            " — to'g'rimi?",
            "❓ c14 Akmal aytdi: $300 chiqim («tavsif») — to'g'rimi?",
        ),
        (
            _view(
                id=15,
                kind=claims.KIND_PROMISE,
                amount=None,
                currency=None,
                direction=None,
                description="hujjat yuborish",
                due=date(2026, 9, 18),
                made_by=PromiseMadeBy.me,
            ),
            "❓ <code>c15</code> <b>Akmal</b> aytdi: sen va'da bergansan — "
            "«hujjat yuborish» (18-sen) — to'g'rimi?",
            "❓ c15 Akmal aytdi: sen va'da bergansan — «hujjat yuborish» (18-sen)"
            " — to'g'rimi?",
        ),
        (
            _view(
                id=16,
                kind=claims.KIND_FULFILMENT,
                amount=None,
                currency=None,
                direction=None,
                description="hujjatlarni yubordim",
                made_by=PromiseMadeBy.them,
            ),
            "❓ <code>c16</code> <b>Akmal</b> aytdi: «hujjatlarni yubordim»"
            " — va'dasi bajarilganmi?",
            "❓ c16 Akmal aytdi: «hujjatlarni yubordim» — va'dasi bajarilganmi?",
        ),
    ],
    ids=["debt-they-owe", "debt-i-owe", "settlement", "transaction", "promise", "fulfil"],
)
def test_the_six_question_lines(view, markup, plain):
    assert f.claim_line(view) == markup
    assert f.claim_line(view, markup=False) == plain


def test_a_settlement_the_owner_made_and_an_undated_promise_read_right():
    paid = _view(
        kind=claims.KIND_SETTLEMENT, direction=DebtDirection.i_owe_them, description=""
    )
    assert f.claim_line(paid).endswith(
        "aytdi: sen unga 5 mln so'm to'lagansan — to'g'rimi?"
    )
    promise = _view(
        kind=claims.KIND_PROMISE,
        amount=None,
        currency=None,
        direction=None,
        description="pul beraman",
        made_by=PromiseMadeBy.me,
    )
    assert "«pul beraman» — to'g'rimi?" in f.claim_line(promise)
    assert "(" not in f.claim_line(promise).split("bergansan")[1]


def test_the_line_escapes_every_hostile_string_in_markup_and_none_in_plain():
    view = _view(person_name="<b>Akmal</b> & Co", description="<i>sabab</i> & x")
    line = f.claim_line(view)
    assert "<b>&lt;b&gt;Akmal&lt;/b&gt; &amp; Co</b>" in line
    assert "«&lt;i&gt;sabab&lt;/i&gt; &amp; x»" in line
    assert "<i>" not in line
    plain = f.claim_line(view, markup=False)
    assert "<b>Akmal</b> & Co aytdi" in plain and "&lt;" not in plain


def test_a_partial_view_never_raises_and_says_what_is_missing():
    view = _view(
        person_name="", amount=None, currency=None, direction=None, description=""
    )
    line = f.claim_line(view)
    assert line == (
        "❓ <code>c12</code> <b>Kimdir</b> aytdi: orangizda noma'lum summa qarz bor"
        " — to'g'rimi?"
    )
    money_less = _view(kind=claims.KIND_TRANSACTION, amount=None, direction=None)
    assert "noma'lum summa pul harakati" in f.claim_line(money_less)


def test_a_transaction_names_its_side():
    """The view carries the transaction's type, so the question says kirim /
    chiqim; a payload without one falls back to the neutral word."""
    cases = (("expense", "chiqim"), ("income", "kirim"), (None, "pul harakati"))
    for txn_type, word in cases:
        view = _view(
            kind=claims.KIND_TRANSACTION,
            direction=None,
            description="",
            txn_type=txn_type,
        )
        assert f"5 mln so'm {word}" in f.claim_line(view)


# --- keyboards ---------------------------------------------------------------


def test_claim_rows_are_labelled_ha_yoq_tuzat():
    markup = keyboards.claim_actions([12, 13])
    assert _labels(markup) == [
        ["✅ Ha c12", "✖️ Yo'q c12", "✏️ Tuzat c12"],
        ["✅ Ha c13", "✖️ Yo'q c13", "✏️ Tuzat c13"],
    ]
    assert _payloads(markup) == [
        ["cl:y:12", "cl:n:12", "cl:e:12"],
        ["cl:y:13", "cl:n:13", "cl:e:13"],
    ]
    assert keyboards.claim_actions([]) is None
    assert keyboards.claim_actions([None]) is None


def test_every_claim_payload_fits_telegrams_64_bytes_for_a_ten_digit_id():
    markups = [
        keyboards.claim_actions([TEN_DIGITS]),
        keyboards.applied_actions([("debt", TEN_DIGITS)], [TEN_DIGITS]),
        keyboards.brief_actions([("debt", TEN_DIGITS)]),
    ]
    payloads = [
        b.callback_data for mk in markups for row in mk.inline_keyboard for b in row
    ]
    assert any(p.startswith("cl:") for p in payloads)
    for payload in payloads:
        assert len(payload.encode()) <= 64


def test_a_receipt_keyboard_puts_record_rows_first_then_claims():
    markup = keyboards.applied_actions([("promise", 7)], [12])
    assert _payloads(markup) == [
        ["rec:d:p7", "rec:e:p7"],
        ["cl:y:12", "cl:n:12", "cl:e:12"],
    ]
    # Labelled as soon as there is more than one row of any kind.
    assert _labels(markup)[0] == ["✅ Bajarildi p7", "✏️ Tuzat p7"]
    assert _labels(keyboards.applied_actions([("promise", 7)], []))[0] == [
        "✅ Bajarildi",
        "✏️ Tuzat",
    ]
    assert keyboards.applied_actions([], []) is None
    assert _payloads(keyboards.applied_actions([], [12])) == [
        ["cl:y:12", "cl:n:12", "cl:e:12"]
    ]


def test_the_keyboard_ceiling_holds_and_only_what_fits_counts_as_shown():
    ids = list(range(1, 40))
    markup = keyboards.applied_actions([("debt", 1), ("debt", 2)], ids)
    assert len(markup.inline_keyboard) == keyboards.MAX_ROWS
    assert keyboards.claim_ids_in(markup) == ids[: keyboards.MAX_ROWS - 2]
    assert keyboards.claim_ids_in(None) == []
    assert keyboards.claim_ids_in(keyboards.record_actions([("debt", 12)])) == []


def test_an_answered_claim_leaves_the_keyboard_and_the_rest_stay():
    markup = keyboards.applied_actions([("debt", 12)], [12, 13])
    trimmed = keyboards.without_claim(markup, 12)
    assert _payloads(trimmed) == [
        ["rec:d:d12", "rec:e:d12", "rec:f:d12"],
        ["cl:y:13", "cl:n:13", "cl:e:13"],
    ]
    assert keyboards.without_claim(keyboards.claim_actions([12]), 12) is None
    assert keyboards.without_claim(None, 12) is None
    # "1" is not "12": the suffix match is on the whole id.
    assert keyboards.without_claim(keyboards.claim_actions([12]), 1) is not None


# --- replies -----------------------------------------------------------------


def test_the_confirmation_asks_after_the_other_lines_and_knows_its_claims():
    applied = Applied(
        promises=[m.Promise(id=7, made_by=PromiseMadeBy.me, description="pul beraman")],
        claims=[_claim(12), _claim(13, claims.KIND_SETTLEMENT)],
        facts=2,
    )
    text = replies.confirmation(applied)
    lines = text.split("\n")
    assert "<code>p7</code>" in lines[0]
    assert lines[1].startswith("🧠 2 ta")
    assert lines[2].startswith(
        "❓ <code>c12</code> <b>Akmal</b> aytdi: u senga 5 mln so'm"
    )
    assert lines[3].startswith(
        "❓ <code>c13</code> <b>Akmal</b> aytdi: 5 mln so'm to'ladi"
    )
    assert replies.confirmation_claim_ids(applied) == [12, 13]
    assert replies.confirmation_refs(applied) == [("promise", 7)]


def test_a_receipt_of_claims_only_is_not_an_empty_one():
    applied = Applied(claims=[_claim(12)])
    assert not applied.is_empty()
    text = replies.confirmation(applied)
    assert text.startswith("❓ <code>c12</code>") and "Yozib oldim" not in text


def test_the_question_message_and_the_list():
    view = claims.view(_claim(12, person_name="Akmal & Co"))
    question = replies.claim_question(view)
    assert question.startswith("❓ <b>Tasdiqlash kerak</b>\n❓ <code>c12</code>")
    assert "Akmal &amp; Co" in question

    assert replies.claims_list([]) == replies.CLAIMS_NONE
    listed = replies.claims_list([view, claims.view(_claim(13))], hidden=2)
    assert listed.startswith(replies.CLAIMS_HEADER)
    assert "<code>c12</code>" in listed and "<code>c13</code>" in listed
    assert "yana 2 ta" in listed and "/davolar" in listed
    assert "yana" not in replies.claims_list([view])


def test_the_tuzat_hint_teaches_only_what_the_kind_can_take():
    debt = replies.claim_tuzat_hint(claims.view(_claim(12)))
    for example in (
        "summa 4 mln",
        "kim Akmal",
        "valyuta $",
        "muddat 2026-10-01",
        "teskari",
    ):
        assert f"<code>/tuzat c12 {example}</code>" in debt
    settlement = replies.claim_tuzat_hint(claims.view(_claim(13, claims.KIND_SETTLEMENT)))
    assert "muddat" not in settlement and "teskari" in settlement
    promise = replies.claim_tuzat_hint(claims.view(_claim(15, claims.KIND_PROMISE)))
    assert "summa" not in promise and "muddat 2026-10-01" in promise
    fulfilment = replies.claim_tuzat_hint(claims.view(_claim(16, claims.KIND_FULFILMENT)))
    assert fulfilment.count("<code>/tuzat") == 1 and "kim Akmal" in fulfilment
    refused = replies.claim_field_refused(
        claims.view(_claim(13, claims.KIND_SETTLEMENT)), "due"
    )
    assert refused.startswith("Bu da'voda muddatni tuzatib bo'lmaydi.")


def test_the_answer_strings_and_the_help():
    assert replies.CLAIM_ACCEPTED_PREFIX.startswith("✅") and "Yozib oldim" in (
        replies.CLAIM_ACCEPTED_PREFIX
    )
    assert replies.CLAIM_DECLINED == "✖️ Yozilmadi. Kerak bo'lsa o'zing yozib qo'y."
    assert "topilmadi" in replies.CLAIM_GONE
    assert "allaqachon" in replies.CLAIM_ALREADY
    assert "/davolar — tasdiqlanmagan da'volar" in replies.HELP
    assert "c12" in replies.TUZAT_USAGE
    nothing = replies.claim_accepted(claims.Accepted(_claim(12), Applied()))
    assert nothing == replies.CLAIM_ACCEPTED_NOTHING
    written = replies.claim_accepted(
        claims.Accepted(
            _claim(12),
            Applied(
                debts=[
                    m.Debt(
                        id=3,
                        direction=DebtDirection.they_owe_me,
                        amount=Decimal("5000000"),
                        currency=Currency.UZS,
                    )
                ]
            ),
        )
    )
    assert written.startswith(replies.CLAIM_ACCEPTED_PREFIX + "\n💰 Qarz")
    assert "<code>d3</code>" in written


# --- the brief ---------------------------------------------------------------


def _brief(queue=None, *, questions=True, stale=True):
    loops = SimpleNamespace(
        questions=[_question()] if questions else [],
        stale=[_stale()] if stale else [],
        quiet=[],
        is_empty=lambda: not (questions or stale),
    )
    return SimpleNamespace(
        day=NOW.date(),
        events=[],
        due={},
        loops=loops,
        queue=queue,
        is_empty=lambda: False,
    )


def test_the_brief_counts_the_queue_in_one_last_line():
    """WP-19: the brief tells; the numbered batch after it asks."""
    body = replies.morning_brief(_brief(questions_mod.QueueSummary(waiting=3, money=2)))
    assert body.endswith("❓ Yana 3 ta savol navbatda (2 tasi pul bo'yicha) — /savollar")
    assert "Yana" not in replies.morning_brief(_brief())
    legacy = _brief()
    del legacy.queue
    assert "Yana" not in replies.morning_brief(legacy)


def test_a_waiting_question_alone_makes_the_brief_worth_sending():
    empty = brief.MorningBrief(now=NOW)
    assert empty.is_empty()
    waiting = questions_mod.QueueSummary(waiting=1, money=0)
    assert not brief.MorningBrief(now=NOW, queue=waiting).is_empty()


# --- notices and the report ----------------------------------------------------


def test_the_notice_counts_claims_as_their_own_kind():
    assert notices.counts_of(Applied(claims=[_claim(12), _claim(13)])) == {"claims": 2}
    assert notices.COUNT_LABEL["claims"] == "da'vo"
    summary = notices.overflow_summary([("GZ", {"claims": 2})])
    assert "2 da'vo" in summary


def _report_data(**kw) -> reports.ReportData:
    summary = SimpleNamespace(
        income={},
        expense={},
        by_category=[],
        biggest=[],
        interactions=0,
        people_seen=[],
        new_debts=[],
        new_promises=[],
    )
    completed = SimpleNamespace(settled_debts=[], done_promises=[], done_tasks=[])
    return reports.ReportData(
        day=NOW.date(), summary=summary, completed=completed, due={}, plan="—", **kw
    )


def test_the_report_carries_one_line_only_when_questions_wait():
    queue = questions_mod.QueueSummary(waiting=3, money=1)
    block = reports.render_data_block(_report_data(queue=queue))
    assert "\n❓ Yana 3 ta savol navbatda (1 tasi pul bo'yicha) — /savollar" in block
    assert block.index("Yana 3 ta") < block.index(reports.H_TOMORROW)
    assert "navbatda" not in reports.render_data_block(_report_data())
    assert reports._stats_json(_report_data(queue=queue))["questions_waiting"] == 3


# --- through the handlers, with real claims ----------------------------------------


@pytest.fixture
def bound(session, monkeypatch):
    """Handlers run inside the test session instead of opening their own."""

    @asynccontextmanager
    async def _scope():
        yield session
        await session.flush()

    monkeypatch.setattr(handlers, "session_scope", _scope)
    return session


async def _claimed(session, result: ex.ExtractionResult | None = None) -> m.Claim:
    """One real claim, the way the extraction parks it."""
    interaction = await _interaction(session, "Akmal: sen menga 5 mln qarzsan")
    result = result or ex.ExtractionResult(
        debts=[
            ex.ExtractedDebt(
                direction="i_owe_them",
                person="Akmal",
                amount=5_000_000,
                currency="UZS",
                reason="yuk haqi",
                asserted_by="them",
            )
        ]
    )
    applied = await apply_extraction(session, interaction, result)
    assert applied.debts == [] and len(applied.claims) == 1
    return applied.claims[0]


async def _debts(session) -> list[m.Debt]:
    return list(await session.scalars(sa.select(m.Debt)))


async def test_ha_writes_the_debt_with_its_buttons_and_drops_the_row(bound):
    claim = await _claimed(bound)
    message = _Message(reply_markup=keyboards.claim_actions([claim.id]))
    callback = _Callback(f"cl:y:{claim.id}", message)

    await handlers.on_claim_button(callback)

    [debt] = await _debts(bound)
    assert debt.direction is DebtDirection.i_owe_them
    assert debt.amount == Decimal("5000000")
    assert debt.history[-1]["field"] == "claim" and debt.history[-1]["by"] == "button"
    fresh = await claims.get(bound, claim.id)
    assert (fresh.state, fresh.result_kind, fresh.result_id) == (
        "accepted",
        "debt",
        debt.id,
    )
    [(text, markup)] = message.sent
    assert text.startswith(replies.CLAIM_ACCEPTED_PREFIX)
    assert f"<code>d{debt.id}</code>" in text and "← sen 5 mln so'm" in text
    assert _payloads(markup) == [
        [f"rec:d:d{debt.id}", f"rec:e:d{debt.id}", f"rec:f:d{debt.id}"]
    ]
    assert message.edited_markup is None
    assert callback.answered


async def test_yoq_writes_nothing_and_drops_the_row(bound):
    claim = await _claimed(bound)
    message = _Message(reply_markup=keyboards.applied_actions([("debt", 5)], [claim.id]))

    await handlers.on_claim_button(_Callback(f"cl:n:{claim.id}", message))

    assert await _debts(bound) == []
    assert (await claims.get(bound, claim.id)).state == "declined"
    [(text, markup)] = message.sent
    assert text == replies.CLAIM_DECLINED and markup is None
    # The receipt's own record row is still there to act on.
    assert _payloads(message.edited_markup) == [["rec:d:d5", "rec:e:d5", "rec:f:d5"]]


async def test_tuzat_button_teaches_the_syntax_and_keeps_the_buttons(bound):
    claim = await _claimed(bound)
    message = _Message(reply_markup=keyboards.claim_actions([claim.id]))

    await handlers.on_claim_button(_Callback(f"cl:e:{claim.id}", message))

    [(text, _)] = message.sent
    assert f"<code>/tuzat c{claim.id} summa 4 mln</code>" in text
    assert f"/tuzat c{claim.id} teskari" in text
    assert message.edited_markup == "untouched"
    assert (await claims.get(bound, claim.id)).state == "pending"
    assert await _debts(bound) == []


async def test_a_stale_button_says_the_claim_is_gone(bound):
    message = _Message(reply_markup=keyboards.claim_actions([424242]))
    await handlers.on_claim_button(_Callback("cl:y:424242", message))
    [(text, _)] = message.sent
    assert text == replies.CLAIM_GONE
    assert message.edited_markup is None
    assert await _debts(bound) == []


async def test_a_second_tap_finds_the_claim_answered_and_writes_nothing_twice(bound):
    claim = await _claimed(bound)
    first = _Message(reply_markup=keyboards.claim_actions([claim.id]))
    await handlers.on_claim_button(_Callback(f"cl:y:{claim.id}", first))
    second = _Message(reply_markup=keyboards.claim_actions([claim.id]))

    await handlers.on_claim_button(_Callback(f"cl:y:{claim.id}", second))
    edit = _Message(reply_markup=keyboards.claim_actions([claim.id]))
    await handlers.on_claim_button(_Callback(f"cl:e:{claim.id}", edit))

    assert len(await _debts(bound)) == 1
    assert second.sent[0][0] == replies.CLAIM_ALREADY
    assert edit.sent[0][0] == replies.CLAIM_ALREADY


async def test_a_malformed_claim_payload_is_only_acknowledged(bound):
    message = _Message()
    callback = _Callback("cl:y:abc", message)
    await handlers.on_claim_button(callback)
    assert message.sent == [] and callback.answered


async def test_the_bot_receipt_carries_the_question_and_marks_it_asked(
    bound, monkeypatch
):
    claim = await _claimed(bound)
    assert claim.asked_at is None

    async def _process(session, interaction):
        return SimpleNamespace(ok=True, applied=Applied(claims=[claim]))

    monkeypatch.setattr(handlers, "process_interaction", _process)
    monkeypatch.setattr(handlers.rag, "looks_like_question", lambda text: False)
    message = SimpleNamespace(
        text="Akmal: sen menga 5 mln qarzsan",
        date=datetime.now(TZ),
        bot=SimpleNamespace(send_chat_action=None),
        chat=SimpleNamespace(id=1),
        sent=[],
    )

    async def _answer(text, **kwargs):
        message.sent.append((text, kwargs.get("reply_markup")))

    message.answer = _answer

    await handlers.on_text(message)

    [(text, markup)] = message.sent
    assert f"❓ <code>c{claim.id}</code> <b>Akmal</b> aytdi: sen unga 5 mln so'm" in text
    assert _payloads(markup) == [
        [f"cl:y:{claim.id}", f"cl:n:{claim.id}", f"cl:e:{claim.id}"]
    ]
    assert (await claims.get(bound, claim.id)).asked_at is not None


async def test_davolar_lists_the_pending_claims_with_buttons_and_marks_them_asked(bound):
    message = _Message()
    await handlers.cmd_claims(message)
    assert message.sent[0] == (replies.CLAIMS_NONE, None)

    claim = await _claimed(bound)
    await handlers.cmd_claims(message)

    [_, (text, markup)] = message.sent
    assert text.startswith(replies.CLAIMS_HEADER)
    assert f"<code>c{claim.id}</code>" in text
    assert _payloads(markup) == [
        [f"cl:y:{claim.id}", f"cl:n:{claim.id}", f"cl:e:{claim.id}"]
    ]
    assert (await claims.get(bound, claim.id)).asked_at is not None


async def test_cmd_tuzat_corrects_a_claim_and_asks_again(bound):
    claim = await _claimed(bound)
    message = _Message()

    await handlers.cmd_edit(message, _command("tuzat", f"c{claim.id} 4 mln"))

    [(text, markup)] = message.sent
    assert text.startswith(replies.CLAIM_EDITED)
    assert "sen unga 4 mln so'm qarzsan" in text
    assert _payloads(markup) == [
        [f"cl:y:{claim.id}", f"cl:n:{claim.id}", f"cl:e:{claim.id}"]
    ]
    fresh = await claims.get(bound, claim.id)
    assert fresh.state == "pending" and fresh.payload["amount"] == 4_000_000
    assert fresh.history[-1]["field"] == "amount" and fresh.history[-1]["by"] == "command"
    assert await _debts(bound) == []


async def test_cmd_tuzat_refuses_a_field_the_claim_has_no_use_for(bound):
    claim = await _claimed(
        bound,
        ex.ExtractionResult(
            debt_settlements=[
                ex.ExtractedSettlement(
                    person="Akmal", amount=2_000_000, currency="UZS", asserted_by="them"
                )
            ]
        ),
    )
    message = _Message()

    await handlers.cmd_edit(message, _command("tuzat", f"c{claim.id} muddat 2026-10-01"))
    await handlers.cmd_edit(message, _command("tuzat", "c424242 4 mln"))

    [(refused, markup), (gone, _)] = message.sent
    assert refused.startswith("Bu da'voda muddatni tuzatib bo'lmaydi.")
    assert f"<code>/tuzat c{claim.id} summa 4 mln</code>" in refused
    assert _payloads(markup) == [
        [f"cl:y:{claim.id}", f"cl:n:{claim.id}", f"cl:e:{claim.id}"]
    ]
    assert gone == replies.CLAIM_GONE
    fresh = await claims.get(bound, claim.id)
    assert fresh.history == [] and "due_date" not in fresh.payload


async def test_cmd_tuzat_on_a_claim_after_the_answer_says_so(bound):
    claim = await _claimed(bound)
    await claims.decline(bound, claim.id, by=claims.BY_BUTTON)
    message = _Message()
    await handlers.cmd_edit(message, _command("tuzat", f"c{claim.id} 4 mln"))
    assert message.sent[0][0] == replies.CLAIM_ALREADY


async def test_ertalab_counts_the_claim_and_asks_nothing(bound):
    claim = await _claimed(bound)
    message = _Message()

    await handlers.cmd_brief(message)

    [(text, markup)] = message.sent
    assert "❓ Yana 1 ta savol navbatda (1 tasi pul bo'yicha) — /savollar" in text
    assert markup is None
    assert (await claims.get(bound, claim.id)).asked_at is None


async def test_the_gathered_brief_and_report_see_the_pending_claims(session):
    claim = await _claimed(session)
    report = await reports.gather(session, datetime.now(TZ).date())
    assert report.queue.waiting == 1
    await claims.decline(session, claim.id, by=claims.BY_COMMAND)
    assert (await reports.gather(session, datetime.now(TZ).date())).queue.waiting == 0
    assert claim.id


async def test_accepting_a_fulfilment_closes_the_promise_and_receipts_it(bound):
    """The fulfilment claim: "hujjatlarni yubordim" said by Akmal about his own
    promise — asked, and on Ha the promise is marked done with the claim in
    its history."""
    opened = await _interaction(bound, "Akmal hujjatlarni yuboradi")
    await apply_extraction(
        bound,
        opened,
        ex.ExtractionResult(
            promises=[
                ex.ExtractedPromise(
                    made_by="them", person="Akmal", description="hujjatlarni yuborish"
                )
            ]
        ),
    )
    claim = await _claimed(
        bound,
        ex.ExtractionResult(
            fulfilments=[
                ex.ExtractedFulfilment(
                    made_by="them",
                    person="Akmal",
                    description="hujjatlarni yubordim",
                    asserted_by="them",
                )
            ]
        ),
    )
    assert claim.kind == claims.KIND_FULFILMENT
    message = _Message(reply_markup=keyboards.claim_actions([claim.id]))

    await handlers.on_claim_button(_Callback(f"cl:y:{claim.id}", message))

    promise = await bound.scalar(sa.select(m.Promise))
    assert promise.status is PromiseStatus.done
    assert promise.history[-1]["field"] == "claim"
    [(text, _)] = message.sent
    assert text.startswith(replies.CLAIM_ACCEPTED_PREFIX)
    assert "Va'da bajarilildi" not in text and "Va'da bajarildi" in text

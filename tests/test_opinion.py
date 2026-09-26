"""WP-58: opinion mode, cited evidence, "topilmadi" without a model call,
and the amount guard on every answer."""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa

from miya.bot import handlers
from miya.config import settings
from miya.db import models as m
from miya.db.enums import ChatType, Direction, InteractionSource
from miya.services import amounts, passages, rag
from tests.test_close_and_correct import _command, _Message, bound  # noqa: F401
from tests.test_pipeline import _open_debt
from tests.test_rag import _StubClient, _text_response, _tool_use_response

TZ = settings.tz
NOW = datetime.now(TZ).replace(microsecond=0)
DM = 8101
_ids = itertools.count(1)


@pytest.fixture(autouse=True)
def no_embedder(monkeypatch):
    """Word search only: no test here may wait on a real embedder."""

    def none():
        raise RuntimeError("no embedder in tests")

    monkeypatch.setattr(rag, "get_embedder", none)


@pytest.mark.parametrize(
    ("text", "mode"),
    [
        ("Akmal bilan konteyner masalasi bo'lgandi nima deb oylaysan", "opinion"),
        ("shunday narsa bolgandi nima deb oylaysan", "opinion"),
        ("что думаешь про Акмала", "opinion"),
        ("esingdami Sardor bojxona haqida gapirgandi", "question"),
        ("u qancha to'lashini aytdi", "note"),
        ("Akmalga 5 mln berdim", "note"),
        ("Akmal bilan gaplashgandik, 5 mln beradi", "note"),
        ("Akmal qachon keladi?", "question"),
        ("qancha qarzim bor", "question"),
    ],
)
def test_classify(text, mode):
    assert rag.classify(text) == mode


async def _voice(session, text="konteyner bojxonada ushlab qolindi, 7 mln berasan"):
    akmal = m.Person(display_name="Akmal", aliases=[])
    session.add(akmal)
    session.add(m.ChatMonitor(tg_chat_id=DM, chat_type=ChatType.private, title="Akmal"))
    await session.flush()
    row = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.in_,
        person_id=akmal.id,
        tg_chat_id=DM,
        occurred_at=NOW - timedelta(days=2),
        transcript=text,
        media={"type": "voice", "processed": True},
        meta={"tg_message_id": next(_ids)},
    )
    session.add(row)
    await session.flush()
    await passages.index_pending(session)
    return akmal, row


def _boom():
    raise AssertionError("no model call was expected")


QUESTION = "Akmal bilan konteyner masalasi bo'lgandi nima deb oylaysan"


async def test_opinion_prefetches_evidence_into_the_first_turn(session, monkeypatch):
    _, row = await _voice(session)
    stub = _StubClient([_text_response(f"<b>Yozuvlarda:</b> • «konteyner» [m{row.id}]")])
    monkeypatch.setattr(rag, "get_client", lambda: stub)
    result = await rag.answer_full(session, QUESTION, now=NOW)
    first = stub.calls[0]["messages"][0]["content"]
    assert "MODE: opinion" in first and "EVIDENCE (search_history result):" in first
    assert f"m{row.id}" in first
    assert result.refs == [f"m{row.id}"] and f"<code>m{row.id}</code>" in result.text
    assert result.text.endswith(f"<i>Asl matn: /manba m{row.id}</i>")


async def test_not_found_is_deterministic_and_calls_no_model(session, monkeypatch):
    monkeypatch.setattr(rag, "get_client", _boom)
    result = await rag.answer_full(session, "bojxona masalasi nima deb oylaysan", now=NOW)
    assert result.text.startswith(rag.NOT_FOUND) and not result.model_called
    assert rag.NOT_FOUND_HINT in result.text


async def test_need_detail_for_a_contentless_question(session, monkeypatch):
    monkeypatch.setattr(rag, "get_client", _boom)
    result = await rag.answer_full(session, "shunday narsa bolgandi nima deb oylaysan")
    assert result.text == rag.NEED_DETAIL


def test_unknown_refs_are_stripped_and_known_ones_rendered():
    text, refs = rag.render_citations("a [m1] b [m999] c [f2]", {"m1", "f2"})
    assert text == "a <code>m1</code> b  c <code>f2</code>" and refs == ["m1", "f2"]


async def test_fallback_evidence_block_when_the_model_cites_nothing(session, monkeypatch):
    _, row = await _voice(session)
    stub = _StubClient([_text_response("Menimcha hammasi yaxshi.")])
    monkeypatch.setattr(rag, "get_client", lambda: stub)
    result = await rag.answer_full(session, QUESTION, now=NOW)
    assert (
        "<b>Yozuvlarda:</b>" in result.text and f"<code>m{row.id}</code>" in result.text
    )


def test_amount_guard_replaces_an_invented_amount():
    out = amounts.guard_amounts(
        "Akmal sizga 7 mln qarz.",
        sql_texts=['{"outstanding": "5000000.00"}'],
        quote_texts=[],
    )
    assert out == f"Akmal sizga {amounts.AMOUNT_UNVERIFIED} qarz."


def test_amount_guard_keeps_sql_and_quoted_amounts():
    out = amounts.guard_amounts(
        "Qarz 5 mln. U «7 mln berasan» dedi.",
        sql_texts=['"5000000.00"'],
        quote_texts=["7 mln berasan"],
    )
    assert amounts.AMOUNT_UNVERIFIED not in out


def test_quoted_amount_used_as_a_balance_is_replaced():
    out = amounts.guard_amounts(
        "Akmal sizdan 7 mln qarz. U «7 mln berasan» degan.",
        sql_texts=['"5000000.00"'],
        quote_texts=["7 mln berasan"],
    )
    assert out == (
        f"Akmal sizdan {amounts.AMOUNT_UNVERIFIED} qarz. U «7 mln berasan» degan."
    )


async def test_amount_guard_keeps_lookup_code_and_list_transactions_amounts(
    session, monkeypatch
):
    await _open_debt(session, amount="5000000")
    stub = _StubClient(
        [
            _tool_use_response("open_debts", {"person": "Akmal"}),
            _text_response("Akmal sizga <b>5 mln UZS</b> qarzdor, 9 mln emas."),
        ]
    )
    monkeypatch.setattr(rag, "get_client", lambda: stub)
    answer = await rag.answer(session, "Akmal menga qancha qarz?", now=NOW)
    assert "5 mln UZS" in answer and amounts.AMOUNT_UNVERIFIED in answer
    assert {"lookup_code", "list_transactions", "open_debts"} <= rag.SQL_TOOLS


async def test_answer_is_stored_on_the_question_row(bound, monkeypatch):  # noqa: F811
    monkeypatch.setattr(
        rag, "get_client", lambda: _StubClient([_text_response("Javob tayyor.")])
    )
    message = _Message()
    message.text = "Akmal qachon keladi?"
    message.date = NOW
    await handlers.on_text(message)
    row = await bound.scalar(
        sa.select(m.Interaction).where(m.Interaction.raw_text == "Akmal qachon keladi?")
    )
    assert row.meta["kind"] == "question" and row.meta["answer"] == "Javob tayyor."
    assert row.meta["mode"] == "question"
    [(_, keyboard)] = message.sent
    assert keyboard.inline_keyboard[0][0].callback_data == f"vq:n:{row.id}"


async def test_fikr_and_savol_force_the_mode(bound, monkeypatch):  # noqa: F811
    seen = []

    async def fake(session, text, *, mode=None, **kw):
        seen.append(mode)
        return rag.RagAnswer(text="ok", mode=mode)

    monkeypatch.setattr(rag, "answer_full", fake)
    for command, handler in (("fikr", handlers.cmd_opinion), ("savol", handlers.cmd_ask)):
        message = _Message()
        message.date = NOW
        await handler(message, _command(command, "Akmalga 5 mln berdim"))
    assert seen == ["opinion", "question"]


async def test_bare_code_is_routed_before_classify(bound, monkeypatch):  # noqa: F811
    def never(text):
        raise AssertionError("classify must not see a bare code")

    monkeypatch.setattr(rag, "classify", never)
    message = _Message()
    message.text = "GS367?"
    message.date = NOW
    await handlers.on_text(message)
    assert message.sent

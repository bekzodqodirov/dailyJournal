"""WP-75: a follow-up question keeps the earlier question and answer."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from miya.bot import handlers
from miya.config import settings
from miya.db import models as m
from miya.db.enums import Direction, InteractionSource
from miya.services import rag, recall
from tests.test_close_and_correct import bound  # noqa: F401
from tests.test_rag import _StubClient, _text_response

TZ = settings.tz
NOW = datetime.now(TZ).replace(microsecond=0)


@pytest.fixture(autouse=True)
def no_embedder(monkeypatch):
    def none():
        raise RuntimeError("no embedder in tests")

    monkeypatch.setattr(rag, "get_embedder", none)
    monkeypatch.setattr(handlers, "get_embedder", none)


class _Sent:
    message_id = 777


def _message(text: str, *, reply_to: int | None = None, at=NOW):
    message = SimpleNamespace(
        text=text,
        date=at,
        bot=SimpleNamespace(send_chat_action=None),
        chat=SimpleNamespace(id=1),
        reply_to_message=(
            SimpleNamespace(message_id=reply_to) if reply_to is not None else None
        ),
        sent=[],
    )

    async def answer(body, **kwargs):
        message.sent.append(body)
        return _Sent()

    message.answer = answer
    return message


async def _asked(session, question: str, answer: str, *, at, refs=(), answer_id=555):
    row = m.Interaction(
        source=InteractionSource.assistant_bot,
        direction=Direction.in_,
        occurred_at=at,
        raw_text=question,
        processed=True,
        meta={
            "kind": "question",
            "mode": "question",
            "answer": answer,
            "refs": list(refs),
            "answer_message_id": answer_id,
        },
    )
    session.add(row)
    await session.flush()
    return row


async def _typing(message):
    return None


@pytest.fixture
def stub(monkeypatch):
    client = _StubClient([_text_response("Keyin konteyner chiqdi.")])
    monkeypatch.setattr(rag, "get_client", lambda: client)
    monkeypatch.setattr(handlers, "_typing", _typing)
    return client


def _turns(call) -> list[tuple[str, str]]:
    return [(m["role"], m["content"]) for m in call["messages"]]


async def test_reply_to_an_answer_is_a_question_with_history(bound, stub):  # noqa: F811
    await _asked(
        bound, "Akmal qachon to'laydi?", "Juma kuni.", at=NOW - timedelta(hours=3)
    )

    message = _message("u qaysi kartaga tashlaydi", reply_to=555)
    await handlers.on_text(message)

    turns = _turns(stub.calls[0])
    assert turns[0] == ("user", "Akmal qachon to'laydi?")
    assert turns[1] == ("assistant", "Juma kuni.")
    assert turns[-1][1].endswith("u qaysi kartaga tashlaydi")
    row = await bound.scalar(
        sa.select(m.Interaction).where(
            m.Interaction.raw_text == "u qaysi kartaga tashlaydi"
        )
    )
    assert row.meta["kind"] == "question"


async def test_recent_question_is_included_within_the_window(bound, stub):  # noqa: F811
    await _asked(bound, "Akmal qachon keladi?", "Ertaga.", at=NOW - timedelta(minutes=10))

    await handlers.on_text(_message("Vali-chi, qachon keladi?"))

    assert _turns(stub.calls[0])[:2] == [
        ("user", "Akmal qachon keladi?"),
        ("assistant", "Ertaga."),
    ]


async def test_old_question_is_not_included(bound, stub):  # noqa: F811
    await _asked(bound, "Akmal qachon keladi?", "Ertaga.", at=NOW - timedelta(hours=2))

    await handlers.on_text(_message("Vali qachon keladi?"))

    assert len(stub.calls[0]["messages"]) == 1


async def test_history_refs_are_valid_citations(session, monkeypatch):
    client = _StubClient([_text_response("Oldin aytilgandek [m42].")])
    monkeypatch.setattr(rag, "get_client", lambda: client)
    turn = rag.HistoryTurn(question="Akmal?", answer="Bojxonada [m42]", refs=["m42"])

    result = await rag.answer_full(session, "keyin-chi?", now=NOW, history=(turn,))

    assert "<code>m42</code>" in result.text and "m42" in result.refs


async def test_followup_inherits_person_for_prefetch(session, monkeypatch):
    seen = []

    async def search(session, embedder, text, **kwargs):
        seen.append(text)
        return recall.RecallResult()

    monkeypatch.setattr(recall, "search", search)
    turn = rag.HistoryTurn(
        question="Akmal bilan konteyner masalasi nima bo'lgandi?", answer="…"
    )

    await rag.answer_full(
        session, "keyin nima bo'lgandi?", now=NOW, history=(turn,), mode="question"
    )

    assert seen and "Akmal" in seen[0]

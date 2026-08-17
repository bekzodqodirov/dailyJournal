"""RAG chat (Phase 3): question routing, SQL-backed tools, the Sonnet loop.

The Anthropic client is stubbed with scripted responses; the tools run against
the real database — the point under test is that every figure in the model's
context came from SQL.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from types import SimpleNamespace

import anthropic
import httpx
import pytest
import sqlalchemy as sa

from miya.config import settings
from miya.db import models as m
from miya.db.enums import Currency, TransactionType
from miya.services import rag
from tests.test_pipeline import _interaction, _open_debt

TZ = settings.tz


# --- question routing --------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Akmal menga qancha qarz?", True),
        ("qancha qarzim bor", True),  # leading interrogative, no question mark
        ("Сколько он должен", True),
        ("who owes me money", True),
        ("Akmalga 5 mln berdim", False),
        ("u qancha to'lashini aytdi", False),  # mid-sentence question word
        ("ertaga soat 15:00 da uchrashuv", False),
        ("", False),
    ],
)
def test_looks_like_question(text, expected):
    assert rag.looks_like_question(text) is expected


# --- tools read straight from SQL --------------------------------------------


async def test_open_debts_tool_returns_exact_sql_figures(session):
    await _open_debt(session, amount="5000000", due=date(2026, 8, 25))
    await session.flush()

    output = json.loads(await rag._run_tool(session, None, "open_debts", {}))

    assert output == [
        {
            "person": "Akmal",
            "direction": "they_owe_me",
            "currency": "UZS",
            "outstanding": "5000000.00",
            "earliest_due": "2026-08-25",
            "debt_count": 1,
        }
    ]


async def test_open_debts_tool_reports_an_unknown_person(session):
    output = json.loads(
        await rag._run_tool(session, None, "open_debts", {"person": "Zebiniso"})
    )
    assert "error" in output


async def test_spending_summary_tool_totals_by_currency(session):
    interaction = await _interaction(session)
    now = datetime.now(TZ)
    session.add_all(
        [
            m.Transaction(
                type=TransactionType.expense,
                amount=1_200_000,
                currency=Currency.UZS,
                category="transport",
                occurred_at=now,
                source_interaction_id=interaction.id,
            ),
            m.Transaction(
                type=TransactionType.income,
                amount=300,
                currency=Currency.USD,
                category="client",
                occurred_at=now,
                source_interaction_id=interaction.id,
            ),
        ]
    )
    await session.flush()

    day = now.date().isoformat()
    output = json.loads(
        await rag._run_tool(
            session, None, "spending_summary", {"date_from": day, "date_to": day}
        )
    )

    assert output["expense"] == {"UZS": "1200000.00"}
    assert output["income"] == {"USD": "300.00"}
    assert output["top_expense_categories"][0]["category"] == "transport"


async def test_search_memories_tool_degrades_without_an_embedder(session):
    output = json.loads(
        await rag._run_tool(session, None, "search_memories", {"query": "yuk"})
    )
    assert "error" in output


# --- the Sonnet loop ---------------------------------------------------------


def _usage() -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=100,
        output_tokens=50,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )


def _tool_use_response(name: str, args: dict) -> SimpleNamespace:
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="tool_use", name=name, input=args, id="tu_1"),
        ],
        stop_reason="tool_use",
        usage=_usage(),
    )


def _text_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=_usage(),
    )


class _StubClient:
    def __init__(self, responses: list) -> None:
        self.messages = self  # client.messages.create → self.create
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


async def test_a_money_question_is_answered_from_the_sql_tool(session, monkeypatch):
    await _open_debt(session, amount="5000000")
    await session.flush()

    stub = _StubClient(
        [
            _tool_use_response("open_debts", {"person": "Akmal"}),
            _text_response("Akmal sizga <b>5 mln UZS</b> qarzdor."),
        ]
    )
    monkeypatch.setattr(rag, "get_client", lambda: stub)

    answer = await rag.answer(session, "Akmal menga qancha qarz?")

    assert answer == "Akmal sizga <b>5 mln UZS</b> qarzdor."
    # The exact SQL figure was in the model's context before it answered.
    tool_result = stub.calls[1]["messages"][2]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert "5000000.00" in tool_result["content"]
    assert "they_owe_me" in tool_result["content"]

    await session.flush()  # autoflush is off; make the pending usage rows visible
    spent = list(
        await session.scalars(sa.select(m.UsageLog).where(m.UsageLog.operation == "rag"))
    )
    assert len(spent) == 2


async def test_an_api_failure_yields_the_fallback_answer(session, monkeypatch):
    error = anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.anthropic.com")
    )
    stub = _StubClient([error])
    monkeypatch.setattr(rag, "get_client", lambda: stub)

    answer = await rag.answer(session, "Akmal menga qancha qarz?")
    assert answer == rag.FALLBACK_ANSWER


async def test_a_broken_tool_does_not_kill_the_answer(session, monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("tool exploded")

    monkeypatch.setattr(rag, "_run_tool", _boom)
    stub = _StubClient(
        [
            _tool_use_response("open_debts", {}),
            _text_response("javob"),
        ]
    )
    monkeypatch.setattr(rag, "get_client", lambda: stub)

    answer = await rag.answer(session, "qancha qarz?")

    assert answer == "javob"
    tool_result = stub.calls[1]["messages"][2]["content"][0]
    assert "error" in tool_result["content"]


async def test_the_tool_round_limit_is_enforced(session, monkeypatch):
    stub = _StubClient(
        [_tool_use_response("due_items", {}) for _ in range(rag.MAX_TOOL_ROUNDS)]
    )
    monkeypatch.setattr(rag, "get_client", lambda: stub)

    answer = await rag.answer(session, "nima qoldi?")

    assert answer == rag.FALLBACK_ANSWER
    assert len(stub.calls) == rag.MAX_TOOL_ROUNDS

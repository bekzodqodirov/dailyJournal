"""WP-11: the ledger. A voided transaction and an internal transfer are not
money: no total, list or tool may count them. A voided row proves no
contact; an internal transfer still does."""

from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from miya.api.main import app
from miya.config import settings
from miya.db import models as m
from miya.db.enums import Currency, TransactionType
from miya.services import queries, rag

TZ = settings.tz
TOKEN = "ledger-token-" + "x" * 51
PACKAGE = Path(__file__).resolve().parents[1] / "miya"


def _txn(amount: str, *, category="transport", **over) -> m.Transaction:
    fields = {
        "type": TransactionType.expense,
        "amount": Decimal(amount),
        "currency": Currency.UZS,
        "category": category,
        "occurred_at": datetime.now(TZ) - timedelta(minutes=5),
    }
    fields.update(over)
    return m.Transaction(**fields)


@pytest.fixture
async def ledger(session):
    """One live expense, one voided, one internal — and one live income."""
    session.add_all(
        [
            _txn("100000"),
            _txn("900000", category="voided", voided_at=datetime.now(TZ)),
            _txn("700000", category="internal", is_internal=True),
            _txn("50000", type=TransactionType.income, category=None),
            _txn(
                "800000",
                type=TransactionType.income,
                category=None,
                voided_at=datetime.now(TZ),
                void_reason="reinstall_duplicate",
            ),
        ]
    )
    await session.commit()


async def test_day_summary_counts_only_active_rows(session, ledger):
    summary = await queries.day_summary(session)
    assert summary.expense == {Currency.UZS: Decimal("100000.00")}
    assert summary.income == {Currency.UZS: Decimal("50000.00")}
    assert [c for c, _, _ in summary.by_category] == ["transport"]
    assert [t.amount for t in summary.biggest] == [Decimal("100000.00")]


async def test_spending_summary_counts_only_active_rows(session, ledger):
    today = datetime.now(TZ).date()
    summary = await queries.spending_summary(session, today, today)
    assert summary.expense == {Currency.UZS: Decimal("100000.00")}
    assert summary.income == {Currency.UZS: Decimal("50000.00")}
    assert [t.amount for t in summary.biggest] == [Decimal("100000.00")]


async def test_the_api_and_the_rag_tool_count_only_active_rows(
    session, ledger, monkeypatch
):
    today = datetime.now(TZ).date().isoformat()
    monkeypatch.setattr(settings, "api_bearer_token", TOKEN)
    with TestClient(app) as client:
        client.headers.update({"Authorization": f"Bearer {TOKEN}"})
        body = client.get(f"/v1/transactions?date_from={today}&date_to={today}").json()
    assert body["expense"] == {"UZS": "100000.00"}
    assert body["income"] == {"UZS": "50000.00"}

    tool = json.loads(
        await rag._run_tool(
            session, None, "spending_summary", {"date_from": today, "date_to": today}
        )
    )
    assert tool["expense"] == {"UZS": "100000.00"}


async def test_an_internal_transfer_is_contact_but_a_voided_row_is_not(session):
    akmal = m.Person(display_name="Akmal", aliases=[])
    vali = m.Person(display_name="Vali", aliases=[])
    session.add_all([akmal, vali])
    await session.flush()
    internal_at = datetime.now(TZ) - timedelta(days=2)
    session.add_all(
        [
            _txn(
                "1000",
                counterparty_person_id=akmal.id,
                is_internal=True,
                occurred_at=internal_at,
            ),
            _txn("1000", counterparty_person_id=vali.id, voided_at=datetime.now(TZ)),
        ]
    )
    await session.commit()

    assert await queries.last_contact_at(session, akmal.id) == internal_at
    assert await queries.last_contact_at(session, vali.id) is None


def _sum_statements_without_filter(tree: ast.AST) -> list[int]:
    """Lines of a sum over Transaction.amount whose enclosing statement
    never mentions ACTIVE_TXN."""
    offenders = []
    for statement in ast.walk(tree):
        if not isinstance(statement, ast.stmt) or isinstance(
            statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
        ):
            continue
        source_names = {
            node.id if isinstance(node, ast.Name) else node.attr
            for node in ast.walk(statement)
            if isinstance(node, ast.Name | ast.Attribute)
        }
        for node in ast.walk(statement):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "sum"
                and node.args
                and ast.unparse(node.args[0]) == "Transaction.amount"
                and "ACTIVE_TXN" not in source_names
            ):
                offenders.append(node.lineno)
    return sorted(set(offenders))


def test_every_sum_over_transactions_filters_active_rows():
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(), str(path))
        offenders += [
            f"{path.name}:{line}" for line in _sum_statements_without_filter(tree)
        ]
    assert offenders == [], f"sum(Transaction.amount) without ACTIVE_TXN: {offenders}"


def test_the_sum_guard_catches_what_it_is_for():
    bad = "totals = sa.select(sa.func.sum(Transaction.amount)).where(x)"
    good = "totals = sa.select(sa.func.sum(Transaction.amount)).where(ACTIVE_TXN)"
    assert _sum_statements_without_filter(ast.parse(bad)) == [1]
    assert _sum_statements_without_filter(ast.parse(good)) == []

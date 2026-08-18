"""The internal HTTP layer (spec §11).

These endpoints exist for a future dashboard and hold no logic of their own —
what matters is that they read through the same services the bot uses, so an
HTTP client and Telegram can never disagree about a balance.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from miya.api.main import app
from miya.config import settings
from miya.db import models as m
from miya.db.enums import Currency, DebtDirection, DebtStatus, InteractionSource
from miya.db.session import SessionLocal

TZ = settings.tz
TOKEN = "test-token"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "api_bearer_token", TOKEN)
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield c


@pytest.fixture
async def seeded(session):
    """One person owing 5 mln, with a promise and today's expense."""
    person = m.Person(display_name="Akmal", aliases=[])
    session.add(person)
    await session.flush()

    interaction = m.Interaction(
        source=InteractionSource.assistant_bot,
        occurred_at=datetime.now(TZ),
        raw_text="test",
    )
    session.add(interaction)
    await session.flush()

    debt = m.Debt(
        direction=DebtDirection.they_owe_me,
        person_id=person.id,
        amount=Decimal("5000000"),
        currency=Currency.UZS,
        due_date=date(2026, 8, 25),
        source_interaction_id=interaction.id,
    )
    session.add_all(
        [
            debt,
            m.Promise(
                made_by="them",
                person_id=person.id,
                description="juma kuni qaytaradi",
                source_interaction_id=interaction.id,
            ),
            m.Transaction(
                type="expense",
                amount=Decimal("1200000"),
                currency=Currency.UZS,
                category="transport",
                occurred_at=datetime.now(TZ),
                source_interaction_id=interaction.id,
            ),
        ]
    )
    await session.commit()
    yield person, debt


async def test_debts_are_reported_as_exact_strings(client, seeded):
    person, _ = seeded
    body = client.get("/v1/debts").json()

    assert body["debts"] == [
        {
            "person_id": person.id,
            "person": "Akmal",
            "direction": "they_owe_me",
            "currency": "UZS",
            "outstanding": "5000000.00",
            "earliest_due": "2026-08-25",
            "debt_count": 1,
        }
    ]


async def test_debts_can_be_filtered_by_direction(client, seeded):
    assert client.get("/v1/debts?direction=i_owe_them").json()["debts"] == []
    assert len(client.get("/v1/debts?direction=they_owe_me").json()["debts"]) == 1


async def test_settling_a_debt_updates_its_status(client, seeded):
    _, debt = seeded

    partial = client.post(
        f"/v1/debts/{debt.id}/settle", json={"amount": "2000000", "currency": "UZS"}
    ).json()
    assert partial["status"] == "partially_paid"
    assert partial["outstanding"] == "3000000.00"

    full = client.post(
        f"/v1/debts/{debt.id}/settle", json={"amount": "3000000", "currency": "UZS"}
    ).json()
    assert full["status"] == "settled"
    assert full["outstanding"] == "0.00"

    # And the balance query — the one the bot uses — agrees.
    assert client.get("/v1/debts").json()["debts"] == []


async def test_settling_in_the_wrong_currency_is_refused(client, seeded):
    _, debt = seeded
    response = client.post(
        f"/v1/debts/{debt.id}/settle", json={"amount": "100", "currency": "USD"}
    )
    assert response.status_code == 422
    assert "UZS" in response.json()["detail"]


async def test_settling_a_missing_debt_is_a_404(client, seeded):
    assert client.post("/v1/debts/999999/settle", json={"amount": "1"}).status_code == 404


async def test_a_non_positive_payment_is_rejected(client, seeded):
    _, debt = seeded
    assert (
        client.post(f"/v1/debts/{debt.id}/settle", json={"amount": "0"}).status_code
        == 422
    )


async def test_promises_and_person_summary_read_through_the_services(client, seeded):
    person, _ = seeded

    promises = client.get("/v1/promises").json()["promises"]
    assert promises[0]["description"] == "juma kuni qaytaradi"

    summary = client.get(f"/v1/people/{person.id}/summary").json()
    assert summary["person"]["display_name"] == "Akmal"
    assert summary["balances"][0]["outstanding"] == "5000000.00"
    assert summary["total_interactions"] >= 0

    assert client.get("/v1/people/999999/summary").status_code == 404


async def test_transactions_report_totals_per_currency(client, seeded):
    today = datetime.now(TZ).date().isoformat()
    body = client.get(f"/v1/transactions?date_from={today}&date_to={today}").json()

    assert body["expense"] == {"UZS": "1200000.00"}
    assert body["by_category"][0]["category"] == "transport"


async def test_usage_reports_spend(client, session):
    session.add(
        m.UsageLog(
            provider="anthropic",
            model=settings.extract_model,
            operation="extract",
            input_tokens=1000,
            output_tokens=100,
            cost_usd=Decimal("0.001500"),
        )
    )
    await session.commit()

    today = datetime.now(TZ).date().isoformat()
    body = client.get(f"/v1/usage?date_from={today}&date_to={today}").json()

    assert body["total_usd"] == "0.001500"
    assert body["rows"][0]["operation"] == "extract"


async def test_ask_goes_through_the_same_rag_path(client, monkeypatch, session):
    from miya.services import rag

    seen = {}

    async def _fake_answer(sess, question, **kwargs):
        seen["question"] = question
        return "Akmal sizga 5 mln so'm qarzdor."

    monkeypatch.setattr(rag, "answer", _fake_answer)

    body = client.post("/v1/ask", json={"question": "Akmal qancha qarz?"}).json()

    assert body["answer"] == "Akmal sizga 5 mln so'm qarzdor."
    assert seen["question"] == "Akmal qancha qarz?"


def test_every_v1_route_requires_the_token(monkeypatch):
    monkeypatch.setattr(settings, "api_bearer_token", TOKEN)
    with TestClient(app) as anonymous:
        for path in ("/v1/debts", "/v1/promises", "/v1/config"):
            assert anonymous.get(path).status_code == 401, path
        assert anonymous.post("/v1/ask", json={"question": "x"}).status_code == 401


async def test_the_api_and_the_bot_never_disagree_about_a_balance(client, seeded):
    """Both read services/queries.py, so the numbers are the same object."""
    from miya.services import queries

    http = client.get("/v1/debts").json()["debts"][0]
    async with SessionLocal() as fresh:
        [balance] = await queries.open_debts(fresh)

    assert http["outstanding"] == str(balance.outstanding)
    assert http["direction"] == balance.direction.value


async def test_a_settled_debt_from_http_is_invisible_to_the_bot_query(client, seeded):
    _, debt = seeded
    client.post(f"/v1/debts/{debt.id}/settle", json={"amount": "5000000"})

    from miya.services import queries

    async with SessionLocal() as fresh:
        assert await queries.open_debts(fresh) == []
        row = await fresh.get(m.Debt, debt.id)
        assert row.status is DebtStatus.settled

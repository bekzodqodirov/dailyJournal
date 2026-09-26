"""WP-31: every command that names a person accepts a client code.

A code is exactly one known client or plainly unknown — never a fuzzy guess,
never a person created under the code's name, and a destructive `/unut`
never picks between two people.
"""

from __future__ import annotations

import json
from decimal import Decimal

import sqlalchemy as sa

from miya.bot import handlers, replies
from miya.db import models as m
from miya.db.enums import Currency, DebtDirection
from miya.services import claims, codes, rag
from miya.services import extraction as ex
from miya.services.persistence import apply_extraction
from tests.test_claims_surface import _interaction
from tests.test_close_and_correct import _command, _Message, bound  # noqa: F401


async def _person(session, name: str, code: str | None = None) -> m.Person:
    person = m.Person(display_name=name, aliases=[])
    session.add(person)
    await session.flush()
    if code:
        await codes.attach(session, person, code, source="command", by="command")
    return person


async def _debt(session, person: m.Person, amount: str = "5000000") -> m.Debt:
    debt = m.Debt(
        direction=DebtDirection.they_owe_me,
        person_id=person.id,
        amount=Decimal(amount),
        currency=Currency.UZS,
    )
    session.add(debt)
    await session.flush()
    return debt


def _reply(message: _Message) -> str:
    [(text, _)] = message.sent
    return text


async def test_kim_by_code_shows_the_holder(bound):  # noqa: F811
    await _person(bound, "Akmal", "GS367")
    message = _Message()
    await handlers.cmd_person(message, _command("kim", "GS367"))
    assert "Akmal" in _reply(message)


async def test_kim_by_an_unknown_code_says_so(bound):  # noqa: F811
    message = _Message()
    await handlers.cmd_person(message, _command("kim", "gs 999"))
    assert _reply(message) == replies.code_unknown("GS999")


def test_tarix_reads_a_spaced_code_as_one_token():
    assert handlers._parse_history_args("GS 367") == ("GS367", replies.TARIX_DEFAULT)
    assert handlers._parse_history_args("GS 367 20") == ("GS367", 20)


async def test_tarix_by_code_resolves_to_the_holder(bound, monkeypatch):  # noqa: F811
    akmal = await _person(bound, "Akmal", "GS367")
    seen = {}

    async def timeline(session, person_id, *, limit):
        seen.update(person_id=person_id, limit=limit)
        return []

    monkeypatch.setattr(handlers.queries, "timeline", timeline)
    await handlers.cmd_history(_Message(), _command("tarix", "GS 367"))
    assert seen == {"person_id": akmal.id, "limit": replies.TARIX_DEFAULT}


async def test_eslab_by_code_stores_the_memory_on_the_holder(bound):  # noqa: F811
    akmal = await _person(bound, "Akmal", "GS367")
    await handlers.cmd_remember(_Message(), _command("eslab", "GS367: mashinasi oq"))
    [memory] = list(
        await bound.scalars(sa.select(m.Memory).where(m.Memory.person_id == akmal.id))
    )
    assert memory.content == "mashinasi oq"


async def test_tuzat_moves_a_debt_to_the_code_holder(bound):  # noqa: F811
    akmal = await _person(bound, "Akmal", "GS367")
    vali = await _person(bound, "Vali", "GS412")
    debt = await _debt(bound, akmal)
    await handlers.cmd_edit(_Message(), _command("tuzat", f"d{debt.id} GS412"))
    await bound.refresh(debt)
    assert debt.person_id == vali.id
    assert debt.history[-1]["field"] == "person"


async def test_tuzat_to_an_unknown_code_never_asks_for_a_new_person(bound):  # noqa: F811
    akmal = await _person(bound, "Akmal", "GS367")
    debt = await _debt(bound, akmal)
    message = _Message()
    await handlers.cmd_edit(message, _command("tuzat", f"d{debt.id} GS999"))
    assert _reply(message) == replies.code_unknown("GS999")
    assert "Yangi odam" not in _reply(message)
    await bound.refresh(debt)
    assert debt.person_id == akmal.id
    assert list(await bound.scalars(sa.select(m.Person.display_name))) == ["Akmal"]


async def test_tuzat_a_claim_to_a_code_sets_its_person(bound):  # noqa: F811
    akmal = await _person(bound, "Akmal", "GS367")
    interaction = await _interaction(bound, "Sardor: sen menga 5 mln qarzsan")
    applied = await apply_extraction(
        bound,
        interaction,
        ex.ExtractionResult(
            debts=[
                ex.ExtractedDebt(
                    direction="i_owe_them",
                    person="Sardor",
                    amount=5_000_000,
                    currency="UZS",
                    reason="yuk",
                    asserted_by="them",
                )
            ]
        ),
    )
    [claim] = applied.claims
    await handlers.cmd_edit(_Message(), _command("tuzat", f"c{claim.id} GS367"))
    fresh = await claims.get(bound, claim.id)
    assert fresh.person_id == akmal.id
    assert fresh.person_name == "Akmal (GS367)"

    message = _Message()
    await handlers.cmd_edit(message, _command("tuzat", f"c{claim.id} GS999"))
    assert replies.code_unknown("GS999") in message.sent[0][0]


async def test_unut_by_code_previews_the_holder(bound):  # noqa: F811
    await _person(bound, "Akmal", "GS367")
    await _person(bound, "Vali")
    plan, payload = await handlers._build_purge_plan(bound, "GS367")
    assert not isinstance(plan, str)
    akmal_id = await bound.scalar(
        sa.select(m.Person.id).where(m.Person.display_name == "Akmal")
    )
    assert payload == f"unut:p:{akmal_id}"


async def test_unut_between_two_namesakes_asks_back_without_a_button(bound):  # noqa: F811
    await _person(bound, "Akmal", "GS367")
    await _person(bound, "Akmal", "GS412")
    message = _Message()
    await handlers.cmd_purge(message, _command("unut", "Akmal"))
    [(text, keyboard)] = message.sent
    assert "Kimni nazarda tutding" in text
    assert "GS367" in text and "GS412" in text
    assert "/unut GS" in text
    assert keyboard is None


async def test_rag_open_debts_by_a_code_returns_only_the_holder(session):
    akmal = await _person(session, "Akmal", "GS367")
    vali = await _person(session, "Vali")
    await _debt(session, akmal, "5000000")
    await _debt(session, vali, "7000000")
    output = json.loads(
        await rag._run_tool(session, None, "open_debts", {"person": "gs 367"})
    )
    assert [row["person"] for row in output] == ["Akmal"]

    missing = json.loads(
        await rag._run_tool(session, None, "open_debts", {"person": "GS999"})
    )
    assert missing == {"error": "client code not assigned: GS999"}

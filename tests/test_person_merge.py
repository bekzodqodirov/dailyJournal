"""WP-77: merging two people, and placeholder people for unheld codes."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest
import sqlalchemy as sa

from miya.bot import handlers, replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import (
    ChatType,
    Currency,
    DebtDirection,
    Direction,
    InteractionSource,
    PromiseMadeBy,
    TransactionType,
)
from miya.services import codes, extraction, people, persistence
from tests.test_close_and_correct import (  # noqa: F401
    _Callback,
    _command,
    _Message,
    bound,
)

NOW = datetime.now(settings.tz).replace(microsecond=0)


async def _person(session, name, **fields) -> m.Person:
    person = m.Person(display_name=name, aliases=fields.pop("aliases", []), **fields)
    session.add(person)
    await session.flush()
    return person


async def _everything(session, person) -> dict:
    row = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.in_,
        person_id=person.id,
        tg_chat_id=4040,
        occurred_at=NOW,
        raw_text="salom",
    )
    session.add(row)
    await session.flush()
    session.add_all(
        [
            m.Debt(
                direction=DebtDirection.they_owe_me,
                person_id=person.id,
                amount=Decimal("100"),
                currency=Currency.UZS,
            ),
            m.Promise(made_by=PromiseMadeBy.them, person_id=person.id, description="x"),
            m.Transaction(
                type=TransactionType.expense,
                amount=Decimal("5"),
                currency=Currency.UZS,
                occurred_at=NOW,
                counterparty_person_id=person.id,
            ),
            m.ConversationWindow(
                tg_chat_id=4040,
                person_id=person.id,
                started_at=NOW,
                ended_at=NOW,
                message_count=1,
                char_count=5,
                text="x",
                custom_id=f"w-merge-{person.id}",
            ),
            m.Memory(content="fakt", person_id=person.id, occurred_at=NOW, tags=[]),
            m.Claim(
                interaction_id=row.id,
                person_id=person.id,
                person_name="x",
                kind="debt",
                payload={},
            ),
            m.Passage(
                interaction_id=row.id,
                chunk_no=0,
                source=InteractionSource.telegram_userbot,
                occurred_at=NOW,
                body="salom",
                embed_text="salom",
                search_norm="salom",
                speaker_person_id=person.id,
                chat_person_id=person.id,
            ),
        ]
    )
    await session.flush()
    await codes.attach(session, person, "GS501", source="command", by="command")
    return {"interaction": row}


async def test_every_fk_row_moves(session):
    source = await _person(session, "Акмал")
    target = await _person(session, "Akmal")
    await _everything(session, source)

    plan = await people.merge_plan(session, source, target)
    assert plan["debts"] == 1 and plan["passages"] == 2 and plan["codes"] == 1
    await people.merge_into(session, source, target, by="command")

    after = await people.merge_plan(session, target, target)
    assert after == plan
    assert await session.get(m.Person, source.id) is None


async def test_aliases_merge_without_duplicates_or_codes(session):
    source = await _person(session, "Akmal GS367", aliases=["Акмал", "Akmal aka"])
    target = await _person(session, "Akmal", aliases=["Akmal aka"])

    merged = await people.merge_into(session, source, target, by="command")

    # "Акмал" is "Akmal" in Latin, "Akmal aka" is already there, and a
    # spelling with a code in it is never an alias.
    assert merged.aliases == ["Akmal aka"]


async def test_two_telegram_accounts_are_refused(session):
    source = await _person(session, "Akmal", telegram_id=111)
    target = await _person(session, "Akmal K", telegram_id=222)

    with pytest.raises(people.MergeRefused):
        await people.merge_into(session, source, target, by="command")


async def test_a_code_on_both_sides_ends_as_one_row(session):
    source = await _person(session, "Акмал", telegram_id=333)
    target = await _person(session, "Akmal")
    await codes.attach(session, target, "GS367", source="command", by="command")
    session.add(
        m.ClientCode(code="GS367", person_id=source.id, status="rejected", source="auto")
    )
    await session.flush()

    merged = await people.merge_into(session, source, target, by="command")

    rows = list(await session.scalars(sa.select(m.ClientCode)))
    assert [(r.code, r.person_id) for r in rows] == [("GS367", merged.id)]
    assert merged.telegram_id == 333


async def test_placeholder_folds_into_the_named_person(bound, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "code_placeholders", True)
    row = m.Interaction(
        source=InteractionSource.assistant_bot,
        direction=Direction.in_,
        occurred_at=NOW,
        raw_text="GS367 5 mln qarz",
    )
    bound.add(row)
    await bound.flush()
    applied = persistence.Applied()
    await persistence.write_debt(
        bound,
        row,
        extraction.ExtractedDebt(
            direction="they_owe_me", person="GS367", amount=5_000_000
        ),
        applied,
        now=NOW,
    )
    [placeholder] = list(await bound.scalars(sa.select(m.Person)))
    assert placeholder.display_name == "GS367"
    akmal = await _person(bound, "Akmal")

    message = _Message()
    await handlers.cmd_code(message, _command("kod", "Akmal GS367"))

    assert message.sent[0][0] == replies.placeholder_merged("GS367", "Akmal")
    debt = await bound.scalar(sa.select(m.Debt))
    assert debt.person_id == akmal.id
    assert await codes.holder(bound, "GS367") == akmal


async def test_default_writes_no_debt_for_an_unknown_code(session, monkeypatch):
    monkeypatch.setattr(settings, "code_placeholders", False)
    row = m.Interaction(
        source=InteractionSource.assistant_bot,
        direction=Direction.in_,
        occurred_at=NOW,
        raw_text="GS999 5 mln qarz",
    )
    session.add(row)
    await session.flush()
    applied = persistence.Applied()
    await persistence.write_debt(
        session,
        row,
        extraction.ExtractedDebt(
            direction="they_owe_me", person="GS999", amount=5_000_000
        ),
        applied,
        now=NOW,
    )

    assert await session.scalar(sa.select(sa.func.count(m.Debt.id))) == 0
    assert applied.unknown_codes == ["GS999"]


async def test_birlashtir_previews_and_a_stale_button_is_handled(bound):  # noqa: F811
    source = await _person(bound, "Акмал Каримов")
    target = await _person(bound, "Vali Toshmatov")
    bound.add(m.ChatMonitor(tg_chat_id=5050, chat_type=ChatType.private, title="x"))
    await bound.flush()

    message = _Message()
    await handlers.cmd_merge(
        message, _command("birlashtir", "Акмал Каримов > Vali Toshmatov")
    )
    text, markup = message.sent[0]
    assert text.startswith("🔗 <b>Акмал Каримов</b> → <b>Vali Toshmatov</b>")
    assert markup.inline_keyboard[0][0].callback_data == f"birl:{source.id}:{target.id}"

    done = _Message()
    await handlers.on_merge_button(_Callback(f"birl:{source.id}:{target.id}", done))
    stale = _Message()
    await handlers.on_merge_button(_Callback(f"birl:{source.id}:{target.id}", stale))

    assert await bound.get(m.Person, source.id) is None
    usage = _Message()
    await handlers.cmd_merge(usage, _command("birlashtir", "faqat bitta"))
    assert usage.sent[0][0] == replies.MERGE_USAGE

"""WP-61: `/unut` forgets a person completely, and says what stays."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa

from miya.bot import replies
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
from miya.services import passages, purge, recall, windows

TZ = settings.tz
NOW = datetime.now(TZ).replace(microsecond=0)
GROUP = -9301


async def _person(session, name: str, **fields) -> m.Person:
    person = m.Person(display_name=name, aliases=fields.pop("aliases", []), **fields)
    session.add(person)
    await session.flush()
    return person


async def _say(session, text, *, person=None, chat=None, at=NOW, **fields):
    row = m.Interaction(
        source=InteractionSource.telegram_userbot if chat else InteractionSource.manual,
        direction=Direction.in_,
        person_id=person.id if person else None,
        tg_chat_id=chat,
        occurred_at=at,
        raw_text=text,
        **fields,
    )
    session.add(row)
    await session.flush()
    return row


async def _fact(session, content, *, person=None, source=None, at=NOW) -> m.Memory:
    memory = m.Memory(
        content=content,
        person_id=person.id if person else None,
        source_interaction_id=source.id if source else None,
        occurred_at=at,
        tags=[],
    )
    session.add(memory)
    await session.flush()
    return memory


async def _gone(session, model, row_id) -> bool:
    return (
        await session.scalar(
            sa.select(sa.func.count()).select_from(model).where(model.id == row_id)
        )
    ) == 0


async def _purge(session, person):
    plan = await purge.plan_person(session, person)
    result = await purge.execute(session, plan)
    return plan, result


async def test_eslab_fact_is_deleted_and_counted(session):
    akmal = await _person(session, "Akmal")
    fact = await _fact(session, "Akmal Samarqanddan", person=akmal)

    plan = await purge.plan_person(session, akmal)
    assert plan.counts["memories"] == 1 and plan.memory_ids == [fact.id]
    result = await purge.execute(session, plan)

    assert result.memories == 1
    assert await _gone(session, m.Memory, fact.id)


async def test_fact_from_someone_elses_message_is_deleted(session):
    akmal = await _person(session, "Akmal")
    vali = await _person(session, "Vali")
    said = await _say(session, "Akmal kecha Xitoydan qaytdi", person=vali)
    about = await _fact(session, "Akmal Xitoydan qaytdi", source=said)
    unrelated = await _fact(session, "Vali mashina oldi", source=said)

    await _purge(session, akmal)

    assert await _gone(session, m.Memory, about.id)
    assert not await _gone(session, m.Memory, unrelated.id)
    assert not await _gone(session, m.Interaction, said.id)


async def test_person_with_only_a_phone_number_can_be_forgotten(session):
    akmal = await _person(session, "Akmal", phone="+998901234567")

    plan = await purge.plan_person(session, akmal)
    assert plan.is_empty() is False
    assert plan.counts["person_card"] == 1
    await purge.execute(session, plan)

    assert await _gone(session, m.Person, akmal.id)


async def test_person_with_only_a_promise_is_not_nothing(session):
    akmal = await _person(session, "Akmal")
    session.add(
        m.Promise(made_by=PromiseMadeBy.them, person_id=akmal.id, description="to'laydi")
    )
    await session.flush()

    plan = await purge.plan_person(session, akmal)

    assert plan.is_empty() is False and plan.counts["promises"] == 1
    assert "va'da: 1 ta" in replies.purge_preview(plan)


async def test_group_window_is_rerendered_without_their_lines(session):
    session.add(m.ChatMonitor(tg_chat_id=GROUP, chat_type=ChatType.group, title="Yuk"))
    akmal = await _person(session, "Akmal")
    vali = await _person(session, "Vali")
    lines = [
        await _say(session, "konteyner menda", person=akmal, chat=GROUP),
        await _say(
            session,
            "yaxshi, Akmal olib kelsin",
            person=vali,
            chat=GROUP,
            at=NOW + timedelta(minutes=1),
        ),
    ]
    window = m.ConversationWindow(
        tg_chat_id=GROUP,
        person_id=None,
        started_at=lines[0].occurred_at,
        ended_at=lines[-1].occurred_at,
        message_count=2,
        char_count=40,
        text=windows.render_window(lines, {akmal.id: "Akmal", vali.id: "Vali"}),
        custom_id=f"w-{uuid.uuid4().hex}",
    )
    session.add(window)
    await session.flush()
    for line in lines:
        line.window_id = window.id
    synthetic = await _say(
        session,
        window.text,
        chat=GROUP,
        meta={"kind": "window", "window_id": window.id},
        window_id=window.id,
        summary="Akmal konteyner olib keladi",
    )

    plan, _ = await _purge(session, akmal)

    assert plan.counts["group_lines"] == 1
    await session.refresh(window)
    await session.refresh(synthetic)
    assert "konteyner menda" not in window.text
    assert "olib kelsin" in window.text and window.message_count == 1
    assert synthetic.raw_text == window.text
    assert synthetic.summary == "[o'chirilgan] konteyner olib keladi"


async def test_pending_claims_naming_them_are_deleted(session):
    akmal = await _person(session, "Akmal")
    said = await _say(session, "5 mln qarzing bor")
    named = m.Claim(interaction_id=said.id, person_name="Akmal", kind="debt", payload={})
    other = m.Claim(interaction_id=said.id, person_name="Sardor", kind="debt", payload={})
    session.add_all([named, other])
    await session.flush()

    plan, result = await _purge(session, akmal)

    assert plan.counts["claims"] == 1 and result.claims == 1
    assert await _gone(session, m.Claim, named.id)
    assert not await _gone(session, m.Claim, other.id)


async def test_transaction_description_is_scrubbed_and_amount_kept(session):
    akmal = await _person(session, "Akmal")
    txn = m.Transaction(
        type=TransactionType.expense,
        amount=Decimal("1200000"),
        currency=Currency.UZS,
        description="Akmalga konteyner uchun",
        counterparty_person_id=akmal.id,
        occurred_at=NOW,
    )
    session.add(txn)
    await session.flush()

    plan, _ = await _purge(session, akmal)

    assert plan.kept["transactions_kept"] == 1
    await session.refresh(txn)
    assert txn.amount == Decimal("1200000")
    assert "Akmal" not in txn.description and "[o'chirilgan]" in txn.description
    assert txn.counterparty_person_id is None


async def test_other_profiles_mentioning_them_are_marked_stale(session):
    akmal = await _person(session, "Akmal")
    vali = await _person(
        session, "Vali", notes="Akmal bilan sherik", profile_updated_at=NOW
    )
    sardor = await _person(session, "Sardor", notes="bojxonachi", profile_updated_at=NOW)

    plan, _ = await _purge(session, akmal)

    assert plan.profile_stale_person_ids == [vali.id]
    await session.refresh(vali)
    await session.refresh(sardor)
    assert vali.profile_updated_at is None and sardor.profile_updated_at is not None


async def test_after_purge_neither_qidir_nor_search_history_returns_their_words(session):
    akmal = await _person(session, "Akmal")
    vali = await _person(session, "Vali")
    await _say(session, "konteyner narxi 1200 dollar", person=akmal, chat=9302)
    await _fact(session, "Akmal konteyner ishini qiladi")
    await _say(session, "konteyner bojxonada", person=vali, chat=9303)
    await passages.index_pending(session)

    await _purge(session, akmal)

    result = await recall.search(session, None, "konteyner", now=NOW)
    texts = [line.text for e in result.episodes for line in e.lines] + [
        f.text for f in result.facts
    ]
    assert texts and all("1200" not in t and "Akmal" not in t for t in texts)


async def test_range_purge_deletes_eslab_facts_dated_in_range(session):
    day = date(2026, 3, 10)
    inside = await _fact(
        session, "martdagi eslatma", at=datetime(2026, 3, 10, 12, tzinfo=TZ)
    )
    outside = await _fact(
        session, "keyingi eslatma", at=datetime(2026, 3, 12, 12, tzinfo=TZ)
    )
    akmal = await _person(session, "Akmal")
    debt = m.Debt(
        direction=DebtDirection.they_owe_me,
        person_id=akmal.id,
        amount=Decimal("5000000"),
        currency=Currency.UZS,
    )
    session.add(debt)
    await session.flush()
    session.add(
        m.DebtPayment(
            debt_id=debt.id,
            amount=Decimal("1000000"),
            currency=Currency.UZS,
            paid_at=datetime(2026, 3, 10, 15, tzinfo=TZ),
        )
    )
    await session.flush()

    plan = await purge.plan_range(session, day, day)
    assert plan.memory_ids == [inside.id] and plan.is_empty() is False
    assert plan.kept == {"payments_in_range": 1}
    await purge.execute(session, plan)

    assert await _gone(session, m.Memory, inside.id)
    assert not await _gone(session, m.Memory, outside.id)
    assert (
        await session.scalar(sa.select(sa.func.count()).select_from(m.DebtPayment))
    ) == 1


async def test_preview_lists_what_stays(session):
    akmal = await _person(session, "Akmal")
    vali = await _person(session, "Vali")
    session.add(
        m.Transaction(
            type=TransactionType.expense,
            amount=Decimal("100000"),
            currency=Currency.UZS,
            description="Akmalga",
            counterparty_person_id=akmal.id,
            occurred_at=NOW,
        )
    )
    await _say(session, "Akmal ertaga keladi", person=vali, chat=9304)
    await passages.index_pending(session)

    plan = await purge.plan_person(session, akmal)
    text = replies.purge_preview(plan)

    assert "odam kartasi (ism, telefon, taxalluslar): 1 ta" in text
    assert "<b>Qoladi (o'chirilmaydi):</b>" in text
    assert "pul harakati: 1 ta — summa qoladi, ism olib tashlanadi" in text
    assert "boshqa yozuvlarda tilga olingan: 1 ta" in text

    result = await purge.execute(session, plan)
    done = replies.purge_done(plan, result)
    assert done == "🗑 <b>Akmal</b> o'chirildi: 0 ta yozuv, 0 ta xotira, 0 ta da'vo."

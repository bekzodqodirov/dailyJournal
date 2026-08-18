"""The whole userbot chain, end to end (spec §7B).

Telegram messages → conversation window → Batch API → debts in the database.
Every unit is covered elsewhere; this test exists to prove they compose, since
that is where the schema and the lifecycle actually meet.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import sqlalchemy as sa

from miya.config import settings
from miya.db import models as m
from miya.db.enums import (
    ChatType,
    DebtDirection,
    Direction,
    InteractionSource,
    WindowStatus,
)
from miya.services import batch, chats, windows
from miya.services.people import resolve_person

TZ = settings.tz
CHAT = -100777

EXTRACTED = json.dumps(
    {
        "summary": "Akmal 12 mln qarz oldi, juma kuni qaytaradi",
        "language": "uz",
        "debts": [
            {
                "direction": "they_owe_me",
                "person": "Akmal",
                "amount": 12000000,
                "currency": "UZS",
                "reason": "yuk to'lovi",
                "due_date": "2026-08-21",
            }
        ],
        "promises": [
            {
                "made_by": "them",
                "person": "Akmal",
                "description": "juma kuni qaytaradi",
                "due_date": "2026-08-21",
            }
        ],
        "facts": ["Akmal Guangzhou orqali yuk yuboradi"],
        "tags": ["logistics", "finance"],
    }
)


class _Batches:
    def __init__(self, owner):
        self._owner = owner

    async def create(self, *, requests):
        self._owner.requests = requests
        return SimpleNamespace(id="batch_chain")

    async def retrieve(self, batch_id):
        return SimpleNamespace(id=batch_id, processing_status="ended")

    async def results(self, batch_id):
        entries = self._owner.results

        async def _gen():
            for entry in entries:
                yield entry

        return _gen()


class _StubClient:
    def __init__(self):
        self.messages = SimpleNamespace(batches=_Batches(self))
        self.requests: list = []
        self.results: list = []


def _succeeded(custom_id: str, body: str):
    return SimpleNamespace(
        custom_id=custom_id,
        result=SimpleNamespace(
            type="succeeded",
            message=SimpleNamespace(
                content=[SimpleNamespace(type="text", text=body)],
                usage=SimpleNamespace(
                    input_tokens=1200,
                    output_tokens=300,
                    cache_read_input_tokens=0,
                    cache_creation_input_tokens=0,
                ),
            ),
        ),
    )


async def test_a_telegram_conversation_becomes_a_debt(session, monkeypatch):
    # 1. The chat is known and monitored, and the person is known.
    await chats.sync_dialogs(
        session,
        [chats.DialogInfo(tg_chat_id=CHAT, chat_type=ChatType.private, title="Akmal")],
    )
    person = await resolve_person(session, "Akmal", telegram_id=555)

    # 2. The userbot stored three messages an hour ago.
    start = datetime.now(TZ) - timedelta(hours=1)
    lines = [
        (False, "aka, 12 mln kerak edi"),
        (True, "mayli, bugun o'tkazaman"),
        (False, "juma kuni qaytaraman"),
    ]
    for index, (out, text) in enumerate(lines):
        session.add(
            m.Interaction(
                source=InteractionSource.telegram_userbot,
                direction=Direction.out if out else Direction.in_,
                person_id=person.id,
                tg_chat_id=CHAT,
                occurred_at=start + timedelta(minutes=index),
                raw_text=text,
                meta={"tg_message_id": 100 + index},
            )
        )
    await session.flush()

    # 3. The chat has been quiet for an hour, so the window flushes.
    [window] = await windows.flush_ready_windows(session)
    assert window.message_count == 3
    assert "[THEM (Akmal)] aka, 12 mln kerak edi" in window.text
    assert "[ME] mayli, bugun o'tkazaman" in window.text

    # 4. It goes out in a batch and comes back extracted.
    stub = _StubClient()
    monkeypatch.setattr(batch, "get_client", lambda: stub)
    await batch.submit_pending(session)
    assert stub.requests[0]["custom_id"] == window.custom_id

    stub.results = [_succeeded(window.custom_id, EXTRACTED)]
    outcome = await batch.collect_batch(session, "batch_chain")
    await session.commit()

    # 5. The conversation is now structured data attributed to the right person.
    assert outcome.applied == 1
    assert window.status is WindowStatus.applied

    debt = await session.scalar(sa.select(m.Debt))
    assert debt.person_id == person.id
    assert debt.amount == 12_000_000
    assert debt.direction is DebtDirection.they_owe_me
    assert debt.due_date.isoformat() == "2026-08-21"

    promise = await session.scalar(sa.select(m.Promise))
    assert promise.person_id == person.id

    memory = await session.scalar(sa.select(m.Memory))
    assert "Guangzhou" in memory.content
    assert memory.embedding is None  # the embed job picks it up next tick

    # 6. Every message is claimed by the window — including the synthetic row
    #    that owns the extraction — so nothing can be extracted twice.
    rows = list(
        await session.scalars(sa.select(m.Interaction).order_by(m.Interaction.id))
    )
    assert len(rows) == 4  # three real messages + the window's own row
    assert all(r.window_id == window.id for r in rows)
    assert all(r.processed for r in rows)
    assert await windows.flush_ready_windows(session) == []

    # 7. The spend was recorded at batch pricing.
    usage = await session.scalar(
        sa.select(m.UsageLog).where(m.UsageLog.operation == "extract_window")
    )
    assert usage.batch is True
    assert usage.cost_usd > 0


async def test_purging_the_chat_afterwards_leaves_nothing_behind(session, monkeypatch):
    """`/unut chat …` must reach the derived rows too, not just the messages."""
    from miya.services import purge

    await test_a_telegram_conversation_becomes_a_debt(session, monkeypatch)

    plan = await purge.plan_chat(session, CHAT)
    await purge.execute(session, plan)
    await session.commit()

    for model in (m.Interaction, m.Debt, m.Promise, m.Memory, m.ConversationWindow):
        remaining = await session.scalar(sa.select(sa.func.count()).select_from(model))
        assert remaining == 0, f"{model.__name__} survived the chat purge"

"""JV-17: owner first, takeover and draft invalidation — MIYA never talks over
the owner.

The owner reading a chat, typing in it or writing in it, and a client
editing or deleting a message, are recorded by the userbot's read-only
handlers (fake Telethon events here) and read by both the responder and the
outbox claim. The owner's own message cancels the chat's drafts, closes its
escalation as ``auto:own`` and pauses the AI there; MIYA's own sends never
count. Runs against real PostgreSQL with the real JV-13 red-line guard; the
model is a fake.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from dotenv import dotenv_values
from telethon import events
from telethon.tl.types import (
    DraftMessage,
    DraftMessageEmpty,
    PeerUser,
    UpdateDraftMessage,
)

from miya.bot import replies
from miya.config import Settings, settings
from miya.db import models as m
from miya.db.enums import ChatType, Direction, InteractionSource
from miya.db.session import session_scope
from miya.jarvis import (
    chat_state,
    chatkey,
    costs,
    gate,
    kb,
    outbox,
    policy,
    redlines,
    responder,
    stalls,
    style,
)
from miya.services import authorship, safety
from miya.tools import doctor
from miya.userbot import main as userbot
from tests.test_responder import (
    DM,
    OTHER_DM,
    OWNER_ID,
    DownEmbedder,
    FakeClient,
    _escalations,
    _owner_says,
    _rows,
    _says,
    _setup,
    _state,
    _tick,
)
from tests.test_userbot_catchup import CHAT as TG_CHAT
from tests.test_userbot_catchup import _Client
from tests.test_userbot_catchup import _message as _tg_message

TZ = settings.tz
ROOT = Path(__file__).resolve().parents[1]
TG_KEY = chatkey.telegram(TG_CHAT)
SUPERGROUP = -1001234567890


@pytest.fixture
def on(monkeypatch):
    """Jarvis on (the env ceiling at draft), the JV-13 red-line guard, a fake
    model, the plan's owner-first settings and an Telegram sender switch."""
    monkeypatch.setattr(settings, "jarvis_enabled", True)
    monkeypatch.setattr(settings, "jarvis_mode", "draft")
    monkeypatch.setattr(settings, "owner_telegram_id", OWNER_ID)
    monkeypatch.setattr(settings, "owner_aliases", "")
    monkeypatch.setattr(settings, "extract_model", "test-extract-model")
    monkeypatch.setattr(settings, "reason_model", "test-reason-model")
    monkeypatch.setattr(settings, "extract_model_price", "1.00,5.00")
    monkeypatch.setattr(settings, "reason_model_price", "3.00,15.00")
    monkeypatch.setattr(settings, "jarvis_daily_cost_cap_usd", Decimal("3.00"))
    monkeypatch.setattr(settings, "jarvis_debounce_seconds", 45)
    monkeypatch.setattr(settings, "jarvis_owner_first_seconds", 0)
    monkeypatch.setattr(settings, "jarvis_max_message_age_seconds", 900)
    monkeypatch.setattr(settings, "jarvis_max_ai_turns", 2)
    monkeypatch.setattr(settings, "jarvis_history_turns", 8)
    monkeypatch.setattr(settings, "jarvis_per_chat_daily_cap", 6)
    monkeypatch.setattr(settings, "jarvis_send_min_gap_seconds", 0)
    monkeypatch.setattr(settings, "jarvis_takeover_minutes", 120)
    monkeypatch.setattr(settings, "kb_match_threshold", 0.75)
    monkeypatch.setattr(outbox, "channel_send_enabled", lambda channel: True)
    client = FakeClient()
    monkeypatch.setattr(costs, "get_client", lambda: client)
    monkeypatch.setattr(kb, "get_embedder", lambda: DownEmbedder())
    monkeypatch.setattr(style, "get_embedder", lambda: DownEmbedder())
    monkeypatch.setattr(userbot, "_last_catch_up", None)
    monkeypatch.setattr(userbot, "_dialog_tops", {})
    previous = gate.registered_guard()
    redlines.install()
    yield client
    gate.register_guard(previous)


def _now() -> datetime:
    return datetime.now(TZ).replace(microsecond=0)


async def _draft_mode(session, now: datetime) -> None:
    """The stored mode at draft: the responder's rows are draft cards."""
    await policy.set_mode(
        session, scope=policy.GLOBAL, mode="draft", by="command", now=now
    )


async def _draft_mode_committed(now: datetime) -> None:
    async with session_scope() as scope:
        await _draft_mode(scope, now)


async def _tg_monitor() -> None:
    """The fake Telethon chat (tests.test_userbot_catchup.CHAT), switched on."""
    async with session_scope() as scope:
        scope.add(
            m.ChatMonitor(
                tg_chat_id=TG_CHAT,
                chat_type=ChatType.private,
                title="Akmal",
                monitor_enabled=True,
            )
        )


def _out(mid: int, *, at: datetime, text: str) -> SimpleNamespace:
    message = _tg_message(mid, at=at, text=text)
    message.out = True
    return message


async def _stored(session, mid: int, *, tg_chat_id: int = TG_CHAT) -> m.Interaction:
    return await session.scalar(
        sa.select(m.Interaction)
        .where(
            m.Interaction.tg_chat_id == tg_chat_id,
            m.Interaction.meta["tg_message_id"].astext == str(mid),
        )
        .execution_options(populate_existing=True)
    )


def _states(rows) -> list[tuple[str, str | None]]:
    return [(r.state, r.last_error) for r in rows]


# --- the owner wrote -----------------------------------------------------------------


async def test_owner_reply_cancels_and_pauses(session, on):
    await _tg_monitor()
    t0 = _now()
    await _draft_mode_committed(t0)
    client = _Client([])
    await userbot.on_new_message(client, _tg_message(301, at=t0, text="Yuk bor edi"))
    result = await responder.run(now=t0 + timedelta(minutes=1))
    assert [o.code for o in result.outcomes] == [responder.DRAFTED]
    [draft] = await _rows(session, dm=TG_CHAT)
    assert draft.state == outbox.DRAFT

    # The owner answers the client themselves, live.
    await userbot.on_new_message(
        client, _out(302, at=t0 + timedelta(minutes=2), text="Ha, ertaga gaplashamiz")
    )
    [draft] = await _rows(session, dm=TG_CHAT)
    assert (draft.state, draft.last_error) == (outbox.CANCELLED, gate.OWNER_REPLIED)
    last = draft.history[-1]
    assert (last["old"], last["new"], last["by"]) == ("draft", "cancelled", "auto:own")
    state = await _state(session, dm=TG_CHAT)
    assert state.owner_replied_at is not None
    assert state.ai_paused_until - state.owner_replied_at == timedelta(minutes=120)
    assert state.ai_turns_since_client == 0

    # The client writes again within the takeover: the AI stays silent.
    await userbot.on_new_message(
        client, _tg_message(303, at=t0 + timedelta(minutes=5), text="Yana yuk bor")
    )
    result = await responder.run(now=t0 + timedelta(minutes=6))
    assert [o.code for o in result.outcomes] == [responder.AI_PAUSED]
    assert len(await _rows(session, dm=TG_CHAT)) == 1

    # After it, a new client message is drafted as usual.
    later = state.ai_paused_until + timedelta(minutes=1)
    await userbot.on_new_message(client, _tg_message(304, at=later, text="Mebel bor"))
    result = await responder.run(now=later + timedelta(minutes=1))
    assert [o.code for o in result.outcomes] == [responder.DRAFTED]
    assert _states(await _rows(session, dm=TG_CHAT)) == [
        (outbox.CANCELLED, gate.OWNER_REPLIED),
        (outbox.DRAFT, None),
    ]


async def test_owner_reply_closes_the_escalation_at_once(session, on):
    person = await _setup(session)
    t0 = _now()
    await _draft_mode(session, t0)
    await _says(session, person, "Narxi qancha?", at=t0)
    [opened] = await _tick(session, t0 + timedelta(minutes=1))
    assert opened.code == responder.RED_LINE
    [stall] = await _rows(session)
    assert stall.state == outbox.DRAFT and stall.origin == "stall"

    mine = await _owner_says(
        session, person, "Hozir aytaman", at=t0 + timedelta(minutes=2)
    )
    taken = await chat_state.owner_wrote(session, mine, now=t0 + timedelta(minutes=2))
    assert taken is not None and taken.cancelled == (stall.id,)
    [escalation] = await _escalations(session)
    assert taken.escalation_id == escalation.id
    assert (escalation.state, escalation.resolution) == ("resolved", "owner_replied")
    assert escalation.answered_by == "auto:own"
    assert escalation.answered_at == mine.occurred_at
    entry = escalation.history[-1]
    assert (entry["field"], entry["old"], entry["new"]) == ("state", "open", "resolved")
    assert (entry["by"], entry["code"]) == ("auto:own", "owner_replied")
    [stall] = await _rows(session)
    assert (stall.state, stall.last_error) == (outbox.CANCELLED, gate.OWNER_REPLIED)


async def test_owner_reply_closes_an_escalation_written_after_it(session, on):
    # The reader was down: the responder escalated the client's question
    # without seeing the owner's answer, which the catch-up stores later.
    # The escalation is about the client's message, which came first.
    person = await _setup(session)
    t0 = _now() - timedelta(minutes=10)
    await _draft_mode(session, t0)
    await _says(session, person, "Narxi qancha?", at=t0)
    [opened] = await _tick(session, t0 + timedelta(seconds=165))
    assert opened.code == responder.RED_LINE
    [escalation] = await _escalations(session)
    assert escalation.created_at > t0 + timedelta(seconds=60)
    mine = await _owner_says(
        session, person, "Hozir aytaman", at=t0 + timedelta(seconds=60)
    )
    taken = await chat_state.owner_wrote(session, mine, now=_now())
    assert taken is not None and taken.escalation_id == escalation.id
    [escalation] = await _escalations(session)
    assert (escalation.state, escalation.answered_by) == ("resolved", "auto:own")
    [stall] = await _rows(session)
    assert (stall.state, stall.last_error) == (outbox.CANCELLED, gate.OWNER_REPLIED)


async def test_owner_sticker_takes_the_chat_over(session, on):
    # Stickers and GIFs are never stored, but the owner sent one themselves.
    await _tg_monitor()
    t0 = _now() - timedelta(minutes=5)
    await _draft_mode_committed(t0)
    client = _Client([])
    await userbot.on_new_message(client, _tg_message(321, at=t0, text="Yuk bor edi"))
    await responder.run(now=_now() + timedelta(minutes=1))
    [draft] = await _rows(session, dm=TG_CHAT)
    assert draft.state == outbox.DRAFT
    sticker = _out(322, at=t0 + timedelta(minutes=2), text="")
    sticker.sticker = object()
    await userbot.on_new_message(client, sticker)
    assert await _stored(session, 322) is None
    [draft] = await _rows(session, dm=TG_CHAT)
    assert (draft.state, draft.last_error) == (outbox.CANCELLED, gate.OWNER_REPLIED)
    state = await _state(session, dm=TG_CHAT)
    assert state.owner_replied_at == sticker.date.astimezone(TZ)
    assert state.ai_paused_until == state.owner_replied_at + timedelta(minutes=120)
    # A client's sticker is no takeover.
    theirs = _tg_message(323, at=t0 + timedelta(minutes=3), text="")
    theirs.sticker = object()
    assert not await userbot.note_owner_sticker(theirs)


async def test_observation_rows_stay_but_the_chat_is_paused(session, on):
    # In shadow mode nothing is ever sent: the row is the record of what
    # Jarvis would have said, and stays; the pause still holds.
    person = await _setup(session)
    t0 = _now()
    await _says(session, person, "Yuk bor edi", at=t0)
    [drafted] = await _tick(session, t0 + timedelta(minutes=1))
    assert drafted.code == responder.DRAFTED
    mine = await _owner_says(session, person, "Ha", at=t0 + timedelta(minutes=2))
    taken = await chat_state.owner_wrote(session, mine, now=t0 + timedelta(minutes=2))
    assert taken is not None and taken.cancelled == ()
    assert [r.state for r in await _rows(session)] == [outbox.SHADOW]
    state = await _state(session)
    assert state.ai_paused_until == mine.occurred_at + timedelta(minutes=120)


async def test_owner_reply_from_the_catch_up_takes_over(session, on):
    await _tg_monitor()
    t0 = _now() - timedelta(minutes=3)
    await _draft_mode_committed(t0)
    client = _Client([])
    await userbot.on_new_message(client, _tg_message(311, at=t0, text="Yuk bor edi"))
    await responder.run(now=_now() + timedelta(minutes=1))
    [draft] = await _rows(session, dm=TG_CHAT)
    assert draft.state == outbox.DRAFT
    async with session_scope() as scope:
        monitor = await scope.scalar(
            sa.select(m.ChatMonitor).where(m.ChatMonitor.tg_chat_id == TG_CHAT)
        )
        monitor.last_seen_message_id = 311
        monitor.monitoring_since = t0 - timedelta(hours=1)
    # The reader was down when the owner answered; the catch-up reads it.
    reply = _out(312, at=t0 + timedelta(seconds=90), text="Ha, aytavering")
    assert await userbot.catch_up_sweep(_Client([reply])) == 1
    [draft] = await _rows(session, dm=TG_CHAT)
    assert (draft.state, draft.last_error) == (outbox.CANCELLED, gate.OWNER_REPLIED)
    state = await _state(session, dm=TG_CHAT)
    assert state.owner_replied_at == reply.date.astimezone(TZ)


async def test_owner_reply_claim_reads_the_stored_message(session, on):
    # A message whose signal never got recorded still cancels the row at the
    # claim: the gate reads the owner's stored messages too.
    person = await _setup(session)
    t0 = _now() - timedelta(minutes=10)
    await _draft_mode(session, t0)
    await _says(session, person, "Yuk bor edi", at=t0)
    await _tick(session, t0 + timedelta(minutes=1))
    [draft] = await _rows(session)
    await outbox.approve(session, draft.id, by="button", now=t0 + timedelta(minutes=2))
    await _owner_says(session, person, "Ha", at=t0 + timedelta(minutes=3))
    assert (
        await outbox.claim_ready(session, "telegram", now=t0 + timedelta(minutes=4)) == []
    )
    [draft] = await _rows(session)
    assert (draft.state, draft.last_error) == (outbox.CANCELLED, gate.OWNER_REPLIED)
    assert draft.history[-1]["by"] == "auto:own"


# --- composing and reading ------------------------------------------------------------


def _draft_update(chat_id: int, *, at: datetime, text: str = "Hozir yozaman"):
    return UpdateDraftMessage(
        peer=PeerUser(user_id=chat_id), draft=DraftMessage(message=text, date=at)
    )


async def test_composing_blocks_claim(session, on):
    person = await _setup(session)
    now = _now()
    t0 = now - timedelta(minutes=5)
    await _draft_mode(session, t0)
    await _says(session, person, "Yuk bor edi", at=t0)
    await _tick(session, t0 + timedelta(minutes=1))
    [draft] = await _rows(session)
    await outbox.approve(session, draft.id, by="button", now=t0 + timedelta(minutes=2))
    await session.commit()

    # A cleared draft says nothing; the owner typing does.
    empty = UpdateDraftMessage(peer=PeerUser(user_id=DM), draft=DraftMessageEmpty())
    assert not await userbot.on_draft_update(empty)
    assert await userbot.on_draft_update(_draft_update(DM, at=now))
    state = await _state(session)
    assert state.owner_composing_at == now

    assert (
        await outbox.claim_ready(session, "telegram", now=now + timedelta(seconds=5))
        == []
    )
    [draft] = await _rows(session)
    assert draft.state == outbox.APPROVED and draft.last_error == gate.OWNER_COMPOSING
    assert draft.next_attempt_at == now + gate.COMPOSING_HOLD
    # Once the owner stopped typing, the tapped draft goes.
    [claimed] = await outbox.claim_ready(
        session, "telegram", now=now + gate.COMPOSING_HOLD + timedelta(seconds=1)
    )
    assert claimed.id == draft.id


async def test_composing_holds_the_responder(session, on):
    person = await _setup(session)
    t0 = _now()
    await _says(session, person, "Yuk bor edi", at=t0)
    typing = t0 + timedelta(seconds=30)
    assert await chat_state.owner_composing(session, tg_chat_id=DM, at=typing, now=typing)
    # A chat Jarvis keeps no state for is not recorded.
    assert not await chat_state.owner_composing(
        session, tg_chat_id=OTHER_DM, at=typing, now=typing
    )
    [waiting] = await _tick(session, t0 + timedelta(seconds=50))
    assert waiting.code == responder.WAITING
    assert (await _state(session)).respond_after == typing + gate.COMPOSING_HOLD
    assert await _rows(session) == [] and on.calls == []
    [drafted] = await _tick(session, typing + gate.COMPOSING_HOLD + timedelta(seconds=1))
    assert drafted.code == responder.DRAFTED


async def test_owner_read_skips_the_chat(session, on, monkeypatch):
    person = await _setup(session)
    t0 = _now() - timedelta(minutes=1)
    first = await _says(session, person, "Assalomu alaykum", at=t0)
    last = await _says(session, person, "Yuk bor edi", at=t0 + timedelta(seconds=5))
    await session.commit()
    read_at = datetime.now(TZ)
    # A read that stops short of the client's latest message is not "seen".
    short = SimpleNamespace(
        chat_id=DM, max_id=first.meta["tg_message_id"], contents=False, outbox=False
    )
    assert not await userbot.on_message_read(short)
    # The client reading the owner's messages, or a voice note played, neither.
    theirs = SimpleNamespace(
        chat_id=DM, max_id=last.meta["tg_message_id"], contents=False, outbox=True
    )
    assert not await userbot.on_message_read(theirs)
    full = SimpleNamespace(
        chat_id=DM, max_id=last.meta["tg_message_id"], contents=False, outbox=False
    )
    assert await userbot.on_message_read(full)
    state = await _state(session)
    assert state.owner_seen_at >= read_at
    [outcome] = await _tick(session, max(state.owner_seen_at, t0) + timedelta(minutes=1))
    assert outcome.code == responder.OWNER_SEEN
    assert await _rows(session) == [] and on.calls == []


async def test_owner_read_up_to_a_deleted_message(session, on):
    # The client's latest message was deleted: the owner can only read up to
    # the one before it, and that read covers the chat.
    person = await _setup(session)
    t0 = _now() - timedelta(minutes=1)
    first = await _says(session, person, "Yuk bor edi", at=t0)
    last = await _says(session, person, "Mebel", at=t0 + timedelta(seconds=5))
    assert (
        await chat_state.client_deleted(
            session, tg_chat_id=None, message_ids=[last.meta["tg_message_id"]], now=_now()
        )
        == ()
    )
    assert await chat_state.owner_read(
        session, tg_chat_id=DM, max_id=first.meta["tg_message_id"], now=_now()
    )
    assert (await _state(session)).owner_seen_at is not None


async def test_owner_read_blocks_an_auto_claim(session, on, monkeypatch):
    person = await _setup(session)
    now = _now()
    t0 = now - timedelta(minutes=5)
    monkeypatch.setattr(settings, "jarvis_mode", "auto")
    await policy.set_mode(session, scope=policy.GLOBAL, mode="auto", by="command", now=t0)
    trigger = await _says(session, person, "Assalomu alaykum", at=t0, enqueue=False)
    assert await responder.enqueue_candidate(session, trigger, now=t0)
    row = await outbox.create(
        session,
        channel="telegram",
        chat_key=chatkey.telegram(DM),
        origin="auto_reply",
        authored_by="ai",
        body="Assalomu alaykum!",
        now=t0 + timedelta(minutes=1),
        intent="greeting",
        person_id=person.id,
        trigger_interaction_id=trigger.id,
    )
    assert row.state == outbox.APPROVED and row.approved_by == "auto:policy"
    assert await chat_state.owner_read(
        session, tg_chat_id=DM, max_id=trigger.meta["tg_message_id"], now=now
    )
    assert (
        await outbox.claim_ready(session, "telegram", now=now + timedelta(seconds=1))
        == []
    )
    [row] = await _rows(session)
    assert (row.state, row.last_error) == (outbox.BLOCKED, gate.OWNER_SEEN)


# --- the client edited or deleted -----------------------------------------------------


async def test_edit_expires_draft(session, on):
    await _tg_monitor()
    t0 = _now()
    await _draft_mode_committed(t0)
    client = _Client([])
    await userbot.on_new_message(client, _tg_message(401, at=t0, text="Yuk bor edi"))
    await responder.run(now=t0 + timedelta(minutes=1))
    [first] = await _rows(session, dm=TG_CHAT)
    assert first.state == outbox.DRAFT
    assert (await _state(session, dm=TG_CHAT)).respond_after is None

    edited = _tg_message(401, at=t0, text="Yuk bor edi, Xitoydan mebel")
    await userbot.on_message_edited(client, edited)
    [first] = await _rows(session, dm=TG_CHAT)
    assert (first.state, first.last_error) == (outbox.EXPIRED, gate.TRIGGER_EDITED)
    assert first.history[-1]["by"] == "auto:expiry"
    # The chat is queued again: the edited words get a fresh draft, a row of
    # its own although the text is the same.
    assert (await _state(session, dm=TG_CHAT)).respond_after is not None
    result = await responder.run(now=t0 + timedelta(minutes=3))
    assert [o.code for o in result.outcomes] == [responder.DRAFTED]
    first, second = await _rows(session, dm=TG_CHAT)
    assert second.state == outbox.DRAFT
    assert second.trigger_interaction_id == first.trigger_interaction_id
    assert second.body == first.body
    assert second.idempotency_key != first.idempotency_key
    # The same proposal once more is the same row, not a third one.
    again = await outbox.create(
        session,
        channel="telegram",
        chat_key=TG_KEY,
        origin="auto_reply",
        authored_by="ai",
        body=second.body,
        now=t0 + timedelta(minutes=4),
        intent=second.intent,
        person_id=second.person_id,
        trigger_interaction_id=second.trigger_interaction_id,
    )
    assert again.id == second.id

    # An edit that changes nothing (a reaction) and an edit of a message no
    # draft read (an hour older than the one answered) expire nothing and
    # queue nothing.
    await userbot.on_message_edited(client, _tg_message(401, at=t0, text=edited.message))
    old = t0 - timedelta(hours=1)
    await userbot.on_new_message(client, _tg_message(402, at=old, text="Salom"))
    await userbot.on_message_edited(client, _tg_message(402, at=old, text="Salom aka"))
    assert (await _stored(session, 402)).raw_text == "Salom aka"
    assert [r.state for r in await _rows(session, dm=TG_CHAT)] == [
        outbox.EXPIRED,
        outbox.DRAFT,
    ]
    assert (await _state(session, dm=TG_CHAT)).respond_after is None


async def test_edit_reopens_the_escalation_fresh(session, on):
    person = await _setup(session)
    t0 = _now()
    await _draft_mode(session, t0)
    question = await _says(session, person, "Narxi qancha?", at=t0)
    await _tick(session, t0 + timedelta(minutes=1))
    [old] = await _escalations(session)
    [stall] = await _rows(session)
    question.raw_text = "Narxi qancha, Toshkentgacha?"
    question.meta = {
        **question.meta,
        "edits": [
            {"at": (t0 + timedelta(minutes=2)).isoformat(), "old": "Narxi qancha?"}
        ],
    }
    await session.flush()
    expired = await chat_state.client_edited(
        session, question, now=t0 + timedelta(minutes=2)
    )
    assert expired == (stall.id,)
    [old] = await _escalations(session)
    assert (old.state, old.resolution) == ("cancelled", "cancelled")
    assert old.history[-1]["code"] == gate.TRIGGER_EDITED
    [outcome] = await _tick(session, t0 + timedelta(minutes=3))
    assert outcome.code == responder.RED_LINE
    old, new = await _escalations(session)
    assert new.state == "open" and new.interaction_id == question.id
    first, second = await _rows(session)
    assert first.state == outbox.EXPIRED and second.state == outbox.DRAFT
    assert second.body == stalls.template("price", "latin", "neutral")
    assert second.escalation_id == new.id


async def test_delete_cancels(session, on):
    await _tg_monitor()
    t0 = _now()
    await _draft_mode_committed(t0)
    client = _Client([])
    await userbot.on_new_message(client, _tg_message(501, at=t0, text="Yuk bor edi"))
    await responder.run(now=t0 + timedelta(minutes=1))
    [draft] = await _rows(session, dm=TG_CHAT)
    assert draft.state == outbox.DRAFT
    # A supergroup numbers its messages on its own: the same id there is
    # another message.
    async with session_scope() as scope:
        scope.add(
            m.Interaction(
                source=InteractionSource.telegram_userbot,
                direction=Direction.in_,
                tg_chat_id=SUPERGROUP,
                occurred_at=t0,
                raw_text="Salom",
                meta={"tg_message_id": 501},
            )
        )

    # Telegram does not say where a private message was deleted.
    deleted = SimpleNamespace(deleted_ids=[501, 999999], chat_id=None)
    assert await userbot.on_message_deleted(deleted) == 1
    [draft] = await _rows(session, dm=TG_CHAT)
    assert (draft.state, draft.last_error) == (outbox.CANCELLED, gate.TRIGGER_DELETED)
    assert outbox.DELETED_KEY in (await _stored(session, 501)).meta
    assert (
        outbox.DELETED_KEY
        not in (await _stored(session, 501, tg_chat_id=SUPERGROUP)).meta
    )

    # A message deleted before the responder read it is never answered.
    await userbot.on_new_message(
        client, _tg_message(502, at=t0 + timedelta(minutes=2), text="Mebel bor")
    )
    assert (
        await userbot.on_message_deleted(SimpleNamespace(deleted_ids=[502], chat_id=None))
        == 0
    )
    result = await responder.run(now=t0 + timedelta(minutes=4))
    assert [o.code for o in result.outcomes] == [responder.NOTHING]
    assert len(await _rows(session, dm=TG_CHAT)) == 1


async def test_delete_in_the_burst_redrafts_the_rest(session, on):
    # One draft answers the whole burst: deleting its first message takes
    # the draft off the table, and what is left is drafted again.
    person = await _setup(session)
    t0 = _now()
    await _draft_mode(session, t0)
    first = await _says(session, person, "Yuk bor edi", at=t0)
    second = await _says(session, person, "Xitoydan", at=t0 + timedelta(seconds=5))
    await _tick(session, t0 + timedelta(minutes=1))
    [draft] = await _rows(session)
    assert draft.trigger_interaction_id == second.id
    gone = await chat_state.client_deleted(
        session,
        tg_chat_id=None,
        message_ids=[first.meta["tg_message_id"]],
        now=t0 + timedelta(minutes=2),
    )
    assert gone == (draft.id,)
    [draft] = await _rows(session)
    assert (draft.state, draft.last_error) == (outbox.EXPIRED, gate.BURST_DELETED)
    assert (await _state(session)).respond_after is not None
    [outcome] = await _tick(session, t0 + timedelta(minutes=4))
    assert outcome.code == responder.DRAFTED
    old, fresh = await _rows(session)
    assert fresh.state == outbox.DRAFT and fresh.trigger_interaction_id == second.id
    assert fresh.idempotency_key != old.idempotency_key


async def test_edited_words_are_shielded_before_a_redraft(session, on):
    # A clean message gets a draft; the client edits it into a scam. The
    # JV-09 shield reads the new words: nothing is drafted from them and the
    # owner is warned.
    await _tg_monitor()
    t0 = _now()
    await _draft_mode_committed(t0)
    client = _Client([])
    await userbot.on_new_message(client, _tg_message(411, at=t0, text="Yuk bor edi"))
    await responder.run(now=t0 + timedelta(minutes=1))
    [first] = await _rows(session, dm=TG_CHAT)
    scam = "Kartangiz bloklandi: https://payme-uz.xyz/login"
    await userbot.on_message_edited(client, _tg_message(411, at=t0, text=scam))
    row = await _stored(session, 411)
    assert row.raw_text == scam
    assert row.meta["safety"]["verdict"] == "scam"
    assert row.meta["safety_notice"] == safety.NOTICE_SCAM
    [first] = await _rows(session, dm=TG_CHAT)
    assert first.state == outbox.EXPIRED
    result = await responder.run(now=t0 + timedelta(minutes=3))
    assert [o.code for o in result.outcomes] == [responder.UNSAFE]
    assert len(await _rows(session, dm=TG_CHAT)) == 1


async def test_edit_that_swaps_the_media_is_not_redrafted(session, on):
    await _tg_monitor()
    t0 = _now()
    await _draft_mode_committed(t0)
    client = _Client([])
    await userbot.on_new_message(client, _tg_message(421, at=t0, text="Yuk bor edi"))
    await responder.run(now=t0 + timedelta(minutes=1))
    swapped = _tg_message(421, at=t0, text="Mana rasm")
    swapped.photo = object()
    await userbot.on_message_edited(client, swapped)
    [first] = await _rows(session, dm=TG_CHAT)
    assert (first.state, first.last_error) == (outbox.EXPIRED, gate.TRIGGER_EDITED)
    assert (await _state(session, dm=TG_CHAT)).respond_after is None


# --- MIYA's own sends ----------------------------------------------------------------


async def test_signals_from_ai_rows_ignored(session, on):
    person = await _setup(session)
    now = _now()
    t0 = now - timedelta(minutes=10)
    await _draft_mode(session, t0)
    await _says(session, person, "Yuk bor edi", at=t0)
    await _tick(session, t0 + timedelta(minutes=1))
    [sent] = await _rows(session)
    await outbox.approve(session, sent.id, by="button", now=t0 + timedelta(minutes=2))
    [claimed] = await outbox.claim_ready(session, "telegram", now=now)
    assert claimed.id == sent.id

    # While MIYA sends, a read of the chat is the sender's own.
    question = await _says(session, person, "Mebel bor", at=now, enqueue=False)
    assert not await chat_state.owner_read(
        session, tg_chat_id=DM, max_id=question.meta["tg_message_id"], now=now
    )
    # The echo arrives before the send is reported (plan Q11): untagged, but
    # its text is the row in flight.
    echo = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.out,
        person_id=person.id,
        tg_chat_id=DM,
        occurred_at=now,
        raw_text=sent.body,
        meta={"tg_message_id": 7777},
    )
    session.add(echo)
    await session.flush()
    assert authorship.is_owner_words(echo)  # untagged, it would read as the owner's
    assert await chat_state.owner_wrote(session, echo, now=now) is None

    # Reported: the echo is tagged as MIYA's and stays no takeover.
    await outbox.complete(session, sent.id, external_id=f"{DM}:7777", sent_at=now)
    echo = await _stored(session, 7777, tg_chat_id=DM)
    assert authorship.is_ai_reply(echo)
    assert await chat_state.owner_wrote(session, echo, now=now) is None
    # An owner-dictated message MIYA delivered is not the owner typing either.
    dictated = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.out,
        person_id=person.id,
        tg_chat_id=DM,
        occurred_at=now,
        raw_text="Ertaga gaplashamiz",
        meta={"tg_message_id": 7778, **authorship.echo_meta(authorship.Echo(1, "owner"))},
    )
    session.add(dictated)
    await session.flush()
    assert await chat_state.owner_wrote(session, dictated, now=now) is None

    state = await _state(session)
    assert state.owner_replied_at is None and state.ai_paused_until is None
    assert state.owner_seen_at is None and state.ai_turns_since_client == 1
    [sent] = await _rows(session)
    assert sent.state == outbox.SENT


async def test_untagged_echo_never_cancels_another_row(session, on):
    # Two approved AI rows in one chat: the first goes on the wire and its
    # echo is stored untagged (plan Q11). The claim of the second never
    # reads that echo as the owner's reply.
    person = await _setup(session)
    now = _now()
    t0 = now - timedelta(minutes=10)
    await _draft_mode(session, t0)
    rows = []
    for offset, (text, body) in enumerate(
        (("Yuk bor edi", "Assalomu alaykum!"), ("Mebel bor", "Salom!"))
    ):
        trigger = await _says(
            session, person, text, at=t0 + timedelta(minutes=offset), enqueue=False
        )
        rows.append(
            await outbox.create(
                session,
                channel="telegram",
                chat_key=chatkey.telegram(DM),
                origin="auto_reply",
                authored_by="ai",
                body=body,
                now=t0 + timedelta(minutes=2),
                intent="greeting",
                person_id=person.id,
                trigger_interaction_id=trigger.id,
            )
        )
    first, second = rows
    await outbox.approve(session, first.id, by="button", now=t0 + timedelta(minutes=3))
    [claimed] = await outbox.claim_ready(session, "telegram", now=now)
    assert claimed.id == first.id
    echo = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.out,
        person_id=person.id,
        tg_chat_id=DM,
        occurred_at=now + timedelta(seconds=1),
        raw_text=first.body,
        meta={"tg_message_id": 7790},
    )
    session.add(echo)
    await session.flush()
    await outbox.approve(session, second.id, by="button", now=now)
    [claimed] = await outbox.claim_ready(
        session, "telegram", now=now + timedelta(seconds=2)
    )
    assert claimed.id == second.id
    # The owner typing the same words long after an in-doubt send is theirs.
    await session.execute(
        sa.update(m.OutboxMessage)
        .where(m.OutboxMessage.id == first.id)
        .values(state=outbox.UNKNOWN)
    )
    later = await _owner_says(session, person, first.body, at=now + timedelta(hours=1))
    assert not await chat_state.miya_echo(session, later)


async def test_owner_reply_resets_the_ai_turns(session, on):
    person = await _setup(session)
    t0 = _now()
    await _says(session, person, "Yuk bor edi", at=t0)
    state = await _state(session)
    state.ai_turns_since_client = 2
    state.last_ai_sent_at = t0 - timedelta(minutes=1)
    await session.flush()
    mine = await _owner_says(session, person, "Ha", at=t0 + timedelta(minutes=1))
    assert await chat_state.owner_wrote(session, mine, now=t0 + timedelta(minutes=1))
    assert (await _state(session)).ai_turns_since_client == 0


# --- contained ------------------------------------------------------------------------


async def test_signal_failures_are_contained(session, on, monkeypatch, caplog):
    await _tg_monitor()
    t0 = _now()
    client = _Client([])

    async def _boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(chat_state, "owner_wrote", _boom)
    monkeypatch.setattr(chat_state, "owner_read", _boom)
    monkeypatch.setattr(chat_state, "owner_composing", _boom)
    monkeypatch.setattr(chat_state, "client_deleted", _boom)
    with caplog.at_level(logging.ERROR, logger="miya.userbot.main"):
        await userbot.on_new_message(client, _out(601, at=t0, text="Salom"))
        assert not await userbot.on_message_read(
            SimpleNamespace(chat_id=TG_CHAT, max_id=601, contents=False, outbox=False)
        )
        assert not await userbot.on_draft_update(_draft_update(TG_CHAT, at=t0))
        assert (
            await userbot.on_message_deleted(
                SimpleNamespace(deleted_ids=[601], chat_id=None)
            )
            == 0
        )
    failed = {r.getMessage(): r for r in caplog.records}
    assert failed["owner takeover not recorded"].tg_message_id == 601
    assert failed["owner takeover not recorded"].error == "RuntimeError"
    assert failed["owner read not recorded"].tg_chat_id == TG_CHAT
    assert failed["owner typing not recorded"].tg_chat_id == TG_CHAT
    assert failed["deleted messages not recorded"].tg_message_ids == [601]
    # The message itself was stored: the reader goes on.
    assert await _stored(session, 601) is not None


async def test_one_draft_that_fails_never_stops_the_others(
    session, on, monkeypatch, caplog
):
    person = await _setup(session)
    t0 = _now()
    await _draft_mode(session, t0)
    await _says(session, person, "Yuk bor edi", at=t0)
    await _tick(session, t0 + timedelta(minutes=1))
    await _says(session, person, "Mebel bor", at=t0 + timedelta(minutes=2))
    await _tick(session, t0 + timedelta(minutes=3))
    first, second = await _rows(session)
    real = outbox.cancel

    async def _flaky(session_, row_id, **kwargs):
        if row_id == first.id:
            raise RuntimeError("boom")
        return await real(session_, row_id, **kwargs)

    monkeypatch.setattr(outbox, "cancel", _flaky)
    mine = await _owner_says(session, person, "Ha", at=t0 + timedelta(minutes=4))
    with caplog.at_level(logging.ERROR, logger="miya.jarvis.chat_state"):
        taken = await chat_state.owner_wrote(session, mine, now=t0 + timedelta(minutes=4))
    assert taken.cancelled == (second.id,)
    [failed] = [r for r in caplog.records if r.getMessage() == "draft not cancelled"]
    assert failed.outbox_id == first.id and failed.error == "RuntimeError"
    first, second = await _rows(session)
    assert first.state == outbox.DRAFT and second.state == outbox.CANCELLED


# --- what the owner is told -----------------------------------------------------------


class _Bot:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((text, kwargs))
        return SimpleNamespace(message_id=len(self.sent))


async def test_owner_is_told_once_about_what_they_were_shown(session, on):
    from miya.worker import main as worker

    t0 = _now()
    await _draft_mode(session, t0)
    akmal = await _setup(session, dm=DM, name="Akmal‮<b>")
    await _says(session, akmal, "Narxi qancha?", at=t0)
    sardor = await _setup(session, dm=OTHER_DM, name="Sardor")
    await _says(session, sardor, "Yuk bor edi", at=t0, dm=OTHER_DM)
    karim_dm = 7163
    karim = await _setup(session, dm=karim_dm, name="Karim")
    karim_says = await _says(session, karim, "Yuk bor edi", at=t0, dm=karim_dm)
    await _tick(session, t0 + timedelta(minutes=1))
    [escalation] = await _escalations(session)
    [carded] = await _rows(session, dm=OTHER_DM)
    [edited] = await _rows(session, dm=karim_dm)
    # Shown to the owner (the cards and the alert, JV-18).
    await session.execute(
        sa.update(m.Escalation)
        .where(m.Escalation.id == escalation.id)
        .values(alerted_at=t0, alert_tg_message_id=556)
    )
    await session.execute(
        sa.update(m.OutboxMessage)
        .where(m.OutboxMessage.id.in_([carded.id, edited.id]))
        .values(card_tg_message_id=555)
    )
    await session.flush()

    at = t0 + timedelta(minutes=2)
    for person, dm in ((akmal, DM), (sardor, OTHER_DM)):
        mine = await _owner_says(session, person, "Ha", at=at, dm=dm)
        assert await chat_state.owner_wrote(session, mine, now=at)
    karim_says.raw_text = "Yuk bor edi, mebel"
    await session.flush()
    assert await chat_state.client_edited(session, karim_says, now=at)
    await session.commit()

    bot = _Bot()
    # «qoralama yangilanadi» waits until the fresh draft is there.
    assert await worker.jarvis_takeover_notices(bot) == 2
    [redrafted] = await _tick(session, at + timedelta(minutes=1))
    assert redrafted.code == responder.DRAFTED
    await session.commit()
    assert await worker.jarvis_takeover_notices(bot) == 1
    texts = sorted(text for text, _kw in bot.sent)
    assert texts == sorted(
        [
            replies.JARVIS_OWN_REPLY_DRAFT,
            replies.JARVIS_CLIENT_EDITED,
            "✅ Akmal&lt;b&gt;ga o'zingiz javob berdingiz — eskalatsiya yopildi.",
        ]
    )
    # Each answers the card or alert it is about, silently.
    assert all(kw["disable_notification"] is True for _t, kw in bot.sent)
    assert sorted(kw["reply_parameters"].message_id for _t, kw in bot.sent) == [
        555,
        555,
        556,
    ]
    # The stall nobody was shown is no news; and each is told once.
    assert await worker.jarvis_takeover_notices(bot) == 0
    assert len(bot.sent) == 3


async def test_no_refresh_promised_for_an_edit_too_old_to_answer(session, on):
    # The client edits a message 20 minutes after sending it while its card
    # is still live: the card goes, but no fresh draft is coming, so the
    # owner is not told one is.
    from miya.worker import main as worker

    t0 = _now() - timedelta(minutes=20)
    await _draft_mode(session, t0)
    person = await _setup(session)
    question = await _says(session, person, "Yuk bor edi", at=t0)
    await _tick(session, t0 + timedelta(minutes=1))
    [draft] = await _rows(session)
    await session.execute(
        sa.update(m.OutboxMessage)
        .where(m.OutboxMessage.id == draft.id)
        .values(card_tg_message_id=557)
    )
    question.raw_text = "Yuk bor edi, mebel"
    await session.flush()
    now = _now()
    assert await chat_state.client_edited(session, question, now=now) == (draft.id,)
    assert (await _state(session)).respond_after is None
    await session.commit()
    bot = _Bot()
    assert await worker.jarvis_takeover_notices(bot) == 0
    assert bot.sent == []


# --- the handlers and the key ---------------------------------------------------------


def test_signal_handlers_only_with_jarvis(monkeypatch):
    added: list[tuple[object, object]] = []
    client = SimpleNamespace(
        add_event_handler=lambda fn, builder: added.append((fn, builder))
    )
    monkeypatch.setattr(settings, "jarvis_enabled", False)
    assert not userbot.add_signal_handlers(client)
    assert added == []
    monkeypatch.setattr(settings, "jarvis_enabled", True)
    assert userbot.add_signal_handlers(client)
    builders = {fn.__name__: builder for fn, builder in added}
    read = builders["on_message_read"]
    assert isinstance(read, events.MessageRead) and read.inbox is True
    raw = builders["on_draft_update"]
    assert isinstance(raw, events.Raw) and raw.types is UpdateDraftMessage
    assert isinstance(builders["on_message_deleted"], events.MessageDeleted)


def test_takeover_key_in_all_four_places():
    defaults = Settings(_env_file=None)
    example = dotenv_values(ROOT / ".env.example")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    assert defaults.jarvis_takeover_minutes == 120
    assert example["JARVIS_TAKEOVER_MINUTES"] == "120"
    assert "`JARVIS_TAKEOVER_MINUTES`" in readme
    assert "JARVIS_TAKEOVER_MINUTES" in doctor.EXPECTED_ENV_KEYS
    at = lines.index("JARVIS_TAKEOVER_MINUTES=120")
    assert lines[at - 1].startswith("# ")
    assert chat_state.takeover() == timedelta(minutes=settings.jarvis_takeover_minutes)

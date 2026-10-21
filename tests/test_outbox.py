"""JV-11: the Jarvis core — the one outbound chokepoint, its gate, caps and audit.

Runs against real PostgreSQL with a fake adapter: no channel is attached.
Every outbox row is created and moved only through miya/jarvis/outbox.py; an
AST test holds the rest of the code base to that.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import os
import re
import subprocess
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from aiogram.filters import CommandObject
from dotenv import dotenv_values
from telethon.tl.types import User

from miya.bot import handlers, replies
from miya.config import Settings, settings
from miya.db import models as m
from miya.db.enums import ChatType, Direction, InteractionSource
from miya.db.session import SessionLocal
from miya.jarvis import caps, chatkey, costs, gate, outbox, policy, status
from miya.services import authorship, people, purge
from miya.services.reminders import day_start
from miya.tools import doctor
from miya.userbot import main as userbot
from tests.conftest import database_available

ROOT = Path(__file__).resolve().parents[1]
TZ = settings.tz
NOW = datetime.now(TZ).replace(hour=12, minute=0, second=0, microsecond=0)
DM = 7101
KEY = chatkey.telegram(DM)
OWNER_ID = 990001


class PassGuard:
    """Stands in for the JV-13 red-line guard. A passing verdict also passes
    the statement allowlist unless ``shapes_ok`` says otherwise."""

    def __init__(self, verdict: str = "pass", *, shapes_ok: bool | None = None) -> None:
        self.verdict = verdict
        self.shapes_ok = verdict == "pass" if shapes_ok is None else shapes_ok
        self.calls: list[tuple[str, str]] = []

    def check(self, body, *, role, allowed_tokens, **names):
        self.calls.append((body, role))
        return gate.GuardResult(
            verdict=self.verdict, checker_version="test", shapes_ok=self.shapes_ok
        )


@pytest.fixture
def jarvis(monkeypatch):
    """Jarvis on, the ceiling at auto, the channel switched on, a passing guard."""
    monkeypatch.setattr(settings, "jarvis_enabled", True)
    monkeypatch.setattr(settings, "jarvis_mode", "auto")
    monkeypatch.setattr(settings, "owner_telegram_id", OWNER_ID)
    monkeypatch.setattr(settings, "jarvis_send_min_gap_seconds", 0)
    monkeypatch.setattr(outbox, "channel_send_enabled", lambda channel: True)
    previous = gate.registered_guard()
    guard = PassGuard()
    gate.register_guard(guard)
    yield guard
    # Back to the guard outbox registered (the JV-13 red-line guard).
    gate.register_guard(previous)


@pytest.fixture
def no_guard():
    previous = gate.registered_guard()
    gate.register_guard(None)
    yield
    gate.register_guard(previous)


async def _person(session, name="Akmal", telegram_id=DM) -> m.Person:
    person = m.Person(display_name=name, aliases=[], telegram_id=telegram_id)
    session.add(person)
    await session.flush()
    return person


async def _chat(session, tg_chat_id=DM, chat_type=ChatType.private) -> None:
    session.add(
        m.ChatMonitor(
            tg_chat_id=tg_chat_id,
            chat_type=chat_type,
            title="Akmal",
            monitor_enabled=True,
        )
    )
    await session.flush()


async def _inbound(
    session,
    person=None,
    *,
    text="Assalomu alaykum, yuk bormi?",
    at=None,
    tg_chat_id=DM,
    meta=None,
    media=None,
    message_id=None,
) -> m.Interaction:
    row = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.in_,
        person_id=person.id if person else None,
        tg_chat_id=tg_chat_id,
        occurred_at=at or NOW - timedelta(minutes=5),
        raw_text=text,
        media=media,
        meta={"tg_message_id": message_id or next(_message_ids), **(meta or {})},
    )
    session.add(row)
    await session.flush()
    return row


def _counter():
    n = 100
    while True:
        n += 1
        yield n


_message_ids = _counter()


async def _setup(session, *, mode="draft"):
    person = await _person(session)
    await _chat(session)
    trigger = await _inbound(session, person)
    await policy.set_mode(session, scope=policy.GLOBAL, mode=mode, by="command", now=NOW)
    return person, trigger


async def _ai_row(session, person, trigger, *, body="Assalomu alaykum, aka!", **kw):
    return await outbox.create(
        session,
        channel="telegram",
        chat_key=KEY,
        origin=kw.pop("origin", "auto_reply"),
        authored_by="ai",
        body=body,
        now=kw.pop("now", NOW),
        intent=kw.pop("intent", "greeting"),
        person_id=person.id,
        trigger_interaction_id=trigger.id if trigger else None,
        **kw,
    )


async def _command(session, *, at=None) -> m.Interaction:
    """The owner's message to the bot that an owner-command row comes from."""
    row = m.Interaction(
        source=InteractionSource.assistant_bot,
        direction=Direction.in_,
        occurred_at=at or NOW,
        raw_text="Akmalga yozib qo'y",
    )
    session.add(row)
    await session.flush()
    return row


async def _owner_row(session, person, *, body="Ertaga 14:00 da gaplashamiz", **kw):
    now = kw.pop("now", NOW)
    if "command_interaction_id" not in kw:
        kw["command_interaction_id"] = (await _command(session, at=now)).id
    return await outbox.create(
        session,
        channel=kw.pop("channel", "telegram"),
        chat_key=kw.pop("chat_key", KEY),
        origin="owner_command",
        authored_by="owner",
        body=body,
        now=now,
        person_id=person.id if person else None,
        **kw,
    )


async def _approved(session, row, *, by="button", now=NOW):
    result = await outbox.approve(session, row.id, by=by, now=now)
    assert result.approved, result.code
    return result.row


async def _reload(session, row_id) -> m.OutboxMessage:
    return await session.scalar(
        sa.select(m.OutboxMessage)
        .where(m.OutboxMessage.id == row_id)
        .execution_options(populate_existing=True)
    )


# --- the state machine -------------------------------------------------------------


def test_the_transition_table_is_the_plans():
    t = outbox.TRANSITIONS
    assert t["new"] == {"shadow", "draft", "approved", "blocked"}
    assert t["draft"] == {"approved", "blocked", "cancelled", "expired"}
    assert t["approved"] == {"sending", "draft", "blocked", "cancelled", "expired"}
    assert t["sending"] == {"sent", "failed", "approved", "unknown"}
    # In doubt is never queued again: only a report can settle it.
    assert t["unknown"] == {"sent", "failed"}
    for terminal in ("sent", "failed", "cancelled", "expired", "blocked"):
        assert t[terminal] == frozenset(), terminal
    # Nothing ever goes straight from a draft or a shadow row to the wire.
    assert "sending" not in t["draft"] | t["shadow"]
    assert set(t) - {"new"} == set(m.OUTBOX_STATES)


async def test_state_machine_transitions(session, jarvis):
    person, trigger = await _setup(session)
    row = await _ai_row(session, person, trigger)
    assert row.state == "draft"
    with pytest.raises(outbox.IllegalTransition):
        await outbox.complete(session, row.id, external_id=f"{DM}:1", sent_at=NOW)

    await _approved(session, row)
    with pytest.raises(outbox.AlreadyAnswered):
        await outbox.approve(session, row.id, by="button", now=NOW)
    [claimed] = await outbox.claim_ready(session, "telegram", now=NOW)
    assert claimed.id == row.id and claimed.body == row.body
    assert (await _reload(session, row.id)).state == "sending"
    with pytest.raises(outbox.AlreadyAnswered):
        await outbox.cancel(session, row.id, by="button", now=NOW)

    done = await outbox.complete(session, row.id, external_id=f"{DM}:900", sent_at=NOW)
    assert done.state == "sent" and done.sent_at == NOW
    # The same report twice is one send.
    again = await outbox.complete(session, row.id, external_id=f"{DM}:900", sent_at=NOW)
    assert again.id == done.id

    states = [
        (e["old"], e["new"], e["by"]) for e in done.history if e["field"] == "state"
    ]
    assert states == [
        (None, "draft", "auto:policy"),
        ("draft", "approved", "button"),
        ("approved", "sending", "gate"),
        ("sending", "sent", "adapter"),
    ]
    assert all(set(e) >= {"at", "field", "old", "new", "by"} for e in done.history)
    assert all(len(e["by"]) <= 16 for e in done.history)


async def test_cancel_expire_and_fail(session, jarvis):
    person, trigger = await _setup(session)
    first = await _ai_row(session, person, trigger, body="Assalomu alaykum!")
    cancelled = await outbox.cancel(session, first.id, by="button", now=NOW)
    assert cancelled.state == "cancelled"

    late = await _ai_row(session, person, trigger, body="Salom, aka!")
    assert late.expires_at == NOW + timedelta(hours=settings.jarvis_draft_expiry_hours)
    later = late.expires_at + timedelta(seconds=1)
    assert await outbox.expire_due(session, now=later) == [late.id]
    assert (await _reload(session, late.id)).state == "expired"
    # An expired draft can no longer be approved.
    with pytest.raises(outbox.AlreadyAnswered):
        await outbox.approve(session, late.id, by="button", now=later)

    flaky = await _approved(
        session, await _ai_row(session, person, trigger, body="Xo'p!")
    )
    [claimed] = await outbox.claim_ready(session, "telegram", now=NOW)
    retry = NOW + timedelta(minutes=3)
    row = await outbox.fail(
        session, claimed.id, code="flood_wait", now=NOW, retry_at=retry
    )
    assert row.state == "approved" and row.next_attempt_at == retry
    assert await outbox.claim_ready(session, "telegram", now=NOW) == []
    [claimed] = await outbox.claim_ready(session, "telegram", now=retry)
    row = await outbox.fail(session, claimed.id, code="user_blocked", now=retry)
    assert row.state == "failed" and row.last_error == "user_blocked"
    assert row.id == flaky.id


async def test_idempotency_key_unique(session, jarvis):
    person, trigger = await _setup(session)
    first = await _ai_row(session, person, trigger)
    second = await _ai_row(session, person, trigger)
    assert first.id == second.id
    assert first.idempotency_key == outbox.idempotency_key(
        origin="auto_reply",
        interaction_id=trigger.id,
        chat_key=KEY,
        body_sha256=gate.body_hash(first.body),
    )
    # Another body is another proposal.
    other = await _ai_row(session, person, trigger, body="Qanday yordam bera olaman?")
    assert other.id != first.id
    # The database itself refuses a second row with the key.
    session.add(
        m.OutboxMessage(
            channel="telegram",
            chat_key=KEY,
            origin="auto_reply",
            authored_by="ai",
            body="x",
            body_sha256="0" * 64,
            state="draft",
            idempotency_key=first.idempotency_key,
        )
    )
    with pytest.raises(sa.exc.IntegrityError):
        await session.flush()
    await session.rollback()


# --- concurrency ------------------------------------------------------------------


@pytest.fixture
async def committed(session):
    """Tests that need several connections commit for real; the session
    fixture's truncate cleans up after them."""
    yield session
    await session.rollback()


async def test_concurrent_claim_skip_locked(committed, jarvis):
    session = committed
    person, trigger = await _setup(session)
    rows = []
    for text in ("Salom!", "Assalomu alaykum!", "Xush kelibsiz!"):
        rows.append(
            await _approved(session, await _ai_row(session, person, trigger, body=text))
        )
    await session.commit()

    sent: list[int] = []

    async def waiting() -> int:
        async with SessionLocal() as s:
            return int(
                await s.scalar(
                    sa.select(sa.func.count())
                    .select_from(m.OutboxMessage)
                    .where(m.OutboxMessage.state == "approved")
                )
                or 0
            )

    async def fake_adapter(name: str) -> None:
        """Claims, 'sends', completes — exactly the adapter contract. A claim
        that finds another one running gets nothing and polls again."""
        for _ in range(200):
            async with SessionLocal() as s:
                claimed = await outbox.claim_ready(
                    s, "telegram", now=NOW, account=name, limit=1
                )
                await s.commit()
            for item in claimed:
                sent.append(item.id)
                async with SessionLocal() as s:
                    await outbox.complete(
                        s, item.id, external_id=f"{DM}:{5000 + item.id}", sent_at=NOW
                    )
                    await s.commit()
            if not await waiting():
                return
            await asyncio.sleep(0.01)

    await asyncio.gather(*(fake_adapter(f"w{i}") for i in range(4)))
    assert sorted(sent) == sorted(r.id for r in rows)  # each exactly once
    async with SessionLocal() as s:
        states = set(
            await s.scalars(
                sa.select(m.OutboxMessage.state).where(
                    m.OutboxMessage.id.in_([r.id for r in rows])
                )
            )
        )
    assert states == {"sent"}


async def test_a_locked_row_is_skipped_not_waited_for(committed, jarvis):
    session = committed
    person, trigger = await _setup(session)
    row = await _approved(session, await _ai_row(session, person, trigger))
    await session.commit()
    async with SessionLocal() as first, SessionLocal() as second:
        [claimed] = await outbox.claim_ready(first, "telegram", now=NOW)
        # The first claim's transaction still holds the row lock.
        assert (
            await asyncio.wait_for(
                outbox.claim_ready(second, "telegram", now=NOW), timeout=10
            )
            == []
        )
        await first.commit()
        await second.rollback()
    assert claimed.id == row.id


async def test_concurrent_claims_are_serialised(committed, jarvis, monkeypatch):
    """Two claimers at once never both see room under the gap or a cap: the
    second gets nothing while the first claim runs, and the next poll counts
    the first claim."""
    session = committed
    monkeypatch.setattr(settings, "jarvis_send_min_gap_seconds", 20)
    person, trigger = await _setup(session)
    first = await _approved(session, await _ai_row(session, person, trigger))
    second = await _approved(
        session, await _ai_row(session, person, trigger, body="Xush kelibsiz!")
    )
    await session.commit()
    async with SessionLocal() as one, SessionLocal() as two:
        [claimed] = await outbox.claim_ready(one, "telegram", now=NOW, limit=1)
        # The second row is not locked, yet the second claimer waits its turn.
        assert (
            await asyncio.wait_for(
                outbox.claim_ready(two, "telegram", now=NOW, limit=1), timeout=10
            )
            == []
        )
        await two.rollback()
        await one.commit()
        # Now the first claim is counted: the gap holds the second row.
        assert await outbox.claim_ready(two, "telegram", now=NOW, limit=1) == []
        await two.commit()
    assert claimed.id == first.id
    held = await _reload(session, second.id)
    assert held.state == "approved" and held.last_error == "send_gap"


# --- kill switches and pauses -----------------------------------------------------


async def test_pause_blocks_claim_of_approved_row(session, jarvis):
    person, trigger = await _setup(session)
    row = await _approved(session, await _ai_row(session, person, trigger))
    await policy.pause(session, by="command", now=NOW)
    assert await outbox.claim_ready(session, "telegram", now=NOW) == []
    held = await _reload(session, row.id)
    assert held.state == "approved" and held.last_error == "paused"
    assert held.history[-1]["field"] == "hold"  # recorded, never a silent skip

    await policy.resume(session, by="command", now=NOW)
    later = held.next_attempt_at
    [claimed] = await outbox.claim_ready(session, "telegram", now=later)
    assert claimed.id == row.id


async def test_channel_pause_and_breaker_stop_only_that_channel(session, jarvis):
    person, trigger = await _setup(session)
    row = await _approved(session, await _ai_row(session, person, trigger))
    until = NOW + timedelta(hours=1)
    await policy.trip_breaker(
        session, channel="telegram", reason="peer_flood", until=until, by="gate", now=NOW
    )
    assert await outbox.claim_ready(session, "telegram", now=NOW) == []
    held = await _reload(session, row.id)
    assert held.last_error == "breaker" and held.next_attempt_at == until
    [claimed] = await outbox.claim_ready(session, "telegram", now=until)
    assert claimed.id == row.id


async def test_kill_switches(session, jarvis, monkeypatch):
    person, trigger = await _setup(session)
    row = await _approved(session, await _ai_row(session, person, trigger))
    monkeypatch.setattr(outbox, "channel_send_enabled", lambda channel: False)
    assert await outbox.claim_ready(session, "telegram", now=NOW) == []
    assert (await _reload(session, row.id)).last_error == "channel_disabled"
    # Off: nothing is created or claimed, and nothing is even looked at.
    monkeypatch.setattr(settings, "jarvis_enabled", False)
    assert (
        await outbox.claim_ready(session, "telegram", now=NOW + timedelta(hours=1)) == []
    )
    with pytest.raises(outbox.JarvisDisabled):
        await _ai_row(session, person, trigger, body="Yangi qoralama")
    monkeypatch.setattr(settings, "jarvis_enabled", True)
    monkeypatch.setattr(settings, "jarvis_mode", "off")
    with pytest.raises(outbox.JarvisDisabled):
        await _owner_row(session, person)
    # The channel switch keys land with their adapters; until then: off.
    monkeypatch.undo()
    assert outbox.channel_send_enabled("telegram") is False
    assert outbox.channel_send_enabled("nowhere") is False


# --- the guard ---------------------------------------------------------------------


async def test_guard_hash_mismatch_reguards(session, jarvis):
    person, trigger = await _setup(session, mode="auto")
    # An autonomous row, and three the owner approved on their card.
    auto = await _ai_row(session, person, trigger, body="Salom!")
    assert auto.approved_by == "auto:policy"
    tapped = []
    for text in ("Assalomu alaykum!", "Xush kelibsiz!", "Yaxshimisiz!"):
        row = await _ai_row(session, person, trigger, body=text, intent="price")
        tapped.append(await _approved(session, row))
    # The body changes behind the guard's back (a bug, a hand edit).
    changed = ("Salom, aka!", "Narxi 100$", "Yana bir matn", "Boshqa matn")
    for row, body in zip([auto, *tapped], changed, strict=True):
        await session.execute(
            sa.text("UPDATE outbox_messages SET body = :b WHERE id = :i"),
            {"b": body, "i": row.id},
        )
    jarvis.calls.clear()

    # The autonomous row is re-guarded and sent with the fresh verdict bound.
    [claimed] = await outbox.claim_ready(session, "telegram", now=NOW, limit=1)
    fresh = await _reload(session, claimed.id)
    assert claimed.id == auto.id and claimed.body == "Salom, aka!"
    assert jarvis.calls == [("Salom, aka!", "reply")]
    assert (
        fresh.guard["body_sha256"] == gate.body_hash("Salom, aka!") == fresh.body_sha256
    )

    gate.register_guard(PassGuard("block"))
    assert await outbox.claim_ready(session, "telegram", now=NOW, limit=1) == []
    blocked = await _reload(session, tapped[0].id)
    assert blocked.state == "blocked" and blocked.last_error == "guard_failed"

    # The owner tapped send on another text: a passing new body goes back to
    # the card, never out (rule 16).
    gate.register_guard(PassGuard())
    assert await outbox.claim_ready(session, "telegram", now=NOW, limit=1) == []
    back = await _reload(session, tapped[1].id)
    assert back.state == "draft" and back.last_error == "body_changed"
    assert back.approved_by is None
    assert back.guard["body_sha256"] == gate.body_hash("Yana bir matn")

    gate.register_guard(None)
    assert await outbox.claim_ready(session, "telegram", now=NOW, limit=1) == []
    third = await _reload(session, tapped[2].id)
    assert third.state == "blocked" and third.last_error == "guard_unavailable"


async def test_an_unapproved_statement_is_a_draft_never_auto(session, jarvis):
    """Rule 13a: an AI text with a free-form statement in an auto intent is
    a draft card, never approved on its own; the owner's tap still sends it."""
    person, trigger = await _setup(session, mode="auto")
    gate.register_guard(PassGuard(shapes_ok=False))
    row = await _ai_row(session, person, trigger, body="Bosh ustiga, aka.")
    assert row.state == "draft" and row.approved_by is None
    assert row.last_error == gate.UNAPPROVED_STATEMENT
    assert row.guard["verdict"] == "pass" and row.guard["shapes_ok"] is False
    assert row.history[0]["code"] == gate.UNAPPROVED_STATEMENT
    assert await outbox.claim_ready(session, "telegram", now=NOW) == []
    # A draft is never blocked by the allowlist: the owner reads and taps.
    tapped = await _approved(session, row)
    assert tapped.approved_by == "button"
    [claimed] = await outbox.claim_ready(session, "telegram", now=NOW)
    assert claimed.id == row.id
    # A shaped text in the same intent is approved on its own.
    gate.register_guard(PassGuard())
    auto = await _ai_row(session, person, trigger, body="Salom, aka!")
    assert auto.state == "approved" and auto.approved_by == "auto:policy"
    assert auto.guard["shapes_ok"] is True and auto.last_error is None


async def test_an_auto_row_without_shapes_ok_is_never_sent(session, jarvis):
    """At the claim an autonomous row whose verdict lacks a passing shape
    check (it says no, or it was stored before the check) goes back to the
    owner's card; so does a re-guarded body that fails it."""
    person, trigger = await _setup(session, mode="auto")
    said_no = await _ai_row(session, person, trigger, body="Salom!")
    legacy = await _ai_row(session, person, trigger, body="Assalomu alaykum!")
    changed = await _ai_row(session, person, trigger, body="Rahmat!")
    for row in (said_no, legacy, changed):
        assert row.approved_by == "auto:policy"
    await session.execute(
        sa.text(
            "UPDATE outbox_messages SET guard = jsonb_set(guard, '{shapes_ok}', "
            "'false') WHERE id = :i"
        ),
        {"i": said_no.id},
    )
    await session.execute(
        sa.text("UPDATE outbox_messages SET guard = guard - 'shapes_ok' WHERE id = :i"),
        {"i": legacy.id},
    )
    await session.execute(
        sa.text("UPDATE outbox_messages SET body = :b WHERE id = :i"),
        {"b": "Bosh ustiga, aka.", "i": changed.id},
    )
    gate.register_guard(PassGuard(shapes_ok=False))
    assert await outbox.claim_ready(session, "telegram", now=NOW) == []
    for row in (said_no, legacy, changed):
        back = await _reload(session, row.id)
        assert back.state == "draft", back.id
        assert back.last_error == gate.UNAPPROVED_STATEMENT
        assert back.approved_by is None and back.approved_at is None
        assert back.history[-1]["by"] == "gate"
    fresh = await _reload(session, changed.id)
    assert fresh.guard["body_sha256"] == gate.body_hash("Bosh ustiga, aka.")
    assert fresh.guard["shapes_ok"] is False and fresh.guard["verdict"] == "pass"
    # Nothing of them reached an adapter.
    assert (await caps.day_counts(session, NOW)).auto_sent == 0


def test_gate_refuses_auto_without_a_passing_shape_check():
    """gate.evaluate: an autonomous row needs ``shapes_ok`` in its bound
    verdict; an owner-tapped AI row does not; a re-guard binds the fresh one."""
    sha = gate.body_hash("Assalomu alaykum!")
    for guard in (
        {"verdict": "pass", "body_sha256": sha},
        {"verdict": "pass", "body_sha256": sha, "shapes_ok": False},
        {"verdict": "pass", "body_sha256": sha, "shapes_ok": "true"},
    ):
        decision = gate.evaluate(_gate_row(guard=guard), _gate_ctx())
        assert (decision.action, decision.line, decision.code) == (
            "redraft",
            "guard",
            gate.UNAPPROVED_STATEMENT,
        ), guard
        assert not decision.sends
        tapped = _gate_row(guard=guard, approved_by="button")
        assert gate.evaluate(tapped, _gate_ctx()).sends
    assert gate.evaluate(_gate_row(), _gate_ctx()).sends
    # The body moved: re-guarded, a failing shape check is a redraft with the
    # fresh verdict bound, a passing one a send.
    moved = _gate_row(guard={"verdict": "pass", "body_sha256": "0" * 64})
    back = gate.evaluate(moved, _gate_ctx(guard=PassGuard(shapes_ok=False)))
    assert (back.action, back.code) == ("redraft", gate.UNAPPROVED_STATEMENT)
    assert back.guard is not None and not back.guard.shapes_ok
    assert back.body_sha256 == sha
    assert gate.evaluate(moved, _gate_ctx(guard=PassGuard())).sends


def test_gate_rechecks_a_kb_row_at_the_claim():
    """An autonomous row approved for a KB text is guarded again at the
    claim with the entries active now: a retired entry approves nothing."""
    body = "Ofisimiz Toshkentda joylashgan."
    sha = gate.body_hash(body)
    bound = {"verdict": "pass", "body_sha256": sha, "shapes_ok": True, "shapes": ["kb"]}

    class KbGuard:
        def check(self, text, *, role, allowed_tokens, approved_kb=(), **names):
            return gate.GuardResult(
                verdict="pass", shapes_ok=text in approved_kb, shapes=("kb",)
            )

    row = _gate_row(body=body, guard=bound, intent="general_question")
    retired = gate.evaluate(row, _gate_ctx(guard=KbGuard()))
    assert (retired.action, retired.code) == ("redraft", gate.UNAPPROVED_STATEMENT)
    assert not retired.sends
    active = gate.evaluate(row, _gate_ctx(guard=KbGuard(), approved_kb=(body,)))
    assert active.sends and active.code == "reguarded"
    # The owner's tap needs no KB: the bound verdict stands.
    tapped = _gate_row(body=body, guard=bound, approved_by="button")
    assert gate.evaluate(tapped, _gate_ctx(guard=KbGuard())).sends


async def test_create_takes_no_approved_texts_from_its_caller(session, jarvis):
    """Rule 13a: a caller cannot hand create() the texts that make a body an
    approved statement (JV-15/JV-14 load them from the owner's records), nor
    a bare string read one character a text."""
    person, trigger = await _setup(session, mode="auto")
    fact = "Ofisimiz Toshkentda joylashgan."
    for kw in (
        {"approved_kb": (fact,)},
        {"approved_kb": fact},
        {"stall_texts": (fact,)},
        {"stall_texts": "x"},
    ):
        with pytest.raises(outbox.OutboxError, match=outbox.APPROVED_TEXTS_REFUSED):
            await _ai_row(session, person, trigger, body=fact, **kw)
    rows = await session.scalar(sa.select(sa.func.count()).select_from(m.OutboxMessage))
    assert rows == 0


async def test_approve_refuses_the_auto_approver(session, jarvis):
    """Only create() approves on its own: a tap ``by`` the policy approver
    is refused and the draft stays a draft."""
    gate.register_guard(PassGuard(shapes_ok=False))
    person, trigger = await _setup(session, mode="auto")
    draft = await _ai_row(session, person, trigger, intent="info_gathering")
    assert draft.state == "draft" and draft.last_error == gate.UNAPPROVED_STATEMENT
    with pytest.raises(outbox.OutboxError, match=outbox.BAD_APPROVER):
        await outbox.approve(session, draft.id, by=outbox.AUTO_APPROVER, now=NOW)
    assert (await _reload(session, draft.id)).state == "draft"
    assert (await _approved(session, draft)).approved_by == "button"


async def test_no_guard_fails_closed(session, jarvis, no_guard):
    person, trigger = await _setup(session)
    # A new AI draft cannot pass a guard that does not exist.
    row = await _ai_row(session, person, trigger)
    assert row.state == "blocked" and row.last_error == "guard_unavailable"
    assert row.guard["verdict"] == "guard_unavailable"

    # A draft that passed once is re-guarded at approval when its body moved:
    # without a guard it stays a draft, with the code recorded.
    gate.register_guard(PassGuard())
    draft = await _ai_row(session, person, trigger, body="Salom!")
    gate.register_guard(None)
    await session.execute(
        sa.text("UPDATE outbox_messages SET body = 'Salom, aka!' WHERE id = :i"),
        {"i": draft.id},
    )
    result = await outbox.approve(session, draft.id, by="button", now=NOW)
    assert not result.approved and result.code == "guard_unavailable"
    kept = await _reload(session, draft.id)
    assert kept.state == "draft" and kept.last_error == "guard_unavailable"

    # The owner's own words never wait on a guard.
    owner = await _owner_row(session, person)
    assert owner.state == "draft"
    assert (await outbox.approve(session, owner.id, by="button", now=NOW)).approved


async def test_sent_requires_approved_check(session):
    session.add(
        m.OutboxMessage(
            channel="telegram",
            chat_key=KEY,
            origin="owner_command",
            authored_by="owner",
            body="salom",
            body_sha256="0" * 64,
            state="sent",
            idempotency_key="k-sent",
        )
    )
    with pytest.raises(sa.exc.IntegrityError) as caught:
        await session.flush()
    assert "ck_outbox_messages_sent_approved" in str(caught.value)
    await session.rollback()


@pytest.mark.parametrize("state", ["approved", "sending", "sent"])
async def test_ai_row_cannot_be_approved_without_pass_check(session, state):
    def row(authored_by: str, guard: dict, key: str) -> m.OutboxMessage:
        return m.OutboxMessage(
            channel="telegram",
            chat_key=KEY,
            origin="auto_reply",
            authored_by=authored_by,
            body="salom",
            body_sha256="0" * 64,
            guard=guard,
            state=state,
            approved_by="button",
            idempotency_key=key,
        )

    for guard in ({}, {"verdict": "block"}, {"verdict": "guard_unavailable"}):
        session.add(row("ai", guard, f"k-{state}"))
        with pytest.raises(sa.exc.IntegrityError) as caught:
            await session.flush()
        assert "ck_outbox_messages_guarded" in str(caught.value)
        await session.rollback()
    # The owner's own words and a passing AI text are allowed.
    session.add_all(
        [row("owner", {}, f"o-{state}"), row("ai", {"verdict": "pass"}, f"p-{state}")]
    )
    await session.flush()
    await session.rollback()


# --- caps and costs ----------------------------------------------------------------


async def _sent_row(session, person, *, at, body, chat_key=KEY, authored_by="ai"):
    """A row sent at ``at``, through the real path."""
    if authored_by == "ai":
        trigger = await _inbound(
            session,
            person,
            at=at - timedelta(minutes=1),
            tg_chat_id=chatkey.tg_chat_id(chat_key),
        )
        row = await outbox.create(
            session,
            channel="telegram",
            chat_key=chat_key,
            origin="auto_reply",
            authored_by="ai",
            body=body,
            now=at,
            intent="greeting",
            person_id=person.id,
            trigger_interaction_id=trigger.id,
        )
    else:
        row = await _owner_row(session, person, body=body, now=at, chat_key=chat_key)
    if row.state == "draft":
        await _approved(session, row, now=at)
    [claimed] = await outbox.claim_ready(session, "telegram", now=at, limit=1)
    assert claimed.id == row.id
    await outbox.complete(
        session, row.id, external_id=f"{DM}:{row.id + 7000}", sent_at=at
    )
    return row


async def test_caps_count_from_day_start(session, jarvis, monkeypatch):
    person, trigger = await _setup(session, mode="auto")
    midnight = day_start(NOW)
    await _sent_row(session, person, at=midnight - timedelta(hours=2), body="Kecha")
    first = await _sent_row(
        session, person, at=midnight + timedelta(hours=8), body="Bugun"
    )
    assert first.approved_by == "auto:policy"
    counts = await caps.day_counts(session, NOW)
    assert counts.sent == 1 and counts.auto_sent == 1
    assert await caps.chat_ai_sent_today(session, KEY, NOW) == 1

    # The per-chat cap holds the next AI row until the day turns.
    monkeypatch.setattr(settings, "jarvis_per_chat_daily_cap", 1)
    row = await _ai_row(session, person, trigger, body="Yana salom")
    assert row.state == "approved"
    assert await outbox.claim_ready(session, "telegram", now=NOW) == []
    held = await _reload(session, row.id)
    assert held.last_error == "chat_cap" and held.next_attempt_at == caps.next_day(NOW)
    assert caps.next_day(NOW) == midnight + timedelta(days=1)

    # The daily auto cap, counted the same way, holds a row in another chat.
    monkeypatch.setattr(settings, "jarvis_auto_daily_cap", 1)
    asked = await _inbound(session, None, tg_chat_id=DM + 1)
    other = await outbox.create(
        session,
        channel="telegram",
        chat_key=chatkey.telegram(DM + 1),
        origin="auto_reply",
        authored_by="ai",
        body="Salom!",
        now=NOW,
        intent="greeting",
        trigger_interaction_id=asked.id,
    )
    assert other.state == "approved"
    assert await outbox.claim_ready(session, "telegram", now=NOW) == []
    assert (await _reload(session, other.id)).last_error == "auto_cap"
    # A new day: the counts start again.
    tomorrow = midnight + timedelta(days=1, hours=9)
    assert (await caps.day_counts(session, tomorrow)).sent == 0


async def test_draft_cap_keeps_the_draft_off_the_cards(session, jarvis, monkeypatch):
    person, trigger = await _setup(session)
    monkeypatch.setattr(settings, "jarvis_draft_daily_cap", 1)
    first = await _ai_row(session, person, trigger, body="Salom!")
    second = await _ai_row(session, person, trigger, body="Assalomu alaykum!")
    assert first.state == second.state == "draft"
    assert first.last_error is None and second.last_error == caps.DRAFT_CAP_CODE
    counts = await caps.day_counts(session, NOW)
    assert counts.drafts == 2 and counts.draft_cards == 1


async def test_draft_cards_count_only_cards(session, jarvis, monkeypatch):
    person, trigger = await _setup(session, mode="auto")
    auto = await _ai_row(session, person, trigger)
    assert auto.state == "approved"
    group = -3003
    await _chat(session, tg_chat_id=group, chat_type=ChatType.group)
    asked = await _inbound(session, person, tg_chat_id=group)
    blocked = await outbox.create(
        session,
        channel="telegram",
        chat_key=chatkey.telegram(group),
        origin="auto_reply",
        authored_by="ai",
        body="Salom hammaga",
        now=NOW,
        intent="greeting",
        trigger_interaction_id=asked.id,
    )
    assert blocked.state == "blocked"
    card = await _ai_row(session, person, trigger, body="Narx haqida", intent="price")
    assert card.state == "draft"
    # Neither the blocked row nor the autonomous one was ever a card.
    counts = await caps.day_counts(session, NOW)
    assert counts.drafts == 1 and counts.draft_cards == 1

    # A draft made over the cap stays off the count after its last_error moves on.
    monkeypatch.setattr(settings, "jarvis_draft_daily_cap", 1)
    held = await _ai_row(session, person, trigger, body="Yana narx", intent="price")
    assert held.last_error == caps.DRAFT_CAP_CODE
    await _approved(session, held)
    assert (await _reload(session, held.id)).last_error is None
    counts = await caps.day_counts(session, NOW)
    assert counts.drafts == 2 and counts.draft_cards == 1

    # An autonomous row the gate sends back to the owner becomes a card.
    await policy.set_mode(
        session,
        scope=policy.channel_intent_scope("telegram", "greeting"),
        mode="draft",
        by="command",
        now=NOW,
    )
    await outbox.claim_ready(session, "telegram", now=NOW)
    assert (await _reload(session, auto.id)).state == "draft"
    counts = await caps.day_counts(session, NOW)
    assert counts.drafts == 3 and counts.draft_cards == 2


async def test_cost_cap_from_usage_log(session, jarvis):
    def usage(operation, cost, at=NOW):
        return m.UsageLog(
            provider="anthropic",
            model="m",
            operation=operation,
            cost_usd=cost,
            created_at=at,
        )

    session.add_all(
        [
            usage("jarvis_draft", Decimal("1.50")),
            usage("jarvis_guard", Decimal("1.00")),
            # Core MIYA's spend and yesterday's never count.
            usage("extract", Decimal("50")),
            usage("jarvis_draft", Decimal("9"), at=NOW - timedelta(days=1)),
        ]
    )
    await session.flush()
    spend = await costs.spend_today(session, NOW)
    assert spend == costs.Spend(usd=Decimal("2.50"), unpriced=0)
    assert not await costs.over_cap(session, NOW)
    session.add(usage("jarvis_intent", Decimal("0.60")))
    await session.flush()
    assert await costs.over_cap(session, NOW)
    # An unpriced Jarvis call makes the spend unknown: counted as over.
    assert costs.cap_reached(costs.Spend(Decimal("0"), unpriced=1), Decimal("3"))
    assert isinstance(settings.jarvis_daily_cost_cap_usd, Decimal)


async def test_cost_cap_holds_auto_rows_only(session, jarvis):
    person, trigger = await _setup(session, mode="auto")
    auto = await _ai_row(session, person, trigger)
    owner = await _approved(session, await _owner_row(session, person))
    session.add(
        m.UsageLog(
            provider="anthropic",
            operation="jarvis_draft",
            cost_usd=Decimal("5"),
            created_at=NOW,
        )
    )
    await session.flush()
    claimed = await outbox.claim_ready(session, "telegram", now=NOW)
    assert [c.id for c in claimed] == [owner.id]
    assert (await _reload(session, auto.id)).last_error == "cost_cap"


async def test_a_row_the_gate_cannot_judge_is_held_alone(session, jarvis, monkeypatch):
    person, trigger = await _setup(session)
    bad = await _approved(session, await _ai_row(session, person, trigger, body="Salom!"))
    good = await _approved(session, await _owner_row(session, person))
    real = outbox._trigger

    async def _broken(s, row):
        if row.id == bad.id:
            raise RuntimeError("boom")
        return await real(s, row)

    monkeypatch.setattr(outbox, "_trigger", _broken)
    claimed = await outbox.claim_ready(session, "telegram", now=NOW)
    assert [c.id for c in claimed] == [good.id]
    held = await _reload(session, bad.id)
    assert held.state == "approved" and held.last_error == "gate_error"
    assert held.next_attempt_at == NOW + outbox.HOLD_POLL


# --- the lease ---------------------------------------------------------------------


async def test_expired_lease_goes_unknown_not_resent(session, jarvis):
    person, trigger = await _setup(session)
    row = await _approved(session, await _ai_row(session, person, trigger))
    [claimed] = await outbox.claim_ready(session, "telegram", now=NOW)
    # Not before the lease runs out.
    assert await outbox.reconcile(session, now=NOW + timedelta(minutes=1)) == []
    after = claimed.lease_until + timedelta(seconds=1)
    assert await outbox.reconcile(session, now=after) == [
        outbox.Reconciled(row.id, "unknown")
    ]
    unknown = await _reload(session, row.id)
    assert unknown.state == "unknown" and unknown.last_error == "lease_expired"
    # Never resent blindly: no claim, ever, and a transient failure cannot requeue it.
    assert (
        await outbox.claim_ready(session, "telegram", now=after + timedelta(hours=1))
        == []
    )
    # The owner is told once.
    assert await outbox.unknown_untold(session) == [row.id]
    await outbox.mark_told(session, row.id, now=after)
    assert await outbox.unknown_untold(session) == []
    # A late report still settles it.
    sent = await outbox.complete(session, row.id, external_id=f"{DM}:4242", sent_at=after)
    assert sent.state == "sent"


async def test_unknown_rows_still_count_towards_the_caps(session, jarvis):
    person, trigger = await _setup(session, mode="auto")
    row = await _ai_row(session, person, trigger)
    assert row.approved_by == "auto:policy"
    [claimed] = await outbox.claim_ready(session, "telegram", now=NOW)
    after = claimed.lease_until + timedelta(seconds=1)
    await outbox.reconcile(session, now=after)
    assert (await _reload(session, row.id)).state == "unknown"
    # It may be on the wire: it counts like a sent row, from its claim, so a
    # crashed adapter never makes room for one more message.
    counts = await caps.day_counts(session, after)
    assert counts.sent == 1 and counts.auto_sent == 1
    assert await caps.chat_ai_sent_today(session, KEY, after) == 1
    assert await caps.channel_sent_today(session, "telegram", after) == 1
    assert await caps.last_send_at(session, "telegram") == NOW


async def test_expired_lease_with_an_echo_is_sent(session, jarvis):
    person, trigger = await _setup(session)
    row = await _approved(session, await _ai_row(session, person, trigger))
    [claimed] = await outbox.claim_ready(session, "telegram", now=NOW)
    # The userbot stored the echo, untagged: the sender died before complete().
    echo = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.out,
        tg_chat_id=DM,
        occurred_at=NOW + timedelta(seconds=5),
        raw_text=row.body,
        meta={"tg_message_id": 555},
    )
    session.add(echo)
    await session.flush()
    after = claimed.lease_until + timedelta(seconds=1)
    assert await outbox.reconcile(session, now=after) == [
        outbox.Reconciled(row.id, "sent")
    ]
    sent = await _reload(session, row.id)
    assert sent.external_message_id == authorship.telegram_external_id(DM, 555)
    assert sent.sent_interaction_id == echo.id
    await session.refresh(echo)
    assert authorship.is_ai_reply(echo) and echo.meta["outbox_id"] == row.id


# --- the echo ----------------------------------------------------------------------


async def test_complete_writes_the_tagged_echo(session, jarvis):
    person, trigger = await _setup(session)
    row = await _approved(session, await _ai_row(session, person, trigger))
    await outbox.claim_ready(session, "telegram", now=NOW)
    sent = await outbox.complete(session, row.id, external_id=f"{DM}:321", sent_at=NOW)
    echo = await session.get(m.Interaction, sent.sent_interaction_id)
    assert echo.direction is Direction.out and echo.tg_chat_id == DM
    assert echo.raw_text == row.body and echo.meta["tg_message_id"] == 321
    assert authorship.is_ai_reply(echo) and not authorship.is_owner_words(echo)
    state = await session.get(m.JarvisChatState, KEY)
    assert state.last_ai_sent_at == NOW and state.ai_turns_since_client == 1
    # The userbot sees the same message later: the unique index keeps one row,
    # and find_echo now names the outbox row.
    found = await authorship.find_echo(
        session, channel="telegram", external_id=authorship.telegram_external_id(DM, 321)
    )
    assert found == authorship.Echo(row.id, "ai")


async def test_complete_tags_an_already_ingested_echo(session, jarvis, monkeypatch):
    """Carry-over (b): the userbot stored the echo before the sender recorded
    the external id; complete() tags that row instead of writing a second."""
    person, trigger = await _setup(session)
    row = await _approved(
        session, await _owner_row(session, person, body="Kechki 6 da gaplashamiz")
    )
    await outbox.claim_ready(session, "telegram", now=NOW)
    earlier = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.out,
        tg_chat_id=DM,
        occurred_at=NOW,
        raw_text=row.body,
        meta={"tg_message_id": 654},
    )
    session.add(earlier)
    await session.flush()
    sent = await outbox.complete(session, row.id, external_id=f"{DM}:654", sent_at=NOW)
    assert sent.sent_interaction_id == earlier.id
    count = await session.scalar(
        sa.select(sa.func.count())
        .select_from(m.Interaction)
        .where(m.Interaction.tg_chat_id == DM, m.Interaction.direction == Direction.out)
    )
    assert count == 1
    await session.refresh(earlier)
    # Dictated by the owner: tagged as sent by MIYA, still the owner's words.
    assert earlier.meta["sent_by"] == "miya" and earlier.meta["authored_by"] == "owner"
    assert authorship.is_owner_words(earlier)


def _tg_out(message_id: int, text: str):
    peer = User(id=DM, first_name="Akmal", username="akmal_test")

    async def get_chat():
        return peer

    return SimpleNamespace(
        id=message_id,
        chat_id=DM,
        date=NOW,
        message=text,
        out=True,
        mentioned=False,
        file=None,
        sticker=None,
        gif=None,
        voice=None,
        audio=None,
        video_note=None,
        photo=None,
        video=None,
        document=None,
        get_chat=get_chat,
        get_sender=get_chat,
    )


async def _echoes(message_id: int) -> list[m.Interaction]:
    async with SessionLocal() as s:
        return list(
            await s.scalars(
                sa.select(m.Interaction).where(
                    m.Interaction.tg_chat_id == DM,
                    m.Interaction.meta["tg_message_id"].astext == str(message_id),
                )
            )
        )


async def test_userbot_and_complete_store_one_tagged_echo_either_order(committed, jarvis):
    session = committed
    person, trigger = await _setup(session)
    first = await _approved(
        session, await _ai_row(session, person, trigger, body="Salom!")
    )
    second = await _approved(
        session, await _ai_row(session, person, trigger, body="Xo'p!")
    )
    await outbox.claim_ready(session, "telegram", now=NOW)
    await session.commit()

    # The userbot is first: it stores the echo untagged (no external id yet),
    # and complete() tags that row.
    assert await userbot.ingest_message(None, _tg_out(901, first.body)) is True
    [untagged] = await _echoes(901)
    assert "sent_by" not in untagged.meta
    await outbox.complete(session, first.id, external_id=f"{DM}:901", sent_at=NOW)
    await session.commit()
    [echo] = await _echoes(901)
    assert echo.id == untagged.id and echo.meta["outbox_id"] == first.id
    assert authorship.is_ai_reply(echo)

    # complete() is first: the userbot's copy is the same row (and find_echo
    # would tag it anyway).
    await outbox.complete(session, second.id, external_id=f"{DM}:902", sent_at=NOW)
    await session.commit()
    assert await userbot.ingest_message(None, _tg_out(902, second.body)) is False
    [echo] = await _echoes(902)
    assert echo.meta["outbox_id"] == second.id and authorship.is_ai_reply(echo)


# --- the gate ----------------------------------------------------------------------


async def test_gate_blocks_ai_row_in_quarantined_chat(session, jarvis):
    person, trigger = await _setup(session)
    ai = await _approved(session, await _ai_row(session, person, trigger))
    owner = await _approved(session, await _owner_row(session, person))
    # A later message in the chat is a scam (JV-09).
    await _inbound(
        session,
        person,
        text="Kartangizdagi SMS kodni yuboring",
        at=NOW - timedelta(minutes=1),
        meta={"safety": {"verdict": "scam", "score": 90, "reasons": ["code_request"]}},
    )
    assert await outbox.claim_ready(session, "telegram", now=NOW) == []
    blocked = await _reload(session, ai.id)
    assert blocked.state == "blocked" and blocked.last_error == "unsafe_chat"
    # The owner's own row goes back to its card for a fresh warning.
    back = await _reload(session, owner.id)
    assert back.state == "draft" and back.last_error == "safety_warning"
    unsafe = await outbox.unsafe_inbound(session, back, None)
    await outbox.record_warning(
        session,
        owner.id,
        kind=gate.WARNED_SAFETY,
        value=max(unsafe),
        by="button",
        now=NOW,
    )
    await _approved(session, back)
    [claimed] = await outbox.claim_ready(session, "telegram", now=NOW)
    assert claimed.id == owner.id


async def test_gate_blocks_ai_row_after_dangerous_file(session, jarvis):
    person, trigger = await _setup(session)
    ai = await _approved(session, await _ai_row(session, person, trigger))
    await _inbound(
        session,
        person,
        text=None,
        at=NOW - timedelta(minutes=1),
        media={"type": "document", "risk": {"kind": "exec", "name": "ilova.apk"}},
    )
    assert await outbox.claim_ready(session, "telegram", now=NOW) == []
    blocked = await _reload(session, ai.id)
    assert blocked.state == "blocked" and blocked.last_error == "unsafe_chat"


async def test_a_safe_file_or_an_earlier_scam_does_not_block(session, jarvis):
    person, _ = await _setup(session)
    await _inbound(
        session,
        person,
        at=NOW - timedelta(hours=2),
        meta={"safety": {"verdict": "spam", "score": 60, "reasons": []}},
    )
    trigger = await _inbound(
        session, person, media={"type": "photo", "risk": {"kind": "ok", "name": None}}
    )
    ai = await _approved(session, await _ai_row(session, person, trigger))
    [claimed] = await outbox.claim_ready(session, "telegram", now=NOW)
    assert claimed.id == ai.id


async def test_gate_personal_rule_blocks_ai_allows_warned_owner_row(session, jarvis):
    person, trigger = await _setup(session)
    ai = await _approved(session, await _ai_row(session, person, trigger))
    owner = await _approved(session, await _owner_row(session, person))
    session.add(
        m.ReplyRule(
            subject="person",
            person_id=person.id,
            address=f"tg:{DM}",
            mode="never",
            reason="personal",
            set_by="command",
        )
    )
    await session.flush()
    assert await outbox.claim_ready(session, "telegram", now=NOW) == []
    assert (await _reload(session, ai.id)).last_error == "personal_chat"
    assert (await _reload(session, ai.id)).state == "blocked"
    back = await _reload(session, owner.id)
    assert back.state == "draft" and back.last_error == "personal_warning"

    await outbox.record_warning(
        session, owner.id, kind=gate.WARNED_PERSONAL, value=True, by="button", now=NOW
    )
    await _approved(session, back)
    [claimed] = await outbox.claim_ready(session, "telegram", now=NOW)
    assert claimed.id == owner.id
    # A new AI draft for that chat is blocked at once.
    late = await _ai_row(session, person, trigger, body="Yana salom")
    assert late.state == "blocked" and late.last_error == "personal_chat"


async def test_the_ai_never_writes_first(session, jarvis):
    person, trigger = await _setup(session, mode="auto")
    with pytest.raises(outbox.OutboxError) as caught:
        await _ai_row(session, person, None)
    assert str(caught.value) == "no_trigger"
    # An owner command names the message it comes from, so the same short
    # text a day later is a new row, never yesterday's.
    with pytest.raises(outbox.OutboxError) as caught:
        await _owner_row(session, person, command_interaction_id=None)
    assert str(caught.value) == "no_source_interaction"
    first = await _owner_row(session, person, body="Ok")
    again = await _owner_row(session, person, body="Ok", now=NOW + timedelta(days=1))
    assert first.id != again.id

    # A row that lost its trigger after create (a hand edit) never leaves.
    row = await _ai_row(session, person, trigger)
    assert row.state == "approved"
    await session.execute(
        sa.text("UPDATE outbox_messages SET trigger_interaction_id = NULL WHERE id = :i"),
        {"i": row.id},
    )
    claimed = await outbox.claim_ready(session, "telegram", now=NOW)
    assert row.id not in {c.id for c in claimed}
    blocked = await _reload(session, row.id)
    assert blocked.state == "blocked" and blocked.last_error == "no_trigger"
    lonely = _gate_row(trigger_interaction_id=None)
    decision = gate.evaluate(lonely, _gate_ctx(trigger=None))
    assert (decision.action, decision.line, decision.code) == (
        "block",
        "trigger",
        "no_trigger",
    )


async def test_the_safety_line_fails_closed_where_it_cannot_read(session, jarvis):
    """WhatsApp and Gmail verdicts cannot be read yet: forcing the channel
    switch on never lets an AI row past the safety line."""
    person, trigger = await _setup(session, mode="auto")
    jid = "998901234567@s.whatsapp.net"
    key = chatkey.whatsapp(jid)
    ai = await outbox.create(
        session,
        channel="whatsapp",
        chat_key=key,
        origin="auto_reply",
        authored_by="ai",
        body="Assalomu alaykum!",
        now=NOW,
        intent="greeting",
        person_id=person.id,
        to_address=jid,
        trigger_interaction_id=trigger.id,
    )
    assert ai.state == "approved"
    owner = await _approved(
        session, await _owner_row(session, person, channel="whatsapp", chat_key=key)
    )
    assert outbox.channel_send_enabled("whatsapp")
    assert await outbox.claim_ready(session, "whatsapp", now=NOW) == []
    blocked = await _reload(session, ai.id)
    assert blocked.state == "blocked" and blocked.last_error == "safety_unreadable"
    held = await _reload(session, owner.id)
    assert held.state == "approved" and held.last_error == "safety_unreadable"


async def test_gate_hard_exclusions(session, jarvis):
    owner_self = await _person(session, "Men", telegram_id=OWNER_ID)
    row = await _owner_row(
        session, owner_self, chat_key=chatkey.telegram(OWNER_ID), body="eslatma"
    )
    assert row.state == "blocked" and row.last_error == "saved_messages"
    service = await _owner_row(
        session, None, chat_key=chatkey.telegram(777000), body="kod"
    )
    assert service.last_error == "service_chat"
    bot = await _person(session, "Helper", telegram_id=4444)
    bot.telegram_username = "helper_bot"
    await session.flush()
    to_bot = await _owner_row(session, bot, chat_key=chatkey.telegram(4444), body="start")
    assert to_bot.last_error == "bot_chat"
    await _chat(session, tg_chat_id=-1001, chat_type=ChatType.channel)
    channel = await _owner_row(session, None, chat_key=chatkey.telegram(-1001), body="x")
    assert channel.last_error == "channel_chat"


async def test_groups_get_no_ai_reply_unless_enabled_and_addressed(session, jarvis):
    person, _ = await _setup(session)
    group = -2002
    await _chat(session, tg_chat_id=group, chat_type=ChatType.group)
    trigger = await _inbound(session, person, tg_chat_id=group)

    async def ai_row(body):
        return await outbox.create(
            session,
            channel="telegram",
            chat_key=chatkey.telegram(group),
            origin="auto_reply",
            authored_by="ai",
            body=body,
            now=NOW,
            intent="greeting",
            trigger_interaction_id=trigger.id,
        )

    off = await ai_row("Salom hammaga")
    assert off.state == "blocked" and off.last_error == "group_off"
    session.add(
        m.ReplyRule(
            subject="chat",
            chat_key=chatkey.telegram(group),
            mode="draft",
            reason="group",
            set_by="command",
        )
    )
    await session.flush()
    row = await _approved(session, await ai_row("Salom, aka"))
    # Only a message addressed to the owner (meta.to_me) may get an AI reply.
    assert await outbox.claim_ready(session, "telegram", now=NOW) == []
    assert (await _reload(session, row.id)).last_error == "group_not_addressed"


async def test_gate_demoted_intent_blocks_auto_row(session, jarvis):
    person, trigger = await _setup(session, mode="auto")
    row = await _ai_row(session, person, trigger)
    assert row.state == "approved" and row.approved_by == "auto:policy"
    await policy.set_mode(
        session,
        scope=policy.channel_intent_scope("telegram", "greeting"),
        mode="draft",
        by="command",
        now=NOW,
    )
    assert await outbox.claim_ready(session, "telegram", now=NOW) == []
    back = await _reload(session, row.id)
    assert back.state == "draft" and back.last_error == "demoted"
    assert back.approved_by is None


async def test_env_ceiling_caps_auto(session, jarvis, monkeypatch):
    person, trigger = await _setup(session, mode="auto")
    monkeypatch.setattr(settings, "jarvis_mode", "draft")
    row = await _ai_row(session, person, trigger)
    assert row.state == "draft"  # the stored auto never exceeds the .env ceiling
    never = await _ai_row(session, person, trigger, body="Narx haqida", intent="price")
    assert never.state == "draft"
    monkeypatch.setattr(settings, "jarvis_mode", "shadow")
    shadow = await _ai_row(session, person, trigger, body="Kuzatuv")
    assert shadow.state == "shadow"


async def test_gate_owner_rows_count_towards_account_gap(session, jarvis, monkeypatch):
    person, trigger = await _setup(session, mode="auto")
    monkeypatch.setattr(settings, "jarvis_send_min_gap_seconds", 20)
    sent_at = NOW - timedelta(seconds=5)
    await _sent_row(session, person, at=sent_at, body="O'zim yozdim", authored_by="owner")
    row = await _ai_row(session, person, trigger)
    assert await outbox.claim_ready(session, "telegram", now=NOW) == []
    held = await _reload(session, row.id)
    assert held.last_error == "send_gap"
    assert held.next_attempt_at == sent_at + timedelta(seconds=20)
    # The owner's row never counts towards the AI's own caps.
    assert await caps.chat_ai_sent_today(session, KEY, NOW) == 0
    assert (await caps.day_counts(session, NOW)).auto_sent == 0
    ready = held.next_attempt_at
    claimed = await outbox.claim_ready(session, "telegram", now=ready)
    assert [c.id for c in claimed] == [row.id]
    # One claim run takes one row per account when a gap is set.
    a = await _ai_row(session, person, trigger, body="Birinchi")
    b = await _ai_row(session, person, trigger, body="Ikkinchi")
    later = ready + timedelta(minutes=5)
    claimed = await outbox.claim_ready(session, "telegram", now=later)
    assert [c.id for c in claimed] == [a.id]
    assert (await _reload(session, b.id)).last_error == "send_gap"


async def test_gate_rejects_non_text_payload(session, jarvis):
    base = _gate_row()
    ctx = _gate_ctx()
    for body in (b"bytes", None, {"media": "x"}, "", "   ", "a\x1bb", "x" * 4097):
        decision = gate.evaluate(replace(base, body=body), ctx)
        assert (decision.action, decision.code) == ("block", "payload_not_text"), body
    assert gate.evaluate(replace(base, body="Salom\nxayr\t!"), ctx).sends
    person, trigger = await _setup(session)
    with pytest.raises(outbox.NotPlainText):
        await _ai_row(session, person, trigger, body="  ")
    # A control character smuggled in after the fact never leaves.
    row = await _approved(session, await _ai_row(session, person, trigger))
    await session.execute(
        sa.text("UPDATE outbox_messages SET body = :b WHERE id = :i"),
        {"b": "Salom\x07", "i": row.id},
    )
    assert await outbox.claim_ready(session, "telegram", now=NOW) == []
    assert (await _reload(session, row.id)).last_error == "payload_not_text"


async def test_owner_replied_composing_and_takeover(session, jarvis):
    person, trigger = await _setup(session, mode="auto")
    row = await _ai_row(session, person, trigger)
    session.add(
        m.JarvisChatState(
            chat_key=KEY,
            channel="telegram",
            owner_composing_at=NOW - timedelta(seconds=10),
        )
    )
    await session.flush()
    assert await outbox.claim_ready(session, "telegram", now=NOW) == []
    held = await _reload(session, row.id)
    assert held.last_error == "owner_composing"
    state = await session.get(m.JarvisChatState, KEY)
    state.owner_replied_at = NOW - timedelta(seconds=1)
    await session.flush()
    assert await outbox.claim_ready(session, "telegram", now=held.next_attempt_at) == []
    cancelled = await _reload(session, row.id)
    assert cancelled.state == "cancelled" and cancelled.last_error == "owner_replied"
    assert cancelled.history[-1]["by"] == "auto:own"


async def test_edited_trigger_expires_and_scheduled_send_waits(session, jarvis):
    person, trigger = await _setup(session)
    row = await _approved(session, await _ai_row(session, person, trigger))
    trigger.meta = {**trigger.meta, "edits": [{"at": NOW.isoformat(), "text": "eski"}]}
    await session.flush()
    assert await outbox.claim_ready(session, "telegram", now=NOW) == []
    gone = await _reload(session, row.id)
    assert gone.state == "expired" and gone.last_error == "trigger_edited"

    at_six = NOW + timedelta(hours=6)
    later = await _approved(session, await _owner_row(session, person, not_before=at_six))
    assert later.expires_at == at_six + timedelta(
        hours=settings.jarvis_draft_expiry_hours
    )
    assert await outbox.claim_ready(session, "telegram", now=NOW) == []
    assert (await _reload(session, later.id)).next_attempt_at == at_six
    [claimed] = await outbox.claim_ready(session, "telegram", now=at_six)
    assert claimed.id == later.id


async def test_auto_rows_wait_out_the_quiet_hours(session, jarvis):
    person, trigger = await _setup(session, mode="auto")
    night = NOW.replace(hour=23, minute=50)
    row = await _ai_row(session, person, trigger, now=night - timedelta(minutes=1))
    assert row.state == "approved"
    assert await outbox.claim_ready(session, "telegram", now=night) == []
    held = await _reload(session, row.id)
    assert held.last_error == "quiet_hours"
    assert held.next_attempt_at == outbox.quiet_until(night)
    assert held.next_attempt_at.astimezone(TZ).hour == settings.quiet_hours_parsed[1].hour


def _gate_row(**kw) -> gate.Row:
    body = kw.pop("body", "Assalomu alaykum!")
    sha = gate.body_hash(body)
    values = {
        "id": 1,
        "channel": "telegram",
        "chat_key": KEY,
        "origin": "auto_reply",
        "authored_by": "ai",
        "body": body,
        "body_sha256": sha,
        "guard": {"verdict": "pass", "body_sha256": sha, "shapes_ok": True},
        "created_at": NOW - timedelta(minutes=1),
        "intent": "greeting",
        "approved_by": "auto:policy",
        "approved_at": NOW - timedelta(minutes=1),
        "trigger_interaction_id": 9,
    }
    values.update(kw)
    return gate.Row(**values)


def _gate_ctx(**kw) -> gate.Context:
    values = {
        "now": NOW,
        "jarvis_enabled": True,
        "channel_send_enabled": True,
        "env_ceiling": "auto",
        "policies": {policy.GLOBAL: policy.PolicySnapshot(policy.GLOBAL, mode="auto")},
        "chat": policy.ChatFacts(
            chat_key=KEY, channel="telegram", tg_chat_id=DM, chat_type="private"
        ),
        "trigger": gate.Trigger(id=9, occurred_at=NOW - timedelta(minutes=2)),
        "caps": gate.Caps(auto_cap=20, per_chat_cap=6, gap_seconds=20),
        "guard": PassGuard(),
    }
    values.update(kw)
    return gate.Context(**values)


PLAN_ORDER = (
    ("kill_switch", "kill switches (JARVIS_ENABLED"),
    ("pause", "pauses, breakers"),
    ("mode", "rollout mode: env ceiling JARVIS_MODE"),
    ("rules", "reply_rules + hard exclusions"),
    ("safety", "safety shield:"),
    ("chat_state", "chat state: owner replied/composing/read"),
    ("trigger", "trigger edited/deleted, expired, not_before"),
    ("caps", "caps (daily drafts/auto"),
    ("guard", "guard binding: body_sha256 unchanged"),
    ("payload", "payload shape: text body only"),
    ("quiet_hours", "quiet hours for AI auto rows"),
)


def test_gate_order_matches_plan():
    assert tuple(line for line, _ in PLAN_ORDER) == gate.LINES
    plan = (ROOT / "docs" / "jarvis-plan.md").read_text(encoding="utf-8")
    section = plan[plan.index("### 3.2 The outbound chokepoint") : plan.index("### 3.3")]
    positions = [section.index(phrase) for _, phrase in PLAN_ORDER]
    assert positions == sorted(positions)

    # A row that fails every line: each fix uncovers exactly the next line.
    sha_other = gate.body_hash("other")
    row = _gate_row(
        body="a\x07",
        guard={"verdict": "pass", "body_sha256": sha_other},
        expires_at=NOW - timedelta(seconds=1),
    )
    ctx = _gate_ctx(
        jarvis_enabled=False,
        policies={
            policy.GLOBAL: policy.PolicySnapshot(
                policy.GLOBAL, mode="draft", pause_reason="owner"
            )
        },
        rules=(policy.RuleSnapshot(1, "person", "never", "personal", person_id=1),),
        unsafe_inbound_ids=(10,),
        chat_state=gate.ChatState(owner_seen_at=NOW),
        caps=gate.Caps(auto_sent=99, auto_cap=1, per_chat_cap=6),
        guard=PassGuard("block"),
        quiet_until=NOW + timedelta(hours=7),
    )
    fixes = [
        ("kill_switch", lambda r, c: (r, replace(c, jarvis_enabled=True))),
        (
            "pause",
            lambda r, c: (
                r,
                replace(
                    c, policies={policy.GLOBAL: policy.PolicySnapshot("global", "draft")}
                ),
            ),
        ),
        (
            "mode",
            lambda r, c: (
                r,
                replace(
                    c, policies={policy.GLOBAL: policy.PolicySnapshot("global", "auto")}
                ),
            ),
        ),
        ("rules", lambda r, c: (r, replace(c, rules=()))),
        ("safety", lambda r, c: (r, replace(c, unsafe_inbound_ids=()))),
        ("chat_state", lambda r, c: (r, replace(c, chat_state=None))),
        ("trigger", lambda r, c: (replace(r, expires_at=None), c)),
        (
            "caps",
            lambda r, c: (r, replace(c, caps=gate.Caps(auto_cap=20, per_chat_cap=6))),
        ),
        ("guard", lambda r, c: (r, replace(c, guard=PassGuard()))),
        (
            "payload",
            lambda r, c: (
                replace(r, body="Salom", body_sha256=gate.body_hash("Salom")),
                c,
            ),
        ),
        ("quiet_hours", lambda r, c: (r, replace(c, quiet_until=None))),
    ]
    seen = []
    for line, fix in fixes:
        decision = gate.evaluate(row, ctx)
        assert decision.line == line, (line, decision)
        seen.append(decision.line)
        row, ctx = fix(row, ctx)
    assert seen == list(gate.LINES)
    final = gate.evaluate(row, ctx)
    assert final.sends and final.guard is not None  # re-guarded: the new body is bound


def test_gate_is_pure():
    tree = ast.parse((ROOT / "miya" / "jarvis" / "gate.py").read_text(encoding="utf-8"))
    imported = {
        alias.name if isinstance(node, ast.Import) else node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in (node.names if isinstance(node, ast.Import) else [node])
    }
    assert not any("sqlalchemy" in (name or "") for name in imported)
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]


# --- only outbox.py writes ---------------------------------------------------------

OUTBOX_MODULE = ROOT / "miya" / "jarvis" / "outbox.py"
RAW_WRITE = re.compile(r"\b(insert\s+into|update)\s+outbox_messages\b", re.IGNORECASE)


def _model_name(node: ast.AST | None) -> str | None:
    """``OutboxMessage``, ``m.OutboxMessage`` or their ``.__table__``: the
    name of the model the expression stands for."""
    if isinstance(node, ast.Attribute) and node.attr == "__table__":
        node = node.value
    return getattr(node, "id", None) or getattr(node, "attr", None)


def _outbox_writes(tree: ast.AST) -> list[int]:
    """Lines that write ``outbox_messages`` outside the chokepoint: an ORM
    insert/update of OutboxMessage under any alias (``pg_insert``,
    ``sa.update``, ``OutboxMessage.__table__.insert()``), a new
    OutboxMessage(...), a ``.state`` assignment in a module that knows the
    model, or raw SQL text."""
    lines: list[int] = []
    names_model = any(
        isinstance(node, ast.Name | ast.Attribute)
        and (getattr(node, "id", None) or getattr(node, "attr", None)) == "OutboxMessage"
        for node in ast.walk(tree)
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            )
            writes = name.casefold().endswith(("insert", "update"))
            first = node.args[0] if node.args else None
            if writes and _model_name(first) == "OutboxMessage":
                lines.append(node.lineno)
            # OutboxMessage.__table__.insert() / .update()
            if (
                writes
                and isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "__table__"
                and _model_name(func.value) == "OutboxMessage"
            ):
                lines.append(node.lineno)
            if name == "OutboxMessage":
                lines.append(node.lineno)
        if isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if names_model and any(
                isinstance(t, ast.Attribute) and t.attr == "state" for t in targets
            ):
                lines.append(node.lineno)
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and RAW_WRITE.search(node.value)
        ):
            lines.append(node.lineno)
    return sorted(set(lines))


def test_only_outbox_writes_state():
    offenders = []
    for path in sorted((ROOT / "miya").rglob("*.py")):
        if path == OUTBOX_MODULE or "__pycache__" in path.parts:
            continue
        for line in _outbox_writes(ast.parse(path.read_text(encoding="utf-8"))):
            offenders.append(f"{path.relative_to(ROOT)}:{line}")
    assert offenders == []
    # The chokepoint itself does write.
    assert _outbox_writes(ast.parse(OUTBOX_MODULE.read_text(encoding="utf-8")))


def test_the_write_detector_catches_each_shape():
    source = (
        "a = sa.update(OutboxMessage).values(state='sent')\n"
        "b = insert(OutboxMessage)\n"
        "c = OutboxMessage(body='x')\n"
        "row.state = 'sent'\n"
        "d = sa.text('UPDATE outbox_messages SET state = 1')\n"
        "e = 'insert into outbox_messages values (1)'\n"
        "f = sa.delete(OutboxMessage)\n"
        "g = 'SELECT 1 FROM outbox_messages'\n"
        "h = pg_insert(OutboxMessage).values(body='x')\n"
        "i = OutboxMessage.__table__.update().values(state='sent')\n"
        "j = sa.insert(m.OutboxMessage.__table__)\n"
        "k = f'UPDATE outbox_messages SET state = {x}'\n"
        "n = sa.update(m.Interaction).values(state='x')\n"
    )
    assert _outbox_writes(ast.parse(source)) == [1, 2, 3, 4, 5, 6, 9, 10, 11, 12]
    # A .state assignment in a module that never names the model is another table's.
    assert _outbox_writes(ast.parse("receipt.state = 'read'\n")) == []


def test_merge_moves_only_the_person_link():
    jarvis_columns = [
        (key, column.key)
        for key, model, column in people._fk_columns()
        if model in (m.OutboxMessage, m.Escalation, m.JarvisChatState, m.ReplyRule)
    ]
    assert jarvis_columns == [
        ("outbox", "person_id"),
        ("escalations", "person_id"),
        ("chat_state", "person_id"),
        ("reply_rules", "person_id"),
    ]


# --- merge and purge ---------------------------------------------------------------


async def _jarvis_rows(session, person, trigger):
    row = await _approved(session, await _ai_row(session, person, trigger))
    escalation = m.Escalation(
        interaction_id=trigger.id,
        person_id=person.id,
        channel="telegram",
        chat_key=KEY,
        reasons=["price"],
        summary="Narx so'rayapti",
    )
    session.add_all(
        [
            escalation,
            m.JarvisChatState(chat_key=KEY, channel="telegram", person_id=person.id),
            m.ReplyRule(
                subject="person",
                person_id=person.id,
                address=f"tg:{DM}",
                mode="never",
                reason="personal",
                set_by="command",
                note="Akmal — oila",
            ),
        ]
    )
    await session.flush()
    return row, escalation


async def test_purge_and_merge_cover_jarvis_tables(session, jarvis):
    for model in (
        "OutboxMessage",
        "Escalation",
        "JarvisChatState",
        "JarvisPolicy",
        "ReplyRule",
    ):
        assert model in m.__all__
        assert f"m.{model}," in (ROOT / "tests" / "conftest.py").read_text(
            encoding="utf-8"
        )

    person, trigger = await _setup(session)
    row, escalation = await _jarvis_rows(session, person, trigger)
    twin = await _person(session, "Акмал", telegram_id=None)
    session.add(
        m.ReplyRule(
            subject="person",
            person_id=twin.id,
            mode="draft",
            reason="owner",
            set_by="command",
        )
    )
    await session.flush()

    # Merge: the person link moves, the stricter rule stays active.
    plan = await people.merge_plan(session, twin, person)
    assert plan["reply_rules"] == 1
    merged = await people.merge_into(session, twin, person, by="command")
    rules = list(
        await session.scalars(
            sa.select(m.ReplyRule)
            .where(m.ReplyRule.person_id == merged.id)
            .execution_options(populate_existing=True)
        )
    )
    assert sorted((r.mode, r.state) for r in rules) == [
        ("draft", "revoked"),
        ("never", "active"),
    ]
    other = await _person(session, "Bobur", telegram_id=None)
    session.add(
        m.Escalation(
            interaction_id=trigger.id,
            person_id=other.id,
            channel="telegram",
            chat_key="tg:8801",
            reasons=["deadline"],
        )
    )
    await session.flush()
    plan = await people.merge_plan(session, other, merged)
    assert plan["escalations"] == 1
    await people.merge_into(session, other, merged, by="command")
    moved = await session.scalar(
        sa.select(sa.func.count())
        .select_from(m.Escalation)
        .where(m.Escalation.person_id == merged.id)
    )
    assert moved == 2

    # Purge: drafts, escalations and chat state go; the address rule stays.
    person_plan = await purge.plan_person(session, merged)
    assert row.id in person_plan.outbox_ids and person_plan.counts["outbox"] >= 1
    assert person_plan.counts["escalations"] == 2
    assert "Jarvis xabari" in replies.purge_preview(person_plan)
    await purge.execute(session, person_plan)
    assert (
        await session.scalar(sa.select(sa.func.count()).select_from(m.OutboxMessage)) == 0
    )
    assert await session.scalar(sa.select(sa.func.count()).select_from(m.Escalation)) == 0
    assert await session.get(m.JarvisChatState, KEY) is None
    kept = list(
        await session.scalars(
            sa.select(m.ReplyRule).execution_options(populate_existing=True)
        )
    )
    # The active never rule kept its address snapshot; only the person-only
    # (revoked) rule went. Nothing reopens the personal chat.
    assert [(r.address, r.person_id, r.note, r.mode) for r in kept] == [
        (f"tg:{DM}", None, None, "never")
    ]


async def test_chat_and_range_purges_take_the_jarvis_rows(session, jarvis):
    person, trigger = await _setup(session)
    row, _ = await _jarvis_rows(session, person, trigger)
    chat_plan = await purge.plan_chat(session, DM)
    assert chat_plan.outbox_ids == [row.id] and chat_plan.chat_state_keys == [KEY]
    await purge.execute(session, chat_plan)
    assert (
        await session.scalar(sa.select(sa.func.count()).select_from(m.OutboxMessage)) == 0
    )
    # The owner's chat rule survives a chat purge.
    assert await session.scalar(sa.select(sa.func.count()).select_from(m.ReplyRule)) == 1

    trigger = await _inbound(session, person)
    command = await _owner_row(session, person, body="Ertaga gaplashamiz")
    range_plan = await purge.plan_range(session, NOW.date(), NOW.date())
    assert command.id in range_plan.outbox_ids
    await purge.execute(session, range_plan)
    assert await session.get(m.OutboxMessage, command.id) is None


async def test_a_range_with_only_jarvis_rows_can_be_purged(session, jarvis):
    person = await _person(session)
    await _chat(session)
    await policy.set_mode(
        session, scope=policy.GLOBAL, mode="draft", by="command", now=NOW
    )
    # Yesterday's message, today's draft: today holds nothing but the draft.
    trigger = await _inbound(session, person, at=day_start(NOW) - timedelta(hours=1))
    row = await _ai_row(session, person, trigger)
    plan = await purge.plan_range(session, NOW.date(), NOW.date())
    assert plan.interaction_ids == [] and plan.outbox_ids == [row.id]
    assert not plan.is_empty()
    assert "Jarvis xabari" in replies.purge_preview(plan)
    await purge.execute(session, plan)
    assert await session.get(m.OutboxMessage, row.id) is None
    assert await session.get(m.Interaction, trigger.id) is not None


async def test_merge_keeps_a_rule_that_names_its_own_chat(session, jarvis):
    akmal = await _person(session)
    twin = await _person(session, "Akmal WA", telegram_id=None)
    address = "wa:998901234567@s.whatsapp.net"
    session.add_all(
        [
            m.ReplyRule(
                subject="chat",
                person_id=akmal.id,
                chat_key=KEY,
                mode="never",
                reason="personal",
                set_by="command",
            ),
            m.ReplyRule(
                subject="address",
                person_id=twin.id,
                address=address,
                mode="never",
                reason="personal",
                set_by="command",
            ),
        ]
    )
    await session.flush()
    merged = await people.merge_into(session, twin, akmal, by="command")

    async def rules():
        return sorted(
            await session.scalars(
                sa.select(m.ReplyRule).execution_options(populate_existing=True)
            ),
            key=lambda r: r.subject,
        )

    address_rule, chat_rule = await rules()
    # One active rule per person; the other keeps guarding its own address.
    assert (chat_rule.state, chat_rule.person_id) == ("active", merged.id)
    assert (address_rule.state, address_rule.person_id) == ("active", None)
    assert address_rule.history[-1]["field"] == "person_id"
    assert address_rule.history[-1]["code"] == "merged"

    # Forgetting the merged person reopens neither chat.
    await purge.execute(session, await purge.plan_person(session, merged))
    assert [(r.chat_key, r.address, r.state, r.person_id) for r in await rules()] == [
        (None, address, "active", None),
        (KEY, None, "active", None),
    ]


# --- the question budget -------------------------------------------------------------


async def test_never_writes_question_log(session, jarvis, monkeypatch):
    person, trigger = await _setup(session, mode="auto")
    row = await _ai_row(session, person, trigger)
    draft = await _owner_row(session, person)
    await _approved(session, draft)
    claimed = await outbox.claim_ready(session, "telegram", now=NOW, limit=5)
    for item in claimed:
        await outbox.complete(
            session, item.id, external_id=f"{DM}:{item.id}", sent_at=NOW
        )
    await status.gather(session, NOW)
    await status.recent_actions(session)
    assert row.id in {c.id for c in claimed}
    assert (
        await session.scalar(sa.select(sa.func.count()).select_from(m.QuestionLog)) == 0
    )
    # Statically: no Jarvis code names the log, the budget or its module.
    for path in sorted((ROOT / "miya" / "jarvis").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        used = {
            getattr(node, "id", None) or getattr(node, "attr", None)
            for node in ast.walk(tree)
            if isinstance(node, ast.Name | ast.Attribute)
        }
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import | ast.ImportFrom)
            for alias in node.names
        }
        assert not used & {"QuestionLog", "question_budget_per_day"}, path.name
        assert "questions" not in imported and "QuestionLog" not in imported, path.name
        sql = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and re.search(r"(insert\s+into|update)\s+question_log", node.value, re.I)
        ]
        assert sql == [], path.name


# --- rule 20: off means off ------------------------------------------------------------


def test_jarvis_off_registers_nothing_at_import():
    code = (
        "import sys\n"
        "import miya.bot.main, miya.bot.handlers, miya.worker.main, miya.userbot.main\n"
        "import miya.api.main, miya.services.people, miya.services.purge\n"
        "loaded = sorted(n for n in sys.modules if n.startswith('miya.jarvis'))\n"
        "print(loaded)\n"
        "assert loaded == [], loaded\n"
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT), "JARVIS_ENABLED": "false"}
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def test_jarvis_off_registers_nothing(monkeypatch):
    from miya.worker import main as worker

    async def jobs(enabled: bool) -> list[str]:
        registered: list[str] = []

        class _Scheduler:
            def __init__(self, *args, **kwargs):
                pass

            def add_job(self, func, trigger, *, id, **kwargs):
                registered.append(id)

            def add_listener(self, *args):
                pass

            def start(self):
                pass

            def get_jobs(self):
                return registered

            def shutdown(self, wait=False):
                pass

        class _Stop:
            def set(self):
                pass

            async def wait(self):
                return None

        class _Bot:
            def __init__(self, **kwargs):
                self.session = SimpleNamespace(close=self._close)

            async def _close(self):
                return None

        class _Engine:
            async def dispose(self):
                return None

        async def _quiet(*args, **kwargs):
            return None

        monkeypatch.setattr(settings, "jarvis_enabled", enabled)
        monkeypatch.setattr(settings, "assistant_bot_token", "123:abc")
        monkeypatch.setattr(settings, "owner_telegram_id", 1)
        monkeypatch.setattr(worker, "AsyncIOScheduler", _Scheduler)
        monkeypatch.setattr(worker, "Bot", _Bot)
        monkeypatch.setattr(worker.asyncio, "Event", _Stop)
        monkeypatch.setattr(worker, "catch_up", _quiet)
        monkeypatch.setattr(worker, "heartbeat_job", _quiet)
        monkeypatch.setattr(worker, "_prune_job_heartbeats", _quiet)
        monkeypatch.setattr(worker, "engine", _Engine())
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler", lambda *a, **k: None)
        await worker.run()
        return registered

    off = await jobs(False)
    assert off and not [job for job in off if job.startswith("jarvis")]
    on = await jobs(True)
    assert set(on) - set(off) == {"jarvis_outbox"}
    # The userbot has no Telegram write path yet, and never imports miya.jarvis.
    tree = ast.parse(Path(userbot.__file__).read_text(encoding="utf-8"))
    imports = [
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    ] + [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    assert not [name for name in imports if name.startswith("miya.jarvis")]


async def test_the_outbox_job_reconciles_and_tells_the_owner(
    session, jarvis, monkeypatch
):
    from miya.worker import main as worker

    person, trigger = await _setup(session)
    row = await _approved(session, await _ai_row(session, person, trigger))
    [claimed] = await outbox.claim_ready(session, "telegram", now=NOW)
    stale = await _ai_row(session, person, trigger, body="Eski qoralama")
    await session.commit()

    told: list[str] = []

    async def _notify(bot, text, **kwargs):
        told.append(text)
        return True

    monkeypatch.setattr(worker, "notify", _notify)
    later = claimed.lease_until + timedelta(hours=13)
    await worker.jarvis_outbox_job(None, now=later)
    assert told == [replies.JARVIS_SEND_UNKNOWN.format(id=row.id)]
    assert (await _reload(session, row.id)).state == "unknown"
    assert (await _reload(session, stale.id)).state == "expired"
    # Told once, never again.
    await worker.jarvis_outbox_job(None, now=later + timedelta(minutes=1))
    assert len(told) == 1


# --- /jarvis -------------------------------------------------------------------------


class _Message:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def answer(self, text, **kwargs):
        self.sent.append(text)


@pytest.fixture
def bound(session, monkeypatch):
    @asynccontextmanager
    async def _scope():
        yield session
        await session.flush()

    monkeypatch.setattr(handlers, "session_scope", _scope)
    return session


async def _jarvis_command(args: str | None) -> str:
    message = _Message()
    await handlers.cmd_jarvis(
        message, CommandObject(prefix="/", command="jarvis", args=args)
    )
    [text] = message.sent
    return text


async def test_jarvis_commands(bound, jarvis, monkeypatch):
    monkeypatch.setattr(settings, "jarvis_mode", "draft")
    person, trigger = await _setup(bound, mode="draft")
    draft = await _ai_row(bound, person, trigger)

    text = await _jarvis_command(None)
    assert text == (
        "🤖 Jarvis: qoralama (har javobni siz tasdiqlaysiz)\n"
        "Bugun: 1 ta qoralama · 0 ta yuborildi · 0 ta eskalatsiya\n"
        f"Limit: 1/{settings.jarvis_draft_daily_cap}\n"
        "Kanallar: Telegram ishlayapti · WhatsApp ishlayapti · Gmail ishlayapti"
    )
    assert await _jarvis_command("toxtat") == (
        "⛔ Jarvis to'xtatildi. Hech qanday xabar yuborilmaydi. Qayta yoqish: /jarvis yoq"
    )
    paused = await _jarvis_command(None)
    assert paused.endswith(replies.JARVIS_PAUSED)
    # A channel resume does not lift the global pause: the owner sees the
    # status, which says so, never «yoqildi».
    still = await _jarvis_command("yoq telegram")
    assert still.startswith("🤖 Jarvis: ") and still.endswith(replies.JARVIS_PAUSED)
    assert await _jarvis_command("yoq") == "✅ Jarvis yoqildi (qoralama rejimi)."
    assert not (await _jarvis_command(None)).endswith(replies.JARVIS_PAUSED)
    assert await _jarvis_command("yoq faks") == replies.JARVIS_UNKNOWN_CHANNEL
    assert await _jarvis_command("nimadir") == replies.JARVIS_USAGE

    await policy.trip_breaker(
        bound,
        channel="telegram",
        reason="peer_flood",
        until=datetime.now(TZ) + timedelta(hours=1),
        by="gate",
        now=NOW,
    )
    assert "Telegram cheklangan" in await _jarvis_command(None)
    assert await _jarvis_command("yoq telegram") == "✅ Jarvis yoqildi (qoralama rejimi)."
    assert "Telegram ishlayapti" in await _jarvis_command(None)

    history = await _jarvis_command("tarix")
    assert history.startswith("🧾 Jarvis tarixi (oxirgi ")
    # In the owner's words: fields, states and who acted.
    assert f"j{draft.id} · holat: — → qoralama (avtomatik)" in history
    assert "Jarvis · to'xtatish: — → siz (buyruq)" in history
    assert "Telegram · cheklov: peer_flood → — (buyruq)" in history
    for internal in ("state:", "pause:", "breaker:", "(button)", "(command)"):
        assert internal not in history, internal
    assert await session_count(bound, m.QuestionLog) == 0

    # Jarvis switched off: «yoq» cannot turn it on, and says so.
    monkeypatch.setattr(settings, "jarvis_enabled", False)
    off = await _jarvis_command("yoq")
    assert off.startswith("🤖 Jarvis: o'chiq\n") and "yoqildi" not in off


async def session_count(session, model) -> int:
    return int(await session.scalar(sa.select(sa.func.count()).select_from(model)) or 0)


async def test_jarvis_command_when_the_database_fails(monkeypatch):
    @asynccontextmanager
    async def _broken():
        raise sa.exc.OperationalError("SELECT 1", {}, Exception("down"))
        yield  # pragma: no cover - the generator form needs it

    monkeypatch.setattr(handlers, "session_scope", _broken)
    assert await _jarvis_command("toxtat") == replies.JARVIS_FAILED


def test_the_mode_labels_and_strings():
    assert replies.JARVIS_MODE_LABELS == {
        "off": "o'chiq",
        "shadow": "kuzatuv (faqat yozib boradi)",
        "draft": "qoralama (har javobni siz tasdiqlaysiz)",
        "auto": "avtomatik",
    }
    assert replies.jarvis_resumed("auto") == "✅ Jarvis yoqildi (avtomatik rejimi)."
    assert replies.jarvis_resumed("shadow") == "✅ Jarvis yoqildi (kuzatuv rejimi)."
    off = status.Status(
        mode="off",
        paused=False,
        counts=caps.DayCounts(0, 0, 0, 0, 0),
        limit_used=0,
        limit_cap=40,
        channels=(
            ("telegram", "not_connected"),
            ("whatsapp", "paused"),
            ("gmail", "limited"),
        ),
    )
    assert replies.jarvis_status(off) == (
        "🤖 Jarvis: o'chiq\nBugun: 0 ta qoralama · 0 ta yuborildi · 0 ta eskalatsiya\n"
        "Limit: 0/40\n"
        "Kanallar: Telegram ulanmagan · WhatsApp to'xtatilgan · Gmail cheklangan"
    )
    assert replies.jarvis_history([]) == replies.JARVIS_HISTORY_EMPTY
    at = NOW.replace(hour=14, minute=5)
    actions = [
        status.Action(at, "outbox", "12", "state", "draft", "approved", "button"),
        status.Action(at, "escalation", "3", "state", "open", "snoozed", "command"),
        status.Action(at, "rule", "5", "state", "active", "revoked", "command"),
        status.Action(at, "policy", "global", "mode", "shadow", "draft", "command"),
        status.Action(at, "policy", "channel:gmail", "hold", None, "x", "gate"),
        status.Action(at, "outbox", "7", "state", "sending", "unknown", "reconcile", "c"),
    ]
    lines = replies.jarvis_history(actions).splitlines()[1:]
    assert [line.split(" · ", 1)[1] for line in lines] == [
        "j12 · holat: qoralama → tasdiqlandi (siz)",
        "e3 · holat: ochiq → keyinga qoldirildi (buyruq)",
        "qoida 5 · holat: faol → bekor qilindi (buyruq)",
        "Jarvis · rejim: kuzatuv → qoralama (buyruq)",
        "Gmail · kutish: — → x (tekshiruv)",
        "j7 · holat: yuborilmoqda → aniq emas [c] (qayta tekshiruv)",
    ]


# --- schema and configuration --------------------------------------------------------


def _migration():
    path = ROOT / "alembic" / "versions" / "0025_jarvis_core.py"
    spec = importlib.util.spec_from_file_location("migration_0025", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_freezes_the_model_lists():
    migration = _migration()
    assert migration.revision == "0025_jarvis_core" and len(migration.revision) <= 32
    assert migration.down_revision == "0024_sheet_rows"
    assert migration.CHANNELS == m.JARVIS_CHANNELS
    assert migration.MODES == m.JARVIS_MODES
    assert migration.SCOPE_PATTERN == m.JARVIS_SCOPE_PATTERN
    assert migration.RULE_SUBJECTS == m.REPLY_RULE_SUBJECTS
    assert migration.RULE_MODES == m.REPLY_RULE_MODES
    assert migration.RULE_REASONS == m.REPLY_RULE_REASONS
    assert migration.RULE_STATES == m.REPLY_RULE_STATES
    assert migration.ESCALATION_REASONS == m.ESCALATION_REASONS
    assert migration.ESCALATION_STATES == m.ESCALATION_STATES
    assert migration.ESCALATION_OPEN_STATES == m.ESCALATION_OPEN_STATES
    assert migration.ESCALATION_RESOLUTIONS == m.ESCALATION_RESOLUTIONS
    assert migration.ORIGINS == m.OUTBOX_ORIGINS == outbox.ORIGINS
    assert migration.AUTHORS == m.OUTBOX_AUTHORS
    assert migration.OUTBOX_STATES == m.OUTBOX_STATES
    assert migration.APPROVERS == m.OUTBOX_APPROVERS
    assert tuple(policy.MODES) == m.JARVIS_MODES
    assert chatkey.CHANNELS == m.JARVIS_CHANNELS
    # Migrations never import app code.
    source = (ROOT / "alembic" / "versions" / "0025_jarvis_core.py").read_text()
    assert "import miya" not in source and "from miya" not in source


async def test_the_seed_and_checks_hold_in_the_database(session):
    if not await database_available():
        pytest.skip("no database")
    for bad in (
        m.JarvisPolicy(scope="everything", mode="draft"),
        m.JarvisPolicy(scope="global", mode="loud"),
        m.ReplyRule(subject="person", mode="never", reason="personal"),
        m.Escalation(interaction_id=None, channel="telegram", chat_key=KEY),
    ):
        session.add(bad)
        with pytest.raises(sa.exc.IntegrityError):
            await session.flush()
        await session.rollback()
    person = await _person(session)
    trigger = await _inbound(session, person)
    session.add(
        m.Escalation(
            interaction_id=trigger.id, channel="telegram", chat_key=KEY, reasons=["x"]
        )
    )
    with pytest.raises(sa.exc.IntegrityError):
        await session.flush()
    await session.rollback()
    person = await _person(session)
    trigger = await _inbound(session, person)
    for _ in range(2):
        session.add(
            m.Escalation(
                interaction_id=trigger.id,
                channel="telegram",
                chat_key=KEY,
                reasons=["price"],
            )
        )
    with pytest.raises(sa.exc.IntegrityError) as caught:
        await session.flush()
    assert "ux_escalations_open_chat" in str(caught.value)
    await session.rollback()


def _alembic(url: str, *args: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(ROOT)},
        check=True,
        capture_output=True,
        timeout=300,
    )


def test_the_round_trip_seeds_the_careful_start():
    url = os.environ.get("MIYA_MIGRATION_DATABASE_URL")
    if not url:
        pytest.skip("MIYA_MIGRATION_DATABASE_URL is not set")
    _alembic(url, "upgrade", "head")
    _alembic(url, "downgrade", "0024_sheet_rows")
    _alembic(url, "upgrade", "head")
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            seeded = conn.execute(
                sa.text("SELECT scope, mode FROM jarvis_policy ORDER BY scope")
            ).all()
    finally:
        engine.dispose()
    assert [tuple(r) for r in seeded] == [("global", "shadow")]


KEYS = {
    "JARVIS_ENABLED": "false",
    "JARVIS_MODE": "draft",
    "JARVIS_DRAFT_DAILY_CAP": "40",
    "JARVIS_AUTO_DAILY_CAP": "20",
    "JARVIS_PER_CHAT_DAILY_CAP": "6",
    "JARVIS_SEND_MIN_GAP_SECONDS": "20",
    "JARVIS_DRAFT_EXPIRY_HOURS": "12",
    "JARVIS_DAILY_COST_CAP_USD": "3.00",
}


def test_the_config_keys_are_in_all_four_places():
    env = dotenv_values(ROOT / ".env.example")
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for key, default in KEYS.items():
        assert env[key] == default, key
        # A comment line sits right above each key.
        above = text.splitlines()[text.splitlines().index(f"{key}={default}") - 1]
        assert above.startswith("#"), key
        assert f"`{key}`" in readme, key
        assert key in doctor.EXPECTED_ENV_KEYS, key
    fresh = Settings(_env_file=None)
    assert fresh.jarvis_enabled is False and fresh.jarvis_mode == "draft"
    assert (
        fresh.jarvis_draft_daily_cap,
        fresh.jarvis_auto_daily_cap,
        fresh.jarvis_per_chat_daily_cap,
        fresh.jarvis_send_min_gap_seconds,
        fresh.jarvis_draft_expiry_hours,
    ) == (40, 20, 6, 20, 12)
    assert fresh.jarvis_daily_cost_cap_usd == Decimal("3.00")
    assert isinstance(fresh.jarvis_daily_cost_cap_usd, Decimal)
    with pytest.raises(ValueError):
        Settings(_env_file=None, jarvis_mode="loud")
    with pytest.raises(ValueError):
        Settings(_env_file=None, jarvis_daily_cost_cap_usd="-1")

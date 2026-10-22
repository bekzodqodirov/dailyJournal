"""JV-19: the Telegram sender — the narrow exception to the userbot's
read-only rule, with human pacing.

Telethon is never connected: a fake client records every call in order and
answers the way Telethon does. The outbox, the gate and the echo run against
real PostgreSQL; rows reach the sender only through ``outbox.claim_ready``.
"""

from __future__ import annotations

import asyncio
import os
import random
import signal
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from dotenv import dotenv_values
from telethon import errors
from telethon.tl import functions, types
from telethon.tl.types import User

from miya.bot import replies
from miya.config import Settings, settings
from miya.db import models as m
from miya.db.enums import ChatType, Direction, InteractionSource
from miya.db.session import SessionLocal
from miya.jarvis import caps, chatkey, gate, outbox, policy
from miya.services import authorship
from miya.tools import backfill, doctor
from miya.userbot import main as userbot
from miya.userbot import sender as tg_sender
from tests.test_outbox import ROOT, PassGuard

TZ = settings.tz
NOW = datetime.now(TZ).replace(hour=12, minute=0, second=0, microsecond=0)
DM = 7191
GROUP = -7192
KEY = chatkey.telegram(DM)
OWNER_ID = 990019
ACCOUNT = f"tg:{OWNER_ID}"
TRIGGER_MESSAGE = 4100
# Telethon retries a request this many more times after an internal Telegram
# error, then (raise_last_call_error=True, as the userbot builds its client)
# raises that last error.
TELETHON_RETRIES = 5


@pytest.fixture
def jarvis(monkeypatch):
    """Jarvis on, Telegram sending on, a passing guard, no anti-ban gap."""
    monkeypatch.setattr(settings, "jarvis_enabled", True)
    monkeypatch.setattr(settings, "jarvis_mode", "draft")
    monkeypatch.setattr(settings, "telegram_send_enabled", True)
    monkeypatch.setattr(settings, "owner_telegram_id", OWNER_ID)
    monkeypatch.setattr(settings, "jarvis_send_min_gap_seconds", 0)
    previous = gate.registered_guard()
    guard = PassGuard()
    gate.register_guard(guard)
    yield guard
    gate.register_guard(previous)


class FakeClient:
    """The Telethon calls the sender and the reader make, recorded in order."""

    def __init__(
        self,
        *,
        send_errors=(),
        missing=(),
        entity_errors=None,
        history=(),
        dialogs=(),
        dialog_errors=(),
    ) -> None:
        self.events: list[tuple] = []
        self.requests: list[functions.messages.SendMessageRequest] = []
        self.send_errors = list(send_errors)
        self.missing = set(missing)
        self.entity_errors = dict(entity_errors or {})
        self.history = list(history)
        self.dialogs = list(dialogs)
        self.dialog_errors = list(dialog_errors)
        self.sent: list[int] = []
        self.next_id = 5000

    async def get_input_entity(self, chat_id):
        self.events.append(("entity", chat_id))
        if chat_id in self.entity_errors:
            raise self.entity_errors[chat_id]
        if chat_id in self.missing:
            raise ValueError("not in the session cache")
        if chat_id > 0:
            return types.InputPeerUser(user_id=chat_id, access_hash=1)
        return types.InputPeerChat(chat_id=-chat_id)

    async def iter_dialogs(self):
        self.events.append(("dialogs",))
        if self.dialog_errors:
            raise self.dialog_errors.pop(0)
        for dialog in self.dialogs:
            yield dialog

    async def send_read_acknowledge(self, peer, max_id=None):
        self.events.append(("read", max_id))

    def action(self, peer, kind):
        client = self

        class _Action:
            async def __aenter__(self):
                client.events.append(("typing", kind))

            async def __aexit__(self, *exc):
                client.events.append(("typing_done", kind))
                return False

        return _Action()

    async def __call__(self, request):
        self.requests.append(request)
        self.events.append(("send", request.random_id))
        if self.send_errors:
            error = self.send_errors.pop(0)
            if isinstance(error, errors.ServerError | errors.TimedOutError):
                # Telethon's own retries of the same request, then the last
                # error (UserMethods._call with raise_last_call_error).
                self.events.extend(
                    [("telethon_retry", request.random_id)] * TELETHON_RETRIES
                )
            if error is not None:
                raise error
        self.next_id += 1
        self.sent.append(self.next_id)
        return types.UpdateShortSentMessage(
            out=True, id=self.next_id, pts=1, pts_count=1, date=NOW
        )

    async def get_messages(self, peer, limit=None):
        self.events.append(("history", limit))
        return list(self.history)

    def is_connected(self):
        return True


def _sleeper(events: list[tuple]):
    async def _sleep(seconds):
        events.append(("sleep", seconds))

    return _sleep


def _sender(client, *, clock=None) -> tg_sender.Sender:
    return tg_sender.Sender(
        client,
        account=ACCOUNT,
        status={},
        sleep=_sleeper(client.events),
        clock=clock or (lambda: NOW),
    )


async def _setup(session, *, chat_id=DM, chat_type=ChatType.private):
    person = m.Person(display_name="Akmal", aliases=[], telegram_id=chat_id)
    session.add(person)
    session.add(
        m.ChatMonitor(
            tg_chat_id=chat_id,
            chat_type=chat_type,
            title="Akmal",
            monitor_enabled=True,
            last_seen_message_id=TRIGGER_MESSAGE,
        )
    )
    await session.flush()
    trigger = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.in_,
        person_id=person.id,
        tg_chat_id=chat_id,
        occurred_at=NOW - timedelta(minutes=5),
        raw_text="Assalomu alaykum, yuk bormi?",
        meta={"tg_message_id": TRIGGER_MESSAGE},
    )
    session.add(trigger)
    await session.flush()
    await policy.set_mode(
        session, scope=policy.GLOBAL, mode="draft", by="command", now=NOW
    )
    return person, trigger


async def _approved_ai(
    session, person, trigger, *, body="Assalomu alaykum, aka!", chat_id=DM
):
    row = await outbox.create(
        session,
        channel="telegram",
        chat_key=chatkey.telegram(chat_id),
        origin="auto_reply",
        authored_by="ai",
        body=body,
        now=NOW,
        intent="greeting",
        person_id=person.id,
        trigger_interaction_id=trigger.id,
    )
    result = await outbox.approve(session, row.id, by="button", now=NOW)
    assert result.approved, result.code
    return result.row


async def _approved_owner(session, person, *, body):
    command = m.Interaction(
        source=InteractionSource.assistant_bot,
        direction=Direction.in_,
        occurred_at=NOW,
        raw_text="Akmalga yozib qo'y",
    )
    session.add(command)
    await session.flush()
    row = await outbox.create(
        session,
        channel="telegram",
        chat_key=KEY,
        origin="owner_command",
        authored_by="owner",
        body=body,
        now=NOW,
        person_id=person.id,
        command_interaction_id=command.id,
    )
    result = await outbox.approve(session, row.id, by="button", now=NOW)
    assert result.approved, result.code
    return result.row


async def _reload(row_id) -> m.OutboxMessage:
    async with SessionLocal() as s:
        return await s.get(m.OutboxMessage, row_id)


async def _echoes(chat_id=DM) -> list[m.Interaction]:
    async with SessionLocal() as s:
        return list(
            await s.scalars(
                sa.select(m.Interaction)
                .where(
                    m.Interaction.tg_chat_id == chat_id,
                    m.Interaction.direction == Direction.out,
                )
                .order_by(m.Interaction.id)
            )
        )


def _sends(client) -> list[tuple]:
    return [e for e in client.events if e[0] == "send"]


# --- pacing ---------------------------------------------------------------------------


async def test_pacing_plan_applied(session, jarvis):
    """Entity, the pre-send pause, the read receipt up to the message
    answered, "typing…" for the body's length, then the text: in that order."""
    person, trigger = await _setup(session)
    row = await _approved_ai(session, person, trigger)
    await session.commit()
    client = FakeClient()
    sender = _sender(client)
    assert await sender.poll_once() == 1

    kinds = [e[0] for e in client.events]
    assert kinds == ["entity", "sleep", "read", "typing", "sleep", "typing_done", "send"]
    pre_delay = client.events[1][1]
    assert settings.jarvis_reply_delay_min_seconds <= pre_delay
    assert pre_delay <= settings.jarvis_reply_delay_max_seconds
    assert client.events[2] == ("read", TRIGGER_MESSAGE)
    assert client.events[3] == ("typing", "typing")
    assert client.events[4][1] == pytest.approx(
        len(row.body) / settings.jarvis_typing_chars_per_second, abs=0.001
    )
    sent = await _reload(row.id)
    assert sent.state == "sent" and sent.read_ack_at is not None
    assert sent.external_message_id == authorship.telegram_external_id(DM, client.sent[0])
    assert sender.status["sender_sent_at"] == NOW.isoformat()


async def test_pacing_follows_the_settings_and_marks_read_only_when_replying(
    session, jarvis, monkeypatch
):
    monkeypatch.setattr(settings, "jarvis_reply_delay_min_seconds", 4)
    monkeypatch.setattr(settings, "jarvis_reply_delay_max_seconds", 4)
    monkeypatch.setattr(settings, "jarvis_typing_chars_per_second", 1)
    monkeypatch.setattr(settings, "jarvis_typing_max_seconds", 2)
    person, _trigger = await _setup(session)
    # An owner command answers no client message: nothing is marked read.
    await _approved_owner(session, person, body="Kechki 6 da gaplashamiz")
    await session.commit()
    client = FakeClient()
    assert await _sender(client).poll_once() == 1
    assert ("sleep", 4.0) in client.events
    assert not [e for e in client.events if e[0] == "read"]
    assert [e for e in client.events if e[0] == "sleep"] == [
        ("sleep", 4.0),
        ("sleep", 2.0),
    ]

    plan = outbox.pacing_for(
        "x" * 600, channel="telegram", has_trigger=True, rng=random.Random(1)
    )
    assert plan.typing_ms == 2000 and plan.mark_read
    # A lone bound set above the other default is the lower bound, never an error.
    monkeypatch.setattr(settings, "jarvis_reply_delay_min_seconds", 9)
    monkeypatch.setattr(settings, "jarvis_reply_delay_max_seconds", 7)
    plan = outbox.pacing_for(
        "x", channel="telegram", has_trigger=False, rng=random.Random(2)
    )
    assert 7 <= plan.pre_delay_seconds <= 9 and not plan.mark_read


async def test_min_gap_between_sends_per_account(session, jarvis, monkeypatch):
    monkeypatch.setattr(settings, "jarvis_send_min_gap_seconds", 20)
    person, trigger = await _setup(session)
    first = await _approved_ai(session, person, trigger, body="Salom!")
    second = await _approved_ai(session, person, trigger, body="Xush kelibsiz!")
    await session.commit()
    client = FakeClient()
    clock = {"now": NOW}
    sender = _sender(client, clock=lambda: clock["now"])
    assert await sender.poll_once() == 1
    clock["now"] = NOW + timedelta(seconds=5)
    assert await sender.poll_once() == 0  # the gap holds the second row
    assert (await _reload(second.id)).state == "approved"
    clock["now"] = NOW + timedelta(seconds=21)
    assert await sender.poll_once() == 1
    assert (await _reload(first.id)).claimed_by == ACCOUNT
    assert (await _reload(second.id)).state == "sent"


# --- exactly once ---------------------------------------------------------------------


def test_random_id_deterministic():
    one = tg_sender.random_id_for("abc")
    assert one == tg_sender.random_id_for("abc")
    assert one != tg_sender.random_id_for("abd")
    assert -(2**63) <= one < 2**63 and one != 0
    # Not the key's own digest: salted, so it never leaks the key.
    assert one != int.from_bytes(bytes.fromhex("ab" * 8), "big", signed=True)


async def test_random_id_survives_a_retry_and_a_duplicate_is_already_sent(
    session, jarvis
):
    person, trigger = await _setup(session)
    row = await _approved_ai(session, person, trigger)
    await session.commit()
    echo = SimpleNamespace(
        id=777, out=True, message=row.body, date=NOW + timedelta(seconds=15)
    )
    client = FakeClient(
        send_errors=[
            errors.FloodWaitError(request=None, capture=10),
            errors.RandomIdDuplicateError(request=None),
        ],
        history=[echo],
    )
    clock = {"now": NOW}
    sender = _sender(client, clock=lambda: clock["now"])
    assert await sender.poll_once() == 0
    sender.resume_at = 0.0
    clock["now"] = NOW + timedelta(seconds=11)
    assert await sender.poll_once() == 1
    ids = [request.random_id for request in client.requests]
    assert ids == [tg_sender.random_id_for(row.idempotency_key)] * 2
    # Telethon retried the duplicate itself before the error reached the sender.
    assert ("telethon_retry", ids[0]) in client.events
    sent = await _reload(row.id)
    assert sent.state == "sent" and sent.external_message_id == f"{DM}:777"
    assert ("history", tg_sender.RECONCILE_MESSAGES) in client.events


async def test_parse_mode_none(session, jarvis):
    """The text goes out exactly as written: no entities, no link preview,
    no media, no buttons, never a paid message (rule 27)."""
    person, _trigger = await _setup(session)
    body = "**qalin** <b>emas</b> _bu_ ham https://example.uz"
    await _approved_owner(session, person, body=body)
    await session.commit()
    client = FakeClient()
    assert await _sender(client).poll_once() == 1
    [request] = client.requests
    assert isinstance(request, functions.messages.SendMessageRequest)
    assert request.message == body
    assert request.entities is None
    assert request.no_webpage is True
    assert request.reply_markup is None
    assert request.allow_paid_stars is None
    # A private chat is answered unquoted; a group quotes the message.
    assert request.reply_to is None


def _claimed(**changes) -> outbox.ClaimedSend:
    fields = {
        "id": 1,
        "channel": "telegram",
        "chat_key": chatkey.telegram(GROUP),
        "tg_chat_id": GROUP,
        "to_address": None,
        "body": "Salom!",
        "reply_to_external_id": None,
        "trigger_interaction_id": 1,
        "idempotency_key": "k",
        "authored_by": "owner",
        "lease_until": NOW + caps.SEND_LEASE,
        "pacing": outbox.PacingPlan(0, 0, False),
        "trigger_message_id": TRIGGER_MESSAGE,
    }
    return outbox.ClaimedSend(**{**fields, **changes})


def test_a_group_reply_quotes_the_message():
    assert tg_sender.reply_to_of(_claimed()) == TRIGGER_MESSAGE
    assert tg_sender.reply_to_of(_claimed(reply_to_external_id=f"{GROUP}:42")) == 42
    private = _claimed(tg_chat_id=DM, chat_key=KEY)
    assert tg_sender.reply_to_of(private) is None
    assert _claimed().claimed_at == NOW


def test_the_message_id_is_read_from_telegrams_answer():
    rid = tg_sender.random_id_for("k")
    assert (
        tg_sender.sent_message_id(
            types.UpdateShortSentMessage(out=True, id=9, pts=1, pts_count=1, date=NOW),
            rid,
        )
        == 9
    )
    updates = types.Updates(
        updates=[
            types.UpdateMessageID(id=11, random_id=rid + 1),
            types.UpdateMessageID(id=12, random_id=rid),
        ],
        users=[],
        chats=[],
        date=NOW,
        seq=0,
    )
    assert tg_sender.sent_message_id(updates, rid) == 12
    assert tg_sender.sent_message_id(None, rid) is None


# --- the echo -------------------------------------------------------------------------


def _tg_out(message_id: int, text: str, *, at: datetime):
    peer = User(id=DM, first_name="Akmal", username="akmal_test")

    async def get_chat():
        return peer

    return SimpleNamespace(
        id=message_id,
        chat_id=DM,
        date=at,
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
        reply_to=None,
        entities=None,
        get_chat=get_chat,
        get_sender=get_chat,
    )


async def test_echo_tagged_and_deduped_with_catchup(session, jarvis):
    """The sender's message is stored once, tagged as MIYA's; the catch-up
    reading it back stores nothing new and never takes the chat over as
    the owner's own reply; the catch-up cursor moved past it."""
    person, trigger = await _setup(session)
    row = await _approved_ai(session, person, trigger)
    await session.commit()
    client = FakeClient()
    assert await _sender(client).poll_once() == 1
    [message_id] = client.sent
    [echo] = await _echoes()
    assert echo.meta["tg_message_id"] == message_id
    assert echo.meta["outbox_id"] == row.id and authorship.is_ai_reply(echo)
    async with SessionLocal() as s:
        monitor = await s.scalar(
            sa.select(m.ChatMonitor).where(m.ChatMonitor.tg_chat_id == DM)
        )
        assert monitor.last_seen_message_id == message_id

    class _History:
        def __init__(self, messages):
            self.messages = messages

        async def iter_messages(self, chat_id, **kwargs):
            for message in self.messages:
                yield message

    for at in (NOW, NOW + timedelta(seconds=1)):
        stored, top = await backfill.catch_up_chat(
            _History([_tg_out(message_id, row.body, at=at)]),
            DM,
            after_id=TRIGGER_MESSAGE,
            since=NOW - timedelta(days=1),
            limit=10,
        )
        assert (stored, top) == (0, message_id)
    [again] = await _echoes()
    assert again.id == echo.id and authorship.is_ai_reply(again)
    async with SessionLocal() as s:
        state = await s.get(m.JarvisChatState, KEY)
        assert state is None or state.ai_paused_until is None


async def test_crash_after_send_reconciles_unknown(session, jarvis, monkeypatch):
    """The message went out but the process could not record it: the row is
    left on the wire, its lease runs out, reconcile makes it unknown (or sent
    when the reader stored the echo), and nothing ever sends it again."""
    person, trigger = await _setup(session)
    lost = await _approved_ai(session, person, trigger, body="Salom!")
    await session.commit()
    real_complete = outbox.complete

    async def _crash(*args, **kwargs):
        raise ConnectionError("database gone")

    monkeypatch.setattr(outbox, "complete", _crash)
    client = FakeClient()
    sender = _sender(client)
    assert await sender.poll_once() == 0
    assert (await _reload(lost.id)).state == "sending"
    assert sender.status["sender_error"] == tg_sender.CODE_NOT_RECORDED
    monkeypatch.setattr(outbox, "complete", real_complete)

    later = NOW + caps.SEND_LEASE + timedelta(minutes=1)
    async with SessionLocal() as s:
        [done] = await outbox.reconcile(s, now=later)
        await s.commit()
    assert done.state == "unknown"
    async with SessionLocal() as s:
        assert await outbox.unknown_untold(s) == [lost.id]
    # Never claimed again, however long the sender polls.
    later_sender = _sender(client, clock=lambda: later + timedelta(hours=1))
    assert await later_sender.poll_once() == 0
    assert len(_sends(client)) == 1

    # The same crash, but the live reader stored the echo: reconcile sends it.
    second = await _approved_ai(session, person, trigger, body="Xush kelibsiz!")
    await session.commit()
    monkeypatch.setattr(outbox, "complete", _crash)
    assert await _sender(client).poll_once() == 0
    monkeypatch.setattr(outbox, "complete", real_complete)
    assert await userbot.ingest_message(
        None, _tg_out(client.sent[-1], second.body, at=NOW)
    )
    async with SessionLocal() as s:
        [done] = await outbox.reconcile(s, now=later)
        await s.commit()
    assert done.state == "sent"
    reconciled = await _reload(second.id)
    assert reconciled.external_message_id == f"{DM}:{client.sent[-1]}"


async def test_an_unknown_outcome_is_read_back_never_resent(session, jarvis):
    person, trigger = await _setup(session)
    found = await _approved_ai(session, person, trigger, body="Salom!")
    await session.commit()
    echo = SimpleNamespace(id=888, out=True, message="Salom!", date=NOW)
    client = FakeClient(send_errors=[ConnectionError("dropped")], history=[echo])
    assert await _sender(client).poll_once() == 1
    assert (await _reload(found.id)).external_message_id == f"{DM}:888"

    lost = await _approved_ai(session, person, trigger, body="Xush kelibsiz!")
    await session.commit()
    client = FakeClient(send_errors=[errors.ServerError(None, "RPC_CALL_FAIL", 500)])
    sender = _sender(client)
    assert await sender.poll_once() == 0
    # Not found back: left on the wire for reconcile, never retried here.
    assert (await _reload(lost.id)).state == "sending"
    assert await sender.poll_once() == 0
    assert len(_sends(client)) == 1
    assert ("history", tg_sender.RECONCILE_MESSAGES) in client.events

    # Telegram's -503 timeout on the send: read back too, never failed.
    timed_out = await _approved_ai(session, person, trigger, body="Yaxshi kun!")
    await session.commit()
    client = FakeClient(send_errors=[errors.TimeoutError(None)])
    assert await _sender(client).poll_once() == 0
    assert (await _reload(timed_out.id)).state == "sending"
    assert ("history", tg_sender.RECONCILE_MESSAGES) in client.events


@pytest.mark.parametrize(
    "error",
    [errors.RandomIdDuplicateError(request=None), errors.TimeoutError(None)],
    ids=["duplicate", "timeout"],
)
async def test_telethon_hands_the_sender_the_last_rpc_error(monkeypatch, error):
    """The userbot's client is built with raise_last_call_error=True: after its
    own retries Telethon raises Telegram's error, not ``ValueError('Request
    was unsuccessful')``, which the sender would read as an unknown outcome
    (and _peer as a chat missing from the cache)."""
    from telethon import TelegramClient
    from telethon.client import users as telethon_users
    from telethon.sessions import StringSession

    calls: list[object] = []

    class _MTProto:
        def send(self, request, ordered=False):
            calls.append(request)
            future = asyncio.get_running_loop().create_future()
            future.set_exception(error)
            return future

    async def _no_wait(seconds):
        return None

    monkeypatch.setattr(telethon_users, "asyncio", SimpleNamespace(sleep=_no_wait))
    client = TelegramClient(StringSession(), 1, "hash", raise_last_call_error=True)
    client._sender = _MTProto()
    request = functions.messages.SendMessageRequest(
        peer=types.InputPeerUser(user_id=DM, access_hash=1),
        message="Salom!",
        random_id=tg_sender.random_id_for("k"),
    )
    with pytest.raises(type(error)):
        await client(request)
    assert len(calls) == TELETHON_RETRIES + 1


async def test_a_stop_before_the_send_releases_the_row(session, jarvis):
    """Shut down during the pause: nothing went out, so the row waits
    approved for the next start instead of becoming «in doubt»."""
    person, trigger = await _setup(session)
    row = await _approved_ai(session, person, trigger)
    await session.commit()
    client = FakeClient()

    async def _stopped(seconds):
        raise asyncio.CancelledError

    sender = tg_sender.Sender(
        client, account=ACCOUNT, status={}, sleep=_stopped, clock=lambda: NOW
    )
    with pytest.raises(asyncio.CancelledError):
        await sender.poll_once()
    released = await _reload(row.id)
    assert released.state == "approved" and released.last_error == "interrupted"
    assert client.requests == []
    assert await _sender(client).poll_once() == 1


# --- platform limits ------------------------------------------------------------------


async def test_floodwait_requeues(session, jarvis):
    person, trigger = await _setup(session)
    row = await _approved_ai(session, person, trigger)
    await session.commit()
    client = FakeClient(send_errors=[errors.FloodWaitError(request=None, capture=120)])
    sender = _sender(client)
    verdict_polls = await sender.poll_once()
    assert verdict_polls == 0
    waiting = await _reload(row.id)
    assert waiting.state == "approved" and waiting.last_error == outbox.FLOOD_WAIT_CODE
    assert waiting.next_attempt_at == NOW + timedelta(seconds=120)
    # No claim at all until Telegram's time is up.
    assert await sender.poll_once() == 0
    assert len(_sends(client)) == 1
    async with SessionLocal() as s:
        [wait] = await outbox.flood_waits(s, now=NOW)
        assert wait.id == row.id and wait.seconds == 120
        # The breaker is not tripped by a short wait.
        stop = policy.stop_for(await policy.load(s), "telegram", NOW)
        assert stop is None
    assert replies.jarvis_flood_wait(120) == (
        "⏳ Telegram 2 daqiqa kutishni so'radi — xabarlar keyinroq yuboriladi."
    )


async def test_a_long_floodwait_trips_the_breaker(session, jarvis):
    person, trigger = await _setup(session)
    row = await _approved_ai(session, person, trigger)
    await session.commit()
    wait = tg_sender.FLOOD_BREAKER_SECONDS + 1
    client = FakeClient(send_errors=[errors.FloodWaitError(request=None, capture=wait)])
    await _sender(client).poll_once()
    async with SessionLocal() as s:
        snap = (await policy.load(s))[policy.channel_scope("telegram")]
        assert snap.breaker_reason == tg_sender.CODE_FLOOD_LONG and snap.tripped(NOW)
    # Telegram named the time: held at least BREAKER_FOR, then it ends.
    assert snap.breaker_until == NOW + tg_sender.BREAKER_FOR
    assert (await _reload(row.id)).state == "approved"


async def test_a_dialog_floodwait_while_finding_the_chat_requeues(session, jarvis):
    """The chat is not in the session cache and the dialog sync that would
    find it meets a flood wait: the row is re-queued at Telegram's time, not
    slept on inside the claim and not failed as a missing chat."""
    person, trigger = await _setup(session)
    row = await _approved_ai(session, person, trigger)
    await session.commit()
    client = FakeClient(
        missing={DM}, dialog_errors=[errors.FloodWaitError(request=None, capture=120)]
    )
    sender = _sender(client)
    assert await sender.poll_once() == 0
    waiting = await _reload(row.id)
    assert waiting.state == "approved" and waiting.last_error == outbox.FLOOD_WAIT_CODE
    assert waiting.next_attempt_at == NOW + timedelta(seconds=120)
    assert ("sleep", 120) not in client.events
    assert client.events.count(("dialogs",)) == 1
    assert client.requests == []
    # And the sender asks for nothing until Telegram's time is up.
    assert await sender.poll_once() == 0
    assert client.events.count(("dialogs",)) == 1

    # A long one trips the breaker instead; the row is held, never failed.
    held = await _approved_ai(session, person, trigger, body="Xush kelibsiz!")
    await session.commit()
    wait = tg_sender.FLOOD_BREAKER_SECONDS + 1
    client = FakeClient(
        missing={DM}, dialog_errors=[errors.FloodWaitError(request=None, capture=wait)]
    )
    assert await _sender(client).poll_once() == 0
    kept = await _reload(held.id)
    assert kept.state == "approved" and kept.last_error == tg_sender.CODE_FLOOD_LONG
    async with SessionLocal() as s:
        snap = (await policy.load(s))[policy.channel_scope("telegram")]
    assert snap.breaker_reason == tg_sender.CODE_FLOOD_LONG


async def test_a_chat_missing_after_a_real_sync_fails_as_not_found(session, jarvis):
    person, trigger = await _setup(session)
    row = await _approved_ai(session, person, trigger)
    await session.commit()
    client = FakeClient(missing={DM})
    assert await _sender(client).poll_once() == 0
    gone = await _reload(row.id)
    assert gone.state == "failed" and gone.last_error == tg_sender.CODE_PEER_NOT_FOUND
    assert client.events.count(("dialogs",)) == 1


async def test_a_breaker_that_cannot_be_stored_holds_the_sender_itself(
    session, jarvis, monkeypatch
):
    person, trigger = await _setup(session)
    row = await _approved_ai(session, person, trigger)
    await session.commit()

    async def _broken(*args, **kwargs):
        raise ConnectionError("database gone")

    monkeypatch.setattr(policy, "trip_breaker", _broken)
    client = FakeClient(send_errors=[errors.PeerFloodError(request=None)])
    sender = _sender(client)
    before = time.monotonic()
    assert await sender.poll_once() == 0
    # Not the short local pause: nothing else would hold the next row.
    assert sender.resume_at >= before + tg_sender.BREAKER_FOR.total_seconds()
    assert (await _reload(row.id)).state == "approved"
    assert await sender.poll_once() == 0
    assert len(_sends(client)) == 1


async def test_peerflood_trips_breaker_and_reader_continues(session, jarvis, monkeypatch):
    from miya.worker import main as worker

    person, trigger = await _setup(session)
    row = await _approved_ai(session, person, trigger, body="Salom!")
    other = await _approved_ai(session, person, trigger, body="Xush kelibsiz!")
    await session.commit()
    client = FakeClient(send_errors=[errors.PeerFloodError(request=None)])
    sender = _sender(client)
    assert await sender.poll_once() == 0
    async with SessionLocal() as s:
        snap = (await policy.load(s))[policy.channel_scope("telegram")]
    assert snap.breaker_reason == tg_sender.CODE_PEER_FLOOD
    # Telegram gave no time: it holds until the owner lifts it, as the alert
    # says, not for a day after which the gate would send into it again.
    assert snap.breaker_until == NOW + tg_sender.BREAKER_UNTIL_LIFTED
    assert snap.tripped(NOW + timedelta(days=365))
    held = await _reload(row.id)
    assert held.state == "approved" and held.last_error == tg_sender.CODE_PEER_FLOOD
    # Nothing retries into it: the gate holds every row while it is in force.
    sender.resume_at = 0.0
    assert await sender.poll_once() == 0
    assert len(_sends(client)) == 1
    assert (await _reload(other.id)).state == "approved"

    # Reading goes on: a new client message is stored as ever.
    incoming = _tg_out(TRIGGER_MESSAGE + 1, "Yuk qachon keladi?", at=NOW)
    incoming.out = False
    assert await userbot.ingest_message(None, incoming) is True

    # The owner hears it once.
    told: list[str] = []

    async def _notify(bot, text, **kwargs):
        told.append(text)
        return True

    monkeypatch.setattr(worker, "notify", _notify)
    await worker.jarvis_send_notices(None, now=NOW)
    await worker.jarvis_send_notices(None, now=NOW + timedelta(minutes=1))
    assert told == [replies.JARVIS_TELEGRAM_LIMITED]
    assert told[0] == (
        "⛔ Telegram yuborishni cheklab qo'ydi. MIYA Telegramdan yozishni to'xtatdi; "
        "o'qish davom etadi. @SpamBot orqali holatni tekshiring. Qayta yoqish: "
        "/jarvis yoq telegram"
    )
    # /jarvis yoq telegram lifts it.
    async with SessionLocal() as s:
        await policy.resume(
            s, scope=policy.channel_scope("telegram"), by="command", now=NOW
        )
        await s.commit()
        assert policy.stop_for(await policy.load(s), "telegram", NOW) is None


@pytest.mark.parametrize(
    ("error", "action", "code"),
    [
        (errors.UserIsBlockedError(None), "fail", "user_blocked"),
        (errors.InputUserDeactivatedError(None), "fail", "user_deactivated"),
        (errors.ChatWriteForbiddenError(None), "fail", "write_forbidden"),
        (errors.RPCError(None, "ALLOW_PAYMENT_REQUIRED_250", 400), "fail", "paid_only"),
        (
            errors.RPCError(None, "PRIVACY_PREMIUM_REQUIRED", 403),
            "fail",
            "write_forbidden",
        ),
        (errors.PeerFloodError(None), "breaker", "peer_flood"),
        (errors.AuthKeyDuplicatedError(None), "breaker", "auth_key_duplicated"),
        (errors.SessionRevokedError(None), "breaker", "session_revoked"),
        (errors.UserDeactivatedBanError(None), "breaker", "account_banned"),
        (errors.UserRestrictedError(None), "breaker", "account_restricted"),
        # The account's spam limit for groups, not one chat's ban.
        (errors.UserBannedInChannelError(None), "breaker", "account_restricted"),
        # Any other 401: the session is dead for every row.
        (errors.SessionExpiredError(None), "breaker", "session_revoked"),
        (errors.AuthKeyInvalidError(None), "breaker", "session_revoked"),
        (errors.AuthKeyPermEmptyError(None), "breaker", "session_revoked"),
        # A 420 Telethon does not name is a limit, never one row's failure.
        (errors.FloodError(None, "FLOOD"), "breaker", "flood_wait_long"),
        (errors.FloodWaitError(None, capture=30), "requeue", "flood_wait"),
        (errors.SlowModeWaitError(None, capture=30), "requeue", "slow_mode"),
        (errors.MessageTooLongError(None), "fail", "send_rejected"),
        (tg_sender.PeerNotFound("1"), "fail", "peer_not_found"),
    ],
)
def test_errors_map_to_their_handling(error, action, code):
    verdict = tg_sender.classify(error, on_wire=True)
    assert (verdict.action, verdict.code) == (action, code)


def test_an_error_that_says_nothing_is_unknown_only_on_the_wire():
    assert tg_sender.classify(ConnectionError(), on_wire=True).action == "unknown"
    before = tg_sender.classify(ConnectionError(), on_wire=False)
    assert (before.action, before.code) == ("requeue", "connection")
    assert tg_sender.classify(RuntimeError(), on_wire=True).action == "unknown"
    assert tg_sender.classify(RuntimeError(), on_wire=False).code == "send_error"
    # Telegram's -503 "Timeout" is a TimedOutError, not a ServerError: on the
    # wire nobody knows whether it went, so it is read back, never failed.
    timeout = errors.TimeoutError(None)
    assert not isinstance(timeout, errors.ServerError)
    assert tg_sender.classify(timeout, on_wire=True).action == "unknown"
    assert tg_sender.classify(timeout, on_wire=False) == tg_sender.Verdict(
        "requeue", "connection", tg_sender.RETRY_SECONDS
    )
    internal = errors.ServerError(None, "RPC_CALL_FAIL", 500)
    assert tg_sender.classify(internal, on_wire=True).action == "unknown"


async def test_a_blocked_client_fails_the_row_and_the_owner_hears_why(
    session, jarvis, monkeypatch
):
    from miya.worker import main as worker

    person, trigger = await _setup(session)
    blocked = await _approved_ai(session, person, trigger, body="Salom!")
    await session.commit()
    client = FakeClient(send_errors=[errors.UserIsBlockedError(None)])
    await _sender(client).poll_once()
    failed = await _reload(blocked.id)
    assert failed.state == "failed" and failed.last_error == "user_blocked"

    sent = await _approved_owner(session, person, body="Kechki 6 da gaplashamiz")
    await session.commit()
    async with SessionLocal() as s:
        await outbox.mark_carded(s, sent.id, message_id=9911, now=NOW)
        await s.commit()
    await session.commit()
    client = FakeClient()
    assert await _sender(client).poll_once() == 1

    delivered: list[tuple[str, int | None, bool]] = []

    async def _deliver(bot, text, *, silent=False, reply_to=None, reply_markup=None):
        delivered.append((text, reply_to, silent))
        return True, None

    monkeypatch.setattr(worker, "deliver", _deliver)
    await worker.jarvis_send_notices(None, now=NOW)
    assert delivered == [
        ("✖️ Akmalga yuborib bo'lmadi: u sizni bloklagan", None, False),
        (f"✅ Yuborildi ({NOW.strftime('%H:%M')})", 9911, True),
    ]
    await worker.jarvis_send_notices(None, now=NOW + timedelta(minutes=1))
    assert len(delivered) == 2
    # A name the client chose is shown escaped and without bidi controls.
    assert replies.jarvis_send_failed("<b>‮Ali", "paid_only") == (
        "✖️ &lt;b&gt;Aliga yuborib bo'lmadi: faqat pullik xabar qabul qiladi"
    )
    assert replies.jarvis_send_failed(None, "something_else").endswith("kutilmagan xato")


# --- containment ----------------------------------------------------------------------


async def test_bad_row_fails_alone(session, jarvis):
    person, trigger = await _setup(session)
    other_chat = 7193
    stranger = m.Person(display_name="Bobur", aliases=[], telegram_id=other_chat)
    session.add(stranger)
    session.add(
        m.ChatMonitor(
            tg_chat_id=other_chat,
            chat_type=ChatType.private,
            title="Bobur",
            monitor_enabled=True,
        )
    )
    await session.flush()
    stranger_trigger = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.in_,
        person_id=stranger.id,
        tg_chat_id=other_chat,
        occurred_at=NOW - timedelta(minutes=6),
        raw_text="Salom",
        meta={"tg_message_id": 77},
    )
    session.add(stranger_trigger)
    await session.flush()
    broken = await outbox.create(
        session,
        channel="telegram",
        chat_key=chatkey.telegram(other_chat),
        origin="auto_reply",
        authored_by="ai",
        body="Salom!",
        now=NOW - timedelta(minutes=1),
        intent="greeting",
        person_id=stranger.id,
        trigger_interaction_id=stranger_trigger.id,
    )
    assert (await outbox.approve(session, broken.id, by="button", now=NOW)).approved
    good = await _approved_ai(session, person, trigger)
    await session.commit()

    client = FakeClient(entity_errors={other_chat: RuntimeError("boom")})
    sender = _sender(client)
    for _ in range(2):
        await sender.poll_once()
    assert (await _reload(broken.id)).last_error == tg_sender.CODE_SEND_ERROR
    assert (await _reload(broken.id)).state == "failed"
    assert (await _reload(good.id)).state == "sent"
    # Not in the session's cache even after a dialog sync: failed, no contact import.
    client.entity_errors.clear()
    client.missing.add(other_chat)
    missing = await outbox.create(
        session,
        channel="telegram",
        chat_key=chatkey.telegram(other_chat),
        origin="auto_reply",
        authored_by="ai",
        body="Xush kelibsiz!",
        now=NOW,
        intent="greeting",
        person_id=stranger.id,
        trigger_interaction_id=stranger_trigger.id,
    )
    assert (await outbox.approve(session, missing.id, by="button", now=NOW)).approved

    await session.commit()
    await sender.poll_once()
    gone = await _reload(missing.id)
    assert gone.state == "failed" and gone.last_error == tg_sender.CODE_PEER_NOT_FOUND
    assert ("dialogs",) in client.events
    assert len(client.sent) == 1


async def test_sender_sends_nothing_the_claim_did_not_hand_out(
    session, jarvis, monkeypatch
):
    person, trigger = await _setup(session)
    row = await _approved_ai(session, person, trigger)
    await session.commit()
    handed: list[int] = []
    real = outbox.claim_ready

    async def _claim(*args, **kwargs):
        rows = await real(*args, **kwargs)
        handed.extend(r.id for r in rows)
        return []

    monkeypatch.setattr(outbox, "claim_ready", _claim)
    client = FakeClient()
    assert await _sender(client).poll_once() == 0
    assert handed == [row.id] and client.requests == []
    # Switched off, the claim hands out nothing at all.
    monkeypatch.setattr(outbox, "claim_ready", real)
    monkeypatch.setattr(settings, "telegram_send_enabled", False)
    other = await _approved_ai(session, person, trigger, body="Xush kelibsiz!")
    await session.commit()
    assert await _sender(client).poll_once() == 0
    assert (await _reload(other.id)).state == "approved" and client.requests == []


async def test_sender_crash_restarts_and_reader_continues(session, jarvis, monkeypatch):
    person, trigger = await _setup(session)
    row = await _approved_ai(session, person, trigger)
    await session.commit()
    client = FakeClient()
    real_poll = tg_sender.Sender.poll_once
    crashes = {"poll": 1, "supervisor": 1}

    async def _poll(self):
        if crashes["poll"]:
            crashes["poll"] -= 1
            raise RuntimeError("loop bug")
        return await real_poll(self)

    real_supervise = tg_sender.supervise

    async def _supervise(sender):
        if crashes["supervisor"]:
            crashes["supervisor"] -= 1
            raise RuntimeError("supervisor bug")
        await real_supervise(sender)

    monkeypatch.setattr(tg_sender.Sender, "poll_once", _poll)
    monkeypatch.setattr(tg_sender, "supervise", _supervise)

    async def _sleep(seconds):
        await asyncio.sleep(0.01)

    reads = {"n": 0}

    async def _reader():
        while True:
            reads["n"] += 1
            await asyncio.sleep(0.01)

    reader = asyncio.create_task(_reader())
    status: dict = {}
    handle = tg_sender.start(
        client, account=ACCOUNT, status=status, sleep=_sleep, clock=lambda: NOW
    )
    try:
        for _ in range(500):
            if "sender_sent_at" in status:
                break
            await asyncio.sleep(0.02)
    finally:
        handle.cancel()
        await asyncio.sleep(0.05)
    assert client.sent and (await _reload(row.id)).state == "sent"
    assert handle.restarts == 1 and crashes == {"poll": 0, "supervisor": 0}
    assert status["sender"] == "stopped"
    assert not reader.done() and reads["n"] > 1
    reader.cancel()


# --- the process ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sending", "client_kwargs"),
    [
        (True, {"flood_sleep_threshold": 7, "raise_last_call_error": True}),
        (False, {"flood_sleep_threshold": 60, "raise_last_call_error": False}),
    ],
)
async def test_startup_floodwait_does_not_exit(monkeypatch, sending, client_kwargs):
    """A long FloodWait on the startup dialog sync no longer ends run(): the
    handlers are registered, the sender starts with both switches on, and the
    client's flood_sleep_threshold is USERBOT_FLOOD_SLEEP_SECONDS. With
    sending off the reader keeps Telethon's defaults (60 s, no last error)."""
    made: dict = {}
    ran: list[str] = []

    class _Client:
        def __init__(self, session, api_id, api_hash, **kwargs):
            made.update(kwargs)
            self.handlers: list = []

        async def connect(self):
            return None

        def is_connected(self):
            return True

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            return SimpleNamespace(
                id=OWNER_ID, username=None, first_name="Owner", last_name=None
            )

        async def iter_dialogs(self):
            raise errors.FloodWaitError(request=None, capture=3600)
            yield  # pragma: no cover - an async generator

        def add_event_handler(self, handler, event=None):
            self.handlers.append(handler)

        async def run_until_disconnected(self):
            ran.append("listening")

        async def disconnect(self):
            ran.append("disconnected")

    class _Engine:
        async def dispose(self):
            return None

    async def _quiet(*args, **kwargs):
        return None

    started: list = []

    def _start(client, *, account, status):
        started.append(account)
        return SimpleNamespace(cancel=lambda: started.append("cancelled"))

    monkeypatch.setattr(settings, "userbot_enabled", True)
    monkeypatch.setattr(settings, "telethon_api_id", 1)
    monkeypatch.setattr(settings, "telethon_api_hash", "hash")
    monkeypatch.setattr(settings, "telethon_session", "1session")
    monkeypatch.setattr(settings, "jarvis_enabled", True)
    monkeypatch.setattr(settings, "telegram_send_enabled", sending)
    monkeypatch.setattr(settings, "userbot_flood_sleep_seconds", 7)
    monkeypatch.setattr(userbot, "StringSession", lambda value: object())
    monkeypatch.setattr(userbot, "TelegramClient", _Client)
    monkeypatch.setattr(userbot, "beat_userbot", _quiet)
    monkeypatch.setattr(userbot, "approved_media_loop", _quiet)
    monkeypatch.setattr(userbot, "_startup_catch_up", _quiet)
    monkeypatch.setattr(userbot, "engine", _Engine())
    monkeypatch.setattr(userbot, "setup_logging", lambda component: None)
    monkeypatch.setattr(tg_sender, "start", _start)
    await userbot.run()
    assert made == client_kwargs
    assert ran == ["listening", "disconnected"]
    assert started == ([ACCOUNT, "cancelled"] if sending else [])


@pytest.mark.parametrize("error", [errors.FloodWaitError, errors.FloodPremiumWaitError])
async def test_the_reader_waits_out_short_flood_waits(monkeypatch, error):
    """With the sender's short client threshold a 20 s wait reaches the
    reader, a plain one or the download-speed one of an account without
    Premium: it sleeps it out and reads again, a bounded number of times; a
    longer one is raised at once."""
    slept: list[float] = []

    async def _sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(userbot.asyncio, "sleep", _sleep)
    calls: list[int] = []

    async def _download():
        calls.append(1)
        if len(calls) <= 2:
            raise error(request=None, capture=20)
        return "file"

    assert await userbot._reader_call(_download) == "file"
    assert len(calls) == 3 and slept == [21, 21]

    async def _long():
        raise error(request=None, capture=3600)

    slept.clear()
    with pytest.raises(error):
        await userbot._reader_call(_long)
    assert slept == []

    async def _always():
        raise error(request=None, capture=20)

    with pytest.raises(error):
        await userbot._reader_call(_always)
    assert slept == [21] * userbot.READER_FLOOD_RETRIES


async def test_sigterm_stops_the_sender_before_the_client_goes(monkeypatch):
    """``docker compose stop`` sends SIGTERM: the sender is cancelled and
    awaited (so a row it was pacing is put back approved) while the client is
    still connected, and only then does the client disconnect and run() end."""
    ran: list[str] = []
    gone = asyncio.Event()

    class _Client:
        def __init__(self, session, api_id, api_hash, **kwargs):
            pass

        async def connect(self):
            return None

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            return SimpleNamespace(
                id=OWNER_ID, username=None, first_name="Owner", last_name=None
            )

        async def iter_dialogs(self):
            for _ in ():
                yield _  # pragma: no cover

        def add_event_handler(self, handler, event=None):
            return None

        async def run_until_disconnected(self):
            ran.append("listening")
            # Never send a real SIGTERM unless the userbot's handler took it.
            assert signal.getsignal(signal.SIGTERM) not in (signal.SIG_DFL, None)
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.wait_for(gone.wait(), timeout=10)

        async def disconnect(self):
            ran.append("disconnected")
            gone.set()

    class _Engine:
        async def dispose(self):
            ran.append("pool closed")

    async def _quiet(*args, **kwargs):
        return None

    async def _pacing():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # The interrupted row is written back before the client goes.
            await asyncio.sleep(0)
            ran.append("row released")
            raise

    def _start(client, *, account, status):
        task = asyncio.create_task(_pacing())
        return SimpleNamespace(task=task, cancel=task.cancel)

    @asynccontextmanager
    async def _scope():
        yield None

    async def _sync(session, dialogs):
        return 0, 0

    monkeypatch.setattr(settings, "userbot_enabled", True)
    monkeypatch.setattr(settings, "telethon_api_id", 1)
    monkeypatch.setattr(settings, "telethon_api_hash", "hash")
    monkeypatch.setattr(settings, "telethon_session", "1session")
    monkeypatch.setattr(settings, "jarvis_enabled", True)
    monkeypatch.setattr(settings, "telegram_send_enabled", True)
    monkeypatch.setattr(userbot, "StringSession", lambda value: object())
    monkeypatch.setattr(userbot, "TelegramClient", _Client)
    monkeypatch.setattr(userbot, "beat_userbot", _quiet)
    monkeypatch.setattr(userbot, "approved_media_loop", _quiet)
    monkeypatch.setattr(userbot, "_startup_catch_up", _quiet)
    monkeypatch.setattr(userbot, "session_scope", _scope)
    monkeypatch.setattr(userbot, "sync_dialogs", _sync)
    monkeypatch.setattr(userbot, "engine", _Engine())
    monkeypatch.setattr(userbot, "setup_logging", lambda component: None)
    monkeypatch.setattr(tg_sender, "start", _start)
    await userbot.run()
    assert ran[:3] == ["listening", "row released", "disconnected"]
    assert ran[-1] == "pool closed"
    # The handlers are gone with the client: a later SIGTERM is the default.
    assert signal.getsignal(signal.SIGTERM) in (signal.SIG_DFL, None)


async def test_a_short_dialog_floodwait_is_slept_through(monkeypatch):
    slept: list[int] = []

    async def _sleep(seconds):
        slept.append(seconds)

    class _Client:
        calls = 0

        async def iter_dialogs(self):
            _Client.calls += 1
            if _Client.calls == 1:
                raise errors.FloodWaitError(request=None, capture=3)
            for _ in ():
                yield _  # pragma: no cover

    synced: list = []

    @asynccontextmanager
    async def _scope():
        yield None

    async def _sync(session, dialogs):
        synced.append(list(dialogs))
        return 0, 0

    monkeypatch.setattr(userbot.asyncio, "sleep", _sleep)
    monkeypatch.setattr(userbot, "session_scope", _scope)
    monkeypatch.setattr(userbot, "sync_dialogs", _sync)
    assert await userbot.sync_from_client(_Client()) == (0, 0)
    assert slept == [3] and synced == [[]]


def test_the_sender_starts_only_with_both_switches(monkeypatch):
    started: list = []
    monkeypatch.setattr(
        tg_sender,
        "start",
        lambda client, *, account, status: started.append(account) or "h",
    )
    for jarvis_on, send_on in ((False, False), (True, False), (False, True)):
        monkeypatch.setattr(settings, "jarvis_enabled", jarvis_on)
        monkeypatch.setattr(settings, "telegram_send_enabled", send_on)
        assert userbot.start_sender(object(), user_id=OWNER_ID) is None
    monkeypatch.setattr(settings, "jarvis_enabled", True)
    monkeypatch.setattr(settings, "telegram_send_enabled", True)
    assert userbot.start_sender(object(), user_id=OWNER_ID) == "h"
    assert started == [ACCOUNT]

    def _broken(*args, **kwargs):
        raise RuntimeError("no loop")

    monkeypatch.setattr(tg_sender, "start", _broken)
    # A sender that cannot start never stops the reader.
    assert userbot.start_sender(object(), user_id=OWNER_ID) is None


def test_cards_leave_copy_mode_with_telegram_sending(monkeypatch):
    monkeypatch.setattr(settings, "telegram_send_enabled", False)
    assert outbox.channel_send_enabled("telegram") is False
    monkeypatch.setattr(settings, "telegram_send_enabled", True)
    assert outbox.channel_send_enabled("telegram") is True
    assert outbox.channel_send_enabled("whatsapp") is False


async def test_the_beat_carries_the_sender_status(session, monkeypatch):
    monkeypatch.setattr(userbot, "_last_catch_up", None)
    monkeypatch.setattr(userbot, "_sender_status", {"sender": "running"})
    assert await userbot.beat_userbot(enabled=True, connected=True, user_id=OWNER_ID)
    async with SessionLocal() as s:
        beat = await s.get(m.Heartbeat, "userbot")
        assert beat.detail == {
            "enabled": True,
            "connected": True,
            "user": OWNER_ID,
            "sender": "running",
        }


# --- configuration --------------------------------------------------------------------

KEYS = {
    "TELEGRAM_SEND_ENABLED": "false",
    "USERBOT_FLOOD_SLEEP_SECONDS": "5",
    "JARVIS_REPLY_DELAY_MIN_SECONDS": "3",
    "JARVIS_REPLY_DELAY_MAX_SECONDS": "7",
    "JARVIS_TYPING_CHARS_PER_SECOND": "6",
    "JARVIS_TYPING_MAX_SECONDS": "15",
}


def test_the_config_keys_are_in_all_four_places():
    env = dotenv_values(ROOT / ".env.example")
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for key, default in KEYS.items():
        assert env[key] == default, key
        assert lines[lines.index(f"{key}={default}") - 1].startswith("#"), key
        assert f"`{key}`" in readme, key
        assert key in doctor.EXPECTED_ENV_KEYS, key
    fresh = Settings(_env_file=None)
    assert fresh.telegram_send_enabled is False
    assert (
        fresh.userbot_flood_sleep_seconds,
        fresh.jarvis_reply_delay_min_seconds,
        fresh.jarvis_reply_delay_max_seconds,
        fresh.jarvis_typing_chars_per_second,
        fresh.jarvis_typing_max_seconds,
    ) == (5, 3, 7, 6, 15)


def test_the_reply_delay_bounds_are_ordered():
    with pytest.raises(ValueError, match="JARVIS_REPLY_DELAY_MIN_SECONDS"):
        Settings(
            _env_file=None,
            jarvis_reply_delay_min_seconds=8,
            jarvis_reply_delay_max_seconds=5,
        )
    same = Settings(
        _env_file=None, jarvis_reply_delay_min_seconds=5, jarvis_reply_delay_max_seconds=5
    )
    assert same.jarvis_reply_delay_min_seconds == 5
    # Rule 20: one key set on its own never fails validation.
    assert Settings(_env_file=None, jarvis_reply_delay_min_seconds=9)
    with pytest.raises(ValueError):
        Settings(_env_file=None, jarvis_typing_chars_per_second=0)

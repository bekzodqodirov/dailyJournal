"""Jarvis phases 1-3 end to end (JV-01..JV-23): a client's Telegram message
through the userbot's live handler, the responder tick, the owner's card, the
owner's tap or reply, the Telegram sender's claim, pacing and send, and the
echo read back by the userbot.

Every component runs as it does in production, each in its own transactions
(the real ``session_scope``) against real PostgreSQL, with the real JV-13
red-line guard and gate. Only the edges are fakes, the ones the unit tests of
each step already use: Telethon (``tests/test_userbot_sender.FakeClient``),
the model (``tests/test_responder.FakeClient``, ``tests/test_jarvis_commands.
Model``), the admin bot (``tests/test_jarvis_cards.FakeBot`` and its callback
and owner-message fakes). The clock is frozen (time-machine) at the start of
each test and moved by hand, so the debounce and every "now" are exact.
Phase 4 (Gmail, WhatsApp) is not built and is not exercised here.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa
import time_machine
from aiogram.filters import CommandObject

from miya.bot import handlers, replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import ChatType, Direction
from miya.db.session import session_scope
from miya.jarvis import chatkey, costs, gate, kb, outbox, policy, redlines, stalls, style
from miya.services import authorship, rag, safety
from miya.userbot import main as userbot
from miya.userbot import sender as tg_sender
from miya.worker import main as worker
from tests.test_jarvis_cards import (
    FakeBot,
    _buttons,
    _Callback,
    _CardMessage,
    _owner_message,
)
from tests.test_jarvis_commands import Model
from tests.test_responder import OWNER_ID, DownEmbedder
from tests.test_responder import FakeClient as FakeModel
from tests.test_safety_messages import CHAT, _tg_message
from tests.test_safety_messages import _Client as _ReaderClient
from tests.test_userbot_sender import FakeClient as FakeTelethon
from tests.test_userbot_sender import _sleeper

TZ = settings.tz
KEY = chatkey.telegram(CHAT)
ACCOUNT = f"tg:{OWNER_ID}"
GREETING_REPLY = "Assalomu alaykum! Yuk qaysi shahardan?"


# --- the harness -----------------------------------------------------------------------


class Clock:
    """The frozen wall clock: every ``datetime.now`` in MIYA reads it."""

    def __init__(self, traveller) -> None:
        self._traveller = traveller

    def now(self) -> datetime:
        return datetime.now(TZ)

    def advance(self, seconds: float) -> datetime:
        self._traveller.shift(timedelta(seconds=seconds))
        return self.now()


@pytest.fixture
def clock():
    # Frozen at the real moment, so PostgreSQL's own now() (created_at) stays
    # within the test's minutes of Python's.
    start = datetime.now(TZ).replace(microsecond=0)
    with time_machine.travel(start, tick=False) as traveller:
        yield Clock(traveller)


@pytest.fixture
def jv(monkeypatch):
    """Jarvis on at the draft ceiling with Telegram sending switched on (the
    sender is ready, so cards carry send buttons), the JV-13 guard, a fake
    model for the responder and the cards, no anti-ban gap."""
    monkeypatch.setattr(settings, "jarvis_enabled", True)
    monkeypatch.setattr(settings, "jarvis_mode", "draft")
    monkeypatch.setattr(settings, "telegram_send_enabled", True)
    monkeypatch.setattr(settings, "owner_telegram_id", OWNER_ID)
    monkeypatch.setattr(settings, "owner_aliases", "")
    monkeypatch.setattr(settings, "extract_model", "test-extract-model")
    monkeypatch.setattr(settings, "reason_model", "test-reason-model")
    monkeypatch.setattr(settings, "extract_model_price", "1.00,5.00")
    monkeypatch.setattr(settings, "reason_model_price", "3.00,15.00")
    monkeypatch.setattr(settings, "jarvis_debounce_seconds", 45)
    monkeypatch.setattr(settings, "jarvis_owner_first_seconds", 0)
    monkeypatch.setattr(settings, "jarvis_max_message_age_seconds", 900)
    monkeypatch.setattr(settings, "jarvis_max_ai_turns", 2)
    monkeypatch.setattr(settings, "jarvis_history_turns", 8)
    monkeypatch.setattr(settings, "jarvis_per_chat_daily_cap", 6)
    monkeypatch.setattr(settings, "jarvis_send_min_gap_seconds", 0)
    monkeypatch.setattr(settings, "jarvis_takeover_minutes", 120)
    monkeypatch.setattr(settings, "jarvis_draft_daily_cap", 40)
    monkeypatch.setattr(settings, "jarvis_escalation_daily_cap", 30)
    monkeypatch.setattr(settings, "jarvis_draft_expiry_hours", 12)
    monkeypatch.setattr(settings, "kb_match_threshold", 0.75)
    monkeypatch.setattr(settings, "sheets_enabled", False)
    model = FakeModel()
    monkeypatch.setattr(costs, "get_client", lambda: model)
    monkeypatch.setattr(kb, "get_embedder", lambda: DownEmbedder())
    monkeypatch.setattr(style, "get_embedder", lambda: DownEmbedder())
    previous = gate.registered_guard()
    redlines.install()
    yield model
    gate.register_guard(previous)


@pytest.fixture
def media_in_tmp(monkeypatch, tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    monkeypatch.setattr(type(settings), "media_dir", property(lambda self: media))
    return media


async def _client(*, name: str = "Akmal", mode: str | None = "draft") -> int:
    """The counterparty (a known person) and their private chat, committed;
    the stored Jarvis mode (None keeps the default, observation)."""
    async with session_scope() as session:
        person = m.Person(display_name=name, aliases=[], telegram_id=CHAT)
        session.add(person)
        session.add(
            m.ChatMonitor(
                tg_chat_id=CHAT,
                chat_type=ChatType.private,
                title=name,
                monitor_enabled=True,
                monitoring_since=datetime.now(TZ) - timedelta(days=30),
            )
        )
        await session.flush()
        if mode is not None:
            await policy.set_mode(
                session,
                scope=policy.GLOBAL,
                mode=mode,
                by="command",
                now=datetime.now(TZ),
            )
        return person.id


_mids = iter(range(70_000, 80_000))


async def _client_writes(text: str, *, at: datetime) -> int:
    """A live incoming private message, through the userbot's NewMessage
    handler (ingest, then the reply candidate). Returns its message id."""
    mid = next(_mids)
    await userbot.on_new_message(_ReaderClient(), _tg_message(text, mid=mid, at=at))
    return mid


async def _owner_writes(text: str, *, at: datetime, mid: int | None = None) -> int:
    """An outgoing message in the client's chat, as the userbot sees it."""
    mid = mid if mid is not None else next(_mids)
    await userbot.on_new_message(
        _ReaderClient(), _tg_message(text, mid=mid, at=at, out=True)
    )
    return mid


async def _respond(bot: FakeBot, clock: Clock) -> None:
    """One worker ``jarvis_respond`` tick, after the debounce."""
    await worker.jarvis_respond_job(
        bot, now=clock.advance(settings.jarvis_debounce_seconds + 1)
    )


def _sender(client: FakeTelethon) -> tg_sender.Sender:
    return tg_sender.Sender(
        client,
        account=ACCOUNT,
        status={},
        sleep=_sleeper(client.events),
        clock=lambda: datetime.now(TZ),
    )


def _sends(client: FakeTelethon) -> list[tuple]:
    return [e for e in client.events if e[0] == "send"]


async def _rows(session) -> list[m.OutboxMessage]:
    return list(
        await session.scalars(
            sa.select(m.OutboxMessage)
            .where(m.OutboxMessage.chat_key == KEY)
            .order_by(m.OutboxMessage.id)
            .execution_options(populate_existing=True)
        )
    )


async def _escalations(session) -> list[m.Escalation]:
    return list(
        await session.scalars(
            sa.select(m.Escalation)
            .where(m.Escalation.chat_key == KEY)
            .order_by(m.Escalation.id)
            .execution_options(populate_existing=True)
        )
    )


async def _state(session) -> m.JarvisChatState | None:
    return await session.scalar(
        sa.select(m.JarvisChatState)
        .where(m.JarvisChatState.chat_key == KEY)
        .execution_options(populate_existing=True)
    )


async def _reload(session, model, ident):
    return await session.scalar(
        sa.select(model)
        .where(model.id == ident)
        .execution_options(populate_existing=True)
    )


async def _outgoing(session) -> list[m.Interaction]:
    return list(
        await session.scalars(
            sa.select(m.Interaction)
            .where(
                m.Interaction.tg_chat_id == CHAT,
                m.Interaction.direction == Direction.out,
            )
            .order_by(m.Interaction.id)
            .execution_options(populate_existing=True)
        )
    )


def _bot_cards(bot: FakeBot) -> list[tuple[str, list[tuple[str, str]], int]]:
    """Every message the admin bot sent the owner: text, buttons, message id."""
    return [
        (text, _buttons(kwargs.get("reply_markup")), 7001 + i)
        for i, (text, kwargs) in enumerate(bot.sent)
    ]


# --- 1. shadow: stored, never shown, never sent ----------------------------------------


async def test_shadow_live_message_is_drafted_and_nothing_is_sent(
    session, jv, clock, media_in_tmp
):
    await _client(mode=None)  # the stored mode's default: observation
    mid = await _client_writes("Assalomu alaykum, yuk bor edi", at=clock.now())
    state = await _state(session)
    assert state is not None and state.respond_after == clock.now() + timedelta(
        seconds=settings.jarvis_debounce_seconds
    )
    # The live handler only queued it: no model call in a Telethon handler.
    assert jv.calls == []

    bot = FakeBot()
    await _respond(bot, clock)
    [row] = await _rows(session)
    assert row.state == outbox.SHADOW and row.origin == "auto_reply"
    assert row.authored_by == "ai" and row.body == GREETING_REPLY
    trigger = await _reload(session, m.Interaction, row.trigger_interaction_id)
    assert trigger.meta["tg_message_id"] == mid
    assert jv.roles() == ["intent", "draft", "guard"]
    # Observation: no card, no alert, nothing claimable, nothing on the wire.
    assert bot.sent == [] and await _escalations(session) == []
    telegram = FakeTelethon()
    assert await _sender(telegram).poll_once() == 0
    assert telegram.events == []
    assert (await _reload(session, m.OutboxMessage, row.id)).state == outbox.SHADOW
    assert await _outgoing(session) == []
    # /qoralamalar lists it for calibration.
    listing = _Message()
    await handlers.cmd_shadow_drafts(listing)
    [shown] = listing.sent
    assert replies.SHADOW_DRAFTS_HEADER in shown
    assert "Akmal" in shown and GREETING_REPLY in shown


# --- 2. draft: card, tap, paced send, tagged echo ---------------------------------------


async def test_draft_card_tap_sends_once_and_the_echo_is_no_takeover(
    session, jv, clock, media_in_tmp
):
    await _client()
    mid = await _client_writes("Assalomu alaykum", at=clock.now())
    bot = FakeBot()
    await _respond(bot, clock)
    [row] = await _rows(session)
    assert row.state == outbox.DRAFT and row.body == GREETING_REPLY
    assert row.intent in ("greeting", "info_gathering")

    # The draft card reached the owner in the same run, with the send button.
    [(text, buttons, card_id)] = _bot_cards(bot)
    assert text.startswith(f"✍️ Qoralama j{row.id} · Akmal · Telegram\n")
    assert f"«{GREETING_REPLY}»" in text
    assert replies.JARVIS_COPY_FOOTER not in text
    assert buttons == [
        ("✅ Yuborish", f"jv:y:{row.id}"),
        ("✏️ Tahrirlash", f"jv:e:{row.id}"),
        ("🙋 O'zim yozaman", f"jv:o:{row.id}"),
        ("✖️ Bekor", f"jv:n:{row.id}"),
    ]
    shown = await _reload(session, m.OutboxMessage, row.id)
    assert shown.card_tg_message_id == card_id

    # Nothing leaves before the tap.
    telegram = FakeTelethon()
    sender = _sender(telegram)
    assert await sender.poll_once() == 0 and telegram.events == []

    clock.advance(30)
    tap = _Callback(f"jv:y:{row.id}", card_id)
    await handlers.on_jarvis_draft_button(tap)
    [(edited, markup)] = tap.message.edited
    assert edited.endswith("⏳ Navbatga qo'yildi — Telegram orqali yuboriladi")
    assert markup is None
    approved = await _reload(session, m.OutboxMessage, row.id)
    assert approved.state == outbox.APPROVED and approved.approved_by == "button"

    # The sender claims it, paces like a person, sends it once.
    clock.advance(5)
    assert await sender.poll_once() == 1
    kinds = [e[0] for e in telegram.events]
    assert kinds == ["entity", "sleep", "read", "typing", "sleep", "typing_done", "send"]
    assert telegram.events[0] == ("entity", CHAT)
    assert telegram.events[2] == ("read", mid)
    pre_delay = telegram.events[1][1]
    assert (
        settings.jarvis_reply_delay_min_seconds
        <= pre_delay
        <= settings.jarvis_reply_delay_max_seconds
    )
    [request] = telegram.requests
    assert request.message == GREETING_REPLY
    assert request.random_id == tg_sender.random_id_for(approved.idempotency_key)
    assert request.entities is None and request.no_webpage is True
    assert request.reply_to is None  # a private chat is answered unquoted
    [message_id] = telegram.sent

    sent = await _reload(session, m.OutboxMessage, row.id)
    assert sent.state == outbox.SENT and sent.sent_at == clock.now()
    assert sent.external_message_id == authorship.telegram_external_id(CHAT, message_id)
    [echo] = await _outgoing(session)
    assert echo.id == sent.sent_interaction_id
    assert echo.raw_text == GREETING_REPLY
    assert echo.meta["tg_message_id"] == message_id
    assert echo.meta["outbox_id"] == row.id and authorship.is_ai_reply(echo)
    assert not authorship.is_owner_typed(echo)

    # The next poll finds nothing: exactly once.
    clock.advance(5)
    assert await sender.poll_once() == 0
    assert len(_sends(telegram)) == 1

    # The userbot then reads its own message back (NewMessage, outgoing): it
    # is stored once, still MIYA's, and the chat is not taken over.
    clock.advance(1)
    echo_message = _tg_message(GREETING_REPLY, mid=message_id, at=clock.now(), out=True)
    await userbot.on_new_message(_ReaderClient(), echo_message)
    assert not await userbot.note_owner_message(echo_message)
    [again] = await _outgoing(session)
    assert again.id == echo.id and authorship.is_ai_reply(again)
    state = await _state(session)
    assert state.owner_replied_at is None and state.ai_paused_until is None
    assert state.last_ai_sent_at == sent.sent_at

    # The owner is told it went (jarvis_outbox's send notices).
    await worker.jarvis_send_notices(bot, now=clock.now())
    assert bot.sent[-1][0].startswith("✅ Yuborildi")


class EchoingTelethon(FakeTelethon):
    """Telethon delivering the account's own message to the userbot's
    NewMessage handler while the send call is still on the wire, before the
    sender could record it (the race ``complete`` must survive)."""

    def __init__(self, at) -> None:
        super().__init__()
        self.at = at
        self.read_back: list[m.Interaction] = []

    async def __call__(self, request):
        result = await super().__call__(request)
        await userbot.on_new_message(
            _ReaderClient(),
            _tg_message(request.message, mid=self.sent[-1], at=self.at(), out=True),
        )
        async with session_scope() as scope:
            self.read_back = list(
                await scope.scalars(
                    sa.select(m.Interaction).where(
                        m.Interaction.tg_chat_id == CHAT,
                        m.Interaction.direction == Direction.out,
                    )
                )
            )
        return result


async def test_an_echo_read_before_the_send_is_recorded_is_no_takeover(
    session, jv, clock, media_in_tmp
):
    await _client()
    await _client_writes("Assalomu alaykum", at=clock.now())
    bot = FakeBot()
    await _respond(bot, clock)
    [row] = await _rows(session)
    [(_text, _shown, card_id)] = _bot_cards(bot)
    clock.advance(20)
    await handlers.on_jarvis_draft_button(_Callback(f"jv:y:{row.id}", card_id))
    clock.advance(5)
    telegram = EchoingTelethon(lambda: datetime.now(TZ))
    assert await _sender(telegram).poll_once() == 1
    [message_id] = telegram.sent
    # The reader stored it first, untagged: the row was still on the wire.
    [early] = telegram.read_back
    assert early.meta["tg_message_id"] == message_id
    assert "outbox_id" not in early.meta
    # One stored message, tagged by complete, and the AI not paused: the
    # untagged echo the reader stored first was matched to the row on the wire.
    [echo] = await _outgoing(session)
    assert echo.meta["tg_message_id"] == message_id
    assert echo.meta["outbox_id"] == row.id and authorship.is_ai_reply(echo)
    sent = await _reload(session, m.OutboxMessage, row.id)
    assert sent.state == outbox.SENT and sent.sent_interaction_id == echo.id
    state = await _state(session)
    assert state.owner_replied_at is None and state.ai_paused_until is None


# --- 3. a price question: the alert, the stall, the owner's own words -------------------


async def _price_alert(session, bot: FakeBot, clock: Clock):
    await _client()
    await _client_writes("Narxi qancha?", at=clock.now())
    await _respond(bot, clock)
    [escalation] = await _escalations(session)
    assert escalation.state == "open" and escalation.reasons == ["price"]
    [stall] = await _rows(session)
    assert stall.origin == "stall" and stall.escalation_id == escalation.id
    assert stall.body == stalls.template("price", "latin", "neutral")
    # In draft mode the stall waits for the owner like any draft.
    assert stall.state == outbox.DRAFT
    # The alert went out at once, in the responder's run, with three buttons.
    text, buttons, alert_id = _bot_cards(bot)[0]
    assert text.startswith("⚠️ Akmal narx so'rayapti · Telegram\n")
    assert "💬 «Narxi qancha?»" in text
    assert f"🤖 Yuborishga tayyor: «{stall.body}»" in text
    assert replies.JARVIS_ESCALATION_FOOTER in text
    assert buttons == [
        ("✅ Tasdiqlash va yuborish", f"je:y:{escalation.id}"),
        ("✍️ O'zim javob yozaman", f"je:o:{escalation.id}"),
        ("⏰ 1 soatdan keyin eslat", f"je:s:{escalation.id}"),
    ]
    alerted = await _reload(session, m.Escalation, escalation.id)
    assert alerted.alert_tg_message_id == alert_id
    return escalation, stall, alert_id


async def test_price_question_owner_words_are_sent_exactly(
    session, jv, clock, media_in_tmp
):
    bot = FakeBot()
    escalation, stall, alert_id = await _price_alert(session, bot, clock)
    # A red line: no intent and no draft model call (only the alert's
    # digit-free summary, written after the alert went out).
    assert set(jv.roles()) <= {"summary"}

    # The stall is never sent on its own: nothing is claimable before a tap.
    telegram = FakeTelethon()
    sender = _sender(telegram)
    clock.advance(60)
    assert await sender.poll_once() == 0 and telegram.events == []

    # The owner replies to the alert with their own words.
    mine = "Narxi 500 ming so'm, ertaga 14:00 da yetkazamiz"
    clock.advance(30)
    message = _owner_message(mine, replying_to=_CardMessage(alert_id))
    await handlers.on_text(message)
    [(card_text, markup)] = message.sent
    assert f"«{replies.escape(mine)}»" in card_text
    assert replies.JARVIS_OWNER_TEXT_WARNING in card_text
    assert ("✅ Tasdiqlash va yuborish", f"je:y:{escalation.id}") in _buttons(markup)
    rows = await _rows(session)
    [own] = [r for r in rows if r.origin == "escalation"]
    assert own.authored_by == "owner" and own.body == mine
    assert (await _reload(session, m.OutboxMessage, stall.id)).state == outbox.CANCELLED
    new_alert = (await _reload(session, m.Escalation, escalation.id)).alert_tg_message_id

    # «Tasdiqlash va yuborish» sends exactly the owner's words, once.
    clock.advance(10)
    tap = _Callback(f"je:y:{escalation.id}", new_alert)
    await handlers.on_jarvis_escalation_button(tap)
    assert tap.message.edited[0][0].endswith(
        "⏳ Navbatga qo'yildi — Telegram orqali yuboriladi"
    )
    clock.advance(5)
    assert await sender.poll_once() == 1
    [request] = telegram.requests
    assert request.message == mine
    own = await _reload(session, m.OutboxMessage, own.id)
    assert own.state == outbox.SENT
    assert request.random_id == tg_sender.random_id_for(own.idempotency_key)
    closed = await _reload(session, m.Escalation, escalation.id)
    assert (closed.state, closed.resolution) == ("resolved", "sent")
    # The cancelled stall never left; nothing else is claimable.
    clock.advance(5)
    assert await sender.poll_once() == 0 and len(_sends(telegram)) == 1
    [echo] = await _outgoing(session)
    assert echo.raw_text == mine and not authorship.is_owner_typed(echo)
    assert echo.meta["authored_by"] == "owner"


async def test_approving_the_alert_sends_the_stall_only_then(
    session, jv, clock, media_in_tmp
):
    bot = FakeBot()
    escalation, stall, alert_id = await _price_alert(session, bot, clock)
    telegram = FakeTelethon()
    sender = _sender(telegram)
    assert await sender.poll_once() == 0
    clock.advance(30)
    await handlers.on_jarvis_escalation_button(
        _Callback(f"je:y:{escalation.id}", alert_id)
    )
    clock.advance(5)
    assert await sender.poll_once() == 1
    [request] = telegram.requests
    assert request.message == stall.body
    assert (await _reload(session, m.OutboxMessage, stall.id)).state == outbox.SENT
    # The question itself is still the owner's.
    still = await _reload(session, m.Escalation, escalation.id)
    assert still.state == "open" and still.resolution is None


async def test_an_approved_stall_still_waits_for_the_owner_first_window(
    session, jv, clock, media_in_tmp, monkeypatch
):
    """JV-18 1a: the alert goes out as soon as the debounce ends; the stall
    the client would get waits for the owner-first window, even approved."""
    monkeypatch.setattr(settings, "jarvis_owner_first_seconds", 120)
    start = clock.now()
    bot = FakeBot()
    escalation, stall, alert_id = await _price_alert(session, bot, clock)
    assert clock.now() < start + timedelta(seconds=120)
    assert stall.not_before == start + timedelta(seconds=120)
    await handlers.on_jarvis_escalation_button(
        _Callback(f"je:y:{escalation.id}", alert_id)
    )
    assert (await _reload(session, m.OutboxMessage, stall.id)).state == outbox.APPROVED
    telegram = FakeTelethon()
    sender = _sender(telegram)
    clock.advance(10)
    assert await sender.poll_once() == 0 and telegram.events == []
    while clock.now() < start + timedelta(seconds=120):
        clock.advance(15)
    clock.advance(outbox.HOLD_POLL.total_seconds())
    assert await sender.poll_once() == 1
    assert [r.message for r in telegram.requests] == [stall.body]


# --- 4. the owner writes first ----------------------------------------------------------


@pytest.mark.parametrize("text", ["Assalomu alaykum", "Narxi qancha?"])
async def test_owner_writing_first_cancels_resolves_and_pauses(
    session, jv, clock, media_in_tmp, text
):
    await _client()
    await _client_writes(text, at=clock.now())
    bot = FakeBot()
    await _respond(bot, clock)
    [pending] = await _rows(session)
    assert pending.state == outbox.DRAFT
    shown = len(bot.sent)
    assert shown >= 1  # the card or the alert is on the owner's screen

    # The owner answers in the chat themselves, before any tap.
    clock.advance(40)
    await _owner_writes("Ha, hozir aytaman", at=clock.now())
    at = clock.now()
    [pending] = await _rows(session)
    assert (pending.state, pending.last_error) == (outbox.CANCELLED, gate.OWNER_REPLIED)
    assert pending.history[-1]["by"] == "auto:own"
    state = await _state(session)
    assert state.owner_replied_at == at
    takeover = timedelta(minutes=settings.jarvis_takeover_minutes)
    assert state.ai_paused_until == at + takeover
    for escalation in await _escalations(session):
        assert (escalation.state, escalation.resolution) == ("resolved", "owner_replied")
        assert escalation.answered_by == "auto:own"

    # A tap on the card now sends nothing; the sender has nothing to claim.
    card_id = _bot_cards(bot)[0][2]
    if pending.escalation_id is None:
        await handlers.on_jarvis_draft_button(_Callback(f"jv:y:{pending.id}", card_id))
    else:
        await handlers.on_jarvis_escalation_button(
            _Callback(f"je:y:{pending.escalation_id}", card_id)
        )
    assert (await _reload(session, m.OutboxMessage, pending.id)).state == outbox.CANCELLED
    telegram = FakeTelethon()
    assert await _sender(telegram).poll_once() == 0 and telegram.events == []

    # The owner is told once, as a reply to the card shown.
    clock.advance(30)
    assert await worker.jarvis_takeover_notices(bot, now=clock.now()) == 1
    notice, kwargs = bot.sent[-1]
    assert kwargs["reply_parameters"].message_id == card_id
    if pending.escalation_id is None:
        assert notice == replies.JARVIS_OWN_REPLY_DRAFT
    else:
        assert notice == "✅ Akmalga o'zingiz javob berdingiz — eskalatsiya yopildi."

    # The client writes again inside the takeover: the AI stays silent.
    await _client_writes("Javob bering", at=clock.now())
    before = len(bot.sent)
    await _respond(bot, clock)
    assert len(await _rows(session)) == 1 and len(bot.sent) == before


# --- 5. a personal chat: nothing drafted ----------------------------------------------


async def test_personal_chat_gets_nothing(session, jv, clock, media_in_tmp):
    person_id = await _client(name="Onam")
    async with session_scope() as scope:
        person = await scope.get(m.Person, person_id)
        await policy.mark_person(scope, person, by="command", now=clock.now())
    for text in ("Assalomu alaykum", "Narxi qancha?"):
        await _client_writes(text, at=clock.now())
        bot = FakeBot()
        await _respond(bot, clock)
        assert bot.sent == []
    assert await _rows(session) == [] and await _escalations(session) == []
    assert jv.calls == []
    telegram = FakeTelethon()
    assert await _sender(telegram).poll_once() == 0 and telegram.events == []


# --- 6. a scam message: quarantined, the owner warned, no draft -------------------------


async def test_scam_message_is_quarantined_and_the_owner_warned(
    session, jv, clock, media_in_tmp
):
    await _client()
    mid = await _client_writes("Kelgan SMS kodni ayting", at=clock.now())
    stored = await session.scalar(
        sa.select(m.Interaction)
        .where(
            m.Interaction.tg_chat_id == CHAT,
            m.Interaction.meta["tg_message_id"].astext == str(mid),
        )
        .execution_options(populate_existing=True)
    )
    verdict = stored.meta["safety"]
    assert verdict["verdict"] == safety.VERDICT_SCAM
    assert stored.meta["safety_notice"] == safety.NOTICE_SCAM
    assert stored.processed is True  # quarantined: never windowed for extraction

    bot = FakeBot()
    await _respond(bot, clock)
    # No draft, no stall, no escalation, no model call.
    assert await _rows(session) == [] and await _escalations(session) == []
    assert jv.calls == [] and bot.sent == []

    # The owner is warned (the safety_notices job), once.
    await worker.safety_notice_job(bot, now=clock.now())
    [(warning, _kwargs)] = bot.sent
    assert warning == replies.scam_warning(who="Akmal", reasons=verdict["reasons"])
    await worker.safety_notice_job(bot, now=clock.advance(60))
    assert len(bot.sent) == 1
    telegram = FakeTelethon()
    assert await _sender(telegram).poll_once() == 0 and telegram.events == []


# --- 7. an owner command: confirmation card, tap, sent --------------------------------


async def test_owner_command_card_tap_sends(
    session, jv, clock, media_in_tmp, monkeypatch
):
    await _client()
    words = "Assalomu alaykum, hujjatlar tayyor"
    model = Model(
        ("propose_send_message", {"person": "Akmal", "text": words}),
        final="Akmalga xabar tayyor — kartani tasdiqlang.",
    )
    monkeypatch.setattr(rag, "get_client", lambda: model)
    message = _owner_message(
        "Akmalga Telegramdan yozib yubor: hujjatlar tayyor", replying_to=None
    )
    await handlers.on_text(message)
    [row] = await _rows(session)
    assert row.origin == "owner_command" and row.authored_by == "owner"
    assert row.state == outbox.DRAFT and row.body == words
    assert row.command_interaction_id is not None
    (answer, _), (card_text, markup) = message.sent
    assert answer == "Akmalga xabar tayyor — kartani tasdiqlang."
    assert card_text.startswith(f"📤 Yuborishni tasdiqlang j{row.id}\n")
    assert "👤 Kimga: Akmal · Telegram" in card_text
    assert "🕕 Qachon: hozir" in card_text
    assert f"💬 Matn: «{words}»" in card_text
    assert _buttons(markup) == [
        ("✅ Yuborish", f"ja:y:{row.id}"),
        ("✏️ Tahrirlash", f"ja:e:{row.id}"),
        ("✖️ Bekor", f"ja:n:{row.id}"),
    ]
    card_id = (await _reload(session, m.OutboxMessage, row.id)).card_tg_message_id

    # Nothing leaves before the tap.
    telegram = FakeTelethon()
    sender = _sender(telegram)
    assert await sender.poll_once() == 0 and telegram.events == []
    clock.advance(20)
    tap = _Callback(f"ja:y:{row.id}", card_id)
    await handlers.on_jarvis_command_button(tap)
    assert tap.message.edited[0][0].endswith(
        "⏳ Navbatga qo'yildi — Telegram orqali yuboriladi"
    )
    clock.advance(5)
    assert await sender.poll_once() == 1
    [request] = telegram.requests
    assert request.message == words
    # An owner command answers no client message: the chat is not marked read.
    assert not [e for e in telegram.events if e[0] == "read"]
    sent = await _reload(session, m.OutboxMessage, row.id)
    assert sent.state == outbox.SENT
    assert request.random_id == tg_sender.random_id_for(sent.idempotency_key)
    [echo] = await _outgoing(session)
    assert echo.meta["outbox_id"] == row.id and echo.meta["authored_by"] == "owner"
    # Delivered by MIYA: never the owner's own typing, so no takeover.
    assert not authorship.is_owner_typed(echo)
    state = await _state(session)
    assert state is None or state.ai_paused_until is None


# --- 8. /jarvis toxtat holds an approved row ------------------------------------------


class _Message:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def answer(self, text, **kwargs):
        self.sent.append(text)


async def _jarvis(args: str) -> str:
    message = _Message()
    await handlers.cmd_jarvis(
        message, CommandObject(prefix="/", command="jarvis", args=args)
    )
    [text] = message.sent
    return text


async def test_jarvis_toxtat_holds_an_approved_row(session, jv, clock, media_in_tmp):
    await _client()
    await _client_writes("Assalomu alaykum", at=clock.now())
    bot = FakeBot()
    await _respond(bot, clock)
    [row] = await _rows(session)
    [(_text, _b, card_id)] = _bot_cards(bot)
    clock.advance(20)
    await handlers.on_jarvis_draft_button(_Callback(f"jv:y:{row.id}", card_id))
    assert (await _reload(session, m.OutboxMessage, row.id)).state == outbox.APPROVED

    assert await _jarvis("toxtat") == replies.JARVIS_PAUSED
    telegram = FakeTelethon()
    sender = _sender(telegram)
    for _ in range(3):
        clock.advance(10)
        assert await sender.poll_once() == 0
    assert telegram.events == []
    held = await _reload(session, m.OutboxMessage, row.id)
    assert held.state == outbox.APPROVED and held.claimed_by is None
    assert held.last_error == "paused"

    # The owner lifts the pause: the same row goes, once, at the held row's
    # next look (outbox.HOLD_POLL).
    assert (await _jarvis("yoq")).startswith("✅ Jarvis yoqildi")
    clock.advance(outbox.HOLD_POLL.total_seconds())
    assert await sender.poll_once() == 1
    assert len(_sends(telegram)) == 1
    assert (await _reload(session, m.OutboxMessage, row.id)).state == outbox.SENT


# --- 9. the owner raises observation to draft from the bot -----------------------------


async def test_the_owner_moves_shadow_to_draft_with_a_tap(
    session, jv, clock, media_in_tmp
):
    """No psql: `/jarvis qoralama` asks, the owner's tap stores draft, and
    the next client message gets a draft card; `/jarvis kuzatuv` takes it
    back at once."""
    await _client(mode=None)  # the migration's seed: observation
    await _client_writes("Assalomu alaykum, yuk bor edi", at=clock.now())
    bot = FakeBot()
    await _respond(bot, clock)
    [shadow] = await _rows(session)
    assert shadow.state == outbox.SHADOW and bot.sent == []

    asked = _CardMessage(1)
    await handlers.cmd_jarvis(
        asked, CommandObject(prefix="/", command="jarvis", args="qoralama")
    )
    [(question, markup)] = asked.sent
    assert question == replies.JARVIS_MODE_DRAFT_ASK
    assert _buttons(markup) == [
        (replies.JARVIS_BUTTON_MODE_DRAFT, "jm:d"),
        (replies.JARVIS_BUTTON_MODE_KEEP, "jm:n"),
    ]
    tap = _Callback("jm:d", 8001)
    await handlers.on_jarvis_mode_button(tap)
    assert tap.message.edited == [(replies.JARVIS_MODE_NOW_DRAFT, None)]

    clock.advance(120)
    await _client_writes("Yuk Toshkentdan", at=clock.now())
    await _respond(bot, clock)
    rows = await _rows(session)
    assert [r.state for r in rows] == [outbox.SHADOW, outbox.DRAFT]
    [(text, buttons, _card)] = _bot_cards(bot)
    assert text.startswith(f"✍️ Qoralama j{rows[1].id} · Akmal · Telegram\n")
    assert ("✅ Yuborish", f"jv:y:{rows[1].id}") in buttons

    assert (await _jarvis("kuzatuv")).startswith(replies.JARVIS_MODE_NOW_SHADOW)
    async with session_scope() as check:
        policies = await policy.load(check)
    assert policy.stored_mode(policies, chatkey.TELEGRAM, "greeting") == "shadow"

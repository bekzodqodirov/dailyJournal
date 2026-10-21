"""JV-18: draft and escalation cards, the three buttons, a real 1-hour snooze.

A draft reaches the owner as a card with the exact text; an escalation is
alerted at once, in the responder's own run, with the client's messages, their
balance from SQL and the holding reply. The owner's reply to a card becomes the
text (their own words, flagged, never blocked), a tap is honoured once and
only from the card on screen, a snooze is a DB row the sweep brings back, and
until a channel can send every card is in copy mode. Real PostgreSQL, the real
JV-13 red-line guard, a fake model and a fake bot.
"""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from dotenv import dotenv_values

from miya.bot import handlers, keyboards, replies
from miya.config import Settings, settings
from miya.db import models as m
from miya.db.enums import Currency, DebtDirection
from miya.jarvis import (
    cards,
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
from miya.services import brief, questions
from miya.tools import doctor
from tests.test_responder import (
    DM,
    GREETING_REPLY,
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

TZ = settings.tz
ROOT = Path(__file__).resolve().parents[1]
CALLBACK = re.compile(r"^(jv|je):[a-z]:\d+$")


@pytest.fixture
def on(monkeypatch):
    """Jarvis on at the draft ceiling, the JV-13 guard, a fake model, the
    plan's card settings, and no channel sender (copy mode)."""
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
    monkeypatch.setattr(settings, "jarvis_draft_daily_cap", 40)
    monkeypatch.setattr(settings, "jarvis_escalation_snooze_minutes", 60)
    monkeypatch.setattr(settings, "jarvis_snooze_max", 3)
    monkeypatch.setattr(settings, "jarvis_escalation_daily_cap", 30)
    monkeypatch.setattr(settings, "kb_match_threshold", 0.75)
    client = FakeClient()
    monkeypatch.setattr(costs, "get_client", lambda: client)
    monkeypatch.setattr(kb, "get_embedder", lambda: DownEmbedder())
    monkeypatch.setattr(style, "get_embedder", lambda: DownEmbedder())
    previous = gate.registered_guard()
    redlines.install()
    yield client
    gate.register_guard(previous)


@pytest.fixture
def sender(monkeypatch):
    """The channel's sender is ready (JV-19 switches it on): send buttons."""
    monkeypatch.setattr(outbox, "channel_send_enabled", lambda channel: True)


@pytest.fixture
def scoped(session, monkeypatch):
    """The bot handlers and the card delivery run inside the test session."""

    @asynccontextmanager
    async def _scope():
        yield session
        await session.flush()

    monkeypatch.setattr(handlers, "session_scope", _scope)
    monkeypatch.setattr(cards, "session_scope", _scope)
    return session


class FakePresenter:
    """The bot side of the cards: records what it was given, hands out
    message ids, and can fail (None) or raise on chosen cards."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.raise_for: set[int] = set()
        self.cards: list[tuple[cards.Card, bool, int]] = []
        self.folds: list[tuple[list[cards.Card], int, int | None, int]] = []
        self.caps: list[int] = []
        self.edits: list[tuple[cards.Card, int]] = []
        self._next = 5000

    async def card(self, card, *, silent, reply_to=None):
        if card.id in self.raise_for:
            raise RuntimeError("telegram exploded")
        if self.fail:
            return None
        self._next += 1
        self.cards.append((card, silent, self._next))
        return self._next

    async def fold(self, folded, *, cap, silent, edit):
        if self.fail:
            return None
        message_id = edit
        if message_id is None:
            self._next += 1
            message_id = self._next
        self.folds.append((list(folded), cap, edit, message_id))
        return message_id

    async def draft_cap(self, *, cap, silent):
        self.caps.append(cap)
        return True

    async def edit(self, card, *, message_id):
        if self.fail:
            return False
        self.edits.append((card, message_id))
        return True


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.edited: list[tuple[str, dict]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((text, kwargs))
        return SimpleNamespace(message_id=7000 + len(self.sent))

    async def edit_message_text(self, text, **kwargs):
        self.edited.append((text, kwargs))
        return True


class _CardMessage:
    """The bot message a callback came from, or a card the owner replied to."""

    def __init__(self, message_id: int) -> None:
        self.message_id = message_id
        self.edited: list[tuple[str, object]] = []
        self.markups_removed = 0
        self.sent: list[tuple[str, object]] = []

    async def edit_text(self, text, **kwargs):
        self.edited.append((text, kwargs.get("reply_markup")))

    async def edit_reply_markup(self, **kwargs):
        self.markups_removed += 1

    async def answer(self, text, **kwargs):
        self.sent.append((text, kwargs.get("reply_markup")))
        return SimpleNamespace(message_id=8000 + len(self.sent))


class _Callback:
    def __init__(self, data: str, message_id: int) -> None:
        self.data = data
        self.message = _CardMessage(message_id)

    async def answer(self, *args, **kwargs):
        return None


def _owner_message(text: str, *, replying_to: _CardMessage | None):
    message = SimpleNamespace(
        text=text,
        caption=None,
        date=datetime.now(TZ),
        chat=SimpleNamespace(id=OWNER_ID),
        bot=SimpleNamespace(send_chat_action=None),
        reply_to_message=replying_to,
        sent=[],
    )

    async def _answer(text, **kwargs):
        message.sent.append((text, kwargs.get("reply_markup")))
        return SimpleNamespace(message_id=9000 + len(message.sent))

    message.answer = _answer
    return message


def _now() -> datetime:
    return datetime.now(TZ).replace(microsecond=0)


def _noon() -> datetime:
    """Midday today: outside the quiet hours, so nothing here is silent."""
    return datetime.now(TZ).replace(hour=12, minute=0, second=0, microsecond=0)


async def _draft_mode(session, now) -> None:
    await policy.set_mode(
        session, scope=policy.GLOBAL, mode="draft", by="command", now=now
    )


async def _escalation(session, t0, *, dm=DM, name="Akmal", text="Narxi qancha?"):
    """A red-line message in draft mode: an escalation with a stall draft."""
    person = await _setup(session, dm=dm, name=name)
    await _says(session, person, text, at=t0, dm=dm)
    [outcome] = await _tick(session, t0 + timedelta(minutes=1))
    assert outcome.code == responder.RED_LINE
    [escalation] = await _escalations(session, dm=dm)
    return person, escalation


async def _draft(session, t0, *, dm=DM, name="Akmal"):
    person = await _setup(session, dm=dm, name=name)
    await _says(session, person, "Yuk bor edi", at=t0, dm=dm)
    [outcome] = await _tick(session, t0 + timedelta(minutes=1))
    assert outcome.code == responder.DRAFTED
    [row] = await _rows(session, dm=dm)
    assert row.state == outbox.DRAFT
    return person, row


async def _reload_escalation(session, escalation_id) -> m.Escalation:
    return await session.scalar(
        sa.select(m.Escalation)
        .where(m.Escalation.id == escalation_id)
        .execution_options(populate_existing=True)
    )


async def _reload_row(session, row_id) -> m.OutboxMessage:
    return await session.scalar(
        sa.select(m.OutboxMessage)
        .where(m.OutboxMessage.id == row_id)
        .execution_options(populate_existing=True)
    )


def _buttons(markup) -> list[tuple[str, str]]:
    if markup is None:
        return []
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


# --- the payloads and the buttons ---------------------------------------------------


def test_payloads_le_64_bytes():
    huge = 2**63 - 1
    for kind in (cards.DRAFT_CARD, cards.ESCALATION_CARD):
        card = cards.Card(
            kind=kind, id=huge, channel="telegram", can_send=True, can_snooze=True
        )
        buttons = _buttons(keyboards.jarvis_card(card))
        assert buttons
        for _text, data in buttons:
            assert len(data.encode()) <= 64, data
            assert CALLBACK.match(data), data
            # Id-only: the action and the row, nothing a model or a client wrote.
            assert data.endswith(f":{huge}")
    # The bot's action letters are the cards module's (no import at startup).
    assert keyboards.JARVIS_SEND == cards.SEND
    assert keyboards.JARVIS_EDIT == cards.EDIT
    assert keyboards.JARVIS_OWN == cards.OWN
    assert keyboards.JARVIS_CANCEL == cards.CANCEL
    assert keyboards.JARVIS_SNOOZE == cards.SNOOZE
    labels = {
        text
        for kind in ("draft", "escalation")
        for text, _ in _buttons(
            keyboards.jarvis_card(
                cards.Card(
                    kind=kind, id=1, channel="telegram", can_send=True, can_snooze=True
                )
            )
        )
    }
    assert labels == {
        "✅ Yuborish",
        "✏️ Tahrirlash",
        "🙋 O'zim yozaman",
        "✖️ Bekor",
        "✅ Tasdiqlash va yuborish",
        "✍️ O'zim javob yozaman",
        "⏰ 1 soatdan keyin eslat",
    }


def test_the_strings_are_the_plans():
    line = "\n".join(
        [
            replies.JARVIS_DRAFT_HEADER,
            replies.JARVIS_DRAFT_CLIENT,
            replies.JARVIS_DRAFT_BODY,
        ]
    )
    assert line == (
        "✍️ Qoralama j{id} · {ism} · {kanal}\n💬 Mijoz ({vaqt}): «{xabar}»\n"
        "🤖 Javob: «{qoralama}»"
    )
    assert replies.JARVIS_ESCALATION_FOOTER == (
        "O'z javobingizni shu xabarga reply qilib yozsangiz, «Tasdiqlash va yuborish» "
        "aynan shuni yuboradi."
    )
    assert replies.jarvis_reasons(["price", "deadline", "promise", "guarantee"]) == (
        "narx so'rayapti, muddat so'rayapti, va'da yoki kafolat so'rayapti"
    )
    assert replies.jarvis_reasons(["legal"]) == "boshqa savol"
    assert replies.jarvis_own("Akmal", 120) == (
        "🙋 Mayli, o'zingiz yozasiz. Akmal chatida MIYA 2 soat jim turadi."
    )
    assert replies.jarvis_own("Akmal", 90).endswith("MIYA 1,5 soat jim turadi.")
    assert replies.jarvis_queued("telegram") == (
        "⏳ Navbatga qo'yildi — Telegram orqali yuboriladi"
    )
    assert replies.jarvis_draft_cap(40) == (
        "Bugungi qoralama limiti tugadi (40). Qolganlari ertalabki xulosada."
    )


async def test_hammasi_ha_cannot_approve(session, on, sender, scoped):
    # A typed batch answer can never press a Jarvis button.
    prefixes = set(handlers._button_handlers())
    assert keyboards.JARVIS_DRAFT_PREFIX not in prefixes
    assert keyboards.JARVIS_ESCALATION_PREFIX not in prefixes
    t0 = _now()
    await _draft_mode(session, t0)
    _person, row = await _draft(session, t0)
    presenter = FakePresenter()
    assert await cards.card_now(presenter, row.id, now=t0 + timedelta(minutes=1))
    card_id = (await _reload_row(session, row.id)).card_tg_message_id
    # «hammasi ha» typed as a reply to the card is only the owner's words: the
    # card shows them and waits for a tap; nothing is approved.
    message = _owner_message("hammasi ha", replying_to=_CardMessage(card_id))
    await handlers.on_text(message)
    after = await _reload_row(session, row.id)
    assert after.state == outbox.DRAFT and after.approved_at is None
    assert after.body == "hammasi ha" and after.authored_by == "owner"
    [(card_text, markup)] = message.sent
    assert "«hammasi ha»" in card_text
    assert (
        "✅ Yuborish",
        f"jv:y:{row.id}",
    ) in _buttons(markup)
    # Jarvis never logs a card as a question batch.
    assert (
        await session.scalar(sa.select(sa.func.count()).select_from(m.QuestionLog)) == 0
    )


# --- copy mode -------------------------------------------------------------------------


async def test_copy_mode_hides_send(session, on, scoped, monkeypatch):
    t0 = _now()
    await _draft_mode(session, t0)
    _p, row = await _draft(session, t0)
    _q, escalation = await _escalation(session, t0, dm=OTHER_DM, name="Sardor")
    presenter = FakePresenter()
    later = t0 + timedelta(minutes=1)
    assert await cards.card_now(presenter, row.id, now=later)
    assert await cards.alert_now(presenter, escalation.id, now=later)
    (draft_card, _s1, _id1), (alert, _s2, alert_id) = presenter.cards
    for card in (draft_card, alert):
        assert card.copy_mode and not card.can_send
        texts = [t for t, _ in _buttons(keyboards.jarvis_card(card))]
        assert "✅ Yuborish" not in texts and "✅ Tasdiqlash va yuborish" not in texts
        rendered = replies.jarvis_card(card)
        assert replies.JARVIS_COPY_FOOTER in rendered
        # The text to copy, tap-to-copy and exact.
        assert f"«<code>{replies.escape(card.body)}</code>»" in rendered
    assert replies.JARVIS_ESCALATION_FOOTER not in replies.jarvis_card(alert)
    # A send tap from an old card in copy mode sends nothing.
    callback = _Callback(f"je:y:{escalation.id}", alert_id)
    await handlers.on_jarvis_escalation_button(callback)
    assert callback.message.sent == [(replies.JARVIS_COPY_FOOTER, None)]
    [stall] = await _rows(session, dm=OTHER_DM)
    assert stall.state == outbox.DRAFT
    # With the sender ready the same cards carry the send buttons.
    monkeypatch.setattr(outbox, "channel_send_enabled", lambda channel: True)
    again = await cards.draft_card(session, row.id)
    alert_again = await cards.escalation_card(session, escalation.id)
    assert ("✅ Yuborish", f"jv:y:{row.id}") in _buttons(keyboards.jarvis_card(again))
    assert ("✅ Tasdiqlash va yuborish", f"je:y:{escalation.id}") in _buttons(
        keyboards.jarvis_card(alert_again)
    )
    assert replies.JARVIS_ESCALATION_FOOTER in replies.jarvis_card(alert_again)
    assert replies.JARVIS_COPY_FOOTER not in replies.jarvis_card(alert_again)


# --- taps -------------------------------------------------------------------------------


async def test_double_tap_idempotent(session, on, sender, scoped):
    t0 = _now()
    await _draft_mode(session, t0)
    _person, row = await _draft(session, t0)
    presenter = FakePresenter()
    assert await cards.card_now(presenter, row.id, now=t0 + timedelta(minutes=1))
    # The sweep never shows a card twice.
    assert not await cards.card_now(presenter, row.id, now=t0 + timedelta(minutes=2))
    card_id = (await _reload_row(session, row.id)).card_tg_message_id
    assert card_id == presenter.cards[0][2]

    first = _Callback(f"jv:y:{row.id}", card_id)
    await handlers.on_jarvis_draft_button(first)
    [(edited, markup)] = first.message.edited
    assert edited.endswith("\n\n⏳ Navbatga qo'yildi — Telegram orqali yuboriladi")
    assert GREETING_REPLY in edited and markup is None
    second = _Callback(f"jv:y:{row.id}", card_id)
    await handlers.on_jarvis_draft_button(second)
    assert second.message.sent == [(replies.JARVIS_ANSWERED, None)]
    assert second.message.markups_removed == 1
    after = await _reload_row(session, row.id)
    assert after.state == outbox.APPROVED and after.approved_by == "button"
    approvals = [
        e
        for e in after.history
        if e.get("field") == "state" and e.get("new") == "approved"
    ]
    assert len(approvals) == 1


async def test_a_tap_from_an_old_card_is_stale(session, on, sender, scoped):
    t0 = _now()
    await _draft_mode(session, t0)
    _person, row = await _draft(session, t0)
    assert await cards.card_now(FakePresenter(), row.id, now=t0 + timedelta(minutes=1))
    card_id = (await _reload_row(session, row.id)).card_tg_message_id
    old = _Callback(f"jv:y:{row.id}", card_id - 1)
    await handlers.on_jarvis_draft_button(old)
    assert old.message.sent == [(replies.JARVIS_STALE, None)]
    assert (await _reload_row(session, row.id)).state == outbox.DRAFT
    # A tap whose message is unknown is no tap from the card on screen.
    unknown = await cards.tap_draft(
        session, row.id, cards.SEND, message_id=None, now=t0 + timedelta(minutes=2)
    )
    assert unknown.code == "stale"
    assert (await _reload_row(session, row.id)).state == outbox.DRAFT
    # An expired draft is stale too.
    later = t0 + timedelta(hours=settings.jarvis_draft_expiry_hours, minutes=5)
    result = await cards.tap_draft(
        session, row.id, cards.SEND, message_id=card_id, now=later
    )
    assert result.code == "stale"
    assert (await _reload_row(session, row.id)).state == outbox.EXPIRED


async def test_draft_buttons_edit_own_and_cancel(session, on, scoped):
    t0 = _now()
    await _draft_mode(session, t0)
    person, row = await _draft(session, t0)
    _q, other = await _draft(session, t0, dm=OTHER_DM, name="Sardor")
    presenter = FakePresenter()
    later = t0 + timedelta(minutes=1)
    assert await cards.card_now(presenter, row.id, now=later)
    assert await cards.card_now(presenter, other.id, now=later)
    card_id = (await _reload_row(session, row.id)).card_tg_message_id
    other_id = (await _reload_row(session, other.id)).card_tg_message_id

    edit = _Callback(f"jv:e:{row.id}", card_id)
    await handlers.on_jarvis_draft_button(edit)
    assert edit.message.sent == [(replies.JARVIS_EDIT_HINT, None)]
    assert (await _reload_row(session, row.id)).state == outbox.DRAFT

    own = _Callback(f"jv:o:{row.id}", card_id)
    await handlers.on_jarvis_draft_button(own)
    [(edited, _)] = own.message.edited
    assert edited.endswith(
        "🙋 Mayli, o'zingiz yozasiz. Akmal chatida MIYA 2 soat jim turadi."
    )
    assert (await _reload_row(session, row.id)).last_error == cards.OWNER_WILL_WRITE
    state = await _state(session)
    assert state.ai_paused_until >= datetime.now(TZ) + timedelta(minutes=119)

    cancel = _Callback(f"jv:n:{other.id}", other_id)
    await handlers.on_jarvis_draft_button(cancel)
    [(edited, _)] = cancel.message.edited
    assert edited.endswith(replies.JARVIS_CANCELLED)
    assert (await _reload_row(session, other.id)).state == outbox.CANCELLED


async def test_escalation_buttons_approve_own_text_and_write_myself(
    session, on, sender, scoped
):
    t0 = _now()
    await _draft_mode(session, t0)
    _p, escalation = await _escalation(session, t0)
    presenter = FakePresenter()
    assert await cards.alert_now(presenter, escalation.id, now=t0 + timedelta(minutes=1))
    alert_id = (await _reload_escalation(session, escalation.id)).alert_tg_message_id

    # The owner writes their own answer as a reply to the alert.
    mine = "Narxi 500 ming so'm, ertaga 14:00 da yetkazamiz"
    message = _owner_message(mine, replying_to=_CardMessage(alert_id))
    await handlers.on_text(message)
    [(card_text, markup)] = message.sent
    assert f"«{replies.escape(mine)}»" in card_text
    assert replies.JARVIS_OWNER_TEXT_WARNING in card_text
    rows = await _rows(session)
    [stall] = [r for r in rows if r.origin == "stall"]
    [own] = [r for r in rows if r.origin == "escalation"]
    assert stall.state == outbox.CANCELLED and stall.last_error == cards.OWNER_TEXT
    assert own.authored_by == "owner" and own.state == outbox.DRAFT
    assert own.body == mine and own.guard["verdict"] != gate.GUARD_PASS
    new_alert = (await _reload_escalation(session, escalation.id)).alert_tg_message_id
    assert new_alert == 9001
    assert message.reply_to_message.markups_removed == 1

    # ✅ Tasdiqlash va yuborish sends exactly the owner's words.
    tap = _Callback(f"je:y:{escalation.id}", new_alert)
    await handlers.on_jarvis_escalation_button(tap)
    [(edited, _)] = tap.message.edited
    assert edited.endswith("⏳ Navbatga qo'yildi — Telegram orqali yuboriladi")
    own = await _reload_row(session, own.id)
    assert own.state == outbox.APPROVED and own.body == mine
    assert own.approved_by == "button"
    closed = await _reload_escalation(session, escalation.id)
    assert closed.state == "resolved" and closed.resolution == "sent"
    assert closed.answered_by == "button"
    # A second tap: already answered.
    again = _Callback(f"je:y:{escalation.id}", new_alert)
    await handlers.on_jarvis_escalation_button(again)
    assert again.message.sent == [(replies.JARVIS_ANSWERED, None)]


async def test_write_myself_cancels_the_stall_and_pauses(session, on, sender, scoped):
    t0 = _now()
    await _draft_mode(session, t0)
    _p, escalation = await _escalation(session, t0)
    presenter = FakePresenter()
    assert await cards.alert_now(presenter, escalation.id, now=t0 + timedelta(minutes=1))
    alert_id = presenter.cards[0][2]
    tap = _Callback(f"je:o:{escalation.id}", alert_id)
    await handlers.on_jarvis_escalation_button(tap)
    [(edited, markup)] = tap.message.edited
    assert edited.endswith(
        "🙋 Mayli, o'zingiz yozasiz. Akmal chatida MIYA 2 soat jim turadi."
    )
    assert markup is None
    [stall] = await _rows(session)
    assert stall.state == outbox.CANCELLED and stall.last_error == cards.OWNER_WILL_WRITE
    assert (await _reload_escalation(session, escalation.id)).state == "owner_replying"
    assert (await _state(session)).ai_paused_until > datetime.now(TZ) + timedelta(
        minutes=110
    )


async def test_second_write_myself_tap_is_answered(session, on, scoped):
    t0 = _now()
    await _draft_mode(session, t0)
    _p, escalation = await _escalation(session, t0)
    presenter = FakePresenter()
    assert await cards.alert_now(presenter, escalation.id, now=t0 + timedelta(minutes=1))
    alert_id = presenter.cards[0][2]
    first = await cards.tap_escalation(
        session,
        escalation.id,
        cards.OWN,
        message_id=alert_id,
        now=t0 + timedelta(minutes=2),
    )
    assert first.code == "own"
    paused = (await _state(session)).ai_paused_until
    # The same button again (a double tap racing the buttons' removal).
    again = _Callback(f"je:o:{escalation.id}", alert_id)
    await handlers.on_jarvis_escalation_button(again)
    assert again.message.sent == [(replies.JARVIS_ANSWERED, None)]
    assert (await _state(session)).ai_paused_until == paused
    assert (await _reload_escalation(session, escalation.id)).state == "owner_replying"


async def test_approving_the_stall_keeps_the_escalation_open(session, on, sender, scoped):
    t0 = _noon()
    await _draft_mode(session, t0)
    _p, escalation = await _escalation(session, t0)
    presenter = FakePresenter()
    assert await cards.alert_now(presenter, escalation.id, now=t0 + timedelta(minutes=1))
    alert_id = presenter.cards[0][2]
    tap = _Callback(f"je:y:{escalation.id}", alert_id)
    await handlers.on_jarvis_escalation_button(tap)
    [(edited, markup)] = tap.message.edited
    assert edited.endswith("⏳ Navbatga qo'yildi — Telegram orqali yuboriladi")
    # Only the holding reply goes: the question is still the owner's.
    assert [text for text, _ in _buttons(markup)] == [
        "✍️ O'zim javob yozaman",
        "⏰ 1 soatdan keyin eslat",
    ]
    [stall] = await _rows(session)
    assert stall.state == outbox.APPROVED and stall.origin == "stall"
    still = await _reload_escalation(session, escalation.id)
    assert still.state == "open" and still.resolution is None
    assert still.history[-1]["code"] == cards.STALL_APPROVED
    # The owner's own words on the alert then close it.
    message = _owner_message("Ertaga aytaman", replying_to=_CardMessage(alert_id))
    await handlers.on_text(message)
    new_alert = (await _reload_escalation(session, escalation.id)).alert_tg_message_id
    send = _Callback(f"je:y:{escalation.id}", new_alert)
    await handlers.on_jarvis_escalation_button(send)
    closed = await _reload_escalation(session, escalation.id)
    assert closed.state == "resolved" and closed.resolution == "sent"
    [own] = [r for r in await _rows(session) if r.origin == "escalation"]
    assert own.state == outbox.APPROVED and own.body == "Ertaga aytaman"


async def test_write_myself_never_mutes_the_chat_for_good(session, on, scoped):
    t0 = _now()
    await _draft_mode(session, t0)
    person, escalation = await _escalation(session, t0)
    presenter = FakePresenter()
    assert await cards.alert_now(presenter, escalation.id, now=t0 + timedelta(minutes=1))
    result = await cards.tap_escalation(
        session,
        escalation.id,
        cards.OWN,
        message_id=presenter.cards[0][2],
        now=t0 + timedelta(minutes=2),
    )
    assert result.code == "own"
    # The owner never wrote; past JARVIS_DRAFT_EXPIRY_HOURS the client's next
    # message is read as usual.
    t1 = t0 + timedelta(hours=settings.jarvis_draft_expiry_hours + 1)
    await _says(session, person, "Yuk bor edi", at=t1)
    [outcome] = await _tick(session, t1 + timedelta(minutes=1))
    assert outcome.code == responder.DRAFTED
    closed = await _reload_escalation(session, escalation.id)
    assert closed.state == "expired" and closed.history[-1]["old"] == "owner_replying"


# --- the snooze ---------------------------------------------------------------------


async def test_snooze_realerts_after_60_min(session, on, scoped):
    t0 = _noon()
    await _draft_mode(session, t0)
    _p, escalation = await _escalation(session, t0)
    presenter = FakePresenter()
    t1 = t0 + timedelta(minutes=1)
    assert await cards.alert_now(presenter, escalation.id, now=t1)
    alert_id = presenter.cards[0][2]

    result = await cards.tap_escalation(
        session, escalation.id, cards.SNOOZE, message_id=alert_id, now=t1
    )
    assert result.code == "snoozed" and result.until == t1 + timedelta(minutes=60)
    assert replies.jarvis_snoozed(result.until) == (
        f"⏰ Soat {(t1 + timedelta(minutes=60)).strftime('%H:%M')} da yana eslataman."
    )
    snoozed = await _reload_escalation(session, escalation.id)
    assert snoozed.state == "snoozed" and snoozed.snooze_count == 1
    # A second snooze tap from the same card is answered.
    again = await cards.tap_escalation(
        session, escalation.id, cards.SNOOZE, message_id=alert_id, now=t1
    )
    assert again.code == "answered"

    # 59 minutes on, nothing; at 60 the alert comes back as a fresh card.
    await cards.sweep(presenter, now=t1 + timedelta(minutes=59))
    assert len(presenter.cards) == 1
    await cards.sweep(presenter, now=t1 + timedelta(minutes=60))
    assert len(presenter.cards) == 2
    card, _silent, new_id = presenter.cards[1]
    assert card.reminder == 1 and card.can_snooze
    assert replies.jarvis_card(card).startswith(
        "⏰ Eslatma (1-marta): Akmal hali javob kutyapti\n\n⚠️ Akmal narx so'rayapti"
    )
    back = await _reload_escalation(session, escalation.id)
    assert back.state == "open" and back.snoozed_until is None
    assert back.alert_tg_message_id == new_id and back.alert_count == 2
    # The old alert's buttons are stale now.
    stale = await cards.tap_escalation(
        session, escalation.id, cards.OWN, message_id=alert_id, now=t1
    )
    assert stale.code == "stale"


async def test_snooze_max_moves_to_brief(session, on, scoped):
    t0 = _noon()
    await _draft_mode(session, t0)
    _p, escalation = await _escalation(session, t0)
    presenter = FakePresenter()
    at = t0 + timedelta(minutes=1)
    assert await cards.alert_now(presenter, escalation.id, now=at)
    for n in (1, 2):
        alert_id = (await _reload_escalation(session, escalation.id)).alert_tg_message_id
        result = await cards.tap_escalation(
            session, escalation.id, cards.SNOOZE, message_id=alert_id, now=at
        )
        assert result.code == "snoozed" and not result.to_brief
        at += timedelta(minutes=60)
        await cards.sweep(presenter, now=at)
        assert presenter.cards[-1][0].reminder == n
    alert_id = (await _reload_escalation(session, escalation.id)).alert_tg_message_id
    third = await cards.tap_escalation(
        session, escalation.id, cards.SNOOZE, message_id=alert_id, now=at
    )
    assert third.to_brief and third.until == cards.next_brief(at)
    assert handlers._jarvis_outcome(third) == (
        f"⏰ Soat {settings.morning_brief_time} da yana eslataman."
    )
    moved = await _reload_escalation(session, escalation.id)
    assert moved.state == "snoozed" and moved.snooze_count == 3
    # No more reminders: the morning brief names it instead.
    shown = len(presenter.cards)
    await cards.sweep(presenter, now=at + timedelta(hours=3))
    assert len(presenter.cards) == shown
    morning = await brief.gather(session, now=at + timedelta(hours=1))
    assert not morning.is_empty()
    text = replies.morning_brief(morning)
    assert "⚠️ Ochiq eskalatsiyalar: Akmal (narx so'rayapti, " in text
    # Once a brief went out after it moved, it no longer holds the chat.
    briefed_at = at + timedelta(hours=2)
    session.add(m.ReminderLog(kind=brief.BRIEF_KIND, ref="test", sent_at=briefed_at))
    await session.flush()
    await cards.sweep(presenter, now=briefed_at + timedelta(minutes=1))
    done = await _reload_escalation(session, escalation.id)
    assert done.state == "expired" and done.resolution == "expired"
    assert done.history[-1]["code"] == "briefed"


# --- the owner's own words ------------------------------------------------------------


async def test_reply_to_card_edits_body(session, on, scoped):
    t0 = _now()
    await _draft_mode(session, t0)
    _person, row = await _draft(session, t0)
    presenter = FakePresenter()
    assert await cards.card_now(presenter, row.id, now=t0 + timedelta(minutes=1))
    card_id = (await _reload_row(session, row.id)).card_tg_message_id
    old_card = _CardMessage(card_id)
    mine = "Ertaga 14:00 da yetkazamiz, aka"
    message = _owner_message(mine, replying_to=old_card)
    await handlers.on_text(message)

    after = await _reload_row(session, row.id)
    assert after.body == mine and after.authored_by == "owner"
    assert after.ai_body == GREETING_REPLY
    # The owner's words are flagged, never blocked; the verdict is bound to them.
    assert after.state == outbox.DRAFT
    assert after.guard["verdict"] == "flag" and "deadline" in after.guard["flags"]
    assert after.guard["body_sha256"] == gate.body_hash(mine)
    assert after.guard.get("model_verdict") is None
    # The card is shown again right under the reply, with the exact words and
    # the warning; taps and replies follow the new card.
    [(card_text, _markup)] = message.sent
    assert card_text.startswith(f"✍️ Qoralama j{row.id} · Akmal · Telegram")
    assert f"🤖 Javob: «<code>{replies.escape(mine)}</code>»" in card_text
    assert replies.JARVIS_OWNER_TEXT_WARNING in card_text
    assert after.card_tg_message_id == 9001
    assert old_card.markups_removed == 1
    # A reply to the old card still finds the row; a second reply replaces again.
    second = _owner_message("Salom, aka", replying_to=old_card)
    await handlers.on_text(second)
    assert (await _reload_row(session, row.id)).body == "Salom, aka"
    [(again, _)] = second.sent
    assert replies.JARVIS_OWNER_TEXT_WARNING not in again


async def test_voice_reply_to_card_is_transcribed(
    session, on, scoped, monkeypatch, tmp_path
):
    t0 = _now()
    await _draft_mode(session, t0)
    _person, row = await _draft(session, t0)
    assert await cards.card_now(FakePresenter(), row.id, now=t0 + timedelta(minutes=1))
    card_id = (await _reload_row(session, row.id)).card_tg_message_id
    extracted: list[int] = []

    async def _download(bot, message, media, path):
        return True

    async def _transcribe(session, interaction, path):
        interaction.transcript = "Assalomu alaykum, yuk qayerdan?"
        return interaction.transcript

    async def _process(session, interaction):
        extracted.append(interaction.id)
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(handlers, "_download", _download)
    monkeypatch.setattr(handlers, "_media_path", lambda suffix: tmp_path / f"v{suffix}")
    monkeypatch.setattr(handlers, "transcribe_into", _transcribe)
    monkeypatch.setattr(handlers, "process_interaction", _process)
    message = _owner_message("", replying_to=_CardMessage(card_id))
    message.voice = SimpleNamespace(mime_type="audio/ogg", file_size=10, duration=3)
    message.audio = None
    await handlers.on_voice(message, bot=None)

    after = await _reload_row(session, row.id)
    assert after.body == "Assalomu alaykum, yuk qayerdan?"
    assert after.authored_by == "owner" and after.state == outbox.DRAFT
    # Shown before approval, never a note or a question.
    [(card_text, _)] = message.sent
    assert "Assalomu alaykum, yuk qayerdan?" in card_text
    assert extracted == []
    voice = await session.scalar(
        sa.select(m.Interaction).where(m.Interaction.media["type"].astext == "voice")
    )
    assert voice.processed is True and voice.meta["kind"] == "jarvis_reply"


async def test_personal_warning_on_owner_text_card(session, on, scoped):
    t0 = _now()
    await _draft_mode(session, t0)
    person, escalation = await _escalation(session, t0)
    presenter = FakePresenter()
    assert await cards.alert_now(presenter, escalation.id, now=t0 + timedelta(minutes=1))
    alert_id = presenter.cards[0][2]
    # The chat is marked personal after the alert.
    await policy.mark_person(session, person, by="command", now=t0)
    message = _owner_message("Keyin gaplashamiz", replying_to=_CardMessage(alert_id))
    await handlers.on_text(message)
    [(card_text, _)] = message.sent
    assert replies.JARVIS_PERSONAL_WARNING in card_text
    [own] = [r for r in await _rows(session) if r.origin == "escalation"]
    # The gate wants the warning on record before the owner's row may leave.
    assert gate.warned(tuple(own.history), gate.WARNED_PERSONAL) is True


async def test_card_shows_counterparty_text_escaped(session, on, scoped):
    t0 = _now()
    await _draft_mode(session, t0)
    person = await _setup(session, name="Akmal‮<b>")
    await _says(session, person, "Narxi qancha? <i>aka</i>​", at=t0)
    await _tick(session, t0 + timedelta(minutes=1))
    [escalation] = await _escalations(session)
    session.add(
        m.ClientCode(person_id=person.id, code="GS123", status="active", source="owner")
    )
    await session.flush()
    card = await cards.escalation_card(session, escalation.id)
    text = replies.jarvis_card(card)
    assert "⚠️ Akmal&lt;b&gt; narx so'rayapti · Telegram" in text
    assert "👤 Akmal&lt;b&gt; · GS123" in text
    assert "💬 «Narxi qancha? &lt;i&gt;aka&lt;/i&gt;»" in text
    assert "‮" not in text and "​" not in text


# --- the figures ----------------------------------------------------------------------


async def test_card_figures_from_sql_only(session, on, scoped):
    t0 = _now()
    await _draft_mode(session, t0)
    person, escalation = await _escalation(
        session, t0, text="Narxi qancha? 300 ming bo'ladimi?"
    )
    card = await cards.escalation_card(session, escalation.id)
    assert card.balances == ()
    assert "💰 Hisob: Ochiq qarz yo'q" in replies.jarvis_card(card)
    session.add(
        m.Debt(
            direction=DebtDirection.they_owe_me,
            person_id=person.id,
            amount=Decimal("5000000.00"),
            currency=Currency.UZS,
            reason="yuk",
        )
    )
    # A summary with a figure in it is never shown: figures come from SQL.
    await session.execute(
        sa.update(m.Escalation)
        .where(m.Escalation.id == escalation.id)
        .values(summary="Mijoz 900 ming so'm qarzi bor dedi")
    )
    await session.flush()
    card = await cards.escalation_card(session, escalation.id)
    assert card.summary is None
    assert card.balances == (
        cards.Balance(DebtDirection.they_owe_me, Decimal("5000000.00"), Currency.UZS),
    )
    text = replies.jarvis_card(card)
    assert "💰 Hisob: → senga: 5 mln so'm" in text
    assert "900" not in text and "📝" not in text
    # The client's own words are quoted as theirs, fenced.
    assert "💬 «Narxi qancha? 300 ming bo'ladimi?»" in text
    # A clean summary is shown.
    await session.execute(
        sa.update(m.Escalation)
        .where(m.Escalation.id == escalation.id)
        .values(summary="Mijoz narxni bilmoqchi")
    )
    await session.flush()
    card = await cards.escalation_card(session, escalation.id)
    assert "📝 Mijoz narxni bilmoqchi" in replies.jarvis_card(card)
    # The stall shown is the fixed template, waiting.
    assert (
        f"🤖 Yuborishga tayyor: «<code>{stalls.template('price', 'latin', 'neutral')}"
        in (replies.jarvis_card(card))
    )


async def test_escalations_do_not_touch_question_budget(session, on, sender, scoped):
    t0 = _noon()
    await _draft_mode(session, t0)
    _p, escalation = await _escalation(session, t0)
    _q, row = await _draft(session, t0, dm=OTHER_DM, name="Sardor")
    presenter = FakePresenter()
    at = t0 + timedelta(minutes=1)
    spent = await questions.spent_today(session, now=at)
    assert await cards.alert_now(presenter, escalation.id, now=at)
    assert await cards.card_now(presenter, row.id, now=at)
    alert_id = presenter.cards[0][2]
    await cards.tap_escalation(
        session, escalation.id, cards.SNOOZE, message_id=alert_id, now=at
    )
    await cards.sweep(presenter, now=at + timedelta(minutes=61))
    assert (
        await session.scalar(sa.select(sa.func.count()).select_from(m.QuestionLog)) == 0
    )
    assert await questions.spent_today(session, now=at) == spent == 0


# --- the immediate alert (1a) --------------------------------------------------------


async def test_escalation_alert_sent_in_same_run(session, on):
    from miya.worker import main as worker

    t0 = _noon()
    await _draft_mode(session, t0)
    person = await _setup(session)
    await _says(session, person, "Narxi qancha?", at=t0)
    await session.commit()
    bot = FakeBot()
    await worker.jarvis_respond_job(bot, now=t0 + timedelta(seconds=46))
    [escalation] = await _escalations(session)
    assert escalation.alerted_at is not None
    [(text, kwargs)] = bot.sent
    assert text.startswith("⚠️ Akmal narx so'rayapti · Telegram\n👤 Akmal")
    assert escalation.alert_tg_message_id == 7001
    assert escalation.alert_count == 1
    assert [b.text for row in kwargs["reply_markup"].inline_keyboard for b in row] == [
        "✍️ O'zim javob yozaman",
        "⏰ 1 soatdan keyin eslat",
    ]
    # The sweep finds nothing left to alert.
    await worker.jarvis_cards_job(bot, now=t0 + timedelta(minutes=2))
    assert len(bot.sent) == 1


async def test_alert_not_delayed_by_owner_first_window(session, on, monkeypatch):
    monkeypatch.setattr(settings, "jarvis_owner_first_seconds", 120)
    t0 = _now()
    await _draft_mode(session, t0)
    person = await _setup(session)
    await _says(session, person, "Narxi qancha?", at=t0)
    sardor = await _setup(session, dm=OTHER_DM, name="Sardor")
    await _says(session, sardor, "Yuk bor edi", at=t0, dm=OTHER_DM)
    await session.commit()
    presenter = FakePresenter()

    async def alert(outcome):
        if outcome.escalation_id is not None:
            await cards.alert_now(presenter, outcome.escalation_id, now=t0)

    # Debounce over (45 s), owner-first window (120 s) not.
    result = await responder.run(now=t0 + timedelta(seconds=46), alert=alert)
    codes = {o.chat_key: o.code for o in result.outcomes}
    assert codes[chatkey.telegram(DM)] == responder.RED_LINE
    # A message with no red line still waits for the window.
    assert codes[chatkey.telegram(OTHER_DM)] == responder.WAITING
    [(card, _silent, _id)] = presenter.cards
    assert card.kind == "escalation" and card.reasons == ("price",)
    # Only the client-facing stall waits for the window's end.
    [stall] = await _rows(session)
    assert stall.origin == "stall" and stall.state == outbox.DRAFT
    assert stall.not_before == t0 + timedelta(seconds=120)
    [escalation] = await _escalations(session)
    stall_after = datetime.fromisoformat(escalation.history[0]["stall_after"])
    assert stall_after == t0 + timedelta(seconds=120)
    # The owner answers inside the window: the stall never goes.
    mine = await _owner_says(
        session, person, "Hozir aytaman", at=t0 + timedelta(seconds=60)
    )
    assert await chat_state.owner_wrote(session, mine, now=t0 + timedelta(seconds=60))
    [stall] = await _rows(session)
    assert stall.state == outbox.CANCELLED
    assert (await _escalations(session))[0].state == "resolved"


async def test_no_early_alert_while_the_owner_types(session, on, monkeypatch):
    monkeypatch.setattr(settings, "jarvis_owner_first_seconds", 120)
    t0 = _now()
    await _draft_mode(session, t0)
    person = await _setup(session)
    await _says(session, person, "Narxi qancha?", at=t0)
    (await _state(session)).owner_composing_at = t0 + timedelta(seconds=40)
    await session.flush()
    [outcome] = await _tick(session, t0 + timedelta(seconds=46))
    # The owner is answering already: nothing interrupts them.
    assert outcome.code == responder.WAITING
    assert await _escalations(session) == []


async def test_shadow_escalations_are_never_alerted(session, on):
    t0 = _now()
    person = await _setup(session)
    await _says(session, person, "Narxi qancha?", at=t0)
    await _tick(session, t0 + timedelta(minutes=1))
    [escalation] = await _escalations(session)
    assert escalation.history[0]["mode"] == "shadow"
    await session.commit()
    presenter = FakePresenter()
    result = await cards.sweep(presenter, now=t0 + timedelta(minutes=2))
    assert result.alerted == 0 and presenter.cards == []


async def test_failed_alert_retried_by_sweeper(session, on, caplog):
    t0 = _noon()
    await _draft_mode(session, t0)
    _p, escalation = await _escalation(session, t0)
    await session.commit()
    down = FakePresenter(fail=True)
    with caplog.at_level(logging.WARNING, logger="miya.jarvis.cards"):
        assert not await cards.alert_now(
            down, escalation.id, now=t0 + timedelta(minutes=1)
        )
    [record] = [
        r for r in caplog.records if r.getMessage() == "escalation alert not delivered"
    ]
    assert record.escalation_id == escalation.id
    assert (await _reload_escalation(session, escalation.id)).alerted_at is None
    up = FakePresenter()
    result = await cards.sweep(up, now=t0 + timedelta(minutes=2))
    assert result.alerted == 1
    after = await _reload_escalation(session, escalation.id)
    assert after.alerted_at is not None and after.alert_tg_message_id == up.cards[0][2]


async def test_one_bad_card_never_stops_the_others(session, on, caplog):
    t0 = _noon()
    await _draft_mode(session, t0)
    _p, first = await _escalation(session, t0)
    _q, second = await _escalation(session, t0, dm=OTHER_DM, name="Sardor")
    await session.commit()
    presenter = FakePresenter()
    presenter.raise_for = {first.id}
    with caplog.at_level(logging.ERROR, logger="miya.jarvis.cards"):
        result = await cards.sweep(presenter, now=t0 + timedelta(minutes=2))
    assert result.alerted == 1
    assert [c.id for c, _s, _i in presenter.cards] == [second.id]
    [failed] = [r for r in caplog.records if r.getMessage() == "escalation alert failed"]
    assert failed.escalation_id == first.id and failed.error == "RuntimeError"
    assert (await _reload_escalation(session, first.id)).alerted_at is None


# --- caps and quiet hours ---------------------------------------------------------------


async def test_alerts_fold_over_the_daily_cap(session, on, monkeypatch):
    monkeypatch.setattr(settings, "jarvis_escalation_daily_cap", 1)
    t0 = _noon()
    await _draft_mode(session, t0)
    _a, first = await _escalation(session, t0)
    _b, second = await _escalation(session, t0, dm=OTHER_DM, name="Sardor")
    _c, third = await _escalation(session, t0, dm=7163, name="Karim")
    await session.commit()
    presenter = FakePresenter()
    at = t0 + timedelta(minutes=2)
    for escalation in (first, second, third):
        assert await cards.alert_now(presenter, escalation.id, now=at)
    assert [c.id for c, _s, _i in presenter.cards] == [first.id]
    (folded_once, cap, edit, fold_id), (folded_twice, _cap, edit_again, same_id) = (
        presenter.folds
    )
    assert cap == 1 and edit is None
    assert [c.id for c in folded_once] == [second.id]
    # The day's one folded message is edited, not sent again.
    assert edit_again == fold_id == same_id
    assert [c.id for c in folded_twice] == [second.id, third.id]
    text = replies.jarvis_fold(folded_twice, 1)
    assert text == (
        "⚠️ Bugungi eskalatsiya limiti tugadi (1). Qolganlari:\n"
        "⚠️ Sardor narx so'rayapti · Telegram\n⚠️ Karim narx so'rayapti · Telegram"
    )
    for escalation in (second, third):
        after = await _reload_escalation(session, escalation.id)
        assert after.alerted_at is not None and after.alert_tg_message_id == fold_id
    # A reply to the folded message names no single card.
    assert await cards.find_card(session, fold_id) is None


async def test_draft_cap_told_once_and_left_to_brief(session, on, monkeypatch):
    monkeypatch.setattr(settings, "jarvis_draft_daily_cap", 0)
    t0 = _noon()
    await _draft_mode(session, t0)
    _p, row = await _draft(session, t0)
    assert (await _reload_row(session, row.id)).last_error == "draft_cap"
    await session.commit()
    presenter = FakePresenter()
    first = await cards.sweep(presenter, now=t0 + timedelta(minutes=2))
    second = await cards.sweep(presenter, now=t0 + timedelta(minutes=3))
    assert first.cap_told and not second.cap_told
    assert presenter.caps == [0] and presenter.cards == []
    assert (await _reload_row(session, row.id)).card_tg_message_id is None
    items = await cards.brief_items(session, now=t0 + timedelta(hours=1))
    assert items.unanswered == ("Akmal",)
    assert replies.jarvis_brief_lines(items) == ["📨 Javobsiz chatlar: 1 — Akmal"]
    # Capped at noon, the draft has expired unanswered by the next morning's
    # brief: the brief still names the chat.
    morning = cards.next_brief(t0)
    assert morning - t0 > timedelta(hours=settings.jarvis_draft_expiry_hours)
    assert row.id in await outbox.expire_due(session, now=morning)
    assert (await _reload_row(session, row.id)).state == outbox.EXPIRED
    assert (await cards.brief_items(session, now=morning)).unanswered == ("Akmal",)
    # Once the owner wrote in the chat, it is no longer unanswered.
    state = await _state(session)
    state.owner_replied_at = morning - timedelta(hours=1)
    await session.flush()
    assert (await cards.brief_items(session, now=morning)).unanswered == ()


async def test_cards_arrive_silently_in_quiet_hours(session, on):
    from miya.worker import main as worker

    t0 = _noon()
    await _draft_mode(session, t0)
    _p, escalation = await _escalation(session, t0)
    await session.commit()
    night = t0.replace(hour=2) + timedelta(days=1)
    bot = FakeBot()
    assert await cards.alert_now(worker.JarvisPresenter(bot), escalation.id, now=night)
    [(_text, kwargs)] = bot.sent
    # Delivered, not held: silently.
    assert kwargs["disable_notification"] is True


# --- JV-17 carry-over: the notices answer the card -------------------------------------


async def test_takeover_notices_answer_the_card_shown(session, on):
    from miya.worker import main as worker

    t0 = _noon()
    await _draft_mode(session, t0)
    person, row = await _draft(session, t0)
    sardor, escalation = await _escalation(session, t0, dm=OTHER_DM, name="Sardor")
    await session.commit()
    bot = FakeBot()
    presenter = worker.JarvisPresenter(bot)
    at = t0 + timedelta(minutes=2)
    assert await cards.card_now(presenter, row.id, now=at)
    assert await cards.alert_now(presenter, escalation.id, now=at)
    card_id = (await _reload_row(session, row.id)).card_tg_message_id
    alert_id = (await _reload_escalation(session, escalation.id)).alert_tg_message_id
    assert {card_id, alert_id} == {7001, 7002}
    for who, dm in ((person, DM), (sardor, OTHER_DM)):
        mine = await _owner_says(session, who, "Ha", at=at + timedelta(minutes=1), dm=dm)
        assert await chat_state.owner_wrote(session, mine, now=at + timedelta(minutes=1))
    await session.commit()
    assert await worker.jarvis_takeover_notices(bot, now=at + timedelta(minutes=2)) == 2
    replied = sorted(kw["reply_parameters"].message_id for _t, kw in bot.sent[2:])
    assert replied == sorted([card_id, alert_id])


# --- review: the sweep, the summary, mode changes, size, the reply's chat ------------


async def test_shadow_escalations_never_fill_the_sweep(session, on, monkeypatch):
    monkeypatch.setattr(cards, "SWEEP_BATCH", 1)
    t0 = _noon()
    sardor = await _setup(session, dm=OTHER_DM, name="Sardor")
    await _says(session, sardor, "Narxi qancha?", at=t0, dm=OTHER_DM)
    await _tick(session, t0 + timedelta(minutes=1))
    [shadow] = await _escalations(session, dm=OTHER_DM)
    assert shadow.history[0]["mode"] == "shadow" and shadow.alerted_at is None
    await _draft_mode(session, t0)
    _p, escalation = await _escalation(session, t0 + timedelta(minutes=2))
    await session.commit()
    assert not await cards.alert_now(
        FakePresenter(fail=True), escalation.id, now=t0 + timedelta(minutes=3)
    )
    up = FakePresenter()
    result = await cards.sweep(up, now=t0 + timedelta(minutes=4))
    # The older shadow escalation never takes the batch's one place.
    assert result.alerted == 1
    assert [c.id for c, _s, _i in up.cards] == [escalation.id]


async def test_alert_gets_a_digit_free_summary(session, on, scoped):
    t0 = _noon()
    await _draft_mode(session, t0)
    _p, escalation = await _escalation(session, t0)
    presenter = FakePresenter()
    assert await cards.alert_now(presenter, escalation.id, now=t0 + timedelta(minutes=1))
    # The alert went out first, never waiting for the model; the line followed.
    [(alert, _silent, alert_id)] = presenter.cards
    assert alert.summary is None and "📝" not in replies.jarvis_card(alert)
    [(edited, message_id)] = presenter.edits
    assert message_id == alert_id
    assert "📝 Mijoz narxni bilmoqchi" in replies.jarvis_card(edited)
    after = await _reload_escalation(session, escalation.id)
    assert after.summary == "Mijoz narxni bilmoqchi"
    assert after.alert_tg_message_id == alert_id
    assert on.roles()[-1] == "summary"
    [system] = [kw["system"] for role, kw in on.calls if role == "summary"]
    assert system == cards.SUMMARY_PROMPT
    used = await session.scalar(
        sa.select(sa.func.count())
        .select_from(m.UsageLog)
        .where(m.UsageLog.operation == "jarvis_summary")
    )
    assert used == 1 and "jarvis_summary" in costs.JARVIS_OPERATIONS
    # Later cards carry it too.
    card = await cards.escalation_card(session, escalation.id)
    assert card.summary == "Mijoz narxni bilmoqchi"


async def test_a_summary_with_a_figure_is_never_kept(session, on, scoped):
    on.summary = "Mijoz 300 ming so'mga rozi bo'lishini so'rayapti"
    t0 = _noon()
    await _draft_mode(session, t0)
    _p, escalation = await _escalation(session, t0)
    presenter = FakePresenter()
    assert await cards.alert_now(presenter, escalation.id, now=t0 + timedelta(minutes=1))
    assert presenter.edits == []
    assert (await _reload_escalation(session, escalation.id)).summary is None
    # A model that fails costs the alert nothing either.
    on.summary = RuntimeError("model down")
    _q, other = await _escalation(session, t0, dm=OTHER_DM, name="Sardor")
    assert await cards.alert_now(presenter, other.id, now=t0 + timedelta(minutes=1))
    assert (await _reload_escalation(session, other.id)).summary is None


async def test_shadow_escalation_closes_once_the_mode_alerts(session, on, scoped):
    t0 = _noon()
    person = await _setup(session)
    await _says(session, person, "Narxi qancha?", at=t0)
    await _tick(session, t0 + timedelta(minutes=1))
    [shadow] = await _escalations(session)
    assert shadow.history[0]["mode"] == "shadow"
    # The owner switches to drafts; the client asks again within the hours an
    # open escalation would otherwise hold the chat.
    t1 = t0 + timedelta(hours=2)
    await _draft_mode(session, t1)
    await _says(session, person, "Narxi qancha bo'ladi?", at=t1)
    [outcome] = await _tick(session, t1 + timedelta(minutes=1))
    assert outcome.code == responder.RED_LINE
    old, new = await _escalations(session)
    assert old.id == shadow.id and old.state == "expired"
    assert old.history[-1]["code"] == responder.MODE_CHANGED
    assert new.history[0]["mode"] == "draft" and new.state == "open"
    presenter = FakePresenter()
    assert await cards.alert_now(presenter, new.id, now=t1 + timedelta(minutes=1))
    assert [c.id for c, _s, _i in presenter.cards] == [new.id]


async def test_a_card_is_never_clipped():
    from miya.worker import main as worker

    at = datetime.now(TZ)
    lines = tuple(cards.ClientLine(f"m{i} " + "x" * 400, at) for i in range(3))
    body = "y" * 2600
    card = cards.Card(
        kind=cards.ESCALATION_CARD,
        id=1,
        channel="telegram",
        name="Akmal",
        messages=lines,
        body=body,
        owner_text=True,
        personal_warning=True,
    )
    text = replies.jarvis_card(card)
    # The text, its warning and the footer stay; the oldest client line goes.
    assert f"<code>{body}</code>" in text
    assert replies.JARVIS_PERSONAL_WARNING in text and replies.JARVIS_COPY_FOOTER in text
    assert "m0 " not in text and "m1 " in text and "m2 " in text
    assert len(text) <= replies.TELEGRAM_LIMIT - replies.JARVIS_CARD_ROOM
    assert replies.jarvis_card_fits(card)
    huge = cards.Card(kind=cards.DRAFT_CARD, id=2, channel="telegram", body="&" * 2000)
    assert not replies.jarvis_card_fits(huge)
    # The bot never delivers a card deliver() would clip.
    bot = FakeBot()
    assert await worker.JarvisPresenter(bot).card(huge, silent=False) is None
    assert bot.sent == []


async def test_owner_text_too_long_for_a_card_is_refused(session, on, monkeypatch):
    @asynccontextmanager
    async def _nested():
        async with session.begin_nested():
            yield session

    monkeypatch.setattr(handlers, "session_scope", _nested)
    monkeypatch.setattr(cards, "session_scope", _nested)
    t0 = _now()
    await _draft_mode(session, t0)
    _person, row = await _draft(session, t0)
    row_id = row.id
    assert await cards.card_now(FakePresenter(), row_id, now=t0 + timedelta(minutes=1))
    card_id = (await _reload_row(session, row_id)).card_tg_message_id
    # Within OWNER_TEXT_MAX, but escaped it would not fit one message.
    message = _owner_message(
        "&" * cards.OWNER_TEXT_MAX, replying_to=_CardMessage(card_id)
    )
    await handlers.on_text(message)
    assert message.sent == [(replies.JARVIS_TOO_LONG, None)]
    after = await _reload_row(session, row_id)
    assert after.body == GREETING_REPLY and after.authored_by == "ai"
    assert after.card_tg_message_id == card_id
    # Longer than OWNER_TEXT_MAX: refused before anything is written.
    longer = _owner_message(
        "a" * (cards.OWNER_TEXT_MAX + 1), replying_to=_CardMessage(card_id)
    )
    await handlers.on_text(longer)
    assert longer.sent == [(replies.JARVIS_TOO_LONG, None)]
    assert (await _reload_row(session, row_id)).body == GREETING_REPLY


async def test_a_failed_card_lookup_leaves_the_message_to_miya(
    session, on, scoped, monkeypatch, caplog
):
    async def _broken(session, message_id):
        raise RuntimeError("lookup exploded")

    monkeypatch.setattr(cards, "find_card", _broken)
    message = _owner_message("Akmal ertaga keladi", replying_to=_CardMessage(4242))
    with caplog.at_level(logging.ERROR, logger="miya.bot.handlers"):
        assert await handlers._jarvis_card_reply(message, message.text) is False
    assert message.sent == []
    [record] = [
        r for r in caplog.records if r.getMessage() == "jarvis card lookup failed"
    ]
    assert record.message_id == 4242 and record.error == "RuntimeError"


async def test_a_reply_in_another_chat_is_no_card_reply(session, on, scoped):
    t0 = _now()
    await _draft_mode(session, t0)
    _person, row = await _draft(session, t0)
    assert await cards.card_now(FakePresenter(), row.id, now=t0 + timedelta(minutes=1))
    card_id = (await _reload_row(session, row.id)).card_tg_message_id
    elsewhere = _owner_message("Salom hammaga", replying_to=_CardMessage(card_id))
    elsewhere.chat = SimpleNamespace(id=-1001234)
    assert handlers._replied_id(elsewhere) is None
    assert await handlers._jarvis_card_reply(elsewhere, elsewhere.text) is False
    assert elsewhere.sent == []
    after = await _reload_row(session, row.id)
    assert after.body == GREETING_REPLY and after.authored_by == "ai"


async def test_card_names_the_chat_when_no_person_resolved(session, on, scoped):
    t0 = _now()
    await _draft_mode(session, t0)
    _p, row = await _draft(session, t0)
    _q, escalation = await _escalation(session, t0, dm=OTHER_DM, name="Sardor")
    await session.execute(
        sa.update(m.OutboxMessage)
        .where(m.OutboxMessage.id == row.id)
        .values(person_id=None)
    )
    await session.execute(
        sa.update(m.Escalation)
        .where(m.Escalation.id == escalation.id)
        .values(person_id=None)
    )
    await session.execute(
        sa.update(m.ChatMonitor)
        .where(m.ChatMonitor.tg_chat_id == DM)
        .values(title="Yuk ‮guruhi")
    )
    await session.flush()
    card = await cards.draft_card(session, row.id)
    assert card.name == "Yuk ‮guruhi" and card.code is None
    # Shown through the escape and bidi stripping like any name.
    assert replies.jarvis_card(card).startswith(f"✍️ Qoralama j{row.id} · Yuk guruhi")
    alert = await cards.escalation_card(session, escalation.id)
    assert alert.name == "Sardor"
    # No title either: the generic «Mijoz».
    await session.execute(
        sa.update(m.ChatMonitor).where(m.ChatMonitor.tg_chat_id == DM).values(title=None)
    )
    await session.flush()
    assert (await cards.draft_card(session, row.id)).name is None


# --- the keys ---------------------------------------------------------------------------

KEYS = {
    "JARVIS_ESCALATION_SNOOZE_MINUTES": "60",
    "JARVIS_SNOOZE_MAX": "3",
    "JARVIS_ESCALATION_DAILY_CAP": "30",
}


def test_card_keys_in_all_four_places():
    defaults = Settings(_env_file=None)
    example = dotenv_values(ROOT / ".env.example")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    for key, value in KEYS.items():
        assert str(getattr(defaults, key.lower())) == value, key
        assert example[key] == value, key
        assert f"`{key}`" in readme, key
        assert key in doctor.EXPECTED_ENV_KEYS, key
        at = lines.index(f"{key}={value}")
        assert lines[at - 1].startswith("# "), key
    assert cards.snooze_for() == timedelta(
        minutes=settings.jarvis_escalation_snooze_minutes
    )

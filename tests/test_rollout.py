"""JV-23: the rollout controller — per-intent graduation, auto mode, demotion —
and the owner's question bank.

Autonomy grows one channel × intent at a time and only on the owner's tap:
the daily job offers an intent that met §6.4, the owner's «✅ Ha, avtomatik»
promotes it (and JARVIS_MODE must be auto too), «↩️ Bu xato edi», a breaker,
a red line found after a send and the cost cap twice in a week take it back.
An autonomous reply may ask only a question from the owner's question bank;
the database refuses an autonomous row without a passing shape check (0028).
Real PostgreSQL, the real JV-13 red-line guard, a fake bot.
"""

from __future__ import annotations

import importlib.util
import itertools
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from aiogram.filters import CommandObject

from alembic.migration import MigrationContext
from alembic.operations import Operations
from miya.bot import handlers, keyboards, replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import ChatType, Direction, InteractionSource
from miya.jarvis import (
    cards,
    chatkey,
    gate,
    outbox,
    policy,
    qbank,
    redlines,
    rollout,
)
from miya.worker import main as worker
from tests.test_responder import (
    DownEmbedder,
    FakeClient,
    _escalations,
    _says,
    _tick,
)
from tests.test_responder import _rows as _responder_rows
from tests.test_responder import _setup as _responder_setup

TZ = settings.tz
ROOT = Path(__file__).resolve().parents[1]
NOW = datetime.now(TZ).replace(hour=12, minute=0, second=0, microsecond=0)
DM = 7231
KEY = chatkey.telegram(DM)
OWNER_ID = 990023
TELEGRAM = chatkey.TELEGRAM
GREETING = "greeting"
_ids = itertools.count(50_000)


class PassGuard:
    """A guard that passes everything with a passing shape check, or blocks."""

    def __init__(self, verdict: str = "pass", *, flags: tuple[str, ...] = ()) -> None:
        self.verdict = verdict
        self.flags = flags

    def check(self, body, *, role, allowed_tokens, **names):
        return gate.GuardResult(
            verdict=self.verdict,
            flags=self.flags,
            checker_version="test",
            shapes_ok=self.verdict == "pass",
        )


@pytest.fixture
def jarvis(monkeypatch):
    """Jarvis on, the ceiling at auto, Telegram sending, the plan's
    graduation defaults, the real red-line guard."""
    monkeypatch.setattr(settings, "jarvis_enabled", True)
    monkeypatch.setattr(settings, "jarvis_mode", "auto")
    monkeypatch.setattr(settings, "owner_telegram_id", OWNER_ID)
    monkeypatch.setattr(settings, "owner_aliases", "")
    monkeypatch.setattr(settings, "jarvis_send_min_gap_seconds", 0)
    monkeypatch.setattr(settings, "jarvis_auto_daily_cap", 20)
    monkeypatch.setattr(settings, "jarvis_draft_daily_cap", 1000)
    monkeypatch.setattr(settings, "jarvis_per_chat_daily_cap", 100)
    monkeypatch.setattr(settings, "jarvis_graduation_min_days", 7)
    monkeypatch.setattr(settings, "jarvis_graduation_min_drafts", 30)
    monkeypatch.setattr(settings, "jarvis_graduation_min_approval_pct", 90)
    monkeypatch.setattr(settings, "jarvis_auto_in_quiet_hours", False)
    monkeypatch.setattr(settings, "jarvis_daily_cost_cap_usd", Decimal("3.00"))
    monkeypatch.setattr(outbox, "channel_send_enabled", lambda channel: True)
    previous = gate.registered_guard()
    redlines.install()
    yield
    gate.register_guard(previous)


@pytest.fixture
def on(monkeypatch):
    """The responder's setting (tests/test_responder.py): Jarvis on, the
    real guard, a fake model, priced calls."""
    from miya.jarvis import costs, kb, style

    monkeypatch.setattr(settings, "jarvis_enabled", True)
    monkeypatch.setattr(settings, "jarvis_mode", "draft")
    monkeypatch.setattr(settings, "owner_telegram_id", 900016)
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
def scoped(session, monkeypatch):
    """The bot handlers and the worker jobs run inside the test session."""

    @asynccontextmanager
    async def _scope():
        yield session
        await session.flush()

    monkeypatch.setattr(handlers, "session_scope", _scope)
    monkeypatch.setattr(worker, "session_scope", _scope)
    monkeypatch.setattr(cards, "session_scope", _scope)
    return session


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((text, kwargs))
        return SimpleNamespace(message_id=7000 + len(self.sent))


class _Message:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id
        self.sent: list[tuple[str, object]] = []
        self.markups_removed = 0

    async def answer(self, text, **kwargs):
        self.sent.append((text, kwargs.get("reply_markup")))
        return SimpleNamespace(message_id=8000 + len(self.sent))

    async def edit_text(self, text, **kwargs):
        self.sent.append((text, kwargs.get("reply_markup")))

    async def edit_reply_markup(self, **kwargs):
        self.markups_removed += 1


class _Callback:
    def __init__(self, data: str, message_id: int) -> None:
        self.data = data
        self.message = _Message(message_id)

    async def answer(self, *args, **kwargs):
        return None


def _payloads(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


async def _person(session, *, name="Akmal", dm=DM) -> m.Person:
    person = m.Person(display_name=name, aliases=[], telegram_id=dm)
    session.add(person)
    session.add(
        m.ChatMonitor(
            tg_chat_id=dm, chat_type=ChatType.private, title=name, monitor_enabled=True
        )
    )
    await session.flush()
    return person


async def _inbound(session, person, *, text="Assalomu alaykum", at=None, dm=DM):
    row = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.in_,
        person_id=person.id,
        tg_chat_id=dm,
        occurred_at=at or NOW - timedelta(minutes=5),
        raw_text=text,
        meta={"tg_message_id": next(_ids)},
    )
    session.add(row)
    await session.flush()
    return row


async def _ai_row(session, person, trigger, *, body, intent=GREETING, now=NOW, **kw):
    return await outbox.create(
        session,
        channel=TELEGRAM,
        chat_key=KEY,
        origin=kw.pop("origin", "auto_reply"),
        authored_by="ai",
        body=body,
        now=now,
        intent=intent,
        person_id=person.id,
        trigger_interaction_id=trigger.id,
        **kw,
    )


async def _reload(session, row_id) -> m.OutboxMessage:
    return await session.scalar(
        sa.select(m.OutboxMessage)
        .where(m.OutboxMessage.id == row_id)
        .execution_options(populate_existing=True)
    )


async def _global(session, mode: str) -> None:
    await policy.set_mode(session, scope=policy.GLOBAL, mode=mode, by="command", now=NOW)


async def _graduate(session, intent: str = GREETING) -> None:
    await policy.set_mode(
        session,
        scope=policy.channel_intent_scope(TELEGRAM, intent),
        mode="auto",
        by="button",
        now=NOW - timedelta(days=1),
    )


def _history(at: datetime, *states: str) -> list[dict]:
    entries = []
    previous = None
    for i, state in enumerate(states):
        entries.append(
            {
                "at": (at + timedelta(minutes=i)).isoformat(),
                "field": "state",
                "old": previous,
                "new": state,
                "by": "auto:policy" if previous is None else "button",
            }
        )
        previous = state
    return entries


def _draft(
    *,
    at: datetime,
    states: tuple[str, ...] = ("draft", "approved", "sending", "sent"),
    approved_by: str | None = "button",
    authored_by: str = "ai",
    intent: str = GREETING,
    channel: str = TELEGRAM,
) -> m.OutboxMessage:
    """A draft card as the chokepoint leaves it after the owner decided."""
    chat = 700_000 + next(_ids) if channel == TELEGRAM else None
    return m.OutboxMessage(
        channel=channel,
        chat_key=f"tg:{chat}" if chat else f"wa:{next(_ids)}@s.whatsapp.net",
        tg_chat_id=chat,
        origin="auto_reply",
        intent=intent,
        authored_by=authored_by,
        body="Assalomu alaykum!",
        ai_body="Assalomu alaykum!",
        body_sha256="0" * 64,
        guard={"verdict": "pass", "shapes_ok": True},
        state=states[-1],
        approved_by=approved_by if states[-1] != "draft" else None,
        sent_at=at if states[-1] == "sent" else None,
        idempotency_key=f"jv23-{next(_ids)}",
        history=_history(at, *states),
        created_at=at,
    )


async def _seed_drafts(
    session,
    *,
    days: int = 8,
    unchanged: int = 27,
    edited: int = 0,
    other: int = 3,
    pending: int = 0,
    intent: str = GREETING,
    end: datetime = NOW,
) -> None:
    """``unchanged`` drafts approved as written, ``edited`` rewritten by the
    owner, ``other`` cancelled or expired, ``pending`` still waiting; the
    first ``days`` days before ``end``."""
    start = end - timedelta(days=days)
    rows = []
    total = unchanged + edited + other
    for i in range(total):
        at = start + timedelta(minutes=5 * i)
        if i < unchanged:
            rows.append(_draft(at=at, intent=intent))
        elif i < unchanged + edited:
            rows.append(_draft(at=at, intent=intent, authored_by="owner"))
        else:
            rows.append(
                _draft(
                    at=at, intent=intent, states=("draft", "expired"), approved_by=None
                )
            )
    for i in range(pending):
        rows.append(
            _draft(
                at=end - timedelta(hours=1, minutes=i),
                intent=intent,
                states=("draft",),
                approved_by=None,
            )
        )
    session.add_all(rows)
    await session.flush()


# --- the criteria (§6.4) ----------------------------------------------------------------


async def test_criteria_math(session, jarvis):
    await _global(session, "draft")
    await _seed_drafts(session, days=8, unchanged=27, other=3, pending=4)
    found = await rollout.metrics(session, TELEGRAM, GREETING, now=NOW)
    # 27 of 30 decided drafts unchanged: 90 %; the waiting ones count neither way.
    assert (found.days, found.drafts, found.unchanged, found.pending) == (8, 30, 27, 4)
    assert found.pct == 90 and found.stored == "draft" and found.ready()
    assert rollout.offer_due(found, NOW)

    # One percent short, one draft short, one day short: not ready.
    for changes in (
        {"unchanged": 26},
        {"drafts": 29, "unchanged": 27},
        {"days": 6},
    ):
        values = {
            "drafts": found.drafts,
            "unchanged": found.unchanged,
            "days": found.days,
            **changes,
        }
        short = rollout.Metrics(
            TELEGRAM, GREETING, stored="draft", effective="draft", **values
        )
        assert not short.ready(), changes
    # Rounded down, never up: 29 of 31 is 93 %.
    assert (
        rollout.Metrics(TELEGRAM, GREETING, "draft", "draft", drafts=31, unchanged=29).pct
        == 93
    )
    # An edited draft is not «unchanged»: two rewritten of 30 is 86 % with 26.
    await session.execute(sa.delete(m.OutboxMessage))
    await _seed_drafts(session, unchanged=26, edited=2, other=2)
    edited = await rollout.metrics(session, TELEGRAM, GREETING, now=NOW)
    assert (edited.drafts, edited.unchanged, edited.pct) == (30, 26, 86)
    assert not edited.ready()

    # An incident restarts the window: the drafts before it no longer count.
    await session.execute(sa.delete(m.OutboxMessage))
    await _seed_drafts(session)
    await rollout.record_incident(
        session,
        channel=TELEGRAM,
        intent=GREETING,
        code=rollout.EDIT_INCIDENT,
        by="reply",
        now=NOW - timedelta(days=2),
    )
    after = await rollout.metrics(session, TELEGRAM, GREETING, now=NOW)
    assert after.since is not None and after.drafts < 30 and not after.ready()

    # A breaker trip in the last 7 days holds graduation back.
    await session.execute(
        sa.delete(m.JarvisPolicy).where(m.JarvisPolicy.scope != "global")
    )
    await policy.trip_breaker(
        session,
        channel=TELEGRAM,
        reason="peer_flood",
        until=NOW - timedelta(days=3),
        by="adapter",
        now=NOW - timedelta(days=4),
    )
    limited = await rollout.metrics(session, TELEGRAM, GREETING, now=NOW)
    assert limited.breaker_recent and not limited.ready()
    old_trip = await rollout.metrics(
        session, TELEGRAM, GREETING, now=NOW + timedelta(days=5)
    )
    assert not old_trip.breaker_recent

    # The stall and the never-auto intents are never measured for graduation.
    assert "stall" not in rollout.GRADUATING
    assert set(rollout.GRADUATING) == set(policy.AUTO_INTENTS) == set(gate.AUTO_INTENTS)


async def test_scope_lock_reads_the_row_fresh(session, jarvis):
    # A row already in the session is read again under the lock: an entry
    # another transaction committed meanwhile is kept, not overwritten.
    scope = rollout.scope_of(TELEGRAM, GREETING)
    await policy.note(
        session, scope=scope, field="first", old=None, new=1, by="t", now=NOW
    )
    await session.execute(
        sa.text(
            "UPDATE jarvis_policy SET history = history || CAST(:entry AS jsonb) "
            "WHERE scope = :scope"
        ),
        {"entry": '[{"field": "elsewhere"}]', "scope": scope},
    )
    row = await policy.note(
        session, scope=scope, field="second", old=None, new=2, by="t", now=NOW
    )
    assert [e["field"] for e in row.history] == ["first", "elsewhere", "second"]


async def test_no_silent_promotion(session, jarvis, scoped):
    """The job only offers; the stored mode moves only on the owner's tap on
    the offer they were shown, and a typed batch answer never reaches it."""
    await _global(session, "draft")
    await _seed_drafts(session)
    bot = FakeBot()
    assert await worker.jarvis_rollout_job(bot, now=NOW) == 1
    [(text, kwargs)] = bot.sent
    assert text == (
        "🎓 «salomlashish» javoblari tayyor: 8 kunda 30 ta qoralama, 90% tini "
        "o'zgartirmasdan tasdiqladingiz, qizil chiziq buzilmadi. Endi bunday "
        "javoblarni MIYA o'zi yuborsinmi? (kuniga 20 tagacha)"
    )
    markup = kwargs["reply_markup"]
    assert [b.text for row in markup.inline_keyboard for b in row] == [
        "✅ Ha, avtomatik",
        "⏳ Yana kuzataylik",
    ]
    assert _payloads(markup) == ["jg:y:telegram:greeting", "jg:n:telegram:greeting"]
    policies = await policy.load(session)
    assert policy.stored_mode(policies, TELEGRAM, GREETING) == "draft"
    # Once: the next day's run does not ask again.
    assert await worker.jarvis_rollout_job(bot, now=NOW + timedelta(days=1)) == 0
    assert len(bot.sent) == 1
    # Not a button the typed batch answers reach.
    assert keyboards.JARVIS_GRADUATION_PREFIX not in handlers._button_handlers()
    # A tap from another message (an old offer) changes nothing.
    stale = _Callback("jg:y:telegram:greeting", 4242)
    await handlers.on_jarvis_graduation_button(stale)
    assert stale.message.sent == [(replies.JARVIS_GRADUATION_STALE, None)]
    policies = await policy.load(session)
    assert policy.stored_mode(policies, TELEGRAM, GREETING) == "draft"
    # The tap on the offer itself promotes, by the owner's button.
    tap = _Callback("jg:y:telegram:greeting", 7001)
    await handlers.on_jarvis_graduation_button(tap)
    [(said, _)] = tap.message.sent
    assert said.startswith("✅ «salomlashish» javoblarini endi MIYA o'zi yuboradi")
    assert replies.JARVIS_CEILING_HINT not in said
    row = await session.get(
        m.JarvisPolicy,
        policy.channel_intent_scope(TELEGRAM, GREETING),
        populate_existing=True,
    )
    assert row.mode == "auto"
    [promotion] = [e for e in row.history if e["field"] == "mode"]
    assert promotion["by"] == "button" and promotion["code"] == rollout.GRADUATED
    # A second tap is stale: the offer is answered.
    again = _Callback("jg:y:telegram:greeting", 7001)
    await handlers.on_jarvis_graduation_button(again)
    assert again.message.sent == [(replies.JARVIS_GRADUATION_STALE, None)]


async def test_declined_offer_waits_and_a_changed_one_is_refused(session, jarvis, scoped):
    await _global(session, "draft")
    await _seed_drafts(session)
    bot = FakeBot()
    await worker.jarvis_rollout_job(bot, now=NOW)
    tap = _Callback("jg:n:telegram:greeting", 7001)
    await handlers.on_jarvis_graduation_button(tap)
    assert tap.message.sent == [
        (
            "⏳ Mayli, «salomlashish» javoblari qoralama bo'lib qoladi. "
            "7 kundan keyin yana so'rayman.",
            None,
        )
    ]
    # The tap ran on the real clock (up to half a day from NOW): a week after
    # it, never sooner, the offer comes again.
    assert await worker.jarvis_rollout_job(bot, now=NOW + timedelta(days=6)) == 0
    assert await worker.jarvis_rollout_job(bot, now=NOW + timedelta(days=8)) == 1
    # Between the offer and the tap an incident happened: no promotion.
    await rollout.record_incident(
        session,
        channel=TELEGRAM,
        intent=GREETING,
        code=rollout.EDIT_INCIDENT,
        by="reply",
        now=NOW + timedelta(days=8, minutes=1),
    )
    late = _Callback("jg:y:telegram:greeting", 7002)
    await handlers.on_jarvis_graduation_button(late)
    [(said, _)] = late.message.sent
    assert said.startswith("⏳ «salomlashish» hali tayyor emas")
    policies = await policy.load(session)
    assert policy.stored_mode(policies, TELEGRAM, GREETING) == "draft"


async def test_never_auto_intents_rejected(session, jarvis, scoped):
    await _global(session, "auto")
    person = await _person(session)
    for intent in rollout.NEVER_AUTO_INTENTS:
        assert intent not in policy.AUTO_INTENTS
        result = await rollout.accept(session, TELEGRAM, intent, message_id=1, now=NOW)
        assert result.code == policy.NEVER_AUTO, intent
        for scope in (
            policy.channel_intent_scope(TELEGRAM, intent),
            policy.intent_scope(intent),
        ):
            with pytest.raises(ValueError, match=policy.NEVER_AUTO):
                await policy.set_mode(
                    session, scope=scope, mode="auto", by="button", now=NOW
                )
        # Even with every stored mode and the ceiling at auto, such a row is a
        # card for the owner, never approved on its own.
        trigger = await _inbound(session, person)
        row = await _ai_row(
            session, person, trigger, body="Assalomu alaykum, aka!", intent=intent
        )
        assert row.state == "draft" and row.approved_by is None, intent
    # The holding reply too (JV-23): never sent without the owner's tap.
    stall = gate.Row(
        id=1,
        channel=TELEGRAM,
        chat_key=KEY,
        origin="stall",
        authored_by="ai",
        body="Aka, hozir aniqlashtirib, o'zim tez orada yozvoraman.",
        body_sha256=None,
        guard={},
        created_at=NOW,
        intent="stall",
        approved_by=gate.AUTO_APPROVER,
        trigger_interaction_id=9,
    )
    ctx = gate.Context(
        now=NOW,
        jarvis_enabled=True,
        channel_send_enabled=True,
        env_ceiling="auto",
        policies={policy.GLOBAL: policy.PolicySnapshot(policy.GLOBAL, mode="auto")},
        chat=policy.ChatFacts(chat_key=KEY, channel=TELEGRAM, tg_chat_id=DM),
    )
    decision = gate.evaluate(stall, ctx)
    assert (decision.action, decision.code) == ("redraft", "never_auto")
    # The button with a never-auto intent in its payload is refused.
    tap = _Callback("jg:y:telegram:price", 7001)
    await handlers.on_jarvis_graduation_button(tap)
    assert tap.message.sent == [
        ("⛔ «narx» javoblari hech qachon avtomatik yuborilmaydi.", None)
    ]
    assert await session.get(m.JarvisPolicy, "channel:telegram:intent:price") is None


async def test_env_ceiling_respected(session, jarvis, scoped, monkeypatch):
    """A tap stores ``auto``, but nothing is sent on its own until
    JARVIS_MODE=auto as well; the owner is told so."""
    monkeypatch.setattr(settings, "jarvis_mode", "draft")
    await _global(session, "draft")
    await _seed_drafts(session)
    bot = FakeBot()
    assert await worker.jarvis_rollout_job(bot, now=NOW) == 1
    [(offer, _)] = bot.sent
    assert offer.endswith(
        "\nAvtomatik rejim uchun serverdagi .env da JARVIS_MODE=auto qilish kerak."
    )
    tap = _Callback("jg:y:telegram:greeting", 7001)
    await handlers.on_jarvis_graduation_button(tap)
    [(said, _)] = tap.message.sent
    assert said.endswith(replies.JARVIS_CEILING_HINT)
    person = await _person(session)
    trigger = await _inbound(session, person)
    held = await _ai_row(session, person, trigger, body="Assalomu alaykum, aka!")
    assert held.state == "draft" and held.approved_by is None
    view = await rollout.modes(session, now=NOW)
    text = replies.jarvis_modes(view)
    assert "• salomlashish: avtomatik (ruxsat berilgan, lekin hozir: qoralama)" in text
    assert replies.JARVIS_CEILING_HINT in text
    # The ceiling raised: the same text goes out on its own.
    monkeypatch.setattr(settings, "jarvis_mode", "auto")
    again = await _inbound(session, person)
    sent = await _ai_row(session, person, again, body="Assalomu alaykum, aka!")
    assert sent.state == "approved" and sent.approved_by == "auto:policy"
    assert replies.JARVIS_CEILING_HINT not in replies.jarvis_modes(
        await rollout.modes(session, now=NOW)
    )


async def test_rejim_shows_each_intent(session, jarvis, scoped, monkeypatch):
    await _global(session, "draft")
    # The command reads the real clock.
    real = datetime.now(TZ)
    await _seed_drafts(session, days=3, unchanged=10, other=0, pending=2, end=real)
    await _graduate(session, "info_gathering")
    message = _Message(1)
    await handlers.cmd_jarvis(message, CommandObject(command="jarvis", args="rejim"))
    [(text, _)] = message.sent
    assert text.startswith("🎛 Jarvis rejimlari — Telegram:")
    assert (
        "• salomlashish: qoralama — 3 kun, 10 ta qoralama, 100% o'zgartirmasdan, "
        "2 ta kutmoqda" in text
    )
    assert "• ma'lumot so'rash: avtomatik" in text
    assert "• umumiy savol: qoralama — 0 kun, 0 ta qoralama, 0% o'zgartirmasdan" in text
    assert "🔒 Hech qachon avtomatik emas: narx, chegirma" in text
    assert "kutib turish javobi" in text
    assert "Yuqori chegara (.env JARVIS_MODE): avtomatik" in text
    assert "kamida 7 kun, 30 ta qoralama, 90% o'zgartirmasdan" in text
    # «rejim» is in the usage line, and an unknown word shows the usage.
    assert "/jarvis rejim" in replies.JARVIS_USAGE
    wrong = _Message(2)
    await handlers.cmd_jarvis(wrong, CommandObject(command="jarvis", args="nimadir"))
    assert wrong.sent == [(replies.JARVIS_USAGE, None)]


# --- autonomous sends -------------------------------------------------------------------


async def _sent_auto(session, person, *, body="Assalomu alaykum, aka!", at=NOW):
    trigger = await _inbound(session, person, at=at - timedelta(minutes=5))
    row = await _ai_row(session, person, trigger, body=body, now=at)
    assert row.state == "approved" and row.approved_by == "auto:policy", row.last_error
    [claimed] = await outbox.claim_ready(session, TELEGRAM, now=at)
    assert claimed.id == row.id
    message_id = next(_ids)
    await outbox.complete(
        session,
        row.id,
        external_id=f"{DM}:{message_id}",
        sent_at=at + timedelta(seconds=9),
    )
    return row


async def test_xato_demotes(session, jarvis, scoped):
    await _global(session, "draft")
    await _graduate(session)
    person = await _person(session, name="Akmal ‮")
    row = await _sent_auto(session, person)
    # Another autonomous row of the same intent, approved but not sent yet.
    trigger = await _inbound(session, person)
    waiting = await _ai_row(session, person, trigger, body="Salom, aka!")
    assert waiting.state == "approved"
    bot = FakeBot()
    assert await worker.jarvis_autonomy_notices(bot, now=NOW + timedelta(minutes=1)) == 1
    [(text, kwargs)] = bot.sent
    # The client's name is stripped of bidi marks and escaped.
    assert text == "🤖 Avtomatik yuborildi · Akmal : «Assalomu alaykum, aka!»"
    assert kwargs["disable_notification"] is True
    assert _payloads(kwargs["reply_markup"]) == [f"jv:x:{row.id}"]
    assert kwargs["reply_markup"].inline_keyboard[0][0].text == "↩️ Bu xato edi"
    # Told once.
    assert await worker.jarvis_autonomy_notices(bot, now=NOW + timedelta(minutes=2)) == 0
    # A tap from another message is stale; the notice's own demotes.
    stale = _Callback(f"jv:x:{row.id}", 4242)
    await handlers.on_jarvis_draft_button(stale)
    assert stale.message.sent == [(replies.JARVIS_MISTAKE_STALE, None)]
    tap = _Callback(f"jv:x:{row.id}", 7001)
    await handlers.on_jarvis_draft_button(tap)
    assert tap.message.sent == [
        (
            "↩️ «salomlashish» yana qoralama rejimiga qaytdi: "
            "siz «Bu xato edi» deb belgiladingiz.",
            None,
        )
    ]
    policies = await policy.load(session)
    assert policy.stored_mode(policies, TELEGRAM, GREETING) == "draft"
    flagged = await _reload(session, row.id)
    assert [e["field"] for e in flagged.history][-2:] == [
        outbox.MISTAKE_FIELD,
        outbox.REVIEW_FIELD,
    ]
    [case] = await outbox.review_cases(session)
    assert case.id == row.id and case.reasons == (outbox.REVIEW_MISTAKE,)
    # The tap told the owner: no second notice of the same demotion.
    assert await rollout.demotions_untold(session) == []
    # A second tap changes nothing.
    again = _Callback(f"jv:x:{row.id}", 7001)
    await handlers.on_jarvis_draft_button(again)
    assert again.message.sent == [(replies.JARVIS_ANSWERED, None)]
    # The approved row of the same intent goes back to the owner's card.
    assert (
        await outbox.claim_ready(session, TELEGRAM, now=NOW + timedelta(minutes=3)) == []
    )
    back = await _reload(session, waiting.id)
    assert back.state == "draft" and back.last_error == "demoted"
    # The window starts again: the next offer needs a fresh week.
    found = await rollout.metrics(session, TELEGRAM, GREETING, now=NOW)
    assert found.since is not None and not found.ready()
    assert keyboards.JARVIS_MISTAKE == rollout.MISTAKE_ACTION
    assert keyboards.JARVIS_GRADUATE_YES == rollout.ACCEPT_ACTION
    assert keyboards.JARVIS_GRADUATE_NO == rollout.DECLINE_ACTION


async def test_breaker_and_incident_demote_automatically(session, jarvis, scoped):
    await _global(session, "draft")
    await _graduate(session)
    await _graduate(session, "info_gathering")
    await policy.trip_breaker(
        session,
        channel=TELEGRAM,
        reason="peer_flood",
        until=NOW + timedelta(hours=1),
        by="adapter",
        now=NOW,
    )
    policies = await policy.load(session)
    for intent in (GREETING, "info_gathering"):
        assert policy.stored_mode(policies, TELEGRAM, intent) == "draft"
    untold = await rollout.demotions_untold(session)
    assert {(d.intent, d.code) for d in untold} == {
        (GREETING, rollout.BREAKER),
        ("info_gathering", rollout.BREAKER),
    }
    bot = FakeBot()
    assert await worker.jarvis_autonomy_notices(bot, now=NOW) == 2
    told = [text for text, _ in bot.sent]
    assert (
        "↩️ «salomlashish» yana qoralama rejimiga qaytdi: kanalda cheklov ishladi." in told
    )
    assert (
        "↩️ «ma'lumot so'rash» yana qoralama rejimiga qaytdi: kanalda cheklov ishladi."
        in told
    )
    assert await worker.jarvis_autonomy_notices(bot, now=NOW) == 0

    # A red line found in a sent reply by a later guard (an incident after
    # the send) demotes as well, once.
    await policy.resume(
        session, scope=policy.channel_scope(TELEGRAM), by="command", now=NOW
    )
    await _graduate(session)
    person = await _person(session)
    row = await _sent_auto(session, person, at=NOW + timedelta(hours=2))
    gate.register_guard(PassGuard("block", flags=("promise",)))
    later = NOW + timedelta(days=1)
    demoted = await rollout.recheck_sends(session, now=later)
    assert [(d.intent, d.code) for d in demoted] == [(GREETING, rollout.INCIDENT)]
    [case] = [c for c in await outbox.review_cases(session) if c.id == row.id]
    assert case.code == outbox.REVIEW_INCIDENT and case.reasons == ("promise",)
    assert await rollout.recheck_sends(session, now=later) == []
    assert (await _reload(session, row.id)).guard["shapes_ok"] is True
    await worker.jarvis_autonomy_notices(bot, now=later)
    assert bot.sent[-1][0] == (
        "↩️ «salomlashish» yana qoralama rejimiga qaytdi: "
        "yuborilgan javobda qizil chiziq topildi."
    )


async def test_recheck_reads_every_send_of_the_week(session, jarvis, monkeypatch):
    # More autonomous sends than one page: the newest is read again too.
    monkeypatch.setattr(outbox, "RECHECK_BATCH", 2)
    await _global(session, "draft")
    await _graduate(session)
    person = await _person(session)
    rows = [
        await _sent_auto(session, person, at=NOW + timedelta(minutes=10 * i))
        for i in range(5)
    ]
    # A shape note (never a block) is not an incident's reason.
    gate.register_guard(PassGuard("block", flags=("promise", gate.FREE_QUESTION)))
    later = NOW + timedelta(days=1)
    demoted = await rollout.recheck_sends(session, now=later)
    assert [(d.intent, d.code) for d in demoted] == [(GREETING, rollout.INCIDENT)]
    kept = {
        c.id: c.reasons
        for c in await outbox.review_cases(session)
        if c.code == outbox.REVIEW_INCIDENT
    }
    assert {row.id: ("promise",) for row in rows}.items() <= kept.items()
    assert await outbox.sent_auto_ids(session, now=later) == []


async def test_cost_cap_twice_in_a_week_demotes(session, jarvis):
    await _global(session, "draft")
    await _graduate(session)

    def usage(at: datetime, cost: str) -> m.UsageLog:
        return m.UsageLog(
            provider="anthropic",
            operation="jarvis_draft",
            model="test-reason-model",
            input_tokens=1,
            output_tokens=1,
            cost_usd=Decimal(cost),
            created_at=at,
        )

    session.add(usage(NOW - timedelta(days=2), "3.50"))
    await session.flush()
    assert await rollout.cost_cap_days(session, now=NOW) == 1
    assert await rollout.check_cost_cap(session, now=NOW) == []
    session.add(usage(NOW - timedelta(days=5), "3.00"))
    await session.flush()
    assert await rollout.cost_cap_days(session, now=NOW) == 2
    demoted = await rollout.check_cost_cap(session, now=NOW)
    assert [(d.channel, d.intent, d.code) for d in demoted] == [
        (TELEGRAM, GREETING, rollout.COST_CAP)
    ]
    policies = await policy.load(session)
    assert policy.stored_mode(policies, TELEGRAM, GREETING) == "draft"


async def test_cost_capped_week_holds_the_offer(session, jarvis):
    # The cost cap reached twice this week: the next daily job would demote
    # the intent again, so it is not offered and a tap on an older offer
    # does not promote it.
    await _global(session, "draft")
    await _seed_drafts(session, days=8, unchanged=30)
    ready = await rollout.metrics(session, TELEGRAM, GREETING, now=NOW)
    assert ready.ready() and not ready.cost_capped
    assert [x.intent for x in await rollout.offers_due(session, now=NOW)] == [GREETING]
    for back in (1, 2):
        session.add(
            m.UsageLog(
                provider="anthropic",
                operation="jarvis_draft",
                model="test-reason-model",
                input_tokens=1,
                output_tokens=1,
                cost_usd=Decimal("3.20"),
                created_at=NOW - timedelta(days=back),
            )
        )
    await session.flush()
    capped = await rollout.metrics(session, TELEGRAM, GREETING, now=NOW)
    assert capped.cost_capped and not capped.ready()
    assert await rollout.offers_due(session, now=NOW) == []
    message_id = 8123
    await rollout.record_offer(
        session, TELEGRAM, GREETING, message_id=message_id, now=NOW
    )
    tapped = await rollout.accept(
        session, TELEGRAM, GREETING, message_id=message_id, now=NOW
    )
    assert tapped.code == "not_ready"
    policies = await policy.load(session)
    assert policy.stored_mode(policies, TELEGRAM, GREETING) == "draft"


async def test_auto_held_in_quiet_hours(session, jarvis, monkeypatch):
    night = NOW.replace(hour=1)
    await _global(session, "draft")
    await _graduate(session)
    person = await _person(session)
    trigger = await _inbound(session, person, at=night - timedelta(minutes=5))
    row = await _ai_row(
        session, person, trigger, body="Assalomu alaykum, aka!", now=night
    )
    assert row.state == "approved" and row.approved_by == "auto:policy"
    assert await outbox.claim_ready(session, TELEGRAM, now=night) == []
    held = await _reload(session, row.id)
    assert held.state == "approved" and held.last_error == "quiet_hours"
    morning = datetime.combine(night.date(), settings.quiet_hours_parsed[1], tzinfo=TZ)
    assert held.next_attempt_at == morning
    # The owner's own tap is never held by the quiet hours.
    owned = await _ai_row(
        session, person, trigger, body="Salom, aka!", intent="other", now=night
    )
    await outbox.approve(session, owned.id, by="button", now=night)
    claimed = await outbox.claim_ready(
        session, TELEGRAM, now=night + timedelta(seconds=1)
    )
    assert [c.id for c in claimed] == [owned.id]
    # With JARVIS_AUTO_IN_QUIET_HOURS the autonomous row goes at night too.
    monkeypatch.setattr(settings, "jarvis_auto_in_quiet_hours", True)
    await session.execute(
        sa.update(m.OutboxMessage)
        .where(m.OutboxMessage.id == row.id)
        .values(next_attempt_at=None)
        .execution_options(synchronize_session=False)
    )
    claimed = await outbox.claim_ready(
        session, TELEGRAM, now=night + timedelta(minutes=1)
    )
    assert [c.id for c in claimed] == [row.id]


# --- the statement allowlist with the question bank ------------------------------------


async def test_auto_needs_shapes_ok(session, jarvis):
    """Graduated, a text goes out on its own only when every sentence has an
    approved shape and every question is in the owner's question bank; any
    other is a draft card (never blocked). The database refuses an
    autonomous row without a passing shape check."""
    await _global(session, "draft")
    await _graduate(session, "info_gathering")
    person = await _person(session)

    async def row(body):
        trigger = await _inbound(session, person)
        return await _ai_row(session, person, trigger, body=body, intent="info_gathering")

    banked = await row("Rahmat, aka. Yuk qaysi shahardan? Yukingiz necha kilo?")
    assert banked.state == "approved" and banked.guard["shapes_ok"] is True
    assert banked.guard["shapes"] == ["courtesy", "question", "question"]
    free = await row("Rahmat, aka. Yukingiz qaysi rangda?")
    assert free.state == "draft" and free.guard["shapes_ok"] is False
    assert free.guard["shapes"] == ["courtesy", "free_question"]
    # A red line is still a block, bank or no bank.
    timed = await row("Yuk qachongacha kerak bo'ladi?")
    assert timed.state == "blocked"
    unbanked = await row("Yuk qayerdan qayerga?")
    assert unbanked.state == "draft" and unbanked.approved_by is None
    assert unbanked.last_error == gate.UNAPPROVED_STATEMENT
    assert unbanked.guard["shapes"] == ["free_question"]
    assert unbanked.guard["flags"] == [gate.FREE_QUESTION]
    statement = await row("Bosh ustiga, aka.")
    assert statement.state == "draft" and statement.guard["shapes_ok"] is False
    # Drafts are unaffected: the owner's tap still sends the free question.
    assert (await outbox.approve(session, unbanked.id, by="button", now=NOW)).approved

    # Defense in depth: no autonomous row without shapes_ok, NULL never passes.
    for guard in (
        {"verdict": "pass"},
        {"verdict": "pass", "shapes_ok": False},
        {"verdict": "pass", "shapes_ok": "true"},
        {"verdict": "pass", "shapes_ok": None},
    ):
        forged = _draft(at=NOW, states=("approved",), approved_by="auto:policy")
        forged.approved_by = "auto:policy"
        forged.guard = guard
        with pytest.raises(sa.exc.IntegrityError) as caught:
            async with session.begin_nested():
                session.add(forged)
                await session.flush()
        assert "ck_outbox_messages_auto_shapes" in str(caught.value), guard
    # The same row on the owner's tap, or with the check, is fine.
    tapped = _draft(at=NOW, states=("approved",), approved_by="button")
    tapped.approved_by = "button"
    tapped.guard = {"verdict": "pass"}
    shaped = _draft(at=NOW, states=("approved",))
    shaped.approved_by = "auto:policy"
    session.add_all([tapped, shaped])
    await session.flush()


async def test_a_removed_bank_question_redrafts_at_the_claim(session, jarvis):
    await _global(session, "draft")
    await _graduate(session, "info_gathering")
    person = await _person(session)
    trigger = await _inbound(session, person)
    row = await _ai_row(
        session, person, trigger, body="Yuk qaysi shahardan?", intent="info_gathering"
    )
    assert row.state == "approved"
    [entry] = [e for e in await qbank.load(session) if e.text == "Yuk qaysi shahardan?"]
    removed = await qbank.remove(session, entry.ref, by="button", now=NOW)
    assert removed.code == "removed"
    # Its Cyrillic twin matches the same way, its Russian twin is the same
    # question: both go too.
    left = await qbank.texts(session)
    assert "Юк қайси шаҳардан?" not in left and "Откуда груз?" not in left
    assert len(left) == len(qbank.SEEDS) - 3
    assert await outbox.claim_ready(session, TELEGRAM, now=NOW) == []
    back = await _reload(session, row.id)
    assert back.state == "draft" and back.last_error == gate.UNAPPROVED_STATEMENT
    assert back.approved_by is None and back.guard["shapes"] == ["free_question"]


def test_question_bank_seeds_pass_the_guard():
    seeds = qbank.SEEDS
    assert len(seeds) == len(set(seeds)) >= 30
    scripts = {redlines.script_of(text) for text in seeds}
    assert scripts == {"latin", "cyrillic", "russian"}
    for text in seeds:
        assert qbank.check_entry(text) is None, text
        verdict = redlines.check_outbound(
            text,
            "reply",
            client_script=redlines.script_of(text),
            question_bank=seeds,
        )
        assert verdict.passed and verdict.shapes_ok, (text, verdict)
        assert verdict.shapes.shapes == ("question",), text
        assert not any(ch.isdigit() for ch in text), text
    # Every twin group is three seeds, one per script.
    grouped = [text for group in qbank.TWINS for text in group]
    assert len(grouped) == len(set(grouped)) and set(grouped) <= set(seeds)
    for group in qbank.TWINS:
        assert [redlines.script_of(t) for t in group] == ["latin", "cyrillic", "russian"]
    # Each topic in each script: where from, where to, the cargo, the
    # weight, the cubes, the places, the code, who receives it, the papers,
    # the pickup address.
    for words in (
        ("shahardan", "шаҳардан", "откуда груз"),
        ("shaharga", "шаҳарга", "куда"),
        ("qanday yuk", "қандай юк", "какой груз"),
        ("kilo", "кило", "весит"),
        ("kub", "куб", "объём"),
        ("joy", "жой", "мест"),
        ("kodingiz", "кодингиз", "код клиента"),
        ("qabul", "қабул", "контактное"),
        ("hujjat", "ҳужжат", "документы"),
        ("manzil", "манзил", "адрес"),
    ):
        for word in words:
            assert any(word in text.casefold() for text in seeds), word


def test_bank_slot_fillers_are_name_honorific_and_codes():
    bank = qbank.SEEDS

    def check(text, *, names=(), tokens=frozenset()):
        return redlines.check_outbound(
            text,
            "reply",
            tokens,
            allowed_names=names,
            client_script=redlines.script_of(text),
            question_bank=bank,
        )

    for text, names, tokens in (
        ("Akmal aka, yuk qaysi shahardan?", ("Akmal Karimov",), frozenset()),
        ("Yuk qaysi shahardan, aka?", (), frozenset()),
        ("Rahmat aka, yuk qaysi shahardan?", (), frozenset()),
        ("GS367 yukingiz necha kilo?", (), frozenset({"GS367"})),
        ("Сколько мест, брат?", (), frozenset()),
        ("Юк қайси шаҳардан?", (), frozenset()),
    ):
        verdict = check(text, names=names, tokens=tokens)
        assert verdict.passed and verdict.shapes_ok, (text, verdict)
    # Nothing else is a slot: another word, another person, a code the
    # client never wrote, a time.
    for text, tokens in (
        ("Yuk qaysi shahardan keladi?", frozenset()),
        ("Sardor aka, yuk qaysi shahardan?", frozenset()),
        ("Yuk qaysi shaharga, Toshkentgami?", frozenset()),
    ):
        assert not check(text, tokens=tokens).shapes_ok, text
    assert not check("GS368 yukingiz necha kilo?", tokens=frozenset({"GS367"})).passed
    assert not check("Yuk qaysi shahardan, ertaga?").shapes_ok
    # Without a bank the grammar alone reads it (drafts, the intent call).
    assert redlines.check_shapes("Yuk qayerdan qayerga?").ok
    assert not redlines.check_shapes("Yuk qayerdan qayerga?", question_bank=bank).ok


async def test_savolbank_add_and_remove(session, jarvis, scoped):
    async def say(args: str) -> list[tuple[str, object]]:
        message = _Message(1)
        await handlers.cmd_question_bank(
            message, CommandObject(command="savolbank", args=args)
        )
        return message.sent

    [(listing, markup)] = await say("")
    assert listing.startswith("❓ Savollar banki (34 ta)")
    assert "1. Yuk qaysi shahardan?" in listing
    assert _payloads(markup)[0] == f"qb:d:{qbank.ref_of('Yuk qaysi shahardan?')}"
    assert keyboards.QBANK_PREFIX not in handlers._button_handlers()
    # Each addition is guarded: a time, a price, two questions, a statement.
    for text, code in (
        ("Yuk qachon tayyor bo'ladi?", qbank.NOT_A_QUESTION),
        ("Narxi qancha?", qbank.RED_LINE),
        ("Yuk 5 kilomi?", qbank.RED_LINE),
        ("Yuk qaysi shahardan? Necha kilo?", qbank.NOT_ONE_QUESTION),
        ("Yuk bizda.", qbank.RED_LINE),
        ("Salom", qbank.NOT_A_QUESTION),
        ("Yuk qaysi shahardan?", qbank.ALREADY),
    ):
        [(said, keyboard)] = await say(f"qo'sh {text}")
        assert said == replies.qbank_refused(code) and keyboard is None, text
    # A proposal is not in the bank before the owner's tap.
    [(proposed, keyboard)] = await say("qo'sh Mashinangiz bormi?")
    assert proposed.startswith("❓ Savollar bankiga qo'shilsinmi?\n«Mashinangiz bormi?»")
    ref = qbank.ref_of("Mashinangiz bormi?")
    assert _payloads(keyboard) == [f"qb:y:{ref}", f"qb:n:{ref}"]
    assert "Mashinangiz bormi?" not in await qbank.texts(session)
    tap = _Callback(f"qb:y:{ref}", 9)
    await handlers.on_question_bank_button(tap)
    assert tap.message.sent == [("✅ Savol qo'shildi: «Mashinangiz bormi?»", None)]
    assert "Mashinangiz bormi?" in await qbank.texts(session)
    again = _Callback(f"qb:y:{ref}", 9)
    await handlers.on_question_bank_button(again)
    assert again.message.sent == [(replies.QBANK_STALE, None)]
    # A declined proposal stays out.
    [(_, keyboard)] = await say("qo'sh Yukingiz qanday?")
    declined = _Callback(_payloads(keyboard)[1], 10)
    await handlers.on_question_bank_button(declined)
    assert declined.message.sent == [("✖️ Qo'shilmadi: «Yukingiz qanday?»", None)]
    assert "Yukingiz qanday?" not in await qbank.texts(session)
    # Removing is at once, by number or by the 🗑 button.
    [(removed, _)] = await say("o'chir 3")
    assert removed == "🗑 Savol olib tashlandi: «Qanday yuk?»"
    assert "Qanday yuk?" not in await qbank.texts(session)
    assert "Қандай юк?" not in await qbank.texts(session)
    assert "Какой груз?" not in await qbank.texts(session)
    [(missing, _)] = await say("o'chir 999")
    assert missing == replies.QBANK_NOT_FOUND
    bin_tap = _Callback(f"qb:d:{ref}", 11)
    await handlers.on_question_bank_button(bin_tap)
    assert bin_tap.message.sent == [
        ("🗑 Savol olib tashlandi: «Mashinangiz bormi?»", None)
    ]
    # Every edit is the audit trail, in the bank's own row.
    row = await session.get(m.JarvisPolicy, policy.QUESTION_BANK, populate_existing=True)
    fields = [e["field"] for e in row.history]
    assert fields.count(qbank.FIELD) >= 4 and qbank.PROPOSED_FIELD in fields
    assert {e["by"] for e in row.history} == {"command", "button"}
    actions = replies.jarvis_history(await rollout_actions(session))
    assert "Savollar banki" in actions and "savol" in actions


async def rollout_actions(session):
    from miya.jarvis import status

    return await status.recent_actions(session, limit=20)


# --- the red-line review cases ----------------------------------------------------------


def test_owner_edit_reasons():
    reasons, red = rollout.edit_reasons(
        "Assalomu alaykum! Mol bizda, aka.", "Assalomu alaykum!"
    )
    assert reasons == (rollout.STATEMENT_REASON,) and not red
    reasons, red = rollout.edit_reasons("Salom! Narxini keyin aytaman.", "Salom!")
    assert "price" in reasons and red
    # A courtesy or a question rewritten in other words is nothing to review.
    assert rollout.edit_reasons(
        "Assalomu alaykum! Yuk qaysi shahardan?", "Assalomu alaykum! Yuk qayerdan?"
    ) == ((), False)
    assert rollout.edit_reasons("Salom!", "Salom! Ertaga yozaman.") == ((), False)
    # Read with the row's context: a greeting with the client's name, a
    # question with the chat's code, an approved KB answer are not statements.
    assert rollout.edit_reasons(
        "Assalomu alaykum, Akmal aka! Yuk qaysi shahardan?",
        "Yuk qaysi shahardan?",
        allowed_names=("Akmal",),
    ) == ((), False)
    assert rollout.edit_reasons(
        "GS367 yukingiz necha kilo?", "Necha kilo?", allowed_tokens=frozenset({"GS367"})
    ) == ((), False)
    answer = "Biz Xitoydan yuk olib kelamiz."
    assert rollout.edit_reasons(f"Salom! {answer}", "Salom!", approved_kb=(answer,)) == (
        (),
        False,
    )
    assert rollout.edit_reasons(
        "Assalomu alaykum, Akmal aka! Yuk qaysi shahardan?", "Yuk qaysi shahardan?"
    ) == ((rollout.STATEMENT_REASON,), False)


async def test_owner_edit_is_kept_for_review(session, jarvis, scoped, monkeypatch):
    monkeypatch.setattr(settings, "jarvis_mode", "draft")
    await _global(session, "draft")
    person = await _person(session)
    trigger = await _inbound(session, person, text="Yuk bormi?")
    gate.register_guard(PassGuard())
    row = await _ai_row(
        session, person, trigger, body="Assalomu alaykum! Mol bizda, aka."
    )
    assert row.state == "draft"
    await outbox.mark_carded(session, row.id, message_id=5001, now=NOW)
    result = await cards.owner_reply(
        session, cards.CardRef(cards.DRAFT_CARD, row.id), "Assalomu alaykum!", now=NOW
    )
    assert result.code == "edited"
    [case] = await outbox.review_cases(session)
    assert case.id == row.id and case.code == outbox.REVIEW_EDIT
    assert case.ai_body == "Assalomu alaykum! Mol bizda, aka."
    assert case.owner_body == "Assalomu alaykum!"
    assert case.reasons == (rollout.STATEMENT_REASON,)
    # Once per row; a statement alone is no red-line incident.
    await cards.owner_reply(
        session, cards.CardRef(cards.DRAFT_CARD, row.id), "Salom!", now=NOW
    )
    assert len(await outbox.review_cases(session)) == 1
    found = await rollout.metrics(session, TELEGRAM, GREETING, now=NOW)
    assert found.since is None
    text = replies.jarvis_modes(await rollout.modes(session, now=NOW))
    assert "🧪 Qizil chiziq uchun ko'rib chiqiladigan holatlar: 1 ta" in text

    # A review that fails is contained: the owner's edit stands.
    async def broken(*args, **kwargs):
        raise RuntimeError("boom")

    second = await _ai_row(session, person, trigger, body="Salom, aka! Mol bizda.")
    await outbox.mark_carded(session, second.id, message_id=5002, now=NOW)
    monkeypatch.setattr(outbox, "note_review", broken)
    edited = await cards.owner_reply(
        session, cards.CardRef(cards.DRAFT_CARD, second.id), "Salom!", now=NOW
    )
    assert edited.code == "edited"
    assert (await _reload(session, second.id)).body == "Salom!"


# --- the responder at the auto level ---------------------------------------------------


async def test_courtesy_never_auto_after_a_red_line_ask(session, on, monkeypatch):
    """A client message with a payment claim or a price, deadline or promise
    ask is escalated (JV-16) and only the holding reply is queued, which is
    never sent on its own (JV-23): even with every stored mode and the
    ceiling at auto, no courtesy-only reply goes out for it."""
    monkeypatch.setattr(settings, "jarvis_mode", "auto")
    await policy.set_mode(
        session, scope=policy.GLOBAL, mode="auto", by="command", now=NOW
    )
    on.draft = (
        '{"reply": "Assalomu alaykum, aka!", "needs_owner": false, "reason_code": "none"}'
    )
    t0 = datetime.now(TZ).replace(microsecond=0)
    for i, text in enumerate(
        (
            "Assalomu alaykum, pulni tashladim",
            "Salom, narxi qancha?",
            "Salom aka, ertaga yetkazasizmi?",
            "Assalomu alaykum, va'da bering",
        )
    ):
        dm = 7240 + i
        person = await _responder_setup(session, dm=dm, name=f"Mijoz{i}")
        await _says(session, person, text, at=t0, dm=dm)
        await _tick(session, t0 + timedelta(minutes=1))
        [escalation] = await _escalations(session, dm=dm)
        assert escalation.state == "open", text
        rows = await _responder_rows(session, dm=dm)
        assert rows and all(r.origin == "stall" for r in rows), text
        for row in rows:
            assert row.state != outbox.APPROVED and row.approved_by is None, text
    assert "draft" not in on.roles()


# --- the schema ------------------------------------------------------------------------


def _migration():
    path = ROOT / "alembic" / "versions" / "0028_auto_shapes.py"
    spec = importlib.util.spec_from_file_location("migration_0028", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0028_freezes_the_model():
    migration = _migration()
    assert migration.revision == "0028_auto_shapes" and len(migration.revision) <= 32
    assert migration.down_revision == "0027_knowledge_base"
    assert migration.AUTO_SHAPES_CHECK == m.OUTBOX_AUTO_SHAPES_CHECK
    assert migration.SCOPE_PATTERN == m.JARVIS_SCOPE_PATTERN
    assert migration.QUESTION_BANK_SCOPE == policy.QUESTION_BANK == qbank.SCOPE
    assert migration.AUTO_APPROVER == gate.AUTO_APPROVER
    assert migration.UNAPPROVED == gate.UNAPPROVED_STATEMENT
    source = (ROOT / "alembic" / "versions" / "0028_auto_shapes.py").read_text()
    assert "import miya" not in source and "from miya" not in source


async def test_migration_0028_takes_the_approver_off_forged_rows(session):
    """The data step: an approved autonomous row without a passing shape
    check goes back to the card, a finished one loses the approver; one on
    the wire stops the upgrade."""
    migration = _migration()

    def forged(state: str, key: str) -> m.OutboxMessage:
        return m.OutboxMessage(
            channel=TELEGRAM,
            chat_key=KEY,
            origin="auto_reply",
            authored_by="ai",
            body="Salom",
            body_sha256="0" * 64,
            guard={"verdict": "pass"},
            state=state,
            approved_by="auto:policy",
            idempotency_key=key,
            history=[],
        )

    def run(sync_session, states):
        conn = sync_session.connection()
        conn.execute(
            sa.text(
                "ALTER TABLE outbox_messages "
                "DROP CONSTRAINT ck_outbox_messages_auto_shapes"
            )
        )
        conn.execute(
            sa.text("ALTER TABLE jarvis_policy DROP CONSTRAINT ck_jarvis_policy_scope")
        )
        conn.execute(
            sa.text(
                "ALTER TABLE jarvis_policy ADD CONSTRAINT ck_jarvis_policy_scope "
                f"CHECK (scope ~ '{migration.PREVIOUS_SCOPE_PATTERN}')"
            )
        )
        for state in states:
            sync_session.add(forged(state, f"forged-{state}"))
        sync_session.flush()
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()

    await session.run_sync(run, ("approved", "blocked"))
    rows = {
        r.idempotency_key: r
        for r in await session.scalars(
            sa.select(m.OutboxMessage).execution_options(populate_existing=True)
        )
    }
    approved = rows["forged-approved"]
    assert approved.state == "draft" and approved.approved_by is None
    assert approved.last_error == gate.UNAPPROVED_STATEMENT
    assert approved.history[0]["by"] == "migration"
    blocked = rows["forged-blocked"]
    assert blocked.state == "blocked" and blocked.approved_by is None
    assert blocked.history[-1]["field"] == "approved_by"
    await session.rollback()

    with pytest.raises(RuntimeError, match="on the wire"):
        await session.run_sync(run, ("sending",))
    await session.rollback()


def test_migration_0028_round_trips():
    url = os.environ.get("MIYA_MIGRATION_DATABASE_URL")
    if not url:
        pytest.skip("MIYA_MIGRATION_DATABASE_URL is not set")
    from tests.test_outbox import _alembic

    def constraints() -> dict[str, str]:
        engine = sa.create_engine(url)
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    sa.text(
                        "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conname IN ('ck_outbox_messages_auto_shapes', "
                        "'ck_jarvis_policy_scope')"
                    )
                )
                return {str(name): str(definition) for name, definition in rows}
        finally:
            engine.dispose()

    _alembic(url, "upgrade", "head")
    found = constraints()
    assert "shapes_ok" in found["ck_outbox_messages_auto_shapes"]
    assert "question_bank" in found["ck_jarvis_policy_scope"]
    _alembic(url, "downgrade", "0027_knowledge_base")
    found = constraints()
    assert "ck_outbox_messages_auto_shapes" not in found
    assert "question_bank" not in found["ck_jarvis_policy_scope"]
    _alembic(url, "upgrade", "head")
    assert "ck_outbox_messages_auto_shapes" in constraints()

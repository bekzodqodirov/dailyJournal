"""The owner moves Jarvis's global mode from the bot, never by hand in psql.

`/jarvis qoralama` (or `/jarvis rejim qoralama`) asks first and the owner's
«✅ Ha, qoralama rejimi» tap raises observation (shadow) to draft — a
loosening change, so a tap like JV-12's; `/jarvis kuzatuv` lowers it at once.
Nothing here ever stores ``auto``: that stays the JV-23 graduation's. The env
ceiling JARVIS_MODE still caps what is stored, and the reply says which .env
line holds it back. `/jarvis yoq` in observation mode says that nothing will
be shown. Real PostgreSQL, the handlers inside the test session.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from aiogram.filters import CommandObject

from miya.bot import handlers, keyboards, replies
from miya.config import settings
from miya.db import models as m
from miya.jarvis import chatkey, policy, rollout

TZ = settings.tz
TELEGRAM = chatkey.TELEGRAM


@pytest.fixture
def jarvis(monkeypatch):
    monkeypatch.setattr(settings, "jarvis_enabled", True)
    monkeypatch.setattr(settings, "jarvis_mode", "draft")


@pytest.fixture
def scoped(session, monkeypatch):
    @asynccontextmanager
    async def _scope():
        yield session
        await session.flush()

    monkeypatch.setattr(handlers, "session_scope", _scope)
    return session


class _Message:
    def __init__(self, message_id: int = 1) -> None:
        self.message_id = message_id
        self.sent: list[tuple[str, object]] = []
        self.edited: list[tuple[str, object]] = []

    async def answer(self, text, **kwargs):
        self.sent.append((text, kwargs.get("reply_markup")))
        return SimpleNamespace(message_id=8000 + len(self.sent))

    async def edit_text(self, text, **kwargs):
        self.edited.append((text, kwargs.get("reply_markup")))

    async def edit_reply_markup(self, **kwargs):
        return None


class _Callback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.message = _Message(7001)

    async def answer(self, *args, **kwargs):
        return None


async def _jarvis(args: str | None) -> tuple[str, object]:
    message = _Message()
    await handlers.cmd_jarvis(
        message, CommandObject(prefix="/", command="jarvis", args=args)
    )
    [sent] = message.sent
    return sent


def _payloads(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


async def _stored(session) -> str | None:
    row = await session.get(m.JarvisPolicy, policy.GLOBAL, populate_existing=True)
    return row.mode if row is not None else None


async def _seed(session, mode: str = "shadow") -> None:
    """The migration's seed: ('global', 'shadow')."""
    await policy.set_mode(
        session,
        scope=policy.GLOBAL,
        mode=mode,
        by="migration",
        now=datetime.now(TZ) - timedelta(days=1),
    )


async def test_qoralama_asks_and_only_the_tap_raises_the_mode(session, jarvis, scoped):
    await _seed(session)
    text, markup = await _jarvis("qoralama")
    assert text == replies.JARVIS_MODE_DRAFT_ASK
    assert _payloads(markup) == ["jm:d", "jm:n"]
    # Asking changed nothing.
    assert await _stored(session) == "shadow"

    tap = _Callback("jm:d")
    await handlers.on_jarvis_mode_button(tap)
    assert tap.message.edited == [(replies.JARVIS_MODE_NOW_DRAFT, None)]
    assert await _stored(session) == "draft"
    row = await session.get(m.JarvisPolicy, policy.GLOBAL)
    last = row.history[-1]
    assert (last["field"], last["old"], last["new"], last["by"]) == (
        "mode",
        "shadow",
        "draft",
        "button",
    )
    # Every reply kind now resolves to draft, so the graduation clock can run
    # (rollout.Metrics.ready needs stored == draft).
    policies = await policy.load(session)
    assert policy.stored_mode(policies, TELEGRAM, "greeting") == "draft"
    assert (
        await rollout.metrics(session, TELEGRAM, "greeting", now=datetime.now(TZ))
    ).stored == "draft"

    # A second tap, or the command again, changes nothing and says so.
    again = _Callback("jm:d")
    await handlers.on_jarvis_mode_button(again)
    assert again.message.edited == [("Jarvis allaqachon qoralama rejimida.", None)]
    assert await _jarvis("qoralama") == ("Jarvis allaqachon qoralama rejimida.", None)
    assert len((await session.get(m.JarvisPolicy, policy.GLOBAL)).history) == 2


async def test_a_missing_global_row_is_observation_and_can_be_raised(
    session, jarvis, scoped
):
    """No seeded row: the default is shadow, the tap creates the row."""
    text, markup = await _jarvis("rejim qoralama")
    assert text == replies.JARVIS_MODE_DRAFT_ASK and markup is not None
    await handlers.on_jarvis_mode_button(_Callback("jm:d"))
    assert await _stored(session) == "draft"


async def test_no_keeps_the_mode(session, jarvis, scoped):
    await _seed(session)
    tap = _Callback("jm:n")
    await handlers.on_jarvis_mode_button(tap)
    assert tap.message.edited == [(replies.JARVIS_MODE_KEPT, None)]
    assert await _stored(session) == "shadow"
    # A forged payload is ignored.
    for data in ("jm:a", "jm:d:auto", "jm:"):
        await handlers.on_jarvis_mode_button(_Callback(data))
    assert await _stored(session) == "shadow"


async def test_kuzatuv_lowers_at_once_and_names_the_kinds_it_cannot_reach(
    session, jarvis, scoped
):
    await _seed(session, "draft")
    # A graduated reply kind keeps its own mode: the owner is told so.
    await policy.set_mode(
        session,
        scope=policy.channel_intent_scope(TELEGRAM, "greeting"),
        mode="auto",
        by="button",
        now=datetime.now(TZ),
    )
    text, markup = await _jarvis("kuzatuv")
    assert markup is None
    assert text.startswith(replies.JARVIS_MODE_NOW_SHADOW)
    assert "Telegram · salomlashish" in text and "/jarvis toxtat" in text
    assert await _stored(session) == "shadow"
    row = await session.get(m.JarvisPolicy, policy.GLOBAL)
    assert row.history[-1]["by"] == "command"
    policies = await policy.load(session)
    assert policy.stored_mode(policies, TELEGRAM, "info_gathering") == "shadow"
    assert policy.stored_mode(policies, TELEGRAM, "greeting") == "auto"

    # Without such kinds, a second kuzatuv just says it already is.
    await session.delete(
        await session.get(
            m.JarvisPolicy, policy.channel_intent_scope(TELEGRAM, "greeting")
        )
    )
    await session.flush()
    assert await _jarvis("rejim kuzatuv") == ("Jarvis allaqachon kuzatuv rejimida.", None)


async def test_the_env_ceiling_is_named(session, jarvis, scoped, monkeypatch):
    await _seed(session)
    monkeypatch.setattr(settings, "jarvis_mode", "shadow")
    ceiling = replies.JARVIS_MODE_CEILING_LOW.format(m="shadow")
    text, markup = await _jarvis("qoralama")
    assert text == f"{replies.JARVIS_MODE_DRAFT_ASK}\n{ceiling}" and markup is not None
    tap = _Callback("jm:d")
    await handlers.on_jarvis_mode_button(tap)
    # Stored, but capped: the effective mode stays shadow until .env changes.
    assert tap.message.edited == [(f"{replies.JARVIS_MODE_NOW_DRAFT}\n{ceiling}", None)]
    assert await _stored(session) == "draft"
    policies = await policy.load(session)
    assert (
        policy.effective_mode(
            policies, TELEGRAM, "greeting", env_ceiling=policy.ceiling()
        )
        == "shadow"
    )
    monkeypatch.setattr(settings, "jarvis_enabled", False)
    text, _ = await _jarvis("qoralama")
    assert text == f"Jarvis allaqachon qoralama rejimida.\n{replies.JARVIS_SWITCHED_OFF}"
    assert "JARVIS_ENABLED=true" in replies.JARVIS_SWITCHED_OFF
    assert "make reload" in replies.JARVIS_SWITCHED_OFF


async def test_auto_is_never_a_command(session, jarvis, scoped, monkeypatch):
    monkeypatch.setattr(settings, "jarvis_mode", "auto")
    await _seed(session)
    for args in ("auto", "avtomatik", "rejim auto", "rejim avtomatik", "qoralama auto"):
        assert await _jarvis(args) == (replies.JARVIS_USAGE, None)
    assert await _stored(session) == "shadow"
    with pytest.raises(ValueError):
        await policy.set_global_mode(
            session,
            mode="auto",
            by="command",
            now=datetime.now(TZ),  # type: ignore[arg-type]
        )


async def test_yoq_in_observation_says_nothing_is_shown(session, jarvis, scoped):
    await _seed(session)
    assert await _jarvis("toxtat") == (replies.JARVIS_PAUSED, None)
    text, _ = await _jarvis("yoq")
    assert text == f"✅ Jarvis yoqildi (kuzatuv rejimi).\n{replies.JARVIS_SHADOW_HINT}"
    assert "/jarvis qoralama" in replies.JARVIS_SHADOW_HINT
    status, _ = await _jarvis(None)
    assert status.endswith(replies.JARVIS_SHADOW_HINT)
    # Phase 4 channels that are not connected are not listed.
    assert "Kanallar: Telegram " in status
    assert "WhatsApp" not in status and "Gmail" not in status
    # In draft mode, «yoq» is the plain line.
    await _seed(session, "draft")
    assert await _jarvis("yoq") == ("✅ Jarvis yoqildi (qoralama rejimi).", None)


async def test_the_failure_is_contained(session, jarvis, monkeypatch):
    @asynccontextmanager
    async def _down():
        raise RuntimeError("database down")
        yield  # pragma: no cover

    monkeypatch.setattr(handlers, "session_scope", _down)
    assert await _jarvis("kuzatuv") == (replies.JARVIS_FAILED, None)
    assert await _jarvis("qoralama") == (replies.JARVIS_FAILED, None)
    tap = _Callback("jm:d")
    await handlers.on_jarvis_mode_button(tap)
    assert tap.message.sent == [(replies.JARVIS_FAILED, None)]


def test_help_usage_and_the_button_registry():
    assert keyboards.JARVIS_MODE_PREFIX == "jm"
    assert keyboards.JARVIS_MODE_PREFIX not in handlers._button_handlers()
    for text in (replies.HELP, replies.JARVIS_USAGE):
        assert "/jarvis qoralama" in text and "/jarvis kuzatuv" in text
    assert "yoq [kanal] — to'xtatishni bekor qilish" in replies.HELP
    assert "whatsapp" not in replies.JARVIS_UNKNOWN_CHANNEL.split("(")[0].casefold()

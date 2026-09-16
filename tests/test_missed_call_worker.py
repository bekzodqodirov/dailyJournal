"""The worker's missed-call nudge job (build step 6).

The core — the loop detector, collect_missed's once-plus-snooze discipline —
is covered in test_phone_events_core.py. Here: the job itself. It sends one
short message per open missed-call loop with the ✅ Bog'landim / ⏰ Ertaga
keyboard, nudges each loop once ever (plus once more after a snooze
expires), holds inside quiet hours, marks only what notify() actually
delivered, caps a sweep at MAX_PER_SWEEP, and is registered after the
nudges job with the heartbeat/health pair still last. Telegram is stubbed;
the database is real.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import sqlalchemy as sa

from miya.bot import keyboards, replies
from miya.config import settings
from miya.db import models as m
from miya.services import nudges, phone_events
from miya.worker import main as worker
from tests.test_chat_notices import _Bot
from tests.test_health_worker import _KeepEngine

TZ = settings.tz
DEVICE = "phone-1"


class _MarkupBot(_Bot):
    """The chat-notices stub, also keeping each message's keyboard."""

    def __init__(self, *, reachable: bool = True) -> None:
        super().__init__(reachable=reachable)
        self.markups: list = []

    async def send_message(self, chat_id, text, **kwargs) -> None:
        await super().send_message(chat_id, text, **kwargs)
        self.markups.append(kwargs.get("reply_markup"))


class _FlakyBot(_Bot):
    """Delivers the first ``deliverable`` messages, then Telegram is down."""

    def __init__(self, deliverable: int) -> None:
        super().__init__()
        self.deliverable = deliverable

    async def send_message(self, chat_id, text, **kwargs) -> None:
        if len(self.sent) >= self.deliverable:
            raise RuntimeError("telegram is down")
        await super().send_message(chat_id, text, **kwargs)


def _quiet(monkeypatch, value: bool) -> None:
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: value)


def _event(
    call_log_id: int,
    *,
    number: str | None = "+998901234567",
    contact_name: str | None = None,
    minutes_ago: int = 120,
    call_type: str = "missed",
) -> dict:
    started = datetime.now(TZ) - timedelta(minutes=minutes_ago)
    return {
        "call_log_id": call_log_id,
        "started_at": started.isoformat(),
        "duration_seconds": 0,
        "type": call_type,
        "number": number,
        "contact_name": contact_name,
    }


async def _missed(session, *events: dict) -> None:
    outcome = await phone_events.ingest_call_events(session, DEVICE, list(events))
    assert outcome.rejected == []
    assert outcome.accepted == len(events)
    await session.commit()


async def _interaction_id(session, call_log_id: int) -> int:
    key = phone_events.call_event_key(DEVICE, call_log_id)
    found = await session.scalar(
        sa.select(m.Interaction.id).where(m.Interaction.media["event_key"].astext == key)
    )
    assert found is not None
    return found


async def _nudged_refs(session) -> list[str]:
    await session.commit()  # see the job's own committed rows, not a snapshot
    return list(
        await session.scalars(
            sa.select(m.ReminderLog.ref)
            .where(m.ReminderLog.kind == nudges.MISSED_KIND)
            .order_by(m.ReminderLog.id)
        )
    )


# --- one nudge, with the buttons ---------------------------------------------


async def test_a_missed_call_is_nudged_with_the_contact_buttons(session, monkeypatch):
    await _missed(session, _event(1, contact_name="Akmal"))
    interaction_id = await _interaction_id(session, 1)
    _quiet(monkeypatch, False)
    bot = _MarkupBot()

    await worker.missed_call_nudge_job(bot)

    [text] = bot.sent
    assert "Akmal" in text
    assert bot.markups == [keyboards.missed_actions(interaction_id)]
    assert await _nudged_refs(session) == [str(interaction_id)]


async def test_the_text_is_the_reply_modules_own(session, monkeypatch):
    """One formatter, not two: the job sends replies.missed_nudge verbatim."""
    await _missed(session, _event(1, contact_name="Akmal"))
    _quiet(monkeypatch, False)
    [loop] = await nudges.collect_missed(session)
    bot = _Bot()

    await worker.missed_call_nudge_job(bot)

    assert bot.sent == [replies.missed_nudge(loop)]


async def test_one_nudge_ever_then_only_the_brief_carries_it(session, monkeypatch):
    await _missed(session, _event(1, contact_name="Akmal"))
    _quiet(monkeypatch, False)
    bot = _Bot()

    await worker.missed_call_nudge_job(bot)
    await worker.missed_call_nudge_job(bot)

    assert len(bot.sent) == 1
    assert len(await _nudged_refs(session)) == 1


async def test_a_snooze_earns_exactly_one_more_nudge(session, monkeypatch):
    await _missed(session, _event(1, contact_name="Akmal"))
    interaction_id = await _interaction_id(session, 1)
    _quiet(monkeypatch, False)
    bot = _Bot()
    await worker.missed_call_nudge_job(bot)
    assert len(bot.sent) == 1

    # ⏰ Ertaga: while the snooze runs, the sweep stays silent.
    interaction = await session.get(m.Interaction, interaction_id)
    nudges.snooze(interaction, until=datetime.now(TZ) + timedelta(hours=6))
    await session.commit()
    await worker.missed_call_nudge_job(bot)
    assert len(bot.sent) == 1

    # The snooze expires (rewritten to the moment of the first nudge, which
    # is already in the past): nudged once more, and then never again.
    first_nudge = await session.scalar(
        sa.select(m.ReminderLog.sent_at).where(m.ReminderLog.kind == nudges.MISSED_KIND)
    )
    interaction = await session.get(m.Interaction, interaction_id)
    nudges.snooze(interaction, until=first_nudge)
    await session.commit()
    await worker.missed_call_nudge_job(bot)
    assert len(bot.sent) == 2
    await worker.missed_call_nudge_job(bot)
    assert len(bot.sent) == 2
    assert await _nudged_refs(session) == [str(interaction_id)] * 2


# --- quiet hours and delivery ------------------------------------------------


async def test_quiet_hours_hold_the_nudge_and_never_drop_it(session, monkeypatch):
    await _missed(session, _event(1, contact_name="Akmal"))
    _quiet(monkeypatch, True)
    bot = _Bot()

    # 02:00 — the ring keeps until morning.
    await worker.missed_call_nudge_job(bot)
    assert bot.sent == []
    assert await _nudged_refs(session) == []

    _quiet(monkeypatch, False)
    await worker.missed_call_nudge_job(bot)
    assert len(bot.sent) == 1


async def test_an_undelivered_nudge_is_not_marked_and_is_retried(session, monkeypatch):
    await _missed(session, _event(1, contact_name="Akmal"))
    _quiet(monkeypatch, False)
    bot = _Bot(reachable=False)

    await worker.missed_call_nudge_job(bot)
    assert bot.sent == []
    assert await _nudged_refs(session) == []

    bot.reachable = True
    await worker.missed_call_nudge_job(bot)
    assert len(bot.sent) == 1
    assert len(await _nudged_refs(session)) == 1


async def test_only_what_was_delivered_is_marked(session, monkeypatch):
    """Telegram dies mid-sweep: the two delivered nudges are marked, the
    third is not, and the next sweep sends only the third."""
    await _missed(
        session,
        _event(1, number="+998901111111", minutes_ago=180),
        _event(2, number="+998902222222", minutes_ago=150),
        _event(3, number="+998903333333", minutes_ago=120),
    )
    ids = [await _interaction_id(session, n) for n in (1, 2, 3)]
    _quiet(monkeypatch, False)
    bot = _FlakyBot(deliverable=2)

    await worker.missed_call_nudge_job(bot)
    assert len(bot.sent) == 2
    # Oldest first, and only the delivered two in the ledger.
    assert await _nudged_refs(session) == [str(ids[0]), str(ids[1])]

    bot.deliverable = 3
    await worker.missed_call_nudge_job(bot)
    assert len(bot.sent) == 3
    assert await _nudged_refs(session) == [str(i) for i in ids]


async def test_a_sweep_is_capped_and_the_tail_comes_next_sweep(session, monkeypatch):
    count = nudges.MAX_PER_SWEEP + 2
    await _missed(
        session,
        *[
            _event(n, number=f"+9989012345{n:02d}", minutes_ago=180 - n)
            for n in range(count)
        ],
    )
    _quiet(monkeypatch, False)
    bot = _Bot()

    await worker.missed_call_nudge_job(bot)
    assert len(bot.sent) == nudges.MAX_PER_SWEEP

    # Nothing dropped: the tail qualifies again and goes out next sweep.
    await worker.missed_call_nudge_job(bot)
    assert len(bot.sent) == count
    assert len(await _nudged_refs(session)) == count


async def test_the_morning_brief_keyboard_carries_the_missed_rows(session, monkeypatch):
    """The 09:00 brief the worker sends is the owner's daily surface: the
    📵 section must arrive with its ✅ Bog'landim / ⏰ rows, not as bare text."""
    await _missed(session, _event(1, minutes_ago=180))
    interaction_id = await _interaction_id(session, 1)
    bot = _MarkupBot()

    assert await worker.brief_job(bot) is True

    [markup] = bot.markups
    assert markup is not None
    payloads = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert f"rec:ma:m{interaction_id}" in payloads
    assert f"rec:ms:m{interaction_id}" in payloads


# --- registration ------------------------------------------------------------


async def test_the_job_is_registered_after_nudges_and_before_the_monitors(monkeypatch):
    """run() wires missed_call_nudge right after nudges, on the nudges'
    cadence, with the heartbeat/health pair still last so the job listener
    covers it too."""
    registered: list[str] = []
    jobs: dict[str, tuple] = {}

    class _Scheduler:
        def __init__(self, *args, **kwargs):
            pass

        def add_job(self, func, trigger, *, id, **kwargs):
            registered.append(id)
            jobs[id] = (trigger, kwargs)

        def add_listener(self, callback, mask):
            pass

        def start(self):
            pass

        def get_jobs(self):
            return registered

        def shutdown(self, wait=False):
            pass

    class _StopAtOnce:
        def set(self):
            pass

        async def wait(self):
            return None

    async def _beat(session, component, *, detail=None, now=None):
        return None

    async def _no_catch_up(bot):
        return None

    class _FakeBot:
        def __init__(self, **kwargs):
            self.session = SimpleNamespace(close=self._close)

        async def _close(self):
            return None

    monkeypatch.setattr(settings, "assistant_bot_token", "123:abc")
    monkeypatch.setattr(settings, "owner_telegram_id", 1)
    monkeypatch.setattr(worker, "AsyncIOScheduler", _Scheduler)
    monkeypatch.setattr(worker, "Bot", _FakeBot)
    monkeypatch.setattr(worker.asyncio, "Event", _StopAtOnce)
    monkeypatch.setattr(worker.health, "beat", _beat)
    monkeypatch.setattr(worker, "catch_up", _no_catch_up)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda *a, **k: None)
    monkeypatch.setattr(worker, "engine", _KeepEngine())

    await worker.run()

    assert registered.index("missed_call_nudge") == registered.index("nudges") + 1
    assert registered[-2:] == ["heartbeat", "health"]
    trigger, kwargs = jobs["missed_call_nudge"]
    assert trigger.interval == timedelta(minutes=30)
    assert kwargs["max_instances"] == 1
    assert kwargs["coalesce"] is True

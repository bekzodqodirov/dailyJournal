"""Build step 2, the verifiers' findings on the worker and the mutation paths.

Every test here failed on the code the two adversarial reviews looked at: a
receipt sent twice when the window job and the batch poll coincide, a
morning brief sent at night by the startup catch-up, a whole group backlog
billed at full price for one addressed line, a "Ha" that switched on a chat
nobody was asked about, a paid call erased from usage_log when applying it
failed, a /chats decision that did not count as an answer, a follow-up that
became a fresh nudge after "Javob berdim", and a channel announced as a
group. Telegram and Anthropic are stubbed; the database is real.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
from types import SimpleNamespace

import sqlalchemy as sa

from miya.bot import handlers, replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import ChatType, Direction, InteractionSource, WindowStatus
from miya.services import batch, chats, nudges, reminders, windows
from miya.services import extraction as ex
from miya.worker import main as worker
from tests.test_close_and_correct import (
    _Callback,
    _Message,
    bound,  # noqa: F401 — fixture
)
from tests.test_open_loops import _ago, _monitor, _msg, _now, _person

TZ = settings.tz
PRIVATE = 1001
GROUP = -1002
CHANNEL = -1003


class _Bot:
    """Records what the owner would have received.

    ``send_message`` yields to the loop the way a real Telegram round trip
    does, so two jobs running at once actually interleave.
    """

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.documents: list[tuple] = []

    async def send_message(self, chat_id, text, **kwargs) -> None:
        await asyncio.sleep(0.01)
        self.sent.append(text)

    async def send_document(self, chat_id, document, **kwargs) -> None:
        await asyncio.sleep(0.01)
        self.documents.append((document, kwargs))


class _Editable(_Message):
    async def edit_text(self, text, reply_markup=None):
        self.sent.append((text, reply_markup))


def _dialog(chat_id, chat_type, title, *, is_bot=False):
    return chats.DialogInfo(
        tg_chat_id=chat_id, chat_type=chat_type, title=title, is_bot=is_bot
    )


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime.combine(_now().date(), time(hour, minute), tzinfo=TZ)


async def _none(now):
    return None


# --- B1. one receipt, however many jobs deliver it ------------------------------


def _parked(text: str, *, now: datetime) -> m.Interaction:
    return m.Interaction(
        source=InteractionSource.telegram_userbot,
        tg_chat_id=GROUP,
        occurred_at=now,
        raw_text="oyna",
        meta={
            "kind": "window",
            batch.NOTICE_KEY: {
                "text": text,
                "chat": "GZ logistika",
                "counts": {"debts": 1},
                "queued_at": now.isoformat(),
            },
        },
    )


async def test_two_coinciding_notice_jobs_send_each_receipt_once(session, monkeypatch):
    """window_job and batch_poll_job both deliver receipts, and their
    intervals coincide every 15 minutes; max_instances is per job id, so the
    two ran side by side and both read the queue before either marked it."""
    session.add(_parked("receipt", now=datetime.now(TZ)))
    await session.commit()
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: False)
    # A fresh lock per test: an asyncio.Lock binds to the loop that first
    # contends it, and every test here runs on its own.
    monkeypatch.setattr(worker, "_notice_lock", asyncio.Lock(), raising=False)
    bot = _Bot()

    await asyncio.gather(worker.chat_notice_job(bot), worker.chat_notice_job(bot))

    assert bot.sent == ["receipt"]
    assert await batch.pending_notices(session) == []


# --- B2. the catch-up brief keeps to the daytime --------------------------------


async def test_brief_catch_up_waits_for_the_brief_time(session, monkeypatch):
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: False)
    assert await worker._brief_is_missing(_at(8, 59)) is False


async def test_brief_catch_up_is_silent_inside_quiet_hours(session, monkeypatch):
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: True)
    assert await worker._brief_is_missing(_at(10)) is False


async def test_brief_catch_up_is_stale_once_the_evening_report_is_due(
    session, monkeypatch
):
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: False)
    report_at = settings.report_time_parsed
    assert await worker._brief_is_missing(_at(report_at.hour, report_at.minute)) is False


async def test_brief_catch_up_sends_a_missed_daytime_brief_once(session, monkeypatch):
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: False)
    assert await worker._brief_is_missing(_at(9, 1)) is True
    assert await worker._brief_is_missing(_at(18, 59)) is True

    session.add(m.ReminderLog(kind=worker.BRIEF_KIND, ref=_now().date().isoformat()))
    await session.commit()
    assert await worker._brief_is_missing(_at(9, 1)) is False


async def test_a_restart_at_night_sends_no_brief(session, monkeypatch):
    """The first start after the migration, or a reboot at 23:45, with the
    real quiet hours and the real report time: nothing at night."""
    assert reminders.in_quiet_hours(_at(23, 45))
    monkeypatch.setattr(settings, "backup_age_recipient", "")
    monkeypatch.setattr(worker, "_missed_report_day", _none)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return _at(23, 45)

    monkeypatch.setattr(worker, "datetime", _Clock)
    bot = _Bot()

    await worker.catch_up(bot)

    assert bot.sent == []


# --- B3. only the slice with the addressed line is urgent -----------------------


async def test_only_the_slice_holding_the_addressed_line_is_instant(session):
    """A week of backfill after "Ha", or a day of the userbot being down,
    yields several windows from one backlog. One line aimed at the owner
    makes the backlog close after five minutes — but only the window that
    actually holds it is worth full price."""
    akmal = await _person(session)
    await _monitor(session, GROUP, ChatType.group)
    total = settings.window_max_messages + 5
    # Ten minutes old: past the five-minute idle of an addressed backlog,
    # well inside the thirty minutes plain group traffic waits.
    start = _ago(minutes=10)
    for index in range(total):
        await _msg(
            session,
            f"gap {index}",
            at=start + timedelta(seconds=index),
            chat_id=GROUP,
            person=akmal,
            to_me=index == total - 1,
        )

    created = await windows.flush_ready_windows(session)

    assert [(w.message_count, w.instant) for w in created] == [
        (settings.window_max_messages, False),
        (5, True),
    ]
    assert "→ ME" in created[1].text and "→ ME" not in created[0].text
    # The un-addressed slice is the batch's; the addressed one the window job's.
    assert [w.id for w in await windows.pending_windows(session)] == [created[0].id]
    assert [w.id for w in await windows.pending_windows(session, instant=True)] == [
        created[1].id
    ]


async def test_a_private_backlog_is_instant_throughout(session):
    akmal = await _person(session)
    await _monitor(session, PRIVATE, ChatType.private)
    start = _ago(minutes=10)
    for index in range(settings.window_max_messages + 2):
        await _msg(
            session,
            f"gap {index}",
            at=start + timedelta(seconds=index),
            chat_id=PRIVATE,
            person=akmal,
        )

    created = await windows.flush_ready_windows(session)

    assert len(created) == 2 and all(w.instant for w in created)


# --- B4. a tap counts only for a chat that was asked about ----------------------


async def test_a_tap_for_a_chat_nobody_asked_about_changes_nothing(session):
    private = await chats.ensure_monitor(session, _dialog(111, ChatType.private, "Akmal"))
    bot_dm = await chats.ensure_monitor(
        session, _dialog(222, ChatType.private, "MIYA", is_bot=True)
    )
    service = await chats.ensure_monitor(
        session, _dialog(chats.TELEGRAM_SERVICE_ID, ChatType.private, "Telegram")
    )
    unasked = await chats.ensure_monitor(session, _dialog(-333, ChatType.group, "GZ"))
    # A private chat the owner toggled in /chats carries asked_at too — still
    # not a group, still no backfill.
    chats.mark_asked(private)
    await session.flush()

    for monitor in (private, bot_dm, service, unasked):
        assert await chats.accept_join(session, monitor.id) is None
        assert await chats.decline_join(session, monitor.id) is None

    assert private.monitor_enabled is True and private.backfill_requested_at is None
    assert bot_dm.monitor_enabled is False and bot_dm.asked_at is None
    assert service.monitor_enabled is False and service.backfill_requested_at is None
    assert unasked.monitor_enabled is False and unasked.asked_at is None
    assert await chats.pending_backfills(session) == []
    # The question for the group is still owed.
    assert [w.id for w in await chats.awaiting_join_question(session)] == [unasked.id]


async def test_a_tap_for_an_asked_group_or_channel_still_counts(session):
    group = await chats.ensure_monitor(session, _dialog(-333, ChatType.group, "GZ"))
    channel = await chats.ensure_monitor(
        session, _dialog(CHANNEL, ChatType.channel, "Bojxona")
    )
    chats.mark_asked(group)
    chats.mark_asked(channel)
    await session.flush()

    accepted = await chats.accept_join(session, group.id)
    declined = await chats.decline_join(session, channel.id)

    assert accepted is group and group.monitor_enabled is True
    assert group.backfill_requested_at is not None
    assert declined is channel and channel.monitor_enabled is False
    assert [p.id for p in await chats.pending_backfills(session)] == [group.id]


# --- B5. a paid call is never erased ---------------------------------------------


def _usage() -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=500,
        output_tokens=80,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )


async def _extract_ok(text, *, now=None):
    return ex.ExtractionOutcome(
        result=ex.ExtractionResult(
            summary="Akmal 5 mln oldi",
            debts=[
                ex.ExtractedDebt(
                    direction="they_owe_me",
                    person="Akmal",
                    amount=5_000_000,
                    reason="yuk",
                )
            ],
        ),
        model=settings.extract_model,
        usage=_usage(),
    )


async def test_a_paid_call_survives_a_failure_while_applying_it(session, monkeypatch):
    """Applying raised after the API call: the rollback used to take the
    usage row with it, so the retry was billed as if this call never
    happened. The row is committed first; only the rows land or not."""
    akmal = await _person(session)
    await _monitor(session, PRIVATE, ChatType.private)
    await _msg(session, "5 mln oldim", at=_ago(minutes=6), chat_id=PRIVATE, person=akmal)
    [window] = await windows.flush_ready_windows(session)
    await session.commit()
    monkeypatch.setattr(batch, "extract", _extract_ok)

    async def _explode(session, window, result):
        raise RuntimeError("persistence bug")

    monkeypatch.setattr(batch, "_apply_result", _explode)

    outcome = await batch.extract_instant(session)

    assert outcome.applied == 0
    [usage] = list(await session.scalars(sa.select(m.UsageLog)))
    assert usage.operation == "extract_window_instant" and usage.cost_usd > 0
    fresh = await session.get(m.ConversationWindow, window.id)
    assert fresh.attempts == 1 and fresh.instant is True
    assert fresh.status is WindowStatus.pending
    # Nothing half-landed: no debt, no synthetic window row.
    assert await session.scalar(sa.select(sa.func.count()).select_from(m.Debt)) == 0
    assert (
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(m.Interaction)
            .where(m.Interaction.direction == Direction.na)
        )
        == 0
    )


# --- B6. a /chats decision is an answer -------------------------------------------


async def test_a_chats_toggle_counts_as_the_answer(session):
    group = await chats.ensure_monitor(session, _dialog(-333, ChatType.group, "GZ"))
    assert group.asked_at is None

    await chats.toggle(session, group.id, "vision_enabled")
    assert group.asked_at is None  # only the monitor switch is a decision

    await chats.toggle(session, group.id, "monitor_enabled")
    assert group.monitor_enabled is True and group.asked_at is not None
    assert await chats.awaiting_join_question(session) == []

    first = group.asked_at
    await chats.toggle(session, group.id, "monitor_enabled")
    assert group.monitor_enabled is False and group.asked_at == first
    # Switched off again, but decided: never asked.
    assert await chats.awaiting_join_question(session) == []


# --- B7. Javob berdim covers the follow-ups -----------------------------------


async def test_javob_berdim_covers_the_follow_ups_in_a_private_chat(bound):  # noqa: F811
    akmal = await _person(bound)
    await _monitor(bound)
    asked = await _msg(bound, "narxi qancha?", at=_ago(hours=9), person=akmal)
    first = await _msg(bound, "qachon yuborasiz?", at=_ago(hours=8), person=akmal)
    last = await _msg(bound, "javob bormi?", at=_ago(hours=7), person=akmal)
    # The common case: the owner taps right after the newest message, which
    # is still younger than LOOP_QUESTION_HOURS and so invisible to the
    # engine's walk. Every follow-up must carry the mark, not only this one.
    fresh = await _msg(bound, "aka?", at=_ago(hours=1), person=akmal)
    [loop] = await nudges.unanswered_questions(bound)
    assert loop.interaction_id == asked.id and loop.follow_ups == 2

    message = _Editable()
    await handlers.on_record_button(_Callback(f"rec:qa:q{asked.id}", message))

    assert message.sent[-1] == (replies.NUDGE_ANSWERED, None)
    for row in (asked, first, last, fresh):
        assert nudges.is_answered(row)
    assert await nudges.unanswered_questions(bound) == []
    assert await nudges.collect(bound) == []
    # And hours later, once the fresh one is old enough to be walked.
    later = datetime.now(settings.tz) + timedelta(hours=6)
    assert await nudges.unanswered_questions(bound, now=later) == []
    assert await nudges.collect(bound, now=later) == []


async def test_javob_berdim_in_a_group_covers_only_what_was_aimed_at_him(
    bound,  # noqa: F811
):
    akmal = await _person(bound)
    sardor = await _person(bound, "Sardor")
    await _monitor(bound, GROUP, ChatType.group)
    asked = await _msg(
        bound, "Bekzod aka, narxi qancha?", at=_ago(hours=9), chat_id=GROUP, person=akmal
    )
    follow = await _msg(
        bound, "Begi, qachon yuborasiz?", at=_ago(hours=8), chat_id=GROUP, person=akmal
    )
    fresh = await _msg(
        bound, "Bekzod aka, ko'rdingizmi?", at=_ago(hours=1), chat_id=GROUP, person=akmal
    )
    for row in (asked, follow, fresh):
        row.meta = {**row.meta, "to_me": True}
    # Other people talking afterwards is not the follow-up.
    other = await _msg(
        bound, "ular gaplashyapti?", at=_ago(hours=7), chat_id=GROUP, person=sardor
    )
    await bound.flush()
    [loop] = await nudges.unanswered_questions(bound)
    assert loop.interaction_id == asked.id and loop.follow_ups == 1

    message = _Editable()
    await handlers.on_record_button(_Callback(f"rec:qa:q{asked.id}", message))

    for row in (asked, follow, fresh):
        assert nudges.is_answered(row)
    assert not nudges.is_answered(other)
    assert await nudges.unanswered_questions(bound) == []
    later = datetime.now(settings.tz) + timedelta(hours=6)
    assert await nudges.unanswered_questions(bound, now=later) == []


# --- B8. a channel is announced as a channel -----------------------------------

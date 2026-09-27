"""WP-18: one sender of tap-requests, numbered batches, the budget held."""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta
from types import SimpleNamespace

import sqlalchemy as sa

from miya.bot import handlers, replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import ChatType, Direction, InteractionSource, PromiseMadeBy
from miya.services import (
    approvals,
    claims,
    health,
    money_events,
    persistence,
    phone_events,
    sms_money,
)
from miya.services import extraction as ex
from miya.services.ingest import IngestResult
from miya.worker import main as worker
from tests.test_claims_worker import _landed_them_window

TZ = settings.tz
DAY = datetime(2026, 9, 21, tzinfo=TZ).date()


def _at(hour: int, minute: int = 0, day=DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


class _Bot:
    def __init__(self, *, reachable: bool = True) -> None:
        self.sent: list[tuple[str, object]] = []
        self.reachable = reachable
        self.next_id = 100

    async def send_message(self, chat_id, text, **kwargs):
        if not self.reachable:
            raise RuntimeError("telegram down")
        self.sent.append((text, kwargs.get("reply_markup")))
        self.next_id += 1
        return SimpleNamespace(message_id=self.next_id)


def _payloads(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _texts(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


async def _claim(session, *, at, amount=5_000_000) -> m.Claim:
    row = m.Interaction(
        source=InteractionSource.assistant_bot, occurred_at=at, raw_text="x"
    )
    session.add(row)
    await session.flush()
    item = ex.ExtractedDebt(
        direction="i_owe_them",
        person="Akmal",
        amount=amount,
        reason="yuk haqi",
        asserted_by="them",
    )
    return await claims.create(session, row, claims.KIND_DEBT, item, now=at)


async def _missed(session, n: int, *, at):
    await phone_events.ingest_call_events(
        session,
        "dev",
        [
            {
                "call_log_id": n,
                "started_at": at.isoformat(),
                "duration_seconds": 0,
                "type": "missed",
                "number": f"+99890{n:07d}",
                "contact_name": None,
                "sim_slot": 0,
            }
        ],
    )


def _video(at) -> m.Interaction:
    return m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.in_,
        occurred_at=at,
        media={
            "type": "video",
            "approval": {"state": approvals.PENDING, "reason": "video"},
        },
    )


async def _question(session, *, at) -> m.Interaction:
    person = m.Person(display_name="Sardor", aliases=[])
    chat = m.ChatMonitor(
        tg_chat_id=4242, chat_type=ChatType.private, title="Sardor", monitor_enabled=True
    )
    session.add_all([person, chat])
    await session.flush()
    row = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.in_,
        tg_chat_id=4242,
        person_id=person.id,
        occurred_at=at,
        raw_text="Yuk qachon keladi?",
        meta={"tg_message_id": 1},
    )
    session.add(row)
    await session.flush()
    return row


def _brief(session, day=DAY) -> None:
    session.add(m.ReminderLog(kind="brief", ref=day.isoformat()))


async def test_quiet_hours_send_nothing(session):
    await _claim(session, at=_at(1))
    await session.commit()
    bot = _Bot()
    await worker.question_job(bot, now=_at(2))
    assert bot.sent == []


async def test_mixed_kinds_arrive_as_one_numbered_message(session):
    claim = await _claim(session, at=_at(9))
    await _missed(session, 1, at=_at(8))
    video = _video(_at(9))
    session.add(video)
    question = await _question(session, at=_at(6))
    _brief(session)
    await session.commit()
    bot = _Bot()

    await worker.question_job(bot, now=_at(14))

    [(text, markup)] = bot.sent
    assert text.startswith("❓ <b>Savollar</b> · bugun 4/10")
    assert [t.split()[0] for t in _texts(markup)] == ["1"] * 3 + ["2"] * 2 + ["3"] * 2 + [
        "4"
    ] * 2
    payloads = _payloads(markup)
    assert f"cl:y:{claim.id}" in payloads
    assert any(p.startswith("rec:ma:m") for p in payloads)
    assert f"md:y:{video.id}" in payloads
    assert f"rec:qa:q{question.id}" in payloads
    logs = list(await session.scalars(sa.select(m.QuestionLog)))
    assert len(logs) == 4 and {row.tg_message_id for row in logs} == {101}


async def test_failed_send_marks_nothing(session):
    claim = await _claim(session, at=_at(9))
    await session.commit()
    await worker.question_job(_Bot(reachable=False), now=_at(14))
    await session.refresh(claim)
    assert claim.asked_at is None
    assert await session.scalar(sa.select(sa.func.count(m.QuestionLog.id))) == 0


async def test_reminder_job_no_longer_asks_hali_ochiqmi(session, monkeypatch):
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: False)
    person = m.Person(display_name="Sardor", aliases=[])
    session.add(person)
    await session.flush()
    session.add(
        m.Promise(
            made_by=PromiseMadeBy.them,
            person_id=person.id,
            description="hujjat",
            created_at=datetime.now(TZ) - timedelta(days=10),
        )
    )
    await session.commit()
    bot = _Bot()
    await worker.reminder_job(bot)
    assert bot.sent == []


async def test_budget_holds_over_a_simulated_day(session):
    for i in range(40):
        await _claim(session, at=_at(7, i % 60), amount=100_000 * (i + 1))
    for i in range(15):
        await _missed(session, i + 1, at=_at(6, i))
    for i in range(30):
        session.add(
            m.ChatMonitor(
                tg_chat_id=-5000 - i,
                chat_type=ChatType.group,
                title=f"Guruh {i}",
                monitor_enabled=False,
            )
        )
    session.add_all([_video(_at(6, i)) for i in range(12)])
    person = m.Person(display_name="Vali", aliases=[])
    session.add(person)
    await session.flush()
    for i in range(8):
        session.add(
            m.Promise(
                made_by=PromiseMadeBy.them,
                person_id=person.id,
                description=f"va'da {i}",
                created_at=_at(6) - timedelta(days=10 + i),
            )
        )
    for i in range(5):
        row = m.Interaction(
            source=InteractionSource.phone_sms,
            direction=Direction.in_,
            occurred_at=_at(6, i),
            raw_text=f"Oplata {100 + i} 000 sum",
            processed=True,
            needs_review=False,
            media={"type": "sms", "sender": "Payme"},
        )
        session.add(row)
        await session.flush()
        await money_events.apply_reading(
            session,
            row,
            sms_money.read(row.raw_text),
            channel=money_events.channel_for_sms("Payme"),
            now=_at(6, i),
        )
    await session.commit()

    bot = _Bot()
    tick = _at(7, 30)
    while tick <= _at(23, 30):
        if tick == _at(9):
            _brief(session)
            await session.commit()
        await worker.question_job(bot, now=tick)
        tick += timedelta(minutes=5)

    shown = await session.scalar(
        sa.select(sa.func.count(m.QuestionLog.id)).where(
            m.QuestionLog.sent_at >= _at(0),
            m.QuestionLog.sent_at < _at(0) + timedelta(days=1),
        )
    )
    assert 0 < shown <= settings.question_budget_per_day
    assert len(bot.sent) <= 6
    states = set(await session.scalars(sa.select(m.Claim.state)))
    assert states == {claims.PENDING}
    assert await session.scalar(sa.select(sa.func.count(m.Claim.id))) == 40


async def test_receipt_carries_claim_rows_only_within_budget(session, monkeypatch):
    landed = await _landed_them_window(session)
    [claim] = landed.applied.claims
    now = datetime.now(TZ)
    session.add_all(
        [
            m.QuestionLog(kind="nudge", ref=f"q{i}", via="push", sent_at=now)
            for i in range(10)
        ]
    )
    await session.commit()
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: False)
    bot = _Bot()

    await worker.chat_notice_job(bot)

    [(text, markup)] = bot.sent
    assert markup is None
    assert "Tasdiqlash navbatda — /savollar" in text
    await session.refresh(claim)
    assert claim.asked_at is None


async def test_worker_startup_deletes_stale_job_heartbeats(session):
    session.add_all(
        [
            m.Heartbeat(
                component="job:claim_ask", detail={}, last_seen_at=datetime.now(TZ)
            ),
            m.Heartbeat(
                component="job:questions", detail={}, last_seen_at=datetime.now(TZ)
            ),
            m.Heartbeat(component="bot", detail={}, last_seen_at=datetime.now(TZ)),
        ]
    )
    await session.commit()
    removed = await worker._prune_job_heartbeats(session, ["job:questions"])
    await session.commit()
    assert removed == 1
    left = set(await session.scalars(sa.select(m.Heartbeat.component)))
    assert left == {"job:questions", "bot"}
    assert health.JOB_PREFIX == "job:"


async def test_bot_receipt_claim_rows_are_not_counted(session):
    row = m.Interaction(
        source=InteractionSource.assistant_bot, occurred_at=datetime.now(TZ), raw_text="x"
    )
    session.add(row)
    await session.flush()
    claim = await claims.create(
        session,
        row,
        claims.KIND_DEBT,
        ex.ExtractedDebt(
            direction="i_owe_them", person="Akmal", amount=5_000_000, asserted_by="them"
        ),
    )
    applied = persistence.Applied(claims=[claim])
    handlers._receipt(IngestResult(interaction=row, applied=applied))
    assert claim.asked_at is not None
    assert await session.scalar(sa.select(sa.func.count(m.QuestionLog.id))) == 0


def test_worker_registers_questions_and_not_the_old_jobs():
    source = inspect.getsource(worker.run)
    assert 'id="questions"' in source
    for old in ("claim_ask", "new_chat_ask", "media_ask", "nudges", "missed_call_nudge"):
        assert f'id="{old}"' not in source


async def test_md_inside_a_batch_removes_only_its_row(session, monkeypatch):
    from contextlib import asynccontextmanager

    from miya.bot import keyboards
    from tests.test_close_and_correct import _Callback, _Message

    @asynccontextmanager
    async def _scope():
        yield session
        await session.flush()

    monkeypatch.setattr(handlers, "session_scope", _scope)
    video = _video(datetime.now(TZ))
    session.add(video)
    claim = await _claim(session, at=datetime.now(TZ))
    await session.flush()
    markup = keyboards.question_batch(
        [
            SimpleNamespace(kind="claim", subject=claim),
            SimpleNamespace(kind="media", subject=video),
        ]
    )
    message = _Message(reply_markup=markup)
    await handlers.on_media_button(_Callback(f"md:y:{video.id}", message))
    assert _payloads(message.edited_markup) == [
        f"cl:y:{claim.id}",
        f"cl:n:{claim.id}",
        f"cl:e:{claim.id}",
    ]
    assert message.sent[0][0] == replies.MEDIA_APPROVED


async def test_a_single_media_question_is_still_edited_in_place(session, monkeypatch):
    from contextlib import asynccontextmanager

    from miya.bot import keyboards

    @asynccontextmanager
    async def _scope():
        yield session
        await session.flush()

    monkeypatch.setattr(handlers, "session_scope", _scope)
    video = _video(datetime.now(TZ))
    session.add(video)
    await session.flush()

    class _Msg:
        reply_markup = keyboards.media_approval(video.id)
        edited = None

        async def edit_text(self, text, reply_markup=None):
            self.edited = text

    message = _Msg()

    class _Cb:
        data = f"md:y:{video.id}"

        def __init__(self):
            self.message = message

        async def answer(self, *a, **k):
            pass

    await handlers.on_media_button(_Cb())
    assert message.edited == replies.MEDIA_APPROVED

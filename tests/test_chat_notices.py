"""Nothing lands from a Telegram chat without the owner being told.

A phone call that produced a debt already got a confirmation; a debt the
extractor pulled from a group chat landed in /qarz silently and the owner met
it in the evening report as an established fact. These cover the receipt text,
the queue parked with the rows, quiet hours, the cap, and the worker's delivery.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa

from miya.bot import notices, replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import (
    ChatType,
    Currency,
    DebtDirection,
    InteractionSource,
    WindowStatus,
)
from miya.services import batch
from miya.services import extraction as ex
from miya.services.persistence import Applied
from miya.worker import main as worker
from tests.test_batch import CHAT, DEBT_JSON, _StubClient, _succeeded, _usage, _window

TZ = settings.tz
WHEN = datetime(2026, 9, 15, 14, 20, tzinfo=TZ)


def _debt(amount: str = "5000000") -> m.Debt:
    return m.Debt(
        direction=DebtDirection.they_owe_me,
        amount=Decimal(amount),
        currency=Currency.UZS,
    )


def _receipt(**kwargs) -> str:
    defaults = {
        "chat_title": "GZ logistika",
        "people": ["Akmal"],
        "ended_at": WHEN,
        "applied": Applied(debts=[_debt()]),
    }
    return notices.window_notice(**{**defaults, **kwargs})


class _Bot:
    """Records what the owner would have received; optionally unreachable."""

    def __init__(self, *, reachable: bool = True) -> None:
        self.sent: list[str] = []
        self.documents: list[tuple] = []
        self.reachable = reachable

    async def send_message(self, chat_id, text, **kwargs) -> None:
        if not self.reachable:
            raise RuntimeError("telegram is down")
        self.sent.append(text)

    async def send_document(self, chat_id, document, **kwargs) -> None:
        if not self.reachable:
            raise RuntimeError("telegram is down")
        self.documents.append((document, kwargs))


# --- the receipt text --------------------------------------------------------


def test_the_receipt_names_the_chat_the_person_and_reuses_the_owners_receipt():
    text = _receipt()
    head, _, body = text.partition("\n\n")
    assert "Telegramdan yozib olindi" in head
    assert "<b>GZ logistika</b> · Akmal · 15-sen 14:20" in head
    # The lines are the owner's own receipt — one formatter, not two.
    assert body == replies.confirmation(Applied(debts=[_debt()]))
    assert "5 mln so'm" in body


def test_a_private_chat_does_not_say_the_name_twice():
    text = _receipt(chat_title="Akmal", people=["Akmal"])
    assert text.count("Akmal") == 1


def test_a_chat_the_userbot_has_not_synced_is_named_after_the_person():
    text = _receipt(chat_title=None, people=["Akmal"])
    assert "<b>Akmal</b>" in text
    assert notices.UNKNOWN_CHAT not in text


def test_nobody_known_still_gives_a_readable_header():
    assert f"<b>{notices.UNKNOWN_CHAT}</b>" in _receipt(chat_title=None, people=[])


def test_chat_titles_and_names_from_telegram_are_escaped():
    text = _receipt(chat_title="<b>GZ</b> & co", people=["<i>Akmal</i>"])
    assert "&lt;b&gt;GZ&lt;/b&gt; &amp; co" in text
    assert "&lt;i&gt;Akmal&lt;/i&gt;" in text


def test_counts_keep_a_question_apart_from_a_payment():
    applied = Applied(
        debts=[_debt()],
        unmatched_settlements=[("Sardor", Decimal(2), Currency.UZS)],
        ambiguous_settlements=[("Akmal", Decimal(1), Currency.UZS)],
    )
    assert notices.counts_of(applied) == {"debts": 1, "settlements": 1, "questions": 1}
    assert notices.counts_of(Applied(facts=3)) == {}


def test_the_overflow_summary_totals_per_chat():
    text = notices.overflow_summary(
        [
            ("GZ logistika", {"debts": 1}),
            ("GZ logistika", {"debts": 1, "promises": 1}),
            ("Akmal", {"transactions": 2}),
        ]
    )
    assert "Yana 3 ta suhbatdan yozib olindi" in text
    assert "<b>GZ logistika</b>: 2 qarz, 1 va'da" in text
    assert "<b>Akmal</b>: 2 pul harakati" in text
    assert "/qarz" in text


# --- parked with the rows ----------------------------------------------------


async def _monitored(session, title: str = "GZ logistika") -> None:
    session.add(
        m.ChatMonitor(
            tg_chat_id=CHAT, chat_type=ChatType.group, title=title, monitor_enabled=True
        )
    )
    await session.flush()


def _result(**overrides) -> ex.ExtractionResult:
    fields = {
        "summary": "Akmal 5 mln oldi",
        "debts": [
            ex.ExtractedDebt(
                direction="they_owe_me", person="Akmal", amount=5_000_000, reason="yuk"
            )
        ],
    }
    return ex.ExtractionResult(**{**fields, **overrides})


async def test_an_applied_window_says_what_landed_and_where(session):
    await _monitored(session)
    window = await _window(session)

    landed = await batch._apply_result(session, window, _result())
    await session.flush()

    assert [d.amount for d in landed.applied.debts] == [Decimal(5_000_000)]
    assert landed.chat_title == "GZ logistika"
    assert landed.people == ["Akmal"]
    assert landed.notice is not None
    assert "GZ logistika" in landed.notice
    assert "5 mln so'm" in landed.notice

    # Parked on the window's interaction in the same transaction as the rows.
    parked = landed.interaction.meta[batch.NOTICE_KEY]
    assert parked["text"] == landed.notice
    assert parked["chat"] == "GZ logistika"
    assert parked["counts"] == {"debts": 1}
    [queued] = await batch.pending_notices(session)
    assert queued.interaction.id == landed.interaction.id
    assert queued.text == landed.notice


async def test_a_facts_only_window_is_applied_but_not_worth_a_ping(session):
    window = await _window(session)

    landed = await batch._apply_result(
        session,
        window,
        ex.ExtractionResult(summary="ob-havo haqida", facts=["Akmal Dubayga ketdi"]),
    )
    await session.flush()

    assert window.status is WindowStatus.applied
    assert landed.applied.facts == 1
    assert landed.notice is None
    assert batch.NOTICE_KEY not in (landed.interaction.meta or {})
    assert await batch.pending_notices(session) == []


async def test_a_group_receipt_names_who_owes_not_who_spoke_first(session):
    sardor = m.Person(display_name="Sardor")
    session.add(sardor)
    await session.flush()
    window = await _window(session)
    window.person_id = sardor.id  # the window's person is whoever spoke first

    landed = await batch._apply_result(session, window, _result())

    assert landed.people == ["Akmal"]
    assert "Sardor" not in (landed.notice or "")


async def test_the_real_time_fallback_parks_a_receipt_too(session, monkeypatch):
    window = await _window(session)
    window.attempts = settings.batch_max_attempts - 1
    await session.flush()

    stub = _StubClient()
    monkeypatch.setattr(batch, "get_client", lambda: stub)
    await batch.submit_pending(session)
    stub.results = [_succeeded(window.custom_id, "not json at all")]

    async def _fake_extract(text, *, now=None):
        return ex.ExtractionOutcome(
            result=_result(), model=settings.extract_model, usage=_usage()
        )

    monkeypatch.setattr(batch, "extract", _fake_extract)

    outcome = await batch.collect_batch(session, "batch_1")

    [landed] = outcome.windows
    assert outcome.applied == 1
    assert landed.notice is not None
    assert "5 mln so'm" in landed.notice


async def test_a_receipt_that_cannot_render_never_costs_the_ledger(session, monkeypatch):
    def _explode(**kwargs):
        raise RuntimeError("formatter bug")

    monkeypatch.setattr(notices, "window_notice", _explode)
    window = await _window(session)

    landed = await batch._apply_result(session, window, _result())

    assert window.status is WindowStatus.applied
    assert landed.notice is None
    assert await session.scalar(sa.select(sa.func.count()).select_from(m.Debt)) == 1


# --- the worker delivers -----------------------------------------------------


async def _submitted_debt_window(session, monkeypatch) -> m.ConversationWindow:
    """A monitored group window whose batch has finished with one debt."""
    await _monitored(session)
    window = await _window(session)
    stub = _StubClient()
    monkeypatch.setattr(batch, "get_client", lambda: stub)
    await batch.submit_pending(session)
    stub.results = [_succeeded(window.custom_id, DEBT_JSON)]
    await session.commit()
    return window


def _parked(index: int, chat: str, *, now: datetime) -> m.Interaction:
    return m.Interaction(
        source=InteractionSource.telegram_userbot,
        tg_chat_id=CHAT,
        occurred_at=now + timedelta(minutes=index),
        raw_text=f"oyna {index}",
        meta={
            "kind": "window",
            batch.NOTICE_KEY: {
                "text": f"receipt {index}",
                "chat": chat,
                "counts": {"debts": 1},
                "queued_at": now.isoformat(),
            },
        },
    )


async def test_the_poll_tells_the_owner_where_a_chat_debt_came_from(session, monkeypatch):
    await _submitted_debt_window(session, monkeypatch)
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: False)
    bot = _Bot()

    await worker.batch_poll_job(bot)

    [text] = bot.sent
    assert "Telegramdan yozib olindi" in text
    assert "GZ logistika" in text
    assert "Akmal" in text
    assert "5 mln so'm" in text
    # Told once: the queue is empty and the row records when.
    assert await batch.pending_notices(session) == []
    told = await session.scalar(
        sa.select(m.Interaction).where(m.Interaction.meta.has_key(batch.NOTICE_KEY))
    )
    assert told.meta[batch.NOTIFIED_KEY]


async def test_quiet_hours_delay_the_receipt_and_never_drop_it(session, monkeypatch):
    await _submitted_debt_window(session, monkeypatch)
    quiet = True
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: quiet)
    bot = _Bot()

    # 02:00 — the debt lands, the owner sleeps.
    await worker.batch_poll_job(bot)
    assert bot.sent == []
    assert await session.scalar(sa.select(sa.func.count()).select_from(m.Debt)) == 1
    [queued] = await batch.pending_notices(session)
    assert "5 mln so'm" in queued.text

    # First poll after quiet hours: told, and only once.
    quiet = False
    await worker.chat_notice_job(bot)
    assert len(bot.sent) == 1
    await worker.chat_notice_job(bot)
    assert len(bot.sent) == 1


async def test_a_backlog_is_capped_and_the_rest_summarised(session, monkeypatch):
    """One poll after an outage can apply a day of windows: ten receipts is
    fine, a hundred is not."""
    now = datetime.now(TZ)
    cap = notices.MAX_DETAILED
    for index in range(cap + 3):
        session.add(_parked(index, "GZ logistika" if index % 2 else "Akmal", now=now))
    await session.commit()
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: False)
    bot = _Bot()

    await worker.chat_notice_job(bot)

    assert len(bot.sent) == cap + 1
    # Oldest conversation first, one by one, up to the cap...
    assert bot.sent[:cap] == [f"receipt {i}" for i in range(cap)]
    # ...then the rest folded into one summary, per chat.
    summary = bot.sent[-1]
    assert "Yana 3 ta suhbatdan" in summary
    assert "<b>Akmal</b>: 2 qarz" in summary
    assert "<b>GZ logistika</b>: 1 qarz" in summary
    # The summarised ones count as told: a second sweep sends nothing.
    await worker.chat_notice_job(bot)
    assert len(bot.sent) == cap + 1
    assert await batch.pending_notices(session) == []


async def test_an_unreachable_owner_keeps_every_receipt_queued(session, monkeypatch):
    now = datetime.now(TZ)
    for index in range(2):
        session.add(_parked(index, "GZ logistika", now=now))
    await session.commit()
    monkeypatch.setattr(worker.reminders, "in_quiet_hours", lambda now=None: False)
    bot = _Bot(reachable=False)

    await worker.chat_notice_job(bot)
    assert bot.sent == []
    assert len(await batch.pending_notices(session)) == 2

    bot.reachable = True
    await worker.chat_notice_job(bot)
    assert bot.sent == ["receipt 0", "receipt 1"]

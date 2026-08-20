"""Bot-facing behaviour: the owner gate, and the Uzbek text the owner reads."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from miya.bot import replies
from miya.bot.handlers import is_owner
from miya.config import settings
from miya.db import models as m
from miya.db.enums import (
    Currency,
    DebtDirection,
    PromiseMadeBy,
    TaskPriority,
    TransactionType,
)
from miya.services.persistence import Applied
from miya.services.queries import DaySummary, DebtBalance, PersonSummary

# --- owner gate -------------------------------------------------------------


def test_only_the_configured_owner_passes_the_gate(monkeypatch):
    monkeypatch.setattr(settings, "owner_telegram_id", 4242)
    assert is_owner(4242) is True
    assert is_owner(9999) is False
    assert is_owner(None) is False


def test_an_unconfigured_owner_id_locks_everyone_out(monkeypatch):
    """A missing OWNER_TELEGRAM_ID must not turn the bot into a public one."""
    monkeypatch.setattr(settings, "owner_telegram_id", None)
    assert is_owner(None) is False
    assert is_owner(0) is False
    assert is_owner(4242) is False


# --- confirmations ----------------------------------------------------------


def test_confirmation_reports_a_recorded_debt():
    applied = Applied(
        debts=[
            m.Debt(
                direction=DebtDirection.they_owe_me,
                amount=Decimal("5000000"),
                currency=Currency.UZS,
                due_date=date(2026, 8, 25),
            )
        ]
    )
    text = replies.confirmation(applied)
    assert "5 mln so'm" in text
    assert "senga" in text
    assert "25-avg" in text


def test_confirmation_reports_the_direction_the_owner_owes():
    applied = Applied(
        debts=[
            m.Debt(
                direction=DebtDirection.i_owe_them,
                amount=Decimal("300"),
                currency=Currency.USD,
            )
        ]
    )
    assert "← sen" in replies.confirmation(applied)


def test_confirmation_warns_about_a_repayment_with_no_matching_debt():
    applied = Applied(unmatched_settlements=[("Akmal", Decimal("1000000"), Currency.UZS)])
    text = replies.confirmation(applied)
    assert "⚠️" in text and "Akmal" in text and "1 mln so'm" in text


def test_confirmation_covers_every_extracted_kind():
    applied = Applied(
        promises=[m.Promise(made_by=PromiseMadeBy.me, description="pul o'tkazaman")],
        transactions=[
            m.Transaction(
                type=TransactionType.expense,
                amount=Decimal("45000"),
                currency=Currency.UZS,
                category="food",
            )
        ],
        events=[
            m.Event(
                title="Uchrashuv",
                start_at=datetime(2026, 8, 20, 15, 0, tzinfo=settings.tz),
            )
        ],
        tasks=[m.Task(description="Konteynerni tekshirish", priority=TaskPriority.high)],
        facts=2,
    )
    text = replies.confirmation(applied)
    assert "🤝" in text and "pul o'tkazaman" in text
    assert "📉" in text and "45 ming so'm" in text
    assert "📅" in text and "15:00" in text
    assert "✔️" in text and "yuqori" in text
    assert "2 ta yangi ma'lumot" in text


def test_confirmation_still_acknowledges_a_note_with_nothing_structured():
    text = replies.confirmation(Applied())
    assert "Yozib oldim" in text


def test_a_contact_name_cannot_inject_html():
    applied = Applied(unmatched_settlements=[("<b>evil", Decimal("1000"), Currency.UZS)])
    text = replies.confirmation(applied)
    assert "<b>evil" not in text
    assert "&lt;b&gt;evil" in text


# --- reports ----------------------------------------------------------------


def _balance(name, direction, amount, currency=Currency.UZS, due=None):
    return DebtBalance(
        person=m.Person(display_name=name, aliases=[]),
        direction=direction,
        currency=currency,
        outstanding=Decimal(amount),
        earliest_due=due,
        count=1,
    )


def test_debts_report_splits_the_two_directions():
    text = replies.debts_report(
        [
            _balance("Akmal", DebtDirection.they_owe_me, "5000000"),
            _balance("Dilnoza", DebtDirection.i_owe_them, "300", Currency.USD),
        ]
    )
    assert "Senga qarzdorlar" in text and "Akmal" in text
    assert "Sen qarzdorsan" in text and "$300" in text


def test_debts_report_says_so_when_there_is_nothing():
    assert "Ochiq qarz yo'q" in replies.debts_report([])


def test_promises_report_separates_mine_from_theirs():
    person = m.Person(display_name="Akmal", aliases=[])
    text = replies.promises_report(
        [
            (m.Promise(made_by=PromiseMadeBy.me, description="men to'layman"), person),
            (m.Promise(made_by=PromiseMadeBy.them, description="u qaytaradi"), person),
        ]
    )
    assert "Sen va'da bergansan" in text and "men to'layman" in text
    assert "Senga va'da berishgan" in text and "u qaytaradi" in text


def test_day_report_renders_money_and_people():
    summary = DaySummary(
        day=date(2026, 8, 17),
        expense={Currency.UZS: Decimal("145000")},
        by_category=[("food", Currency.UZS, Decimal("45000"))],
        people_seen=[(m.Person(display_name="Akmal", aliases=[]), 3)],
        interactions=7,
    )
    text = replies.day_report(summary)
    assert "17-avgust 2026" in text
    assert "Chiqim: 145 ming so'm" in text
    assert "Akmal (3)" in text
    assert "7 ta yozuv" in text


def test_day_report_handles_a_day_with_no_money_movement():
    text = replies.day_report(DaySummary(day=date(2026, 8, 17)))
    assert "pul harakati yo'q" in text


def test_person_report_shows_balances_and_aliases():
    person = m.Person(display_name="Akmal aka", aliases=["Akmal GZ"])
    summary = PersonSummary(
        person=person,
        balances=[_balance("Akmal aka", DebtDirection.they_owe_me, "5000000")],
        open_promises=[m.Promise(made_by=PromiseMadeBy.them, description="qaytaradi")],
        total_interactions=12,
    )
    text = replies.person_report(summary)
    assert "Akmal aka" in text
    assert "Akmal GZ" in text
    assert "5 mln so'm" in text
    assert "qaytaradi" in text


def test_reminder_body_is_empty_when_nothing_is_due():
    assert replies.reminder([], [], [], []) == ""


def test_reminder_body_lists_what_is_due():
    text = replies.reminder(
        [_balance("Akmal", DebtDirection.they_owe_me, "5000000", due=date(2000, 1, 1))],
        [],
        [m.Task(description="Hujjat topshirish", due_date=date(2000, 1, 1))],
        [],
    )
    assert "Qarz muddati" in text and "Akmal" in text
    assert "Vazifalar" in text and "Hujjat topshirish" in text


@pytest.mark.parametrize("body", [replies.HELP, replies.FAILED_EXTRACTION_HINT])
def test_static_owner_text_is_uzbek(body):
    assert any(word in body for word in ("qarz", "Yozib", "buyruq", "ma'lumot"))


# --- startup: the backlog is data, not noise --------------------------------


@pytest.mark.asyncio
async def test_polling_keeps_updates_queued_while_the_bot_was_down(monkeypatch):
    """A restart must not discard what the owner sent meanwhile.

    Container restarts are routine (rebuild, crash, VPS reboot) and a message
    sent during one exists nowhere else — dropping it would silently lose a
    debt or a promise, which is the one thing this system must never do.
    """
    from miya.bot import main as bot_main

    seen: dict = {}

    class _Me:
        username = "miya_test_bot"

    class _Session:
        async def close(self):
            pass

    class _Bot:
        session = _Session()

        def __init__(self, *a, **kw):
            pass

        async def get_me(self):
            return _Me()

    class _Dispatcher:
        async def start_polling(self, bot, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(bot_main, "Bot", _Bot)
    monkeypatch.setattr(bot_main, "build_dispatcher", lambda: _Dispatcher())

    class _Engine:
        async def dispose(self):
            pass

    monkeypatch.setattr(bot_main, "engine", _Engine())
    monkeypatch.setattr(settings, "assistant_bot_token", "test-token")
    monkeypatch.setattr(settings, "owner_telegram_id", 4242)

    await bot_main.run()

    assert seen.get("drop_pending_updates") is False

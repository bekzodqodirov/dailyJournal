"""Uzbek formatting — pure functions, no database."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from miya.bot import formatting as f
from miya.db.enums import Currency, DebtDirection


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    [
        ("5000000", Currency.UZS, "5 mln so'm"),
        ("5500000", Currency.UZS, "5.5 mln so'm"),
        ("500000", Currency.UZS, "500 ming so'm"),
        ("12000", Currency.UZS, "12 ming so'm"),
        ("850", Currency.UZS, "850 so'm"),
        ("1200.50", Currency.USD, "$1200.5"),
        ("99", Currency.USD, "$99"),
        ("300", Currency.CNY, "¥300"),
        ("50000", Currency.KRW, "₩50000"),
        ("1500", Currency.RUB, "1500 rubl"),
    ],
)
def test_money_reads_the_way_the_owner_says_it(amount, currency, expected):
    assert f.money(Decimal(amount), currency) == expected


def test_foreign_currency_is_never_rounded_to_millions():
    """A rounded USD figure is more confusing than a long one."""
    assert f.money(Decimal("2500000"), Currency.USD) == "$2500000"


def test_short_date_uses_uzbek_month_abbreviations():
    assert f.short_date(date(2026, 8, 25)) == "25-avg"
    assert f.short_date(date(2026, 1, 3)) == "3-yan"
    assert f.short_date(None) == "muddatsiz"


def test_full_date():
    assert f.full_date(date(2026, 8, 17)) == "17-avgust 2026"


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (date(2026, 8, 17), "bugun"),
        (date(2026, 8, 18), "ertaga"),
        (date(2026, 8, 19), "indinga"),
        (date(2026, 8, 16), "kecha"),
        (date(2026, 8, 14), "3 kun kechikdi"),
        (date(2026, 8, 22), "5 kundan keyin"),
        (date(2026, 9, 30), "30-sen"),
    ],
)
def test_relative_day(target, expected):
    assert f.relative_day(target, today=date(2026, 8, 17)) == expected


def test_debt_line_is_written_from_the_owners_point_of_view():
    line = f.debt_line(
        "Akmal",
        DebtDirection.they_owe_me,
        Decimal("5000000"),
        Currency.UZS,
        date(2026, 8, 25),
    )
    assert line.startswith("Akmal → senga: 5 mln so'm")

    line = f.debt_line(
        "Akmal", DebtDirection.i_owe_them, Decimal("300"), Currency.USD, None
    )
    assert line == "Akmal ← sen: $300"


def test_escape_neutralises_html_in_contact_names():
    """A contact called "<b>x" must not be able to break Telegram's HTML parser."""
    assert f.escape("<b>Akmal</b> & co") == "&lt;b&gt;Akmal&lt;/b&gt; &amp; co"


def test_clock_renders_in_tashkent_time():
    utc_noon = datetime.fromisoformat("2026-08-17T12:00:00+00:00")
    assert f.clock(utc_noon) == "17:00"  # Asia/Tashkent is UTC+5, no DST


def test_bullet_list_falls_back_to_the_empty_marker():
    assert f.bullet_list([], empty="—") == "—"
    assert f.bullet_list(["a", "b"], empty="—") == "• a\n• b"


# --- labels for namesakes (WP-78) --------------------------------------------------


async def test_two_akmals_are_told_apart_by_code_in_qarz(session):
    from decimal import Decimal

    from miya.bot import replies
    from miya.db import models as m
    from miya.db.enums import Currency, DebtDirection
    from miya.services import codes, queries

    for code, amount in (("GS367", "5000000"), ("GS412", "3000000")):
        person = m.Person(display_name="Akmal", aliases=[])
        session.add(person)
        await session.flush()
        await codes.attach(session, person, code, source="command", by="command")
        session.add(
            m.Debt(
                direction=DebtDirection.they_owe_me,
                person_id=person.id,
                amount=Decimal(amount),
                currency=Currency.UZS,
            )
        )
    await session.flush()

    text = replies.debts_report(await queries.open_debts(session))

    assert "Akmal · GS367: 5 mln so'm" in text
    assert "Akmal · GS412: 3 mln so'm" in text


async def test_a_unique_name_without_a_code_is_unchanged_and_labels_escape(session):
    from miya.db import models as m
    from miya.services import codes

    solo = m.Person(display_name="Vali", aliases=[])
    twin_a = m.Person(display_name="<b>Ali</b>", aliases=[])
    twin_b = m.Person(display_name="<b>Ali</b>", aliases=[])
    session.add_all([solo, twin_a, twin_b])
    await session.flush()

    found = await codes.labels(session, [solo, twin_a, twin_b])

    assert found[solo.id] == "Vali"
    assert found[twin_a.id] == f"<b>Ali</b> (#{twin_a.id})"
    await codes.apply_labels(session, [twin_a])
    from miya.bot.formatting import escape, person_label

    assert escape(person_label(twin_a)).startswith("&lt;b&gt;Ali")

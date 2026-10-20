"""JV-03: the deterministic day and time-of-day reader (miya/services/when.py).

No database, no clock: every call passes ``today``.
"""

from __future__ import annotations

import ast
from datetime import date, datetime, time
from pathlib import Path

import pytest

from miya.config import settings
from miya.services import when

# A Thursday, so «ertaga» is Friday and «dushanba» is four days ahead.
TODAY = date(2026, 10, 22)
TOMORROW = date(2026, 10, 23)
MONDAY = date(2026, 10, 26)
WHEN_PY = Path(__file__).resolve().parents[1] / "miya" / "services" / "when.py"


@pytest.mark.parametrize(
    ("text", "day", "at"),
    [
        # The plan's table.
        ("kechki 6", None, time(18, 0)),
        ("soat 14:00", None, time(14, 0)),
        ("14:00 gacha", None, time(14, 0)),
        ("ertalab 9", None, time(9, 0)),
        ("завтра в 10", TOMORROW, time(10, 0)),
        # The spec's own sentences and the owner check.
        ("Ertaga 14:00 gacha hisoblab beraman", TOMORROW, time(14, 0)),
        ("ertaga soat 3 da tashlab beraman", TOMORROW, time(15, 0)),
        ("Sardorga yozib yubor, kechki 6 da gaplashamiz", None, time(18, 0)),
        ("dushanba 10:00", MONDAY, time(10, 0)),
        # Uzbek Cyrillic and Russian forms.
        ("эртага соат 10", TOMORROW, time(10, 0)),
        ("кечки 6 да", None, time(18, 0)),
        ("в 6 вечера", None, time(18, 0)),
        ("в 2 часа дня", None, time(14, 0)),
        ("в пятницу к 14:00", TOMORROW, time(14, 0)),
        ("послезавтра утром в 8", date(2026, 10, 24), time(8, 0)),
        # «N gacha» and a part of the day said apart from the number.
        ("18 gacha", None, time(18, 0)),
        ("kechqurun gaplashamiz, soat 7 da", None, time(19, 0)),
        ("ertalab 7", None, time(7, 0)),
        ("kechasi 12", None, time(0, 0)),
        ("ertaga kechasi 11", TOMORROW, time(23, 0)),
        ("soat 09:00", None, time(9, 0)),
        ("soat 14.00 da", None, time(14, 0)),
        # A day alone; weekdays with their case endings.
        ("ertaga", TOMORROW, None),
        ("dushanbagacha", MONDAY, None),
        ("bugun kechqurun", TODAY, None),
        # Ambiguous or empty → None (no day and no time).
        ("soat 7", None, None),  # morning or evening: two equally likely readings
        ("soat 3 yoki 4 da", None, None),  # two times
        ("5-6 da", None, None),  # a range
        ("в 3 или 4", None, None),
        ("ertalab 15", None, None),  # the part of the day contradicts the hour
        ("kechki 12", None, None),
        ("soat 25", None, None),
        ("ertaga yoki dushanba", None, None),  # two days
        ("ertaga kechki 6, yo'q, ertalab 9", None, None),
        # A night hour after midnight with a day: that day's or the next night's.
        ("bugun kechasi 2", None, None),
        ("ertaga kechasi 12", None, None),
        ("nima gap", None, None),  # nothing at all
        ("", None, None),
    ],
)
def test_when_table(text, day, at):
    expected = None if day is None and at is None else when.When(day=day, at=at)
    assert when.parse(text, today=TODAY) == expected


@pytest.mark.parametrize(
    "text",
    [
        "3 soat 5 daqiqa",  # a duration, not «soat 5»
        "до 5 числа",  # the 5th of the month
        "в 10 раз",
        "5-gacha",  # an ordinal date
        "1000 gacha",
    ],
)
def test_quantities_are_not_clock_times(text):
    assert when.parse_time(text) is None


def test_parse_time_ignores_the_day():
    assert when.parse_time("ertaga soat 14:00 gacha") == time(14, 0)
    assert when.parse_time("ertaga") is None


def test_the_same_time_said_twice_is_one_time():
    assert when.parse("soat 14:00, ha, 14:00 gacha", today=TODAY) == when.When(
        None, time(14, 0)
    )


def test_a_weekday_said_on_that_day_is_next_week():
    thursday = date(2026, 10, 22)
    assert when.next_weekday(thursday, 3) == date(2026, 10, 29)
    assert when.parse("payshanba", today=thursday) == when.When(date(2026, 10, 29), None)


def test_a_name_that_is_a_weekday_never_picks_a_day_silently():
    # «Juma» is a name too: with another day word it is two days, not a guess.
    assert when.parse("Juma aka dushanba keladi", today=TODAY) is None


def test_moment_needs_both_halves():
    both = when.When(TOMORROW, time(14, 0))
    assert both.moment(settings.tz) == datetime(2026, 10, 23, 14, 0, tzinfo=settings.tz)
    assert when.When(TOMORROW, None).moment(settings.tz) is None
    assert when.When(None, time(14, 0)).moment(settings.tz) is None


def test_records_and_the_reader_share_one_day_table():
    from miya.services import records

    assert records._RELATIVE_DAYS is when.RELATIVE_DAYS
    assert records._WEEKDAYS is when.WEEKDAYS


def test_when_is_pure():
    """No database, no settings, no network: only the text helpers."""
    tree = ast.parse(WHEN_PY.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    miya = {name for name in imported if name.startswith("miya")}
    assert miya == {"miya.services.text"}

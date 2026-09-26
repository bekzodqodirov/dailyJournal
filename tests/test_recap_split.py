"""WP-50: long recaps are split between sections, never clipped; buttons
only for lines the owner can see."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal

from miya.bot import keyboards, replies
from miya.bot.formatting import TELEGRAM_LIMIT, split_message, tg_len
from miya.config import settings
from miya.db import models as m
from miya.db.enums import Currency, DebtDirection
from miya.services.brief import MorningBrief
from miya.services.queries import DebtBalance


def test_short_text_is_one_unchanged_part():
    assert split_message("salom\n\ndunyo") == ["salom\n\ndunyo"]


def _long(lines: int = 400) -> str:
    blocks = []
    for b in range(lines // 20):
        blocks.append(
            "\n".join(f"📦 <b>bo'lim {b}</b> qator {i} — yuk" for i in range(20))
        )
    return "\n\n".join(blocks)


def test_every_part_fits_in_utf16_units():
    text = _long(600)
    assert len(text) > 12_000
    parts = split_message(text, max_parts=10, continued="(davomi {i}/{n})")
    assert len(parts) > 1
    assert all(tg_len(p) <= TELEGRAM_LIMIT for p in parts)


def test_sections_are_kept_whole_when_they_fit():
    blocks = ["\n".join(f"{b}-{i}" for i in range(50)) for b in range(12)]
    parts = split_message("\n\n".join(blocks), limit=800, max_parts=20)
    for block in blocks:
        assert any(block in part for part in parts)


def test_no_line_or_tag_is_split():
    parts = split_message(_long(600), max_parts=10, continued="(davomi {i}/{n})")
    for part in parts:
        assert part.count("<b>") == part.count("</b>")
        for line in part.split("\n"):
            assert not line or line.startswith(("📦", "(davomi")), line


def test_continuation_header_numbers_parts():
    parts = split_message(_long(600), max_parts=10, continued="(davomi {i}/{n})")
    n = len(parts)
    for i, part in enumerate(parts[1:], start=2):
        assert part.startswith(f"(davomi {i}/{n})\n\n")


def test_overflow_caps_parts_and_says_where_the_rest_is():
    parts = split_message(
        _long(600), max_parts=2, continued="(davomi {i}/{n})", overflow="… /hisobot"
    )
    assert len(parts) == 2
    assert parts[-1].endswith("… /hisobot")
    assert all(tg_len(p) <= TELEGRAM_LIMIT for p in parts)


def test_brief_buttons_only_for_visible_lines():
    balances = []
    for i in range(60):
        person = m.Person(id=1000 + i, display_name=f"Mijoz {i} " + "x" * 60, aliases=[])
        balances.append(
            DebtBalance(
                person=person,
                direction=DebtDirection.they_owe_me,
                currency=Currency.UZS,
                outstanding=Decimal("1000000"),
                earliest_due=date(2026, 9, 1),
                count=1,
                ids=[5000 + i],
            )
        )
    brief = MorningBrief(
        now=datetime(2026, 9, 26, 9, 0, tzinfo=settings.tz), due={"debts": balances}
    )
    parts = replies.morning_brief_parts(brief, max_parts=1)
    assert len(parts) == 1 and parts[0].endswith(replies.BRIEF_OVERFLOW)
    due, _ = replies.morning_brief_refs(brief, parts)
    assert 0 < len(due) < 60
    keyboard = keyboards.brief_actions(due)
    for row in keyboard.inline_keyboard:
        for button in row:
            handle = re.search(r"d\d+", button.callback_data)
            assert handle and handle.group(0) in parts[0]

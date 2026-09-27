"""WP-53: the evening recap, rendered — pure, from built inputs."""

from __future__ import annotations

import ast
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from miya.bot import recap_text
from miya.bot.formatting import money
from miya.config import settings
from miya.db import models as m
from miya.db.enums import Currency, DebtDirection
from miya.services import questions, recaps
from miya.services.queries import MoneyDay, RepaymentLine
from tests import recap_helpers

TZ = settings.tz
DAY = datetime(2026, 9, 20, tzinfo=TZ).date()
NOON = datetime(2026, 9, 20, 12, 0, tzinfo=TZ)


def _person_day(name="Akmal", pid=1, **fields) -> recaps.PersonDay:
    person = m.Person(id=pid, display_name=name, aliases=[])
    base = {"messages_in": 3, "messages_out": 1, "last_at": NOON}
    return recaps.PersonDay(person=person, **{**base, **fields})


def _money(**fields) -> MoneyDay:
    return MoneyDay(**fields)


def _busy(**fields):
    base = {
        "money": _money(
            income={Currency.UZS: Decimal("5000000.00"), Currency.USD: Decimal("300.00")},
            expense={Currency.UZS: Decimal("250000.00")},
        ),
        "people": [_person_day()],
        "groups": [
            recaps.GroupDay(tg_chat_id=-1, title="Yuk guruhi", messages=12, to_me=2)
        ],
        "calls": recaps.CallStats(total=2, incoming=1, outgoing=1, seconds=420),
    }
    return recap_helpers.activity(DAY, **{**base, **fields})


def test_sections_in_order_with_exact_headings():
    text = recap_helpers.render(_busy(), day=DAY)
    order = [
        "🌆 <b>Bugun nima bo'ldi</b>",
        recap_text.RECAP_MONEY,
        recap_text.RECAP_PEOPLE,
        recap_text.RECAP_GROUPS,
        recap_text.RECAP_CALLS,
    ]
    positions = [text.index(h) for h in order]
    assert positions == sorted(positions)


def test_empty_day_is_one_short_message():
    parts = recap_text.evening_parts(
        recap_helpers.activity(DAY), {}, None, None, day=DAY, tz=TZ
    )
    assert len(parts) == 1
    assert recap_text.EVENING_EMPTY in parts[0]
    assert recap_text.RECAP_MONEY not in parts[0]


def test_every_figure_is_formatting_money_of_the_input():
    text = recap_helpers.render(_busy(), day=DAY)
    assert f"Kirim: {money(Decimal('5000000.00'), Currency.UZS)}" in text
    assert f"Kirim: {money(Decimal('300.00'), Currency.USD)}" in text
    assert f"Chiqim: {money(Decimal('250000.00'), Currency.UZS)}" in text
    # One line per currency, never a sum across them.
    assert text.count("Kirim:") == 2


def test_prose_is_labelled_escaped_and_footer_present():
    act = _busy()
    text = recap_helpers.render(
        act, prose={"p:1": "Invoys <b>haqida</b> so'radi"}, day=DAY, prose_status="model"
    )
    assert "🤖 <i>Invoys &lt;b&gt;haqida&lt;/b&gt; so'radi</i>" in text
    assert text.rstrip().endswith(recap_text.PROSE_LABEL)


def test_fallback_says_the_ai_was_down_and_keeps_all_lists():
    text = recap_helpers.render(_busy(), day=DAY, prose_status="fallback")
    assert recap_text.PROSE_DOWN in text
    assert recap_text.RECAP_PEOPLE in text and recap_text.RECAP_GROUPS in text
    assert "Qisqa xulosa yo'q — /tarix Akmal" in text


def test_person_block_shows_questions_claims_and_refs():
    day = _person_day(
        questions=[
            recaps.AskedQuestion(1, "Yuk qachon keladi?", NOON, False, None),
            recaps.AskedQuestion(2, "Narxi qancha?", NOON, True, None),
        ],
        claim_ids=[12],
        new_debt_ids=[44],
        transaction_ids=[7],
    )
    text = recap_helpers.render(_busy(people=[day]), day=DAY)
    assert "❓ So'radi: «Yuk qachon keladi?» — javobsiz" in text
    assert "❓ So'radi: «Narxi qancha?» — javob berildi" in text
    assert "🗣 Da'vo: 1 ta tasdiq kutmoqda (<code>c12</code>) — /savollar" in text
    assert "🧾 Bugun yozildi: <code>d44, x7</code>" in text


def test_client_code_is_shown_when_present():
    text = recap_helpers.render(_busy(), day=DAY, codes={1: ["GS367"]})
    assert "<b>Akmal</b> (GS367)" in text
    two = recap_helpers.render(_busy(), day=DAY, codes={1: ["GS367", "GS412"]})
    assert "(GS367)" not in two


def test_every_counterparty_string_is_escaped():
    hostile = "<script>x</script>"
    act = _busy(
        people=[_person_day(name=hostile)],
        groups=[recaps.GroupDay(tg_chat_id=-1, title=hostile, messages=1)],
        money=_money(
            repayments=[
                RepaymentLine(
                    1,
                    2,
                    3,
                    hostile,
                    DebtDirection.they_owe_me,
                    Decimal("10.00"),
                    Currency.USD,
                    NOON,
                )
            ]
        ),
    )
    text = recap_helpers.render(act, day=DAY)
    assert "<script>" not in text and "&lt;script&gt;" in text


def test_recap_text_imports_only_formatting():
    tree = ast.parse(Path(recap_text.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
    assert imported <= {"__future__", "miya.bot.formatting"}


def test_queue_line_renders_in_the_footer():
    queue = questions.QueueSummary(waiting=4, money=2)
    text = recap_helpers.render(_busy(), queue=queue, day=DAY)
    assert "❓ Yana 4 ta savol navbatda (2 tasi pul bo'yicha) — /savollar" in text
    assert text.index("navbatda") > text.index(recap_text.RECAP_CALLS)

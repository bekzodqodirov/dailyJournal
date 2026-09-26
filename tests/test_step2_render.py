"""Build step 2, verifier fixes C1–C4: rendering and wording.

Pure functions, no database: the open-loop lines in their two forms (owner
markup / the report's plain data block), the import boundary between the
report and the bot's reply module, the reworded strings, the channel-aware
new-chat question, and the callback payloads under Telegram's 64-byte cap.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from miya.bot import formatting as f
from miya.bot import keyboards, recap_text, replies
from miya.config import settings
from miya.db.enums import (
    ChatType,
    Currency,
    DebtDirection,
    DebtStatus,
    PromiseMadeBy,
    PromiseStatus,
    TaskStatus,
)
from miya.services.loops import QuietCounterparty, StaleCommitment, UnansweredQuestion
from miya.services.queries import DebtBalance
from tests import recap_helpers

NOW = datetime(2026, 9, 15, 9, 0, tzinfo=settings.tz)
TEN_DIGITS = 9_999_999_999


def _person(name="Akmal"):
    return SimpleNamespace(display_name=name)


def _question(**kw) -> UnansweredQuestion:
    fields = {
        "kind": "question",
        "ref": "q7",
        "age": timedelta(hours=6),
        "stake_rank": Decimal(0),
        "person": _person(),
        "interaction_id": 7,
        "tg_chat_id": 1001,
        "chat_title": "Akmal",
        "is_group": False,
        "text": "konteyner qachon keladi?",
        "asked_at": NOW - timedelta(hours=6),
        "follow_ups": 0,
    }
    return UnansweredQuestion(**{**fields, **kw})


def _promise(**kw):
    fields = {
        "id": 7,
        "made_by": PromiseMadeBy.them,
        "description": "invoice yuboradi",
        "due_date": None,
        "status": PromiseStatus.open,
    }
    return SimpleNamespace(**{**fields, **kw})


def _stale(record_kind="promise", record=None, **kw) -> StaleCommitment:
    record = record or _promise()
    fields = {
        "kind": record_kind,
        "ref": f.ref(record_kind, record.id),
        "age": timedelta(days=9),
        "stake_rank": Decimal(0),
        "person": _person(),
        "record_kind": record_kind,
        "record": record,
        "description": getattr(record, "description", ""),
        "created_at": NOW - timedelta(days=9),
        "last_touched_at": NOW - timedelta(days=9),
        "untouched_for": timedelta(days=9),
        "outstanding": None,
        "currency": None,
    }
    return StaleCommitment(**{**fields, **kw})


def _balance(direction=DebtDirection.i_owe_them, ids=(5,), **kw) -> DebtBalance:
    fields = {
        "person": _person("Sardor"),
        "direction": direction,
        "currency": Currency.USD,
        "outstanding": Decimal("1200"),
        "earliest_due": None,
        "count": len(ids),
        "ids": list(ids),
    }
    return DebtBalance(**{**fields, **kw})


def _quiet(**kw) -> QuietCounterparty:
    fields = {
        "kind": "quiet",
        "ref": "",
        "age": timedelta(days=40),
        "stake_rank": Decimal(0),
        "person": _person("Sardor"),
        "last_contact_at": NOW - timedelta(days=40),
        "days_quiet": 40,
        "balances": [_balance()],
        "promises": [],
    }
    return QuietCounterparty(**{**fields, **kw})


def _payloads(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def _imported_modules(path: Path) -> set[str]:
    """Every module a file imports at module level, by AST — not by grep."""
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _type_only() -> set[str]:
    """Imports formatting.py makes under ``if TYPE_CHECKING`` (annotations)."""
    tree = ast.parse(Path(f.__file__).read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and getattr(node.test, "id", None) == "TYPE_CHECKING":
            for sub in ast.walk(node):
                if isinstance(sub, ast.ImportFrom) and sub.module:
                    names.add(sub.module)
                    names.update(f"{sub.module}.{a.name}" for a in sub.names)
    return names


# --- C1: age_label's home, and one renderer for the brief and the report -----


def test_age_label_lives_in_formatting_and_replies_reexports_it():
    assert replies.age_label is f.age_label
    assert f.age_label(timedelta(hours=3, minutes=20)) == "3 soat"
    assert f.age_label(timedelta(days=2, hours=5)) == "2 kun"
    assert f.age_label(timedelta(seconds=-5)) == "0 daqiqa"


def test_the_recap_renderer_does_not_import_the_bots_reply_module():
    """recap_text imports only formatting: the services build the recap and
    call it, so it can never be part of an import cycle (WP-53)."""
    imported = _imported_modules(Path(recap_text.__file__))
    assert not any(name.startswith("miya.bot.replies") for name in imported)
    assert "miya.bot.formatting" in imported
    # formatting itself never imports the services at runtime — that is what
    # keeps it importable from reports.py.
    runtime = _imported_modules(Path(f.__file__))
    assert not any(name.startswith("miya.services") for name in runtime - _type_only())


def test_formatting_imports_cleanly_without_the_services_loaded():
    """The runtime import graph, not just the AST: formatting must not pull
    the services in — reports.py relies on that."""
    code = (
        "import sys; import miya.bot.formatting; "
        "print(sorted(m for m in sys.modules if m.startswith('miya.services')))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]"


def test_question_line_plain_form_is_the_reports_previous_rendering():
    q = _question(follow_ups=2)
    assert (
        replies.question_line(q, markup=False)
        == "Akmal, 6 soat javobsiz: «konteyner qachon keladi?» (+2 xabar)"
    )
    assert (
        replies.question_line(q)
        == "<b>Akmal</b> · 6 soat: «konteyner qachon keladi?» (+2 xabar)"
    )


def test_question_line_names_the_group_in_both_forms():
    q = _question(is_group=True, chat_title="GZ & Co")
    assert replies.question_line(q, markup=False).startswith("Akmal (GZ & Co), 6 soat")
    assert replies.question_line(q).startswith("<b>Akmal</b> (GZ &amp; Co) · 6 soat")


def test_quiet_line_plain_form_is_the_reports_previous_rendering():
    q = _quiet(promises=[_promise()])
    assert (
        replies.quiet_line(q, markup=False)
        == "Sardor: 40 kun jim — $1200 (qarzingiz); 1 ta ochiq va'da"
    )
    assert (
        replies.quiet_line(q)
        == "<b>Sardor</b> · 40 kun jim: ← sen $1200 <code>d5</code>; 1 ta va'da"
    )
    they_owe = _quiet(balances=[_balance(DebtDirection.they_owe_me, ids=(5, 8))])
    assert "$1200 (sizdan qarzi)" in replies.quiet_line(they_owe, markup=False)
    assert "→ senga $1200 <code>d5, d8</code>" in replies.quiet_line(they_owe)
    assert replies.quiet_line(_quiet(balances=[]), markup=False).endswith("jim — —")


def test_plain_form_carries_no_tags_and_no_escaping():
    """The report escapes the finished line itself, so escaping it there
    equals escaping every name in it — which is what the block promises."""
    q = _question(person=_person("Akmal <GZ>"), text="a & b?")
    plain = replies.question_line(q, markup=False)
    assert "<b>" not in plain and "&lt;" not in plain and "&amp;" not in plain
    assert f.escape(plain) == "Akmal &lt;GZ&gt;, 6 soat javobsiz: «a &amp; b?»"
    assert "<code>" not in replies.quiet_line(_quiet(), markup=False)


def test_the_recap_uses_the_shared_renderers_escaped():
    act = recap_helpers.activity(
        NOW.date(),
        questions_open=[_question(person=_person("Akmal <GZ>"), follow_ups=1)],
    )
    text = recap_helpers.render(act)
    [section] = [b for b in text.split("\n\n") if b.startswith(recap_text.RECAP_OPEN)]
    assert "Akmal &lt;GZ&gt;" in section and "konteyner qachon keladi?" in section
    assert "<GZ>" not in text


def test_a_long_question_is_cut_the_same_way_in_both_forms():
    q = _question(text="x" * 200)
    for markup in (True, False):
        line = replies.question_line(q, markup=markup)
        assert "«" + "x" * 119 + "…»" in line


# --- C2: wording -------------------------------------------------------------


def test_stale_line_says_how_long_nothing_moved():
    line = replies.stale_line(_stale())
    assert line.endswith(" · 9 kundan beri harakat yo'q")
    assert "tegilmagan" not in line
    assert line.startswith("🤝 <code>p7</code> U — Akmal: invoice yuboradi · muddatsiz")
    plain = replies.stale_line(_stale(), markup=False)
    assert (
        plain
        == "🤝 p7 U — Akmal: invoice yuboradi · muddatsiz · 9 kundan beri harakat yo'q"
    )


def test_stale_debt_line_shows_what_is_still_owed():
    debt = SimpleNamespace(
        id=12,
        direction=DebtDirection.they_owe_me,
        amount=Decimal("5000000"),
        currency=Currency.UZS,
        due_date=None,
        status=DebtStatus.partially_paid,
    )
    s = _stale("debt", debt, outstanding=Decimal("2000000"), currency=Currency.UZS)
    line = replies.stale_line(s)
    assert "→ senga: 2 mln so'm" in line and "5 mln" not in line
    assert line.endswith("qisman to'langan · 9 kundan beri harakat yo'q")


def test_the_brief_sections_read_naturally_and_keep_their_order():
    assert replies.BRIEF_STALE == "📌 <b>Muddatsiz, turib qolganlar</b>"
    assert "qarab" not in replies.BRIEF_STALE
    assert replies.BRIEF_DUE == "⏰ <b>Muddati bugun va kechikkanlar</b>"
    loops = SimpleNamespace(
        questions=[_question()],
        stale=[_stale()],
        quiet=[_quiet()],
        is_empty=lambda: False,
    )
    brief = SimpleNamespace(
        day=NOW.date(),
        events=[SimpleNamespace(start_at=NOW, title="Bojxona", location=None)],
        due={"promises": [(_promise(id=9, due_date=NOW.date()), _person())]},
        loops=loops,
        is_empty=lambda: False,
    )
    body = replies.morning_brief(brief)
    order = [
        replies.BRIEF_EVENTS,
        replies.BRIEF_DUE,
        replies.BRIEF_QUESTIONS,
        replies.BRIEF_STALE,
        replies.BRIEF_QUIET,
    ]
    positions = [body.index(label) for label in order]
    assert positions == sorted(positions)
    assert "9 kundan beri harakat yo'q" in body


def test_the_brief_all_clear_and_the_nudge_strings():
    assert replies.BRIEF_ALL_CLEAR == (
        "✅ Hammasi joyida — bugun uchrashuv ham, ochiq qolgan narsa ham yo'q."
    )
    assert replies.NUDGE_ANSWERED == (
        "✅ Javob berilgan deb yozib qo'ydim — boshqa eslatmayman."
    )
    assert "belgilandi" not in replies.NUDGE_ANSWERED
    tz = settings.tz
    now = datetime.now(tz)
    today = now.replace(hour=9, minute=0, second=0, microsecond=0)
    assert replies.nudge_snoozed(today) == (
        "⏰ Bugun 09:00 dagi ertalabki xulosada yana eslataman."
    )
    assert replies.nudge_snoozed(today + timedelta(days=1)) == (
        "⏰ Ertaga 09:00 dagi ertalabki xulosada yana eslataman."
    )
    assert "to'liq ro'yxat: /ertalab" in replies.nudge_overflow(3)
    assert "/ertalab — kechagi xulosa va bugungi ishlar" in replies.HELP


def test_the_nudge_counts_follow_ups_in_plain_words():
    q = _question(is_group=True, chat_title="GZ <Co>", follow_ups=2)
    body = replies.nudge(q)
    assert body.splitlines()[0] == replies.NUDGE_HEADER
    assert "<b>Akmal</b> · GZ &lt;Co&gt; · 6 soat oldin" in body
    assert "<i>Keyin yana 2 ta xabar keldi — hali javob yo'q.</i>" in body
    assert "keyingi xabar" not in body
    assert "<i>" not in replies.nudge(_question())


def test_every_owner_facing_loop_line_escapes_html():
    hostile = _person("<b>Akmal</b>")
    for line in (
        replies.question_line(_question(person=hostile, text="<i>x?</i>")),
        replies.stale_line(_stale(person=hostile, record=_promise(description="<u>"))),
        replies.quiet_line(_quiet(person=hostile)),
        replies.nudge(_question(person=hostile, chat_title="<s>", is_group=True)),
        replies.new_group_question("<GZ>", -1, chat_type=ChatType.channel),
        replies.new_group_accepted("<GZ>", -1, 7),
        replies.new_group_declined("<GZ>", -1),
    ):
        assert "<b>Akmal</b>" not in line
        assert "<i>x" not in line and "<u>" not in line and "<s>" not in line
        assert "<GZ>" not in line


# --- C3: a channel is not a group ------------------------------------------------


@pytest.mark.parametrize(
    ("chat_type", "label"),
    [
        (ChatType.channel, "📢 <b>Yangi kanal:</b>"),
        (ChatType.group, "👥 <b>Yangi guruh:</b>"),
        (None, "👥 <b>Yangi guruh:</b>"),
        (ChatType.private, "👥 <b>Yangi guruh:</b>"),
    ],
)
def test_new_chat_question_names_the_kind_of_chat(chat_type, label):
    body = replies.new_group_question("GZ <logistika>", -222, chat_type=chat_type)
    assert body == f"{label} GZ &lt;logistika&gt; — o'qiymi?"


def test_new_chat_question_positional_chat_type_and_untitled_chat():
    assert replies.new_group_question(None, -5, ChatType.channel) == (
        "📢 <b>Yangi kanal:</b> chat -5 — o'qiymi?"
    )
    assert "Yangi guruh" in replies.new_group_question(None, -5)


def test_the_two_answers_do_not_say_guruh_and_read_as_first_person():
    accepted = replies.new_group_accepted("GZ", -222, 7)
    declined = replies.new_group_declined("GZ", -222)
    assert "guruh" not in accepted and "guruh" not in declined
    assert accepted == "✅ <b>GZ</b> — endi o'qiyman. Oxirgi 7 kunini ham o'qib chiqaman."
    assert declined == "👌 <b>GZ</b> — o'qimayman. Kerak bo'lsa /chats dan yoqasiz."


# --- C4: buttons match the words, payloads fit ------------------------------------


def test_button_labels_match_the_wording():
    nudge = keyboards.nudge_actions(7)
    assert _labels(nudge) == ["✅ Javob berdim", "⏰ Ertalab eslat"]
    group = keyboards.new_group_question(3)
    assert _labels(group) == ["✅ Ha", "✖️ Yo'q"]
    # The brief reuses the reminder's rows — labelled, since it carries many.
    brief = keyboards.brief_actions([("debt", 12)])
    assert _labels(brief) == ["✅ Bajarildi d12", "✏️ Tuzat d12", "🔄 Teskari d12"]


def test_every_step2_payload_fits_telegrams_64_bytes_for_a_ten_digit_id():
    markups = [
        keyboards.nudge_actions(TEN_DIGITS),
        keyboards.new_group_question(TEN_DIGITS),
        keyboards.brief_actions(
            [("debt", TEN_DIGITS), ("promise", TEN_DIGITS), ("task", TEN_DIGITS)]
        ),
        keyboards.question_actions(
            [
                ("debt", TEN_DIGITS - 1),
                ("promise", TEN_DIGITS - 1),
                ("task", TEN_DIGITS - 1),
            ]
        ),
    ]
    payloads = [p for m in markups for p in _payloads(m)]
    assert payloads, "no payloads rendered"
    assert all(len(p.encode()) <= 64 for p in payloads)
    assert f"rec:qa:q{TEN_DIGITS}" in payloads and f"ng:n:{TEN_DIGITS}" in payloads
    assert (
        f"rec:o:d{TEN_DIGITS - 1}" in payloads and f"rec:c:t{TEN_DIGITS - 1}" in payloads
    )


def test_record_line_plain_form_for_every_kind():
    debt = SimpleNamespace(
        id=1,
        direction=DebtDirection.they_owe_me,
        amount=Decimal("100"),
        currency=Currency.USD,
        due_date=None,
        status=DebtStatus.open,
    )
    task = SimpleNamespace(
        id=3, description="<x>", due_date=None, status=TaskStatus.doing
    )
    assert f.record_line("debt", debt, _person("A<b")) == (
        "💰 <code>d1</code> A&lt;b → senga: $100"
    )
    assert f.record_line("debt", debt, _person("A<b"), markup=False) == (
        "💰 d1 A<b → senga: $100"
    )
    assert f.record_line("task", task, markup=False) == "✔️ t3 <x> · muddatsiz · jarayonda"
    assert replies.record_line is f.record_line

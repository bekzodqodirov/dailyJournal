"""docs/owner-decisions.md is binding; it must say what the owner said on 2026-09-25.

No database. Guards against the file drifting back to superseded answers
(20–30 confirmations, SMS "not built yet") or presenting a design default as
the owner's words.
"""

from __future__ import annotations

from pathlib import Path

DOC = Path(__file__).resolve().parents[1] / "docs" / "owner-decisions.md"


def _text() -> str:
    return DOC.read_text(encoding="utf-8")


def test_the_2026_09_25_answers_are_recorded():
    text = _text()
    for needle in ("5–10 confirmations a day", "Payme app", "GS code", "bekzodaka"):
        assert needle in text, needle


def test_the_old_confirmation_budget_is_not_a_standing_rule():
    for line in _text().splitlines():
        if "20–30" in line:
            assert "it was 20–30" in line, line


def test_design_defaults_are_marked_as_such():
    lines = [line.strip().lstrip("- ") for line in _text().splitlines()]
    assert any(line.startswith("Design default") for line in lines)


def test_profiles_no_longer_refresh_every_30_minutes():
    assert "profiles refresh every 30 min" not in _text()


def test_every_owner_quote_is_verbatim():
    for line in _text().splitlines():
        if "Owner's words" in line:
            assert "«" in line, line


def test_the_owners_username_is_never_written_here():
    # Answer 8 contained the real @username; it belongs in .env only.
    text = _text()
    assert "@‹username›" in text
    for line in text.splitlines():
        if "@" in line and "Owner's words" in line:
            assert "@‹username›" in line, line


def test_the_2026_10_20_jarvis_answers_are_recorded():
    # JV-11: the owner's four Jarvis answers, verbatim.
    text = " ".join(_text().split())
    for quote in (
        "«Promptdagidek»",
        "«hammaga yoza olsin faqat men belgilagan chatlarga yozmasin shaxsiy chatlarga»",
        "«hammasida yozsin legin apk fillarni ochmasin va spammlardan ehtiyot bolsin»",
        "«Ehtiyot bo'lib boramiz»",
    ):
        assert quote in text, quote
    assert "Telegram, WhatsApp, Gmail, Google Sheets" in text

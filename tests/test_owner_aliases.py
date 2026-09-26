"""How people address the owner in groups (owner answer 8, 2026-09-25).

No database. The owner's real Telegram username must never enter the
repository: it is public. The forbidden strings therefore come only from the
environment (MIYA_FORBIDDEN_STRINGS, and the @ entries of OWNER_ALIASES when
that is set in the process environment, as on the server).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from dotenv import dotenv_values

from miya.bot import replies
from miya.config import settings
from miya.db.enums import ChatType
from miya.userbot import main as userbot
from tests.test_health_surface import _status

ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = ROOT / ".env.example"
NEW_ALIASES = (
    "Bekzod, Begi, Bekzod aka, bekzodaka, Begika, Bega, GSR Logistics, @owner_test123"
)


def test_env_example_owner_aliases_line_is_blank_and_uncommented():
    lines = [
        line
        for line in ENV_EXAMPLE.read_text().splitlines()
        if line.startswith("OWNER_ALIASES")
    ]
    assert lines == ["OWNER_ALIASES="]
    assert dotenv_values(ENV_EXAMPLE)["OWNER_ALIASES"] == ""


def test_env_example_example_mentions_the_joined_form():
    example = [
        line
        for line in ENV_EXAMPLE.read_text().splitlines()
        if line.startswith("# Example: OWNER_ALIASES=")
    ]
    assert len(example) == 1
    assert "bekzodaka" in example[0] and "@your_username" in example[0]


def _forbidden() -> set[str]:
    values = set(os.environ.get("MIYA_FORBIDDEN_STRINGS", "").split(","))
    aliases = os.environ.get("OWNER_ALIASES", "")
    values |= {a for a in aliases.split(",") if a.strip().startswith("@")}
    return {v.strip().lstrip("@").lower() for v in values if v.strip().lstrip("@")}


def test_no_tracked_file_contains_the_owner_username():
    forbidden = _forbidden()
    if not forbidden:
        pytest.skip("MIYA_FORBIDDEN_STRINGS unset and OWNER_ALIASES has no @entry here")
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=False
    )
    if listed.returncode != 0:
        pytest.skip("not a git checkout")
    offenders = []
    for name in listed.stdout.decode().split("\0"):
        if not name:
            continue
        path = ROOT / name
        try:
            content = path.read_bytes()
        except OSError:
            continue
        if b"\0" in content[:4096]:
            continue  # binary
        text = content.decode("utf-8", errors="ignore").lower()
        offenders += [name for value in forbidden if value in text]
    assert offenders == []


def _group_message(text: str) -> SimpleNamespace:
    return SimpleNamespace(message=text, out=False, mentioned=False)


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        ("bekzodaka, salom", True),
        ("Бекзодака", True),
        ("@owner_test123 qarang", True),
        ("@OWNER_TEST123", True),
        ("Bekzod aka, konteyner qachon?", True),
        ("Begalar keldi", False),
        ("Bekzodjon aytdi", False),
    ],
)
def test_new_alias_list_flags_the_new_forms(monkeypatch, text, flagged):
    monkeypatch.setattr(settings, "owner_aliases", NEW_ALIASES)
    assert userbot.addressed_to_owner(_group_message(text), ChatType.group) is flagged


def test_holat_mentions_blank_aliases(monkeypatch):
    monkeypatch.setattr(settings, "owner_aliases", "")
    assert replies.OWNER_ALIASES_BLANK in replies.status_report(_status(), [])
    monkeypatch.setattr(settings, "owner_aliases", NEW_ALIASES)
    assert replies.OWNER_ALIASES_BLANK not in replies.status_report(_status(), [])

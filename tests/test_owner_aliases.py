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


# --- WP-36: the extractor knows the owner; nobody is created as the owner -----

from datetime import datetime  # noqa: E402

import sqlalchemy as sa  # noqa: E402

from miya.db import models as m  # noqa: E402
from miya.db.enums import Direction, InteractionSource  # noqa: E402
from miya.services import extraction as ex  # noqa: E402
from miya.services import people  # noqa: E402
from miya.services.persistence import apply_extraction  # noqa: E402

WP36_ALIASES = "Bekzod, Begika, @owner_test123"


@pytest.fixture
def owner_aliases(monkeypatch):
    monkeypatch.setattr(settings, "owner_aliases", WP36_ALIASES)


def test_the_block_names_the_owner_but_never_the_username(owner_aliases):
    block = ex.owner_names_block()
    assert "THE OWNER'S OWN NAMES" in block
    assert "Bekzod" in block and "Begika" in block
    assert "@owner_test123" not in block and "owner_test123" not in block
    assert "'Bekzod akaga 5 mln berdim'" in block
    text = ex.extraction_system_block()[0]["text"]
    assert block in text
    assert ex.extraction_system_block() == ex.extraction_system_block()


def test_no_block_without_aliases(monkeypatch):
    monkeypatch.setattr(settings, "owner_aliases", "")
    assert ex.owner_names_block() == ""
    assert "THE OWNER'S OWN NAMES" not in ex.extraction_system_block()[0]["text"]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Bekzod aka", True),
        ("БЕКЗОД", True),
        ("begika", True),
        ("Bekzod Karimov", False),
        ("Akmal", False),
        ("@owner_test123", False),
    ],
)
def test_is_owner_alias(owner_aliases, name, expected):
    assert people.is_owner_alias(name) is expected


def _debt(person: str) -> ex.ExtractedDebt:
    return ex.ExtractedDebt(
        direction="they_owe_me",
        person=person,
        amount=5_000_000,
        currency="UZS",
        reason="yuk",
        asserted_by="me",
    )


async def _note(session) -> m.Interaction:
    interaction = m.Interaction(
        source=InteractionSource.assistant_bot,
        direction=Direction.in_,
        occurred_at=datetime.now(settings.tz),
        raw_text="x",
    )
    session.add(interaction)
    await session.flush()
    return interaction


async def test_a_debt_naming_the_owner_writes_nothing(session, owner_aliases):
    interaction = await _note(session)
    applied = await apply_extraction(
        session, interaction, ex.ExtractionResult(debts=[_debt("Bekzod aka")])
    )
    assert applied.owner_named == ["Bekzod aka"]
    assert interaction.needs_review is True
    assert list(await session.scalars(sa.select(m.Person))) == []
    assert list(await session.scalars(sa.select(m.Debt))) == []
    assert "bu sizning ismingiz" in replies.confirmation(applied)


async def test_a_real_client_sharing_the_first_name_still_resolves(
    session, owner_aliases
):
    client = m.Person(display_name="Bekzod Karimov", aliases=["Bekzod"])
    session.add(client)
    await session.flush()
    interaction = await _note(session)
    applied = await apply_extraction(
        session, interaction, ex.ExtractionResult(debts=[_debt("Bekzod Karimov")])
    )
    [debt] = applied.debts
    assert debt.person_id == client.id and applied.owner_named == []


async def test_a_non_strict_caller_creates_nobody_named_after_the_owner(
    session, owner_aliases
):
    assert await people.resolve_person(session, "Bekzod", telegram_id=4242) is None
    assert list(await session.scalars(sa.select(m.Person))) == []


# --- WP-37: suffixes, joined and hyphenated forms, channels, runtime @ --------


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        ("Bekzodga ayting", True),
        ("bekzodakaga yozdim", True),
        ("Бекзодга", True),
        ("Bekzod-aka, qarang", True),
        ("Бекзодака", True),
        ("GSR Logisticsga", True),
        ("Begaga ayt", True),
        ("@owner_test123 qarang", True),
        ("Begalar keldi", False),
        ("Bekzodjon aytdi", False),
        ("Begim keldi", False),
        ("Bekzodbek keldi", False),
        ("email@owner_test123x", False),
        ("GS367 keldi", False),
    ],
)
def test_suffixed_and_joined_forms(monkeypatch, text, flagged):
    monkeypatch.setattr(settings, "owner_aliases", NEW_ALIASES)
    assert userbot.addressed_to_owner(_group_message(text), ChatType.group) is flagged


def test_channels_and_the_owners_own_messages_are_never_addressed(monkeypatch):
    monkeypatch.setattr(settings, "owner_aliases", NEW_ALIASES)
    assert not userbot.addressed_to_owner(_group_message("Bekzod aka"), ChatType.channel)
    own = SimpleNamespace(message="Bekzod aka", out=True, mentioned=False)
    assert not userbot.addressed_to_owner(own, ChatType.group)
    mention = SimpleNamespace(message="salom", out=False, mentioned=True)
    assert userbot.addressed_to_owner(mention, ChatType.group)


def test_the_runtime_username_counts_without_env(monkeypatch):
    monkeypatch.setattr(settings, "owner_aliases", "")
    monkeypatch.setattr(userbot, "_RUNTIME_ALIASES", ("@owner_test123",))
    assert userbot.addressed_to_owner(
        _group_message("@owner_test123 salom"), ChatType.group
    )

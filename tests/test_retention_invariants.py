"""Message text is only ever deleted by /unut (WP-07; owner answer 5).

The owner's words (2026-09-25): every allowed chat is read and *stored*, and
"such-and-such happened, what do you think?" must be answered from it. The
retention job and future housekeeping sit next to the ``interactions`` table;
one careless DELETE or ``.transcript = None`` would silently destroy the store
that recall depends on. These tests make such a line fail the suite, naming
the file.
"""

from __future__ import annotations

import ast
import os
from datetime import datetime, timedelta
from pathlib import Path

from miya.config import settings
from miya.db import models as m
from miya.db.enums import Direction, InteractionSource
from miya.services import call_recordings

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "miya"
# The one module allowed to delete interactions: /unut's purge.
PURGE = PACKAGE / "services" / "purge.py"
TEXT_FIELDS = {"raw_text", "transcript"}
# Files allowed to set a text field to None/'' (keep this list tiny):
# call_recordings writes a context line on a new row; userbot.record_edit
# (WP-74) must first copy the old value into meta.edits.
BLANKING_ALLOWED = {
    PACKAGE / "services" / "call_recordings.py",
    PACKAGE / "userbot" / "main.py",
}


def _sources() -> list[Path]:
    return sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


def _is_interaction(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "Interaction"


def _looks_like_an_interaction_instance(node: ast.AST) -> bool:
    name = node.id if isinstance(node, ast.Name) else getattr(node, "attr", "")
    return "interaction" in name.lower()


def _call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _deleting_calls(tree: ast.AST) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if (
            name == "delete"
            and node.args
            and (
                _is_interaction(node.args[0])
                or _looks_like_an_interaction_instance(node.args[0])
            )
        ):
            lines.append(node.lineno)
        # sa.update(Interaction).values(raw_text=…/transcript=…)
        if (
            name == "values"
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Call)
            and _call_name(node.func.value) == "update"
            and node.func.value.args
            and _is_interaction(node.func.value.args[0])
            and any(kw.arg in TEXT_FIELDS for kw in node.keywords)
        ):
            lines.append(node.lineno)
    return lines


def test_only_purge_deletes_interactions():
    offenders = []
    for path in _sources():
        if path == PURGE:
            continue
        for line in _deleting_calls(ast.parse(path.read_text(), str(path))):
            offenders.append(f"{path.relative_to(ROOT)}:{line}")
    assert offenders == [], (
        "only miya/services/purge.py (/unut) may delete interactions or their "
        f"text; found: {offenders}"
    )


def _is_blank(value: ast.AST) -> bool:
    return isinstance(value, ast.Constant) and value.value in (None, "")


def _blanking_assignments(tree: ast.AST) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if not _is_blank(value):
            continue
        for target in targets:
            if isinstance(target, ast.Attribute) and target.attr in TEXT_FIELDS:
                lines.append(node.lineno)
    return lines


def test_no_code_blanks_message_text():
    offenders = []
    for path in _sources():
        if path in BLANKING_ALLOWED:
            continue
        for line in _blanking_assignments(ast.parse(path.read_text(), str(path))):
            offenders.append(f"{path.relative_to(ROOT)}:{line}")
    assert offenders == [], f"code blanks raw_text/transcript: {offenders}"


def test_the_guards_catch_what_they_are_for():
    """The detectors themselves, so a refactor cannot make them vacuous."""
    assert _deleting_calls(ast.parse("sa.delete(Interaction).where(x)")) == [1]
    assert _deleting_calls(ast.parse("await session.delete(interaction)")) == [1]
    assert _deleting_calls(
        ast.parse("sa.update(Interaction).values(transcript=None)")
    ) == [1]
    assert _deleting_calls(ast.parse("sa.delete(Debt)")) == []
    assert _blanking_assignments(ast.parse("row.raw_text = None")) == [1]
    assert _blanking_assignments(ast.parse("row.transcript = ''")) == [1]
    assert _blanking_assignments(ast.parse("row.transcript = text")) == []


async def test_retention_job_deletes_audio_but_keeps_text(session, monkeypatch, tmp_path):
    recordings = tmp_path / "calls"
    recordings.mkdir()
    monkeypatch.setattr(settings, "call_recordings_dir", str(recordings))
    media = settings.media_dir  # the bot_media sibling of the recordings folder
    (media / "voice").mkdir(parents=True)
    call_file = recordings / "call.m4a"
    voice_file = media / "voice" / "note.ogg"
    for path in (call_file, voice_file):
        path.write_bytes(b"audio")

    long_ago = datetime.now(settings.tz) - timedelta(
        days=settings.audio_retention_days + 10
    )
    call = m.Interaction(
        source=InteractionSource.phone_call,
        direction=Direction.na,
        occurred_at=long_ago,
        raw_text="Akmal bilan qo'ng'iroq",
        transcript="Konteyner ertaga jo'natiladi",
        media={"path": str(call_file)},
        processed=True,
        needs_review=False,
    )
    voice = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.in_,
        occurred_at=long_ago,
        raw_text="[THEM] (ovozli xabar)",
        transcript="Pulni juma kuni o'tkazaman",
        media={"audio_path": str(voice_file)},
        processed=True,
        needs_review=False,
    )
    session.add_all([call, voice])
    await session.flush()
    old = long_ago.timestamp()
    for path in (call_file, voice_file):
        os.utime(path, (old, old))

    deleted = await call_recordings.purge_old_audio(
        session, now=datetime.now(settings.tz) + timedelta(days=100)
    )

    assert deleted == 2
    assert not call_file.exists() and not voice_file.exists()
    await session.refresh(call)
    await session.refresh(voice)
    assert call.raw_text == "Akmal bilan qo'ng'iroq"
    assert call.transcript == "Konteyner ertaga jo'natiladi"
    assert voice.raw_text == "[THEM] (ovozli xabar)"
    assert voice.transcript == "Pulni juma kuni o'tkazaman"

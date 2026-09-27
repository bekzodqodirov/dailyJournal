"""WP-16: migration 0014's data statement and schema guards."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from miya.db import models as m
from miya.db.enums import ChatType, InteractionSource

ROOT = Path(__file__).resolve().parents[1]


def _load():
    path = ROOT / "alembic" / "versions" / "0014_question_budget.py"
    spec = importlib.util.spec_from_file_location("migration_0014", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mig = _load()


async def test_an_asked_group_left_off_becomes_rule_legacy(session):
    off = m.ChatMonitor(
        tg_chat_id=-1001, chat_type=ChatType.group, title="Off", asked_at=sa.func.now()
    )
    on = m.ChatMonitor(
        tg_chat_id=-1002,
        chat_type=ChatType.group,
        title="On",
        monitor_enabled=True,
        asked_at=sa.func.now(),
    )
    private = m.ChatMonitor(
        tg_chat_id=5, chat_type=ChatType.private, asked_at=sa.func.now()
    )
    session.add_all([off, on, private])
    await session.commit()
    await session.execute(sa.text(mig.BACKFILL_DECIDED_BY))
    await session.commit()
    for row in (off, on, private):
        await session.refresh(row)
    assert (off.decided_by, on.decided_by, private.decided_by) == (
        "rule:legacy",
        "owner",
        None,
    )


async def test_question_log_refuses_an_unknown_kind(session):
    session.add(m.QuestionLog(kind="x", ref="c1", via="push"))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_deleting_a_claim_clears_its_duplicates_link(session):
    row = m.Interaction(
        source=InteractionSource.assistant_bot, occurred_at=sa.func.now(), raw_text="x"
    )
    session.add(row)
    await session.flush()
    first = m.Claim(interaction_id=row.id, kind="debt", payload={})
    session.add(first)
    await session.flush()
    again = m.Claim(interaction_id=row.id, kind="debt", payload={}, duplicate_of=first.id)
    session.add(again)
    await session.commit()
    await session.delete(first)
    await session.commit()
    await session.refresh(again)
    assert again.duplicate_of is None

"""WP-21: migration 0015's dedupe keeps every row and renames the key."""

from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

import sqlalchemy as sa

from miya.config import settings
from miya.db import models as m
from miya.db.enums import Direction, InteractionSource

ROOT = Path(__file__).resolve().parents[1]


def _load():
    path = ROOT / "alembic" / "versions" / "0015_chat_catchup.py"
    spec = importlib.util.spec_from_file_location("migration_0015", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_duplicates_keep_both_rows_and_texts(session):
    mig = _load()
    await session.execute(sa.text("DROP INDEX ux_interactions_tg_message"))
    try:
        rows = [
            m.Interaction(
                source=InteractionSource.telegram_userbot,
                direction=Direction.in_,
                tg_chat_id=99,
                occurred_at=datetime.now(settings.tz),
                raw_text=text,
                meta={"tg_message_id": 5},
            )
            for text in ("birinchi", "ikkinchi")
        ]
        session.add_all(rows)
        await session.flush()
        await session.execute(sa.text(mig.RENAME_DUPLICATES))
        for row in rows:
            await session.refresh(row)
        first, second = rows
        assert first.meta == {"tg_message_id": 5}
        assert second.meta == {"tg_message_id_duplicate": 5}
        assert (first.raw_text, second.raw_text) == ("birinchi", "ikkinchi")
    finally:
        await session.rollback()

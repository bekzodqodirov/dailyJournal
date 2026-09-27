"""Build step 5: self-monitoring.

Until now no process recorded that it was alive: a userbot that lost its
session, a worker stuck at startup or a bot polling nothing looked, from the
outside, exactly like a quiet day. One small table fixes the first half of
that — every process, and every scheduler job, upserts one row here as it
runs, and ``/holat`` plus the worker's health job read the rows back.

``heartbeats``: one row per component ("bot", "worker", "userbot", "api",
"backup") or scheduler job ("job:<id>"), the last time it was seen and a
small JSON detail (``{"enabled": false}`` for a userbot switched off on
purpose, ``{"ok": false, "error": …}`` for a job that raised, ``{"sent":
true, …}`` for the backup that reached Telegram).

Revision ID: 0011_heartbeats
Revises: 0010_person_memory
Create Date: Build step 5
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0011_heartbeats"
down_revision: str | None = "0010_person_memory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "heartbeats",
        sa.Column("component", sa.Text(), primary_key=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_table("heartbeats")

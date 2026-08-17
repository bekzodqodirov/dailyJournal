"""Reminder log — dedupe pings within 24 hours.

Revision ID: 0002_reminder_log
Revises: 0001_initial
Create Date: Phase 1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_reminder_log"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reminder_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("ref", sa.Text, nullable=False),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_reminder_log_kind_ref_sent", "reminder_log", ["kind", "ref", "sent_at"]
    )


def downgrade() -> None:
    op.drop_table("reminder_log")

"""Audit trail for owner corrections on debts, promises and tasks.

Build step 1 (docs/owner-decisions.md): the owner can close and correct any
single record — `/bajarildi`, `/yop`, `/tuzat` and the inline buttons. None of
those may be silent, so every change is appended to a ``history`` JSONB list on
the row itself: ``[{"at", "field", "old", "new", "by"}, ...]``.

A column rather than a separate edits table because the trail belongs to the
row: `/unut` cascades a person's debts and promises away and takes their
corrections with them, with no orphaned rows naming amounts or people left
behind.

Revision ID: 0007_record_history
Revises: 0006_vision_on_for_new_chats
Create Date: Build step 1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_record_history"
down_revision: str | None = "0006_vision_on_for_new_chats"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("debts", "promises", "tasks")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(
            table,
            sa.Column(
                "history",
                postgresql.JSONB(astext_type=sa.Text()),
                server_default=sa.text("'[]'::jsonb"),
                nullable=False,
            ),
        )


def downgrade() -> None:
    for table in TABLES:
        op.drop_column(table, "history")

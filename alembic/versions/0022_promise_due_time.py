"""Promise and task deadlines keep the time of day (JV-03).

«Ertaga 14:00 gacha» is a day and a time. ``due_date`` stays the one source
of the day, so every reader that only knows the date is unchanged; the new
``due_time`` is NULL for a date-only deadline, and a CHECK keeps a time from
existing without a day.

Revision ID: 0022_promise_due_time
Revises: 0021_search_passages
Create Date: JV-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0022_promise_due_time"
down_revision: str | None = "0021_search_passages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("promises", "tasks")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("due_time", sa.Time()))
        op.create_check_constraint(
            f"ck_{table}_due_time", table, "due_time IS NULL OR due_date IS NOT NULL"
        )


def downgrade() -> None:
    for table in TABLES:
        op.drop_constraint(f"ck_{table}_due_time", table, type_="check")
        op.drop_column(table, "due_time")

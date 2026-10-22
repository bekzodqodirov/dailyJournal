"""The Google Sheets mirror's bookkeeping (JV-07).

One new table, ``sheet_rows``: for each promise, task, receipt and followed-up
client mirrored into the owner's spreadsheet, the tab and the sheet row it
owns, the hash of what was last planned for that row, and whether the sheet
has caught up (pending, synced, failed; tombstone and cleared once the entity
is gone). ``entity_id`` is polymorphic and has no foreign key: a purged
entity leaves its row behind as a tombstone, so its cells can be cleared.

The CHECK value lists are frozen here; tests/test_sheets_sync.py keeps them
equal to the model's.

Revision ID: 0024_sheet_rows
Revises: 0023_receipts
Create Date: JV-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0024_sheet_rows"
down_revision: str | None = "0023_receipts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABS = ("Tasks_Promises", "Client_FollowUps", "Receipts")
ENTITY_KINDS = ("promise", "task", "receipt", "person")
STATES = ("pending", "synced", "failed", "tombstone", "cleared")


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.create_table(
        "sheet_rows",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tab", sa.String(length=32), nullable=False),
        sa.Column("entity_kind", sa.String(length=16), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("row_number", sa.Integer()),
        sa.Column("content_hash", sa.CHAR(length=64), nullable=False),
        sa.Column(
            "state", sa.String(length=16), nullable=False, server_default="pending"
        ),
        sa.Column("attempts", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(length=64)),
        sa.Column("synced_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(f"tab IN ({_sql_list(TABS)})", name="ck_sheet_rows_tab"),
        sa.CheckConstraint(
            f"entity_kind IN ({_sql_list(ENTITY_KINDS)})",
            name="ck_sheet_rows_entity_kind",
        ),
        sa.CheckConstraint(f"state IN ({_sql_list(STATES)})", name="ck_sheet_rows_state"),
        sa.CheckConstraint(
            "row_number IS NULL OR row_number >= 2", name="ck_sheet_rows_row_number"
        ),
        sa.UniqueConstraint(
            "tab", "entity_kind", "entity_id", name="uq_sheet_rows_entity"
        ),
    )
    op.create_index(
        "ux_sheet_rows_row_number",
        "sheet_rows",
        ["tab", "row_number"],
        unique=True,
        postgresql_where=sa.text("row_number IS NOT NULL"),
    )
    op.create_index(
        "ix_sheet_rows_due",
        "sheet_rows",
        ["next_attempt_at"],
        postgresql_where=sa.text("state IN ('pending', 'failed', 'tombstone')"),
    )


def downgrade() -> None:
    op.drop_index("ix_sheet_rows_due", table_name="sheet_rows")
    op.drop_index("ux_sheet_rows_row_number", table_name="sheet_rows")
    op.drop_table("sheet_rows")

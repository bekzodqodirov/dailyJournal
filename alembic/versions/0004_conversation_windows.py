"""Conversation windows for the Telegram userbot stream.

Revision ID: 0004_conversation_windows
Revises: 0003_call_recordings
Create Date: Phase 4
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from miya.db import enums as e

revision: str = "0004_conversation_windows"
down_revision: str | None = "0003_call_recordings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WINDOW_LABELS = tuple(m.value for m in e.WindowStatus)
# Referenced by the table below without re-emitting CREATE TYPE (see 0001).
WINDOW_STATUS = postgresql.ENUM(
    *WINDOW_LABELS, name=e.WINDOW_STATUS_ENUM, create_type=False
)


def upgrade() -> None:
    postgresql.ENUM(*WINDOW_LABELS, name=e.WINDOW_STATUS_ENUM).create(
        op.get_bind(), checkfirst=True
    )

    op.create_table(
        "conversation_windows",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tg_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("person_id", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("message_count", sa.Integer(), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "status",
            WINDOW_STATUS,
            server_default=e.WindowStatus.pending.value,
            nullable=False,
        ),
        sa.Column("batch_id", sa.Text(), nullable=True),
        sa.Column("custom_id", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("custom_id"),
    )
    op.create_index(
        "ix_conversation_windows_status",
        "conversation_windows",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_conversation_windows_batch", "conversation_windows", ["batch_id"]
    )

    op.add_column(
        "interactions", sa.Column("window_id", sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        "fk_interactions_window_id",
        "interactions",
        "conversation_windows",
        ["window_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # "Which userbot messages are still unclaimed?" runs every few minutes.
    op.create_index(
        "ix_interactions_unwindowed",
        "interactions",
        ["tg_chat_id", "occurred_at"],
        postgresql_where=sa.text("window_id IS NULL AND source = 'telegram_userbot'"),
    )


def downgrade() -> None:
    op.drop_index("ix_interactions_unwindowed", table_name="interactions")
    op.drop_constraint("fk_interactions_window_id", "interactions", type_="foreignkey")
    op.drop_column("interactions", "window_id")
    op.drop_index("ix_conversation_windows_batch", table_name="conversation_windows")
    op.drop_index("ix_conversation_windows_status", table_name="conversation_windows")
    op.drop_table("conversation_windows")
    postgresql.ENUM(*WINDOW_LABELS, name=e.WINDOW_STATUS_ENUM).drop(
        op.get_bind(), checkfirst=True
    )

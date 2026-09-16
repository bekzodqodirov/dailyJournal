"""Build step 2, part B: the instant path and new groups in one tap.

Two additions, both flags the processes hand each other through the database
because none of them can call the others:

* ``conversation_windows.instant`` — a window from a private chat, or a group
  window holding a message aimed at the owner, is extracted in real time by the
  worker's window job instead of waiting for the next batch. Un-addressed
  group traffic keeps the half-price batch.
* ``chat_monitors.asked_at`` / ``backfill_requested_at`` / ``backfill_done_at``
  / ``backfill_attempts`` — the owner is asked once, in the bot, whether a new
  group or channel should be read; "Ha" switches it on and asks the userbot
  (the only process with a Telegram user session) to read the last week.

Revision ID: 0008_open_loops_surface
Revises: 0007_record_history
Create Date: Build step 2
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_open_loops_surface"
down_revision: str | None = "0007_record_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversation_windows",
        sa.Column("instant", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "chat_monitors", sa.Column("asked_at", sa.DateTime(timezone=True), nullable=True)
    )
    # Every group and channel that exists at this point was either switched
    # on from /chats or left off on purpose: the owner has already decided,
    # and the first sweeps after the deploy must not ask him about each of
    # them, five every two minutes. Only chats discovered from here on are
    # asked. The enum's stored values are 'private', 'group', 'channel'.
    op.execute("UPDATE chat_monitors SET asked_at = now() WHERE chat_type <> 'private'")
    op.add_column(
        "chat_monitors",
        sa.Column("backfill_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "chat_monitors",
        sa.Column("backfill_done_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "chat_monitors",
        sa.Column("backfill_attempts", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("chat_monitors", "backfill_attempts")
    op.drop_column("chat_monitors", "backfill_done_at")
    op.drop_column("chat_monitors", "backfill_requested_at")
    op.drop_column("chat_monitors", "asked_at")
    op.drop_column("conversation_windows", "instant")

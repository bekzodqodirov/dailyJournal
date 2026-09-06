"""Analyse images in monitored chats by default.

Vision started off because every photo is a paid model call and most photos in
a personal Telegram account are not receipts. In practice the owner wants the
opposite default: the invoices and payment screenshots that matter arrive as
images, and a per-chat opt-in meant each one was missed until he noticed and
went to /chats. The toggle stays — this only flips which way it points.

Existing rows are flipped too. A chat left at the old default was never an
explicit "no": nobody had been asked.

Revision ID: 0005_vision_on_by_default
Revises: 0004_conversation_windows
Create Date: Phase 5
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_vision_on_by_default"
down_revision: str | None = "0004_conversation_windows"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "chat_monitors",
        "vision_enabled",
        server_default=sa.true(),
        existing_type=sa.Boolean(),
        existing_nullable=False,
    )
    op.execute("UPDATE chat_monitors SET vision_enabled = true")


def downgrade() -> None:
    op.alter_column(
        "chat_monitors",
        "vision_enabled",
        server_default=sa.false(),
        existing_type=sa.Boolean(),
        existing_nullable=False,
    )

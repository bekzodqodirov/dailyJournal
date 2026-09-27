"""Switch vision on for the chats discovered after 0005.

0005 flipped the column default to true and every row with it, but the chat
registry (`miya.services.chats`) kept passing an explicit ``vision_enabled=False``
on insert, so each chat the userbot discovered after that migration was created
with vision off and its photos were skipped as ``vision_disabled``. The service
now leaves the column to the schema default; this revision repairs the rows made
in between.

The same rule as 0005 applies: a chat sitting at the old default was never an
explicit "no" from the owner — the code decided, not him. There is no toggle
history to tell an explicit /chats opt-out in that window apart from the bug, so
such a chat is flipped too; the cost is a few extra image reads until he taps
👁 again, against a receipt silently left unread.

No schema change, so ``alembic check`` is unaffected. Nothing to undo on
downgrade: the ``false`` values were a defect, not state.

Revision ID: 0006_vision_on_for_new_chats
Revises: 0005_vision_on_by_default
Create Date: Phase 5
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_vision_on_for_new_chats"
down_revision: str | None = "0005_vision_on_by_default"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE chat_monitors SET vision_enabled = true WHERE NOT vision_enabled")


def downgrade() -> None:
    pass

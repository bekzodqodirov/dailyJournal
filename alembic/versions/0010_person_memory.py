"""Build step 4: per-person memory.

"Akmal kim?", "Akmal bilan nima bo'lgan edi?" — the assistant must answer
from everything it holds about one person. Until now a remembered fact was
only reachable by cosine similarity, and the written profile had nowhere to
live. Three additions, no new table:

* ``memories.person_id`` — the person a fact is about (nullable, SET NULL on
  purge of the person), with an index on ``(person_id, occurred_at DESC)`` so
  "his facts, newest first" needs no embedding. Existing facts inherit the
  person of the interaction they were extracted from.
* ``people.profile_updated_at`` — when the short written profile in
  ``people.notes`` was last generated, so the worker refreshes only people
  with activity newer than their profile.
* ``interactions`` index on ``(person_id, occurred_at DESC)`` — the timeline
  query ("what happened with him", newest first, page by ``before``).

Revision ID: 0010_person_memory
Revises: 0009_claims
Create Date: Build step 4
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_person_memory"
down_revision: str | None = "0009_claims"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "memories",
        sa.Column(
            "person_id",
            sa.Integer(),
            sa.ForeignKey("people.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_memories_person_occurred",
        "memories",
        ["person_id", sa.text("occurred_at DESC")],
    )
    op.add_column(
        "people",
        sa.Column("profile_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_interactions_person_occurred",
        "interactions",
        ["person_id", sa.text("occurred_at DESC")],
    )
    # A fact extracted from a private chat or a call is about the person on
    # that interaction. Facts from interactions with no person stay unscoped —
    # they remain reachable by similarity, exactly as before.
    op.execute(
        "UPDATE memories SET person_id = i.person_id FROM interactions i "
        "WHERE memories.source_interaction_id = i.id "
        "AND memories.person_id IS NULL AND i.person_id IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_index("ix_interactions_person_occurred", table_name="interactions")
    op.drop_column("people", "profile_updated_at")
    op.drop_index("ix_memories_person_occurred", table_name="memories")
    op.drop_column("memories", "person_id")

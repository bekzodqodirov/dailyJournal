"""Group windows belong to nobody (WP-47): data only.

A group window used to take its first speaker as its person, and every fact
and summary of the window followed. This detaches them: group window rows
and their window-level memories lose the person, and the people they were
filed under get their written profile marked stale so it is rewritten from
what is really theirs. Memories written from a named people[] item
(tags contain 'person') keep their person: those were never the guess.

Revision ID: 0018_group_windows_unowned
Revises: 0017_identity_codes
Create Date: WP-47
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018_group_windows_unowned"
down_revision: str | None = "0017_identity_codes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GROUP_CHATS = "SELECT tg_chat_id FROM chat_monitors WHERE chat_type::text <> 'private'"

STATEMENTS = (
    f"""
    UPDATE conversation_windows SET person_id = NULL
     WHERE person_id IS NOT NULL AND tg_chat_id IN ({GROUP_CHATS})
    """,
    f"""
    UPDATE interactions SET person_id = NULL
     WHERE metadata->>'kind' = 'window' AND person_id IS NOT NULL
       AND tg_chat_id IN ({GROUP_CHATS})
    """,
    # The CTE keeps the person ids from before the update; RETURNING would
    # hand back the rows after it, with person_id already NULL.
    f"""
    WITH hit AS (
      SELECT m.id, m.person_id
        FROM memories m
        JOIN interactions i ON i.id = m.source_interaction_id
       WHERE i.metadata->>'kind' = 'window'
         AND i.tg_chat_id IN ({GROUP_CHATS})
         AND m.person_id IS NOT NULL
         AND NOT ('person' = ANY(m.tags))
         FOR UPDATE OF m
    ), upd AS (
      UPDATE memories SET person_id = NULL FROM hit WHERE memories.id = hit.id
    )
    UPDATE people SET profile_updated_at = NULL
     WHERE id IN (SELECT DISTINCT person_id FROM hit)
    """,
)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(sa.text(statement))


def downgrade() -> None:
    """A no-op: the first-speaker guess is not worth restoring."""

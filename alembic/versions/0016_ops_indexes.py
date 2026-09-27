"""Missing indexes (WP-23): debts by person, every cascading or set-null
foreign key, and the memories still waiting for an embedding.

Revision ID: 0016_ops_indexes
Revises: 0015_chat_catchup
Create Date: WP-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016_ops_indexes"
down_revision: str | None = "0015_chat_catchup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOURCE_TABLES = (
    "debts",
    "promises",
    "transactions",
    "events",
    "tasks",
    "memories",
    "usage_log",
)
PLAIN = (
    ("ix_conversation_windows_person", "conversation_windows", "person_id"),
    ("ix_interactions_window", "interactions", "window_id"),
    ("ix_tasks_related_promise", "tasks", "related_promise_id"),
    ("ix_claims_person", "claims", "person_id"),
)


def upgrade() -> None:
    op.create_index("ix_debts_person", "debts", ["person_id"])
    for table in SOURCE_TABLES:
        op.create_index(
            f"ix_{table}_source_interaction", table, ["source_interaction_id"]
        )
    op.create_index(
        "ix_memories_unembedded",
        "memories",
        ["id"],
        postgresql_where=sa.text("embedding IS NULL"),
    )
    for name, table, column in PLAIN:
        op.create_index(name, table, [column])


def downgrade() -> None:
    for name, table, _ in reversed(PLAIN):
        op.drop_index(name, table_name=table)
    op.drop_index("ix_memories_unembedded", table_name="memories")
    for table in reversed(SOURCE_TABLES):
        op.drop_index(f"ix_{table}_source_interaction", table_name=table)
    op.drop_index("ix_debts_person", table_name="debts")

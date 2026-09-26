"""Recap prose cache (WP-52): one or two model-written sentences per person
or group of a recap window, keyed by the input they were written from.

Revision ID: 0020_recap_digests
Revises: 0019_recap_delivery
Create Date: WP-52
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0020_recap_digests"
down_revision: str | None = "0019_recap_delivery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recap_digests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("digest_date", sa.Date(), nullable=False),
        sa.Column("subject_key", sa.String(64), nullable=False),
        sa.Column(
            "person_id",
            sa.Integer(),
            sa.ForeignKey("people.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("tg_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("input_hash", sa.CHAR(64), nullable=False),
        sa.Column(
            "source_interaction_ids",
            postgresql.ARRAY(sa.Integer()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("prose", sa.Text(), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "digest_date",
            "subject_key",
            "input_hash",
            name="uq_recap_digests_subject_input",
        ),
    )
    op.create_index(
        "ix_recap_digests_date_end", "recap_digests", ["digest_date", "window_end"]
    )
    op.create_index("ix_recap_digests_person", "recap_digests", ["person_id"])


def downgrade() -> None:
    op.drop_index("ix_recap_digests_person", table_name="recap_digests")
    op.drop_index("ix_recap_digests_date_end", table_name="recap_digests")
    op.drop_table("recap_digests")

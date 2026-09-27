"""Client codes and code mentions (WP-30).

* ``client_codes`` — a code (GS367) held by a person: one active holder per
  code (partial unique index), several codes per person, moves and
  suggestions kept with their history.
* ``code_mentions`` — which interaction named which code or waybill.
* ``interactions.codes_indexed_at`` — when a row's text was indexed.

Every column is nullable or defaulted: no backfill.

Revision ID: 0017_identity_codes
Revises: 0016_ops_indexes
Create Date: WP-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0017_identity_codes"
down_revision: str | None = "0016_ops_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "client_codes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code", sa.Text, nullable=False),
        sa.Column(
            "person_id",
            sa.Integer,
            sa.ForeignKey("people.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column(
            "source_interaction_id",
            sa.Integer,
            sa.ForeignKey("interactions.id", ondelete="CASCADE"),
        ),
        sa.Column("note", sa.Text),
        sa.Column("asked_at", sa.DateTime(timezone=True)),
        sa.Column("answered_at", sa.DateTime(timezone=True)),
        sa.Column(
            "history", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "code ~ '^[A-Z]{1,4}[0-9]{1,9}$'", name="ck_client_codes_format"
        ),
        sa.CheckConstraint(
            "status IN ('active','suggested','rejected','detached')",
            name="ck_client_codes_status",
        ),
    )
    op.create_index(
        "ux_client_codes_active_code",
        "client_codes",
        ["code"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ux_client_codes_code_person", "client_codes", ["code", "person_id"], unique=True
    )
    op.create_index("ix_client_codes_person", "client_codes", ["person_id"])
    op.create_index(
        "ix_client_codes_suggested",
        "client_codes",
        ["created_at"],
        postgresql_where=sa.text("status = 'suggested'"),
    )
    op.create_index(
        "ix_client_codes_source_interaction", "client_codes", ["source_interaction_id"]
    )

    op.create_table(
        "code_mentions",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "interaction_id",
            sa.Integer,
            sa.ForeignKey("interactions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("code", sa.Text, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('client','waybill')", name="ck_code_mentions_kind"),
        sa.UniqueConstraint(
            "interaction_id", "kind", "code", name="ux_code_mentions_interaction_code"
        ),
    )
    op.create_index(
        "ix_code_mentions_code_occurred",
        "code_mentions",
        ["code", sa.text("occurred_at DESC")],
    )

    op.add_column(
        "interactions", sa.Column("codes_indexed_at", sa.DateTime(timezone=True))
    )
    op.create_index(
        "ix_interactions_codes_unindexed",
        "interactions",
        ["id"],
        postgresql_where=sa.text("codes_indexed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_interactions_codes_unindexed", table_name="interactions")
    op.drop_column("interactions", "codes_indexed_at")
    op.drop_index("ix_code_mentions_code_occurred", table_name="code_mentions")
    op.drop_table("code_mentions")
    for name in (
        "ix_client_codes_source_interaction",
        "ix_client_codes_suggested",
        "ix_client_codes_person",
        "ux_client_codes_code_person",
        "ux_client_codes_active_code",
    ):
        op.drop_index(name, table_name="client_codes")
    op.drop_table("client_codes")

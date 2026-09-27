"""Build step 3: ask before writing what a counterparty asserts.

One new table, ``claims``. The extractor already marks every debt, settlement,
promise, transaction and fulfilment with who said it (``asserted_by``); from
this step on what the *other* side asserted — "you owe me", "I paid you back",
"you promised" — is not written into the ledger but parked here as the
extracted item, and the owner is asked. A "Ha" writes the row through the
usual writer and records it in ``result_kind``/``result_id``; a "Yo'q" writes
nothing; a `/tuzat` before the answer edits ``payload`` and is audited in
``history``. ``asked_at`` lets the worker ask what a receipt did not show.

Revision ID: 0009_claims
Revises: 0008_open_loops_surface
Create Date: Build step 3
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009_claims"
down_revision: str | None = "0008_open_loops_surface"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "claims",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "interaction_id",
            sa.Integer(),
            sa.ForeignKey("interactions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "person_id",
            sa.Integer(),
            sa.ForeignKey("people.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("person_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "state", sa.String(length=16), nullable=False, server_default="pending"
        ),
        sa.Column("asked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answered_by", sa.String(length=16), nullable=True),
        sa.Column("result_kind", sa.String(length=16), nullable=True),
        sa.Column("result_id", sa.Integer(), nullable=True),
        sa.Column(
            "history",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_claims_interaction", "claims", ["interaction_id"])
    op.create_index("ix_claims_state_created", "claims", ["state", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_claims_state_created", table_name="claims")
    op.drop_index("ix_claims_interaction", table_name="claims")
    op.drop_table("claims")

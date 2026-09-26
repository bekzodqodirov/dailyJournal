"""The question budget (WP-16): a log of every tap-request put in front of the
owner, duplicate and bank-evidence links on claims, per-group decision and
activity state, and the heartbeats of the jobs the budget replaces.

Revision ID: 0014_question_budget
Revises: 0013_money_ledger
Create Date: WP-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014_question_budget"
down_revision: str | None = "0013_money_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The groups asked about before the budget keep what the owner already has:
# a group he switched on was his decision; one left off is not re-asked.
BACKFILL_DECIDED_BY = """
UPDATE chat_monitors SET decided_by = CASE WHEN monitor_enabled THEN 'owner'
                                           ELSE 'rule:legacy' END
 WHERE asked_at IS NOT NULL AND chat_type::text <> 'private'
"""
# The jobs WP-18 folds into the question batch. Best effort: an old worker
# still running during the migration can re-write them, so the worker also
# drops stale job rows at start.
RETIRED_JOB_HEARTBEATS = """
DELETE FROM heartbeats WHERE component IN
 ('job:claim_ask', 'job:new_chat_ask', 'job:media_ask', 'job:nudges',
  'job:missed_call_nudge')
"""


def upgrade() -> None:
    op.create_table(
        "question_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("ref", sa.Text, nullable=False),
        sa.Column("via", sa.String(8), nullable=False),
        sa.Column("tg_message_id", sa.BigInteger),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('claim','missed','still_open','nudge','groups','media','money')",
            name="ck_question_log_kind",
        ),
        sa.CheckConstraint(
            "via IN ('push','brief','evening','receipt')", name="ck_question_log_via"
        ),
    )
    op.create_index("ix_question_log_sent_at", "question_log", ["sent_at"])
    op.create_index(
        "ix_question_log_kind_ref", "question_log", ["kind", "ref", "sent_at"]
    )

    op.add_column(
        "claims",
        sa.Column(
            "duplicate_of",
            sa.Integer,
            sa.ForeignKey("claims.id", ondelete="SET NULL"),
        ),
    )
    op.add_column(
        "claims",
        sa.Column(
            "evidence_txn_id",
            sa.Integer,
            sa.ForeignKey("transactions.id", ondelete="SET NULL"),
        ),
    )
    op.create_index(
        "ix_claims_duplicate_of",
        "claims",
        ["duplicate_of"],
        postgresql_where=sa.text("duplicate_of IS NOT NULL"),
    )
    op.create_index(
        "ux_claims_evidence_txn",
        "claims",
        ["evidence_txn_id"],
        unique=True,
        postgresql_where=sa.text("evidence_txn_id IS NOT NULL"),
    )

    op.add_column("chat_monitors", sa.Column("decided_by", sa.String(16)))
    op.add_column(
        "chat_monitors", sa.Column("offered_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "chat_monitors",
        sa.Column("digest_shows", sa.SmallInteger, nullable=False, server_default="0"),
    )
    op.add_column(
        "chat_monitors",
        sa.Column("seen_count", sa.Integer, nullable=False, server_default="0"),
    )
    op.add_column(
        "chat_monitors", sa.Column("last_seen_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "chat_monitors", sa.Column("owner_active_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "chat_monitors", sa.Column("addressed_at", sa.DateTime(timezone=True))
    )
    op.execute(BACKFILL_DECIDED_BY)
    op.execute(RETIRED_JOB_HEARTBEATS)


def downgrade() -> None:
    for column in (
        "addressed_at",
        "owner_active_at",
        "last_seen_at",
        "seen_count",
        "digest_shows",
        "offered_at",
        "decided_by",
    ):
        op.drop_column("chat_monitors", column)
    op.drop_index("ux_claims_evidence_txn", table_name="claims")
    op.drop_index("ix_claims_duplicate_of", table_name="claims")
    op.drop_column("claims", "evidence_txn_id")
    op.drop_column("claims", "duplicate_of")
    op.drop_index("ix_question_log_kind_ref", table_name="question_log")
    op.drop_index("ix_question_log_sent_at", table_name="question_log")
    op.drop_table("question_log")

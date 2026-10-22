"""Receipts read from images (JV-04).

One new table, ``receipts``: what a payment screenshot or a terminal check
says — amount and currency, the printed date and time, payer, payee and their
cards' last four digits, the issuing bank or app and its reference — one row
per image interaction. It is the image's word, not the bank's record, so
nothing here books money; ``verdict`` is the later bank cross-check (JV-05)
and starts ``waiting``. ``reading`` keeps the model's answer for audit only.

The CHECK value lists are frozen here; tests/test_receipts_read.py keeps them
equal to the model's.

Revision ID: 0023_receipts
Revises: 0022_promise_due_time
Create Date: JV-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0023_receipts"
down_revision: str | None = "0022_promise_due_time"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DIRECTIONS = ("in", "out", "unknown")
STATES = ("read", "needs_review", "rejected")
VERDICTS = (
    "waiting",
    "confirmed",
    "unconfirmed",
    "suspicious",
    "owner_ok",
    "owner_fake",
)
REASONS = (
    "duplicate_image",
    "duplicate_reference",
    "stale_date",
    "future_date",
    "own_card_no_bank",
    "amount_near_miss",
)


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    currency = postgresql.ENUM(name="currency", create_type=False)
    op.create_table(
        "receipts",
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
        ),
        sa.Column(
            "direction", sa.String(length=8), nullable=False, server_default="unknown"
        ),
        sa.Column("amount", sa.Numeric(14, 2)),
        sa.Column("currency", currency),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.Column("payer_name", sa.Text()),
        sa.Column("payee_name", sa.Text()),
        sa.Column("issuer", sa.Text()),
        sa.Column("reference", sa.Text()),
        sa.Column("payer_card_last4", sa.String(length=4)),
        sa.Column("payee_card_last4", sa.String(length=4)),
        sa.Column("image_sha256", sa.CHAR(length=64)),
        sa.Column(
            "reading",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column(
            "verdict", sa.String(length=16), nullable=False, server_default="waiting"
        ),
        sa.Column(
            "verdict_reasons",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "transaction_id",
            sa.Integer(),
            sa.ForeignKey("transactions.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "claim_id",
            sa.Integer(),
            sa.ForeignKey("claims.id", ondelete="SET NULL"),
        ),
        sa.Column("notified_at", sa.DateTime(timezone=True)),
        sa.Column("notice_tg_message_id", sa.BigInteger()),
        sa.Column("verdict_notified_at", sa.DateTime(timezone=True)),
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
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            f"direction IN ({_sql_list(DIRECTIONS)})", name="ck_receipts_direction"
        ),
        sa.CheckConstraint("amount > 0", name="ck_receipts_amount_positive"),
        sa.CheckConstraint(
            "(amount IS NULL) = (currency IS NULL)", name="ck_receipts_amount_currency"
        ),
        sa.CheckConstraint(
            "payer_card_last4 IS NULL OR payer_card_last4 ~ '^[0-9]{4}$'",
            name="ck_receipts_payer_card_last4",
        ),
        sa.CheckConstraint(
            "payee_card_last4 IS NULL OR payee_card_last4 ~ '^[0-9]{4}$'",
            name="ck_receipts_payee_card_last4",
        ),
        sa.CheckConstraint(f"state IN ({_sql_list(STATES)})", name="ck_receipts_state"),
        sa.CheckConstraint(
            f"verdict IN ({_sql_list(VERDICTS)})", name="ck_receipts_verdict"
        ),
        sa.CheckConstraint(
            f"verdict_reasons <@ ARRAY[{_sql_list(REASONS)}]::text[]",
            name="ck_receipts_verdict_reasons",
        ),
    )
    op.create_index(
        "ux_receipts_interaction", "receipts", ["interaction_id"], unique=True
    )
    op.create_index("ix_receipts_person", "receipts", ["person_id"])
    op.create_index("ix_receipts_image_sha256", "receipts", ["image_sha256"])
    op.create_index(
        "ux_receipts_transaction",
        "receipts",
        ["transaction_id"],
        unique=True,
        postgresql_where=sa.text("transaction_id IS NOT NULL"),
    )
    op.create_index("ix_receipts_claim", "receipts", ["claim_id"])
    op.create_index(
        "ix_receipts_waiting",
        "receipts",
        ["created_at"],
        postgresql_where=sa.text("verdict = 'waiting'"),
    )
    op.create_index(
        "ix_receipts_reference",
        "receipts",
        ["issuer", "reference"],
        postgresql_where=sa.text("reference IS NOT NULL"),
    )


def downgrade() -> None:
    for name in (
        "ix_receipts_reference",
        "ix_receipts_waiting",
        "ix_receipts_claim",
        "ux_receipts_transaction",
        "ix_receipts_image_sha256",
        "ix_receipts_person",
        "ux_receipts_interaction",
    ):
        op.drop_index(name, table_name="receipts")
    op.drop_table("receipts")

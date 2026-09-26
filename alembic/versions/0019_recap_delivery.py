"""The recap delivery ledger (WP-49).

daily_reports gains a kind (evening now, morning later), the window it
covers, the message parts and how many went out, how the prose was made,
and when it was delivered. The report date alone is no longer unique: a
day has one row per kind. Every existing row counts as delivered.

Revision ID: 0019_recap_delivery
Revises: 0018_group_windows_unowned
Create Date: WP-49
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0019_recap_delivery"
down_revision: str | None = "0018_group_windows_unowned"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "daily_reports",
        sa.Column("kind", sa.String(16), nullable=False, server_default="evening"),
    )
    op.add_column(
        "daily_reports", sa.Column("window_start", sa.DateTime(timezone=True))
    )
    op.add_column("daily_reports", sa.Column("window_end", sa.DateTime(timezone=True)))
    op.add_column(
        "daily_reports",
        sa.Column(
            "parts",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "daily_reports",
        sa.Column("parts_sent", sa.SmallInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "daily_reports",
        sa.Column("prose_status", sa.String(16), nullable=False, server_default="none"),
    )
    op.add_column("daily_reports", sa.Column("delivered_at", sa.DateTime(timezone=True)))
    op.add_column(
        "daily_reports",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # History counts as delivered: nothing old is ever sent again.
    op.execute(
        "UPDATE daily_reports SET delivered_at = created_at, window_end = created_at, "
        "parts = jsonb_build_array(content), parts_sent = 1"
    )
    op.create_check_constraint(
        "ck_daily_reports_kind", "daily_reports", "kind IN ('evening','morning')"
    )
    op.create_check_constraint(
        "ck_daily_reports_prose_status",
        "daily_reports",
        "prose_status IN ('model','cached','fallback','none')",
    )
    op.drop_constraint("daily_reports_report_date_key", "daily_reports", type_="unique")
    op.create_unique_constraint(
        "uq_daily_reports_date_kind", "daily_reports", ["report_date", "kind"]
    )
    op.create_index("ix_debt_payments_paid_at", "debt_payments", ["paid_at"])


def downgrade() -> None:
    op.execute("DELETE FROM daily_reports WHERE kind <> 'evening'")
    op.drop_index("ix_debt_payments_paid_at", table_name="debt_payments")
    op.drop_constraint("uq_daily_reports_date_kind", "daily_reports", type_="unique")
    op.create_unique_constraint(
        "daily_reports_report_date_key", "daily_reports", ["report_date"]
    )
    op.drop_constraint("ck_daily_reports_prose_status", "daily_reports", type_="check")
    op.drop_constraint("ck_daily_reports_kind", "daily_reports", type_="check")
    for column in (
        "updated_at",
        "delivered_at",
        "prose_status",
        "parts_sent",
        "parts",
        "window_end",
        "window_start",
        "kind",
    ):
        op.drop_column("daily_reports", column)

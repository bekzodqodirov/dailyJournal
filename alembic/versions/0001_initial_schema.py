"""Initial schema: people, interactions, debts, promises, transactions, memories.

Revision ID: 0001_initial
Revises:
Create Date: Phase 0
"""

from __future__ import annotations

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBED_DIM = 1024  # BAAI/bge-m3

# (type name, labels) — created up front so several tables can share one type.
ENUMS: list[tuple[str, tuple[str, ...]]] = [
    ("currency", ("UZS", "USD", "CNY", "KRW", "RUB")),
    (
        "interaction_source",
        (
            "assistant_bot",
            "telegram_userbot",
            "phone_call",
            "manual",
            "receipt_photo",
            "calendar",
        ),
    ),
    ("direction", ("in", "out", "na")),
    ("chat_type", ("private", "group", "channel")),
    ("debt_direction", ("they_owe_me", "i_owe_them")),
    ("debt_status", ("open", "partially_paid", "settled")),
    ("promise_made_by", ("me", "them")),
    ("promise_status", ("open", "done", "broken", "cancelled")),
    ("transaction_type", ("income", "expense")),
    ("event_source", ("manual", "extracted", "gcal")),
    ("event_status", ("planned", "done", "cancelled")),
    ("task_priority", ("low", "med", "high")),
    ("task_status", ("todo", "doing", "done", "dropped")),
]


def _enum(name: str) -> postgresql.ENUM:
    """Reference an already-created type; never re-emit CREATE TYPE."""
    labels = dict(ENUMS)[name]
    return postgresql.ENUM(*labels, name=name, create_type=False)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    bind = op.get_bind()
    for name, labels in ENUMS:
        postgresql.ENUM(*labels, name=name).create(bind, checkfirst=True)

    op.create_table(
        "people",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column(
            "aliases",
            postgresql.ARRAY(sa.Text),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("telegram_id", sa.BigInteger, unique=True),
        sa.Column("telegram_username", sa.Text),
        sa.Column("phone", sa.Text),
        sa.Column("relationship", sa.Text),
        sa.Column("notes", sa.Text),
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
    )
    op.create_index("ix_people_display_name", "people", ["display_name"])
    op.create_index("ix_people_phone", "people", ["phone"])
    op.create_index("ix_people_aliases", "people", ["aliases"], postgresql_using="gin")

    op.create_table(
        "chat_monitors",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tg_chat_id", sa.BigInteger, nullable=False, unique=True),
        sa.Column("chat_type", _enum("chat_type"), nullable=False),
        sa.Column("title", sa.Text),
        sa.Column(
            "monitor_enabled", sa.Boolean, nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "vision_enabled", sa.Boolean, nullable=False, server_default=sa.false()
        ),
        sa.Column("docs_enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "interactions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("source", _enum("interaction_source"), nullable=False),
        sa.Column(
            "direction", _enum("direction"), nullable=False, server_default="na"
        ),
        sa.Column(
            "person_id",
            sa.Integer,
            sa.ForeignKey("people.id", ondelete="SET NULL"),
        ),
        sa.Column("tg_chat_id", sa.BigInteger),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_text", sa.Text),
        sa.Column("transcript", sa.Text),
        sa.Column("media", postgresql.JSONB),
        sa.Column("summary", sa.Text),
        sa.Column("metadata", postgresql.JSONB),
        sa.Column("processed", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column(
            "needs_review", sa.Boolean, nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_interactions_occurred_at", "interactions", ["occurred_at"])
    op.create_index(
        "ix_interactions_chat_occurred", "interactions", ["tg_chat_id", "occurred_at"]
    )
    op.create_index("ix_interactions_person", "interactions", ["person_id"])
    op.create_index(
        "ix_interactions_unprocessed",
        "interactions",
        ["created_at"],
        postgresql_where=sa.text("processed = false"),
    )

    op.create_table(
        "debts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("direction", _enum("debt_direction"), nullable=False),
        sa.Column(
            "person_id",
            sa.Integer,
            sa.ForeignKey("people.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column(
            "currency", _enum("currency"), nullable=False, server_default="UZS"
        ),
        sa.Column("reason", sa.Text),
        sa.Column(
            "status", _enum("debt_status"), nullable=False, server_default="open"
        ),
        sa.Column("due_date", sa.Date),
        sa.Column(
            "source_interaction_id",
            sa.Integer,
            sa.ForeignKey("interactions.id", ondelete="CASCADE"),
        ),
        sa.Column("notes", sa.Text),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("settled_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("amount > 0", name="ck_debts_amount_positive"),
    )
    op.create_index("ix_debts_status_person", "debts", ["status", "person_id"])
    op.create_index("ix_debts_due_date", "debts", ["due_date"])

    op.create_table(
        "debt_payments",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "debt_id",
            sa.Integer,
            sa.ForeignKey("debts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency", _enum("currency"), nullable=False),
        sa.Column(
            "paid_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("note", sa.Text),
        sa.CheckConstraint("amount > 0", name="ck_debt_payments_amount_positive"),
    )
    op.create_index("ix_debt_payments_debt", "debt_payments", ["debt_id"])

    op.create_table(
        "promises",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("made_by", _enum("promise_made_by"), nullable=False),
        sa.Column(
            "person_id",
            sa.Integer,
            sa.ForeignKey("people.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("due_date", sa.Date),
        sa.Column(
            "status", _enum("promise_status"), nullable=False, server_default="open"
        ),
        sa.Column(
            "source_interaction_id",
            sa.Integer,
            sa.ForeignKey("interactions.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_promises_status_due", "promises", ["status", "due_date"])
    op.create_index("ix_promises_person", "promises", ["person_id"])

    op.create_table(
        "transactions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("type", _enum("transaction_type"), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column(
            "currency", _enum("currency"), nullable=False, server_default="UZS"
        ),
        sa.Column("category", sa.Text),
        sa.Column("description", sa.Text),
        sa.Column(
            "counterparty_person_id",
            sa.Integer,
            sa.ForeignKey("people.id", ondelete="SET NULL"),
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "source_interaction_id",
            sa.Integer,
            sa.ForeignKey("interactions.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("amount > 0", name="ck_transactions_amount_positive"),
    )
    op.create_index(
        "ix_transactions_occurred_type", "transactions", ["occurred_at", "type"]
    )
    op.create_index("ix_transactions_category", "transactions", ["category"])

    op.create_table(
        "events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True)),
        sa.Column("location", sa.Text),
        sa.Column("attendees", postgresql.JSONB),
        sa.Column("description", sa.Text),
        sa.Column(
            "source", _enum("event_source"), nullable=False, server_default="manual"
        ),
        sa.Column("gcal_event_id", sa.Text, unique=True),
        sa.Column(
            "status", _enum("event_status"), nullable=False, server_default="planned"
        ),
        sa.Column(
            "source_interaction_id",
            sa.Integer,
            sa.ForeignKey("interactions.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_events_start_at", "events", ["start_at"])

    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("due_date", sa.Date),
        sa.Column(
            "priority", _enum("task_priority"), nullable=False, server_default="med"
        ),
        sa.Column(
            "status", _enum("task_status"), nullable=False, server_default="todo"
        ),
        sa.Column(
            "source_interaction_id",
            sa.Integer,
            sa.ForeignKey("interactions.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "related_promise_id",
            sa.Integer,
            sa.ForeignKey("promises.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_tasks_status_due", "tasks", ["status", "due_date"])

    op.create_table(
        "memories",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.Vector(dim=EMBED_DIM)),
        sa.Column(
            "source_interaction_id",
            sa.Integer,
            sa.ForeignKey("interactions.id", ondelete="CASCADE"),
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "tags", postgresql.ARRAY(sa.Text), nullable=False, server_default="{}"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_memories_occurred_at", "memories", ["occurred_at"])
    op.create_index("ix_memories_tags", "memories", ["tags"], postgresql_using="gin")
    op.create_index(
        "ix_memories_embedding_hnsw",
        "memories",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    op.create_table(
        "daily_reports",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("report_date", sa.Date, nullable=False, unique=True),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("stats", postgresql.JSONB),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "usage_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("model", sa.Text),
        sa.Column("operation", sa.Text),
        sa.Column("input_tokens", sa.Integer),
        sa.Column("output_tokens", sa.Integer),
        sa.Column("cache_read_tokens", sa.Integer),
        sa.Column("cache_write_tokens", sa.Integer),
        sa.Column("audio_seconds", sa.Numeric(10, 2)),
        sa.Column("batch", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("cost_usd", sa.Numeric(12, 6)),
        sa.Column(
            "source_interaction_id",
            sa.Integer,
            sa.ForeignKey("interactions.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_usage_log_created_at", "usage_log", ["created_at"])


def downgrade() -> None:
    for table in (
        "usage_log",
        "daily_reports",
        "memories",
        "tasks",
        "events",
        "transactions",
        "promises",
        "debt_payments",
        "debts",
        "interactions",
        "chat_monitors",
        "people",
    ):
        op.drop_table(table)

    bind = op.get_bind()
    for name, labels in ENUMS:
        postgresql.ENUM(*labels, name=name).drop(bind, checkfirst=True)

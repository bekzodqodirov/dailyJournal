"""ORM models for MIYA (spec §4).

Conventions:
  * Money is ``NUMERIC(14, 2)`` paired with a ``currency`` enum column.
  * Every timestamp is ``timestamptz``; the application timezone is Asia/Tashkent.
  * Anything derived from an interaction carries ``source_interaction_id`` so a
    purge of that interaction cascades to the rows it produced.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from miya.config import settings
from miya.db import enums as e
from miya.db.base import Base, created_at_column, money_column, pg_enum

# --- Native enum types (single instance each; see pg_enum docstring) ---------
CURRENCY = pg_enum(e.Currency, e.CURRENCY_ENUM)
INTERACTION_SOURCE = pg_enum(e.InteractionSource, e.INTERACTION_SOURCE_ENUM)
DIRECTION = pg_enum(e.Direction, e.DIRECTION_ENUM)
CHAT_TYPE = pg_enum(e.ChatType, e.CHAT_TYPE_ENUM)
DEBT_DIRECTION = pg_enum(e.DebtDirection, e.DEBT_DIRECTION_ENUM)
DEBT_STATUS = pg_enum(e.DebtStatus, e.DEBT_STATUS_ENUM)
PROMISE_MADE_BY = pg_enum(e.PromiseMadeBy, e.PROMISE_MADE_BY_ENUM)
PROMISE_STATUS = pg_enum(e.PromiseStatus, e.PROMISE_STATUS_ENUM)
TRANSACTION_TYPE = pg_enum(e.TransactionType, e.TRANSACTION_TYPE_ENUM)
EVENT_SOURCE = pg_enum(e.EventSource, e.EVENT_SOURCE_ENUM)
EVENT_STATUS = pg_enum(e.EventStatus, e.EVENT_STATUS_ENUM)
TASK_PRIORITY = pg_enum(e.TaskPriority, e.TASK_PRIORITY_ENUM)
TASK_STATUS = pg_enum(e.TaskStatus, e.TASK_STATUS_ENUM)


class Person(Base):
    __tablename__ = "people"

    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # Alternate spellings the owner actually uses: "Akmal aka", "Akmal GZ".
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(sa.Text), nullable=False, server_default="{}"
    )
    telegram_id: Mapped[int | None] = mapped_column(sa.BigInteger, unique=True)
    telegram_username: Mapped[str | None] = mapped_column(sa.Text)
    phone: Mapped[str | None] = mapped_column(sa.Text)
    relationship_: Mapped[str | None] = mapped_column("relationship", sa.Text)
    notes: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = created_at_column(onupdate=sa.func.now())

    __table_args__ = (
        sa.Index("ix_people_display_name", "display_name"),
        sa.Index("ix_people_phone", "phone"),
        sa.Index("ix_people_aliases", "aliases", postgresql_using="gin"),
    )


class ChatMonitor(Base):
    """Which Telegram chats the userbot is allowed to ingest (spec §6)."""

    __tablename__ = "chat_monitors"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_chat_id: Mapped[int] = mapped_column(sa.BigInteger, unique=True, nullable=False)
    chat_type: Mapped[e.ChatType] = mapped_column(CHAT_TYPE, nullable=False)
    title: Mapped[str | None] = mapped_column(sa.Text)
    # Default: private chats on, groups and channels off until whitelisted.
    monitor_enabled: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )
    vision_enabled: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )
    docs_enabled: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.true()
    )
    updated_at: Mapped[datetime] = created_at_column(onupdate=sa.func.now())


class Interaction(Base):
    """Every input lands here first, before extraction."""

    __tablename__ = "interactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[e.InteractionSource] = mapped_column(
        INTERACTION_SOURCE, nullable=False
    )
    direction: Mapped[e.Direction] = mapped_column(
        DIRECTION, nullable=False, server_default=e.Direction.na.value
    )
    person_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("people.id", ondelete="SET NULL")
    )
    tg_chat_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    occurred_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )
    raw_text: Mapped[str | None] = mapped_column(sa.Text)
    transcript: Mapped[str | None] = mapped_column(sa.Text)
    # {type, path, mime, size, caption, processed, sha256, ...}
    media: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    summary: Mapped[str | None] = mapped_column(sa.Text)
    meta: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)
    processed: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )
    # Set when extraction failed validation twice — data is never dropped.
    needs_review: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )
    created_at: Mapped[datetime] = created_at_column()

    person: Mapped[Person | None] = relationship(lazy="raise")

    __table_args__ = (
        sa.Index("ix_interactions_occurred_at", "occurred_at"),
        sa.Index("ix_interactions_chat_occurred", "tg_chat_id", "occurred_at"),
        sa.Index("ix_interactions_person", "person_id"),
        sa.Index(
            "ix_interactions_unprocessed",
            "created_at",
            postgresql_where=sa.text("processed = false"),
        ),
        # Call-recording dedupe: hash lookups happen once a minute (spec §7C).
        sa.Index(
            "ix_interactions_media_sha256",
            sa.text("(media ->> 'sha256')"),
            postgresql_where=sa.text("source = 'phone_call'"),
        ),
    )


class Debt(Base):
    __tablename__ = "debts"

    id: Mapped[int] = mapped_column(primary_key=True)
    direction: Mapped[e.DebtDirection] = mapped_column(DEBT_DIRECTION, nullable=False)
    person_id: Mapped[int] = mapped_column(
        sa.ForeignKey("people.id", ondelete="CASCADE"), nullable=False
    )
    amount: Mapped[Decimal] = money_column(nullable=False)
    currency: Mapped[e.Currency] = mapped_column(
        CURRENCY, nullable=False, server_default=e.Currency.UZS.value
    )
    reason: Mapped[str | None] = mapped_column(sa.Text)
    status: Mapped[e.DebtStatus] = mapped_column(
        DEBT_STATUS, nullable=False, server_default=e.DebtStatus.open.value
    )
    due_date: Mapped[date | None] = mapped_column(sa.Date)
    source_interaction_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("interactions.id", ondelete="CASCADE")
    )
    notes: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = created_at_column()
    settled_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    person: Mapped[Person] = relationship(lazy="raise")
    payments: Mapped[list[DebtPayment]] = relationship(
        back_populates="debt", lazy="raise", cascade="all, delete-orphan"
    )

    __table_args__ = (
        sa.CheckConstraint("amount > 0", name="ck_debts_amount_positive"),
        sa.Index("ix_debts_status_person", "status", "person_id"),
        sa.Index("ix_debts_due_date", "due_date"),
    )


class DebtPayment(Base):
    __tablename__ = "debt_payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    debt_id: Mapped[int] = mapped_column(
        sa.ForeignKey("debts.id", ondelete="CASCADE"), nullable=False
    )
    amount: Mapped[Decimal] = money_column(nullable=False)
    currency: Mapped[e.Currency] = mapped_column(CURRENCY, nullable=False)
    paid_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    note: Mapped[str | None] = mapped_column(sa.Text)

    debt: Mapped[Debt] = relationship(back_populates="payments", lazy="raise")

    __table_args__ = (
        sa.CheckConstraint("amount > 0", name="ck_debt_payments_amount_positive"),
        sa.Index("ix_debt_payments_debt", "debt_id"),
    )


class Promise(Base):
    __tablename__ = "promises"

    id: Mapped[int] = mapped_column(primary_key=True)
    made_by: Mapped[e.PromiseMadeBy] = mapped_column(PROMISE_MADE_BY, nullable=False)
    person_id: Mapped[int] = mapped_column(
        sa.ForeignKey("people.id", ondelete="CASCADE"), nullable=False
    )
    description: Mapped[str] = mapped_column(sa.Text, nullable=False)
    due_date: Mapped[date | None] = mapped_column(sa.Date)
    status: Mapped[e.PromiseStatus] = mapped_column(
        PROMISE_STATUS, nullable=False, server_default=e.PromiseStatus.open.value
    )
    source_interaction_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("interactions.id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime] = created_at_column()
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    person: Mapped[Person] = relationship(lazy="raise")

    __table_args__ = (
        sa.Index("ix_promises_status_due", "status", "due_date"),
        sa.Index("ix_promises_person", "person_id"),
    )


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[e.TransactionType] = mapped_column(TRANSACTION_TYPE, nullable=False)
    amount: Mapped[Decimal] = money_column(nullable=False)
    currency: Mapped[e.Currency] = mapped_column(
        CURRENCY, nullable=False, server_default=e.Currency.UZS.value
    )
    category: Mapped[str | None] = mapped_column(sa.Text)
    description: Mapped[str | None] = mapped_column(sa.Text)
    counterparty_person_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("people.id", ondelete="SET NULL")
    )
    occurred_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )
    source_interaction_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("interactions.id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime] = created_at_column()

    counterparty: Mapped[Person | None] = relationship(lazy="raise")

    __table_args__ = (
        sa.CheckConstraint("amount > 0", name="ck_transactions_amount_positive"),
        sa.Index("ix_transactions_occurred_type", "occurred_at", "type"),
        sa.Index("ix_transactions_category", "category"),
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(sa.Text, nullable=False)
    start_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    location: Mapped[str | None] = mapped_column(sa.Text)
    attendees: Mapped[list[Any] | None] = mapped_column(JSONB)
    description: Mapped[str | None] = mapped_column(sa.Text)
    source: Mapped[e.EventSource] = mapped_column(
        EVENT_SOURCE, nullable=False, server_default=e.EventSource.manual.value
    )
    gcal_event_id: Mapped[str | None] = mapped_column(sa.Text, unique=True)
    status: Mapped[e.EventStatus] = mapped_column(
        EVENT_STATUS, nullable=False, server_default=e.EventStatus.planned.value
    )
    source_interaction_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("interactions.id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (sa.Index("ix_events_start_at", "start_at"),)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    description: Mapped[str] = mapped_column(sa.Text, nullable=False)
    due_date: Mapped[date | None] = mapped_column(sa.Date)
    priority: Mapped[e.TaskPriority] = mapped_column(
        TASK_PRIORITY, nullable=False, server_default=e.TaskPriority.med.value
    )
    status: Mapped[e.TaskStatus] = mapped_column(
        TASK_STATUS, nullable=False, server_default=e.TaskStatus.todo.value
    )
    source_interaction_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("interactions.id", ondelete="CASCADE")
    )
    related_promise_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("promises.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = created_at_column()
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (sa.Index("ix_tasks_status_due", "status", "due_date"),)


class Memory(Base):
    """RAG store: durable facts + interaction summaries, embedded with bge-m3."""

    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(primary_key=True)
    content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.embed_dim))
    source_interaction_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("interactions.id", ondelete="CASCADE")
    )
    occurred_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(sa.Text), nullable=False, server_default="{}"
    )
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        sa.Index("ix_memories_occurred_at", "occurred_at"),
        sa.Index("ix_memories_tags", "tags", postgresql_using="gin"),
        sa.Index(
            "ix_memories_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class DailyReport(Base):
    __tablename__ = "daily_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_date: Mapped[date] = mapped_column(sa.Date, unique=True, nullable=False)
    content: Mapped[str] = mapped_column(sa.Text, nullable=False)
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = created_at_column()


class UsageLog(Base):
    """Per-call cost accounting for Anthropic and ElevenLabs (spec §9)."""

    __tablename__ = "usage_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(sa.Text, nullable=False)  # anthropic|elevenlabs
    model: Mapped[str | None] = mapped_column(sa.Text)
    operation: Mapped[str | None] = mapped_column(sa.Text)  # extract|report|rag|...
    input_tokens: Mapped[int | None] = mapped_column(sa.Integer)
    output_tokens: Mapped[int | None] = mapped_column(sa.Integer)
    cache_read_tokens: Mapped[int | None] = mapped_column(sa.Integer)
    cache_write_tokens: Mapped[int | None] = mapped_column(sa.Integer)
    audio_seconds: Mapped[Decimal | None] = mapped_column(sa.Numeric(10, 2))
    batch: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )
    cost_usd: Mapped[Decimal | None] = mapped_column(sa.Numeric(12, 6))
    source_interaction_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("interactions.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (sa.Index("ix_usage_log_created_at", "created_at"),)


class ReminderLog(Base):
    """One row per reminder actually sent, so nothing is pinged twice in a day."""

    __tablename__ = "reminder_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)  # debt|promise|task|event
    # Stable key for the thing reminded about: a row id, or "person:currency"
    # for a debt balance that spans several rows.
    ref: Mapped[str] = mapped_column(sa.Text, nullable=False)
    sent_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        sa.Index("ix_reminder_log_kind_ref_sent", "kind", "ref", "sent_at"),
    )


__all__ = [
    "ChatMonitor",
    "DailyReport",
    "Debt",
    "DebtPayment",
    "Event",
    "Interaction",
    "Memory",
    "Person",
    "Promise",
    "ReminderLog",
    "Task",
    "Transaction",
    "UsageLog",
]

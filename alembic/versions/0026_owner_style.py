"""The owner's style (JV-14): profile, few-shot examples and per-person tone.

Three new tables and three new columns:

* ``owner_style_profiles`` — one row per built profile version: the
  deterministic traits, the digit-free prompt prose, the owner's guarded
  stall overrides; a new profile is a ``draft`` until the owner activates
  it, and at most one is ``active`` (``ux_owner_style_profiles_active``).
* ``owner_style_examples`` — context-reply pairs of a profile. The text is
  never copied: both messages are joined by id, so ``/unut`` and edits
  apply, and purging either message takes the example with it.
* ``owner_style_people`` — how the owner writes to one person: honorific,
  siz/sen, script, language and greeting.
* ``chat_monitors.style_import_requested_at`` / ``style_import_done_at`` /
  ``style_import_attempts`` — the history import request on the chat's row,
  the same shape as the backfill request.

State columns are ``String(N)`` with named CHECKs; no native enum label is
added. The CHECK value lists are frozen here; tests/test_style.py keeps them
equal to the model's.

Revision ID: 0026_owner_style
Revises: 0025_jarvis_core
Create Date: JV-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0026_owner_style"
down_revision: str | None = "0025_jarvis_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBED_DIM = 1024
PROFILE_STATES = ("draft", "active", "retired")
EXAMPLE_SOURCES = ("history", "owner_edit")
ADDRESS_FORMS = ("siz", "sen", "unknown")
PERSON_SCRIPTS = ("latin", "cyrillic", "mixed")
PERSON_LANGS = ("uz", "ru", "mix")


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )


def _updated_at() -> sa.Column:
    return sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )


def _jsonb(name: str, default: str) -> sa.Column:
    return sa.Column(
        name,
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default=sa.text(f"'{default}'::jsonb"),
    )


def upgrade() -> None:
    op.create_table(
        "owner_style_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        _jsonb("traits", "{}"),
        sa.Column("prompt_text", sa.Text(), nullable=False),
        _jsonb("stalls", "{}"),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True)),
        sa.Column("window_end", sa.DateTime(timezone=True)),
        sa.Column("input_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("approved_by", sa.String(length=16)),
        _jsonb("history", "[]"),
        _created_at(),
        _updated_at(),
        sa.UniqueConstraint("version", name="uq_owner_style_profiles_version"),
        sa.CheckConstraint(
            f"state IN ({_sql_list(PROFILE_STATES)})",
            name="ck_owner_style_profiles_state",
        ),
        sa.CheckConstraint(
            "sample_count >= 0", name="ck_owner_style_profiles_sample_count"
        ),
    )
    op.create_index(
        "ux_owner_style_profiles_active",
        "owner_style_profiles",
        [sa.text("(true)")],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
    )

    op.create_table(
        "owner_style_examples",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "profile_id",
            sa.Integer(),
            sa.ForeignKey("owner_style_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "interaction_id",
            sa.Integer(),
            sa.ForeignKey("interactions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "context_interaction_id",
            sa.Integer(),
            sa.ForeignKey("interactions.id", ondelete="CASCADE"),
        ),
        sa.Column("intent", sa.String(length=24)),
        sa.Column("script", sa.String(length=16)),
        sa.Column(
            "source", sa.String(length=16), nullable=False, server_default="history"
        ),
        sa.Column("embedding", Vector(EMBED_DIM)),
        _created_at(),
        _updated_at(),
        sa.UniqueConstraint(
            "profile_id", "interaction_id", name="uq_owner_style_examples_interaction"
        ),
        sa.CheckConstraint(
            f"source IN ({_sql_list(EXAMPLE_SOURCES)})",
            name="ck_owner_style_examples_source",
        ),
    )
    op.create_index(
        "ix_owner_style_examples_interaction",
        "owner_style_examples",
        ["interaction_id"],
    )
    op.create_index(
        "ix_owner_style_examples_context",
        "owner_style_examples",
        ["context_interaction_id"],
    )
    op.create_index(
        "ix_owner_style_examples_embedding_hnsw",
        "owner_style_examples",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    op.create_table(
        "owner_style_people",
        sa.Column(
            "person_id",
            sa.Integer(),
            sa.ForeignKey("people.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("honorific", sa.String(length=16)),
        sa.Column(
            "address_form",
            sa.String(length=8),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("script", sa.String(length=16)),
        sa.Column("lang", sa.String(length=4)),
        sa.Column("greeting", sa.Text()),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint(
            f"address_form IN ({_sql_list(ADDRESS_FORMS)})",
            name="ck_owner_style_people_address_form",
        ),
        sa.CheckConstraint(
            f"script IN ({_sql_list(PERSON_SCRIPTS)})",
            name="ck_owner_style_people_script",
        ),
        sa.CheckConstraint(
            f"lang IN ({_sql_list(PERSON_LANGS)})", name="ck_owner_style_people_lang"
        ),
    )

    op.add_column(
        "chat_monitors",
        sa.Column("style_import_requested_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "chat_monitors",
        sa.Column("style_import_done_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "chat_monitors",
        sa.Column(
            "style_import_attempts",
            sa.SmallInteger(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("chat_monitors", "style_import_attempts")
    op.drop_column("chat_monitors", "style_import_done_at")
    op.drop_column("chat_monitors", "style_import_requested_at")
    op.drop_table("owner_style_people")
    op.drop_table("owner_style_examples")
    op.drop_index("ux_owner_style_profiles_active", table_name="owner_style_profiles")
    op.drop_table("owner_style_profiles")

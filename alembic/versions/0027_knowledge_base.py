"""The business knowledge base (JV-15): facts and answers the owner approved.

One new table, ``knowledge_entries``: a ``fact`` («Ofisimiz Toshkentda
joylashgan») or a ``faq`` (a question and its answer). An entry the owner
typed in (``/bilim qo'sh``) is ``active`` at once; one MIYA mined from the
owner's own past answers is a ``suggested`` row until the owner taps
«✅ Qo'shish»; a retired one is never read again. ``guard`` keeps the
red-line verdict in the ``kb`` role the text passed when it was stored (no
price, discount, payment term, payment confirmation, deadline, promise or
guarantee). The search columns are copied from ``memories``: an embedding
with its HNSW index and the partial index of rows still waiting for one,
``search_norm`` with the computed ``search_tsv`` (GIN) and a trigram GIN.

State columns are ``String(N)`` with named CHECKs; no native enum label is
added. The CHECK value lists are frozen here; tests/test_kb.py keeps them
equal to the model's.

Revision ID: 0027_knowledge_base
Revises: 0026_owner_style
Create Date: JV-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0027_knowledge_base"
down_revision: str | None = "0026_owner_style"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBED_DIM = 1024
KINDS = ("fact", "faq")
STATES = ("suggested", "active", "retired")
SOURCES = ("owner", "suggested", "import")


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _timestamp(name: str) -> sa.Column:
    return sa.Column(
        name,
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
        "knowledge_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("title", sa.Text()),
        sa.Column("question", sa.Text()),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("lang", sa.String(length=4)),
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "source_interaction_id",
            sa.Integer(),
            sa.ForeignKey("interactions.id", ondelete="CASCADE"),
        ),
        _jsonb("guard", "{}"),
        sa.Column("embedding", Vector(EMBED_DIM)),
        sa.Column("search_norm", sa.Text()),
        sa.Column(
            "search_tsv",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('simple'::regconfig, coalesce(search_norm, ''))",
                persisted=True,
            ),
        ),
        _jsonb("history", "[]"),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.CheckConstraint(
            f"kind IN ({_sql_list(KINDS)})", name="ck_knowledge_entries_kind"
        ),
        sa.CheckConstraint(
            f"state IN ({_sql_list(STATES)})", name="ck_knowledge_entries_state"
        ),
        sa.CheckConstraint(
            f"source IN ({_sql_list(SOURCES)})", name="ck_knowledge_entries_source"
        ),
        sa.CheckConstraint("btrim(answer) <> ''", name="ck_knowledge_entries_answer"),
        sa.CheckConstraint(
            "kind <> 'faq' OR question IS NOT NULL",
            name="ck_knowledge_entries_faq_question",
        ),
    )
    op.create_index(
        "ix_knowledge_entries_source_interaction",
        "knowledge_entries",
        ["source_interaction_id"],
    )
    op.create_index(
        "ix_knowledge_entries_tags",
        "knowledge_entries",
        ["tags"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_knowledge_entries_tsv",
        "knowledge_entries",
        ["search_tsv"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_knowledge_entries_trgm",
        "knowledge_entries",
        ["search_norm"],
        postgresql_using="gin",
        postgresql_ops={"search_norm": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_knowledge_entries_embedding_hnsw",
        "knowledge_entries",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index(
        "ix_knowledge_entries_unembedded",
        "knowledge_entries",
        ["id"],
        postgresql_where=sa.text("embedding IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("knowledge_entries")

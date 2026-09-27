"""Passages (WP-55): every stored message, transcript, document and note as
searchable text — full-text, trigram and vector — with who said it where.

Revision ID: 0021_search_passages
Revises: 0020_recap_digests
Create Date: WP-55
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0021_search_passages"
down_revision: str | None = "0020_recap_digests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBED_DIM = 1024


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    source = postgresql.ENUM(name="interaction_source", create_type=False)
    op.create_table(
        "passages",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "interaction_id",
            sa.Integer(),
            sa.ForeignKey("interactions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_no", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("source", source, nullable=False),
        sa.Column("tg_chat_id", sa.BigInteger()),
        sa.Column(
            "chat_person_id",
            sa.Integer(),
            sa.ForeignKey("people.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "speaker_person_id",
            sa.Integer(),
            sa.ForeignKey("people.id", ondelete="SET NULL"),
        ),
        sa.Column("from_owner", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("media_kind", sa.Text()),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("embed_text", sa.Text(), nullable=False),
        sa.Column("search_norm", sa.Text(), nullable=False),
        sa.Column(
            "search_tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple'::regconfig, search_norm)", persisted=True),
        ),
        sa.Column("embedding", Vector(EMBED_DIM)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("interaction_id", "chunk_no", name="uq_passages_chunk"),
    )
    op.create_index(
        "ix_passages_tsv", "passages", ["search_tsv"], postgresql_using="gin"
    )
    op.create_index(
        "ix_passages_trgm",
        "passages",
        ["search_norm"],
        postgresql_using="gin",
        postgresql_ops={"search_norm": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_passages_embedding_hnsw",
        "passages",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index(
        "ix_passages_speaker_occurred",
        "passages",
        ["speaker_person_id", sa.text("occurred_at DESC")],
    )
    op.create_index(
        "ix_passages_chat_person_occurred",
        "passages",
        ["chat_person_id", sa.text("occurred_at DESC")],
    )
    op.create_index(
        "ix_passages_chat_occurred", "passages", ["tg_chat_id", sa.text("occurred_at DESC")]
    )
    op.create_index("ix_passages_occurred", "passages", [sa.text("occurred_at DESC")])
    op.create_index(
        "ix_passages_unembedded",
        "passages",
        ["id"],
        postgresql_where=sa.text("embedding IS NULL"),
    )
    op.add_column(
        "interactions", sa.Column("search_indexed_at", sa.DateTime(timezone=True))
    )
    op.create_index(
        "ix_interactions_unindexed",
        "interactions",
        ["id"],
        postgresql_where=sa.text("search_indexed_at IS NULL"),
    )
    op.add_column("memories", sa.Column("search_norm", sa.Text()))
    op.add_column(
        "memories",
        sa.Column(
            "search_tsv",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('simple'::regconfig, coalesce(search_norm, ''))",
                persisted=True,
            ),
        ),
    )
    op.create_index("ix_memories_tsv", "memories", ["search_tsv"], postgresql_using="gin")
    op.create_index(
        "ix_memories_trgm",
        "memories",
        ["search_norm"],
        postgresql_using="gin",
        postgresql_ops={"search_norm": "gin_trgm_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_memories_trgm", table_name="memories")
    op.drop_index("ix_memories_tsv", table_name="memories")
    op.drop_column("memories", "search_tsv")
    op.drop_column("memories", "search_norm")
    op.drop_index("ix_interactions_unindexed", table_name="interactions")
    op.drop_column("interactions", "search_indexed_at")
    op.drop_table("passages")

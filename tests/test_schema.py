"""Schema-level checks that need no database connection."""

from __future__ import annotations

import sqlalchemy as sa

from miya.db import models as m
from miya.db.base import Base


def test_mappers_configure():
    """Catches broken relationship() targets and unresolvable annotations."""
    sa.orm.configure_mappers()


def test_all_spec_tables_present():
    expected = {
        "people",
        "chat_monitors",
        "interactions",
        "debts",
        "debt_payments",
        "promises",
        "transactions",
        "transaction_evidence",
        "events",
        "tasks",
        "memories",
        "daily_reports",
        "usage_log",
    }
    assert expected <= set(Base.metadata.tables)


def test_money_columns_are_numeric_14_2():
    for table, column in (
        (m.Debt, "amount"),
        (m.DebtPayment, "amount"),
        (m.Transaction, "amount"),
    ):
        col = table.__table__.c[column]
        assert isinstance(col.type, sa.Numeric), f"{table.__name__}.{column}"
        assert (col.type.precision, col.type.scale) == (14, 2)


def test_every_timestamp_is_timezone_aware():
    for table in Base.metadata.tables.values():
        for col in table.c:
            if isinstance(col.type, sa.DateTime):
                assert col.type.timezone, f"{table.name}.{col.name} is naive"


def test_direction_enum_stores_in_not_in_underscore():
    # Direction.in_ must land in the database as the label "in".
    labels = m.DIRECTION.enums
    assert labels == ["in", "out", "na"]


def test_derived_rows_cascade_from_their_interaction():
    """Purging an interaction must remove everything extracted from it (spec §10)."""
    for table in (m.Debt, m.Promise, m.Transaction, m.Event, m.Task, m.Memory):
        fks = list(table.__table__.c["source_interaction_id"].foreign_keys)
        assert fks, table.__name__
        assert fks[0].ondelete == "CASCADE", table.__name__
    # Evidence goes with its transaction and with its interaction.
    for column in ("transaction_id", "interaction_id"):
        [fk] = m.TransactionEvidence.__table__.c[column].foreign_keys
        assert fk.ondelete == "CASCADE", column


def test_transaction_history_defaults_to_an_empty_list():
    default = m.Transaction.__table__.c["history"].server_default
    assert "'[]'" in str(default.arg)


def test_memories_embedding_matches_configured_dim():
    from miya.config import settings

    assert m.Memory.__table__.c["embedding"].type.dim == settings.embed_dim


# --- WP-23: the indexes a delete or a person lookup needs -----------------------

NEW_INDEXES = {
    "ix_debts_person",
    "ix_memories_unembedded",
    "ix_conversation_windows_person",
    "ix_interactions_window",
    "ix_tasks_related_promise",
    "ix_claims_person",
    *(
        f"ix_{t}_source_interaction"
        for t in (
            "debts",
            "promises",
            "transactions",
            "events",
            "tasks",
            "memories",
            "usage_log",
        )
    ),
}


async def test_the_ops_indexes_exist(session):
    names = set(await session.scalars(sa.text("SELECT indexname FROM pg_indexes")))
    assert names >= NEW_INDEXES


async def test_every_cascading_foreign_key_leads_an_index(session):
    """Section 2.4: a foreign key that cascades or sets null is the first
    column of some index on its table, or every parent delete scans it."""
    rows = await session.execute(
        sa.text(
            """
            SELECT c.conrelid::regclass::text, a.attname
              FROM pg_constraint c
              JOIN pg_attribute a
                ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
             WHERE c.contype = 'f' AND c.confdeltype IN ('c', 'n')
               AND NOT EXISTS (
                     SELECT 1 FROM pg_index i
                      WHERE i.indrelid = c.conrelid AND i.indkey[0] = c.conkey[1]
                   )
            """
        )
    )
    assert rows.all() == []


async def _plan(session, query: str) -> str:
    await session.execute(sa.text("SET LOCAL enable_seqscan = off"))
    rows = await session.execute(sa.text(f"EXPLAIN {query}"))
    return "\n".join(r[0] for r in rows.all())


async def test_person_and_unembedded_lookups_use_their_indexes(session):
    assert "ix_debts_person" in await _plan(
        session, "SELECT 1 FROM debts WHERE person_id = 1"
    )
    assert "ix_memories_unembedded" in await _plan(
        session, "SELECT id FROM memories WHERE embedding IS NULL ORDER BY id LIMIT 128"
    )

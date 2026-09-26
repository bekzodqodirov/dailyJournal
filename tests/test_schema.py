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

"""The money ledger (WP-11): void, history, card, channel and evidence for
transactions, and reinstall-safe content keys for phone events.

* ``transactions`` gains ``voided_at``/``void_reason`` (a correction never
  deletes a row), ``history``, ``card_last4``, ``channel`` and
  ``is_internal``; every money total filters on queries.ACTIVE_TXN.
* ``transaction_evidence``: every text that reported one payment, so the
  same payment seen through two channels is one transaction.
* ``ux_interactions_content_key``: a reinstall of the companion re-mints the
  device id and re-uploads the inbox; the content key (the event itself, not
  the device) makes that re-harvest a list of duplicates. Existing event rows
  are backfilled; a reinstall that already happened is found, and its
  duplicate transactions are voided.
* the enum ``interaction_source`` gains ``phone_notification``.

Data steps compare enum columns as text: PostgreSQL refuses a label added by
ADD VALUE earlier in the same transaction, and a fresh install runs every
migration in one.

Revision ID: 0013_money_ledger
Revises: 0012_phone_events
Create Date: WP-11
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0013_money_ledger"
down_revision: str | None = "0012_phone_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# --- frozen copies of the app's key functions ----------------------------------
#
# Never import app code in a migration: the app moves on, this file must not.
# tests/test_migration_0013_money.py keeps these equal to phone_events and
# sms_money as they are at 0013.


def _frozen_normalise_sender(sender: str) -> str:
    return "".join(ch for ch in (sender or "").casefold() if ch.isalnum())


def _frozen_epoch(moment: datetime | str) -> int:
    if isinstance(moment, str):
        moment = datetime.fromisoformat(moment)
    return int(moment.timestamp())


def _frozen_sms_content_key(sender: str, received_at: datetime | str, body: str) -> str:
    material = (
        f"v1|{_frozen_normalise_sender(sender)}|{_frozen_epoch(received_at)}|"
        f"{' '.join((body or '').split())}"
    )
    return "smsc:" + hashlib.sha256(material.encode()).hexdigest()[:32]


def _frozen_call_content_key(
    number: str | None, started_at: datetime | str, duration_seconds, call_type: str
) -> str:
    digits = re.sub(r"\D", "", number or "")[-9:]
    material = (
        f"v1|{digits}|{_frozen_epoch(started_at)}|{duration_seconds}|{call_type}"
    )
    return "callc:" + hashlib.sha256(material.encode()).hexdigest()[:32]


# --- backfills -----------------------------------------------------------------


def backfill_content_keys(bind) -> dict[str, int]:
    """Give every phone-event row its content key. The first row per key
    keeps it; a later one (a reinstall already happened) is marked
    ``content_key_duplicate_of`` and its transactions are voided."""
    rows = bind.execute(
        sa.text(
            "SELECT id, source::text AS source, media, raw_text, occurred_at "
            "FROM interactions "
            "WHERE source::text IN ('phone_sms', 'phone_call') AND media ? 'event_key' "
            "ORDER BY id"
        )
    ).all()
    now = datetime.now(UTC)
    first_of: dict[str, int] = {}
    keyed = duplicates = voided = 0
    for row in rows:
        media = row.media or {}
        if media.get("content_key"):
            first_of.setdefault(media["content_key"], row.id)
            continue
        if row.source == "phone_sms":
            key = _frozen_sms_content_key(
                media.get("sender") or "", row.occurred_at, row.raw_text or ""
            )
        else:
            key = _frozen_call_content_key(
                media.get("phone"),
                row.occurred_at,
                media.get("duration_seconds"),
                media.get("call_type"),
            )
        first = first_of.get(key)
        if first is None:
            first_of[key] = row.id
            bind.execute(
                sa.text(
                    "UPDATE interactions SET media = media || "
                    "jsonb_build_object('content_key', CAST(:key AS text)) WHERE id = :id"
                ),
                {"key": key, "id": row.id},
            )
            keyed += 1
            continue
        bind.execute(
            sa.text(
                "UPDATE interactions SET media = media || "
                "jsonb_build_object('content_key_duplicate_of', CAST(:first AS integer)) "
                "WHERE id = :id"
            ),
            {"first": first, "id": row.id},
        )
        duplicates += 1
        entry = {
            "at": now.isoformat(),
            "field": "status",
            "old": "active",
            "new": "void",
            "by": "migration",
            "reason": "reinstall_duplicate",
        }
        result = bind.execute(
            sa.text(
                "UPDATE transactions SET voided_at = :now, "
                "void_reason = 'reinstall_duplicate', "
                "history = history || CAST(:entry AS jsonb) "
                "WHERE source_interaction_id = :id AND voided_at IS NULL"
            ),
            {"now": now, "entry": json.dumps([entry]), "id": row.id},
        )
        voided += result.rowcount or 0
    counts = {"keyed": keyed, "duplicates": duplicates, "voided": voided}
    print(f"0013 content keys: {counts}")
    return counts


def _card(value) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[0-9]{4}", value) else None


def backfill_evidence(bind) -> int:
    """One evidence row per existing SMS transaction, and its channel and
    card copied onto the transaction. In Python, not SQL: ``[:alnum:]``
    follows the collation's ctype and need not match normalise_sender."""
    rows = bind.execute(
        sa.text(
            "SELECT t.id AS txn_id, i.id AS interaction_id, i.media "
            "FROM transactions t JOIN interactions i ON i.id = t.source_interaction_id "
            "WHERE i.source::text = 'phone_sms' ORDER BY t.id"
        )
    ).all()
    added = 0
    for row in rows:
        media = row.media or {}
        channel = "sms:" + _frozen_normalise_sender(media.get("sender") or "")
        card = _card((media.get("payment") or {}).get("card_last4"))
        result = bind.execute(
            sa.text(
                "INSERT INTO transaction_evidence "
                "(transaction_id, interaction_id, channel, card_last4) "
                "VALUES (:txn, :interaction, :channel, :card) ON CONFLICT DO NOTHING"
            ),
            {
                "txn": row.txn_id,
                "interaction": row.interaction_id,
                "channel": channel,
                "card": card,
            },
        )
        added += result.rowcount or 0
        bind.execute(
            sa.text(
                "UPDATE transactions SET channel = COALESCE(channel, :channel), "
                "card_last4 = COALESCE(card_last4, :card) WHERE id = :txn"
            ),
            {"channel": channel, "card": card, "txn": row.txn_id},
        )
    print(f"0013 evidence rows: {added}")
    return added


# --- schema --------------------------------------------------------------------


def upgrade() -> None:
    # (a) Nothing below uses the label (see 0012 for the rule).
    op.execute("ALTER TYPE interaction_source ADD VALUE IF NOT EXISTS 'phone_notification'")

    # (b) The ledger columns.
    op.add_column("transactions", sa.Column("voided_at", sa.DateTime(timezone=True)))
    op.add_column("transactions", sa.Column("void_reason", sa.Text))
    op.add_column(
        "transactions",
        sa.Column(
            "history", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
    )
    op.add_column("transactions", sa.Column("card_last4", sa.String(4)))
    op.add_column("transactions", sa.Column("channel", sa.Text))
    op.add_column(
        "transactions",
        sa.Column("is_internal", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.create_check_constraint(
        "ck_transactions_card_last4",
        "transactions",
        "card_last4 IS NULL OR card_last4 ~ '^[0-9]{4}$'",
    )

    # (c) Evidence.
    op.create_table(
        "transaction_evidence",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "transaction_id",
            sa.Integer,
            sa.ForeignKey("transactions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "interaction_id",
            sa.Integer,
            sa.ForeignKey("interactions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel", sa.Text, nullable=False),
        sa.Column("card_last4", sa.String(4)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("interaction_id", name="ux_transaction_evidence_interaction"),
        sa.UniqueConstraint(
            "transaction_id", "channel", name="ux_transaction_evidence_txn_channel"
        ),
    )

    # (d) Indexes.
    op.create_index(
        "ix_transactions_money_match",
        "transactions",
        ["currency", "type", "amount", "occurred_at"],
    )
    op.create_index(
        "ix_transactions_counterparty", "transactions", ["counterparty_person_id"]
    )
    op.create_index(
        "ix_interactions_money_notice_pending",
        "interactions",
        ["occurred_at"],
        postgresql_where=sa.text(
            "(metadata ? 'money_notice') AND NOT (metadata ? 'money_notified')"
        ),
    )

    # (e), (f) Backfills.
    bind = op.get_bind()
    backfill_content_keys(bind)
    backfill_evidence(bind)

    # (g) Only now: the backfill left one key per content.
    op.create_index(
        "ux_interactions_content_key",
        "interactions",
        [sa.text("(media ->> 'content_key')")],
        unique=True,
        postgresql_where=sa.text("media ->> 'content_key' IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ux_interactions_content_key", table_name="interactions")
    op.drop_index("ix_interactions_money_notice_pending", table_name="interactions")
    op.drop_index("ix_transactions_counterparty", table_name="transactions")
    op.drop_index("ix_transactions_money_match", table_name="transactions")
    op.drop_table("transaction_evidence")
    op.drop_constraint("ck_transactions_card_last4", "transactions", type_="check")
    for column in (
        "is_internal",
        "channel",
        "card_last4",
        "history",
        "void_reason",
        "voided_at",
    ):
        op.drop_column("transactions", column)
    # The 'phone_notification' label stays, for the same reason as in 0012.

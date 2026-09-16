"""Phone events (build step 6): the enum label for SMS and the event-key dedupe.

The Android companion starts uploading call-log entries and SMS as metadata
batches. Two schema changes carry all of it:

* the ``interaction_source`` enum gains the label ``phone_sms`` — call-log
  events reuse ``phone_call``;
* a partial unique expression index on ``media ->> 'event_key'`` makes the
  ingest idempotent: a retried batch inserts nothing twice, whatever the
  pre-insert check missed under concurrency.

Revision ID: 0012_phone_events
Revises: 0011_heartbeats
Create Date: Build step 6
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012_phone_events"
down_revision: str | None = "0011_heartbeats"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # On PostgreSQL 16 ADD VALUE works inside alembic's transaction, with one
    # rule: no statement in the same transaction may use the new label. This
    # migration honours that — nothing below inserts, compares or casts
    # 'phone_sms'; the first use is application code after the upgrade.
    op.execute("ALTER TYPE interaction_source ADD VALUE IF NOT EXISTS 'phone_sms'")

    # One row per phone event, ever: the key is minted on the phone from the
    # device id and the provider row (plus a content hash for SMS), so a
    # retried upload lands on this index instead of a second interaction.
    # Partial, because only event rows carry the key (same shape as
    # ix_interactions_media_sha256 in 0003).
    op.create_index(
        "ux_interactions_event_key",
        "interactions",
        [sa.text("(media ->> 'event_key')")],
        unique=True,
        postgresql_where=sa.text("media ->> 'event_key' IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ux_interactions_event_key", table_name="interactions")
    # The 'phone_sms' enum label stays: PostgreSQL cannot remove a label from
    # an enum type, and pretending otherwise (recreating the type) would
    # rewrite every row of interactions for nothing. The label is inert while
    # no code writes it.

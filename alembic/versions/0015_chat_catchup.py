"""Never lose a message from an allowed chat (WP-21).

* ``chat_monitors.monitoring_since`` — the moment a chat was switched on;
  the catch-up never reads before it.
* ``chat_monitors.last_seen_message_id`` — the newest Telegram message id
  the userbot saw in that chat; after downtime it reads from here on.
* ``ux_interactions_tg_message`` — one row per Telegram message, enforced by
  the database: the catch-up sweep and the live handler can ingest the same
  message at the same moment, and a read-then-insert check cannot stop the
  second copy. Existing duplicates keep their rows and text (only /unut
  deletes text); the later copies' key is renamed.

Revision ID: 0015_chat_catchup
Revises: 0014_question_budget
Create Date: WP-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015_chat_catchup"
down_revision: str | None = "0014_question_budget"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

USERBOT_ROWS = "source::text = 'telegram_userbot' AND metadata ? 'tg_message_id'"

RENAME_DUPLICATES = f"""
WITH ranked AS (
  SELECT id,
         row_number() OVER (
           PARTITION BY tg_chat_id, (metadata->>'tg_message_id')
           ORDER BY id
         ) AS n
    FROM interactions
   WHERE {USERBOT_ROWS}
)
UPDATE interactions i
   SET metadata = (i.metadata - 'tg_message_id')
                  || jsonb_build_object('tg_message_id_duplicate',
                                        i.metadata->'tg_message_id')
  FROM ranked
 WHERE ranked.id = i.id AND ranked.n > 1
"""

BACKFILL_MONITORS = f"""
UPDATE chat_monitors c
   SET monitoring_since = COALESCE(
         (SELECT min(occurred_at) FROM interactions i
           WHERE i.tg_chat_id = c.tg_chat_id AND i.source::text = 'telegram_userbot'),
         now())
 WHERE c.monitor_enabled;
UPDATE chat_monitors c
   SET last_seen_message_id = s.top
  FROM (SELECT tg_chat_id, max((metadata->>'tg_message_id')::bigint) AS top
          FROM interactions
         WHERE {USERBOT_ROWS}
         GROUP BY tg_chat_id) s
 WHERE s.tg_chat_id = c.tg_chat_id
"""


def upgrade() -> None:
    op.add_column(
        "chat_monitors", sa.Column("monitoring_since", sa.DateTime(timezone=True))
    )
    op.add_column("chat_monitors", sa.Column("last_seen_message_id", sa.BigInteger))
    op.execute(RENAME_DUPLICATES)
    for statement in BACKFILL_MONITORS.split(";"):
        if statement.strip():
            op.execute(statement)
    op.create_index(
        "ux_interactions_tg_message",
        "interactions",
        ["tg_chat_id", sa.text("((metadata ->> 'tg_message_id')::bigint)")],
        unique=True,
        # Not ``source::text``: an index predicate must be immutable, and the
        # enum-to-text cast is not. 'telegram_userbot' exists since 0001.
        postgresql_where=sa.text(
            "source = 'telegram_userbot' AND metadata ? 'tg_message_id'"
        ),
    )


def downgrade() -> None:
    op.drop_index("ux_interactions_tg_message", table_name="interactions")
    op.drop_column("chat_monitors", "last_seen_message_id")
    op.drop_column("chat_monitors", "monitoring_since")

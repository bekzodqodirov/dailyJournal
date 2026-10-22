"""The Jarvis core (JV-11): the outbound chokepoint and its bookkeeping.

Five new tables:

* ``jarvis_policy`` — the rollout mode, pauses and breakers per scope
  (``global``, ``channel:<c>``, ``intent:<i>``, ``channel:<c>:intent:<i>``);
  the data step seeds ``('global', 'shadow')``.
* ``reply_rules`` — who the AI may write to: the owner's personal deny-list
  and the group reply policy, with channel-qualified address snapshots.
* ``jarvis_chat_state`` — per chat: the last client message, when the
  responder may look, and what the owner did since.
* ``escalations`` — client messages only the owner may answer, one open row
  per chat.
* ``outbox_messages`` — every message MIYA may send from the owner's
  account. Two CHECKs carry the safety contract: a sent row was approved, and
  AI-authored text is approved, claimed or sent only with a passing guard.

State columns are ``String(N)`` with named CHECKs; no native enum label is
added. The CHECK value lists are frozen here; tests/test_outbox.py keeps them
equal to the model's.

Revision ID: 0025_jarvis_core
Revises: 0024_sheet_rows
Create Date: JV-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0025_jarvis_core"
down_revision: str | None = "0024_sheet_rows"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHANNELS = ("telegram", "whatsapp", "gmail")
MODES = ("off", "shadow", "draft", "auto")
SCOPE_PATTERN = "^(global|channel:[a-z]+|intent:[a-z_]+|channel:[a-z]+:intent:[a-z_]+)$"
RULE_SUBJECTS = ("person", "chat", "address")
RULE_MODES = ("never", "draft", "auto")
RULE_REASONS = ("personal", "group", "spam", "bot", "owner")
RULE_STATES = ("active", "proposed", "revoked")
ESCALATION_REASONS = (
    "price",
    "discount",
    "payment_terms",
    "deal",
    "deadline",
    "promise",
    "guarantee",
    "payment_claim",
    "identity",
    "complaint",
    "legal",
    "other_people",
    "other",
)
ESCALATION_STATES = (
    "open",
    "snoozed",
    "owner_replying",
    "resolved",
    "expired",
    "cancelled",
)
ESCALATION_OPEN_STATES = ("open", "snoozed", "owner_replying")
ESCALATION_RESOLUTIONS = ("sent", "owner_replied", "expired", "cancelled")
ORIGINS = ("auto_reply", "stall", "owner_command", "escalation")
AUTHORS = ("ai", "owner")
OUTBOX_STATES = (
    "shadow",
    "draft",
    "approved",
    "sending",
    "sent",
    "failed",
    "unknown",
    "cancelled",
    "expired",
    "blocked",
)
APPROVERS = ("button", "reply", "command", "auto:policy")


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _stamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    ]


def _history() -> sa.Column:
    return sa.Column(
        "history",
        postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default=sa.text("'[]'::jsonb"),
    )


def _ts(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True))


def _fk(name: str, target: str, ondelete: str, *, nullable: bool = True) -> sa.Column:
    return sa.Column(
        name,
        sa.Integer(),
        sa.ForeignKey(target, ondelete=ondelete),
        nullable=nullable,
    )


def upgrade() -> None:
    op.create_table(
        "jarvis_policy",
        sa.Column("scope", sa.Text(), primary_key=True),
        sa.Column("mode", sa.String(length=8)),
        _ts("paused_until"),
        sa.Column("pause_reason", sa.String(length=32)),
        _ts("breaker_until"),
        sa.Column("breaker_reason", sa.String(length=32)),
        _history(),
        *_stamps(),
        sa.CheckConstraint(f"mode IN ({_sql_list(MODES)})", name="ck_jarvis_policy_mode"),
        sa.CheckConstraint(f"scope ~ '{SCOPE_PATTERN}'", name="ck_jarvis_policy_scope"),
    )

    op.create_table(
        "reply_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subject", sa.String(length=16), nullable=False),
        _fk("person_id", "people.id", "SET NULL"),
        sa.Column("chat_key", sa.Text()),
        sa.Column("address", sa.Text()),
        sa.Column("mode", sa.String(length=8), nullable=False),
        sa.Column("reason", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("set_by", sa.String(length=16)),
        sa.Column("note", sa.Text()),
        _history(),
        *_stamps(),
        sa.CheckConstraint(
            f"subject IN ({_sql_list(RULE_SUBJECTS)})", name="ck_reply_rules_subject"
        ),
        sa.CheckConstraint(
            f"mode IN ({_sql_list(RULE_MODES)})", name="ck_reply_rules_mode"
        ),
        sa.CheckConstraint(
            f"reason IN ({_sql_list(RULE_REASONS)})", name="ck_reply_rules_reason"
        ),
        sa.CheckConstraint(
            f"state IN ({_sql_list(RULE_STATES)})", name="ck_reply_rules_state"
        ),
        sa.CheckConstraint(
            "num_nonnulls(person_id, chat_key, address) >= 1",
            name="ck_reply_rules_target",
        ),
    )
    op.create_index("ix_reply_rules_person", "reply_rules", ["person_id"])
    for name, column in (
        ("ux_reply_rules_active_chat", "chat_key"),
        ("ux_reply_rules_active_address", "address"),
        ("ux_reply_rules_active_person", "person_id"),
    ):
        op.create_index(
            name,
            "reply_rules",
            [column],
            unique=True,
            postgresql_where=sa.text("state = 'active'"),
        )

    op.create_table(
        "jarvis_chat_state",
        sa.Column("chat_key", sa.Text(), primary_key=True),
        sa.Column("channel", sa.String(length=16), nullable=False),
        _fk("person_id", "people.id", "SET NULL"),
        _fk("last_inbound_interaction_id", "interactions.id", "SET NULL"),
        _ts("last_inbound_at"),
        _ts("respond_after"),
        _ts("owner_seen_at"),
        _ts("owner_composing_at"),
        _ts("owner_replied_at"),
        _ts("ai_paused_until"),
        _ts("last_ai_sent_at"),
        sa.Column(
            "ai_turns_since_client",
            sa.SmallInteger(),
            nullable=False,
            server_default="0",
        ),
        *_stamps(),
        sa.CheckConstraint(
            f"channel IN ({_sql_list(CHANNELS)})", name="ck_jarvis_chat_state_channel"
        ),
    )
    op.create_index("ix_jarvis_chat_state_person", "jarvis_chat_state", ["person_id"])
    op.create_index(
        "ix_jarvis_chat_state_last_inbound",
        "jarvis_chat_state",
        ["last_inbound_interaction_id"],
    )
    op.create_index(
        "ix_jarvis_chat_state_due",
        "jarvis_chat_state",
        ["respond_after"],
        postgresql_where=sa.text("respond_after IS NOT NULL"),
    )

    op.create_table(
        "escalations",
        sa.Column("id", sa.Integer(), primary_key=True),
        _fk("interaction_id", "interactions.id", "CASCADE", nullable=False),
        _fk("person_id", "people.id", "SET NULL"),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("chat_key", sa.Text(), nullable=False),
        sa.Column(
            "reasons",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("summary", sa.Text()),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="open"),
        _ts("snoozed_until"),
        sa.Column("snooze_count", sa.SmallInteger(), nullable=False, server_default="0"),
        _ts("alerted_at"),
        sa.Column("alert_count", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("alert_tg_message_id", sa.BigInteger()),
        _ts("answered_at"),
        sa.Column("answered_by", sa.String(length=16)),
        sa.Column("resolution", sa.String(length=16)),
        _history(),
        *_stamps(),
        sa.CheckConstraint(
            f"channel IN ({_sql_list(CHANNELS)})", name="ck_escalations_channel"
        ),
        sa.CheckConstraint(
            f"reasons <@ ARRAY[{_sql_list(ESCALATION_REASONS)}]::text[]",
            name="ck_escalations_reasons",
        ),
        sa.CheckConstraint(
            f"state IN ({_sql_list(ESCALATION_STATES)})", name="ck_escalations_state"
        ),
        sa.CheckConstraint(
            f"resolution IN ({_sql_list(ESCALATION_RESOLUTIONS)})",
            name="ck_escalations_resolution",
        ),
    )
    op.create_index("ix_escalations_interaction", "escalations", ["interaction_id"])
    op.create_index("ix_escalations_person", "escalations", ["person_id"])
    op.create_index(
        "ux_escalations_open_chat",
        "escalations",
        ["chat_key"],
        unique=True,
        postgresql_where=sa.text(f"state IN ({_sql_list(ESCALATION_OPEN_STATES)})"),
    )
    op.create_index(
        "ix_escalations_snoozed",
        "escalations",
        ["snoozed_until"],
        postgresql_where=sa.text("state = 'snoozed'"),
    )
    op.create_index(
        "ix_escalations_unalerted",
        "escalations",
        ["created_at"],
        postgresql_where=sa.text("alerted_at IS NULL"),
    )

    op.create_table(
        "outbox_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("chat_key", sa.Text(), nullable=False),
        sa.Column("tg_chat_id", sa.BigInteger()),
        sa.Column("to_address", sa.Text()),
        _fk("person_id", "people.id", "SET NULL"),
        _fk("trigger_interaction_id", "interactions.id", "CASCADE"),
        _fk("command_interaction_id", "interactions.id", "SET NULL"),
        _fk("sent_interaction_id", "interactions.id", "SET NULL"),
        _fk("escalation_id", "escalations.id", "SET NULL"),
        sa.Column("reply_to_external_id", sa.Text()),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("intent", sa.String(length=24)),
        sa.Column("authored_by", sa.String(length=8), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("ai_body", sa.Text()),
        sa.Column("body_sha256", sa.CHAR(length=64), nullable=False),
        sa.Column(
            "guard",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("approved_by", sa.String(length=16)),
        _ts("approved_at"),
        _ts("not_before"),
        _ts("expires_at"),
        sa.Column("card_tg_message_id", sa.BigInteger()),
        _ts("lease_until"),
        sa.Column("claimed_by", sa.String(length=32)),
        sa.Column("attempts", sa.SmallInteger(), nullable=False, server_default="0"),
        _ts("next_attempt_at"),
        sa.Column("last_error", sa.String(length=64)),
        sa.Column("external_message_id", sa.Text()),
        _ts("read_ack_at"),
        _ts("sent_at"),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        _history(),
        *_stamps(),
        sa.CheckConstraint(
            f"channel IN ({_sql_list(CHANNELS)})", name="ck_outbox_messages_channel"
        ),
        sa.CheckConstraint(
            f"origin IN ({_sql_list(ORIGINS)})", name="ck_outbox_messages_origin"
        ),
        sa.CheckConstraint(
            f"authored_by IN ({_sql_list(AUTHORS)})",
            name="ck_outbox_messages_authored_by",
        ),
        sa.CheckConstraint(
            f"state IN ({_sql_list(OUTBOX_STATES)})", name="ck_outbox_messages_state"
        ),
        sa.CheckConstraint(
            f"approved_by IN ({_sql_list(APPROVERS)})",
            name="ck_outbox_messages_approved_by",
        ),
        sa.CheckConstraint(
            "state <> 'sent' OR approved_by IS NOT NULL",
            name="ck_outbox_messages_sent_approved",
        ),
        sa.CheckConstraint(
            "state NOT IN ('approved', 'sending', 'sent') OR authored_by = 'owner' "
            "OR COALESCE(guard->>'verdict', '') = 'pass'",
            name="ck_outbox_messages_guarded",
        ),
    )
    op.create_index(
        "ux_outbox_messages_idempotency",
        "outbox_messages",
        ["idempotency_key"],
        unique=True,
    )
    for name, column in (
        ("ix_outbox_messages_person", "person_id"),
        ("ix_outbox_messages_trigger", "trigger_interaction_id"),
        ("ix_outbox_messages_command", "command_interaction_id"),
        ("ix_outbox_messages_sent_interaction", "sent_interaction_id"),
        ("ix_outbox_messages_escalation", "escalation_id"),
    ):
        op.create_index(name, "outbox_messages", [column])
    op.create_index(
        "ix_outbox_messages_ready",
        "outbox_messages",
        ["channel", "next_attempt_at"],
        postgresql_where=sa.text("state = 'approved'"),
    )
    op.create_index(
        "ix_outbox_messages_sent_at",
        "outbox_messages",
        ["sent_at"],
        postgresql_where=sa.text("sent_at IS NOT NULL"),
    )
    op.create_index(
        "ux_outbox_messages_external",
        "outbox_messages",
        ["channel", "external_message_id"],
        unique=True,
        postgresql_where=sa.text("external_message_id IS NOT NULL"),
    )
    op.create_index(
        "ix_outbox_messages_chat_state", "outbox_messages", ["chat_key", "state"]
    )
    op.create_index(
        "ix_outbox_messages_lease",
        "outbox_messages",
        ["lease_until"],
        postgresql_where=sa.text("state = 'sending'"),
    )

    # The careful start: everything is stored, nothing is shown or sent.
    op.execute("INSERT INTO jarvis_policy (scope, mode) VALUES ('global', 'shadow')")


def downgrade() -> None:
    op.drop_table("outbox_messages")
    op.drop_table("escalations")
    op.drop_table("jarvis_chat_state")
    op.drop_table("reply_rules")
    op.drop_table("jarvis_policy")

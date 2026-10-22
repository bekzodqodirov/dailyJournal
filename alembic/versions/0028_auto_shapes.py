"""Defense in depth for autonomy (JV-23): an autonomous row needs shapes_ok.

Two changes, no new table:

* ``ck_outbox_messages_auto_shapes`` on ``outbox_messages``: an AI-authored
  row approved by the policy itself (``approved_by = 'auto:policy'``, no
  owner tap) must carry a bound verdict whose ``shapes_ok`` is ``true``
  (rule 13a, the statement allowlist with the question bank): the JSON
  boolean ``true``, so neither a missing key nor the string ``"true"``
  passes (``gate.shapes_passed`` reads it the same way). COALESCE: a CHECK
  passes when its expression is NULL, and a verdict without the key makes
  the comparison NULL, so it is read as false. The code already holds this
  line (``outbox.create``, ``gate.evaluate`` at the claim); the database
  now holds it too.
* ``ck_jarvis_policy_scope`` admits one more scope, ``question_bank``: the
  row whose ``history`` holds the owner's edits of the question bank (the
  only questions an autonomous reply may ask, ``miya/jarvis/qbank.py``).

The data step first takes the policy approver off rows that would break the
new CHECK: an ``approved`` one goes back to the owner's card (``draft``,
``unapproved_statement``), as the claim would have sent it; a finished one
that never went out (blocked, expired, cancelled, failed) keeps its state.
Each change is written to the row's history. A row on the wire (``sending``,
``sent``, ``unknown``) without a passing shape check cannot exist (the claim
has refused such a row since the allowlist landed, before any sender); if one
does, the upgrade stops and names it rather than hide it.

Revision ID: 0028_auto_shapes
Revises: 0027_knowledge_base
Create Date: JV-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0028_auto_shapes"
down_revision: str | None = "0027_knowledge_base"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AUTO_APPROVER = "auto:policy"
AUTO_SHAPES_CHECK = (
    "authored_by <> 'ai' OR approved_by IS DISTINCT FROM 'auto:policy' "
    "OR COALESCE(guard->'shapes_ok' = 'true'::jsonb, false)"
)
QUESTION_BANK_SCOPE = "question_bank"
SCOPE_PATTERN = (
    "^(global|question_bank|channel:[a-z]+|intent:[a-z_]+"
    "|channel:[a-z]+:intent:[a-z_]+)$"
)
PREVIOUS_SCOPE_PATTERN = (
    "^(global|channel:[a-z]+|intent:[a-z_]+|channel:[a-z]+:intent:[a-z_]+)$"
)
WIRE_STATES = ("sending", "sent", "unknown")
# The code a redrafted row carries (gate.UNAPPROVED_STATEMENT).
UNAPPROVED = "unapproved_statement"
BY = "migration"

_VIOLATING = (
    "authored_by = 'ai' AND approved_by = 'auto:policy' "
    "AND NOT COALESCE(guard->'shapes_ok' = 'true'::jsonb, false)"
)


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    bind = op.get_bind()
    on_wire = bind.execute(
        sa.text(
            f"SELECT id FROM outbox_messages WHERE {_VIOLATING} "
            f"AND state IN ({_sql_list(WIRE_STATES)}) ORDER BY id LIMIT 20"
        )
    ).scalars()
    found = [int(row_id) for row_id in on_wire]
    if found:
        raise RuntimeError(
            "outbox_messages rows on the wire without a passing shape check: "
            + ", ".join(str(row_id) for row_id in found)
        )
    op.execute(
        sa.text(
            "UPDATE outbox_messages SET "
            "history = history || jsonb_build_array(jsonb_build_object("
            "'at', to_jsonb(now()), "
            "'field', 'state', 'old', state, 'new', 'draft', 'by', :by, "
            "'code', :code)), "
            "state = 'draft', approved_by = NULL, approved_at = NULL, "
            "next_attempt_at = NULL, last_error = :code "
            f"WHERE {_VIOLATING} AND state = 'approved'"
        ).bindparams(by=BY, code=UNAPPROVED)
    )
    op.execute(
        sa.text(
            "UPDATE outbox_messages SET "
            "history = history || jsonb_build_array(jsonb_build_object("
            "'at', to_jsonb(now()), "
            "'field', 'approved_by', 'old', approved_by, 'new', NULL, "
            "'by', :by, 'code', :code)), "
            "approved_by = NULL, approved_at = NULL "
            f"WHERE {_VIOLATING}"
        ).bindparams(by=BY, code=UNAPPROVED)
    )
    op.create_check_constraint(
        "ck_outbox_messages_auto_shapes", "outbox_messages", AUTO_SHAPES_CHECK
    )
    op.drop_constraint("ck_jarvis_policy_scope", "jarvis_policy", type_="check")
    op.create_check_constraint(
        "ck_jarvis_policy_scope", "jarvis_policy", f"scope ~ '{SCOPE_PATTERN}'"
    )


def downgrade() -> None:
    # The old pattern has no place for the question bank: its row (the
    # owner's edits) goes with the scope.
    op.execute(
        sa.text("DELETE FROM jarvis_policy WHERE scope = :scope").bindparams(
            scope=QUESTION_BANK_SCOPE
        )
    )
    op.drop_constraint("ck_jarvis_policy_scope", "jarvis_policy", type_="check")
    op.create_check_constraint(
        "ck_jarvis_policy_scope",
        "jarvis_policy",
        f"scope ~ '{PREVIOUS_SCOPE_PATTERN}'",
    )
    op.drop_constraint("ck_outbox_messages_auto_shapes", "outbox_messages", type_="check")

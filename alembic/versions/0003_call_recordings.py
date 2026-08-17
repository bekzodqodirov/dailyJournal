"""Expression index for call-recording dedupe by media sha256.

Revision ID: 0003_call_recordings
Revises: 0002_reminder_log
Create Date: Phase 2
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_call_recordings"
down_revision: str | None = "0002_reminder_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The scan checks every candidate file's hash against interactions once a
    # minute; a partial expression index keeps that lookup off a seq scan.
    op.create_index(
        "ix_interactions_media_sha256",
        "interactions",
        [sa.text("(media ->> 'sha256')")],
        postgresql_where=sa.text("source = 'phone_call'"),
    )


def downgrade() -> None:
    op.drop_index("ix_interactions_media_sha256", table_name="interactions")

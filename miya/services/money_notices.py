"""Telegram receipts for money events (WP-15).

Until WP-15 lands, ``note`` is a no-op with its final signature, so the
booking service already calls it for every verdict.
"""

from __future__ import annotations

from datetime import datetime

from miya.db.models import Interaction, Transaction


def note(
    interaction: Interaction,
    verdict: str,
    *,
    txn: Transaction | None,
    merged: bool,
    now: datetime,
) -> None:
    """Queue the owner's receipt for one money event (filled in by WP-15)."""
    return None

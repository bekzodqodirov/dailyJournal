"""Who wrote an outgoing message: the owner, or MIYA in their name (JV-10).

Once MIYA can send (the Jarvis outbox), the owner's accounts carry two kinds
of outgoing message: words the owner typed, and replies MIYA sent for them.
Both arrive as ``direction='out'`` rows. Only the first kind is the owner's
word: an automatic stall such as "aniqlashtirib xabar beraman" must never
become the owner's promise, close an open question or train the style
profile. A message MIYA sent carries its tag in the interaction metadata:

    {"sent_by": "miya", "authored_by": "ai" | "owner", "outbox_id": <id>}

``authored_by='owner'`` is a message the owner dictated or approved word for
word (an owner command, an edited card): it stays the owner's words.
``authored_by='ai'`` is MIYA's own text, and is never the owner's.

This module is the one place that decides it. Every consumer asks
``is_owner_words`` / ``is_ai_reply`` for a loaded row, or filters with
``owner_words_clause`` / ``ai_reply_clause`` in SQL; the two forms agree on
every row, a row with no metadata at all included. A row without the tag
behaves exactly as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from miya.db.enums import Direction
from miya.db.models import Interaction

Author = Literal["ai", "owner"]

SENT_BY_KEY: Final = "sent_by"
AUTHORED_BY_KEY: Final = "authored_by"
OUTBOX_ID_KEY: Final = "outbox_id"
SENT_BY_MIYA: Final = "miya"
AUTHORED_BY_AI: Final[Author] = "ai"
AUTHORED_BY_OWNER: Final[Author] = "owner"

# The transcript labels (windows, recap prose input, passage context): the
# owner's own words stay [ME]; MIYA's automatic replies are [MIYA].
SPEAKER_OWNER: Final = "ME"
SPEAKER_MIYA: Final = "MIYA"

# outbox_messages.channel for the Telegram account.
CHANNEL_TELEGRAM: Final = "telegram"

# The outbox table the echo lookup reads. It is created by the Jarvis core
# migration; until then no row can match and the lookup says so.
OUTBOX_TABLE: Final = "outbox_messages"


def _meta(row: Interaction) -> dict[str, Any]:
    meta = row.meta
    return meta if isinstance(meta, dict) else {}


def is_ai_reply(row: Interaction) -> bool:
    """Is this an outgoing message MIYA wrote itself (``authored_by='ai'``)?"""
    meta = _meta(row)
    return (
        row.direction is Direction.out
        and SENT_BY_KEY in meta
        and meta.get(AUTHORED_BY_KEY) == AUTHORED_BY_AI
    )


def is_owner_words(row: Interaction) -> bool:
    """Is this outgoing message the owner's own words?

    ``direction='out' AND NOT (metadata ? 'sent_by' AND
    metadata->>'authored_by' = 'ai')``: everything the owner sent, plus what
    MIYA sent with the owner's exact text, minus MIYA's own replies.
    """
    return row.direction is Direction.out and not is_ai_reply(row)


def _ai_authored() -> sa.ColumnElement[bool]:
    # COALESCE: with no metadata, or a tag without authored_by, the AND is
    # NULL, and NOT NULL would drop the row from both predicates. The Python
    # form treats either as "not MIYA's text", so the SQL form must too.
    return sa.func.coalesce(
        sa.and_(
            Interaction.meta.has_key(SENT_BY_KEY),
            Interaction.meta[AUTHORED_BY_KEY].astext == AUTHORED_BY_AI,
        ),
        sa.false(),
    )


def ai_reply_clause() -> sa.ColumnElement[bool]:
    """SQL twin of ``is_ai_reply``."""
    return sa.and_(Interaction.direction == Direction.out, _ai_authored())


def owner_words_clause() -> sa.ColumnElement[bool]:
    """SQL twin of ``is_owner_words``: the one predicate for the owner's words."""
    return sa.and_(Interaction.direction == Direction.out, sa.not_(_ai_authored()))


def said_by_clause(direction: Direction) -> sa.ColumnElement[bool]:
    """The rows that are the words of ``direction``'s side.

    For ``out`` that is ``owner_words_clause``: MIYA's own replies are not the
    owner's lines. Any other direction is that direction as stored.
    """
    if direction is Direction.out:
        return owner_words_clause()
    return Interaction.direction == direction


def speaker_label(row: Interaction) -> str | None:
    """``MIYA`` for an automatic reply, ``ME`` for the owner's own words, and
    None for anything that is not outgoing (the caller names the other side)."""
    if is_ai_reply(row):
        return SPEAKER_MIYA
    if is_owner_words(row):
        return SPEAKER_OWNER
    return None


# --- tagging the echo of a sent message -----------------------------------------------


@dataclass(frozen=True, slots=True)
class Echo:
    """The outbox row an outgoing message was sent from."""

    outbox_id: int
    authored_by: Author


def author_of(value: object) -> Author:
    """``authored_by`` as stored on an outbox row. Anything but an explicit
    ``'owner'`` is MIYA's text: an unknown value never becomes the owner's
    words."""
    return AUTHORED_BY_OWNER if value == AUTHORED_BY_OWNER else AUTHORED_BY_AI


def echo_meta(echo: Echo) -> dict[str, Any]:
    """The metadata keys that mark an interaction as sent by MIYA."""
    return {
        SENT_BY_KEY: SENT_BY_MIYA,
        AUTHORED_BY_KEY: echo.authored_by,
        OUTBOX_ID_KEY: echo.outbox_id,
    }


def tagged(meta: dict[str, Any] | None, echo: Echo) -> dict[str, Any]:
    """``meta`` with the echo tag merged in (a new dict; JSONB needs one)."""
    return {**(meta or {}), **echo_meta(echo)}


def telegram_external_id(chat_id: int, message_id: int) -> str:
    """``outbox_messages.external_message_id`` of a Telegram message.

    Qualified by the chat: a supergroup numbers its messages on its own, so a
    bare message id is not unique on the channel.
    """
    return f"{chat_id}:{message_id}"


async def find_echo(
    session: AsyncSession, *, channel: str, external_id: str
) -> Echo | None:
    """The outbox row that sent the message ``external_id`` on ``channel``.

    Ingest calls this for every outgoing message it stores; it tags the row
    only when the outbox row already carries ``external_message_id`` (e.g.
    after a reconcile). A row ingested before ``outbox.complete`` records the
    id is stored untagged, and ``outbox.complete`` must tag that existing row
    (the IntegrityError on the ``ux_interactions_tg_message`` path in JV-19).
    Before the outbox table exists nothing can match.
    """
    present = await session.scalar(
        sa.text("SELECT to_regclass(:table) IS NOT NULL"), {"table": OUTBOX_TABLE}
    )
    if not present:
        return None
    row = (
        await session.execute(
            sa.text(
                f"SELECT id, authored_by FROM {OUTBOX_TABLE} "
                "WHERE channel = :channel AND external_message_id = :external_id "
                "ORDER BY id LIMIT 1"
            ),
            {"channel": channel, "external_id": external_id},
        )
    ).first()
    if row is None:
        return None
    return Echo(outbox_id=int(row[0]), authored_by=author_of(row[1]))


__all__ = [
    "AUTHORED_BY_AI",
    "AUTHORED_BY_KEY",
    "AUTHORED_BY_OWNER",
    "CHANNEL_TELEGRAM",
    "OUTBOX_ID_KEY",
    "SENT_BY_KEY",
    "SENT_BY_MIYA",
    "SPEAKER_MIYA",
    "SPEAKER_OWNER",
    "Author",
    "Echo",
    "ai_reply_clause",
    "author_of",
    "echo_meta",
    "find_echo",
    "is_ai_reply",
    "is_owner_words",
    "owner_words_clause",
    "said_by_clause",
    "speaker_label",
    "tagged",
    "telegram_external_id",
]

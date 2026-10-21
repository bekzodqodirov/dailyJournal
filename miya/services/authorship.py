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
from datetime import timedelta
from typing import Any, Final, Literal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from miya.db.enums import Direction
from miya.db.models import Interaction, OutboxMessage

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
# interactions.metadata key of a Telegram message's id.
TG_MESSAGE_KEY: Final = "tg_message_id"
# How long after MIYA's send its echo may still arrive (JV-17, plan Q11).
ECHO_WINDOW: Final = timedelta(minutes=2)
# outbox_messages.state of a row on the wire (claimed, or in doubt after its
# lease ran out) and of one sent (OUTBOX_STATES in miya.db.models).
_ON_THE_WIRE: Final = ("sending", "unknown")
_SENT: Final = "sent"
# What Python's str.strip() takes off both ends, for the SQL text match.
_BLANKS: Final = " \t\n\r\x0b\x0c"


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


def is_owner_typed(row: Interaction) -> bool:
    """Did the owner write this outgoing message themselves, rather than
    through MIYA? ``is_owner_words`` minus every message MIYA sent (an owner
    command's echo included): the owner taking a chat over (JV-17) is a
    message they typed, never one MIYA delivered for them."""
    return is_owner_words(row) and SENT_BY_KEY not in _meta(row)


def owner_typed_clause() -> sa.ColumnElement[bool]:
    """SQL twin of ``is_owner_typed``."""
    # COALESCE: a row with no metadata at all was typed by the owner.
    sent_by_miya = sa.func.coalesce(Interaction.meta.has_key(SENT_BY_KEY), sa.false())
    return sa.and_(owner_words_clause(), sa.not_(sent_by_miya))


def untagged_echo_clause() -> sa.ColumnElement[bool]:
    """Is this outgoing Telegram message MIYA's own send whose echo is not
    tagged yet (plan Q11: the echo can be stored before the outbox records
    the sent message's id)? True when an outbox row of the same chat carries
    its external id, or has exactly its text and was on the wire around it:
    in flight or in doubt (``sending``/``unknown``) with a claim lease
    reaching ECHO_WINDOW before the message, or ``sent`` within ECHO_WINDOW
    before it. Each state is bounded in time, so an old send never hides a
    later message the owner typed with the same words."""
    external = sa.func.concat(
        sa.cast(Interaction.tg_chat_id, sa.Text),
        ":",
        Interaction.meta[TG_MESSAGE_KEY].astext,
    )
    text = sa.func.btrim(Interaction.raw_text, _BLANKS)
    since = Interaction.occurred_at - sa.literal(ECHO_WINDOW, sa.Interval())
    return sa.exists().where(
        OutboxMessage.channel == CHANNEL_TELEGRAM,
        OutboxMessage.tg_chat_id == Interaction.tg_chat_id,
        sa.or_(
            sa.and_(
                Interaction.meta.has_key(TG_MESSAGE_KEY),
                OutboxMessage.external_message_id == external,
            ),
            sa.and_(
                text != "",
                sa.func.btrim(OutboxMessage.body, _BLANKS) == text,
                sa.or_(
                    sa.and_(
                        OutboxMessage.state.in_(_ON_THE_WIRE),
                        OutboxMessage.lease_until >= since,
                    ),
                    sa.and_(OutboxMessage.state == _SENT, OutboxMessage.sent_at >= since),
                ),
            ),
        ),
    )


def owner_takeover_clause() -> sa.ColumnElement[bool]:
    """The one predicate for a message the owner typed in a chat (JV-17):
    ``owner_typed_clause`` minus MIYA's untagged echoes."""
    return sa.and_(owner_typed_clause(), sa.not_(untagged_echo_clause()))


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
    only when the outbox row already carries ``external_message_id``. A
    message ingested before ``outbox.complete`` records the id is stored
    untagged, and ``outbox.complete`` tags that existing row itself (JV-11),
    so whichever side stores the echo first, it ends up tagged once.
    """
    row = (
        await session.execute(
            sa.select(OutboxMessage.id, OutboxMessage.authored_by)
            .where(
                OutboxMessage.channel == channel,
                OutboxMessage.external_message_id == external_id,
            )
            .order_by(OutboxMessage.id)
            .limit(1)
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
    "ECHO_WINDOW",
    "OUTBOX_ID_KEY",
    "SENT_BY_KEY",
    "SENT_BY_MIYA",
    "SPEAKER_MIYA",
    "SPEAKER_OWNER",
    "TG_MESSAGE_KEY",
    "Author",
    "Echo",
    "ai_reply_clause",
    "author_of",
    "echo_meta",
    "find_echo",
    "is_ai_reply",
    "is_owner_typed",
    "is_owner_words",
    "owner_takeover_clause",
    "owner_typed_clause",
    "owner_words_clause",
    "said_by_clause",
    "speaker_label",
    "tagged",
    "telegram_external_id",
    "untagged_echo_clause",
]

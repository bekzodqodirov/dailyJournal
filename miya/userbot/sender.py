"""The Telegram sender (JV-19): the narrow exception to the userbot's
read-only rule (docs/jarvis-plan.md §3.2, rules 12, 24, 25, 27).

The userbot reads. This module is the one place in MIYA that writes to
Telegram as the owner, and an AST test (``tests/test_userbot.py``) holds it
to that: every other module of ``miya/userbot`` and every module that
imports Telethon makes no write call at all, and here the writes live in
three named functions only (``_mark_read``, ``_typing``, ``_send``). It runs
as a task inside the userbot process (one Telethon client per session: a
second one would risk ``AuthKeyDuplicatedError``), started only with
JARVIS_ENABLED and TELEGRAM_SEND_ENABLED.

* **Rows come only from the outbox.** Every POLL_SECONDS the sender calls
  ``outbox.claim_ready('telegram', …)``, which re-runs the whole gate in one
  transaction and commits before any network I/O. Nothing else here reads or
  changes ``outbox_messages``; each row is reported back with
  ``outbox.complete`` or ``outbox.fail``.
* **Human pacing.** The claim's pacing plan: a pause drawn between
  JARVIS_REPLY_DELAY_MIN_SECONDS and JARVIS_REPLY_DELAY_MAX_SECONDS, the chat
  marked read up to the message answered (only here, only when a reply is
  really sent), "typing…" for the body's length (JARVIS_TYPING_CHARS_PER_SECOND,
  at most JARVIS_TYPING_MAX_SECONDS), then the text. The gap between two sends
  from the account (JARVIS_SEND_MIN_GAP_SECONDS) is held by the claim, counted
  in SQL, and one row is claimed at a time.
* **Exactly once.** Telegram's ``random_id`` is derived from the row's
  idempotency key, so a second attempt of the same row is a no-op at
  Telegram's side: ``RandomIdDuplicateError`` means it already went. A send
  whose outcome is not known (the connection dropped mid-call) is never
  retried: the chat's newest messages are read back (``reconcile``); found,
  the row is sent, else its lease runs out and ``outbox.reconcile`` makes it
  ``unknown`` and the owner is told. MIYA never resends blindly.
* **Platform limits (rule 24).** A flood wait re-queues the row at Telegram's
  time (nothing sleeps inside a claim) and the sender asks for nothing until
  then; a flood wait above FLOOD_BREAKER_SECONDS, ``PeerFloodError``, a
  restriction or a dead session trips the Telegram breaker
  (``policy.trip_breaker``) and the worker alerts the owner; nothing retries
  into it, and reading goes on. A breaker Telegram gave no time for lasts
  until the owner lifts it (/jarvis yoq telegram). A client who blocked the
  owner, a deleted account, a chat that forbids writing or takes only paid
  messages fails the row for good; ``allow_paid_stars`` is never set.
* **Plain text only (rule 27).** The request carries the body and nothing
  else: no entities (no parse mode), no link preview, no media, no buttons.
* **Contained (rule 25).** Each row is handled in its own try/except; a
  failure is logged with the row's id and the loop goes on. The loop runs
  under ``retry.with_backoff``; should the task die anyway, a done-callback
  logs it and starts it again. Neither ever stops the reader.

Logs carry ids and codes only, never a body (rule 23).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sys
import time
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Literal

# Telethon ships no type information; everything taken from it is checked
# by the tests against the fake client instead.
from telethon import errors  # type: ignore[import-untyped]
from telethon.tl import functions, types  # type: ignore[import-untyped]

from miya.config import settings
from miya.db.session import session_scope
from miya.jarvis import chatkey, gate, outbox, policy
from miya.services import authorship, retry

log = logging.getLogger(__name__)

CHANNEL: Final = chatkey.TELEGRAM
# How often the outbox is asked for a row, and how many rows one claim takes.
POLL_SECONDS: Final = 2.0
CLAIM_LIMIT: Final = 1
# A flood wait longer than this is a limit, not pacing: the breaker trips.
FLOOD_BREAKER_SECONDS: Final = 300
# A flood wait above FLOOD_BREAKER_SECONDS (Telegram named the time) stops
# Telegram sending this long, never shorter than Telegram's own wait, unless
# the owner lifts it first (/jarvis yoq telegram). Rows held behind any
# breaker are offered again after this long; the gate holds them meanwhile.
BREAKER_FOR: Final = timedelta(hours=24)
# Every other breaker (a spam limit, a restriction, a dead session) gave no
# time: it lasts until the owner lifts it, as the alert says. The column keeps
# an end, so it is this far away rather than none.
BREAKER_UNTIL_LIFTED: Final = timedelta(days=3650)
# After a breaker trip the sender asks the outbox again only after this long
# (the gate holds the rows while the breaker is in force).
BREAKER_LOCAL_PAUSE_SECONDS: Final = FLOOD_BREAKER_SECONDS
# A connection error before anything went out: the row is tried again then.
RETRY_SECONDS: Final = 60
# How many of the chat's newest messages a reconcile reads back, and how
# long before the claim a message found there may have been sent.
RECONCILE_MESSAGES: Final = 20
RECONCILE_SLACK: Final = timedelta(minutes=2)
# The loop is restarted for as long as the process lives, after a pause that
# grows to RESTART_CAP_SECONDS.
RESTART_ATTEMPTS: Final = sys.maxsize
RESTART_BASE_SECONDS: Final = 2.0
RESTART_CAP_SECONDS: Final = 300.0
# What a paid-message refusal says (Telegram's error, not one Telethon names).
PAID_ERROR_PREFIX: Final = "ALLOW_PAYMENT_REQUIRED"
PREMIUM_ONLY_ERROR: Final = "PRIVACY_PREMIUM_REQUIRED"
# Salt of the Telegram random_id, so it is never the idempotency key itself.
RANDOM_ID_SALT: Final = "miya:tg-random-id:"
BY: Final = outbox.BY_ADAPTER

# The codes the sender reports (outbox.last_error, String(64)).
CODE_SENT: Final = "sent"
CODE_FLOOD: Final = outbox.FLOOD_WAIT_CODE
CODE_SLOW_MODE: Final = "slow_mode"
CODE_FLOOD_LONG: Final = "flood_wait_long"
CODE_PEER_FLOOD: Final = "peer_flood"
CODE_AUTH_DUPLICATED: Final = "auth_key_duplicated"
CODE_SESSION_REVOKED: Final = "session_revoked"
CODE_BANNED: Final = "account_banned"
CODE_FROZEN: Final = "account_frozen"
CODE_RESTRICTED: Final = "account_restricted"
CODE_USER_BLOCKED: Final = "user_blocked"
CODE_USER_DEACTIVATED: Final = "user_deactivated"
CODE_WRITE_FORBIDDEN: Final = "write_forbidden"
CODE_PAID_ONLY: Final = "paid_only"
CODE_PEER_NOT_FOUND: Final = "peer_not_found"
CODE_REJECTED: Final = "send_rejected"
CODE_CONNECTION: Final = "connection"
CODE_SEND_ERROR: Final = "send_error"
CODE_NOT_TEXT: Final = gate.PAYLOAD_NOT_TEXT
CODE_UNKNOWN: Final = "outcome_unknown"
CODE_NOT_RECORDED: Final = "complete_failed"
CODE_INTERRUPTED: Final = "interrupted"

Action = Literal["sent", "requeue", "breaker", "fail", "unknown"]
Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], datetime]


class PeerNotFound(LookupError):
    """The chat is not in the session's entity cache, even after a dialog
    sync; contacts are never imported to find it."""


@dataclass(frozen=True, slots=True)
class Verdict:
    """How one send ended, or what an error means for its row."""

    action: Action
    code: str
    seconds: int = 0


_BREAKERS: Final[
    tuple[tuple[type[BaseException] | tuple[type[BaseException], ...], str], ...]
] = (
    (errors.PeerFloodError, CODE_PEER_FLOOD),
    (errors.AuthKeyDuplicatedError, CODE_AUTH_DUPLICATED),
    ((errors.SessionRevokedError, errors.AuthKeyUnregisteredError), CODE_SESSION_REVOKED),
    ((errors.UserDeactivatedBanError, errors.UserDeactivatedError), CODE_BANNED),
    (errors.FrozenMethodInvalidError, CODE_FROZEN),
    # "Banned from sending messages in supergroups/channels": the account's
    # spam limit for groups (the group side of PEER_FLOOD), not one chat's ban,
    # which comes back as CHAT_WRITE_FORBIDDEN or CHANNEL_PRIVATE.
    ((errors.UserRestrictedError, errors.UserBannedInChannelError), CODE_RESTRICTED),
    # Any other 401 (an expired or invalid auth key, ...): the session is dead
    # for every row, not just this one.
    (errors.UnauthorizedError, CODE_SESSION_REVOKED),
)
_PERMANENT: Final[
    tuple[tuple[type[BaseException] | tuple[type[BaseException], ...], str], ...]
] = (
    (errors.UserIsBlockedError, CODE_USER_BLOCKED),
    (errors.InputUserDeactivatedError, CODE_USER_DEACTIVATED),
    (
        (
            errors.ChatWriteForbiddenError,
            errors.ChatSendPlainForbiddenError,
            errors.ChatRestrictedError,
            errors.ChatGuestSendForbiddenError,
            errors.ChatAdminRequiredError,
            errors.ChannelPrivateError,
            errors.UserPrivacyRestrictedError,
            errors.YouBlockedUserError,
        ),
        CODE_WRITE_FORBIDDEN,
    ),
    ((PeerNotFound, errors.PeerIdInvalidError), CODE_PEER_NOT_FOUND),
)
# Codes that end a row for good: an error with one of them in a pacing step
# (the read receipt, "typing…") ends the row there too.
_HARD: Final = frozenset(code for _, code in _PERMANENT) | {CODE_PAID_ONLY}


def classify(exc: BaseException, *, on_wire: bool) -> Verdict:
    """What an error from Telegram means for the row (rule 24).

    ``on_wire``: the error came from the send request itself, so a failure
    that says nothing about the outcome (a dropped connection, a server
    error) may have sent the message after all: ``unknown``, never retried.
    Before that nothing went out, and such a failure is retried later."""
    if isinstance(exc, errors.SlowModeWaitError):
        return Verdict("requeue", CODE_SLOW_MODE, max(int(exc.seconds or 0), 1))
    if isinstance(exc, errors.FloodWaitError | errors.FloodPremiumWaitError):
        seconds = int(getattr(exc, "seconds", 0) or 0)
        if seconds > FLOOD_BREAKER_SECONDS:
            return Verdict("breaker", CODE_FLOOD_LONG, seconds)
        return Verdict("requeue", CODE_FLOOD, max(seconds, 1))
    for kinds, code in _BREAKERS:
        if isinstance(exc, kinds):
            return Verdict("breaker", code)
    for kinds, code in _PERMANENT:
        if isinstance(exc, kinds):
            return Verdict("fail", code)
    if isinstance(exc, errors.FloodError):
        # A 420 Telethon does not name: a limit all the same, never per row.
        return Verdict("breaker", CODE_FLOOD_LONG, int(getattr(exc, "seconds", 0) or 0))
    message = str(getattr(exc, "message", "") or "")
    if message.startswith(PAID_ERROR_PREFIX):
        return Verdict("fail", CODE_PAID_ONLY)
    if message == PREMIUM_ONLY_ERROR:
        return Verdict("fail", CODE_WRITE_FORBIDDEN)
    # Telegram's internal errors and its -503 "Timeout" (a TimedOutError, not
    # a ServerError) say nothing about whether the message went out.
    if isinstance(exc, errors.ServerError | errors.TimedOutError | OSError):
        if on_wire:
            return Verdict("unknown", CODE_UNKNOWN)
        return Verdict("requeue", CODE_CONNECTION, RETRY_SECONDS)
    if isinstance(exc, errors.RPCError):
        # Telegram refused the request: nothing was sent.
        return Verdict("fail", CODE_REJECTED)
    if on_wire:
        return Verdict("unknown", CODE_UNKNOWN)
    return Verdict("fail", CODE_SEND_ERROR)


def random_id_for(idempotency_key: str) -> int:
    """Telegram's ``random_id`` of a row: a signed 64-bit number derived from
    its idempotency key, the same on every attempt, never zero."""
    digest = hashlib.sha256(f"{RANDOM_ID_SALT}{idempotency_key}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True) or 1


def _message_id(external_id: str | None) -> int | None:
    """``123`` or ``<chat>:123`` (authorship.telegram_external_id) → 123."""
    if not external_id:
        return None
    _chat, _sep, message = external_id.rpartition(":")
    try:
        return int(message)
    except ValueError:
        return None


def reply_to_of(row: outbox.ClaimedSend) -> int | None:
    """The message the reply quotes: the one the row names, else, in a
    group, the message it answers (so the addressee is clear). In a private
    chat a person just writes, unquoted."""
    explicit = _message_id(row.reply_to_external_id)
    if explicit is not None:
        return explicit
    if row.tg_chat_id is not None and row.tg_chat_id < 0:
        return row.trigger_message_id
    return None


def sent_message_id(result: object, random_id: int) -> int | None:
    """The id Telegram gave the message sent with ``random_id``."""
    if isinstance(result, types.UpdateShortSentMessage):
        return int(result.id)
    if isinstance(result, types.UpdateShort):
        updates: list[object] = [result.update]
    else:
        updates = list(getattr(result, "updates", None) or [])
    for update in updates:
        if isinstance(update, types.UpdateMessageID) and update.random_id == random_id:
            return int(update.id)
    for update in updates:
        if isinstance(update, types.UpdateNewMessage | types.UpdateNewChannelMessage):
            message = update.message
            if getattr(message, "out", False) and isinstance(
                getattr(message, "id", None), int
            ):
                return int(message.id)
    return None


# --- the writes: the only Telegram write calls in MIYA -------------------------------


async def _mark_read(client: Any, peer: object, max_id: int) -> None:
    """The chat read up to the message answered: only here, only when a
    reply is really sent (§5.8)."""
    await client.send_read_acknowledge(peer, max_id=max_id)


async def _typing(client: Any, peer: object, typing_ms: int, sleep: Sleep) -> None:
    """ "typing…" for the planned time; nothing for an empty plan."""
    if typing_ms <= 0:
        return
    async with client.action(peer, "typing"):
        await sleep(typing_ms / 1000)


async def _send(client: Any, peer: object, row: outbox.ClaimedSend) -> tuple[object, int]:
    """The text and nothing else (rule 27): no entities, no preview, no
    media, no buttons, never a paid message. Returns Telegram's answer and
    the random_id it was sent with."""
    reply_to = reply_to_of(row)
    request = functions.messages.SendMessageRequest(
        peer=peer,
        message=row.body,
        no_webpage=True,
        reply_to=types.InputReplyToMessage(reply_to_msg_id=reply_to)
        if reply_to is not None
        else None,
        random_id=random_id_for(row.idempotency_key),
    )
    return await client(request), request.random_id


async def reconcile(client: Any, peer: object, row: outbox.ClaimedSend) -> int | None:
    """Read the chat's newest messages back to find a send whose outcome is
    not known: the owner's outgoing message with exactly this text, from the
    claim on. Its id, or None (nothing found, or the read failed: the lease
    then runs out and ``outbox.reconcile`` decides, never a resend)."""
    since = row.claimed_at - RECONCILE_SLACK
    try:
        messages = await client.get_messages(peer, limit=RECONCILE_MESSAGES)
    except Exception as exc:
        log.warning(
            "telegram send not read back",
            extra={
                "outbox_id": row.id,
                "tg_chat_id": row.tg_chat_id,
                "error": type(exc).__name__,
            },
        )
        return None
    body = row.body.strip()
    for message in messages or ():
        date = getattr(message, "date", None)
        if (
            getattr(message, "out", False)
            and (getattr(message, "message", None) or "").strip() == body
            and isinstance(date, datetime)
            and date >= since
            and isinstance(getattr(message, "id", None), int)
        ):
            return int(message.id)
    return None


# --- the outbox side -------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(settings.tz)


def breaker_until(verdict: Verdict, *, now: datetime) -> datetime:
    """The end of the breaker a verdict trips: Telegram's own long wait (at
    least BREAKER_FOR) when it named one, else until the owner lifts it."""
    if verdict.code == CODE_FLOOD_LONG and verdict.seconds > 0:
        return now + max(BREAKER_FOR, timedelta(seconds=verdict.seconds))
    return now + BREAKER_UNTIL_LIFTED


async def _record(row: outbox.ClaimedSend, verdict: Verdict, *, now: datetime) -> bool:
    """Report a send that did not go out: re-queued at Telegram's time, held
    behind the breaker it tripped, or failed for good. Contained: a report
    that cannot be written is logged and the row's lease runs out instead
    (``outbox.reconcile``: never a blind resend). False when a breaker was
    due and could not be stored, so the caller holds sending itself."""
    retry_at: datetime | None = None
    stored = True
    if verdict.action == "breaker":
        until = breaker_until(verdict, now=now)
        retry_at = min(until, now + max(BREAKER_FOR, timedelta(seconds=verdict.seconds)))
        try:
            async with session_scope() as session:
                await policy.trip_breaker(
                    session,
                    channel=CHANNEL,
                    reason=verdict.code,
                    until=until,
                    by=BY,
                    now=now,
                )
            log.error(
                "telegram breaker tripped",
                extra={
                    "outbox_id": row.id,
                    "code": verdict.code,
                    "until": until.isoformat(),
                },
            )
        except Exception as exc:
            stored = False
            log.error(
                "telegram breaker not recorded",
                extra={
                    "outbox_id": row.id,
                    "code": verdict.code,
                    "error": type(exc).__name__,
                },
            )
    elif verdict.action == "requeue":
        retry_at = now + timedelta(seconds=verdict.seconds)
    try:
        async with session_scope() as session:
            await outbox.fail(
                session, row.id, code=verdict.code, now=now, retry_at=retry_at
            )
    except Exception as exc:
        log.error(
            "telegram send failure not recorded",
            extra={
                "outbox_id": row.id,
                "code": verdict.code,
                "error": type(exc).__name__,
            },
        )
    return stored


async def _complete(
    row: outbox.ClaimedSend, message_id: int, *, sent_at: datetime
) -> bool:
    """``outbox.complete``: sent, the tagged echo written (or the one the
    reader stored tagged), the catch-up cursor moved. False when that could
    not be written; the lease then runs out and ``outbox.reconcile`` finds
    the echo or tells the owner."""
    chat_id = row.tg_chat_id if row.tg_chat_id is not None else 0
    try:
        async with session_scope() as session:
            await outbox.complete(
                session,
                row.id,
                external_id=authorship.telegram_external_id(chat_id, message_id),
                sent_at=sent_at,
            )
        return True
    except Exception as exc:
        log.error(
            "telegram send not recorded",
            extra={
                "outbox_id": row.id,
                "tg_chat_id": row.tg_chat_id,
                "tg_message_id": message_id,
                "error": type(exc).__name__,
            },
        )
        return False


# --- the loop --------------------------------------------------------------------------


@dataclass(slots=True)
class Sender:
    """One Telegram account's sender. ``status`` is the userbot's beat
    detail share (merged into the single ``userbot`` heartbeat, whose detail
    is replaced whole, so the beat stays its one writer)."""

    client: Any
    account: str | None = None
    status: MutableMapping[str, object] = field(default_factory=dict)
    sleep: Sleep = asyncio.sleep
    clock: Clock = _now
    # A flood wait or a breaker trip: no claim before this (time.monotonic()).
    resume_at: float = 0.0

    async def run(self) -> None:
        """Poll the outbox for as long as the process lives."""
        self.status["sender"] = "running"
        while True:
            await self.poll_once()
            await self.sleep(POLL_SECONDS)

    async def poll_once(self) -> int:
        """One claim and its sends; returns how many went out. A claim that
        fails is logged and the next poll tries again."""
        if time.monotonic() < self.resume_at:
            return 0
        try:
            async with session_scope() as session:
                rows = await outbox.claim_ready(
                    session,
                    CHANNEL,
                    now=self.clock(),
                    account=self.account,
                    limit=CLAIM_LIMIT,
                )
        except Exception as exc:
            log.error("telegram claim failed", extra={"error": type(exc).__name__})
            self.status["sender_error"] = "claim_failed"
            return 0
        sent = 0
        for row in rows:
            try:
                verdict = await self.send_one(row)
            except Exception as exc:
                # send_one contains its steps; this is the last line. The
                # row's stage is not known, so it is left to its lease.
                log.error(
                    "telegram send failed for one row",
                    extra={"outbox_id": row.id, "error": type(exc).__name__},
                )
                self.status["sender_error"] = CODE_SEND_ERROR
                continue
            if verdict.action == "sent":
                sent += 1
        return sent

    async def send_one(self, row: outbox.ClaimedSend) -> Verdict:
        """Pace, mark read, type, send, report: one claimed row."""
        if row.tg_chat_id is None:
            return await self._settle(row, Verdict("fail", CODE_PEER_NOT_FOUND))
        if not gate.plain_text(row.body):
            return await self._settle(row, Verdict("fail", CODE_NOT_TEXT))
        peer: object | None = None
        on_wire = False
        try:
            peer = await self._peer(row.tg_chat_id)
            await self.sleep(row.pacing.pre_delay_seconds)
            if row.pacing.mark_read and row.trigger_message_id is not None:
                await self._pacing_step(
                    row, _mark_read(self.client, peer, row.trigger_message_id)
                )
            await self._pacing_step(
                row, _typing(self.client, peer, row.pacing.typing_ms, self.sleep)
            )
            on_wire = True
            result, random_id = await _send(self.client, peer, row)
        except asyncio.CancelledError:
            if not on_wire:
                # Stopped during the pause or "typing…": nothing went out, so
                # the row waits approved for the next start, not in doubt.
                await _record(row, Verdict("requeue", CODE_INTERRUPTED), now=self.clock())
            raise
        except errors.RandomIdDuplicateError:
            # This random_id went before: the row is already in the chat.
            log.info("telegram send was already made", extra={"outbox_id": row.id})
            found = await reconcile(self.client, peer, row) if peer is not None else None
            return await self._finish(row, found)
        except Exception as exc:
            verdict = classify(exc, on_wire=on_wire)
            log.warning(
                "telegram send did not go out",
                extra={
                    "outbox_id": row.id,
                    "tg_chat_id": row.tg_chat_id,
                    "code": verdict.code,
                    "error": type(exc).__name__,
                },
            )
            if verdict.action == "unknown":
                found = (
                    await reconcile(self.client, peer, row) if peer is not None else None
                )
                return await self._finish(row, found)
            return await self._settle(row, verdict)
        found = sent_message_id(result, random_id)
        if found is None:
            found = await reconcile(self.client, peer, row)
        return await self._finish(row, found)

    async def _pacing_step(self, row: outbox.ClaimedSend, step: Awaitable[None]) -> None:
        """The read receipt or "typing…": a limit, a dead session or a chat
        that cannot be written to ends the row there; any other failure is
        logged and the reply still goes out (pacing is not the message)."""
        try:
            await step
        except Exception as exc:
            verdict = classify(exc, on_wire=False)
            if verdict.action in ("requeue", "breaker") or verdict.code in _HARD:
                raise
            log.info(
                "telegram pacing step failed",
                extra={"outbox_id": row.id, "error": type(exc).__name__},
            )

    async def _peer(self, chat_id: int) -> object:
        """The chat's input peer from the session's cache, after one dialog
        sync when it is not there; contacts are never imported.

        That sync never sleeps inside the claim and is never skipped: a flood
        wait on the dialog list is raised, so the row is re-queued at
        Telegram's time (or the breaker trips), and ``PeerNotFound`` means a
        dialog list was really read without the chat in it."""
        try:
            return await self.client.get_input_entity(chat_id)
        except ValueError:
            pass
        from miya.userbot import main as userbot

        await userbot.sync_from_client(self.client, sleep_through=False)
        try:
            return await self.client.get_input_entity(chat_id)
        except ValueError as exc:
            raise PeerNotFound(str(chat_id)) from exc

    async def _settle(self, row: outbox.ClaimedSend, verdict: Verdict) -> Verdict:
        stored = await _record(row, verdict, now=self.clock())
        if verdict.action == "breaker":
            if stored:
                # The gate holds the rows while the stored breaker is in force.
                self._pause(max(BREAKER_LOCAL_PAUSE_SECONDS, verdict.seconds))
            else:
                # No breaker stored: nothing would hold the next row, so the
                # sender itself asks for none until the breaker would end.
                self._pause(
                    max(BREAKER_FOR, timedelta(seconds=verdict.seconds)).total_seconds()
                )
        elif verdict.code == CODE_FLOOD:
            self._pause(verdict.seconds)
        self.status["sender_error"] = verdict.code
        return verdict

    def _pause(self, seconds: float) -> None:
        self.resume_at = max(self.resume_at, time.monotonic() + seconds)
        self.status["sender_paused_until"] = (
            self.clock() + timedelta(seconds=seconds)
        ).isoformat()

    async def _finish(self, row: outbox.ClaimedSend, message_id: int | None) -> Verdict:
        if message_id is None:
            log.warning(
                "telegram send outcome unknown; left to reconcile",
                extra={"outbox_id": row.id, "tg_chat_id": row.tg_chat_id},
            )
            self.status["sender_error"] = CODE_UNKNOWN
            return Verdict("unknown", CODE_UNKNOWN)
        sent_at = self.clock()
        if not await _complete(row, message_id, sent_at=sent_at):
            self.status["sender_error"] = CODE_NOT_RECORDED
            return Verdict("unknown", CODE_NOT_RECORDED)
        self.status["sender_sent_at"] = sent_at.isoformat()
        log.info(
            "telegram send made",
            extra={
                "outbox_id": row.id,
                "tg_chat_id": row.tg_chat_id,
                "tg_message_id": message_id,
            },
        )
        return Verdict("sent", CODE_SENT)


# --- supervision -----------------------------------------------------------------------


async def _run_logged(sender: Sender) -> None:
    try:
        await sender.run()
    except Exception as exc:
        log.error("telegram sender loop died", extra={"error": type(exc).__name__})
        sender.status["sender"] = "restarting"
        sender.status["sender_error"] = type(exc).__name__[:64]
        raise


async def supervise(sender: Sender) -> None:
    """``Sender.run`` under ``retry.with_backoff``: a loop that dies is
    logged with its error class and started again after a growing pause."""
    await retry.with_backoff(
        lambda: _run_logged(sender),
        attempts=RESTART_ATTEMPTS,
        base=RESTART_BASE_SECONDS,
        cap=RESTART_CAP_SECONDS,
        retry_on=(Exception,),
        sleep=sender.sleep,
    )


@dataclass(slots=True)
class Handle:
    """The running sender task, restarted by its done-callback should the
    supervisor itself ever end; ``cancel`` stops it for good (shutdown)."""

    sender: Sender
    task: asyncio.Task[None] | None = None
    restarts: int = 0
    stopping: bool = False

    def cancel(self) -> None:
        self.stopping = True
        if self.task is not None:
            self.task.cancel()


def _on_done(handle: Handle, task: asyncio.Task[None]) -> None:
    if handle.stopping or task.cancelled():
        handle.sender.status["sender"] = "stopped"
        return
    error = task.exception()
    handle.restarts += 1
    log.error(
        "telegram sender task ended; starting it again",
        extra={
            "error": type(error).__name__ if error is not None else None,
            "restarts": handle.restarts,
        },
    )
    handle.sender.status["sender"] = "restarting"
    _launch(handle)


def _launch(handle: Handle) -> None:
    task = asyncio.create_task(supervise(handle.sender), name="telegram-sender")
    task.add_done_callback(lambda done: _on_done(handle, done))
    handle.task = task


def start(
    client: Any,
    *,
    account: str | None = None,
    status: MutableMapping[str, object] | None = None,
    sleep: Sleep = asyncio.sleep,
    clock: Clock = _now,
) -> Handle:
    """Start the sender as a task next to the reader; returns its handle."""
    sender = Sender(
        client,
        account=account,
        status=status if status is not None else {},
        sleep=sleep,
        clock=clock,
    )
    handle = Handle(sender)
    _launch(handle)
    log.info("telegram sender started", extra={"account": account})
    return handle


__all__ = [
    "CHANNEL",
    "FLOOD_BREAKER_SECONDS",
    "Handle",
    "PeerNotFound",
    "Sender",
    "Verdict",
    "breaker_until",
    "classify",
    "random_id_for",
    "reconcile",
    "reply_to_of",
    "sent_message_id",
    "start",
    "supervise",
]

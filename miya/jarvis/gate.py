"""The outbound gate: one pure decision per outbox row (docs/jarvis-plan.md §3.2).

``evaluate(row, ctx)`` runs every gate line, in the plan's order, against a
frozen snapshot of the row and the facts ``outbox.claim_ready`` read in the
claim transaction. The same function serves every adapter (the Telegram
sender, the WhatsApp bridge claim, ``gmail_send``), so a channel can never
skip a line. The first line that objects decides:

* ``hold`` — not now: the row stays ``approved`` and is looked at again at
  ``retry_at`` (a pause, a cap, the anti-ban gap, a scheduled time, quiet
  hours). The code is recorded on the row, never a silent skip.
* ``redraft`` — the row needs the owner again: back to ``draft`` (an auto row
  whose intent was demoted, an owner row whose card must show a new warning,
  a body that changed after the owner's tap).
* ``block`` / ``expire`` / ``cancel`` — the row is never sent.
* ``send`` — every line passed; the row may be claimed.

An autonomous row (``auto:policy``, no owner tap) also needs the statement
allowlist (§5.2 step 3, rule 13a): its bound verdict must say ``shapes_ok``,
every sentence a courtesy, a question, a stall or an approved KB text, and a
``general_question`` must quote an approved KB text (§6.2). One without it
never goes out on its own: it goes back to the owner's card
(``unapproved_statement``).

The red-line guard is reached through the small ``Guard`` interface.
``outbox`` registers the real one (``redlines.GUARD``, JV-13) when it is
imported; with no guard registered the binding check fails closed: AI-authored
text whose verdict is not bound to its exact body is never claimed
(``guard_unavailable``).
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Literal, Protocol

from miya.jarvis import policy
from miya.jarvis.policy import ChatFacts, Mode, PolicySnapshot, RuleSnapshot

Action = Literal["send", "hold", "redraft", "block", "expire", "cancel"]
GuardRole = Literal["reply", "stall", "owner", "kb"]

# The gate lines of §3.2, in order. tests/test_outbox.py proves evaluate()
# checks them in exactly this order.
LINES: Final[tuple[str, ...]] = (
    "kill_switch",
    "pause",
    "mode",
    "rules",
    "safety",
    "chat_state",
    "trigger",
    "caps",
    "guard",
    "payload",
    "quiet_hours",
)

AI: Final = "ai"
OWNER: Final = "owner"
AUTO_APPROVER: Final = "auto:policy"
GUARD_PASS: Final = "pass"
GUARD_UNAVAILABLE: Final = "guard_unavailable"
GUARD_FAILED: Final = "guard_failed"
# The flag of a guard that raised instead of answering.
GUARD_ERROR: Final = "guard_error"
PAYLOAD_NOT_TEXT: Final = "payload_not_text"
NO_TRIGGER: Final = "no_trigger"
SAFETY_UNREADABLE: Final = "safety_unreadable"
# The statement allowlist (§5.2 step 3): an AI text with a sentence that is
# not a courtesy, a question, a stall or an approved KB text. Its flag on the
# verdict, and the code of an autonomous row sent back to the card for it.
UNAPPROVED_STATEMENT: Final = "unapproved_statement"

# The longest body any channel takes in one message (Telegram's limit).
MAX_BODY_CHARS: Final = 4096
# How long "the owner is typing in this chat" holds an AI row back.
COMPOSING_HOLD: Final = timedelta(seconds=60)
# The owner-first and invalidation codes (JV-17): the owner wrote in the chat
# after the client's message, the client edited or deleted that message, or
# deleted an earlier message of the burst the draft answered.
OWNER_REPLIED: Final = "owner_replied"
OWNER_COMPOSING: Final = "owner_composing"
OWNER_SEEN: Final = "owner_seen"
AI_PAUSED: Final = "ai_paused"
TRIGGER_EDITED: Final = "trigger_edited"
TRIGGER_DELETED: Final = "trigger_deleted"
BURST_DELETED: Final = "burst_deleted"

# history ``field`` values the cards (JV-18, JV-20) write when a card shows a
# warning; an owner-authored row passes the matching line only after it.
WARNED_PERSONAL: Final = "warned_personal"
WARNED_SAFETY: Final = "warned_safety"

# Intents that may ever be sent on their own (§6.2); every other intent
# always goes to the owner.
AUTO_INTENTS: Final = frozenset(
    {"greeting", "info_gathering", "general_question", "stall"}
)
# Auto intents that go out on their own only when the text quotes an active
# knowledge-base entry verbatim (§6.2: «only when a KB entry matched»).
KB_ONLY_INTENTS: Final = frozenset({"general_question"})
KB_SHAPE: Final = "kb"


def body_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# --- the guard interface ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GuardResult:
    """A red-line verdict: ``pass`` or anything else (a block or escalation).

    ``shapes_ok`` is the statement allowlist (§5.2 step 3): True only when the
    red lines passed and every sentence has an approved shape (``shapes``,
    one name per sentence). It never blocks a draft; an autonomous row needs
    it. A guard that does not say is False: fail closed."""

    verdict: str
    flags: tuple[str, ...] = ()
    det_hits: tuple[str, ...] = ()
    model_verdict: str | None = None
    checker_version: str = ""
    shapes_ok: bool = False
    shapes: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.verdict == GUARD_PASS

    def bound_to(self, sha256: str) -> dict[str, object]:
        """``outbox_messages.guard``: the verdict bound to one exact body."""
        return {
            "verdict": self.verdict,
            "flags": list(self.flags),
            "det_hits": list(self.det_hits),
            "model_verdict": self.model_verdict,
            "body_sha256": sha256,
            "checker_version": self.checker_version,
            "shapes_ok": self.shapes_ok,
            "shapes": list(self.shapes),
        }


def shapes_passed(guard: Mapping[str, object]) -> bool:
    """Does a stored verdict (``outbox_messages.guard``) carry a passing
    statement-allowlist check? A verdict stored before the check existed, or
    by a guard that does not make it, does not."""
    return guard.get("verdict") == GUARD_PASS and guard.get("shapes_ok") is True


def auto_shapes_ok(intent: str | None, shapes_ok: bool, shapes: Sequence[str]) -> bool:
    """The statement allowlist as an autonomous send needs it: every sentence
    an approved shape and, for a ``KB_ONLY_INTENTS`` intent, a verbatim
    knowledge-base entry among them (§6.2). A general question answered with
    questions and courtesy alone matched no entry: it is the owner's card."""
    return shapes_ok and (intent not in KB_ONLY_INTENTS or KB_SHAPE in shapes)


class Guard(Protocol):
    """The deterministic red-line check (JV-13 ``redlines.check_outbound``).

    Pure: no I/O, no model. ``allowed_tokens`` are the GS codes and waybills
    seen verbatim in this chat's inbound messages, the only digits AI text
    may carry. ``other_names`` are people the text must not name unless they
    are in ``allowed_names`` (the counterparty, the owner's aliases), and
    ``client_script`` is the script the client wrote in (§5.2 step 2).
    ``approved_kb`` are the active owner-approved knowledge-base texts and
    ``stall_texts`` the owner's stall overrides: verbatim, they are approved
    statements for the allowlist (§5.2 step 3).
    """

    def check(
        self,
        body: str,
        *,
        role: GuardRole,
        allowed_tokens: frozenset[str],
        allowed_names: tuple[str, ...] = (),
        other_names: tuple[str, ...] = (),
        client_script: str | None = None,
        approved_kb: tuple[str, ...] = (),
        stall_texts: tuple[str, ...] = (),
    ) -> GuardResult: ...


_GUARD: list[Guard] = []


def register_guard(guard: Guard | None) -> None:
    """Install the red-line guard (JV-13), or remove it with None."""
    _GUARD.clear()
    if guard is not None:
        _GUARD.append(guard)


def registered_guard() -> Guard | None:
    return _GUARD[0] if _GUARD else None


def unavailable() -> GuardResult:
    """The verdict when no guard is registered: never a pass."""
    return GuardResult(verdict=GUARD_UNAVAILABLE)


def run_guard(
    guard: Guard | None,
    body: str,
    *,
    role: GuardRole,
    allowed_tokens: frozenset[str],
    allowed_names: tuple[str, ...] = (),
    other_names: tuple[str, ...] = (),
    client_script: str | None = None,
    approved_kb: tuple[str, ...] = (),
    stall_texts: tuple[str, ...] = (),
) -> GuardResult:
    """Run the guard; no guard, or one that raises, is a failed check. The
    error's class is kept in ``det_hits`` (``error:<class>``) for the log
    line the caller writes with the row's ids (rule 25)."""
    if guard is None:
        return unavailable()
    try:
        return guard.check(
            body,
            role=role,
            allowed_tokens=allowed_tokens,
            allowed_names=allowed_names,
            other_names=other_names,
            client_script=client_script,
            approved_kb=approved_kb,
            stall_texts=stall_texts,
        )
    except Exception as exc:
        return GuardResult(
            verdict=GUARD_FAILED,
            flags=(GUARD_ERROR,),
            det_hits=(f"error:{type(exc).__name__}",),
        )


def guard_role(origin: str, authored_by: str) -> GuardRole:
    if authored_by == OWNER:
        return "owner"
    return "stall" if origin == "stall" else "reply"


# --- the snapshot and the facts ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Row:
    """The outbox row as the gate sees it. ``body`` is typed ``object`` on
    purpose: the payload line proves it is plain text."""

    id: int
    channel: str
    chat_key: str
    origin: str
    authored_by: str
    body: object
    body_sha256: str | None
    guard: Mapping[str, object]
    created_at: datetime
    intent: str | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    not_before: datetime | None = None
    expires_at: datetime | None = None
    trigger_interaction_id: int | None = None
    history: tuple[Mapping[str, object], ...] = ()

    @property
    def ai(self) -> bool:
        # Anything but an explicit owner is MIYA's own text (authorship.author_of).
        return self.authored_by != OWNER

    @property
    def auto(self) -> bool:
        return self.ai and self.approved_by == AUTO_APPROVER


@dataclass(frozen=True, slots=True)
class Trigger:
    """The client message the row answers."""

    id: int
    occurred_at: datetime
    # The latest edit of it, and whether it was deleted (JV-17 records both).
    edited_at: datetime | None = None
    deleted: bool = False
    # In a group: was it addressed to the owner (meta.to_me, never to_me_maybe)?
    to_me: bool = False


@dataclass(frozen=True, slots=True)
class ChatState:
    owner_seen_at: datetime | None = None
    owner_composing_at: datetime | None = None
    owner_replied_at: datetime | None = None
    ai_paused_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class Caps:
    """Counts read in the claim transaction, with the claims made in it so far."""

    auto_sent: int = 0
    chat_ai_sent: int = 0
    channel_sent: int = 0
    last_send_at: datetime | None = None
    cost_over_cap: bool = False
    auto_cap: int = 0
    per_chat_cap: int = 0
    gap_seconds: int = 0
    # The platform's own daily cap (GMAIL_/WHATSAPP_DAILY_SEND_CAP); None: none.
    channel_cap: int | None = None
    # When the daily caps reset (the next midnight on the owner's clock).
    resets_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Context:
    now: datetime
    jarvis_enabled: bool
    channel_send_enabled: bool
    env_ceiling: Mode
    policies: Mapping[str, PolicySnapshot]
    chat: ChatFacts
    rules: tuple[RuleSnapshot, ...] = ()
    trigger: Trigger | None = None
    # The trigger, or inbound messages in this chat since it, that carry a
    # reply-blocking meta.safety verdict or a media.risk.
    unsafe_inbound_ids: tuple[int, ...] = ()
    # False when the shield verdicts of this chat cannot be read (a channel
    # whose ingest does not exist yet): the safety line fails closed.
    safety_readable: bool = True
    chat_state: ChatState | None = None
    caps: Caps = field(default_factory=Caps)
    guard: Guard | None = None
    allowed_tokens: frozenset[str] = frozenset()
    # The names the text may and may not carry, and the client's script.
    allowed_names: tuple[str, ...] = ()
    other_names: tuple[str, ...] = ()
    client_script: str | None = None
    # Approved texts for the statement allowlist: the active KB entries and
    # the owner's stall overrides (JV-15, JV-14).
    approved_kb: tuple[str, ...] = ()
    stall_texts: tuple[str, ...] = ()
    # The end of the quiet hours when ``now`` is inside them, else None.
    quiet_until: datetime | None = None
    auto_in_quiet_hours: bool = False


@dataclass(frozen=True, slots=True)
class Decision:
    action: Action
    line: str | None = None
    code: str | None = None
    retry_at: datetime | None = None
    # A fresh guard verdict when the line re-guarded the body.
    guard: GuardResult | None = None
    body_sha256: str | None = None

    @property
    def sends(self) -> bool:
        return self.action == "send"


def _hold(line: str, code: str, retry_at: datetime | None = None) -> Decision:
    return Decision("hold", line, code, retry_at)


def warned(history: Sequence[Mapping[str, object]], kind: str) -> object | None:
    """The value of the latest card warning of ``kind``, or None."""
    for entry in reversed(history):
        if entry.get("field") == kind:
            return entry.get("new")
    return None


# --- the lines ------------------------------------------------------------------------


def _kill_switch(row: Row, ctx: Context) -> Decision | None:
    if not ctx.jarvis_enabled:
        return _hold("kill_switch", "jarvis_disabled")
    if not ctx.channel_send_enabled:
        return _hold("kill_switch", "channel_disabled")
    return None


def _pause(row: Row, ctx: Context) -> Decision | None:
    stop = policy.stop_for(ctx.policies, row.channel, ctx.now)
    return _hold("pause", stop.code, stop.until) if stop is not None else None


def _mode(row: Row, ctx: Context) -> Decision | None:
    if ctx.env_ceiling == "off":
        return Decision("block", "mode", "mode_off")
    if not row.ai:
        return None
    mode = policy.effective_mode(
        ctx.policies, row.channel, row.intent, env_ceiling=ctx.env_ceiling
    )
    if row.auto:
        if row.intent not in AUTO_INTENTS:
            return Decision("redraft", "mode", "never_auto")
        if mode == "auto":
            return None
        if mode == "draft":
            return Decision("redraft", "mode", "demoted")
        return Decision("block", "mode", f"mode_{mode}")
    if mode in ("off", "shadow"):
        return Decision("block", "mode", f"mode_{mode}")
    return None


def _rules(row: Row, ctx: Context) -> Decision | None:
    if not row.ai:
        # The owner's own words may go to a personal chat, but only after
        # their card showed the warning; never to a hard exclusion.
        if (
            policy.never_rule(ctx.rules) is not None
            and warned(row.history, WARNED_PERSONAL) is None
        ):
            return Decision("redraft", "rules", "personal_warning")
        excluded = policy.hard_exclusion(ctx.chat)
        return Decision("block", "rules", excluded) if excluded is not None else None
    # One precedence for every AI row (policy.ai_refusal, §5.4): a personal
    # rule, the hard exclusions, groups off unless enabled and then only for
    # messages addressed to the owner.
    refused = policy.ai_refusal(
        ctx.chat,
        ctx.rules,
        addressed=ctx.trigger is not None and ctx.trigger.to_me,
    )
    if refused is not None:
        return Decision("block", "rules", refused)
    if row.auto and policy.rule_mode(ctx.rules) == policy.RULE_DRAFT:
        return Decision("redraft", "rules", "rule_draft")
    return None


def _safety(row: Row, ctx: Context) -> Decision | None:
    if not ctx.safety_readable:
        if row.ai:
            return Decision("block", "safety", SAFETY_UNREADABLE)
        return _hold("safety", SAFETY_UNREADABLE)
    if not ctx.unsafe_inbound_ids:
        return None
    if row.ai:
        return Decision("block", "safety", "unsafe_chat")
    latest = max(ctx.unsafe_inbound_ids)
    seen = warned(row.history, WARNED_SAFETY)
    if isinstance(seen, int) and seen >= latest:
        return None
    return Decision("redraft", "safety", "safety_warning")


def _chat_state(row: Row, ctx: Context) -> Decision | None:
    state = ctx.chat_state
    if not row.ai or state is None:
        return None
    since = ctx.trigger.occurred_at if ctx.trigger is not None else row.created_at
    if not row.auto and row.approved_at is not None:
        # The owner tapped send after looking at the chat: only what they
        # did after that tap counts against it.
        since = max(since, row.approved_at)
    if state.owner_replied_at is not None and state.owner_replied_at > since:
        return Decision("cancel", "chat_state", OWNER_REPLIED)
    composing = state.owner_composing_at
    if (
        composing is not None
        and composing > since
        and composing + COMPOSING_HOLD > ctx.now
    ):
        return _hold("chat_state", OWNER_COMPOSING, composing + COMPOSING_HOLD)
    if row.auto:
        if state.owner_seen_at is not None and state.owner_seen_at > since:
            return Decision("block", "chat_state", OWNER_SEEN)
        if state.ai_paused_until is not None and state.ai_paused_until > ctx.now:
            return Decision("block", "chat_state", AI_PAUSED)
    return None


def _trigger(row: Row, ctx: Context) -> Decision | None:
    trigger = ctx.trigger
    if row.ai and row.trigger_interaction_id is None:
        # Rule 17: the AI never starts a conversation.
        return Decision("block", "trigger", NO_TRIGGER)
    if row.ai and trigger is not None:
        if trigger.deleted:
            return Decision("cancel", "trigger", TRIGGER_DELETED)
        if trigger.edited_at is not None and trigger.edited_at >= row.created_at:
            return Decision("expire", "trigger", TRIGGER_EDITED)
    if row.ai and row.trigger_interaction_id is not None and trigger is None:
        return Decision("cancel", "trigger", "trigger_missing")
    if row.expires_at is not None and row.expires_at <= ctx.now:
        return Decision("expire", "trigger", "expired")
    if row.not_before is not None and row.not_before > ctx.now:
        return _hold("trigger", "not_before", row.not_before)
    return None


def _caps(row: Row, ctx: Context) -> Decision | None:
    caps = ctx.caps
    tomorrow = caps.resets_at
    if row.auto and caps.auto_sent >= caps.auto_cap:
        return _hold("caps", "auto_cap", tomorrow)
    if row.ai and caps.chat_ai_sent >= caps.per_chat_cap:
        return _hold("caps", "chat_cap", tomorrow)
    if caps.last_send_at is not None and caps.gap_seconds > 0:
        ready = caps.last_send_at + timedelta(seconds=caps.gap_seconds)
        if ready > ctx.now:
            return _hold("caps", "send_gap", ready)
    if caps.channel_cap is not None and caps.channel_sent >= caps.channel_cap:
        return _hold("caps", "channel_cap", tomorrow)
    if row.auto and caps.cost_over_cap:
        return _hold("caps", "cost_cap", tomorrow)
    return None


def _guard(row: Row, ctx: Context) -> Decision | None:
    if not isinstance(row.body, str):
        return None  # the payload line refuses it
    sha = body_hash(row.body)
    bound = row.body_sha256 == sha and row.guard.get("body_sha256") == sha
    leaned_on_kb = row.auto and KB_SHAPE in _shape_names(row.guard)
    if row.ai and bound and row.guard.get("verdict") == GUARD_PASS and not leaned_on_kb:
        if row.auto and not (
            shapes_passed(row.guard)
            and auto_shapes_ok(row.intent, True, _shape_names(row.guard))
        ):
            # Rule 13a: free-form statements go out only after an owner tap.
            return Decision("redraft", "guard", UNAPPROVED_STATEMENT)
        return None
    # An autonomous row approved for a KB text is guarded again with the KB
    # entries active now: a retired entry approves nothing (JV-15).
    if not row.ai and bound:
        return None
    result = run_guard(
        ctx.guard,
        row.body,
        role=guard_role(row.origin, row.authored_by),
        allowed_tokens=ctx.allowed_tokens,
        allowed_names=ctx.allowed_names,
        other_names=ctx.other_names,
        client_script=ctx.client_script,
        approved_kb=ctx.approved_kb,
        stall_texts=ctx.stall_texts,
    )
    if not row.ai:
        # The owner's words are flagged, never blocked; but a body that
        # changed after the tap goes back to the card (rule 16).
        guard = result if ctx.guard is not None else None
        return Decision("redraft", "guard", "body_changed", guard=guard, body_sha256=sha)
    if ctx.guard is None:
        return Decision("block", "guard", GUARD_UNAVAILABLE)
    if not result.passed:
        return Decision("block", "guard", GUARD_FAILED, guard=result, body_sha256=sha)
    if not row.auto:
        if not plain_text(row.body):
            return None  # never offered again: the payload line refuses it
        # The owner tapped send on another text: the new one goes back to
        # the card, with the fresh verdict bound to it (rule 16).
        return Decision("redraft", "guard", "body_changed", guard=result, body_sha256=sha)
    if not auto_shapes_ok(row.intent, result.shapes_ok, result.shapes):
        # Re-guarded, the red lines pass but a sentence is a free-form
        # statement (or a general question quotes no KB entry): back to the
        # owner's card with the fresh verdict bound.
        return Decision(
            "redraft", "guard", UNAPPROVED_STATEMENT, guard=result, body_sha256=sha
        )
    # An autonomous row re-guarded and passed: the claim binds the fresh
    # verdict to this body.
    return Decision("send", "guard", "reguarded", guard=result, body_sha256=sha)


def _shape_names(guard: Mapping[str, object]) -> tuple[str, ...]:
    shapes = guard.get("shapes")
    if not isinstance(shapes, list | tuple):
        return ()
    return tuple(s for s in shapes if isinstance(s, str))


def plain_text(body: object) -> bool:
    """Rule 27: a non-blank string of printable text, nothing else."""
    if not isinstance(body, str) or not body.strip() or len(body) > MAX_BODY_CHARS:
        return False
    return all(ch in "\n\t" or unicodedata.category(ch) != "Cc" for ch in body)


def _payload(row: Row, ctx: Context) -> Decision | None:
    if not plain_text(row.body):
        return Decision("block", "payload", PAYLOAD_NOT_TEXT)
    return None


def _quiet_hours(row: Row, ctx: Context) -> Decision | None:
    if row.auto and ctx.quiet_until is not None and not ctx.auto_in_quiet_hours:
        return _hold("quiet_hours", "quiet_hours", ctx.quiet_until)
    return None


_CHECKS = (
    ("kill_switch", _kill_switch),
    ("pause", _pause),
    ("mode", _mode),
    ("rules", _rules),
    ("safety", _safety),
    ("chat_state", _chat_state),
    ("trigger", _trigger),
    ("caps", _caps),
    ("guard", _guard),
    ("payload", _payload),
    ("quiet_hours", _quiet_hours),
)


def evaluate(row: Row, ctx: Context) -> Decision:
    """Every gate line of §3.2, in order; the first objection decides."""
    reguarded: Decision | None = None
    for _name, check in _CHECKS:
        decision = check(row, ctx)
        if decision is None:
            continue
        if decision.action == "send":
            reguarded = decision
            continue
        return decision
    if reguarded is not None:
        return reguarded
    return Decision("send")


__all__ = [
    "AI_PAUSED",
    "AUTO_INTENTS",
    "BURST_DELETED",
    "COMPOSING_HOLD",
    "GUARD_ERROR",
    "GUARD_FAILED",
    "GUARD_PASS",
    "GUARD_UNAVAILABLE",
    "KB_ONLY_INTENTS",
    "KB_SHAPE",
    "LINES",
    "MAX_BODY_CHARS",
    "NO_TRIGGER",
    "OWNER_COMPOSING",
    "OWNER_REPLIED",
    "OWNER_SEEN",
    "PAYLOAD_NOT_TEXT",
    "SAFETY_UNREADABLE",
    "TRIGGER_DELETED",
    "TRIGGER_EDITED",
    "UNAPPROVED_STATEMENT",
    "WARNED_PERSONAL",
    "WARNED_SAFETY",
    "Action",
    "Caps",
    "ChatState",
    "Context",
    "Decision",
    "Guard",
    "GuardResult",
    "GuardRole",
    "Row",
    "Trigger",
    "auto_shapes_ok",
    "body_hash",
    "evaluate",
    "guard_role",
    "plain_text",
    "register_guard",
    "registered_guard",
    "run_guard",
    "shapes_passed",
    "unavailable",
    "warned",
]

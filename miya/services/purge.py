"""Purge tooling (spec §10): `/unut <odam | chat | sana oralig'i>`.

Deleting is the one operation MIYA can do that the owner cannot undo, so the
flow is deliberately two-step: build a **plan** that says exactly what would
disappear, show it, and only then execute.

Everything derived from an interaction hangs off ``source_interaction_id`` with
``ON DELETE CASCADE``, so removing the interactions removes the debts,
promises, transactions, events, tasks and memories that came out of them. Media
files on disk are unlinked in the same pass, each with the JSON sidecar that
sits beside it — a purge that left the audio behind would not be a purge, and
one that left the sidecar behind would leave the purged person's name and phone
number on disk, which is worse.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.models import (
    ChatMonitor,
    Claim,
    ConversationWindow,
    Debt,
    DebtPayment,
    Event,
    Interaction,
    Memory,
    Passage,
    Person,
    Promise,
    Task,
    Transaction,
)
from miya.services import call_recordings, claims, windows
from miya.services.people import best_match
from miya.services.queries import day_bounds
from miya.services.text import (
    alias_pattern,
    name_pattern,
    normalise_for_search,
    to_prefix_tsquery,
)

log = logging.getLogger(__name__)

REDACTED = "[o'chirilgan]"
# A pending claim that names someone this closely is about them.
CLAIM_NAME_SCORE = 85
# Media keys that hold a filesystem path.
_PATH_KEYS = ("path", "audio_path")
# …and the key that holds a list of them: second copies of a recording we
# already had, noted by the sweep so they stay purgeable.
_PATH_LIST_KEYS = ("duplicate_paths",)


@dataclass(slots=True)
class PurgePlan:
    """What a purge would remove. Nothing is deleted until `execute` runs."""

    kind: str  # person | chat | range
    label: str  # human description, already safe to show
    interaction_ids: list[int] = field(default_factory=list)
    person_id: int | None = None
    tg_chat_id: int | None = None
    date_from: date | None = None
    date_to: date | None = None
    counts: dict[str, int] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)
    # Windows holding the rendered transcript of the purged messages. They must
    # go too: a pending one would be re-extracted hours later and recreate the
    # person and debts the owner just deleted.
    window_ids: list[int] = field(default_factory=list)
    # A person purge reaches past their own messages (WP-61): facts about
    # them, claims naming them, their lines in group windows, the names in
    # kept payments, and other people's profiles that mention them.
    memory_ids: list[int] = field(default_factory=list)
    claim_ids: list[int] = field(default_factory=list)
    rerender_window_ids: list[int] = field(default_factory=list)
    scrub_transaction_ids: list[int] = field(default_factory=list)
    profile_stale_person_ids: list[int] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    # What stays, said out loud in the preview.
    kept: dict[str, int] = field(default_factory=dict)

    def is_empty(self) -> bool:
        if self.kind == "person":
            return False  # the card itself is something to forget
        return (
            not self.interaction_ids
            and not self.counts.get("debts")
            and not self.memory_ids
        )


@dataclass(slots=True)
class PurgeResult:
    interactions: int = 0
    memories: int = 0
    claims: int = 0
    files_deleted: int = 0
    person_deleted: bool = False


async def _count(session: AsyncSession, model, column, ids: list[int]) -> int:
    if not ids:
        return 0
    return (
        await session.scalar(
            sa.select(sa.func.count()).select_from(model).where(column.in_(ids))
        )
        or 0
    )


async def _collect(session: AsyncSession, plan: PurgePlan) -> PurgePlan:
    """Fill in the derived-row counts and media paths for a plan's interactions."""
    ids = plan.interaction_ids
    plan.counts = {
        "interactions": len(ids),
        "debts": await _count(session, Debt, Debt.source_interaction_id, ids),
        "promises": await _count(session, Promise, Promise.source_interaction_id, ids),
        "transactions": await _count(
            session, Transaction, Transaction.source_interaction_id, ids
        ),
        "events": await _count(session, Event, Event.source_interaction_id, ids),
        "tasks": await _count(session, Task, Task.source_interaction_id, ids),
        "memories": await _count(session, Memory, Memory.source_interaction_id, ids),
    }

    if ids:
        media_rows = await session.scalars(
            sa.select(Interaction.media).where(
                Interaction.id.in_(ids), Interaction.media.isnot(None)
            )
        )
        files: list[str] = []
        for media in media_rows:
            for key in _PATH_KEYS:
                value = (media or {}).get(key)
                if value:
                    files.append(value)
            for key in _PATH_LIST_KEYS:
                values = (media or {}).get(key) or []
                if isinstance(values, list):
                    files.extend(str(v) for v in values if v)
        plan.files = files

        window_ids = await session.scalars(
            sa.select(Interaction.window_id)
            .where(Interaction.id.in_(ids), Interaction.window_id.isnot(None))
            .distinct()
        )
        plan.window_ids = [w for w in window_ids if w]
    return plan


def person_names(person: Person) -> list[str]:
    """Every name the person goes by — the display name and the aliases, not
    the @handles or codes."""
    names = [person.display_name, *(person.aliases or [])]
    return [n for n in dict.fromkeys(n.strip() for n in names if n) if n]


async def plan_person(session: AsyncSession, person: Person) -> PurgePlan:
    """Forget a person: their messages, their debts, everything derived, and
    everything that names them — or say what stays (WP-61)."""
    ids = list(
        await session.scalars(
            sa.select(Interaction.id).where(Interaction.person_id == person.id)
        )
    )
    plan = PurgePlan(
        kind="person",
        label=person.display_name,
        interaction_ids=ids,
        person_id=person.id,
        names=person_names(person),
    )
    await _collect(session, plan)
    pattern = name_pattern(plan.names)

    # (b) facts: about them, from their messages, or naming them unowned.
    memory_ids = set(
        await session.scalars(sa.select(Memory.id).where(Memory.person_id == person.id))
    )
    if ids:
        memory_ids |= set(
            await session.scalars(
                sa.select(Memory.id).where(Memory.source_interaction_id.in_(ids))
            )
        )
    if pattern is not None:
        for memory_id, content in await session.execute(
            sa.select(Memory.id, Memory.content).where(Memory.person_id.is_(None))
        ):
            if pattern.search(normalise_for_search(content or "")):
                memory_ids.add(memory_id)
    plan.memory_ids = sorted(memory_ids)

    # (c) pending claims about them, resolved or only named.
    claim_ids = []
    for claim in await session.scalars(
        sa.select(Claim).where(
            Claim.state == claims.PENDING,
            sa.or_(Claim.person_id == person.id, Claim.person_id.is_(None)),
        )
    ):
        if claim.person_id == person.id or (
            claim.person_name
            and best_match(claim.person_name, [person])[1] >= CLAIM_NAME_SCORE
        ):
            claim_ids.append(claim.id)
    plan.claim_ids = sorted(claim_ids)

    # (d) windows: a window of their own goes; a group window loses their
    # lines and is re-rendered from the rest.
    person_windows = set(
        await session.scalars(
            sa.select(ConversationWindow.id).where(
                ConversationWindow.person_id == person.id
            )
        )
    )
    plan.rerender_window_ids = sorted({*plan.window_ids, *person_windows})
    plan.window_ids = []
    group_lines = 0
    if ids:
        group_lines = (
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(Interaction)
                .join(ConversationWindow, ConversationWindow.id == Interaction.window_id)
                .where(
                    Interaction.id.in_(ids),
                    ConversationWindow.person_id.is_(None),
                    sa.func.coalesce(Interaction.meta["kind"].astext, "") != "window",
                )
            )
            or 0
        )

    # (e) payments with them whose source stays: the amount stays, the
    # name goes.
    kept_txns = sa.select(Transaction.id).where(
        Transaction.counterparty_person_id == person.id
    )
    if ids:
        kept_txns = kept_txns.where(
            sa.or_(
                Transaction.source_interaction_id.is_(None),
                Transaction.source_interaction_id.not_in(ids),
            )
        )
    plan.scrub_transaction_ids = sorted(await session.scalars(kept_txns))

    # (f) other people's profiles written with them in mind.
    stale = []
    if pattern is not None:
        for other_id, notes in await session.execute(
            sa.select(Person.id, Person.notes).where(
                Person.id != person.id, Person.notes.isnot(None)
            )
        ):
            if pattern.search(normalise_for_search(notes or "")):
                stale.append(other_id)
    plan.profile_stale_person_ids = sorted(stale)

    # (g) what stays.
    mentions = 0
    terms = (t for n in plan.names for t in normalise_for_search(n).split())
    query = to_prefix_tsquery(list(dict.fromkeys(terms)))
    if query:
        mentions_q = (
            sa.select(sa.func.count(sa.distinct(Passage.interaction_id)))
            .select_from(Passage)
            .where(Passage.search_tsv.op("@@")(sa.func.to_tsquery("simple", query)))
        )
        if ids:
            mentions_q = mentions_q.where(Passage.interaction_id.not_in(ids))
        mentions = await session.scalar(mentions_q) or 0
    plan.kept = {
        "transactions_kept": len(plan.scrub_transaction_ids),
        "mentions_kept": mentions,
    }

    plan.counts["memories"] = len(plan.memory_ids)
    plan.counts["claims"] = len(plan.claim_ids)
    plan.counts["group_lines"] = group_lines
    # Debts and promises hang off the person too, not only off an interaction.
    plan.counts["debts"] = (
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(Debt)
            .where(Debt.person_id == person.id)
        )
        or 0
    )
    plan.counts["promises"] = (
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(Promise)
            .where(Promise.person_id == person.id)
        )
        or 0
    )
    plan.counts["person_card"] = 1
    return plan


async def plan_chat(session: AsyncSession, tg_chat_id: int) -> PurgePlan:
    ids = list(
        await session.scalars(
            sa.select(Interaction.id).where(Interaction.tg_chat_id == tg_chat_id)
        )
    )
    monitor = await session.scalar(
        sa.select(ChatMonitor).where(ChatMonitor.tg_chat_id == tg_chat_id)
    )
    plan = PurgePlan(
        kind="chat",
        label=(monitor.title if monitor and monitor.title else str(tg_chat_id)),
        interaction_ids=ids,
        tg_chat_id=tg_chat_id,
    )
    return await _collect(session, plan)


async def plan_range(session: AsyncSession, date_from: date, date_to: date) -> PurgePlan:
    start, _ = day_bounds(date_from)
    _, end = day_bounds(date_to)
    ids = list(
        await session.scalars(
            sa.select(Interaction.id).where(
                Interaction.occurred_at >= start, Interaction.occurred_at < end
            )
        )
    )
    plan = PurgePlan(
        kind="range",
        label=f"{date_from.isoformat()} … {date_to.isoformat()}",
        interaction_ids=ids,
        date_from=date_from,
        date_to=date_to,
    )
    await _collect(session, plan)
    # /eslab facts have no interaction to cascade from; their date decides.
    memory_ids = set(
        await session.scalars(
            sa.select(Memory.id).where(
                Memory.source_interaction_id.is_(None),
                Memory.occurred_at >= start,
                Memory.occurred_at < end,
            )
        )
    )
    if ids:
        memory_ids |= set(
            await session.scalars(
                sa.select(Memory.id).where(Memory.source_interaction_id.in_(ids))
            )
        )
    plan.memory_ids = sorted(memory_ids)
    plan.counts["memories"] = len(plan.memory_ids)
    # Repayments dated in the range on a debt that stays are kept: removing
    # them would make the balance wrong.
    payments = (
        sa.select(sa.func.count())
        .select_from(DebtPayment)
        .join(Debt, Debt.id == DebtPayment.debt_id)
        .where(DebtPayment.paid_at >= start, DebtPayment.paid_at < end)
    )
    if ids:
        payments = payments.where(
            sa.or_(
                Debt.source_interaction_id.is_(None),
                Debt.source_interaction_id.not_in(ids),
            )
        )
    plan.kept = {"payments_in_range": await session.scalar(payments) or 0}
    return plan


def _unlink(paths: list[str]) -> int:
    """Remove media files and their sidecars.

    A missing file is already in the desired state. The sidecar an upload
    writes next to a call recording holds the counterparty's name, their phone
    number, the device id and the call id — everything the audio does not say
    out loud — so a purge that took only the audio would leave the person the
    owner asked to forget named on disk forever.
    """
    deleted = 0
    for raw in paths:
        path = Path(raw)
        try:
            path.unlink()
            deleted += 1
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("could not delete media file %s", path, exc_info=True)
        try:
            call_recordings.sidecar_path(path).unlink(missing_ok=True)
        except OSError:
            log.warning("could not delete sidecar for %s", path, exc_info=True)
    return deleted


async def execute(session: AsyncSession, plan: PurgePlan) -> PurgeResult:
    """Carry out a plan. Rows go first; files only after the delete succeeds."""
    result = PurgeResult(
        interactions=len(plan.interaction_ids),
        memories=len(plan.memory_ids),
        claims=len(plan.claim_ids),
    )

    if plan.claim_ids:
        await session.execute(sa.delete(Claim).where(Claim.id.in_(plan.claim_ids)))
    if plan.memory_ids:
        await session.execute(sa.delete(Memory).where(Memory.id.in_(plan.memory_ids)))
    if plan.interaction_ids:
        # Cascades take the derived rows (debts, promises, memories, …).
        await session.execute(
            sa.delete(Interaction).where(Interaction.id.in_(plan.interaction_ids))
        )

    if plan.window_ids:
        # A surviving window is a second copy of the conversation, and a
        # pending one would be extracted hours later — recreating the person
        # and the debts the owner just asked to forget, and billing him for it.
        await session.execute(
            sa.delete(ConversationWindow).where(
                ConversationWindow.id.in_(plan.window_ids)
            )
        )

    if plan.kind == "chat" and plan.tg_chat_id is not None:
        await session.execute(
            sa.delete(ConversationWindow).where(
                ConversationWindow.tg_chat_id == plan.tg_chat_id
            )
        )
    elif plan.kind == "range" and plan.date_from and plan.date_to:
        start, _ = day_bounds(plan.date_from)
        _, end = day_bounds(plan.date_to)
        await session.execute(
            sa.delete(ConversationWindow).where(
                ConversationWindow.ended_at >= start,
                ConversationWindow.ended_at < end,
            )
        )
    elif plan.kind == "person" and plan.person_id is not None:
        await _forget_person(session, plan)
        result.person_deleted = True

    await session.flush()
    result.files_deleted = _unlink(plan.files)
    log.warning(
        "purged %s %r: %d interactions, %d files",
        plan.kind,
        plan.label,
        result.interactions,
        result.files_deleted,
    )
    return result


def _redact(pattern, text: str | None) -> str | None:
    if pattern is None or not text:
        return text
    return pattern.sub(REDACTED, text)


async def _forget_person(session: AsyncSession, plan: PurgePlan) -> None:
    """The person-only steps, after their rows are gone."""
    await session.flush()
    pattern = alias_pattern(tuple(plan.names))
    for window_id in plan.rerender_window_ids:
        window = await session.get(ConversationWindow, window_id)
        if window is None or not await windows.rerender(session, window):
            continue
        for row in await session.scalars(
            sa.select(Interaction).where(
                Interaction.window_id == window.id,
                Interaction.meta["kind"].astext == "window",
            )
        ):
            row.summary = _redact(pattern, row.summary)
    if plan.scrub_transaction_ids:
        for txn in await session.scalars(
            sa.select(Transaction).where(Transaction.id.in_(plan.scrub_transaction_ids))
        ):
            txn.description = _redact(pattern, txn.description)
    if plan.profile_stale_person_ids:
        await session.execute(
            sa.update(Person)
            .where(Person.id.in_(plan.profile_stale_person_ids))
            .values(profile_updated_at=None)
        )
    # Deleting the person cascades to their debts and promises, including
    # ones that came from a call the owner is otherwise keeping.
    await session.execute(sa.delete(Person).where(Person.id == plan.person_id))


def parse_range(text: str) -> tuple[date, date] | None:
    """Accept `2026-08-01..2026-08-15` or a single `2026-08-01`."""
    text = text.strip()
    if ".." in text:
        left, _, right = text.partition("..")
    else:
        left = right = text
    try:
        start = date.fromisoformat(left.strip())
        end = date.fromisoformat(right.strip())
    except ValueError:
        return None
    if end < start:
        start, end = end, start
    return start, end


def today() -> date:
    return datetime.now(settings.tz).date()

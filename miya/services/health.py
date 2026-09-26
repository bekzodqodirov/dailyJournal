"""Self-monitoring (build step 5): heartbeats, one status, alerts with memory.

MIYA is four processes and a scheduler that can each die quietly. The owner
must learn when that happens and must know what to type; this module is the
shared core of that:

* **Heartbeats** — every process and every scheduler job upserts one row in
  ``heartbeats`` through :func:`beat`; :func:`beats` reads them back.
* **Status** — :func:`gather` collects the heartbeats, the database, the
  disk, the newest backup, the extraction queue and the spend into one
  :class:`Status`, which ``/holat`` renders and the worker's health job
  judges. It is bounded (a handful of indexed counts) and tolerates an empty
  database and a missing backup directory.
* **Problems** — :func:`problems` is pure: Status in, an ordered list of
  :class:`Problem` out, each with its Uzbek text and the remedy.
* **Alert memory** — :func:`alerts_due` and the ``mark_*`` helpers keep the
  same problem from being repeated more often than ``ALERT_REPEAT_HOURS``
  and produce one recovery notice when it clears. The memory is the house
  ledger, ``reminder_log``, so the bot's watchdog and the worker's health
  job share it: whichever of them is alive speaks, and neither repeats the
  other.

Nothing here sends anything. The worker and the bot decide *how* (quiet
hours, plain-text fallback) and record only what was actually delivered.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from miya.bot.formatting import age_label, escape, usd
from miya.config import settings
from miya.db.enums import InteractionSource, WindowStatus
from miya.db.models import (
    Claim,
    ConversationWindow,
    Heartbeat,
    Interaction,
    ReminderLog,
    UsageLog,
)
from miya.services import backup, queries

log = logging.getLogger(__name__)

COMPONENTS = ("bot", "worker", "userbot", "api")
JOB_PREFIX = "job:"
# The heartbeat the worker writes after the nightly file went (or failed to
# go) to Telegram: detail {"sent": bool, "sent_at": iso, "path": str, ...}.
BACKUP_COMPONENT = "backup"

ALERT_KIND = "alert:"
RECOVERED_KIND = "recovered:"

Severity = Literal["critical", "warning"]
PROBLEM_KEYS = (
    "db_down",
    "disk_low",
    "worker_silent",
    "userbot_silent",
    "api_silent",
    "backup_unconfigured",
    "backup_stale",
    "backup_failed",
    "anthropic_failing",
    "review_backlog",
    "spend_high",
)
# Flagged inputs pile up quietly; past this many the pile is itself a fault.
REVIEW_BACKLOG_THRESHOLD = 20
# No successful Anthropic call for this long while windows wait = failing.
ANTHROPIC_SILENCE = timedelta(hours=3)
# A failed window counts against Anthropic for this long. Failed windows are
# kept forever (the text is the owner's), so an unbounded count would nag
# about one bad night for months.
FAILED_WINDOW_HORIZON = timedelta(hours=24)

_GB = 1024**3
_MB = 1024**2


# --- heartbeats -------------------------------------------------------------


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(settings.tz)


async def beat(
    session: AsyncSession,
    component: str,
    *,
    detail: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    """Record that ``component`` is alive now. One upsert; ``detail`` replaces
    the stored dict whole (an absent detail stores ``{}``)."""
    stmt = pg_insert(Heartbeat).values(
        component=component, last_seen_at=_now(now), detail=dict(detail or {})
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[Heartbeat.component],
        set_={
            "last_seen_at": stmt.excluded.last_seen_at,
            "detail": stmt.excluded.detail,
        },
    )
    await session.execute(stmt)


async def beats(session: AsyncSession) -> dict[str, Heartbeat]:
    """Every heartbeat row, keyed by component — fresh from the database,
    not from the identity map, so a beat in the same session is visible."""
    rows = await session.scalars(
        sa.select(Heartbeat).execution_options(populate_existing=True)
    )
    return {row.component: row for row in rows}


@dataclass(slots=True)
class Component:
    """One process or job as seen through its heartbeat.

    ``stale`` is "older than the threshold" — or never seen at all. The
    userbot's threshold is USERBOT_STALE_MINUTES, everything else's (jobs
    included) HEARTBEAT_STALE_MINUTES; for a job that runs nightly the flag
    is therefore only meaningful next to the job's own interval. ``disabled``
    is a userbot switched off with USERBOT_ENABLED=false: it beats once with
    ``{"enabled": false}`` and then goes quiet on purpose, which is not a
    fault and is never alerted.
    """

    name: str
    last_seen_at: datetime | None
    age: timedelta | None
    detail: dict[str, Any]
    stale: bool
    disabled: bool


def stale_after(name: str) -> timedelta:
    minutes = (
        settings.userbot_stale_minutes
        if name == "userbot"
        else settings.heartbeat_stale_minutes
    )
    return timedelta(minutes=minutes)


def component_of(
    name: str, row: Heartbeat | None, *, now: datetime | None = None
) -> Component:
    """Judge one heartbeat row (or its absence) against its threshold."""
    now = _now(now)
    if row is None:
        return Component(name, None, None, {}, stale=True, disabled=False)
    age = max(now - row.last_seen_at, timedelta(0))
    detail = dict(row.detail or {})
    return Component(
        name,
        row.last_seen_at,
        age,
        detail,
        stale=age > stale_after(name),
        disabled=detail.get("enabled") is False,
    )


# --- status -------------------------------------------------------------------


@dataclass(slots=True)
class BackupInfo:
    """The newest ``*.dump.age`` on disk and whether it reached Telegram."""

    path: Path | None
    created_at: datetime | None
    size: int | None
    sent_to_telegram: bool
    sent_at: datetime | None
    # Older than BACKUP_MAX_AGE_HOURS, or missing, while a recipient is set.
    stale: bool
    configured: bool
    # The worker recorded that sending *this* file to Telegram failed.
    send_failed: bool = False
    send_error: str | None = None


@dataclass(slots=True)
class Status:
    now: datetime
    components: dict[str, Component]
    # The ``job:*`` rows, keyed by the bare scheduler job id.
    jobs: dict[str, Component]
    userbot_last_message_at: datetime | None
    db_ok: bool
    db_size_bytes: int | None
    disk_free_bytes: int | None
    disk_total_bytes: int | None
    disk_low: bool
    backup: BackupInfo
    windows_pending: int
    windows_submitted: int
    # Failed within FAILED_WINDOW_HORIZON, not all time.
    windows_failed: int
    needs_review: int
    claims_pending: int
    cost_today_usd: Decimal
    cost_month_usd: Decimal
    anthropic_last_ok_at: datetime | None
    anthropic_failing: bool
    # The companion app's last accepted batch, when a phone ever uploaded.
    # Informational only: a phone-less install is healthy, so no Problem
    # and no staleness alert ever comes from this row.
    phone: Component | None = None
    # Money texts waiting in /tekshir's money block (WP-14): shown, never
    # counted into the backlog alarm — they are the owner's call, not a fault.
    money_review: int = 0

    @classmethod
    def unreachable(cls, now: datetime) -> Status:
        """What /holat can still say when the database does not answer."""
        free, total = _disk_usage()
        return cls(
            now=now,
            components={},
            jobs={},
            userbot_last_message_at=None,
            db_ok=False,
            db_size_bytes=None,
            disk_free_bytes=free,
            disk_total_bytes=total,
            disk_low=_is_disk_low(free),
            backup=BackupInfo(
                path=None,
                created_at=None,
                size=None,
                sent_to_telegram=False,
                sent_at=None,
                stale=False,
                configured=bool(settings.backup_age_recipient),
            ),
            windows_pending=0,
            windows_submitted=0,
            windows_failed=0,
            needs_review=0,
            claims_pending=0,
            cost_today_usd=Decimal(0),
            cost_month_usd=Decimal(0),
            anthropic_last_ok_at=None,
            anthropic_failing=False,
        )


def _is_disk_low(free: int | None) -> bool:
    return free is not None and free < settings.disk_min_free_gb * _GB


def _disk_usage() -> tuple[int | None, int | None]:
    """Free and total bytes of the data volume, else of "/", else unknown."""
    for candidate in (Path(settings.backup_dir).parent, Path("/")):
        try:
            usage = shutil.disk_usage(candidate)
        except OSError:
            continue
        return usage.free, usage.total
    return None, None


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=settings.tz)


async def _backup_info(rows: dict[str, Heartbeat], now: datetime) -> BackupInfo:
    configured = bool(settings.backup_age_recipient)
    path = await backup.newest_backup()
    created_at = backup.backup_stamp(path) if path else None
    size: int | None = None
    if path is not None:
        try:
            size = path.stat().st_size
        except OSError:
            size = None
    row = rows.get(BACKUP_COMPONENT)
    detail = dict((row.detail if row is not None else None) or {})
    about_this_file = path is not None and detail.get("path") == str(path)
    sent = about_this_file and detail.get("sent") is True
    send_failed = about_this_file and detail.get("sent") is False
    max_age = timedelta(hours=settings.backup_max_age_hours)
    stale = configured and (created_at is None or now - created_at > max_age)
    return BackupInfo(
        path=path,
        created_at=created_at,
        size=size,
        sent_to_telegram=sent,
        sent_at=_parse_iso(detail.get("sent_at")) if sent else None,
        stale=stale,
        configured=configured,
        send_failed=send_failed,
        send_error=str(detail["error"]) if send_failed and detail.get("error") else None,
    )


async def _window_counts(session: AsyncSession, now: datetime) -> tuple[int, int, int]:
    rows = await session.execute(
        sa.select(ConversationWindow.status, sa.func.count())
        .where(
            ConversationWindow.status.in_([WindowStatus.pending, WindowStatus.submitted])
        )
        .group_by(ConversationWindow.status)
    )
    by_status = {status: int(count) for status, count in rows.all()}
    failed = await session.scalar(
        sa.select(sa.func.count())
        .select_from(ConversationWindow)
        .where(
            ConversationWindow.status == WindowStatus.failed,
            ConversationWindow.created_at >= now - FAILED_WINDOW_HORIZON,
        )
    )
    return (
        by_status.get(WindowStatus.pending, 0),
        by_status.get(WindowStatus.submitted, 0),
        int(failed or 0),
    )


async def gather(session: AsyncSession, *, now: datetime | None = None) -> Status:
    """Everything ``/holat`` shows, in one pass of bounded queries.

    Works on an empty database and without a backup directory. If the
    database itself does not answer, ``db_ok`` is False and every
    database-derived field is empty — the disk and the backup file are still
    read, since those are what the owner can act on.
    """
    now = _now(now)
    free, total = _disk_usage()
    disk_low = _is_disk_low(free)

    rows: dict[str, Heartbeat] = {}
    db_ok = True
    try:
        await session.execute(sa.text("SELECT 1"))
    except Exception:
        log.warning("health: the database did not answer", exc_info=True)
        db_ok = False

    db_size = None
    userbot_last_message_at = None
    pending = submitted = failed = 0
    needs_review = claims_pending = money_review = 0
    cost_today = cost_month = Decimal("0")
    anthropic_last_ok_at = None
    if db_ok:
        rows = await beats(session)
        db_size = await session.scalar(
            sa.select(sa.func.pg_database_size(sa.func.current_database()))
        )
        userbot_last_message_at = await session.scalar(
            sa.select(sa.func.max(Interaction.occurred_at)).where(
                Interaction.source == InteractionSource.telegram_userbot
            )
        )
        pending, submitted, failed = await _window_counts(session, now)
        needs_review = int(
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(Interaction)
                .where(Interaction.needs_review.is_(True))
                .where(Interaction.source.notin_(queries.MONEY_SOURCES))
            )
            or 0
        )
        money_review = await queries.money_review_count(session)
        claims_pending = int(
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(Claim)
                .where(Claim.state == "pending")
            )
            or 0
        )
        today = now.astimezone(settings.tz).date()
        usage = await queries.usage_summary(session, today.replace(day=1), today)
        cost_today, cost_month = usage.today_usd, usage.total_usd
        anthropic_last_ok_at = await session.scalar(
            sa.select(sa.func.max(UsageLog.created_at)).where(
                UsageLog.provider == "anthropic"
            )
        )

    anthropic_silent = (
        anthropic_last_ok_at is None or now - anthropic_last_ok_at > ANTHROPIC_SILENCE
    )
    return Status(
        now=now,
        components={
            name: component_of(name, rows.get(name), now=now) for name in COMPONENTS
        },
        phone=(
            component_of("phone", rows["phone"], now=now) if "phone" in rows else None
        ),
        jobs={
            key[len(JOB_PREFIX) :]: component_of(key[len(JOB_PREFIX) :], row, now=now)
            for key, row in rows.items()
            if key.startswith(JOB_PREFIX)
        },
        userbot_last_message_at=userbot_last_message_at,
        db_ok=db_ok,
        db_size_bytes=int(db_size) if db_size is not None else None,
        disk_free_bytes=free,
        disk_total_bytes=total,
        disk_low=disk_low,
        backup=await _backup_info(rows, now),
        windows_pending=pending,
        windows_submitted=submitted,
        windows_failed=failed,
        needs_review=needs_review,
        claims_pending=claims_pending,
        money_review=money_review,
        cost_today_usd=cost_today,
        cost_month_usd=cost_month,
        anthropic_last_ok_at=anthropic_last_ok_at,
        anthropic_failing=failed > 0 or (pending > 0 and anthropic_silent),
    )


# --- problems ---------------------------------------------------------------


@dataclass(slots=True)
class Problem:
    key: str
    severity: Severity
    # Uzbek, HTML-escaped, and it names the remedy.
    text: str


def size_label(size: int | None) -> str:
    """'512 MB' / '3.4 GB' — the owner's units, not bytes."""
    if size is None:
        return "?"
    if size >= _GB:
        return f"{size / _GB:.1f} GB".replace(".0 GB", " GB")
    if size >= _MB or size == 0:
        return f"{size // _MB} MB"
    return f"{size // 1024} KB"


def _since(component: Component) -> str:
    """'25 daqiqadan beri jim' / 'hali bir marta ham urmagan'."""
    if component.age is None:
        return "hali bir marta ham xabar bermagan"
    return f"{age_label(component.age)}dan beri jim"


def problems(status: Status) -> list[Problem]:
    """What is wrong, worst first. Pure — Status in, list out.

    When the database itself is down only that (and the disk) is reported:
    every other judgement rests on rows that could not be read.
    """
    found: list[Problem] = []
    backup_cmd = "<code>make backup</code>"

    if not status.db_ok:
        found.append(
            Problem(
                "db_down",
                "critical",
                "❌ Baza javob bermayapti — hech narsa yozilmayapti va o'qilmayapti. "
                "Serverda: <code>docker compose ps</code>, "
                "<code>docker compose logs db</code>, keyin <code>make up</code>.",
            )
        )
    if status.disk_low:
        found.append(
            Problem(
                "disk_low",
                "critical",
                f"❌ Diskda joy kam: {escape(size_label(status.disk_free_bytes))} bo'sh "
                f"(chegara {settings.disk_min_free_gb:g} GB). To'lsa hamma narsa "
                "to'xtaydi. Serverda: <code>docker system prune</code>, eski "
                "zaxiralarni va <code>/data/call_recordings</code>'ni tekshir.",
            )
        )
    if not status.db_ok:
        return found

    worker = status.components["worker"]
    if worker.stale:
        found.append(
            Problem(
                "worker_silent",
                "warning",
                f"❌ Rejalashtiruvchi (worker) {escape(_since(worker))} — eslatmalar, "
                "hisobot va zaxira nusxa to'xtab turibdi. Serverda: "
                "<code>docker compose restart worker</code>, keyin "
                "<code>make worker</code> bilan logni ko'r.",
            )
        )
    userbot = status.components["userbot"]
    if userbot.stale and not userbot.disabled:
        found.append(
            Problem(
                "userbot_silent",
                "warning",
                f"❌ Telegram o'quvchi {escape(_since(userbot))} — chatlar "
                "o'qilmayapti. Serverda: <code>docker compose restart userbot</code>; "
                "sessiya tugagan bo'lsa <code>make userbot-login</code> qilib "
                "TELETHON_SESSION'ni yangila.",
            )
        )
    api = status.components["api"]
    if api.stale:
        found.append(
            Problem(
                "api_silent",
                "warning",
                f"⚠️ API {escape(_since(api))} — telefon ilovasi va tekshiruv nuqtasi "
                "ishlamayapti. Serverda: <code>docker compose restart api</code>.",
            )
        )

    info = status.backup
    if not info.configured:
        # Without a key no backup is ever written: silence here would let the
        # owner run for a year believing the ledger is safe.
        found.append(
            Problem(
                "backup_unconfigured",
                "warning",
                "⚠️ Zaxira nusxa sozlanmagan — hech qanday zaxira yozilmayapti. "
                "Server diski buzilsa, hamma qarz va yozuvlar yo'qoladi. Serverda: "
                "<code>make backup-key</code>, chiqqan qatorni .env'ga yoz, keyin "
                "<code>docker compose up -d --force-recreate worker bot</code> va "
                "<code>make backup</code>.",
            )
        )
    elif info.stale:
        if info.created_at is None:
            what = "⚠️ Zaxira nusxa umuman yo'q."
        else:
            what = (
                "⚠️ Zaxira nusxa eskirgan: oxirgisi "
                f"{escape(age_label(status.now - info.created_at))} oldin."
            )
        found.append(
            Problem(
                "backup_stale",
                "warning",
                f"{what} Serverda: {backup_cmd}; sabab uchun <code>make worker</code> "
                "logini ko'r.",
            )
        )
    else:
        backup_job = status.jobs.get("backup")
        if info.send_failed:
            reason = (
                f" Sabab: <code>{escape(info.send_error)}</code>."
                if info.send_error
                else ""
            )
            found.append(
                Problem(
                    "backup_failed",
                    "warning",
                    "⚠️ Zaxira nusxa Telegramga yuborilmadi — fayl diskda turibdi: "
                    f"<code>{escape(str(info.path))}</code>.{reason} Keyingi kecha "
                    f"qayta uriniladi; hozir kerak bo'lsa {backup_cmd}.",
                )
            )
        elif backup_job is not None and backup_job.detail.get("ok") is False:
            error = str(backup_job.detail.get("error") or "noma'lum xato")
            found.append(
                Problem(
                    "backup_failed",
                    "warning",
                    "⚠️ Zaxira nusxa ishi xato bilan tugadi: "
                    f"<code>{escape(error)}</code>. Serverda: {backup_cmd}; "
                    "<code>make worker</code> logini ko'r.",
                )
            )

    if status.anthropic_failing:
        if status.anthropic_last_ok_at is None:
            last = "hali hech qachon"
        else:
            last = f"{age_label(status.now - status.anthropic_last_ok_at)} oldin"
        found.append(
            Problem(
                "anthropic_failing",
                "warning",
                f"⚠️ Anthropic bilan ishlamayapti: {status.windows_pending} ta suhbat "
                f"kutmoqda, {status.windows_failed} ta muvaffaqiyatsiz, oxirgi "
                f"muvaffaqiyat {escape(last)}. ANTHROPIC_API_KEY va balansni tekshir; "
                "<code>make worker</code> logi sababini ko'rsatadi.",
            )
        )
    if status.needs_review >= REVIEW_BACKLOG_THRESHOLD:
        found.append(
            Problem(
                "review_backlog",
                "warning",
                f"⚠️ {status.needs_review} ta xabar ishlanmay qoldi — /tekshir orqali "
                "ko'rib chiq.",
            )
        )
    daily, monthly = settings.spend_alert_daily_usd, settings.spend_alert_monthly_usd
    if daily > 0 and status.cost_today_usd >= daily:
        found.append(
            Problem(
                "spend_high",
                "warning",
                f"⚠️ Bugungi API xarajati {usd(status.cost_today_usd)} — kunlik chegara "
                f"{usd(daily)}dan oshdi. /xarajat bilan qaysi ish ko'p sarflayotganini "
                "ko'r; kutilmagan bo'lsa, Anthropic Console'da MIYA kalitini vaqtincha "
                "o'chir.",
            )
        )
    elif monthly > 0 and status.cost_month_usd >= monthly:
        found.append(
            Problem(
                "spend_high",
                "warning",
                f"⚠️ Bu oygi API xarajati {usd(status.cost_month_usd)} — oylik chegara "
                f"{usd(monthly)}dan oshdi. /xarajat'ni ko'r.",
            )
        )
    return found


# --- alert memory -----------------------------------------------------------


async def alerts_due(
    session: AsyncSession, problems: list[Problem], *, now: datetime | None = None
) -> tuple[list[Problem], list[str]]:
    """Which problems to say now, and which keys have recovered.

    A problem is due when no ``alert:<key>`` row is newer than
    ALERT_REPEAT_HOURS. A key has recovered when it is no longer a problem
    and its last ``alert:`` row is newer than its last ``recovered:`` row.
    """
    now = _now(now)
    kinds = [ALERT_KIND + key for key in PROBLEM_KEYS] + [
        RECOVERED_KIND + key for key in PROBLEM_KEYS
    ]
    rows = await session.execute(
        sa.select(ReminderLog.kind, sa.func.max(ReminderLog.sent_at))
        .where(ReminderLog.kind.in_(kinds))
        .group_by(ReminderLog.kind)
    )
    last: dict[str, datetime] = dict(rows.all())
    repeat = timedelta(hours=settings.alert_repeat_hours)

    due = [
        problem
        for problem in problems
        if (
            last.get(ALERT_KIND + problem.key) is None
            or now - last[ALERT_KIND + problem.key] > repeat
        )
    ]
    current = {problem.key for problem in problems}
    recovered = []
    for key in PROBLEM_KEYS:
        if key in current:
            continue
        alerted = last.get(ALERT_KIND + key)
        if alerted is None:
            continue
        cleared = last.get(RECOVERED_KIND + key)
        if cleared is None or cleared < alerted:
            recovered.append(key)
    return due, recovered


def _mark(
    session: AsyncSession, kind: str, keys: list[str], now: datetime | None
) -> None:
    for key in keys:
        row = ReminderLog(kind=kind + key, ref=key)
        if now is not None:
            row.sent_at = now
        session.add(row)


def mark_alerted(
    session: AsyncSession, keys: list[str], *, now: datetime | None = None
) -> None:
    """Record that these problems were *actually delivered* (the caller's
    commit writes the rows). Call only after the send succeeded."""
    _mark(session, ALERT_KIND, keys, now)


def mark_recovered(
    session: AsyncSession, keys: list[str], *, now: datetime | None = None
) -> None:
    _mark(session, RECOVERED_KIND, keys, now)


_RECOVERY = {
    "spend_high": "✅ API xarajati yana chegara ichida — tiklandi",
    "worker_silent": "✅ Rejalashtiruvchi (worker) qayta ishlayapti — tiklandi",
    "userbot_silent": "✅ Telegram o'quvchi qayta ulandi — tiklandi",
    "api_silent": "✅ API qayta javob beryapti — tiklandi",
    "db_down": "✅ Baza qayta javob beryapti — tiklandi",
    "disk_low": "✅ Diskda yana joy bor — tiklandi",
    "backup_unconfigured": "✅ Zaxira nusxa sozlandi — tiklandi",
    "backup_stale": "✅ Zaxira nusxa yana yangi — tiklandi",
    "backup_failed": "✅ Zaxira nusxa yana Telegramga yetib bordi — tiklandi",
    "anthropic_failing": "✅ Anthropic qayta ishlayapti — tiklandi",
    "review_backlog": "✅ Ishlanmagan xabarlar navbati bo'shadi — tiklandi",
}


def recovery_text(key: str) -> str:
    """The one-line "it is back" notice for a problem key."""
    return _RECOVERY.get(key) or f"✅ {escape(key)} — tiklandi"


__all__ = [
    "ALERT_KIND",
    "BACKUP_COMPONENT",
    "COMPONENTS",
    "JOB_PREFIX",
    "PROBLEM_KEYS",
    "RECOVERED_KIND",
    "REVIEW_BACKLOG_THRESHOLD",
    "BackupInfo",
    "Component",
    "Problem",
    "Status",
    "alerts_due",
    "beat",
    "beats",
    "component_of",
    "gather",
    "mark_alerted",
    "mark_recovered",
    "problems",
    "recovery_text",
    "size_label",
    "stale_after",
]

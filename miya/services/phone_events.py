"""Phone events (build step 6): call-log entries and SMS from the companion.

The Android app uploads two metadata streams next to the recordings it
already sends: call-log events (incoming/outgoing/missed/rejected — number,
timestamp, duration, nothing else) and SMS, mostly payment notifications.
This module is the whole server side of that ingest:

* every event becomes exactly one interaction, idempotently — the key is
  minted on the phone (``call_event_key`` / ``sms_event_key``) and the
  partial unique index ``ux_interactions_event_key`` is the guarantee; the
  pre-select here only makes retried batches cheap;
* a money SMS is parsed DETERMINISTICALLY (services/sms_money.py) into one
  transaction. A bank SMS is the bank's record, not a counterparty claim:
  nothing here calls ``ingest.process_interaction`` or the claim gate, and
  no model token is ever spent on it;
* a call-log row must NEVER carry ``media["call_id"]``. The recording
  uploader dedupes on that key (call_recordings.find_ingested), the phone
  deletes the audio when told "duplicate" — a call-log row wearing the
  recording's key would destroy the recording of that same call. Event rows
  use ``media["event_key"]`` and nothing else.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from miya.config import settings
from miya.db.enums import Direction, InteractionSource
from miya.db.models import Interaction
from miya.services import money_events, sms_money
from miya.services.people import find_by_phone, resolve_person

log = logging.getLogger(__name__)

CALL_TYPES = ("incoming", "outgoing", "missed", "rejected", "unknown")
MEDIA_CALL_LOG = "call_log"
MEDIA_SMS = "sms"
# One upload may carry at most this many events; the API caps the request
# body to the same figure, so a bigger batch is a caller bug, not data.
MAX_BATCH = 200
# The API refuses longer bodies too; this is the service's own line.
SMS_BODY_MAX_CHARS = 4096

# Only a conversation has a direction; a missed or rejected ring does not.
_DIRECTION_OF_CALL = {"incoming": Direction.in_, "outgoing": Direction.out}

_MISSED_LABEL = {"missed": "javobsiz", "rejected": "rad etilgan"}


def call_event_key(device_id: str, call_log_id: int) -> str:
    """The dedupe key of one call-log row: stable across retries."""
    return f"{device_id}:call:{call_log_id}"


def sms_event_key(
    device_id: str,
    sms_id: int,
    sender: str,
    received_at: datetime | str,
    body: str,
) -> str:
    """The dedupe key of one SMS.

    The content hash is load-bearing: Android's ``Sms._ID`` restarts after a
    wipe, so the provider id alone could make a genuinely new message look
    like an old one. Hashing sender, timestamp and body ties the key to the
    message itself.
    """
    if isinstance(received_at, str):
        received_at = datetime.fromisoformat(received_at)
    digest = hashlib.sha256(
        f"{sender}|{received_at.isoformat()}|{body}".encode()
    ).hexdigest()[:16]
    return f"{device_id}:sms:{sms_id}:{digest}"


# --- reinstall-safe content keys (WP-11) ---------------------------------------
#
# The event key carries the device id, and a reinstall (allowBackup=false)
# mints a new one and resets the SMS cursor — the whole inbox comes back.
# The content key is the event itself, so the server answers that
# re-harvest with "duplicates". Timestamps enter as UTC epoch seconds: a
# changed phone time zone or millisecond rounding in a backup app cannot
# move them. The migration 0013 carries frozen copies of both functions;
# a test keeps them equal.

CONTENT_KEY_VERSION = "v1"
CONTENT_KEY_CONSTRAINTS = frozenset(
    {"ux_interactions_event_key", "ux_interactions_content_key"}
)


def _norm_body(body: str) -> str:
    """Whitespace runs collapsed (CRLF, NBSP, trailing spaces); case kept."""
    return " ".join(body.split())


def _epoch(moment: datetime | str) -> int:
    if isinstance(moment, str):
        moment = datetime.fromisoformat(moment)
    return int(moment.timestamp())


def sms_content_key(sender: str, received_at: datetime | str, body: str) -> str:
    material = (
        f"{CONTENT_KEY_VERSION}|{sms_money.normalise_sender(sender)}|"
        f"{_epoch(received_at)}|{_norm_body(body)}"
    )
    return "smsc:" + hashlib.sha256(material.encode()).hexdigest()[:32]


def call_content_key(
    number: str | None, started_at: datetime | str, duration_seconds: int, call_type: str
) -> str:
    digits = re.sub(r"\D", "", number or "")[-9:]
    material = (
        f"{CONTENT_KEY_VERSION}|{digits}|{_epoch(started_at)}|"
        f"{duration_seconds}|{call_type}"
    )
    return "callc:" + hashlib.sha256(material.encode()).hexdigest()[:32]


@dataclass(slots=True)
class EventOutcome:
    """What one batch became: nothing in it is ever silently dropped —
    every event is accepted, a duplicate, or rejected with its reason."""

    accepted: int = 0
    duplicates: int = 0
    rejected: list[tuple[int, str]] = field(default_factory=list)  # (index, reason)


# --- validation ---------------------------------------------------------------


def _parse_ts(value: object) -> datetime | None:
    """A client timestamp: ISO-8601 WITH an offset (or an aware datetime).

    Used verbatim as ``occurred_at`` so retries stay stable; a naive value
    would be ambiguous the moment the phone travels, so it is refused.
    """
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value
    return None


def _int(value: object, *, minimum: int | None = None) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if minimum is not None and value < minimum:
        return None
    return value


def _optional_str(value: object) -> tuple[str | None, bool]:
    """(cleaned value, ok): None and '' collapse to None, non-strings fail."""
    if value is None:
        return None, True
    if isinstance(value, str):
        return value.strip() or None, True
    return None, False


def _validate_call(event: dict) -> tuple[dict | None, str | None]:
    call_log_id = _int(event.get("call_log_id"), minimum=0)
    if call_log_id is None:
        return None, "bad call_log_id"
    started_at = _parse_ts(event.get("started_at"))
    if started_at is None:
        return None, "bad started_at (ISO-8601 with offset required)"
    duration = _int(event.get("duration_seconds"), minimum=0)
    if duration is None:
        return None, "bad duration_seconds"
    call_type = event.get("type")
    if call_type not in CALL_TYPES:
        return None, "bad type"
    number, ok = _optional_str(event.get("number"))
    if not ok:
        return None, "bad number"
    contact_name, ok = _optional_str(event.get("contact_name"))
    if not ok:
        return None, "bad contact_name"
    sim_slot = event.get("sim_slot")
    if sim_slot is not None and _int(sim_slot) is None:
        return None, "bad sim_slot"
    return {
        "call_log_id": call_log_id,
        "started_at": started_at,
        "duration_seconds": duration,
        "type": call_type,
        "number": number,
        "contact_name": contact_name,
    }, None


def _validate_sms(message: dict) -> tuple[dict | None, str | None]:
    sms_id = _int(message.get("sms_id"), minimum=0)
    if sms_id is None:
        return None, "bad sms_id"
    sender = message.get("sender")
    if not isinstance(sender, str) or not sender.strip():
        return None, "bad sender"
    received_at = _parse_ts(message.get("received_at"))
    if received_at is None:
        return None, "bad received_at (ISO-8601 with offset required)"
    body = message.get("body")
    if not isinstance(body, str):
        return None, "bad body"
    if len(body) > SMS_BODY_MAX_CHARS:
        return None, f"body over {SMS_BODY_MAX_CHARS} chars"
    sim_slot = message.get("sim_slot")
    if sim_slot is not None and _int(sim_slot) is None:
        return None, "bad sim_slot"
    return {
        "sms_id": sms_id,
        "sender": sender.strip(),
        "received_at": received_at,
        "body": body,
        "sim_slot": sim_slot,
    }, None


# --- shared mechanics ---------------------------------------------------------


async def _existing_keys(
    session: AsyncSession, keys: list[str], content_keys: list[str]
) -> set[str]:
    """Which of these event or content keys are already interactions — one
    query, so a fully-duplicate retry batch costs a single pair of index
    scans."""
    if not keys and not content_keys:
        return set()
    event_key = Interaction.media["event_key"].astext
    content_key = Interaction.media["content_key"].astext
    rows = await session.execute(
        sa.select(event_key, content_key).where(
            sa.or_(event_key.in_(keys), content_key.in_(content_keys))
        )
    )
    return {key for row in rows.all() for key in row if key is not None}


async def _ingest_batch(
    session: AsyncSession,
    device_id: str,
    events: list[dict],
    validate,
    key_of,
    insert,
    content_key_of=None,
) -> EventOutcome:
    """Validate, pre-select, then insert each event under its own SAVEPOINT.

    The savepoint (session.begin_nested) catching IntegrityError is the real
    race guard: two workers posting the same event both pass the pre-select,
    and the second one's insert lands on ux_interactions_event_key and is
    counted a duplicate instead of failing the batch. An event is a
    duplicate when either its event key or its content key is known; the
    content key rides in ``fields["content_key"]`` for the insert to store.
    The caller commits.
    """
    if len(events) > MAX_BATCH:
        raise ValueError(f"batch of {len(events)} exceeds MAX_BATCH={MAX_BATCH}")
    outcome = EventOutcome()
    prepared: list[tuple[int, dict | None, str | None, str | None]] = []
    keys: list[str] = []
    content_keys: list[str] = []
    for index, event in enumerate(events):
        fields, error = validate(event)
        if error is not None or fields is None:
            prepared.append((index, None, None, error or "invalid"))
            continue
        key = key_of(device_id, fields)
        if content_key_of is not None:
            fields["content_key"] = content_key_of(fields)
            content_keys.append(fields["content_key"])
        prepared.append((index, fields, key, None))
        keys.append(key)

    existing = await _existing_keys(session, keys, content_keys)
    for index, fields, key, error in prepared:
        if error is not None:
            outcome.rejected.append((index, error))
            continue
        content_key = fields.get("content_key")
        if key in existing or (content_key is not None and content_key in existing):
            outcome.duplicates += 1
            continue
        try:
            async with session.begin_nested():
                await insert(session, device_id, key, fields)
        except IntegrityError as exc:
            # Only the event-key collision is a duplicate; any other
            # constraint would be a programming error hiding as one.
            constraint = getattr(
                getattr(getattr(exc, "orig", None), "diag", None),
                "constraint_name",
                None,
            )
            if constraint is not None and constraint not in CONTENT_KEY_CONSTRAINTS:
                raise
            if constraint is None:
                log.debug("integrity error taken as a duplicate for %s", key)
            outcome.duplicates += 1
            continue
        # The same batch may carry the same event twice.
        existing.add(key)
        if content_key is not None:
            existing.add(content_key)
        outcome.accepted += 1
    return outcome


# --- call-log events ----------------------------------------------------------


def _call_line(call_type: str, who: str) -> str:
    """The one Uzbek line /tarix shows for a call-log row (raw; the renderer
    escapes). Mirrors call_recordings._context_line's arrows."""
    if call_type == "incoming":
        return f"[qo'ng'iroq ← {who}]"
    if call_type == "outgoing":
        return f"[qo'ng'iroq → {who}]"
    label = _MISSED_LABEL.get(call_type)
    if label:
        return f"[qo'ng'iroq ✖ {who} — {label}]"
    return f"[qo'ng'iroq: {who}]"


async def _insert_call_event(
    session: AsyncSession, device_id: str, key: str, fields: dict
) -> Interaction:
    number: str | None = fields["number"]
    contact_name: str | None = fields["contact_name"]
    digits = re.sub(r"\D", "", number or "") or None
    person = None
    if contact_name:
        # The phone's address book is trusted, exactly as the recording
        # sidecar's counterparty_name is: resolve fuzzily, learn the
        # spelling, backfill the number.
        # The phone book is the owner's own words: a code in it is attached.
        person = await resolve_person(
            session, contact_name, phone=digits, code_policy="attach", source="contact"
        )
    elif number:
        # A bare number never creates a Person; it may match one.
        person = await find_by_phone(session, number)

    interaction = Interaction(
        source=InteractionSource.phone_call,
        direction=_DIRECTION_OF_CALL.get(fields["type"], Direction.na),
        person_id=person.id if person else None,
        occurred_at=fields["started_at"],  # the client's own moment, verbatim
        raw_text=_call_line(fields["type"], contact_name or number or "noma'lum raqam"),
        processed=True,  # metadata only: there is nothing to extract
        needs_review=False,
        media={
            "type": MEDIA_CALL_LOG,
            "event_key": key,
            "content_key": fields.get("content_key"),
            "call_type": fields["type"],
            "phone": number,
            "contact_name": contact_name,
            "duration_seconds": fields["duration_seconds"],
            "device_id": device_id,
            # NEVER "call_id" here: the recording uploader dedupes on it and
            # the phone deletes audio it is told is a duplicate. A test pins
            # that the recording of this same call still uploads.
        },
    )
    session.add(interaction)
    await session.flush()
    return interaction


async def ingest_call_events(
    session: AsyncSession, device_id: str, events: list[dict]
) -> EventOutcome:
    """One batch of call-log events → interactions, idempotently.

    Fields per event: ``call_log_id`` (int), ``started_at`` (ISO with
    offset, becomes occurred_at verbatim), ``duration_seconds`` (int >= 0),
    ``type`` (one of CALL_TYPES), ``number``, ``contact_name``, ``sim_slot``.
    The caller commits.
    """
    return await _ingest_batch(
        session,
        device_id,
        events,
        _validate_call,
        lambda device, fields: call_event_key(device, fields["call_log_id"]),
        _insert_call_event,
        content_key_of=lambda fields: call_content_key(
            fields["number"],
            fields["started_at"],
            fields["duration_seconds"],
            fields["type"],
        ),
    )


# --- SMS ----------------------------------------------------------------------


def _sender_phone(sender: str) -> str | None:
    """The sender as a phone number, or None for an alphanumeric sender.

    Only a numeric sender with at least 7 digits may reach find_by_phone —
    "PAYME" must never be looked up, let alone become a Person.
    """
    cleaned = re.sub(r"[\s\-().+]", "", sender)
    if cleaned.isdigit() and len(cleaned) >= 7:
        return sender
    return None


async def _insert_sms(
    session: AsyncSession, device_id: str, key: str, fields: dict
) -> Interaction:
    sender: str = fields["sender"]
    body: str = fields["body"]

    person = None
    phone = _sender_phone(sender)
    if phone is not None:
        person = await find_by_phone(session, phone)

    media: dict = {
        "type": MEDIA_SMS,
        "event_key": key,
        "content_key": fields.get("content_key"),
        "sender": sender,
        "sim_slot": fields["sim_slot"],
        "device_id": device_id,
    }
    interaction = Interaction(
        source=InteractionSource.phone_sms,
        direction=Direction.in_,
        person_id=person.id if person else None,
        occurred_at=fields["received_at"],  # the client's own moment, verbatim
        raw_text=body,
        processed=True,
        needs_review=False,
        media=media,
    )
    session.add(interaction)
    await session.flush()

    # Deterministic, token-free: a bank SMS is the bank's record, so it goes
    # through sms_money and the one booking service (money_events), never
    # through extraction or the claim gate.
    parsed = sms_money.parse(sender, body, received_at=fields["received_at"])
    if parsed is not None:
        await money_events.apply_reading(
            session,
            interaction,
            parsed,
            channel=money_events.channel_for_sms(sender),
            now=datetime.now(settings.tz),
        )
    return interaction


async def ingest_sms(
    session: AsyncSession, device_id: str, messages: list[dict]
) -> EventOutcome:
    """One batch of SMS → interactions (plus a transaction per money SMS).

    Fields per message: ``sms_id`` (int), ``sender`` (str, may be
    alphanumeric), ``received_at`` (ISO with offset), ``body`` (str, at most
    SMS_BODY_MAX_CHARS), ``sim_slot``. The caller commits.
    """
    return await _ingest_batch(
        session,
        device_id,
        messages,
        _validate_sms,
        lambda device, fields: sms_event_key(
            device,
            fields["sms_id"],
            fields["sender"],
            fields["received_at"],
            fields["body"],
        ),
        _insert_sms,
        content_key_of=lambda fields: sms_content_key(
            fields["sender"], fields["received_at"], fields["body"]
        ),
    )


# --- payment-app notifications (WP-41) ---------------------------------------------

MEDIA_NOTIFICATION = "notification"
# The API caps the batch to this: 50 notifications fit the 1 MiB body cap.
NOTIFICATION_MAX_BATCH = 50
NOTIFICATION_TEXT_MAX = 4096
NOTIFICATION_SHORT_MAX = 512
NOTIFICATION_LINES_MAX = 20
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")


def notification_body(
    text: str | None, big_text: str | None, lines: list[str] | None
) -> str:
    """big_text, else text, then the inbox lines; '' counts as missing,
    exactly as the phone's takeIf { it.isNotEmpty() } does."""
    body = big_text or text or ""
    if lines:
        body += "\n" + "\n".join(lines)
    return body


def _epoch_ms(moment: datetime) -> int:
    """Integer milliseconds since the epoch — never float timestamp()*1000,
    which can round one off; the phone computes the same key."""
    return (moment - EPOCH) // timedelta(milliseconds=1)


def notification_event_key(
    device_id: str,
    package: str,
    notification_id: int,
    tag: str | None,
    when_at: datetime,
    title: str | None,
    body: str,
) -> str:
    """The dedupe key of one posted notification; mirrored byte for byte by
    the Android app (PaymentApps.kt)."""
    material = f"{notification_id}|{tag or ''}|{_epoch_ms(when_at)}|{title or ''}|{body}"
    digest = hashlib.sha256(material.encode()).hexdigest()[:16]
    return f"{device_id}:ntf:{package}:{digest}"


def notification_content_key(
    package: str, when_at: datetime, title: str | None, body: str
) -> str:
    """The same push after a reinstall or on a second device."""
    material = (
        f"{CONTENT_KEY_VERSION}|{package.lower()}|{_epoch(when_at)}|"
        f"{title or ''}|{_norm_body(body)}"
    )
    return "ntfc:" + hashlib.sha256(material.encode()).hexdigest()[:32]


def _validate_notification(item: dict) -> tuple[dict | None, str | None]:
    package = item.get("package")
    if not isinstance(package, str) or not _PACKAGE_RE.match(package.strip()):
        return None, "bad package"
    posted_at = _parse_ts(item.get("posted_at"))
    if posted_at is None:
        return None, "bad posted_at (ISO-8601 with offset required)"
    when_at = None
    if item.get("when_at") is not None:
        when_at = _parse_ts(item.get("when_at"))
        if when_at is None:
            return None, "bad when_at (ISO-8601 with offset required)"
    fields: dict = {
        "package": package.strip(),
        "posted_at": posted_at,
        "when_at": when_at,
    }
    for name, limit in (
        ("title", NOTIFICATION_SHORT_MAX),
        ("text", NOTIFICATION_TEXT_MAX),
        ("big_text", NOTIFICATION_TEXT_MAX),
        ("sub_text", NOTIFICATION_SHORT_MAX),
        ("tag", 255),
        ("channel_id", 255),
        ("category", 64),
    ):
        value = item.get(name)
        if value is not None and not isinstance(value, str):
            return None, f"bad {name}"
        if value is not None and len(value) > limit:
            return None, f"{name} over {limit} chars"
        fields[name] = value
    lines = item.get("lines") or []
    if not isinstance(lines, list) or len(lines) > NOTIFICATION_LINES_MAX:
        return None, "bad lines"
    if any(not isinstance(x, str) or len(x) > NOTIFICATION_SHORT_MAX for x in lines):
        return None, "bad lines"
    fields["lines"] = lines
    notification_id = item.get("notification_id", 0)
    if _int(notification_id) is None:
        return None, "bad notification_id"
    fields["notification_id"] = notification_id
    fields["body"] = notification_body(fields["text"], fields["big_text"], lines)
    if not (fields["title"] or fields["body"].strip()):
        return None, "empty notification"
    return fields, None


def _when(fields: dict) -> datetime:
    return fields["when_at"] or fields["posted_at"]


async def _insert_notification(
    session: AsyncSession, device_id: str, key: str, fields: dict
) -> Interaction:
    parts = [
        fields["title"],
        fields["big_text"] or fields["text"],
        *fields["lines"],
    ]
    text = "\n".join(p for p in parts if p)
    occurred = _when(fields)
    package = fields["package"]
    interaction = Interaction(
        source=InteractionSource.phone_notification,
        direction=Direction.in_,
        occurred_at=occurred,
        raw_text=text,
        processed=True,
        needs_review=False,
        media={
            "type": MEDIA_NOTIFICATION,
            "event_key": key,
            "content_key": fields.get("content_key"),
            "package": package,
            "device_id": device_id,
            "title": fields["title"],
            "channel_id": fields["channel_id"],
            "category": fields["category"],
        },
    )
    session.add(interaction)
    await session.flush()

    allowed = settings.payment_app_packages_parsed
    if allowed and package.lower() not in allowed:
        interaction.media = {
            **interaction.media,
            "money": {"verdict": "ignore", "reason": "not_payment_app"},
        }
        return interaction
    # The same reader and the same booking service as a bank SMS: neither
    # channel can book what the other would refuse.
    reading = sms_money.read(text, received_at=occurred)
    await money_events.apply_reading(
        session,
        interaction,
        reading,
        channel=money_events.channel_for_app(package),
        now=datetime.now(settings.tz),
    )
    return interaction


async def ingest_notifications(
    session: AsyncSession, device_id: str, items: list[dict]
) -> EventOutcome:
    """One batch of posted notifications → interactions; a completed payment
    becomes one transaction (or evidence on the matching SMS one). The
    caller commits."""
    return await _ingest_batch(
        session,
        device_id,
        items,
        _validate_notification,
        lambda device, fields: notification_event_key(
            device,
            fields["package"],
            fields["notification_id"],
            fields["tag"],
            _when(fields),
            fields["title"],
            fields["body"],
        ),
        _insert_notification,
        content_key_of=lambda fields: notification_content_key(
            fields["package"], _when(fields), fields["title"], fields["body"]
        ),
    )

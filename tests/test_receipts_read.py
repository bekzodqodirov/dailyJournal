"""JV-04: receipts read from images, and the owner's receipt card.

One EXTRACT_MODEL vision call per image (faked here: no network) says
whether it is a payment receipt and what it prints; ``services.receipts``
keeps the checked reading as a ``receipts`` row and never writes the ledger.
The bot, the userbot and the worker run against real PostgreSQL with
Telegram replaced by stand-ins.
"""

# ruff: noqa: F811 - the shared fixtures are imported, then requested by name.

from __future__ import annotations

import importlib.util
import itertools
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import pytest
import sqlalchemy as sa

from miya.bot import handlers, replies
from miya.bot.formatting import clock, day_label
from miya.config import settings
from miya.db import models as m
from miya.db.enums import ChatType, Currency, Direction, InteractionSource
from miya.services import (
    claims,
    documents,
    ingest,
    people,
    phone_events,
    purge,
    questions,
    receipts,
    safety,
    vision,
)
from miya.services import extraction as ex
from miya.services.media_policy import MediaKind, forced_plan, plan_for
from miya.services.persistence import Applied, apply_extraction
from miya.userbot import main as userbot
from miya.worker import main as worker
from tests.test_close_and_correct import _Message, bound  # noqa: F401
from tests.test_safety_files import (  # noqa: F401
    CHAT,
    _Client,
    _DocMessage,
    _private_monitor,
    _row,
    _tg_document,
    bot_stubs,
    media_in_tmp,
)

TZ = settings.tz
NOW = datetime.now(TZ).replace(second=0, microsecond=0)
PAID = NOW - timedelta(hours=1)
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + bytes(64)
PNG = b"\x89PNG\r\n\x1a\n" + bytes(64)
PDF = b"%PDF-1.4\n" + bytes(64)
INCOME_SMS = "Karta *1234 hisobiga 1 250 000 so'm tushdi"
EXPENSE_SMS = "Платёж успешно проведён\nKORZINKA\n250 000 сум"
_ids = itertools.count(1)


def _fields(**overrides) -> vision.ReceiptFields:
    values = {
        "amount": 1250000,
        "currency": "UZS",
        "paid_at": PAID.strftime("%Y-%m-%dT%H:%M"),
        "payer_name": "Akmal T.",
        "payee_name": "Sardor Q.",
        "payer_card_last4": "8600 **** **** 4321",
        "payee_card_last4": "****1234",
        "issuer": "Payme",
        "reference": "123456789",
    }
    values.update(overrides)
    return vision.ReceiptFields(**values)


def _payload(**overrides) -> vision.VisionPayload:
    return vision.VisionPayload(
        description="Payme: 1 250 000 so'm, Akmal T. -> Sardor Q.",
        is_receipt=True,
        receipt=_fields(**overrides),
    )


def _usage():
    return SimpleNamespace(
        input_tokens=100,
        output_tokens=40,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )


def _answer(parsed, stop_reason="end_turn"):
    return SimpleNamespace(parsed_output=parsed, stop_reason=stop_reason, usage=_usage())


def _text_answer(text: str):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=_usage(),
    )


class _VisionClient:
    """Stands in for the model client: one queued answer per call."""

    def __init__(self, answers=(), created=()):
        self.answers = list(answers)
        self.created = list(created)
        self.parse_calls: list[dict] = []
        self.create_calls: list[dict] = []
        self.messages = self

    @staticmethod
    def _next(queue):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def parse(self, **kwargs):
        self.parse_calls.append(kwargs)
        return self._next(self.answers)

    async def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return self._next(self.created)


@pytest.fixture
def model(monkeypatch):
    """Install a fake vision client; no network, no backoff sleep."""
    monkeypatch.setattr(vision, "_structured_output_unusable", False)
    monkeypatch.setattr(vision, "_RETRY_BASE_SECONDS", 0.0)

    def _install(answers=(), created=()):
        client = _VisionClient(answers, created)
        monkeypatch.setattr(vision, "get_client", lambda: client)
        return client

    return _install


def _reading(**overrides) -> vision.VisionReading:
    values = {
        "description": "Payme: 1 250 000 so'm",
        "model": "x",
        "is_receipt": True,
        "receipt": _fields(),
        "image_sha256": "a" * 64,
    }
    values.update(overrides)
    return vision.VisionReading(**values)


async def _chat_photo(session, *, person=None, at=NOW, direction=Direction.in_):
    if person is None:
        person = m.Person(display_name="Akmal", aliases=[])
        session.add(person)
        await session.flush()
    row = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=direction,
        person_id=person.id,
        tg_chat_id=CHAT,
        occurred_at=at,
        media={"type": "photo", "processed": True},
        meta={"tg_message_id": next(_ids)},
    )
    session.add(row)
    await session.flush()
    return row, person


async def _receipts(session) -> list[m.Receipt]:
    return list(await session.scalars(sa.select(m.Receipt).order_by(m.Receipt.id)))


# --- reading ----------------------------------------------------------------------


async def test_receipt_reading_persisted_decimal(session, model, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "payment_own_cards", "1234")
    client = model([_answer(_payload(amount=1250000.5))])
    image = tmp_path / "chek.jpg"
    image.write_bytes(JPEG)
    row, person = await _chat_photo(session)

    described = await ingest.describe_into(session, row, image)
    await session.commit()

    assert described == "Payme: 1 250 000 so'm, Akmal T. -> Sardor Q."
    assert row.meta["vision"] == described  # the extractor reads it as before
    [receipt] = await _receipts(session)
    await session.refresh(receipt)
    assert receipt.amount == Decimal("1250000.50")
    assert isinstance(receipt.amount, Decimal)
    assert receipt.currency is Currency.UZS
    assert receipt.paid_at == PAID
    assert (receipt.payer_card_last4, receipt.payee_card_last4) == ("4321", "1234")
    assert (receipt.issuer, receipt.reference) == ("Payme", "123456789")
    assert (receipt.payer_name, receipt.payee_name) == ("Akmal T.", "Sardor Q.")
    # The payee card is one of the owner's: money came to them.
    assert receipt.direction == "in"
    assert receipt.state == "read" and receipt.verdict == "waiting"
    assert receipt.verdict_reasons == []
    assert receipt.person_id == person.id
    assert receipt.image_sha256 == vision.hashlib.sha256(JPEG).hexdigest()
    assert receipt.reading["fields"]["amount"] == 1250000.5
    assert row.meta["receipt"]["amount"] == "1250000.50"
    assert row.needs_review is False
    # The model was sent what the bytes are, with the image's own date.
    [call] = client.parse_calls
    content = call["messages"][0]["content"]
    assert content[0]["source"]["media_type"] == "image/jpeg"
    assert content[2]["text"] == f"CURRENT_DATE: {NOW.date().isoformat()}"
    usage = await session.scalar(sa.select(m.UsageLog))
    assert usage.operation == "receipt_vision"
    # Nothing was booked: a receipt is the image's word, not the bank's.
    assert await session.scalar(sa.select(sa.func.count(m.Transaction.id))) == 0


async def test_a_picture_that_is_no_receipt_writes_no_row(session, model, tmp_path):
    model([_answer(vision.VisionPayload(description="Mushuk rasmi", is_receipt=False))])
    image = tmp_path / "rasm.jpg"
    image.write_bytes(JPEG)
    row, _ = await _chat_photo(session)

    assert await ingest.describe_into(session, row, image) == "Mushuk rasmi"
    await session.flush()
    assert await _receipts(session) == []
    usage = await session.scalar(sa.select(m.UsageLog))
    assert usage.operation == "vision"


def test_paid_at_and_cards_parse_strictly():
    assert receipts.parse_paid_at("2026-10-20T14:05") == datetime(
        2026, 10, 20, 14, 5, tzinfo=TZ
    )
    assert receipts.parse_paid_at("2026-10-20 9:05:30") == datetime(
        2026, 10, 20, 9, 5, 30, tzinfo=TZ
    )
    assert receipts.parse_paid_at("2026-10-20T09:05Z") == datetime(
        2026, 10, 20, 14, 5, tzinfo=TZ
    )
    # A printed date with no time is no moment to match a bank row against.
    assert receipts.parse_paid_at("2026-10-20") is None
    assert receipts.parse_paid_at("20.10.2026 14:05") is None
    assert receipts.parse_paid_at("2026-02-30T10:00") is None
    # The calendar's edge cannot move to the owner's clock: no moment, no crash.
    assert receipts.parse_paid_at("9999-12-31T23:59Z") is None
    assert receipts.parse_paid_at("0001-01-01T00:00+05:00") is None
    assert receipts.card_last4("8600 **** **** 1234") == "1234"
    assert receipts.card_last4("**12") is None
    # Only ASCII digits: the CHECK on the column accepts nothing else.
    assert receipts.card_last4("８６００ **** １２３４") is None
    assert receipts.card_last4(None) is None


async def test_a_retry_covers_one_unusable_answer(session, model, tmp_path):
    client = model([_answer(None, stop_reason="max_tokens"), _answer(_payload())])
    image = tmp_path / "chek.png"
    image.write_bytes(PNG)

    reading = await vision.read_image(image, now=NOW)

    assert reading.ok and reading.is_receipt
    assert len(client.parse_calls) == 2
    # The second attempt says why it is asked again.
    assert "not valid" in client.parse_calls[1]["messages"][0]["content"][-1]["text"]
    assert reading.usage.calls == 2  # both attempts are billed


async def test_transport_errors_are_left_to_the_client_retries(model, tmp_path):
    error = anthropic.APIConnectionError(request=httpx.Request("POST", "https://x"))
    client = model([error, _answer(_payload())])
    image = tmp_path / "chek.jpg"
    image.write_bytes(JPEG)

    reading = await vision.read_image(image, now=NOW)

    assert reading.error == "APIConnectionError"
    assert len(client.parse_calls) == 1


async def test_an_uncompilable_schema_falls_back_to_prompted_json(model, tmp_path):
    grammar = anthropic.BadRequestError(
        message="Error code: 400 - Grammar compilation timed out.",
        response=SimpleNamespace(status_code=400, headers={}, request=None),
        body=None,
    )
    client = model([grammar], created=[_text_answer(_payload().model_dump_json())])
    image = tmp_path / "chek.jpg"
    image.write_bytes(JPEG)

    reading = await vision.read_image(image, now=NOW)

    assert reading.ok and reading.receipt.reference == "123456789"
    [call] = client.create_calls
    assert "JSON Schema" in call["messages"][0]["content"][-1]["text"]


async def test_failed_read_needs_review(bound, bot_stubs, media_in_tmp, model):
    """A photo the owner sent the bot is a receipt to look at, read or not."""
    client = model([_answer(None), _answer(None)])
    bot_stubs.content = JPEG
    message = _PhotoMessage()

    await handlers.on_photo(message, bot=None)

    assert len(client.parse_calls) == 2
    [(text, _)] = message.sent
    assert text == replies.RECEIPT_UNREADABLE
    [receipt] = await _receipts(bound)
    assert receipt.state == "needs_review" and receipt.amount is None
    interaction = await bound.get(m.Interaction, receipt.interaction_id)
    assert interaction.needs_review is True
    assert interaction.meta["receipt"]["state"] == "needs_review"
    assert receipt.notified_at is not None  # the card was the reply
    # Both failed attempts were still paid for.
    usage = await bound.scalar(sa.select(m.UsageLog))
    assert usage.input_tokens == 200


async def test_a_receipt_without_an_amount_needs_review(session):
    row, _ = await _chat_photo(session)
    receipt = await receipts.record(
        session, row, _reading(receipt=_fields(amount=None)), presume_receipt=False
    )
    assert receipt.state == "needs_review"
    assert row.needs_review is True
    assert (receipt.amount, receipt.currency) == (None, None)


async def test_a_card_number_read_as_the_amount_needs_review(session):
    """A figure NUMERIC(14,2) cannot hold is no total: the row is kept unread."""
    row, _ = await _chat_photo(session)
    reading = _reading(receipt=_fields(amount=8600123412341234.0))
    receipt = await receipts.record(session, row, reading, presume_receipt=False)
    await session.commit()
    assert receipt.state == "needs_review"
    assert (receipt.amount, receipt.currency) == (None, None)
    assert row.needs_review is True


async def test_the_audit_copy_keeps_only_the_last_four_card_digits(session):
    row, _ = await _chat_photo(session)
    reading = _reading(
        receipt=_fields(
            payer_card_last4="8600 1234 5678 9012", payee_card_last4="9860 0000 1111 2222"
        )
    )
    receipt = await receipts.record(session, row, reading, presume_receipt=False)
    await session.commit()
    await session.refresh(receipt)
    fields = receipt.reading["fields"]
    assert (fields["payer_card_last4"], fields["payee_card_last4"]) == ("9012", "2222")
    assert "8600 1234 5678 9012" not in str(receipt.reading)
    assert (receipt.payer_card_last4, receipt.payee_card_last4) == ("9012", "2222")


async def test_a_reading_the_table_refuses_still_keeps_the_interaction(
    session, monkeypatch
):
    """Whatever slips past the parser, the photo is flagged, never dropped."""
    row, _ = await _chat_photo(session)

    async def _refused(session, interaction, reading, *, presume_receipt):
        session.add(
            m.Receipt(
                interaction_id=interaction.id, state="read", payer_card_last4="１２３４"
            )
        )
        await session.flush()

    monkeypatch.setattr(receipts, "record", _refused)
    reading = _reading(usage=_usage())
    described = await ingest.apply_vision(session, row, reading, presume_receipt=True)
    await session.commit()

    assert described == reading.description
    assert row.needs_review is True
    assert row.meta["vision"] == reading.description
    assert await _receipts(session) == []
    # The bill written before the savepoint survives it.
    assert await session.scalar(sa.select(sa.func.count(m.UsageLog.id))) == 1


def test_the_card_strips_bidi_controls_from_printed_names():
    receipt = SimpleNamespace(
        state="read",
        amount=Decimal("50000.00"),
        currency=Currency.UZS,
        paid_at=None,
        payer_name="Akmal\u202e4321",
        payee_name="Sardor\u200b Q.",
        payee_card_last4="1234",
        issuer="Pay\u202eme",
        reference="12\u2066345",
    )
    card = replies.receipt_card(receipt, sender="Ali\u202e", channel="Yuk\u202egruppa")
    assert not any(ch in card for ch in "\u202e\u200b\u2066")
    assert "Akmal4321 → Sardor Q. ****1234" in card
    assert "Payme · № 12345" in card
    assert "Ali · Yukgruppa" in card


async def test_a_failed_chat_read_flags_the_message_only(session):
    """In a chat a picture is not a receipt until the model says so."""
    row, _ = await _chat_photo(session)
    reading = _reading(description="", is_receipt=False, receipt=None, error="refusal")
    assert await ingest.apply_vision(session, row, reading, presume_receipt=False) is None
    assert row.needs_review is True
    assert await _receipts(session) == []


async def test_a_later_read_replaces_only_a_failed_one(session):
    row, _ = await _chat_photo(session)
    failed = _reading(description="", receipt=None, error="refusal")
    first = await receipts.record(session, row, failed, presume_receipt=True)
    assert first.state == "needs_review"

    again = await receipts.record(session, row, _reading(), presume_receipt=True)
    assert again.id == first.id and again.state == "read"
    assert [h["new"] for h in again.history] == ["needs_review", "read"]
    # A row that was read is never overwritten by a later reading.
    third = await receipts.record(
        session, row, _reading(receipt=_fields(amount=5)), presume_receipt=True
    )
    assert third.amount == Decimal("1250000.00")


def test_the_migration_freezes_the_model_lists():
    path = Path(__file__).parents[1] / "alembic" / "versions" / "0023_receipts.py"
    spec = importlib.util.spec_from_file_location("migration_0023", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "0022_promise_due_time"
    assert len(migration.revision) <= 32
    assert migration.DIRECTIONS == m.RECEIPT_DIRECTIONS
    assert migration.STATES == m.RECEIPT_STATES
    assert migration.VERDICTS == m.RECEIPT_VERDICTS
    assert migration.REASONS == m.RECEIPT_REASONS


async def test_the_checks_hold_in_the_database(session):
    row, _ = await _chat_photo(session)
    session.add(
        m.Receipt(interaction_id=row.id, state="read", amount=Decimal("5"), currency=None)
    )
    with pytest.raises(sa.exc.IntegrityError):
        await session.flush()
    await session.rollback()

    row, _ = await _chat_photo(session)
    session.add(m.Receipt(interaction_id=row.id, state="read", verdict_reasons=["fake"]))
    with pytest.raises(sa.exc.IntegrityError):
        await session.flush()


# --- image files go to vision, never past the shield --------------------------------


class _PhotoMessage(_Message):
    def __init__(self, caption=None):
        super().__init__()
        self.photo = [SimpleNamespace(file_size=4096)]
        self.caption = caption
        self.date = NOW
        self.message_id = 42


def test_image_files_are_planned_like_photos():
    on = plan_for(
        MediaKind.document,
        vision_enabled=True,
        docs_enabled=False,
        filename="Chek.PNG",
        size=2048,
    )
    assert (on.download, on.vision, on.read_document) == (True, True, False)
    off = plan_for(
        MediaKind.document, vision_enabled=False, docs_enabled=True, filename="chek.jpg"
    )
    assert (off.download, off.skip_reason) == (False, "vision_disabled")
    big = plan_for(
        MediaKind.document,
        vision_enabled=True,
        docs_enabled=True,
        filename="chek.webp",
        size=vision.MAX_IMAGE_BYTES + 1,
    )
    assert (big.download, big.ask, big.skip_reason) == (False, False, "image_too_large")
    heic = plan_for(
        MediaKind.document, vision_enabled=True, docs_enabled=True, filename="IMG_1.HEIC"
    )
    assert (heic.download, heic.skip_reason) == (False, "heic_image")
    forced = forced_plan(MediaKind.document, "chek.jpeg")
    assert (forced.download, forced.vision) == (True, True)
    # The shield still comes first: a double extension is an installer.
    shielded = plan_for(
        MediaKind.document,
        vision_enabled=True,
        docs_enabled=True,
        filename="chek.jpg.apk",
    )
    assert (shielded.download, shielded.skip_reason) == (False, "dangerous_file")


def test_the_bytes_must_be_an_image(tmp_path):
    for content, kind in ((JPEG, "ok"), (PNG, "ok"), (PDF, "mismatch")):
        path = tmp_path / "chek.jpg"
        path.write_bytes(content)
        assert safety.sniff_magic(path, ".jpg").kind == kind
    webp = tmp_path / "chek.webp"
    webp.write_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 " + bytes(32))
    assert safety.sniff_magic(webp, ".webp") == safety.SAFE


async def test_image_document_goes_to_vision(
    bound, bot_stubs, media_in_tmp, model, monkeypatch
):
    client = model([_answer(_payload())])
    parsed: list[Path] = []

    def _parse(path):
        parsed.append(path)
        return None

    monkeypatch.setattr(documents, "read_document", _parse)
    bot_stubs.content = PNG
    message = _DocMessage("Screenshot_2026.png", mime="image/png")
    message.date = NOW

    await handlers.on_document(message, bot=None)

    assert len(client.parse_calls) == 1
    assert parsed == []  # never handed to the document parsers
    [receipt] = await _receipts(bound)
    interaction = await bound.get(m.Interaction, receipt.interaction_id)
    assert interaction.source is InteractionSource.receipt_photo
    assert interaction.media["filename"] == "Screenshot_2026.png"
    [(text, _), _reply] = message.sent
    assert text.startswith("🧾 Chek · siz · MIYA bot\nChekda: 1.25 mln so'm")
    assert bot_stubs.extracted == [interaction.id]


async def test_an_image_file_that_is_not_an_image_never_reaches_vision(
    bound, bot_stubs, media_in_tmp, model
):
    client = model([])
    bot_stubs.content = PDF
    message = _DocMessage("chek.jpg", mime="image/jpeg")
    message.date = NOW

    await handlers.on_document(message, bot=None)

    assert client.parse_calls == []
    [receipt] = await _receipts(bound)
    assert receipt.state == "needs_review"
    assert message.sent == [(replies.RECEIPT_UNREADABLE, None)]


async def test_a_disguised_installer_named_like_an_image_is_deleted(
    bound, bot_stubs, media_in_tmp, model
):
    client = model([])
    bot_stubs.content = b"\x7fELF\x02\x01\x01" + bytes(64)
    message = _DocMessage("chek.png", mime="image/png")

    await handlers.on_document(message, bot=None)

    assert client.parse_calls == []
    assert list(media_in_tmp.iterdir()) == []
    assert message.sent == [(replies.dangerous_file_refused("chek.png"), None)]
    assert await _receipts(bound) == []


async def test_a_photo_whose_bytes_are_an_installer_is_refused(
    bound, bot_stubs, media_in_tmp, model
):
    client = model([])
    bot_stubs.content = b"\x7fELF\x02\x01\x01" + bytes(64)
    message = _PhotoMessage()

    await handlers.on_photo(message, bot=None)

    assert client.parse_calls == []
    assert list(media_in_tmp.iterdir()) == []
    assert message.sent == [(replies.dangerous_file_refused(None), None)]


async def test_heic_gets_a_hint_and_is_never_fetched(bound, bot_stubs, media_in_tmp):
    message = _DocMessage("IMG_0042.HEIC", mime="image/heic")
    await handlers.on_document(message, bot=None)
    assert bot_stubs.downloads == []
    assert message.sent == [(replies.RECEIPT_HEIC_HINT, None)]
    assert await bound.scalar(sa.select(sa.func.count(m.Interaction.id))) == 0


async def test_the_userbot_reads_an_image_file_with_vision(session, media_in_tmp, model):
    client = model([_answer(_payload())])
    await _private_monitor()
    message = _tg_document(31, "chek.jpg", mime="image/jpeg")

    assert await userbot.ingest_message(_Client(JPEG), message) is True

    row = await _row(session, 31)
    assert len(client.parse_calls) == 1
    assert row.meta["vision"].startswith("Payme")
    [receipt] = await _receipts(session)
    assert receipt.interaction_id == row.id and receipt.notified_at is None


async def test_the_userbot_never_sends_a_fake_image_to_vision(
    session, media_in_tmp, model
):
    client = model([])
    await _private_monitor()
    message = _tg_document(32, "chek.jpg", mime="image/jpeg")

    await userbot.ingest_message(_Client(PDF), message)

    row = await _row(session, 32)
    assert client.parse_calls == []
    assert row.media["skipped"] == "mismatched_type"
    assert row.needs_review is True
    assert await _receipts(session) == []


# --- money: claims and the typed/bank match ------------------------------------------


async def _window_with_receipt(session, *, message_at: datetime, paid_at: datetime):
    person = m.Person(display_name="Akmal", aliases=[])
    session.add(person)
    await session.flush()
    window = m.ConversationWindow(
        tg_chat_id=CHAT,
        person_id=person.id,
        started_at=message_at,
        ended_at=message_at,
        message_count=1,
        char_count=10,
        text="[rasm]",
        custom_id=f"w-{next(_ids)}",
    )
    session.add(window)
    await session.flush()
    photo, _ = await _chat_photo(session, person=person, at=message_at)
    photo.window_id = window.id
    await receipts.record(
        session,
        photo,
        _reading(receipt=_fields(paid_at=paid_at.strftime("%Y-%m-%dT%H:%M"))),
        presume_receipt=False,
    )
    synthetic = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.na,
        person_id=person.id,
        tg_chat_id=CHAT,
        occurred_at=message_at,
        raw_text="[rasm]",
        meta={"kind": "window", "window_id": window.id},
        window_id=window.id,
    )
    session.add(synthetic)
    await session.flush()
    return synthetic


async def _sms(session, at: datetime, body: str):
    await phone_events.ingest_sms(
        session,
        "dev-1",
        [
            {
                "sms_id": next(_ids),
                "sender": "Payme",
                "received_at": at.isoformat(),
                "body": body,
                "sim_slot": 0,
            }
        ],
    )
    await session.flush()


async def test_counterparty_receipt_is_claim_with_receipt_at(session):
    """A client's receipt, sent days after they paid: the claim is asked,
    never booked, and the bank match anchors on the printed time."""
    paid = NOW - timedelta(days=3)
    synthetic = await _window_with_receipt(session, message_at=NOW, paid_at=paid)
    item = ex.ExtractedTransaction(
        type="income",
        amount=1250000,
        currency="UZS",
        counterparty="Akmal",
        asserted_by="them",
    )

    applied = await apply_extraction(
        session, synthetic, ex.ExtractionResult(transactions=[item])
    )

    [claim] = applied.claims
    assert applied.transactions == []
    assert claim.payload["receipt_at"] == paid.isoformat()
    assert claims.receipt_at(claim) == paid
    assert await claims._anchor(session, claim) == paid
    [receipt] = await _receipts(session)
    assert receipt.claim_id == claim.id
    assert await session.scalar(sa.select(sa.func.count(m.Transaction.id))) == 0

    # The bank's SMS from the day of payment proves it, though it is three
    # days older than the message that carried the receipt.
    await _sms(session, paid + timedelta(minutes=2), INCOME_SMS)
    await session.refresh(claim)
    [bank] = await session.scalars(sa.select(m.Transaction))
    assert claim.evidence_txn_id == bank.id


async def test_a_receipt_for_another_amount_is_not_attached(session):
    synthetic = await _window_with_receipt(session, message_at=NOW, paid_at=PAID)
    item = ex.ExtractedTransaction(
        type="income",
        amount=900000,
        currency="UZS",
        counterparty="Akmal",
        asserted_by="them",
    )
    applied = await apply_extraction(
        session, synthetic, ex.ExtractionResult(transactions=[item])
    )
    [claim] = applied.claims
    assert "receipt_at" not in claim.payload
    assert await claims._anchor(session, claim) == NOW


async def test_owner_photo_then_sms_not_double_booked(session):
    """The owner's own photo of a payment is their word, like a typed note;
    the bank SMS that follows is its evidence, not a second payment."""
    photo = m.Interaction(
        source=InteractionSource.receipt_photo,
        direction=Direction.in_,
        occurred_at=PAID,
        media={"type": "photo"},
        meta={"vision": "Korzinka 250 000 so'm"},
    )
    session.add(photo)
    await session.flush()
    await apply_extraction(
        session,
        photo,
        ex.ExtractionResult(
            transactions=[
                ex.ExtractedTransaction(
                    type="expense", amount=250000, category="oziq-ovqat"
                )
            ]
        ),
    )

    await _sms(session, PAID + timedelta(minutes=4), EXPENSE_SMS)

    [txn] = await session.scalars(sa.select(m.Transaction))
    assert txn.source_interaction_id == photo.id
    assert txn.channel == "sms:payme"
    assert txn.history[-1]["matched"] == "typed"
    assert await session.scalar(sa.select(sa.func.count(m.TransactionEvidence.id))) == 1


async def test_the_bot_card_does_not_contradict_what_was_booked(
    bound, bot_stubs, media_in_tmp, model, monkeypatch
):
    model([_answer(_payload())])
    bot_stubs.content = JPEG

    async def _booked(session, interaction):
        interaction.processed = True
        return SimpleNamespace(
            ok=True, applied=Applied(transactions=[SimpleNamespace(id=1)])
        )

    monkeypatch.setattr(handlers, "process_interaction", _booked)
    monkeypatch.setattr(replies, "confirmation", lambda applied: "✅ Yozildi")
    message = _PhotoMessage()
    await handlers.on_photo(message, bot=None)

    # The card is its own message (JV-05 edits it in place), the reply follows.
    [(card, buttons), (reply, _)] = message.sent
    assert replies.RECEIPT_FOOTER not in card
    assert card.endswith("Bank tasdig'i: ⏳ kutilmoqda")
    assert reply == "✅ Yozildi"
    assert buttons is not None
    # Both the footer rule and "a card of its own" hold for every later edit.
    [receipt] = await _receipts(bound)
    photo = await bound.get(m.Interaction, receipt.interaction_id)
    assert photo.meta[receipts.BOOKED_KEY] is True
    assert photo.meta[receipts.CARD_ALONE_KEY] is True
    assert receipts.card_editable(photo)


# --- the owner's card ----------------------------------------------------------------


class _Bot:
    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.fail = fail

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append((text, kwargs))
        return SimpleNamespace(message_id=900 + len(self.sent))


async def test_receipt_card_sent_once(session):
    row, _ = await _chat_photo(session)
    session.add(m.ChatMonitor(tg_chat_id=CHAT, chat_type=ChatType.private, title="Akmal"))
    receipt = await receipts.record(session, row, _reading(), presume_receipt=False)
    await session.commit()

    bot = _Bot()
    await worker.receipt_notice_job(bot, now=NOW.replace(hour=12))
    await worker.receipt_notice_job(bot, now=NOW.replace(hour=12))

    [(text, kwargs)] = bot.sent
    assert text == (
        "🧾 Chek · Akmal · Telegram\n"
        f"Chekda: 1.25 mln so'm · {day_label(PAID)} {clock(PAID)}\n"
        "To'lovchi: Akmal T. → Sardor Q. ****1234\n"
        "Payme · № 123456789\n"
        "Bank tasdig'i: ⏳ kutilmoqda\n"
        "Summa rasmdan o'qildi; hisobga faqat bank SMS yoki Payme tasdig'i bilan "
        "yoziladi."
    )
    assert kwargs["disable_notification"] is False
    await session.refresh(receipt)
    assert receipt.notified_at is not None
    assert receipt.notice_tg_message_id == 901
    # A card is not a tap-request: the question budget is untouched.
    assert await questions.spent_today(session, now=NOW) == 0


async def test_a_group_card_names_the_group_and_unreadable_says_so(session):
    row, _ = await _chat_photo(session)
    session.add(m.ChatMonitor(tg_chat_id=CHAT, chat_type=ChatType.group, title="Yuk"))
    await receipts.record(
        session, row, _reading(receipt=_fields(amount=None)), presume_receipt=False
    )
    await session.commit()
    bot = _Bot()
    await worker.receipt_notice_job(bot, now=NOW.replace(hour=12))
    assert [text for text, _ in bot.sent] == [replies.RECEIPT_UNREADABLE]


async def test_no_card_for_own_old_or_bot_receipts(session):
    own, person = await _chat_photo(session, direction=Direction.out)
    await receipts.record(session, own, _reading(), presume_receipt=False)
    old, _ = await _chat_photo(session, person=person, at=NOW - timedelta(days=3))
    await receipts.record(session, old, _reading(), presume_receipt=False)
    bot_photo = m.Interaction(
        source=InteractionSource.receipt_photo,
        direction=Direction.in_,
        occurred_at=NOW,
    )
    session.add(bot_photo)
    await session.flush()
    await receipts.record(session, bot_photo, _reading(), presume_receipt=True)
    await session.commit()

    bot = _Bot()
    await worker.receipt_notice_job(bot, now=NOW.replace(hour=12))
    assert bot.sent == []


async def test_cards_wait_through_quiet_hours_and_a_failed_send(session, monkeypatch):
    row, _ = await _chat_photo(session)
    receipt = await receipts.record(session, row, _reading(), presume_receipt=False)
    await session.commit()
    night = NOW.replace(hour=23, minute=45)

    bot = _Bot()
    await worker.receipt_notice_job(bot, now=night)
    assert bot.sent == []
    await worker.receipt_notice_job(_Bot(fail=True), now=NOW.replace(hour=12))
    await session.refresh(receipt)
    assert receipt.notified_at is None

    monkeypatch.setattr(settings, "money_receipts_silent_at_night", True)
    await worker.receipt_notice_job(bot, now=night)
    [(_, kwargs)] = bot.sent
    assert kwargs["disable_notification"] is True


def test_the_card_job_is_registered_every_minute():
    source = Path(worker.__file__).read_text()
    start = source.index("        receipt_notice_job,\n")
    block = source[start : source.index("scheduler.add_job", start)]
    for needle in (
        "IntervalTrigger(minutes=1)",
        'id="receipt_notices"',
        "max_instances=1",
        "coalesce=True",
    ):
        assert needle in block, needle


# --- forgetting and merging ------------------------------------------------------------


async def test_purge_and_merge_cover_receipts(session):
    row, akmal = await _chat_photo(session)
    await receipts.record(session, row, _reading(), presume_receipt=False)
    twin = m.Person(display_name="Акмал", aliases=[])
    session.add(twin)
    await session.flush()

    # Merging moves the receipt with everything else.
    plan = await people.merge_plan(session, akmal, twin)
    assert plan["receipts"] == 1
    await people.merge_into(session, akmal, twin, by="command")
    [receipt] = await _receipts(session)
    assert receipt.person_id == twin.id

    # A receipt in someone else's chat that prints their name goes too, and
    # the reading mirrored on the message that stays.
    other = m.Person(display_name="Bobur", aliases=[])
    session.add(other)
    await session.flush()
    kept, _ = await _chat_photo(session, person=other)
    await receipts.record(
        session,
        kept,
        _reading(receipt=_fields(payer_name="Akmal Toshev", payee_name="Bobur")),
        presume_receipt=False,
    )
    await session.flush()

    plan = await purge.plan_person(session, twin)
    assert plan.counts["receipts"] == 2
    assert "chek: 2 ta" in replies.purge_preview(plan)
    await purge.execute(session, plan)
    await session.flush()
    assert await _receipts(session) == []
    await session.refresh(kept)
    assert "receipt" not in kept.meta

    # A chat purge counts them and the cascade takes them.
    row, _ = await _chat_photo(session, person=other)
    await receipts.record(session, row, _reading(), presume_receipt=False)
    chat_plan = await purge.plan_chat(session, CHAT)
    assert chat_plan.counts["receipts"] == 1
    await purge.execute(session, chat_plan)
    await session.flush()
    assert await _receipts(session) == []

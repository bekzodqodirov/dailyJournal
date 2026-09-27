"""WP-41: payment-app notifications through the same reader and booking
service as a bank SMS."""

from __future__ import annotations

from datetime import datetime, timedelta

import sqlalchemy as sa

from miya.config import settings
from miya.db import models as m
from miya.db.enums import InteractionSource
from miya.services import money_notices, phone_events
from tests.test_phone_events_api import DEVICE, client, device  # noqa: F401

PKG = "uz.dida.payme"
PAID = "Платёж успешно проведён\nKORZINKA\n25 000 сум"
ADVERT = (
    "Click orqali barcha to'lovlar uchun 30 000 so'mgacha keshbek! Batafsil: click.uz"
)
DECLINED = "Оплата 250 000 сум отклонена. Карта *1234"


def _at(minutes: int = 30) -> str:
    moment = datetime.now(settings.tz) - timedelta(minutes=minutes)
    return moment.replace(microsecond=0).isoformat()


def _push(notification_id=1, *, text=PAID, package=PKG, at=None, **over) -> dict:
    item = {
        "package": package,
        "posted_at": at or _at(),
        "title": None,
        "text": text,
        "notification_id": notification_id,
    }
    item.update(over)
    return item


def _payload(*items, device_id=DEVICE) -> dict:
    return {"device_id": device_id, "notifications": list(items)}


async def _rows(session) -> list[m.Interaction]:
    return list(
        await session.scalars(
            sa.select(m.Interaction)
            .where(m.Interaction.source == InteractionSource.phone_notification)
            .order_by(m.Interaction.id)
        )
    )


async def _txns(session) -> list[m.Transaction]:
    return list(await session.scalars(sa.select(m.Transaction)))


async def test_a_device_may_post_notifications_and_read_nothing(session, device):  # noqa: F811
    response = device.post("/v1/phone/notifications", json=_payload())
    assert response.status_code == 200
    assert response.json() == {"accepted": 0, "duplicates": 0, "rejected": []}
    assert device.get("/v1/transactions").status_code == 401


async def test_the_batch_is_capped_and_a_bad_item_is_named(session, device):  # noqa: F811
    too_many = [_push(i) for i in range(51)]
    assert (
        device.post("/v1/phone/notifications", json=_payload(*too_many)).status_code
        == 422
    )
    response = device.post(
        "/v1/phone/notifications",
        json=_payload(_push(1), _push(2, posted_at="2026-09-25T10:00:00")),
    )
    assert response.json()["accepted"] == 1
    [rejected] = response.json()["rejected"]
    assert rejected["index"] == 1 and "posted_at" in rejected["reason"]


async def test_a_payment_push_books_one_transaction_and_queues_a_receipt(
    session,
    device,  # noqa: F811
):
    response = device.post("/v1/phone/notifications", json=_payload(_push()))
    assert response.json()["accepted"] == 1
    [row] = await _rows(session)
    [txn] = await _txns(session)
    assert txn.channel == f"app:{PKG}"
    assert row.media["package"] == PKG and row.processed is True
    queue = await money_notices.pending(session)
    assert [t.id for _, t in queue.fresh] == [txn.id]


async def test_a_repost_and_a_reinstall_are_duplicates(session, device):  # noqa: F811
    item = _push(at=_at(40))
    device.post("/v1/phone/notifications", json=_payload(item))
    again = device.post("/v1/phone/notifications", json=_payload(item))
    assert again.json()["duplicates"] == 1
    other_device = device.post(
        "/v1/phone/notifications",
        json=_payload({**item, "notification_id": 99}, device_id="another-device"),
    )
    assert other_device.json()["duplicates"] == 1
    assert len(await _rows(session)) == 1 and len(await _txns(session)) == 1


async def test_adverts_and_declines_book_nothing(session, device):  # noqa: F811
    device.post(
        "/v1/phone/notifications",
        json=_payload(_push(1, text=ADVERT), _push(2, text=DECLINED)),
    )
    advert, declined = await _rows(session)
    assert advert.needs_review is False
    assert advert.media["money"]["reason"] == "advert"
    assert declined.needs_review is True
    assert await _txns(session) == []


async def test_a_package_outside_the_allow_list_is_ignored(
    session,
    device,  # noqa: F811
    monkeypatch,
):
    monkeypatch.setattr(settings, "payment_app_packages", "uz.dida.payme, uz.click")
    device.post(
        "/v1/phone/notifications", json=_payload(_push(package="com.example.game"))
    )
    [row] = await _rows(session)
    assert row.media["money"] == {"verdict": "ignore", "reason": "not_payment_app"}
    assert await _txns(session) == []


def test_the_event_key_vector_shared_with_the_phone():
    """The phone's whenMs was 1727246400123; it sends whole seconds, so both
    sides hash 1727246400000. PaymentAppsTest.kt asserts the same hex."""
    when = datetime.fromisoformat("2024-09-25T11:40:00+05:00")
    assert phone_events._epoch_ms(when) == 1727246400000
    assert (
        phone_events.notification_event_key(
            "dev", "uz.example.app", 7, None, when, "T", "B"
        )
        == "dev:ntf:uz.example.app:db723ede0309a96d"
    )
    empty_big = phone_events.notification_body("x", "", None)
    missing_big = phone_events.notification_body("x", None, None)
    assert empty_big == missing_big == "x"
    assert (
        phone_events.notification_event_key(
            "dev", "uz.example.app", 7, None, when, "T", empty_big
        )
        == "dev:ntf:uz.example.app:a00c3d48443a1edd"
    )

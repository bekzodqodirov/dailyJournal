"""WP-10: book / review / ignore. A payment text is booked only when it is a
completed payment with a direction, an amount and evidence; everything else
waits in /tekshir or is ignored, and nothing is ever invented."""

from __future__ import annotations

import pytest

from miya.config import settings
from miya.services import sms_money
from tests.fixtures.payment_texts import AT, PAYMENT_TEXTS


def _id(case: dict) -> str:
    return case["body"][:40]


@pytest.mark.parametrize("case", PAYMENT_TEXTS, ids=_id)
def test_the_corpus(case):
    parsed = sms_money.read(case["body"], received_at=case["received_at"])
    assert (parsed.verdict.value, parsed.reason) == (case["verdict"], case["reason"])
    for key in ("type", "amount", "card_last4", "gs_codes", "waybills"):
        if key in case:
            assert getattr(parsed, key) == case[key], key


@pytest.mark.parametrize("case", PAYMENT_TEXTS, ids=_id)
def test_only_book_is_high(case):
    parsed = sms_money.read(case["body"], received_at=case["received_at"])
    assert (parsed.confidence == sms_money.HIGH) is (
        parsed.verdict is sms_money.Verdict.BOOK
    )


def test_parse_is_read_behind_the_sender_gate():
    body = "Hisobingizdan 25 000 so'm yechildi. Karta *1234"
    assert sms_money.parse("Beeline", body) is None
    parsed = sms_money.parse("Payme", body, received_at=AT)
    assert parsed.verdict is sms_money.Verdict.BOOK
    assert parsed.sender == "Payme"


@pytest.mark.parametrize(
    "name",
    ["Sardor Kodirov", "Begi", "Bega", "GSR Logistics", "Akmal", "Sardor", "KORZINKA.UZ"],
)
def test_no_marker_fires_inside_a_name(name):
    text = sms_money._norm(name)
    fired = [stem for stem, rx in sms_money.ALL_MARKERS if rx.search(text)]
    assert fired == []


def test_an_advert_goes_to_review_when_configured(monkeypatch):
    body = "Click orqali 30 000 so'mgacha keshbek! Batafsil: click.uz"
    assert sms_money.read(body).verdict is sms_money.Verdict.IGNORE
    monkeypatch.setattr(settings, "payment_adverts_to_review", True)
    parsed = sms_money.read(body)
    assert (parsed.verdict, parsed.reason) == (sms_money.Verdict.REVIEW, "advert")


def test_review_keeps_what_could_be_read_for_one_tap_booking():
    parsed = sms_money.read("Oplata 250 000 UZS otklonena. Karta *1234")
    assert parsed.type is sms_money.TransactionType.expense
    assert parsed.card_last4 == "1234"
    assert "otklon" in parsed.markers


def test_without_received_at_a_date_never_decides():
    parsed = sms_money.read("Oplata 25 000 sum 16.10.2099. Karta *1234")
    assert parsed.verdict is sms_money.Verdict.BOOK


def test_the_merchant_is_never_a_gs_code_or_waybill():
    parsed = sms_money.read("Oplata 250 000 so'm karta *1234\nYW26-004715 GS367")
    assert parsed.merchant is None

"""Build step 6: the deterministic money-SMS parser.

A bank SMS is the bank's record — it must never reach the extractor, so
this parser is all there is between a Payme notification and /xarajat.
Every rule from the contract is pinned one by one, and the failure mode is
always the honest one: a figure that cannot be read with certainty comes
back LOW with no amount, never a guess.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from miya.config import settings
from miya.db.enums import Currency, TransactionType
from miya.services import sms_money


def parse(sender, body):
    return sms_money.parse(sender, body)


# --- senders -----------------------------------------------------------------


def test_sender_normalisation_ignores_case_spaces_and_punctuation():
    assert sms_money.normalise_sender("Kapital Bank") == "kapitalbank"
    assert sms_money.normalise_sender("PAY-ME.") == "payme"
    assert sms_money.normalise_sender("8600") == "8600"
    assert sms_money.normalise_sender("") == ""


@pytest.mark.parametrize(
    "sender", ["Payme", "PAYME", "Click", "Uzum", "KAPITALBANK", "8600", "9860", "TBC"]
)
def test_the_baked_in_senders_are_recognised(sender):
    assert sms_money.is_payment_sender(sender)


@pytest.mark.parametrize("sender", ["Beeline", "Ucell", "+998901234567", "Akmal", ""])
def test_everyone_else_is_not_a_bank(sender):
    assert not sms_money.is_payment_sender(sender)
    assert parse(sender, "Oplata 25 000 sum") is None


def test_payment_sms_senders_extends_the_list_the_same_way(monkeypatch):
    assert not sms_money.is_payment_sender("MyBank")
    monkeypatch.setattr(settings, "payment_sms_senders", "My-Bank, o'zbank 777")
    assert sms_money.is_payment_sender("MYBANK")  # normalised like the rest
    assert sms_money.is_payment_sender("O'zBank")
    assert sms_money.is_payment_sender("777")
    assert "mybank" in sms_money.known_senders()
    assert sms_money.known_senders().issuperset(sms_money.DEFAULT_SENDERS)
    assert parse("My-Bank", "Oplata 25 000 sum").confidence == sms_money.HIGH


# --- direction keywords, one by one ------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "Oplata 25 000 sum",
        "Оплата 25 000 сум",
        "To'lov 25 000 so'm",
        "To‘lov 25 000 so'm",  # curly apostrophe
        "Pokupka 25 000 sum",
        "Покупка 25 000 сум",
        "Xarid 25 000 so'm",
        "Spisanie 25 000 sum",
        "Списание 25 000 сум",
        "Snyatie 25 000 sum",
        "Снятие 25 000 сум",
        "Chiqim 25 000 so'm",
        "Perevod na kartu 25 000 sum",
        "Перевод на карту 25 000 сум",
    ],
)
def test_every_expense_keyword(body):
    parsed = parse("Payme", body)
    assert parsed.confidence == sms_money.HIGH
    assert parsed.type is TransactionType.expense
    assert parsed.amount == Decimal("25000.00")


@pytest.mark.parametrize(
    "body",
    [
        "Postuplenie 1 500 000 sum",
        "Поступление 1 500 000 сум",
        "Popolnenie 1 500 000 sum",
        "Пополнение 1 500 000 сум",
        "Zachislenie 1 500 000 sum",
        "Зачисление 1 500 000 сум",
        "Kirim 1 500 000 so'm",
        "Tushum 1 500 000 so'm",
        "Perevod ot AKMAL 1 500 000 sum",
        "Перевод от AKMAL 1 500 000 сум",
    ],
)
def test_every_income_keyword(body):
    parsed = parse("Payme", body)
    assert parsed.confidence == sms_money.HIGH
    assert parsed.type is TransactionType.income
    assert parsed.amount == Decimal("1500000.00")


# --- the amount ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "amount"),
    [
        ("Oplata 25 000 sum", Decimal("25000.00")),
        ("Оплата 1 250 000 сум", Decimal("1250000.00")),  # NBSP
        ("Оплата 25 000 сум", Decimal("25000.00")),  # narrow NBSP
        ("Оплата 25 000 сум", Decimal("25000.00")),  # thin space
        ("Oplata 1.250.000 sum", Decimal("1250000.00")),  # dot thousands
        ("Oplata 1,250,000.50 sum", Decimal("1250000.50")),
        ("Oplata 1 250 000,50 sum", Decimal("1250000.50")),  # decimal comma
        ("Oplata: 50000.00 UZS", Decimal("50000.00")),
        ("Oplata 99000 sum", Decimal("99000.00")),
    ],
)
def test_thousand_separators_and_both_decimal_marks(body, amount):
    parsed = parse("Payme", body)
    assert parsed.confidence == sms_money.HIGH
    assert parsed.amount == amount


def test_the_amount_may_come_from_a_summa_line():
    parsed = parse("Payme", "Pokupka\nSumma: 99 000 so'm\nOstatok: 1 000 000 so'm")
    assert parsed.confidence == sms_money.HIGH
    assert parsed.type is TransactionType.expense
    assert parsed.amount == Decimal("99000.00")
    assert parsed.balance_after == Decimal("1000000.00")

    cyrillic = parse("Click", "Покупка\nСумма: 45 000 сум")
    assert cyrillic.confidence == sms_money.HIGH
    assert cyrillic.amount == Decimal("45000.00")


def test_dates_times_and_card_digits_are_never_the_amount():
    parsed = parse("Payme", "Оплата 25 000 сум 16.09.2026 14:30\nUZCARD *1234")
    assert parsed.amount == Decimal("25000.00")

    only_noise = parse("Payme", "Oplata 16.09.2026 14:30")
    assert only_noise.confidence == sms_money.LOW
    assert only_noise.amount is None


def test_a_zero_amount_is_no_amount():
    parsed = parse("Payme", "Oplata 0 sum")
    assert parsed.confidence == sms_money.LOW
    assert parsed.amount is None


# --- the balance trap ---------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "Оплата произведена\nОстаток: 500 000 сум",
        "Oplata bajarildi\nBalans: 500 000 so'm",
        "To'lov o'tdi\nQoldiq: 500 000 so'm",
        "Оплата прошла\nДоступно: 500 000 сум",
        "Oplata o'tdi\nOstatok: 500 000 sum",
    ],
)
def test_the_balance_is_never_the_amount(body):
    """The trap: the only figure sits in a balance clause. LOW, not a guess."""
    parsed = parse("Payme", body)
    assert parsed.confidence == sms_money.LOW
    assert parsed.amount is None
    assert parsed.balance_after == Decimal("500000.00")


def test_the_balance_clause_feeds_balance_after_next_to_a_real_amount():
    parsed = parse("Payme", "Оплата 25 000 сум\nОстаток: 1 250 000 сум")
    assert parsed.confidence == sms_money.HIGH
    assert parsed.amount == Decimal("25000.00")
    assert parsed.balance_after == Decimal("1250000.00")


# --- currency -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "currency"),
    [
        ("Oplata 25 000 sum", Currency.UZS),
        ("Оплата 25 000 сум", Currency.UZS),
        ("Оплата 25 000 сўм", Currency.UZS),
        ("Oplata 25 000 so'm", Currency.UZS),
        ("Oplata: 50000.00 UZS", Currency.UZS),
        ("Oplata $50", Currency.USD),
        ("Spisanie 300 usd", Currency.USD),
        ("Оплата 300 доллар", Currency.USD),
        ("Oplata 25 000", Currency.UZS),  # no token: the default
    ],
)
def test_currency_comes_from_the_adjacent_token_and_defaults_to_uzs(body, currency):
    parsed = parse("Payme", body)
    assert parsed.confidence == sms_money.HIGH
    assert parsed.currency is currency


# --- card and merchant --------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "last4"),
    [
        ("Oplata 25 000 sum karta *1234", "1234"),
        ("Oplata 25 000 sum karta ****5678", "5678"),
        ("Oplata 25 000 sum karta 8600****4321", "4321"),
        ("Oplata 25 000 sum", None),
    ],
)
def test_the_card_mask_yields_last4(body, last4):
    assert parse("Payme", body).card_last4 == last4


def test_merchant_from_the_labelled_line_and_the_trailing_caps_run():
    labelled = parse("Payme", "Oplata 25 000 sum\nJoy: Korzinka Yunusobod")
    assert labelled.merchant == "Korzinka Yunusobod"
    cyrillic = parse("Payme", "Оплата 25 000 сум\nМесто: KORZINKA.UZ")
    assert cyrillic.merchant == "KORZINKA.UZ"

    caps = parse("Payme", "Оплата 25 000 сум\nUZCARD *1234\nKORZINKA.UZ\nОстаток: 1 сум")
    assert caps.merchant == "KORZINKA.UZ"
    # Card brands, currency codes and bare figures are not merchants.
    none = parse("Payme", "Oplata 25 000 UZS\nUZCARD *1234")
    assert none.merchant is None


# --- the category map ---------------------------------------------------------


@pytest.mark.parametrize(
    ("merchant", "body", "category"),
    [
        ("KORZINKA.UZ", "", "oziq-ovqat"),
        ("MAKRO SUPERMARKET", "", "oziq-ovqat"),
        ("YANDEX.TAXI", "", "transport"),
        (None, "taxi uchun to'lov", "transport"),
        (None, "gaz uchun to'lov", "kommunal"),
        ("SVET TOLOVI", "", "kommunal"),
        ("MAGAZIN", "", "other"),  # "gaz" must not match inside a word
        (None, "", "other"),
    ],
)
def test_the_keyword_category_map(merchant, body, category):
    assert sms_money.category_of(merchant, body) == category


# --- confidence ---------------------------------------------------------------


def test_a_bank_sms_with_no_direction_is_low_and_amountless():
    parsed = parse("Payme", "Vash kod: 1234. Nikomu ne soobshchayte")
    assert parsed.confidence == sms_money.LOW
    assert parsed.amount is None
    assert parsed.sender == "Payme"


def test_the_full_payme_shape_end_to_end():
    parsed = parse(
        "Payme",
        "Оплата 25 000 сум\nUZCARD *1234\nKORZINKA.UZ\n⏰ 14:30 16.09.2026\n"
        "Остаток: 1 250 000 сум",
    )
    assert parsed.confidence == sms_money.HIGH
    assert parsed.type is TransactionType.expense
    assert parsed.amount == Decimal("25000.00")
    assert parsed.currency is Currency.UZS
    assert parsed.card_last4 == "1234"
    assert parsed.merchant == "KORZINKA.UZ"
    assert parsed.balance_after == Decimal("1250000.00")


def test_the_click_one_liner_shape():
    parsed = parse(
        "Click", "Оплата: 50000.00 UZS. Карта: 8600****1234. Остаток: 150000.00 UZS"
    )
    assert parsed.confidence == sms_money.HIGH
    assert parsed.amount == Decimal("50000.00")
    assert parsed.card_last4 == "1234"
    assert parsed.balance_after == Decimal("150000.00")


def test_the_last_star_run_is_the_card_not_the_leading_bin():
    """ "UZCARD *8600**1234" ends in the card; 8600 is only the BIN."""
    parsed = sms_money.parse("UZCARD", "Oplata: 50 000 UZS. Karta: *8600**1234")
    assert parsed is not None and parsed.card_last4 == "1234"

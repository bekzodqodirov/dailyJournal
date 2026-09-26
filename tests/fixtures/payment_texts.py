"""The payment-text corpus (WP-10): one case per shape the reader must get
right. Both tests/test_sms_money_safety.py and future packages read it; real
texts from the owner's phone are added here (WP-82), anonymised — no real
names, card digits or phone numbers.

Each case: body, received_at (or None), expected verdict and reason, and
where it matters the direction, amount, card, GS codes and waybills.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from miya.config import settings
from miya.db.enums import TransactionType

EXPENSE = TransactionType.expense
INCOME = TransactionType.income
AT = datetime(2026, 9, 25, 12, 0, tzinfo=settings.tz)


def case(body, verdict, reason, **expected):
    return {
        "body": body,
        "received_at": expected.pop("received_at", AT),
        "verdict": verdict,
        "reason": reason,
        **expected,
    }


PAYMENT_TEXTS: list[dict] = [
    # --- declined, reversed, pending: never booked ---------------------------
    case(
        "Oplata 250 000 UZS otklonena. Karta *1234",
        "review",
        "declined",
        amount=Decimal("250000.00"),
    ),
    case("Оплата 250 000 сум отклонена. Карта *1234", "review", "declined"),
    case(
        "Oplata ne proshla: 250 000 UZS, karta *1234, nedostatochno sredstv",
        "review",
        "declined",
    ),
    case("To'lov amalga oshmadi: 250 000 so'm. Karta *1234", "review", "declined"),
    case(
        "To'lov bekor qilindi: 250 000 so'm qaytarildi. Karta *1234",
        "review",
        "reversal",
    ),
    case(
        "Возврат 250 000 сум на карту *1234. Отмена оплаты KORZINKA",
        "review",
        "reversal",
    ),
    case("Blokirovka 250 000 UZS karta *1234", "review", "pending"),
    # --- reminders and adverts ------------------------------------------------
    case(
        "Uzum Nasiya: 15.10.2026 sanasida 450 000 so'm to'lov yechiladi",
        "review",
        "reminder",
    ),
    case(
        "Click orqali barcha to'lovlar uchun 30 000 so'mgacha keshbek! "
        "Batafsil: click.uz",
        "ignore",
        "advert",
    ),
    case("Кешбэк 3 000 сум зачислен на карту *1234", "review", "advert_with_evidence"),
    # --- one-time codes ----------------------------------------------------------
    case("To'lovni tasdiqlash kodi: 482913. Summa: 150 000 so'm", "ignore", "otp"),
    case("Kartangiz *1234 bilan to'lov qilish uchun kod: 5521", "ignore", "otp"),
    case("Vash kod: 1234. Nikomu ne soobshchayte", "ignore", "otp"),
    case(
        "Oplata 25 000 so'm muvaffaqiyatli. Karta *1234. Terminal kodi: 5411",
        "review",
        "otp_conflict",
        amount=Decimal("25000.00"),
    ),
    # --- completed payments -------------------------------------------------------
    case(
        "Payme: 1 500 000 so'm kartangizga tushdi. Karta *1234. Izoh: mijoz kodi GS367",
        "book",
        "ok",
        type=INCOME,
        amount=Decimal("1500000.00"),
        gs_codes=("GS367",),
    ),
    case(
        "Perevod ot SARDOR KODIROV 1 000 000 sum karta *5678",
        "book",
        "ok",
        type=INCOME,
        amount=Decimal("1000000.00"),
        card_last4="5678",
    ),
    case(
        "GS367 uchun to'lov 1 500 000 so'm qabul qilindi. Karta *1234",
        "book",
        "ok",
        type=INCOME,
        amount=Decimal("1500000.00"),
        gs_codes=("GS367",),
    ),
    case(
        "Oplata YW26-004715 GS367 250 000 so'm karta *1234",
        "book",
        "ok",
        type=EXPENSE,
        amount=Decimal("250000.00"),
        waybills=("YW26-004715",),
        gs_codes=("GS367",),
    ),
    case("Karta *1234 hisobiga 500 000 so'm tushdi", "book", "ok", type=INCOME),
    case(
        "Hisobingizdan 25 000 so'm yechildi. Karta *1234",
        "book",
        "ok",
        type=EXPENSE,
        amount=Decimal("25000.00"),
    ),
    case(
        "Вам поступил перевод 500 000 сум от AKMAL A.",
        "book",
        "ok",
        type=INCOME,
        amount=Decimal("500000.00"),
    ),
    case(
        "Платёж успешно проведён\nKORZINKA\n25 000 сум",
        "book",
        "ok",
        type=EXPENSE,
        amount=Decimal("25000.00"),
    ),
    case(
        "HUMOCARD *1234: oplata 25000.00 UZS; KORZINKA; 16.09.26 14:30; "
        "balans: 1250000.00 UZS",
        "book",
        "ok",
        type=EXPENSE,
        amount=Decimal("25000.00"),
        card_last4="1234",
    ),
    case(
        "Оплата 25 000 сум\nUZCARD •• 1234\nKORZINKA.UZ",
        "book",
        "ok",
        card_last4="1234",
    ),
    case("Oplata 25 000 sum\nKarta **** 1234", "book", "ok", card_last4="1234"),
    # --- not enough to book ----------------------------------------------------------
    case("Oplata 25 000 sum", "review", "no_evidence", amount=Decimal("25000.00")),
    case(
        "Perevod ot AKMAL 100 000 sum. Spisanie 100 000 sum. Karta *1234",
        "review",
        "conflict",
    ),
    case("Karta *1234 bo'yicha 250 000 so'm", "review", "no_direction"),
    case("Oplata. Karta *1234", "review", "no_amount"),
    case("Hurmatli mijoz, bank ertaga ishlamaydi", "ignore", "info"),
    case(
        "Oplata 25 000 sum 16.10.2026. Karta *1234",
        "review",
        "future_date",
    ),
]

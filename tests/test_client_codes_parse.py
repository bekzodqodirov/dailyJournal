"""WP-29: GS client codes and YW waybills, parsed by one regex."""

from __future__ import annotations

import pytest

from miya.services import sms_money, text
from miya.services.codes import (
    canonical_client_code,
    find_client_codes,
    find_waybills,
    split_codes,
)


@pytest.mark.parametrize(
    "raw",
    [
        "GS367",
        "gs367",
        "GS 367",
        "gs-367",
        "GS–367",
        "GS.367",
        "GS#367",
        "GS №367",
        "ГС367",
        "гс-367",
        "GС367",
        "GS0367",
    ],
)
def test_canonical_client_code(raw):
    assert canonical_client_code(raw) == "GS367"


@pytest.mark.parametrize("raw", ["GSR367", "GS", "GS1234567", "Akmal GS367", "GS367A"])
def test_not_a_code(raw):
    assert canonical_client_code(raw) is None


@pytest.mark.parametrize(
    ("raw", "codes"),
    [
        ("GS367 va GS412ga yuk keldi", ["GS367", "GS412"]),
        ("GS367ning yuki", ["GS367"]),
        ("ГС367 келди", ["GS367"]),
        ("GS 367 GS367", ["GS367"]),
        ("GSR Logistics", []),
        ("bags 20", []),
        ("logs367", []),
        ("GS367ta", []),
        # A harmless false positive: only a held code ever affects identity.
        ("GS25 do'konida", ["GS25"]),
        ("GS5", ["GS5"]),
    ],
)
def test_find_client_codes(raw, codes):
    assert find_client_codes(raw) == codes


def test_one_regex_everywhere():
    reading = sms_money.read("GS5 250 000 so'm karta *1234")
    assert reading.gs_codes == ("GS5",)
    assert str(reading.amount) == "250000.00"
    assert "gs5" in text.normalise_for_search("GS 5").split()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Akmal (GS367)", (["GS367"], "Akmal")),
        ("GS367 — Akmal aka", (["GS367"], "Akmal aka")),
        ("GS367", (["GS367"], "")),
    ],
)
def test_split_codes(name, expected):
    assert split_codes(name) == expected


@pytest.mark.parametrize(
    ("raw", "waybills"),
    [
        ("YW26-004715", ["YW26-004715"]),
        ("yw26-004715", ["YW26-004715"]),
        ("YW26 – 004715", ["YW26-004715"]),
        ("YW26004715", ["YW26-004715"]),
        ("XY26-004715", []),
        ("YW26-004715-2", ["YW26-004715"]),
    ],
)
def test_find_waybills(raw, waybills):
    assert find_waybills(raw) == waybills

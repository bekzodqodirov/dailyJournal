"""WP-28: one normaliser for Uzbek Latin/Cyrillic, Russian, missing
apostrophes, GS codes and waybills."""

from __future__ import annotations

import pytest

from miya.services.text import (
    normalise_for_search,
    query_terms,
    stem_query_token,
    to_prefix_tsquery,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Bo'ldi", "boldi"),
        ("Boʻldi", "boldi"),
        ("boldi", "boldi"),
        ("Бўлди", "boldi"),
        ("Qarz", "karz"),
        ("карз", "karz"),
        ("Қарз", "karz"),
        ("G'ani", "gani"),
        ("Ғани", "gani"),
        ("YW26-004715", "yw26-004715"),
        ("Контейнер", "konteyner"),
        ("Wang Wei", "vang vei"),
        ("salom 😀!!", "salom"),
    ],
)
def test_normalise_for_search(text, expected):
    assert normalise_for_search(text) == expected


@pytest.mark.parametrize("text", ["GS 367", "gs-367", "GS367", "ГС 367"])
def test_gs_codes_are_joined(text):
    assert "gs367" in normalise_for_search(text).split()


def test_query_terms_drop_the_question_around_the_subject():
    text = "Akmal bilan konteyner masalasi bo'lgandi, nima deb o'ylaysan?"
    assert query_terms(text) == ["akmal", "konteyner"]


@pytest.mark.parametrize(
    ("token", "stem"),
    [
        ("konteynerni", "konteyner"),
        ("bojxonada", "bojxona"),
        ("oplatu", "oplat"),
        ("yuk", "yuk"),
    ],
)
def test_stem_query_token(token, stem):
    assert stem_query_token(token) == stem


def test_to_prefix_tsquery():
    assert (
        to_prefix_tsquery(["konteyner", "yw26-004715"])
        == "'konteyner':* | 'yw26-004715':*"
    )
    assert to_prefix_tsquery(["a'b"]) is None

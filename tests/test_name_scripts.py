"""WP-28: a person is the same person in Latin and Cyrillic."""

from __future__ import annotations

import sqlalchemy as sa

from miya.db import models as m
from miya.services.people import best_match, find_person, normalise, resolve_person


def test_normalise_folds_script_and_honorifics():
    assert normalise("Бекзод ака") == "bekzod"
    assert normalise("Шерзод") == "sherzod"
    assert normalise("Ғайрат") == "g'ayrat"


def test_best_match_across_scripts():
    akmal = m.Person(display_name="Акмал", aliases=[])
    xurshid = m.Person(display_name="Хуршид", aliases=[])
    assert best_match("Akmal", [akmal]) == (akmal, 100)
    assert best_match("Xurshid", [xurshid])[1] == 100


async def test_resolve_person_reuses_the_cyrillic_person(session):
    akmal = m.Person(display_name="Акмал", aliases=[])
    session.add(akmal)
    await session.flush()
    found = await resolve_person(session, "Akmal")
    assert found.id == akmal.id
    assert "Akmal" in (found.aliases or [])
    assert await session.scalar(sa.select(sa.func.count(m.Person.id))) == 1


async def test_find_person_across_scripts(session):
    sherzod = m.Person(display_name="Sherzod", aliases=[])
    session.add(sherzod)
    await session.flush()
    match = await find_person(session, "Шерзод")
    assert match.person is not None and match.person.id == sherzod.id

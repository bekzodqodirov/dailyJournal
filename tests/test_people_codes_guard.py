"""WP-29: codes compare exactly — GS368 is never a spelling of GS367."""

from __future__ import annotations

import sqlalchemy as sa

from miya.db import models as m
from miya.services.people import best_match, resolve_person


def _p(name: str) -> m.Person:
    return m.Person(display_name=name, aliases=[])


def test_best_match_codes():
    akmal = _p("Akmal GS367")
    assert best_match("Akmal GS368", [akmal]) == (akmal, 0.0)
    assert best_match("gs-367", [akmal]) == (akmal, 100.0)
    assert best_match("Akmal", [akmal]) == (akmal, 100.0)
    assert best_match("GS368", [_p("GS367")])[1] == 0.0


async def test_a_different_code_is_a_different_person(session):
    old = _p("Akmal GS367")
    session.add(old)
    await session.flush()
    new = await resolve_person(session, "Akmal GS368")
    assert new.id != old.id
    assert old.aliases == []
    assert await session.scalar(sa.select(sa.func.count(m.Person.id))) == 2


async def test_no_alias_with_a_code_is_learned(session):
    old = _p("Akmal GS367")
    session.add(old)
    await session.flush()
    found = await resolve_person(session, "Akmal aka GS367")
    assert found.id == old.id
    assert not any("GS" in alias.upper() for alias in found.aliases or [])

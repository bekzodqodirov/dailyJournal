"""JV-11: Jarvis policy — modes, the env ceiling, pauses, breakers, reply rules
and hard exclusions, chat keys. The resolution functions are pure; the writers
run against real PostgreSQL."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa

from miya.config import settings
from miya.db import models as m
from miya.jarvis import chatkey, policy

NOW = datetime.now(settings.tz).replace(hour=12, minute=0, second=0, microsecond=0)


def _policies(**modes: str | None) -> dict[str, policy.PolicySnapshot]:
    scopes = {key.replace("__", ":"): value for key, value in modes.items()}
    return {
        scope: policy.PolicySnapshot(scope, mode=policy.as_mode(mode))
        for scope, mode in scopes.items()
    }


def test_the_most_specific_scope_wins():
    policies = _policies(
        **{
            "global": "draft",
            "channel__telegram": "auto",
            "intent__greeting": "shadow",
            "channel__telegram__intent__greeting": "auto",
        }
    )
    resolve = policy.stored_mode
    assert resolve(policies, "telegram", "greeting") == "auto"
    assert resolve(policies, "whatsapp", "greeting") == "shadow"
    assert resolve(policies, "telegram", "info_gathering") == "auto"
    assert resolve(policies, "gmail", None) == "draft"
    # NULL inherits; nothing at all is the migration's seed.
    inherit = _policies(**{"global": "draft", "channel__telegram": None})
    assert resolve(inherit, "telegram", None) == "draft"
    assert resolve({}, "telegram", None) == policy.DEFAULT_MODE == "shadow"


def test_the_env_ceiling_is_never_exceeded(monkeypatch):
    policies = _policies(**{"global": "auto"})
    for ceiling, expected in (
        ("auto", "auto"),
        ("draft", "draft"),
        ("shadow", "shadow"),
        ("off", "off"),
    ):
        assert (
            policy.effective_mode(policies, "telegram", "greeting", env_ceiling=ceiling)
            == expected
        )
    monkeypatch.setattr(settings, "jarvis_enabled", False)
    monkeypatch.setattr(settings, "jarvis_mode", "auto")
    assert policy.ceiling() == "off"
    monkeypatch.setattr(settings, "jarvis_enabled", True)
    assert policy.ceiling() == "auto"
    assert settings.model_fields["jarvis_mode"].default == "draft"


def test_pauses_and_breakers():
    open_ended = policy.PolicySnapshot("global", pause_reason="owner")
    assert open_ended.paused(NOW) and open_ended.paused(NOW + timedelta(days=400))
    timed = policy.PolicySnapshot(
        "channel:telegram", breaker_until=NOW + timedelta(minutes=5), breaker_reason="x"
    )
    assert timed.tripped(NOW) and not timed.tripped(NOW + timedelta(minutes=5))
    stops = {"global": open_ended}
    assert policy.stop_for(stops, "gmail", NOW) == policy.Stop("paused", None)
    stops = {"channel:telegram": timed}
    assert policy.stop_for(stops, "telegram", NOW) == policy.Stop(
        "breaker", NOW + timedelta(minutes=5)
    )
    assert policy.stop_for(stops, "gmail", NOW) is None


async def test_pause_resume_and_breaker_are_recorded(session):
    row = await policy.pause(session, by="command", now=NOW)
    assert (
        row.scope == "global" and row.pause_reason == "owner" and row.paused_until is None
    )
    await policy.trip_breaker(
        session,
        channel="telegram",
        reason="peer_flood",
        until=NOW + timedelta(hours=1),
        by="gate",
        now=NOW,
    )
    policies = await policy.load(session)
    assert policies["global"].paused(NOW)
    assert policies["channel:telegram"].tripped(NOW)

    await policy.resume(
        session, scope=policy.channel_scope("telegram"), by="command", now=NOW
    )
    await policy.resume(session, by="command", now=NOW)
    policies = await policy.load(session)
    assert not policies["global"].paused(NOW)
    assert not policies["channel:telegram"].tripped(NOW)
    rows = {r.scope: r for r in await session.scalars(sa.select(m.JarvisPolicy))}
    assert [e["field"] for e in rows["global"].history] == ["pause", "pause"]
    assert [e["new"] for e in rows["global"].history] == ["owner", None]
    assert [e["field"] for e in rows["channel:telegram"].history] == [
        "breaker",
        "breaker",
    ]

    await policy.set_mode(
        session, scope="intent:greeting", mode="draft", by="command", now=NOW
    )
    await policy.set_mode(
        session, scope="intent:greeting", mode="draft", by="command", now=NOW
    )
    greeting = await session.get(m.JarvisPolicy, "intent:greeting")
    assert greeting.mode == "draft" and len(greeting.history) == 1


async def test_a_scope_that_is_not_one_is_refused(session):
    session.add(m.JarvisPolicy(scope="channel:", mode="draft"))
    with pytest.raises(sa.exc.IntegrityError):
        await session.flush()
    await session.rollback()


def _rule(mode="never", subject="person", reason="personal", **kw) -> policy.RuleSnapshot:
    return policy.RuleSnapshot(
        id=kw.pop("id", 1), subject=subject, mode=mode, reason=reason, **kw
    )


def test_rule_precedence():
    never = _rule("never", "address", address="tg:1")
    chat_auto = _rule("auto", "chat", "owner", id=2, chat_key="tg:1")
    person_draft = _rule("draft", "person", "owner", id=3, person_id=5)
    # Any never rule wins, whatever else applies.
    assert policy.never_rule([chat_auto, person_draft, never]) is never
    # Then the chat rule, then the person's.
    assert policy.rule_mode([person_draft, chat_auto]) == "auto"
    assert policy.rule_mode([person_draft]) == "draft"
    assert policy.rule_mode([]) is None
    group = _rule("draft", "chat", "group", chat_key="tg:-5")
    assert policy.group_enabled([group])
    assert not policy.group_enabled([_rule("never", "chat", "group", chat_key="tg:-5")])
    assert not policy.group_enabled([chat_auto])


def test_hard_exclusions():
    def facts(**kw) -> policy.ChatFacts:
        return policy.ChatFacts(chat_key="tg:1", channel="telegram", **kw)

    assert policy.hard_exclusion(facts(is_self=True)) == "saved_messages"
    assert policy.hard_exclusion(facts(tg_chat_id=777000)) == "service_chat"
    assert policy.hard_exclusion(facts(is_bot=True)) == "bot_chat"
    assert policy.hard_exclusion(facts(chat_type="channel")) == "channel_chat"
    assert policy.hard_exclusion(facts(is_owner=True)) == "owner_alias"
    assert policy.hard_exclusion(facts(chat_type="private", tg_chat_id=5)) is None
    assert policy.looks_like_bot("Helper_Bot") and not policy.looks_like_bot("akmal")
    assert not policy.looks_like_bot(None)


async def test_load_rules_finds_chat_person_and_address(session):
    person = m.Person(display_name="Akmal", aliases=[])
    session.add(person)
    await session.flush()
    session.add_all(
        [
            m.ReplyRule(subject="chat", chat_key="tg:1", mode="draft", reason="owner"),
            m.ReplyRule(
                subject="person", person_id=person.id, mode="never", reason="personal"
            ),
            m.ReplyRule(
                subject="address", address="tg:9", mode="never", reason="personal"
            ),
            m.ReplyRule(
                subject="address",
                address="tg:8",
                mode="never",
                reason="spam",
                state="revoked",
            ),
        ]
    )
    await session.flush()
    rules = await policy.load_rules(
        session, chat_key="tg:1", person_id=person.id, addresses=["tg:9", "tg:8"]
    )
    assert sorted((r.subject, r.mode) for r in rules) == [
        ("address", "never"),
        ("chat", "draft"),
        ("person", "never"),
    ]
    # One active rule per chat, address and person.
    session.add(m.ReplyRule(subject="chat", chat_key="tg:1", mode="auto", reason="owner"))
    with pytest.raises(sa.exc.IntegrityError):
        await session.flush()
    await session.rollback()


def test_chat_keys():
    assert chatkey.telegram(-1001234) == "tg:-1001234"
    assert (
        chatkey.whatsapp("998901234567@s.whatsapp.net")
        == "wa:998901234567@s.whatsapp.net"
    )
    assert chatkey.gmail("18c2f") == "mail:18c2f"
    parsed = chatkey.parse("tg:-55")
    assert parsed == chatkey.ChatKey("telegram", "-55") and str(parsed) == "tg:-55"
    assert chatkey.tg_chat_id("tg:-55") == -55
    assert chatkey.tg_chat_id("wa:x@s.whatsapp.net") is None
    assert chatkey.channel_of("mail:1") == "gmail"
    for bad in ("", "tg:", "tg:abc", "sms:1", "telegram"):
        with pytest.raises(chatkey.BadChatKey):
            chatkey.parse(bad)
    assert chatkey.channel_word("Telegram") == "telegram"
    assert chatkey.channel_word("pochta") == "gmail"
    assert chatkey.channel_word("faks") is None

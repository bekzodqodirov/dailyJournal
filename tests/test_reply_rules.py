"""JV-12: the owner's personal deny-list and the group reply policy.

The precedence (§5.4) is pure and tested exhaustively; marking, unmarking,
group switches, merge and purge run against real PostgreSQL; the commands
and the jr: buttons run through the bot handlers with a fake Telegram.
"""

from __future__ import annotations

import itertools
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa
from aiogram.filters import CommandObject

from miya.bot import handlers, keyboards, replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import ChatType, Direction, InteractionSource
from miya.jarvis import chatkey, gate, outbox, policy, status
from miya.services import codes as client_codes
from miya.services import people, purge

TZ = settings.tz
NOW = datetime.now(TZ).replace(hour=12, minute=0, second=0, microsecond=0)
OWNER_ID = 990001
DM = 7201
KEY = chatkey.telegram(DM)
GROUP = -100777
GROUP_KEY = chatkey.telegram(GROUP)
JID = "998901234567@s.whatsapp.net"


class PassGuard:
    def check(self, body, *, role, allowed_tokens):
        return gate.GuardResult(verdict="pass", checker_version="test")


@pytest.fixture
def jarvis(monkeypatch):
    """Jarvis on with every ceiling at auto: no setting holds a rule back."""
    monkeypatch.setattr(settings, "jarvis_enabled", True)
    monkeypatch.setattr(settings, "jarvis_mode", "auto")
    monkeypatch.setattr(settings, "owner_telegram_id", OWNER_ID)
    monkeypatch.setattr(settings, "jarvis_send_min_gap_seconds", 0)
    monkeypatch.setattr(outbox, "channel_send_enabled", lambda channel: True)
    gate.register_guard(PassGuard())
    yield
    gate.register_guard(None)


@pytest.fixture
def bound(session, monkeypatch):
    @asynccontextmanager
    async def _scope():
        yield session
        await session.flush()

    monkeypatch.setattr(handlers, "session_scope", _scope)
    return session


class _Message:
    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []
        self.edited: list[tuple[str, object]] = []

    async def answer(self, text, **kwargs):
        self.sent.append((text, kwargs.get("reply_markup")))

    async def edit_text(self, text, **kwargs):
        self.edited.append((text, kwargs.get("reply_markup")))


class _Callback:
    def __init__(self, data: str, message: _Message | None = None) -> None:
        self.data = data
        self.message = message or _Message()
        self.answered = 0

    async def answer(self, *args, **kwargs):
        self.answered += 1


def _payloads(markup) -> list[str]:
    if markup is None:
        return []
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def _labels(markup) -> list[str]:
    if markup is None:
        return []
    return [b.text for row in markup.inline_keyboard for b in row]


async def _command(handler, name: str, args: str | None) -> tuple[str, object]:
    message = _Message()
    if handler is handlers.cmd_personal_list:
        await handler(message)
    else:
        await handler(message, CommandObject(prefix="/", command=name, args=args))
    [sent] = message.sent
    return sent


async def _tap(data: str, message: _Message | None = None) -> _Callback:
    callback = _Callback(data, message)
    await handlers.on_rule_button(callback)
    assert callback.answered >= 1 or callback.message.edited
    return callback


async def _person(session, name="Akmal", *, telegram_id=DM, phone=None, **kw):
    person = m.Person(
        display_name=name, aliases=[], telegram_id=telegram_id, phone=phone, **kw
    )
    session.add(person)
    await session.flush()
    return person


async def _chat(session, tg_chat_id=DM, chat_type=ChatType.private, title="Akmal"):
    monitor = m.ChatMonitor(
        tg_chat_id=tg_chat_id, chat_type=chat_type, title=title, monitor_enabled=True
    )
    session.add(monitor)
    await session.flush()
    return monitor


_ids = itertools.count(500)


async def _message(session, person, text, *, out=False, tg_chat_id=None, at=None):
    row = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.out if out else Direction.in_,
        person_id=person.id if person else None,
        tg_chat_id=tg_chat_id if tg_chat_id is not None else person.telegram_id,
        occurred_at=at or NOW - timedelta(minutes=5),
        raw_text=text,
        meta={"tg_message_id": next(_ids)},
    )
    session.add(row)
    await session.flush()
    return row


async def _rules(session) -> list[m.ReplyRule]:
    return list(
        await session.scalars(
            sa.select(m.ReplyRule)
            .order_by(m.ReplyRule.id)
            .execution_options(populate_existing=True)
        )
    )


def _snap(rule_id, subject, mode, reason, **kw) -> policy.RuleSnapshot:
    return policy.RuleSnapshot(rule_id, subject, mode, reason, **kw)


PRIVATE = policy.ChatFacts(
    chat_key=KEY, channel="telegram", tg_chat_id=DM, chat_type="private"
)
GROUP_FACTS = policy.ChatFacts(
    chat_key=GROUP_KEY, channel="telegram", tg_chat_id=GROUP, chat_type="group"
)
NEVER = _snap(1, "person", "never", "personal", person_id=1)
LOOSE = (
    _snap(2, "chat", "auto", "owner", chat_key=KEY),
    _snap(3, "person", "auto", "owner", person_id=2),
    _snap(4, "chat", "draft", "group", chat_key=GROUP_KEY),
    _snap(5, "address", "auto", "owner", address=f"tg:{DM}"),
)


def _gate_row(**kw) -> gate.Row:
    body = "Assalomu alaykum!"
    sha = gate.body_hash(body)
    values = {
        "id": 1,
        "channel": "telegram",
        "chat_key": KEY,
        "origin": "auto_reply",
        "authored_by": "ai",
        "body": body,
        "body_sha256": sha,
        "guard": {"verdict": "pass", "body_sha256": sha},
        "created_at": NOW - timedelta(minutes=1),
        "intent": "greeting",
        "approved_by": "auto:policy",
        "approved_at": NOW - timedelta(minutes=1),
        "trigger_interaction_id": 9,
    }
    values.update(kw)
    return gate.Row(**values)


def _gate_ctx(**kw) -> gate.Context:
    values = {
        "now": NOW,
        "jarvis_enabled": True,
        "channel_send_enabled": True,
        "env_ceiling": "auto",
        "policies": {policy.GLOBAL: policy.PolicySnapshot(policy.GLOBAL, mode="auto")},
        "chat": PRIVATE,
        "trigger": gate.Trigger(id=9, occurred_at=NOW - timedelta(minutes=2), to_me=True),
        "caps": gate.Caps(auto_cap=20, per_chat_cap=6),
        "guard": PassGuard(),
    }
    values.update(kw)
    return gate.Context(**values)


# --- the precedence (§5.4), pure and exhaustive ---------------------------------------


def test_never_wins_over_everything():
    facts_cases = (
        PRIVATE,
        GROUP_FACTS,
        policy.ChatFacts(chat_key="wa:123@g.us", channel="whatsapp"),
        policy.ChatFacts(chat_key="mail:t1", channel="gmail"),
    )
    for size in range(len(LOOSE) + 1):
        for others in itertools.combinations(LOOSE, size):
            for order in (0, -1):
                rules = (*others, NEVER) if order else (NEVER, *others)
                for facts in facts_cases:
                    for addressed in (True, False, None):
                        assert (
                            policy.ai_refusal(facts, rules, addressed=addressed)
                            == policy.PERSONAL_CHAT
                        ), (others, facts.chat_key, addressed)
    # Without it the loosest rules and modes would send: the never rule alone
    # stops an auto row in an enabled group, addressed to the owner.
    ctx = _gate_ctx(chat=GROUP_FACTS, rules=LOOSE)
    assert gate.evaluate(_gate_row(chat_key=GROUP_KEY), ctx).sends
    blocked = gate.evaluate(
        _gate_row(chat_key=GROUP_KEY), _gate_ctx(chat=GROUP_FACTS, rules=(*LOOSE, NEVER))
    )
    assert (blocked.action, blocked.line, blocked.code) == (
        "block",
        "rules",
        "personal_chat",
    )
    # The owner's own words still need the card's personal warning.
    owner = _gate_row(authored_by="owner", origin="owner_command", approved_by="button")
    back = gate.evaluate(owner, _gate_ctx(rules=(NEVER,)))
    assert (back.action, back.code) == ("redraft", "personal_warning")


def test_precedence_order_below_never():
    # hard exclusions, then groups off, then not addressed; a rule that
    # loosens can lift none of them.
    bot = policy.ChatFacts(chat_key=KEY, channel="telegram", tg_chat_id=DM, is_bot=True)
    assert policy.ai_refusal(bot, LOOSE, addressed=True) == policy.EXCLUDED_BOT
    assert policy.ai_refusal(GROUP_FACTS, (), addressed=True) == policy.GROUP_OFF
    assert policy.ai_refusal(GROUP_FACTS, LOOSE, addressed=False) == (
        policy.GROUP_NOT_ADDRESSED
    )
    assert policy.ai_refusal(GROUP_FACTS, LOOSE, addressed=None) is None
    assert policy.ai_refusal(GROUP_FACTS, LOOSE, addressed=True) is None
    assert policy.ai_refusal(PRIVATE, (), addressed=None) is None
    # A chat rule outranks the person's.
    rules = (
        _snap(3, "person", "auto", "owner", person_id=2),
        _snap(2, "chat", "draft", "owner", chat_key=KEY),
    )
    assert policy.rule_mode(rules) == "draft"
    assert policy.rule_mode(rules[:1]) == "auto"
    # A draft rule sends an auto row back to the owner's card.
    redraft = gate.evaluate(_gate_row(), _gate_ctx(rules=rules))
    assert (redraft.action, redraft.code) == ("redraft", "rule_draft")


def test_saved_messages_service_bots_never():
    cases = {
        policy.EXCLUDED_SAVED_MESSAGES: policy.ChatFacts(
            chat_key=chatkey.telegram(OWNER_ID),
            channel="telegram",
            tg_chat_id=OWNER_ID,
            chat_type="private",
            is_self=True,
        ),
        policy.EXCLUDED_SERVICE_CHAT: policy.ChatFacts(
            chat_key="tg:777000", channel="telegram", tg_chat_id=777000
        ),
        policy.EXCLUDED_BOT: policy.ChatFacts(
            chat_key=KEY, channel="telegram", tg_chat_id=DM, is_bot=True
        ),
        policy.EXCLUDED_CHANNEL: policy.ChatFacts(
            chat_key="tg:-1001", channel="telegram", tg_chat_id=-1001, chat_type="channel"
        ),
        policy.EXCLUDED_OWNER: policy.ChatFacts(
            chat_key=KEY, channel="telegram", tg_chat_id=DM, is_owner=True
        ),
        policy.EXCLUDED_BROADCAST: policy.ChatFacts(
            chat_key="wa:status@broadcast", channel="whatsapp"
        ),
    }
    newsletter = policy.ChatFacts(chat_key="wa:1203@newsletter", channel="whatsapp")
    assert policy.hard_exclusion(newsletter) == policy.EXCLUDED_BROADCAST
    for code, facts in cases.items():
        assert policy.hard_exclusion(facts) == code
        # No rule loosens a hard exclusion, for MIYA's text or the owner's.
        enabling = (*LOOSE, _snap(8, "chat", "auto", "group", chat_key=facts.chat_key))
        for addressed in (True, None):
            assert policy.ai_refusal(facts, enabling, addressed=addressed) == code
        ctx = _gate_ctx(chat=facts, rules=enabling)
        for row in (
            _gate_row(chat_key=facts.chat_key),
            _gate_row(
                chat_key=facts.chat_key,
                authored_by="owner",
                origin="owner_command",
                approved_by="button",
            ),
        ):
            decision = gate.evaluate(row, ctx)
            assert (decision.action, decision.code) == ("block", code)


async def test_hard_exclusions_are_read_from_the_database(session, jarvis, monkeypatch):
    monkeypatch.setattr(settings, "owner_aliases", "Bekzod, @owner_test123")
    bot = await _person(
        session, "Kargo bot", telegram_id=7301, telegram_username="KargoBot"
    )
    alias = await _person(session, "Bekzod", telegram_id=7302)
    for person in (bot, alias):
        facts = await outbox.chat_facts(
            session,
            chat_key=chatkey.telegram(person.telegram_id),
            channel="telegram",
            tg_chat_id=person.telegram_id,
            person_id=person.id,
        )
        assert policy.hard_exclusion(facts) in (
            policy.EXCLUDED_BOT,
            policy.EXCLUDED_OWNER,
        )
    saved = await outbox.chat_facts(
        session,
        chat_key=chatkey.telegram(OWNER_ID),
        channel="telegram",
        tg_chat_id=OWNER_ID,
        person_id=None,
    )
    assert policy.hard_exclusion(saved) == policy.EXCLUDED_SAVED_MESSAGES


async def test_group_default_never(session):
    assert policy.ai_refusal(GROUP_FACTS, (), addressed=True) == policy.GROUP_OFF
    # A WhatsApp group the adapter did not describe is still a group.
    wa_group = policy.ChatFacts(chat_key="wa:12036@g.us", channel="whatsapp")
    assert policy.chat_kind(wa_group) == "group"
    assert policy.ai_refusal(wa_group, (), addressed=True) == policy.GROUP_OFF
    # A never-seen negative Telegram id (no chat_monitors row) is a group too.
    facts = await outbox.chat_facts(
        session,
        chat_key="tg:-100999",
        channel="telegram",
        tg_chat_id=-100999,
        person_id=None,
    )
    assert facts.chat_type == "group"
    assert policy.ai_refusal(facts, (), addressed=True) == policy.GROUP_OFF
    decision = gate.evaluate(_gate_row(chat_key="tg:-100999"), _gate_ctx(chat=facts))
    assert (decision.action, decision.code) == ("block", "group_off")


async def test_group_reply_command_and_confirm(bound, jarvis):
    session = bound
    monitor = await _chat(session, GROUP, ChatType.group, title="Kargo Toshkent")
    await _chat(session, -100778, ChatType.group, title="Oila")
    facts = await outbox.chat_facts(
        session, chat_key=GROUP_KEY, channel="telegram", tg_chat_id=GROUP, person_id=None
    )
    assert facts.chat_type == "group"

    # Switching on is a loosening: only a question until the owner taps.
    text, markup = await _command(handlers.cmd_group_reply, "guruhai", "kargo toshkent")
    assert text == "👥 «Kargo Toshkent» guruhida MIYA javob yozsinmi? " + (
        "Faqat sizga murojaat qilingan xabarlarga, har biri sizning tasdig'ingiz bilan."
    )
    assert _payloads(markup) == [f"jr:g:{monitor.id}", f"jr:n:{monitor.id}"]
    assert _labels(markup) == ["✅ Ha, yoq", "✖️ Yo'q"]
    assert await _rules(session) == []
    kept = await _tap(f"jr:n:{monitor.id}")
    assert kept.message.edited == [(replies.RULE_KEPT, None)]
    assert await _rules(session) == []

    done = await _tap(f"jr:g:{monitor.id}")
    assert done.message.edited == [
        (
            "👥 «Kargo Toshkent» guruhida MIYA javob yozishi yoqildi (faqat sizga "
            "murojaat qilinganda).",
            None,
        )
    ]
    [rule] = await _rules(session)
    assert (rule.subject, rule.chat_key, rule.mode, rule.reason, rule.state) == (
        "chat",
        GROUP_KEY,
        "draft",
        "group",
        "active",
    )
    loaded = await policy.load_rules(session, chat_key=GROUP_KEY, person_id=None)
    assert policy.ai_refusal(facts, loaded, addressed=True) is None
    assert policy.ai_refusal(facts, loaded, addressed=False) == policy.GROUP_NOT_ADDRESSED
    # Replies there are cards for the owner, never sent on their own.
    assert policy.rule_mode(loaded) == "draft"
    # A second tap changes nothing.
    await _tap(f"jr:g:{monitor.id}")
    assert len(await _rules(session)) == 1

    usage, _ = await _command(handlers.cmd_group_reply, "guruhai", None)
    assert (
        usage
        == f"{replies.GROUP_REPLY_USAGE}\n👥 Javob yoqilgan guruhlar: «Kargo Toshkent»"
    )

    # Switching off is immediate.
    text, markup = await _command(handlers.cmd_group_reply, "guruhai", "Kargo Toshkent")
    assert text == "👥 «Kargo Toshkent» guruhida MIYA javob yozishi o'chirildi."
    assert markup is None
    [rule] = await _rules(session)
    assert rule.state == "revoked"
    loaded = await policy.load_rules(session, chat_key=GROUP_KEY, person_id=None)
    assert policy.ai_refusal(facts, loaded, addressed=True) == policy.GROUP_OFF

    # A personal group stays personal: /guruhai cannot open it.
    await _command(handlers.cmd_personal, "shaxsiy", "Oila")
    text, markup = await _command(handlers.cmd_group_reply, "guruhai", "Oila")
    assert text == "🔒 «Oila» shaxsiy deb belgilangan — MIYA u yerda javob yozmaydi."
    assert markup is None
    assert (await _command(handlers.cmd_group_reply, "guruhai", "Yo'q guruh"))[0] == (
        "❓ «Yo'q guruh» nomli guruh topilmadi."
    )

    # Titles are matched folded: a smart apostrophe, Cyrillic, any case.
    curly = await _chat(session, -100779, ChatType.group, title="Do‘stlar")
    cyrillic = await _chat(session, -100780, ChatType.group, title="Оила Ташкент")
    for typed, monitor in (("do'stlar", curly), ("Oila Tashkent", cyrillic)):
        text, markup = await _command(handlers.cmd_group_reply, "guruhai", typed)
        assert _payloads(markup) == [f"jr:g:{monitor.id}", f"jr:n:{monitor.id}"]
    # Two groups of one title: the ids tell them apart, and one picks a group.
    await _chat(session, -100781, ChatType.group, title="Do‘stlar")
    text, markup = await _command(handlers.cmd_group_reply, "guruhai", "Do'stlar")
    assert text == (
        "Qaysi guruh? «Do‘stlar» (-100779), «Do‘stlar» (-100781) — nomini "
        "to'liqroq yozing yoki raqamini: <code>/guruhai -100779</code>"
    )
    assert markup is None
    _, markup = await _command(handlers.cmd_group_reply, "guruhai", "-100781")
    assert _payloads(markup)[0].startswith("jr:g:")
    text, _ = await _command(handlers.cmd_personal, "shaxsiy", "do'stlar")
    assert text.startswith("Qaysi chat? «Do‘stlar» (-100779)")
    assert text.endswith("<code>/shaxsiy -100779</code>")
    assert (await _command(handlers.cmd_personal, "shaxsiy", "-100781"))[0] == (
        replies.personal_marked("Do‘stlar")
    )


# --- marking, merge, purge ------------------------------------------------------------


async def test_mark_person_snapshots_every_address(session, jarvis):
    person = await _person(session, phone="+998 90 123-45-67")
    assert await people.known_addresses(session, person) == [f"tg:{DM}", f"wa:{JID}"]
    marked = await policy.mark_person(session, person, by="command", now=NOW)
    assert not marked.already
    head, snapshot = await _rules(session)
    assert head.id == marked.rule_id
    assert (head.subject, head.person_id, head.chat_key, head.address) == (
        "person",
        person.id,
        KEY,
        f"tg:{DM}",
    )
    assert (head.mode, head.reason, head.state, head.set_by) == (
        "never",
        "personal",
        "active",
        "command",
    )
    assert (snapshot.subject, snapshot.address, snapshot.person_id) == (
        "address",
        f"wa:{JID}",
        None,
    )
    assert policy.link_of(snapshot.history) == head.id
    # Marking again adds nothing.
    again = await policy.mark_person(session, person, by="button", now=NOW)
    assert again == policy.Marked(rule_id=head.id, already=True)
    assert len(await _rules(session)) == 2

    # A person known only from their messages: their private chats.
    imported = await _person(session, "Bobur", telegram_id=None, phone="901234567")
    await _message(session, imported, "Salom", tg_chat_id=7202)
    await _message(session, imported, "Guruhda", tg_chat_id=-100555)
    # A local number has no country code: no WhatsApp jid is guessed.
    assert await people.known_addresses(session, imported) == ["tg:7202"]

    # An owner rule already on the chat is tightened, never loosened.
    other = await _person(session, "Dilshod", telegram_id=7203)
    session.add(
        m.ReplyRule(subject="chat", chat_key="tg:7203", mode="draft", reason="owner")
    )
    await session.flush()
    dilshod = await policy.mark_person(session, other, by="command", now=NOW)
    chat_rule = next(r for r in await _rules(session) if r.chat_key == "tg:7203")
    assert (chat_rule.mode, chat_rule.reason) == ("never", "personal")
    # It now guards Dilshod's marking too, and is listed through it.
    assert [e["field"] for e in chat_rule.history] == ["mode", "reason", "linked"]
    assert policy.links_of(chat_rule.history) == [dilshod.rule_id]
    listed = [item.rule_id for item in await policy.personal_chats(session)]
    assert dilshod.rule_id in listed and chat_rule.id not in listed


async def test_rule_survives_person_purge_by_address(session, jarvis):
    person = await _person(session, phone="+998901234567")
    await _chat(session)
    await _message(session, person, "Salom")
    await policy.mark_person(session, person, by="command", now=NOW)
    await purge.execute(session, await purge.plan_person(session, person))
    assert await session.get(m.Person, person.id) is None
    rules = await _rules(session)
    assert [(r.chat_key, r.address, r.person_id, r.state, r.mode) for r in rules] == [
        (KEY, f"tg:{DM}", None, "active", "never"),
        (None, f"wa:{JID}", None, "active", "never"),
    ]
    # The person writes again and is created anew: still personal, by the
    # chat key and by the address.
    back = await _person(session, "Akmal")
    loaded = await policy.load_rules(
        session, chat_key=KEY, person_id=back.id, addresses=[f"tg:{DM}"]
    )
    assert policy.never_rule(loaded) is not None
    trigger = await _message(session, back, "Yana salom")
    # A WhatsApp row with no address of its own: its chat key is the address.
    wa_row = await outbox.create(
        session,
        channel="whatsapp",
        chat_key=f"wa:{JID}",
        origin="auto_reply",
        authored_by="ai",
        body="Assalomu alaykum!",
        now=NOW,
        intent="greeting",
        to_address=None,
        trigger_interaction_id=trigger.id,
    )
    assert (wa_row.state, wa_row.last_error) == ("blocked", "personal_chat")
    row = await outbox.create(
        session,
        channel="telegram",
        chat_key=KEY,
        origin="auto_reply",
        authored_by="ai",
        body="Assalomu alaykum!",
        now=NOW,
        intent="greeting",
        person_id=back.id,
        trigger_interaction_id=trigger.id,
    )
    assert (row.state, row.last_error) == ("blocked", "personal_chat")
    # The list still names the chat, by its title.
    [listed] = await policy.personal_chats(session)
    assert (listed.name, listed.person_id) == ("Akmal", None)


async def test_merge_moves_rules(session, jarvis):
    source = await _person(session, "Akmal WA", telegram_id=None, phone="+998901234567")
    target = await _person(session, "Akmal")
    marked = await policy.mark_person(session, source, by="command", now=NOW)
    plan = await people.merge_plan(session, source, target)
    assert plan["reply_rules"] == 1
    merged = await people.merge_into(session, source, target, by="command")
    head = await session.get(m.ReplyRule, marked.rule_id, populate_existing=True)
    assert (head.person_id, head.state, head.mode) == (merged.id, "active", "never")
    found = await policy.personal_rule(session, person_id=merged.id)
    assert found is not None and found.id == marked.rule_id
    loaded = await policy.load_rules(session, chat_key=KEY, person_id=merged.id)
    assert policy.never_rule(loaded) is not None

    # Both sides marked: one rule stays the person's, the other keeps its
    # own address; nothing is reopened.
    twin = await _person(session, "Akmal 2", telegram_id=None)
    await _message(session, twin, "Salom", tg_chat_id=7204)
    await policy.mark_person(session, twin, by="command", now=NOW)
    await policy.mark_person(session, merged, by="command", now=NOW)
    await people.merge_into(session, twin, merged, by="command")
    active = [r for r in await _rules(session) if r.state == "active"]
    assert all(r.mode == "never" for r in active)
    assert {r.address for r in active} >= {f"wa:{JID}", "tg:7204", f"tg:{DM}"}
    assert sum(1 for r in active if r.person_id == merged.id) == 1


async def test_unmark_keeps_what_another_marking_needs(session, jarvis):
    # Duplicates sharing one number: Akmal WA's rule holds the WhatsApp jid
    # that Akmal's marking relies on too.
    wa_twin = await _person(session, "Akmal WA", telegram_id=None, phone="+998901234567")
    akmal = await _person(session, "Akmal", phone="+998901234567")
    first = await policy.mark_person(session, wa_twin, by="command", now=NOW)
    second = await policy.mark_person(session, akmal, by="command", now=NOW)
    shared = await session.get(m.ReplyRule, first.rule_id)
    assert shared.address == f"wa:{JID}"
    assert second.rule_id in policy.links_of(shared.history)
    assert policy.link_of(shared.history) is None
    # Each person keeps a line of their own.
    listed = await policy.personal_chats(session)
    assert [item.rule_id for item in listed] == [first.rule_id, second.rule_id]
    # Unmarking Akmal WA leaves Akmal's WhatsApp chat closed.
    await policy.unmark_personal(session, first.rule_id, by="button", now=NOW)
    wa = await policy.load_rules(session, chat_key=f"wa:{JID}", person_id=None)
    assert policy.never_rule(wa) is not None
    [carried] = [
        r
        for r in await _rules(session)
        if r.state == "active" and r.address == f"wa:{JID}"
    ]
    assert (carried.subject, carried.person_id) == ("address", None)
    assert policy.link_of(carried.history) == second.rule_id
    listed = await policy.personal_chats(session)
    assert [item.rule_id for item in listed] == [second.rule_id]
    # Unmarking Akmal as well opens everything.
    await policy.unmark_personal(session, second.rule_id, by="button", now=NOW)
    assert [r.id for r in await _rules(session) if r.state == "active"] == []

    # The other way round: a snapshot of one marking that a second one relies
    # on outlives the first.
    jid = "998901112233@s.whatsapp.net"
    sardor = await _person(session, "Sardor", telegram_id=7230, phone="+998901112233")
    twin = await _person(session, "Sardor WA", telegram_id=None, phone="+998901112233")
    head = await policy.mark_person(session, sardor, by="command", now=NOW)
    other = await policy.mark_person(session, twin, by="command", now=NOW)
    [snapshot] = [r for r in await _rules(session) if r.address == f"wa:{jid}"]
    assert policy.link_of(snapshot.history) == head.rule_id
    assert other.rule_id in policy.links_of(snapshot.history)
    await policy.unmark_personal(session, head.rule_id, by="button", now=NOW)
    snapshot = await session.get(m.ReplyRule, snapshot.id, populate_existing=True)
    assert snapshot.state == "active"
    tg = await policy.load_rules(session, chat_key="tg:7230", person_id=None)
    assert policy.never_rule(tg) is None
    listed = await policy.personal_chats(session)
    assert [item.rule_id for item in listed] == [other.rule_id]


# --- the commands and the buttons -----------------------------------------------------


async def test_unmark_needs_confirm(bound, jarvis):
    session = bound
    person = await _person(session, phone="+998901234567")
    await _chat(session)
    text, markup = await _command(handlers.cmd_personal, "shaxsiy", "Akmal")
    assert text == (
        "🔒 Akmal shaxsiy deb belgilandi. MIYA bu chatga hech qachon o'zi yozmaydi "
        "(o'qish davom etadi)."
    )
    assert markup is None
    head, snapshot = await _rules(session)
    assert head.person_id == person.id and snapshot.address == f"wa:{JID}"

    # The same command on a personal chat only asks.
    text, markup = await _command(handlers.cmd_personal, "shaxsiy", "Akmal")
    assert text == (
        "🔓 Akmalni shaxsiydan chiqaraymi? Shunda MIYA unga qoralama tayyorlay oladi."
    )
    assert _payloads(markup) == [f"jr:y:{head.id}", f"jr:n:{head.id}"]
    assert _labels(markup) == ["✅ Ha, chiqar", "✖️ Yo'q"]
    assert all(r.state == "active" for r in await _rules(session))

    # A typed batch answer can never reach these buttons.
    assert keyboards.RULE_PREFIX == "jr"
    assert "jr" not in handlers._button_handlers()

    kept = await _tap(f"jr:n:{head.id}")
    assert kept.message.edited == [(replies.RULE_KEPT, None)]
    assert all(r.state == "active" for r in await _rules(session))

    done = await _tap(f"jr:y:{head.id}")
    assert done.message.edited == [("🔓 Akmal shaxsiydan chiqarildi.", None)]
    assert [r.state for r in await _rules(session)] == ["revoked", "revoked"]
    assert head.history[-1]["field"] == "state"
    assert head.history[-1]["new"] == "revoked" and head.history[-1]["by"] == "button"
    # A second tap changes nothing.
    again = await _tap(f"jr:y:{head.id}")
    assert again.message.edited == [(replies.RULE_STALE, None)]
    loaded = await policy.load_rules(session, chat_key=KEY, person_id=person.id)
    assert policy.never_rule(loaded) is None

    # The 🔓 button in the list asks the same question first.
    await _command(handlers.cmd_personal, "shaxsiy", "Akmal")
    new_head = next(r for r in await _rules(session) if r.state == "active")
    ask = await _tap(f"jr:u:{new_head.id}")
    [(question, confirm)] = ask.message.sent
    assert question.startswith("🔓 Akmalni shaxsiydan chiqaraymi?")
    assert _payloads(confirm) == [f"jr:y:{new_head.id}", f"jr:n:{new_head.id}"]
    assert new_head.state == "active"
    # Malformed payloads do nothing.
    for data in ("jr", "jr:y", "jr:y:abc", "jr:z:1", "jr:y:1:2"):
        await _tap(data)
    assert sum(1 for r in await _rules(session) if r.state == "active") == 2


async def test_shaxsiy_by_name_by_code_and_by_chat(bound, jarvis):
    session = bound
    akmal = await _person(session)
    sardor = await _person(session, "Sardor", telegram_id=7205)
    await client_codes.attach(session, sardor, "GS367", source="command", by="command")
    await _chat(session, -100778, ChatType.group, title="Oila")

    assert (await _command(handlers.cmd_personal, "shaxsiy", "GS367"))[0] == (
        replies.personal_marked("Sardor")
    )
    assert (await _command(handlers.cmd_personal, "shaxsiy", "akmal"))[0] == (
        replies.personal_marked("Akmal")
    )
    assert (await _command(handlers.cmd_personal, "shaxsiy", "Oila"))[0] == (
        replies.personal_marked("Oila")
    )
    rules = {(r.subject, r.person_id, r.chat_key) for r in await _rules(session)}
    assert ("person", sardor.id, "tg:7205") in rules
    assert ("person", akmal.id, KEY) in rules
    assert ("chat", None, "tg:-100778") in rules

    assert (await _command(handlers.cmd_personal, "shaxsiy", "GS999"))[0] == (
        replies.code_unknown("GS999")
    )
    assert (await _command(handlers.cmd_personal, "shaxsiy", "Zafar"))[0] == (
        replies.person_not_found("Zafar")
    )
    assert (await _command(handlers.cmd_personal, "shaxsiy", None))[0] == (
        replies.PERSONAL_USAGE
    )
    # A name a counterparty chose is shown without its bidi tricks.
    trick = await _person(session, "Ali\u202e<b>", telegram_id=7206)
    await client_codes.attach(session, trick, "GS370", source="command", by="command")
    text, _ = await _command(handlers.cmd_personal, "shaxsiy", "GS370")
    assert "\u202e" not in text and "&lt;b&gt;" in text
    assert any(r.person_id == trick.id for r in await _rules(session))


async def test_suggestions_from_sql_signals(bound, jarvis, monkeypatch):
    session = bound
    monkeypatch.setattr(settings, "owner_aliases", "Bekzod")
    mother = await _person(session, "Onajon", telegram_id=7210)
    await _message(session, mother, "Bolam, keldingmi?")
    friend = await _person(session, "Sardor", telegram_id=7211)
    for text in ("qalaysan, jigar?", "Senga ertaga qo'ng'iroq qilaman", "ok"):
        await _message(session, friend, text, out=True)
    await _message(session, friend, "Zo'r")
    cousin = await _person(
        session, "Jasur", telegram_id=7212, relationship_="amaki o'g'li"
    )
    await _message(session, cousin, "Assalomu alaykum")
    # A warm client is still a client.
    client = await _person(session, "Akmal aka", telegram_id=7213)
    for text in ("qalaysan", "senga yuk keldi", "narxi qancha bo'ldi"):
        await _message(session, client, text, out=True)
    coded = await _person(session, "Akam", telegram_id=7214)
    await client_codes.attach(session, coded, "GS368", source="command", by="command")
    await _message(session, coded, "Salom")
    unmarked = await _person(session, "Opajon", telegram_id=7215)
    await _message(session, unmarked, "Salom")
    marked = await policy.mark_person(session, unmarked, by="command", now=NOW)
    await policy.unmark_personal(session, marked.rule_id, by="button", now=NOW)
    already = await _person(session, "Dadajon", telegram_id=7216)
    await _message(session, already, "Salom")
    await policy.mark_person(session, already, by="command", now=NOW)
    bot = await _person(
        session, "Onajon bot", telegram_id=7217, telegram_username="ona_bot"
    )
    await _message(session, bot, "Salom")
    owner = await _person(session, "Bekzod", telegram_id=7218, relationship_="aka")
    await _message(session, owner, "Salom")

    found = await policy.suggest_personal(session, now=NOW)
    assert [s.person_id for s in found] == [mother.id, cousin.id, friend.id]
    assert found[0].signals.kin and found[2].signals.informal == 2
    # Nothing is marked by a suggestion.
    assert {r.person_id for r in await _rules(session) if r.state == "active"} == {
        already.id
    }

    text, markup = await _command(handlers.cmd_personal_list, "shaxsiylar", None)
    assert text.startswith("🔒 Shaxsiy chatlar (1):\n• Dadajon\n\n💡 Oila yoki do'st")
    assert "💡 Oila yoki do'st bo'lishi mumkin:\n• Onajon\n• Jasur\n• Sardor" in text
    payloads = _payloads(markup)
    assert payloads[0].startswith("jr:u:")
    assert payloads[1:] == [f"jr:p:{mother.id}", f"jr:p:{cousin.id}", f"jr:p:{friend.id}"]
    assert _labels(markup)[1:] == ["🔒 Onajon", "🔒 Jasur", "🔒 Sardor"]
    assert all(len(p.encode()) <= 64 for p in payloads)

    # One tap marks; the list moves the person up.
    page = _Message()
    tapped = await _tap(f"jr:p:{mother.id}", page)
    assert tapped.message.sent == [(replies.personal_marked("Onajon"), None)]
    [(refreshed, refreshed_markup)] = page.edited
    assert refreshed.startswith("🔒 Shaxsiy chatlar (2):\n• Dadajon\n• Onajon")
    assert f"jr:p:{mother.id}" not in _payloads(refreshed_markup)
    assert await policy.personal_rule(session, person_id=mother.id) is not None
    gone = await _tap("jr:p:999999")
    assert gone.message.sent == [(replies.RULE_STALE, None)]


async def test_suggestions_skip_chats_already_personal(bound, jarvis):
    session = bound
    # Forgotten with /unut after /shaxsiy, then back with the same account.
    forgotten = await _person(session, "Akajon", telegram_id=7219)
    await _message(session, forgotten, "Salom")
    await policy.mark_person(session, forgotten, by="command", now=NOW)
    await purge.execute(session, await purge.plan_person(session, forgotten))
    returned = await _person(session, "Akajon", telegram_id=7219)
    await _message(session, returned, "Yana salom")
    # A private chat marked by its title.
    brother = await _person(session, "Ukajon", telegram_id=7222)
    await _message(session, brother, "Salom")
    monitor = await _chat(session, 7222, title="Ukajon")
    chat_rule = await policy.mark_chat(session, monitor, by="command", now=NOW)
    fresh = await _person(session, "Opajon", telegram_id=7223)
    await _message(session, fresh, "Salom")

    found = await policy.suggest_personal(session, now=NOW)
    assert [s.person_id for s in found] == [fresh.id]
    [held] = [r for r in await _rules(session) if r.chat_key == "tg:7219"]
    assert len(await policy.personal_chats(session)) == 2

    # /shaxsiy finds the marking that already holds the chat and only asks.
    for name, rule_id in (("Akajon", held.id), ("Ukajon", chat_rule.rule_id)):
        text, markup = await _command(handlers.cmd_personal, "shaxsiy", name)
        assert text == replies.personal_unmark_question(name)
        assert _payloads(markup) == [f"jr:y:{rule_id}", f"jr:n:{rule_id}"]
    assert len(await policy.personal_chats(session)) == 2

    # A 🔒 tap from an older list adds the person without a second line, and
    # unmarking them opens the chat they were shown with.
    await _tap(f"jr:p:{returned.id}")
    listed = await policy.personal_chats(session)
    assert len(listed) == 2
    mark = next(item for item in listed if item.person_id == returned.id)
    await _tap(f"jr:y:{mark.rule_id}")
    loaded = await policy.load_rules(session, chat_key="tg:7219", person_id=returned.id)
    assert policy.never_rule(loaded) is None
    assert [item.rule_id for item in await policy.personal_chats(session)] == [
        chat_rule.rule_id
    ]


def test_personal_signals_are_specific():
    signals = policy.personal_signals
    assert signals(names=["Akam"], relationship=None, texts=[]).suggested
    assert signals(names=["Мама"], relationship=None, texts=[]).suggested
    # Bare honorifics are what clients get too.
    assert not signals(names=["Akmal aka", "Rustam opa"], relationship=None, texts=[]).kin
    assert signals(names=["X"], relationship="do'stim emas, do'st", texts=[]).kin
    assert not signals(names=["X"], relationship="mijoz", texts=[]).kin
    warm = [(True, "qalaysan"), (True, "senga aytdim"), (False, "ok")]
    assert signals(names=["X"], relationship=None, texts=warm).suggested
    assert signals(names=["X"], relationship=None, texts=warm[:1]).informal == 1
    assert not signals(names=["X"], relationship=None, texts=warm[:1]).suggested
    # The client's own «sen» does not count; work talk disqualifies.
    theirs = [(False, "qalaysan"), (False, "senga")]
    assert not signals(names=["X"], relationship=None, texts=theirs).suggested
    work = [*warm, (False, "GS367 yuki qachon keladi?")]
    assert not signals(names=["Akam"], relationship=None, texts=work).suggested
    # One work word in more than ten messages is tolerated.
    many = [*warm, *([(False, "rahmat")] * 8), (False, "kurs qancha")]
    assert signals(names=["X"], relationship=None, texts=many).suggested
    assert signals(names=["X"], relationship=None, texts=[(True, "❤️")]).informal == 1


# --- failures are contained (rule 25) -------------------------------------------------


async def test_rule_failures_are_contained(bound, jarvis, monkeypatch, caplog):
    session = bound
    person = await _person(session)
    real_mark, real_suggest = policy.mark_person, policy.suggest_personal

    async def _boom(*args, **kwargs):
        raise RuntimeError("db went away")

    caplog.set_level(logging.INFO)
    monkeypatch.setattr(policy, "mark_person", _boom)
    tapped = await _tap(f"jr:p:{person.id}")
    assert tapped.message.sent == [(replies.JARVIS_FAILED, None)]
    assert tapped.answered == 1
    record = next(r for r in caplog.records if r.getMessage() == "reply rule tap failed")
    assert record.error == "RuntimeError" and record.ref_id == person.id
    assert record.rule_action == "p"
    assert "db went away" not in caplog.text

    text, _ = await _command(handlers.cmd_personal, "shaxsiy", "Akmal")
    assert text == replies.JARVIS_FAILED
    assert next(r for r in caplog.records if r.getMessage() == "/shaxsiy failed")

    # The suggestions failing never hides the list.
    monkeypatch.setattr(policy, "suggest_personal", _boom)
    text, _ = await _command(handlers.cmd_personal_list, "shaxsiylar", None)
    assert text.startswith("🔒 Shaxsiy chatlar (0):")
    record = next(
        r for r in caplog.records if r.getMessage() == "personal suggestions failed"
    )
    assert record.error == "RuntimeError"

    # One unreadable person is skipped, the rest are still suggested.
    monkeypatch.setattr(policy, "mark_person", real_mark)
    monkeypatch.setattr(policy, "suggest_personal", real_suggest)
    friend = await _person(session, "Onajon", telegram_id=7220)
    await _message(session, friend, "Salom")
    real = policy.personal_signals

    def _flaky(*, names, relationship, texts):
        if names[0] == "Akmal":
            raise ValueError("bad row")
        return real(names=names, relationship=relationship, texts=texts)

    await _message(session, person, "Salom")
    monkeypatch.setattr(policy, "personal_signals", _flaky)
    found = await policy.suggest_personal(session, now=NOW)
    assert [s.person_id for s in found] == [friend.id]
    record = next(
        r for r in caplog.records if r.getMessage() == "personal suggestion skipped"
    )
    assert record.person_id == person.id and record.error == "ValueError"


# --- strings and the audit ------------------------------------------------------------


def test_owner_strings_are_the_plans():
    assert replies.personal_marked("Ona") == (
        "🔒 Ona shaxsiy deb belgilandi. MIYA bu chatga hech qachon o'zi yozmaydi "
        "(o'qish davom etadi)."
    )
    assert replies.personal_unmark_question("Ona") == (
        "🔓 Onani shaxsiydan chiqaraymi? Shunda MIYA unga qoralama tayyorlay oladi."
    )
    assert (replies.PERSONAL_UNMARK_YES, replies.PERSONAL_UNMARK_NO) == (
        "✅ Ha, chiqar",
        "✖️ Yo'q",
    )
    assert replies.PERSONAL_LIST_HEADER.format(n=3) == "🔒 Shaxsiy chatlar (3):"
    assert replies.PERSONAL_SUGGESTIONS_HEADER == "💡 Oila yoki do'st bo'lishi mumkin:"
    assert replies.group_reply_on("Kargo") == (
        "👥 «Kargo» guruhida MIYA javob yozishi yoqildi (faqat sizga murojaat "
        "qilinganda)."
    )
    assert replies.group_reply_off("Kargo") == (
        "👥 «Kargo» guruhida MIYA javob yozishi o'chirildi."
    )
    assert replies.group_reply_on("<i>") == (
        "👥 «&lt;i&gt;» guruhida MIYA javob yozishi yoqildi (faqat sizga murojaat "
        "qilinganda)."
    )
    markup = keyboards.personal_list([(1, None)], [(2, "Ali\u202egpj.exe")])
    assert _labels(markup) == ["🔓 Nomaʼlum chat", "🔒 Aligpj.exe"]


async def test_rule_actions_read_in_jarvis_tarix(session, jarvis):
    person = await _person(session)
    session.add(m.ReplyRule(subject="chat", chat_key=KEY, mode="draft", reason="owner"))
    await session.flush()
    marked = await policy.mark_person(session, person, by="command", now=NOW)
    await policy.unmark_personal(session, marked.rule_id, by="button", now=NOW)
    history = replies.jarvis_history(await status.recent_actions(session, limit=20))
    assert "rejim: qoralama → hech qachon (buyruq)" in history
    assert "sabab: siz → shaxsiy (buyruq)" in history
    assert "holat: faol → bekor qilindi (siz)" in history
    assert f"bog'langan belgi: — → qoida {marked.rule_id} (buyruq)" in history
    for internal in ("never", "personal", "(button)", "(command)"):
        assert internal not in history, internal

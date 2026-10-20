"""JV-09: the spam and scam message shield, with owner warnings.

Spam and scam messages are flagged in ``meta.safety``, never answered and
kept out of «javobsiz» lists; the owner is warned about a scam or a possibly
hijacked account once per chat per day. The classifiers are pure and run
over a fixture corpus; the ingest path (userbot), the open-loops engine,
the brief and the warning job run against real PostgreSQL with Telegram
replaced by stand-ins.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from dotenv import dotenv_values
from telethon.tl.types import (
    KeyboardButtonRow,
    KeyboardButtonUrl,
    MessageEntityTextUrl,
    ReplyInlineMarkup,
    User,
)

from miya.bot import replies
from miya.config import Settings, settings
from miya.db import models as m
from miya.db.enums import ChatType, Direction, InteractionSource
from miya.db.session import session_scope
from miya.services import (
    brief,
    loops,
    nudges,
    queries,
    questions,
    recaps,
    safety,
    windows,
)
from miya.tools import doctor
from miya.userbot import main as userbot
from miya.worker import main as worker
from tests.fixtures.scam_corpus import BENIGN, HIJACK, SCAM, SPAM

TZ = settings.tz
ROOT = Path(__file__).resolve().parents[1]
CHAT = 616161
OTHER_CHAT = 626262
DAY = datetime.now(TZ).replace(hour=14, minute=0, second=0, microsecond=0)
_ids = itertools.count(100)

# Corpus thresholds, recorded: every hard hit quarantined with its rule,
# every hijack and spam case caught, and no ordinary business message flagged.
HARD_RECALL_MIN = 1.0
SOFT_RECALL_MIN = 1.0
PRECISION_MIN = 1.0


# --- the pure shield over the corpus --------------------------------------------


def _features(case: dict) -> safety.MessageFeatures:
    extra = dict(case["features"])
    name = extra.pop("attachment", None)
    verdict = safety.classify_attachment(name) if name else None
    urls = safety.extract_urls(
        case["text"],
        text_links=extra.pop("text_links", ()),
        buttons=extra.pop("buttons", ()),
    )
    return safety.MessageFeatures(
        text=case["text"],
        urls=tuple(urls),
        attachment=verdict if verdict is not None and verdict.kind != safety.OK else None,
        has_file=name is not None,
        **extra,
    )


def _verdict(case: dict) -> safety.MessageVerdict:
    return safety.scam_verdict(_features(case))


def _ids_of(cases: list[dict]) -> list[str]:
    return [c["text"][:40] or str(c["features"]) for c in cases]


def test_corpus_hard_hits_quarantined():
    caught = 0
    for case in SCAM:
        verdict = _verdict(case)
        assert verdict.verdict == "scam", case["text"]
        assert verdict.quarantined and verdict.score == safety.HARD_SCORE, case["text"]
        assert case["reason"] in verdict.reasons, (case["text"], verdict.reasons)
        assert verdict.notice == safety.NOTICE_SCAM
        caught += 1
    assert caught / len(SCAM) >= HARD_RECALL_MIN


def test_corpus_spam_and_hijack_caught():
    caught = 0
    for case in SPAM + HIJACK:
        verdict = _verdict(case)
        assert verdict.verdict == case["verdict"], (case["text"], verdict)
        assert not verdict.quarantined, case["text"]
        caught += 1
    assert caught / len(SPAM + HIJACK) >= SOFT_RECALL_MIN
    for case in SPAM:
        assert _verdict(case).score >= safety.SPAM_SCORE
        assert _verdict(case).notice is None  # spam is counted, never a warning
    for case in HIJACK:
        assert _verdict(case).notice == safety.NOTICE_HIJACK


@pytest.mark.parametrize("case", BENIGN, ids=_ids_of(BENIGN))
def test_benign_business_messages_pass(case):
    verdict = _verdict(case)
    assert verdict.verdict == "ok", (case["text"], verdict)
    assert verdict.score < safety.SPAM_SCORE
    assert safety.with_message_verdict({"tg_message_id": 1}, verdict, notify=True) == {
        "tg_message_id": 1
    }


def test_corpus_precision():
    flagged = [c for c in SCAM + SPAM + HIJACK + BENIGN if _verdict(c).flagged]
    bad = [c for c in flagged if c["verdict"] != "ok"]
    assert len(bad) / len(flagged) >= PRECISION_MIN


@pytest.mark.parametrize(
    "url",
    [
        "https://paymee.uz/pay",  # one letter added
        "https://payrne.uz",  # «rn» for «m»
        "https://uzkard.uz",  # «k» for «c»
        "https://kapitalbankk.uz",
        "https://payme-uz.com/login",  # the brand glued to another site
        "https://paymebonus.xyz",
        "http://pаyme.uz",  # a Cyrillic «а»
        "https://рауме.uz",  # every letter a Cyrillic stand-in
        "https://xn--pyme-53d.uz",  # the same, as punycode
        "https://payme.uz.check-card.top/x",  # the real name as a subdomain
    ],
)
def test_lookalike_payme_domain_hard_hit(url):
    [found] = safety.extract_urls(f"To'lovni tasdiqlang: {url}")
    link = safety.link_verdict(found)
    assert set(link.hard) & {
        safety.R_LOOKALIKE_DOMAIN,
        safety.R_BRAND_DOMAIN,
        safety.R_MIXED_SCRIPT_DOMAIN,
    }, (url, link)
    verdict = safety.scam_verdict(
        safety.MessageFeatures(text=url, urls=(found,), saved_contact=True)
    )
    assert verdict.verdict == "scam"


@pytest.mark.parametrize(
    "url",
    [
        "https://payme.uz/checkout/1",
        "https://checkout.paycom.uz/abc",
        "https://my.gov.uz/uz",
        "https://t.me/+AbCd",
        "https://click.uz",
        "https://www.kapitalbank.uz/uz/",
        # Real, unrelated sites a letter or two from a sensitive name.
        "https://payment.uz",
        "https://planet.uz",
        "https://cloud.ru",
        "https://doodle.com",
        "https://telegraf.com",
        "https://apply.uz",
        "https://uzcars.uz",
    ],
)
def test_the_real_sites_pass(url):
    [found] = safety.extract_urls(url)
    assert safety.link_verdict(found) == safety.LinkVerdict()


def test_new_contact_alone_not_blocked():
    """Owner decision 2: a new client writing first is answered."""
    first = safety.MessageFeatures(text="Assalomu alaykum, narx qancha?")
    assert first.first_contact
    verdict = safety.scam_verdict(first)
    assert verdict.verdict == "ok"
    assert verdict.reasons == (safety.R_FIRST_CONTACT,)
    assert verdict.score < safety.SPAM_SCORE
    meta = safety.with_message_verdict({}, verdict, notify=True)
    assert meta == {} and not safety.blocks_reply(meta)
    # No single soft signal blocks, and a first contact plus any one other
    # still does not: only a combination of signals reaches the threshold.
    assert all(weight < safety.SPAM_SCORE for weight in safety.SOFT_WEIGHTS.values())
    others = [w for r, w in safety.SOFT_WEIGHTS.items() if r != safety.R_FIRST_CONTACT]
    assert safety.SOFT_WEIGHTS[safety.R_FIRST_CONTACT] + max(others) < safety.SPAM_SCORE


def test_a_known_contact_is_not_a_safety_signal():
    """Knowing someone lowers nothing: a hard rule from a saved contact with
    history is still a scam."""
    known = safety.MessageFeatures(
        text="Kelgan sms kodni ayting", saved_contact=True, prior_messages=30
    )
    assert safety.scam_verdict(known).verdict == "scam"


def test_a_stranger_with_history_is_never_a_hijack():
    """Only a contact (saved, or someone the owner wrote to) can be hijacked;
    a stranger's earlier messages make their spam no less spam."""
    text = "Mana ko'ring: https://bit.ly/3abcde"
    [found] = safety.extract_urls(text)
    stranger = safety.MessageFeatures(text=text, urls=(found,), prior_messages=10)
    assert not stranger.known and not stranger.first_contact
    assert safety.scam_verdict(stranger).verdict == "ok"
    for contact in ({"saved_contact": True}, {"owner_wrote_before": True}):
        known = safety.MessageFeatures(
            text=text, urls=(found,), prior_messages=10, **contact
        )
        assert safety.scam_verdict(known).verdict == "hijack"


def test_telegram_text_links_and_buttons_are_read():
    found = safety.extract_urls(
        "Batafsil",
        text_links=[("payme.uz", "https://evil.top/x")],
        buttons=["https://bit.ly/abc"],
    )
    assert [(f.source, f.host) for f in found] == [
        ("text_link", "evil.top"),
        ("button", "bit.ly"),
    ]
    assert safety.R_TEXT_LINK_MISMATCH in safety.link_verdict(found[0]).hard
    # A text link whose shown words name no site is an ordinary link.
    [plain] = safety.extract_urls("", text_links=[("shu yerda", "https://payme.uz/c")])
    assert safety.link_verdict(plain) == safety.LinkVerdict()


def test_a_zero_width_space_cannot_hide_a_link():
    [found] = safety.extract_urls("kiring: payme-uz.​xyz/login")
    assert found.host == "payme-uz.xyz"


def test_extra_safe_domains_never_loosen_file_rules():
    [lookalike] = safety.extract_urls("https://paymee.uz/x")
    assert safety.link_verdict(lookalike).hard
    assert (
        safety.link_verdict(lookalike, extra_safe=["paymee.uz"]) == safety.LinkVerdict()
    )
    [installer] = safety.extract_urls("https://paymee.uz/app.apk")
    assert safety.link_verdict(installer, extra_safe=["https://www.Paymee.uz/"]).hard == (
        safety.R_EXEC_URL,
    )


def test_safety_extra_safe_domains_is_documented_everywhere(monkeypatch):
    monkeypatch.delenv("SAFETY_EXTRA_SAFE_DOMAINS", raising=False)
    assert Settings(_env_file=None).safety_extra_safe_domains_list == ()
    fresh = Settings(_env_file=None, safety_extra_safe_domains="a.uz, b.com  c.ru")
    assert fresh.safety_extra_safe_domains_list == ("a.uz", "b.com", "c.ru")
    env = (ROOT / ".env.example").read_text()
    assert "\nSAFETY_EXTRA_SAFE_DOMAINS=\n" in env
    assert dotenv_values(ROOT / ".env.example")["SAFETY_EXTRA_SAFE_DOMAINS"] == ""
    assert "`SAFETY_EXTRA_SAFE_DOMAINS`" in (ROOT / "README.md").read_text()
    assert "SAFETY_EXTRA_SAFE_DOMAINS" in doctor.EXPECTED_ENV_KEYS


def test_the_owner_strings():
    assert replies.scam_warning(
        who="Akmal", reasons=["code_request", "first_contact"]
    ) == (
        "🚫 Akmaldan kelgan xabar firibgarlikka o'xshaydi (SMS kod yoki parol "
        "so'ralyapti). Javob bermang, havolani ochmang."
    )
    assert replies.hijack_warning(who="Akmal") == (
        "⚠️ Akmalning akkaunti buzilgan bo'lishi mumkin: odatda bunday yozmaydi. Unga "
        "qo'ng'iroq qilib tekshiring, pul o'tkazmang."
    )
    assert replies.UNSAFE_IGNORED_LINE.format(n=3) == (
        "🚫 Kecha 3 ta spam/firibgar xabar e'tiborsiz qoldirildi."
    )
    # Every hard rule has a label for {sabab}.
    assert set(replies.SCAM_REASON_LABELS) == safety.HARD_REASONS


def test_a_sender_chosen_name_is_escaped_and_stripped():
    text = replies.scam_warning(who="<b>Ak‮mal</b>", reasons=["exec_url"])
    assert "<b>" not in text and "‮" not in text
    assert text.startswith("🚫 &lt;b&gt;Akmal&lt;/b&gt;dan")


# --- the userbot ------------------------------------------------------------------


def _peer(**flags) -> User:
    return User(id=CHAT, first_name="Akmal", username="akmal_test", **flags)


def _tg_message(
    text: str,
    *,
    mid: int | None = None,
    peer: User | None = None,
    out: bool = False,
    at: datetime | None = None,
    photo: bool = False,
    entities=None,
    reply_markup=None,
    **extra,
):
    peer = peer or _peer()

    async def get_chat():
        return peer

    async def get_sender():
        return peer

    return SimpleNamespace(
        id=mid or next(_ids),
        chat_id=CHAT,
        date=at or datetime.now(TZ) - timedelta(minutes=1),
        message=text,
        out=out,
        mentioned=False,
        file=SimpleNamespace(name=None, size=2048, mime_type="image/jpeg")
        if photo
        else None,
        sticker=None,
        gif=None,
        voice=None,
        audio=None,
        video_note=None,
        photo=object() if photo else None,
        video=None,
        document=None,
        entities=entities,
        reply_markup=reply_markup,
        get_chat=get_chat,
        get_sender=get_sender,
        **extra,
    )


class _Client:
    def __init__(self) -> None:
        self.downloads: list[str] = []

    async def download_media(self, message, file):
        self.downloads.append(file)
        Path(file).write_bytes(b"\xff\xd8\xff" + b"0" * 64)


@pytest.fixture
def media_in_tmp(monkeypatch, tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    monkeypatch.setattr(type(settings), "media_dir", property(lambda self: media))
    return media


async def _private_monitor(chat_id: int = CHAT) -> None:
    async with session_scope() as session:
        session.add(
            m.ChatMonitor(
                tg_chat_id=chat_id,
                chat_type=ChatType.private,
                title="Akmal",
                monitor_enabled=True,
                monitoring_since=datetime.now(TZ) - timedelta(days=30),
            )
        )


async def _row(session, mid: int) -> m.Interaction:
    rows = await session.scalars(
        sa.select(m.Interaction).where(m.Interaction.tg_chat_id == CHAT)
    )
    [row] = [r for r in rows if r.meta["tg_message_id"] == mid]
    await session.refresh(row)
    return row


async def test_runs_before_model(session, media_in_tmp, monkeypatch):
    """A scam photo is judged in the first transaction: nothing is
    downloaded, vision never runs, and the message is never windowed for
    the extractor."""

    async def _no_model(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("a model saw a quarantined message")

    monkeypatch.setattr(userbot, "read_image", _no_model)
    monkeypatch.setattr(windows, "text_for_extraction", _no_model)
    await _private_monitor()
    client = _Client()
    message = _tg_message(
        "Kartangiz bloklandi: https://payme-uz.xyz/login", mid=1, photo=True
    )
    assert await userbot.ingest_message(client, message) is True

    row = await _row(session, 1)
    assert client.downloads == []
    assert row.meta["safety"]["verdict"] == "scam"
    assert safety.R_BRAND_DOMAIN in row.meta["safety"]["reasons"]
    assert row.meta["safety_notice"] == safety.NOTICE_SCAM
    assert row.media["skipped"] == safety.SKIP_QUARANTINED
    assert row.media["processed"] is True and "approval" not in row.media
    assert row.processed is True and row.window_id is None
    assert await windows.flush_ready_windows(session, now=datetime.now(TZ)) == []
    assert safety.blocks_reply(row.meta)


async def test_a_text_link_and_a_scam_flag_come_from_telegram(session, media_in_tmp):
    await _private_monitor()
    text = "To'lov cheki: payme.uz"
    entity = MessageEntityTextUrl(
        offset=len("To'lov cheki: "), length=8, url="http://x.top/c"
    )
    await userbot.ingest_message(_Client(), _tg_message(text, mid=2, entities=[entity]))
    markup = ReplyInlineMarkup(
        rows=[
            KeyboardButtonRow(
                buttons=[KeyboardButtonUrl(text="Olish", url="https://a.b")]
            )
        ]
    )
    await userbot.ingest_message(
        _Client(), _tg_message("Salom", mid=3, peer=_peer(scam=True), reply_markup=markup)
    )

    assert (
        safety.R_TEXT_LINK_MISMATCH in (await _row(session, 2)).meta["safety"]["reasons"]
    )
    flagged = await _row(session, 3)
    assert flagged.meta["safety"]["verdict"] == "scam"
    assert safety.R_TELEGRAM_SCAM in flagged.meta["safety"]["reasons"]


async def test_a_new_contact_is_stored_like_anyone_else(session, media_in_tmp):
    await _private_monitor()
    await userbot.ingest_message(_Client(), _tg_message("Assalomu alaykum, narx?", mid=4))
    row = await _row(session, 4)
    assert "safety" not in row.meta and "safety_notice" not in row.meta
    assert row.processed is False


async def test_forwarded_spam_from_a_bot_is_flagged_not_quarantined(
    session, media_in_tmp
):
    await _private_monitor()
    message = _tg_message(
        "Tekin premium! bit.ly/abc12 Do'stlaringizga ulashing",
        mid=5,
        fwd_from=SimpleNamespace(from_id=None),
        via_bot_id=777,
    )
    await userbot.ingest_message(_Client(), message)
    row = await _row(session, 5)
    assert row.meta["safety"]["verdict"] == "spam"
    for reason in (safety.R_FORWARDED, safety.R_VIA_BOT, safety.R_SHORTENER):
        assert reason in row.meta["safety"]["reasons"]
    assert "safety_notice" not in row.meta  # spam is counted, never a warning
    assert row.processed is False  # still extracted: only a scam is quarantined
    assert safety.blocks_reply(row.meta)


async def test_a_known_contact_suddenly_asking_for_a_loan(session, media_in_tmp):
    await _private_monitor()
    client = _Client()
    contact = _peer(contact=True)
    start = datetime.now(TZ) - timedelta(days=3)
    for i, text in enumerate(["GS367 yuki qachon?", "Rahmat aka", "Hujjatlar tayyor"]):
        await userbot.ingest_message(
            client,
            _tg_message(text, mid=10 + i, peer=contact, at=start + timedelta(hours=i)),
        )
    await userbot.ingest_message(
        client, _tg_message("Aka, pul kerak edi, qarz berib turing", mid=20, peer=contact)
    )
    row = await _row(session, 20)
    assert row.meta["safety"]["verdict"] == "hijack"
    assert row.meta["safety"]["reasons"][0] == safety.R_HIJACK_LOAN
    assert row.meta["safety_notice"] == safety.NOTICE_HIJACK
    assert row.processed is False
    assert not safety.is_hidden(row.meta) and safety.blocks_reply(row.meta)


async def test_the_owners_own_words_and_old_history_are_not_judged(session, media_in_tmp):
    await _private_monitor()
    client = _Client()
    scam = "Sizga kelgan SMS kodni ayting"
    await userbot.ingest_message(client, _tg_message(scam, mid=30, out=True))
    old = datetime.now(TZ) - safety.NOTICE_MAX_AGE - timedelta(hours=1)
    await userbot.ingest_message(client, _tg_message(scam, mid=31, at=old))
    await userbot.ingest_message(client, _tg_message(scam, mid=32), archive=True)

    assert "safety" not in (await _row(session, 30)).meta
    stale = await _row(session, 31)
    assert stale.meta["safety"]["verdict"] == "scam"
    assert "safety_notice" not in stale.meta  # history, not news
    assert "safety" not in (await _row(session, 32)).meta  # an archive import


async def test_a_dangerous_file_keeps_its_one_warning(session, media_in_tmp):
    """JV-01's verdict and warning stay; the shield adds no second one."""
    await _private_monitor()
    message = _tg_message("Bu sizmi rasmda?", mid=40)
    message.document = object()
    message.file = SimpleNamespace(name="Rasmlar.jpg.apk", size=2048, mime_type=None)
    await userbot.ingest_message(_Client(), message)
    row = await _row(session, 40)
    assert row.meta["safety"]["verdict"] == safety.VERDICT_DANGEROUS_FILE
    assert row.meta["safety_notice"] == safety.NOTICE_DANGEROUS_FILE
    assert row.media["skipped"] == "dangerous_file"
    assert row.processed is True  # quarantined: the lure is never extracted


def test_a_dangerous_file_replaces_a_shield_warning():
    """An installer's warning (do not open the file) outranks a scam or a
    hijack warning, even one already sent or folded."""
    exe = safety.Verdict(safety.EXEC, "magic")
    for queued in (safety.NOTICE_HIJACK, safety.NOTICE_SCAM):
        sent = {
            "safety_notice": queued,
            safety.SAFETY_NOTIFIED_KEY: DAY.isoformat(),
            safety.SAFETY_FOLDED_KEY: True,
        }
        out = safety.with_file_verdict(sent, exe, notify=True)
        assert out["safety_notice"] == safety.NOTICE_DANGEROUS_FILE
        assert safety.SAFETY_NOTIFIED_KEY not in out
        assert safety.SAFETY_FOLDED_KEY not in out
        assert out["safety"]["verdict"] == safety.VERDICT_DANGEROUS_FILE
    # A file warning already queued is never replaced, nor is a macro note
    # allowed to displace a shield warning.
    filed = {"safety_notice": safety.NOTICE_DANGEROUS_FILE}
    assert safety.with_file_verdict(filed, exe, notify=True) == {
        **filed,
        "safety": exe.as_safety(),
    }
    macro = safety.Verdict(safety.MACRO, "vba")
    hijack = {"safety_notice": safety.NOTICE_HIJACK}
    assert safety.with_file_verdict(hijack, macro, notify=True) == hijack


class _InstallerClient(_Client):
    async def download_media(self, message, file):
        self.downloads.append(file)
        Path(file).write_bytes(b"MZ" + b"\x00" * 128)


async def test_a_hijacked_contact_sending_an_installer(session, media_in_tmp):
    """A known contact's loan request with an installer posing as a PDF: the
    owner gets the file warning, not only the hijack one."""
    await _private_monitor()
    client = _InstallerClient()
    contact = _peer(contact=True)
    start = datetime.now(TZ) - timedelta(days=3)
    for i, text in enumerate(["GS367 yuki qachon?", "Rahmat aka", "Hujjatlar tayyor"]):
        await userbot.ingest_message(
            client,
            _tg_message(text, mid=45 + i, peer=contact, at=start + timedelta(hours=i)),
        )
    message = _tg_message("Aka, pul kerak edi, qarz berib turing", mid=48, peer=contact)
    message.document = object()
    message.file = SimpleNamespace(
        name="hisob.pdf", size=2048, mime_type="application/pdf"
    )
    await userbot.ingest_message(client, message)

    row = await _row(session, 48)
    assert client.downloads  # a hijack is not quarantined; the bytes were sniffed
    assert row.meta["safety"]["verdict"] == safety.VERDICT_DANGEROUS_FILE
    assert row.meta["safety_notice"] == safety.NOTICE_DANGEROUS_FILE
    bot = _Bot()
    await worker.safety_notice_job(bot)
    [(text, _)] = bot.sent
    assert text == await worker.safety_notice_text(session, row)
    assert "«hisob.pdf»" in text and text != replies.hijack_warning(who="Akmal")


async def test_a_failing_shield_never_costs_the_message(
    session, media_in_tmp, monkeypatch
):
    def _broken(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(safety, "scam_verdict", _broken)
    await _private_monitor()
    await userbot.ingest_message(
        _Client(), _tg_message("Kelgan SMS kodni ayting", mid=50)
    )
    row = await _row(session, 50)
    assert "safety" not in row.meta and row.processed is False


# --- «javobsiz» lists ----------------------------------------------------------------


async def test_spam_not_in_unanswered(session, media_in_tmp):
    await _private_monitor()
    await _private_monitor(OTHER_CHAT)
    client = _Client()
    asked = datetime.now(TZ) - timedelta(hours=8)
    # Spam asking a question, and a scam asking one, in one chat ...
    await userbot.ingest_message(
        client,
        _tg_message("Tekin premium olasizmi? bit.ly/abc12 ulashing", mid=60, at=asked),
    )
    await userbot.ingest_message(
        client, _tg_message("Kelgan SMS kodni aytasizmi?", mid=61, at=asked)
    )
    # ... and a real client's question in another.
    real = _tg_message("Konteyner qachon keladi?", mid=62, at=asked)
    real.chat_id = OTHER_CHAT
    await userbot.ingest_message(client, real)

    [loop] = await loops.unanswered_questions(session)
    assert loop.tg_chat_id == OTHER_CHAT
    assert [q.tg_chat_id for q in await nudges.collect(session)] == [OTHER_CHAT]
    pending = await questions.collect(session, for_push=False)
    assert [p.ref for p in pending if p.kind == questions.KIND_NUDGE] == [
        f"q{loop.interaction_id}"
    ]


async def test_a_big_file_from_a_spammer_is_never_a_tap(session):
    """Spam files are filtered before the oldest-first window, so a spammer
    sending more big files than it holds never crowds out a real one."""
    session.add(m.ChatMonitor(tg_chat_id=CHAT, chat_type=ChatType.private, title="X"))
    start = datetime.now(TZ) - timedelta(hours=5)
    flood = [{"safety": {"verdict": "spam", "score": 60}} for _ in range(25)]
    flood.append({"safety": {"verdict": "scam", "score": 100}})
    rows = []
    for i, meta in enumerate([*flood, {}]):
        row = m.Interaction(
            source=InteractionSource.telegram_userbot,
            direction=Direction.in_,
            tg_chat_id=CHAT,
            occurred_at=start + timedelta(minutes=i),
            media={
                "type": "video",
                "processed": False,
                "approval": {"state": "pending", "reason": "video", "pushable": True},
            },
            meta={"tg_message_id": 700 + i, **meta},
        )
        session.add(row)
        rows.append(row)
    await session.commit()
    pending = await questions.collect(session, for_push=False)
    assert [p.ref for p in pending if p.kind == questions.KIND_MEDIA] == [
        f"i{rows[-1].id}"
    ]


async def test_a_spammers_big_file_opens_no_question(session, media_in_tmp):
    await _private_monitor()
    message = _tg_message(
        "Tekin premium! bit.ly/abc12 Do'stlaringizga ulashing",
        mid=72,
        fwd_from=SimpleNamespace(from_id=None),
    )
    message.video = object()
    message.file = SimpleNamespace(
        name="promo.mp4", size=5_000_000, mime_type="video/mp4"
    )
    client = _Client()
    await userbot.ingest_message(client, message)
    row = await _row(session, 72)
    assert row.meta["safety"]["verdict"] == "spam"
    assert "approval" not in row.media
    assert row.media["skipped"] == safety.SKIP_SPAM
    assert client.downloads == []


async def test_spam_and_scam_stay_out_of_the_recap_and_who_wrote(session, media_in_tmp):
    """The recap's per-person «javobsiz» questions and «📨 Kecha sizga
    yozganlar» list only people the owner owes an answer."""
    await _private_monitor()
    await _private_monitor(OTHER_CHAT)
    client = _Client()
    asked = datetime.now(TZ) - timedelta(hours=2)
    await userbot.ingest_message(
        client,
        _tg_message("Tekin premium olasizmi? bit.ly/abc12 ulashing", mid=90, at=asked),
    )
    await userbot.ingest_message(
        client, _tg_message("Kelgan SMS kodni aytasizmi?", mid=91, at=asked)
    )
    client_peer = User(id=OTHER_CHAT, first_name="Vali", username="vali_test")
    real = _tg_message("Konteyner qachon keladi?", mid=92, at=asked, peer=client_peer)
    real.chat_id = OTHER_CHAT
    await userbot.ingest_message(client, real)

    start, end = asked - timedelta(hours=1), datetime.now(TZ)
    day = await recaps.gather_activity(session, start, end, now=end)
    asked_texts = [q.text for person in day.people for q in person.questions]
    assert asked_texts == ["Konteyner qachon keladi?"]
    wrote, total = await queries.who_wrote(session, (start, end))
    assert total == 1
    assert [w.person.display_name for w in wrote] == ["Vali"]


async def test_an_unnamed_group_sender_has_no_history(session):
    """An anonymous admin or a channel posting into a group is nobody MIYA
    knows: the group's history is other people's."""
    group = -808080
    session.add(m.ChatMonitor(tg_chat_id=group, chat_type=ChatType.group, title="G"))
    for i in range(5):
        session.add(
            m.Interaction(
                source=InteractionSource.telegram_userbot,
                direction=Direction.in_,
                tg_chat_id=group,
                occurred_at=datetime.now(TZ) - timedelta(hours=i + 1),
                raw_text="GS367 yuki qachon?",
                meta={"tg_message_id": 800 + i},
            )
        )
    await session.commit()
    history = await userbot._sender_history(
        session, person_id=None, chat_id=group, private=False
    )
    assert history.prior_messages == 0 and not history.owner_wrote_before
    private = await userbot._sender_history(
        session, person_id=None, chat_id=group, private=True
    )
    assert private.prior_messages == 5


async def test_only_the_owners_own_words_make_a_contact(session):
    """MIYA's automatic replies in a chat are not the owner writing (JV-10)."""
    session.add(m.ChatMonitor(tg_chat_id=CHAT, chat_type=ChatType.private, title="A"))
    session.add(
        m.Interaction(
            source=InteractionSource.telegram_userbot,
            direction=Direction.out,
            tg_chat_id=CHAT,
            occurred_at=datetime.now(TZ) - timedelta(hours=1),
            raw_text="Assalomu alaykum",
            meta={"tg_message_id": 850, "sent_by": "jarvis", "authored_by": "ai"},
        )
    )
    await session.commit()
    history = await userbot._sender_history(
        session, person_id=None, chat_id=CHAT, private=True
    )
    assert history.owner_wrote_before is False


# --- the warning job -----------------------------------------------------------------


class _Bot:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((text, kwargs))


async def _queued(session, *, notice=safety.NOTICE_SCAM, chat=CHAT, at=DAY, reasons=None):
    person = await session.scalar(
        sa.select(m.Person).where(m.Person.display_name == "Akmal")
    )
    if person is None:
        person = m.Person(display_name="Akmal", aliases=[])
        session.add(person)
        await session.flush()
    row = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.in_,
        person_id=person.id,
        tg_chat_id=chat,
        occurred_at=at,
        raw_text="x",
        meta={
            "tg_message_id": next(_ids),
            "safety": {
                "verdict": "scam" if notice == safety.NOTICE_SCAM else "hijack",
                "score": 100,
                "reasons": reasons or ["code_request"],
            },
            "safety_notice": notice,
        },
    )
    session.add(row)
    await session.commit()
    return row


async def test_warning_once_per_day(session):
    first = await _queued(session)
    second = await _queued(session, at=DAY + timedelta(minutes=5))
    hijack = await _queued(
        session, notice=safety.NOTICE_HIJACK, at=DAY + timedelta(minutes=6)
    )
    elsewhere = await _queued(session, chat=OTHER_CHAT, at=DAY + timedelta(minutes=7))
    bot = _Bot()
    await worker.safety_notice_job(bot, now=DAY + timedelta(minutes=10))
    await worker.safety_notice_job(bot, now=DAY + timedelta(minutes=11))

    assert [text for text, _ in bot.sent] == [
        replies.scam_warning(who="Akmal", reasons=["code_request"]),
        replies.scam_warning(who="Akmal", reasons=["code_request"]),
    ]
    for row in (first, second, hijack, elsewhere):
        await session.refresh(row)
        assert safety.SAFETY_NOTIFIED_KEY in row.meta
    assert safety.SAFETY_FOLDED_KEY not in first.meta
    assert second.meta[safety.SAFETY_FOLDED_KEY] is True
    assert hijack.meta[safety.SAFETY_FOLDED_KEY] is True
    assert safety.SAFETY_FOLDED_KEY not in elsewhere.meta
    # A warning is not a tap-request: the day's question budget is untouched.
    assert await session.scalar(sa.select(sa.func.count(m.QuestionLog.id))) == 0

    # The next day the same chat is warned again.
    tomorrow = DAY + timedelta(days=1)
    await _queued(session, notice=safety.NOTICE_HIJACK, at=tomorrow)
    await worker.safety_notice_job(bot, now=tomorrow + timedelta(minutes=1))
    assert bot.sent[-1][0] == replies.hijack_warning(who="Akmal")
    assert len(bot.sent) == 3


async def test_a_scam_warning_is_silent_at_night(session):
    night = DAY.replace(hour=23, minute=45)
    await _queued(session, at=night)
    bot = _Bot()
    await worker.safety_notice_job(bot, now=night + timedelta(minutes=1))
    [(_, kwargs)] = bot.sent
    assert kwargs["disable_notification"] is True


async def test_a_seeded_message_produces_the_warning(session, media_in_tmp):
    """Acceptance: a scam message in, one warning out, naming the rule."""
    await _private_monitor()
    await userbot.ingest_message(
        _Client(), _tg_message("Telefoningizga kelgan kodni yozib yuboring", mid=80)
    )
    bot = _Bot()
    await worker.safety_notice_job(bot)
    [(text, _)] = bot.sent
    assert text == (
        "🚫 Akmaldan kelgan xabar firibgarlikka o'xshaydi (SMS kod yoki parol "
        "so'ralyapti). Javob bermang, havolani ochmang."
    )


# --- the brief ---------------------------------------------------------------------


async def test_the_brief_counts_yesterdays_spam(session):
    now = datetime.now(TZ).replace(hour=9, minute=0, second=0, microsecond=0)
    yesterday = now - timedelta(days=1)
    for verdict, direction, at in (
        ("spam", Direction.in_, yesterday),
        ("scam", Direction.in_, yesterday),
        (safety.VERDICT_DANGEROUS_FILE, Direction.in_, yesterday),
        ("hijack", Direction.in_, yesterday),  # warned about, not ignored
        ("scam", Direction.out, yesterday),
        ("spam", Direction.in_, now - timedelta(minutes=30)),  # today
        ("archive", Direction.in_, yesterday),  # an import, never seen arrive
    ):
        meta = {"tg_message_id": next(_ids), "safety": {"verdict": verdict}}
        if verdict == "archive":
            meta = {**meta, "archive": True, "safety": {"verdict": "dangerous_file"}}
        session.add(
            m.Interaction(
                source=InteractionSource.telegram_userbot,
                direction=direction,
                tg_chat_id=CHAT,
                occurred_at=at,
                meta=meta,
            )
        )
    await session.commit()

    gathered = await brief.gather(session, now=now)
    assert gathered.unsafe_ignored == 3
    text = replies.morning_brief(gathered)
    assert "🚫 Kecha 3 ta spam/firibgar xabar e'tiborsiz qoldirildi." in text


def test_the_brief_says_nothing_without_spam():
    empty = brief.MorningBrief(now=DAY)
    assert "spam" not in replies.morning_brief(empty)
    assert "🚫 Kecha 2 ta" in replies.morning_brief(
        brief.MorningBrief(now=DAY, unsafe_ignored=2)
    )


async def test_a_failing_spam_count_never_costs_the_brief(session, monkeypatch):
    async def _broken(*args, **kwargs):
        raise RuntimeError("db")

    monkeypatch.setattr(brief.queries, "unsafe_ignored_count", _broken)
    gathered = await brief.gather(session, now=DAY)
    assert gathered.unsafe_ignored == 0

"""WP-62: a fixed set of conversations for the recall eval.

``build`` seeds people, chats, messages (a voice note, a call, a group
window, a note and a question), two facts, and indexes everything. It
returns each message's key → interaction id (``f1``/``f2`` → memory id).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from miya.config import settings
from miya.db import models as m
from miya.db.enums import ChatType, Direction, InteractionSource, WindowStatus
from miya.services import codes, passages, windows
from miya.services.embeddings import Embedder

TZ = settings.tz
FIXTURE_NOW = datetime(2026, 9, 25, 12, 0, tzinfo=TZ)
GROUP = -100500

PEOPLE = {
    "AkmalK": {
        "display_name": "Akmal Karimov",
        "aliases": ["Akmal aka"],
        "telegram_id": 1001,
        "notes": "yetkazib beruvchi, Yiwu",
    },
    "AkmalT": {"display_name": "Akmal Toshkent", "telegram_id": 1002, "notes": "mijoz"},
    "Sardor": {
        "display_name": "Sardor",
        "aliases": ["Sardor broker"],
        "telegram_id": 1003,
        "phone": "+998901112233",
    },
    "Бобур": {"display_name": "Бобур", "telegram_id": 1004},
    "Wang": {"display_name": "Wang Wei", "telegram_id": 1005},
    "Dilnoza": {"display_name": "Dilnoza", "telegram_id": 1006},
}
CHATS = [
    (1001, ChatType.private, "Akmal Karimov"),
    (1002, ChatType.private, "Akmal Toshkent"),
    (GROUP, ChatType.group, "GSR Yiwu ombor"),
    (1004, ChatType.private, "Бобур"),
    (1005, ChatType.private, "Wang Wei"),
    (1006, ChatType.private, "Dilnoza"),
]

# key, chat, Tashkent time, sender (None = the owner), text, extra fields
MESSAGES = [
    ("a1", 1001, "2026-09-10 10:02", "AkmalK", "Assalomu alaykum, Bekzod aka", {}),
    ("a2", 1001, "2026-09-10 10:05", None, "Konteyner qachon chiqadi?", {}),
    (
        "a3",
        1001,
        "2026-09-10 10:20",
        "AkmalK",
        None,
        {
            "transcript": "Konteyner Qorg'osda uch kundan beri turibdi, "
            "hujjatlar chala ekan",
            "media": {"type": "voice", "processed": True},
        },
    ),
    ("a4", 1001, "2026-09-10 10:22", None, "Qaysi hujjat yetishmayapti?", {}),
    (
        "a5",
        1001,
        "2026-09-10 10:30",
        "AkmalK",
        "Invoysda og'irlik noto'g'ri yozilgan, qayta qilib beraman",
        {},
    ),
    (
        "a6",
        1001,
        "2026-08-15 09:00",
        "AkmalK",
        "Keyingi partiya narxi kilosiga 1.2 dollar bo'ladi",
        {},
    ),
    (
        "g0",
        GROUP,
        "2026-08-20 10:00",
        "Sardor",
        "Avgust oxirida bojxonada yangi qoidalar kiradi",
        {},
    ),
    ("g1", GROUP, "2026-09-11 14:00", "Sardor", "Salom hammaga", {}),
    (
        "g2",
        GROUP,
        "2026-09-11 14:03",
        "Sardor",
        "Bojxona konteynerni ushlab qoldi, sertifikat kerak ekan",
        {},
    ),
    ("g3", GROUP, "2026-09-11 14:05", "AkmalK", "Sertifikatni ertaga yuboraman", {}),
    ("g4", GROUP, "2026-09-11 14:06", None, "Tezroq bo'lsin, mijoz kutyapti", {}),
    (
        "g5",
        GROUP,
        "2026-09-12 09:10",
        "AkmalK",
        "Kechirasiz, sertifikat bugun ham tayyor bo'lmadi",
        {},
    ),
    (
        "g6",
        GROUP,
        "2026-09-18 16:40",
        "Sardor",
        "GS367 ning 12 ta karobkasi Yiwu omborga keldi, nakladnoy YW26-004715",
        {},
    ),
    ("g7", GROUP, "2026-09-18 16:45", "AkmalK", "ok", {}),
    (
        "t1",
        1002,
        "2026-09-05 11:00",
        "AkmalT",
        "Bekzod aka, mebel yukim qachon keladi?",
        {},
    ),
    ("t2", 1002, "2026-09-05 11:10", None, "Keyingi haftada Toshkentda bo'ladi", {}),
    (
        "b1",
        1004,
        "2026-09-19 18:00",
        "Бобур",
        "Ассалому алайкум, GS 367 юким қачон келади?",
        {},
    ),
    ("b2", 1004, "2026-09-19 18:05", None, "Yiwu omborga keldi, 12 ta karobka", {}),
    ("w1", 1005, "2026-08-20 10:00", "Wang", "Цена за кг будет 1.1 доллара", {}),
    (
        "w2",
        1005,
        "2026-09-17 15:30",
        "Wang",
        "Оплата будет в пятницу, после отгрузки",
        {},
    ),
    ("w3", 1005, "2026-09-17 15:31", "Wang", "Новая цена 1.3 доллара за кг", {}),
    (
        "d1",
        1006,
        "2026-09-16 12:00",
        "Dilnoza",
        "Avgust hisobotini tayyorladim, soliqqa topshirdim",
        {},
    ),
    ("d2", 1006, "2026-09-16 12:05", None, "Rahmat", {}),
    ("n1", 1001, "2026-09-01 09:00", "AkmalK", "rahmat", {}),
    ("n2", GROUP, "2026-09-02 09:00", "Sardor", "ok", {}),
    ("n3", 1005, "2026-09-03 09:00", "Wang", "xo'p", {}),
    ("n4", 1006, "2026-09-04 09:00", "Dilnoza", "👍", {}),
    ("n5", 1002, "2026-09-06 09:00", "AkmalT", "Ertaga uchrashamiz", {}),
    ("n6", 1006, "2026-09-07 13:00", "Dilnoza", "Ovqatga chiqamizmi?", {}),
    ("n7", 1004, "2026-09-08 09:00", None, "Jo'natdim", {}),
    ("n8", GROUP, "2026-09-09 09:00", "Sardor", "Bugun ob-havo yaxshi", {}),
]

CALL_TRANSCRIPT = (
    "Assalomu alaykum Bekzod aka, bojxonada yuk hali ham ushlab turibdi, sertifikat "
    "kelmaguncha chiqarmaymiz deyishyapti. Men ertaga inspektor bilan gaplashaman."
)


def _at(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)


async def build(session, embedder: Embedder | None = None) -> dict[str, int]:
    ids: dict[str, int] = {}
    people: dict[str, m.Person] = {}
    for key, fields in PEOPLE.items():
        person = m.Person(**{"aliases": [], **fields})
        session.add(person)
        people[key] = person
    session.add_all(
        m.ChatMonitor(tg_chat_id=chat, chat_type=kind, title=title)
        for chat, kind, title in CHATS
    )
    await session.flush()
    await codes.attach(session, people["Бобур"], "GS367", source="command", by="command")

    rows: dict[str, m.Interaction] = {}
    for number, (key, chat, when, sender, text, extra) in enumerate(MESSAGES, 1):
        chat_person = next((p for p in people.values() if p.telegram_id == chat), None)
        speaker = people[sender] if sender else None
        row = m.Interaction(
            source=InteractionSource.telegram_userbot,
            direction=Direction.in_ if sender else Direction.out,
            # A DM's rows belong to its person; a group's to the speaker.
            person_id=(speaker or chat_person).id if (speaker or chat_person) else None,
            tg_chat_id=chat,
            occurred_at=_at(when),
            raw_text=text,
            processed=True,
            meta={"tg_message_id": number},
            **extra,
        )
        session.add(row)
        rows[key] = row

    rows["call1"] = m.Interaction(
        source=InteractionSource.phone_call,
        direction=Direction.in_,
        person_id=people["Sardor"].id,
        occurred_at=_at("2026-09-16 17:00"),
        raw_text="[qo'ng'iroq ← Sardor]",
        transcript=CALL_TRANSCRIPT,
        media={"type": "call", "phone": "+998901112233", "processed": True},
        processed=True,
    )
    rows["note1"] = m.Interaction(
        source=InteractionSource.assistant_bot,
        direction=Direction.in_,
        occurred_at=_at("2026-09-12 20:00"),
        raw_text="Akmal sertifikatni yana kechiktirdi, bu uchinchi marta",
        processed=True,
    )
    rows["q1"] = m.Interaction(
        source=InteractionSource.assistant_bot,
        direction=Direction.in_,
        occurred_at=_at("2026-09-13 09:00"),
        raw_text="Akmal konteyner nima bo'ldi?",
        meta={"kind": "question"},
        processed=True,
    )
    session.add_all([rows["call1"], rows["note1"], rows["q1"]])
    await session.flush()

    members = [rows["g2"], rows["g3"], rows["g4"]]
    names = {p.id: p.display_name for p in people.values()}
    window = m.ConversationWindow(
        tg_chat_id=GROUP,
        person_id=None,
        started_at=members[0].occurred_at,
        ended_at=members[-1].occurred_at,
        message_count=len(members),
        char_count=sum(len(r.raw_text or "") for r in members),
        text=windows.render_window(members, names),
        status=WindowStatus.applied,
        custom_id=f"w-{uuid.uuid4().hex}",
    )
    session.add(window)
    await session.flush()
    for row in members:
        row.window_id = window.id
    # As batch._window_interaction builds it.
    rows["win1"] = m.Interaction(
        source=InteractionSource.telegram_userbot,
        direction=Direction.na,
        person_id=None,
        tg_chat_id=GROUP,
        occurred_at=window.ended_at,
        raw_text=window.text,
        meta={
            "kind": "window",
            "window_id": window.id,
            "message_count": window.message_count,
        },
        window_id=window.id,
        processed=True,
    )
    session.add(rows["win1"])

    facts = {
        "f1": m.Memory(
            content="Akmal Karimov Yiwu'dan mebel yetkazib beradi",
            person_id=people["AkmalK"].id,
            occurred_at=_at("2026-09-01 12:00"),
            tags=[],
        ),
        "f2": m.Memory(
            content="Sardor bojxona sertifikati haqida ogohlantirdi",
            occurred_at=_at("2026-09-11 15:00"),
            tags=[],
        ),
    }
    session.add_all(facts.values())
    await session.flush()

    for row in rows.values():
        await codes.index_interaction(session, row)
    while await passages.index_pending(session):
        pass
    if embedder is not None:
        while await passages.embed_pending(session, embedder):
            pass

    ids.update({key: row.id for key, row in rows.items()})
    ids.update({key: fact.id for key, fact in facts.items()})
    ids["people"] = {key: p.id for key, p in people.items()}  # type: ignore[assignment]
    ids["window"] = window.id
    return ids

"""Chat monitors and the `/chats` keyboard (spec §6)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa

from alembic.migration import MigrationContext
from alembic.operations import Operations
from miya.bot.keyboards import PAGE_SIZE, ChatsPage, chats_keyboard
from miya.db import models as m
from miya.db.enums import ChatType
from miya.services import chats


def _dialog(chat_id: int, chat_type: ChatType, title: str) -> chats.DialogInfo:
    return chats.DialogInfo(tg_chat_id=chat_id, chat_type=chat_type, title=title)


async def test_sync_applies_the_spec_defaults(session):
    created, renamed = await chats.sync_dialogs(
        session,
        [
            _dialog(111, ChatType.private, "Akmal aka"),
            _dialog(-222, ChatType.group, "GZ logistika"),
            _dialog(-333, ChatType.channel, "Bojxona yangiliklari"),
        ],
    )

    assert (created, renamed) == (3, 0)
    rows = {r.tg_chat_id: r for r in await session.scalars(sa.select(m.ChatMonitor))}
    assert rows[111].monitor_enabled is True  # private: on
    assert rows[-222].monitor_enabled is False  # group: whitelist only
    assert rows[-333].monitor_enabled is False  # channel: off
    # Vision is on everywhere (0005): receipts and payment screenshots are images.
    assert all(r.vision_enabled is True for r in rows.values())
    assert all(r.docs_enabled is True for r in rows.values())


async def test_resync_never_overwrites_the_owners_toggles(session):
    await chats.sync_dialogs(session, [_dialog(-222, ChatType.group, "GZ logistika")])
    monitor = await chats.get_monitor(session, -222)
    monitor.monitor_enabled = True  # the owner whitelisted this work group
    monitor.vision_enabled = True
    await session.flush()

    created, renamed = await chats.sync_dialogs(
        session, [_dialog(-222, ChatType.group, "GZ logistika 2026")]
    )

    assert (created, renamed) == (0, 1)
    assert monitor.title == "GZ logistika 2026"  # titles do follow Telegram
    assert monitor.monitor_enabled is True
    assert monitor.vision_enabled is True


async def test_ensure_monitor_creates_a_chat_seen_for_the_first_time(session):
    monitor = await chats.ensure_monitor(
        session, _dialog(444, ChatType.private, "Yangi odam")
    )
    assert monitor.monitor_enabled is True

    again = await chats.ensure_monitor(
        session, _dialog(444, ChatType.private, "Yangi odam")
    )
    assert again.id == monitor.id


async def test_every_newly_discovered_chat_reads_images(session):
    """Owner decision: receipts and payment screenshots arrive as photos, so a
    chat must start with vision on — whichever path first registers it, and
    whatever its type. Read back from the database, not from the instance."""
    await chats.sync_dialogs(
        session,
        [
            _dialog(777, ChatType.private, "Dilshod"),
            _dialog(-778, ChatType.group, "Yuk guruhi"),
            _dialog(-779, ChatType.channel, "Kanal"),
        ],
    )
    await chats.ensure_monitor(session, _dialog(780, ChatType.private, "Sardor"))
    await chats.ensure_monitor(session, _dialog(-781, ChatType.group, "Omborxona"))

    rows = dict(
        (
            await session.execute(
                sa.select(m.ChatMonitor.tg_chat_id, m.ChatMonitor.vision_enabled)
            )
        ).all()
    )
    assert rows == {777: True, -778: True, -779: True, 780: True, -781: True}


async def test_the_schema_owns_the_fixed_toggle_defaults(session):
    """The registry passes neither `vision_enabled` nor `docs_enabled` on insert,
    relying on the column defaults the migrations set (0001, 0005). If someone
    changes the model's `server_default` without a migration, or the other way
    round, this is the test that notices."""
    await session.execute(
        sa.insert(m.ChatMonitor).values(tg_chat_id=888, chat_type=ChatType.private)
    )
    row = (
        await session.execute(
            sa.select(
                m.ChatMonitor.monitor_enabled,
                m.ChatMonitor.vision_enabled,
                m.ChatMonitor.docs_enabled,
            ).where(m.ChatMonitor.tg_chat_id == 888)
        )
    ).one()
    assert tuple(row) == (False, True, True)


def _load_revision(name: str):
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_0006_switches_vision_on_for_chats_made_while_the_service_forced_it_off(
    session,
):
    """Chats registered between 0005 and the service fix were inserted with an
    explicit false. The repair flips exactly those and leaves the rest alone."""
    seeded = {901: False, 902: True, -903: False}
    await session.execute(
        sa.insert(m.ChatMonitor).values(
            [
                {"tg_chat_id": cid, "chat_type": ChatType.private, "vision_enabled": on}
                for cid, on in seeded.items()
            ]
        )
    )
    revision = _load_revision("0006_vision_on_for_new_chats")

    def run_upgrade(sync_session):
        ctx = MigrationContext.configure(sync_session.connection())
        with Operations.context(ctx):
            revision.upgrade()

    await session.run_sync(run_upgrade)

    rows = dict(
        (
            await session.execute(
                sa.select(m.ChatMonitor.tg_chat_id, m.ChatMonitor.vision_enabled)
            )
        ).all()
    )
    assert rows == {901: True, 902: True, -903: True}


async def test_toggle_flips_exactly_one_flag(session):
    monitor = await chats.ensure_monitor(
        session, _dialog(555, ChatType.group, "Ish guruhi")
    )
    assert monitor.monitor_enabled is False

    await chats.toggle(session, monitor.id, "monitor_enabled")
    assert monitor.monitor_enabled is True
    assert monitor.vision_enabled is True  # untouched: still the default
    assert monitor.docs_enabled is True

    await chats.toggle(session, monitor.id, "monitor_enabled")
    assert monitor.monitor_enabled is False


async def test_an_unknown_toggle_field_is_refused(session):
    monitor = await chats.ensure_monitor(
        session, _dialog(666, ChatType.private, "Kimdir")
    )
    try:
        await chats.toggle(session, monitor.id, "processed")
    except ValueError as exc:
        assert "processed" in str(exc)
    else:  # pragma: no cover - the guard must hold
        raise AssertionError("an arbitrary column must not be toggleable")


async def test_monitored_chats_are_listed_first(session):
    await chats.sync_dialogs(
        session,
        [
            _dialog(-1, ChatType.group, "O'chirilgan guruh"),
            _dialog(2, ChatType.private, "Yoqilgan odam"),
        ],
    )
    rows, total = await chats.list_monitors(session)

    assert total == 2
    assert rows[0].tg_chat_id == 2


# --- keyboard ---------------------------------------------------------------


def _monitor(**kwargs) -> m.ChatMonitor:
    defaults = {
        "id": 1,
        "tg_chat_id": 42,
        "chat_type": ChatType.private,
        "title": "Akmal aka",
        "monitor_enabled": True,
        "vision_enabled": False,
        "docs_enabled": True,
    }
    return m.ChatMonitor(**{**defaults, **kwargs})


def test_the_keyboard_shows_state_and_carries_toggle_payloads():
    markup = chats_keyboard(ChatsPage(monitors=[_monitor()], page=0, total=1))
    [row] = markup.inline_keyboard

    assert row[0].text.startswith("✅")
    assert "Akmal aka" in row[0].text
    assert row[0].callback_data == "ch:t:1:m"
    assert row[1].callback_data == "ch:t:1:v"
    assert row[2].callback_data == "ch:t:1:d"
    assert row[1].text == "👁❌"
    assert row[2].text == "📄✅"


def test_a_long_chat_title_is_truncated_to_fit_a_button():
    markup = chats_keyboard(
        ChatsPage(monitors=[_monitor(title="A" * 80)], page=0, total=1)
    )
    assert len(markup.inline_keyboard[0][0].text) < 40


def test_navigation_appears_only_when_there_is_a_second_page():
    one_page = chats_keyboard(ChatsPage(monitors=[_monitor()], page=0, total=1))
    assert len(one_page.inline_keyboard) == 1

    many = chats_keyboard(ChatsPage(monitors=[_monitor()], page=0, total=PAGE_SIZE * 3))
    nav = many.inline_keyboard[-1]
    assert nav[1].text == "1/3"
    # Paging wraps, so ◀️ on the first page lands on the last.
    assert nav[0].callback_data == "ch:p:2"
    assert nav[2].callback_data == "ch:p:1"

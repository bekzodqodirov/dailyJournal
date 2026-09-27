"""WP-34: import the owner's client-code list from CSV or Excel."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from miya.bot import handlers, replies
from miya.config import settings
from miya.db import models as m
from miya.services import client_import, codes
from tests.test_close_and_correct import _Callback, _Message, bound  # noqa: F401


def _csv(tmp_path, text: str, name: str = "clients.csv", bom: bool = False):
    path = tmp_path / name
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode())
    return path


async def _person(session, name: str, code: str | None = None, **fields) -> m.Person:
    person = m.Person(display_name=name, aliases=[], **fields)
    session.add(person)
    await session.flush()
    if code:
        await codes.attach(session, person, code, source="command", by="command")
    return person


# --- reading -------------------------------------------------------------------


def test_reads_a_semicolon_csv_with_a_bom(tmp_path):
    path = _csv(tmp_path, "Kod;Ism;Telefon\nGS367;Akmal;+998 90 123 45 67\n", bom=True)
    [row] = client_import.read_rows(path)
    assert (row.code_raw, row.name, row.phone, row.line_no) == (
        "GS367",
        "Akmal",
        "+998 90 123 45 67",
        2,
    )


def test_reads_english_headers_in_any_order(tmp_path):
    path = _csv(tmp_path, "name,code,username,note\nVali,gs-12,@vali,supplier\n")
    [row] = client_import.read_rows(path)
    assert (row.code_raw, row.name, row.telegram, row.note) == (
        "gs-12",
        "Vali",
        "@vali",
        "supplier",
    )


def test_a_headerless_file_is_code_then_name(tmp_path):
    path = _csv(tmp_path, "GS1,Akmal\n\nGS2,Vali\n")
    rows = client_import.read_rows(path)
    assert [(r.code_raw, r.name) for r in rows] == [("GS1", "Akmal"), ("GS2", "Vali")]


def test_reads_an_xlsx(tmp_path):
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.append(["Mijoz kodi", "F.I.O", "Tel"])
    sheet.append(["GS367", "Akmal", 998901234567])
    path = tmp_path / "clients.xlsx"
    book.save(path)
    [row] = client_import.read_rows(path)
    assert (row.code_raw, row.name, row.phone) == ("GS367", "Akmal", "998901234567")


def test_an_unreadable_file_is_bad(tmp_path):
    with pytest.raises(client_import.BadFile):
        client_import.read_rows(_csv(tmp_path, "", "x.csv"))
    with pytest.raises(client_import.BadFile):
        client_import.read_rows(_csv(tmp_path, "hello", "x.pdf"))
    with pytest.raises(client_import.BadFile):
        client_import.read_rows(_csv(tmp_path, "ism,telefon\nAkmal,1\n"))


# --- planning and applying -----------------------------------------------------


async def test_plan_sorts_every_case_and_writes_nothing(session, tmp_path):
    await _person(session, "Akmal", "GS1")  # same
    await _person(session, "Vali", "GS2")  # conflict: the file says Sardor
    await _person(session, "Dilnoza", phone="+998901112233")  # attach by phone
    await _person(session, "Bobur", telegram_username="Bobur_tg")  # by username
    await _person(session, "Jasur")  # attach by name
    path = _csv(
        tmp_path,
        "kod,ism,telefon,telegram\n"
        "GS1,Akmal,,\n"
        "GS2,Sardor,,\n"
        "GS3,Dilnoza X,90 111 22 33,\n"
        "GS4,Bobur,,@bobur_TG\n"
        "GS5,Jasur,,\n"
        "GS6,Yangi Odam,,\n"
        "GS7,Aziz,,\n"
        "GS7,Anvar,,\n"
        "XYZ,Nobody,,\n"
        "GS8,,,\n",
    )
    rows = client_import.read_rows(path)
    before = await session.scalar(sa.select(sa.func.count(m.ClientCode.id)))

    plan = await client_import.plan_import(session, rows)

    def codes_in(kind):
        return sorted(p.code or p.row.code_raw for p in getattr(plan, kind))

    assert codes_in("same") == ["GS1"]
    assert codes_in("conflict") == ["GS2", "GS7", "GS7"]
    assert codes_in("attach") == ["GS3", "GS4", "GS5"]
    assert codes_in("create") == ["GS6"]
    assert codes_in("bad") == ["GS8", "XYZ"]
    assert await session.scalar(sa.select(sa.func.count(m.ClientCode.id))) == before
    [vali] = [p for p in plan.conflict if p.code == "GS2"]
    assert vali.against == "Vali"


async def test_a_namesake_who_is_already_a_client_is_not_reused(session, tmp_path):
    await _person(session, "Akmal", "GS1")
    rows = client_import.read_rows(_csv(tmp_path, "kod,ism\nGS2,Akmal\n"))
    plan = await client_import.plan_import(session, rows)
    assert [p.code for p in plan.create] == ["GS2"]


async def test_apply_writes_and_a_second_run_is_all_same(session, tmp_path):
    jasur = await _person(session, "Jasur")
    path = _csv(
        tmp_path,
        "kod,ism,telefon,telegram,izoh\n"
        "GS5,Jasur,+998 90 555 66 77,@jasur,\n"
        "GS6,Yangi Odam,,,yetkazib beruvchi\n"
        "GS9,Sobir,,,\n",
    )
    plan = await client_import.plan_import(session, client_import.read_rows(path))
    written = await client_import.apply_import(session, plan, by="import")
    assert written == {"attach": 1, "create": 2}
    assert (await codes.holder(session, "GS5")).id == jasur.id
    assert jasur.phone == "998905556677" and jasur.telegram_username == "jasur"
    new = await codes.holder(session, "GS6")
    assert new.display_name == "Yangi Odam" and new.relationship_ == "yetkazib beruvchi"
    assert (await codes.holder(session, "GS9")).relationship_ == "mijoz"
    [row] = list(
        await session.scalars(sa.select(m.ClientCode).where(m.ClientCode.code == "GS6"))
    )
    assert row.source == "import"

    again = await client_import.plan_import(session, client_import.read_rows(path))
    assert again.counts() == {
        "attach": 0,
        "create": 0,
        "same": 3,
        "conflict": 0,
        "bad": 0,
    }


# --- the bot -------------------------------------------------------------------


@pytest.fixture
def media_in_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr(type(settings), "media_dir", property(lambda self: tmp_path))
    return tmp_path


class _DocMessage(_Message):
    def __init__(self, filename: str) -> None:
        super().__init__()
        self.document = SimpleNamespace(
            file_name=filename, file_size=100, mime_type="text/csv"
        )
        self.caption = "/kodlar"
        self.date = datetime.now(settings.tz)
        self.edited_text: str | None = None

    async def edit_text(self, text, reply_markup=None):
        self.edited_text = text


def _no_model(monkeypatch):
    async def boom(*args, **kwargs):
        raise AssertionError("the model must not read a client list")

    monkeypatch.setattr(handlers, "process_interaction", boom)


def _download_writes(monkeypatch, content: bytes):
    async def fake(bot, message, media, path):
        path.write_bytes(content)
        return True

    monkeypatch.setattr(handlers, "_download", fake)


async def test_a_captioned_file_previews_then_applies(
    bound,  # noqa: F811
    monkeypatch,
    media_in_tmp,
):
    _no_model(monkeypatch)
    _download_writes(monkeypatch, b"kod,ism\nGS367,Akmal\nGS412,Vali\n")
    message = _DocMessage("clients.csv")

    await handlers.cmd_code_import(message, bot=None)

    [(text, keyboard)] = message.sent
    counts = {"attach": 0, "create": 2, "same": 0, "conflict": 0, "bad": 0}
    assert text == replies.import_preview("clients.csv", counts)
    [[yes, no]] = keyboard.inline_keyboard
    assert yes.callback_data.startswith("kod:imp:") and no.callback_data == "kod:impno"
    assert await codes.holder(bound, "GS367") is None

    await handlers.on_code_button(_Callback(yes.callback_data, message))
    assert message.edited_text == replies.import_done({"attach": 0, "create": 2}, [])
    assert (await codes.holder(bound, "GS367")).display_name == "Akmal"


async def test_cancel_writes_nothing(bound, monkeypatch, media_in_tmp):  # noqa: F811
    _no_model(monkeypatch)
    _download_writes(monkeypatch, b"kod,ism\nGS367,Akmal\n")
    message = _DocMessage("clients.csv")
    await handlers.cmd_code_import(message, bot=None)
    await handlers.on_code_button(_Callback("kod:impno", message))
    assert message.edited_text == replies.IMPORT_CANCELLED
    assert await codes.holder(bound, "GS367") is None


async def test_an_unreadable_file_says_which_columns(bound, monkeypatch, media_in_tmp):  # noqa: F811
    _no_model(monkeypatch)
    _download_writes(monkeypatch, b"%PDF-1.4")
    message = _DocMessage("clients.pdf")
    await handlers.cmd_code_import(message, bot=None)
    assert message.sent == [(replies.IMPORT_BAD_FILE, None)]


def test_every_command_is_registered_before_the_content_handlers():
    """A Command handler after the catch-all would never run in production."""
    names = [h.callback.__name__ for h in handlers.router.message.handlers]
    first_content = names.index("on_text")
    for handler in handlers.router.message.handlers[first_content:]:
        assert not any(
            type(f.callback).__name__ in ("Command", "CommandStart")
            for f in handler.filters
        ), handler.callback.__name__
    assert names.index("cmd_code_import") < names.index("cmd_code_suggestions")

"""The Google Sheets one-way mirror (JV-07). The Sheets API is faked; rows are
real PostgreSQL rows."""

from __future__ import annotations

import importlib.util
import logging
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from miya.bot import replies
from miya.config import settings
from miya.db import models as m
from miya.db.enums import (
    Currency,
    Direction,
    InteractionSource,
    PromiseMadeBy,
    PromiseStatus,
    TaskStatus,
    TransactionType,
)
from miya.services import health, purge, sheets, sheets_sync
from miya.services.sheets import CellRange, TabInfo

TZ = settings.tz
_RANGE = re.compile(r"^'(?P<tab>[^']+)'!A(?P<row>\d+):[A-Z](?P=row)$")


class FakeSheetsAPI(sheets.SheetsAPI):
    """An in-memory spreadsheet: tabs with a grid size, cells by row."""

    def __init__(self, tabs: dict[str, TabInfo] | None = None) -> None:
        self.info: dict[str, TabInfo] = dict(tabs or {})
        self.grid: dict[str, dict[int, list[Any]]] = {name: {} for name in self.info}
        self.requests: list[list[dict[str, Any]]] = []
        self.writes: list[list[CellRange]] = []
        self.tab_calls = 0
        # The next write fails with this; ``land`` decides whether its cells
        # reached the sheet first (a timeout after the server applied it).
        self.fail_write: BaseException | None = None
        self.land = False

    async def tabs(self) -> dict[str, TabInfo]:
        self.tab_calls += 1
        return dict(self.info)

    async def batch_update(self, requests: list[dict[str, Any]]) -> None:
        self.requests.append(requests)
        by_id = {info.sheet_id: name for name, info in self.info.items()}
        for request in requests:
            if "addSheet" in request:
                props = request["addSheet"]["properties"]
                grid = props["gridProperties"]
                self.info[props["title"]] = TabInfo(
                    props["sheetId"], grid["rowCount"], grid["columnCount"]
                )
                self.grid[props["title"]] = {}
                by_id[props["sheetId"]] = props["title"]
            elif "updateCells" in request:
                body = request["updateCells"]
                tab = by_id[body["start"]["sheetId"]]
                values = body["rows"][0]["values"]
                self.grid[tab][1] = [v["userEnteredValue"]["stringValue"] for v in values]
            elif "updateSheetProperties" in request:
                props = request["updateSheetProperties"]["properties"]
                tab = by_id[props["sheetId"]]
                grid = props["gridProperties"]
                self.info[tab] = TabInfo(
                    props["sheetId"], grid["rowCount"], grid["columnCount"]
                )

    def _apply(self, data: list[CellRange]) -> None:
        for item in data:
            match = _RANGE.match(item.range)
            assert match, item.range
            tab, row = match["tab"], int(match["row"])
            assert row <= self.info[tab].row_count, "write past the grid"
            self.grid[tab][row] = list(item.values[0])

    async def write_values(self, data: list[CellRange]) -> None:
        if self.fail_write is not None:
            exc, self.fail_write = self.fail_write, None
            if self.land:
                self._apply(data)
            raise exc
        self.writes.append(data)
        self._apply(data)

    def rows(self, tab: str) -> dict[int, list[Any]]:
        """Data rows with anything in them (row 1 is the header)."""
        return {
            n: cells
            for n, cells in self.grid.get(tab, {}).items()
            if n > 1 and any(c != "" for c in cells)
        }

    def by_id(self, tab: str) -> dict[str, list[Any]]:
        return {cells[0]: cells for cells in self.rows(tab).values()}


def _now() -> datetime:
    return datetime.now(TZ).replace(microsecond=0)


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "sheets_enabled", True)
    monkeypatch.setattr(settings, "sheets_spreadsheet_id", "sheet-test-id")


async def _person(session, name: str = "Akmal") -> m.Person:
    person = m.Person(display_name=name)
    session.add(person)
    await session.flush()
    return person


async def _message(session, person: m.Person | None, **overrides) -> m.Interaction:
    fields: dict[str, Any] = {
        "source": InteractionSource.telegram_userbot,
        "direction": Direction.in_,
        "person_id": person.id if person else None,
        "tg_chat_id": 5001,
        "occurred_at": _now() - timedelta(hours=1),
        "raw_text": "Ertaga hujjatlarni olib kelaman",
    }
    fields.update(overrides)
    interaction = m.Interaction(**fields)
    session.add(interaction)
    await session.flush()
    return interaction


async def _promise(session, person: m.Person, **overrides) -> m.Promise:
    fields: dict[str, Any] = {
        "made_by": PromiseMadeBy.me,
        "person_id": person.id,
        "description": "hujjatlarni yuborish",
        "due_date": date(2026, 10, 22),
        "due_time": time(14, 0),
    }
    fields.update(overrides)
    promise = m.Promise(**fields)
    session.add(promise)
    await session.flush()
    return promise


async def _receipt(session, person: m.Person, **overrides) -> m.Receipt:
    interaction = await _message(session, person, raw_text=None)
    fields: dict[str, Any] = {
        "interaction_id": interaction.id,
        "person_id": person.id,
        "direction": "in",
        "amount": Decimal("1250000.00"),
        "currency": Currency.UZS,
        "paid_at": datetime(2026, 10, 20, 14, 3, tzinfo=TZ),
        "payer_name": "AKMAL R.",
        "payee_name": "BEKZOD Q.",
        "payee_card_last4": "4321",
        "issuer": "Payme",
        "reference": "TX-998877",
        "state": "read",
    }
    fields.update(overrides)
    receipt = m.Receipt(**fields)
    session.add(receipt)
    await session.flush()
    return receipt


async def _ledger(session) -> list[m.SheetRow]:
    return list(
        await session.scalars(
            sa.select(m.SheetRow)
            .order_by(m.SheetRow.tab, m.SheetRow.row_number)
            .execution_options(populate_existing=True)
        )
    )


# --- writing ----------------------------------------------------------------------


async def test_rows_written_once_with_fake_api(session):
    person = await _person(session)
    source = await _message(session, person)
    promise = await _promise(session, person, source_interaction_id=source.id)
    task = m.Task(description="Bojxonaga borish", status=TaskStatus.todo)
    session.add(task)
    receipt = await _receipt(session, person)
    await session.commit()
    api = FakeSheetsAPI()

    result = await sheets_sync.sync(session, api, now=_now())

    assert result.ok and result.written == 4 and result.failed == 0
    assert sorted(result.created_tabs) == sorted(m.SHEET_TABS)
    tasks = api.by_id("Tasks_Promises")
    assert set(tasks) == {f"p{promise.id}", f"t{task.id}"}
    assert tasks[f"p{promise.id}"] == [
        f"p{promise.id}",
        "va'da",
        "siz",
        "Akmal",
        "hujjatlarni yuborish",
        "2026-10-22 14:00",
        "ochiq",
        "telegram",
        source.occurred_at.astimezone(TZ).strftime("%Y-%m-%d %H:%M"),
        "Ertaga hujjatlarni olib kelaman",
    ]
    assert tasks[f"t{task.id}"][1:4] == ["vazifa", "siz", ""]
    assert set(api.by_id("Receipts")) == {f"r{receipt.id}"}
    assert set(api.by_id("Client_FollowUps")) == {f"k{person.id}"}
    ledger = await _ledger(session)
    assert {row.state for row in ledger} == {"synced"}
    assert all(row.row_number >= 2 and row.synced_at for row in ledger)

    # Nothing changed: no write, no new row, not even a look at the tabs.
    again = await sheets_sync.sync(session, api, now=_now())
    assert again.ok and again.written == 0
    assert len(api.writes) == 3 and api.tab_calls == 1
    assert len(await _ledger(session)) == 4


async def test_missing_tabs_and_headers_come_from_one_batch_update(session):
    api = FakeSheetsAPI(
        {"Receipts": TabInfo(7, 1000, 26), "Sheet1": TabInfo(0, 1000, 26)}
    )

    await sheets_sync.sync(session, api, now=_now())

    [requests] = api.requests
    added = [r["addSheet"]["properties"] for r in requests if "addSheet" in r]
    assert [p["title"] for p in added] == ["Tasks_Promises", "Client_FollowUps"]
    assert len({p["sheetId"] for p in added} | {0, 7}) == 4
    assert all(p["gridProperties"]["frozenRowCount"] == 1 for p in added)
    assert api.grid["Tasks_Promises"][1] == list(replies.SHEETS_TASKS_HEADER)
    assert api.grid["Client_FollowUps"][1] == list(replies.SHEETS_FOLLOWUPS_HEADER)
    assert " | ".join(replies.SHEETS_TASKS_HEADER) == (
        "ID | Tur | Kim | Kimga | Matn | Muddat | Holat | Kanal | Manba vaqti | Kontekst"
    )
    assert " | ".join(replies.SHEETS_FOLLOWUPS_HEADER) == (
        "ID | Mijoz | Kod | Kanal | Oxirgi xabar | Javobsiz | Ochiq eskalatsiya | "
        "Ochiq va'dalar | Keyingi muddat"
    )
    assert " | ".join(replies.SHEETS_RECEIPTS_HEADER) == (
        "ID | Chekdagi vaqt | Summa | Valyuta | To'lovchi | Qabul qiluvchi karta | "
        "Bank/ilova | Chek № | Kimdan | Kanal | Bank tasdig'i | Bank yozuvi"
    )


async def test_an_existing_tab_gets_its_header_with_every_write(session):
    person = await _person(session)
    await _promise(session, person)
    await session.commit()
    tabs = {tab: TabInfo(i, 1000, 26) for i, tab in enumerate(m.SHEET_TABS)}
    api = FakeSheetsAPI(tabs)

    await sheets_sync.sync(session, api, now=_now())

    assert api.requests == []  # nothing missing, nothing too small
    assert api.grid["Tasks_Promises"][1] == list(replies.SHEETS_TASKS_HEADER)


async def test_existing_empty_tabs_get_their_header_with_nothing_due(session):
    tabs = {tab: TabInfo(i, 1000, 26) for i, tab in enumerate(m.SHEET_TABS)}
    api = FakeSheetsAPI(tabs)

    result = await sheets_sync.sync(session, api, now=_now())

    assert result.ok and api.requests == []
    for tab in m.SHEET_TABS:
        assert api.grid[tab][1] == list(sheets_sync.HEADERS[tab]), tab
        assert api.rows(tab) == {}

    # Seen once in this process: a quiet run does not look again.
    await sheets_sync.sync(session, api, now=_now())
    assert api.tab_calls == 1 and len(api.writes) == 3


async def test_the_grid_grows_before_a_row_past_its_end(session):
    person = await _person(session)
    await _promise(session, person)
    await session.commit()
    tabs = {tab: TabInfo(i, 1, 5) for i, tab in enumerate(m.SHEET_TABS)}
    api = FakeSheetsAPI(tabs)

    result = await sheets_sync.sync(session, api, now=_now())

    assert result.ok
    grown = [r["updateSheetProperties"] for r in api.requests[0]]
    assert {g["properties"]["sheetId"] for g in grown} == {0, 1, 2}
    tasks = next(g for g in grown if g["properties"]["sheetId"] == 0)
    assert tasks["properties"]["gridProperties"]["rowCount"] >= 2
    assert tasks["properties"]["gridProperties"]["columnCount"] == 10
    assert len(api.rows("Tasks_Promises")) == 1


async def test_change_detected_by_hash(session):
    person = await _person(session)
    promise = await _promise(session, person)
    other = await _promise(session, person, description="pul o'tkazish")
    await session.commit()
    api = FakeSheetsAPI()
    await sheets_sync.sync(session, api, now=_now())
    before = {
        row.entity_id: (row.row_number, row.content_hash)
        for row in await _ledger(session)
        if row.tab == "Tasks_Promises"
    }

    promise.description = "hujjatlar va pasport nusxasini yuborish"
    promise.status = PromiseStatus.done
    await session.commit()
    result = await sheets_sync.sync(session, api, now=_now())

    assert result.written == 2  # the promise, and the client's open count
    tasks_write = next(w for w in api.writes[-2:] if "Tasks_Promises" in w[0].range)
    assert [item.range for item in tasks_write[1:]] == [
        sheets_sync.a1_range("Tasks_Promises", before[promise.id][0])
    ]
    cells = api.by_id("Tasks_Promises")[f"p{promise.id}"]
    assert cells[4] == "hujjatlar va pasport nusxasini yuborish"
    assert cells[6] == "bajarilgan"
    after = {
        row.entity_id: (row.row_number, row.content_hash)
        for row in await _ledger(session)
        if row.tab == "Tasks_Promises"
    }
    assert after[promise.id][1] != before[promise.id][1]
    assert after[promise.id][0] == before[promise.id][0]
    assert after[other.id] == before[other.id]
    assert len(api.rows("Tasks_Promises")) == 2


async def test_retry_after_timeout_no_duplicate(session):
    person = await _person(session)
    await _promise(session, person)
    await session.commit()
    api = FakeSheetsAPI()
    # The server applied the write, but the answer never came back.
    api.fail_write, api.land = TimeoutError(), True
    now = _now()

    first = await sheets_sync.sync(session, api, now=now)

    assert first.error == "timeout" and first.failed >= 1
    failed = [r for r in await _ledger(session) if r.state == "failed"]
    assert failed and all(r.last_error == "timeout" and r.attempts == 1 for r in failed)
    assert all(r.next_attempt_at > now for r in failed)

    # Still inside the backoff: nothing is retried.
    writes = len(api.writes)
    await sheets_sync.sync(session, api, now=now + timedelta(seconds=5))
    assert len(api.writes) == writes

    later = now + timedelta(seconds=settings.sheets_sync_seconds + 1)
    second = await sheets_sync.sync(session, api, now=later)

    assert second.ok and second.written >= 1
    assert {r.state for r in await _ledger(session)} == {"synced"}
    assert len(api.rows("Tasks_Promises")) == 1
    assert len(api.rows("Client_FollowUps")) == 1
    assert sorted(api.rows("Tasks_Promises")) == [2]


async def test_the_backoff_counts_from_the_end_of_the_write(session, monkeypatch):
    person = await _person(session)
    await _promise(session, person)
    await session.commit()
    api = FakeSheetsAPI()
    api.fail_write = TimeoutError()
    # The write hung for 400 seconds before it gave up.
    ticks = iter([1000.0, 1400.0])
    monkeypatch.setattr(sheets_sync, "monotonic", lambda: next(ticks))
    now = _now()

    await sheets_sync.sync(session, api, now=now)

    failed = [r for r in await _ledger(session) if r.state == "failed"]
    base = timedelta(seconds=settings.sheets_sync_seconds)
    assert failed
    assert all(r.next_attempt_at == now + timedelta(seconds=400) + base for r in failed)


async def test_a_failing_layout_call_marks_rows_failed_and_writes_nothing(session):
    person = await _person(session)
    await _promise(session, person)
    await session.commit()

    class _Down(FakeSheetsAPI):
        async def tabs(self):
            raise sheets.SheetsError("http_403")

    api = _Down()
    result = await sheets_sync.sync(session, api, now=_now())

    assert result.error == "http_403" and result.written == 0
    assert api.writes == []
    assert {(r.state, r.last_error) for r in await _ledger(session)} == {
        ("failed", "http_403")
    }


async def test_purge_clears_row(session):
    person = await _person(session, "Sardor")
    keep = await _person(session, "Akmal")
    gone = await _promise(session, person)
    kept = await _promise(session, keep)
    await session.commit()
    api = FakeSheetsAPI()
    await sheets_sync.sync(session, api, now=_now())
    assert f"p{gone.id}" in api.by_id("Tasks_Promises")
    gone_id, person_id = gone.id, person.id

    plan = await purge.plan_person(session, person)
    await purge.execute(session, plan)
    await session.commit()

    ledger = await _ledger(session)
    marks = {(r.entity_kind, r.entity_id): r.state for r in ledger}
    assert marks[("promise", gone_id)] == "tombstone"
    assert marks[("person", person_id)] == "tombstone"
    assert marks[("promise", kept.id)] == "synced"
    # No digest of the forgotten cells is left behind.
    hashes = {(r.entity_kind, r.entity_id): r.content_hash for r in ledger}
    assert hashes[("promise", gone_id)] == sheets_sync.BLANK_HASH
    assert hashes[("person", person_id)] == sheets_sync.BLANK_HASH
    assert hashes[("promise", kept.id)] != sheets_sync.BLANK_HASH

    result = await sheets_sync.sync(session, api, now=_now())

    assert result.cleared == 2
    assert set(api.by_id("Tasks_Promises")) == {f"p{kept.id}"}
    assert set(api.by_id("Client_FollowUps")) == {f"k{keep.id}"}
    assert "Sardor" not in repr(api.grid)
    marks = {(r.entity_kind, r.entity_id): r.state for r in await _ledger(session)}
    assert marks[("promise", gone_id)] == "cleared"
    assert marks[("person", person_id)] == "cleared"


async def test_tombstone_never_raises_into_the_purge(session, monkeypatch, caplog):
    async def broken(*args, **kwargs):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(session, "scalars", broken)
    with caplog.at_level(logging.ERROR, logger="miya.services.sheets_sync"):
        assert await sheets_sync.tombstone_missing(session) == 0
    assert "RuntimeError" in caplog.text


async def test_a_client_with_nothing_open_any_more_is_cleared(session):
    person = await _person(session)
    promise = await _promise(session, person)
    await session.commit()
    api = FakeSheetsAPI()
    await sheets_sync.sync(session, api, now=_now())
    assert f"k{person.id}" in api.by_id("Client_FollowUps")

    promise.status = PromiseStatus.done
    await session.commit()
    await sheets_sync.sync(session, api, now=_now())

    assert api.by_id("Client_FollowUps") == {}
    row = next(r for r in await _ledger(session) if r.entity_kind == "person")
    assert row.state == "cleared"

    # They come back, to the same row.
    promise.status = PromiseStatus.open
    await session.commit()
    await sheets_sync.sync(session, api, now=_now())
    back = next(r for r in await _ledger(session) if r.entity_kind == "person")
    assert back.state == "synced" and back.row_number == row.row_number


async def test_followup_row_from_sql(session):
    person = await _person(session, "Javlon")
    session.add(m.ClientCode(code="GS367", person_id=person.id, source="command"))
    await _message(session, person, occurred_at=datetime(2026, 10, 19, 9, 5, tzinfo=TZ))
    await _promise(session, person, due_date=date(2026, 10, 25), due_time=None)
    await _promise(session, person, made_by=PromiseMadeBy.them, due_time=time(9, 30))
    await _promise(session, person, status=PromiseStatus.done, due_date=date(2026, 1, 1))
    await session.commit()
    api = FakeSheetsAPI()

    await sheets_sync.sync(session, api, now=_now())

    assert api.by_id("Client_FollowUps")[f"k{person.id}"] == [
        f"k{person.id}",
        "Javlon",
        "GS367",
        "telegram",
        "2026-10-19 09:05",
        "yo'q",
        "",
        2,
        "2026-10-22 09:30",
    ]
    them = [c for c in api.by_id("Tasks_Promises").values() if c[2] == "Javlon"]
    assert them and them[0][3] == "siz"


async def test_raw_value_option():
    calls: list[tuple[str, dict[str, Any], int]] = []

    class _Request:
        def __init__(self, name: str, kwargs: dict[str, Any], response: dict) -> None:
            self.name, self.kwargs, self.response = name, kwargs, response

        def execute(self, num_retries: int = 0):
            calls.append((self.name, self.kwargs, num_retries))
            return self.response

    class _Values:
        def batchUpdate(self, **kwargs):
            return _Request("values.batchUpdate", kwargs, {})

    class _Spreadsheets:
        def values(self):
            return _Values()

        def get(self, **kwargs):
            props = {"sheetId": 3, "title": "Receipts"}
            props["gridProperties"] = {"rowCount": 1000, "columnCount": 26}
            return _Request("get", kwargs, {"sheets": [{"properties": props}]})

        def batchUpdate(self, **kwargs):
            return _Request("batchUpdate", kwargs, {})

    class _Service:
        def spreadsheets(self):
            return _Spreadsheets()

    api = sheets.GoogleSheetsAPI("sheet-id", "/nonexistent.json", service=_Service())
    formula = '=HYPERLINK("http://x")'
    await api.write_values([CellRange("'Receipts'!A2:L2", [[formula, 5]])])
    assert await api.tabs() == {"Receipts": TabInfo(3, 1000, 26)}
    await api.batch_update([{"addSheet": {}}])

    name, kwargs, retries = calls[0]
    assert name == "values.batchUpdate"
    assert kwargs["spreadsheetId"] == "sheet-id"
    assert kwargs["body"]["valueInputOption"] == "RAW"
    assert kwargs["body"]["data"] == [
        {"range": "'Receipts'!A2:L2", "values": [[formula, 5]]}
    ]
    assert {c[2] for c in calls} == {sheets.GOOGLE_NUM_RETRIES} == {5}


async def test_a_formula_typed_by_a_client_reaches_the_sheet_as_text(session):
    person = await _person(session, '=IMPORTXML("http://evil")‮')
    await _promise(session, person, description="=1+1")
    await session.commit()
    api = FakeSheetsAPI()

    await sheets_sync.sync(session, api, now=_now())

    [cells] = api.by_id("Tasks_Promises").values()
    assert cells[3] == '=IMPORTXML("http://evil")'  # bidi mark gone, text kept
    assert cells[4] == "=1+1"


async def test_disabled_is_noop(session, monkeypatch):
    person = await _person(session)
    await _promise(session, person)
    await session.commit()
    monkeypatch.setattr(settings, "sheets_enabled", False)
    api = FakeSheetsAPI()

    assert await sheets_sync.run(api=api) is None
    assert sheets.get_api() is None

    assert api.tab_calls == 0 and api.writes == [] and api.requests == []
    assert await _ledger(session) == []
    assert health.SHEETS_COMPONENT not in await health.beats(session)


async def test_a_missing_key_file_is_a_noop_that_reports(
    session, monkeypatch, enabled, tmp_path
):
    person = await _person(session)
    await _promise(session, person)
    await session.commit()
    monkeypatch.setattr(
        settings, "sheets_service_account_json", str(tmp_path / "missing.json")
    )

    assert sheets.get_api() is None
    result = await sheets_sync.run()

    assert result is not None and result.error == "key_file_missing"
    assert await _ledger(session) == []
    beat = (await health.beats(session))[health.SHEETS_COMPONENT]
    assert beat.detail["ok"] is False and beat.detail["error"] == "key_file_missing"

    monkeypatch.setattr(settings, "sheets_spreadsheet_id", " ")
    assert sheets.config_problem() == "no_spreadsheet_id"


async def test_an_unreadable_key_path_counts_as_missing(
    session, monkeypatch, enabled, tmp_path
):
    monkeypatch.setattr(
        settings, "sheets_service_account_json", str(tmp_path / "locked" / "key.json")
    )

    def denied(self):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "is_file", denied)
    assert sheets.config_problem() == "key_file_missing"
    assert sheets.get_api() is None

    result = await sheets_sync.run()

    assert result is not None and result.error == "key_file_missing"
    beat = (await health.beats(session))[health.SHEETS_COMPONENT]
    assert beat.detail["ok"] is False and beat.detail["error"] == "key_file_missing"


async def test_a_config_check_that_raises_still_beats_failing(
    session, monkeypatch, enabled
):
    def broken():
        raise OSError("stale file handle")

    monkeypatch.setattr(sheets, "config_problem", broken)

    result = await sheets_sync.run()

    assert result is not None and result.error == "OSError"
    beat = (await health.beats(session))[health.SHEETS_COMPONENT]
    assert beat.detail["ok"] is False and beat.detail["error"] == "OSError"


async def test_a_quiet_run_inside_the_backoff_keeps_the_mirror_failing(
    session, monkeypatch, enabled
):
    # A long backoff: every run after the failure has nothing due.
    monkeypatch.setattr(settings, "sheets_sync_seconds", 900)
    person = await _person(session)
    await _promise(session, person)
    await session.commit()
    api = FakeSheetsAPI()
    t0 = _now()
    assert (await sheets_sync.run(api=api, now=t0)).ok

    await session.execute(sa.update(m.Promise).values(description="yangi matn"))
    await session.commit()
    api.fail_write = sheets.SheetsError("http_403")
    t1 = t0 + timedelta(minutes=1)
    await sheets_sync.run(api=api, now=t1)
    writes = len(api.writes)
    for minutes in (3, 6, 12):
        quiet = await sheets_sync.run(api=api, now=t1 + timedelta(minutes=minutes))
        assert quiet.error == "http_403"
    assert len(api.writes) == writes  # nothing was due

    t2 = t1 + timedelta(minutes=12)
    detail = (await health.beats(session))[health.SHEETS_COMPONENT].detail
    assert detail["ok"] is False and detail["error"] == "http_403"
    assert detail["last_ok_at"] == t0.isoformat()
    assert detail["failing_since"] == t1.isoformat()
    status = await health.gather(session, now=t2)
    assert "sheets_failing" in [p.key for p in health.problems(status)]

    # Once the row goes out the mirror is healthy again.
    t3 = t1 + timedelta(minutes=16)
    assert (await sheets_sync.run(api=api, now=t3)).ok
    detail = (await health.beats(session))[health.SHEETS_COMPONENT].detail
    assert detail["ok"] is True and detail["failing_since"] is None


async def test_run_beats_the_last_success_and_the_start_of_a_failure(session, enabled):
    person = await _person(session)
    promise = await _promise(session, person)
    await session.commit()
    api = FakeSheetsAPI()
    t0 = _now()

    assert (await sheets_sync.run(api=api, now=t0)).ok
    detail = (await health.beats(session))[health.SHEETS_COMPONENT].detail
    assert detail["ok"] is True and detail["last_ok_at"] == t0.isoformat()
    assert detail["failing_since"] is None and detail["written"] == 2

    promise.description = "yangi matn"
    await session.commit()
    api.fail_write = sheets.SheetsError("http_403")
    t1 = t0 + timedelta(minutes=1)
    await sheets_sync.run(api=api, now=t1)
    t2 = t0 + timedelta(minutes=12)
    api.fail_write = sheets.SheetsError("http_403")
    await sheets_sync.run(api=api, now=t2)

    rows = await health.beats(session)
    detail = rows[health.SHEETS_COMPONENT].detail
    assert detail["ok"] is False and detail["error"] == "http_403"
    assert detail["last_ok_at"] == t0.isoformat()
    assert detail["failing_since"] == t1.isoformat()
    status = await health.gather(session, now=t2)
    assert [p.key for p in health.problems(status) if p.key == "sheets_failing"] == [
        "sheets_failing"
    ]
    report = replies.status_report(status, [])
    assert "📊 Sheets: oxirgi yangilanish 12 daqiqa oldin" in report


async def test_receipt_said_and_bank_columns_separate(session):
    person = await _person(session)
    txn = m.Transaction(
        type=TransactionType.income,
        amount=Decimal("1250000.00"),
        currency=Currency.UZS,
        occurred_at=datetime(2026, 10, 20, 14, 9, tzinfo=TZ),
        channel="sms:KAPITALBANK",
    )
    session.add(txn)
    await session.flush()
    confirmed = await _receipt(
        session, person, verdict="confirmed", transaction_id=txn.id
    )
    said_only = await _receipt(
        session,
        person,
        amount=Decimal("300.50"),
        currency=Currency.USD,
        verdict="unconfirmed",
        reference="TX-1",
    )
    unreadable = await _receipt(
        session, person, amount=None, currency=None, state="needs_review"
    )
    rejected = await _receipt(session, person, state="rejected")
    await session.commit()
    api = FakeSheetsAPI()

    await sheets_sync.sync(session, api, now=_now())

    rows = api.by_id("Receipts")
    assert f"r{rejected.id}" not in rows
    assert rows[f"r{confirmed.id}"] == [
        f"r{confirmed.id}",
        "2026-10-20 14:03",
        "1250000.00",
        "UZS",
        "AKMAL R.",
        "BEKZOD Q. ****4321",
        "Payme",
        "TX-998877",
        "Akmal",
        "Telegram",
        "✅ bankda tasdiqlandi",
        f"x{txn.id} · 1250000.00 UZS · 2026-10-20 14:09 · KAPITALBANK",
    ]
    said = rows[f"r{said_only.id}"]
    # What the image says stays in its own columns, its currency beside it;
    # with no bank row the bank column is empty, never the receipt's figure.
    assert said[2:4] == ["300.50", "USD"]
    assert said[10] == "⏳ bankdan tasdiq yo'q" and said[11] == ""
    blank = rows[f"r{unreadable.id}"]
    assert blank[2:4] == ["", ""] and blank[10] == "🧾 chek o'qilmadi"


async def test_a_bot_receipt_names_the_owner_and_the_bot(session):
    person = await _person(session)
    receipt = await _receipt(session, person, person_id=None)
    interaction = await session.get(m.Interaction, receipt.interaction_id)
    interaction.source = InteractionSource.receipt_photo
    interaction.tg_chat_id = None
    interaction.person_id = None
    await session.commit()
    api = FakeSheetsAPI()

    await sheets_sync.sync(session, api, now=_now())

    assert api.by_id("Receipts")[f"r{receipt.id}"][8:10] == ["siz", "MIYA bot"]


async def test_a_row_that_fails_to_render_does_not_stop_the_rest(
    session, monkeypatch, caplog
):
    person = await _person(session)
    bad = await _promise(session, person)
    good = await _promise(session, person, description="boshqa")
    await session.commit()
    api = FakeSheetsAPI()
    await sheets_sync.sync(session, api, now=_now())
    bad_id = bad.id

    real = sheets_sync.promise_cells

    def flaky(promise, name, source):
        if promise.id == bad_id:
            raise ValueError("bad row")
        return real(promise, name, source)

    monkeypatch.setattr(sheets_sync, "promise_cells", flaky)
    good.description = "boshqa matn"
    await session.commit()
    with caplog.at_level(logging.ERROR, logger="miya.services.sheets_sync"):
        result = await sheets_sync.sync(session, api, now=_now())

    assert result.ok and result.written == 1
    assert api.by_id("Tasks_Promises")[f"p{good.id}"][4] == "boshqa matn"
    # The row it could not render is left as it was, never cleared.
    assert f"p{bad_id}" in api.by_id("Tasks_Promises")
    record = next(r for r in caplog.records if "not rendered" in r.getMessage())
    assert record.entity_kind == "promise" and record.entity_id == bad_id
    assert "bad row" not in caplog.text


async def test_a_tab_that_cannot_be_built_is_left_alone(session, monkeypatch):
    person = await _person(session)
    await _promise(session, person)
    await _receipt(session, person)
    await session.commit()
    api = FakeSheetsAPI()
    await sheets_sync.sync(session, api, now=_now())

    async def broken(session, now):
        await session.execute(sa.text("SELECT 1/0"))
        raise AssertionError("unreachable")

    monkeypatch.setitem(sheets_sync.BUILDERS, "Receipts", broken)
    await session.execute(sa.update(m.Receipt).values(issuer="Click"))
    await session.commit()
    result = await sheets_sync.sync(session, api, now=_now())

    # The other tabs still went out; the mirror reports itself unhealthy.
    assert result.error == "plan_failed"
    states = {r.tab: r.state for r in await _ledger(session)}
    assert states["Receipts"] == "synced"  # not tombstoned
    assert len(api.rows("Receipts")) == 1


def test_error_codes():
    from googleapiclient.errors import HttpError

    class _Resp(dict):
        status = 403
        reason = "Forbidden"

    assert sheets.error_code(HttpError(_Resp(), b"{}")) == "http_403"
    assert sheets.error_code(TimeoutError()) == "timeout"
    assert sheets.error_code(sheets.SheetsError("key_file_missing")) == "key_file_missing"
    assert sheets.error_code(ConnectionResetError()) == "network"
    assert sheets.error_code(ValueError("secret text")) == "ValueError"
    assert len(sheets.error_code(sheets.SheetsError("x" * 100))) == 64


def test_retry_waits_grow_and_stop_at_the_cap():
    now = _now()
    base = timedelta(seconds=settings.sheets_sync_seconds)
    assert sheets_sync.retry_at(1, now) == now + base
    assert sheets_sync.retry_at(2, now) == now + 2 * base
    assert sheets_sync.retry_at(1000, now) == now + sheets_sync.RETRY_CAP


def test_the_holat_line_only_when_enabled(monkeypatch):
    now = _now()
    row = m.Heartbeat(
        component="sheets",
        last_seen_at=now,
        detail={"ok": True, "last_ok_at": (now - timedelta(minutes=3)).isoformat()},
    )
    sheets_beat = health.component_of("sheets", row, now=now)
    status = health.Status.unreachable(now)
    status.db_ok = True
    status.sheets = sheets_beat
    monkeypatch.setattr(settings, "sheets_enabled", False)
    assert replies._sheets_line(status) is None
    monkeypatch.setattr(settings, "sheets_enabled", True)
    assert replies._sheets_line(status) == "📊 Sheets: oxirgi yangilanish 3 daqiqa oldin"
    status.sheets = None
    assert replies._sheets_line(status) == "📊 Sheets: oxirgi yangilanish hali yo'q"


def test_the_migration_freezes_the_model_lists():
    path = Path(__file__).parents[1] / "alembic" / "versions" / "0024_sheet_rows.py"
    spec = importlib.util.spec_from_file_location("migration_0024", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "0023_receipts"
    assert len(migration.revision) <= 32
    assert migration.TABS == m.SHEET_TABS
    assert migration.ENTITY_KINDS == m.SHEET_ENTITY_KINDS
    assert migration.STATES == m.SHEET_ROW_STATES
    assert tuple(sheets_sync.HEADERS) == m.SHEET_TABS


async def test_the_checks_hold_in_the_database(session):
    session.add(
        m.SheetRow(tab="Other", entity_kind="promise", entity_id=1, content_hash="0" * 64)
    )
    with pytest.raises(sa.exc.IntegrityError):
        await session.flush()
    await session.rollback()
    session.add_all(
        [
            m.SheetRow(
                tab="Receipts",
                entity_kind="receipt",
                entity_id=i,
                row_number=2,
                content_hash="0" * 64,
            )
            for i in (1, 2)
        ]
    )
    with pytest.raises(sa.exc.IntegrityError):
        await session.flush()
    await session.rollback()


def test_the_job_is_registered_every_sync_interval():
    from miya.worker import main as worker

    source = Path(worker.__file__).read_text()
    start = source.index("        sheets_sync_job,\n")
    block = source[start : source.index("scheduler.add_job", start)]
    for needle in (
        "IntervalTrigger(seconds=settings.sheets_sync_seconds)",
        'id="sheets_sync"',
        "max_instances=1",
        "coalesce=True",
    ):
        assert needle in block, needle


async def test_the_worker_job_is_a_noop_while_disabled(session, monkeypatch):
    from miya.worker import main as worker

    monkeypatch.setattr(settings, "sheets_enabled", False)
    person = await _person(session)
    await _promise(session, person)
    await session.commit()
    await worker.sheets_sync_job()
    assert await _ledger(session) == []

"""The Google Sheets client behind the one-way mirror (JV-07).

The sync logic (``sheets_sync``) talks to a tiny :class:`SheetsAPI`, like
``gcal.CalendarAPI``, so tests swap in a fake and never touch the network.
:class:`GoogleSheetsAPI` is the real one: a service account (the
``spreadsheets`` scope only) whose e-mail address the owner shares the
spreadsheet with as Editor. The blocking client runs in a thread, every call
uses the client's own retry on 429 and 5xx (``execute(num_retries=...)``,
JV-02), and values are always written ``RAW``, so a counterparty's name that
starts with ``=`` stays text and never becomes a formula.

Nothing here reads cell data back: the only read is the spreadsheet's tab
list and sizes, so missing tabs can be created and the grid grown.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from miya.config import settings

log = logging.getLogger(__name__)

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
# The Google client's own exponential backoff on 429 and 5xx (JV-02).
GOOGLE_NUM_RETRIES = 5
# One request may not hang the sync job: the socket gives up after this.
HTTP_TIMEOUT_SECONDS = 60
# Values are written as given, never parsed as formulas or dates.
VALUE_INPUT_OPTION = "RAW"
# Error codes are stored in a String(64) column.
ERROR_CODE_MAX = 64

# Codes for a mirror that cannot even start; health.py words them.
KEY_FILE_MISSING = "key_file_missing"
BAD_KEY_FILE = "bad_key_file"
NO_SPREADSHEET_ID = "no_spreadsheet_id"

# A cell: text, or a whole number (counts). Money is always text.
Cell = str | int


class SheetsError(Exception):
    """A failure the sync records by its code alone."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class TabInfo:
    """One tab of the spreadsheet: its id and its grid size."""

    sheet_id: int
    row_count: int
    column_count: int


@dataclass(frozen=True, slots=True)
class CellRange:
    """Values for an explicit A1 range, e.g. ``'Receipts'!A5:L5``."""

    range: str
    values: list[list[Cell]]


class SheetsAPI(ABC):
    # Set once the tabs and headers were seen in place in this process; until
    # then every sync looks, even with nothing to write.
    layout_checked: bool = False

    @abstractmethod
    async def tabs(self) -> dict[str, TabInfo]:
        """The spreadsheet's tabs by title (``spreadsheets.get``)."""

    @abstractmethod
    async def batch_update(self, requests: list[dict[str, Any]]) -> None:
        """Structural changes in one ``spreadsheets.batchUpdate``."""

    @abstractmethod
    async def write_values(self, data: list[CellRange]) -> None:
        """Every range in one ``values.batchUpdate``, written RAW."""


def error_code(exc: BaseException) -> str:
    """What a failure is called in ``sheet_rows.last_error`` and the health
    beat: an HTTP status, a timeout, an auth refusal; never the message,
    which can quote what was sent."""
    if isinstance(exc, SheetsError):
        code = exc.code
    elif isinstance(exc, TimeoutError):
        code = "timeout"
    else:
        names = {cls.__name__ for cls in type(exc).__mro__}
        status = getattr(exc, "status_code", None)
        if "HttpError" in names and isinstance(status, int):
            code = f"http_{status}"
        elif "RefreshError" in names:
            code = "auth"
        elif isinstance(exc, ConnectionError) or "TransportError" in names:
            code = "network"
        else:
            code = type(exc).__name__
    return code[:ERROR_CODE_MAX]


class GoogleSheetsAPI(SheetsAPI):
    """google-api-python-client wrapper; every call goes through a thread.

    ``service`` is a ready ``build("sheets", "v4")`` resource, for tests; by
    default it is built on first use from the service-account key file.
    """

    def __init__(
        self,
        spreadsheet_id: str | None = None,
        key_file: str | None = None,
        *,
        service: Any = None,
    ) -> None:
        self._spreadsheet_id = spreadsheet_id or settings.sheets_spreadsheet_id
        self._key_file = key_file or settings.sheets_service_account_json
        self._service: Any = service

    def _get_service(self) -> Any:
        if self._service is None:
            # Deferred imports: the Google client stack is only needed when
            # the mirror is switched on.
            import google_auth_httplib2  # type: ignore[import-untyped]
            import httplib2  # type: ignore[import-untyped]
            from google.oauth2 import service_account
            from googleapiclient.discovery import build  # type: ignore[import-untyped]

            load = service_account.Credentials.from_service_account_file
            try:
                creds = load(self._key_file, scopes=SHEETS_SCOPES)  # type: ignore[no-untyped-call]
            except FileNotFoundError as exc:
                raise SheetsError(KEY_FILE_MISSING) from exc
            except (ValueError, KeyError) as exc:
                raise SheetsError(BAD_KEY_FILE) from exc
            http = google_auth_httplib2.AuthorizedHttp(
                creds, http=httplib2.Http(timeout=HTTP_TIMEOUT_SECONDS)
            )
            self._service = build("sheets", "v4", http=http, cache_discovery=False)
        return self._service

    def _tabs(self) -> dict[str, TabInfo]:
        response = (
            self._get_service()
            .spreadsheets()
            .get(
                spreadsheetId=self._spreadsheet_id,
                fields="sheets.properties(sheetId,title,gridProperties)",
            )
            .execute(num_retries=GOOGLE_NUM_RETRIES)
        )
        found: dict[str, TabInfo] = {}
        for sheet in response.get("sheets", []):
            props = sheet.get("properties") or {}
            grid = props.get("gridProperties") or {}
            title = props.get("title")
            if isinstance(title, str):
                found[title] = TabInfo(
                    sheet_id=int(props.get("sheetId", 0)),
                    row_count=int(grid.get("rowCount", 0)),
                    column_count=int(grid.get("columnCount", 0)),
                )
        return found

    def _batch_update(self, requests: list[dict[str, Any]]) -> None:
        (
            self._get_service()
            .spreadsheets()
            .batchUpdate(spreadsheetId=self._spreadsheet_id, body={"requests": requests})
            .execute(num_retries=GOOGLE_NUM_RETRIES)
        )

    def _write_values(self, data: list[CellRange]) -> None:
        body = {
            "valueInputOption": VALUE_INPUT_OPTION,
            "data": [{"range": item.range, "values": item.values} for item in data],
        }
        (
            self._get_service()
            .spreadsheets()
            .values()
            .batchUpdate(spreadsheetId=self._spreadsheet_id, body=body)
            .execute(num_retries=GOOGLE_NUM_RETRIES)
        )

    async def tabs(self) -> dict[str, TabInfo]:
        return await asyncio.to_thread(self._tabs)

    async def batch_update(self, requests: list[dict[str, Any]]) -> None:
        await asyncio.to_thread(self._batch_update, requests)

    async def write_values(self, data: list[CellRange]) -> None:
        await asyncio.to_thread(self._write_values, data)


def config_problem() -> str | None:
    """Why the mirror cannot run as configured, as an error code; None when
    the spreadsheet id is set and the key file exists. A key file the worker
    may not even look at (a directory it cannot enter) counts as missing."""
    if not settings.sheets_spreadsheet_id.strip():
        return NO_SPREADSHEET_ID
    try:
        present = Path(settings.sheets_service_account_json).is_file()
    except OSError:
        present = False
    if not present:
        return KEY_FILE_MISSING
    return None


# One client per spreadsheet and key file for the life of the process, so the
# service is built and the layout checked once, not every minute.
_clients: dict[tuple[str, str], GoogleSheetsAPI] = {}


def get_api() -> GoogleSheetsAPI | None:
    """The real client, or None while the mirror is off or not configured."""
    if not settings.sheets_enabled or config_problem() is not None:
        return None
    key = (settings.sheets_spreadsheet_id, settings.sheets_service_account_json)
    client = _clients.get(key)
    if client is None:
        client = _clients[key] = GoogleSheetsAPI(*key)
    return client


__all__ = [
    "BAD_KEY_FILE",
    "GOOGLE_NUM_RETRIES",
    "KEY_FILE_MISSING",
    "NO_SPREADSHEET_ID",
    "SHEETS_SCOPES",
    "VALUE_INPUT_OPTION",
    "Cell",
    "CellRange",
    "GoogleSheetsAPI",
    "SheetsAPI",
    "SheetsError",
    "TabInfo",
    "config_problem",
    "error_code",
    "get_api",
]

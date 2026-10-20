"""Log lines for every MIYA process (JV-02).

``setup_logging(component)`` is the one place a process configures logging.
With ``LOG_FORMAT=json`` each record becomes one JSON object on one line,
carrying ``ts``, ``level``, ``logger``, ``component``, ``msg`` and the
record's extras (ids and counts); ``LOG_FORMAT=text`` keeps the plain
``time level logger: message`` lines.

Logs carry ids and counts, never message, SMS, notification or draft bodies.
The redaction filter is the backstop for a careless ``extra=``: an extra named
like a body is dropped before any formatter sees it, in both formats.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, Literal, TextIO

from miya.config import settings

LogFormat = Literal["json", "text"]

TEXT_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

# Extras that would carry what someone wrote; never written to a log line.
REDACTED_EXTRAS = frozenset({"text", "body", "raw_text", "transcript", "draft"})

# Attributes every LogRecord has; anything else on a record came from extra=.
_RECORD_ATTRS = frozenset(
    {
        *logging.LogRecord("", logging.INFO, "", 0, "", None, None).__dict__,
        "message",
        "asctime",
        # uvicorn's coloured copy of the message; msg already carries it.
        "color_message",
    }
)


class RedactBodies(logging.Filter):
    """Drop body-shaped extras from the record before it is formatted."""

    def filter(self, record: logging.LogRecord) -> bool:
        for name in REDACTED_EXTRAS:
            record.__dict__.pop(name, None)
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per record; extras that are not JSON become strings."""

    def __init__(self, component: str) -> None:
        super().__init__()
        self.component = component

    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "component": self.component,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if (
                key in _RECORD_ATTRS
                or key in REDACTED_EXTRAS
                or key in line
                or key.startswith("_")
            ):
                continue
            line[key] = value
        if record.exc_info:
            line["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            line["stack"] = self.formatStack(record.stack_info)
        return json.dumps(line, ensure_ascii=False, default=str)


class MiyaHandler(logging.StreamHandler[TextIO]):
    """The handler ``setup_logging`` installs; a second call replaces it."""


def setup_logging(
    component: str,
    *,
    fmt: LogFormat | None = None,
    stream: TextIO | None = None,
    force: bool = False,
) -> MiyaHandler | None:
    """Configure the root logger for one process.

    Like ``logging.basicConfig`` it leaves logging alone when something else
    already put a handler on the root logger (pytest's capture, an embedding
    server), unless ``force`` is set; an earlier MIYA handler is replaced, so
    calling it twice is harmless. Other handlers are never removed.
    Returns the installed handler, or None when nothing was changed.
    """
    root = logging.getLogger()
    if not force and any(not isinstance(h, MiyaHandler) for h in root.handlers):
        return None
    for old in [h for h in root.handlers if isinstance(h, MiyaHandler)]:
        root.removeHandler(old)
        old.close()
    handler = MiyaHandler(stream if stream is not None else sys.stderr)
    handler.addFilter(RedactBodies())
    chosen = fmt if fmt is not None else settings.log_format
    if chosen == "json":
        handler.setFormatter(JsonFormatter(component))
    else:
        handler.setFormatter(logging.Formatter(TEXT_FORMAT))
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())
    return handler

"""JV-02: one logging setup for every process, and the rules new code meets.

No database. The logging tests install the MIYA handler on a StringIO and
take it off again, so pytest's own capture (caplog) is never disturbed.
"""

from __future__ import annotations

import ast
import io
import json
import logging
import re
import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from miya import logs
from miya.config import Settings

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "miya"


@pytest.fixture
def install():
    """setup_logging onto a StringIO; the root logger is restored afterwards."""
    root = logging.getLogger()
    level = root.level
    installed: list[logging.Handler] = []

    def _install(component: str = "worker", fmt: logs.LogFormat = "json") -> io.StringIO:
        stream = io.StringIO()
        handler = logs.setup_logging(component, fmt=fmt, stream=stream, force=True)
        assert handler is not None
        installed.append(handler)
        return stream

    yield _install
    for handler in root.handlers[:]:
        if isinstance(handler, logs.MiyaHandler):
            root.removeHandler(handler)
    root.setLevel(level)


def _lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_json_line_per_record(install):
    stream = install("worker")
    log = logging.getLogger("miya.worker.main")

    log.info("swept %d chats", 3, extra={"chat_id": 42, "count": 3})
    log.warning("second line")

    first, second = _lines(stream)
    assert first["level"] == "INFO"
    assert first["logger"] == "miya.worker.main"
    assert first["component"] == "worker"
    assert first["msg"] == "swept 3 chats"
    assert first["chat_id"] == 42 and first["count"] == 3
    assert first["ts"].endswith("+00:00")
    assert {"ts", "level", "logger", "component", "msg"} <= set(second)
    assert second["msg"] == "second line"


def test_json_keeps_an_exception_on_one_line(install):
    stream = install("api")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logging.getLogger("miya.api.main").exception("failed", extra={"outbox_id": 7})

    (line,) = _lines(stream)
    assert line["outbox_id"] == 7
    assert "RuntimeError: boom" in line["exc"]
    assert stream.getvalue().count("\n") == 1


def test_json_turns_odd_extras_into_strings(install):
    stream = install()
    logging.getLogger("miya.x").info("odd", extra={"path": Path("/data/a.ogg")})
    (line,) = _lines(stream)
    assert line["path"] == "/data/a.ogg"


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_redaction_drops_body_extras(install, fmt):
    stream = install("userbot", fmt)
    bodies = {name: f"secret-{name}" for name in logs.REDACTED_EXTRAS}

    logging.getLogger("miya.userbot.main").warning(
        "ingested", extra={**bodies, "interaction_id": 9}
    )

    out = stream.getvalue()
    assert "secret-" not in out
    if fmt == "json":
        (line,) = _lines(stream)
        assert line["interaction_id"] == 9
        assert not set(line) & logs.REDACTED_EXTRAS


def test_the_installed_handler_strips_body_extras_from_the_record(install):
    install("userbot")
    handler = next(
        h for h in logging.getLogger().handlers if isinstance(h, logs.MiyaHandler)
    )
    assert any(isinstance(f, logs.RedactBodies) for f in handler.filters)

    record = logging.LogRecord("miya.x", logging.INFO, "", 0, "m", None, None)
    for name in logs.REDACTED_EXTRAS:
        setattr(record, name, f"secret-{name}")
    record.interaction_id = 9

    assert handler.filter(record)
    assert not set(record.__dict__) & logs.REDACTED_EXTRAS
    assert record.interaction_id == 9


def test_redacted_names_are_the_plans():
    assert {"text", "body", "raw_text", "transcript", "draft"} == logs.REDACTED_EXTRAS


def test_text_format_keeps_the_current_lines(install):
    stream = install("bot", "text")
    logging.getLogger("miya.bot.main").warning("hello %s", "there")
    line = stream.getvalue().strip()
    assert re.fullmatch(
        r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} WARNING  miya\.bot\.main: hello there",
        line,
    )


def test_a_second_setup_replaces_the_first(install):
    first = install("api")
    second = install("api")
    logging.getLogger("miya.api.main").warning("once")

    ours = [h for h in logging.getLogger().handlers if isinstance(h, logs.MiyaHandler)]
    assert len(ours) == 1
    assert first.getvalue() == ""
    assert len(_lines(second)) == 1


def test_setup_leaves_logging_alone_when_someone_configured_it(caplog):
    # pytest's capture handler is on the root logger, as uvicorn's or a test
    # runner's would be: like basicConfig, setup_logging changes nothing.
    assert logging.getLogger().handlers
    assert logs.setup_logging("worker") is None
    assert not any(isinstance(h, logs.MiyaHandler) for h in logging.getLogger().handlers)
    logging.getLogger("miya.x").warning("still captured")
    assert "still captured" in caplog.text


def test_caplog_still_sees_records_with_the_handler_installed(install, caplog):
    install()
    with caplog.at_level(logging.WARNING):
        logging.getLogger("miya.x").warning("seen", extra={"chat_id": 1})
    assert [r.getMessage() for r in caplog.records] == ["seen"]


def test_setup_uses_log_level_and_log_format(install, monkeypatch):
    from miya.config import settings

    monkeypatch.setattr(settings, "log_level", "warning")
    monkeypatch.setattr(settings, "log_format", "json")
    stream = io.StringIO()
    handler = logs.setup_logging("worker", stream=stream, force=True)
    try:
        assert logging.getLogger().level == logging.WARNING
        assert isinstance(handler.formatter, logs.JsonFormatter)
    finally:
        logging.getLogger().removeHandler(handler)


def test_log_format_setting():
    assert Settings(_env_file=None).log_format == "json"
    assert Settings(_env_file=None, log_format="text").log_format == "text"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, log_format="xml")


# --- code rules ----------------------------------------------------------------


def _python_sources(base: Path) -> list[Path]:
    return sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)


def _calls(tree: ast.AST, name: str) -> list[ast.Call]:
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if called == name:
            found.append(node)
    return found


def test_no_basicconfig_outside_logs():
    offenders = []
    for path in _python_sources(PACKAGE):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders += [
            f"{path.relative_to(ROOT)}:{call.lineno}"
            for call in _calls(tree, "basicConfig")
            if path != PACKAGE / "logs.py"
        ]
    assert offenders == []


ENTRY_POINTS = {
    "worker/main.py": "worker",
    "api/main.py": "api",
    "bot/main.py": "bot",
    "userbot/main.py": "userbot",
    "tools/backfill.py": "backfill",
}


@pytest.mark.parametrize(("relative", "component"), sorted(ENTRY_POINTS.items()))
def test_each_process_sets_up_logging_with_its_name(relative, component):
    tree = ast.parse((PACKAGE / relative).read_text(encoding="utf-8"))
    names = [
        call.args[0].value
        for call in _calls(tree, "setup_logging")
        if call.args and isinstance(call.args[0], ast.Constant)
    ]
    assert names and set(names) == {component}


# Rule 26: new code has no placeholder markers and no stub bodies. These are
# the services modules that existed before the Jarvis plan; every other module
# there is new code and is held to the rule.
LEGACY_SERVICES = frozenset(
    {
        "__init__.py",
        "amounts.py",
        "approvals.py",
        "audio.py",
        "backup.py",
        "batch.py",
        "brief.py",
        "call_recordings.py",
        "chats.py",
        "claims.py",
        "client_import.py",
        "codes.py",
        "documents.py",
        "embeddings.py",
        "extraction.py",
        "gcal.py",
        "health.py",
        "ingest.py",
        "loops.py",
        "media_policy.py",
        "memories.py",
        "money_events.py",
        "money_notices.py",
        "nudges.py",
        "owner_address.py",
        "passages.py",
        "people.py",
        "persistence.py",
        "phone_events.py",
        "planner.py",
        "profiles.py",
        "prompts.py",
        "purge.py",
        "queries.py",
        "question_replies.py",
        "questions.py",
        "rag.py",
        "recall.py",
        "recaps.py",
        "records.py",
        "reminders.py",
        "reports.py",
        "restore_drill.py",
        "sms_money.py",
        "text.py",
        "transcription.py",
        "usage.py",
        "vision.py",
        "windows.py",
    }
)

# Split so this file does not match its own scan.
_MARKERS = re.compile(
    r"\b(?:" + "|".join(("TO" + "DO", "FIX" + "ME", "X" + "XX")) + r")\b"
)
_NOT_IMPLEMENTED = re.compile(r"not\s+implemented", re.IGNORECASE)


def new_code_paths() -> list[Path]:
    """The files rule 26 names, as far as they exist today."""
    paths = _python_sources(PACKAGE / "jarvis") if (PACKAGE / "jarvis").is_dir() else []
    paths.append(PACKAGE / "logs.py")
    paths += [
        p
        for p in sorted((PACKAGE / "services").glob("*.py"))
        if p.name not in LEGACY_SERVICES
    ]
    sidecar = ROOT / "whatsapp" / "src"
    if sidecar.is_dir():
        paths += sorted(
            p
            for p in sidecar.rglob("*")
            if p.is_file() and p.suffix in {".js", ".mjs", ".cjs", ".ts", ".mts"}
        )
    return paths


def _decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names = set()
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute):
            names.add(target.attr)
        elif isinstance(target, ast.Name):
            names.add(target.id)
    return names


def _base_names(node: ast.ClassDef) -> set[str]:
    names = set()
    for base in node.bases:
        target = base.value if isinstance(base, ast.Subscript) else base
        if isinstance(target, ast.Attribute):
            names.add(target.attr)
        elif isinstance(target, ast.Name):
            names.add(target.id)
    return names


def _is_stub_statement(statement: ast.stmt) -> bool:
    if isinstance(statement, ast.Pass):
        return True
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and statement.value.value is Ellipsis
    )


def _raises_not_implemented(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Raise) and child.exc is not None:
            exc = child.exc.func if isinstance(child.exc, ast.Call) else child.exc
            if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
                return True
    return False


def python_placeholders(source: str) -> list[str]:
    """Stub bodies in Python source: a function that only passes, only says
    ``...``, has nothing but a docstring, or raises NotImplementedError, unless
    it is abstract (an abstractmethod, an overload or a Protocol member)."""
    problems: list[str] = []

    def visit(node: ast.AST, in_protocol: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, "Protocol" in _base_names(child))
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                abstract = in_protocol or bool(
                    _decorator_names(child) & {"abstractmethod", "overload"}
                )
                body = child.body
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    body = body[1:]
                stub = not body or all(_is_stub_statement(s) for s in body)
                if not abstract and (stub or _raises_not_implemented(child)):
                    problems.append(f"{child.name}:{child.lineno}")
                visit(child, False)
            else:
                visit(child, in_protocol)

    visit(ast.parse(source), False)
    return problems


def text_placeholders(source: str) -> list[int]:
    """Line numbers carrying a placeholder marker or a 'not implemented'."""
    return [
        number
        for number, line in enumerate(source.splitlines(), start=1)
        if _MARKERS.search(line) or _NOT_IMPLEMENTED.search(line)
    ]


def test_no_placeholders_in_new_code():
    paths = new_code_paths()
    assert PACKAGE / "services" / "retry.py" in paths
    assert PACKAGE / "services" / "safety.py" in paths
    problems = []
    for path in paths:
        source = path.read_text(encoding="utf-8")
        where = path.relative_to(ROOT).as_posix()
        problems += [f"{where}:{line}" for line in text_placeholders(source)]
        if path.suffix == ".py":
            problems += [f"{where} {stub}" for stub in python_placeholders(source)]
    assert problems == []


def test_the_placeholder_check_catches_stubs():
    marker = "TO" + "DO"
    source = f'''
from abc import ABC, abstractmethod
from typing import Protocol, overload


def passes():
    pass


def dots():
    ...


async def docstring_only():
    """Later."""


def raises():
    raise NotImplementedError("soon")


class Port(ABC):
    @abstractmethod
    def send(self) -> None: ...

    def real(self) -> int:
        return 1


class Reader(Protocol):
    def read(self) -> str: ...


@overload
def pick(x: int) -> int: ...
@overload
def pick(x: str) -> str: ...
def pick(x):
    return x  # {marker} tidy up
'''
    assert python_placeholders(source) == [
        "passes:6",
        "dots:10",
        "docstring_only:14",
        "raises:18",
    ]
    assert text_placeholders(source) == [
        source.splitlines().index(f"    return x  # {marker} tidy up") + 1
    ]
    assert text_placeholders("throw new Error('not implemented')") == [1]
    assert text_placeholders("const fixme_free = 1") == []


# Behaviour item 4: the modules held to mypy strict. A module that does not
# exist yet cannot be listed (mypy refuses a missing path), so each one must
# join `files` in the work package that adds it.
STRICT_MODULES = (
    "miya/jarvis",
    "miya/logs.py",
    "miya/services/authorship.py",
    "miya/services/retry.py",
    "miya/services/safety.py",
    "miya/services/when.py",
)


def test_strict_modules_are_type_checked_once_they_exist():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    files = config["tool"]["mypy"]["files"]
    missing = [p for p in STRICT_MODULES if (ROOT / p).exists() and p not in files]
    assert missing == []


def test_the_api_lets_uvicorn_log_through_the_root_handler(monkeypatch):
    import runpy
    import sys

    import uvicorn

    from miya.config import settings

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: calls.append(k))
    monkeypatch.setattr(settings, "api_bearer_token", "ab" * 32)
    monkeypatch.setattr(settings, "upload_tokens", "")
    monkeypatch.delitem(sys.modules, "miya.api.__main__", raising=False)
    runpy.run_module("miya.api", run_name="__main__")
    assert len(calls) == 1
    assert "log_config" in calls[0] and calls[0]["log_config"] is None


def test_no_module_defines_a_top_level_name_twice():
    """A second ``def`` with the same name silently replaces the first for
    every caller in the module (JV-18 once shadowed the receipt card's date
    helper this way); ruff's F811 only catches an unused first definition."""
    root = Path(__file__).resolve().parents[1] / "miya"
    duplicates = []
    for path in sorted(root.rglob("*.py")):
        seen: dict[str, int] = {}
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                if node.name in seen:
                    duplicates.append(
                        f"{path.relative_to(root.parent)}:{node.lineno} {node.name} "
                        f"(first at {seen[node.name]})"
                    )
                seen.setdefault(node.name, node.lineno)
    assert duplicates == []

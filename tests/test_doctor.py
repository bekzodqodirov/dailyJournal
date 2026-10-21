"""`make doctor`: the preflight check of .env and the server (WP-06). No database."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from dotenv import dotenv_values

from miya.tools import doctor

ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = ROOT / ".env.example"
GIB_KB = 1024 * 1024  # kB in a GiB
GOOD_MEM = f"MemTotal: {int(7.7 * GIB_KB)} kB\nSwapTotal: {4 * GIB_KB} kB\n"
DISK = 60 * 1000**3
PASSWORD = "p" * 48


def _env(**overrides: str) -> dict[str, str]:
    """A complete, correct .env: every expected key, every required value."""
    env = {key: "" for key in doctor.EXPECTED_ENV_KEYS}
    env.update(
        POSTGRES_PASSWORD=PASSWORD,
        DATABASE_URL=f"postgresql+psycopg://miya:{PASSWORD}@db:5432/miya",
        ANTHROPIC_API_KEY="sk-test",
        ELEVENLABS_API_KEY="el-test",
        ASSISTANT_BOT_TOKEN="123:abc",
        OWNER_TELEGRAM_ID="123456789",
        USERBOT_ENABLED="true",
        TELETHON_API_ID="12345",
        TELETHON_API_HASH="abcdef",
        TELETHON_SESSION="session",
        OWNER_ALIASES="Bekzod, Bekzod aka",
        API_BEARER_TOKEN="a" * 64,
        UPLOAD_TOKENS="phone:" + "b" * 64,
        BACKUP_AGE_RECIPIENT="age1" + "x" * 58,
    )
    env.update(overrides)
    return env


def _check(env=None, mem=GOOD_MEM, disk=DISK, key_file=True) -> list[doctor.Finding]:
    return doctor.check(env if env is not None else _env(), mem, disk, key_file)


def _texts(findings, level=None) -> list[str]:
    return [f.text for f in findings if level is None or f.level == level]


def test_expected_keys_match_env_example():
    assert set(doctor.EXPECTED_ENV_KEYS) == set(dotenv_values(ENV_EXAMPLE))


def test_a_complete_env_on_a_good_server_is_clean():
    findings = _check()
    assert findings == []
    assert doctor.exit_code(findings) == 0
    assert doctor.summary(findings) == "✅ Hammasi joyida — endi: make up"


def test_a_missing_key_is_a_warning_naming_it():
    env = _env()
    del env["QUIET_HOURS"]
    findings = _check(env)
    assert _texts(findings, "warn") == [
        "⚠️ QUIET_HOURS .env faylida yo'q — .env.example'dan ko'chir."
    ]
    assert doctor.exit_code(findings) == 0


def test_a_comment_shaped_value_is_an_error_naming_the_key():
    findings = _check(_env(TELETHON_API_ID="# https://my.telegram.org"))
    [error] = _texts(findings, "error")
    assert error.startswith("❌ TELETHON_API_ID: qiymat izohga o'xshaydi")


@pytest.mark.parametrize(
    "key", sorted({**doctor.REQUIRED_HINTS, **doctor.TELETHON_HINTS})
)
def test_each_required_key_left_blank_is_an_error_with_its_hint(key):
    findings = _check(_env(**{key: ""}))
    hint = {**doctor.REQUIRED_HINTS, **doctor.TELETHON_HINTS}[key]
    assert f"❌ {key} bo'sh — to'ldir: {hint}" in _texts(findings, "error")
    assert doctor.exit_code(findings) == 1


def test_the_blank_backup_recipient_points_at_make_backup_key():
    errors = _texts(_check(_env(BACKUP_AGE_RECIPIENT="")), "error")
    assert any("make backup-key" in e for e in errors)


def test_the_api_token_length():
    short = _texts(_check(_env(API_BEARER_TOKEN="a" * 31)), "error")
    assert short == [
        "❌ API_BEARER_TOKEN juda qisqa (31 belgi) — kamida 32: openssl rand -hex 32"
    ]
    assert _check(_env(API_BEARER_TOKEN="f" * 64)) == []


def test_a_weak_or_reused_device_token_warns():
    weak = _texts(_check(_env(UPLOAD_TOKENS="phone:short")), "warn")
    assert len(weak) == 1 and "«phone»" in weak[0]
    reused = _texts(_check(_env(UPLOAD_TOKENS="phone:" + "a" * 64)), "warn")
    assert len(reused) == 1


def test_the_owner_id_must_be_digits():
    errors = _texts(_check(_env(OWNER_TELEGRAM_ID="@someone")), "error")
    assert errors == [
        "❌ OWNER_TELEGRAM_ID faqat raqam bo'lishi kerak (masalan 123456789)."
    ]


@pytest.mark.parametrize(
    ("recipient", "ok"),
    [("xyz", False), ("age1" + "q" * 58, True), ("ssh-ed25519 AAAAC3Nza", True)],
)
def test_the_backup_recipient_format(recipient, ok):
    errors = _texts(_check(_env(BACKUP_AGE_RECIPIENT=recipient)), "error")
    assert (errors == []) is ok


def test_blank_aliases_only_warn():
    findings = _check(_env(OWNER_ALIASES=""))
    assert _texts(findings, "error") == []
    assert len(_texts(findings, "warn")) == 1
    assert doctor.exit_code(findings) == 0


def test_a_small_server_is_an_error_and_missing_swap_warns():
    small = _check(mem=f"MemTotal: 3900000 kB\nSwapTotal: {2 * GIB_KB} kB\n")
    [error] = _texts(small, "error")
    assert "RAM bor; MIYA uchun kamida 8 GB kerak" in error
    no_swap = _check(mem=f"MemTotal: {int(7.7 * GIB_KB)} kB\nSwapTotal: 0 kB\n")
    assert _texts(no_swap, "error") == []
    assert _texts(no_swap, "warn") == [
        "⚠️ Swap yo'q — 2–4 GB swap qo'sh (docs/ornatish.md, 1-qadam)."
    ]


def test_a_small_disk_warns():
    assert len(_texts(_check(disk=10 * 1000**3), "warn")) == 1


def test_the_database_password():
    default = _texts(_check(_env(POSTGRES_PASSWORD="change-me")), "error")
    assert len(default) == 1 and "change-me" in default[0]
    mismatch = _texts(
        _check(_env(DATABASE_URL="postgresql+psycopg://miya:other@db:5432/miya")), "error"
    )
    assert mismatch == [
        "❌ DATABASE_URL'dagi parol POSTGRES_PASSWORD bilan bir xil emas."
    ]


def test_a_disabled_userbot_skips_the_telethon_checks():
    env = _env(USERBOT_ENABLED="false", TELETHON_API_ID="", TELETHON_API_HASH="")
    env["TELETHON_SESSION"] = ""
    assert _check(env) == []


def test_the_sheets_mirror_needs_its_spreadsheet_id_only_when_enabled():
    assert _check(_env(SHEETS_ENABLED="false")) == []
    errors = _texts(_check(_env(SHEETS_ENABLED="true")), "error")
    hint = doctor.SHEETS_HINTS["SHEETS_SPREADSHEET_ID"]
    assert errors == [f"❌ SHEETS_SPREADSHEET_ID bo'sh — to'ldir: {hint}"]
    assert _check(_env(SHEETS_ENABLED="true", SHEETS_SPREADSHEET_ID="1AbC")) == []


def test_a_missing_key_file_warns_only_when_a_recipient_is_set():
    assert len(_texts(_check(key_file=False), "warn")) == 1


def test_errors_come_first_and_the_exit_code_follows_them():
    findings = _check(_env(OWNER_ALIASES="", API_BEARER_TOKEN="short"))
    assert [f.level for f in findings] == ["error", "warn"]
    assert doctor.exit_code(findings) == 1
    assert doctor.summary(findings) == "❌ 1 ta muammo. Tuzatib, qaytadan: make doctor"


def test_the_module_runs_with_a_broken_env_because_it_never_loads_settings():
    env = {**os.environ, **_env(TELETHON_API_ID="# x")}
    run = subprocess.run(
        [sys.executable, "-m", "miya.tools.doctor"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert "TELETHON_API_ID: qiymat izohga o'xshaydi" in run.stdout
    assert "Traceback" not in run.stderr
    assert run.returncode == 1


# --- many missing keys, the Sheets key, the Jarvis switches -------------------------


def test_many_missing_keys_are_one_line():
    env = _env()
    for key in ("QUIET_HOURS", "JARVIS_MODE", "SHEETS_ENABLED", "LOG_FORMAT"):
        del env[key]
    [line] = _texts(_check(env), "warn")
    assert line.startswith("⚠️ .env faylida 4 ta yangi sozlama yo'q — ular standart")
    assert "QUIET_HOURS" in line and "LOG_FORMAT" in line


KEY_JSON = (
    b'{"type": "service_account", "project_id": "p", '
    b'"client_email": "miya@p.iam.gserviceaccount.com", "private_key": "x"}'
)


def _reader(files: dict[str, bytes | Exception]):
    def read(path: str) -> bytes:
        found = files.get(path, FileNotFoundError(path))
        if isinstance(found, Exception):
            raise found
        return found

    return read


def _sheets(files, **env) -> list[doctor.Finding]:
    full = _env(SHEETS_ENABLED="true", SHEETS_SPREADSHEET_ID="1AbC", **env)
    return doctor.check(full, GOOD_MEM, DISK, True, read_file=_reader(files))


def test_the_sheets_key_is_read_and_its_address_printed():
    findings = _sheets({doctor.SHEETS_KEY_DEFAULT: KEY_JSON})
    assert [(f.level, f.text) for f in findings] == [
        (
            "info",
            "ℹ️ Google jadvalni shu manzilga «Muharrir» qilib ulashing: "
            "miya@p.iam.gserviceaccount.com",
        )
    ]
    assert doctor.exit_code(findings) == 0
    assert doctor.summary(findings) == "✅ Hammasi joyida — endi: make up"
    # The configured path is the one read.
    other = _sheets(
        {"/app/secrets/k.json": KEY_JSON},
        SHEETS_SERVICE_ACCOUNT_JSON="/app/secrets/k.json",
    )
    assert [f.level for f in other] == ["info"]


def test_a_missing_or_wrong_sheets_key_is_an_error():
    [missing] = _texts(_sheets({}), "error")
    assert missing.startswith("❌ SHEETS_SERVICE_ACCOUNT_JSON: ")
    assert "secrets/sheets_service_account.json" in missing
    path = doctor.SHEETS_KEY_DEFAULT
    for raw in (b"not json", b'{"type": "authorized_user"}', b"[]"):
        [wrong] = _texts(_sheets({path: raw}), "error")
        assert "type=service_account" in wrong
    [no_email] = _texts(_sheets({path: b'{"type": "service_account"}'}), "error")
    assert "client_email" in no_email
    # Off: never read.
    assert _check(_env(SHEETS_ENABLED="false")) == []


def test_an_unreadable_sheets_key_warns_with_the_chown():
    """make up's init hands ./secrets to uid 10001 after the doctor: a
    root-owned key only warns, so the doctor does not block that fix."""
    findings = _sheets({doctor.SHEETS_KEY_DEFAULT: PermissionError("denied")})
    [warn] = _texts(findings, "warn")
    assert "sudo chown 10001:10001 secrets/sheets_service_account.json" in warn
    assert doctor.exit_code(findings) == 0


def test_the_real_reader_resolves_relative_paths(tmp_path, monkeypatch):
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "k.json").write_bytes(KEY_JSON)
    monkeypatch.setattr(doctor, "APP_DIR", tmp_path)
    assert doctor._read_app_file("./secrets/k.json") == KEY_JSON
    with pytest.raises(FileNotFoundError):
        doctor._read_app_file("./secrets/none.json")


def test_send_without_jarvis_warns():
    [warn] = _texts(_check(_env(TELEGRAM_SEND_ENABLED="true")), "warn")
    assert "TELEGRAM_SEND_ENABLED=true, lekin JARVIS_ENABLED=false" in warn
    assert "make reload" in warn


def test_jarvis_without_the_reader_warns():
    off = _texts(
        _check(
            _env(
                JARVIS_ENABLED="true",
                USERBOT_ENABLED="false",
                TELETHON_API_ID="",
                TELETHON_API_HASH="",
            )
        ),
        "warn",
    )
    assert off == [
        "⚠️ JARVIS_ENABLED=true, lekin USERBOT_ENABLED=false — mijoz xabarlari "
        "o'qilmaydi, Jarvis hech narsa qilmaydi. .env da USERBOT_ENABLED=true "
        "qil, keyin make reload."
    ]
    no_session = _texts(_check(_env(JARVIS_ENABLED="true", TELETHON_SESSION="")), "warn")
    assert any(
        "JARVIS_ENABLED=true, lekin TELETHON_SESSION bo'sh" in w for w in no_session
    )
    assert _check(_env(JARVIS_ENABLED="true", JARVIS_MODE="draft")) == []


def test_jarvis_mode_ceiling_notes():
    [auto] = _check(_env(JARVIS_ENABLED="true", JARVIS_MODE="auto"))
    assert auto.level == "info" and "«✅ Ha, avtomatik»" in auto.text
    [shadow] = _check(_env(JARVIS_ENABLED="true", JARVIS_MODE="shadow"))
    assert shadow.level == "info" and "JARVIS_MODE=draft" in shadow.text
    # Jarvis off: none of it.
    assert _check(_env(JARVIS_MODE="auto")) == []


def test_an_unpriced_model_warns_with_jarvis_on():
    from miya.services import usage

    assert frozenset(usage.MODEL_PRICES) == doctor.PRICED_MODELS
    assert {
        "EXTRACT_MODEL": settings_default("extract_model"),
        "REASON_MODEL": settings_default("reason_model"),
    } == doctor.MODEL_DEFAULTS
    [warn] = _texts(_check(_env(JARVIS_ENABLED="true", REASON_MODEL="my-model")), "warn")
    assert warn.startswith("⚠️ REASON_MODEL=my-model narxi noma'lum")
    assert "REASON_MODEL_PRICE=" in warn
    assert (
        _check(
            _env(JARVIS_ENABLED="true", REASON_MODEL="my-model", REASON_MODEL_PRICE="1,2")
        )
        == []
    )
    assert _check(_env(REASON_MODEL="my-model")) == []


def settings_default(field: str) -> str:
    from miya.config import Settings

    return Settings.model_fields[field].default

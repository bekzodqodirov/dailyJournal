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

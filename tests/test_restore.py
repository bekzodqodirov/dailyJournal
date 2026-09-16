"""`python -m miya.tools.restore`: the way back from a backup (build step 5)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import sqlalchemy as sa

from miya.config import settings
from miya.services import backup
from miya.tools import restore

SUFFIX = backup.BACKUP_SUFFIX


@pytest.fixture
def identity(tmp_path) -> Path:
    key = tmp_path / "backup-key.txt"
    key.write_text("AGE-SECRET-KEY-1FAKE\n")
    return key


@pytest.fixture
def whole(tmp_path) -> Path:
    path = tmp_path / f"miya-20260915-033000{SUFFIX}"
    path.write_bytes(b"ciphertext")
    return path


# --- arguments ----------------------------------------------------------------


def test_the_identity_is_required():
    with pytest.raises(SystemExit) as exit_info:
        restore.main(["some.dump.age"])
    assert exit_info.value.code == 2


def test_a_missing_file_is_a_usage_error(identity, tmp_path, capsys):
    code = restore.main([str(tmp_path / "nope.dump.age"), "--identity", str(identity)])
    assert code == restore.EXIT_USAGE
    assert "no such file" in capsys.readouterr().err


def test_a_missing_identity_is_a_usage_error(whole, tmp_path, capsys):
    code = restore.main([str(whole), "--identity", str(tmp_path / "missing.txt")])
    assert code == restore.EXIT_USAGE
    err = capsys.readouterr().err
    assert "identity" in err
    assert "AGE-SECRET-KEY" not in err


def test_the_target_defaults_to_the_configured_database(monkeypatch, whole, identity):
    seen: dict[str, object] = {}

    def fake_restore(file, ident, *, to, dry_run, force):
        seen.update(file=file, identity=ident, to=to, dry_run=dry_run, force=force)
        return restore.EXIT_OK

    monkeypatch.setattr(restore, "restore", fake_restore)
    assert restore.main([str(whole), "--identity", str(identity)]) == 0
    assert seen == {
        "file": whole,
        "identity": identity,
        "to": settings.database_url,
        "dry_run": False,
        "force": False,
    }
    restore.main(
        [str(whole), "--identity", str(identity), "--to", "postgresql://x/y", "--dry-run"]
    )
    assert seen["to"] == "postgresql://x/y"
    assert seen["dry_run"] is True
    restore.main([str(whole), "--identity", str(identity), "--force"])
    assert seen["force"] is True


def test_masked_hides_the_password_and_drops_the_driver():
    assert (
        restore.masked("postgresql+psycopg://miya:secret@db:5432/miya")
        == "postgresql://miya:***@db:5432/miya"
    )
    assert restore.masked("postgresql://db/miya") == "postgresql://db/miya"


# --- pieces -------------------------------------------------------------------


def _pieces(tmp_path, numbers) -> list[Path]:
    paths = []
    for number in numbers:
        path = tmp_path / f"miya-20260915-033000{SUFFIX}.part{number:02d}"
        path.write_bytes(bytes([number]))
        paths.append(path)
    return paths


def test_a_whole_backup_is_its_own_single_part(whole):
    assert restore.resolve_parts(whole) == [whole]


def test_any_piece_finds_its_siblings_in_order(tmp_path):
    created = _pieces(tmp_path, [3, 1, 2])
    (tmp_path / f"miya-20260101-000000{SUFFIX}.part01").write_bytes(b"other backup")
    parts = restore.resolve_parts(created[0])  # part03 was named
    assert [p.name[-6:] for p in parts] == ["part01", "part02", "part03"]
    assert parts == sorted(created)


def test_a_gap_in_the_pieces_is_refused(tmp_path):
    first, _third = _pieces(tmp_path, [1, 3])
    with pytest.raises(restore.RestoreError) as info:
        restore.resolve_parts(first)
    assert info.value.code == restore.EXIT_USAGE
    assert "incomplete" in str(info.value)


def test_pieces_not_starting_at_one_are_refused(tmp_path):
    (second,) = _pieces(tmp_path, [2])
    with pytest.raises(restore.RestoreError):
        restore.resolve_parts(second)


# --- pg_restore stderr --------------------------------------------------------

_EXTENSION_NOISE = """\
pg_restore: error: could not execute query: ERROR:  must be owner of extension vector
Command was: DROP EXTENSION IF EXISTS vector;
pg_restore: error: could not execute query: ERROR:  must be owner of extension vector
Command was: COMMENT ON EXTENSION vector IS 'vector data type';
pg_restore: warning: errors ignored on restore: 2
"""

_REAL_LOSS = """\
pg_restore: error: could not execute query: ERROR:  must be owner of extension vector
Command was: CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
pg_restore: error: COPY failed for table "debts": ERROR:  type "vector" does not exist
DETAIL:  something
Command was: COPY public.debts (id) FROM stdin;
pg_restore: warning: errors ignored on restore: 2
"""


def test_extension_ownership_noise_is_not_serious():
    assert restore.serious_errors(_EXTENSION_NOISE) == []


def test_a_failed_copy_is_serious():
    serious = restore.serious_errors(_REAL_LOSS)
    assert len(serious) == 1
    assert 'COPY failed for table "debts"' in serious[0]
    assert "COPY public.debts" in serious[0]


def test_pg_restore_treats_ignored_extension_errors_as_success(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        assert command[:5] == [
            "pg_restore",
            "--no-owner",
            "--no-privileges",
            "--clean",
            "--if-exists",
        ]
        assert "PGPASSWORD" in kwargs["env"] and kwargs["env"]["PGPASSWORD"] == "pw"
        assert "pw" not in " ".join(command)  # never on argv
        return subprocess.CompletedProcess(
            command, 1, stdout=b"", stderr=_EXTENSION_NOISE.encode()
        )

    monkeypatch.setattr(restore.subprocess, "run", fake_run)
    restore.pg_restore(tmp_path / "x.dump", "postgresql+psycopg://miya:pw@db/miya")


def test_pg_restore_fails_on_a_real_loss(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command, 1, stdout=b"", stderr=_REAL_LOSS.encode()
        )

    monkeypatch.setattr(restore.subprocess, "run", fake_run)
    with pytest.raises(restore.RestoreError) as info:
        restore.pg_restore(tmp_path / "x.dump", "postgresql://miya@db/miya")
    assert "1 serious error" in str(info.value)


def test_pg_restore_list_mode_returns_the_toc(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        assert command == ["pg_restore", "--list", str(tmp_path / "x.dump")]
        return subprocess.CompletedProcess(command, 0, stdout=b"; TOC\n", stderr=b"")

    monkeypatch.setattr(restore.subprocess, "run", fake_run)
    toc = restore.pg_restore(tmp_path / "x.dump", "postgresql://x/y", list_only=True)
    assert toc == "; TOC\n"


# --- the command, with the binaries stubbed ---------------------------------


def _stub_decrypt(monkeypatch, payload: bytes = b"PGDMP fake archive"):
    calls: list[list[Path]] = []

    def fake_decrypt(parts, identity, out_fd):
        calls.append(list(parts))
        os.write(out_fd, payload)

    monkeypatch.setattr(restore, "decrypt", fake_decrypt)
    return calls


def test_a_target_with_data_is_refused_without_force(monkeypatch, whole, identity):
    monkeypatch.setattr(restore, "target_has_data", lambda url: True)
    monkeypatch.setattr(
        restore, "decrypt", lambda *a, **k: pytest.fail("must not decrypt")
    )
    with pytest.raises(restore.RestoreError) as info:
        restore.restore(whole, identity, to="postgresql://miya:pw@db/miya")
    assert info.value.code == restore.EXIT_USAGE
    assert "--force" in str(info.value)
    assert "pw" not in str(info.value)


def test_force_restores_over_data_and_cleans_the_plaintext_up(
    monkeypatch, whole, identity, capsys
):
    monkeypatch.setattr(restore, "target_has_data", lambda url: True)
    monkeypatch.setattr(restore, "schema_revision", lambda url: "0011_heartbeats")
    decrypts = _stub_decrypt(monkeypatch)
    restored: list[tuple[Path, bytes, bool]] = []
    temp_files: list[Path] = []

    def fake_pg_restore(archive, url, *, list_only=False):
        temp_files.append(archive)
        restored.append((archive, archive.read_bytes(), list_only))
        assert archive.stat().st_mode & 0o777 == 0o600
        return ""

    monkeypatch.setattr(restore, "pg_restore", fake_pg_restore)

    code = restore.restore(whole, identity, to="postgresql://miya@db/miya", force=True)

    assert code == restore.EXIT_OK
    assert decrypts == [[whole]]
    assert [(data, listed) for _, data, listed in restored] == [
        (b"PGDMP fake archive", False)
    ]
    assert not temp_files[0].exists()  # the plaintext never outlives the command
    out = capsys.readouterr().out
    assert "0011_heartbeats" in out
    assert "make migrate" in out


def test_a_dry_run_only_lists_and_never_touches_the_target(
    monkeypatch, tmp_path, identity, capsys
):
    parts = _pieces(tmp_path, [1, 2])
    monkeypatch.setattr(
        restore, "target_has_data", lambda url: pytest.fail("dry run must not connect")
    )
    monkeypatch.setattr(
        restore, "schema_revision", lambda url: pytest.fail("dry run must not connect")
    )
    decrypts = _stub_decrypt(monkeypatch)
    listed: list[bool] = []

    def fake_pg_restore(archive, url, *, list_only=False):
        listed.append(list_only)
        return "; Archive created at …\n180; TABLE public debts\n"

    monkeypatch.setattr(restore, "pg_restore", fake_pg_restore)

    code = restore.restore(parts[1], identity, to="postgresql://x/y", dry_run=True)

    assert code == restore.EXIT_OK
    assert decrypts == [parts]  # both pieces, in order, from the second one
    assert listed == [True]
    captured = capsys.readouterr()
    assert "TABLE public debts" in captured.out
    assert "nothing was written" in captured.err


def test_a_failed_decrypt_leaves_no_plaintext_behind(monkeypatch, whole, identity):
    created: list[str] = []
    real_mkstemp = restore.tempfile.mkstemp

    def recording_mkstemp(**kwargs):
        fd, name = real_mkstemp(**kwargs)
        created.append(name)
        return fd, name

    monkeypatch.setattr(restore.tempfile, "mkstemp", recording_mkstemp)
    monkeypatch.setattr(restore, "target_has_data", lambda url: False)

    def failing_decrypt(parts, identity, out_fd):
        os.write(out_fd, b"half a dump")
        raise restore.RestoreError("age could not decrypt the backup: bad key")

    monkeypatch.setattr(restore, "decrypt", failing_decrypt)

    with pytest.raises(restore.RestoreError):
        restore.restore(whole, identity, to="postgresql://x/y")
    assert created and not Path(created[0]).exists()


def test_an_unreachable_target_is_a_failure_not_a_crash(monkeypatch, whole, identity):
    def refuse(url):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(restore, "target_has_data", refuse)
    with pytest.raises(restore.RestoreError) as info:
        restore.restore(whole, identity, to="postgresql://miya:pw@db/miya")
    assert info.value.code == restore.EXIT_FAILED
    assert "could not connect" in str(info.value)
    assert "pw" not in str(info.value)


def test_a_restore_without_alembic_version_is_reported(monkeypatch, whole, identity):
    monkeypatch.setattr(restore, "target_has_data", lambda url: False)
    monkeypatch.setattr(restore, "schema_revision", lambda url: None)
    _stub_decrypt(monkeypatch)
    monkeypatch.setattr(restore, "pg_restore", lambda *a, **k: "")
    assert restore.restore(whole, identity, to="postgresql://x/y") == restore.EXIT_FAILED


# --- the real thing -----------------------------------------------------------


def _round_trip_dsn() -> str:
    """A scratch database the restore may overwrite, or skip.

    Set MIYA_RESTORE_TEST_DSN to a database that is *not* DATABASE_URL: the
    restore drops and recreates every table in it.
    """
    dsn = os.environ.get("MIYA_RESTORE_TEST_DSN", "")
    if not dsn:
        pytest.skip("MIYA_RESTORE_TEST_DSN is not set")
    if dsn == settings.database_url:
        pytest.skip("MIYA_RESTORE_TEST_DSN must not be the test database itself")
    for binary in ("age", "age-keygen", "pg_dump", "pg_restore"):
        if not shutil.which(binary):
            pytest.skip(f"{binary} is needed for the round-trip test")
    return dsn


async def test_a_real_backup_restores_whole_and_in_pieces(
    session, monkeypatch, tmp_path, capsys
):
    """pg_dump | age → file → (split) → restore → the same schema revision."""
    import psycopg

    dsn = _round_trip_dsn()
    key = tmp_path / "key.txt"
    subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
    recipient = next(
        line.split(": ")[1].strip()
        for line in key.read_text().splitlines()
        if line.startswith("# public key:")
    )
    monkeypatch.setattr(settings, "backup_age_recipient", recipient)
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path / "backups"))
    result = await backup.create_backup()
    assert result.ok, result.error

    # Dry run: the archive lists, nothing is connected to.
    assert restore.main([str(result.path), "--identity", str(key), "--dry-run"]) == 0
    assert "debts" in capsys.readouterr().out

    # Whole file, --force because the scratch database may hold a prior run.
    code = restore.main(
        [str(result.path), "--identity", str(key), "--to", dsn, "--force"]
    )
    assert code == 0, capsys.readouterr().err
    expected = await session.scalar(sa.text("SELECT version_num FROM alembic_version"))
    with psycopg.connect(dsn.replace("postgresql+psycopg://", "postgresql://")) as conn:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row is not None and row[0] == expected

    # In pieces: tiny pieces so the join is exercised on a small dump.
    monkeypatch.setattr(backup, "TELEGRAM_PART_BYTES", 4096)
    monkeypatch.setattr(backup, "MAX_PARTS", 10_000)
    parts = backup.split_for_telegram(result.path)
    assert len(parts) > 1
    code = restore.main([str(parts[-1]), "--identity", str(key), "--to", dsn, "--force"])
    assert code == 0, capsys.readouterr().err
    backup.remove_parts(parts)

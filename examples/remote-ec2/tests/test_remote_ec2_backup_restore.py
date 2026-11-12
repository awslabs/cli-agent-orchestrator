"""backup.sh and restore.sh round-trip a real (temp) SQLite database."""

from __future__ import annotations

import gzip
import shutil
import sqlite3
from pathlib import Path

import pytest
from remote_ec2_support import recording_stub, write_stub

pytestmark = pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI missing")


def make_db(path: Path, rows: int = 25) -> list[tuple]:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE terminals (id TEXT PRIMARY KEY, name TEXT, created REAL)")
    con.executemany(
        "INSERT INTO terminals VALUES (?, ?, ?)",
        [(f"t{n:04d}", f"agent-{n}", n * 1.5) for n in range(rows)],
    )
    con.commit()
    data = con.execute("SELECT * FROM terminals ORDER BY id").fetchall()
    con.close()
    return data


def read_rows(path: Path) -> tuple[str, list[tuple]]:
    con = sqlite3.connect(path)
    check = con.execute("PRAGMA integrity_check").fetchone()[0]
    rows = con.execute("SELECT * FROM terminals ORDER BY id").fetchall()
    con.close()
    return check, rows


def backups_in(folder: Path) -> list[Path]:
    return sorted(folder.glob("cao-*.db.gz"))


def test_backup_then_drill_restore_round_trips(sandbox) -> None:
    db = sandbox.root / "cao" / "db" / "cli-agent-orchestrator.db"
    rows = make_db(db)
    dest = sandbox.root / "backups"

    result = sandbox.run("backup.sh", "--db", str(db), "--dest", str(dest), "--no-upload")
    assert result.returncode == 0, result.stderr
    assert "integrity_check ok" in result.stdout
    [backup] = backups_in(dest)
    assert backup.stat().st_mode & 0o777 == 0o600
    assert not list(dest.glob(".cao-*.partial"))
    with gzip.open(backup, "rb") as fh:
        assert fh.read(16) == b"SQLite format 3\x00"

    drill = sandbox.root / "drill"
    result = sandbox.run("restore.sh", "--drill", str(backup), "--to", str(drill))
    assert result.returncode == 0, result.stderr
    assert "integrity_check ok" in result.stdout
    assert "terminals" in result.stdout
    check, restored = read_rows(drill / "cli-agent-orchestrator.db")
    assert check == "ok"
    assert restored == rows


def test_backup_without_s3_uri_keeps_the_local_copy_and_says_so(sandbox) -> None:
    db = sandbox.root / "db.sqlite"
    make_db(db)
    recording_stub(sandbox.stubs, "aws", sandbox.calls)
    result = sandbox.run("backup.sh", "--db", str(db), "--dest", str(sandbox.root / "b"))
    assert result.returncode == 0, result.stderr
    assert "BACKUP_S3_URI is empty" in result.stderr
    assert sandbox.recorded_calls() == []


def test_backup_uploads_to_the_s3_prefix(sandbox) -> None:
    db = sandbox.root / "db.sqlite"
    make_db(db)
    recording_stub(sandbox.stubs, "aws", sandbox.calls)
    result = sandbox.run(
        "backup.sh",
        "--db",
        str(db),
        "--dest",
        str(sandbox.root / "b"),
        BACKUP_S3_URI="s3://example-bucket/cao-box/",
    )
    assert result.returncode == 0, result.stderr
    [call] = sandbox.recorded_calls()
    assert call.startswith("aws s3 cp --only-show-errors ")
    assert " s3://example-bucket/cao-box/cao-" in call


def test_backup_rejects_a_malformed_s3_uri(sandbox) -> None:
    db = sandbox.root / "db.sqlite"
    make_db(db)
    recording_stub(sandbox.stubs, "aws", sandbox.calls)
    result = sandbox.run(
        "backup.sh",
        "--db",
        str(db),
        "--dest",
        str(sandbox.root / "b"),
        BACKUP_S3_URI="example-bucket",
    )
    assert result.returncode == 2
    assert sandbox.recorded_calls() == []


def test_backup_prunes_to_keep(sandbox) -> None:
    db = sandbox.root / "db.sqlite"
    make_db(db)
    dest = sandbox.root / "b"
    dest.mkdir()
    for stamp in ["20200101T000000Z", "20200102T000000Z", "20200103T000000Z"]:
        (dest / f"cao-{stamp}.db.gz").write_bytes(b"old")
    result = sandbox.run(
        "backup.sh", "--db", str(db), "--dest", str(dest), "--keep", "2", "--no-upload"
    )
    assert result.returncode == 0, result.stderr
    names = [p.name for p in backups_in(dest)]
    assert len(names) == 2
    assert "cao-20200103T000000Z.db.gz" in names
    assert "cao-20200101T000000Z.db.gz" not in names


def test_backup_fails_on_a_missing_database(sandbox) -> None:
    result = sandbox.run("backup.sh", "--db", str(sandbox.root / "nope.db"), "--no-upload")
    assert result.returncode == 1
    assert "no database" in result.stderr


def test_restore_refuses_a_corrupt_backup(sandbox) -> None:
    bad = sandbox.root / "bad.db"
    bad.write_bytes(b"this is not a database" * 100)
    result = sandbox.run("restore.sh", "--drill", str(bad), "--to", str(sandbox.root / "d"))
    assert result.returncode != 0
    assert not (sandbox.root / "d" / "cli-agent-orchestrator.db").exists()


def live_env(sandbox) -> dict[str, str]:
    systemctl = recording_stub(sandbox.stubs, "fake-systemctl", sandbox.calls)
    write_stub(sandbox.stubs, "curl", f'echo "curl $*" >> "{sandbox.calls}"\nexit 0\n')
    return {"SYSTEMCTL": str(systemctl), "CAO_URL": "http://127.0.0.1:1"}


def test_live_restore_needs_yes(sandbox) -> None:
    db = sandbox.root / "live" / "cao.db"
    make_db(db, rows=3)
    backup = sandbox.root / "backup.db"
    make_db(backup, rows=9)
    result = sandbox.run("restore.sh", "--live", str(backup), "--db", str(db), **live_env(sandbox))
    assert result.returncode == 2
    assert "--yes" in result.stderr
    assert sandbox.recorded_calls() == []
    assert len(read_rows(db)[1]) == 3


def test_live_restore_swaps_the_database_and_keeps_the_old_one(sandbox) -> None:
    db = sandbox.root / "live" / "cao.db"
    old_rows = make_db(db, rows=3)
    backup = sandbox.root / "backup.db"
    new_rows = make_db(backup, rows=9)
    result = sandbox.run(
        "restore.sh", "--live", str(backup), "--db", str(db), "--yes", **live_env(sandbox)
    )
    assert result.returncode == 0, result.stderr
    calls = sandbox.recorded_calls()
    assert calls[0] == "fake-systemctl stop cao-server.service"
    assert calls[1] == "fake-systemctl start cao-server.service"
    assert calls[2].startswith("curl ") and "http://127.0.0.1:1/health" in calls[2]
    assert read_rows(db) == ("ok", new_rows)
    [kept] = list(db.parent.glob("cao.db.pre-restore-*"))
    assert read_rows(kept) == ("ok", old_rows)


def test_live_restore_refuses_when_a_journal_remains(sandbox) -> None:
    db = sandbox.root / "live" / "cao.db"
    make_db(db, rows=3)
    (db.parent / "cao.db-journal").write_bytes(b"hot")
    backup = sandbox.root / "backup.db"
    make_db(backup, rows=9)
    result = sandbox.run(
        "restore.sh", "--live", str(backup), "--db", str(db), "--yes", **live_env(sandbox)
    )
    assert result.returncode == 1
    assert "not restoring over it" in result.stderr
    assert sandbox.recorded_calls() == ["fake-systemctl stop cao-server.service"]
    assert len(read_rows(db)[1]) == 3


def failing_integrity_sqlite(sandbox) -> None:
    """A sqlite3 wrapper that reports corruption on integrity_check, else the real CLI."""
    real = shutil.which("sqlite3")
    write_stub(
        sandbox.stubs,
        "sqlite3",
        'case "$*" in\n'
        '  *integrity_check*) echo "*** in database main ***"; echo "Page 2: never used"; exit 0 ;;\n'
        f'  *) exec "{real}" "$@" ;;\n'
        "esac\n",
    )


def test_backup_keeps_nothing_when_integrity_check_fails(sandbox) -> None:
    db = sandbox.root / "db.sqlite"
    make_db(db)
    failing_integrity_sqlite(sandbox)
    recording_stub(sandbox.stubs, "aws", sandbox.calls)
    dest = sandbox.root / "b"
    result = sandbox.run(
        "backup.sh",
        "--db",
        str(db),
        "--dest",
        str(dest),
        BACKUP_S3_URI="s3://example-bucket/cao-box/",
    )
    assert result.returncode == 1
    assert "integrity_check failed" in result.stderr
    assert backups_in(dest) == []
    assert not list(dest.glob(".cao-*"))
    assert sandbox.recorded_calls() == []  # nothing uploaded


def test_live_restore_stops_nothing_when_integrity_check_fails(sandbox) -> None:
    db = sandbox.root / "live" / "cao.db"
    make_db(db, rows=3)
    backup = sandbox.root / "backup.db"
    make_db(backup, rows=9)
    env = live_env(sandbox)
    failing_integrity_sqlite(sandbox)
    result = sandbox.run("restore.sh", "--live", str(backup), "--db", str(db), "--yes", **env)
    assert result.returncode == 1
    assert "nothing changed" in result.stderr
    assert sandbox.recorded_calls() == []
    assert len(read_rows(db)[1]) == 3

"""backup.sh and restore.sh round-trip a real (temp) SQLite database."""

from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
from pathlib import Path

import pytest
from remote_ec2_support import recording_stub, write_stub

pytestmark = pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI missing")
KMS_KEY = "arn:aws:kms:ap-southeast-2:0:key/test-key"


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
    assert dest.stat().st_mode & 0o777 == 0o700
    assert not list(dest.glob(".cao-*.partial"))
    with gzip.open(backup, "rb") as fh:
        assert fh.read(16) == b"SQLite format 3\x00"

    drill = sandbox.root / "drill"
    result = sandbox.run("restore.sh", "--drill", str(backup), "--to", str(drill))
    assert result.returncode == 0, result.stderr
    assert "integrity_check ok" in result.stdout
    assert "terminals" in result.stdout
    assert (drill / "cli-agent-orchestrator.db").stat().st_mode & 0o777 == 0o600
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
        BACKUP_KMS_KEY_ARN=KMS_KEY,
    )
    assert result.returncode == 0, result.stderr
    [call] = sandbox.recorded_calls()
    assert call.startswith(
        f"aws s3 cp --only-show-errors --sse aws:kms --sse-kms-key-id {KMS_KEY} "
    )
    assert " s3://example-bucket/cao-box/cao-" in call


def test_backup_upload_needs_the_kms_key(sandbox) -> None:
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
    assert result.returncode == 2
    assert "BACKUP_KMS_KEY_ARN" in result.stderr
    assert sandbox.recorded_calls() == []


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


class LiveBox:
    """A fake box for restore.sh --live: a root-looking `id`, the box user in
    `getent`, a root-owned-looking /etc/cao-box, and the user's live DB."""

    def __init__(self, sandbox, rows: int = 3, db_mode: int = 0o640) -> None:
        self.sandbox = sandbox
        self.uid = os.getuid()
        self.gid = os.getgid()
        self.home = sandbox.root / "boxhome"
        self.db = self.home / ".aws" / "cli-agent-orchestrator" / "db" / "cli-agent-orchestrator.db"
        self.rows = make_db(self.db, rows=rows)
        self.db.chmod(db_mode)
        self.etc = sandbox.root / "etc-cao-box"
        self.etc.mkdir()
        self.user_env = self.etc / "box-user.env"
        self.write_user_env(self.uid)
        self.passwd_uid = self.uid
        self.run_uid = 0
        self.etc_mode = "755"
        self.on_start = ""

    def write_user_env(self, uid: int, extra: str = "") -> None:
        self.user_env.write_text(
            f"BOX_USER=boxuser\nBOX_UID={uid}\nBOX_HOME={self.home}\n{extra}", encoding="utf-8"
        )

    def env(self) -> dict[str, str]:
        sb = self.sandbox
        snapshot = sb.root / "db-at-stop.bin"
        systemctl = write_stub(
            sb.stubs,
            "fake-systemctl",
            f'echo "fake-systemctl $*" >> "{sb.calls}"\n'
            f'if [ "$1" = stop ]; then cp "{self.db}" "{snapshot}"; fi\n'
            f'if [ "$1" = start ]; then {self.on_start or ":"}; fi\n',
        )
        write_stub(sb.stubs, "curl", f'echo "curl $*" >> "{sb.calls}"\nexit 0\n')
        # runuser -u USER -- CMD...: log it, then run CMD (the tests are one user).
        write_stub(
            sb.stubs,
            "runuser",
            f'echo "runuser $*" >> "{sb.calls}"\n'
            '[ "$1" = "-u" ] && [ "$3" = "--" ] || exit 64\n'
            'shift 3\nexec "$@"\n',
        )
        write_stub(
            sb.stubs,
            "id",
            f'[ "$1" = "-u" ] && echo {self.run_uid} && exit 0\nexec /usr/bin/id "$@"\n',
        )
        line = f"boxuser:x:{self.passwd_uid}:{self.gid}::{self.home}:/bin/bash"
        write_stub(
            sb.stubs, "getent", f'[ "$*" = "passwd boxuser" ] && echo "{line}" && exit 0\nexit 2\n'
        )
        # /etc/cao-box can't be root-owned in a test: report it as root's.
        write_stub(
            sb.stubs,
            "stat",
            f'case "$*" in\n  *"{self.etc}"*) echo "0 {self.etc_mode}" ;;\n  *) exec /usr/bin/stat "$@" ;;\nesac\n',
        )
        return {
            "SYSTEMCTL": str(systemctl),
            "CAO_URL": "http://127.0.0.1:1",
            "CAO_BOX_ETC": str(self.etc),
        }

    def restore(self, backup: Path, *flags: str):
        return self.sandbox.run("restore.sh", "--live", str(backup), *flags, **self.env())


def a_backup(sandbox, rows: int = 9) -> tuple[Path, list[tuple]]:
    backup = sandbox.root / "backup.db"
    return backup, make_db(backup, rows=rows)


def test_live_restore_needs_yes(sandbox) -> None:
    box = LiveBox(sandbox)
    backup, _ = a_backup(sandbox)
    result = box.restore(backup)
    assert result.returncode == 2
    assert "--yes" in result.stderr
    assert sandbox.recorded_calls() == []
    assert read_rows(box.db)[1] == box.rows


def test_live_restore_is_root_only(sandbox) -> None:
    box = LiveBox(sandbox)
    box.run_uid = os.getuid() or 1000
    backup, _ = a_backup(sandbox)
    result = box.restore(backup, "--yes")
    assert result.returncode == 2
    assert "root-only" in result.stderr
    assert sandbox.recorded_calls() == []


def test_live_restore_refuses_db_flag(sandbox) -> None:
    box = LiveBox(sandbox)
    backup, _ = a_backup(sandbox)
    result = box.restore(backup, "--yes", "--db", str(box.db))
    assert result.returncode == 2
    assert "--db is for drills only" in result.stderr


def test_live_restore_swaps_as_the_box_user_and_keeps_modes(sandbox) -> None:
    box = LiveBox(sandbox, db_mode=0o640)
    backup, new_rows = a_backup(sandbox)
    old_bytes = box.db.read_bytes()
    old_owner = (box.db.stat().st_uid, box.db.stat().st_gid)
    result = box.restore(backup, "--yes")
    assert result.returncode == 0, result.stderr
    assert "back on the restored database" in result.stdout
    # cao-server was stopped while the database was still the old one.
    assert (sandbox.root / "db-at-stop.bin").read_bytes() == old_bytes
    calls = sandbox.recorded_calls()
    root_calls = [c for c in calls if not c.startswith("runuser ")]
    assert root_calls[0] == "fake-systemctl stop cao-server.service"
    assert root_calls[1] == "fake-systemctl start cao-server.service"
    assert root_calls[2].startswith("curl ") and "http://127.0.0.1:1/health" in root_calls[2]
    # Every file operation in the box user's tree ran as the box user.
    box_ops = [
        c.split(" -- ", 1)[1].split()[0] for c in calls if c.startswith("runuser -u boxuser -- ")
    ]
    assert box_ops == ["cat", "cp", "sh", "chmod", "mv", "sqlite3"], calls
    assert read_rows(box.db) == ("ok", new_rows)
    [kept] = list(box.db.parent.glob("cli-agent-orchestrator.db.pre-restore-*"))
    assert read_rows(kept) == ("ok", box.rows)
    for path in (box.db, kept):
        st = path.stat()
        # The box user wrote these files, so they're the box user's; the
        # group follows the platform's rule, so compare with the original.
        assert (st.st_uid, st.st_gid, st.st_mode & 0o777) == (*old_owner, 0o640), path


def test_live_restore_fails_if_the_live_db_is_not_the_restored_one(sandbox) -> None:
    box = LiveBox(sandbox)
    box.on_start = f'/usr/bin/sqlite3 "{box.db}" "CREATE TABLE surprise (x)"'
    backup, _ = a_backup(sandbox)
    result = box.restore(backup, "--yes")
    assert result.returncode == 1
    assert "tables differ" in result.stderr


def test_live_restore_refuses_a_db_the_box_user_does_not_own(sandbox) -> None:
    box = LiveBox(sandbox)
    box.write_user_env(box.uid + 1)
    box.passwd_uid = box.uid + 1
    backup, _ = a_backup(sandbox)
    result = box.restore(backup, "--yes")
    assert result.returncode == 1
    assert "isn't owned by boxuser" in result.stderr
    assert sandbox.recorded_calls() == []


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("extra-line", "exactly BOX_USER, BOX_UID and BOX_HOME"),
        ("uid-mismatch", "uid isn't"),
        ("etc-writable", "must be root's"),
    ],
)
def test_live_restore_refuses_a_bad_box_user_file(sandbox, change, message) -> None:
    box = LiveBox(sandbox)
    if change == "extra-line":
        box.write_user_env(box.uid, extra="LD_PRELOAD=/dev/shm/x.so\n")
    elif change == "uid-mismatch":
        box.passwd_uid = box.uid + 7
    else:
        box.etc_mode = "777"
    backup, _ = a_backup(sandbox)
    result = box.restore(backup, "--yes")
    assert result.returncode == 1
    assert message in result.stderr
    assert sandbox.recorded_calls() == []


@pytest.mark.parametrize("side", ["-journal", "-wal", "-shm"])
def test_live_restore_refuses_when_a_sidecar_file_remains(sandbox, side) -> None:
    box = LiveBox(sandbox)
    (box.db.parent / f"cli-agent-orchestrator.db{side}").write_bytes(b"hot")
    backup, _ = a_backup(sandbox)
    result = box.restore(backup, "--yes")
    assert result.returncode == 1
    assert "not restoring over it" in result.stderr
    root_calls = [c for c in sandbox.recorded_calls() if not c.startswith("runuser ")]
    assert root_calls == ["fake-systemctl stop cao-server.service"]
    assert read_rows(box.db)[1] == box.rows


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
        BACKUP_KMS_KEY_ARN=KMS_KEY,
    )
    assert result.returncode == 1
    assert "integrity_check failed" in result.stderr
    assert backups_in(dest) == []
    assert not list(dest.glob(".cao-*"))
    assert sandbox.recorded_calls() == []  # nothing uploaded


def test_live_restore_stops_nothing_when_integrity_check_fails(sandbox) -> None:
    box = LiveBox(sandbox)
    backup, _ = a_backup(sandbox)
    env = box.env()
    failing_integrity_sqlite(sandbox)
    result = sandbox.run("restore.sh", "--live", str(backup), "--yes", **env)
    assert result.returncode == 1
    assert "nothing changed" in result.stderr
    assert not any(c.startswith("fake-systemctl") for c in sandbox.recorded_calls())
    assert read_rows(box.db)[1] == box.rows


def test_a_drill_refuses_the_live_cao_home(sandbox) -> None:
    cao_home = sandbox.home / ".aws" / "cli-agent-orchestrator"
    live = cao_home / "db" / "cli-agent-orchestrator.db"
    rows = make_db(live, rows=4)
    backup = sandbox.root / "backup.db"
    make_db(backup, rows=9)
    for target in [cao_home / "db", cao_home, cao_home / "db" / "new-sub"]:
        result = sandbox.run("restore.sh", "--drill", str(backup), "--to", str(target))
        assert result.returncode == 2, target
        assert "inside the live CAO home" in result.stderr
    assert read_rows(live) == ("ok", rows)


def test_a_drill_refuses_a_folder_that_is_not_empty(sandbox) -> None:
    backup = sandbox.root / "backup.db"
    make_db(backup, rows=2)
    target = sandbox.root / "busy"
    target.mkdir()
    (target / "keep.txt").write_text("mine", encoding="utf-8")
    result = sandbox.run("restore.sh", "--drill", str(backup), "--to", str(target))
    assert result.returncode == 2
    assert "not empty" in result.stderr
    assert sorted(p.name for p in target.iterdir()) == ["keep.txt"]


def test_a_drill_into_an_empty_folder_works(sandbox) -> None:
    backup = sandbox.root / "backup.db"
    rows = make_db(backup, rows=2)
    target = sandbox.root / "empty"
    target.mkdir()
    result = sandbox.run("restore.sh", "--drill", str(backup), "--to", str(target))
    assert result.returncode == 0, result.stderr
    assert read_rows(target / "cli-agent-orchestrator.db") == ("ok", rows)


@pytest.mark.parametrize("args", [["--to"], ["--drill"], ["--live"], ["--db"]])
def test_flags_without_a_value_are_usage_errors(sandbox, args) -> None:
    result = sandbox.run("restore.sh", *args)
    assert result.returncode == 2
    assert "needs a value" in result.stderr


# ------------------------------------------- N4: symlinks and the box user


def no_systemctl(sandbox) -> bool:
    return not any(c.startswith("fake-systemctl") for c in sandbox.recorded_calls())


def test_a_symlinked_live_db_pointing_at_a_root_only_file_is_refused(sandbox) -> None:
    box = LiveBox(sandbox)
    secret = sandbox.root / "root-only" / "shadow"
    secret.parent.mkdir()
    secret.write_text("TOP-SECRET-ROOT-FILE", encoding="utf-8")
    secret.chmod(0o600)
    box.db.unlink()
    box.db.symlink_to(secret)
    backup, _ = a_backup(sandbox)
    result = box.restore(backup, "--yes")
    assert result.returncode == 1
    assert "is a symlink; refusing" in result.stderr
    assert no_systemctl(sandbox)
    leftovers = [p.name for p in box.db.parent.iterdir() if p.name != box.db.name]
    assert leftovers == [], "nothing may be copied"
    for path in box.db.parent.parent.rglob("*"):
        if path.is_file() and not path.is_symlink():
            assert "TOP-SECRET" not in path.read_text(encoding="utf-8", errors="ignore")


def test_a_symlinked_backup_is_refused(sandbox) -> None:
    box = LiveBox(sandbox)
    real_backup, _ = a_backup(sandbox)
    link = sandbox.root / "backup-link.db"
    link.symlink_to(real_backup)
    result = box.restore(link, "--yes")
    assert result.returncode == 1
    assert "is a symlink; refusing" in result.stderr
    assert sandbox.recorded_calls() == []
    assert read_rows(box.db)[1] == box.rows


@pytest.mark.parametrize("level", ["db", "cli-agent-orchestrator", ".aws"])
def test_a_symlinked_db_folder_is_refused(sandbox, level) -> None:
    box = LiveBox(sandbox)
    folder = {
        "db": box.db.parent,
        "cli-agent-orchestrator": box.db.parent.parent,
        ".aws": box.db.parent.parent.parent,
    }[level]
    moved = folder.with_name(folder.name + "-real")
    folder.rename(moved)
    folder.symlink_to(moved)
    backup, _ = a_backup(sandbox)
    result = box.restore(backup, "--yes")
    assert result.returncode == 1
    assert "is a symlink; refusing" in result.stderr
    assert sandbox.recorded_calls() == []
    assert not list(moved.rglob("*.pre-restore-*"))


def test_a_planted_restoring_symlink_is_refused(sandbox) -> None:
    box = LiveBox(sandbox)
    target = sandbox.root / "elsewhere"
    (box.db.parent / "cli-agent-orchestrator.db.restoring").symlink_to(target)
    backup, _ = a_backup(sandbox)
    result = box.restore(backup, "--yes")
    assert result.returncode == 1
    assert "is a symlink; refusing" in result.stderr
    assert no_systemctl(sandbox)
    assert not target.exists()


def test_a_live_dry_run_checks_everything_and_changes_nothing(sandbox) -> None:
    box = LiveBox(sandbox)
    backup, _ = a_backup(sandbox)
    before = box.db.read_bytes()
    result = box.restore(backup, "--yes", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "dry run; would stop cao-server" in result.stdout
    assert no_systemctl(sandbox)
    assert box.db.read_bytes() == before
    assert not list(box.db.parent.glob("*.pre-restore-*"))

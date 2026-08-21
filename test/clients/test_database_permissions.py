"""Tests for DB dir/file permission hardening (PR #372 security review).

The SQLite DB persists workflow ``spec_snapshot`` (full prompt bodies) and
``inputs_json``, so the dir must be owner-only (0o700) and the DB file (plus any
-wal/-shm siblings) 0o600 — the same posture as claude_code prompt files and the
audit log. The chmods are best-effort: a failure logs and never blocks startup.
"""

import stat
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from cli_agent_orchestrator.clients import database as db_mod


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the module at a temp DB dir/file + a temp engine."""
    db_dir = tmp_path / "db"
    db_file = db_dir / "cli-agent-orchestrator.db"
    monkeypatch.setattr(db_mod, "DB_DIR", db_dir)
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_file, raising=True)
    db_mod._ensure_db_dir()
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    yield db_dir, db_file
    engine.dispose()


class TestDbPermissions:
    def test_init_db_sets_dir_0700_and_file_0600(self, isolated_db):
        db_dir, db_file = isolated_db
        db_mod.init_db()
        assert _mode(db_dir) == 0o700
        assert db_file.exists()
        assert _mode(db_file) == 0o600

    def test_ensure_db_dir_tightens_preexisting_loose_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # exist_ok=True means mkdir's mode is ignored for a pre-existing dir; the
        # explicit chmod must still tighten it to 0o700.
        db_dir = tmp_path / "loose-db"
        db_dir.mkdir(mode=0o755)
        monkeypatch.setattr(db_mod, "DB_DIR", db_dir)
        db_mod._ensure_db_dir()
        assert _mode(db_dir) == 0o700

    def test_init_db_restricts_wal_and_shm_siblings(self, isolated_db):
        db_dir, db_file = isolated_db
        # Simulate leftover WAL/SHM siblings from a prior run with loose perms.
        wal = db_file.with_name(db_file.name + "-wal")
        shm = db_file.with_name(db_file.name + "-shm")
        for sibling in (wal, shm):
            sibling.touch(mode=0o644)
            sibling.chmod(0o644)
        db_mod.init_db()
        assert _mode(wal) == 0o600
        assert _mode(shm) == 0o600

    def test_restrict_db_file_permissions_missing_file_is_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Best-effort: an absent DB file (nothing created yet) must not raise.
        monkeypatch.setattr(
            "cli_agent_orchestrator.constants.DATABASE_FILE",
            tmp_path / "nope" / "absent.db",
            raising=True,
        )
        db_mod._restrict_db_file_permissions()

    def test_restrict_db_file_permissions_chmod_failure_logged_not_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
    ):
        db_file = tmp_path / "x.db"
        db_file.touch()
        monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_file, raising=True)

        def _boom(*args, **kwargs):
            raise OSError("chmod unsupported")

        monkeypatch.setattr(db_mod.os, "chmod", _boom)
        with caplog.at_level("WARNING"):
            db_mod._restrict_db_file_permissions()
        assert any("Could not restrict DB file permissions" in r.message for r in caplog.records)

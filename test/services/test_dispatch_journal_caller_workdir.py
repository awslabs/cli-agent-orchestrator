"""The dispatch journal carries caller_id and working_directory, so a launch
reconciled after a restart is restored with both (haefeif re#7 on PR #802).

The redelivered-LAUNCH reconcile writer can only restore what the journal held,
and the journal held neither field: a recovered terminal lost its callback
parent (``caller_id``) and its recorded workspace (``working_directory``). Both
are added as nullable columns via the existing PRAGMA-gated idempotent
migration, recorded at dispatch, and restored on reconcile.
"""

import sqlite3
import tempfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import cli_agent_orchestrator.clients.database as db
import cli_agent_orchestrator.runtime_channel.api as api
from cli_agent_orchestrator.runtime_channel.protocol import CommandType


@pytest.fixture()
def temp_db(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.close()
    engine = create_engine(f"sqlite:///{tmp.name}", connect_args={"check_same_thread": False})
    db.Base.metadata.create_all(engine)
    TempSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db, "SessionLocal", TempSession)
    return TempSession


# --------------------------------------------------------------------------- #
# migration: an old-schema table gains both columns, idempotently
# --------------------------------------------------------------------------- #
def _columns(path):
    with sqlite3.connect(str(path)) as conn:
        return {row[1] for row in conn.execute("PRAGMA table_info(dispatch_journal)").fetchall()}


def test_migration_adds_caller_and_working_directory_and_is_idempotent(monkeypatch, tmp_path):
    dbfile = tmp_path / "old.sqlite"
    # A dispatch_journal as an earlier revision built it: no caller_id/workdir.
    with sqlite3.connect(str(dbfile)) as conn:
        conn.execute(
            "CREATE TABLE dispatch_journal ("
            "op_id VARCHAR PRIMARY KEY, command_type VARCHAR NOT NULL, "
            "runtime_id VARCHAR NOT NULL, terminal_id VARCHAR, owner VARCHAR, "
            "run_id VARCHAR, step_id VARCHAR, engine VARCHAR, "
            "state VARCHAR NOT NULL, created_at DATETIME, settled_at DATETIME)"
        )

    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", dbfile)

    assert "caller_id" not in _columns(dbfile)
    assert "working_directory" not in _columns(dbfile)

    db._migrate_dispatch_journal()
    cols = _columns(dbfile)
    assert "caller_id" in cols
    assert "working_directory" in cols

    # Idempotent: a second run must not raise (no duplicate-column error).
    db._migrate_dispatch_journal()
    assert "caller_id" in _columns(dbfile)


# --------------------------------------------------------------------------- #
# record_dispatch accepts and returns both fields
# --------------------------------------------------------------------------- #
def test_record_dispatch_persists_caller_and_working_directory(temp_db):
    db.record_dispatch(
        "op-1",
        CommandType.LAUNCH.value,
        "worker-1",
        caller_id="caller-terminal",
        working_directory="/work/here",
    )
    rec = db.get_dispatch_record("op-1")
    assert rec["caller_id"] == "caller-terminal"
    assert rec["working_directory"] == "/work/here"


# --------------------------------------------------------------------------- #
# reconcile restores both onto the recovered terminal row
# --------------------------------------------------------------------------- #
def test_reconciled_launch_restores_caller_and_working_directory(temp_db):
    op_id = "op-reconcile"
    db.record_dispatch(
        op_id,
        CommandType.LAUNCH.value,
        "worker-1",
        owner="cao:local#local",
        caller_id="parent-terminal",
        working_directory="/home/agent/project",
    )
    rec = db.get_dispatch_record(op_id)

    info = {
        "id": "term-recovered",
        "session_name": "cao-term-recovered",
        "name": "developer",
        "provider": "kiro_cli",
    }
    ok = api._persist_reconciled_terminal(info, "worker-1", rec, op_id)
    assert ok is True

    row = db.get_terminal_metadata("term-recovered")
    assert row is not None
    assert row.get("caller_id") == "parent-terminal"
    assert row.get("working_directory") == "/home/agent/project"

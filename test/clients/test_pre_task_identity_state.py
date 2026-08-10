"""The dedicated ``pre_task_identity_state`` column of ordinary terminals.

The pre-task identity launch state lives in its own nullable closed-state
column, never in ``native_session_id`` (which contracts to mean the real
provider-native session running in the pane).  Fresh databases get the
column from ``Base.metadata.create_all``; this migration is the only path
for a database created before the state existed.  The column moves
forward-only (``pending`` -> ``captured`` -> ``ready``), is idempotent,
and a row born without the marker (NULL) never gains one — the legacy
compatibility exemption is permanent.
"""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services.provider_contracts import (
    PRE_TASK_IDENTITY_CAPTURED,
    PRE_TASK_IDENTITY_PENDING,
    PRE_TASK_IDENTITY_READY,
)


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """A real SQLite database on the current ORM schema."""
    path = tmp_path / "metadata.db"
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False},
    )
    database.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(
        database,
        "SessionLocal",
        sessionmaker(autocommit=False, autoflush=False, bind=engine),
    )
    yield engine
    engine.dispose()


@pytest.fixture
def legacy_db(tmp_path, monkeypatch):
    """A database file whose terminals table predates the state column."""
    path = tmp_path / "cli-agent-orchestrator.db"
    with sqlite3.connect(str(path)) as conn:
        conn.execute(
            "CREATE TABLE terminals ("
            "id TEXT PRIMARY KEY, tmux_session TEXT, tmux_window TEXT, provider TEXT)"
        )
        conn.execute(
            "INSERT INTO terminals (id, tmux_session, tmux_window, provider) "
            "VALUES ('legacy01', 'cao-session', 'worker-abcd', 'claude_code')"
        )
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path, raising=False)
    return path


def _columns(path) -> set[str]:
    with sqlite3.connect(str(path)) as conn:
        return {row[1] for row in conn.execute("PRAGMA table_info(terminals)")}


def _row_state(path, terminal_id: str) -> str:
    with sqlite3.connect(str(path)) as conn:
        row = conn.execute(
            "SELECT native_session_id, pre_task_identity_state FROM terminals WHERE id = ?",
            (terminal_id,),
        ).fetchone()
        return row


def test_migration_adds_dedicated_state_column_null_on_existing_rows(legacy_db):
    assert "pre_task_identity_state" not in _columns(legacy_db)
    database._migrate_terminals_schema()
    assert "pre_task_identity_state" in _columns(legacy_db)
    # The existing row keeps NULL state (truthful legacy) and its session id
    # untouched.
    assert _row_state(legacy_db, "legacy01") == (None, None)


def test_creation_stamps_pending_keeps_native_session_id_null(isolated_db):
    database.create_terminal(
        "pending01",
        "cao-session",
        "worker-abcd",
        "claude_code",
        pane_id="%71",
        pane_pid=7171,
        pre_task_identity_state=PRE_TASK_IDENTITY_PENDING,
    )
    row = database.get_terminal_metadata("pending01")
    assert row["pre_task_identity_state"] == PRE_TASK_IDENTITY_PENDING
    assert row["native_session_id"] is None


def test_state_moves_forward_only(isolated_db):
    database.create_terminal(
        "state01",
        "cao-session",
        "worker-abcd",
        "claude_code",
        pane_id="%72",
        pane_pid=7272,
        pre_task_identity_state=PRE_TASK_IDENTITY_PENDING,
    )
    # pending -> captured -> ready: forward moves succeed.
    assert database.set_terminal_pre_task_identity_state("state01", PRE_TASK_IDENTITY_CAPTURED)
    assert database.set_terminal_pre_task_identity_state("state01", PRE_TASK_IDENTITY_READY)
    # Idempotent for the current state.
    assert database.set_terminal_pre_task_identity_state("state01", PRE_TASK_IDENTITY_READY)
    assert (
        database.get_terminal_metadata("state01")["pre_task_identity_state"]
        == PRE_TASK_IDENTITY_READY
    )


def test_state_never_moves_backwards(isolated_db):
    database.create_terminal(
        "state02",
        "cao-session",
        "worker-abcd",
        "claude_code",
        pane_id="%73",
        pane_pid=7373,
        pre_task_identity_state=PRE_TASK_IDENTITY_CAPTURED,
    )
    assert not database.set_terminal_pre_task_identity_state("state02", PRE_TASK_IDENTITY_PENDING)
    # The refused move changed nothing.
    assert (
        database.get_terminal_metadata("state02")["pre_task_identity_state"]
        == PRE_TASK_IDENTITY_CAPTURED
    )


def test_legacy_row_never_gains_the_marker(isolated_db):
    database.create_terminal(
        "legacy02",
        "cao-session",
        "worker-abcd",
        "claude_code",
        pane_id="%74",
        pane_pid=7474,
    )
    assert not database.set_terminal_pre_task_identity_state("legacy02", PRE_TASK_IDENTITY_PENDING)
    assert database.get_terminal_metadata("legacy02")["pre_task_identity_state"] is None


def test_captured_only_from_pending(isolated_db):
    database.create_terminal(
        "state03",
        "cao-session",
        "worker-abcd",
        "claude_code",
        pane_id="%75",
        pane_pid=7575,
        pre_task_identity_state=PRE_TASK_IDENTITY_READY,
    )
    assert not database.set_terminal_pre_task_identity_state("state03", PRE_TASK_IDENTITY_CAPTURED)


def test_unknown_state_refused(isolated_db):
    database.create_terminal(
        "state04",
        "cao-session",
        "worker-abcd",
        "claude_code",
        pane_id="%76",
        pane_pid=7676,
        pre_task_identity_state=PRE_TASK_IDENTITY_PENDING,
    )
    assert not database.set_terminal_pre_task_identity_state("state04", "bogus-state")


def test_absent_row_refused(isolated_db):
    assert not database.set_terminal_pre_task_identity_state("nope", PRE_TASK_IDENTITY_READY)

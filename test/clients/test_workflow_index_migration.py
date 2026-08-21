"""Tests for the workflow_index migration (issue #312, Bolt 2 / N2).

Asserts ``_migrate_workflow_index`` is zero-arg, creates the derived table with
the agreed columns, and is idempotent (running it twice is a no-op).
"""

import sqlite3
from pathlib import Path

import pytest

from cli_agent_orchestrator.clients.database import _migrate_workflow_index


@pytest.fixture
def patched_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    return db_path


def _columns(db_path: Path) -> dict:
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute("PRAGMA table_info(workflow_index)").fetchall()
    # PRAGMA returns (cid, name, type, notnull, dflt_value, pk).
    return {r[1]: r for r in rows}


def test_creates_table_with_expected_columns(patched_db):
    _migrate_workflow_index()
    cols = _columns(patched_db)
    assert set(cols) == {
        "name",
        "source_path",
        "mode",
        "step_count",
        "description",
        "indexed_at",
    }
    # name is the primary key.
    assert cols["name"][5] == 1
    # NOT NULL on the required columns.
    assert cols["source_path"][3] == 1
    assert cols["mode"][3] == 1
    # step_count is additively widened to NULLABLE (U5, A2/BR-4): script-tier
    # rows carry NULL — step count is run-time-determined, unknowable at
    # index time; YAML rows keep populating an int (unchanged write path).
    assert cols["step_count"][3] == 0


def test_is_idempotent(patched_db):
    _migrate_workflow_index()
    # Insert a row, then re-run the migration — it must NOT drop/recreate.
    with sqlite3.connect(str(patched_db)) as conn:
        conn.execute(
            "INSERT INTO workflow_index "
            "(name, source_path, mode, step_count, description, indexed_at) "
            "VALUES ('w', '/p/w.yaml', 'sequential', 1, '', '2026-01-01T00:00:00Z')"
        )
        conn.commit()
    _migrate_workflow_index()  # second run
    with sqlite3.connect(str(patched_db)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM workflow_index").fetchone()[0]
    assert count == 1


def test_zero_arg_callable(patched_db):
    # Calling with no arguments must succeed (NB-1: zero-arg, self-connecting).
    _migrate_workflow_index()


def test_widens_legacy_not_null_step_count(patched_db):
    # Simulate a pre-U5 installed DB: workflow_index exists with step_count
    # NOT NULL (the original schema, before script-tier NULL rows existed).
    with sqlite3.connect(str(patched_db)) as conn:
        conn.execute(
            "CREATE TABLE workflow_index ("
            "name TEXT PRIMARY KEY, "
            "source_path TEXT NOT NULL, "
            "mode TEXT NOT NULL, "
            "step_count INTEGER NOT NULL, "
            "description TEXT NOT NULL DEFAULT '', "
            "indexed_at TEXT NOT NULL"
            ")"
        )
        conn.execute(
            "INSERT INTO workflow_index "
            "(name, source_path, mode, step_count, description, indexed_at) "
            "VALUES ('w', '/p/w.yaml', 'sequential', 1, '', '2026-01-01T00:00:00Z')"
        )
        conn.commit()

    _migrate_workflow_index()

    # The table is fully derived/rebuildable, so the migration drops and
    # recreates it rather than migrating rows in place — assert that
    # semantics: step_count is now nullable, and the legacy row did not
    # survive (the caller's next `list` rebuilds the index from files).
    cols = _columns(patched_db)
    assert cols["step_count"][3] == 0
    with sqlite3.connect(str(patched_db)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM workflow_index").fetchone()[0]
    assert count == 0

    # A NULL step_count row (script-tier) must now insert cleanly.
    with sqlite3.connect(str(patched_db)) as conn:
        conn.execute(
            "INSERT INTO workflow_index "
            "(name, source_path, mode, step_count, description, indexed_at) "
            "VALUES ('script-wf', '/p/w.py', 'script', NULL, '', '2026-01-01T00:00:00Z')"
        )
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM workflow_index").fetchone()[0]
    assert count == 1


def test_widen_migration_logs_when_it_fires(patched_db, caplog):
    with sqlite3.connect(str(patched_db)) as conn:
        conn.execute(
            "CREATE TABLE workflow_index ("
            "name TEXT PRIMARY KEY, "
            "source_path TEXT NOT NULL, "
            "mode TEXT NOT NULL, "
            "step_count INTEGER NOT NULL, "
            "description TEXT NOT NULL DEFAULT '', "
            "indexed_at TEXT NOT NULL"
            ")"
        )
        conn.commit()

    with caplog.at_level("INFO", logger="cli_agent_orchestrator.clients.database"):
        _migrate_workflow_index()

    assert any("rebuilt workflow_index" in record.message for record in caplog.records)

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
    assert cols["step_count"][3] == 1


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

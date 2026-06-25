"""Tests for the workflow_run / workflow_run_step migrations (issue #312, Bolt 4 / N6).

Asserts ``_migrate_workflow_run`` and ``_migrate_workflow_run_step`` are zero-arg,
self-connecting, create the durable tables with the agreed E1/E2 columns, and are
idempotent (running twice is a no-op that preserves existing rows). NO loop columns
ship (Q4=B / B4-BR-12).
"""

import sqlite3
from pathlib import Path

import pytest

from cli_agent_orchestrator.clients.database import (
    _migrate_workflow_run,
    _migrate_workflow_run_step,
)


@pytest.fixture
def patched_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    return db_path


def _columns(db_path: Path, table: str) -> dict:
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1]: r for r in rows}  # (cid, name, type, notnull, dflt_value, pk)


def test_workflow_run_columns(patched_db):
    _migrate_workflow_run()
    cols = _columns(patched_db, "workflow_run")
    assert set(cols) == {
        "run_id",
        "workflow_name",
        "spec_snapshot",
        "inputs_json",
        "state",
        "current_step_id",
        "started_at",
        "finished_at",
    }
    # run_id is the primary key; the nullable columns are current_step_id/finished_at.
    assert cols["run_id"][5] == 1
    assert cols["workflow_name"][3] == 1
    assert cols["spec_snapshot"][3] == 1
    assert cols["current_step_id"][3] == 0
    assert cols["finished_at"][3] == 0


def test_workflow_run_no_loop_columns(patched_db):
    # B4-BR-12 / Q4=B: NO loop columns ship in N6 (they are N8's additive migration).
    _migrate_workflow_run()
    cols = _columns(patched_db, "workflow_run")
    assert "iteration_counter" not in cols
    assert "which_guard_fired" not in cols
    assert "iterations_run" not in cols


def test_workflow_run_step_columns(patched_db):
    _migrate_workflow_run_step()
    cols = _columns(patched_db, "workflow_run_step")
    assert set(cols) == {
        "run_id",
        "step_id",
        "state",
        "attempts",
        "output_json",
        "error",
        "updated_at",
    }
    # Composite PRIMARY KEY (run_id, step_id): both carry pk>0.
    assert cols["run_id"][5] > 0
    assert cols["step_id"][5] > 0
    # reprompted / terminal_id are deliberately NOT journaled (F3).
    assert "reprompted" not in cols
    assert "terminal_id" not in cols


def test_migrations_are_idempotent(patched_db):
    _migrate_workflow_run()
    _migrate_workflow_run_step()
    with sqlite3.connect(str(patched_db)) as conn:
        conn.execute(
            "INSERT INTO workflow_run "
            "(run_id, workflow_name, spec_snapshot, inputs_json, state, started_at) "
            "VALUES ('r1', 'wf', '{}', '{}', 'running', '2026-01-01T00:00:00Z')"
        )
        conn.commit()
    # Second run must NOT drop/recreate the table.
    _migrate_workflow_run()
    _migrate_workflow_run_step()
    with sqlite3.connect(str(patched_db)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM workflow_run").fetchone()[0]
    assert count == 1


def test_zero_arg_callables(patched_db):
    # NB-1: both migrators are zero-arg, self-connecting.
    _migrate_workflow_run()
    _migrate_workflow_run_step()

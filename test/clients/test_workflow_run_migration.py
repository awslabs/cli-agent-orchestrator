"""Tests for the workflow_run / workflow_run_step migrations (issue #312, Bolt 4 / N6).

Asserts ``_migrate_workflow_run`` and ``_migrate_workflow_run_step`` are zero-arg,
self-connecting, create the durable tables with the agreed E1/E2 columns, and are
idempotent (running twice is a no-op that preserves existing rows). NO loop columns
ship (Q4=B / B4-BR-12).

U3 (issue #312, script-tier journal extension, C3) additively appends
``tier``/``generation`` to ``workflow_run`` and ``call_fingerprint`` to
``workflow_run_step`` (domain-entities E1/E2). The column-set assertions below
are updated to include them; the defaults (``tier='yaml'``, ``generation='1'``,
``call_fingerprint=NULL``) preserve a pre-U3/YAML row's observable shape
(INV-1/INV-2).
"""

import sqlite3
from pathlib import Path

import pytest

from cli_agent_orchestrator.clients.database import (
    _migrate_workflow_run,
    _migrate_workflow_run_indexes,
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


def _index_names(db_path: Path) -> set:
    """User-defined index names on the DB (excludes SQLite's implicit autoindexes)."""
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    return {r[0] for r in rows if not r[0].startswith("sqlite_")}


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
        "tier",
        "generation",
    }
    # run_id is the primary key; the nullable columns are current_step_id/finished_at.
    assert cols["run_id"][5] == 1
    assert cols["workflow_name"][3] == 1
    assert cols["spec_snapshot"][3] == 1
    assert cols["current_step_id"][3] == 0
    assert cols["finished_at"][3] == 0
    # U3 additive columns (E1): tier/generation default to the YAML-preserving values.
    assert cols["tier"][4] == "'yaml'"
    assert cols["generation"][4] == "'1'"


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
        "call_fingerprint",
    }
    # Composite PRIMARY KEY (run_id, step_id): both carry pk>0.
    assert cols["run_id"][5] > 0
    assert cols["step_id"][5] > 0
    # reprompted / terminal_id are deliberately NOT journaled (F3).
    assert "reprompted" not in cols
    assert "terminal_id" not in cols
    # U3 additive column (E2): defaults to NULL (INV-2). PRAGMA table_info reports
    # the literal default expression as the string "NULL", not Python None.
    assert cols["call_fingerprint"][4] == "NULL"


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


# ---------------------------------------------------------------------------
# U1 (issue #505, journal-list-and-indexes) — _migrate_workflow_run_indexes.
# Two single-column indexes on workflow_run (ADR-6): additive, idempotent, and
# column-preserving (indexes are not columns, so the C-4 assertion is untouched).
# ---------------------------------------------------------------------------
def test_workflow_run_indexes_created(patched_db):
    # IR-1: after the base table + index migrator run, both single-column
    # indexes are present in sqlite_master.
    _migrate_workflow_run()
    _migrate_workflow_run_indexes()
    names = _index_names(patched_db)
    assert "idx_workflow_run_started_at" in names
    assert "idx_workflow_run_state" in names


def test_workflow_run_indexes_idempotent(patched_db):
    # IR-2: running the index migrator twice does not raise and leaves exactly
    # the two indexes (CREATE INDEX IF NOT EXISTS, additive-only).
    _migrate_workflow_run()
    _migrate_workflow_run_indexes()
    _migrate_workflow_run_indexes()  # second run must be a no-op
    names = _index_names(patched_db)
    assert names == {"idx_workflow_run_started_at", "idx_workflow_run_state"}


def test_workflow_run_indexes_do_not_change_columns(patched_db):
    # IR-4 / C-4 guard: the migrator creates only indexes, never columns. The
    # exact-column set (mirrored from test_workflow_run_columns) must be
    # unchanged after the index migrator runs — PRAGMA table_info reports no
    # index as a column.
    _migrate_workflow_run()
    before = set(_columns(patched_db, "workflow_run"))
    _migrate_workflow_run_indexes()
    after = set(_columns(patched_db, "workflow_run"))
    assert after == before
    assert after == {
        "run_id",
        "workflow_name",
        "spec_snapshot",
        "inputs_json",
        "state",
        "current_step_id",
        "started_at",
        "finished_at",
        "tier",
        "generation",
    }


def test_workflow_run_indexes_failure_is_logged_not_raised(
    patched_db, monkeypatch: pytest.MonkeyPatch
):
    # IR-3: a migrate failure is swallowed (logged at debug), never propagated —
    # a missing index degrades to a table scan, not a crash. Simulate by making
    # sqlite3.connect raise. The migrator does a local ``import sqlite3``, which
    # resolves the same shared module object, so patching connect on it here also
    # affects the migrator's call.
    def _boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("simulated connect failure")

    monkeypatch.setattr(sqlite3, "connect", _boom, raising=True)
    # Must return without raising.
    _migrate_workflow_run_indexes()

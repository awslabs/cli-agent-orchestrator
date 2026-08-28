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

U1 (issue #504, event-log substrate) additively adds the append-only
``workflow_run_event`` table (21 columns, PK ``(run_id, seq)``), the
``workflow_run_seq`` high-water table, and three nullable
``workflow_run_step`` columns (``terminal_id``/``reprompted``/``error_kind``).
The tests below are EXTENDED (never deleted, C-1): the new tables get their own
column-set assertions, the step-columns test gains the three additive columns,
and the idempotency test exercises the two new migrators.
"""

import sqlite3
from pathlib import Path

import pytest

from cli_agent_orchestrator.clients.database import (
    _migrate_workflow_run,
    _migrate_workflow_run_event,
    _migrate_workflow_run_indexes,
    _migrate_workflow_run_seq,
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
        "manifest_json",
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
    # manifest-column additive column (issue #583 Bolt 2, ADR-583-12): one TEXT column
    # defaulting to NULL, so every pre-Bolt-2 row reads as "manifest absent".
    #
    # THIS ASSERTION IS THE UNIT'S WHOLE MITIGATION FOR THE SILENT-MIGRATION RISK, not a
    # routine column check. ``_migrate_workflow_run``'s body is wrapped in
    # ``except Exception`` -> ``logger.debug``, so a failed ALTER leaves the column absent
    # with no visible signal; the symptom then appears at the Bolt 2 approval gate, which
    # reads an absent manifest as "never approved" and refuses the run — arbitrarily far
    # from the cause. Asserting on a FRESH database is what turns that from assumed into
    # verified. Do not delete it as redundant with the column-set assertion above: the set
    # proves presence, these two prove the shape a reader depends on.
    assert cols["manifest_json"][2] == "TEXT"
    assert cols["manifest_json"][4] == "NULL"


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
    # U1 (issue #504) additively adds terminal_id / reprompted / error_kind, so
    # they now belong in the column set alongside the U3 call_fingerprint.
    assert set(cols) == {
        "run_id",
        "step_id",
        "state",
        "attempts",
        "output_json",
        "error",
        "updated_at",
        "call_fingerprint",
        "terminal_id",
        "reprompted",
        "error_kind",
        "result_json",
    }
    # Composite PRIMARY KEY (run_id, step_id): both carry pk>0.
    assert cols["run_id"][5] > 0
    assert cols["step_id"][5] > 0
    # MERGE NOTE (2026-08-17, #583 x #504). #583 asserted here that ``reprompted`` and
    # ``terminal_id`` were deliberately NOT journaled (F3) and that the COLUMNS did not
    # exist. #504 then ADDED both columns plus ``error_kind``, so those two assertions
    # became FALSE and are removed rather than reconciled — this is the one place in the
    # merge where the two changes genuinely disagreed instead of both appending. The
    # #583 point that survives: ``terminal_id`` is ALSO carried inside the ``result_json``
    # envelope, so the value now exists in two places and a reader must not assume the
    # column and the envelope field are the same write.
    # U3 additive column (E2): defaults to NULL (INV-2). PRAGMA table_info reports
    # the literal default expression as the string "NULL", not Python None.
    assert cols["call_fingerprint"][4] == "NULL"
    # U1 additive columns (issue #504): all three nullable, defaulting to NULL so
    # a pre-U1 row reads back observably identical to its pre-extension shape.
    assert cols["terminal_id"][3] == 0
    assert cols["reprompted"][3] == 0
    assert cols["error_kind"][3] == 0
    assert cols["terminal_id"][4] == "NULL"
    assert cols["reprompted"][4] == "NULL"
    assert cols["error_kind"][4] == "NULL"
    # result-envelope additive column (issue #583, BR-7/BR-10): one TEXT column defaulting
    # to NULL, so every pre-#583 row reads as "envelope absent".
    assert cols["result_json"][4] == "NULL"
    assert cols["result_json"][2] == "TEXT"


def test_workflow_run_event_columns(patched_db):
    # U1 (issue #504): the append-only event table carries the 21 ADR-1 columns,
    # a composite PK (run_id, seq), and NOT NULL on the five required columns.
    _migrate_workflow_run_event()
    cols = _columns(patched_db, "workflow_run_event")
    assert set(cols) == {
        "run_id",
        "seq",
        "event_type",
        "event_schema_version",
        "ts",
        "step_id",
        "attempt",
        "state",
        "elapsed_ms",
        "provider",
        "agent_profile",
        "engine",
        "terminal_id",
        "terminal_offset_start",
        "terminal_offset_len",
        "error_kind",
        "reason",
        "validation_result",
        "output_ref",
        "iteration",
        "which_guard_fired",
    }
    # Composite PRIMARY KEY (run_id, seq): both carry pk>0.
    assert cols["run_id"][5] > 0
    assert cols["seq"][5] > 0
    # The five required columns are NOT NULL; ts is display-only but still NN.
    assert cols["run_id"][3] == 1
    assert cols["seq"][3] == 1
    assert cols["event_type"][3] == 1
    assert cols["event_schema_version"][3] == 1
    assert cols["ts"][3] == 1
    # A representative optional column is nullable, and the RESERVED loop fields
    # (FR-1.5) ship as columns but stay nullable (NULL in the MVP).
    assert cols["step_id"][3] == 0
    assert cols["iteration"][3] == 0
    assert cols["which_guard_fired"][3] == 0


def test_workflow_run_seq_columns(patched_db):
    # U1 (issue #504): the high-water table keys on run_id with a NOT NULL
    # high_water counter.
    _migrate_workflow_run_seq()
    cols = _columns(patched_db, "workflow_run_seq")
    assert set(cols) == {"run_id", "high_water"}
    assert cols["run_id"][5] == 1  # PRIMARY KEY
    assert cols["high_water"][3] == 1  # NOT NULL


def test_migrations_are_idempotent(patched_db):
    _migrate_workflow_run()
    _migrate_workflow_run_step()
    _migrate_workflow_run_event()
    _migrate_workflow_run_seq()
    with sqlite3.connect(str(patched_db)) as conn:
        conn.execute(
            "INSERT INTO workflow_run "
            "(run_id, workflow_name, spec_snapshot, inputs_json, state, started_at) "
            "VALUES ('r1', 'wf', '{}', '{}', 'running', '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO workflow_run_event "
            "(run_id, seq, event_type, event_schema_version, ts) "
            "VALUES ('r1', 1, 'run.started', 1, '2026-01-01T00:00:00Z')"
        )
        conn.execute("INSERT INTO workflow_run_seq (run_id, high_water) VALUES ('r1', 1)")
        conn.commit()
    # Second run must NOT drop/recreate the tables (U1 event/seq migrators too).
    _migrate_workflow_run()
    _migrate_workflow_run_step()
    _migrate_workflow_run_event()
    _migrate_workflow_run_seq()
    with sqlite3.connect(str(patched_db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM workflow_run").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM workflow_run_event").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM workflow_run_seq").fetchone()[0] == 1


def test_zero_arg_callables(patched_db):
    # NB-1: all migrators are zero-arg, self-connecting.
    _migrate_workflow_run()
    _migrate_workflow_run_step()
    _migrate_workflow_run_event()
    _migrate_workflow_run_seq()
    _migrate_workflow_run_indexes()


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
        # manifest-column (issue #583 Bolt 2): this set is MIRRORED from
        # test_workflow_run_columns, so an additive column has to be added in both places.
        # There is a third mirror in production code —
        # workflow_journal._REQUIRED_RUN_COLUMNS — guarded by its own equality assertion in
        # test_workflow_journal_connection_posture.py. Three places, one column set.
        "manifest_json",
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

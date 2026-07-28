"""Tests for U2 ``insert_run_with_steps`` — the atomic durable insert (issue #505, TR-1).

The async submission path (``POST /workflows/runs:submit``) needs the run row and
its seeded step rows to be durable **together** before it acks a run with 202 (the
``run-id-allocated-before-ack`` invariant). ``insert_run_with_steps`` composes the
run INSERT and the step-seed INSERT into ONE transaction (one commit).

Each test maps to a business rule:

- TR-1 (durable insert is atomic): the MANDATED crash-recovery scenario — a REAL
  ``sqlite3.Error`` raised from the step INSERT *after* the run INSERT within the
  same transaction leaves NO row in ``workflow_run`` (rollback of both), and the
  error propagates (it is NOT swallowed, unlike the engine's best-effort write).
- Happy path: the run row + every seeded step row are durable after one call.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cli_agent_orchestrator.clients.database import (
    _migrate_workflow_run,
    _migrate_workflow_run_step,
)
from cli_agent_orchestrator.services import workflow_journal
from cli_agent_orchestrator.services.workflow_journal import insert_run_with_steps


@pytest.fixture(autouse=True)
def _patched_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the journal at a temp DB and create the run tables (real SQLite)."""
    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    _migrate_workflow_run()
    _migrate_workflow_run_step()
    yield db_path


def test_insert_run_with_steps_happy_run_and_steps_durable():
    insert_run_with_steps(
        run_id="run-atomic-ok",
        workflow_name="wf",
        spec_snapshot="{}",
        inputs_json="{}",
        state="running",
        started_at="2026-07-27T00:00:00Z",
        steps=[("s1", "pending"), ("s2", "pending")],
        updated_at="2026-07-27T00:00:00Z",
    )
    row = workflow_journal.get_run("run-atomic-ok")
    assert row is not None
    assert row.state == "running"
    assert row.tier == "yaml"
    assert row.generation == "1"
    steps = workflow_journal.get_steps("run-atomic-ok")
    assert {s.step_id for s in steps} == {"s1", "s2"}
    assert all(s.state == "pending" for s in steps)


def test_tr1_step_insert_crash_rolls_back_the_run_row():
    """TR-1 (MANDATED real crash): a genuine ``sqlite3.Error`` from the step INSERT,
    raised AFTER the run INSERT within the SAME transaction, must leave NO row in
    ``workflow_run`` — the whole transaction rolls back and NEITHER row is
    committed. This is a REAL crash between the two inserts (a NOT NULL constraint
    violation on the step's ``state``), not a mocked return value.

    Without the single-transaction wrapper (i.e. calling ``insert_run`` then
    ``insert_steps`` back-to-back), the run INSERT would already have autocommitted
    a phantom RUNNING row with no step rows.
    """
    with pytest.raises(sqlite3.Error):
        insert_run_with_steps(
            run_id="run-atomic-crash",
            workflow_name="wf",
            spec_snapshot="{}",
            inputs_json="{}",
            state="running",
            started_at="2026-07-27T00:00:00Z",
            # The step's state is None -> the second INSERT (executemany) violates
            # the NOT NULL constraint on workflow_run_step.state and raises AFTER
            # the run INSERT already executed within the same (uncommitted) txn.
            steps=[("s1", None)],  # type: ignore[list-item]
            updated_at="2026-07-27T00:00:00Z",
        )
    # Rollback left NEITHER row: no phantom RUNNING run visible to list/status.
    assert workflow_journal.get_run("run-atomic-crash") is None
    assert workflow_journal.get_steps("run-atomic-crash") == []


def test_tr1_error_propagates_not_swallowed():
    """The atomic insert re-raises on failure (hard precondition of the async ack),
    in deliberate contrast to the engine's best-effort, swallowed write-through."""
    raised = False
    try:
        insert_run_with_steps(
            run_id="run-atomic-raise",
            workflow_name="wf",
            spec_snapshot="{}",
            inputs_json="{}",
            state="running",
            started_at="2026-07-27T00:00:00Z",
            steps=[("s1", None)],  # type: ignore[list-item]
            updated_at="2026-07-27T00:00:00Z",
        )
    except sqlite3.Error:
        raised = True
    assert raised is True
    assert workflow_journal.get_run("run-atomic-raise") is None

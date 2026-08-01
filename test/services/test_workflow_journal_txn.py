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


class TestSettleRunStateIfRunning:
    """PR #525 review — the conditional terminal-state write behind the background
    drive's FAILED backstop.

    The backstop exists so a scheduling bug cannot orphan a run in ``running``
    forever. Written unconditionally it ALSO overwrote a run the engine had already
    settled, so a drive that raised during post-settlement bookkeeping turned a true
    ``completed``/``cancelled`` into a false ``failed`` — a wrong terminal state is
    worse than a visibly-stuck one, because it is indistinguishable from a real one.
    """

    def _seed(self, run_id: str, state: str) -> None:
        workflow_journal.insert_run(
            run_id,
            "wf",
            "{}",
            "{}",
            state,
            "2026-08-01T00:00:00Z",
        )

    def test_settles_a_running_row_and_reports_true(self):
        """Happy path: while the row is still ``running`` the write lands."""
        self._seed("run-guard-running", "running")
        settled = workflow_journal.settle_run_state_if_running(
            "run-guard-running", "failed", "2026-08-01T00:05:00Z"
        )
        assert settled is True
        row = workflow_journal.get_run("run-guard-running")
        assert row is not None
        assert row.state == "failed"
        assert row.finished_at == "2026-08-01T00:05:00Z"

    def test_refuses_to_overwrite_a_completed_row(self):
        """THE finding-1 guard: a settled COMPLETED row is never downgraded to FAILED.

        MUTATION PROOF: drop the ``AND state = ?`` clause from
        ``settle_run_state_if_running`` and this fails on ``row.state``.
        """
        self._seed("run-guard-completed", "completed")
        settled = workflow_journal.settle_run_state_if_running(
            "run-guard-completed", "failed", "2026-08-01T00:05:00Z"
        )
        assert settled is False
        row = workflow_journal.get_run("run-guard-completed")
        assert row is not None
        assert row.state == "completed"

    def test_refuses_to_overwrite_a_cancelled_row(self):
        """The ``CancelledError``-arm case: a journalled CANCELLED survives shutdown."""
        self._seed("run-guard-cancelled", "cancelled")
        settled = workflow_journal.settle_run_state_if_running(
            "run-guard-cancelled", "failed", "2026-08-01T00:05:00Z"
        )
        assert settled is False
        row = workflow_journal.get_run("run-guard-cancelled")
        assert row is not None
        assert row.state == "cancelled"

    def test_absent_row_reports_false_and_does_not_insert(self):
        """A missing run is a no-op, not an upsert — the backstop must not mint rows."""
        settled = workflow_journal.settle_run_state_if_running(
            "run-guard-absent", "failed", "2026-08-01T00:05:00Z"
        )
        assert settled is False
        assert workflow_journal.get_run("run-guard-absent") is None

    def test_update_run_state_still_reopens_a_settled_run(self):
        """REGRESSION GUARD for the guard itself: ``update_run_state`` must stay
        UNCONDITIONAL.

        The resume path (``script_runner.resume_script_run``,
        ``workflow_service.resume_from_last_completed``) calls ``update_run_state`` to
        write state BACK to ``running`` on an already-terminal row. Pushing the
        backstop's ``WHERE state = 'running'`` predicate into that shared function —
        the obvious "tidy-up" — would silently make EVERY resume a no-op, turning this
        data-integrity fix into a worse data-integrity bug on a path the review never
        raised. This test fails the moment someone does that.
        """
        self._seed("run-reopen", "completed")
        workflow_journal.update_run_state("run-reopen", "running", None)
        row = workflow_journal.get_run("run-reopen")
        assert row is not None
        assert row.state == "running"
        assert row.finished_at is None

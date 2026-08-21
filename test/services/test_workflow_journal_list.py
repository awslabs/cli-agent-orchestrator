"""Tests for U1 ``list_runs`` + ``RunSummaryRow`` (issue #505, journal-list-and-indexes).

Exercises the DAL list primitive against a temp SQLite DB (the same patched
``DATABASE_FILE`` fixture pattern as ``test_script_journal_extension.py`` /
``test_workflow_journal_resume.py``). Each test maps to a U1 business rule:

- QR-3 (total-order ordering): newest-first, ``run_id DESC`` tiebreak on a
  same-second ``started_at`` collision, deterministic across repeated calls.
- QR-2 / LR-2 (WHERE only when filtering; unknown state is empty): state filter
  returns only matching rows, no state returns every state, an unmatched state
  returns ``[]``.
- LR-3 / LR-1 (empty is valid; limit/offset paging): empty table returns ``[]``;
  the limit clamp ([1, 500]) and offset paging behave.
- ER-1 (raise, never swallow): a simulated DB error surfaces as ``sqlite3.Error``,
  NOT an empty list.
- QR-1 (parameterized only): a state value carrying SQL metacharacters binds as a
  literal — no injection, table survives.
- The narrow projection: ``RunSummaryRow`` carries exactly the seven summary
  fields (no ``spec_snapshot`` / ``inputs_json``).
"""

from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path

import pytest

from cli_agent_orchestrator.clients.database import (
    _migrate_workflow_run,
    _migrate_workflow_run_step,
)
from cli_agent_orchestrator.services import workflow_journal
from cli_agent_orchestrator.services.workflow_journal import RunSummaryRow, list_runs


@pytest.fixture(autouse=True)
def _patched_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the journal at a temp DB and create the run tables."""
    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    _migrate_workflow_run()
    _migrate_workflow_run_step()
    yield db_path


def _seed(run_id: str, *, state: str = "running", started_at: str, tier: str = "yaml") -> None:
    """Insert one workflow_run row via the real DAL, then set its tier."""
    workflow_journal.insert_run(
        run_id=run_id,
        workflow_name="wf",
        spec_snapshot="{}",
        inputs_json="{}",
        state=state,
        started_at=started_at,
        tier=tier,
    )


# ---------------------------------------------------------------------------
# QR-3 — total-order ordering (started_at DESC, run_id DESC)
# ---------------------------------------------------------------------------
def test_list_runs_newest_first_with_run_id_tiebreak():
    # Two rows share the SAME whole-second started_at -> run_id DESC must decide.
    _seed("run-a", started_at="2026-07-27T00:00:00Z")
    _seed("run-b", started_at="2026-07-27T00:00:00Z")
    _seed("run-c", started_at="2026-07-27T00:00:05Z")  # later second -> first

    rows = list_runs()
    ids = [r.run_id for r in rows]
    # run-c is newest by started_at; run-b before run-a by run_id DESC tiebreak.
    assert ids == ["run-c", "run-b", "run-a"]


def test_list_runs_same_second_order_is_deterministic_across_calls():
    _seed("run-1", started_at="2026-07-27T00:00:00Z")
    _seed("run-2", started_at="2026-07-27T00:00:00Z")
    _seed("run-3", started_at="2026-07-27T00:00:00Z")

    first = [r.run_id for r in list_runs()]
    second = [r.run_id for r in list_runs()]
    assert first == second == ["run-3", "run-2", "run-1"]


# ---------------------------------------------------------------------------
# QR-2 / LR-2 — WHERE only when filtering; unknown state is empty
# ---------------------------------------------------------------------------
def test_list_runs_state_filter_returns_only_matching():
    _seed("run-r1", state="running", started_at="2026-07-27T00:00:01Z")
    _seed("run-r2", state="running", started_at="2026-07-27T00:00:02Z")
    _seed("run-c1", state="completed", started_at="2026-07-27T00:00:03Z")

    running = list_runs(state="running")
    assert {r.run_id for r in running} == {"run-r1", "run-r2"}
    assert all(r.state == "running" for r in running)


def test_list_runs_no_state_returns_every_state():
    _seed("run-r1", state="running", started_at="2026-07-27T00:00:01Z")
    _seed("run-c1", state="completed", started_at="2026-07-27T00:00:02Z")
    _seed("run-f1", state="failed", started_at="2026-07-27T00:00:03Z")

    states = {r.state for r in list_runs()}
    assert states == {"running", "completed", "failed"}


def test_list_runs_unknown_state_returns_empty_not_error():
    _seed("run-r1", state="running", started_at="2026-07-27T00:00:01Z")
    # A well-formed but unmatched state string is a valid empty answer (LR-2).
    assert list_runs(state="bogus") == []


# ---------------------------------------------------------------------------
# LR-3 / LR-1 — empty is valid; limit clamp + offset paging
# ---------------------------------------------------------------------------
def test_list_runs_empty_table_returns_empty_list():
    assert list_runs() == []


def test_list_runs_limit_clamped_low_to_one():
    for i in range(3):
        _seed(f"run-{i}", started_at=f"2026-07-27T00:00:0{i}Z")
    # limit=0 clamps up to 1 -> exactly one row (the newest).
    rows = list_runs(limit=0)
    assert len(rows) == 1
    assert rows[0].run_id == "run-2"


def test_list_runs_limit_clamped_high_to_five_hundred(_patched_journal):
    # A limit above the cap of 500 must return at most 500 rows. Seed 501 rows
    # (via one raw connection for speed) so the clamp is observable directly on
    # the returned row count: 1000 would return all 501 if the clamp were absent.
    with sqlite3.connect(str(_patched_journal)) as conn:
        conn.executemany(
            "INSERT INTO workflow_run "
            "(run_id, workflow_name, spec_snapshot, inputs_json, state, "
            " current_step_id, started_at, finished_at, tier, generation) "
            "VALUES (?, 'wf', '{}', '{}', 'running', NULL, ?, NULL, 'yaml', '1')",
            [(f"run-{i:04d}", f"2026-07-27T00:00:{i % 60:02d}Z") for i in range(501)],
        )
        conn.commit()

    rows = list_runs(limit=1000)
    assert len(rows) == 500


def test_list_runs_offset_paging():
    for i in range(4):
        _seed(f"run-{i}", started_at=f"2026-07-27T00:00:0{i}Z")
    # Newest-first: run-3, run-2, run-1, run-0.
    page1 = list_runs(limit=2)
    page2 = list_runs(limit=2, offset=2)
    assert [r.run_id for r in page1] == ["run-3", "run-2"]
    assert [r.run_id for r in page2] == ["run-1", "run-0"]


def test_list_runs_negative_offset_floored_to_zero():
    for i in range(3):
        _seed(f"run-{i}", started_at=f"2026-07-27T00:00:0{i}Z")
    # A negative offset would be an invalid SQLite OFFSET; the floor keeps it 0.
    assert [r.run_id for r in list_runs(offset=-5)] == ["run-2", "run-1", "run-0"]


# ---------------------------------------------------------------------------
# ER-1 — raise, never swallow
# ---------------------------------------------------------------------------
def test_list_runs_db_error_propagates_not_swallowed(monkeypatch: pytest.MonkeyPatch):
    # A DB failure must surface as sqlite3.Error, NOT a silent empty list — the
    # deliberate opposite of the migrator's best-effort posture.
    def _boom():
        raise sqlite3.OperationalError("simulated connect failure")

    monkeypatch.setattr(workflow_journal, "_connect", _boom, raising=True)
    with pytest.raises(sqlite3.Error):
        list_runs()


# ---------------------------------------------------------------------------
# QR-1 — parameterized only (no injection)
# ---------------------------------------------------------------------------
def test_list_runs_state_metacharacters_bind_as_literal():
    _seed("run-r1", state="running", started_at="2026-07-27T00:00:01Z")
    injection = "running'; DROP TABLE workflow_run; --"
    # The metacharacters bind as one literal state value -> no match -> [].
    assert list_runs(state=injection) == []
    # And the table still exists (the injection did not execute).
    from cli_agent_orchestrator.constants import DATABASE_FILE

    with sqlite3.connect(str(DATABASE_FILE)) as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='workflow_run'"
        ).fetchone()
    assert row is not None


# ---------------------------------------------------------------------------
# Narrow projection — RunSummaryRow carries exactly the 7 summary fields
# ---------------------------------------------------------------------------
def test_run_summary_row_has_exactly_seven_narrow_fields():
    field_names = {f.name for f in dataclasses.fields(RunSummaryRow)}
    assert field_names == {
        "run_id",
        "workflow_name",
        "state",
        "tier",
        "started_at",
        "finished_at",
        "current_step_id",
    }
    # The large payload columns are deliberately excluded from the projection.
    assert "spec_snapshot" not in field_names
    assert "inputs_json" not in field_names
    assert "generation" not in field_names


def test_list_runs_maps_columns_into_summary_row():
    _seed("run-x", state="completed", started_at="2026-07-27T00:00:00Z", tier="script")
    # Give it a terminal finished_at + a current step to exercise the full map.
    workflow_journal.update_run_state("run-x", "completed", "2026-07-27T00:01:00Z")
    workflow_journal.update_run_current_step("run-x", "step-7")

    (row,) = list_runs()
    assert isinstance(row, RunSummaryRow)
    assert row.run_id == "run-x"
    assert row.workflow_name == "wf"
    assert row.state == "completed"
    assert row.tier == "script"
    assert row.started_at == "2026-07-27T00:00:00Z"
    assert row.finished_at == "2026-07-27T00:01:00Z"
    assert row.current_step_id == "step-7"

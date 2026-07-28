"""Tests for the U1 event-log substrate DAL (issue #504).

Covers the load-bearing behavior from
``construction/U1-event-log-substrate/functional-design``:

- append -> read round-trip: every event field survives a durable write/read
  (BR-5, ADR-1 columns).
- ``read_events`` after-seq cursor slices the timeline (FR-5.2 replay cursor).
- gap synthesis (Algorithm 2, BR-4): a swallowed append leaves a hole that
  ``read_events_with_gaps`` DECLARES as a ``GapMarker`` without renumbering.
- ``persist_high_water`` monotonicity (BR-11) and the two rebuild re-seed terms
  ``persisted_high_water`` / ``max_event_seq`` returning 0 on an unknown run
  (BR-3 co-terms; U2 consumes them).
- ``append_event`` duplicate ``(run_id, seq)`` raises ``sqlite3.IntegrityError``
  rather than silently overwriting (BR-10).
- ``delete_run`` cascades across all four tables and is a no-op on an unknown id
  (FR-11 / NFR-SEC-5, BR-12).
- NFR-PERF-1-T: the event migrator runs at most once across N appends — a
  call-count assertion (NOT timing) proving the memoized ``_connect_event`` does
  not migrate per append (BR-7).

The journal points at a temp SQLite DB via the patched ``DATABASE_FILE``,
mirroring ``test_workflow_journal_resume.py``'s fixture pattern exactly.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import workflow_journal
from cli_agent_orchestrator.services.workflow_journal import EventRow, GapMarker


@pytest.fixture(autouse=True)
def _patched_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the journal at a fresh temp DB and reset the migration memo.

    ``_connect_event`` self-migrates on first use per (process, db-path), so no
    explicit migrator call is needed here. The module-level
    ``_event_migrated_paths`` set is cleared so a prior test's paths never leak
    into this one (each test gets a unique tmp path anyway).
    """
    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    workflow_journal._event_migrated_paths.clear()
    yield db_path
    workflow_journal._event_migrated_paths.clear()


def _all_fields_event(run_id: str, seq: int) -> dict:
    """A fully-populated event payload — every optional column carries a value."""
    return dict(
        event_schema_version=1,
        ts="2026-07-27T00:00:00Z",
        step_id="step-a",
        attempt=2,
        state="failed",
        elapsed_ms=1234,
        provider="kiro_cli",
        agent_profile="developer",
        engine="yaml",
        terminal_id="term-1",
        terminal_offset_start=100,
        terminal_offset_len=42,
        error_kind="timeout",
        reason="retry",
        validation_result="invalid",
        output_ref="run/step-a/attempt-2",
        iteration=None,
        which_guard_fired=None,
    )


# ---------------------------------------------------------------------------
# append -> read round-trip
# ---------------------------------------------------------------------------
def test_append_event_round_trip_preserves_every_field():
    fields = _all_fields_event("r1", 1)
    workflow_journal.append_event("r1", 1, "step.attempt.failed", **fields)

    rows = workflow_journal.read_events("r1")
    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, EventRow)
    assert row.run_id == "r1"
    assert row.seq == 1
    assert row.event_type == "step.attempt.failed"
    assert row.event_schema_version == 1
    assert row.ts == "2026-07-27T00:00:00Z"
    assert row.step_id == "step-a"
    assert row.attempt == 2
    assert row.state == "failed"
    assert row.elapsed_ms == 1234
    assert row.provider == "kiro_cli"
    assert row.agent_profile == "developer"
    assert row.engine == "yaml"
    assert row.terminal_id == "term-1"
    assert row.terminal_offset_start == 100
    assert row.terminal_offset_len == 42
    assert row.error_kind == "timeout"
    assert row.reason == "retry"
    assert row.validation_result == "invalid"
    assert row.output_ref == "run/step-a/attempt-2"
    assert row.iteration is None
    assert row.which_guard_fired is None


def test_append_event_minimal_optional_columns_default_none():
    workflow_journal.append_event(
        "r1", 1, "run.started", event_schema_version=1, ts="2026-07-27T00:00:00Z"
    )
    (row,) = workflow_journal.read_events("r1")
    assert row.event_type == "run.started"
    assert row.step_id is None
    assert row.provider is None
    assert row.output_ref is None


def test_read_events_ordered_by_seq_not_insertion_order():
    # seq is the sole ordering authority (BR-5); insert out of order.
    for seq in (3, 1, 2):
        workflow_journal.append_event(
            "r1", seq, "step.started", event_schema_version=1, ts="2026-07-27T00:00:00Z"
        )
    assert [r.seq for r in workflow_journal.read_events("r1")] == [1, 2, 3]


def test_read_events_after_seq_cursor_slices_timeline():
    for seq in (1, 2, 3):
        workflow_journal.append_event(
            "r1", seq, "step.started", event_schema_version=1, ts="2026-07-27T00:00:00Z"
        )
    assert [r.seq for r in workflow_journal.read_events("r1", after_seq=1)] == [2, 3]
    # after the last seq -> empty (a fully-caught-up follower)
    assert workflow_journal.read_events("r1", after_seq=3) == []


# ---------------------------------------------------------------------------
# gap synthesis (Algorithm 2, BR-4)
# ---------------------------------------------------------------------------
def test_read_events_with_gaps_declares_hole_without_renumbering():
    # append 1, 2, 4 (seq 3 was "swallowed") -> one declared gap, no renumber.
    for seq in (1, 2, 4):
        workflow_journal.append_event(
            "r1", seq, "step.started", event_schema_version=1, ts="2026-07-27T00:00:00Z"
        )
    rows, gaps = workflow_journal.read_events_with_gaps("r1")
    # rows keep their real seqs — the hole is NOT hidden by renumbering.
    assert [r.seq for r in rows] == [1, 2, 4]
    assert len(gaps) == 1
    gap = gaps[0]
    assert isinstance(gap, GapMarker)
    assert gap.after_seq == 2
    assert gap.before_seq == 4
    assert gap.missing_count == 1
    assert gap.reason == "append_failed"


def test_read_events_with_gaps_none_when_contiguous():
    for seq in (1, 2, 3):
        workflow_journal.append_event(
            "r1", seq, "step.started", event_schema_version=1, ts="2026-07-27T00:00:00Z"
        )
    rows, gaps = workflow_journal.read_events_with_gaps("r1")
    assert [r.seq for r in rows] == [1, 2, 3]
    assert gaps == []


def test_read_events_with_gaps_multi_missing_reports_count():
    # append 1, 5 -> a single gap spanning seqs 2,3,4 (missing_count 3).
    for seq in (1, 5):
        workflow_journal.append_event(
            "r1", seq, "step.started", event_schema_version=1, ts="2026-07-27T00:00:00Z"
        )
    _, gaps = workflow_journal.read_events_with_gaps("r1")
    assert len(gaps) == 1
    assert (gaps[0].after_seq, gaps[0].before_seq, gaps[0].missing_count) == (1, 5, 3)


def test_read_events_with_gaps_after_seq_cursor_detects_gap_against_cursor():
    # A follower resumed at after_seq=2; the next durable event is seq 5, so the
    # gap is measured from the CURSOR (2), not the first returned row.
    for seq in (1, 2, 5):
        workflow_journal.append_event(
            "r1", seq, "step.started", event_schema_version=1, ts="2026-07-27T00:00:00Z"
        )
    rows, gaps = workflow_journal.read_events_with_gaps("r1", after_seq=2)
    assert [r.seq for r in rows] == [5]
    assert len(gaps) == 1
    assert (gaps[0].after_seq, gaps[0].before_seq, gaps[0].missing_count) == (2, 5, 2)


# ---------------------------------------------------------------------------
# high-water monotonicity + rebuild re-seed terms (BR-3, BR-11)
# ---------------------------------------------------------------------------
def test_persist_high_water_is_monotonic():
    workflow_journal.persist_high_water("r1", 5)
    assert workflow_journal.persisted_high_water("r1") == 5
    # a lower seq NEVER lowers the high-water.
    workflow_journal.persist_high_water("r1", 3)
    assert workflow_journal.persisted_high_water("r1") == 5
    # a higher seq advances it.
    workflow_journal.persist_high_water("r1", 8)
    assert workflow_journal.persisted_high_water("r1") == 8


def test_reseed_terms_zero_on_unknown_run():
    assert workflow_journal.persisted_high_water("nope") == 0
    assert workflow_journal.max_event_seq("nope") == 0


def test_max_event_seq_tracks_largest_appended_seq():
    for seq in (1, 2, 4):
        workflow_journal.append_event(
            "r1", seq, "step.started", event_schema_version=1, ts="2026-07-27T00:00:00Z"
        )
    assert workflow_journal.max_event_seq("r1") == 4


def test_reseed_terms_degrade_to_zero_on_read_failure(monkeypatch: pytest.MonkeyPatch):
    # The rebuild re-seed terms must never raise into the rebuild path; a DB read
    # error degrades to 0 (BR-3 posture). Force _connect_event to raise.
    def _boom():
        raise sqlite3.OperationalError("simulated read failure")

    monkeypatch.setattr(workflow_journal, "_connect_event", _boom)
    assert workflow_journal.persisted_high_water("r1") == 0
    assert workflow_journal.max_event_seq("r1") == 0


# ---------------------------------------------------------------------------
# duplicate (run_id, seq) is an integrity error, not a silent overwrite (BR-10)
# ---------------------------------------------------------------------------
def test_append_event_duplicate_seq_raises_integrity_error():
    workflow_journal.append_event(
        "r1", 1, "run.started", event_schema_version=1, ts="2026-07-27T00:00:00Z"
    )
    with pytest.raises(sqlite3.IntegrityError):
        workflow_journal.append_event(
            "r1", 1, "run.started", event_schema_version=1, ts="2026-07-27T00:00:01Z"
        )
    # the original row is intact — no overwrite.
    (row,) = workflow_journal.read_events("r1")
    assert row.ts == "2026-07-27T00:00:00Z"


# ---------------------------------------------------------------------------
# per-run deletion cascade (FR-11 / NFR-SEC-5, BR-12)
# ---------------------------------------------------------------------------
def _seed_full_run(run_id: str) -> None:
    """Seed a run across all four tables the delete cascade owns."""
    workflow_journal.insert_run(
        run_id=run_id,
        workflow_name="wf",
        spec_snapshot="{}",
        inputs_json="{}",
        state="running",
        started_at="2026-07-27T00:00:00Z",
    )
    workflow_journal.insert_steps(run_id, [("step-a", "pending")], "2026-07-27T00:00:00Z")
    workflow_journal.append_event(
        run_id, 1, "run.started", event_schema_version=1, ts="2026-07-27T00:00:00Z"
    )
    workflow_journal.persist_high_water(run_id, 1)


def test_delete_run_cascades_across_all_four_tables():
    _seed_full_run("r1")
    # sanity: rows present before delete.
    assert workflow_journal.get_run("r1") is not None
    assert workflow_journal.get_steps("r1")
    assert workflow_journal.read_events("r1")
    assert workflow_journal.persisted_high_water("r1") == 1

    workflow_journal.delete_run("r1")

    assert workflow_journal.get_run("r1") is None
    assert workflow_journal.get_steps("r1") == []
    assert workflow_journal.read_events("r1") == []
    assert workflow_journal.persisted_high_water("r1") == 0
    assert workflow_journal.max_event_seq("r1") == 0


def test_delete_run_events_only_removes_events():
    _seed_full_run("r1")
    workflow_journal.delete_run_events("r1")
    assert workflow_journal.read_events("r1") == []
    # the run row + high-water survive delete_run_events (events-only cascade).
    assert workflow_journal.get_run("r1") is not None
    assert workflow_journal.persisted_high_water("r1") == 1


def test_delete_run_unknown_id_is_noop_not_error():
    # BR-12: deleting an absent run id must not raise and must not fault reads.
    workflow_journal.delete_run("never-existed")
    assert workflow_journal.get_run("never-existed") is None


# ---------------------------------------------------------------------------
# NFR-PERF-1-T: the event migrator runs at most once across N appends (BR-7).
# Call-count assertion — NEVER timing.
# ---------------------------------------------------------------------------
def test_event_migrator_runs_at_most_once(monkeypatch: pytest.MonkeyPatch):
    calls = {"n": 0}
    real = database._migrate_workflow_run_event

    def _counting_migrator() -> None:
        calls["n"] += 1
        real()

    # _connect_event imports the migrator lazily from the database module, so
    # patching the database-module attribute is picked up on the next append.
    monkeypatch.setattr(database, "_migrate_workflow_run_event", _counting_migrator)
    # Force a cold path so the first append actually triggers a migration.
    workflow_journal._event_migrated_paths.clear()

    for seq in range(1, 51):  # N = 50 appends
        workflow_journal.append_event(
            "r1", seq, "step.started", event_schema_version=1, ts="2026-07-27T00:00:00Z"
        )

    # The whole point of the memoized _connect_event: the migrator fires at most
    # once regardless of how many events are appended. FAILS if it runs per append.
    assert calls["n"] <= 1

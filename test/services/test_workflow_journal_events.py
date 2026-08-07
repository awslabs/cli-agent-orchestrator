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


# ---------------------------------------------------------------------------
# PR 526 review — SHOULD-FIX: a TRAILING swallowed append must be declared.
#
# The adjacency scan only finds holes BETWEEN stored rows, so a hole at the END
# of the sequence (the last append(s) before a crash were swallowed) had no
# successor row to compare against and was invisible — exactly the forward-fault
# shape the two-term high-water design exists to catch. The durable high-water is
# the last seq ever ALLOCATED (persisted BEFORE each fallible append), so
# high_water > last-stored-seq is a real hole.
#
# Declared for TERMINAL runs only: a live run legitimately sits one seq ahead for
# the duration of every in-flight append.
# ---------------------------------------------------------------------------
def _seed_run_state(run_id: str, state: str) -> None:
    workflow_journal.insert_run(
        run_id=run_id,
        workflow_name="wf",
        spec_snapshot="{}",
        inputs_json="{}",
        state=state,
        started_at="2026-07-27T00:00:00Z",
    )


def test_trailing_gap_declared_when_last_append_was_swallowed():
    """Events 1,2 landed; seq 3 was allocated (high-water) but its append was
    lost, and the run then ended. The hole at the END must be declared."""
    for seq in (1, 2):
        workflow_journal.append_event(
            "r1", seq, "step.started", event_schema_version=1, ts="2026-07-27T00:00:00Z"
        )
    workflow_journal.persist_high_water("r1", 3)  # allocated, never landed
    _seed_run_state("r1", "completed")

    rows, gaps = workflow_journal.read_events_with_gaps("r1")
    assert [r.seq for r in rows] == [1, 2]
    assert len(gaps) == 1
    gap = gaps[0]
    assert gap.after_seq == 2
    assert gap.missing_count == 1
    assert gap.reason == "append_failed_trailing"
    # missing_count arithmetic stays identical to an interior gap.
    assert gap.missing_count == gap.before_seq - gap.after_seq - 1


def test_trailing_gap_counts_multiple_lost_trailing_appends():
    workflow_journal.append_event(
        "r1", 1, "step.started", event_schema_version=1, ts="2026-07-27T00:00:00Z"
    )
    workflow_journal.persist_high_water("r1", 4)  # 2,3,4 allocated, none landed
    _seed_run_state("r1", "failed")

    _, gaps = workflow_journal.read_events_with_gaps("r1")
    assert len(gaps) == 1
    assert (gaps[0].after_seq, gaps[0].missing_count) == (1, 3)
    assert gaps[0].reason == "append_failed_trailing"


def test_no_trailing_gap_when_high_water_matches_the_last_stored_event():
    """The healthy case: every allocated seq landed -> no trailing gap."""
    for seq in (1, 2, 3):
        workflow_journal.append_event(
            "r1", seq, "step.started", event_schema_version=1, ts="2026-07-27T00:00:00Z"
        )
    workflow_journal.persist_high_water("r1", 3)
    _seed_run_state("r1", "completed")

    rows, gaps = workflow_journal.read_events_with_gaps("r1")
    assert [r.seq for r in rows] == [1, 2, 3]
    assert gaps == []


def test_no_trailing_gap_for_a_still_running_run():
    """A LIVE run is legitimately one ahead mid-append (high-water is persisted
    BEFORE the append), so declaring there would emit a phantom gap on every
    poll of every running run. Must stay silent until the run is terminal."""
    for seq in (1, 2):
        workflow_journal.append_event(
            "r1", seq, "step.started", event_schema_version=1, ts="2026-07-27T00:00:00Z"
        )
    workflow_journal.persist_high_water("r1", 3)  # in-flight append
    _seed_run_state("r1", "running")

    _, gaps = workflow_journal.read_events_with_gaps("r1")
    assert gaps == []

    # ...and the moment it goes terminal, the same state DOES declare the hole.
    workflow_journal.update_run_state("r1", "completed", "2026-07-27T00:00:05Z")
    _, gaps_after = workflow_journal.read_events_with_gaps("r1")
    assert [g.reason for g in gaps_after] == ["append_failed_trailing"]


def test_trailing_gap_declared_against_the_cursor_on_a_caught_up_page():
    """A follower already at seq 2 reading a terminal run whose seq 3 was lost
    gets an EMPTY page that still declares the trailing hole."""
    for seq in (1, 2):
        workflow_journal.append_event(
            "r1", seq, "step.started", event_schema_version=1, ts="2026-07-27T00:00:00Z"
        )
    workflow_journal.persist_high_water("r1", 3)
    _seed_run_state("r1", "completed")

    rows, gaps = workflow_journal.read_events_with_gaps("r1", after_seq=2)
    assert rows == []
    assert len(gaps) == 1
    assert (gaps[0].after_seq, gaps[0].missing_count) == (2, 1)


def test_interior_and_trailing_gaps_are_both_declared():
    """Both mechanisms coexist: a hole in the middle AND one at the end."""
    for seq in (1, 3):  # seq 2 lost (interior)
        workflow_journal.append_event(
            "r1", seq, "step.started", event_schema_version=1, ts="2026-07-27T00:00:00Z"
        )
    workflow_journal.persist_high_water("r1", 5)  # 4,5 lost (trailing)
    _seed_run_state("r1", "completed")

    _, gaps = workflow_journal.read_events_with_gaps("r1")
    assert [g.reason for g in gaps] == ["append_failed", "append_failed_trailing"]
    assert (gaps[0].after_seq, gaps[0].missing_count) == (1, 1)
    assert (gaps[1].after_seq, gaps[1].missing_count) == (3, 2)


# ---------------------------------------------------------------------------
# PR 526 human review — NIT N2: a FROM-START read hid the trailing gap when the
# run had NO stored events.
#
# `prev` was seeded `rows[0].seq - 1 if rows else None`, and the trailing block
# is guarded on `prev is not None`, so a run with zero stored rows skipped it
# entirely. That is the MOST severe loss shape there is — every append swallowed,
# nothing landed at all — and the default read declared nothing for it, while a
# cursor read of the very same run DID declare it. The two reads must agree.
# ---------------------------------------------------------------------------
def test_from_start_read_declares_the_trailing_gap_when_nothing_landed():
    """Total loss: high-water 3 allocated, zero events stored, run terminal. A
    from-start read must declare all 3 missing — it declared nothing before."""
    workflow_journal.persist_high_water("r1", 3)  # 1,2,3 allocated, none landed
    _seed_run_state("r1", "failed")

    rows, gaps = workflow_journal.read_events_with_gaps("r1")

    assert rows == []
    assert len(gaps) == 1, f"total loss must be declared, got {gaps}"
    assert gaps[0].reason == "append_failed_trailing"
    assert gaps[0].after_seq == 0
    assert gaps[0].before_seq == 4  # high_water + 1 sentinel
    assert gaps[0].missing_count == 3  # == high_water: the whole sequence
    # The interior-gap arithmetic invariant still holds.
    assert gaps[0].missing_count == gaps[0].before_seq - gaps[0].after_seq - 1


def test_from_start_and_cursor_reads_agree_on_a_total_loss():
    """The defect was an INCONSISTENCY between two reads of one run, so pin the
    agreement directly: a from-start read and a cursor-at-0 read must declare the
    same hole. Before the fix the from-start side returned no gaps at all."""
    workflow_journal.persist_high_water("r1", 2)
    _seed_run_state("r1", "completed")

    _, from_start = workflow_journal.read_events_with_gaps("r1")
    _, from_cursor = workflow_journal.read_events_with_gaps("r1", after_seq=0)

    assert from_start == from_cursor
    assert [g.reason for g in from_start] == ["append_failed_trailing"]


def test_from_start_read_of_a_healthy_empty_run_declares_nothing():
    """The quiet case must stay quiet: a terminal run that genuinely recorded no
    events (high_water 0) declares no gap. Without this, seeding prev=0 could
    have turned every empty run into a false loss report."""
    _seed_run_state("r1", "completed")  # no events, no high-water persisted

    rows, gaps = workflow_journal.read_events_with_gaps("r1")
    assert rows == []
    assert gaps == []


def test_from_start_read_of_an_empty_running_run_declares_nothing():
    """And the terminal-only guard still applies to the empty case: a LIVE run
    whose first append is in flight must not be reported as a total loss."""
    workflow_journal.persist_high_water("r1", 1)  # first append in flight
    _seed_run_state("r1", "running")

    rows, gaps = workflow_journal.read_events_with_gaps("r1")
    assert rows == []
    assert gaps == []


# ---------------------------------------------------------------------------
# PR 526 review fix cycle 1 — a LEADING hole must be declared on the default read.
#
# The from-start seed was `rows[0].seq - 1`, which is self-referential: it defines
# "previous" as one below the first SURVIVING row, so a hole before that row is
# invisible by construction. The same run read with an explicit cursor at 0 DID
# declare it. Seeding 0 unconditionally makes every shape agree.
# ---------------------------------------------------------------------------
def test_leading_hole_is_declared_on_a_from_start_read():
    """Seqs 1,2 were allocated and lost; 3,4,5 landed. The default read (what the
    web uses) must declare the leading hole, not silently start at 3."""
    for seq in (3, 4, 5):
        workflow_journal.append_event(
            "r1", seq, "step.started", event_schema_version=1, ts="2026-07-27T00:00:00Z"
        )
    workflow_journal.persist_high_water("r1", 5)
    _seed_run_state("r1", "completed")

    rows, gaps = workflow_journal.read_events_with_gaps("r1")
    assert [r.seq for r in rows] == [3, 4, 5]
    assert [g.reason for g in gaps] == ["append_failed"]
    assert (gaps[0].after_seq, gaps[0].before_seq, gaps[0].missing_count) == (0, 3, 2)


def test_from_start_and_cursor_reads_agree_on_a_leading_hole():
    """The agreement contract, on the shape that used to break it."""
    for seq in (3, 4, 5):
        workflow_journal.append_event(
            "r1", seq, "step.started", event_schema_version=1, ts="2026-07-27T00:00:00Z"
        )
    workflow_journal.persist_high_water("r1", 5)
    _seed_run_state("r1", "completed")

    _, from_start = workflow_journal.read_events_with_gaps("r1")
    _, from_cursor = workflow_journal.read_events_with_gaps("r1", after_seq=0)
    assert from_start == from_cursor
    assert [g.reason for g in from_start] == ["append_failed"]


def test_healthy_run_declares_no_phantom_leading_gap():
    """The counterpart: the unconditional 0 seed must not invent a hole before
    seq 1 on a run that lost nothing."""
    for seq in (1, 2, 3):
        workflow_journal.append_event(
            "r1", seq, "step.started", event_schema_version=1, ts="2026-07-27T00:00:00Z"
        )
    workflow_journal.persist_high_water("r1", 3)
    _seed_run_state("r1", "completed")

    rows, gaps = workflow_journal.read_events_with_gaps("r1")
    assert [r.seq for r in rows] == [1, 2, 3]
    assert gaps == [], f"phantom gap on a healthy run: {gaps}"


# ---------------------------------------------------------------------------
# PR #526 review round 3 — BLOCKING: a NEGATIVE replay cursor fabricated a gap.
#
# `read_events_with_gaps` seeded `prev = after_seq` verbatim, so a negative cursor
# on a perfectly healthy, lossless run declared a phantom loss:
#     after_seq=-5  on seqs 1..3  ->  (after_seq=-5, before_seq=1,
#                                      missing_count=5, reason="append_failed")
# That inverts this module's central contract — a declared gap must mean an event
# was ACTUALLY lost. The reader now clamps to 0.
#
# The clamp lives in the reader (not only behind the route's ge=0 bound) because
# the reader is SHARED: both arms of /events plus /compare and /diagnostics call
# it. These tests drive the reader directly, so they hold regardless of the route.
# ---------------------------------------------------------------------------
def _seed_healthy_run(run_id: str = "healthy") -> None:
    """A lossless run: seqs 1..3 all landed, high-water agrees, state terminal."""
    _seed_run_state(run_id, "completed")
    for seq in (1, 2, 3):
        workflow_journal.append_event(
            run_id, seq, "step.started", event_schema_version=1, ts="2026-07-27T00:00:00Z"
        )
    workflow_journal.persist_high_water(run_id, 3)


@pytest.mark.parametrize("cursor", [-1, -5, -100])
def test_negative_cursor_declares_no_phantom_gap_on_a_healthy_run(cursor: int):
    """THE regression test (FR-2.2/FR-2.3). RED before the clamp: the pre-fix reader
    returned a GapMarker with missing_count == abs(cursor) for each of these."""
    _seed_healthy_run()

    rows, gaps = workflow_journal.read_events_with_gaps("healthy", cursor)

    assert [r.seq for r in rows] == [1, 2, 3]
    assert gaps == [], f"phantom gap fabricated from cursor {cursor}: {gaps}"


@pytest.mark.parametrize("cursor", [-1, -5, -100])
def test_clamped_negative_cursor_agrees_with_a_from_start_read(cursor: int):
    """The clamp target is 0 — exactly what the from-start branch seeds — so a
    clamped negative cursor and `after_seq=None` must agree. Clamping to None
    instead would pass this for the healthy shape and FAIL the total-loss test
    below, which is why both exist."""
    _seed_healthy_run()

    from_start_rows, from_start_gaps = workflow_journal.read_events_with_gaps("healthy")
    clamped_rows, clamped_gaps = workflow_journal.read_events_with_gaps("healthy", cursor)

    assert [r.seq for r in clamped_rows] == [r.seq for r in from_start_rows]
    assert clamped_gaps == from_start_gaps


def test_clamp_does_not_hide_a_total_loss_run():
    """The clamp must not silence a REAL hole. A run whose every append was
    swallowed (high-water 3, zero rows) still declares its trailing loss on a
    negative cursor. Clamping to None would skip the trailing block and turn this
    GREEN-but-wrong — reintroducing the defect an earlier round fixed."""
    _seed_run_state("lost", "completed")
    workflow_journal.persist_high_water("lost", 3)

    rows, gaps = workflow_journal.read_events_with_gaps("lost", -5)

    assert rows == []
    assert len(gaps) == 1
    assert gaps[0].missing_count == 3
    assert gaps[0].reason == "append_failed_trailing"


def test_negative_cursor_still_declares_a_real_interior_gap():
    """A clamped cursor must not suppress an interior hole either: seq 2 swallowed,
    1 and 3 landed -> the gap is still declared."""
    _seed_run_state("interior", "completed")
    for seq in (1, 3):
        workflow_journal.append_event(
            "interior", seq, "step.started", event_schema_version=1, ts="2026-07-27T00:00:00Z"
        )
    workflow_journal.persist_high_water("interior", 3)

    rows, gaps = workflow_journal.read_events_with_gaps("interior", -5)

    assert [r.seq for r in rows] == [1, 3]
    assert [(g.after_seq, g.before_seq, g.missing_count) for g in gaps] == [(1, 3, 1)]

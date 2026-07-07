"""Tests for U3 ScriptJournalExtension (issue #312, script-tier journal, C3).

Covers the four additive algorithms from
``aidlc-docs/construction/U3-script-journal-extension/functional-design``:

- A1 write-through append (``append_step``): upsert-on-conflict, stable
  fingerprint across attempts (business-logic-model §A1, business-rules VR-4).
- A2 replay lookup (``lookup_replay``): all four outcomes — absent, partial,
  COMPLETED+match (replay), COMPLETED+mismatch (raise) (DR-1..DR-4).
- A3 generation fence (``check_generation``): match proceeds, mismatch raises
  ``StaleGenerationError``, unknown run raises ``KeyError`` (DR-5/DR-6).
- ``update_run_generation``: persists a bumped generation (A4).
- A crash-injection-style resume scenario (NFR-REL-2/M3): journal COMPLETED
  steps with fingerprints, simulate a crash (no live registry entry), then
  verify ``lookup_replay`` would replay the completed calls (no re-execution)
  and the tail (never-arrived call) executes fresh.
- ``_is_resumable_for_tier`` (DR-7/DR-8): the tier-aware resumability predicate
  U3 supplies for U4/U5 to wire into the resume route later.
- Migration idempotency: the additive ``ALTER TABLE`` migrators are safe to run
  twice and preserve INV-2 defaults on a pre-existing (pre-U3-shaped) row.

The journal points at a temp SQLite DB via the patched ``DATABASE_FILE``,
mirroring ``test_workflow_journal_resume.py``'s fixture pattern exactly.
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
from cli_agent_orchestrator.services.workflow_journal import RunRow
from cli_agent_orchestrator.services.workflow_service import (
    ReplayDivergenceError,
    StaleGenerationError,
    _is_resumable_for_tier,
    check_generation,
    update_run_generation,
)


@pytest.fixture(autouse=True)
def _patched_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the journal at a temp DB and create the tables (U3 migrators included)."""
    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    _migrate_workflow_run()
    _migrate_workflow_run_step()
    yield db_path


def _direct_connect() -> sqlite3.Connection:
    from cli_agent_orchestrator.constants import DATABASE_FILE

    return sqlite3.connect(str(DATABASE_FILE))


def _seed_run(run_id: str, *, state: str = "running", tier: str = "script", generation: str = "1"):
    workflow_journal.insert_run(
        run_id=run_id,
        workflow_name="wf",
        spec_snapshot="{}",
        inputs_json="{}",
        state=state,
        started_at="2026-07-03T00:00:00Z",
    )
    # insert_run does not carry tier/generation (base signature, INV-1) — set
    # them directly with a raw UPDATE, mirroring how U4's start_run path for a
    # script run would set them (out of scope for U3 to build that path).
    with _direct_connect() as conn:
        conn.execute(
            "UPDATE workflow_run SET tier = ?, generation = ? WHERE run_id = ?",
            (tier, generation, run_id),
        )


FP_A = "a" * 64
FP_B = "b" * 64


# ---------------------------------------------------------------------------
# A1 — append_step
# ---------------------------------------------------------------------------
def test_append_step_inserts_running_row_with_fingerprint(_patched_journal):
    _seed_run("run-1")
    workflow_journal.append_step("run-1", "call-1", "running", "2026-07-03T00:00:01Z", FP_A)

    row = workflow_journal.get_step("run-1", "call-1")
    assert row is not None
    assert row.state == "running"
    assert row.call_fingerprint == FP_A


def test_append_step_upserts_on_conflict_without_overwriting_fingerprint(_patched_journal):
    """A1: a re-arrival at the same key upserts state/updated_at; fingerprint is stable (VR-4)."""
    _seed_run("run-1")
    workflow_journal.append_step("run-1", "call-1", "running", "2026-07-03T00:00:01Z", FP_A)
    # A second attempt at the SAME key arrives with a DIFFERENT fingerprint value
    # supplied by the caller (e.g. a bug, or a resume replaying the arrival) —
    # append_step must not let it clobber the originally-recorded fingerprint.
    workflow_journal.append_step("run-1", "call-1", "completed", "2026-07-03T00:00:02Z", FP_B)

    row = workflow_journal.get_step("run-1", "call-1")
    assert row is not None
    assert row.state == "completed"  # state DOES update (A1 upsert)
    assert row.updated_at == "2026-07-03T00:00:02Z"
    assert row.call_fingerprint == FP_A  # fingerprint stays stable across attempts


def test_append_step_does_not_raise_on_duplicate_key(_patched_journal):
    """A1: duplicate registration is an upsert, never a raised IntegrityError."""
    _seed_run("run-1")
    workflow_journal.append_step("run-1", "call-1", "running", "2026-07-03T00:00:01Z", FP_A)
    # Must not raise.
    workflow_journal.append_step("run-1", "call-1", "running", "2026-07-03T00:00:02Z", FP_A)


# ---------------------------------------------------------------------------
# A2 — lookup_replay (DR-1..DR-4)
# ---------------------------------------------------------------------------
def test_lookup_replay_absent_returns_none(_patched_journal):
    _seed_run("run-1")
    assert workflow_journal.lookup_replay("run-1", "call-1", FP_A) is None


def test_lookup_replay_partial_returns_none(_patched_journal):
    _seed_run("run-1")
    workflow_journal.append_step("run-1", "call-1", "running", "2026-07-03T00:00:01Z", FP_A)
    assert workflow_journal.lookup_replay("run-1", "call-1", FP_A) is None


def test_lookup_replay_completed_match_returns_row(_patched_journal):
    _seed_run("run-1")
    workflow_journal.append_step("run-1", "call-1", "running", "2026-07-03T00:00:01Z", FP_A)
    workflow_journal.append_step("run-1", "call-1", "completed", "2026-07-03T00:00:02Z", FP_A)

    row = workflow_journal.lookup_replay("run-1", "call-1", FP_A)
    assert row is not None
    assert row.state == "completed"
    assert row.call_fingerprint == FP_A


def test_lookup_replay_completed_mismatch_raises(_patched_journal):
    _seed_run("run-1")
    workflow_journal.append_step("run-1", "call-1", "running", "2026-07-03T00:00:01Z", FP_A)
    workflow_journal.append_step("run-1", "call-1", "completed", "2026-07-03T00:00:02Z", FP_A)

    with pytest.raises(ReplayDivergenceError):
        workflow_journal.lookup_replay("run-1", "call-1", FP_B)


# ---------------------------------------------------------------------------
# A3 — check_generation (DR-5/DR-6)
# ---------------------------------------------------------------------------
def test_check_generation_match_proceeds(_patched_journal):
    _seed_run("run-1", generation="1")
    check_generation("run-1", "1")  # must not raise


def test_check_generation_mismatch_raises_stale(_patched_journal):
    _seed_run("run-1", generation="2")
    with pytest.raises(StaleGenerationError):
        check_generation("run-1", "1")


def test_check_generation_unknown_run_raises_keyerror(_patched_journal):
    with pytest.raises(KeyError):
        check_generation("no-such-run", "1")


# ---------------------------------------------------------------------------
# update_run_generation
# ---------------------------------------------------------------------------
def test_update_run_generation_persists_bump(_patched_journal):
    _seed_run("run-1", generation="1")
    update_run_generation("run-1", "2")

    row = workflow_journal.get_run("run-1")
    assert row is not None
    assert row.generation == "2"
    # And the fence now uses the NEW generation.
    check_generation("run-1", "2")
    with pytest.raises(StaleGenerationError):
        check_generation("run-1", "1")


# ---------------------------------------------------------------------------
# Crash-injection-style resume scenario (NFR-REL-2 / M3)
# ---------------------------------------------------------------------------
def test_crash_then_resume_replays_completed_and_executes_tail(_patched_journal):
    """Simulate a crash mid-run: two calls journaled COMPLETED, a third never arrived.

    No live registry entry exists (the process is gone — the crash). Rebuilding
    the resume decision purely from ``lookup_replay`` must:
      - replay call-1 and call-2 (COMPLETED + matching fingerprint) without
        re-executing them,
      - decide call-3 (never arrived) needs fresh execution.

    Full end-to-end resume-route wiring is out of scope for U3 (the runner/route
    belongs to U4/U5) — this exercises the journal primitives at the granularity
    U3 ships (per the code-generation-plan's explicit scoping note).
    """
    _seed_run("run-1", state="running", generation="1")
    workflow_journal.append_step("run-1", "call-1", "running", "2026-07-03T00:00:01Z", FP_A)
    workflow_journal.append_step("run-1", "call-1", "completed", "2026-07-03T00:00:02Z", FP_A)
    workflow_journal.append_step("run-1", "call-2", "running", "2026-07-03T00:00:03Z", FP_B)
    workflow_journal.append_step("run-1", "call-2", "completed", "2026-07-03T00:00:04Z", FP_B)
    # call-3 never arrived before the crash — no row exists for it.

    replay_1 = workflow_journal.lookup_replay("run-1", "call-1", FP_A)
    replay_2 = workflow_journal.lookup_replay("run-1", "call-2", FP_B)
    replay_3 = workflow_journal.lookup_replay("run-1", "call-3", "c" * 64)

    assert replay_1 is not None and replay_1.state == "completed"
    assert replay_2 is not None and replay_2.state == "completed"
    assert replay_3 is None  # absent -> execute fresh (the resume tail)

    # A resumed drive would bump the generation before re-spawning (A4); the
    # fenced-out orphan carrying the OLD generation is rejected afterward.
    update_run_generation("run-1", "2")
    with pytest.raises(StaleGenerationError):
        check_generation("run-1", "1")
    check_generation("run-1", "2")  # the resumer's own calls proceed


# ---------------------------------------------------------------------------
# _is_resumable_for_tier (DR-7/DR-8) — journal primitive only, not wired to a route
# ---------------------------------------------------------------------------
def _row(state: str, tier: str) -> RunRow:
    return RunRow(
        run_id="r",
        workflow_name="wf",
        spec_snapshot="{}",
        inputs_json="{}",
        state=state,
        current_step_id=None,
        started_at="2026-07-03T00:00:00Z",
        finished_at=None,
        tier=tier,
        generation="1",
    )


def test_is_resumable_for_tier_completed_never_resumable():
    assert _is_resumable_for_tier(_row("completed", "yaml")) is False
    assert _is_resumable_for_tier(_row("completed", "script")) is False


def test_is_resumable_for_tier_cancelled_script_resumable():
    assert _is_resumable_for_tier(_row("cancelled", "script")) is True


def test_is_resumable_for_tier_cancelled_yaml_not_resumable():
    assert _is_resumable_for_tier(_row("cancelled", "yaml")) is False


def test_is_resumable_for_tier_failed_resumable_both_tiers():
    assert _is_resumable_for_tier(_row("failed", "yaml")) is True
    assert _is_resumable_for_tier(_row("failed", "script")) is True


def test_is_resumable_for_tier_running_crash_remnant_resumable():
    assert _is_resumable_for_tier(_row("running", "script")) is True


# ---------------------------------------------------------------------------
# Migration idempotency + INV-2 defaults
# ---------------------------------------------------------------------------
def test_migration_is_idempotent_and_preserves_defaults(_patched_journal):
    # Running the migrators a second time must not raise and must not disturb
    # existing columns/rows.
    _migrate_workflow_run()
    _migrate_workflow_run_step()

    _seed_run("run-yaml", tier="yaml", generation="1")
    row = workflow_journal.get_run("run-yaml")
    assert row is not None
    assert row.tier == "yaml"
    assert row.generation == "1"


def test_pre_u3_row_reads_with_inv2_defaults(_patched_journal):
    """A row inserted via the base ``insert_run`` (no tier/generation set) reads
    back with the INV-2 defaults, exactly like a pre-U3 row would (INV-1)."""
    workflow_journal.insert_run(
        run_id="legacy-run",
        workflow_name="wf",
        spec_snapshot="{}",
        inputs_json="{}",
        state="running",
        started_at="2026-07-03T00:00:00Z",
    )
    row = workflow_journal.get_run("legacy-run")
    assert row is not None
    assert row.tier == "yaml"
    assert row.generation == "1"


def test_pre_u3_step_row_reads_with_null_fingerprint(_patched_journal):
    """A step row inserted via the base ``insert_steps`` reads back with
    ``call_fingerprint = None`` (INV-2), exactly like a pre-U3 YAML step."""
    workflow_journal.insert_run(
        run_id="legacy-run",
        workflow_name="wf",
        spec_snapshot="{}",
        inputs_json="{}",
        state="running",
        started_at="2026-07-03T00:00:00Z",
    )
    workflow_journal.insert_steps("legacy-run", [("s1", "pending")], "2026-07-03T00:00:00Z")
    steps = workflow_journal.get_steps("legacy-run")
    assert len(steps) == 1
    assert steps[0].call_fingerprint is None

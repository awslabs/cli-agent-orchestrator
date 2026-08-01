"""Tests for U2 event emission into the durable event log (issue #504).

Covers the load-bearing behavior from
``construction/U2-event-emission/functional-design`` and ``business-rules.md``:

- happy path: a mock-worker run through ``start_run`` emits the FR-1.4 taxonomy in
  transition order, read back via U1's ``read_events`` (run.started ... step.started
  ... step.completed ... run.completed) with the FR-1.2 fields populated.
- BR-2 (best-effort, never faults the run): monkeypatching ``append_event`` to
  raise leaves the run COMPLETED and ``_drive`` does not raise; the swallowed
  append leaves a declared hole (no renumber).
- BR-6 (structured error kind on failure events): a worker raising
  ``StepExecutionError(kind="error")`` emits ``step.attempt.failed`` carrying
  ``error_kind="error"`` AND the ``workflow_run_step.error_kind`` column reads
  ``"error"`` after (a cold read needs no event replay).
- BR-7 (cancel maps to skip/cancel, never a failure event): a
  ``StepCancelledError`` yields ``step.skipped`` + ``run.cancelled`` and NO
  failure event.
- BR-8 (seq continuity across restart): emit, clear ``run_registry``, rebuild via
  ``get_run_status``, emit again — the new seqs are strictly greater than the
  pre-restart max (resume above max(persisted_high_water, max_event_seq)).

``run_agent_step`` and ``step_output_store`` are mocked — no real terminals — via
the exact fixture pattern of ``test_workflow_journal_resume.py``. The journal
points at a temp SQLite DB via the patched ``DATABASE_FILE``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import List
from unittest.mock import AsyncMock

import pytest

from cli_agent_orchestrator.clients.database import (
    _migrate_workflow_run,
    _migrate_workflow_run_step,
)
from cli_agent_orchestrator.models.terminal import AgentStepResult, TerminalStatus
from cli_agent_orchestrator.models.workflow import (
    RunState,
    StepState,
    WorkflowSpec,
    WorkflowStep,
)
from cli_agent_orchestrator.models.workflow_runtime import StepOutputRecord
from cli_agent_orchestrator.services import workflow_journal
from cli_agent_orchestrator.services import workflow_service as ws
from cli_agent_orchestrator.services.agent_step import (
    StepCancelledError,
    StepExecutionError,
)

_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}


@pytest.fixture(autouse=True)
def _patched_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the journal at a temp DB, create the tables, clean the registry.

    Mirrors ``test_workflow_journal_resume.py`` exactly; also clears the event
    migration memo so the fresh temp DB self-migrates on first event write.
    """
    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    _migrate_workflow_run()
    _migrate_workflow_run_step()
    workflow_journal._event_migrated_paths.clear()
    ws.run_registry.clear()
    ws._active_drives.clear()
    ws.step_output_store._store.clear()
    yield db_path
    workflow_journal._event_migrated_paths.clear()
    ws.run_registry.clear()
    ws._active_drives.clear()
    ws.step_output_store._store.clear()


def _ok(terminal_id: str = "t1") -> AgentStepResult:
    return AgentStepResult(
        terminal_id=terminal_id, last_message="done", status=TerminalStatus.COMPLETED
    )


def _step(step_id: str, *, schema=None) -> WorkflowStep:
    return WorkflowStep(
        id=step_id,
        provider="kiro_cli",
        agent="dev",
        prompt="go",
        output_schema=schema,
    )


def _spec(name: str = "wf", *, step_ids=("s1",), schema=None) -> WorkflowSpec:
    return WorkflowSpec(
        name=name,
        mode="sequential",
        steps=[_step(sid, schema=schema) for sid in step_ids],
    )


def _put_valid(run_id: str, step_id: str) -> None:
    ws.step_output_store.put(
        run_id,
        step_id,
        StepOutputRecord(
            run_id=run_id,
            step_id=step_id,
            output={"answer": "42"},
            validated=True,
            errors=[],
            state=StepState.COMPLETED,
        ),
    )


def _types(run_id: str) -> List[str]:
    """The ordered event_type sequence durably recorded for a run."""
    return [e.event_type for e in workflow_journal.read_events(run_id)]


# ---------------------------------------------------------------------------
# happy path — the FR-1.4 taxonomy, emitted in transition order
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_happy_path_emits_taxonomy_in_order(monkeypatch):
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))
    await ws.start_run(_spec(step_ids=("s1", "s2")), {}, "runHappy")

    types = _types("runHappy")
    # The run brackets the whole timeline.
    assert types[0] == "run.started"
    assert types[-1] == "run.completed"
    # Both steps went through started -> attempt.started -> terminal.created ->
    # completed (free-form step, no output_schema, so no step.output.received).
    assert types.count("step.started") == 2
    assert types.count("step.attempt.started") == 2
    assert types.count("terminal.created") == 2
    assert types.count("step.completed") == 2
    assert "step.failed" not in types
    assert "step.attempt.failed" not in types
    assert "run.failed" not in types

    # The per-step ordering is preserved for s1 (BR-3): started before its attempt
    # before its terminal before its completion.
    s1_started = types.index("step.started")
    s1_attempt = types.index("step.attempt.started")
    s1_term = types.index("terminal.created")
    s1_done = types.index("step.completed")
    assert s1_started < s1_attempt < s1_term < s1_done


@pytest.mark.asyncio
async def test_happy_path_events_carry_fr12_fields(monkeypatch):
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok("term-xyz")))
    await ws.start_run(_spec(step_ids=("s1",)), {}, "runFields")

    events = {e.event_type: e for e in workflow_journal.read_events("runFields")}
    # step.started carries provider + agent_profile + step_id from the step.
    started = events["step.started"]
    assert started.step_id == "s1"
    assert started.provider == "kiro_cli"
    assert started.agent_profile == "dev"
    assert started.state == StepState.RUNNING.value
    # terminal.created carries the bound terminal id + the attempt number.
    term = events["terminal.created"]
    assert term.terminal_id == "term-xyz"
    assert term.attempt == 1
    # step.attempt.started carries the attempt number.
    assert events["step.attempt.started"].attempt == 1
    # every event stamps the schema version (FR-1.1).
    assert all(e.event_schema_version == 1 for e in workflow_journal.read_events("runFields"))
    # reserved fields stay NULL (FR-1.5).
    assert all(
        e.iteration is None and e.which_guard_fired is None
        for e in workflow_journal.read_events("runFields")
    )


@pytest.mark.asyncio
async def test_validated_output_emits_step_output_received(monkeypatch):
    async def _side(**kwargs):
        _put_valid(
            kwargs["env_vars"]["CAO_WORKFLOW_RUN_ID"],
            kwargs["env_vars"]["CAO_WORKFLOW_STEP_ID"],
        )
        return _ok()

    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(side_effect=_side))
    await ws.start_run(_spec(step_ids=("s1",), schema=_SCHEMA), {}, "runOut")

    events = {e.event_type: e for e in workflow_journal.read_events("runOut")}
    assert "step.output.received" in events
    assert events["step.output.received"].validation_result == "valid"
    assert _types("runOut")[-1] == "run.completed"


@pytest.mark.asyncio
async def test_events_seq_is_contiguous_and_monotonic(monkeypatch):
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))
    await ws.start_run(_spec(step_ids=("s1", "s2")), {}, "runSeq")

    rows, gaps = workflow_journal.read_events_with_gaps("runSeq")
    seqs = [r.seq for r in rows]
    # a clean run leaves NO gap; seqs are 1..N contiguous (BR-1/BR-3).
    assert gaps == []
    assert seqs == list(range(1, len(seqs) + 1))
    # the high-water matches the last allocated seq.
    assert workflow_journal.persisted_high_water("runSeq") == seqs[-1]


# ---------------------------------------------------------------------------
# BR-2 — best-effort: a failing append MUST NOT fault the run; a hole is left
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_append_failure_does_not_break_run_and_leaves_hole(monkeypatch):
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))

    calls = {"n": 0}
    real_append = workflow_journal.append_event

    def _flaky_append(run_id, seq, event_type, **kwargs):
        calls["n"] += 1
        # Drop exactly the 2nd append (leaves a hole at that seq); the high-water
        # for it was already persisted, so the sequence still declares the gap.
        if calls["n"] == 2:
            raise sqlite3.OperationalError("simulated append failure")
        return real_append(run_id, seq, event_type, **kwargs)

    monkeypatch.setattr(ws.workflow_journal, "append_event", _flaky_append)

    # The run still COMPLETES; _drive did not raise (BR-2).
    res = await ws.start_run(_spec(step_ids=("s1", "s2")), {}, "runFlaky")
    assert res.state == RunState.COMPLETED

    rows, gaps = workflow_journal.read_events_with_gaps("runFlaky")
    # The dropped event's seq is absent (a hole), and it is DECLARED as a gap
    # rather than renumbered away (FR-3.3).
    assert len(gaps) == 1
    assert gaps[0].missing_count == 1
    assert 2 not in [r.seq for r in rows]


@pytest.mark.asyncio
async def test_all_appends_failing_still_completes_the_run(monkeypatch):
    # Total emission outage: every append AND every high-water write raises. The
    # run must still COMPLETE (nothing raises into _drive, BR-2).
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))

    def _boom(*a, **k):
        raise sqlite3.OperationalError("db down")

    monkeypatch.setattr(ws.workflow_journal, "append_event", _boom)
    monkeypatch.setattr(ws.workflow_journal, "persist_high_water", _boom)

    res = await ws.start_run(_spec(step_ids=("s1",)), {}, "runOut2")
    assert res.state == RunState.COMPLETED
    assert res.steps[0].state == StepState.COMPLETED


# ---------------------------------------------------------------------------
# BR-6 — a failure carries the structured error kind on the event AND the
# step projection column (cold read needs no event replay)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_failure_carries_error_kind_on_event_and_projection(monkeypatch):
    # A worker that always crashes with kind="error" (a terminal ERROR, not a
    # timeout) exhausts its one attempt (retries=0) and the step FAILS.
    def _crash(**kwargs):
        raise StepExecutionError("boom", kind="error", terminal_id="term-crash")

    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(side_effect=_crash))
    step = WorkflowStep(id="s1", provider="kiro_cli", agent="dev", prompt="go", retries=0)
    spec = WorkflowSpec(name="wf", mode="sequential", steps=[step])
    res = await ws.start_run(spec, {}, "runErr")
    assert res.state == RunState.FAILED

    events = {e.event_type: e for e in workflow_journal.read_events("runErr")}
    # The attempt-failed event carries the structured error kind (FR-1.2 / BR-6).
    assert "step.attempt.failed" in events
    assert events["step.attempt.failed"].error_kind == "error"
    assert events["step.attempt.failed"].terminal_id == "term-crash"
    # The terminal step.failed event carries it too.
    assert events["step.failed"].error_kind == "error"
    # The run failed.
    assert _types("runErr")[-1] == "run.failed"

    # BR-6: a COLD read of the step projection surfaces the error kind with no
    # event replay — the additive workflow_run_step.error_kind column is set.
    ws.run_registry.clear()
    srow = workflow_journal.get_step("runErr", "s1")
    assert srow is not None
    assert srow.state == StepState.FAILED.value
    assert srow.error_kind == "error"


@pytest.mark.asyncio
async def test_timeout_kind_distinguished_from_error(monkeypatch):
    # A timeout (kind="timeout") is recorded distinctly from a crash (kind="error").
    def _timeout(**kwargs):
        raise StepExecutionError("slow", kind="timeout", terminal_id="term-slow")

    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(side_effect=_timeout))
    step = WorkflowStep(id="s1", provider="kiro_cli", agent="dev", prompt="go", retries=0)
    spec = WorkflowSpec(name="wf", mode="sequential", steps=[step])
    await ws.start_run(spec, {}, "runTo")

    events = {e.event_type: e for e in workflow_journal.read_events("runTo")}
    assert events["step.attempt.failed"].error_kind == "timeout"
    assert events["step.failed"].error_kind == "timeout"
    srow = workflow_journal.get_step("runTo", "s1")
    assert srow is not None and srow.error_kind == "timeout"


@pytest.mark.asyncio
async def test_retry_then_success_clears_error_kind_on_projection(monkeypatch):
    # A step that crashes once (kind="error") then succeeds: the attempt.failed
    # event records error_kind="error", but the SETTLED step projection clears the
    # kind back to NULL (the run did not fail — an honest cold read).
    calls = {"n": 0}

    async def _side(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise StepExecutionError("boom", kind="error", terminal_id="term-1")
        return _ok()

    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(side_effect=_side))
    step = WorkflowStep(id="s1", provider="kiro_cli", agent="dev", prompt="go", retries=3)
    spec = WorkflowSpec(name="wf", mode="sequential", steps=[step])
    res = await ws.start_run(spec, {}, "runRetry")
    assert res.state == RunState.COMPLETED

    events = _types("runRetry")
    assert "step.attempt.failed" in events  # attempt 1 failed
    assert "step.completed" in events  # attempt 2 settled
    assert "step.failed" not in events  # the step did not terminally fail

    # The settled projection clears error_kind (the step ultimately succeeded).
    srow = workflow_journal.get_step("runRetry", "s1")
    assert srow is not None
    assert srow.state == StepState.COMPLETED.value
    assert srow.error_kind is None


# ---------------------------------------------------------------------------
# BR-7 — a cancellation maps to step.skipped + run.cancelled, NEVER a failure
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cancel_emits_skip_and_cancelled_not_failure(monkeypatch):
    # The worker cancels the run from inside the step (mirrors the real in-flight
    # cancel), then raises StepCancelledError exactly as run_agent_step would.
    async def _hang_until_cancelled(**kwargs):
        run_id = kwargs["env_vars"]["CAO_WORKFLOW_RUN_ID"]
        cancel_event = kwargs["cancel_event"]
        ws.cancel_run(run_id)
        await cancel_event.wait()
        raise StepCancelledError(terminal_id="term-hung")

    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(side_effect=_hang_until_cancelled))
    res = await ws.start_run(_spec(step_ids=("s1", "s2")), {}, "runCancel")
    assert res.state == RunState.CANCELLED

    types = _types("runCancel")
    # The interrupted step is SKIPPED and the run converges CANCELLED — NEVER a
    # failure event (BR-7).
    assert "step.skipped" in types
    assert types[-1] == "run.cancelled"
    assert "step.failed" not in types
    assert "step.attempt.failed" not in types
    assert "run.failed" not in types


# ---------------------------------------------------------------------------
# BR-8 — seq continuity across a restart (rebuild re-seed)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_seq_continues_strictly_above_pre_restart_max_after_rebuild(monkeypatch):
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))
    await ws.start_run(_spec(step_ids=("s1",)), {}, "runRestart")

    pre_max = workflow_journal.max_event_seq("runRestart")
    assert pre_max > 0

    # Simulate a process restart: drop the live cache, then force a rebuild via
    # the journal-authoritative status read (re-seeds record.event_seq).
    ws.run_registry.clear()
    ws._active_drives.clear()
    ws.get_run_status("runRestart")
    record = ws.run_registry["runRestart"]
    # The re-seed put the counter at max(persisted_high_water, max_event_seq).
    assert record.event_seq == max(
        workflow_journal.persisted_high_water("runRestart"),
        workflow_journal.max_event_seq("runRestart"),
    )
    assert record.event_seq >= pre_max

    # Emit another event via the same _journal_event path; its seq is strictly
    # greater than every pre-restart seq (no renumbering across the boundary).
    await ws._journal_event(record, "run.completed", state=RunState.COMPLETED.value)
    post_seqs = [e.seq for e in workflow_journal.read_events("runRestart")]
    new_seq = max(post_seqs)
    assert new_seq == pre_max + 1
    assert new_seq > pre_max


@pytest.mark.asyncio
async def test_rebuild_reseed_degrades_to_zero_on_unreadable_seq_tables(monkeypatch):
    # The re-seed reads are best-effort: if the seq/event reads fail, they degrade
    # to 0 and the rebuild still returns a record (never raises).
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))
    await ws.start_run(_spec(step_ids=("s1",)), {}, "runReseed")

    ws.run_registry.clear()
    monkeypatch.setattr(ws.workflow_journal, "persisted_high_water", lambda rid: 0)
    monkeypatch.setattr(ws.workflow_journal, "max_event_seq", lambda rid: 0)
    record = ws._rebuild_record_from_journal("runReseed")
    assert record is not None
    assert record.event_seq == 0


# ---------------------------------------------------------------------------
# PR 526 review — SHOULD-FIX: severity on the two best-effort writes.
#
# Severity was inverted: losing an actual EVENT (journal content — punches a hole
# a reader must declare as a gap) logged at DEBUG, i.e. invisible at default log
# levels, while losing the far less consequential high-water logged WARNING.
# Both are now WARNING, and losing an event must never be quieter.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_lost_event_append_is_logged_at_warning(monkeypatch, caplog):
    """A swallowed append_event must surface at WARNING, not DEBUG.

    Goes RED if the append's except-arm is downgraded back to logger.debug.
    """
    import logging

    def _boom(*a, **k):
        raise sqlite3.OperationalError("event table gone")

    monkeypatch.setattr(workflow_journal, "append_event", _boom)
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))

    with caplog.at_level(logging.DEBUG, logger="cli_agent_orchestrator.services.workflow_service"):
        await ws.start_run(_spec(), {}, "runSev")

    append_failures = [r for r in caplog.records if "append_event" in r.getMessage()]
    assert append_failures, "the swallowed append was not logged at all"
    # Every append-loss record is at least WARNING — never DEBUG/INFO.
    for rec in append_failures:
        assert (
            rec.levelno >= logging.WARNING
        ), f"losing a journal event logged at {rec.levelname}; it must be >= WARNING"


@pytest.mark.asyncio
async def test_losing_an_event_is_never_quieter_than_losing_the_high_water(monkeypatch, caplog):
    """The relative-severity invariant: the event append is the MORE consequential
    of the two best-effort writes, so its level must be >= the high-water's."""
    import logging

    def _boom(*a, **k):
        raise sqlite3.OperationalError("db down")

    monkeypatch.setattr(workflow_journal, "append_event", _boom)
    monkeypatch.setattr(workflow_journal, "persist_high_water", _boom)
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))

    with caplog.at_level(logging.DEBUG, logger="cli_agent_orchestrator.services.workflow_service"):
        await ws.start_run(_spec(), {}, "runSev2")

    append_levels = [r.levelno for r in caplog.records if "append_event" in r.getMessage()]
    hw_levels = [r.levelno for r in caplog.records if "high-water" in r.getMessage()]
    assert append_levels and hw_levels, "expected both failure paths to log"
    assert min(append_levels) >= min(hw_levels)


# ---------------------------------------------------------------------------
# PR 526 human review — NIT N3: a DOUBLE read fault must not re-seed event_seq
# to 0 and collide on the (run_id, seq) primary key.
#
# Both re-seed terms — persisted_high_water and max_event_seq — degrade to 0 on a
# read failure, so if BOTH fail the rebuild re-seeded the counter to 0 and the
# next emission re-allocated seq 1, which already exists: the append dies on an
# IntegrityError and the event is LOST. The rebuild now also floors against the
# ``event_seq`` of a record already live in this process, which records what has
# actually been ALLOCATED here (a stronger floor than anything that merely
# PERSISTED) and keeps the counter monotonic across a rebuild.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_double_read_fault_does_not_reseed_below_a_live_records_counter(monkeypatch):
    """Both journal reads fail while a live record exists: the rebuilt counter
    must stay at the live record's allocated high-water, not fall back to 0."""
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))
    await ws.start_run(_spec(step_ids=("s1",)), {}, "runN3")

    live = ws.run_registry["runN3"]
    allocated = live.event_seq
    assert allocated > 0

    # Both durable re-seed reads fail (the double fault).
    monkeypatch.setattr(ws.workflow_journal, "persisted_high_water", lambda rid: 0)
    monkeypatch.setattr(ws.workflow_journal, "max_event_seq", lambda rid: 0)

    rebuilt = ws._rebuild_record_from_journal("runN3")

    assert rebuilt is not None
    assert rebuilt.event_seq == allocated, (
        "a double read fault re-seeded below what this process already allocated; "
        "the next emission would collide on the (run_id, seq) primary key"
    )


@pytest.mark.asyncio
async def test_next_emission_after_a_double_fault_rebuild_does_not_collide(monkeypatch):
    """The consequence, end-to-end: emitting after a double-fault rebuild must
    land a NEW seq, not raise IntegrityError against an existing row. Before the
    fix the counter reset to 0 and the append re-used seq 1."""
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))
    await ws.start_run(_spec(step_ids=("s1",)), {}, "runN3b")

    seqs_before = sorted(e.seq for e in workflow_journal.read_events("runN3b"))
    assert seqs_before, "expected the run to have journaled events"

    monkeypatch.setattr(ws.workflow_journal, "persisted_high_water", lambda rid: 0)
    monkeypatch.setattr(ws.workflow_journal, "max_event_seq", lambda rid: 0)
    rebuilt = ws._rebuild_record_from_journal("runN3b")
    assert rebuilt is not None

    await ws._journal_event(rebuilt, "run.completed", state=RunState.COMPLETED.value)

    seqs_after = sorted(e.seq for e in workflow_journal.read_events("runN3b"))
    # A new row landed (no swallowed IntegrityError), at a strictly higher seq.
    assert (
        len(seqs_after) == len(seqs_before) + 1
    ), f"the emission was lost to a PK collision: {seqs_before} -> {seqs_after}"
    assert max(seqs_after) > max(seqs_before)
    assert len(set(seqs_after)) == len(seqs_after)  # still no duplicate seq


@pytest.mark.asyncio
async def test_cold_rebuild_with_no_live_record_is_unaffected_by_the_floor(monkeypatch):
    """The added term must not change a genuine cold rebuild: with no cached
    record the floor is 0, so the two-term max stands exactly as before."""
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))
    await ws.start_run(_spec(step_ids=("s1",)), {}, "runN3c")

    expected = max(
        workflow_journal.persisted_high_water("runN3c"),
        workflow_journal.max_event_seq("runN3c"),
    )
    ws.run_registry.clear()  # a true cold rebuild — nothing live in this process

    rebuilt = ws._rebuild_record_from_journal("runN3c")
    assert rebuilt is not None
    assert rebuilt.event_seq == expected


# ---------------------------------------------------------------------------
# PR 526 review fix cycle 1 — BLOCKING: a HEALTHY completed run must never be
# reported as having lost trailing events.
#
# The trailing-gap check declares a hole when a TERMINAL run's durable high-water
# exceeds its last stored seq. Its correctness rests entirely on ordering inside
# `_drive`: the high-water is persisted BEFORE each fallible append, so if the
# terminal run STATE were written before the terminal EVENT, every healthy run
# would spend the duration of that final append looking exactly like a lost
# trailing write — and live SSE followers would be told "1 event(s) lost" on the
# common success path (the web store then latches the marker for the session).
#
# These tests pin the ordering through the REAL drive loop rather than a
# hand-seeded snapshot, because a snapshot test cannot observe the window: the
# defect exists only BETWEEN two awaits. They go RED if the two writes are
# swapped back.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_terminal_event_is_durable_before_the_terminal_state_is_visible(monkeypatch):
    """Observed from inside the terminal state write: the run's last event has
    ALREADY landed, so no trailing gap can be synthesized in that window."""
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))

    observed: List[dict] = []
    real_update = workflow_journal.update_run_state

    def _observing_update(run_id: str, state: str, finished_at):
        # Fires while the terminal state transition is in progress. Capture what a
        # concurrent reader would see at exactly this moment.
        if state in ("completed", "failed", "cancelled"):
            observed.append(
                {
                    "high_water": workflow_journal.persisted_high_water(run_id),
                    "max_stored": workflow_journal.max_event_seq(run_id),
                    "types": _types(run_id),
                }
            )
        return real_update(run_id, state, finished_at)

    monkeypatch.setattr(workflow_journal, "update_run_state", _observing_update)
    await ws.start_run(_spec(step_ids=("s1",)), {}, "runOrder1")

    assert observed, "the terminal state write was never observed"
    snap = observed[-1]
    # The terminal event is already durable when the state flips...
    assert (
        "run.completed" in snap["types"]
    ), f"terminal state became visible before its event landed: {snap['types']}"
    # ...so the high-water does NOT exceed the last stored seq: nothing for the
    # trailing-gap check to declare. This is the assertion that fails if the two
    # writes are reordered.
    assert snap["high_water"] == snap["max_stored"], (
        "high-water exceeded the last stored seq while the run was going terminal — "
        f"a healthy run would be reported as a trailing loss: {snap}"
    )


@pytest.mark.asyncio
async def test_healthy_completed_run_declares_no_trailing_gap(monkeypatch):
    """End-to-end: a run in which nothing was ever lost declares no gap on either
    read arm. The user-visible statement of the same defect."""
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))
    await ws.start_run(_spec(step_ids=("s1", "s2")), {}, "runOrder2")

    rows, gaps = workflow_journal.read_events_with_gaps("runOrder2")
    assert gaps == [], f"healthy run reported lost events: {gaps}"
    # The sequence really is complete and contiguous — the gap-free verdict is
    # earned, not the result of an inert check.
    seqs = sorted(r.seq for r in rows)
    assert seqs == list(range(1, len(seqs) + 1))
    assert workflow_journal.persisted_high_water("runOrder2") == max(seqs)
    # And a cursor read mid-stream agrees.
    _, tail_gaps = workflow_journal.read_events_with_gaps("runOrder2", after_seq=seqs[0])
    assert tail_gaps == []


@pytest.mark.asyncio
async def test_a_genuinely_lost_terminal_append_is_still_declared(monkeypatch):
    """The counterpart guard: the reorder must not buy quiet by going blind. When
    the terminal event's append genuinely fails, the hole is still declared."""
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))

    real_append = workflow_journal.append_event

    def _drop_terminal_append(run_id, seq, event_type, **kwargs):
        if event_type == "run.completed":
            raise sqlite3.OperationalError("simulated terminal append failure")
        return real_append(run_id, seq, event_type, **kwargs)

    monkeypatch.setattr(workflow_journal, "append_event", _drop_terminal_append)
    await ws.start_run(_spec(step_ids=("s1",)), {}, "runOrder3")

    types = _types("runOrder3")
    assert "run.completed" not in types  # the append really was dropped
    _, gaps = workflow_journal.read_events_with_gaps("runOrder3")
    assert [g.reason for g in gaps] == [
        "append_failed_trailing"
    ], f"a real trailing loss went undeclared: {gaps}"

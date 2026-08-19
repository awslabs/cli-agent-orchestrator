"""Tests for single-step re-execution against a recorded run (issue #640).

Covers ``workflow_service.replay_single_step``:

- happy path: the step's prompt resolves from the journaled inputs + the recorded
  predecessor's output, and the step runs once
- the replayed step's structured output is read back from the DERIVED key, whose
  57-char truncation exactly fills the 64-char name cap
- each replay is INDEPENDENT: a later replay that emits nothing reports ``None``
  rather than the previous replay's leftover store entry
- the SOURCE run is untouched: its journal rows and its own store entry are
  byte-identical after a replay
- ``prompt_override`` replaces the TEMPLATE, and ``{{...}}`` inside the override
  still resolves against the recorded run
- error taxonomy: unknown run / unknown step (KeyError), script tier + unresolvable
  predecessor (ValueError), corrupt snapshot (ResumeCorruptError)
- a step that fails is reported in the payload, never raised

``run_agent_step`` is mocked — no real terminals. The journal points at a temp
SQLite DB via the patched DATABASE_FILE; the two migrators create the tables in a
fixture. Runs are seeded through the journal DAL directly (not by driving the
engine), so each test states exactly the recorded state it replays against.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock

import pytest

from cli_agent_orchestrator.clients.database import (
    _migrate_workflow_run,
    _migrate_workflow_run_step,
)
from cli_agent_orchestrator.models.terminal import AgentStepResult, TerminalStatus
from cli_agent_orchestrator.models.workflow import (
    InputDecl,
    RunState,
    StepState,
    WorkflowSpec,
    WorkflowStep,
)
from cli_agent_orchestrator.models.workflow_runtime import StepOutputRecord
from cli_agent_orchestrator.services import workflow_journal
from cli_agent_orchestrator.services import workflow_service as ws
from cli_agent_orchestrator.services.agent_step import StepExecutionError
from cli_agent_orchestrator.services.step_output_store import record_step_output

_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
_RUN_ID = "runReplay"
_REPLAY_KEY = f"{_RUN_ID}-replay"


@pytest.fixture(autouse=True)
def _patched_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the journal at a temp DB, create the tables, clean the registry/store."""
    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    _migrate_workflow_run()
    _migrate_workflow_run_step()
    ws.run_registry.clear()
    ws._active_drives.clear()
    ws.step_output_store._store.clear()
    yield db_path
    ws.run_registry.clear()
    ws._active_drives.clear()
    ws.step_output_store._store.clear()


def _ok(terminal_id: str = "t1") -> AgentStepResult:
    return AgentStepResult(
        terminal_id=terminal_id, last_message="done", status=TerminalStatus.COMPLETED
    )


def _spec() -> WorkflowSpec:
    """A two-step spec whose second step templates off the first step's output."""
    return WorkflowSpec(
        name="wf",
        mode="sequential",
        inputs={"topic": InputDecl(type="string", required=True)},
        steps=[
            WorkflowStep(
                id="s1",
                provider="claude_code",
                agent="dev",
                prompt="research {{workflow.inputs.topic}}",
                output_schema=_SCHEMA,
            ),
            WorkflowStep(
                id="s2",
                provider="claude_code",
                agent="reviewer",
                prompt="write about {{steps.s1.output.answer}} for {{workflow.inputs.topic}}",
            ),
        ],
    )


def _seed_run(
    run_id: str = _RUN_ID,
    *,
    spec: Optional[WorkflowSpec] = None,
    spec_snapshot: Optional[str] = None,
    inputs: Optional[Dict[str, Any]] = None,
    outputs: Optional[Dict[str, Dict[str, Any]]] = None,
    tier: str = "yaml",
) -> None:
    """Journal a finished run: spec snapshot, resolved inputs, one row per step.

    ``spec_snapshot`` overrides the serialized spec (used to seed a corrupt one).
    A step named in ``outputs`` is journaled COMPLETED with that output; every other
    step is journaled COMPLETED with no output.
    """
    spec = spec if spec is not None else _spec()
    outputs = outputs if outputs is not None else {"s1": {"answer": "42"}}
    workflow_journal.insert_run(
        run_id=run_id,
        workflow_name=spec.name,
        spec_snapshot=spec.model_dump_json() if spec_snapshot is None else spec_snapshot,
        inputs_json=json.dumps(inputs if inputs is not None else {"topic": "cats"}),
        state=RunState.COMPLETED.value,
        started_at="2026-01-01T00:00:00Z",
        tier=tier,
    )
    workflow_journal.insert_steps(
        run_id,
        [(step.id, StepState.PENDING.value) for step in spec.steps],
        "2026-01-01T00:00:00Z",
    )
    for step in spec.steps:
        out = outputs.get(step.id)
        workflow_journal.update_step(
            run_id=run_id,
            step_id=step.id,
            state=StepState.COMPLETED.value,
            attempts=1,
            updated_at="2026-01-01T00:00:01Z",
            output_json=None if out is None else json.dumps(out),
        )


def _emitting_step(output: Dict[str, Any], *, run_key: str = _REPLAY_KEY, step_id: str = "s2"):
    """An AsyncMock side effect standing in for a worker that calls ``workflow_return``.

    The real worker POSTs its structured return, which lands in ``step_output_store``
    under the ``CAO_WORKFLOW_RUN_ID`` it was handed — so the fake writes the same
    store slot the replay reads back.
    """

    async def _side_effect(*_args, **_kwargs):
        ws.step_output_store.put(
            run_key,
            step_id,
            StepOutputRecord(
                run_id=run_key,
                step_id=step_id,
                output=output,
                validated=True,
                errors=[],
                state=StepState.COMPLETED,
            ),
        )
        return _ok()

    return _side_effect


def _journal_snapshot(run_id: str = _RUN_ID):
    """Everything the journal holds for a run, as comparable plain data."""
    row = workflow_journal.get_run(run_id)
    steps = workflow_journal.get_steps(run_id)
    return row, sorted((s.step_id, s.state, s.attempts, s.output_json) for s in steps)


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_replay_resolves_prompt_from_journaled_predecessor(monkeypatch):
    """The replayed prompt is resolved from the RECORDED run, and the step runs once."""
    _seed_run()
    step_mock = AsyncMock(return_value=_ok())
    monkeypatch.setattr(ws, "run_agent_step", step_mock)

    payload = await ws.replay_single_step(_RUN_ID, "s2")

    assert payload["prompt"] == "write about 42 for cats"
    assert payload["run_id"] == _RUN_ID
    assert payload["step_id"] == "s2"
    assert payload["provider"] == "claude_code"
    assert payload["agent"] == "reviewer"
    assert payload["terminal_id"] == "t1"
    assert payload["last_message"] == "done"
    assert payload["error"] is None

    assert step_mock.await_count == 1
    kwargs = step_mock.await_args.kwargs
    assert kwargs["prompt"] == "write about 42 for cats"
    assert kwargs["agent"] == "reviewer"
    # The step is handed the DERIVED run key, never the source run's id.
    assert kwargs["env_vars"] == {
        "CAO_WORKFLOW_RUN_ID": _REPLAY_KEY,
        "CAO_WORKFLOW_STEP_ID": "s2",
    }


@pytest.mark.asyncio
async def test_replay_returns_the_structured_output_it_emitted(monkeypatch):
    """A schema'd replay reads its output back from the derived store key."""
    _seed_run()
    monkeypatch.setattr(
        ws, "run_agent_step", AsyncMock(side_effect=_emitting_step({"answer": "fresh"}))
    )

    payload = await ws.replay_single_step(_RUN_ID, "s2")

    assert payload["output"] == {"answer": "fresh"}
    assert payload["validated"] is True


@pytest.mark.asyncio
async def test_replay_with_no_emitted_output_reports_none(monkeypatch):
    """A step that returns nothing structured is not an error — output is just None."""
    _seed_run()
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))

    payload = await ws.replay_single_step(_RUN_ID, "s2")

    assert payload["output"] is None
    assert payload["validated"] is None
    assert payload["error"] is None


@pytest.mark.asyncio
async def test_a_replay_never_reports_the_previous_replays_output(monkeypatch):
    """Every replay is independent of the ones before it: the slot is cleared first.

    The derived key is the same for every replay of this step and the store lives for
    the whole process, so an author who edits the prompt to STOP emitting structured
    output must see ``None`` — not the earlier replay's leftover.

    Deliberately no fixture help: the stale entry is written by the FIRST replay
    inside this test, exactly as it happens in a live process where nothing cleans
    the store between replays.
    """
    _seed_run()
    monkeypatch.setattr(
        ws, "run_agent_step", AsyncMock(side_effect=_emitting_step({"answer": "first"}))
    )
    first = await ws.replay_single_step(_RUN_ID, "s2")
    assert first["output"] == {"answer": "first"}
    assert ws.step_output_store.get(_REPLAY_KEY, "s2") is not None  # the stale occupant

    # Same run, same step, prompt now edited to produce no structured return.
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))
    second = await ws.replay_single_step(_RUN_ID, "s2")

    assert second["output"] is None
    assert second["validated"] is None


@pytest.mark.asyncio
async def test_derived_replay_key_fills_the_64_char_name_cap(monkeypatch):
    """A max-length run_id truncates to 57 chars so key + '-replay' is exactly 64.

    57 is the largest prefix that fits WORKFLOW_NAME_RE's 64-char cap (64 - 7), and
    ``record_step_output`` re-validates the key when the worker emits — so a longer
    prefix would 400 the worker's return and a shorter one narrows the anti-collision
    prefix for nothing.
    """
    long_run_id = "r" * 64
    _seed_run(long_run_id)
    step_mock = AsyncMock(return_value=_ok())
    monkeypatch.setattr(ws, "run_agent_step", step_mock)

    await ws.replay_single_step(long_run_id, "s2")

    derived = step_mock.await_args.kwargs["env_vars"]["CAO_WORKFLOW_RUN_ID"]
    assert derived == "r" * 57 + "-replay"
    assert len(derived) == 64
    # The boundary the worker's ``workflow_return`` crosses accepts it.
    assert record_step_output(derived, "s2", {"answer": "ok"}).run_id == derived


# ---------------------------------------------------------------------------
# the source run is never mutated (the load-bearing guarantee)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_replay_does_not_mutate_the_source_run(monkeypatch):
    """Journal rows, the source store entry and the registry all survive a replay."""
    _seed_run()
    # The source run's own recorded output for s2, as the engine would have left it.
    ws.step_output_store.put(
        _RUN_ID,
        "s2",
        StepOutputRecord(
            run_id=_RUN_ID,
            step_id="s2",
            output={"answer": "original"},
            validated=True,
            errors=[],
            state=StepState.COMPLETED,
        ),
    )
    before = _journal_snapshot()
    monkeypatch.setattr(
        ws, "run_agent_step", AsyncMock(side_effect=_emitting_step({"answer": "fresh"}))
    )

    await ws.replay_single_step(_RUN_ID, "s2")

    assert _journal_snapshot() == before
    # The recorded step's own store slot is NOT overwritten by the replay's emit.
    source_record = ws.step_output_store.get(_RUN_ID, "s2")
    assert source_record is not None
    assert source_record.output == {"answer": "original"}
    # And no live run record was fabricated for the source run.
    assert _RUN_ID not in ws.run_registry
    assert _RUN_ID not in ws._active_drives


@pytest.mark.asyncio
async def test_failed_replay_does_not_mutate_the_source_run(monkeypatch):
    """A crashed replay writes no failure back onto the recorded run either."""
    _seed_run()
    before = _journal_snapshot()
    monkeypatch.setattr(
        ws,
        "run_agent_step",
        AsyncMock(side_effect=StepExecutionError("boom", kind="error", terminal_id="t9")),
    )

    payload = await ws.replay_single_step(_RUN_ID, "s2")

    assert payload["error_kind"] == "error"
    assert _journal_snapshot() == before


# ---------------------------------------------------------------------------
# prompt override
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_prompt_override_replaces_the_template_and_still_resolves(monkeypatch):
    """The override wins over the snapshotted prompt; its ``{{...}}`` still resolve."""
    _seed_run()
    step_mock = AsyncMock(return_value=_ok())
    monkeypatch.setattr(ws, "run_agent_step", step_mock)

    payload = await ws.replay_single_step(
        _RUN_ID, "s2", prompt_override="be terse about {{steps.s1.output.answer}}"
    )

    assert payload["prompt"] == "be terse about 42"
    assert step_mock.await_args.kwargs["prompt"] == "be terse about 42"


@pytest.mark.asyncio
async def test_prompt_override_with_a_bad_reference_is_rejected_before_running(monkeypatch):
    """An override naming an unknown step fails with the reference named — no step runs."""
    _seed_run()
    step_mock = AsyncMock(return_value=_ok())
    monkeypatch.setattr(ws, "run_agent_step", step_mock)

    with pytest.raises(ValueError, match="nope"):
        await ws.replay_single_step(_RUN_ID, "s2", prompt_override="{{steps.nope.output.x}}")
    assert step_mock.await_count == 0


# ---------------------------------------------------------------------------
# error taxonomy
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unknown_run_raises_keyerror(monkeypatch):
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))
    with pytest.raises(KeyError, match="unknown run"):
        await ws.replay_single_step("noSuchRun", "s2")


@pytest.mark.asyncio
async def test_unknown_step_raises_keyerror_naming_the_step(monkeypatch):
    _seed_run()
    step_mock = AsyncMock(return_value=_ok())
    monkeypatch.setattr(ws, "run_agent_step", step_mock)
    with pytest.raises(KeyError, match="has no step 's99'"):
        await ws.replay_single_step(_RUN_ID, "s99")
    assert step_mock.await_count == 0


@pytest.mark.asyncio
async def test_script_tier_run_is_rejected(monkeypatch):
    """Script-tier runs have no spec snapshot to resolve a step from (out of scope)."""
    _seed_run(tier="script")
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))
    with pytest.raises(ValueError, match="script-tier"):
        await ws.replay_single_step(_RUN_ID, "s2")


@pytest.mark.asyncio
async def test_corrupt_spec_snapshot_raises_resume_corrupt(monkeypatch):
    _seed_run(spec_snapshot="{not-json")
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))
    with pytest.raises(ws.ResumeCorruptError, match="no usable spec snapshot"):
        await ws.replay_single_step(_RUN_ID, "s2")


@pytest.mark.asyncio
async def test_missing_predecessor_output_is_an_actionable_value_error(monkeypatch):
    """A step whose predecessor never produced output cannot be replayed — say so."""
    _seed_run(outputs={})  # s1 journaled with no output
    step_mock = AsyncMock(return_value=_ok())
    monkeypatch.setattr(ws, "run_agent_step", step_mock)

    with pytest.raises(ValueError) as exc:
        await ws.replay_single_step(_RUN_ID, "s2")

    message = str(exc.value)
    assert "s2" in message and "s1" in message
    assert "no output" in message
    assert step_mock.await_count == 0


@pytest.mark.asyncio
async def test_malformed_run_id_is_rejected(monkeypatch):
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))
    with pytest.raises(ValueError, match="run_id"):
        await ws.replay_single_step("../etc", "s2")


# ---------------------------------------------------------------------------
# a failed step is data, not an exception
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_step_failure_is_reported_not_raised(monkeypatch):
    _seed_run()
    monkeypatch.setattr(
        ws,
        "run_agent_step",
        AsyncMock(
            side_effect=StepExecutionError("timed out waiting", kind="timeout", terminal_id="t7")
        ),
    )

    payload = await ws.replay_single_step(_RUN_ID, "s2")

    assert "timed out waiting" in payload["error"]
    assert payload["error_kind"] == "timeout"
    assert payload["terminal_id"] == "t7"
    assert payload["output"] is None

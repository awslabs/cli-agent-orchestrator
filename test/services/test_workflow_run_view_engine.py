"""The run view's engine half: a YAML step's terminal recorded when it starts, and its answer.

Covers, against a real temp journal:

- ``terminal.created`` is emitted while the step runs, before its outcome, exactly once per
  attempt that opened a terminal, retries included, with the terminal id on the step row
  from that moment;
- the hook runs on the event loop thread, inside ``run_agent_step`` and before the prompt
  is sent (the assumption ``_TerminalRecorder`` is built on);
- a hook failure never fails the step, and a fake ``run_agent_step`` that ignores the hook
  still gets one ``terminal.created``;
- the cancel path still records the terminal id;
- each settled YAML step stores its answer in ``result_json`` through ``build_envelope``:
  redacted, capped at 32 KiB, an empty-text envelope for a failed or cancelled step;
- the structured-output reprompt records its own terminal on the row and emits its own
  ``terminal.created`` with the same attempt number, and its answer is the one stored;
- replay is unaffected: a YAML row with an envelope still never replays;
- resume clears a re-run step's ``result_json`` and ``terminal_id``.
"""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.api import main as api_main
from cli_agent_orchestrator.clients.database import (
    _migrate_workflow_run,
    _migrate_workflow_run_step,
)
from cli_agent_orchestrator.constants import WORKFLOW_JOURNAL_RESULT_MAX_BYTES
from cli_agent_orchestrator.models.terminal import AgentStepResult, TerminalStatus
from cli_agent_orchestrator.models.workflow import (
    RecoveryPolicy,
    RunState,
    StepState,
    WorkflowSpec,
    WorkflowStep,
)
from cli_agent_orchestrator.models.workflow_runtime import StepOutputRecord
from cli_agent_orchestrator.services import step_replay, workflow_journal
from cli_agent_orchestrator.services import workflow_service as ws
from cli_agent_orchestrator.services.agent_step import (
    StepCancelledError,
    StepExecutionError,
)
from cli_agent_orchestrator.services.step_fingerprint import StepCallFields, compute
from cli_agent_orchestrator.services.step_result import build_envelope, parse_envelope

_AGENT_STEP = "cli_agent_orchestrator.services.agent_step"
_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
# A credential shape ``secret_gate`` redacts (GitHub classic PAT: ``ghp_`` + 36 chars).
_TOKEN = "ghp_" + "A1b2C3d4E5" * 3 + "F6g7H8"


@pytest.fixture(autouse=True)
def _patched_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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


def _spec(*step_ids: str, schema: Optional[dict] = None, retries: Optional[int] = None):
    return WorkflowSpec(
        name="nightly-check",
        mode="sequential",
        steps=[
            WorkflowStep(
                id=sid,
                provider="kiro_cli",
                agent="dev",
                prompt=f"do {sid}",
                output_schema=schema,
                retries=retries,
            )
            for sid in step_ids
        ],
    )


def _ok(terminal_id: str, last_message: str = "done") -> AgentStepResult:
    return AgentStepResult(
        terminal_id=terminal_id, last_message=last_message, status=TerminalStatus.COMPLETED
    )


def _events(run_id: str) -> List[Any]:
    return workflow_journal.read_events(run_id)


def _row(run_id: str, step_id: str):
    row = workflow_journal.get_step(run_id, step_id)
    assert row is not None
    return row


def _hooked(script: List[Any]):
    """A fake ``run_agent_step`` that fires the terminal-ready hook like the real one.

    Each script entry is ``(terminal_id, outcome)``: the hook fires with ``terminal_id``
    (unless it is None), then the call returns ``outcome`` when it is an
    ``AgentStepResult`` or raises it when it is an exception.
    """
    calls = iter(script)

    async def _fake(**kwargs: Any) -> AgentStepResult:
        terminal_id, outcome = next(calls)
        hook = kwargs.get("on_step_terminal_ready")
        if terminal_id is not None and hook is not None:
            hook(terminal_id, "v2:" + "0" * 64)
        await asyncio.sleep(0)  # the real call yields (readiness wait) after the hook
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return _fake


# ---------------------------------------------------------------------------
# Event order and timing
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_terminal_created_lands_between_attempt_start_and_outcome(monkeypatch):
    monkeypatch.setattr(ws, "run_agent_step", _hooked([("ab12cd34", _ok("ab12cd34"))]))
    await ws.start_run(_spec("s1"), {}, "run-order")

    types = [e.event_type for e in _events("run-order")]
    assert types == [
        "run.started",
        "step.started",
        "step.attempt.started",
        "terminal.created",
        "step.completed",
        "run.completed",
    ]
    created = [e for e in _events("run-order") if e.event_type == "terminal.created"][0]
    # The field set is unchanged: step_id, attempt, terminal_id, provider, agent_profile.
    assert (created.step_id, created.attempt, created.terminal_id) == ("s1", 1, "ab12cd34")
    assert (created.provider, created.agent_profile) == ("kiro_cli", "dev")
    rows, gaps = workflow_journal.read_events_with_gaps("run-order")
    assert gaps == [] and [r.seq for r in rows] == list(range(1, len(rows) + 1))


@pytest.mark.asyncio
async def test_one_terminal_created_per_attempt_across_retries(monkeypatch):
    monkeypatch.setattr(
        ws,
        "run_agent_step",
        _hooked(
            [
                ("aaaa0001", StepExecutionError("t1", kind="timeout", terminal_id="aaaa0001")),
                ("aaaa0002", StepExecutionError("t2", kind="error", terminal_id="aaaa0002")),
                ("aaaa0003", _ok("aaaa0003")),
            ]
        ),
    )
    result = await ws.start_run(_spec("s1", retries=2), {}, "run-retry")
    assert result.state == RunState.COMPLETED

    step_events = [(e.event_type, e.attempt, e.terminal_id) for e in _events("run-retry")]
    assert step_events[1:-1] == [
        ("step.started", None, None),
        ("step.attempt.started", 1, None),
        ("terminal.created", 1, "aaaa0001"),
        ("step.attempt.failed", 1, "aaaa0001"),
        ("step.attempt.started", 2, None),
        ("terminal.created", 2, "aaaa0002"),
        ("step.attempt.failed", 2, "aaaa0002"),
        ("step.attempt.started", 3, None),
        ("terminal.created", 3, "aaaa0003"),
        ("step.completed", 3, "aaaa0003"),
    ]
    assert _row("run-retry", "s1").terminal_id == "aaaa0003"


@pytest.mark.asyncio
async def test_a_failed_step_records_each_attempt_and_settles_with_an_empty_answer(monkeypatch):
    monkeypatch.setattr(
        ws,
        "run_agent_step",
        _hooked(
            [
                ("bbbb0001", StepExecutionError("t1", kind="timeout", terminal_id="bbbb0001")),
                ("bbbb0002", StepExecutionError("t2", kind="timeout", terminal_id="bbbb0002")),
            ]
        ),
    )
    result = await ws.start_run(_spec("s1", retries=1), {}, "run-fail")
    assert result.state == RunState.FAILED

    types = [e.event_type for e in _events("run-fail")]
    assert types.count("terminal.created") == 2
    assert types.index("step.failed") > types.index("terminal.created")
    row = _row("run-fail", "s1")
    assert row.terminal_id == "bbbb0002"
    # The script tier's failure arms store an envelope with no text and status ``failed``.
    assert parse_envelope(row.result_json) == build_envelope("", "failed", "bbbb0002")


@pytest.mark.asyncio
async def test_the_terminal_id_is_on_the_row_and_in_the_inspection_while_the_step_runs(
    monkeypatch,
):
    seen: Dict[str, Any] = {}

    async def _fake(**kwargs: Any) -> AgentStepResult:
        kwargs["on_step_terminal_ready"]("cc33dd44", "v2:" + "0" * 64)
        for _ in range(200):  # the recording is a task on this loop; let it land
            if workflow_journal.get_step("run-live", "s1").terminal_id:
                break
            await asyncio.sleep(0.005)
        seen["row"] = workflow_journal.get_step("run-live", "s1")
        seen["types"] = [e.event_type for e in _events("run-live")]
        seen["inspection"] = await api_main.get_workflow_run_endpoint("run-live", _scopes=[])
        return _ok("cc33dd44", "the answer")

    monkeypatch.setattr(ws, "run_agent_step", _fake)
    await ws.start_run(_spec("s1"), {}, "run-live")

    assert seen["row"].state == StepState.RUNNING.value
    assert seen["row"].terminal_id == "cc33dd44"
    assert seen["types"][-1] == "terminal.created"
    step = seen["inspection"].steps[0]
    assert (step.state, step.terminal_id, step.result) == ("running", "cc33dd44", None)


@pytest.mark.asyncio
async def test_the_hook_runs_on_the_event_loop_before_the_prompt_is_sent(monkeypatch):
    """Pins the assumption ``_TerminalRecorder`` is built on, with the REAL run_agent_step.

    If ``_notify_terminal_ready`` ever moved into a worker thread, the hook could not
    create a task with ``loop.create_task`` and the recorder would need
    ``loop.call_soon_threadsafe``. This fails first.
    """
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    order: List[str] = []
    hook_threads: List[int] = []
    real_record = ws._record_step_terminal

    def _spy_record(*args: Any, **kwargs: Any):
        order.append("hook")
        hook_threads.append(threading.get_ident())
        assert asyncio.get_running_loop() is loop
        return real_record(*args, **kwargs)

    def _send_input(terminal_id: str, prompt: str, *args: Any, **kwargs: Any) -> bool:
        order.append("send_input")
        return True

    monkeypatch.setattr(ws, "_record_step_terminal", _spy_record)
    terminal = MagicMock()
    terminal.id = "ee55ff66"
    patches = (
        patch(
            f"{_AGENT_STEP}.terminal_service.create_terminal", new=AsyncMock(return_value=terminal)
        ),
        patch(f"{_AGENT_STEP}.terminal_service.send_input", new=_send_input),
        patch(f"{_AGENT_STEP}.terminal_service.delete_terminal", return_value=True),
        patch(f"{_AGENT_STEP}.terminal_service.get_output", return_value="real answer"),
        patch(f"{_AGENT_STEP}.terminal_service.exit_terminal_cli", return_value=None),
        patch(f"{_AGENT_STEP}.wait_until_status", new=AsyncMock(return_value=True)),
        patch(f"{_AGENT_STEP}.status_monitor.get_status", return_value=TerminalStatus.COMPLETED),
        patch(f"{_AGENT_STEP}.terminal_service.get_working_directory", return_value=None),
    )
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        result = await ws.start_run(_spec("s1"), {}, "run-loop")

    assert result.state == RunState.COMPLETED
    assert order == ["hook", "send_input"]
    assert hook_threads == [loop_thread]
    assert [e.event_type for e in _events("run-loop")].count("terminal.created") == 1
    assert _row("run-loop", "s1").terminal_id == "ee55ff66"


# ---------------------------------------------------------------------------
# Best-effort: a hook or journal failure never fails the step
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_hook_that_fails_does_not_fail_the_step_and_the_terminal_is_recorded_once(
    monkeypatch,
):
    real_record = ws._record_step_terminal
    calls = {"n": 0}

    def _first_call_raises(*args: Any, **kwargs: Any):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated hook failure")
        return real_record(*args, **kwargs)

    async def _fake(**kwargs: Any) -> AgentStepResult:
        hook = kwargs["on_step_terminal_ready"]
        try:  # what run_agent_step's _notify_terminal_ready does with a failing hook
            hook("dd44ee55", "v2:" + "0" * 64)
        except Exception:
            pass
        return _ok("dd44ee55")

    monkeypatch.setattr(ws, "_record_step_terminal", _first_call_raises)
    monkeypatch.setattr(ws, "run_agent_step", _fake)
    result = await ws.start_run(_spec("s1"), {}, "run-hookfail")

    assert result.state == RunState.COMPLETED
    created = [e for e in _events("run-hookfail") if e.event_type == "terminal.created"]
    assert [e.terminal_id for e in created] == ["dd44ee55"]
    assert _row("run-hookfail", "s1").terminal_id == "dd44ee55"


@pytest.mark.asyncio
async def test_a_failing_terminal_write_does_not_fail_the_step(monkeypatch):
    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(ws.workflow_journal, "set_step_terminal", _boom)
    monkeypatch.setattr(ws.workflow_journal, "set_step_result", _boom)
    monkeypatch.setattr(ws, "run_agent_step", _hooked([("ff66aa77", _ok("ff66aa77"))]))
    result = await ws.start_run(_spec("s1"), {}, "run-dbdown")

    assert result.state == RunState.COMPLETED
    assert [e.event_type for e in _events("run-dbdown")].count("terminal.created") == 1


@pytest.mark.asyncio
async def test_a_fake_that_ignores_the_hook_still_gets_one_terminal_created(monkeypatch):
    monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok("a1a1b2b2")))
    await ws.start_run(_spec("s1", "s2"), {}, "run-nohook")

    types = [e.event_type for e in _events("run-nohook")]
    assert types.count("terminal.created") == 2
    for step_id in ("s1", "s2"):
        assert _row("run-nohook", step_id).terminal_id == "a1a1b2b2"


# ---------------------------------------------------------------------------
# Cancel path
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_cancelled_step_keeps_its_terminal_id_and_an_empty_answer(monkeypatch):
    async def _cancelled_mid_wait(**kwargs: Any) -> AgentStepResult:
        kwargs["on_step_terminal_ready"]("c0c0d1d1", "v2:" + "0" * 64)
        ws.cancel_run("run-cancel")  # what an operator's cancel does while the step waits
        raise StepCancelledError(terminal_id="c0c0d1d1")

    monkeypatch.setattr(ws, "run_agent_step", _cancelled_mid_wait)
    result = await ws.start_run(_spec("s1"), {}, "run-cancel")
    assert result.state == RunState.CANCELLED

    events = _events("run-cancel")
    types = [e.event_type for e in events]
    assert types.index("terminal.created") < types.index("step.skipped")
    skipped = [e for e in events if e.event_type == "step.skipped"][0]
    assert (skipped.terminal_id, skipped.reason) == ("c0c0d1d1", "cancelled")
    row = _row("run-cancel", "s1")
    assert row.terminal_id == "c0c0d1d1"
    assert parse_envelope(row.result_json) == build_envelope("", "skipped", "c0c0d1d1")


@pytest.mark.asyncio
async def test_a_step_refused_before_any_terminal_records_none(monkeypatch):
    from cli_agent_orchestrator.utils.delegation import DelegationCeilingError

    async def _refused(**kwargs: Any) -> AgentStepResult:
        raise DelegationCeilingError("the step's tools exceed the caller's ceiling")

    monkeypatch.setattr(ws, "run_agent_step", _refused)
    await ws.start_run(_spec("s1"), {}, "run-refused")

    assert "terminal.created" not in [e.event_type for e in _events("run-refused")]
    row = _row("run-refused", "s1")
    assert row.terminal_id is None
    assert parse_envelope(row.result_json) == build_envelope("", "failed", None)


# ---------------------------------------------------------------------------
# The saved answer
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_completed_yaml_step_stores_its_answer_redacted(monkeypatch):
    answer = f"PASS. The deploy used {_TOKEN} for the check."
    monkeypatch.setattr(ws, "run_agent_step", _hooked([("12ab34cd", _ok("12ab34cd", answer))]))
    await ws.start_run(_spec("s1"), {}, "run-answer")

    envelope = parse_envelope(_row("run-answer", "s1").result_json)
    assert envelope is not None
    assert envelope == build_envelope(answer, "completed", "12ab34cd")
    assert envelope.redacted is True and envelope.truncated is False
    assert _TOKEN not in envelope.last_message
    assert envelope.last_message == "PASS. The deploy used [REDACTED:github_pat] for the check."


@pytest.mark.asyncio
async def test_a_long_answer_is_capped_at_the_journal_bound(monkeypatch):
    answer = "x" * (WORKFLOW_JOURNAL_RESULT_MAX_BYTES + 5000)
    monkeypatch.setattr(ws, "run_agent_step", _hooked([("34cd56ef", _ok("34cd56ef", answer))]))
    await ws.start_run(_spec("s1"), {}, "run-long")

    envelope = parse_envelope(_row("run-long", "s1").result_json)
    assert envelope is not None and envelope.truncated is True
    assert len(envelope.last_message.encode("utf-8")) == WORKFLOW_JOURNAL_RESULT_MAX_BYTES


@pytest.mark.asyncio
async def test_the_reprompt_records_its_terminal_with_its_own_event(monkeypatch):
    """The reprompt opens a fresh terminal inside the SAME attempt, and announces it.

    Without its own ``terminal.created``, a run view following the first terminal reads a
    410 once that terminal closes and never learns which terminal the step moved to.
    """

    calls = {"n": 0}

    async def _fake(**kwargs: Any) -> AgentStepResult:
        hook = kwargs["on_step_terminal_ready"]
        calls["n"] += 1
        if calls["n"] == 1:
            hook("0f0f0f01", "v2:" + "0" * 64)
            return _ok("0f0f0f01", "first answer, no structured output")
        # The reprompt: this time the worker returns its structured output.
        run_id = kwargs["env_vars"]["CAO_WORKFLOW_RUN_ID"]
        step_id = kwargs["env_vars"]["CAO_WORKFLOW_STEP_ID"]
        hook("0f0f0f02", "v2:" + "0" * 64)
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
        return _ok("0f0f0f02", "second answer")

    spec = WorkflowSpec(
        name="nightly-check",
        mode="sequential",
        steps=[
            WorkflowStep(
                id="s1",
                provider="kiro_cli",
                agent="dev",
                prompt="reprompt-me",
                output_schema=_SCHEMA,
            )
        ],
    )
    monkeypatch.setattr(ws, "run_agent_step", _fake)
    result = await ws.start_run(spec, {}, "run-reprompt")
    assert result.state == RunState.COMPLETED

    events = _events("run-reprompt")
    opened = [e for e in events if e.event_type == "terminal.created"]
    assert [e.terminal_id for e in opened] == ["0f0f0f01", "0f0f0f02"]
    # Both terminals belong to attempt 1: the reprompt is not a new attempt.
    assert [e.attempt for e in opened] == [1, 1]
    types = [e.event_type for e in events]
    # The second terminal is announced after the reprompt starts and before the outcome.
    assert types.index("step.reprompted") < len(types) - 1 - types[::-1].index("terminal.created")
    assert types.index("step.completed") > len(types) - 1 - types[::-1].index("terminal.created")
    row = _row("run-reprompt", "s1")
    assert row.terminal_id == "0f0f0f02"
    assert parse_envelope(row.result_json) == build_envelope(
        "second answer", "completed", "0f0f0f02"
    )


# ---------------------------------------------------------------------------
# Replay is unaffected
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_yaml_row_with_an_envelope_still_never_replays(monkeypatch):
    """``step_replay.decide`` reads any row, so prove the YAML envelope cannot reach REPLAY.

    The YAML tier writes no call fingerprint, and the gate settles an absent fingerprint
    before it would serve an envelope (rules 4 and 5). The run-step route only consults
    the gate for script-tier runs, which ``test_replay_nfr2_and_c1.py`` pins.
    """
    monkeypatch.setattr(ws, "run_agent_step", _hooked([("9a9a8b8b", _ok("9a9a8b8b", "ok"))]))
    await ws.start_run(_spec("s1"), {}, "run-replay")
    row = _row("run-replay", "s1")
    assert row.call_fingerprint is None and parse_envelope(row.result_json) is not None

    fingerprint = compute(
        StepCallFields(
            provider="kiro_cli",
            agent="dev",
            prompt="do s1",
            model=None,
            engine=None,
            allowed_tools=None,
            effective_working_directory=None,
            use_worktree=False,
            reused_terminal=False,
            timeout=600.0,
        )
    )
    for policy in (None, *RecoveryPolicy):
        decision = step_replay.decide("run-replay", "s1", fingerprint, policy)
        assert decision.verdict is not step_replay.ReplayVerdict.REPLAY
        assert decision.envelope is None


# ---------------------------------------------------------------------------
# The journal setters and resume
# ---------------------------------------------------------------------------
def test_the_single_column_setters_survive_later_state_writes():
    workflow_journal.insert_run(
        run_id="run-cols",
        workflow_name="nightly-check",
        spec_snapshot=_spec("s1").model_dump_json(),
        inputs_json="{}",
        state="running",
        started_at="2026-11-10T00:00:00Z",
    )
    workflow_journal.insert_steps("run-cols", [("s1", "pending")], "2026-11-10T00:00:00Z")
    envelope = json.dumps({"last_message": "hi", "status": "completed"})
    workflow_journal.set_step_terminal("run-cols", "s1", "aa11bb22")
    workflow_journal.set_step_result("run-cols", "s1", envelope)
    workflow_journal.update_step("run-cols", "s1", "completed", 1, "2026-11-10T00:00:01Z")

    row = _row("run-cols", "s1")
    assert (row.state, row.terminal_id, row.result_json) == ("completed", "aa11bb22", envelope)
    # A missing row is a no-op, never an insert.
    workflow_journal.set_step_terminal("run-cols", "ghost", "cc33dd44")
    assert workflow_journal.get_step("run-cols", "ghost") is None


@pytest.mark.asyncio
async def test_resume_clears_a_re_run_steps_answer_and_terminal(monkeypatch):
    monkeypatch.setattr(
        ws,
        "run_agent_step",
        _hooked(
            [
                ("1111aaaa", _ok("1111aaaa", "s1 answer")),
                ("2222bbbb", StepExecutionError("t", kind="timeout", terminal_id="2222bbbb")),
            ]
        ),
    )
    first = await ws.start_run(_spec("s1", "s2", retries=0), {}, "run-resume")
    assert first.state == RunState.FAILED
    assert parse_envelope(_row("run-resume", "s2").result_json) is not None

    seen: Dict[str, Any] = {}

    async def _second(**kwargs: Any) -> AgentStepResult:
        # s2 is about to run again: its old answer and terminal must already be gone.
        seen["row"] = workflow_journal.get_step("run-resume", "s2")
        kwargs["on_step_terminal_ready"]("3333cccc", "v2:" + "0" * 64)
        return _ok("3333cccc", "s2 answer")

    monkeypatch.setattr(ws, "run_agent_step", _second)
    resumed = await ws.resume_from_last_completed("run-resume")
    assert resumed.state == RunState.COMPLETED

    assert (seen["row"].result_json, seen["row"].terminal_id) == (None, None)
    s1, s2 = _row("run-resume", "s1"), _row("run-resume", "s2")
    # The kept step keeps its answer and terminal; the re-run step has its new ones.
    assert parse_envelope(s1.result_json) == build_envelope("s1 answer", "completed", "1111aaaa")
    assert s1.terminal_id == "1111aaaa"
    assert parse_envelope(s2.result_json) == build_envelope("s2 answer", "completed", "3333cccc")
    assert s2.terminal_id == "3333cccc"

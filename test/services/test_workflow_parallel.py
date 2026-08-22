"""Tests for the N7 parallel/pipeline workflow engine (wave scheduling).

Covers the parallel drive introduced by unit N7 in ``workflow_service.py``:
DAG ``needs`` edges, concurrent wave execution (steps with no unsatisfied
needs run together), dependency ordering (a step waits for its needs),
``pipeline`` as a linear chain, halt-on-failure semantics, cancellation,
journal event ordering under concurrency, and the grammar floor (unknown /
self / cyclic ``needs`` refs rejected at validate time).

``run_agent_step`` is mocked — no real terminals are created.
"""

from __future__ import annotations

import asyncio
from typing import List
from unittest.mock import AsyncMock

import pytest

from cli_agent_orchestrator.models.terminal import AgentStepResult, TerminalStatus
from cli_agent_orchestrator.models.workflow import WorkflowSpec, WorkflowStep, validate_only
from cli_agent_orchestrator.models.workflow_runtime import RunState, StepState
from cli_agent_orchestrator.services import workflow_service as ws
from cli_agent_orchestrator.services.agent_step import StepExecutionError


def _ok(terminal_id: str = "t1") -> AgentStepResult:
    return AgentStepResult(
        terminal_id=terminal_id, last_message="done", status=TerminalStatus.COMPLETED
    )


def _steps(*ids: str, needs: dict = None) -> List[WorkflowStep]:
    needs = needs or {}
    return [
        WorkflowStep(
            id=sid,
            provider="claude_code",
            agent="dev",
            prompt=f"do {sid}",
            needs=needs.get(sid, []),
        )
        for sid in ids
    ]


def _spec(mode: str = "parallel", steps=None, name: str = "parwf") -> WorkflowSpec:
    if steps is None:
        steps = _steps("s1", "s2")
    return WorkflowSpec(name=name, mode=mode, steps=steps)


@pytest.fixture(autouse=True)
def _clean_registry(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "cli_agent_orchestrator.constants.DATABASE_FILE", tmp_path / "wf.db", raising=True
    )
    ws.run_registry.clear()
    ws._active_drives.clear()
    ws.step_output_store._store.clear()
    yield
    ws.run_registry.clear()
    ws._active_drives.clear()
    ws.step_output_store._store.clear()


# ---------------------------------------------------------------------------
# Grammar: needs edges
# ---------------------------------------------------------------------------
class TestNeedsGrammar:
    def test_unknown_needs_ref_fails(self):
        result = validate_only("""\
name: wf
mode: parallel
steps:
  - id: s1
    provider: p
    agent: a
    prompt: x
    needs: [ghost]
""")
        assert result.status == "fail"
        assert any("needs unknown step 'ghost'" in e for e in result.errors)

    def test_self_needs_fails(self):
        result = validate_only("""\
name: wf
mode: parallel
steps:
  - id: s1
    provider: p
    agent: a
    prompt: x
    needs: [s1]
""")
        assert result.status == "fail"
        assert any("needs cannot reference itself" in e for e in result.errors)

    def test_cycle_fails(self):
        result = validate_only("""\
name: wf
mode: parallel
steps:
  - id: a
    provider: p
    agent: a
    prompt: x
    needs: [b]
  - id: b
    provider: p
    agent: a
    prompt: y
    needs: [a]
""")
        assert result.status == "fail"
        assert any("dependency cycle" in e for e in result.errors)

    def test_valid_needs_passes(self):
        result = validate_only("""\
name: wf
mode: parallel
steps:
  - id: a
    provider: p
    agent: a
    prompt: x
  - id: b
    provider: p
    agent: a
    prompt: "{{steps.a.output.answer}}"
    needs: [a]
""")
        assert result.status == "pass"
        assert result.errors == []

    def test_sequential_with_needs_still_passes(self):
        # needs are valid in sequential mode too (topo order honors them).
        result = validate_only("""\
name: wf
mode: sequential
steps:
  - id: a
    provider: p
    agent: a
    prompt: x
  - id: b
    provider: p
    agent: a
    prompt: y
    needs: [a]
""")
        assert result.status == "pass"


# ---------------------------------------------------------------------------
# Engine: wave scheduling + concurrency
# ---------------------------------------------------------------------------
class TestParallelDrive:
    @pytest.mark.asyncio
    async def test_independent_steps_run_concurrently(self, monkeypatch):
        """Two steps with no needs run in the SAME wave (overlapping execution)."""
        started: List[str] = []
        finished: List[str] = []
        overlap: List[tuple] = []

        async def _side(*a, **kw):
            step_prompt = kw["prompt"]
            sid = "s1" if step_prompt == "do s1" else "s2"
            started.append(sid)
            # Yield so the sibling coroutine can also start -> overlap.
            await asyncio.sleep(0.05)
            # If both started before either finished, they overlapped.
            if len(started) == 2 and len(finished) == 0:
                overlap.append(True)
            finished.append(sid)
            return _ok(terminal_id=sid)

        monkeypatch.setattr(ws, "run_agent_step", AsyncMock(side_effect=_side))
        res = await ws.start_run(_spec(), {}, "par-run-1")
        assert res.state == RunState.COMPLETED
        assert sorted(started) == ["s1", "s2"]
        assert overlap, "independent steps did not run concurrently"
        assert res.steps[0].state == StepState.COMPLETED
        assert res.steps[1].state == StepState.COMPLETED

    @pytest.mark.asyncio
    async def test_dependent_step_waits_for_its_needs(self, monkeypatch):
        """A step with needs starts only after its dependency settled."""
        order: List[str] = []

        async def _side(*a, **kw):
            sid = "s1" if kw["prompt"] == "do s1" else "s2"
            order.append(sid)
            return _ok(terminal_id=sid)

        monkeypatch.setattr(ws, "run_agent_step", AsyncMock(side_effect=_side))
        spec = _spec(steps=_steps("s1", "s2", needs={"s2": ["s1"]}))
        res = await ws.start_run(spec, {}, "par-run-2")
        assert res.state == RunState.COMPLETED
        # s2 must run strictly after s1 finished.
        assert order.index("s1") < order.index("s2")

    @pytest.mark.asyncio
    async def test_pipeline_runs_in_linear_chain(self, monkeypatch):
        """mode: pipeline = a linear chain; steps run one after another."""
        order: List[str] = []

        async def _side(*a, **kw):
            sid = kw["prompt"].split()[1]
            order.append(sid)
            return _ok(terminal_id=sid)

        monkeypatch.setattr(ws, "run_agent_step", AsyncMock(side_effect=_side))
        spec = WorkflowSpec(
            name="pipe",
            mode="pipeline",
            steps=[
                WorkflowStep(id="p1", provider="p", agent="a", prompt="do p1"),
                WorkflowStep(id="p2", provider="p", agent="a", prompt="do p2", needs=["p1"]),
                WorkflowStep(id="p3", provider="p", agent="a", prompt="do p3", needs=["p2"]),
            ],
        )
        res = await ws.start_run(spec, {}, "par-pipe")
        assert res.state == RunState.COMPLETED
        assert order == ["p1", "p2", "p3"]

    @pytest.mark.asyncio
    async def test_diamond_dag_runs_two_middle_steps_concurrently(self, monkeypatch):
        """a -> {b,c} -> d: b and c overlap; d waits for both."""
        started: List[str] = []
        overlap = {"bc": False}

        async def _side(*a, **kw):
            sid = kw["prompt"].split()[1]
            started.append(sid)
            await asyncio.sleep(0.05)
            if {"b", "c"}.issubset(started):
                overlap["bc"] = True
            return _ok(terminal_id=sid)

        monkeypatch.setattr(ws, "run_agent_step", AsyncMock(side_effect=_side))
        spec = _spec(
            steps=_steps(
                "a",
                "b",
                "c",
                "d",
                needs={"b": ["a"], "c": ["a"], "d": ["b", "c"]},
            )
        )
        res = await ws.start_run(spec, {}, "par-diamond")
        assert res.state == RunState.COMPLETED
        assert overlap["bc"], "b and c should overlap (same wave)"
        # d runs after both b and c finished.
        idx = {sid: i for i, sid in enumerate(started)}
        assert idx["d"] > idx["b"] and idx["d"] > idx["c"]

    @pytest.mark.asyncio
    async def test_halt_stops_scheduling_new_waves(self, monkeypatch):
        """on_failure=halt: a failed step leaves remaining steps SKIPPED."""
        calls: List[str] = []

        async def _side(*a, **kw):
            sid = kw["prompt"].split()[1]
            calls.append(sid)
            if sid == "s1":
                raise StepExecutionError("boom", kind="error", terminal_id="t")
            return _ok(terminal_id=sid)

        monkeypatch.setattr(ws, "run_agent_step", AsyncMock(side_effect=_side))
        # s2 needs s1 -> must never run after s1 halts.
        spec = _spec(
            steps=[
                WorkflowStep(
                    id="s1", provider="p", agent="a", prompt="do s1", on_failure="halt", retries=0
                ),
                WorkflowStep(id="s2", provider="p", agent="a", prompt="do s2", needs=["s1"]),
            ]
        )
        res = await ws.start_run(spec, {}, "par-halt")
        assert res.state == RunState.FAILED
        assert "s2" not in calls
        by_id = {s.id: s for s in res.steps}
        assert by_id["s1"].state == StepState.FAILED
        assert by_id["s2"].state == StepState.SKIPPED

    @pytest.mark.asyncio
    async def test_sibling_keeps_running_when_other_wave_member_halts(self, monkeypatch):
        """Independent siblings in the same wave still settle even if one halts."""
        calls: List[str] = []

        async def _side(*a, **kw):
            sid = kw["prompt"].split()[1]
            calls.append(sid)
            if sid == "s1":
                raise StepExecutionError("boom", kind="error", terminal_id="t")
            return _ok(terminal_id=sid)

        monkeypatch.setattr(ws, "run_agent_step", AsyncMock(side_effect=_side))
        spec = _spec(
            steps=[
                WorkflowStep(
                    id="s1", provider="p", agent="a", prompt="do s1", on_failure="halt", retries=0
                ),
                WorkflowStep(id="s2", provider="p", agent="a", prompt="do s2"),
            ]
        )
        res = await ws.start_run(spec, {}, "par-sib")
        assert res.state == RunState.FAILED
        by_id = {s.id: s for s in res.steps}
        assert by_id["s1"].state == StepState.FAILED
        # s2 has no dependency on s1 and was already in the same wave.
        assert by_id["s2"].state in (StepState.COMPLETED, StepState.COMPLETED_UNVALIDATED)

    @pytest.mark.asyncio
    async def test_cancel_stops_parallel_run(self, monkeypatch):
        async def _side(*a, **kw):
            await asyncio.sleep(0.2)
            return _ok()

        monkeypatch.setattr(ws, "run_agent_step", AsyncMock(side_effect=_side))
        spec = _spec()
        task = asyncio.create_task(ws.start_run(spec, {}, "par-cancel"))
        await asyncio.sleep(0.05)  # let the wave start
        ws.cancel_run("par-cancel")
        res = await task
        assert res.state == RunState.CANCELLED

    @pytest.mark.asyncio
    async def test_templating_across_dependency(self, monkeypatch):
        """A dependent step's prompt can reference its need's output."""
        seen_prompts: List[str] = []

        async def _side(*a, **kw):
            seen_prompts.append(kw["prompt"])
            return _ok(terminal_id=kw["prompt"][:2])

        monkeypatch.setattr(ws, "run_agent_step", AsyncMock(side_effect=_side))
        spec = WorkflowSpec(
            name="tpl",
            mode="parallel",
            steps=[
                WorkflowStep(
                    id="a",
                    provider="p",
                    agent="a",
                    prompt="do a",
                    output_schema={
                        "type": "object",
                        "properties": {"answer": {"type": "string"}},
                        "required": ["answer"],
                    },
                ),
                WorkflowStep(
                    id="b",
                    provider="p",
                    agent="a",
                    prompt="result was {{steps.a.output.answer}}",
                    needs=["a"],
                ),
            ],
        )
        # Provide a's output so the template resolves.
        ws.step_output_store.put(
            "par-tpl",
            "a",
            __import__(
                "cli_agent_orchestrator.models.workflow_runtime", fromlist=["StepOutputRecord"]
            ).StepOutputRecord(
                run_id="par-tpl",
                step_id="a",
                output={"answer": "42"},
                validated=True,
                errors=[],
                state=StepState.COMPLETED,
            ),
        )
        res = await ws.start_run(spec, {}, "par-tpl")
        assert res.state == RunState.COMPLETED
        assert any("result was 42" in p for p in seen_prompts)

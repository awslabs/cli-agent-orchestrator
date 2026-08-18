"""Tests for the run-step replay branch (issue #583, unit ``run-step-replay-branch``).

This is the unit where replay becomes USER-VISIBLE: it closes FR-1 and surfaces
FR-3 and FR-7 over HTTP. The route acts on ``step_replay.decide``'s verdict and
decides nothing itself, so these tests drive the gate through REAL journal rows
in an isolated temp database wherever the verdict is the thing under test, and
mock ``decide`` only where the assertion is about the route's call INTO it.

One test group per rule from
``construction/run-step-replay-branch/functional-design/business-rules.md``:

- BR-1  — the branch sits AFTER the generation fence and BEFORE ``run_agent_step``.
  Both directions: a stale-generation zombie whose step would otherwise REPLAY
  gets the fence's 409, and a REPLAY creates no terminal.
- BR-2/SR-5 — the branch engages for SCRIPT-TIER calls only; a YAML-tier caller,
  a handoff caller and a run-row-level call each reach ``run_agent_step`` with
  ``decide`` never invoked.
- BR-4  — a REPLAY creates NOTHING. Four assertions, in four tests: no terminal,
  no ``begin_step``, no ``settle_step``, and the journal row byte-identical
  afterwards. The response flag proves none of them, which is why it is not one
  of the four.
- BR-5/SR-4 — the replayed ``terminal_id`` is the ENVELOPE's (``StepRow`` has no
  such column), and ``replayed=True`` is what stops a consumer treating that
  dead id as live.
- BR-6/TD-4 — DIVERGED and DECISION_REQUIRED map to 409 in TWO SEPARATE arms.
  Two tests, one per verdict, asserting different ``kind`` values: a single
  parametrised test over both verdicts would pass with one shared arm.
- BR-7  — the halt detail carries ``rule``; the divergence detail has no ``rule``
  key at all. SIX assertions, one per halting condition surfacing its SPECIFIC
  value — "a rule is present" passes with all six returning the same one.
- PR #628 review — three findings land in this file:
  ``TestTheSixHaltingRules::test_rule_outcome_failed`` (F1, a ``failed`` row answered 200),
  ``::test_rule_envelope_lossy`` (F5, a truncated/redacted envelope served as the step's
  output with no field able to say so), and
  :class:`TestAReplayedStepIsVisibleInTheRunResult` (F4, the early return left the replayed
  step out of ``WorkflowRunResult.steps``). The F4 class carries its own BR-4 re-assertion,
  because the fix adds a callback to the replay path and BR-4 is what bounds it: in memory
  only, no terminal, no durable write.
- BR-9/SR-8 — a database failure inside ``decide`` produces 500 AND
  ``run_agent_step`` is not called. The second half is the one that matters:
  asserting the 500 alone passes even if execution already happened.
- BR-10 — the fingerprint is computed from the EFFECTIVE working directory. The
  hoist test varies only that (posted ``None``, two different ``caller_id``
  CWDs); a test varying the POSTED directory would prove nothing, because the
  route would then agree with the stored value either way.
- BR-11/SR-6 — an unknown ``recovery`` is rejected, an absent one behaves as
  undeclared, and each valid value reaches ``decide`` unchanged.
- SR-2  — neither 409 body carries a fingerprint. Digest-shaped fixtures are
  planted on both sides of the comparison and must appear nowhere.
- SR-3  — the replayed ``last_message`` is byte-identical to ``parse_envelope``'s
  output, and the route re-sanitises nothing.

THE JOURNAL IS ISOLATED PER TEST (the ``_isolated_journal`` fixture). Without it
these tests would read and write the developer's real database, and a settled row
from one run would change the verdict of the next.
"""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.clients.database import (
    _migrate_workflow_run,
    _migrate_workflow_run_step,
)
from cli_agent_orchestrator.constants import TERMINALS_RUN_STEP_ROUTE
from cli_agent_orchestrator.models.terminal import AgentStepResult, TerminalStatus
from cli_agent_orchestrator.models.workflow import RecoveryPolicy, StepResultEnvelope
from cli_agent_orchestrator.models.workflow_runtime import RunState
from cli_agent_orchestrator.services import step_replay, workflow_journal, workflow_service
from cli_agent_orchestrator.services.script_runner import ScriptRunRecord
from cli_agent_orchestrator.services.step_fingerprint import StepCallFields, compute
from cli_agent_orchestrator.services.step_replay import ReplayDecision, ReplayVerdict
from cli_agent_orchestrator.services.step_result import parse_envelope, serialise_envelope
from cli_agent_orchestrator.services.workflow_errors import HaltRule

_AGENT_STEP = "cli_agent_orchestrator.services.agent_step"
_RUN_STEP = "cli_agent_orchestrator.api.main.run_agent_step"
_DECIDE = "cli_agent_orchestrator.services.step_replay.decide"
_GET_STEP = "cli_agent_orchestrator.services.step_replay.get_step"
_GET_WD = f"{_AGENT_STEP}.terminal_service.get_working_directory"

TS = "2026-08-17T00:00:00Z"

# A legacy (pre-``v2``) fingerprint: 64 hex with no scheme prefix. Digest-shaped on
# purpose — the SR-2 tests assert it never reaches a response body.
LEGACY_FP = "a" * 64
# A well-formed current-scheme value that is NOT what any test body hashes to, so
# it forces rule 6 (divergence). Also digest-shaped, for the same reason.
FOREIGN_FP = "v2:" + "b" * 64


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolated_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the journal at a temp DB and create both #583-era tables.

    Mirrors ``test_journal_step_lifecycle.py``'s fixture. Isolation is not
    hygiene here, it is correctness: the replay verdict IS a function of the
    journal's contents, so a shared database would make every test in this file
    depend on which ones ran before it.
    """
    monkeypatch.setattr(
        "cli_agent_orchestrator.constants.DATABASE_FILE", tmp_path / "wf.db", raising=True
    )
    _migrate_workflow_run()
    _migrate_workflow_run_step()
    return tmp_path / "wf.db"


def _body(**overrides) -> dict:
    base = {"provider": "kiro_cli", "agent": "developer", "prompt": "do it"}
    base.update(overrides)
    return base


def _env(run_id: str, step_id: Optional[str] = "s1", generation: str = "1") -> dict:
    env = {"CAO_WORKFLOW_RUN_ID": run_id, "CAO_WORKFLOW_GENERATION": generation}
    if step_id is not None:
        env["CAO_WORKFLOW_STEP_ID"] = step_id
    return env


def _register_run(
    run_id: str,
    *,
    generation: str = "1",
    script_tier: bool = True,
) -> Optional[ScriptRunRecord]:
    """Journal a ``workflow_run`` row and (optionally) register a live script record.

    The journal row is what ``check_generation`` reads, so the fence passes (or
    fails) for the real reason rather than a mocked one. ``script_tier=False``
    registers NO record, which is how a YAML-tier run looks to the run-step
    route's guard: the env vars are present, the live ``ScriptRunRecord`` is not.
    """
    workflow_journal.insert_run(
        run_id=run_id,
        workflow_name="wf",
        spec_snapshot="steps: []",
        inputs_json="{}",
        state="running",
        started_at=TS,
        tier="script" if script_tier else "yaml",
        generation=generation,
    )
    if not script_tier:
        return None
    record = ScriptRunRecord(
        run_id=run_id,
        workflow_name="wf",
        state=RunState.RUNNING,
        cancelled=False,
        current_step_id=None,
        step_states={},
        process=None,
        generation=generation,
        started_at=TS,
        finished_at=None,
        tier="script",
    )
    workflow_service.run_registry[run_id] = record
    return record


@pytest.fixture(autouse=True)
def _clean_registry():
    """Drop anything this file registered, so no record leaks into another test."""
    before = dict(workflow_service.run_registry)
    yield
    workflow_service.run_registry.clear()
    workflow_service.run_registry.update(before)


def _envelope(
    last_message: str = "the stored answer",
    status: str = "completed",
    terminal_id: Optional[str] = "original-terminal",
) -> StepResultEnvelope:
    return StepResultEnvelope(last_message=last_message, status=status, terminal_id=terminal_id)


def _seed_step(
    run_id: str,
    step_id: str = "s1",
    *,
    fingerprint: Optional[str] = None,
    envelope: Optional[StepResultEnvelope] = None,
    state: str = "completed",
) -> None:
    """Write one journal row through the REAL writers the production path uses.

    ``fingerprint`` goes in via ``begin_step`` (the only writer of that column,
    exactly as ``settlement-rewire``'s terminal-ready hook does); ``state`` and
    the envelope go in via ``settle_step``. Passing ``state="running"`` stops
    after ``begin_step``, which is how a crashed-mid-execution row looks.
    """
    if fingerprint is not None:
        workflow_journal.begin_step(run_id, step_id, TS, fingerprint)
    if state == "running":
        return
    workflow_journal.settle_step(
        run_id=run_id,
        step_id=step_id,
        state=state,
        updated_at=TS,
        result_json=None if envelope is None else serialise_envelope(envelope),
        output_json=None,
        error=None,
    )


def _route_fingerprint(body: dict, *, effective_working_directory: Optional[str] = None) -> str:
    """The fingerprint the route must compute for ``body`` — the ten components.

    Assembled here from the request rather than read out of the route, so a change
    to the route's field list breaks the REPLAY tests loudly instead of silently
    agreeing with itself. ``TestTheHoist`` additionally pins the route's value
    against the one ``run_agent_step`` publishes, which is the drift guard for the
    other direction.
    """
    return compute(
        StepCallFields(
            provider=body["provider"],
            agent=body["agent"],
            prompt=body["prompt"],
            model=body.get("model"),
            engine=body.get("engine"),
            allowed_tools=(
                None if body.get("allowed_tools") is None else tuple(body["allowed_tools"])
            ),
            effective_working_directory=effective_working_directory,
            use_worktree=body.get("use_worktree", False),
            reused_terminal=body.get("reuse_terminal_id") is not None,
            timeout=body.get("timeout", 600.0),
        )
    )


def _patch_terminal_layer(*, created_id: str = "fresh-terminal", get_wd_return=None):
    """Patch the terminal layer so the REAL ``run_agent_step`` can run end to end.

    Used by the BR-4 tests: with ``run_agent_step`` mocked out, "no terminal was
    created" is vacuously true. Patching one level lower means a route that
    wrongly fell through WOULD reach ``create_terminal``, so the assertion has
    teeth.
    """
    terminal = MagicMock()
    terminal.id = created_id
    return (
        patch(
            f"{_AGENT_STEP}.terminal_service.create_terminal",
            new=AsyncMock(return_value=terminal),
        ),
        patch(f"{_AGENT_STEP}.terminal_service.send_input", return_value=True),
        patch(f"{_AGENT_STEP}.terminal_service.delete_terminal", return_value=True),
        patch(f"{_AGENT_STEP}.terminal_service.get_output", return_value="fresh output"),
        patch(f"{_AGENT_STEP}.terminal_service.exit_terminal_cli", return_value=None),
        patch(f"{_AGENT_STEP}.wait_until_status", new=AsyncMock(return_value=True)),
        patch(
            f"{_AGENT_STEP}.status_monitor.get_status",
            return_value=TerminalStatus.COMPLETED,
        ),
        patch(_GET_WD, return_value=get_wd_return),
    )


def _ok_result(terminal_id: str = "fresh-terminal") -> AgentStepResult:
    return AgentStepResult(
        terminal_id=terminal_id, last_message="fresh answer", status=TerminalStatus.COMPLETED
    )


def _raw_row(run_id: str, step_id: str = "s1") -> Optional[tuple]:
    """Read the row as stored bytes — the byte-identical comparison for BR-4."""
    from cli_agent_orchestrator.constants import DATABASE_FILE

    conn = sqlite3.connect(str(DATABASE_FILE))
    try:
        return conn.execute(
            "SELECT run_id, step_id, state, attempts, output_json, error, updated_at, "
            "call_fingerprint, result_json FROM workflow_run_step "
            "WHERE run_id = ? AND step_id = ?",
            (run_id, step_id),
        ).fetchone()
    finally:
        conn.close()


def _seed_replayable(run_id: str, body: dict, envelope: StepResultEnvelope) -> None:
    """Journal a run + a settled step whose fingerprint matches ``body`` exactly."""
    _register_run(run_id)
    _seed_step(run_id, fingerprint=_route_fingerprint(body), envelope=envelope)


# ---------------------------------------------------------------------------
# BR-4 — a REPLAY creates NOTHING. Four assertions, four tests.
# ---------------------------------------------------------------------------
class TestReplayCreatesNothing:
    """The response flag proves none of these, which is why none of them is it."""

    def test_replay_creates_no_terminal(self, client):
        body = _body(env_vars=_env("run-replay-a"))
        _seed_replayable("run-replay-a", body, _envelope())

        create, send, delete, out, exit_cli, wait, status_p, get_wd = _patch_terminal_layer()
        with create as m_create, send, delete, out, exit_cli, wait, status_p, get_wd:
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)

        assert resp.status_code == 200
        assert resp.json()["replayed"] is True
        # Asserted on the TERMINAL SERVICE, not on the response: ``run_agent_step``
        # is real here, so a fall-through would have created one.
        m_create.assert_not_awaited()

    def test_replay_does_not_call_begin_step(self, client, monkeypatch):
        body = _body(env_vars=_env("run-replay-b"))
        _seed_replayable("run-replay-b", body, _envelope())

        calls: list = []
        real_begin = workflow_journal.begin_step
        monkeypatch.setattr(
            workflow_journal,
            "begin_step",
            lambda *a, **k: (calls.append(a), real_begin(*a, **k))[1],
        )
        with patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)

        assert resp.status_code == 200
        assert calls == []

    def test_replay_does_not_call_settle_step(self, client, monkeypatch):
        body = _body(env_vars=_env("run-replay-c"))
        _seed_replayable("run-replay-c", body, _envelope())

        calls: list = []
        monkeypatch.setattr(
            workflow_journal, "settle_step", lambda *a, **k: calls.append(k) or True
        )
        with patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)

        assert resp.status_code == 200
        assert calls == []

    def test_replay_leaves_the_journal_row_byte_identical(self, client):
        body = _body(env_vars=_env("run-replay-d"))
        _seed_replayable("run-replay-d", body, _envelope())

        before = _raw_row("run-replay-d")
        assert before is not None  # the seed is real, not assumed
        with patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)

        assert resp.status_code == 200
        assert _raw_row("run-replay-d") == before


# ---------------------------------------------------------------------------
# BR-9/SR-8 — an unreadable journal produces 500 and NEVER execution.
# ---------------------------------------------------------------------------
class TestUnreadableJournalNeverExecutes:
    def test_journal_read_failure_is_500(self, client):
        body = _body(env_vars=_env("run-dberr-a"))
        _register_run("run-dberr-a")
        with (
            patch(_GET_STEP, side_effect=sqlite3.OperationalError("database is locked")),
            patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())),
        ):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)
        assert resp.status_code == 500

    def test_journal_read_failure_never_reaches_run_agent_step(self, client):
        """THE HALF THAT MATTERS. A 500 assertion alone passes even when the step
        already executed — an unreadable journal degrading to "just run it" is the
        exact re-execution FR-1 exists to prevent."""
        body = _body(env_vars=_env("run-dberr-b"))
        _register_run("run-dberr-b")
        with (
            patch(_GET_STEP, side_effect=sqlite3.OperationalError("database is locked")),
            patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())) as m_run,
        ):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)
        assert resp.status_code == 500
        m_run.assert_not_awaited()


# ---------------------------------------------------------------------------
# BR-1 — the branch sits AFTER the generation fence.
# ---------------------------------------------------------------------------
class TestFenceOrdering:
    """A fenced-out subprocess must not be handed a cached success (INV-2)."""

    def test_stale_generation_wins_over_a_replayable_step(self, client):
        # The row REPLAYS for generation "1"; this call carries the stale "1"
        # while the run has moved to "2", so the fence must fire first.
        body = _body(env_vars=_env("run-fence-a", generation="1"))
        _register_run("run-fence-a", generation="2")
        _seed_step("run-fence-a", fingerprint=_route_fingerprint(body), envelope=_envelope())

        with patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())) as m_run:
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)

        assert resp.status_code == 409
        # The FENCE's 409, not a replayed 200 and not one of the two new arms.
        assert "current generation is '2'" in resp.text
        assert resp.json()["detail"] != {}
        m_run.assert_not_awaited()

    def test_stale_generation_never_reaches_the_gate(self, client):
        body = _body(env_vars=_env("run-fence-b", generation="1"))
        _register_run("run-fence-b", generation="7")
        _seed_step("run-fence-b", fingerprint=_route_fingerprint(body), envelope=_envelope())

        # ``run_agent_step`` is patched even though the fence should stop the call
        # before it: without the patch a regression here would reach the REAL
        # terminal layer and HANG on tmux instead of failing.
        with (
            patch(_DECIDE) as m_decide,
            patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())) as m_run,
        ):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)

        assert resp.status_code == 409
        m_decide.assert_not_called()
        m_run.assert_not_awaited()


# ---------------------------------------------------------------------------
# BR-6/TD-4 — TWO separate arms. Two tests, never one parametrised over both.
# ---------------------------------------------------------------------------
class TestDivergedArm:
    def test_diverged_is_409_kind_diverged(self, client):
        body = _body(env_vars=_env("run-div"))
        _register_run("run-div")
        # Settled, current-scheme, but a DIFFERENT digest -> rule 6.
        _seed_step("run-div", fingerprint=FOREIGN_FP, envelope=_envelope())

        with patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())) as m_run:
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)

        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["kind"] == "diverged"
        assert detail["step_id"] == "s1"
        # BR-7: no ``rule`` key AT ALL — divergence is always the same condition,
        # and a constant attribute is the inert-field trap.
        assert "rule" not in detail
        m_run.assert_not_awaited()


class TestDecisionRequiredArm:
    def test_decision_required_is_409_kind_decision_required(self, client):
        body = _body(env_vars=_env("run-halt"))
        _register_run("run-halt")
        # Dispatched, outcome unknown, no policy -> rule 2.
        _seed_step("run-halt", fingerprint=_route_fingerprint(body), state="running")

        with patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())) as m_run:
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)

        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["kind"] == "decision_required"
        assert detail["step_id"] == "s1"
        assert detail["rule"] == HaltRule.INTERRUPTED_NO_POLICY.value
        m_run.assert_not_awaited()

    def test_the_two_kinds_are_distinguishable_from_the_fence_and_each_other(self, client):
        """BR-8: three 409s are reachable from this route and ``kind`` is what a
        consumer branches on, so all three must stay distinct."""
        div = _body(env_vars=_env("run-kinds-div"))
        _register_run("run-kinds-div")
        _seed_step("run-kinds-div", fingerprint=FOREIGN_FP, envelope=_envelope())

        halt = _body(env_vars=_env("run-kinds-halt"))
        _register_run("run-kinds-halt")
        _seed_step("run-kinds-halt", fingerprint=_route_fingerprint(halt), state="running")

        fence = _body(env_vars=_env("run-kinds-fence", generation="1"))
        _register_run("run-kinds-fence", generation="9")

        with patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())):
            r_div = client.post(TERMINALS_RUN_STEP_ROUTE, json=div)
            r_halt = client.post(TERMINALS_RUN_STEP_ROUTE, json=halt)
            r_fence = client.post(TERMINALS_RUN_STEP_ROUTE, json=fence)

        assert r_div.status_code == r_halt.status_code == r_fence.status_code == 409
        assert r_div.json()["detail"]["kind"] == "diverged"
        assert r_halt.json()["detail"]["kind"] == "decision_required"
        # The fence's detail is a plain string, so it can never be mistaken for
        # either structured kind.
        assert isinstance(r_fence.json()["detail"], str)


# ---------------------------------------------------------------------------
# BR-7 — each of the six halting conditions surfaces its SPECIFIC rule.
# ---------------------------------------------------------------------------
class TestTheSixHaltingRules:
    """Six assertions, not one "a rule is present": with all six returning the
    same value, the weaker test would still pass.

    The last two arrived with PR #628's review and are asserted THROUGH THE ROUTE, not only at
    the gate, because the route is where an operator reads the code — and both rules exist to
    stop this route answering HTTP 200 for a result that is not a faithful substitute for the
    original call.
    """

    def test_rule_interrupted_no_policy(self, client):
        body = _body(env_vars=_env("run-r2"))
        _register_run("run-r2")
        _seed_step("run-r2", fingerprint=_route_fingerprint(body), state="running")
        with patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)
        assert resp.status_code == 409
        assert resp.json()["detail"]["rule"] == "interrupted_no_policy"

    def test_rule_envelope_absent(self, client):
        body = _body(env_vars=_env("run-r3"))
        _register_run("run-r3")
        # Settled with a matching current-scheme fingerprint but NO envelope.
        _seed_step("run-r3", fingerprint=_route_fingerprint(body), envelope=None)
        with patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)
        assert resp.status_code == 409
        assert resp.json()["detail"]["rule"] == "envelope_absent"

    def test_rule_provenance_unverifiable(self, client):
        body = _body(env_vars=_env("run-r5"))
        _register_run("run-r5")
        _seed_step("run-r5", fingerprint=LEGACY_FP, envelope=_envelope())
        with patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)
        assert resp.status_code == 409
        assert resp.json()["detail"]["rule"] == "provenance_unverifiable"

    def test_rule_policy_manual(self, client):
        body = _body(env_vars=_env("run-r7"), recovery="manual")
        _register_run("run-r7")
        # A VERIFIED match that halts anyway, because a human asked to see it.
        _seed_step("run-r7", fingerprint=_route_fingerprint(body), envelope=_envelope())
        with patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)
        assert resp.status_code == 409
        assert resp.json()["detail"]["rule"] == "policy_manual"

    def test_rule_outcome_failed(self, client):
        """PR #628 review (Copilot F1). A ``failed`` row previously REPLAYED: this route
        answered 200 with a ``StepHandle`` for a call that raised on the original drive, so the
        script continued past a failure that had been silently deleted from the replayed run.
        """
        body = _body(env_vars=_env("run-r8"))
        _register_run("run-r8")
        # Exactly the row the production settler writes for a failed step: state ``failed``,
        # a matching current-scheme fingerprint, and an envelope (``result-envelope`` BR-1
        # writes one unconditionally).
        _seed_step(
            "run-r8",
            fingerprint=_route_fingerprint(body),
            envelope=_envelope(last_message="", status="failed"),
            state="failed",
        )
        with patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())) as m_run:
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)
        assert resp.status_code == 409
        assert resp.json()["detail"]["kind"] == "decision_required"
        assert resp.json()["detail"]["rule"] == "outcome_failed"
        # Fail-CLOSED, not fail-open: halting must not become silent re-execution.
        m_run.assert_not_awaited()

    def test_rule_envelope_lossy(self, client):
        """PR #628 review (Copilot F5). ``build_envelope`` redacts then bounds the text before
        storage, while the SUCCESS arm of this route answers with ``run_agent_step``'s raw
        ``last_message`` — and ``RunStepResponse`` has no ``truncated``/``redacted`` field, so a
        replayed response cannot even say that what it served is abridged.
        """
        body = _body(env_vars=_env("run-r9"))
        _register_run("run-r9")
        lossy = StepResultEnvelope(
            last_message="[REDACTED:aws-secret-key] tail",
            status="completed",
            terminal_id="original-terminal",
            truncated=True,
            redacted=True,
        )
        _seed_step("run-r9", fingerprint=_route_fingerprint(body), envelope=lossy)
        with patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())) as m_run:
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)
        assert resp.status_code == 409
        assert resp.json()["detail"]["kind"] == "decision_required"
        assert resp.json()["detail"]["rule"] == "envelope_lossy"
        m_run.assert_not_awaited()

    def test_the_six_rule_values_are_all_different(self, client):
        """The forcing function for the six tests above: if a later edit made two
        conditions report the same code, each individual assertion could still be
        "corrected" to match, but this one could not."""
        assert len({r.value for r in HaltRule}) == 6


# ---------------------------------------------------------------------------
# SR-2 — neither 409 detail carries a fingerprint. Ever.
# ---------------------------------------------------------------------------
class TestNoDigestInEitherBody:
    def test_diverged_body_carries_neither_digest(self, client):
        body = _body(env_vars=_env("run-nodig-a"))
        _register_run("run-nodig-a")
        stored = FOREIGN_FP
        _seed_step("run-nodig-a", fingerprint=stored, envelope=_envelope())
        computed = _route_fingerprint(body)

        with patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)

        assert resp.status_code == 409
        text = resp.text
        for digest in (stored, computed, stored.removeprefix("v2:"), computed.removeprefix("v2:")):
            assert digest not in text
            # Not even truncated: a 16-char prefix is still a digest.
            assert digest[:16] not in text

    def test_decision_required_body_carries_neither_digest(self, client):
        body = _body(env_vars=_env("run-nodig-b"))
        _register_run("run-nodig-b")
        stored = LEGACY_FP
        _seed_step("run-nodig-b", fingerprint=stored, envelope=_envelope())
        computed = _route_fingerprint(body)

        with patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)

        assert resp.status_code == 409
        text = resp.text
        for digest in (stored, computed, computed.removeprefix("v2:")):
            assert digest not in text
            assert digest[:16] not in text


# ---------------------------------------------------------------------------
# BR-10 — the fingerprint comes from the EFFECTIVE working directory.
# ---------------------------------------------------------------------------
class TestTheHoist:
    """VARYING THE POSTED DIRECTORY WOULD PROVE NOTHING. These vary only the
    EFFECTIVE one: the posted value is ``None`` in every call and the difference
    is the caller terminal's CWD."""

    def test_two_effective_directories_do_not_replay_each_other(self, client):
        run_id = "run-hoist-a"
        body_one = _body(env_vars=_env(run_id), caller_id="sup-one")
        body_two = _body(env_vars=_env(run_id), caller_id="sup-two")
        # Both post working_directory=None; only the inherited CWD differs.
        assert "working_directory" not in body_one

        _register_run(run_id)
        _seed_step(
            run_id,
            fingerprint=_route_fingerprint(body_one, effective_working_directory="/cwd/one"),
            envelope=_envelope(),
        )

        with (
            patch(_GET_WD, return_value="/cwd/one"),
            patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())),
        ):
            replayed = client.post(TERMINALS_RUN_STEP_ROUTE, json=body_one)
        assert replayed.status_code == 200
        assert replayed.json()["replayed"] is True

        with (
            patch(_GET_WD, return_value="/cwd/two"),
            patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())) as m_run,
        ):
            other = client.post(TERMINALS_RUN_STEP_ROUTE, json=body_two)
        # A different effective directory is a different call: it must NOT be
        # served the first one's stored result.
        assert other.status_code == 409
        assert other.json()["detail"]["kind"] == "diverged"
        m_run.assert_not_awaited()

    def test_a_row_keyed_on_the_posted_directory_never_replays(self, client):
        """The direct FALSE-REPLAY detector, and the one that fails ONLY under the
        wrong implementation. The seeded row's fingerprint was computed from the
        POSTED value (``None``): a route that hashed the posted directory would
        match it and serve a stored result for a call that ran somewhere else. A
        route that hashes the EFFECTIVE directory must report divergence."""
        run_id = "run-hoist-d"
        body = _body(env_vars=_env(run_id), caller_id="sup-one")
        _register_run(run_id)
        _seed_step(
            run_id,
            fingerprint=_route_fingerprint(body, effective_working_directory=None),
            envelope=_envelope(),
        )

        with (
            patch(_GET_WD, return_value="/cwd/elsewhere"),
            patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())) as m_run,
        ):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)

        assert resp.status_code == 409
        assert resp.json()["detail"]["kind"] == "diverged"
        assert m_run.await_count == 0

    def test_the_resolution_runs_once(self, client):
        """The route resolved, so ``run_agent_step``'s own inheritance guard must
        not fire — no flag, no second lookup."""
        run_id = "run-hoist-b"
        body = _body(env_vars=_env(run_id), caller_id="sup-one")
        _register_run(run_id)  # no step row -> EXECUTE, so the whole path runs

        create, send, delete, out, exit_cli, wait, status_p, _unused = _patch_terminal_layer()
        with (
            create,
            send,
            delete,
            out,
            exit_cli,
            wait,
            status_p,
            patch(_GET_WD, return_value="/cwd/one") as m_get_wd,
        ):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)

        assert resp.status_code == 200
        assert m_get_wd.call_count == 1

    def test_the_route_and_run_agent_step_compute_the_same_fingerprint(self, client):
        """The drift guard for the two call sites of ``compute`` (TD-2). If either
        field list changes without the other, the value the gate compares stops
        matching the value ``begin_step`` stores and replay silently dies as a
        permanent false DIVERGED."""
        run_id = "run-hoist-c"
        body = _body(env_vars=_env(run_id), caller_id="sup-one", model="fable-5")
        _register_run(run_id)

        gate_fingerprints: list = []
        stored_fingerprints: list = []

        # Both spies bind the REAL callable before the patch is installed —
        # resolving ``step_replay.decide`` inside the spy would resolve the patch
        # and recurse forever.
        real_decide = step_replay.decide

        def _spy_decide(r_id, s_id, fingerprint, policy):
            gate_fingerprints.append(fingerprint)
            return real_decide(r_id, s_id, fingerprint, policy)

        real_begin = workflow_journal.begin_step

        def _spy_begin(r_id, s_id, updated_at, call_fingerprint):
            stored_fingerprints.append(call_fingerprint)
            return real_begin(r_id, s_id, updated_at, call_fingerprint)

        create, send, delete, out, exit_cli, wait, status_p, _unused = _patch_terminal_layer()
        with (
            create,
            send,
            delete,
            out,
            exit_cli,
            wait,
            status_p,
            patch(_GET_WD, return_value="/cwd/one"),
            patch(_DECIDE, side_effect=_spy_decide),
            patch.object(workflow_journal, "begin_step", _spy_begin),
        ):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)

        assert resp.status_code == 200
        assert len(gate_fingerprints) == 1
        assert len(stored_fingerprints) == 1
        assert gate_fingerprints[0] == stored_fingerprints[0]


# ---------------------------------------------------------------------------
# BR-5/SR-4 — the replayed terminal_id and the flag that makes it safe.
# ---------------------------------------------------------------------------
class TestReplayedTerminalId:
    def test_replayed_terminal_id_is_the_envelopes(self, client):
        body = _body(env_vars=_env("run-tid-a"))
        envelope = _envelope(terminal_id="dead-terminal-42")
        _seed_replayable("run-tid-a", body, envelope)

        with patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result("live-terminal"))):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)

        assert resp.status_code == 200
        data = resp.json()
        # ``StepRow`` has no ``terminal_id`` column: the envelope is the only source.
        assert data["terminal_id"] == "dead-terminal-42"
        assert data["terminal_id"] != "live-terminal"

    def test_replayed_is_true_whenever_the_terminal_is_not_live(self, client):
        body = _body(env_vars=_env("run-tid-b"))
        _seed_replayable("run-tid-b", body, _envelope(terminal_id="dead-terminal-43"))

        with patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)

        data = resp.json()
        # The two must never diverge: the flag is the dead id's only mitigation.
        assert data["terminal_id"] == "dead-terminal-43"
        assert data["replayed"] is True
        assert data["status"] == "completed"


# ---------------------------------------------------------------------------
# BR-2/SR-5 — the branch engages for script-tier calls ONLY.
# ---------------------------------------------------------------------------
class TestTheScriptTierGuard:
    def test_yaml_tier_call_reaches_run_agent_step_with_no_gate_call(self, client):
        """Both env vars present, but no live ``ScriptRunRecord`` — which is what a
        YAML-tier run looks like to this guard."""
        run_id = "run-yaml"
        _register_run(run_id, script_tier=False)
        body = _body(env_vars=_env(run_id))
        # A row that WOULD replay if the gate were consulted.
        _seed_step(run_id, fingerprint=_route_fingerprint(body), envelope=_envelope())

        with (
            patch(_DECIDE) as m_decide,
            patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())) as m_run,
        ):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)

        assert resp.status_code == 200
        assert resp.json()["replayed"] is False
        m_decide.assert_not_called()
        m_run.assert_awaited_once()

    def test_handoff_call_reaches_run_agent_step_with_no_gate_call(self, client):
        with (
            patch(_DECIDE) as m_decide,
            patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())) as m_run,
        ):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(caller_id="sup-1"))

        assert resp.status_code == 200
        m_decide.assert_not_called()
        m_run.assert_awaited_once()
        # A handoff caller's directory still resolves inside ``run_agent_step``.
        assert m_run.await_args.kwargs["working_directory"] is None
        assert m_run.await_args.kwargs["caller_id"] == "sup-1"

    def test_run_row_level_call_without_step_id_never_reaches_the_gate(self, client):
        """RUN_ID + GENERATION with no STEP_ID is a legal run-row-level call, and
        there is no step key to decide about."""
        run_id = "run-nostep"
        _register_run(run_id)
        with (
            patch(_DECIDE) as m_decide,
            patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())) as m_run,
        ):
            resp = client.post(
                TERMINALS_RUN_STEP_ROUTE, json=_body(env_vars=_env(run_id, step_id=None))
            )

        assert resp.status_code == 200
        m_decide.assert_not_called()
        m_run.assert_awaited_once()


# ---------------------------------------------------------------------------
# BR-11/SR-6 — the declared policy.
# ---------------------------------------------------------------------------
class TestRecoveryField:
    def test_unknown_recovery_value_is_rejected(self, client):
        """Rejected, NEVER coerced to undeclared: the two differ at the gate's
        rule 2 and at its catch-all, so conflating them would change the verdict."""
        with (
            patch(_DECIDE) as m_decide,
            patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())) as m_run,
        ):
            resp = client.post(
                TERMINALS_RUN_STEP_ROUTE,
                json=_body(env_vars=_env("run-rec-bad"), recovery="idempotant"),
            )

        assert resp.status_code == 422
        m_decide.assert_not_called()
        m_run.assert_not_awaited()

    def test_the_lint_message_names_the_real_gap_and_the_route_agrees(self, client):
        """PR #628 review (Copilot F3) — the two halves of one claim, asserted together.

        The ``unenforced-recovery-policy`` finding used to say a ``recovery=`` on ``run_step``
        was "never validated". This test binds the corrected message to the behaviour it
        describes, IN ONE PLACE, because that is the only way the two stop drifting: the
        message claims a 422, so the route is driven with a bad value and must produce one; the
        message claims the gap is client-side, so a VALID value must be accepted and honoured.

        Two assertions about the same fact from opposite directions. The message alone could be
        re-worded to match a broken route; the route alone could be correct while the message
        lied about it, which is exactly what shipped.
        """
        from cli_agent_orchestrator.services.script_lint import lint_script

        source = (
            "from cao_workflow import run_step\n"
            "run_step('kiro_cli', 'developer', 'x', recovery='idempotant')\n"
        )
        findings = [
            f
            for f in lint_script(source, "s.py").findings
            if f.rule_id == "unenforced-recovery-policy"
        ]
        assert len(findings) == 1
        assert "422" in findings[0].message
        assert "never validated" not in findings[0].message

        # Half 1 — the 422 the message promises. Same bad value the linted source uses.
        with patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())) as m_run:
            bad = client.post(
                TERMINALS_RUN_STEP_ROUTE,
                json=_body(env_vars=_env("run-f3-bad"), recovery="idempotant"),
            )
        assert bad.status_code == 422
        m_run.assert_not_awaited()

        # Half 2 — the gap really is only client-side: a VALID value on ``run_step``'s wire
        # shape is accepted and reaches the gate as the declared policy.
        run_id = "run-f3-good"
        _register_run(run_id)
        seen: list = []

        def _spy_decide(r_id, s_id, fingerprint, policy):
            seen.append(policy)
            return ReplayDecision(
                verdict=ReplayVerdict.EXECUTE, envelope=None, reason="test", rule=None
            )

        with (
            patch(_DECIDE, side_effect=_spy_decide),
            patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())),
        ):
            good = client.post(
                TERMINALS_RUN_STEP_ROUTE,
                json=_body(env_vars=_env(run_id), recovery="idempotent"),
            )
        assert good.status_code == 200
        # The MEMBER, not the string — the gate compares identity against ``RecoveryPolicy``.
        assert seen == [RecoveryPolicy.IDEMPOTENT]

    def test_absent_recovery_yields_the_undeclared_behaviour(self, client):
        """Undeclared REPLAYS at the catch-all where ``manual`` halts at rule 7 — the one
        place the two differ, so this is what discriminates them."""
        run_id = "run-rec-absent"
        body = _body(env_vars=_env(run_id))
        assert "recovery" not in body
        _seed_replayable(run_id, body, _envelope())

        with patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())):
            undeclared = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)
        assert undeclared.status_code == 200
        assert undeclared.json()["replayed"] is True

        # Same row, same fields, ``manual`` declared -> halts instead.
        manual_body = _body(env_vars=_env(run_id), recovery="manual")
        with patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())):
            manual = client.post(TERMINALS_RUN_STEP_ROUTE, json=manual_body)
        assert manual.status_code == 409
        assert manual.json()["detail"]["rule"] == "policy_manual"

    @pytest.mark.parametrize("value", ["idempotent", "reconcile", "manual"])
    def test_each_valid_recovery_value_reaches_decide_unchanged(self, client, value):
        run_id = f"run-rec-{value}"
        _register_run(run_id)
        seen: list = []

        def _spy_decide(r_id, s_id, fingerprint, policy):
            seen.append(policy)
            return ReplayDecision(
                verdict=ReplayVerdict.EXECUTE, envelope=None, reason="test", rule=None
            )

        with (
            patch(_DECIDE, side_effect=_spy_decide),
            patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())),
        ):
            resp = client.post(
                TERMINALS_RUN_STEP_ROUTE,
                json=_body(env_vars=_env(run_id), recovery=value),
            )

        assert resp.status_code == 200
        # The MEMBER, not the string: ``decide`` compares identity against
        # ``RecoveryPolicy`` members, so a bare string would silently miss.
        assert seen == [RecoveryPolicy(value)]

    def test_absent_recovery_reaches_decide_as_none(self, client):
        run_id = "run-rec-none"
        _register_run(run_id)
        seen: list = []

        def _spy_decide(r_id, s_id, fingerprint, policy):
            seen.append(policy)
            return ReplayDecision(
                verdict=ReplayVerdict.EXECUTE, envelope=None, reason="test", rule=None
            )

        with (
            patch(_DECIDE, side_effect=_spy_decide),
            patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())),
        ):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(env_vars=_env(run_id)))

        assert resp.status_code == 200
        assert seen == [None]


# ---------------------------------------------------------------------------
# SR-3 — the envelope is returned VERBATIM; this route adds nothing.
# ---------------------------------------------------------------------------
class TestEnvelopeReturnedVerbatim:
    def test_last_message_is_byte_identical_to_parse_envelope(self, client):
        """A second ``redact_secrets`` pass can match its own ``[REDACTED:<name>]``
        marker, which would make a sanitised message look tampered with. The stored
        text is already redacted and bounded, so it is returned as-is."""
        stored_text = (
            "done: token=[REDACTED:aws_access_key_id] and a literal "
            "[REDACTED:github_token] plus unicode é中 and a tab\tend"
        )
        run_id = "run-verbatim"
        body = _body(env_vars=_env(run_id))
        envelope = _envelope(last_message=stored_text)
        _seed_replayable(run_id, body, envelope)

        with patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)

        assert resp.status_code == 200
        row = workflow_journal.get_step(run_id, "s1")
        assert row is not None
        from_disk = parse_envelope(row.result_json)
        assert from_disk is not None
        assert resp.json()["last_message"] == from_disk.last_message == stored_text

    def test_the_route_never_re_sanitises_and_never_bypasses_the_envelope(self):
        """Scoped to ``run_step``'s own source, not the module: ``api/main.py``
        imports ``secret_gate`` for the graph-export endpoint, which is unrelated
        to this route and must not make the check vacuous."""
        from cli_agent_orchestrator.api.main import run_step

        source = inspect.getsource(run_step)
        # Strip comments/docstring prose, which legitimately DISCUSS these names.
        code_lines = [
            line.split("#", 1)[0]
            for line in source.splitlines()
            if not line.strip().startswith("#")
        ]
        code = "\n".join(code_lines)
        for forbidden in ("secret_gate", "redact_secrets", "build_envelope", "result_json"):
            assert forbidden not in code, forbidden


# ---------------------------------------------------------------------------
# BR-3/SR-7 — the route ACTS on the verdict and decides nothing; it logs nothing new.
# ---------------------------------------------------------------------------
class TestTheRouteDecidesNothing:
    def test_the_route_reimplements_no_part_of_the_decision_order(self):
        """The decision order must exist in exactly ONE place (INV-3). The
        temptation to "just check this one case" at the boundary is how a second
        copy of it appears, so the absence is asserted rather than trusted."""
        import re

        from cli_agent_orchestrator.api.main import run_step

        code = _code_of(run_step)
        # No scheme classification, no policy comparison, no fingerprint comparison.
        assert "scheme_of" not in code
        assert "RecoveryPolicy." not in code
        assert not re.search(r"fingerprint\s*[!=]=", code)
        assert "_REEXECUTION_PERMITTED" not in code
        # The route reads exactly two things off the decision: the verdict and,
        # on a halt, the rule.
        assert "decision.verdict" in code
        assert "decision.rule" in code

    def test_the_route_logs_only_inside_best_effort_bookkeeping_guards(self):
        """SR-7: the gate logs nothing by design and the decision log belongs to
        unit 12. A replay is a normal successful outcome; logging every one would
        make the quiet path noisy and create a new place for a digest to leak.

        TWO calls since PR #628's review, not one, and the shape is what is asserted rather
        than the count alone: both are ``logger.warning`` inside an ``except`` guard around
        best-effort step bookkeeping — ``_settle_step``'s (``settlement-rewire``) and
        ``_record_replayed_step``'s (F4). Neither logs on the success path and neither can
        carry a digest, an envelope or a prompt: each renders a fixed sentence plus
        ``exc_info``. What this test still forbids is a log line on the REPLAY path itself,
        which is the noise-and-leak surface SR-7 is about.
        """
        import re

        from cli_agent_orchestrator.api.main import run_step

        code = _code_of(run_step)
        calls = re.findall(r"logger\.\w+", code)
        assert calls == ["logger.warning", "logger.warning"]
        # Every call is a WARNING inside a bookkeeping guard — never an info/debug narration
        # of a replay, and never one that interpolates a value.
        assert "logger.info" not in code
        assert "logger.debug" not in code
        for line in code.splitlines():
            if "logger." in line:
                assert "exc_info=True" in line or "bookkeeping" in line


def _code_of(func) -> str:
    """A function's source with comment lines stripped.

    Comments and docstrings legitimately DISCUSS the names these checks forbid, so
    a raw text scan of the source would match its own prose.
    """
    source = inspect.getsource(func)
    return "\n".join(
        line.split("#", 1)[0] for line in source.splitlines() if not line.strip().startswith("#")
    )


# ---------------------------------------------------------------------------
# TD-5 — compatibility: ``replayed`` defaults to False.
# ---------------------------------------------------------------------------
class TestCompatibility:
    def test_an_executed_script_step_reports_replayed_false(self, client):
        run_id = "run-compat-a"
        _register_run(run_id)  # no step row -> EXECUTE
        with patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())) as m_run:
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(env_vars=_env(run_id)))

        assert resp.status_code == 200
        assert resp.json()["replayed"] is False
        m_run.assert_awaited_once()

    def test_an_ordinary_handoff_response_reports_replayed_false(self, client):
        with patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body())

        assert resp.status_code == 200
        data = resp.json()
        assert data["replayed"] is False
        # The rest of the response is unchanged for every existing consumer.
        assert data["terminal_id"] == "fresh-terminal"
        assert data["last_message"] == "fresh answer"
        assert data["status"] == "completed"

    def test_the_scope_dependency_is_unchanged(self):
        """SR-1: this unit adds fields to an already-guarded route. It must not
        weaken or duplicate the authorisation path."""
        from cli_agent_orchestrator.api.main import run_step

        signature = inspect.signature(run_step)
        assert "_scopes" in signature.parameters
        source = inspect.getsource(run_step)
        assert "Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN))" in source


# ---------------------------------------------------------------------------
# PR #628 review, P1 — replay exposes the original response status, not journal state.
# ---------------------------------------------------------------------------
class TestReplayPreservesOriginalResponseStatus:
    def test_unvalidated_completion_replays_the_live_completed_status(self, client):
        """A schema-invalid structured output is a journal-quality distinction, not a changed
        run-step response: live and replay must both answer ``completed`` while the durable
        row remains ``completed_unvalidated`` for the YAML-tier-compatible state machine."""
        from cli_agent_orchestrator.services.step_output_store import record_step_output

        run_id = "run-unvalidated-status"
        body = _body(env_vars=_env(run_id))
        _register_run(run_id)
        record_step_output(
            run_id,
            "s1",
            {"answer": "not-an-integer"},
            {
                "type": "object",
                "properties": {"answer": {"type": "integer"}},
                "required": ["answer"],
            },
        )

        async def _run_with_terminal_ready(**kwargs):
            callback = kwargs["on_step_terminal_ready"]
            assert callback is not None
            callback("fresh-terminal", _route_fingerprint(body))
            return _ok_result()

        with patch(_RUN_STEP, new=AsyncMock(side_effect=_run_with_terminal_ready)):
            live = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)
        replay = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)

        assert live.status_code == 200
        assert replay.status_code == 200
        assert live.json()["status"] == "completed"
        assert replay.json()["status"] == live.json()["status"]
        row = workflow_journal.get_step(run_id, "s1")
        assert row is not None
        assert row.state == "completed_unvalidated"


# ---------------------------------------------------------------------------
# PR #628 review, Copilot F4 — a REPLAY must still appear in the run's step list.
# ---------------------------------------------------------------------------
class TestAReplayedStepIsVisibleInTheRunResult:
    """The defect: the early replay return bypasses BOTH script callbacks.

    ``make_step_terminal_recorder`` is what seeds ``ScriptRunRecord.step_states[step_id]`` and
    ``record_step_completion`` is what settles it, and neither fires on the replay path —
    correctly, since a replay must create no terminal and write no durable row (BR-4). But
    ``_finalize`` builds ``WorkflowRunResult.steps`` ONLY from that in-memory map, and
    ``resume_script_run`` reconstructs the record with ``step_states={}``. So a fully replayed
    resume reported ``steps=[]`` while every journal row was sitting right there.

    The fix records the replayed step in memory ONLY, hydrated from the durable row — which is
    why :class:`TestReplayCreatesNothing` above still passes unchanged.
    """

    def test_a_replayed_step_appears_in_the_live_record(self, client):
        run_id = "run-f4-visible"
        body = _body(env_vars=_env(run_id))
        record = _register_run(run_id)
        assert record is not None
        _seed_step(run_id, fingerprint=_route_fingerprint(body), envelope=_envelope())
        assert record.step_states == {}  # nothing yet — this is a resumed drive's starting state

        with patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())) as m_run:
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)

        assert resp.status_code == 200
        assert resp.json()["replayed"] is True
        m_run.assert_not_awaited()  # still no execution

        # THE ASSERTION F4 IS ABOUT.
        assert "s1" in record.step_states
        st = record.step_states["s1"]
        assert st.step_id == "s1"

    def test_the_recorded_state_comes_from_the_durable_row_not_a_default(self, client):
        """Hydrated from the journal, not invented. Asserted on values that could not have
        been guessed: the ``attempts`` count the original drive reached and the terminal id the
        envelope carries. A recorder that wrote ``StepRunState(step_id=...)`` and nothing else
        would pass the previous test and fail this one.
        """
        run_id = "run-f4-hydrated"
        body = _body(env_vars=_env(run_id))
        record = _register_run(run_id)
        assert record is not None
        _seed_step(
            run_id,
            fingerprint=_route_fingerprint(body),
            envelope=_envelope(terminal_id="original-terminal"),
        )

        with patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)
        assert resp.json()["replayed"] is True

        row = workflow_journal.get_step(run_id, "s1")
        assert row is not None
        st = record.step_states["s1"]
        assert st.state.value == row.state
        assert st.attempts == row.attempts
        assert st.attempts >= 1  # the original attempt is not erased by the replay
        assert st.call_fingerprint == row.call_fingerprint

    def test_the_replayed_step_records_no_terminal_id(self, client):
        """THE ONE FIELD THAT MUST NOT BE COPIED, and the reason it gets its own test.

        The response serves the envelope's original ``terminal_id`` because a caller asked what
        the step returned — and pairs it with ``replayed=True`` precisely because that terminal
        no longer exists (SR-4). ``StepRunState.terminal_id`` is a different thing: it is the
        list ``_reconcile_orphans`` sweeps to TEAR DOWN live terminals, so putting a dead id
        there would be an instruction to delete something already gone.
        """
        run_id = "run-f4-no-terminal"
        body = _body(env_vars=_env(run_id))
        record = _register_run(run_id)
        assert record is not None
        _seed_step(
            run_id,
            fingerprint=_route_fingerprint(body),
            envelope=_envelope(terminal_id="original-terminal"),
        )

        with patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)

        # The RESPONSE still carries it — the two are not the same field.
        assert resp.json()["terminal_id"] == "original-terminal"
        assert resp.json()["replayed"] is True
        assert record.step_states["s1"].terminal_id is None

    def test_recording_the_replay_writes_nothing_durable(self, client):
        """BR-4 is not weakened by the fix: the recorder is IN-MEMORY ONLY. Asserted by
        comparing the row byte-for-byte across the replayed call, the same way
        :class:`TestReplayCreatesNothing` does."""
        run_id = "run-f4-nowrite"
        body = _body(env_vars=_env(run_id))
        _register_run(run_id)
        _seed_step(run_id, fingerprint=_route_fingerprint(body), envelope=_envelope())
        before = _raw_row(run_id, "s1")

        with (
            patch("cli_agent_orchestrator.services.workflow_journal.begin_step") as m_begin,
            patch("cli_agent_orchestrator.services.workflow_journal.settle_step") as m_settle,
            patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())),
        ):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)

        assert resp.json()["replayed"] is True
        m_begin.assert_not_called()
        m_settle.assert_not_called()
        assert _raw_row(run_id, "s1") == before

    def test_a_yaml_tier_caller_records_nothing(self, client):
        """The recorder carries the SAME guard as its two siblings, so a non-script call
        reaches no registry lookup and no journal read. Proved by the absence of a record: a
        YAML-tier run has no ``ScriptRunRecord`` at all, and asking for one must not create it.
        """
        run_id = "run-f4-yaml"
        _register_run(run_id, script_tier=False)

        with patch(_RUN_STEP, new=AsyncMock(return_value=_ok_result())) as m_run:
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=_body(env_vars=_env(run_id)))

        assert resp.status_code == 200
        assert resp.json()["replayed"] is False  # no gate at all for this tier
        m_run.assert_awaited_once()
        assert run_id not in workflow_service.run_registry

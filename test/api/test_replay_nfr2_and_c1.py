"""NFR-2 at the provider boundary, and C-1's binding YAML regression (issue #583, unit
``replay-verification-guard``).

The other three deliverables of this unit live in
``test/services/test_replay_verification_guard.py``. These two are here because both need the
assembled ASGI app: deliverable 4 asserts on the PROVIDER boundary across a whole resumed run
through the real run-step route, and deliverable 1 drives the real YAML engine and the real
route side by side. Test-only, like the rest of the unit — no production file is modified
(BR-1/SR-5).

- **Deliverable 4 — NFR-2** (:class:`TestNoProviderCallForAnAlreadyCompletedStep`): *"A
  resumed run performs no provider call for an already-completed step."* ``run-step-replay-
  branch`` BR-4 already asserts no terminal, no ``begin_step``, no ``settle_step`` — for ONE
  route call. This is different: TERMINAL CREATION AND A PROVIDER CALL ARE NOT THE SAME EVENT,
  so a no-terminal assertion would still pass if some future path made a provider call without
  a terminal. The assertion here is on ``terminal_service.send_input`` — the moment the
  prompt actually reaches the provider CLI — over a whole three-step resumed run.
  **BOTH HALVES ARE REQUIRED** (BR-9): the provider was NOT invoked for the replayed steps
  AND WAS invoked for the one that genuinely executes. The first alone passes for a run that
  did nothing at all.

- **Deliverable 1 — C-1** (:class:`TestSequentialYamlWorkflowsKeepWorking`): *"Existing
  sequential YAML workflows keep working."* Every unit in this Bolt claimed cheapness on the
  script-tier guard; this is where the claim is tested rather than asserted.
  **BOTH HALVES ARE REQUIRED** (BR-10): the run completes with unchanged results AND ``decide``
  was never called and no replay read occurred. Results alone would pass even if the gate ran
  and happened to return ``EXECUTE`` every time.

- **Deliverable 5's end-to-end half** (:class:`TestTheUpgradeWindowOverHttp`): the four policy
  routes are proved at the gate in the services file; these two carry the *end-to-end* adjective
  ``bolt-plan.md``:111-118 attaches to the scenario — a legacy-fingerprint row resumed over
  HTTP routes by policy and is never surfaced as a divergence.

THE REAL ``run_agent_step`` RUNS IN EVERY TEST HERE. Patching it out would make "no provider
call" vacuously true and "the YAML tier never reaches the gate" untestable, so the patches go
one level lower — at the terminal layer — exactly as ``test_run_step_replay_branch.py`` does.
A route or engine that wrongly fell through therefore REACHES the spy.

ISOLATION (SR-1): every test points ``constants.DATABASE_FILE`` at a ``tmp_path`` file before
writing anything and asserts it, and every ``run_id`` is prefixed ``rvg-`` so a leak is
attributable on sight. ``run-step-replay-branch``'s Finding 3 found real rows in the
developer's own database written by two sibling api test files; this unit manufactures rows
the gate exists to HALT on, so the requirement is stricter here than anywhere else.
"""

from __future__ import annotations

import sqlite3
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import cli_agent_orchestrator.constants as constants
from cli_agent_orchestrator.clients.database import (
    _migrate_workflow_run,
    _migrate_workflow_run_step,
)
from cli_agent_orchestrator.constants import TERMINALS_RUN_STEP_ROUTE
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.models.workflow import (
    StepResultEnvelope,
    WorkflowSpec,
    WorkflowStep,
)
from cli_agent_orchestrator.models.workflow_runtime import RunState, StepState
from cli_agent_orchestrator.services import step_replay, workflow_journal, workflow_service
from cli_agent_orchestrator.services.script_runner import ScriptRunRecord
from cli_agent_orchestrator.services.step_fingerprint import StepCallFields, compute
from cli_agent_orchestrator.services.step_result import serialise_envelope

# Captured at import, before any fixture can patch it — see the services file's rationale.
_PRODUCTION_DATABASE_FILE = Path(constants.DATABASE_FILE)

_AGENT_STEP = "cli_agent_orchestrator.services.agent_step"

TS = "2026-08-17T00:00:00Z"

# 64 hex with no ``v2:`` prefix — a legacy row by ``scheme_of``'s prefix rule, and a repeating
# pattern rather than a digest of anything (SR-3).
FP_LEGACY = "a1b2c3d4" * 8


# ---------------------------------------------------------------------------
# Isolation + harness
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolated_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Temp SQLite journal + both #583-era tables + an isolated process-local registry."""
    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    _migrate_workflow_run()
    _migrate_workflow_run_step()
    before = dict(workflow_service.run_registry)
    workflow_service.run_registry.clear()
    workflow_service._active_drives.clear()
    workflow_service.step_output_store._store.clear()
    yield db_path
    workflow_service.run_registry.clear()
    workflow_service.run_registry.update(before)
    workflow_service._active_drives.clear()
    workflow_service.step_output_store._store.clear()


def _assert_tmp_db(db_path: Path) -> None:
    """Assert this test is operating on the ``tmp_path`` database (SR-1). One line per test."""
    from cli_agent_orchestrator.constants import DATABASE_FILE as live

    assert Path(live) == db_path, f"journal points at {live}, not the tmp_path database"
    assert (
        Path(live) != _PRODUCTION_DATABASE_FILE
    ), "journal points at the developer's REAL database"
    assert db_path.exists(), "the tmp_path database was never created"


class _ProviderSpy:
    """Records every prompt that actually reached the provider CLI.

    ``terminal_service.send_input`` is THE provider call: it is the bracketed-paste write of
    the prompt into the agent's terminal. Asserting here rather than on terminal creation is
    the whole point of BR-9 — a future path could create no terminal and still talk to a
    provider (a reused terminal already does exactly that), and a no-terminal assertion would
    not notice.
    """

    def __init__(self) -> None:
        self.prompts: List[str] = []
        self.terminals: List[str] = []

    def __call__(self, terminal_id: str, prompt: str, *args, **kwargs) -> bool:
        self.terminals.append(terminal_id)
        self.prompts.append(prompt)
        return True


@contextmanager
def _terminal_layer(spy: _ProviderSpy, *, created_id: str = "rvg-fresh-terminal"):
    """Patch the terminal layer so the REAL ``run_agent_step`` runs end to end.

    Mirrors ``test_run_step_replay_branch.py``'s helper, with ``send_input`` replaced by the
    provider spy. ONE context manager rather than a tuple of eight, so no call site can
    accidentally enter seven of them.
    """
    terminal = MagicMock()
    terminal.id = created_id
    patches = (
        patch(
            f"{_AGENT_STEP}.terminal_service.create_terminal",
            new=AsyncMock(return_value=terminal),
        ),
        patch(f"{_AGENT_STEP}.terminal_service.send_input", new=spy),
        patch(f"{_AGENT_STEP}.terminal_service.delete_terminal", return_value=True),
        patch(f"{_AGENT_STEP}.terminal_service.get_output", return_value="a fresh answer"),
        patch(f"{_AGENT_STEP}.terminal_service.exit_terminal_cli", return_value=None),
        patch(f"{_AGENT_STEP}.wait_until_status", new=AsyncMock(return_value=True)),
        patch(f"{_AGENT_STEP}.status_monitor.get_status", return_value=TerminalStatus.COMPLETED),
        patch(f"{_AGENT_STEP}.terminal_service.get_working_directory", return_value=None),
    )
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


def _register_script_run(run_id: str, *, generation: str = "1") -> ScriptRunRecord:
    """Journal the run row AND register a live ``ScriptRunRecord`` — a script-tier run.

    Both are needed: the journal row is what the generation fence reads, and the live record
    is the run-step route's script-tier discriminator.
    """
    workflow_journal.insert_run(
        run_id=run_id,
        workflow_name="wf",
        spec_snapshot="steps: []",
        inputs_json="{}",
        state="running",
        started_at=TS,
        tier="script",
        generation=generation,
    )
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
    )
    workflow_service.run_registry[run_id] = record
    return record


def _register_yaml_run_row(run_id: str, *, generation: str = "1") -> None:
    """Journal a YAML-tier run row and register NO live record.

    That is exactly what a YAML-tier run looks like to the run-step route's guard: the env vars
    are present, the live ``ScriptRunRecord`` is not.
    """
    workflow_journal.insert_run(
        run_id=run_id,
        workflow_name="wf",
        spec_snapshot="steps: []",
        inputs_json="{}",
        state="running",
        started_at=TS,
        tier="yaml",
        generation=generation,
    )


def _body(step_id: str, run_id: str, *, prompt: str, **overrides) -> dict:
    body = {
        "provider": "kiro_cli",
        "agent": "developer",
        "prompt": prompt,
        "env_vars": {
            "CAO_WORKFLOW_RUN_ID": run_id,
            "CAO_WORKFLOW_STEP_ID": step_id,
            "CAO_WORKFLOW_GENERATION": "1",
        },
    }
    body.update(overrides)
    return body


def _route_fingerprint(body: dict) -> str:
    """The fingerprint the route computes for ``body`` — assembled from the request.

    Assembled here rather than read out of the route, so a change to the route's field list
    breaks these tests loudly instead of silently agreeing with itself.
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
            effective_working_directory=None,
            use_worktree=body.get("use_worktree", False),
            reused_terminal=body.get("reuse_terminal_id") is not None,
            timeout=body.get("timeout", 600.0),
        )
    )


def _envelope(last_message: str, terminal_id: str) -> StepResultEnvelope:
    # Inert text (SR-4): this unit asserts nothing about redaction.
    return StepResultEnvelope(
        last_message=last_message, status="completed", terminal_id=terminal_id
    )


def _seed_replayable(run_id: str, body: dict, envelope: StepResultEnvelope) -> None:
    """Settle a step through the REAL journal writers so it will replay for ``body``."""
    step_id = body["env_vars"]["CAO_WORKFLOW_STEP_ID"]
    workflow_journal.begin_step(run_id, step_id, TS, _route_fingerprint(body))
    workflow_journal.settle_step(
        run_id=run_id,
        step_id=step_id,
        state="completed",
        updated_at=TS,
        result_json=serialise_envelope(envelope),
        output_json=None,
        error=None,
    )


def _manufacture_legacy_row(run_id: str, step_id: str, envelope: StepResultEnvelope) -> None:
    """Direct SQL — a fixture, never an assertion (SR-2).

    A settled row carrying a pre-``v2`` fingerprint is not what any production writer
    produces, so the upgrade window has to be constructed rather than driven.
    """
    from cli_agent_orchestrator.constants import DATABASE_FILE

    with sqlite3.connect(str(DATABASE_FILE)) as conn:
        conn.execute(
            "INSERT INTO workflow_run_step "
            "(run_id, step_id, state, attempts, output_json, error, updated_at, "
            " call_fingerprint, result_json) "
            "VALUES (?, ?, 'completed', 1, NULL, NULL, ?, ?, ?)",
            (run_id, step_id, TS, FP_LEGACY, serialise_envelope(envelope)),
        )


def _raw_row(run_id: str, step_id: str) -> Optional[tuple]:
    """The row as stored bytes — the unchanged-across-the-run comparison."""
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


# The three-step resumed run every NFR-2 test drives: s1 and s2 already settled and
# replayable, s3 never dispatched. One shape, so the three tests below assert three different
# things about the SAME run rather than three differently-shaped runs.
_RESUMED_RUN = "rvg-nfr2"
_PROMPTS = {"s1": "do s1", "s2": "do s2", "s3": "do s3"}


def _drive_resumed_run(client, *, replay_everything: bool = False) -> Tuple[_ProviderSpy, Dict]:
    """POST the three steps of a resumed run in order; return the spy and the responses.

    ``replay_everything`` seeds s3 as replayable too. It exists for the mutation check the
    plan asks for: with nothing left to execute, the "the provider WAS invoked" half must fail
    while the "was not invoked" half still passes — which is exactly why both halves are
    required (BR-9).
    """
    run_id = _RESUMED_RUN
    _register_script_run(run_id)
    bodies = {
        step_id: _body(step_id, run_id, prompt=prompt) for step_id, prompt in _PROMPTS.items()
    }
    _seed_replayable(run_id, bodies["s1"], _envelope("s1's stored answer", "rvg-dead-1"))
    _seed_replayable(run_id, bodies["s2"], _envelope("s2's stored answer", "rvg-dead-2"))
    if replay_everything:
        _seed_replayable(run_id, bodies["s3"], _envelope("s3's stored answer", "rvg-dead-3"))

    spy = _ProviderSpy()
    responses: Dict[str, object] = {}
    with _terminal_layer(spy):
        for step_id in ("s1", "s2", "s3"):
            responses[step_id] = client.post(TERMINALS_RUN_STEP_ROUTE, json=bodies[step_id])
    return spy, responses


# ---------------------------------------------------------------------------
# Deliverable 4 / BR-9 — NFR-2 AT THE PROVIDER BOUNDARY, over a whole resumed run
# ---------------------------------------------------------------------------
class TestNoProviderCallForAnAlreadyCompletedStep:
    """NFR-2's Pass criterion, asserted where the money is actually spent.

    Unit 9's BR-4 covers "a REPLAY creates nothing" for one route call. This covers a WHOLE
    resumed run and asserts on the provider/agent-step boundary, because terminal creation and
    a provider call are not the same event.
    """

    def test_no_provider_call_is_made_for_the_replayed_steps(self, client, _isolated_journal):
        _assert_tmp_db(_isolated_journal)
        spy, responses = _drive_resumed_run(client)

        assert responses["s1"].status_code == 200
        assert responses["s2"].status_code == 200
        assert responses["s1"].json()["replayed"] is True
        assert responses["s2"].json()["replayed"] is True
        # HALF ONE: neither replayed step's prompt ever reached a provider.
        assert _PROMPTS["s1"] not in spy.prompts
        assert _PROMPTS["s2"] not in spy.prompts

    def test_a_provider_call_is_made_for_the_step_that_genuinely_executes(
        self, client, _isolated_journal
    ):
        """HALF TWO, AND THE ONE THAT STOPS THE TEST PASSING FOR A RUN THAT DID NOTHING.
        Without it, a route that returned 200 for every step without ever executing anything
        would satisfy the first half perfectly."""
        _assert_tmp_db(_isolated_journal)
        spy, responses = _drive_resumed_run(client)

        assert responses["s3"].status_code == 200
        assert responses["s3"].json()["replayed"] is False
        assert _PROMPTS["s3"] in spy.prompts

    def test_the_whole_resumed_run_makes_exactly_one_provider_call(self, client, _isolated_journal):
        """BOTH HALVES IN ONE ASSERTION. The exact list is what a future change cannot satisfy
        by weakening one side: it pins the count, the identity and the ordering at once."""
        _assert_tmp_db(_isolated_journal)
        spy, _responses = _drive_resumed_run(client)

        assert spy.prompts == [_PROMPTS["s3"]]
        # And the one call went to a FRESHLY CREATED terminal, never to a replayed step's dead
        # one. A route that re-sent a prompt into a dead terminal id would make exactly one
        # provider call too, so the count alone does not settle it.
        assert spy.terminals == ["rvg-fresh-terminal"]
        assert "rvg-dead-1" not in spy.terminals
        assert "rvg-dead-2" not in spy.terminals

    def test_the_replayed_steps_return_their_stored_results(self, client, _isolated_journal):
        """A replay that returned a fresh answer would still make no provider call for the
        replayed step in a route that had simply lost the envelope, so the payload is asserted
        too — and the dead terminal id, which is the only place the stored envelope can come
        from (``StepRow`` has no ``terminal_id`` column)."""
        _assert_tmp_db(_isolated_journal)
        _spy, responses = _drive_resumed_run(client)

        assert responses["s1"].json()["last_message"] == "s1's stored answer"
        assert responses["s2"].json()["last_message"] == "s2's stored answer"
        assert responses["s1"].json()["terminal_id"] == "rvg-dead-1"
        assert responses["s2"].json()["terminal_id"] == "rvg-dead-2"
        # The executed step got the fresh output, not a stored one.
        assert responses["s3"].json()["last_message"] == "a fresh answer"

    def test_the_replayed_rows_are_byte_identical_after_the_whole_run(
        self, client, _isolated_journal
    ):
        """A replayed step must cost no WRITE either, across the whole run rather than one
        call — a later step's settle must not disturb an earlier step's row."""
        _assert_tmp_db(_isolated_journal)
        run_id = _RESUMED_RUN
        _register_script_run(run_id)
        bodies = {
            step_id: _body(step_id, run_id, prompt=prompt) for step_id, prompt in _PROMPTS.items()
        }
        _seed_replayable(run_id, bodies["s1"], _envelope("s1's stored answer", "rvg-dead-1"))
        _seed_replayable(run_id, bodies["s2"], _envelope("s2's stored answer", "rvg-dead-2"))
        before = {step: _raw_row(run_id, step) for step in ("s1", "s2")}
        assert all(row is not None for row in before.values())  # the seed is real, not assumed

        spy = _ProviderSpy()
        with _terminal_layer(spy):
            for step_id in ("s1", "s2", "s3"):
                assert (
                    client.post(TERMINALS_RUN_STEP_ROUTE, json=bodies[step_id]).status_code == 200
                )

        assert {step: _raw_row(run_id, step) for step in ("s1", "s2")} == before
        # s3 DID execute, so its row is new — the control that shows the comparison above is
        # not simply reading a journal nothing ever wrote to.
        assert _raw_row(run_id, "s3") is not None

    def test_a_run_that_replays_everything_makes_no_provider_call_at_all(
        self, client, _isolated_journal
    ):
        """THE FORCING FUNCTION for the both-halves rule. This is the run the "was not
        invoked" half passes on while proving nothing, and naming it here is what makes
        ``test_a_provider_call_is_made_for_the_step_that_genuinely_executes`` load-bearing
        rather than decorative."""
        _assert_tmp_db(_isolated_journal)
        spy, responses = _drive_resumed_run(client, replay_everything=True)

        assert spy.prompts == []
        assert all(responses[step].json()["replayed"] is True for step in ("s1", "s2", "s3"))


# ---------------------------------------------------------------------------
# Deliverable 1 / BR-10 — C-1: EXISTING SEQUENTIAL YAML WORKFLOWS KEEP WORKING
# ---------------------------------------------------------------------------
def _yaml_spec(name: str = "rvg-yaml-wf") -> WorkflowSpec:
    return WorkflowSpec(
        name=name,
        mode="sequential",
        steps=[
            WorkflowStep(id="y1", provider="kiro_cli", agent="developer", prompt="do y1"),
            WorkflowStep(id="y2", provider="kiro_cli", agent="developer", prompt="do y2"),
            WorkflowStep(id="y3", provider="kiro_cli", agent="developer", prompt="do y3"),
        ],
    )


class _GateSpy:
    """Counts every call into the replay gate and into the gate's own journal read."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.decide_calls: List[tuple] = []
        self.gate_reads: List[tuple] = []
        self.lookup_replay_calls: List[tuple] = []
        real_decide = step_replay.decide
        real_get_step = step_replay.get_step
        real_lookup = workflow_journal.lookup_replay

        def _decide(*a, **k):
            self.decide_calls.append(a)
            return real_decide(*a, **k)

        def _get_step(*a, **k):
            self.gate_reads.append(a)
            return real_get_step(*a, **k)

        def _lookup(*a, **k):
            self.lookup_replay_calls.append(a)
            return real_lookup(*a, **k)

        # ``step_replay.get_step`` is the name the gate binds; spying THERE rather than on
        # ``workflow_journal.get_step`` keeps the engine's own legitimate journal reads out of
        # the count, so the assertion is about the REPLAY read specifically.
        monkeypatch.setattr(step_replay, "decide", _decide)
        monkeypatch.setattr(step_replay, "get_step", _get_step)
        monkeypatch.setattr(workflow_journal, "lookup_replay", _lookup)


class TestSequentialYamlWorkflowsKeepWorking:
    """C-1's binding regression. The YAML tier must be untouched by all of Bolt 1A/1B.

    The engine calls ``run_agent_step`` DIRECTLY in-process (the single-seam rule, ADR-3), so
    the replay branch — which lives in the run-step ROUTE — is structurally out of its path.
    "Structurally" is the claim under test, and the spy is how it stops being an assertion
    about the code's shape and becomes one about its behaviour.
    """

    RUN = "rvg-yaml"

    @pytest.mark.asyncio
    async def test_a_sequential_yaml_run_completes_with_unchanged_results(self, _isolated_journal):
        _assert_tmp_db(_isolated_journal)
        spy = _ProviderSpy()
        with _terminal_layer(spy):
            result = await workflow_service.start_run(_yaml_spec(), {}, f"{self.RUN}-a")

        assert result.state == RunState.COMPLETED
        assert [s.id for s in result.steps] == ["y1", "y2", "y3"]
        assert [s.state for s in result.steps] == [StepState.COMPLETED] * 3
        assert [s.attempts for s in result.steps] == [1, 1, 1]
        assert [s.error for s in result.steps] == [None, None, None]
        # Every step really executed — three provider calls, in spec order.
        assert spy.prompts == ["do y1", "do y2", "do y3"]

    @pytest.mark.asyncio
    async def test_the_durable_journal_of_a_yaml_run_is_unchanged_in_shape(self, _isolated_journal):
        """The write-through half of "keep working": the run and its steps settle exactly as
        they did before #583, and the two columns this issue added stay NULL — the YAML tier
        writes neither, so a row that suddenly carried a fingerprint would mean the script
        tier's writers had leaked into it."""
        _assert_tmp_db(_isolated_journal)
        spy = _ProviderSpy()
        with _terminal_layer(spy):
            await workflow_service.start_run(_yaml_spec(), {}, f"{self.RUN}-b")

        run_row = workflow_journal.get_run(f"{self.RUN}-b")
        assert run_row is not None
        assert run_row.state == RunState.COMPLETED.value
        assert run_row.tier == "yaml"
        assert run_row.current_step_id is None
        rows = {r.step_id: r for r in workflow_journal.get_steps(f"{self.RUN}-b")}
        assert set(rows) == {"y1", "y2", "y3"}
        for row in rows.values():
            assert row.state == StepState.COMPLETED.value
            assert row.attempts == 1
            assert row.call_fingerprint is None
            assert row.result_json is None

    @pytest.mark.asyncio
    async def test_the_yaml_drive_never_calls_the_gate_and_performs_no_replay_read(
        self, _isolated_journal, monkeypatch: pytest.MonkeyPatch
    ):
        """THE HALF THE RESULTS ASSERTION CANNOT COVER (BR-10). A run whose results are
        unchanged would pass the test above even if the gate ran on every step and happened to
        return ``EXECUTE`` every time — which is precisely the cheapness claim every unit in
        this Bolt made and none tested."""
        _assert_tmp_db(_isolated_journal)
        gate = _GateSpy(monkeypatch)
        spy = _ProviderSpy()
        with _terminal_layer(spy):
            result = await workflow_service.start_run(_yaml_spec(), {}, f"{self.RUN}-c")

        # BOTH HALVES, in one test, so neither can be satisfied by breaking the other.
        assert result.state == RunState.COMPLETED
        assert spy.prompts == ["do y1", "do y2", "do y3"]
        assert gate.decide_calls == []
        assert gate.gate_reads == []
        assert gate.lookup_replay_calls == []

    def test_a_yaml_tier_run_step_call_reaches_the_provider_and_not_the_gate(
        self, client, _isolated_journal, monkeypatch: pytest.MonkeyPatch
    ):
        """The route-level face of the same guard, with a row planted that WOULD replay if the
        gate were consulted. It overlaps ``run-step-replay-branch``'s own tier test by design:
        that one asserts the route's guard, this one is C-1's binding assertion that a
        pre-existing YAML caller still gets its step EXECUTED — the stored result must not be
        served to another tier."""
        _assert_tmp_db(_isolated_journal)
        run_id = f"{self.RUN}-route"
        _register_yaml_run_row(run_id)  # journal row, NO live ScriptRunRecord
        body = _body("y1", run_id, prompt="do y1 over http")
        _seed_replayable(run_id, body, _envelope("a stored answer nobody may serve", "rvg-dead"))
        gate = _GateSpy(monkeypatch)

        spy = _ProviderSpy()
        with _terminal_layer(spy):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)

        assert resp.status_code == 200
        assert resp.json()["replayed"] is False
        assert resp.json()["last_message"] == "a fresh answer"
        assert resp.json()["last_message"] != "a stored answer nobody may serve"
        assert spy.prompts == ["do y1 over http"]
        assert gate.decide_calls == []
        assert gate.gate_reads == []

    def test_the_gate_spy_would_notice_a_gate_call(
        self, client, _isolated_journal, monkeypatch: pytest.MonkeyPatch
    ):
        """THE FORCING FUNCTION for the two ``== []`` assertions above. A spy installed on the
        wrong name records nothing and every "never called" assertion passes for free, so a
        call the gate genuinely DOES make is driven through it here."""
        _assert_tmp_db(_isolated_journal)
        run_id = f"{self.RUN}-spy-check"
        _register_script_run(run_id)  # a SCRIPT-tier run: the gate is consulted
        body = _body("s1", run_id, prompt="do s1")
        gate = _GateSpy(monkeypatch)

        spy = _ProviderSpy()
        with _terminal_layer(spy):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)

        assert resp.status_code == 200
        assert len(gate.decide_calls) == 1
        assert len(gate.gate_reads) == 1


# ---------------------------------------------------------------------------
# Deliverable 5's end-to-end half — the upgrade window over HTTP
# ---------------------------------------------------------------------------
class TestTheUpgradeWindowOverHttp:
    """The four policy routes are proved at the gate in the services file. These two carry the
    *end-to-end* half: a resumed run whose stored fingerprint predates ``v2`` routes by the
    current call's declared policy and is NEVER surfaced as a divergence (BR-8).
    """

    RUN = "rvg-upgrade-http"

    def test_an_undeclared_resume_halts_as_unverifiable_and_never_as_diverged(
        self, client, _isolated_journal
    ):
        _assert_tmp_db(_isolated_journal)
        run_id = f"{self.RUN}-undeclared"
        _register_script_run(run_id)
        body = _body("s1", run_id, prompt="do s1")
        _manufacture_legacy_row(run_id, "s1", _envelope("stored under the old scheme", "rvg-old"))

        spy = _ProviderSpy()
        with _terminal_layer(spy):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)

        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["kind"] == "decision_required"
        assert detail["kind"] != "diverged"
        assert detail["rule"] == "provenance_unverifiable"
        assert detail["step_id"] == "s1"
        # Nothing ran, and no digest travelled with the halt (SR-2, inherited).
        assert spy.prompts == []
        assert FP_LEGACY not in resp.text

    def test_an_idempotent_resume_executes_and_never_diverges(self, client, _isolated_journal):
        """Rule 4 end to end: the SAME row, the same route, a declared policy — and the step
        runs instead of halting. Without this half the test above would also pass on a route
        that halted on every legacy row regardless of what its author declared."""
        _assert_tmp_db(_isolated_journal)
        run_id = f"{self.RUN}-idempotent"
        _register_script_run(run_id)
        body = _body("s1", run_id, prompt="do s1", recovery="idempotent")
        _manufacture_legacy_row(run_id, "s1", _envelope("stored under the old scheme", "rvg-old"))

        spy = _ProviderSpy()
        with _terminal_layer(spy):
            resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)

        assert resp.status_code == 200
        assert resp.json()["replayed"] is False
        assert resp.json()["last_message"] == "a fresh answer"
        assert spy.prompts == ["do s1"]

    def test_neither_route_ever_reports_a_divergence(self, client, _isolated_journal):
        """BR-8 at the HTTP boundary, over both routes at once. 409/``diverged`` is the wrong
        answer for an unverifiable row and a reordering of rules 4-6 is what would produce it,
        so the prohibition is asserted where an operator would actually read it."""
        _assert_tmp_db(_isolated_journal)
        statuses: Dict[str, tuple] = {}
        for label, recovery in (("undeclared", None), ("idempotent", "idempotent")):
            run_id = f"{self.RUN}-nodiv-{label}"
            _register_script_run(run_id)
            extra = {} if recovery is None else {"recovery": recovery}
            body = _body("s1", run_id, prompt="do s1", **extra)
            _manufacture_legacy_row(run_id, "s1", _envelope("stored old", "rvg-old"))

            spy = _ProviderSpy()
            with _terminal_layer(spy):
                resp = client.post(TERMINALS_RUN_STEP_ROUTE, json=body)
            detail = resp.json().get("detail")
            kind = detail.get("kind") if isinstance(detail, dict) else None
            statuses[label] = (resp.status_code, kind)

        assert statuses == {
            "undeclared": (409, "decision_required"),
            "idempotent": (200, None),
        }
        assert all(kind != "diverged" for _status, kind in statuses.values())

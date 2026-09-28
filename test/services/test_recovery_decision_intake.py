"""Tests for ``recovery-decision-intake`` (issue #583, unit 12) — FR-7's escape hatch.

The replay gate can return ``DECISION_REQUIRED``; before this unit nothing could resolve
one. These tests cover the resolution: the two-member decision vocabulary, the two
``StepState`` members it writes, ``workflow_journal.apply_decisions``, the ``replay_authorized``
exclusion on the gate's rule 7, and the three surfaces that carry a decision in.

**WHAT "A RESUME" MEANS IN THIS FILE.** The gate is not yet wired into the resume path —
``run-step-replay-branch`` (unit 9) does that — so a "resume" in the loop, rerun, one-shot
and safety-rule groups is modelled by the thing a resume consults: a ``step_replay.decide``
call over the row. The route groups exercise the real HTTP route, and the live-run group
exercises the real ``script_runner.resume_script_run`` admission gates. No test spawns a
subprocess.

Coverage, one group per rule from
``construction/recovery-decision-intake/functional-design/business-rules.md`` and its
``nfr-requirements/``:

- BR-1/TD-1 — the closed TWO-member set, and the vocabulary pins that keep the four places
  that spell it agreeing (:class:`TestTheClosedTwoMemberSet`).
- BR-2 — ``rerun`` -> ``rerun_authorized`` -> rule 1 -> ``EXECUTE``, and the step really
  re-executes (:class:`TestRerunAuthorisesReExecution`).
- BR-3/RL-5 — THE REGRESSION THIS UNIT EXISTS FOR: ``skip`` replays, and a SECOND resume does
  not halt again (:class:`TestTheLoopThisUnitCloses`). Both resumes, plus the counterfactual.
- BR-4/SR-8/RL-7 — the exclusion is on rule 7 ONLY: three tests, one per surviving safety rule
  (:class:`TestSkipAuthorisationKeepsTheSafetyRules`). A single "it replays" test passes with
  the exclusion in the wrong place, which is the placement that silently disables FR-3.
- BR-5 — ``replay_authorized`` stays on the SETTLED path and takes no rule-1 arm
  (:class:`TestReplayAuthorizedStaysOnTheSettledPath`).
- BR-6/RL-2/INV-3 — a decision never silently fails (:class:`TestADecisionNeverSilentlyFails`).
- BR-8/SR-9/RL-4 — a decision destroys no evidence (:class:`TestADecisionDestroysNoEvidence`).
- BR-9/SR-7/RL-6 — one decision authorises exactly ONE attempt
  (:class:`TestOneDecisionAuthorisesOneAttempt`, and its SECOND HALF added by PR #628's review
  in :class:`TestUnconsumedConsentIsRevokedWhenTheDriveEnds`). The original class proved the
  one case ``begin_step`` covers — a dispatched rerun cannot be re-authorised — and the new one
  proves the three it does not: a resume that fails after the grant, a drive that never reaches
  the decided step, and a ``skip``, which NOTHING consumed on any path because a replay writes
  no row by design.
- BR-10/TD-7 — the three surfaces share one closed set
  (:class:`TestTheThreeSurfacesShareOneClosedSet`).
- SR-2/RL-1 — nothing is written until the WHOLE map validates, asserted by reading the
  EARLIER rows' states (:class:`TestNothingIsWrittenUntilTheWholeMapValidates`).
- SR-3 — the writes are one transaction (:class:`TestTheWritesAreOneTransaction`).
- SR-4 — a rejection lands on 400, not 500 (:class:`TestTheRouteMapsARejectionTo400`).
- SR-5/SR-6/TD-6 — the log line is evidence: after the commit, success path only, identifiers
  only (:class:`TestTheLogLineIsEvidence`).
- SC-3 — the live-run 409 precedes any write, asserted WITH a decision payload present
  (:class:`TestTheLiveRunGuardPrecedesAnyWrite`).
- RL-3 — the decision outlives the process that made it (:class:`TestTheDecisionIsDurable`).

Rows are seeded THROUGH THE JOURNAL against a real temp SQLite DB (the patched
``DATABASE_FILE``), mirroring ``test_step_replay.py`` and ``test_journal_step_lifecycle.py``
— not mocked, so the transitions are tested against the row shape ``begin_step`` /
``settle_step`` actually write.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from cli_agent_orchestrator.clients.database import (
    _migrate_workflow_run,
    _migrate_workflow_run_step,
)
from cli_agent_orchestrator.models.workflow import RecoveryPolicy, StepResultEnvelope
from cli_agent_orchestrator.models.workflow_runtime import (
    RecoveryDecision,
    RunState,
    StepResult,
    StepState,
    WorkflowRunResult,
    parse_decision,
)
from cli_agent_orchestrator.services import script_runner, workflow_journal, workflow_service
from cli_agent_orchestrator.services.step_replay import ReplayVerdict, decide
from cli_agent_orchestrator.services.step_result import serialise_envelope
from cli_agent_orchestrator.services.workflow_errors import HaltRule
from cli_agent_orchestrator.services.workflow_journal import apply_decisions

RUN = "run-1"
STEP = "call-1"
STEP_B = "call-2"
STEP_C = "call-3"
TS = "2026-08-16T00:00:00Z"

FP_CALL = "v2:" + "a" * 64  # this call's fingerprint, current scheme
FP_OTHER_V2 = "v2:" + "b" * 64  # a stored current-scheme value that DIFFERS
FP_LEGACY = "c" * 64  # the three-field scheme: a bare digest, no prefix

ENVELOPE = StepResultEnvelope(
    last_message="the step said this",
    status="completed",
    terminal_id="term-1",
)
RESULT_JSON = serialise_envelope(ENVELOPE)

# A resumable script run's snapshot: ``resume_script_run``'s gate 4 needs a JSON object
# carrying a string ``source``, and nothing in these tests ever executes it.
SNAPSHOT = json.dumps({"source": "def main():\n    pass\n"})


@pytest.fixture(autouse=True)
def _patched_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the journal at a temp DB and create the tables (both #583 columns included)."""
    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    _migrate_workflow_run()
    _migrate_workflow_run_step()
    yield db_path


@pytest.fixture(autouse=True)
def _no_leaked_run_state():
    """Guarantee the shared liveness set and run registry are empty around every test.

    ``workflow_service._active_drives`` is ONE module-level set that ``script_runner``
    imported by name (so it must be MUTATED, never rebound), and a test that marks a run
    live must not leave it live for the next one — a leaked claim would make a later resume
    test fail as a 409 for the wrong reason. ``run_registry`` is cleared for the same
    reason: the real resume path registers the record it rebuilds.
    """
    workflow_service._active_drives.clear()
    workflow_service.run_registry.clear()
    yield
    workflow_service._active_drives.clear()
    workflow_service.run_registry.clear()


@pytest.fixture
def client() -> TestClient:
    """A TestClient for the resume route.

    ``base_url`` carries the ``localhost`` Host header ``TrustedHostMiddleware`` requires
    (``test/api/conftest.py`` does the same thing with an explicit header; that conftest does
    not apply to this directory).
    """
    from cli_agent_orchestrator.api.main import app
    from cli_agent_orchestrator.plugins import PluginRegistry

    app.state.plugin_registry = PluginRegistry()
    return TestClient(app, base_url="http://localhost")


def _direct_connect() -> sqlite3.Connection:
    from cli_agent_orchestrator.constants import DATABASE_FILE

    return sqlite3.connect(str(DATABASE_FILE))


def _db_path() -> str:
    from cli_agent_orchestrator.constants import DATABASE_FILE

    return str(DATABASE_FILE)


def _seed_run(
    run_id: str = RUN, *, tier: str = "script", state: str = "failed", snapshot: str = SNAPSHOT
) -> None:
    """Seed the ``workflow_run`` row a resume reads (gate 1/gate 3)."""
    workflow_journal.insert_run(
        run_id=run_id,
        workflow_name="wf",
        spec_snapshot=snapshot,
        inputs_json="{}",
        state=state,
        started_at=TS,
        tier=tier,
    )


def _seed_step(
    *,
    state: str,
    fingerprint: Optional[str] = FP_CALL,
    result_json: Optional[str] = RESULT_JSON,
    run_id: str = RUN,
    step_id: str = STEP,
) -> None:
    """Seed one ``workflow_run_step`` row through the journal's own write path.

    ``begin_step`` writes the fingerprint (``settle_step`` never touches the column, unit 6's
    BR-9), then ``settle_step`` writes the state and the envelope in one statement. A NULL
    fingerprint — the ``absent`` scheme — is set with a raw UPDATE because ``begin_step``'s
    signature requires a ``str``.
    """
    workflow_journal.begin_step(run_id, step_id, TS, fingerprint or FP_LEGACY)
    workflow_journal.settle_step(run_id, step_id, state, TS, result_json, None, None)
    if fingerprint is None:
        with _direct_connect() as conn:
            conn.execute(
                "UPDATE workflow_run_step SET call_fingerprint = NULL "
                "WHERE run_id = ? AND step_id = ?",
                (run_id, step_id),
            )


def _seed_three_halted_steps() -> None:
    """Three settled ``manual``-policy rows, all of which halt at rule 7 until decided."""
    for step_id in (STEP, STEP_B, STEP_C):
        _seed_step(state=StepState.COMPLETED.value, step_id=step_id)


def _state_of(step_id: str = STEP, run_id: str = RUN) -> Optional[str]:
    with _direct_connect() as conn:
        row = conn.execute(
            "SELECT state FROM workflow_run_step WHERE run_id = ? AND step_id = ?",
            (run_id, step_id),
        ).fetchone()
    return None if row is None else row[0]


def _all_columns(step_id: str = STEP, run_id: str = RUN) -> Dict[str, Any]:
    """Every column of one row, so a test can assert what a decision did NOT touch."""
    with _direct_connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM workflow_run_step WHERE run_id = ? AND step_id = ?",
            (run_id, step_id),
        ).fetchone()
    assert row is not None
    return dict(row)


class _FailOnNthExecute:
    """A ``sqlite3.Connection`` proxy that raises on the Nth ``execute`` (SR-3's fault).

    Wraps a real connection so the transaction semantics under test are the REAL ones: the
    ``with`` block still delegates to ``sqlite3.Connection.__exit__``, which is what commits
    on a clean exit and rolls back on an exception. ``__enter__``/``__exit__`` are declared
    explicitly because the ``with`` protocol looks dunders up on the TYPE, so ``__getattr__``
    would never see them.
    """

    def __init__(self, conn: sqlite3.Connection, fail_on: int) -> None:
        self._conn = conn
        self._fail_on = fail_on
        self.executes = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def __enter__(self) -> "_FailOnNthExecute":
        self._conn.__enter__()
        return self

    def __exit__(self, *exc_info: Any) -> Any:
        return self._conn.__exit__(*exc_info)

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        self.executes += 1
        if self.executes == self._fail_on:
            raise sqlite3.OperationalError("disk I/O error (injected)")
        return self._conn.execute(*args, **kwargs)


# ---------------------------------------------------------------------------
# BR-1 / TD-1 — the closed TWO-member set, and the pins that keep every spelling of it
# in agreement. Four places name this vocabulary; only one of them is the enum.
# ---------------------------------------------------------------------------
class TestTheClosedTwoMemberSet:
    def test_the_enum_has_exactly_two_members(self):
        """``reconcile`` is deliberately absent (BR-1): no reconciliation operation exists in
        ``src/`` and the frozen scope defers it, so a third member would be a value the closed
        set cannot act on. When the operation ships, this is the test that fails first."""
        assert [member.value for member in RecoveryDecision] == ["rerun", "skip"]

    def test_reconcile_is_rejected_as_an_unknown_value(self):
        """It is a ``RecoveryPolicy`` member and NOT a ``RecoveryDecision`` one — the one
        shared word between a declaration by the author and a permission from an operator."""
        assert RecoveryPolicy.RECONCILE.value == "reconcile"
        with pytest.raises(ValueError, match="not a recovery decision"):
            parse_decision("reconcile")

    def test_the_journals_decision_map_is_pinned_to_both_enums(self):
        """``workflow_journal`` spells both sides as bare literals (its own convention, unit
        6's BR-12/TD-4). This is the pin that makes a rename on either side fail loudly."""
        assert set(workflow_journal._DECISION_STATES) == {m.value for m in RecoveryDecision}
        assert workflow_journal._DECISION_STATES["rerun"] == StepState.RERUN_AUTHORIZED.value
        assert workflow_journal._DECISION_STATES["skip"] == StepState.REPLAY_AUTHORIZED.value

    def test_the_clis_mirror_is_pinned_to_the_enum(self):
        """The CLI mirrors the member set without importing the model (C-2 keeps it a thin
        HTTP client), so it needs the same pin or it could reject a value the server accepts."""
        from cli_agent_orchestrator.cli.commands.workflow import _RECOVERY_DECISIONS

        assert set(_RECOVERY_DECISIONS) == {m.value for m in RecoveryDecision}

    def test_the_gates_replay_authorized_literal_is_pinned_to_the_enum(self):
        """The gate also spells the state as a bare literal, for the same
        no-sixth-package-import reason (unit 7's TD-1)."""
        from cli_agent_orchestrator.services import step_replay

        assert step_replay._REPLAY_AUTHORIZED == StepState.REPLAY_AUTHORIZED.value
        assert step_replay._RERUN_AUTHORIZED == StepState.RERUN_AUTHORIZED.value


# ---------------------------------------------------------------------------
# BR-2 — ``rerun`` needs NO gate change: rule 1 already admits ``rerun_authorized``.
# ---------------------------------------------------------------------------
class TestRerunAuthorisesReExecution:
    def test_rerun_writes_rerun_authorized_and_the_gate_returns_execute(self):
        _seed_step(state=StepState.COMPLETED.value)
        halted = decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL)
        assert halted.verdict is ReplayVerdict.DECISION_REQUIRED
        assert halted.rule is HaltRule.POLICY_MANUAL

        apply_decisions(RUN, {STEP: RecoveryDecision.RERUN})

        assert _state_of() == StepState.RERUN_AUTHORIZED.value
        authorised = decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL)
        assert authorised.verdict is ReplayVerdict.EXECUTE
        assert authorised.envelope is None

    def test_the_authorised_step_then_actually_re_executes(self):
        """``EXECUTE`` is only an instruction; this asserts the execution it authorises really
        happens — the executor's ``begin_step`` re-baselines the row to ``running`` under the
        NEW call's fingerprint rather than serving the stored result."""
        _seed_step(state=StepState.COMPLETED.value)
        apply_decisions(RUN, {STEP: "rerun"})
        assert decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL).verdict is ReplayVerdict.EXECUTE

        new_fingerprint = "v2:" + "d" * 64
        workflow_journal.begin_step(RUN, STEP, "2026-08-16T00:00:01Z", new_fingerprint)

        row = _all_columns()
        assert row["state"] == StepState.RUNNING.value
        assert row["call_fingerprint"] == new_fingerprint


# ---------------------------------------------------------------------------
# BR-3 / RL-5 / INV-6 — THE LOOP. Both resumes, or the regression is untested.
# ---------------------------------------------------------------------------
class TestTheLoopThisUnitCloses:
    """A ``manual`` step halted at rule 7, decided ``skip``, must replay — and must not halt
    again on the resume AFTER that.

    ``component-methods.md``:431 said ``skip`` "leaves the row settled so it replays". It does
    not: the next resume re-runs the gate over identical inputs and rule 7 fires again, so the
    step halts forever. This is the same defect ADR-583-8 was corrected for once already, for
    ``rerun``. The first test proves the replay; only the SECOND proves the loop terminates.
    """

    def test_skip_makes_the_halted_step_replay(self):
        _seed_step(state=StepState.COMPLETED.value)
        assert decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL).rule is HaltRule.POLICY_MANUAL

        apply_decisions(RUN, {STEP: RecoveryDecision.SKIP})

        assert _state_of() == StepState.REPLAY_AUTHORIZED.value
        first_resume = decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL)
        assert first_resume.verdict is ReplayVerdict.REPLAY
        assert first_resume.envelope == ENVELOPE

    def test_a_second_resume_does_not_halt_again(self):
        """The termination half. The row is unchanged by a replay (the gate writes nothing), so
        a second consultation must reach the same conclusion rather than re-asking."""
        _seed_step(state=StepState.COMPLETED.value)
        apply_decisions(RUN, {STEP: RecoveryDecision.SKIP})

        first = decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL)
        second = decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL)

        assert second.verdict is ReplayVerdict.REPLAY
        assert second.verdict is not ReplayVerdict.DECISION_REQUIRED
        assert second.rule is None
        assert second == first

    def test_without_the_transition_the_same_row_halts_on_every_resume(self):
        """The counterfactual, so the two tests above are not vacuous: leaving the row settled
        and unchanged — the behaviour the design text originally specified — halts twice."""
        _seed_step(state=StepState.COMPLETED.value)

        first = decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL)
        second = decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL)

        assert first.rule is HaltRule.POLICY_MANUAL
        assert second.rule is HaltRule.POLICY_MANUAL


# ---------------------------------------------------------------------------
# BR-4 / SR-8 / RL-7 / INV-2 — the exclusion is on RULE 7 ONLY. Three tests, one per
# surviving safety rule. A single "it replays" test passes with the exclusion hoisted into
# a rule of its own placed earlier, which is the placement that disables FR-3.
# ---------------------------------------------------------------------------
class TestSkipAuthorisationKeepsTheSafetyRules:
    """The human authorised USING a stored result, not bypassing the checks that decide
    whether one is usable."""

    def test_a_replay_authorized_row_with_no_envelope_still_halts(self):
        """Rule 3 still fires (FR-4 guard 2): there is nothing to replay."""
        _seed_step(state=StepState.COMPLETED.value, result_json=None)
        apply_decisions(RUN, {STEP: RecoveryDecision.SKIP})

        d = decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL)
        assert d.verdict is ReplayVerdict.DECISION_REQUIRED
        assert d.rule is HaltRule.ENVELOPE_ABSENT
        assert d.envelope is None

    def test_a_replay_authorized_row_with_a_legacy_scheme_still_halts(self):
        """Rules 4-5 still fire (FR-6): unverifiable provenance never replays as a match."""
        _seed_step(state=StepState.COMPLETED.value, fingerprint=FP_LEGACY)
        apply_decisions(RUN, {STEP: RecoveryDecision.SKIP})

        d = decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL)
        assert d.verdict is ReplayVerdict.DECISION_REQUIRED
        assert d.rule is HaltRule.PROVENANCE_UNVERIFIABLE
        assert d.envelope is None

    def test_a_replay_authorized_row_with_a_mismatched_fingerprint_still_diverges(self):
        """Rule 6 still fires (FR-3). This is the test an exclusion placed before rule 6
        breaks, and the reason the placement is fixed in the design rather than left to
        judgement — a changed script must keep failing loudly."""
        _seed_step(state=StepState.COMPLETED.value, fingerprint=FP_OTHER_V2)
        apply_decisions(RUN, {STEP: RecoveryDecision.SKIP})

        d = decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL)
        assert d.verdict is ReplayVerdict.DIVERGED
        assert d.rule is None
        assert d.envelope is None


# ---------------------------------------------------------------------------
# BR-5 — ``replay_authorized`` is a SETTLED state and takes no rule-1 arm.
# ---------------------------------------------------------------------------
class TestReplayAuthorizedStaysOnTheSettledPath:
    def test_it_returns_replay_with_the_stored_envelope(self):
        _seed_step(state=StepState.COMPLETED.value)
        apply_decisions(RUN, {STEP: RecoveryDecision.SKIP})

        d = decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL)
        assert d.verdict is ReplayVerdict.REPLAY
        assert d.envelope is not None
        assert d.envelope == ENVELOPE

    def test_it_is_not_admitted_by_rule_1_as_execute(self):
        """Rule 1 is for states meaning EXECUTE. Admitting this one there would return
        ``EXECUTE`` with no envelope and re-run the step the human asked to skip — and it would
        do so BEFORE rules 3-5 could judge whether the stored result was usable at all."""
        _seed_step(state=StepState.COMPLETED.value)
        apply_decisions(RUN, {STEP: RecoveryDecision.SKIP})

        d = decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL)
        assert d.verdict is not ReplayVerdict.EXECUTE
        assert decide(RUN, STEP, FP_CALL, None).verdict is not ReplayVerdict.EXECUTE


# ---------------------------------------------------------------------------
# BR-6 / RL-2 / INV-3 — a decision never silently fails.
# ---------------------------------------------------------------------------
class TestADecisionNeverSilentlyFails:
    def test_an_unknown_step_id_raises_and_names_it(self):
        _seed_step(state=StepState.COMPLETED.value)
        with pytest.raises(ValueError) as excinfo:
            apply_decisions(RUN, {"no-such-step": RecoveryDecision.RERUN})
        assert "no-such-step" in str(excinfo.value)
        assert RUN in str(excinfo.value)

    def test_an_unknown_decision_value_raises_and_names_the_step(self):
        _seed_step(state=StepState.COMPLETED.value)
        with pytest.raises(ValueError) as excinfo:
            apply_decisions(RUN, {STEP: "rerunn"})
        assert STEP in str(excinfo.value)
        assert "rerunn" in str(excinfo.value)

    def test_neither_rejection_wrote_anything_nor_returned_success(self):
        """A silent no-op would let the run halt again at the same step with no signal the
        decision never landed, so the operator would re-issue the same typo indefinitely."""
        _seed_step(state=StepState.COMPLETED.value)

        for bad in ({"no-such-step": "rerun"}, {STEP: "reconcile"}, {STEP: ""}):
            with pytest.raises(ValueError):
                apply_decisions(RUN, bad)
            assert _state_of() == StepState.COMPLETED.value
            assert decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL).rule is HaltRule.POLICY_MANUAL

    def test_an_empty_map_is_not_a_failed_decision(self):
        """No decision was supplied, which is not the same as one that did not land: the
        function returns without reading, writing or logging."""
        _seed_step(state=StepState.COMPLETED.value)
        apply_decisions(RUN, {})
        assert _state_of() == StepState.COMPLETED.value


# ---------------------------------------------------------------------------
# TD-2 — the two enum members are not just vocabulary. Two places coerce a journal
# ``state`` string back through ``StepState(...)``, and a decided row now reaches both.
# ---------------------------------------------------------------------------
class TestADecidedRowSurvivesEveryStateCoercion:
    """Adding the members is what keeps these two readers honest about a decided row.

    Without them, ``StepState('rerun_authorized')`` raises: the result route would answer
    500 (its coercion is NOT wrapped) and the rebuild would silently substitute ``PENDING``
    (its coercion IS wrapped, by design, so one corrupt row cannot abort a rebuild). Both
    are worse than the truth, and neither was reachable until this unit wrote the states.
    """

    @pytest.mark.parametrize("decision", list(RecoveryDecision))
    def test_the_result_route_projects_it(self, client: TestClient, decision: RecoveryDecision):
        _seed_run()
        _seed_step(state=StepState.COMPLETED.value)
        apply_decisions(RUN, {STEP: decision})

        resp = client.get(f"/workflows/runs/{RUN}/result")

        assert resp.status_code == 200
        (step,) = resp.json()["steps"]
        assert step["state"] == workflow_journal._DECISION_STATES[decision.value]

    @pytest.mark.parametrize("decision", list(RecoveryDecision))
    def test_the_journal_rebuild_reads_it_without_degrading_to_pending(
        self, decision: RecoveryDecision
    ):
        _seed_run(tier="yaml")
        _seed_step(state=StepState.COMPLETED.value)
        apply_decisions(RUN, {STEP: decision})

        assert StepState(_state_of()) is StepState(
            workflow_journal._DECISION_STATES[decision.value]
        )
        assert StepState(_state_of()) is not StepState.PENDING


# ---------------------------------------------------------------------------
# SR-2 / RL-1 — NOTHING is written until the whole map validates. The assertion has to read
# the EARLIER rows' states; asserting only that the call raised proves nothing.
# ---------------------------------------------------------------------------
class TestNothingIsWrittenUntilTheWholeMapValidates:
    """The one threat this unit introduces, and it exists because ``apply_decisions`` takes a
    MAP: a typo in the third entry must not leave the first two holding durable consent to
    re-execute a side-effecting step, granted by a command that reported FAILURE.
    """

    def test_a_last_entry_with_a_bad_value_leaves_every_earlier_row_untouched(self):
        _seed_three_halted_steps()
        with pytest.raises(ValueError):
            apply_decisions(RUN, {STEP: "rerun", STEP_B: "skip", STEP_C: "rerunn"})

        assert _state_of(STEP) == StepState.COMPLETED.value
        assert _state_of(STEP_B) == StepState.COMPLETED.value
        assert _state_of(STEP_C) == StepState.COMPLETED.value

    def test_a_last_entry_with_an_unknown_step_id_leaves_every_earlier_row_untouched(self):
        _seed_three_halted_steps()
        with pytest.raises(ValueError):
            apply_decisions(RUN, {STEP: "rerun", STEP_B: "skip", "ghost-step": "rerun"})

        assert _state_of(STEP) == StepState.COMPLETED.value
        assert _state_of(STEP_B) == StepState.COMPLETED.value

    def test_the_same_map_without_the_typo_writes_every_row(self):
        """The positive control. Without it, "no row changed" could pass on a function that
        never writes at all — which would make the two tests above worthless."""
        _seed_three_halted_steps()
        apply_decisions(RUN, {STEP: "rerun", STEP_B: "skip", STEP_C: "rerun"})

        assert _state_of(STEP) == StepState.RERUN_AUTHORIZED.value
        assert _state_of(STEP_B) == StepState.REPLAY_AUTHORIZED.value
        assert _state_of(STEP_C) == StepState.RERUN_AUTHORIZED.value


# ---------------------------------------------------------------------------
# SR-3 — the writes are ONE transaction. Guards a DIFFERENT failure from SR-2's validation:
# a database fault part-way through, rather than operator error at the boundary.
# ---------------------------------------------------------------------------
class TestTheWritesAreOneTransaction:
    def test_a_failure_on_the_second_of_three_writes_rolls_the_first_back(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        _seed_three_halted_steps()
        proxies: List[_FailOnNthExecute] = []

        real_connect = workflow_journal._connect

        def _failing_connect() -> Any:
            # Each call gets its OWN proxy with its own counter, and ``_connect``'s
            # ``PRAGMA`` runs on the real connection BEFORE the wrap — so on the write
            # connection ``fail_on=2`` is precisely the SECOND of the three UPDATEs, and on
            # the ``get_steps`` connection (one SELECT) it never fires.
            proxy = _FailOnNthExecute(real_connect(), fail_on=2)
            proxies.append(proxy)
            return proxy

        monkeypatch.setattr(workflow_journal, "_connect", _failing_connect)

        with pytest.raises(sqlite3.OperationalError):
            apply_decisions(RUN, {STEP: "rerun", STEP_B: "skip", STEP_C: "rerun"})

        monkeypatch.setattr(workflow_journal, "_connect", real_connect)

        # The fault landed where the requirement says: one read connection (a single
        # SELECT that never trips the counter) and one write connection that got the
        # FIRST update in before the SECOND raised. Without this, the test would also
        # pass if the very first write had failed — which proves no rollback at all.
        assert len(proxies) == 2
        assert proxies[0].executes == 1
        assert proxies[1].executes == 2

        assert _state_of(STEP) == StepState.COMPLETED.value
        assert _state_of(STEP_B) == StepState.COMPLETED.value
        assert _state_of(STEP_C) == StepState.COMPLETED.value

    def test_the_same_three_writes_all_land_when_nothing_fails(self):
        """The control for the proxy itself: the fault is what rolls the writes back, not the
        wrapper and not the seeding."""
        _seed_three_halted_steps()
        apply_decisions(RUN, {STEP: "rerun", STEP_B: "skip", STEP_C: "rerun"})
        assert _state_of(STEP) == StepState.RERUN_AUTHORIZED.value
        assert _state_of(STEP_B) == StepState.REPLAY_AUTHORIZED.value
        assert _state_of(STEP_C) == StepState.RERUN_AUTHORIZED.value


# ---------------------------------------------------------------------------
# SR-5 / SR-6 / TD-6 — the log line is EVIDENCE: after the commit, success path only,
# identifiers only. The ordering is invisible unless a test asserts the ABSENCE of a line.
# ---------------------------------------------------------------------------
class TestTheLogLineIsEvidence:
    def test_a_rolled_back_transaction_produces_no_log_line(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        """Logging before the commit would record a decision that then rolled back. A log
        claiming consent was granted when it was not is worse than no log at all."""
        _seed_three_halted_steps()
        real_connect = workflow_journal._connect

        def _failing_connect() -> Any:
            return _FailOnNthExecute(real_connect(), fail_on=2)

        monkeypatch.setattr(workflow_journal, "_connect", _failing_connect)

        with caplog.at_level(logging.WARNING, logger=workflow_journal.__name__):
            with pytest.raises(sqlite3.OperationalError):
                apply_decisions(RUN, {STEP: "rerun", STEP_B: "skip", STEP_C: "rerun"})

        assert [r for r in caplog.records if "recovery decision" in r.getMessage()] == []

    def test_a_rejected_map_produces_no_log_line(self, caplog: pytest.LogCaptureFixture):
        """Success path only: a rejection already surfaces as a 400 to the operator who caused
        it, so logging attempted-but-rejected decisions would only fill the log with typos."""
        _seed_three_halted_steps()
        with caplog.at_level(logging.WARNING, logger=workflow_journal.__name__):
            with pytest.raises(ValueError):
                apply_decisions(RUN, {STEP: "rerun", STEP_C: "nope"})

        assert [r for r in caplog.records if "recovery decision" in r.getMessage()] == []

    def test_a_successful_decision_logs_exactly_one_warning_per_step(
        self, caplog: pytest.LogCaptureFixture
    ):
        _seed_three_halted_steps()
        with caplog.at_level(logging.WARNING, logger=workflow_journal.__name__):
            apply_decisions(RUN, {STEP: "rerun", STEP_B: "skip"})

        lines = [r.getMessage() for r in caplog.records if "recovery decision" in r.getMessage()]
        assert len(lines) == 2
        assert sum(1 for line in lines if STEP in line) == 1
        assert sum(1 for line in lines if STEP_B in line) == 1
        assert all(r.levelno == logging.WARNING for r in caplog.records)

    def test_the_log_line_carries_identifiers_only(self, caplog: pytest.LogCaptureFixture):
        """``run_id``, ``step_id``, the decision and the state it wrote — never a fingerprint,
        never step content (SR-5, inherited from ``step-fingerprint``'s SR-2)."""
        _seed_step(state=StepState.COMPLETED.value)
        with caplog.at_level(logging.WARNING, logger=workflow_journal.__name__):
            apply_decisions(RUN, {STEP: "skip"})

        (line,) = [r.getMessage() for r in caplog.records if "recovery decision" in r.getMessage()]
        assert RUN in line
        assert STEP in line
        assert "skip" in line
        assert StepState.REPLAY_AUTHORIZED.value in line
        for forbidden in (FP_CALL, "a" * 64, ENVELOPE.last_message, RESULT_JSON, SNAPSHOT):
            assert forbidden not in line


# ---------------------------------------------------------------------------
# BR-8 / SR-9 / RL-4 — a decision destroys no evidence. Only ``state`` moves.
# ---------------------------------------------------------------------------
class TestADecisionDestroysNoEvidence:
    @pytest.mark.parametrize(
        "decision,expected_state",
        [
            (RecoveryDecision.RERUN, StepState.RERUN_AUTHORIZED.value),
            (RecoveryDecision.SKIP, StepState.REPLAY_AUTHORIZED.value),
        ],
    )
    def test_every_other_column_is_byte_identical(
        self, decision: RecoveryDecision, expected_state: str
    ):
        """The record of what actually happened outlives the decision about what to do next,
        which is what makes a halt diagnosable afterwards (FR-12). ``updated_at`` is in this
        set deliberately: the decision is not an event in the step's own lifecycle."""
        workflow_journal.begin_step(RUN, STEP, TS, FP_CALL)
        workflow_journal.settle_step(
            RUN, STEP, StepState.FAILED.value, TS, RESULT_JSON, '{"out": 1}', "it broke"
        )
        before = _all_columns()

        apply_decisions(RUN, {STEP: decision})
        after = _all_columns()

        assert after["state"] == expected_state
        assert before["state"] != after["state"]
        assert {k: v for k, v in after.items() if k != "state"} == {
            k: v for k, v in before.items() if k != "state"
        }


# ---------------------------------------------------------------------------
# BR-9 / SR-7 / RL-6 — one decision authorises exactly ONE attempt.
# ---------------------------------------------------------------------------
class TestOneDecisionAuthorisesOneAttempt:
    def test_begin_step_consumes_the_authorisation(self):
        _seed_step(state=StepState.COMPLETED.value)
        apply_decisions(RUN, {STEP: "rerun"})
        assert _state_of() == StepState.RERUN_AUTHORIZED.value

        workflow_journal.begin_step(RUN, STEP, "2026-08-16T00:00:01Z", "v2:" + "e" * 64)

        assert _state_of() == StepState.RUNNING.value
        assert _state_of() != StepState.RERUN_AUTHORIZED.value

    def test_a_crash_before_settle_halts_and_asks_again(self):
        """Intended, not a leak: rule 2 means "dispatched, outcome unknown", and consent was
        given for an attempt that has now happened. Carrying it forward would let one decision
        authorise repeated re-execution — which is what FR-7 exists to prevent."""
        _seed_step(state=StepState.COMPLETED.value)
        apply_decisions(RUN, {STEP: "rerun"})
        assert decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL).verdict is ReplayVerdict.EXECUTE

        # The re-run is dispatched and the process dies before it settles.
        workflow_journal.begin_step(RUN, STEP, "2026-08-16T00:00:01Z", FP_CALL)

        d = decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL)
        assert d.verdict is ReplayVerdict.DECISION_REQUIRED
        assert d.rule is HaltRule.INTERRUPTED_NO_POLICY


# ---------------------------------------------------------------------------
# RL-3 — the decision outlives the process that made it.
# ---------------------------------------------------------------------------
class TestTheDecisionIsDurable:
    def test_a_separate_process_reads_the_decided_state(self):
        """A durable row update, not in-memory state — the gate reads journal rows, so an
        in-memory decision would be invisible to the step it was meant to resolve.

        Proved by a SEPARATE INTERPRETER reading the database file, so nothing about this
        process's connection, module state or caches can carry the answer. It proves the write
        is committed and visible elsewhere; it does not exercise CAO's own startup path.
        """
        _seed_step(state=StepState.COMPLETED.value)
        apply_decisions(RUN, {STEP: RecoveryDecision.SKIP})

        probe = (
            "import sqlite3,sys;"
            "c=sqlite3.connect(sys.argv[1]);"
            "print(c.execute('SELECT state FROM workflow_run_step "
            "WHERE run_id=? AND step_id=?',(sys.argv[2],sys.argv[3])).fetchone()[0])"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe, _db_path(), RUN, STEP],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == StepState.REPLAY_AUTHORIZED.value


# ---------------------------------------------------------------------------
# SR-4 — a rejection lands on 400, not 500. The route arms are KeyError -> 404,
# ResumeNotAllowedError -> 409, ResumeCorruptError -> 422, bare ValueError -> 400.
# ---------------------------------------------------------------------------
class TestTheRouteMapsARejectionTo400:
    def test_an_unknown_step_id_produces_400_not_500(self, client: TestClient):
        """A mistyped ``step_id`` is a client error; a 500 would tell the operator to file a
        bug instead of fixing their input."""
        _seed_run()
        _seed_step(state=StepState.COMPLETED.value)

        resp = client.post(
            f"/workflows/runs/{RUN}/resume", json={"decisions": {"ghost-step": "rerun"}}
        )

        assert resp.status_code == 400
        assert resp.status_code != 500

    def test_the_400_detail_names_the_offending_step_id(self, client: TestClient):
        _seed_run()
        _seed_step(state=StepState.COMPLETED.value)

        resp = client.post(
            f"/workflows/runs/{RUN}/resume", json={"decisions": {"ghost-step": "rerun"}}
        )

        assert "ghost-step" in resp.json()["detail"]

    def test_nothing_was_written_by_the_rejected_request(self, client: TestClient):
        _seed_run()
        _seed_step(state=StepState.COMPLETED.value)

        client.post(
            f"/workflows/runs/{RUN}/resume",
            json={"decisions": {STEP: "rerun", "ghost-step": "rerun"}},
        )

        assert _state_of() == StepState.COMPLETED.value


# ---------------------------------------------------------------------------
# SC-3 — concurrent resumes of the SAME run are prevented upstream, and this unit relies on
# that guard. The 409 must precede any write, asserted WITH a decision payload present.
# ---------------------------------------------------------------------------
class TestTheLiveRunGuardPrecedesAnyWrite:
    """Two operators deciding one live run would be the one contention case that needed real
    work. It cannot arise — but only because the decision is applied AFTER the resume's
    admission gates. Applied ahead of them, a second resume would write its consent, hit the
    409 and leave that consent live under the FIRST resume's drive: SR-2's threat at a
    different layer.
    """

    def test_a_live_run_with_a_decision_payload_is_rejected_409(self, client: TestClient):
        _seed_run()
        _seed_step(state=StepState.COMPLETED.value)
        workflow_service._active_drives.add(RUN)

        resp = client.post(f"/workflows/runs/{RUN}/resume", json={"decisions": {STEP: "rerun"}})

        assert resp.status_code == 409

    def test_that_rejection_applied_no_decision(self, client: TestClient):
        """The ordering, pinned. If ``apply_decisions`` ran before the liveness gate, this row
        would be ``rerun_authorized`` — durable consent granted by a request that returned
        409."""
        _seed_run()
        _seed_step(state=StepState.COMPLETED.value)
        workflow_service._active_drives.add(RUN)

        client.post(f"/workflows/runs/{RUN}/resume", json={"decisions": {STEP: "rerun"}})

        assert _state_of() == StepState.COMPLETED.value
        assert _state_of() != StepState.RERUN_AUTHORIZED.value


# ---------------------------------------------------------------------------
# BR-7 / BR-10 / TD-5 / TD-7 — the wiring: decisions reach the script arm's resume, are
# applied before the spawn, and are refused where they would be silently ignored.
# ---------------------------------------------------------------------------
class TestTheRouteWiring:
    def test_the_decisions_reach_the_script_arms_resume(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ):
        _seed_run()
        _seed_step(state=StepState.COMPLETED.value)
        seen: Dict[str, Any] = {}

        async def _capture(run_id: str, decisions: Optional[Dict[str, str]] = None):
            from cli_agent_orchestrator.models.workflow_runtime import WorkflowRunResult

            seen["run_id"] = run_id
            seen["decisions"] = decisions
            return WorkflowRunResult(
                run_id=run_id, workflow_name="wf", state=RunState.COMPLETED, started_at=TS
            )

        monkeypatch.setattr(script_runner, "resume_script_run", _capture)
        resp = client.post(f"/workflows/runs/{RUN}/resume", json={"decisions": {STEP: "skip"}})

        assert resp.status_code == 200
        assert seen["decisions"] == {STEP: "skip"}

    def test_a_resume_without_decisions_calls_the_pre_583_signature(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ):
        """An ordinary resume must not enter the new code path at all — the call is
        byte-identical to before, which is why every existing single-argument stub of
        ``resume_script_run`` still satisfies it."""
        _seed_run()
        calls: List[Tuple[Any, ...]] = []

        async def _single_arg(run_id: str):
            from cli_agent_orchestrator.models.workflow_runtime import WorkflowRunResult

            calls.append((run_id,))
            return WorkflowRunResult(
                run_id=run_id, workflow_name="wf", state=RunState.COMPLETED, started_at=TS
            )

        monkeypatch.setattr(script_runner, "resume_script_run", _single_arg)

        assert client.post(f"/workflows/runs/{RUN}/resume").status_code == 200
        assert client.post(f"/workflows/runs/{RUN}/resume", json={}).status_code == 200
        assert (
            client.post(f"/workflows/runs/{RUN}/resume", json={"decisions": None}).status_code
            == 200
        )
        assert calls == [(RUN,), (RUN,), (RUN,)]

    def test_the_decision_is_applied_before_the_spawn(self, monkeypatch: pytest.MonkeyPatch):
        """BR-7: the gate reads journal rows, so a decision applied after the spawn would be
        invisible to the step it was meant to resolve. Asserted by reading the row from inside
        the stubbed spawn — the last thing ``resume_script_run`` does."""
        import asyncio

        _seed_run()
        _seed_step(state=StepState.COMPLETED.value)
        observed: Dict[str, Any] = {}

        async def _fake_drive(record, snapshot_path, env):
            from cli_agent_orchestrator.models.workflow_runtime import WorkflowRunResult

            observed["state_at_spawn"] = _state_of()
            return WorkflowRunResult(
                run_id=record.run_id,
                workflow_name=record.workflow_name,
                state=RunState.COMPLETED,
                started_at=TS,
            )

        monkeypatch.setattr(script_runner, "_drive_process", _fake_drive)
        asyncio.run(script_runner.resume_script_run(RUN, decisions={STEP: "skip"}))

        assert observed["state_at_spawn"] == StepState.REPLAY_AUTHORIZED.value

    def test_a_yaml_tier_run_refuses_a_decision_rather_than_ignoring_it(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ):
        """INV-3, "a decision never silently fails". The gate that reads these states is
        consulted by the script tier alone, and the YAML resume resets every non-completed step
        to ``PENDING`` and re-runs it — so a ``skip`` there would re-execute the very step the
        operator asked to skip."""
        _seed_run(tier="yaml")
        _seed_step(state=StepState.COMPLETED.value)

        async def _must_not_run(run_id: str):
            raise AssertionError("the YAML resume must not be reached with decisions present")

        monkeypatch.setattr(workflow_service, "resume_from_last_completed", _must_not_run)
        resp = client.post(f"/workflows/runs/{RUN}/resume", json={"decisions": {STEP: "skip"}})

        assert resp.status_code == 400
        assert "script-tier" in resp.json()["detail"]
        assert _state_of() == StepState.COMPLETED.value


# ---------------------------------------------------------------------------
# BR-10 / TD-7 — the three surfaces accept the same closed set. No surface may accept a value
# another rejects.
# ---------------------------------------------------------------------------
class TestTheThreeSurfacesShareOneClosedSet:
    """``reconcile`` is the interesting rejection: it is a real ``RecoveryPolicy`` member, so a
    surface validating against the wrong enum would accept it."""

    def test_the_cli_rejects_an_unknown_value(self):
        from cli_agent_orchestrator.cli.commands.workflow import workflow

        result = CliRunner().invoke(workflow, ["resume", RUN, "--decide", f"{STEP}=reconcile"])

        assert result.exit_code != 0
        assert "not a recovery decision" in result.output

    def test_the_mcp_tool_rejects_an_unknown_value(self):
        import asyncio
        from unittest.mock import patch

        from cli_agent_orchestrator.mcp_server.server import workflow_resume

        with patch("cli_agent_orchestrator.mcp_server.server.requests.post") as post:
            out = asyncio.run(workflow_resume(RUN, decisions={STEP: "reconcile"}))

        assert out["ok"] is False
        assert "not a recovery decision" in out["error"]
        assert post.call_count == 0  # rejected without a round trip

    def test_the_route_rejects_an_unknown_value_with_400(self, client: TestClient):
        _seed_run()
        _seed_step(state=StepState.COMPLETED.value)

        resp = client.post(f"/workflows/runs/{RUN}/resume", json={"decisions": {STEP: "reconcile"}})

        assert resp.status_code == 400
        assert "not a recovery decision" in resp.json()["detail"]
        assert _state_of() == StepState.COMPLETED.value

    def test_all_three_accept_both_members(self, monkeypatch: pytest.MonkeyPatch):
        """The other half of "no surface accepts a value another rejects": the two real members
        must get THROUGH each surface, or the shared set would be enforced by everyone
        rejecting everything."""
        import asyncio
        from unittest.mock import MagicMock, patch

        from cli_agent_orchestrator.cli.commands.workflow import _parse_decisions
        from cli_agent_orchestrator.mcp_server.server import workflow_resume

        for value in (member.value for member in RecoveryDecision):
            assert _parse_decisions([f"{STEP}={value}"]) == {STEP: value}

            response = MagicMock()
            response.status_code = 200
            response.json.return_value = {"run_id": RUN, "state": "completed", "steps": []}
            with patch(
                "cli_agent_orchestrator.mcp_server.server.requests.post", return_value=response
            ) as post:
                out = asyncio.run(workflow_resume(RUN, decisions={STEP: value}))
            assert out["ok"] is True
            assert post.call_args.kwargs["json"] == {"decisions": {STEP: value}}

            _seed_step(state=StepState.COMPLETED.value, step_id=f"{STEP}-{value}")
            apply_decisions(RUN, {f"{STEP}-{value}": value})
            assert _state_of(f"{STEP}-{value}") == workflow_journal._DECISION_STATES[value]


# ---------------------------------------------------------------------------
# The CLI's own parsing rules (FR-7's operator-facing surface).
# ---------------------------------------------------------------------------
class TestTheCliDecideOption:
    def test_a_pair_without_an_equals_sign_is_rejected(self):
        from cli_agent_orchestrator.cli.commands.workflow import _parse_decisions

        with pytest.raises(Exception, match="must be"):
            _parse_decisions([STEP])

    def test_an_empty_step_id_is_rejected(self):
        from cli_agent_orchestrator.cli.commands.workflow import _parse_decisions

        with pytest.raises(Exception, match="empty"):
            _parse_decisions(["=rerun"])

    def test_a_repeated_step_id_is_rejected_rather_than_last_wins(self):
        """Silently dropping one of two decisions for the same step would apply a permission
        the operator did not think they were granting."""
        from cli_agent_orchestrator.cli.commands.workflow import _parse_decisions

        with pytest.raises(Exception, match="more than once"):
            _parse_decisions([f"{STEP}=rerun", f"{STEP}=skip"])

    def test_repeatable_decisions_parse_into_one_map(self):
        from cli_agent_orchestrator.cli.commands.workflow import _parse_decisions

        assert _parse_decisions([f"{STEP}=rerun", f"{STEP_B}=skip"]) == {
            STEP: "rerun",
            STEP_B: "skip",
        }


# ---------------------------------------------------------------------------
# PR #628 review P2 — consent that survives a process death is not consent for
# the next resume. The re-decision gate must inspect the durable row before this
# resume can overwrite it with a fresh decision.
# ---------------------------------------------------------------------------
class TestOrphanedRecoveryConsentRequiresARedecision:
    @staticmethod
    def _completing_drive(monkeypatch: pytest.MonkeyPatch):
        async def _drive(record, path, env):
            record.state = RunState.COMPLETED
            return WorkflowRunResult(
                run_id=RUN, workflow_name="wf", state=RunState.COMPLETED, started_at=TS
            )

        monkeypatch.setattr(script_runner, "_drive_process", _drive)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "authorised_state",
        [StepState.RERUN_AUTHORIZED.value, StepState.REPLAY_AUTHORIZED.value],
    )
    async def test_an_orphaned_authorisation_without_a_fresh_decision_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, authorised_state: str
    ):
        _seed_run()
        _seed_step(state=authorised_state)
        before = _all_columns()
        drive_calls: List[object] = []

        async def _must_not_drive(*args: object) -> WorkflowRunResult:
            drive_calls.append(args)
            raise AssertionError("the drive must not be invoked after a consent refusal")

        monkeypatch.setattr(script_runner, "_drive_process", _must_not_drive)

        with pytest.raises(
            workflow_service.ResumeNotAllowedError,
            match=rf"run '{RUN}' step '{STEP}'.*fresh decision",
        ):
            await script_runner.resume_script_run(RUN)

        assert _all_columns() == before
        assert drive_calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("run_state", "make_live", "message"),
        [
            ("failed", True, "currently executing; cannot resume a live run"),
            ("completed", False, "completed; not resumable"),
        ],
        ids=["liveness-before-consent", "resumability-before-consent"],
    )
    async def test_an_earlier_admission_gate_precedes_orphaned_consent(
        self, run_state: str, make_live: bool, message: str
    ):
        _seed_run(state=run_state)
        _seed_step(state=StepState.RERUN_AUTHORIZED.value)
        if make_live:
            workflow_service._active_drives.add(RUN)

        with pytest.raises(workflow_service.ResumeNotAllowedError, match=message) as error:
            await script_runner.resume_script_run(RUN)

        assert "fresh decision" not in str(error.value)

    @pytest.mark.asyncio
    async def test_a_rerun_revoke_leaves_the_next_ordinary_resume_unblocked(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        _seed_run()
        _seed_step(state=StepState.COMPLETED.value)
        self._completing_drive(monkeypatch)

        first = await script_runner.resume_script_run(RUN, {STEP: "rerun"})
        second = await script_runner.resume_script_run(RUN)

        assert first.state is RunState.COMPLETED
        assert second.state is RunState.COMPLETED
        assert _state_of() == StepState.COMPLETED.value

    @pytest.mark.asyncio
    async def test_a_failed_consent_read_propagates_without_spawning_the_drive(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        _seed_run()
        drive_calls: List[object] = []

        async def _must_not_drive(*args: object) -> WorkflowRunResult:
            drive_calls.append(args)
            raise AssertionError("the drive must not be invoked after a failed consent read")

        def _read_failure(*args: object) -> List[workflow_journal.StepRow]:
            raise sqlite3.Error("injected consent read failure")

        monkeypatch.setattr(script_runner, "_drive_process", _must_not_drive)
        monkeypatch.setattr(workflow_journal, "get_steps", _read_failure)

        with pytest.raises(sqlite3.Error, match="injected consent read failure"):
            await script_runner.resume_script_run(RUN)

        assert drive_calls == []

    @pytest.mark.asyncio
    async def test_a_yaml_resume_with_an_authorised_looking_step_never_hits_the_consent_gate(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        yaml_snapshot = json.dumps(
            {
                "name": "wf",
                "mode": "sequential",
                "steps": [{"id": STEP, "provider": "kiro", "agent": "agent", "prompt": "prompt"}],
            }
        )
        _seed_run(tier="yaml", snapshot=yaml_snapshot)
        _seed_step(state=StepState.RERUN_AUTHORIZED.value)
        get_steps_calls: List[str] = []
        real_get_steps = workflow_journal.get_steps

        def _record_get_steps(run_id: str) -> List[workflow_journal.StepRow]:
            get_steps_calls.append(run_id)
            return real_get_steps(run_id)

        async def _complete_yaml_drive(record, order) -> WorkflowRunResult:
            return WorkflowRunResult(
                run_id=record.run_id,
                workflow_name=record.workflow_name,
                state=RunState.COMPLETED,
                started_at=TS,
            )

        monkeypatch.setattr(workflow_journal, "get_steps", _record_get_steps)
        monkeypatch.setattr(workflow_service, "_drive", _complete_yaml_drive)

        result = await workflow_service.resume_from_last_completed(RUN)

        assert result.state is RunState.COMPLETED
        assert get_steps_calls == [RUN]

    @pytest.mark.asyncio
    async def test_a_refusal_releases_the_claim_and_the_remedy_still_works(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        self._completing_drive(monkeypatch)
        _seed_run()
        _seed_step(state=StepState.RERUN_AUTHORIZED.value)

        with pytest.raises(workflow_service.ResumeNotAllowedError, match="fresh decision"):
            await script_runner.resume_script_run(RUN)

        assert RUN not in workflow_service._active_drives

        with pytest.raises(workflow_service.ResumeNotAllowedError) as second:
            await script_runner.resume_script_run(RUN)

        assert "fresh decision" in str(second.value)
        assert "currently executing" not in str(second.value)

        result = await script_runner.resume_script_run(RUN, {STEP: "rerun"})

        assert result.state is RunState.COMPLETED

    @pytest.mark.asyncio
    async def test_a_fresh_decision_for_the_same_step_is_read_before_it_is_applied(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        _seed_run()
        _seed_step(state=StepState.COMPLETED.value)
        self._completing_drive(monkeypatch)
        events: List[str] = []
        real_get_steps = workflow_journal.get_steps
        real_apply_decisions = workflow_journal.apply_decisions

        def _record_get_steps(*args, **kwargs):
            events.append("read")
            return real_get_steps(*args, **kwargs)

        def _record_apply_decisions(*args, **kwargs):
            events.append("apply")
            return real_apply_decisions(*args, **kwargs)

        monkeypatch.setattr(workflow_journal, "get_steps", _record_get_steps)
        monkeypatch.setattr(workflow_journal, "apply_decisions", _record_apply_decisions)

        result = await script_runner.resume_script_run(RUN, {STEP: "rerun"})

        assert result.state is RunState.COMPLETED
        assert events[:2] == ["read", "apply"]

    @pytest.mark.asyncio
    async def test_a_fresh_decision_for_step_b_cannot_launder_step_as_orphaned_consent(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        _seed_run()
        _seed_step(state=StepState.RERUN_AUTHORIZED.value)
        _seed_step(state=StepState.COMPLETED.value, step_id=STEP_B)
        before_a = _all_columns()
        before_b = _all_columns(STEP_B)
        self._completing_drive(monkeypatch)

        with pytest.raises(
            workflow_service.ResumeNotAllowedError,
            match=rf"run '{RUN}' step '{STEP}'.*fresh decision",
        ):
            await script_runner.resume_script_run(RUN, {STEP_B: "rerun"})

        assert _all_columns() == before_a
        assert _all_columns(STEP_B) == before_b


# ---------------------------------------------------------------------------
# BR-9's SECOND HALF (PR #628 review, Copilot F6) — consent is good for exactly the drive it
# was granted for, so whatever that drive did not consume is REVOKED when it ends.
# ---------------------------------------------------------------------------
class TestUnconsumedConsentIsRevokedWhenTheDriveEnds:
    """``begin_step`` consuming ``rerun_authorized`` covered ONE case. Three were open, and the
    third is not a crash case at all:

    1. the resume raises AFTER ``apply_decisions`` commits (a failing generation bump or
       snapshot materialisation) — consent survived a command that reported failure;
    2. the drive runs but never dispatches the decided step — ``rerun_authorized`` stands;
    3. a ``skip`` is never consumed on ANY path, because a replay writes no row by design. One
       ``skip`` was standing authorisation for every later resume of that run.

    All three contradict the guarantee the CLI, the MCP tool, ``docs/workflows.md``, the
    authoring guide and ``SKILL.md`` all state in the same words: *consent does not persist
    across resumes*.

    Every test here drives the REAL ``resume_script_run`` with ``_drive_process`` substituted,
    so the admission gates, the generation bump, the ``finally`` and the journal writes are the
    shipped ones — only the subprocess is absent.
    """

    @staticmethod
    def _completing_drive(monkeypatch: pytest.MonkeyPatch, dispatch_step: bool = False):
        """Substitute the drive. ``dispatch_step=True`` models the script actually reaching the
        decided step, which is what CONSUMES a ``rerun`` (``begin_step`` -> ``running``)."""

        from cli_agent_orchestrator.models.workflow_runtime import WorkflowRunResult

        async def _drive(record, path, env):
            if dispatch_step:
                workflow_journal.begin_step(RUN, STEP, "2026-08-16T00:00:02Z", FP_CALL)
            record.state = RunState.COMPLETED
            return WorkflowRunResult(
                run_id=RUN, workflow_name="wf", state=RunState.COMPLETED, started_at=TS
            )

        monkeypatch.setattr(script_runner, "_drive_process", _drive)

    @staticmethod
    def _replaying_skip_drive(monkeypatch: pytest.MonkeyPatch):
        """Model replay's durable-row hydration before the ``finally`` revokes skip.

        ``replay_authorized`` is correct during this drive: the gate needs it to replay. The
        returned result and retained registry record must instead match the state the atomic
        revoke restored after the drive ends.
        """

        async def _drive(record, path, env):
            record.state = RunState.COMPLETED
            record.step_states[STEP] = workflow_service.StepRunState(
                step_id=STEP,
                state=StepState.REPLAY_AUTHORIZED,
                attempts=1,
            )
            return WorkflowRunResult(
                run_id=RUN,
                workflow_name="wf",
                state=RunState.COMPLETED,
                steps=[
                    StepResult(
                        id=STEP,
                        state=StepState.REPLAY_AUTHORIZED,
                        attempts=1,
                    )
                ],
                started_at=TS,
            )

        monkeypatch.setattr(script_runner, "_drive_process", _drive)

    @pytest.mark.asyncio
    async def test_a_resume_that_fails_after_the_grant_takes_it_back(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Copilot's own scenario: the generation bump raises AFTER the consent is committed.

        The resume reports failure, so the consent it granted must not outlive it — otherwise
        the operator's next resume re-executes a side-effecting step on consent given to a
        command that failed, without being asked.
        """
        _seed_run()
        _seed_step(state=StepState.COMPLETED.value)

        def _boom(*_a, **_k):
            raise RuntimeError("generation bump failed")

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.workflow_service.update_run_generation", _boom
        )

        with pytest.raises(RuntimeError, match="generation bump failed"):
            await script_runner.resume_script_run(RUN, {STEP: "rerun"})

        assert _state_of() == StepState.COMPLETED.value
        assert _state_of() != StepState.RERUN_AUTHORIZED.value

    @pytest.mark.asyncio
    async def test_a_skip_does_not_stand_after_the_drive_completes(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """THE WORST OF THE THREE, because it needs no failure at all: nothing ever consumed
        ``replay_authorized``, so one ``skip`` silenced rule 7 for that step forever."""
        _seed_run()
        _seed_step(state=StepState.COMPLETED.value)
        self._completing_drive(monkeypatch)

        await script_runner.resume_script_run(RUN, {STEP: "skip"})

        assert _state_of() == StepState.COMPLETED.value
        assert _state_of() != StepState.REPLAY_AUTHORIZED.value

    @pytest.mark.asyncio
    async def test_a_skip_resume_result_reports_the_revoked_prior_state(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The result is built before ``finally`` runs, so this catches publishing the
        temporary authorisation even though the database correctly restores the row."""
        _seed_run()
        _seed_step(state=StepState.COMPLETED.value)
        self._replaying_skip_drive(monkeypatch)

        result = await script_runner.resume_script_run(RUN, {STEP: "skip"})

        assert result.steps[0].state is StepState.COMPLETED
        assert _state_of() == StepState.COMPLETED.value

    @pytest.mark.asyncio
    async def test_a_skip_status_endpoint_reports_the_revoked_prior_state(
        self, monkeypatch: pytest.MonkeyPatch, client: TestClient
    ):
        """The bounded registry window serves the same normalized state as the resume result,
        rather than exposing ``replay_authorized`` after its durable grant was revoked."""
        _seed_run()
        _seed_step(state=StepState.COMPLETED.value)
        self._replaying_skip_drive(monkeypatch)

        await script_runner.resume_script_run(RUN, {STEP: "skip"})
        response = client.get(f"/workflows/runs/{RUN}")

        assert response.status_code == 200
        assert response.json()["steps"][0]["state"] == StepState.COMPLETED.value
        assert _state_of() == StepState.COMPLETED.value

    @pytest.mark.asyncio
    async def test_the_next_resume_halts_again_after_a_skip(self, monkeypatch: pytest.MonkeyPatch):
        """The consequence, asserted through the GATE rather than the column — this is the
        property the documentation promises, and the column is only how it is achieved.

        Note the counterpart in :class:`TestTheLoopThisUnitCloses`: a skip must NOT halt again
        WITHIN the resume it was granted for. Both are true, and together they are what "one
        attempt" means — the decision holds for its own drive and expires with it.
        """
        _seed_run()
        _seed_step(state=StepState.COMPLETED.value)
        self._completing_drive(monkeypatch)

        await script_runner.resume_script_run(RUN, {STEP: "skip"})

        d = decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL)
        assert d.verdict is ReplayVerdict.DECISION_REQUIRED
        assert d.rule is HaltRule.POLICY_MANUAL

    @pytest.mark.asyncio
    async def test_a_rerun_the_drive_never_dispatched_is_taken_back(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """An earlier step halts, so the decided step is never reached and ``begin_step`` never
        fires. The consent must expire with the attempt it was granted for."""
        _seed_run()
        _seed_step(state=StepState.COMPLETED.value)
        self._completing_drive(monkeypatch, dispatch_step=False)

        await script_runner.resume_script_run(RUN, {STEP: "rerun"})

        assert _state_of() == StepState.COMPLETED.value

    @pytest.mark.asyncio
    async def test_a_consumed_rerun_is_not_clobbered_by_the_revoke(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """THE MOST IMPORTANT TEST IN THIS CLASS. The revoke is a COMPARE-AND-SET, so a row the
        drive genuinely moved must be left exactly as the drive left it.

        A blind ``UPDATE ... SET state = <prior>`` would pass every other test here and quietly
        rewrite the outcome of the step that DID re-run — turning a completed rerun back into
        the state it held before the decision, which is both false history and a row the next
        resume would replay instead of the result it just produced.
        """
        _seed_run()
        _seed_step(state=StepState.COMPLETED.value)
        self._completing_drive(monkeypatch, dispatch_step=True)

        await script_runner.resume_script_run(RUN, {STEP: "rerun"})

        # ``begin_step`` moved it to ``running`` during the drive; the revoke matched nothing.
        assert _state_of() == StepState.RUNNING.value
        assert _state_of() != StepState.RERUN_AUTHORIZED.value

    @pytest.mark.asyncio
    async def test_an_ordinary_resume_neither_grants_nor_revokes(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The no-decision path is byte-identical to before: no extra read, no extra write, no
        log line. Asserted by spying on BOTH halves — a revoke that ran with an empty map would
        be harmless but would break the claim that this path is untouched."""
        _seed_run()
        _seed_step(state=StepState.COMPLETED.value)
        self._completing_drive(monkeypatch)
        calls: List[str] = []
        monkeypatch.setattr(
            workflow_journal,
            "apply_decisions",
            lambda *a, **k: calls.append("apply") or {},
        )
        monkeypatch.setattr(
            workflow_journal,
            "revoke_unconsumed_decisions",
            lambda *a, **k: calls.append("revoke") or [],
        )

        await script_runner.resume_script_run(RUN)

        assert calls == []
        assert _state_of() == StepState.COMPLETED.value

    @pytest.mark.asyncio
    async def test_a_failing_revoke_never_masks_the_drives_outcome(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Best-effort, and the direction matters: losing the run's result to a bookkeeping
        error would be a bigger fault than consent living one resume longer. The revoke is a
        compare-and-set, so a later attempt is still safe."""
        _seed_run()
        _seed_step(state=StepState.COMPLETED.value)
        self._completing_drive(monkeypatch)
        monkeypatch.setattr(
            workflow_journal,
            "revoke_unconsumed_decisions",
            lambda *a, **k: (_ for _ in ()).throw(sqlite3.Error("revoke failed")),
        )

        result = await script_runner.resume_script_run(RUN, {STEP: "skip"})

        assert result.state is RunState.COMPLETED

    @pytest.mark.asyncio
    @pytest.mark.parametrize("cancelled", [False, True], ids=["returned-result", "cancelled"])
    async def test_a_failing_live_state_mirror_never_masks_the_drives_outcome(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, cancelled: bool
    ):
        """The mirror is bookkeeping after a successful revoke, so its own bad return value
        must not replace either a completed result or the ``CancelledError`` already in flight.

        ``revoke_unconsumed_decisions`` currently returns keys from ``granted``, but this models
        a future contract regression explicitly: a stray key makes the mirror's map lookup
        raise. The outer guard is therefore part of BR-9's promise, not defensive decoration.
        """
        _seed_run()
        _seed_step(state=StepState.COMPLETED.value)

        if cancelled:

            async def _cancelled_drive(record, path, env):
                raise asyncio.CancelledError()

            monkeypatch.setattr(script_runner, "_drive_process", _cancelled_drive)
        else:
            self._completing_drive(monkeypatch)

        monkeypatch.setattr(
            workflow_journal,
            "revoke_unconsumed_decisions",
            lambda *a, **k: [STEP_B],
        )

        with caplog.at_level(logging.WARNING, logger=script_runner.__name__):
            if cancelled:
                with pytest.raises(asyncio.CancelledError):
                    await script_runner.resume_script_run(RUN, {STEP: "skip"})
            else:
                result = await script_runner.resume_script_run(RUN, {STEP: "skip"})

        if not cancelled:
            assert result.state is RunState.COMPLETED
        assert any(
            "consent was revoked but the live state could not be normalized" in record.getMessage()
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_an_unrecognised_revoked_prior_state_is_not_invented(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        """A corrupt durable prior value is restored atomically but cannot become a typed state.

        The settled replay record remains at its existing typed ``REPLAY_AUTHORIZED`` value:
        manufacturing ``RUNNING`` would publish an in-flight state and alter orphan
        reconciliation semantics. The warning is the observation; it is not permission to
        invent a value that the durable row did not say.
        """
        unknown_prior = "legacy-state-not-in-this-version"
        _seed_run()
        _seed_step(state=StepState.COMPLETED.value)
        self._replaying_skip_drive(monkeypatch)
        real_apply_decisions = workflow_journal.apply_decisions

        def _grant_with_unrecognised_prior(*args, **kwargs):
            real_apply_decisions(*args, **kwargs)
            return {STEP: unknown_prior}

        monkeypatch.setattr(workflow_journal, "apply_decisions", _grant_with_unrecognised_prior)

        with caplog.at_level(logging.WARNING, logger=script_runner.__name__):
            await script_runner.resume_script_run(RUN, {STEP: "skip"})

        live_state = workflow_service.run_registry[RUN].step_states[STEP].state
        assert _state_of() == unknown_prior
        assert live_state is StepState.REPLAY_AUTHORIZED
        assert live_state is not StepState.RUNNING
        assert isinstance(live_state, StepState)
        assert any(
            "unrecognised prior state could not be mirrored" in record.getMessage()
            for record in caplog.records
        )

    def test_revoke_reports_exactly_what_it_took_back(self):
        """The primitive on its own: it returns the steps it reverted, so a caller can log or
        assert on the set rather than re-reading every row."""
        _seed_run()
        _seed_step(state=StepState.COMPLETED.value)
        _seed_step(state=StepState.COMPLETED.value, step_id=STEP_B)

        granted = apply_decisions(RUN, {STEP: "rerun", STEP_B: "skip"})
        assert granted == {STEP: StepState.COMPLETED.value, STEP_B: StepState.COMPLETED.value}

        # STEP is consumed the way production consumes it; STEP_B is not.
        workflow_journal.begin_step(RUN, STEP, "2026-08-16T00:00:02Z", FP_CALL)

        revoked = workflow_journal.revoke_unconsumed_decisions(RUN, granted)

        assert revoked == [STEP_B]
        assert _state_of(STEP) == StepState.RUNNING.value
        assert _state_of(STEP_B) == StepState.COMPLETED.value

    def test_revoke_with_an_empty_map_is_a_no_op(self):
        """The ordinary resume's path through the primitive: no read, no write, no log."""
        assert workflow_journal.revoke_unconsumed_decisions(RUN, {}) == []

    def test_revoke_restores_only_state_and_destroys_no_evidence(self):
        """BR-8/SR-9 applies to taking consent back as much as to granting it: the record of
        what actually happened must survive both."""
        _seed_run()
        _seed_step(state=StepState.COMPLETED.value)
        before = _all_columns()

        granted = apply_decisions(RUN, {STEP: "skip"})
        workflow_journal.revoke_unconsumed_decisions(RUN, granted)

        assert _all_columns() == before

    @pytest.mark.asyncio
    async def test_a_cancelled_resume_still_revokes(self, monkeypatch: pytest.MonkeyPatch):
        """A cancelled resume consumed nothing, so consent must not survive it either.

        WHAT THIS TEST DOES NOT PROVE, stated so the next reader does not over-read it: it does
        NOT discriminate the direct revoke call from an ``await asyncio.to_thread(...)`` one.
        Both forms pass this test, and both pass a real ``task.cancel()`` probe on CPython 3.12
        — measured, not assumed. The direct call in ``resume_script_run``'s ``finally`` is a
        defensive choice (an ``await`` there is at the mercy of cancellation delivery, which is
        version- and repeat-cancel-dependent), not a repair of an observed failure. What this
        test DOES pin is the outcome on the cancellation path, which is the part that matters
        and the part that would break if the revoke were moved out of the ``finally``.
        """
        _seed_run()
        _seed_step(state=StepState.COMPLETED.value)

        async def _cancelled_drive(record, path, env):
            raise asyncio.CancelledError()

        monkeypatch.setattr(script_runner, "_drive_process", _cancelled_drive)

        with pytest.raises(asyncio.CancelledError):
            await script_runner.resume_script_run(RUN, {STEP: "rerun"})

        assert _state_of() == StepState.COMPLETED.value
        assert _state_of() != StepState.RERUN_AUTHORIZED.value

    @pytest.mark.asyncio
    async def test_a_cancelled_skip_normalizes_the_live_record(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """A replayed skip can be cancelled after it hydrated the temporary authorisation;
        revoke must then normalize the retained record even though no result exists."""
        _seed_run()
        _seed_step(state=StepState.COMPLETED.value)

        async def _cancelled_replay_drive(record, path, env):
            record.step_states[STEP] = workflow_service.StepRunState(
                step_id=STEP,
                state=StepState.REPLAY_AUTHORIZED,
                attempts=1,
            )
            raise asyncio.CancelledError()

        monkeypatch.setattr(script_runner, "_drive_process", _cancelled_replay_drive)

        with pytest.raises(asyncio.CancelledError):
            await script_runner.resume_script_run(RUN, {STEP: "skip"})

        assert _state_of() == StepState.COMPLETED.value
        assert workflow_service.run_registry[RUN].step_states[STEP].state is StepState.COMPLETED

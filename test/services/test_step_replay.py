"""Tests for ``replay-gate`` (issue #583, unit 7) — ``services/step_replay.decide``.

THE TEST SHAPE IS PAIRED ON PURPOSE (TD-6). BR-12 asks for totality proved by exhausting
``state`` x ``scheme`` x ``policy`` x fingerprint-match, and the honest implementation of that
is a parameterised sweep — but a sweep asserting only "returns a ``ReplayDecision``" would look
like thorough coverage while asserting almost nothing. So the sweep proves TOTALITY ONLY
(:class:`TestTotality`), and a separate EXPLICIT per-rule table asserts the specific
``verdict``, ``rule`` and ``envelope`` for each rule's triggering state
(:class:`TestPerRuleTable`). The table stays explicit rather than parameterised because a
parameterised table hides which rule a failure belongs to.

Rule coverage, one group per rule from
``construction/replay-gate/functional-design/business-rules.md``:

- BR-1  — the decision ORDER is the contract, so rows satisfying two rules are asserted to
  resolve to the EARLIER one (:class:`TestOrdering`). Each rule can pass in isolation while
  the order is wrong; only a row matching two rules distinguishes them.
- BR-2  — the infinite-halt regression: a ``rerun_authorized`` row returns ``EXECUTE``, and a
  ``running`` row with ``manual`` still halts (:class:`TestInfiniteHaltRegression`). THE MOST
  IMPORTANT TESTS IN THIS FILE. If rule 2 is ever rewritten as "not settled", the first is the
  only test that fails.
- BR-3/BR-4 — provenance before equality: an unverifiable fingerprint is never ``DIVERGED``
  (:class:`TestProvenanceBeforeEquality`).
- ``recovery-decision-intake`` BR-4 — rule 7's ONE exclusion for ``replay_authorized``, and
  that rules 3-6 still pre-empt it (:class:`TestPerRuleTable`, :class:`TestOrdering`). The
  three surviving-safety-rule tests SR-8 asks for live in ``test_recovery_decision_intake.py``,
  with the writer that produces the state.
- BR-5  — undeclared replays where ``MANUAL`` halts (:class:`TestUndeclaredVersusManual`).
- BR-6  — ``envelope`` iff ``REPLAY``, ``rule`` iff ``DECISION_REQUIRED``
  (:class:`TestFieldConditionality`).
- BR-7  — no ``diverged_fields`` attribute (:class:`TestDecisionShape`).
- BR-8  — each halting path returns its SPECIFIC ``HaltRule`` member — six assertions, not
  one "a rule is set" (:class:`TestSpecificHaltRules`). One membership check would pass with
  every path returning ``POLICY_MANUAL``, and unit 12 branches on this value.
- PR #628 review — the two APPENDED rules, one class each
  (:class:`TestFailedOutcomeHalts`, :class:`TestLossyEnvelopeHalts`). Each class asserts the
  halt, its SPECIFIC member, BOTH human escapes (``rerun`` and ``skip``), and that no earlier
  rule was masked — because a halt with no escape is the infinite-halt defect BR-2 exists to
  prevent, and appending a rule is only safe while every rule before it keeps precedence.
- BR-9/SR-4/SR-5 — the posture, by AST walk rather than grep, because this module's docstrings
  discuss logging and both exception types at length (:class:`TestPosture`).
- BR-10 — a failing journal read propagates and produces no verdict (:class:`TestReadFailure`).
- BR-11 — ``reconcile`` and ``idempotent`` are indistinguishable, and no reconciliation branch
  exists (:class:`TestReconcileEquivalence`).
- BR-13 — exactly one read, no writes (:class:`TestOneReadNoWrites`).
- BR-14 — a corrupt envelope is an absent envelope (:class:`TestCorruptEnvelope`).
- SR-1  — ``reason`` carries the identifiers and NEITHER digest (:class:`TestReasonContent`).
- SR-2  — the ``REPLAY`` envelope is returned unaltered (:class:`TestEnvelopePassthrough`).
- SR-3  — the incoming-fingerprint precondition, including that it fires BEFORE the read
  (:class:`TestIncomingFingerprintPrecondition`).
- SR-6  — a ``step_id`` full of SQL metacharacters round-trips as data (:class:`TestNoSql`).
- TD-1  — exactly five package imports, by AST (:class:`TestPosture`).

Rows are seeded THROUGH THE JOURNAL against a real temp SQLite DB (the patched
``DATABASE_FILE``), mirroring ``test_script_journal_extension.py``'s and
``test_journal_step_lifecycle.py``'s fixture pattern — not mocked, so the gate is tested
against the row shape ``begin_step``/``settle_step`` actually write.
"""

from __future__ import annotations

import ast
import inspect
import sqlite3
from pathlib import Path
from typing import Optional

import pytest

from cli_agent_orchestrator.clients.database import (
    _migrate_workflow_run,
    _migrate_workflow_run_step,
)
from cli_agent_orchestrator.models.workflow import RecoveryPolicy, StepResultEnvelope
from cli_agent_orchestrator.models.workflow_runtime import StepState
from cli_agent_orchestrator.services import step_replay, workflow_journal
from cli_agent_orchestrator.services.step_replay import ReplayDecision, ReplayVerdict, decide
from cli_agent_orchestrator.services.step_result import parse_envelope, serialise_envelope
from cli_agent_orchestrator.services.workflow_errors import HaltRule

RUN = "run-1"
STEP = "call-1"
TS = "2026-08-16T00:00:00Z"

# DIGEST-SHAPED FIXTURES (SR-1's verification asks for exactly this): the assertions about
# ``reason`` are then about what the gate DOES with a real digest, not about a string nobody
# supplied.
FP_CALL = "v2:" + "a" * 64  # this call's fingerprint, current scheme
FP_OTHER_V2 = "v2:" + "b" * 64  # a stored current-scheme value that DIFFERS
FP_LEGACY = "c" * 64  # the three-field scheme: a bare digest, no prefix
FP_LEGACY_SAME_DIGEST = "a" * 64  # FP_CALL's digest WITHOUT the prefix — legacy provenance

ENVELOPE = StepResultEnvelope(
    last_message="the step said this",
    status="completed",
    terminal_id="term-1",
)
RESULT_JSON = serialise_envelope(ENVELOPE)

# An envelope that reports its OWN lossiness (rule 9, PR #628 review). Both flags are set so
# one fixture covers both halves of the condition; the single-flag cases are asserted
# separately in :class:`TestLossyEnvelopeHalts`.
LOSSY_ENVELOPE = StepResultEnvelope(
    last_message="[REDACTED:aws-secret-key] tail",
    status="completed",
    terminal_id="term-1",
    truncated=True,
    redacted=True,
)

# States the gate observes, spelled as the literals the gate tests by name (it takes no
# ``StepState`` import for them, TD-1). ``recovery-decision-intake`` (unit 12) has since added
# both members and the writer, so ``test_authorised_state_literals_match_the_enum_values``
# below pins each literal against its member.
RERUN_AUTHORIZED = "rerun_authorized"
REPLAY_AUTHORIZED = "replay_authorized"

_POLICIES: list[Optional[RecoveryPolicy]] = [
    None,
    RecoveryPolicy.IDEMPOTENT,
    RecoveryPolicy.RECONCILE,
    RecoveryPolicy.MANUAL,
]


@pytest.fixture(autouse=True)
def _patched_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the journal at a temp DB and create the tables (both #583 columns included)."""
    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    _migrate_workflow_run()
    _migrate_workflow_run_step()
    yield db_path


def _direct_connect() -> sqlite3.Connection:
    from cli_agent_orchestrator.constants import DATABASE_FILE

    return sqlite3.connect(str(DATABASE_FILE))


def _seed_step(
    *,
    state: str,
    fingerprint: Optional[str],
    result_json: Optional[str] = None,
    run_id: str = RUN,
    step_id: str = STEP,
) -> None:
    """Seed one ``workflow_run_step`` row through the journal's own write path.

    ``begin_step`` writes the fingerprint (``settle_step`` never touches the column, unit 6's
    BR-9), then ``settle_step`` writes the state and the envelope in one statement. A NULL
    fingerprint — the ``absent`` scheme, i.e. a row that predates the column — is set with a
    raw UPDATE because ``begin_step``'s signature requires a ``str``; the same raw-UPDATE
    idiom ``test_script_journal_extension.py`` uses for ``tier``/``generation``.
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


def _module_ast() -> ast.Module:
    """Parse the module's own source. AST, never a grep — its docstrings discuss the very
    things the posture tests forbid (logging, both exception types, ``secret_gate``), so a text
    search would match its prose and pass on a module that really did import them.
    ``step-fingerprint`` learned this the same way."""
    path = Path(inspect.getsourcefile(step_replay) or "")  # type: ignore[arg-type]
    return ast.parse(path.read_text(encoding="utf-8"))


def _package_imports() -> dict[str, set[str]]:
    """Map every imported ``cli_agent_orchestrator`` module path to the names taken from it.

    Walks the whole tree, so a function-local import cannot hide a sixth dependency edge.
    """
    found: dict[str, set[str]] = {}
    for node in ast.walk(_module_ast()):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "cli_agent_orchestrator"
        ):
            assert node.module is not None
            found.setdefault(node.module, set()).update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("cli_agent_orchestrator"):
                    found.setdefault(alias.name, set())
    return found


# ---------------------------------------------------------------------------
# The per-rule table (BR-1's per-rule half) — EXPLICIT, never parameterised, so a failure
# names the rule it belongs to. Each test constructs the row state that makes exactly that
# rule the FIRST match, and asserts the specific verdict, rule and envelope.
# ---------------------------------------------------------------------------
class TestPerRuleTable:
    """One test per rule (plus the policy branches of rules 2, 4 and 8)."""

    def test_rule1_absent_row_executes(self):
        """Rule 1: no row at all — the step has never been dispatched."""
        d = decide(RUN, STEP, FP_CALL, None)
        assert d.verdict is ReplayVerdict.EXECUTE
        assert d.rule is None
        assert d.envelope is None

    def test_rule1_rerun_authorized_row_executes(self):
        """Rule 1's second half: a human consented (the state unit 12 writes)."""
        _seed_step(state=RERUN_AUTHORIZED, fingerprint=FP_CALL, result_json=RESULT_JSON)
        d = decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL)
        assert d.verdict is ReplayVerdict.EXECUTE
        assert d.rule is None
        assert d.envelope is None

    def test_rule2_running_with_idempotent_executes(self):
        _seed_step(state=StepState.RUNNING.value, fingerprint=FP_CALL)
        d = decide(RUN, STEP, FP_CALL, RecoveryPolicy.IDEMPOTENT)
        assert d.verdict is ReplayVerdict.EXECUTE
        assert d.rule is None

    def test_rule2_running_with_reconcile_executes(self):
        _seed_step(state=StepState.RUNNING.value, fingerprint=FP_CALL)
        d = decide(RUN, STEP, FP_CALL, RecoveryPolicy.RECONCILE)
        assert d.verdict is ReplayVerdict.EXECUTE
        assert d.rule is None

    def test_rule2_running_with_manual_halts(self):
        _seed_step(state=StepState.RUNNING.value, fingerprint=FP_CALL)
        d = decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL)
        assert d.verdict is ReplayVerdict.DECISION_REQUIRED
        assert d.rule is HaltRule.INTERRUPTED_NO_POLICY

    def test_rule2_running_undeclared_halts(self):
        """BR-5's other half: undeclared HALTS here, because the alternative is
        re-execution."""
        _seed_step(state=StepState.RUNNING.value, fingerprint=FP_CALL)
        d = decide(RUN, STEP, FP_CALL, None)
        assert d.verdict is ReplayVerdict.DECISION_REQUIRED
        assert d.rule is HaltRule.INTERRUPTED_NO_POLICY

    def test_rule3_settled_without_envelope_halts(self):
        """Rule 3 — FR-4 guard 2. The fingerprint matches and the scheme is current, so only
        the missing envelope can be producing this verdict."""
        _seed_step(state=StepState.COMPLETED.value, fingerprint=FP_CALL, result_json=None)
        d = decide(RUN, STEP, FP_CALL, None)
        assert d.verdict is ReplayVerdict.DECISION_REQUIRED
        assert d.rule is HaltRule.ENVELOPE_ABSENT
        assert d.envelope is None

    def test_rule4_legacy_scheme_with_idempotent_executes(self):
        _seed_step(state=StepState.COMPLETED.value, fingerprint=FP_LEGACY, result_json=RESULT_JSON)
        d = decide(RUN, STEP, FP_CALL, RecoveryPolicy.IDEMPOTENT)
        assert d.verdict is ReplayVerdict.EXECUTE
        assert d.rule is None
        assert d.envelope is None

    def test_rule4_absent_scheme_with_reconcile_executes(self):
        """A NULL column — a row predating the fingerprint column — routes as rule 4 too."""
        _seed_step(state=StepState.COMPLETED.value, fingerprint=None, result_json=RESULT_JSON)
        d = decide(RUN, STEP, FP_CALL, RecoveryPolicy.RECONCILE)
        assert d.verdict is ReplayVerdict.EXECUTE
        assert d.rule is None

    def test_rule5_legacy_scheme_with_manual_halts(self):
        _seed_step(state=StepState.COMPLETED.value, fingerprint=FP_LEGACY, result_json=RESULT_JSON)
        d = decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL)
        assert d.verdict is ReplayVerdict.DECISION_REQUIRED
        assert d.rule is HaltRule.PROVENANCE_UNVERIFIABLE
        assert "legacy" in d.reason

    def test_rule5_absent_scheme_undeclared_halts(self):
        """``legacy`` and ``absent`` route identically but stay DISTINCT FACTS in ``reason``
        — the distinction ``scheme_of`` keeps on purpose."""
        _seed_step(state=StepState.COMPLETED.value, fingerprint=None, result_json=RESULT_JSON)
        d = decide(RUN, STEP, FP_CALL, None)
        assert d.verdict is ReplayVerdict.DECISION_REQUIRED
        assert d.rule is HaltRule.PROVENANCE_UNVERIFIABLE
        assert "absent" in d.reason

    def test_rule6_fingerprint_mismatch_diverges(self):
        """Rule 6 — FR-3. Both values are current-scheme, which is what makes the comparison
        meaningful."""
        _seed_step(
            state=StepState.COMPLETED.value, fingerprint=FP_OTHER_V2, result_json=RESULT_JSON
        )
        d = decide(RUN, STEP, FP_CALL, None)
        assert d.verdict is ReplayVerdict.DIVERGED
        assert d.rule is None
        assert d.envelope is None

    def test_rule7_match_with_manual_halts(self):
        _seed_step(state=StepState.COMPLETED.value, fingerprint=FP_CALL, result_json=RESULT_JSON)
        d = decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL)
        assert d.verdict is ReplayVerdict.DECISION_REQUIRED
        assert d.rule is HaltRule.POLICY_MANUAL
        assert d.envelope is None

    def test_rule7_is_excluded_for_a_replay_authorized_row(self):
        """Rule 7's ONE exclusion (``recovery-decision-intake`` BR-4): the same row that halts
        above falls through to the catch-all once a human has authorised using the stored result. The
        three surviving-safety-rule tests live in ``test_recovery_decision_intake.py``."""
        _seed_step(state=REPLAY_AUTHORIZED, fingerprint=FP_CALL, result_json=RESULT_JSON)
        d = decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL)
        assert d.verdict is ReplayVerdict.REPLAY
        assert d.rule is None
        assert d.envelope == ENVELOPE

    def test_the_exclusion_is_scoped_to_the_manual_halt_and_nothing_else(self):
        """It suppresses ONE verdict for ONE policy. An undeclared row already replayed at rule
        8 before the exclusion existed, so the state must not change that answer either."""
        _seed_step(state=REPLAY_AUTHORIZED, fingerprint=FP_CALL, result_json=RESULT_JSON)
        assert decide(RUN, STEP, FP_CALL, None).verdict is ReplayVerdict.REPLAY
        assert decide(RUN, STEP, FP_CALL, RecoveryPolicy.IDEMPOTENT).verdict is (
            ReplayVerdict.REPLAY
        )

    def test_catch_all_match_undeclared_replays(self):
        """The catch-all — FR-1, and BR-5: undeclared REPLAYS, because replay executes nothing."""
        _seed_step(state=StepState.COMPLETED.value, fingerprint=FP_CALL, result_json=RESULT_JSON)
        d = decide(RUN, STEP, FP_CALL, None)
        assert d.verdict is ReplayVerdict.REPLAY
        assert d.rule is None
        assert d.envelope == ENVELOPE

    def test_catch_all_match_idempotent_replays(self):
        _seed_step(state=StepState.COMPLETED.value, fingerprint=FP_CALL, result_json=RESULT_JSON)
        d = decide(RUN, STEP, FP_CALL, RecoveryPolicy.IDEMPOTENT)
        assert d.verdict is ReplayVerdict.REPLAY
        assert d.envelope == ENVELOPE

    def test_catch_all_match_reconcile_replays(self):
        _seed_step(state=StepState.COMPLETED.value, fingerprint=FP_CALL, result_json=RESULT_JSON)
        d = decide(RUN, STEP, FP_CALL, RecoveryPolicy.RECONCILE)
        assert d.verdict is ReplayVerdict.REPLAY
        assert d.envelope == ENVELOPE

    def test_rule8_halts_a_failed_step_rather_than_replaying_it(self):
        """PR #628 review (Copilot F1) — THIS TEST ASSERTED THE OPPOSITE AND WAS WRONG.

        Its previous form read: "a FAILED row is settled and carries an envelope (unit 2's
        BR-1 writes one unconditionally), so it replays like any other match". The envelope
        premise is true and the conclusion does not follow. On the ORIGINAL drive this step
        RAISED — ``run_agent_step`` raised ``StepExecutionError``, the route answered 502/504
        and the shim turned that into ``ShimHTTPError``. Replaying it answers HTTP 200 with a
        ``StepHandle``, so the author's script continues past a call that failed, and the
        failure is silently deleted from the replayed run.

        Rule 8 halts instead. Envelope ABSENCE still means *crash between the writes* and
        never *the step failed* — that distinction (unit 2's BR-1) is untouched and is why
        this halts as ``OUTCOME_FAILED`` rather than as ``ENVELOPE_ABSENT``.
        """
        _seed_step(state=StepState.FAILED.value, fingerprint=FP_CALL, result_json=RESULT_JSON)
        d = decide(RUN, STEP, FP_CALL, None)
        assert d.verdict is ReplayVerdict.DECISION_REQUIRED
        assert d.rule is HaltRule.OUTCOME_FAILED
        # BR-6: a halting verdict hands the caller no envelope to serve.
        assert d.envelope is None

    def test_rule9_halts_a_lossy_envelope_rather_than_serving_abridged_text(self):
        """PR #628 review (Copilot F5). ``build_envelope`` redacts and then bounds
        ``last_message`` before it reaches SQLite, but the ORIGINAL call answered with
        ``run_agent_step``'s raw text — the success arm of the route never touches the
        envelope. So replaying a lossy envelope serves text the original call did not, and
        ``RunStepResponse`` has no ``truncated``/``redacted`` field in which to say so.
        """
        _seed_step(
            state=StepState.COMPLETED.value,
            fingerprint=FP_CALL,
            result_json=serialise_envelope(LOSSY_ENVELOPE),
        )
        d = decide(RUN, STEP, FP_CALL, None)
        assert d.verdict is ReplayVerdict.DECISION_REQUIRED
        assert d.rule is HaltRule.ENVELOPE_LOSSY
        assert d.envelope is None

    def test_rule10_match_replays_when_the_outcome_is_good_and_the_envelope_is_whole(self):
        """The catch-all still replays — rules 8 and 9 narrowed it, they did not close it."""
        _seed_step(state=StepState.COMPLETED.value, fingerprint=FP_CALL, result_json=RESULT_JSON)
        d = decide(RUN, STEP, FP_CALL, None)
        assert d.verdict is ReplayVerdict.REPLAY
        assert d.envelope == ENVELOPE

    def test_running_literal_matches_the_enum_value(self):
        """The gate spells ``'running'`` as a bare literal (it must not add a sixth package
        import for one string, TD-1). This pins the literal against the enum from THIS file's
        own import, so a rename on either side fails loudly — unit 6's BR-12 precedent."""
        assert StepState.RUNNING.value == "running"
        _seed_step(state="running", fingerprint=FP_CALL, result_json=RESULT_JSON)
        assert decide(RUN, STEP, FP_CALL, None).rule is HaltRule.INTERRUPTED_NO_POLICY

    def test_authorised_state_literals_match_the_enum_values(self):
        """The same pin for the two states ``apply_decisions`` writes (unit 12's TD-2): the gate
        reads them as bare literals, so a rename on either side must fail loudly rather than
        leaving the gate testing a state nothing writes."""
        assert step_replay._RERUN_AUTHORIZED == StepState.RERUN_AUTHORIZED.value
        assert step_replay._REPLAY_AUTHORIZED == StepState.REPLAY_AUTHORIZED.value
        assert RERUN_AUTHORIZED == StepState.RERUN_AUTHORIZED.value
        assert REPLAY_AUTHORIZED == StepState.REPLAY_AUTHORIZED.value


# ---------------------------------------------------------------------------
# BR-1 — the ORDER is the contract. Each rule above can pass in isolation while the order is
# wrong; only a row matching TWO rules distinguishes them.
# ---------------------------------------------------------------------------
class TestOrdering:
    """Rows that satisfy two rules must resolve to the EARLIER one."""

    def test_rerun_authorized_beats_every_later_rule(self):
        """Rule 1 over rules 3, 5 and 6 at once: the row has no envelope, a legacy scheme AND
        a mismatching value, and still executes. Nothing may pre-empt a human's consent."""
        _seed_step(state=RERUN_AUTHORIZED, fingerprint=FP_LEGACY, result_json=None)
        d = decide(RUN, STEP, FP_CALL, None)
        assert d.verdict is ReplayVerdict.EXECUTE
        assert d.rule is None

    def test_running_beats_the_absent_envelope_rule(self):
        """Rule 2 over rule 3: a ``running`` row has no envelope by construction (unit 6's
        split write), so if rule 3 came first every interrupted step would report
        ``ENVELOPE_ABSENT`` and FR-7's trigger would never fire."""
        _seed_step(state=StepState.RUNNING.value, fingerprint=FP_CALL, result_json=None)
        d = decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL)
        assert d.rule is HaltRule.INTERRUPTED_NO_POLICY
        assert d.rule is not HaltRule.ENVELOPE_ABSENT

    def test_absent_envelope_beats_the_provenance_rule(self):
        """Rule 3 over rule 5: settled, no envelope, AND a legacy scheme. Both halt, so only
        the ``HaltRule`` member distinguishes the order — and unit 12 branches on it."""
        _seed_step(state=StepState.COMPLETED.value, fingerprint=FP_LEGACY, result_json=None)
        d = decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL)
        assert d.rule is HaltRule.ENVELOPE_ABSENT
        assert d.rule is not HaltRule.PROVENANCE_UNVERIFIABLE

    def test_absent_envelope_beats_the_policy_execute_rule(self):
        """Rule 3 over rule 4: settled, no envelope, legacy scheme, ``idempotent``. Rule 3 is
        FR-4 guard 2 and it is not negotiable by policy — a row with no result cannot be
        judged, so the human decides even though re-execution was declared safe."""
        _seed_step(state=StepState.COMPLETED.value, fingerprint=FP_LEGACY, result_json=None)
        d = decide(RUN, STEP, FP_CALL, RecoveryPolicy.IDEMPOTENT)
        assert d.verdict is ReplayVerdict.DECISION_REQUIRED
        assert d.rule is HaltRule.ENVELOPE_ABSENT

    def test_policy_execute_beats_the_provenance_halt(self):
        """Rule 4 over rule 5 (BR-3): both fire on the same scheme classification, and rule 4
        first is what stops an author who declared re-execution safe being punished across the
        scheme-upgrade window."""
        _seed_step(state=StepState.COMPLETED.value, fingerprint=FP_LEGACY, result_json=RESULT_JSON)
        assert decide(RUN, STEP, FP_CALL, RecoveryPolicy.IDEMPOTENT).verdict is (
            ReplayVerdict.EXECUTE
        )
        assert decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL).rule is (
            HaltRule.PROVENANCE_UNVERIFIABLE
        )

    def test_provenance_beats_the_divergence_rule(self):
        """Rule 5 over rule 6 (BR-4/INV-2): a legacy stored value can never equal a ``v2``
        one, so if rule 6 came first EVERY legacy row would report ``DIVERGED`` — "the script
        changed between runs" — when the truth is "this value cannot be verified"."""
        _seed_step(state=StepState.COMPLETED.value, fingerprint=FP_LEGACY, result_json=RESULT_JSON)
        d = decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL)
        assert d.verdict is ReplayVerdict.DECISION_REQUIRED
        assert d.verdict is not ReplayVerdict.DIVERGED

    def test_divergence_beats_the_manual_halt(self):
        """Rule 6 over rule 7: a changed script is a different remedy from a requested pause,
        and unit 9 maps the two to different statuses."""
        _seed_step(
            state=StepState.COMPLETED.value, fingerprint=FP_OTHER_V2, result_json=RESULT_JSON
        )
        d = decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL)
        assert d.verdict is ReplayVerdict.DIVERGED
        assert d.rule is None

    def test_manual_halt_beats_the_replay_rule(self):
        """Rule 7 over the catch-all: a verified match with ``manual`` halts, because a human asked
        to see the step."""
        _seed_step(state=StepState.COMPLETED.value, fingerprint=FP_CALL, result_json=RESULT_JSON)
        d = decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL)
        assert d.verdict is ReplayVerdict.DECISION_REQUIRED
        assert d.envelope is None

    def test_divergence_still_beats_a_skip_authorisation(self):
        """Rules 3-6 over rule 7's EXCLUSION (unit 12's BR-4/SR-8). The exclusion is ON rule 7,
        so it cannot pre-empt an earlier rule: a ``replay_authorized`` row whose script changed
        still DIVERGES. Hoisted into a rule of its own before rule 6, this is the assertion
        that breaks — and FR-3 would stop failing loudly on a changed script."""
        _seed_step(state=REPLAY_AUTHORIZED, fingerprint=FP_OTHER_V2, result_json=RESULT_JSON)
        d = decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL)
        assert d.verdict is ReplayVerdict.DIVERGED
        assert d.verdict is not ReplayVerdict.REPLAY


# ---------------------------------------------------------------------------
# BR-2 / INV-3 — the infinite-halt regression. THE SINGLE MOST IMPORTANT TEST IN THIS UNIT.
# ---------------------------------------------------------------------------
class TestInfiniteHaltRegression:
    """Rule 2 guards on ``running`` EXACTLY, and rule 1 admits ``rerun_authorized``.

    Both halves are one fix for one defect. A ``manual`` step halts; the human authorises a
    rerun; the row becomes non-settled; and a rule 2 written as "not settled" halts it AGAIN —
    forever, with no escape from the mechanism built to provide one. If rule 2 is ever
    rewritten as the broader condition, the first test below is the only one that fails.
    """

    def test_rerun_authorized_returns_execute_not_decision_required(self):
        """The whole cycle: halt on ``manual``, then the human authorises the rerun.

        Asserted as a scenario rather than a single state, because the defect only exists as a
        sequence: the second decision must not repeat the first.
        """
        _seed_step(state=StepState.COMPLETED.value, fingerprint=FP_CALL, result_json=RESULT_JSON)
        halted = decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL)
        assert halted.verdict is ReplayVerdict.DECISION_REQUIRED
        assert halted.rule is HaltRule.POLICY_MANUAL

        # The human authorises a rerun (unit 12's transition, simulated by the state alone).
        with _direct_connect() as conn:
            conn.execute(
                "UPDATE workflow_run_step SET state = ? WHERE run_id = ? AND step_id = ?",
                (RERUN_AUTHORIZED, RUN, STEP),
            )

        authorised = decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL)
        assert authorised.verdict is ReplayVerdict.EXECUTE
        assert authorised.verdict is not ReplayVerdict.DECISION_REQUIRED
        assert authorised.rule is None

    def test_running_row_with_manual_still_halts(self):
        """The other half: narrowing rule 2 to ``running`` must not be over-corrected into
        letting an interrupted step re-execute without a policy. FR-7's trigger still
        fires."""
        _seed_step(state=StepState.RUNNING.value, fingerprint=FP_CALL, result_json=RESULT_JSON)
        d = decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL)
        assert d.verdict is ReplayVerdict.DECISION_REQUIRED
        assert d.rule is HaltRule.INTERRUPTED_NO_POLICY

    def test_rerun_authorized_wins_even_with_no_declared_policy(self):
        """Consent is not policy-dependent: the row executes whatever the author declared."""
        _seed_step(state=RERUN_AUTHORIZED, fingerprint=FP_CALL, result_json=RESULT_JSON)
        for policy in _POLICIES:
            assert decide(RUN, STEP, FP_CALL, policy).verdict is ReplayVerdict.EXECUTE

    def test_an_unknown_non_settled_state_is_not_treated_as_running(self):
        """BR-2's wording is "on ``running`` specifically, NEVER on not-settled", and this is
        the row that holds it to that.

        Found by mutation: with rule 1's ``rerun_authorized`` arm intact, broadening rule 2 to
        "not settled" is invisible to the test above, because rule 1 answers first. So that
        test guards ONE half of the fix and this one guards the other — an unknown state with a
        verified match and a readable envelope must reach the catch-all, not be halted as
        ``INTERRUPTED_NO_POLICY`` by a rule 2 that grew.
        """
        _seed_step(
            state="a_state_no_unit_has_written_yet",
            fingerprint=FP_CALL,
            result_json=RESULT_JSON,
        )
        d = decide(RUN, STEP, FP_CALL, None)
        assert d.verdict is ReplayVerdict.REPLAY
        assert d.rule is not HaltRule.INTERRUPTED_NO_POLICY


# ---------------------------------------------------------------------------
# BR-4 / INV-2 — an unverifiable fingerprint is NEVER reported as divergence.
# ---------------------------------------------------------------------------
class TestProvenanceBeforeEquality:
    """Rules 4-5 sit ahead of rule 6, so "the stored hash differs" is never claimed about a
    hash computed under different rules. Each row below carries an envelope, so rule 3 cannot
    be the thing producing the halt."""

    def test_legacy_row_whose_value_differs_is_never_diverged(self):
        _seed_step(state=StepState.COMPLETED.value, fingerprint=FP_LEGACY, result_json=RESULT_JSON)
        d = decide(RUN, STEP, FP_CALL, None)
        assert d.verdict is ReplayVerdict.DECISION_REQUIRED
        assert d.rule is HaltRule.PROVENANCE_UNVERIFIABLE
        assert d.verdict is not ReplayVerdict.DIVERGED

    def test_absent_scheme_row_is_never_diverged(self):
        _seed_step(state=StepState.COMPLETED.value, fingerprint=None, result_json=RESULT_JSON)
        d = decide(RUN, STEP, FP_CALL, None)
        assert d.verdict is ReplayVerdict.DECISION_REQUIRED
        assert d.rule is HaltRule.PROVENANCE_UNVERIFIABLE
        assert d.verdict is not ReplayVerdict.DIVERGED

    def test_a_legacy_row_carrying_the_same_digest_still_never_replays(self):
        """The sharpest case: the stored value is FP_CALL's digest with the prefix stripped, so
        a naive implementation that compared digests rather than classifying first would REPLAY
        it. Unverifiable provenance never replays as a match (FR-6)."""
        _seed_step(
            state=StepState.COMPLETED.value,
            fingerprint=FP_LEGACY_SAME_DIGEST,
            result_json=RESULT_JSON,
        )
        d = decide(RUN, STEP, FP_CALL, None)
        assert d.verdict is ReplayVerdict.DECISION_REQUIRED
        assert d.rule is HaltRule.PROVENANCE_UNVERIFIABLE


# ---------------------------------------------------------------------------
# BR-5 — undeclared replays; MANUAL halts. Same row, one argument different.
# ---------------------------------------------------------------------------
class TestUndeclaredVersusManual:
    """The asymmetry ``RecoveryPolicy``'s docstring (:125-128) argues: undeclared and
    ``MANUAL`` behave identically wherever the alternative is re-execution, and differently
    where a verified replay is available — because replay executes nothing."""

    def test_identical_row_undeclared_replays(self):
        _seed_step(state=StepState.COMPLETED.value, fingerprint=FP_CALL, result_json=RESULT_JSON)
        d = decide(RUN, STEP, FP_CALL, None)
        assert d.verdict is ReplayVerdict.REPLAY
        assert d.envelope == ENVELOPE
        assert d.rule is None

    def test_identical_row_manual_halts(self):
        _seed_step(state=StepState.COMPLETED.value, fingerprint=FP_CALL, result_json=RESULT_JSON)
        d = decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL)
        assert d.verdict is ReplayVerdict.DECISION_REQUIRED
        assert d.rule is HaltRule.POLICY_MANUAL

    def test_undeclared_and_manual_agree_where_the_alternative_is_re_execution(self):
        """The other side of the same asymmetry: on rule 2 they are indistinguishable."""
        _seed_step(state=StepState.RUNNING.value, fingerprint=FP_CALL, result_json=RESULT_JSON)
        assert decide(RUN, STEP, FP_CALL, None) == decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL)


# ---------------------------------------------------------------------------
# BR-8 / INV-5 — each halting path returns its SPECIFIC member. Four assertions, not one.
# ---------------------------------------------------------------------------
class TestSpecificHaltRules:
    """One "a rule is set" check would pass with every path returning ``POLICY_MANUAL``, and
    unit 12 branches on this value — a wrong member routes a human to the wrong remedy."""

    def test_rule2_halts_with_interrupted_no_policy(self):
        _seed_step(state=StepState.RUNNING.value, fingerprint=FP_CALL, result_json=RESULT_JSON)
        assert decide(RUN, STEP, FP_CALL, None).rule is HaltRule.INTERRUPTED_NO_POLICY

    def test_rule3_halts_with_envelope_absent(self):
        _seed_step(state=StepState.COMPLETED.value, fingerprint=FP_CALL, result_json=None)
        assert decide(RUN, STEP, FP_CALL, None).rule is HaltRule.ENVELOPE_ABSENT

    def test_rule5_halts_with_provenance_unverifiable(self):
        _seed_step(state=StepState.COMPLETED.value, fingerprint=FP_LEGACY, result_json=RESULT_JSON)
        assert decide(RUN, STEP, FP_CALL, None).rule is HaltRule.PROVENANCE_UNVERIFIABLE

    def test_rule7_halts_with_policy_manual(self):
        _seed_step(state=StepState.COMPLETED.value, fingerprint=FP_CALL, result_json=RESULT_JSON)
        assert decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL).rule is HaltRule.POLICY_MANUAL

    def test_rule8_halts_with_outcome_failed(self):
        _seed_step(state=StepState.FAILED.value, fingerprint=FP_CALL, result_json=RESULT_JSON)
        assert decide(RUN, STEP, FP_CALL, None).rule is HaltRule.OUTCOME_FAILED

    def test_rule9_halts_with_envelope_lossy(self):
        _seed_step(
            state=StepState.COMPLETED.value,
            fingerprint=FP_CALL,
            result_json=serialise_envelope(LOSSY_ENVELOPE),
        )
        assert decide(RUN, STEP, FP_CALL, None).rule is HaltRule.ENVELOPE_LOSSY

    def test_the_six_members_are_all_reachable_and_all_distinct(self):
        """Together the six assertions above cover every ``HaltRule`` member exactly once. If
        a seventh member is ever added without a producing rule, this fails and says so."""
        produced = set()
        _seed_step(state=StepState.RUNNING.value, fingerprint=FP_CALL, result_json=RESULT_JSON)
        produced.add(decide(RUN, STEP, FP_CALL, None).rule)
        _seed_step(
            state=StepState.COMPLETED.value,
            fingerprint=FP_CALL,
            result_json=None,
            step_id="s2",
        )
        produced.add(decide(RUN, "s2", FP_CALL, None).rule)
        _seed_step(
            state=StepState.COMPLETED.value,
            fingerprint=FP_LEGACY,
            result_json=RESULT_JSON,
            step_id="s3",
        )
        produced.add(decide(RUN, "s3", FP_CALL, None).rule)
        _seed_step(
            state=StepState.COMPLETED.value,
            fingerprint=FP_CALL,
            result_json=RESULT_JSON,
            step_id="s4",
        )
        produced.add(decide(RUN, "s4", FP_CALL, RecoveryPolicy.MANUAL).rule)
        _seed_step(
            state=StepState.FAILED.value,
            fingerprint=FP_CALL,
            result_json=RESULT_JSON,
            step_id="s5",
        )
        produced.add(decide(RUN, "s5", FP_CALL, None).rule)
        _seed_step(
            state=StepState.COMPLETED.value,
            fingerprint=FP_CALL,
            result_json=serialise_envelope(LOSSY_ENVELOPE),
            step_id="s6",
        )
        produced.add(decide(RUN, "s6", FP_CALL, None).rule)
        assert produced == set(HaltRule)


# ---------------------------------------------------------------------------
# PR #628 review, Copilot F1 — rule 8: a FAILED outcome never replays as a success.
# ---------------------------------------------------------------------------
class TestFailedOutcomeHalts:
    """The defect: a ``failed`` row reached the catch-all and REPLAYED, so the route answered
    HTTP 200 and the script continued past a call that raised on the original drive.

    Both escapes are asserted here, not just the halt. A halt with no way out is the
    infinite-halt defect BR-2 exists to prevent, and rule 8 is the first rule added since that
    defect was found — so the escapes are the part most worth pinning.
    """

    @pytest.mark.parametrize("policy", _POLICIES)
    def test_a_failed_row_halts_under_every_declared_policy(self, policy):
        """Including ``idempotent``/``reconcile``. THIS IS THE ONE THAT LOOKS ARGUABLE, so it
        is stated: those policies say re-EXECUTION is safe, which is a claim about running the
        step again — it is not a claim that a recorded FAILURE may be served as a success, and
        rules 2 and 4 are where the gate already acts on the re-execution claim. Serving the
        failure is what the author never asked for.
        """
        _seed_step(state=StepState.FAILED.value, fingerprint=FP_CALL, result_json=RESULT_JSON)
        d = decide(RUN, STEP, FP_CALL, policy)
        assert d.verdict is ReplayVerdict.DECISION_REQUIRED
        assert d.envelope is None

    def test_rerun_escapes_it_through_rule_1(self):
        """``rerun`` -> ``rerun_authorized`` -> rule 1 -> EXECUTE, before rule 8 is reached."""
        _seed_step(state=RERUN_AUTHORIZED, fingerprint=FP_CALL, result_json=RESULT_JSON)
        d = decide(RUN, STEP, FP_CALL, None)
        assert d.verdict is ReplayVerdict.EXECUTE
        assert d.rule is None

    def test_skip_escapes_it_because_the_decision_overwrites_the_state(self):
        """``skip`` -> ``replay_authorized``, which OVERWRITES ``failed`` in the same column
        rule 8 tests — so rule 8 needs no exclusion of its own and the row replays.

        This is the mechanism the fix depends on. If ``apply_decisions`` ever recorded a
        decision beside ``state`` instead of in it, this test fails and rule 8 becomes an
        infinite halt for ``skip``.
        """
        _seed_step(state=REPLAY_AUTHORIZED, fingerprint=FP_CALL, result_json=RESULT_JSON)
        d = decide(RUN, STEP, FP_CALL, None)
        assert d.verdict is ReplayVerdict.REPLAY
        assert d.envelope == ENVELOPE

    def test_a_failed_row_with_no_envelope_still_halts_as_envelope_absent(self):
        """Rule 3 keeps precedence: rule 8 was APPENDED, so nothing before it moved. The
        distinction is not cosmetic — ``ENVELOPE_ABSENT`` means *crash between the writes* and
        is the guard FR-4 defines, so it must not be relabelled by the new rule.
        """
        _seed_step(state=StepState.FAILED.value, fingerprint=FP_CALL, result_json=None)
        d = decide(RUN, STEP, FP_CALL, None)
        assert d.rule is HaltRule.ENVELOPE_ABSENT
        assert d.rule is not HaltRule.OUTCOME_FAILED

    def test_a_failed_row_that_diverges_still_reports_divergence(self):
        """Rule 6 keeps precedence too: "the script changed at this key" is the more actionable
        fact and FR-3 requires it to be loud."""
        _seed_step(state=StepState.FAILED.value, fingerprint=FP_OTHER_V2, result_json=RESULT_JSON)
        assert decide(RUN, STEP, FP_CALL, None).verdict is ReplayVerdict.DIVERGED

    def test_completed_unvalidated_is_not_a_failure_and_still_replays(self):
        """The rule tests ``failed`` EXACTLY, never "not completed". ``completed_unvalidated``
        means the step succeeded and its structured output failed schema validation — the
        original call returned 200, so replaying it reproduces the original run.
        """
        _seed_step(
            state=StepState.COMPLETED_UNVALIDATED.value,
            fingerprint=FP_CALL,
            result_json=RESULT_JSON,
        )
        assert decide(RUN, STEP, FP_CALL, None).verdict is ReplayVerdict.REPLAY

    def test_the_failed_literal_matches_the_enum_value(self):
        """The gate spells ``'failed'`` as a bare literal (TD-1 — no sixth package import for
        one string), pinned against the enum from THIS file's own import."""
        assert StepState.FAILED.value == "failed"
        _seed_step(state="failed", fingerprint=FP_CALL, result_json=RESULT_JSON)
        assert decide(RUN, STEP, FP_CALL, None).rule is HaltRule.OUTCOME_FAILED


# ---------------------------------------------------------------------------
# PR #628 review, Copilot F5 — rule 9: an envelope that reports its own lossiness.
# ---------------------------------------------------------------------------
class TestLossyEnvelopeHalts:
    """The defect: ``build_envelope`` redacts and then bounds ``last_message``, but the original
    call answered with ``run_agent_step``'s RAW text. Replay served the abridged text as the
    step output and ``RunStepResponse`` carries no flag able to say so, so an unchanged script
    that feeds the result into its next prompt computes a different next-step fingerprint and
    FALSELY diverges — reporting a changed script when nothing changed.
    """

    @pytest.mark.parametrize(
        "truncated,redacted",
        [(True, False), (False, True), (True, True)],
        ids=["truncated", "redacted", "both"],
    )
    def test_either_flag_alone_halts(self, truncated: bool, redacted: bool):
        """Both halves of the ``or`` are exercised. One fixture with both flags set would pass
        against an implementation that tested only one of them."""
        _seed_step(
            state=StepState.COMPLETED.value,
            fingerprint=FP_CALL,
            result_json=serialise_envelope(
                StepResultEnvelope(
                    last_message="abridged",
                    status="completed",
                    terminal_id="term-1",
                    truncated=truncated,
                    redacted=redacted,
                )
            ),
        )
        d = decide(RUN, STEP, FP_CALL, None)
        assert d.verdict is ReplayVerdict.DECISION_REQUIRED
        assert d.rule is HaltRule.ENVELOPE_LOSSY
        assert d.envelope is None

    def test_a_whole_envelope_is_untouched_by_the_rule(self):
        """Both flags false — the overwhelmingly common case — still replays. Without this the
        fix could have closed the replay path entirely and every other test would still pass.
        """
        assert ENVELOPE.truncated is False
        assert ENVELOPE.redacted is False
        _seed_step(state=StepState.COMPLETED.value, fingerprint=FP_CALL, result_json=RESULT_JSON)
        assert decide(RUN, STEP, FP_CALL, None).verdict is ReplayVerdict.REPLAY

    def test_skip_escapes_it_by_explicit_exclusion(self):
        """Unlike rule 8, rule 9's condition is the ENVELOPE's and survives the state
        overwrite, so it excludes ``replay_authorized`` by hand. Without that exclusion a human
        who answered ``skip`` would be re-asked on every subsequent resume — forever.
        """
        _seed_step(
            state=REPLAY_AUTHORIZED,
            fingerprint=FP_CALL,
            result_json=serialise_envelope(LOSSY_ENVELOPE),
        )
        d = decide(RUN, STEP, FP_CALL, None)
        assert d.verdict is ReplayVerdict.REPLAY
        assert d.envelope == LOSSY_ENVELOPE

    def test_rerun_escapes_it_through_rule_1(self):
        _seed_step(
            state=RERUN_AUTHORIZED,
            fingerprint=FP_CALL,
            result_json=serialise_envelope(LOSSY_ENVELOPE),
        )
        assert decide(RUN, STEP, FP_CALL, None).verdict is ReplayVerdict.EXECUTE

    def test_a_lossy_envelope_that_diverges_still_reports_divergence(self):
        """Rule 6 precedence again — appended rules never mask an earlier one."""
        _seed_step(
            state=StepState.COMPLETED.value,
            fingerprint=FP_OTHER_V2,
            result_json=serialise_envelope(LOSSY_ENVELOPE),
        )
        assert decide(RUN, STEP, FP_CALL, None).verdict is ReplayVerdict.DIVERGED

    def test_rule_8_precedes_rule_9_on_a_row_matching_both(self):
        """A ``failed`` row with a lossy envelope satisfies both. The order is asserted rather
        than assumed, because each rule passes in isolation while the order is wrong, and unit
        12 branches on the member a human is shown.
        """
        _seed_step(
            state=StepState.FAILED.value,
            fingerprint=FP_CALL,
            result_json=serialise_envelope(LOSSY_ENVELOPE),
        )
        d = decide(RUN, STEP, FP_CALL, None)
        assert d.rule is HaltRule.OUTCOME_FAILED
        assert d.rule is not HaltRule.ENVELOPE_LOSSY

    def test_the_gate_still_imports_neither_secret_gate_nor_build_envelope(self):
        """Rule 9 reads the envelope's OWN self-report (unit 2's INV-5) rather than recomputing
        lossiness, which is what keeps SR-2 intact: a second redaction or bounding pass here
        would be an unreviewed sanitisation path, and it could match its own
        ``[REDACTED:<name>]`` marker.
        """
        imports = _package_imports()
        assert "cli_agent_orchestrator.services.secret_gate" not in imports
        assert imports["cli_agent_orchestrator.services.step_result"] == {"parse_envelope"}


# ---------------------------------------------------------------------------
# BR-6 — ``envelope`` iff REPLAY; ``rule`` iff DECISION_REQUIRED.
# ---------------------------------------------------------------------------
class TestFieldConditionality:
    """For each verdict: the field that belongs to it present, the other absent. A populated
    ``envelope`` on a non-``REPLAY`` verdict would offer a caller a result it was told not to
    use."""

    def test_execute_carries_neither_field(self):
        d = decide(RUN, STEP, FP_CALL, None)
        assert d.verdict is ReplayVerdict.EXECUTE
        assert d.envelope is None
        assert d.rule is None
        assert d.reason

    def test_replay_carries_the_envelope_and_no_rule(self):
        _seed_step(state=StepState.COMPLETED.value, fingerprint=FP_CALL, result_json=RESULT_JSON)
        d = decide(RUN, STEP, FP_CALL, None)
        assert d.verdict is ReplayVerdict.REPLAY
        assert d.envelope is not None
        assert d.rule is None

    def test_diverged_carries_neither_field(self):
        _seed_step(
            state=StepState.COMPLETED.value, fingerprint=FP_OTHER_V2, result_json=RESULT_JSON
        )
        d = decide(RUN, STEP, FP_CALL, None)
        assert d.verdict is ReplayVerdict.DIVERGED
        assert d.envelope is None
        assert d.rule is None

    def test_decision_required_carries_the_rule_and_no_envelope(self):
        """The envelope EXISTS on this row and is parseable — it is withheld because the
        verdict is not ``REPLAY``, not because there was nothing to hand back."""
        _seed_step(state=StepState.COMPLETED.value, fingerprint=FP_CALL, result_json=RESULT_JSON)
        d = decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL)
        assert d.verdict is ReplayVerdict.DECISION_REQUIRED
        assert d.rule is not None
        assert d.envelope is None
        assert parse_envelope(RESULT_JSON) is not None  # the row really did carry one


# ---------------------------------------------------------------------------
# BR-7 / TD-4 — the shape of the returned type.
# ---------------------------------------------------------------------------
class TestDecisionShape:
    def test_replay_decision_has_no_diverged_fields_attribute(self):
        """BR-7: ``compute`` returns ONE digest over ten components and only the digest is
        persisted, so nothing can populate a per-field list. An always-empty one reads as "no
        fields diverged" when the truth is "we cannot tell". Asserting ABSENCE is what stops a
        well-meaning re-addition, and stops unit 9 being written against it."""
        _seed_step(
            state=StepState.COMPLETED.value, fingerprint=FP_OTHER_V2, result_json=RESULT_JSON
        )
        d = decide(RUN, STEP, FP_CALL, None)
        assert not hasattr(d, "diverged_fields")
        assert not hasattr(ReplayDecision, "diverged_fields")
        assert "diverged_fields" not in ReplayDecision.__annotations__

    def test_replay_decision_carries_exactly_the_four_declared_fields(self):
        import dataclasses

        assert [f.name for f in dataclasses.fields(ReplayDecision)] == [
            "verdict",
            "envelope",
            "reason",
            "rule",
        ]

    def test_replay_decision_is_frozen(self):
        """TD-4: a caller able to mutate a decision could launder a ``DIVERGED`` into a
        ``REPLAY`` — the single most consequential edit available in this subsystem."""
        _seed_step(
            state=StepState.COMPLETED.value, fingerprint=FP_OTHER_V2, result_json=RESULT_JSON
        )
        d = decide(RUN, STEP, FP_CALL, None)
        with pytest.raises(Exception):
            d.verdict = ReplayVerdict.REPLAY  # type: ignore[misc]
        assert d.verdict is ReplayVerdict.DIVERGED

    def test_verdict_vocabulary_is_closed_at_four_members(self):
        """Four, not five: ``EXECUTE_VIA_RECONCILE`` was rejected because no reconciliation
        operation exists to serve it (BR-11)."""
        assert [m.value for m in ReplayVerdict] == [
            "execute",
            "replay",
            "diverged",
            "decision_required",
        ]


# ---------------------------------------------------------------------------
# SR-3 / BR-15 — the incoming-fingerprint precondition.
# ---------------------------------------------------------------------------
class TestIncomingFingerprintPrecondition:
    """Without this check a caller bug reaches rule 6, mismatches a stored ``v2`` value, and
    halts the run with ``DIVERGED`` — reporting tampering-shaped evidence about the user's
    script when the defect is in CAO. A truthfulness threat, not a confidentiality one."""

    @pytest.mark.parametrize(
        "bad",
        [
            pytest.param(FP_LEGACY, id="bare-digest"),
            pytest.param("", id="empty"),
            pytest.param("v1:" + "a" * 64, id="older-scheme-prefix"),
        ],
    )
    def test_non_v2_incoming_fingerprint_raises_value_error(self, bad: str):
        _seed_step(state=StepState.COMPLETED.value, fingerprint=FP_CALL, result_json=RESULT_JSON)
        with pytest.raises(ValueError):
            decide(RUN, STEP, bad, None)

    def test_the_message_names_the_step_and_echoes_neither_digest(self):
        """SR-1: the supplied value is arbitrary caller-supplied text under a digest
        prohibition, and the stored one is a digest too."""
        _seed_step(state=StepState.COMPLETED.value, fingerprint=FP_CALL, result_json=RESULT_JSON)
        with pytest.raises(ValueError) as excinfo:
            decide(RUN, STEP, FP_LEGACY, None)
        message = str(excinfo.value)
        assert STEP in message
        assert FP_LEGACY not in message
        assert FP_CALL not in message
        assert "a" * 64 not in message
        assert "c" * 64 not in message

    def test_it_raises_before_the_journal_is_read(self, monkeypatch: pytest.MonkeyPatch):
        """A caller bug must not cost a journal round-trip — and the spy proves the ORDER, not
        merely that a ``ValueError`` happened somewhere."""
        calls: list[tuple[str, str]] = []

        def _spy(run_id: str, step_id: str):
            calls.append((run_id, step_id))
            raise AssertionError("SR-3: the precondition must fire before the read")

        monkeypatch.setattr(step_replay, "get_step", _spy)
        with pytest.raises(ValueError):
            decide(RUN, STEP, FP_LEGACY, None)
        assert calls == []

    def test_a_non_v2_fingerprint_never_returns_diverged(self):
        """The specific false attribution SR-3 exists to prevent: the stored value is a
        ``v2`` match for nothing, so rule 6 would have fired."""
        _seed_step(
            state=StepState.COMPLETED.value, fingerprint=FP_OTHER_V2, result_json=RESULT_JSON
        )
        with pytest.raises(ValueError):
            decide(RUN, STEP, FP_LEGACY, None)


# ---------------------------------------------------------------------------
# BR-10 / INV-4 — a failing journal read propagates and produces NO verdict.
# ---------------------------------------------------------------------------
class TestReadFailure:
    """An unreadable journal degrading to ``EXECUTE`` would re-run completed work under
    exactly the conditions FR-1 exists to prevent, under the guise of a safe default."""

    @staticmethod
    def _patch_get_step_to_raise(monkeypatch: pytest.MonkeyPatch, error: Exception):
        def _boom(run_id: str, step_id: str):
            raise error

        monkeypatch.setattr(step_replay, "get_step", _boom)

    def test_sqlite_error_propagates_unchanged(self, monkeypatch: pytest.MonkeyPatch):
        error = sqlite3.OperationalError("database is locked")
        self._patch_get_step_to_raise(monkeypatch, error)
        with pytest.raises(sqlite3.Error) as excinfo:
            decide(RUN, STEP, FP_CALL, None)
        assert excinfo.value is error  # identity: not re-wrapped, not re-raised as something else

    def test_no_verdict_is_returned_when_the_read_fails(self, monkeypatch: pytest.MonkeyPatch):
        """The row is ABSENT here, so a swallowed error would have produced the most
        dangerous possible verdict — rule 1's ``EXECUTE``."""
        self._patch_get_step_to_raise(monkeypatch, sqlite3.DatabaseError("disk I/O error"))
        returned = None
        try:
            returned = decide(RUN, STEP, FP_CALL, RecoveryPolicy.IDEMPOTENT)
        except sqlite3.Error:
            pass
        assert returned is None


# ---------------------------------------------------------------------------
# BR-13 / SR-5 — one read, no writes.
# ---------------------------------------------------------------------------
_JOURNAL_WRITERS = (
    "insert_run",
    "insert_run_with_steps",
    "insert_steps",
    "update_step",
    "update_run_current_step",
    "update_run_state",
    "settle_run_state_if_running",
    "append_step",
    "begin_step",
    "settle_step",
)


class TestOneReadNoWrites:
    """NFR-2 — the gate sits on the resume hot path, so the read budget is part of the
    contract, not an efficiency preference."""

    def test_get_step_is_called_exactly_once_on_every_rule_path(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Counted per decision, over one row shape per rule, because a second read is the
        obvious way a later refactor would re-fetch the row for the divergence comparison."""
        rows = [
            (None, None, None, None),
            (RERUN_AUTHORIZED, FP_CALL, RESULT_JSON, None),
            (StepState.RUNNING.value, FP_CALL, None, RecoveryPolicy.IDEMPOTENT),
            (StepState.RUNNING.value, FP_CALL, None, None),
            (StepState.COMPLETED.value, FP_CALL, None, None),
            (StepState.COMPLETED.value, FP_LEGACY, RESULT_JSON, RecoveryPolicy.IDEMPOTENT),
            (StepState.COMPLETED.value, FP_LEGACY, RESULT_JSON, None),
            (StepState.COMPLETED.value, FP_OTHER_V2, RESULT_JSON, None),
            (StepState.COMPLETED.value, FP_CALL, RESULT_JSON, RecoveryPolicy.MANUAL),
            (StepState.COMPLETED.value, FP_CALL, RESULT_JSON, None),
        ]
        real = workflow_journal.get_step
        for index, (state, fingerprint, result_json, policy) in enumerate(rows):
            step_id = f"s{index}"
            if state is not None:
                _seed_step(
                    state=state, fingerprint=fingerprint, result_json=result_json, step_id=step_id
                )
            counter = {"n": 0}

            def _spy(run_id: str, step_id_arg: str, _real=real, _counter=counter):
                _counter["n"] += 1
                return _real(run_id, step_id_arg)

            monkeypatch.setattr(step_replay, "get_step", _spy)
            decide(RUN, step_id, FP_CALL, policy)
            assert counter["n"] == 1, f"row {index} read the journal {counter['n']} times"

    def test_no_journal_write_function_is_called(self, monkeypatch: pytest.MonkeyPatch):
        """Every write in ``workflow_journal`` is patched to raise. The gate cannot reach them
        by name (it imports only ``get_step``), and this catches the one way it could — a
        function-local import added later."""
        for name in _JOURNAL_WRITERS:

            def _forbidden(*args, _name=name, **kwargs):
                raise AssertionError(f"SR-5/BR-13: the gate must not call {_name}")

            monkeypatch.setattr(workflow_journal, name, _forbidden)

        # Seed BEFORE patching would be circular, so seed by raw SQL here.
        with _direct_connect() as conn:
            conn.execute(
                "INSERT INTO workflow_run_step (run_id, step_id, state, attempts, output_json, "
                "error, updated_at, call_fingerprint, result_json) "
                "VALUES (?, ?, ?, 1, NULL, NULL, ?, ?, ?)",
                (RUN, STEP, StepState.COMPLETED.value, TS, FP_CALL, RESULT_JSON),
            )
        assert decide(RUN, STEP, FP_CALL, None).verdict is ReplayVerdict.REPLAY

    def test_the_row_is_unchanged_after_a_decision(self):
        """The observable form of SR-5: every column reads back byte-identical."""
        _seed_step(state=StepState.COMPLETED.value, fingerprint=FP_CALL, result_json=RESULT_JSON)
        before = workflow_journal.get_step(RUN, STEP)
        decide(RUN, STEP, FP_CALL, RecoveryPolicy.MANUAL)
        assert workflow_journal.get_step(RUN, STEP) == before


# ---------------------------------------------------------------------------
# BR-14 — a corrupt envelope IS an absent envelope.
# ---------------------------------------------------------------------------
class TestCorruptEnvelope:
    """``parse_envelope`` collapses NULL, malformed JSON and valid-JSON-of-the-wrong-shape to
    ``None`` because every one of them means "this row cannot be replayed". Rule 3 is that
    rule, so no separate branch exists."""

    @pytest.mark.parametrize(
        "corrupt",
        [
            pytest.param("{not json at all", id="malformed-json"),
            pytest.param('{"unexpected":"shape"}', id="wrong-shape"),
        ],
    )
    def test_unparseable_result_json_halts_with_envelope_absent(self, corrupt: str):
        _seed_step(state=StepState.COMPLETED.value, fingerprint=FP_CALL, result_json=corrupt)
        d = decide(RUN, STEP, FP_CALL, None)
        assert d.verdict is ReplayVerdict.DECISION_REQUIRED
        assert d.rule is HaltRule.ENVELOPE_ABSENT

    def test_a_corrupt_envelope_decides_identically_to_a_null_one(self):
        """Identical DECISIONS, not merely identical verdicts — so a later "diagnose the
        corruption in ``reason``" edit would have to be a deliberate one."""
        _seed_step(
            state=StepState.COMPLETED.value,
            fingerprint=FP_CALL,
            result_json="{not json at all",
            step_id="corrupt",
        )
        _seed_step(
            state=StepState.COMPLETED.value,
            fingerprint=FP_CALL,
            result_json=None,
            step_id="corrupt",
            run_id="run-2",
        )
        corrupt = decide(RUN, "corrupt", FP_CALL, None)
        absent = decide("run-2", "corrupt", FP_CALL, None)
        assert corrupt.verdict == absent.verdict
        assert corrupt.rule == absent.rule
        assert corrupt.envelope == absent.envelope
        # Only the identifiers differ, and they are the two the reason is allowed to carry.
        assert corrupt.reason.replace(RUN, "run-2") == absent.reason


# ---------------------------------------------------------------------------
# BR-11 — ``reconcile`` is ``idempotent`` today, and the deferral is stated.
# ---------------------------------------------------------------------------
class TestReconcileEquivalence:
    """No reconciliation operation exists anywhere in ``src/`` and #583's frozen scope defers
    it, so a branch nothing can serve is not invented. When the operation ships, these are the
    tests that fail first."""

    def test_rule2_treats_reconcile_and_idempotent_identically(self):
        _seed_step(state=StepState.RUNNING.value, fingerprint=FP_CALL, result_json=None)
        assert decide(RUN, STEP, FP_CALL, RecoveryPolicy.RECONCILE) == decide(
            RUN, STEP, FP_CALL, RecoveryPolicy.IDEMPOTENT
        )

    def test_rule4_treats_reconcile_and_idempotent_identically(self):
        _seed_step(state=StepState.COMPLETED.value, fingerprint=FP_LEGACY, result_json=RESULT_JSON)
        assert decide(RUN, STEP, FP_CALL, RecoveryPolicy.RECONCILE) == decide(
            RUN, STEP, FP_CALL, RecoveryPolicy.IDEMPOTENT
        )

    def test_the_module_contains_no_reconciliation_branch(self):
        """``RECONCILE`` is named EXACTLY ONCE in the source — inside the one shared
        membership tuple. A second reference would be a branch, which is what BR-11 forbids
        until the operation exists."""
        references = [
            node
            for node in ast.walk(_module_ast())
            if isinstance(node, ast.Attribute) and node.attr == "RECONCILE"
        ]
        assert len(references) == 1, f"{len(references)} references to RECONCILE"

    def test_the_deferral_is_stated_in_the_module_docstring(self):
        """BR-11's other half. A silent equivalence is what a later reader would "tidy up"."""
        doc = step_replay.__doc__ or ""
        assert "reconcil" in doc.lower()
        assert "defer" in doc.lower()

    def test_the_module_docstring_states_that_the_order_is_the_contract(self):
        """BR-1's other half: the order is the thing most likely to be rearranged for
        readability, so the reason it may not be lives with the code.

        The rule COUNT moves when a rule is appended (PR #628's review took it from eight to
        ten), so the phrase is matched around the count rather than including it — the claim
        under test is "the order is the contract", not the arithmetic.
        """
        doc = step_replay.__doc__ or ""
        assert "RULES IS THE CONTRACT" in doc
        assert "ORDER OF THE" in doc


# ---------------------------------------------------------------------------
# SR-1 — ``reason`` carries the identifiers and NEITHER digest.
# ---------------------------------------------------------------------------
_ALL_ROWS = [
    pytest.param(None, None, None, None, id="rule1-absent"),
    pytest.param(RERUN_AUTHORIZED, FP_CALL, RESULT_JSON, None, id="rule1-rerun"),
    pytest.param(
        StepState.RUNNING.value, FP_CALL, None, RecoveryPolicy.IDEMPOTENT, id="rule2-execute"
    ),
    pytest.param(StepState.RUNNING.value, FP_CALL, None, None, id="rule2-halt"),
    pytest.param(StepState.COMPLETED.value, FP_CALL, None, None, id="rule3"),
    pytest.param(
        StepState.COMPLETED.value,
        FP_LEGACY,
        RESULT_JSON,
        RecoveryPolicy.IDEMPOTENT,
        id="rule4",
    ),
    pytest.param(StepState.COMPLETED.value, FP_LEGACY, RESULT_JSON, None, id="rule5"),
    pytest.param(StepState.COMPLETED.value, FP_OTHER_V2, RESULT_JSON, None, id="rule6"),
    pytest.param(
        StepState.COMPLETED.value, FP_CALL, RESULT_JSON, RecoveryPolicy.MANUAL, id="rule7"
    ),
    pytest.param(StepState.COMPLETED.value, FP_CALL, RESULT_JSON, None, id="catch-all"),
    pytest.param(
        REPLAY_AUTHORIZED, FP_CALL, RESULT_JSON, RecoveryPolicy.MANUAL, id="rule7-excluded"
    ),
]


class TestReasonContent:
    """The gate's entire security surface is what it SAYS and what it hands back. It never
    holds a prompt, a path or a model id — it holds two digests and one row — so the one thing
    it could leak is a digest, which ``step-fingerprint``'s SR-2 forbids anywhere at all."""

    @pytest.mark.parametrize("state,fingerprint,result_json,policy", _ALL_ROWS)
    def test_every_reason_names_the_run_and_the_step(
        self,
        state: Optional[str],
        fingerprint: Optional[str],
        result_json: Optional[str],
        policy: Optional[RecoveryPolicy],
    ):
        if state is not None:
            _seed_step(state=state, fingerprint=fingerprint, result_json=result_json)
        d = decide(RUN, STEP, FP_CALL, policy)
        assert RUN in d.reason
        assert STEP in d.reason
        assert d.reason.strip()

    @pytest.mark.parametrize("state,fingerprint,result_json,policy", _ALL_ROWS)
    def test_no_reason_echoes_either_digest(
        self,
        state: Optional[str],
        fingerprint: Optional[str],
        result_json: Optional[str],
        policy: Optional[RecoveryPolicy],
    ):
        if state is not None:
            _seed_step(state=state, fingerprint=fingerprint, result_json=result_json)
        d = decide(RUN, STEP, FP_CALL, policy)
        for forbidden in (FP_CALL, FP_OTHER_V2, FP_LEGACY, "a" * 64, "b" * 64, "c" * 64):
            assert forbidden not in d.reason

    def test_no_reason_echoes_the_stored_message(self):
        """The envelope's ``last_message`` is agent-produced text. It is HANDED BACK on
        ``REPLAY`` (unit 2 already made it safe to store) but never narrated."""
        _seed_step(state=StepState.COMPLETED.value, fingerprint=FP_CALL, result_json=RESULT_JSON)
        d = decide(RUN, STEP, FP_CALL, None)
        assert ENVELOPE.last_message not in d.reason


# ---------------------------------------------------------------------------
# SR-2 — the REPLAY envelope is returned unaltered.
# ---------------------------------------------------------------------------
class TestEnvelopePassthrough:
    def test_the_returned_envelope_is_field_for_field_what_parse_envelope_produced(self):
        """The gate re-sanitises nothing and adds nothing: the value was redacted and then
        bounded by ``build_envelope`` before it reached SQLite (unit 2's SR-1/BR-2), and a
        caller receives exactly what was persisted."""
        _seed_step(state=StepState.COMPLETED.value, fingerprint=FP_CALL, result_json=RESULT_JSON)
        d = decide(RUN, STEP, FP_CALL, None)
        expected = parse_envelope(RESULT_JSON)
        assert expected is not None
        assert d.envelope is not None
        assert d.envelope.model_dump() == expected.model_dump()

    def test_a_truncated_and_redacted_envelope_keeps_its_self_reported_flags(self):
        """Evidence that hides its own lossiness is worse than none (unit 2's BR-4), so the
        gate must not normalise the flags away.

        SEEDED ``replay_authorized`` SINCE PR #628's REVIEW, and the change is the point rather
        than a workaround: rule 9 now HALTS a lossy envelope, so the only path on which one is
        still served is the one where a human answered ``skip`` — and that is exactly the path
        where normalising the flags away would matter, because it is the only path a caller
        ever receives such an envelope on. The previous seed (``failed``) would now halt at
        rule 8 before rule 9 was even reached, which would leave this test asserting nothing
        about the flags.
        """
        _seed_step(
            state=REPLAY_AUTHORIZED,
            fingerprint=FP_CALL,
            result_json=serialise_envelope(LOSSY_ENVELOPE),
        )
        d = decide(RUN, STEP, FP_CALL, None)
        assert d.verdict is ReplayVerdict.REPLAY
        assert d.envelope == LOSSY_ENVELOPE
        assert d.envelope is not None
        assert d.envelope.truncated is True
        assert d.envelope.redacted is True


# ---------------------------------------------------------------------------
# SR-6 — no SQL is issued here, so injection is not reachable.
# ---------------------------------------------------------------------------
class TestNoSql:
    def test_a_step_id_full_of_sql_metacharacters_round_trips_as_data(self):
        """Inherited coverage (``get_step`` is parameterised, unit 6's SR-6), asserted once
        here to keep the claim honest rather than assumed."""
        nasty = "s1'; DROP TABLE workflow_run_step; --"
        _seed_step(
            state=StepState.COMPLETED.value,
            fingerprint=FP_CALL,
            result_json=RESULT_JSON,
            step_id=nasty,
        )
        d = decide(RUN, nasty, FP_CALL, None)
        assert d.verdict is ReplayVerdict.REPLAY
        assert nasty in d.reason
        with _direct_connect() as conn:
            surviving = conn.execute("SELECT COUNT(*) FROM workflow_run_step").fetchone()[0]
        assert surviving == 1


# ---------------------------------------------------------------------------
# BR-9 / SR-4 / SR-5 / TD-1 — the module's posture, by AST walk.
# ---------------------------------------------------------------------------
class TestPosture:
    """AST, never a grep. This module's docstrings name both exception types, ``secret_gate``,
    ``build_envelope`` and logging at length, so a text search would match its own prose and
    pass on a module that really did import or call them."""

    def test_the_module_raises_nothing_but_value_error(self):
        """BR-9: ``DIVERGED`` and ``DECISION_REQUIRED`` are RETURN VALUES. Unit 9 owns the
        raise and the HTTP mapping in one place; a raise here would move the mapping into two
        places and make the gate untestable without exception plumbing."""
        raised: set[str] = set()
        for node in ast.walk(_module_ast()):
            if not isinstance(node, ast.Raise) or node.exc is None:
                continue
            exc = node.exc
            if isinstance(exc, ast.Call):
                exc = exc.func
            if isinstance(exc, ast.Name):
                raised.add(exc.id)
            elif isinstance(exc, ast.Attribute):
                raised.add(exc.attr)
        assert raised == {"ValueError"}, raised

    def test_the_module_names_neither_replay_exception_type(self):
        """TD-1: an unused import of ``ReplayDivergenceError`` would be an invitation."""
        names = {node.id for node in ast.walk(_module_ast()) if isinstance(node, ast.Name)} | {
            node.attr for node in ast.walk(_module_ast()) if isinstance(node, ast.Attribute)
        }
        assert "ReplayDivergenceError" not in names
        assert "RecoveryDecisionRequired" not in names

    def test_the_module_has_no_logger_and_no_print(self):
        """SR-4: a persistence or decision primitive should not own its own observability.
        The caller logs, holding ``verdict``, ``rule`` and ``reason``."""
        forbidden_methods = {
            "debug",
            "info",
            "warning",
            "warn",
            "error",
            "exception",
            "critical",
            "log",
        }
        tree = _module_ast()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id not in {"logger", "logging", "print"}, node.id
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in forbidden_methods, node.func.attr

    def test_the_module_imports_exactly_the_five_declared_dependency_edges(self):
        """TD-1: five imports, five dependency edges. The correspondence is the check that the
        implementation added no hidden dependency, and it is asserted rather than eyeballed."""
        assert set(_package_imports()) == {
            "cli_agent_orchestrator.models.workflow",
            "cli_agent_orchestrator.services.step_fingerprint",
            "cli_agent_orchestrator.services.step_result",
            "cli_agent_orchestrator.services.workflow_errors",
            "cli_agent_orchestrator.services.workflow_journal",
        }

    def test_it_takes_only_halt_rule_from_workflow_errors(self):
        imports = _package_imports()
        assert imports["cli_agent_orchestrator.services.workflow_errors"] == {"HaltRule"}

    def test_it_imports_neither_secret_gate_nor_build_envelope(self):
        """SR-2: the envelope was already made safe by unit 2 — redacted, then bounded, in
        that order. Re-sanitising here would be a second, unreviewed redaction path."""
        imports = _package_imports()
        assert "cli_agent_orchestrator.services.secret_gate" not in imports
        assert imports["cli_agent_orchestrator.services.step_result"] == {"parse_envelope"}
        assert "build_envelope" not in imports["cli_agent_orchestrator.services.step_result"]

    def test_decide_takes_no_connection_path_or_writer_parameter(self):
        """SR-5: no persistence handle can be smuggled in."""
        assert list(inspect.signature(decide).parameters) == [
            "run_id",
            "step_id",
            "fingerprint",
            "declared_policy",
        ]


# ---------------------------------------------------------------------------
# BR-12 — totality. TOTALITY ONLY; the specific verdicts live in the explicit table above.
# ---------------------------------------------------------------------------
_SWEEP_STATES = [
    None,  # no row at all
    RERUN_AUTHORIZED,
    REPLAY_AUTHORIZED,
    StepState.RUNNING.value,
    StepState.COMPLETED.value,
    StepState.COMPLETED_UNVALIDATED.value,
    StepState.FAILED.value,
    "a_state_no_unit_has_written_yet",
]
_SWEEP_SCHEMES = ["v2", "legacy", "absent"]


def _stored_fingerprint(scheme: str, matches: bool) -> Optional[str]:
    if scheme == "absent":
        return None
    if scheme == "legacy":
        # ``matches`` still varies the value: the same digest with the prefix stripped is the
        # case a digest-only comparison would wrongly replay.
        return FP_LEGACY_SAME_DIGEST if matches else FP_LEGACY
    return FP_CALL if matches else FP_OTHER_V2


class TestTotality:
    """Every combination of ``state`` x ``scheme`` x ``policy`` x fingerprint-match returns a
    ``ReplayDecision`` — the cheapest honest proof that no path falls through without a
    verdict, with rule 10 as the catch-all.

    The sweep deliberately includes row shapes production does not produce (a ``running`` row
    carrying an envelope, an unknown future state), because totality is a property of the
    FUNCTION and not of whoever wrote the row.

    It asserts TOTALITY and the two universally-quantified BR-6 field invariants, and nothing
    else: a sweep that also claimed specific verdicts would hide which rule a failure belongs
    to, which is exactly why the per-rule table above stays explicit.
    """

    @pytest.mark.parametrize("state", _SWEEP_STATES)
    @pytest.mark.parametrize("scheme", _SWEEP_SCHEMES)
    @pytest.mark.parametrize("policy", _POLICIES)
    @pytest.mark.parametrize("matches", [True, False])
    def test_every_input_combination_returns_a_decision(
        self,
        state: Optional[str],
        scheme: str,
        policy: Optional[RecoveryPolicy],
        matches: bool,
    ):
        if state is not None:
            _seed_step(
                state=state,
                fingerprint=_stored_fingerprint(scheme, matches),
                result_json=RESULT_JSON,
            )
        d = decide(RUN, STEP, FP_CALL, policy)
        assert isinstance(d, ReplayDecision)
        assert isinstance(d.verdict, ReplayVerdict)
        assert isinstance(d.reason, str) and d.reason
        # BR-6, universally quantified — the same shape of claim as totality itself.
        assert (d.envelope is not None) is (d.verdict is ReplayVerdict.REPLAY)
        assert (d.rule is not None) is (d.verdict is ReplayVerdict.DECISION_REQUIRED)

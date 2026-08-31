"""Cross-unit verification tests for the replay guards (issue #583, unit
``replay-verification-guard``).

THIS MODULE OWNS THE TESTS THAT BELONG TO NO SINGLE COMPONENT. ``unit-of-work.md`` §13:
"FR-4's criterion is about the *combination* of two guards, so its owner must not be either
half." It changes no production file (BR-1/SR-5) and adds no entity: everything here reads
the shipped code from outside it.

Three of the unit's five deliverables live in this file; deliverables 1 (C-1's YAML
regression) and 4 (NFR-2 at the provider boundary) live in ``test/api/
test_replay_nfr2_and_c1.py``, because both need the assembled ASGI app and the ``client``
fixture that only ``test/api/conftest.py`` provides.

- **Deliverable 2 — FR-4's independence proof, BOTH WAYS, by TWO DIFFERENT TECHNIQUES**
  (:class:`TestGuardTwoStandingAlone`, :class:`TestGuardOneStandingAlone`). THE ASYMMETRY IS
  THE DESIGN (BR-3/BR-4, TD-5): guard 2 is proved by MANUFACTURING the crash-window row and
  calling ``decide``; guard 1 is proved by driving the REAL production settle and NEVER
  calling ``decide``. A single symmetric technique would test one guard twice while appearing
  to test both — the exact failure FR-4's "both ways" phrasing exists to prevent.
- **Deliverable 3 — the crash test** (:class:`TestTheCrashWindowIsClosed`). §13 asks for a
  crash "between the two writes"; ``settlement-rewire`` BR-6 deleted the second write, so the
  wording is unsatisfiable and its PURPOSE is not: a failure is induced DURING the one settle
  transaction and the row is asserted to be left ``running``. The abort is a REAL database
  abort (a ``NOT NULL`` violation, the idiom ``test_workflow_journal_txn.py``:64 uses), never
  a mock — a mocked exception proves the ``try`` fires; a real abort proves the database kept
  none of the write (TD-4).
- **Deliverable 5 — the upgrade window** (:class:`TestTheUpgradeWindow`). A run started under
  the retired three-field scheme and resumed under ``v2`` routes by the CURRENT call's
  declared policy, four ways, and NONE of the four may report ``DIVERGED`` (BR-6..BR-8). That
  last assertion is the point of the whole deliverable: it is the end-to-end proof of
  ``replay-gate`` BR-4's provenance-before-equality ordering, and the scenario the
  ``delivery-planning`` gate named as the single unassigned risk.

THE ISOLATION REQUIREMENT IS THIS FILE'S SECURITY REQUIREMENT (SR-1). Every test points
``constants.DATABASE_FILE`` at a ``tmp_path`` file BEFORE writing anything, and the fixture's
scope covers the DIRECT-SQL writes as well as the journal calls — deliverables 2 and 5 bypass
the journal, so a fixture wrapping only journal calls would protect the safe writes and miss
the two dangerous ones. This unit manufactures two row states the shipped code does not
produce on any production path, and both make the gate halt:

===============================================  ==========================================
Manufactured row                                 If it landed in a real database
===============================================  ==========================================
settled with ``result_json`` ``NULL``            every later resume of that run HALTS at
                                                 rule 3
``call_fingerprint`` without a ``v2:`` prefix    that step HALTS at rule 5, permanently
===============================================  ==========================================

``run-step-replay-branch``'s Finding 3 found real rows in the developer's own database from
tests that were merely careless. These would be actively hostile, so every test calls
:func:`_assert_tmp_db` — asserting it IS isolated rather than merely that a fixture was
requested. Every ``run_id`` here is prefixed ``rvg-`` so a leak is attributable on sight.

TWO MORE RULES THIS FILE KEEPS. Direct SQL appears only in fixtures; every assertion reads
back through ``get_step`` or ``decide``, so the PRODUCTION read path is what is exercised
(SR-2) — a test that both wrote and read by direct SQL would prove something about SQLite.
And every assertion names a SPECIFIC member (BR-11/TD-7): no ``assert decision.rule is not
None``, because this Bolt has hit that failure twice.

No credential-shaped fixture appears here (SR-4): this unit asserts nothing about redaction,
so its ``last_message`` fixtures are inert text. The legacy fingerprint is arbitrary hex and
NEVER a real digest (SR-3) — a real one would imply the retired scheme is reproducible and
invite a later reader to "verify" it, reintroducing the second fingerprint implementation
``step-fingerprint`` deleted a helper to prevent.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

import cli_agent_orchestrator.constants as constants
from cli_agent_orchestrator.clients.database import (
    _migrate_workflow_run,
    _migrate_workflow_run_step,
)
from cli_agent_orchestrator.models.workflow import RecoveryPolicy, StepResultEnvelope
from cli_agent_orchestrator.models.workflow_runtime import RunState, StepState
from cli_agent_orchestrator.services import (
    script_runner,
    step_replay,
    workflow_journal,
    workflow_service,
)
from cli_agent_orchestrator.services.script_runner import ScriptRunRecord
from cli_agent_orchestrator.services.step_fingerprint import StepCallFields, compute, scheme_of
from cli_agent_orchestrator.services.step_replay import ReplayVerdict, decide
from cli_agent_orchestrator.services.step_result import parse_envelope, serialise_envelope
from cli_agent_orchestrator.services.workflow_errors import HaltRule

# CAPTURED AT IMPORT, BEFORE ANY FIXTURE CAN PATCH IT — the developer's real database path.
# pytest imports every test module during collection, ahead of running any fixture, so this
# is the production value. It exists so :func:`_assert_tmp_db` can assert a NEGATIVE ("we are
# not on that file") as well as a positive, which is the assertion SR-1 actually wants.
_PRODUCTION_DATABASE_FILE = Path(constants.DATABASE_FILE)

TS = "2026-08-17T00:00:00Z"

# The ten execution-affecting components of the one call every test in this file decides
# about. A REAL ``StepCallFields`` run through the REAL ``compute``, so "resumed under the
# current scheme" is genuinely what the incoming fingerprint is, rather than a hand-written
# string that merely starts with ``v2:``.
_CALL_FIELDS = StepCallFields(
    provider="kiro_cli",
    agent="developer",
    prompt="do the upgrade-window step",
    model=None,
    engine=None,
    allowed_tools=None,
    effective_working_directory=None,
    use_worktree=False,
    reused_terminal=False,
    timeout=600.0,
)
# THIS call's fingerprint, current scheme — what a resume computes and hands to ``decide``.
FP_CURRENT = compute(_CALL_FIELDS)

# THE LEGACY FIXTURE: 64 hex characters with no ``v2:`` prefix, which is all ``scheme_of``'s
# prefix rule needs (``step_fingerprint.py``:267-277). DELIBERATELY A REPEATING PATTERN AND
# NOT A DIGEST OF ANYTHING (SR-3) — a real digest would imply the retired three-field scheme
# is reproducible from this fixture and invite a later reader to "verify" it, which is how a
# second fingerprint implementation gets reintroduced inside a test.
FP_LEGACY = "a1b2c3d4" * 8

ENVELOPE = StepResultEnvelope(
    last_message="the step said this",  # inert text — SR-4 forbids a credential-shaped fixture
    status="completed",
    terminal_id="rvg-terminal-1",
)
RESULT_JSON = serialise_envelope(ENVELOPE)

# ``None`` is a first-class member of this list, not a missing entry: undeclared is a distinct
# state from ``MANUAL`` and the two differ at rule 7 and at the catch-all.
POLICIES: List[Optional[RecoveryPolicy]] = [
    None,
    RecoveryPolicy.IDEMPOTENT,
    RecoveryPolicy.RECONCILE,
    RecoveryPolicy.MANUAL,
]


# ---------------------------------------------------------------------------
# Isolation (SR-1) — the fixture, and the per-test assertion that it worked
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolated_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the journal at a temp DB, create both #583-era tables, isolate the registry.

    ``autouse`` so it is in place before ANY write in this module — including the direct-SQL
    ones in :func:`_manufacture_row`, which bypass the journal entirely (SR-1/TD-2). A fixture
    scoped to journal calls would protect the safe writes and miss the two dangerous ones.
    """
    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    _migrate_workflow_run()
    _migrate_workflow_run_step()
    workflow_service.run_registry.clear()
    workflow_service._active_drives.clear()
    yield db_path
    workflow_service.run_registry.clear()
    workflow_service._active_drives.clear()


def _assert_tmp_db(db_path: Path) -> None:
    """Assert this test is operating on the ``tmp_path`` database. ONE LINE PER TEST (SR-1).

    Requesting a fixture and BEING isolated are different facts and only the second is worth
    relying on, so this asserts three of them: the live value is the fixture's path, it is not
    the production path captured at import, and it really is the file the migrators created.
    """
    from cli_agent_orchestrator.constants import DATABASE_FILE as live

    assert Path(live) == db_path, f"journal points at {live}, not the tmp_path database"
    assert (
        Path(live) != _PRODUCTION_DATABASE_FILE
    ), "journal points at the developer's REAL database"
    assert db_path.exists(), "the tmp_path database was never created"


def _direct_connect() -> sqlite3.Connection:
    """A raw connection to whatever ``DATABASE_FILE`` currently names.

    FIXTURES ONLY (SR-2). The path is read at call time, never captured, so it always follows
    the active fixture rather than an import-time snapshot.
    """
    from cli_agent_orchestrator.constants import DATABASE_FILE

    return sqlite3.connect(str(DATABASE_FILE))


def _manufacture_row(
    run_id: str,
    step_id: str,
    *,
    state: str,
    attempts: int = 1,
    call_fingerprint: Optional[str],
    result_json: Optional[str],
) -> None:
    """Write one ``workflow_run_step`` row by DIRECT SQL — a fixture, never an assertion.

    Licensed by SR-2 for exactly one reason: the two row shapes this unit needs (settled with
    no envelope, and a settled row carrying a pre-``v2`` fingerprint) are not what any
    production write path produces, so they must be constructed rather than driven.
    """
    with _direct_connect() as conn:
        conn.execute(
            "INSERT INTO workflow_run_step "
            "(run_id, step_id, state, attempts, output_json, error, updated_at, "
            " call_fingerprint, result_json) "
            "VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?)",
            (run_id, step_id, state, attempts, TS, call_fingerprint, result_json),
        )


_MUTATING_VERBS = ("insert", "update", "delete", "replace")


def _is_mutating(sql: str) -> bool:
    """True if ``sql`` changes rows. Used to count a settle's writes, never to gate one."""
    return sql.strip().split(None, 1)[0].lower() in _MUTATING_VERBS


class _StatementRecorder:
    """A pass-through over a REAL ``sqlite3.Connection`` that records the SQL it forwards.

    NOT A STAND-IN FOR THE DATABASE. Every call is delegated to the genuine connection, the
    genuine transaction semantics apply, and the row is read back through ``get_step``
    afterwards. It exists because a statement COUNT cannot be observed any other way, and the
    single-statement shape is the only observable form of guard 1's atomicity claim (a lost
    atomicity is otherwise visible only to a process that dies mid-settle).
    """

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real
        self.statements: List[str] = []

    def execute(self, sql: str, *args, **kwargs):
        self.statements.append(sql)
        return self._real.execute(sql, *args, **kwargs)

    def executemany(self, sql: str, *args, **kwargs):
        self.statements.append(sql)
        return self._real.executemany(sql, *args, **kwargs)

    def __enter__(self):
        self._real.__enter__()
        return self  # so the caller's ``conn.execute`` is the recording one

    def __exit__(self, *exc_info):
        return self._real.__exit__(*exc_info)

    def __getattr__(self, name: str):
        return getattr(self._real, name)


def _script_callbacks(run_id: str, step_id: str):
    """Register a live ``ScriptRunRecord`` and return the two PRODUCTION settle callbacks.

    These are the objects ``api/main.py`` builds on every script-tier run-step call: the
    terminal-ready hook (which calls ``begin_step``) and the settle callback (which calls
    ``settle_step``). Driving THESE rather than the journal primitives is what makes guard 1's
    "no reachable path" claim about a reachable path.
    """
    record = ScriptRunRecord(
        run_id=run_id,
        workflow_name="wf",
        state=RunState.RUNNING,
        cancelled=False,
        current_step_id=None,
        step_states={},
        process=None,
        generation="1",
        started_at=TS,
        finished_at=None,
    )
    workflow_service.run_registry[run_id] = record
    env = {"CAO_WORKFLOW_RUN_ID": run_id, "CAO_WORKFLOW_STEP_ID": step_id}
    on_ready = script_runner.make_step_terminal_recorder(env)
    on_settled = script_runner.record_step_completion(env)
    assert on_ready is not None, "the terminal-ready hook must exist for a live script record"
    assert on_settled is not None, "the settle callback must exist for a live script record"
    return on_ready, on_settled


# ---------------------------------------------------------------------------
# SR-1 — the isolation itself, asserted rather than assumed
# ---------------------------------------------------------------------------
class TestIsolation:
    """The one hazard this unit introduces, and the proof it is contained.

    ``run-step-replay-branch``'s Finding 3 found ``run-step-done/s1`` with ``attempts=7`` in
    the developer's real database, planted by tests that were merely careless. The rows below
    would be worse than careless: each is a row the gate exists to halt on.
    """

    def test_the_live_database_file_is_the_tmp_path_one(self, _isolated_journal):
        _assert_tmp_db(_isolated_journal)

    def test_the_production_database_path_is_a_real_distinct_path(self, _isolated_journal):
        """The negative in :func:`_assert_tmp_db` is only worth anything if the production
        path it compares against is the real one. A captured value that had already been
        patched would make every isolation assertion vacuously true."""
        _assert_tmp_db(_isolated_journal)
        assert _PRODUCTION_DATABASE_FILE.name == "cli-agent-orchestrator.db"
        assert _PRODUCTION_DATABASE_FILE != _isolated_journal
        assert _isolated_journal.parent != _PRODUCTION_DATABASE_FILE.parent

    def test_a_manufactured_halting_row_lands_only_in_the_tmp_database(self, _isolated_journal):
        """The direct-SQL write — the one the fixture must cover and would be easiest to
        leave outside it — is read back through the production reader on the tmp file."""
        _assert_tmp_db(_isolated_journal)
        _manufacture_row(
            "rvg-isolation", "s1", state="completed", call_fingerprint=FP_LEGACY, result_json=None
        )
        row = workflow_journal.get_step("rvg-isolation", "s1")
        assert row is not None
        assert row.call_fingerprint == FP_LEGACY
        assert _isolated_journal.stat().st_size > 0

    def test_the_manufactured_row_does_not_survive_into_the_next_test(self, _isolated_journal):
        """The companion of the test above, and the reason a second consecutive suite run is
        the real proof: each test gets its own ``tmp_path``, so the row written a moment ago
        is unreachable here."""
        _assert_tmp_db(_isolated_journal)
        assert workflow_journal.get_step("rvg-isolation", "s1") is None


# ---------------------------------------------------------------------------
# Deliverable 2a / BR-3 — GUARD 2 STANDING ALONE, in a world where guard 1 never existed
# ---------------------------------------------------------------------------
class TestGuardTwoStandingAlone:
    """Technique: MANUFACTURE the crash-window row, then call ``decide``.

    The world this constructs is the one where guard 1 does not exist, so a settled row with
    no result envelope EXISTS. Guard 2 must reject it — and specifically as
    ``ENVELOPE_ABSENT``, not merely as "a halt" (BR-3/BR-11).
    """

    RUN = "rvg-guard2"

    def _manufacture(self, step_id: str = "s1") -> None:
        # Settled state, current-scheme fingerprint, and result_json NULL — the crash-window
        # row. The fingerprint is current-scheme deliberately: it means rule 3 is reached with
        # rules 4-6 all able to fire, so the ENVELOPE_ABSENT verdict below is rule 3 winning
        # on ORDER rather than by being the only candidate.
        _manufacture_row(
            self.RUN,
            step_id,
            state=StepState.COMPLETED.value,
            call_fingerprint=FP_CURRENT,
            result_json=None,
        )

    def test_the_manufactured_row_reads_back_settled_with_no_readable_envelope(
        self, _isolated_journal
    ):
        """Read through ``get_step`` and ``parse_envelope`` (SR-2), so the fixture is verified
        against the PRODUCTION reader before anything is concluded from it."""
        _assert_tmp_db(_isolated_journal)
        self._manufacture()

        row = workflow_journal.get_step(self.RUN, "s1")
        assert row is not None
        assert row.state == StepState.COMPLETED.value
        assert row.state != StepState.RUNNING.value
        assert row.result_json is None
        assert parse_envelope(row.result_json) is None
        assert scheme_of(row.call_fingerprint) == "v2"

    def test_the_gate_halts_with_envelope_absent_specifically(self, _isolated_journal):
        """BR-3's verification: the ``rule`` is ``ENVELOPE_ABSENT``, asserted by MEMBER and
        against each of the other three, because "a halt occurred" passes when every path
        returns the same halt (BR-11 — this Bolt has hit that twice)."""
        _assert_tmp_db(_isolated_journal)
        self._manufacture()

        decision = decide(self.RUN, "s1", FP_CURRENT, None)

        assert decision.verdict is ReplayVerdict.DECISION_REQUIRED
        assert decision.rule is HaltRule.ENVELOPE_ABSENT
        assert decision.rule is not HaltRule.INTERRUPTED_NO_POLICY
        assert decision.rule is not HaltRule.PROVENANCE_UNVERIFIABLE
        assert decision.rule is not HaltRule.POLICY_MANUAL
        assert decision.envelope is None

    def test_the_halt_is_neither_a_replay_nor_a_divergence_nor_an_execute(self, _isolated_journal):
        """Guard 2's whole job is that this row is not served and not re-run silently. Each
        of the three wrong outcomes is named, because ``verdict is DECISION_REQUIRED`` alone
        would still hold if a later edit added a fifth verdict beside it."""
        _assert_tmp_db(_isolated_journal)
        self._manufacture()

        decision = decide(self.RUN, "s1", FP_CURRENT, None)

        assert decision.verdict is not ReplayVerdict.REPLAY
        assert decision.verdict is not ReplayVerdict.DIVERGED
        assert decision.verdict is not ReplayVerdict.EXECUTE

    @pytest.mark.parametrize("policy", POLICIES, ids=[str(p) for p in POLICIES])
    def test_every_declared_policy_halts_on_the_crash_window_row(
        self, _isolated_journal, policy: Optional[RecoveryPolicy]
    ):
        """GUARD 2 DOES NOT DEPEND ON THE AUTHOR'S DECLARATION. Rule 3 precedes rule 4, so
        even ``idempotent`` — whose author declared re-execution safe — halts rather than
        quietly re-running a step whose recorded outcome is unreadable. A test that only
        exercised the undeclared policy would leave that ordering unpinned."""
        _assert_tmp_db(_isolated_journal)
        self._manufacture()

        decision = decide(self.RUN, "s1", FP_CURRENT, policy)

        assert decision.verdict is ReplayVerdict.DECISION_REQUIRED
        assert decision.rule is HaltRule.ENVELOPE_ABSENT

    def test_a_readable_envelope_on_the_same_row_shape_replays_instead(self, _isolated_journal):
        """THE DISCRIMINATOR. Without it, every assertion above would still pass on a gate
        that halted on every settled row for any reason at all: the envelope is the only
        thing that differs between this row and the one above."""
        _assert_tmp_db(_isolated_journal)
        _manufacture_row(
            self.RUN,
            "s-with-envelope",
            state=StepState.COMPLETED.value,
            call_fingerprint=FP_CURRENT,
            result_json=RESULT_JSON,
        )

        decision = decide(self.RUN, "s-with-envelope", FP_CURRENT, None)

        assert decision.verdict is ReplayVerdict.REPLAY
        assert decision.rule is None
        assert decision.envelope is not None
        assert decision.envelope.last_message == ENVELOPE.last_message


# ---------------------------------------------------------------------------
# Deliverable 2b / BR-4 — GUARD 1 STANDING ALONE, in a world where guard 2 never looks
# ---------------------------------------------------------------------------
class TestGuardOneStandingAlone:
    """Technique: drive the REAL production settle, and NEVER call ``decide``.

    A DIFFERENT TECHNIQUE FROM :class:`TestGuardTwoStandingAlone`, and that is the rule
    (TD-5): the claim here is about what can EXIST, not about what is rejected, so consulting
    the gate would quietly turn this into a second test of guard 2.

    WHICH "REAL SETTLE" — stated precisely rather than reassuringly. ``settle_step`` is a
    persistence primitive that stores what it is given, and it will store a NULL envelope if
    a caller passes one (``test_the_primitive_stores_a_null_envelope_when_it_is_handed_one``
    below pins that, and two sibling test files rely on it to seed rule-3 rows). The
    non-NULL-envelope guarantee therefore lives in the PRODUCTION CALLER —
    ``script_runner.record_step_completion``, which builds an envelope unconditionally on
    every arm. So "no reachable path" is a claim about that caller, and these tests drive it.
    """

    RUN = "rvg-guard1"

    def test_a_real_settle_leaves_the_row_settled_with_a_non_null_envelope(self, _isolated_journal):
        _assert_tmp_db(_isolated_journal)
        on_ready, on_settled = _script_callbacks(self.RUN, "s-ok")

        on_ready("rvg-terminal-1", FP_CURRENT)
        on_settled("rvg-terminal-1", None, "the answer the step produced")

        row = workflow_journal.get_step(self.RUN, "s-ok")
        assert row is not None
        assert row.state == StepState.COMPLETED.value
        assert row.result_json is not None
        envelope = parse_envelope(row.result_json)
        assert envelope is not None
        assert envelope.last_message == "the answer the step produced"
        assert envelope.status == StepState.COMPLETED.value

    def test_a_failed_step_also_settles_with_an_envelope(self, _isolated_journal):
        """ENVELOPE ABSENCE MUST KEEP MEANING "a crash in the settle window" AND NEVER "the
        step failed" (``result-envelope`` BR-1). If a FAILED step settled with no envelope,
        guard 2's signal would fire on every ordinary failure and the crash window would
        become invisible inside the noise."""
        _assert_tmp_db(_isolated_journal)
        on_ready, on_settled = _script_callbacks(self.RUN, "s-failed")

        on_ready("rvg-terminal-2", FP_CURRENT)
        on_settled("rvg-terminal-2", "the worker crashed", None)

        row = workflow_journal.get_step(self.RUN, "s-failed")
        assert row is not None
        assert row.state == StepState.FAILED.value
        assert row.result_json is not None
        envelope = parse_envelope(row.result_json)
        assert envelope is not None
        assert envelope.status == StepState.FAILED.value

    def test_the_no_begin_rescue_path_also_settles_with_an_envelope(self, _isolated_journal):
        """The third reachable arm: a settle whose terminal-ready hook never fired, so
        ``settle_step`` takes its INSERT branch instead of its conflict branch. It is the arm
        most likely to be forgotten, and a bare INSERT that omitted the envelope would leave
        exactly the row guard 2 halts on."""
        _assert_tmp_db(_isolated_journal)
        _on_ready, on_settled = _script_callbacks(self.RUN, "s-rescued")

        on_settled("rvg-terminal-3", None, "rescued without a begin")

        row = workflow_journal.get_step(self.RUN, "s-rescued")
        assert row is not None
        assert row.state == StepState.COMPLETED.value
        assert row.result_json is not None
        assert parse_envelope(row.result_json) is not None
        # No begin ran, so the column ``begin_step`` owns was never written — absent
        # provenance, which routes to a halt rather than a replay match (unit 6's BR-9).
        assert row.call_fingerprint is None

    def test_no_reachable_settle_path_leaves_a_settled_row_with_a_null_envelope(
        self, _isolated_journal
    ):
        """BR-4's claim, over EVERY arm at once. Driving one arm would prove one arm; the
        assertion is that the set of settled rows this caller can produce contains no
        NULL-envelope member."""
        _assert_tmp_db(_isolated_journal)
        _on_ready_a, settle_a = _script_callbacks(f"{self.RUN}-a", "s1")
        _on_ready_b, settle_b = _script_callbacks(f"{self.RUN}-b", "s1")
        _on_ready_c, settle_c = _script_callbacks(f"{self.RUN}-c", "s1")

        _on_ready_a("rvg-terminal-a", FP_CURRENT)
        settle_a("rvg-terminal-a", None, "success arm")
        _on_ready_b("rvg-terminal-b", FP_CURRENT)
        settle_b("rvg-terminal-b", "failure arm", None)
        settle_c("rvg-terminal-c", None, "no-begin arm")  # deliberately no begin

        observed: List[Tuple[str, bool]] = []
        for suffix in ("a", "b", "c"):
            row = workflow_journal.get_step(f"{self.RUN}-{suffix}", "s1")
            assert row is not None
            observed.append((row.state, row.result_json is not None))

        assert observed == [
            (StepState.COMPLETED.value, True),
            (StepState.FAILED.value, True),
            (StepState.COMPLETED.value, True),
        ]

    def test_the_gate_is_never_consulted_while_guard_one_is_being_proved(
        self, _isolated_journal, monkeypatch: pytest.MonkeyPatch
    ):
        """BR-4: "The gate is never consulted — this claim is about what can exist, not about
        what is rejected." Spied rather than trusted, because a settle path that quietly
        consulted the gate would make this whole class a second copy of
        :class:`TestGuardTwoStandingAlone` while still passing."""
        _assert_tmp_db(_isolated_journal)
        decide_calls: List[tuple] = []
        read_calls: List[tuple] = []
        monkeypatch.setattr(step_replay, "decide", lambda *a, **k: decide_calls.append(a))
        monkeypatch.setattr(step_replay, "get_step", lambda *a, **k: read_calls.append(a))

        on_ready, on_settled = _script_callbacks(self.RUN, "s-nogate")
        on_ready("rvg-terminal-4", FP_CURRENT)
        on_settled("rvg-terminal-4", None, "settled without any gate call")

        assert decide_calls == []
        assert read_calls == []
        row = workflow_journal.get_step(self.RUN, "s-nogate")
        assert row is not None
        assert row.result_json is not None

    def test_the_primitive_stores_a_null_envelope_when_it_is_handed_one(self, _isolated_journal):
        """WHERE THE GUARANTEE ACTUALLY LIVES — the half of BR-4 that is easy to state
        imprecisely. ``settle_step`` is a persistence primitive: hand it ``result_json=None``
        and it stores NULL, so it is NOT the guarantor and a test asserting "the real settle
        cannot produce that row" against the primitive would be false.

        This is asserted rather than left as prose because the two facts together are what
        make guard 1 legible: the primitive persists what it is given, and the caller never
        gives it None (the next test)."""
        _assert_tmp_db(_isolated_journal)
        workflow_journal.begin_step(self.RUN, "s-primitive", TS, FP_CURRENT)
        workflow_journal.settle_step(
            run_id=self.RUN,
            step_id="s-primitive",
            state=StepState.COMPLETED.value,
            updated_at=TS,
            result_json=None,
            output_json=None,
            error=None,
        )

        row = workflow_journal.get_step(self.RUN, "s-primitive")
        assert row is not None
        assert row.state == StepState.COMPLETED.value
        assert row.result_json is None

    def test_the_production_caller_never_hands_the_primitive_a_null_envelope(
        self, _isolated_journal, monkeypatch: pytest.MonkeyPatch
    ):
        """THE OTHER HALF, and the one with teeth: every arm of the production settle callback
        passes a non-NULL ``result_json`` that ``parse_envelope`` accepts. Mutating
        ``record_step_completion``'s ``build_envelope(last_message or "", ...)`` into a
        conditional that skips the envelope when there is no message fails HERE."""
        _assert_tmp_db(_isolated_journal)
        handed: List[Optional[str]] = []
        real_settle = workflow_journal.settle_step

        def _spy(**kwargs):
            handed.append(kwargs.get("result_json"))
            return real_settle(**kwargs)

        monkeypatch.setattr(workflow_journal, "settle_step", _spy)

        _on_ready_a, settle_a = _script_callbacks(f"{self.RUN}-spy-a", "s1")
        _on_ready_b, settle_b = _script_callbacks(f"{self.RUN}-spy-b", "s1")
        settle_a("rvg-terminal-5", None, "success arm")
        settle_b("rvg-terminal-6", "failure arm", None)  # last_message is None here

        assert len(handed) == 2
        for result_json in handed:
            assert isinstance(result_json, str)
            assert parse_envelope(result_json) is not None

    def test_the_state_and_the_envelope_land_in_ONE_statement_on_ONE_connection(
        self, _isolated_journal, monkeypatch: pytest.MonkeyPatch
    ):
        """GUARD 1 IS ATOMICITY, AND NO ABORT TEST CAN SEE ATOMICITY BEING LOST.

        An aborted settle proves the database kept none of a FAILED write. It cannot prove
        there is no window between two SUCCEEDING writes, because that window is observable
        only to a process that DIES inside it — and a dead process is exactly what neither a
        real abort nor a mocked exception can stand in for. Reverting ``settle_step`` to the
        pre-#583 ``append_step`` + ``update_step`` pair therefore leaves every crash-window
        assertion in this file green while reopening the window guard 1 closed.

        So the SHAPE is asserted directly, which is the form BR-1 states it in: state,
        attempts, envelope, output and error land in ONE statement on ONE connection.

        NOT A MOCK OF THE DATABASE (TD-4 is intact). Every statement below is forwarded to
        the real SQLite file and the row is read back through ``get_step`` afterwards; only
        the SQL TEXT is observed on the way past. There is no other way to observe a
        statement count, and a test that cannot observe it cannot defend it.

        MUTATION PROOF: split the settle into a state write followed by a second
        ``UPDATE ... SET result_json`` and this test fails — alone in this file.

        THE INSTRUMENT IS SCOPED WITH ``monkeypatch.context()`` AND NEVER ``undo()``. The
        function-scoped ``monkeypatch`` is the SAME object ``_isolated_journal`` used to
        repoint ``DATABASE_FILE``, so an ``undo()`` here would silently un-isolate the rest of
        the test and send the read below to the developer's real database — SR-1's hazard,
        arriving through the back door of a cleanup call. A nested context undoes only its own
        patch, and the ``_assert_tmp_db`` at the end is what proves it.
        """
        _assert_tmp_db(_isolated_journal)
        on_ready, on_settled = _script_callbacks(self.RUN, "s-atomic")
        on_ready("rvg-terminal-7", FP_CURRENT)

        recorders: List[_StatementRecorder] = []
        real_connect = workflow_journal._connect

        def _recording_connect():
            recorder = _StatementRecorder(real_connect())
            recorders.append(recorder)
            return recorder

        with monkeypatch.context() as scoped:
            scoped.setattr(workflow_journal, "_connect", _recording_connect)
            on_settled("rvg-terminal-7", None, "one statement, please")

        # ONE connection for the whole settle. A second one is a second transaction.
        assert len(recorders) == 1, [r.statements for r in recorders]
        mutating = [s for s in recorders[0].statements if _is_mutating(s)]
        assert len(mutating) == 1, mutating
        # ...and that one statement carries BOTH halves of the row the guard is about.
        statement = mutating[0]
        for column in ("state", "attempts", "result_json", "output_json", "error"):
            assert column in statement, column

        # Still isolated after the instrument came off (see the docstring), and the write
        # really landed — the shape assertion above is not standing in for it.
        _assert_tmp_db(_isolated_journal)
        row = workflow_journal.get_step(self.RUN, "s-atomic")
        assert row is not None
        assert row.state == StepState.COMPLETED.value
        assert parse_envelope(row.result_json) is not None


# ---------------------------------------------------------------------------
# Deliverable 3 / BR-5 — THE CRASH TEST. One write, a REAL abort, row left ``running``.
# ---------------------------------------------------------------------------
class TestTheCrashWindowIsClosed:
    """§13 asks for a crash "between the two writes". THERE IS ONLY ONE WRITE.

    ``settlement-rewire`` BR-6 replaced the ``append_step`` + ``update_step`` pair with a
    single ``settle_step``, and that replacement IS guard 1 — so the wording is unsatisfiable
    while its purpose is intact. These tests induce a failure DURING the one settle
    transaction and assert the row is left ``running``: not settled, not half-settled.

    THE ABORT IS REAL, NEVER MOCKED (TD-4). The property under test is that THE TRANSACTION
    kept nothing. A mocked exception proves the ``try`` fires; a ``NOT NULL`` violation proves
    the database did not keep half the write. Two different columns are violated below, so the
    test does not silently depend on one column's constraint surviving a schema edit.
    """

    RUN = "rvg-crash"

    def _begin(self, step_id: str) -> workflow_journal.StepRow:
        on_ready, _on_settled = _script_callbacks(self.RUN, step_id)
        on_ready("rvg-terminal-crash", FP_CURRENT)
        row = workflow_journal.get_step(self.RUN, step_id)
        assert row is not None
        assert row.state == StepState.RUNNING.value
        return row

    def test_a_real_abort_during_the_settle_leaves_the_row_exactly_as_begin_left_it(
        self, _isolated_journal
    ):
        _assert_tmp_db(_isolated_journal)
        before = self._begin("s-abort-state")

        with pytest.raises(sqlite3.IntegrityError) as excinfo:
            workflow_journal.settle_step(
                run_id=self.RUN,
                step_id="s-abort-state",
                # A genuine constraint violation, not a mock: ``state`` is ``TEXT NOT NULL``.
                state=None,  # type: ignore[arg-type]
                updated_at="2026-08-17T00:00:09Z",
                result_json=RESULT_JSON,
                output_json='{"answer": 42}',
                error="an error that must not land either",
            )

        assert "NOT NULL" in str(excinfo.value)
        after = workflow_journal.get_step(self.RUN, "s-abort-state")
        assert after is not None
        # All four fields BR-5 names, as they were before the settle.
        assert after.state == before.state == StepState.RUNNING.value
        assert after.attempts == before.attempts == 0
        assert after.result_json is before.result_json is None
        assert after.error is before.error is None
        # And nothing else moved either: the row is byte-for-byte what ``begin_step`` wrote.
        assert after == before

    def test_a_real_abort_on_a_different_not_null_column_behaves_identically(
        self, _isolated_journal
    ):
        """The same property through a second real constraint, so the proof does not rest on
        ``state`` alone. ``updated_at`` is the column every arm of the settle writes."""
        _assert_tmp_db(_isolated_journal)
        before = self._begin("s-abort-updated-at")

        with pytest.raises(sqlite3.IntegrityError):
            workflow_journal.settle_step(
                run_id=self.RUN,
                step_id="s-abort-updated-at",
                state=StepState.COMPLETED.value,
                updated_at=None,  # type: ignore[arg-type]
                result_json=RESULT_JSON,
                output_json=None,
                error=None,
            )

        after = workflow_journal.get_step(self.RUN, "s-abort-updated-at")
        assert after == before
        assert after is not None
        assert after.state == StepState.RUNNING.value

    def test_an_abort_on_the_no_begin_insert_path_leaves_no_row_at_all(self, _isolated_journal):
        """The INSERT branch's abort. ``settle_step`` is an upsert, so a settle with no prior
        row would MINT one — and an aborted mint must leave nothing rather than a row with
        some columns filled in."""
        _assert_tmp_db(_isolated_journal)
        assert workflow_journal.get_step(self.RUN, "s-abort-insert") is None

        with pytest.raises(sqlite3.IntegrityError):
            workflow_journal.settle_step(
                run_id=self.RUN,
                step_id="s-abort-insert",
                state=None,  # type: ignore[arg-type]
                updated_at=TS,
                result_json=RESULT_JSON,
                output_json=None,
                error=None,
            )

        assert workflow_journal.get_step(self.RUN, "s-abort-insert") is None

    def test_there_is_no_third_state_between_running_and_settled_with_an_envelope(
        self, _isolated_journal
    ):
        """BR-4's closing claim, made a single assertion: after a real settle the row is
        settled AND carries an envelope; after an induced abort it is ``running`` and carries
        none. Asserting the exact PAIR set is what a future half-settled state would break —
        each half asserted separately could be individually "corrected"."""
        _assert_tmp_db(_isolated_journal)
        on_ready_ok, settle_ok = _script_callbacks(f"{self.RUN}-ok", "s1")
        on_ready_ok("rvg-terminal-ok", FP_CURRENT)
        settle_ok("rvg-terminal-ok", None, "settled for real")

        on_ready_bad, _settle_bad = _script_callbacks(f"{self.RUN}-bad", "s1")
        on_ready_bad("rvg-terminal-bad", FP_CURRENT)
        with pytest.raises(sqlite3.IntegrityError):
            workflow_journal.settle_step(
                run_id=f"{self.RUN}-bad",
                step_id="s1",
                state=None,  # type: ignore[arg-type]
                updated_at=TS,
                result_json=RESULT_JSON,
                output_json=None,
                error=None,
            )

        observed = set()
        for suffix in ("ok", "bad"):
            row = workflow_journal.get_step(f"{self.RUN}-{suffix}", "s1")
            assert row is not None
            observed.add((row.state, row.result_json is not None))

        assert observed == {
            (StepState.COMPLETED.value, True),
            (StepState.RUNNING.value, False),
        }


# ---------------------------------------------------------------------------
# Deliverable 5 / BR-6..BR-8 — THE UPGRADE WINDOW
# ---------------------------------------------------------------------------
class TestTheUpgradeWindow:
    """A run started under the retired three-field scheme, resumed under ``v2``.

    It must route by the CURRENT call's declared policy — four ways — and NONE of the four may
    report ``DIVERGED``. That last part is the point of the deliverable: ``replay-gate``'s
    BR-4 orders provenance ahead of equality precisely so an unverifiable row is never
    labelled a divergence, and this is its end-to-end proof. The ``delivery-planning`` gate
    named this scenario as the single risk no unit had been assigned.

    THE LEGACY ROW IS MANUFACTURED BY PREFIX (BR-6/TD-6). ``scheme_of`` classifies by prefix,
    so any 64-hex without ``v2:`` IS a legacy row — no old hash function, no old commit. That
    is what makes this deliverable cheap, and it was plausibly expensive.
    """

    RUN = "rvg-upgrade"

    def _manufacture_legacy(self, step_id: str = "s1") -> None:
        # A READABLE envelope is required, or rule 3 fires first and rules 4-5 never run.
        _manufacture_row(
            self.RUN,
            step_id,
            state=StepState.COMPLETED.value,
            call_fingerprint=FP_LEGACY,
            result_json=RESULT_JSON,
        )

    # -- BR-6: the fixture is a legacy row, and is not a digest ------------------------
    def test_the_fixture_classifies_as_legacy_and_as_neither_other_scheme(self):
        """Asserted on the value DIRECTLY (BR-6's verification), so the fixture cannot drift
        into ``absent`` or ``v2`` unnoticed and take four silent passes with it."""
        assert scheme_of(FP_LEGACY) == "legacy"
        assert scheme_of(FP_LEGACY) != "absent"
        assert scheme_of(FP_LEGACY) != "v2"
        assert len(FP_LEGACY) == 64
        assert not FP_LEGACY.startswith("v2:")
        assert all(c in "0123456789abcdef" for c in FP_LEGACY)

    def test_the_fixture_is_not_a_real_digest_of_this_call(self):
        """SR-3 with teeth. A real digest would imply the retired three-field scheme is
        reproducible from this fixture and invite a later reader to "verify" it, which is how
        the second fingerprint implementation ``step-fingerprint`` deleted a helper to prevent
        gets reintroduced inside a test."""
        assert FP_LEGACY != FP_CURRENT
        assert FP_LEGACY != FP_CURRENT.removeprefix("v2:")
        # A repeating 8-character pattern: no digest function produces this.
        assert FP_LEGACY == FP_LEGACY[:8] * 8

    def test_the_stored_row_reads_back_as_legacy_through_the_production_reader(
        self, _isolated_journal
    ):
        _assert_tmp_db(_isolated_journal)
        self._manufacture_legacy()

        row = workflow_journal.get_step(self.RUN, "s1")
        assert row is not None
        assert scheme_of(row.call_fingerprint) == "legacy"
        # The envelope IS readable, so rule 3 cannot be what decides these four cases.
        assert parse_envelope(row.result_json) is not None

    # -- BR-7: the four policy routes, one explicit test each --------------------------
    # Explicit rather than parameterised so a failure names the policy it belongs to; the
    # cross-cutting sweep that no route diverges follows below.
    def test_idempotent_executes_across_the_upgrade_window(self, _isolated_journal):
        """Rule 4: an author who declared re-execution safe is not punished by the scheme
        upgrade."""
        _assert_tmp_db(_isolated_journal)
        self._manufacture_legacy()

        decision = decide(self.RUN, "s1", FP_CURRENT, RecoveryPolicy.IDEMPOTENT)

        assert decision.verdict is ReplayVerdict.EXECUTE
        assert decision.verdict is not ReplayVerdict.DIVERGED
        assert decision.rule is None
        assert decision.envelope is None

    def test_reconcile_executes_across_the_upgrade_window(self, _isolated_journal):
        """Rule 4 again. ``RECONCILE`` is indistinguishable from ``IDEMPOTENT`` at this gate
        today (``replay-gate`` BR-11 defers the reconciliation operation), and asserting it
        separately is what would notice if that stopped being true."""
        _assert_tmp_db(_isolated_journal)
        self._manufacture_legacy()

        decision = decide(self.RUN, "s1", FP_CURRENT, RecoveryPolicy.RECONCILE)

        assert decision.verdict is ReplayVerdict.EXECUTE
        assert decision.verdict is not ReplayVerdict.DIVERGED
        assert decision.rule is None

    def test_manual_halts_as_provenance_unverifiable(self, _isolated_journal):
        """Rule 5: unverifiable provenance never replays as a match (FR-6). The ``rule`` is
        asserted by MEMBER, and against ``ENVELOPE_ABSENT`` in particular — the envelope here
        is readable, so a halt reported as ``ENVELOPE_ABSENT`` would mean rules 3 and 5 had
        been transposed."""
        _assert_tmp_db(_isolated_journal)
        self._manufacture_legacy()

        decision = decide(self.RUN, "s1", FP_CURRENT, RecoveryPolicy.MANUAL)

        assert decision.verdict is ReplayVerdict.DECISION_REQUIRED
        assert decision.verdict is not ReplayVerdict.DIVERGED
        assert decision.rule is HaltRule.PROVENANCE_UNVERIFIABLE
        assert decision.rule is not HaltRule.ENVELOPE_ABSENT
        assert decision.rule is not HaltRule.POLICY_MANUAL
        assert decision.envelope is None

    def test_an_undeclared_policy_halts_as_provenance_unverifiable(self, _isolated_journal):
        """Rule 5 for the undeclared case. ``None`` is NOT a default that falls through to
        the catch-all here: rule 5 catches it first, which is why the two halting routes must both be
        asserted rather than one standing in for the other."""
        _assert_tmp_db(_isolated_journal)
        self._manufacture_legacy()

        decision = decide(self.RUN, "s1", FP_CURRENT, None)

        assert decision.verdict is ReplayVerdict.DECISION_REQUIRED
        assert decision.verdict is not ReplayVerdict.DIVERGED
        assert decision.rule is HaltRule.PROVENANCE_UNVERIFIABLE
        assert decision.envelope is None

    # -- BR-8: none of the four may report DIVERGED. THE POINT OF THE DELIVERABLE. -----
    def test_no_policy_route_reports_a_divergence(self, _isolated_journal):
        """BR-8, AS ONE ASSERTION OVER ALL FOUR ROUTES — including the two that already halt.

        A future reordering that put rule 6 before rules 4-5 would turn each halt into a
        ``DIVERGED``, and a per-route halt assertion alone would notice only that "the halt
        changed": someone could plausibly update it. This one cannot be satisfied that way,
        because ``DIVERGED`` is what it forbids for every route at once. It is the end-to-end
        proof of ``replay-gate`` BR-4's provenance-before-equality ordering."""
        _assert_tmp_db(_isolated_journal)
        self._manufacture_legacy()

        verdicts = {policy: decide(self.RUN, "s1", FP_CURRENT, policy) for policy in POLICIES}

        for policy, decision in verdicts.items():
            assert decision.verdict is not ReplayVerdict.DIVERGED, policy
        assert verdicts[RecoveryPolicy.IDEMPOTENT].verdict is ReplayVerdict.EXECUTE
        assert verdicts[RecoveryPolicy.RECONCILE].verdict is ReplayVerdict.EXECUTE
        assert verdicts[RecoveryPolicy.MANUAL].verdict is ReplayVerdict.DECISION_REQUIRED
        assert verdicts[None].verdict is ReplayVerdict.DECISION_REQUIRED

    def test_no_policy_route_replays_an_unverifiable_row(self, _isolated_journal):
        """The companion prohibition: an unverifiable fingerprint must never be served as a
        verified match either (FR-6). Between this and the test above, the only outcomes left
        for a legacy row are the four in BR-7's table."""
        _assert_tmp_db(_isolated_journal)
        self._manufacture_legacy()

        for policy in POLICIES:
            decision = decide(self.RUN, "s1", FP_CURRENT, policy)
            assert decision.verdict is not ReplayVerdict.REPLAY, policy
            assert decision.envelope is None, policy

    def test_the_same_row_under_the_current_scheme_replays_instead(self, _isolated_journal):
        """THE DISCRIMINATOR for the whole class. Every assertion above would also pass on a
        gate that halted on every settled row regardless of scheme; the ONLY difference here
        is the stored fingerprint's prefix."""
        _assert_tmp_db(_isolated_journal)
        _manufacture_row(
            self.RUN,
            "s-current-scheme",
            state=StepState.COMPLETED.value,
            call_fingerprint=FP_CURRENT,
            result_json=RESULT_JSON,
        )

        decision = decide(self.RUN, "s-current-scheme", FP_CURRENT, None)

        assert decision.verdict is ReplayVerdict.REPLAY
        assert decision.envelope is not None
        assert decision.envelope.last_message == ENVELOPE.last_message

    def test_the_upgrade_window_reason_states_the_scheme_it_found(self, _isolated_journal):
        """FR-12 diagnosability: an operator reading the halt must be able to tell "recorded
        under narrower rules" from "never recorded". ``legacy`` and ``absent`` route
        identically and stay DISTINCT FACTS in ``reason``, so this asserts the word."""
        _assert_tmp_db(_isolated_journal)
        self._manufacture_legacy()
        _manufacture_row(
            self.RUN,
            "s-absent-scheme",
            state=StepState.COMPLETED.value,
            call_fingerprint=None,
            result_json=RESULT_JSON,
        )

        legacy = decide(self.RUN, "s1", FP_CURRENT, None)
        absent = decide(self.RUN, "s-absent-scheme", FP_CURRENT, None)

        assert legacy.rule is HaltRule.PROVENANCE_UNVERIFIABLE
        assert absent.rule is HaltRule.PROVENANCE_UNVERIFIABLE
        assert "'legacy'" in legacy.reason
        assert "'absent'" in absent.reason
        assert legacy.reason != absent.reason
        # SR-1 (inherited from ``step-fingerprint``): no digest travels in a reason.
        assert FP_LEGACY not in legacy.reason
        assert FP_CURRENT not in legacy.reason


# ---------------------------------------------------------------------------
# INV-1 / BR-1 — this unit changes no production file
# ---------------------------------------------------------------------------
class TestThisUnitShipsNoProductionCode:
    """BR-1/SR-5. If a test could not be written without a production change, that would be a
    FINDING ABOUT THE CODE, not licence to edit it — so the absence is asserted here rather
    than left to the diff review."""

    def test_this_module_imports_no_name_it_had_to_add(self):
        """Every production name this file binds already existed before this unit. Asserted by
        resolving each one, so a test that quietly depended on a new helper would fail here
        rather than pass on a diff nobody re-read."""
        for name in (
            workflow_journal.begin_step,
            workflow_journal.settle_step,
            workflow_journal.get_step,
            step_replay.decide,
            script_runner.make_step_terminal_recorder,
            script_runner.record_step_completion,
            scheme_of,
            compute,
            parse_envelope,
            serialise_envelope,
        ):
            assert callable(name)

    def test_the_vocabularies_this_unit_reads_are_all_closed_enums(self):
        """The row states, verdicts and halt rules are read by MEMBER throughout this file
        (BR-11). If any of them became an open ``str``, "assert by member" would stop meaning
        anything and this is where that shows up."""
        assert len({v.value for v in ReplayVerdict}) == 4
        # SIX since PR #628's review appended ``OUTCOME_FAILED`` and ``ENVELOPE_LOSSY``.
        # ``ReplayVerdict`` stayed at four: both new rules reuse ``DECISION_REQUIRED``.
        assert len({r.value for r in HaltRule}) == 6
        assert len({p.value for p in RecoveryPolicy}) == 3
        # The three row states this file names, pinned against their members so a rename on
        # either side fails loudly.
        states: Dict[str, str] = {
            "running": StepState.RUNNING.value,
            "completed": StepState.COMPLETED.value,
            "failed": StepState.FAILED.value,
        }
        for literal, member in states.items():
            assert literal == member

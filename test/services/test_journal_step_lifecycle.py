"""Tests for ``journal-step-lifecycle`` (issue #583, unit 6).

Covers the two new write functions in ``workflow_journal`` — ``begin_step``
(Algorithm A) and ``settle_step`` (Algorithm B) — plus the read-model debt this
unit pays for ``result-envelope`` (unit 2): ``StepRow.result_json`` and the
column in both SELECT lists (BR-15).

Rule coverage, one group per rule from
``construction/journal-step-lifecycle/functional-design/business-rules.md``:

- BR-7  — ``begin_step`` re-baselines ``call_fingerprint`` while the prior row is
  NOT ``completed``, and preserves it once it is. The condition is an open
  negative, so an arbitrary UNKNOWN future state value must re-baseline too:
  that test is what stops a later edit from narrowing it to an allowlist, which
  would silently stop re-baselining when ``rerun_authorized`` (unit 12) ships.
- BR-8  — ``begin_step`` never writes ``attempts``.
- BR-5  — ``attempts`` is cumulative and owned by the SQL, so it survives a
  resume (asserted across three SIMULATED PROCESSES, because a single-process
  test would also pass under caller-authoritative semantics and therefore would
  not discriminate) and never decreases.
- BR-4/BR-13 — ``settle_step`` is an UPSERT, so a settle with no begin is
  rescued rather than lost, and the return value reports which branch ran.
- BR-9  — ``settle_step`` never writes ``call_fingerprint``.
- BR-1/BR-2 — one statement: a settled row always carries the envelope it was
  given, and a forced mid-write failure leaves the row as ``begin_step`` set it.
- BR-3  — both functions raise; neither swallows.
- BR-12 — the bare ``'running'`` literal is pinned to ``StepState.RUNNING.value``
  by importing the enum in THIS FILE only (the module gains no ``models``
  dependency).
- BR-14/SR-2 — ``settle_step`` sanitises nothing and imports neither of unit 2's
  service modules; content round-trips byte-identical.
- BR-6/BR-10/BR-11/SR-6 — the signature deviations are pinned, the two existing
  helpers keep their shapes, the corrected docstring no longer makes the claim
  ``begin_step`` falsified, and a ``step_id`` full of SQL metacharacters is data.
- BR-15 — ``get_step`` and ``get_steps`` both read back what settle wrote.

The journal points at a temp SQLite DB via the patched ``DATABASE_FILE``,
mirroring ``test_script_journal_extension.py``'s fixture pattern.
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
from cli_agent_orchestrator.models.workflow_runtime import StepState
from cli_agent_orchestrator.services import workflow_journal
from cli_agent_orchestrator.services.workflow_journal import begin_step, settle_step

FP_A = "a" * 64
FP_B = "b" * 64
FP_C = "c" * 64

TS = "2026-08-16T00:00:00Z"


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


def _seed_run(run_id: str = "r1", *, state: str = "running", tier: str = "script"):
    workflow_journal.insert_run(
        run_id=run_id,
        workflow_name="wf",
        spec_snapshot="{}",
        inputs_json="{}",
        state=state,
        started_at=TS,
        tier=tier,
    )


def _raw_row(run_id: str = "r1", step_id: str = "s1"):
    """Read the four columns under test straight from SQLite, bypassing ``StepRow``."""
    with _direct_connect() as conn:
        return conn.execute(
            "SELECT state, attempts, call_fingerprint, result_json "
            "FROM workflow_run_step WHERE run_id = ? AND step_id = ?",
            (run_id, step_id),
        ).fetchone()


def _force_state(state: str, run_id: str = "r1", step_id: str = "s1"):
    """Put a row into an arbitrary state directly, including values no enum defines."""
    with _direct_connect() as conn:
        conn.execute(
            "UPDATE workflow_run_step SET state = ? WHERE run_id = ? AND step_id = ?",
            (state, run_id, step_id),
        )


def _settle(
    *,
    run_id: str = "r1",
    step_id: str = "s1",
    state: str = "completed",
    result_json: Optional[str] = '{"ok": true}',
    output_json: Optional[str] = None,
    error: Optional[str] = None,
) -> bool:
    return settle_step(
        run_id=run_id,
        step_id=step_id,
        state=state,
        updated_at=TS,
        result_json=result_json,
        output_json=output_json,
        error=error,
    )


# ---------------------------------------------------------------------------
# BR-7 — the conditional fingerprint re-baseline
# ---------------------------------------------------------------------------
def test_begin_step_sets_fingerprint_on_first_insert():
    """The INSERT path: first arrival records the passed fingerprint and ``running``."""
    _seed_run()
    begin_step("r1", "s1", TS, FP_A)

    state, attempts, fingerprint, result_json = _raw_row()
    assert state == "running"
    assert attempts == 0
    assert fingerprint == FP_A
    assert result_json is None


def test_begin_step_rebaselines_fingerprint_over_a_running_row():
    """A retry within one execution lineage re-baselines: the row is not terminal."""
    _seed_run()
    begin_step("r1", "s1", TS, FP_A)
    begin_step("r1", "s1", TS, FP_B)

    state, _, fingerprint, _ = _raw_row()
    assert state == "running"
    assert fingerprint == FP_B


def test_begin_step_rebaselines_fingerprint_over_a_failed_row():
    """``failed`` is not terminal for this purpose — a re-dispatch is a fresh call."""
    _seed_run()
    begin_step("r1", "s1", TS, FP_A)
    _settle(state="failed", error="boom")
    begin_step("r1", "s1", TS, FP_B)

    state, _, fingerprint, _ = _raw_row()
    assert state == "running"
    assert fingerprint == FP_B


def test_begin_step_rebaselines_fingerprint_over_an_unknown_future_state():
    """BR-7's open negative, and the single most load-bearing test in this file.

    ``'rerun_authorized'`` does not exist yet — ``recovery-decision-intake``
    (unit 12) introduces it, and ``StepState`` does not define it today. This
    test writes an arbitrary state value no enum knows and asserts the
    fingerprint STILL re-baselines, which is only true while the SQL condition
    is ``state != 'completed'`` rather than an allowlist of today's states.

    Narrowing the condition to ``IN ('running', 'failed', ...)`` would keep every
    other test in this file green while silently reinstating the permanent-halt
    bug the rule exists to kill — a rerun that a human authorised would keep its
    stale fingerprint and every later resume would halt again on the same step.
    That regression would otherwise be invisible until unit 12 shipped.
    """
    _seed_run()
    begin_step("r1", "s1", TS, FP_A)
    _force_state("rerun_authorized")
    begin_step("r1", "s1", TS, FP_B)

    state, _, fingerprint, _ = _raw_row()
    assert state == "running"
    assert fingerprint == FP_B

    # A second unknown value, to make the point that it is not the specific
    # string that matters — only that it is not ``completed``.
    _force_state("some_state_invented_in_2027")
    begin_step("r1", "s1", TS, FP_C)
    assert _raw_row()[2] == FP_C


def test_begin_step_preserves_fingerprint_over_a_completed_row():
    """The one negative arm: a ``completed`` row's fingerprint is immutable (INV-3).

    It was recorded against a real result, so it is the value ``lookup_replay``
    must keep comparing against. ``state`` and ``updated_at`` still move.
    """
    _seed_run()
    begin_step("r1", "s1", TS, FP_A)
    _settle(state="completed")
    begin_step("r1", "s1", "2026-08-16T01:00:00Z", FP_B)

    state, _, fingerprint, _ = _raw_row()
    assert state == "running"  # the state DOES move
    assert fingerprint == FP_A  # the fingerprint does NOT
    assert workflow_journal.get_step("r1", "s1").updated_at == "2026-08-16T01:00:00Z"


# ---------------------------------------------------------------------------
# BR-8 — begin_step never writes attempts
# ---------------------------------------------------------------------------
def test_begin_step_first_insert_writes_zero_attempts():
    _seed_run()
    begin_step("r1", "s1", TS, FP_A)
    assert _raw_row()[1] == 0


def test_begin_step_does_not_reset_the_attempt_count():
    """A second begin on a step already settled three times must not zero the count."""
    _seed_run()
    begin_step("r1", "s1", TS, FP_A)
    for _ in range(3):
        _settle(state="failed", error="boom")
    assert _raw_row()[1] == 3

    begin_step("r1", "s1", TS, FP_B)

    state, attempts, _, _ = _raw_row()
    assert state == "running"
    assert attempts == 3  # survived the re-begin


# ---------------------------------------------------------------------------
# BR-5 — attempts is cumulative and SQL-owned
# ---------------------------------------------------------------------------
def test_settle_step_insert_path_writes_one_attempt():
    _seed_run()
    assert _settle() is False
    assert _raw_row()[1] == 1


def test_attempts_accumulate_across_three_simulated_processes():
    """BR-5's point is that the durable count survives a RESUME, not just a retry.

    Each iteration models a separate process: the caller's in-process attempt
    counter is re-created at 0 (``st_attempts`` below), exactly as
    ``StepRunState`` is after a restart. ``settle_step`` takes no ``attempts``
    argument, so the count can only come from the SQL — and it climbs 1, 2, 3.

    A single-process test would NOT discriminate here: it would also pass under
    caller-authoritative semantics, where each fresh process would pass 1 and the
    row would read 1 forever while reporting a step attempted three times as
    attempted once (untruthful under FR-12).
    """
    _seed_run()
    observed = []
    for _ in range(3):
        st_attempts = 0  # a fresh process's per-run counter, always restarting at 0
        st_attempts += 1
        assert st_attempts == 1  # what a caller-supplied count would have written
        begin_step("r1", "s1", TS, FP_A)
        _settle(state="failed", error="boom")
        observed.append(_raw_row()[1])

    assert observed == [1, 2, 3]


def test_attempt_count_never_decreases_across_a_mixed_lifecycle():
    """INV-2: monotonic over begins, settles and a re-begin after a terminal state."""
    _seed_run()
    counts = []
    begin_step("r1", "s1", TS, FP_A)
    counts.append(_raw_row()[1])
    _settle(state="failed", error="boom")
    counts.append(_raw_row()[1])
    begin_step("r1", "s1", TS, FP_B)  # re-dispatch
    counts.append(_raw_row()[1])
    _settle(state="completed")
    counts.append(_raw_row()[1])
    begin_step("r1", "s1", TS, FP_C)  # begin over a completed row
    counts.append(_raw_row()[1])

    assert counts == [0, 1, 1, 2, 2]
    assert all(b >= a for a, b in zip(counts, counts[1:]))


# ---------------------------------------------------------------------------
# BR-4 + BR-13 — the rescue path and the branch report
# ---------------------------------------------------------------------------
def test_settle_with_no_begin_creates_a_complete_row():
    """The no-begin case is live today (the terminal-created callback never fired).

    A bare UPDATE would affect 0 rows and discard the result silently, which is
    worse than the half-written row guard 1 prevents: the replay gate can reject
    a settled row with no envelope, but not a row that does not exist.
    """
    _seed_run()
    _settle(state="completed", result_json='{"ok": true}', output_json='{"n": 1}')

    row = workflow_journal.get_step("r1", "s1")
    assert row is not None
    assert row.state == "completed"
    assert row.attempts == 1
    assert row.result_json == '{"ok": true}'
    assert row.output_json == '{"n": 1}'
    assert row.updated_at == TS


def test_settle_returns_false_when_it_created_the_row():
    _seed_run()
    assert _settle() is False


def test_settle_returns_true_when_a_row_already_existed():
    _seed_run()
    begin_step("r1", "s1", TS, FP_A)
    assert _settle() is True


def test_settle_rescue_path_writes_one_attempt():
    _seed_run()
    _settle()
    assert _raw_row()[1] == 1


# ---------------------------------------------------------------------------
# BR-9 — settle_step never writes call_fingerprint
# ---------------------------------------------------------------------------
def test_fingerprint_is_null_after_a_begin_less_settle():
    """Absent provenance is the DESIGNED outcome of the rescue path (INV-5).

    FR-6 routes a NULL fingerprint to a halt rather than a replay match, so the
    rescued result is durably visible to a human and never replayed as a match.
    """
    _seed_run()
    _settle(state="completed")
    assert _raw_row()[2] is None


def test_fingerprint_unchanged_by_a_normal_settle():
    _seed_run()
    begin_step("r1", "s1", TS, FP_A)
    _settle(state="completed")
    assert _raw_row()[2] == FP_A


# ---------------------------------------------------------------------------
# BR-1 + BR-2 — one statement: settled-and-complete, or untouched
# ---------------------------------------------------------------------------
def test_a_settled_row_always_carries_the_envelope_it_was_given():
    """FR-4 guard 1: no window exists in which the row reads settled and empty."""
    _seed_run()
    begin_step("r1", "s1", TS, FP_A)
    _settle(state="completed", result_json='{"envelope": "yes"}')

    state, _, _, result_json = _raw_row()
    assert state == "completed"
    assert result_json == '{"envelope": "yes"}'


def test_a_mid_write_failure_leaves_the_row_running_with_no_envelope():
    """A REAL crash, not a mocked return: ``state`` violates NOT NULL on the upsert.

    The failure surfaces from the single statement that writes state, attempts,
    envelope, output and error together, so the transaction rolls back and NONE
    of them land. The row stays as ``begin_step`` left it — ``running``, which is
    not settled and which ``lookup_replay``'s existing partial guard already
    rejects (BR-2). Atomic means never half-written, not never-failing.
    """
    _seed_run()
    begin_step("r1", "s1", TS, FP_A)

    with pytest.raises(sqlite3.Error):
        _settle(state=None, result_json='{"envelope": "lost"}')  # type: ignore[arg-type]

    state, attempts, fingerprint, result_json = _raw_row()
    assert state == "running"  # never a settled state with an absent envelope
    assert result_json is None
    assert attempts == 0  # the +1 did not land either
    assert fingerprint == FP_A


# ---------------------------------------------------------------------------
# BR-3 — both raise; neither swallows
# ---------------------------------------------------------------------------
def test_begin_step_propagates_a_db_error(monkeypatch: pytest.MonkeyPatch):
    def _boom():
        raise sqlite3.OperationalError("simulated connect failure")

    monkeypatch.setattr(workflow_journal, "_connect", _boom, raising=True)
    with pytest.raises(sqlite3.Error):
        begin_step("r1", "s1", TS, FP_A)


def test_settle_step_propagates_a_db_error(monkeypatch: pytest.MonkeyPatch):
    def _boom():
        raise sqlite3.OperationalError("simulated connect failure")

    monkeypatch.setattr(workflow_journal, "_connect", _boom, raising=True)
    with pytest.raises(sqlite3.Error):
        _settle()


def test_neither_function_contains_a_try_except():
    """Source inspection, because the posture is the point: the swallow-and-warn
    lives in ``record_step_completion`` and stays there, so a journal failure
    degrades resumability and never fails a running step (INV-4).

    Parsed with ``ast`` rather than string-matched, so a docstring that discusses
    error handling in prose cannot produce a false pass or a false failure.
    """
    for fn in (begin_step, settle_step):
        tree = ast.parse(inspect.getsource(fn))
        handlers = [node for node in ast.walk(tree) if isinstance(node, ast.Try)]
        assert handlers == [], f"{fn.__name__} must not swallow: found a try block"


# ---------------------------------------------------------------------------
# BR-12 — the bare literal, pinned from the test side only
# ---------------------------------------------------------------------------
def test_the_state_begin_step_writes_equals_the_enum_value():
    """``begin_step`` hardcodes ``'running'`` and the module keeps no ``models``
    dependency (TD-4). The enum is imported HERE, so a rename on either side
    fails loudly without coupling the module to ``models``.
    """
    _seed_run()
    begin_step("r1", "s1", TS, FP_A)
    assert _raw_row()[0] == StepState.RUNNING.value


# ---------------------------------------------------------------------------
# BR-14 + SR-2 — persists what it is given; sanitises nothing
# ---------------------------------------------------------------------------
def test_content_columns_round_trip_byte_identical():
    """A credential-shaped and a path-shaped value survive unchanged.

    This asserts the HONEST limit of what this unit can claim (SR-2): the values
    are written as received. ``settlement-rewire`` (unit 8) owns redacting AND
    bounding ``error``/``output_json``, and its own tests assert that redaction
    actually happens before the call. The synthetic key below is assembled from
    two halves and spells TESTONLY so a repository secret scanner does not flag
    this file — the same precaution unit 2's tests take.
    """
    fake_key = "AKIA" + "TESTONLY0000ABCD"
    error = f"provider failed: aws_access_key_id={fake_key} in /Users/someone/.aws/credentials"
    output = '{"path": "/Users/someone/Library/Application Support/x", "n": 1}'

    _seed_run()
    begin_step("r1", "s1", TS, FP_A)
    _settle(state="failed", result_json=None, output_json=output, error=error)

    row = workflow_journal.get_step("r1", "s1")
    assert row is not None
    assert row.error == error  # byte-identical: no redaction, no bounding
    assert row.output_json == output
    assert fake_key in row.error  # the pass-through is unambiguous, not incidental
    assert row.result_json is None


def test_the_journal_module_imports_neither_of_unit_2s_service_modules():
    """SR-2: no security logic inside a persistence primitive.

    Checked over the module's IMPORT GRAPH via ``ast`` — including its
    function-level imports — rather than by grepping the source text, so prose
    that names either module cannot produce a false failure.
    """
    tree = ast.parse(inspect.getsource(workflow_journal))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            imported.add(base)
            imported.update(f"{base}.{alias.name}" for alias in node.names)

    flat = " ".join(sorted(imported))
    assert "secret_gate" not in flat
    assert "step_result" not in flat


# ---------------------------------------------------------------------------
# BR-6 / BR-10 / BR-11 / SR-6 — the contract this unit pins for unit 8
# ---------------------------------------------------------------------------
def test_settle_step_signature_has_no_attempts_parameter_and_returns_bool():
    """BR-6 + BR-13/TD-3, the two deliberate deviations from ``component-methods.md``.

    Pinned so ``settlement-rewire`` (unit 8) cannot be written against the old
    shape and pass review: the count is SQL-owned, so an ``attempts`` argument
    would be accepted and silently ignored.
    """
    sig = inspect.signature(settle_step)
    assert list(sig.parameters) == [
        "run_id",
        "step_id",
        "state",
        "updated_at",
        "result_json",
        "output_json",
        "error",
    ]
    assert "attempts" not in sig.parameters
    assert sig.return_annotation in (bool, "bool")

    begin_sig = inspect.signature(begin_step)
    assert list(begin_sig.parameters) == ["run_id", "step_id", "updated_at", "call_fingerprint"]
    assert begin_sig.return_annotation in (None, "None")


def test_the_existing_helpers_keep_their_signatures():
    """BR-10, the additive-only invariant: this unit ADDS two functions and
    changes neither existing one's shape (nor, therefore, its YAML-tier callers).
    """
    assert list(inspect.signature(workflow_journal.append_step).parameters) == [
        "run_id",
        "step_id",
        "state",
        "updated_at",
        "call_fingerprint",
    ]
    # MERGE NOTE (2026-08-17, #583 x #504): ``error_kind`` is #504's addition to
    # ``update_step``, NOT this change's — ``git diff 135e7ff..949eab1`` shows #583 never
    # touched this signature. The guard keeps its full force: it still proves #583 left
    # ``append_step``/``update_step`` alone, so the expected list is brought up to current
    # reality rather than the assertion being relaxed.
    assert list(inspect.signature(workflow_journal.update_step).parameters) == [
        "run_id",
        "step_id",
        "state",
        "attempts",
        "updated_at",
        "output_json",
        "error",
        "error_kind",
    ]


def test_append_steps_docstring_no_longer_claims_exclusivity():
    """BR-11: ``begin_step`` is the second writer of ``call_fingerprint``, so the
    old claim became false the moment this unit landed. Both docstrings must now
    name the other's regime.
    """
    append_doc = inspect.getdoc(workflow_journal.append_step) or ""
    assert "sole write path" not in append_doc
    assert "begin_step" in append_doc

    begin_doc = inspect.getdoc(begin_step) or ""
    assert "append_step" in begin_doc


def test_a_step_id_full_of_sql_metacharacters_round_trips_as_data():
    """SR-6: every value binds through a ``?`` placeholder, so a hostile-looking
    key is a harmless literal rather than a statement.
    """
    nasty = "s1'; DROP TABLE workflow_run_step; --"
    _seed_run()
    begin_step("r1", nasty, TS, FP_A)
    assert _settle(step_id=nasty) is True

    row = workflow_journal.get_step("r1", nasty)
    assert row is not None
    assert row.step_id == nasty
    assert row.state == "completed"
    # The table is still there, which is the other half of the assertion.
    with _direct_connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM workflow_run_step").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# BR-15 — unit 2's read-model debt, actually paid
# ---------------------------------------------------------------------------
def test_get_step_reads_back_the_envelope_settle_wrote():
    """Without this, ``settle_step`` could write a column no reader returns and
    every other test in this file would still pass. It is the only proof the debt
    from ``result-envelope`` (unit 2) is paid, and unit 7's ``envelope_from_row``
    plus FR-4 guard 2 both depend on it.
    """
    _seed_run()
    begin_step("r1", "s1", TS, FP_A)
    _settle(state="completed", result_json='{"envelope": "read-me"}')

    row = workflow_journal.get_step("r1", "s1")
    assert row is not None
    assert row.result_json == '{"envelope": "read-me"}'
    assert row.call_fingerprint == FP_A  # the pre-existing field still reads correctly


def test_get_steps_reads_back_the_envelope_settle_wrote():
    """The list read model too — a settled step and a never-settled sibling."""
    _seed_run()
    begin_step("r1", "s1", TS, FP_A)
    _settle(step_id="s1", state="completed", result_json='{"envelope": "one"}')
    begin_step("r1", "s2", TS, FP_B)

    rows = {r.step_id: r for r in workflow_journal.get_steps("r1")}
    assert set(rows) == {"s1", "s2"}
    assert rows["s1"].result_json == '{"envelope": "one"}'
    assert rows["s2"].result_json is None  # a running row carries no envelope yet
    assert rows["s1"].state == "completed"
    assert rows["s2"].state == "running"

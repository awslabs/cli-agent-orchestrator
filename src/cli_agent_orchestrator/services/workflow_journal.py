"""Durable run-journal data-access layer (issue #312, Bolt 4 / N6).

A thin, parameterized-SQL data-access module over the ``workflow_run`` /
``workflow_run_step`` tables (clients/database.py ``_migrate_workflow_run*``).
Per Q1=B the journal is the **source of truth** for workflow run execution
state; the Bolt-3 in-memory ``run_registry`` (``RunRecord``) becomes a cache
rebuilt from these rows on a cold read or after a process restart.

Design constraints (functional-design business-logic-model §0/§1, B4-BR-1..5):

- Zero-arg, self-connecting ``sqlite3.connect(str(DATABASE_FILE))`` — mirrors the
  shipped terminals/inbox/workflow_index helpers; no ORM, no session.
- **Parameterized SQL only** — every value binds through ``?`` placeholders, never
  string interpolation (no injection surface; security-design B4-SD-1).
- ``run_id``/``step_id`` are produced + validated by the engine (B3-BR-1, shared
  ``_validate_key_part``) BEFORE they reach this layer; the journal does NOT
  re-validate ad-hoc (project Mandated rule, B4-BR-2).

These helpers raise ``sqlite3.Error`` on a DB failure; the **caller** (the engine
write-through, business-logic-model §1) wraps them best-effort per B4-BR-5 — a
dropped write never raises into the engine drive loop. The read helpers
(``get_run``/``get_steps``) are used by the rebuild + resume read path.

U3 (issue #312, script-tier journal extension, C3) additively extends this
module: ``RunRow.tier``/``RunRow.generation`` and ``StepRow.call_fingerprint``
surface the U3 columns (domain-entities E1/E2/E3) — additive fields only, no
existing field removed/renamed (INV-1). ``append_step``/``lookup_replay``/
``get_step`` are NEW functions; the existing ``insert_run``/``insert_steps``/
``update_step``/``update_run_current_step``/``update_run_state``/``get_run``/
``get_steps`` are otherwise unchanged in behavior (INV-1) — their SELECT lists
grow to surface the additive columns, but a pre-U3/YAML row reads back with the
INV-2 defaults (``tier='yaml'``, ``generation='1'``, ``call_fingerprint=None``),
which is observably identical to the pre-extension shape.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from cli_agent_orchestrator.services.workflow_errors import ReplayDivergenceError

logger = logging.getLogger(__name__)

# Database paths whose journal schema has been VERIFIED PRESENT in THIS process (issue #583).
# Keyed on PATH, not a boolean: five test modules repoint DATABASE_FILE to a temporary
# path mid-process, and a boolean would leave them on a schema-less database (BR-4).
# Holds paths only — never row data, connections or credentials (SR-2).
#
# "VERIFIED PRESENT", NOT "THE MIGRATORS RETURNED" (PR #628 review, Copilot F2). Both
# migrators wrap their whole body in ``except Exception`` -> ``logger.debug`` and therefore
# RETURN NORMALLY when they fail, so "both migrators returned" establishes nothing about the
# schema: a transient lock or a DDL failure would cache a schema-less database for the rest of
# the process and defeat every later retry. ``_connect`` now checks the columns before it
# records the path, which is the only claim this set can honestly make.
_MIGRATED_PATHS: set[str] = set()

# The columns the journal's own SQL reads and writes, per table (PR #628 review). Compared
# against ``PRAGMA table_info`` before a path is cached, because a migrator's silent failure is
# invisible at its return.
#
# ADDING A COLUMN TO EITHER MIGRATOR MEANS WIDENING THE MATCHING SET IN THE SAME CHANGE, and
# ``test_workflow_journal_connection_posture.py`` asserts the two sets against what the
# migrators actually produce on a fresh database — so a column added on one side and not the
# other fails loudly instead of quietly dropping out of the check.
_REQUIRED_RUN_COLUMNS = frozenset(
    {
        "run_id",
        "workflow_name",
        "spec_snapshot",
        "inputs_json",
        "state",
        "current_step_id",
        "started_at",
        "finished_at",
        "tier",
        "generation",
    }
)
_REQUIRED_STEP_COLUMNS = frozenset(
    {
        "run_id",
        "step_id",
        "state",
        "attempts",
        "output_json",
        "error",
        "updated_at",
        "call_fingerprint",
        "terminal_id",
        "reprompted",
        "error_kind",
        "result_json",
    }
)


@dataclass
class RunRow:
    """One ``workflow_run`` row (E1, domain-entities)."""

    run_id: str
    workflow_name: str
    spec_snapshot: str
    inputs_json: str
    state: str
    current_step_id: Optional[str]
    started_at: str
    finished_at: Optional[str]
    tier: str = "yaml"
    generation: str = "1"


@dataclass
class StepRow:
    """One ``workflow_run_step`` row (E2, domain-entities).

    U1 (issue #504) additively extends the row with three nullable fields —
    ``terminal_id``, ``reprompted``, ``error_kind`` — surfacing the additive
    columns added by ``_migrate_workflow_run_step``. All default to ``None`` so
    a pre-U1 row reads back observably identical to its pre-extension shape.
    """

    run_id: str
    step_id: str
    state: str
    attempts: int
    output_json: Optional[str]
    error: Optional[str]
    updated_at: str
    call_fingerprint: Optional[str] = None
    terminal_id: Optional[str] = None
    reprompted: Optional[int] = None
    error_kind: Optional[str] = None
    # issue #583, journal-step-lifecycle (BR-15): the read half of the column
    # ``result-envelope`` (unit 2) added to the schema and deliberately stopped at.
    # Additive and defaulted, so every existing construction site stays valid; both
    # consumers project explicitly by field name (api/main.py, workflow_service.py),
    # so no response body and no rebuilt record changes shape.
    #
    # MERGE NOTE (2026-08-17): ordered AFTER #504's three additive columns. Both
    # changes append to the same SELECT lists and both construct StepRow by
    # POSITIONAL index, so the dataclass order and every SELECT column order must
    # agree. #504 is published on main, so its order is preserved and ``result_json``
    # takes the new last slot — which also honours this field's own append-only rule.
    result_json: Optional[str] = None


@dataclass
class EventRow:
    """One ``workflow_run_event`` row (all 21 columns, domain-entities ADR-1).

    Returned by ``read_events`` / ``read_events_with_gaps``. Append-only and
    immutable once written (BR-5): ``seq`` is the sole ordering authority, ``ts``
    is display/duration only. The optional columns default to ``None`` and
    ``iteration`` / ``which_guard_fired`` are RESERVED (FR-1.5), NULL in the MVP.
    """

    run_id: str
    seq: int
    event_type: str
    event_schema_version: int
    ts: str
    step_id: Optional[str] = None
    attempt: Optional[int] = None
    state: Optional[str] = None
    elapsed_ms: Optional[int] = None
    provider: Optional[str] = None
    agent_profile: Optional[str] = None
    engine: Optional[str] = None
    terminal_id: Optional[str] = None
    terminal_offset_start: Optional[int] = None
    terminal_offset_len: Optional[int] = None
    error_kind: Optional[str] = None
    reason: Optional[str] = None
    validation_result: Optional[str] = None
    output_ref: Optional[str] = None
    iteration: Optional[int] = None
    which_guard_fired: Optional[str] = None


@dataclass
class GapMarker:
    """A declared hole in a run's event sequence (Algorithm 2, BR-4).

    Synthesized at read time by ``read_events_with_gaps`` wherever the stored
    per-run sequence skips one or more values — the mark that lets a client tell
    "nothing happened" from "an event was lost" (FR-3.3). Never stored: the
    sequence is never renumbered to hide a gap.
    """

    after_seq: int
    before_seq: int
    missing_count: int
    reason: str


@dataclass
class RunSummaryRow:
    """A narrow ``workflow_run`` projection for the list view (U1, domain-entities).

    A deliberately narrower sibling of ``RunRow`` over the same table: it holds
    exactly the seven columns a list row renders and omits the large
    ``spec_snapshot`` / ``inputs_json`` payloads (never needed to render a list)
    and the drive-internal ``generation`` counter. Omitting the two large columns
    keeps a multi-row list response small. It is an inert read snapshot with no
    lifecycle of its own — the ``state`` it carries reflects the run lifecycle
    owned elsewhere (U2/U7). Returned by ``list_runs``.
    """

    run_id: str
    workflow_name: str
    state: str
    tier: str
    started_at: str
    finished_at: Optional[str]
    current_step_id: Optional[str]


def _connect() -> sqlite3.Connection:
    """Open a connection to the shared SQLite file (self-connecting, like B2).

    Ensures the ``workflow_run`` / ``workflow_run_step`` tables exist first
    (idempotent ``CREATE TABLE IF NOT EXISTS`` via the shared migrators) so a
    read/write here never races ``init_db()`` — a process that never went
    through the FastAPI lifespan (e.g. a test that instantiates the app
    without entering it as a context manager) still finds its schema.

    Two properties are added by ``journal-connection-posture`` (issue #583,
    NFR-4). The function's name, signature, return type and callers are
    otherwise unchanged (BR-7):

    - **The migrators run at most once per database path per process** (BR-3),
      guarded by :data:`_MIGRATED_PATHS`. The path is read INSIDE this function
      on every call — never captured at import and never cached beside the set
      (BR-5) — so a process that repoints ``DATABASE_FILE`` mid-run still
      migrates the new path on its next call (BR-4). The path is recorded only
      once the REQUIRED SCHEMA IS VERIFIED PRESENT on the new connection (BR-6,
      corrected by PR #628's review — Copilot F2).

      **"Both migrators returned" was the original condition and it was the
      bug.** Each migrator wraps its whole body in ``except Exception`` ->
      ``logger.debug``, so it returns normally when it FAILS: returning is not
      succeeding. One transient lock or DDL failure therefore cached a
      schema-less database for the rest of the process and prevented every
      later retry — the exact outcome the old comment said the ordering
      avoided. The migrators' swallow-and-log posture is deliberately
      UNCHANGED (other callers depend on it, and it is
      ``business-logic-model.md``'s "Error handling" row 1, outside this
      unit); what changed is that this function no longer takes their return
      as evidence. It asks the database instead, with
      :func:`_journal_schema_is_present`.

      A path that fails verification is simply not cached: the connection is
      still returned (the caller's own query is what will fail, with a real
      SQLite error naming the real problem), and the NEXT call re-runs both
      migrators. That is the retry the cache was destroying.
    - **Every connection carries ``busy_timeout``** (BR-1/BR-2). It is a
      per-connection setting rather than a database property, so it cannot be
      memoised alongside the migration state and is set on each new connection.
      At the current value this pragma is a runtime NO-OP: CPython's
      ``sqlite3.connect()`` already applies a 5000 ms busy timeout via its
      ``timeout=5.0`` default, which it implements with
      ``sqlite3_busy_timeout``. It is set explicitly anyway for two reasons —
      the value gets a single named home that can be revised without editing
      this module, and the guarantee survives a future caller passing
      ``timeout=0`` or a change to that stdlib default. It does NOT widen the
      contention window; the per-call cost this function actually removes is
      the migrator DDL above. WAL — which *is* a database-level property,
      shared with every other CAO subsystem using this file — is deliberately
      NOT set here (BR-8, ADR-583-10).

    The timeout is interpolated from the module-level constant and from nothing
    else (SR-1): SQLite accepts no bound parameter for ``PRAGMA busy_timeout``,
    so this is this module's one interpolated statement and its source must
    stay a trusted constant.
    """
    from cli_agent_orchestrator.clients.database import (
        _migrate_workflow_run,
        _migrate_workflow_run_step,
    )
    from cli_agent_orchestrator.constants import (
        DATABASE_FILE,
        WORKFLOW_JOURNAL_BUSY_TIMEOUT_MS,
    )

    path = str(DATABASE_FILE)  # read at call time, never cached (BR-5)
    needs_migration = path not in _MIGRATED_PATHS
    if needs_migration:
        _migrate_workflow_run()
        _migrate_workflow_run_step()
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA busy_timeout = {WORKFLOW_JOURNAL_BUSY_TIMEOUT_MS}")
    if needs_migration:
        # BR-6, corrected: cache the path only once the schema is VERIFIED, never merely
        # because the two silently-failing migrators returned. Verified on the connection this
        # call is about to hand back, so the check and the caller see the same database.
        if _journal_schema_is_present(conn):
            _MIGRATED_PATHS.add(path)
        else:
            # WARNING, not debug: this is the state in which every subsequent journal write
            # fails, and the migrators have already logged their own cause at debug. The path
            # only — never row data (SR-2). Repeats per connection until it is fixed, which is
            # the intended volume for "the journal has no schema".
            logger.warning(
                "journal: required schema absent after migration for '%s'; "
                "not caching the path so the next connection retries",
                path,
            )
    return conn


def _journal_schema_is_present(conn: sqlite3.Connection) -> bool:
    """Do both journal tables carry every column this module's SQL uses?

    The verification :func:`_connect` needs and the migrators cannot provide: they swallow and
    log their own failures, so their return says nothing about the schema (PR #628 review).

    TOTAL — never raises, so it cannot add a failure mode to ``_connect`` that ``_connect``
    did not already have. A ``PRAGMA table_info`` on a missing table returns no rows rather
    than raising, which already answers ``False``; a corrupt or unreadable database raises
    ``sqlite3.Error`` and is ALSO ``False``, because "we could not establish that the schema is
    there" and "the schema is not there" call for the same decision here — do not cache. The
    caller's own query then fails with the real error, which is a better diagnostic than
    anything this predicate could invent.

    Reads only ``PRAGMA table_info`` — no row data is touched, so nothing here can log or
    return step content (SR-2).
    """
    try:
        for table, required in (
            ("workflow_run", _REQUIRED_RUN_COLUMNS),
            ("workflow_run_step", _REQUIRED_STEP_COLUMNS),
        ):
            # The table name is interpolated from a module-level literal pair and from nothing
            # else (SR-1) — SQLite accepts no bound parameter for an identifier, the same
            # constraint that makes the busy_timeout pragma above an interpolation.
            present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if not required <= present:
                return False
    except sqlite3.Error:
        return False
    return True


# ---------------------------------------------------------------------------
# Writes (engine write-through, business-logic-model §1). Each is one short
# transaction; the ``with conn`` context commits on success / rolls back on error.
# ---------------------------------------------------------------------------
def insert_run(
    run_id: str,
    workflow_name: str,
    spec_snapshot: str,
    inputs_json: str,
    state: str,
    started_at: str,
    tier: str = "yaml",
    generation: str = "1",
) -> None:
    """INSERT the ``workflow_run`` row at ``start_run`` (lifecycle table, E1).

    A plain ``INSERT``: a re-INSERT for an already-journaled ``run_id`` raises
    ``sqlite3.IntegrityError`` rather than silently overwriting the durable row
    (a resume never calls this — it only UPDATEs). The engine both pre-checks the
    journal in ``start_run`` and wraps this call best-effort, so a lost race
    logs instead of clobbering history.

    U4 addition (issue #312, script-tier runner, C1): optional ``tier`` /
    ``generation`` kwargs (additive, INV-1 — YAML callers are byte-identical and
    default to ``tier='yaml'``/``generation='1'``, the migration defaults). A
    script run passes ``tier='script'`` in ONE write so a script row is never
    journaled with a transient ``tier='yaml'`` window that would break tier
    dispatch / resumability (code-generation-plan CONTRADICTION #4).
    """
    with _connect() as conn:
        conn.execute(
            "INSERT INTO workflow_run "
            "(run_id, workflow_name, spec_snapshot, inputs_json, state, "
            " current_step_id, started_at, finished_at, tier, generation) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?)",
            (
                run_id,
                workflow_name,
                spec_snapshot,
                inputs_json,
                state,
                started_at,
                tier,
                generation,
            ),
        )


def insert_run_with_steps(
    run_id: str,
    workflow_name: str,
    spec_snapshot: str,
    inputs_json: str,
    state: str,
    started_at: str,
    steps: Sequence[Tuple[str, str]],
    updated_at: str,
    tier: str = "yaml",
    generation: str = "1",
) -> None:
    """Atomically INSERT the run row AND seed its step rows in ONE transaction (U2, TR-1).

    The async submission path (``POST /workflows/runs:submit``) needs the run
    row and its seeded step rows to be durable **together** before it acks a run
    with 202 (the ``run-id-allocated-before-ack`` invariant). Calling
    :func:`insert_run` then :func:`insert_steps` back-to-back is NOT atomic — each
    self-connects and commits independently, so a failure of the second commit
    would leave a committed ``workflow_run`` row with no step rows: a phantom
    RUNNING run that ``list_runs`` / ``get_run_status`` report forever with no
    background task to terminate it.

    This helper opens ONE ``_connect()`` connection and does both INSERTs inside a
    SINGLE ``with conn:`` transaction (one commit). If EITHER statement raises a
    ``sqlite3.Error`` (e.g. the step seed violates a constraint after the run row
    INSERT), the ``with conn`` block rolls the whole transaction back — NEITHER row
    is committed — and the error **propagates** to the caller. Unlike the engine's
    best-effort write-through (:func:`~workflow_service._journal_insert_run`, which
    swallows), this insert is a HARD precondition of the async ack, so its failure
    is surfaced (the caller maps it to 500 and emits NO 202), never swallowed.

    ``insert_run`` / ``insert_steps`` are deliberately left unchanged — the
    blocking engines still call them (INV-1). This is a NEW additive sibling that
    composes the same two INSERTs into one transaction for the async path.
    """
    with _connect() as conn:
        conn.execute(
            "INSERT INTO workflow_run "
            "(run_id, workflow_name, spec_snapshot, inputs_json, state, "
            " current_step_id, started_at, finished_at, tier, generation) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?)",
            (
                run_id,
                workflow_name,
                spec_snapshot,
                inputs_json,
                state,
                started_at,
                tier,
                generation,
            ),
        )
        conn.executemany(
            "INSERT OR REPLACE INTO workflow_run_step "
            "(run_id, step_id, state, attempts, output_json, error, updated_at) "
            "VALUES (?, ?, ?, 0, NULL, NULL, ?)",
            [(run_id, step_id, step_state, updated_at) for step_id, step_state in steps],
        )


def insert_steps(run_id: str, steps: Sequence[Tuple[str, str]], updated_at: str) -> None:
    """INSERT one ``workflow_run_step`` row per ``(step_id, state)`` (E2).

    Called once at ``start_run`` to seed every spec step (typically ``pending``).
    ``INSERT OR REPLACE`` so a re-seed is idempotent.
    """
    with _connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO workflow_run_step "
            "(run_id, step_id, state, attempts, output_json, error, updated_at) "
            "VALUES (?, ?, ?, 0, NULL, NULL, ?)",
            [(run_id, step_id, state, updated_at) for step_id, state in steps],
        )


def update_step(
    run_id: str,
    step_id: str,
    state: str,
    attempts: int,
    updated_at: str,
    output_json: Optional[str] = None,
    error: Optional[str] = None,
    error_kind: Optional[str] = None,
) -> None:
    """UPDATE a step's durable state/attempts/output/error (lifecycle table, E2).

    U2 (issue #504) adds the optional ``error_kind`` param, projected into the
    additive ``workflow_run_step.error_kind`` column (U1) on a failure transition
    so a post-restart cold read surfaces the structured error kind without event
    replay (BR-6). Additive and backward-compatible: ``error_kind=None`` (the
    default, taken by every non-failure and every pre-U2 caller) writes NULL,
    leaving the pre-U2 behavior byte-identical.
    """
    with _connect() as conn:
        conn.execute(
            "UPDATE workflow_run_step "
            "SET state = ?, attempts = ?, output_json = ?, error = ?, "
            "error_kind = ?, updated_at = ? "
            "WHERE run_id = ? AND step_id = ?",
            (state, attempts, output_json, error, error_kind, updated_at, run_id, step_id),
        )


def update_run_current_step(run_id: str, current_step_id: Optional[str]) -> None:
    """UPDATE ``workflow_run.current_step_id`` (FR-6.4 "which step is live")."""
    with _connect() as conn:
        conn.execute(
            "UPDATE workflow_run SET current_step_id = ? WHERE run_id = ?",
            (current_step_id, run_id),
        )


def update_run_state(run_id: str, state: str, finished_at: Optional[str]) -> None:
    """UPDATE ``workflow_run.state`` (+ ``finished_at``) on a run transition (E1).

    ``finished_at`` is set on a terminal transition and cleared (``None``) when a
    resume re-opens a previously-settled run (business-logic-model §3).

    UNCONDITIONAL BY CONTRACT. Do NOT add a ``WHERE state = ...`` predicate here:
    the resume path calls this to write state BACK to ``running`` on an already
    terminal row (``script_runner.resume_script_run`` and
    ``workflow_service.resume_from_last_completed``), so any "only if still
    running" guard would silently turn every resume into a no-op. A caller that
    needs the guarded write wants ``settle_run_state_if_running`` below.
    """
    with _connect() as conn:
        conn.execute(
            "UPDATE workflow_run SET state = ?, finished_at = ? WHERE run_id = ?",
            (state, finished_at, run_id),
        )


def settle_run_state_if_running(run_id: str, state: str, finished_at: Optional[str]) -> bool:
    """Settle a run's state ONLY while the row is still ``running``; report whether it did.

    The conditional sibling of ``update_run_state``, added for the background
    drive's FAILED backstop (issue #505 review). The backstop exists so a
    scheduling bug cannot orphan a run in ``running`` forever — but written
    unconditionally it also overwrites a run the engine ALREADY settled, so a
    drive that raised during post-settlement bookkeeping turned a true
    ``completed``/``cancelled`` into a false ``failed``. That is worse than the
    hole it was closing: the journal row is the durable record of what actually
    happened, and a wrong terminal state is indistinguishable from a real one.

    The state test lives in the SQL (``AND state = 'running'``) rather than in a
    read-then-write on the caller's side, so the check and the write are one
    atomic statement and no concurrent settle can land between them.

    Returns ``True`` when a row was updated and ``False`` when the row was
    already terminal (or absent) — the caller logs the distinction, because a
    silent no-op is indistinguishable from a broken guard when reading logs
    after an incident.
    """
    from cli_agent_orchestrator.models.workflow_runtime import RunState

    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE workflow_run SET state = ?, finished_at = ? " "WHERE run_id = ? AND state = ?",
            (state, finished_at, run_id, RunState.RUNNING.value),
        )
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# recovery-decision-intake additions (issue #583, unit 12) — FR-7's escape hatch.
# A human's decision at a halted step, carried into a resume as a STATE TRANSITION
# on the existing row: no schema change, and the run's evidence survives the
# decision instead of being deleted by it (BR-8, ADR-583-8).
# ---------------------------------------------------------------------------
# The decision -> state map. BARE STRING LITERALS on both sides, matching how this
# module already spells state values (``lookup_replay``, ``begin_step``,
# ``settle_run_state_if_running``) — the module takes no module-level ``models``
# dependency for a vocabulary it only writes (unit 6's BR-12/TD-4 precedent).
# ``test_recovery_decision_intake.py`` pins the KEYS against ``RecoveryDecision``
# and the VALUES against ``StepState`` from its own imports, so a rename or a third
# member on either side fails loudly instead of drifting.
DECISION_STATES: Dict[str, str] = {
    "rerun": "rerun_authorized",  # -> gate rule 1 -> EXECUTE (BR-2)
    "skip": "replay_authorized",  # -> gate rules 7/9 excluded -> catch-all -> REPLAY (BR-3)
}
# Kept as a compatibility alias for existing tests that pin the original name.
_DECISION_STATES = DECISION_STATES


def apply_decisions(run_id: str, decisions: Mapping[str, str]) -> Dict[str, str]:
    """Apply a human's per-step recovery decisions to one run's rows (FR-7, BR-1..BR-10).

    The escape hatch for a halt: the replay gate can return ``DECISION_REQUIRED``,
    and this is the only thing that resolves one. Called on the resume path BEFORE
    the script is spawned (BR-7) — the gate reads journal rows, so a decision applied
    after the spawn would be invisible to the step it was meant to resolve.

    **THE ORDER OF OPERATIONS IS THE REQUIREMENT, NOT AN IMPLEMENTATION CHOICE**
    (SR-2/SR-3/SR-6, RL-1). Read, then validate the WHOLE map, then write inside ONE
    transaction, then log:

    1. read this run's existing step rows (:func:`get_steps`);
    2. validate EVERY entry — nothing is written yet;
    3. one ``with _connect() as conn:`` block, N single-row UPDATEs of ``state``;
    4. AFTER the commit, one warning per decision.

    **Why validate-then-write rather than iterate-and-write** (SR-2, the one threat
    this unit introduces). This function takes a MAP, so a typo in the third entry
    would otherwise leave the first two already transitioned while the operator sees
    their resume REJECTED — ``step-a`` would hold durable consent to re-execute a
    side-effecting step, granted by a command that reported failure. BR-9 makes
    consent one-shot so it cannot outlive its ATTEMPT; this ordering stops it
    outliving its own REJECTION.

    **Why one transaction as WELL as up-front validation** (SR-3/TD-4). The two guard
    different failures and neither is redundant: validation catches operator error at
    the boundary, the transaction catches a database failure part-way through the
    writes. ``with _connect() as conn:`` commits on clean exit and rolls back on an
    exception, which is sufficient — deliberately NOT the ``BEGIN IMMEDIATE`` unit
    6's TD-2a considered, because this is a single-writer, human-initiated path and
    not a two-process race (concurrent resumes of the SAME run are rejected upstream
    with 409 before this function is reached, SC-3).

    **Only ``state`` moves** (BR-8/SR-9/RL-4). ``attempts``, ``result_json``,
    ``output_json``, ``error``, ``call_fingerprint`` and ``updated_at`` are untouched,
    so the record of what actually happened outlives the decision about what to do
    next — which is what makes a halt diagnosable afterwards (FR-12).

    **It never silently no-ops** (BR-6/RL-2/INV-3). An unknown ``step_id`` or an
    unknown decision value raises, because a swallowed typo would let the run halt
    again at the same step with no signal that the decision never landed — and the
    operator would re-issue the same typo indefinitely, concluding the halt mechanism
    is broken. An EMPTY map is not that case: no decision was supplied, so it returns
    without reading, writing or logging.

    **The log line is the one place this module logs, and its position is a rule**
    (SR-6/TD-6). Its neighbours deliberately leave logging to the caller — a caller
    knows more about a no-op than the primitive does. This function is different: it
    is the subsystem's only permission grant, so the grant itself is the evidence and
    only this function holds it. The line goes AFTER the commit and on the success
    path ONLY: logging before the commit would record a decision that then rolled
    back, and a log claiming consent was granted when it was not is worse than no log
    at all. A rejection needs no line — it already surfaces as a 400 to the operator
    who caused it. Identifiers only: ``run_id``, ``step_id``, the decision and the
    state it wrote — never a fingerprint, an envelope or any step content (SR-5).

    A durable, queryable record of *which operator authorised this, and when* is a
    known gap: neither existing log fits (``event_log_service`` is an in-process ring
    buffer; ``audit_log`` is the MEMORY audit log and short-circuits when memory is
    disabled), and a durable workflow event log is the parked #505 work. Recorded as
    an accepted residual in this unit's ``security-requirements.md`` rather than
    papered over; when #505 lands, this line is where the decision joins the timeline.

    **IT RETURNS THE PRIOR STATES, so consent can be revoked (PR #628 review, Copilot
    F6).** The return value is new and additive — every existing caller ignores it. It
    exists because BR-9's "one decision authorises exactly ONE attempt" was only HALF
    implemented: ``begin_step`` consumes ``rerun_authorized`` by flipping the row to
    ``running``, but NOTHING consumes ``replay_authorized`` (a replay writes no row, by
    design), and a resume that fails after this commit leaves either state live for the
    next resume to consume. Reverting needs the state that was overwritten, and this
    function is the only place that still knows it. :func:`revoke_unconsumed_decisions`
    is the other half; ``script_runner.resume_script_run`` holds the pair together.

    Args:
        run_id: the run being resumed. Its rows are the only ones touched.
        decisions: ``step_id`` -> decision, where a value is a ``RecoveryDecision``
            member or the equivalent string. Annotated ``str`` because
            ``RecoveryDecision`` IS a ``(str, Enum)`` and the boundary hands raw JSON
            strings: :func:`parse_decision` is the single validation point for both
            forms (BR-10), so no surface can accept a value another rejects.

    Returns:
        ``step_id`` -> the state each row held BEFORE this call, for exactly the steps
        written. Empty for an empty ``decisions`` map. Hand it to
        :func:`revoke_unconsumed_decisions` when the drive this consent was granted for
        is over.

    Raises:
        ValueError: if any ``step_id`` is absent from this run, or any value is not a
            recovery decision. The message names the offending ``step_id`` and
            carries identifiers only (SR-4/SR-5). Raised BEFORE any write, so the
            rows are untouched; the resume route's existing bare-``ValueError`` arm
            maps it to 400 — correct, because a mistyped ``step_id`` is a client
            error and a 500 would tell the operator to file a bug instead of fixing a
            typo.
        sqlite3.Error: propagated unchanged from the read or the transaction, like
            every other helper here (BR-3's posture). A failed transaction has
            written nothing and produced no log line.
    """
    # Function-local import of the LIGHT models module, exactly as
    # ``settle_run_state_if_running`` imports ``RunState``: no module-level ``models``
    # edge, and no import cost for the many callers that never decide anything.
    from cli_agent_orchestrator.models.workflow_runtime import parse_decision

    if not decisions:
        return {}  # no decision supplied is not a decision that failed

    # 1. The run's own rows are the bound on N (SC-1): a caller cannot enlarge the
    # write set by inventing ids, because step 2 rejects the whole map instead.
    # The states come off the SAME read (PR #628 review): the prior state is what makes
    # the grant revocable, and this is the last moment anything knows it.
    prior_states = {row.step_id: row.state for row in get_steps(run_id)}
    known = set(prior_states)

    # 2. VALIDATE THE WHOLE MAP FIRST. Nothing below this loop writes, and nothing
    # above it does either — the resolved states are collected and only then applied.
    resolved: Dict[str, Tuple[str, str]] = {}  # step_id -> (decision value, new state)
    for step_id, value in decisions.items():
        if step_id not in known:
            raise ValueError(f"run '{run_id}' has no step '{step_id}'; no decision was applied")
        try:
            decision = parse_decision(value)
        except ValueError as e:
            raise ValueError(f"step '{step_id}': {e}; no decision was applied") from e
        resolved[step_id] = (decision.value, _DECISION_STATES[decision.value])

    # 3. ONE transaction. ``state`` and nothing else (BR-8).
    with _connect() as conn:
        for step_id, (_decision, state) in resolved.items():
            conn.execute(
                "UPDATE workflow_run_step SET state = ? WHERE run_id = ? AND step_id = ?",
                (state, run_id, step_id),
            )

    # 4. AFTER the commit, success path only (SR-6). Identifiers only (SR-5).
    for step_id, (decision_value, state) in resolved.items():
        logger.warning(
            "journal: run '%s' step '%s': recovery decision '%s' applied (state -> %s)",
            run_id,
            step_id,
            decision_value,
            state,
        )

    # Only the steps actually written, so a caller cannot revoke a row this call did not
    # touch even by handing the whole map back.
    return {step_id: prior_states[step_id] for step_id in resolved}


def revoke_unconsumed_decisions(run_id: str, prior_states: Mapping[str, str]) -> List[str]:
    """Take back any recovery consent the finished drive did not consume (PR #628 review, F6).

    THE MISSING HALF OF BR-9's "one decision authorises exactly ONE attempt". The other half
    already worked for one case: ``begin_step`` flips a ``rerun_authorized`` row to ``running``,
    so a dispatched rerun cannot be re-authorised by the same decision. Three cases were not
    covered, and the third is not a crash case at all:

    1. the resume FAILS after :func:`apply_decisions` commits — a raising generation bump or
       snapshot materialisation — and the consent survives a command that reported failure;
    2. the drive runs but never reaches the decided step (an earlier step halts), so nothing
       dispatches it and ``rerun_authorized`` stands;
    3. a ``skip`` is NEVER consumed by anything, on any path, because a replay writes no row
       BY DESIGN. One ``skip`` was therefore standing authorisation for every future resume of
       that run — which is precisely what the CLI, the MCP tool, ``docs/workflows.md``, the
       authoring guide and ``SKILL.md`` all promise it is not.

    A COMPARE-AND-SET, NEVER A BLIND WRITE. Each ``UPDATE`` carries
    ``AND state = <the authorised value>``, so a row the drive DID move — to ``running``,
    ``completed``, ``failed``, or anything else — is not matched and cannot be clobbered. That
    is what makes this safe to call unconditionally at the end of every decided resume, and it
    is why the caller does not have to work out which steps were consumed: the database
    answers, atomically, from the state itself.

    ONE transaction, ``state`` and nothing else — the same posture as
    :func:`apply_decisions`, and for the same reason (BR-8): revoking consent must destroy no
    evidence of what actually happened.

    Args:
        run_id: the run whose consent is being revoked.
        prior_states: ``step_id`` -> the state the row held before the decision, exactly as
            :func:`apply_decisions` returned it. A step absent from this map is untouched.

    Returns:
        The ``step_id``s actually reverted, in the map's iteration order — i.e. the consents
        that were NOT consumed. Empty when the drive consumed everything it was granted.

    Raises:
        sqlite3.Error: propagated unchanged, like every other helper here. The caller treats a
            failure as best-effort: it must not mask the drive's own outcome, and the next
            resume's gate still halts on anything it cannot verify.
    """
    if not prior_states:
        return []

    authorised = set(DECISION_STATES.values())
    revoked: List[str] = []
    with _connect() as conn:
        for step_id, prior in prior_states.items():
            if prior in authorised:
                # Refuse to "restore" a row to an authorised state — that would re-grant the
                # consent this function exists to take back. Only reachable if a caller hands
                # back a map from a nested/duplicated apply, which no shipped path does.
                logger.warning(
                    "journal: run '%s' step '%s': prior state is itself an authorisation; "
                    "not revoking",
                    run_id,
                    step_id,
                )
                continue
            for state in authorised:
                cursor = conn.execute(
                    "UPDATE workflow_run_step SET state = ? "
                    "WHERE run_id = ? AND step_id = ? AND state = ?",
                    (prior, run_id, step_id, state),
                )
                if cursor.rowcount:
                    revoked.append(step_id)
                    break

    # AFTER the commit, and only for what was really taken back — the counterpart of
    # ``apply_decisions``' grant line, so the two halves of one consent appear in one log at
    # the same level. Identifiers and states only (SR-5).
    for step_id in revoked:
        logger.warning(
            "journal: run '%s' step '%s': recovery consent was not consumed by this drive "
            "and has been revoked (state -> %s)",
            run_id,
            step_id,
            prior_states[step_id],
        )
    return revoked


# ---------------------------------------------------------------------------
# Reads (rebuild + resume read path, business-logic-model §2/§3).
# ---------------------------------------------------------------------------
def get_run(run_id: str) -> Optional[RunRow]:
    """Return the ``workflow_run`` row for ``run_id``, or ``None`` if absent (E1).

    ``None`` on absent is load-bearing: the rebuild returns ``None`` so
    ``get_run_status`` raises ``KeyError`` -> 404 (F1, contract unchanged).
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT run_id, workflow_name, spec_snapshot, inputs_json, state, "
            "current_step_id, started_at, finished_at, tier, generation "
            "FROM workflow_run WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    if row is None:
        return None
    return RunRow(
        run_id=row[0],
        workflow_name=row[1],
        spec_snapshot=row[2],
        inputs_json=row[3],
        state=row[4],
        current_step_id=row[5],
        started_at=row[6],
        finished_at=row[7],
        tier=row[8],
        generation=row[9],
    )


def get_steps(run_id: str) -> List[StepRow]:
    """Return all ``workflow_run_step`` rows for ``run_id`` (E2).

    U1 (issue #504) grows the SELECT list to surface the additive
    ``terminal_id`` / ``reprompted`` / ``error_kind`` columns; a pre-U1 row reads
    them back as ``None`` (behavior otherwise unchanged, SEAM #1). Issue #583's
    ``journal-step-lifecycle`` (BR-15) then appends ``result_json`` so the envelope
    ``settle_step`` writes is readable. The column order is append-only because the
    construction below indexes positionally.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT run_id, step_id, state, attempts, output_json, error, updated_at, "
            "call_fingerprint, terminal_id, reprompted, error_kind, result_json "
            "FROM workflow_run_step WHERE run_id = ?",
            (run_id,),
        ).fetchall()
    return [
        StepRow(
            run_id=r[0],
            step_id=r[1],
            state=r[2],
            attempts=r[3],
            output_json=r[4],
            error=r[5],
            updated_at=r[6],
            call_fingerprint=r[7],
            terminal_id=r[8],
            reprompted=r[9],
            error_kind=r[10],
            result_json=r[11],
        )
        for r in rows
    ]


def get_step(run_id: str, step_id: str) -> Optional[StepRow]:
    """Return the single ``workflow_run_step`` row for ``(run_id, step_id)`` (E2).

    U3 addition: the read primitive ``lookup_replay`` (A2) is built on. Returns
    ``None`` when the row is absent — a script call that has never arrived.

    U1 (issue #504) grows the SELECT list to surface the additive
    ``terminal_id`` / ``reprompted`` / ``error_kind`` columns (``None`` on a
    pre-U1 row); behavior is otherwise unchanged. Issue #583's
    ``journal-step-lifecycle`` (BR-15) then appends ``result_json``: without it
    ``settle_step`` would write a column no reader returns, and the replay gate
    could not reject a settled row for an absent envelope it has no way to see
    (FR-4 guard 2, unit 7). The column order is append-only because the
    construction below indexes positionally.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT run_id, step_id, state, attempts, output_json, error, updated_at, "
            "call_fingerprint, terminal_id, reprompted, error_kind, result_json "
            "FROM workflow_run_step WHERE run_id = ? AND step_id = ?",
            (run_id, step_id),
        ).fetchone()
    if row is None:
        return None
    return StepRow(
        run_id=row[0],
        step_id=row[1],
        state=row[2],
        attempts=row[3],
        output_json=row[4],
        error=row[5],
        updated_at=row[6],
        call_fingerprint=row[7],
        terminal_id=row[8],
        reprompted=row[9],
        error_kind=row[10],
        result_json=row[11],
    )


def list_runs(
    state: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> List[RunSummaryRow]:
    """List ``workflow_run`` rows newest-first as narrow summaries (U1, FR-3.1).

    One parameterized SELECT over the ``workflow_run`` table returning a list of
    :class:`RunSummaryRow`. The projection is narrow (seven columns; no
    ``spec_snapshot`` / ``inputs_json``) so a multi-row list stays small.

    - ``state`` — when not ``None``, a ``WHERE state = ?`` clause filters to that
      one RunState string. Legality of the value is validated one layer up; a
      well-formed but unmatched string simply returns ``[]`` (QR-2, LR-2). The
      value binds through a ``?`` placeholder — never string-interpolated, so a
      value carrying SQL metacharacters is a harmless literal (QR-1).
    - ``limit`` — clamped to ``[1, 500]`` (values ``< 1`` become ``1``, values
      ``> 500`` become ``500``); ``offset`` is floored at ``0``. The clamp bounds
      a single list response regardless of the caller.
    - Ordering is ``started_at DESC, run_id DESC``. The ``run_id DESC`` tiebreaker
      is mandatory, not decoration: ``started_at`` is a whole-second ISO string,
      so two runs started in the same second collide on the primary key; without
      the tiebreaker their order — and offset paging — would be undefined (QR-3).

    An empty result (empty table or a filter that matches nothing) is a valid
    answer returned as ``[]``, never an error (LR-3). Like the sibling reads
    (``get_run`` / ``get_steps``) this raises ``sqlite3.Error`` on a DB failure
    rather than swallowing it — a silently empty list would hide a broken
    database from a human who explicitly asked to list runs (ER-1).
    """
    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    sql = (
        "SELECT run_id, workflow_name, state, tier, started_at, "
        "finished_at, current_step_id FROM workflow_run"
    )
    params: List[object] = []
    if state is not None:
        sql += " WHERE state = ?"
        params.append(state)
    sql += " ORDER BY started_at DESC, run_id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with _connect() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [
        RunSummaryRow(
            run_id=r[0],
            workflow_name=r[1],
            state=r[2],
            tier=r[3],
            started_at=r[4],
            finished_at=r[5],
            current_step_id=r[6],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# U3 additions (issue #312, script-tier journal extension, C3) — additive only,
# INV-1: no existing helper above is modified.
# ---------------------------------------------------------------------------
def append_step(
    run_id: str,
    step_id: str,
    state: str,
    updated_at: str,
    call_fingerprint: str,
) -> None:
    """Write-through append for a script call (A1, business-logic-model §A1).

    Called at the RUNNING insert for a script call — ``call_fingerprint`` is
    known BEFORE execution (``sha256(provider || agent || prompt)``, ADR-5) so a
    future caller of the reserved ``lookup_replay`` primitive has a stable value
    to compare. The
    completion transition (RUNNING -> COMPLETED/FAILED) reuses the base
    ``update_step`` UNCHANGED (INV-1).

    ``ON CONFLICT ... DO UPDATE`` upserts ``state``/``updated_at`` only — a
    re-executed tail step (e.g. a second resume attempt over the same call)
    already has a prior-attempt row; this is NOT a swallowed IntegrityError, it
    is the documented A1 upsert. ``call_fingerprint`` is deliberately excluded
    from the ``DO UPDATE`` clause so it stays stable across attempts (VR-4) —
    the fingerprint recorded at the FIRST arrival of this ``(run_id, step_id)``
    is the one ``lookup_replay`` compares against on every subsequent attempt.

    **This function is no longer the only writer of ``call_fingerprint``**
    (corrected by ``journal-step-lifecycle``, issue #583, BR-11 — the VR-4
    exclusivity claim this docstring used to make became false the moment
    :func:`begin_step` landed beside it, and is removed here rather than left to
    contradict the code in the same module). The two writers own two regimes,
    and the split is the point:

    - **this function — the YAML tier**: the fingerprint is fixed at the FIRST
      arrival of a ``(run_id, step_id)`` and never moves, because the column is
      excluded from the ``DO UPDATE`` above.
    - **:func:`begin_step` — the script tier**: the fingerprint is RE-BASELINED
      on conflict whenever the prior row is not ``completed`` (BR-7), so a step
      re-dispatched after a failure or a human rerun decision records the
      fingerprint of the call actually about to run. A ``completed`` row's
      fingerprint is preserved, which is the same stability this function
      provides unconditionally.
    """
    with _connect() as conn:
        conn.execute(
            "INSERT INTO workflow_run_step "
            "(run_id, step_id, state, attempts, output_json, error, updated_at, "
            " call_fingerprint) "
            "VALUES (?, ?, ?, 0, NULL, NULL, ?, ?) "
            "ON CONFLICT(run_id, step_id) DO UPDATE SET "
            "state = excluded.state, updated_at = excluded.updated_at",
            (run_id, step_id, state, updated_at, call_fingerprint),
        )


# ---------------------------------------------------------------------------
# journal-step-lifecycle additions (issue #583, unit 6) — the script tier's
# single durable write split into a BEGIN and a SETTLE, so that a settled row and
# an absent result can no longer coexist (FR-4 guard 1, ADR-583-4). Additive:
# every helper above keeps its signature, behaviour and callers (BR-10), and
# nothing in production calls either function until ``settlement-rewire``
# (unit 8) rewires ``record_step_completion``.
# ---------------------------------------------------------------------------
def begin_step(run_id: str, step_id: str, updated_at: str, call_fingerprint: str) -> None:
    """Write the durable RUNNING row for a script call, carrying its fingerprint (A).

    The first half of the split write. Called at terminal creation, BEFORE the
    call executes, so a crash in the execution window leaves the row ``running``
    — a state ``lookup_replay``'s existing partial guard already rejects — rather
    than ``completed`` with no result.

    ``attempts`` is absent from the ``DO UPDATE`` clause (BR-8). This function
    never moves the count; :func:`settle_step` owns it. A second ``begin_step``
    on a step already settled three times must not reset it to 0.

    **Why ``call_fingerprint`` is written conditionally.** :func:`append_step`
    excludes the column from its conflict clause outright, which fixes the
    fingerprint at first arrival forever. That is right for the YAML tier and
    wrong for the script tier: a step re-dispatched after a failure — or after a
    human authorises a rerun — would keep a fingerprint recorded against a call
    that is not the one about to run (possibly under a superseded hashing
    scheme), and every later resume would halt again on the same step. The
    ``CASE`` therefore re-baselines the fingerprint while the prior row is not
    terminal, and preserves it once the row is ``completed`` — a completed row's
    fingerprint is the one recorded against a real result, which is exactly what
    ``lookup_replay`` must keep comparing against (BR-7, INV-3).

    **The condition is an open negative — ``!= 'completed'`` — and must stay
    one.** Rewriting it as an allowlist of the states known today would silently
    stop re-baselining the moment ``recovery-decision-intake`` (unit 12) adds
    ``rerun_authorized``, reinstating the permanent-halt bug the rule exists to
    kill. A test pins that an arbitrary unknown state value still re-baselines.

    ``'running'`` is a bare string literal, matching how this module already
    spells state values (``lookup_replay``, ``settle_run_state_if_running``);
    the module gains no ``models`` dependency for one literal (BR-12, TD-4). A
    test pins the literal equal to ``StepState.RUNNING.value`` from the test
    file's own import, so a rename on either side fails loudly.

    Raises ``sqlite3.Error`` on a DB failure and swallows nothing (BR-3): the
    best-effort posture belongs to the caller (``record_step_completion``), so a
    journal failure degrades resumability and never fails a running step (INV-4).
    """
    with _connect() as conn:
        conn.execute(
            "INSERT INTO workflow_run_step "
            "(run_id, step_id, state, attempts, output_json, error, updated_at, "
            " call_fingerprint) "
            "VALUES (?, ?, 'running', 0, NULL, NULL, ?, ?) "
            "ON CONFLICT(run_id, step_id) DO UPDATE SET "
            "state = excluded.state, "
            "updated_at = excluded.updated_at, "
            "call_fingerprint = CASE "
            "WHEN workflow_run_step.state != 'completed' "
            "THEN excluded.call_fingerprint "
            "ELSE workflow_run_step.call_fingerprint END",
            (run_id, step_id, updated_at, call_fingerprint),
        )


def settle_step(
    run_id: str,
    step_id: str,
    state: str,
    updated_at: str,
    result_json: Optional[str],
    output_json: Optional[str],
    error: Optional[str],
) -> bool:
    """Settle a script call's row — state, count, envelope, output, error — atomically (B).

    The second half of the split write, and the whole of FR-4 guard 1: ``state``,
    ``attempts``, ``result_json``, ``output_json`` and ``error`` land in ONE
    statement on ONE connection, so there is no window in which the row reads
    settled and carries no result (BR-1, INV-1). A failure writes nothing and
    leaves the row as :func:`begin_step` set it — ``running``, which is not
    settled (BR-2). Atomic means never half-written, not never-failing.

    Returns ``True`` when a row already existed and ``False`` when this settle
    created it — the no-begin rescue path below. The caller logs that
    distinction (BR-13, TD-2); this function emits no log line, exactly as
    :func:`settle_run_state_if_running` leaves its own no-op to the caller,
    "because a silent no-op is indistinguishable from a broken guard when
    reading logs after an incident". A single ``INSERT ... ON CONFLICT DO
    UPDATE`` cannot report which branch it took (``cursor.rowcount`` is 1 either
    way), so the branch is detected by a primary-key ``SELECT`` on the same
    connection, immediately before the upsert. ``RETURNING`` would detect it in
    one statement but appears nowhere in ``src/`` and needs SQLite 3.35+, which
    is not verified at the project's CI floor (TD-2).

    **The scope of that detection, stated precisely rather than overclaimed.**
    The ``SELECT`` and the upsert share one connection and one ``with`` block,
    but NOT one transaction: with the stdlib's legacy transaction control
    (``isolation_level=""``) an implicit ``BEGIN`` fires at the first DML
    statement, so a plain ``SELECT`` runs in autocommit and opens no read
    transaction — verified on CPython 3.12.9 / SQLite 3.47.1, where a second
    connection can insert the same key between the two statements without
    blocking. So the returned ``bool`` is an accurate report of what this process
    saw a moment before its write, and it can under-report (``False`` while
    another process created the row in that gap, leaving this statement to take
    the conflict path). It never mis-reports ``True``: nothing in ``src/`` deletes
    a ``workflow_run_step`` row.

    That residual is bounded and deliberate. **BR-1 does not depend on it** — the
    settle itself is one statement and stays atomic whatever the read said — and
    the value feeds a diagnostic warning, not a control-flow decision, so the
    worst case is one missing log line in a race between two processes settling
    the SAME step. Making it exact would need an explicit ``BEGIN IMMEDIATE``
    around both statements, which no other helper in this module takes and which
    would widen the write lock across a read for a log line.

    **It is an UPSERT, not an UPDATE** (BR-4). The no-prior-row case is live
    today — ``script_runner.record_step_completion`` already defends against a
    terminal-created callback that never fired — and a bare ``UPDATE`` there
    would affect 0 rows and discard the settled result silently. That is strictly
    worse than the half-written row guard 1 prevents: the replay gate can reject
    a settled row with no envelope, but it cannot reject a row that does not
    exist.

    **``attempts`` is owned by the SQL and is not a parameter** (BR-5, BR-6):
    ``1`` on the INSERT path, ``existing + 1`` on conflict, so the durable count
    means *total settles ever recorded for this step* and never moves backwards.
    The caller's in-process counter restarts at 0 after a resume, so a
    caller-supplied count would drag a row reading 3 back to 1 — untruthful
    under FR-12. ``component-methods.md`` specifies an ``attempts`` argument;
    it is dropped here on the human's ruling (TD-3), because an argument that is
    accepted and ignored reads as authoritative. Unit 8's call site passes none.

    **``call_fingerprint`` is never written here** (BR-9). On the conflict path
    the column keeps whatever :func:`begin_step` recorded; on the INSERT path it
    stays ``NULL``, so a row rescued from the no-begin path has *absent*
    provenance. That is the intended outcome, not a gap — absent provenance
    routes to a halt rather than a replay match, which makes the rescued result
    durably visible to a human (INV-5).

    **This function sanitises nothing** (BR-14, SR-2). ``result_json``,
    ``output_json`` and ``error`` are persisted as received; it pulls in neither
    unit 2's envelope builder nor its redaction gate, and no security logic lives
    inside a persistence primitive. The caller owns redacting AND bounding both
    remaining content columns, and ``settlement-rewire`` (unit 8) is where that
    is assigned. Until unit 8 lands, a settled row carries a redacted, bounded
    envelope beside a raw, unbounded ``error`` and ``output_json`` — pre-existing
    behaviour that ``update_step`` already has and this function does not
    worsen, recorded as an accepted residual risk in this unit's
    ``security-requirements.md`` SR-3/SR-4 rather than left implicit.

    Raises ``sqlite3.Error`` on a DB failure and swallows nothing (BR-3).
    """
    with _connect() as conn:
        existed = (
            conn.execute(
                "SELECT 1 FROM workflow_run_step WHERE run_id = ? AND step_id = ?",
                (run_id, step_id),
            ).fetchone()
            is not None
        )
        conn.execute(
            "INSERT INTO workflow_run_step "
            "(run_id, step_id, state, attempts, output_json, error, updated_at, "
            " result_json) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, ?) "
            "ON CONFLICT(run_id, step_id) DO UPDATE SET "
            "state = excluded.state, "
            "attempts = workflow_run_step.attempts + 1, "
            "output_json = excluded.output_json, "
            "error = excluded.error, "
            "updated_at = excluded.updated_at, "
            "result_json = excluded.result_json",
            (run_id, step_id, state, output_json, error, updated_at, result_json),
        )
    return existed


def lookup_replay(run_id: str, step_id: str, call_fingerprint: str) -> Optional[StepRow]:
    """Decide replay-from-journal vs execute-fresh for a script call (A2, the M3 core).

    This is a reserved journal primitive. The current run-step route does not call
    it, so script resume re-executes completed calls rather than replaying them.

    Three-way outcome (DR-1/DR-2/DR-3/DR-4, business-rules.md):

    - row absent -> ``None`` (never ran; execute fresh)
    - row present but ``state`` != ``COMPLETED`` -> ``None`` (partial; re-execute)
    - row ``COMPLETED`` and fingerprint matches -> the row (replay; do not execute)
    - row ``COMPLETED`` and fingerprint MISMATCH -> raises ``ReplayDivergenceError``
      (the script changed between runs at the same key; resume cannot honor the
      replay contract, so it fails loudly rather than silently re-executing)

    ``ReplayDivergenceError`` is imported at MODULE level from the ``workflow_errors``
    leaf (issue #583, ADR-583-9, BR-3). It used to be imported here, inside this
    function, from ``workflow_service`` purely to dodge a circular import — that module
    imports this one. The leaf imports nothing, so both sides can bind the name at
    module level and the cycle edge is removed rather than deferred to call time.
    ``workflow_service`` re-exports the name, so its old import path still resolves.
    """
    row = get_step(run_id, step_id)
    if row is None:
        return None
    if row.state != "completed":
        return None
    if row.call_fingerprint != call_fingerprint:
        # Keyword-only per the moved class's signature (issue #583, TD-3). The message's
        # content is unchanged: ``run_id``, ``step_id`` and the fixed phrase — identifiers
        # only, never the two fingerprints (SR-1). ``step_id`` now reaches the rendered
        # string through the structured field, which is what puts it in ``str(exc)``.
        raise ReplayDivergenceError(
            step_id=step_id,
            reason=(
                f"call fingerprint diverged on replay for run '{run_id}' "
                "(the script changed between runs at the same key)"
            ),
        )
    return row


# ---------------------------------------------------------------------------
# U1 additions (issue #504, event-log substrate) — additive only. The durable
# append-only ``workflow_run_event`` table, the ``workflow_run_seq`` high-water
# table, and their read/write DAL. These helpers raise ``sqlite3.Error`` on a DB
# failure exactly like the write family above; the CALLER (U2's emission path)
# wraps them best-effort — a dropped event/high-water write never raises into the
# drive loop (BR-2). U1 adds no new swallow policy of its own.
# ---------------------------------------------------------------------------

# The 21 ``workflow_run_event`` columns, in table order — shared by the append
# INSERT and the read SELECT so the two never drift (BR-5, ADR-1).
_EVENT_COLUMNS: Tuple[str, ...] = (
    "run_id",
    "seq",
    "event_type",
    "event_schema_version",
    "ts",
    "step_id",
    "attempt",
    "state",
    "elapsed_ms",
    "provider",
    "agent_profile",
    "engine",
    "terminal_id",
    "terminal_offset_start",
    "terminal_offset_len",
    "error_kind",
    "reason",
    "validation_result",
    "output_ref",
    "iteration",
    "which_guard_fired",
)

# Memoization set: the (process, db-path) keys whose event/seq migrators have
# already run. Guards ``_connect_event`` so the migrators fire at most once per
# db path per process — NOT once per append (NFR-PERF-1, BR-7).
_event_migrated_paths: Set[str] = set()


def _connect_event() -> sqlite3.Connection:
    """Open a connection for the event tables, migrating at most once (NFR-PERF-1).

    Unlike ``_connect`` (which re-runs its migrators on every call and is left
    untouched to preserve the #505 seam and its documented per-call cost), this
    helper runs ``_migrate_workflow_run_event`` + ``_migrate_workflow_run_seq``
    only the first time it sees a given ``DATABASE_FILE`` in this process, keyed
    through the module-level ``_event_migrated_paths`` set. Emitting N events for
    a run therefore runs the event migrator ≤ 1 time (BR-7); the memoization is
    what makes NFR-PERF-1-T pass. The migrators are imported lazily inside the
    function (mirrors ``_connect``'s lazy import) to avoid an import cycle.
    """
    from cli_agent_orchestrator.clients.database import (
        _migrate_workflow_run_event,
        _migrate_workflow_run_seq,
    )
    from cli_agent_orchestrator.constants import DATABASE_FILE

    path = str(DATABASE_FILE)
    if path not in _event_migrated_paths:
        _migrate_workflow_run_event()
        _migrate_workflow_run_seq()
        _event_migrated_paths.add(path)
    return sqlite3.connect(path)


# ---------------------------------------------------------------------------
# Event writes (U2 emission wraps these best-effort). Each is one short
# transaction committed by the ``with conn`` context.
# ---------------------------------------------------------------------------
def append_event(
    run_id: str,
    seq: int,
    event_type: str,
    *,
    event_schema_version: int,
    ts: str,
    step_id: Optional[str] = None,
    attempt: Optional[int] = None,
    state: Optional[str] = None,
    elapsed_ms: Optional[int] = None,
    provider: Optional[str] = None,
    agent_profile: Optional[str] = None,
    engine: Optional[str] = None,
    terminal_id: Optional[str] = None,
    terminal_offset_start: Optional[int] = None,
    terminal_offset_len: Optional[int] = None,
    error_kind: Optional[str] = None,
    reason: Optional[str] = None,
    validation_result: Optional[str] = None,
    output_ref: Optional[str] = None,
    iteration: Optional[int] = None,
    which_guard_fired: Optional[str] = None,
) -> None:
    """INSERT one immutable ``workflow_run_event`` row (append-only, BR-5).

    ``seq`` is allocated once per emission by the caller's in-memory counter
    (never recomputed here, BR-1), so a re-INSERT for an already-present
    ``(run_id, seq)`` raises ``sqlite3.IntegrityError`` — that indicates a bug
    and the best-effort caller swallows it (logged), never a silent overwrite
    (BR-10). ``run_id``/``seq``/``event_type``/``event_schema_version``/``ts``
    are required; the rest are optional and stored where applicable. Parameterized
    SQL only (BR-9).
    """
    values = (
        run_id,
        seq,
        event_type,
        event_schema_version,
        ts,
        step_id,
        attempt,
        state,
        elapsed_ms,
        provider,
        agent_profile,
        engine,
        terminal_id,
        terminal_offset_start,
        terminal_offset_len,
        error_kind,
        reason,
        validation_result,
        output_ref,
        iteration,
        which_guard_fired,
    )
    placeholders = ", ".join("?" for _ in _EVENT_COLUMNS)
    with _connect_event() as conn:
        conn.execute(
            f"INSERT INTO workflow_run_event ({', '.join(_EVENT_COLUMNS)}) "
            f"VALUES ({placeholders})",
            values,
        )


def persist_high_water(run_id: str, seq: int) -> None:
    """Idempotently record the high-water ``seq`` for a run (BR-11), monotonically.

    An UPSERT of ``max(existing, seq)`` — the high-water never moves backward, so
    a late/lower ``seq`` leaves it unchanged. Best-effort persisted by the caller
    BEFORE the matching ``append_event`` so a rebuild can resume strictly above
    any allocated slot even if the append was later swallowed (BR-3).
    Parameterized SQL only (BR-9).
    """
    with _connect_event() as conn:
        conn.execute(
            "INSERT INTO workflow_run_seq (run_id, high_water) VALUES (?, ?) "
            "ON CONFLICT(run_id) DO UPDATE SET "
            "high_water = max(high_water, excluded.high_water)",
            (run_id, seq),
        )


# ---------------------------------------------------------------------------
# Event reads (rebuild + replay read path). Answerable from the durable tables
# alone with no ``run_registry`` dependency (journal-authoritative, BR-8).
# ---------------------------------------------------------------------------
def _event_row(row: Sequence) -> EventRow:
    """Build an ``EventRow`` from a raw ``_EVENT_COLUMNS``-ordered result row."""
    return EventRow(
        run_id=row[0],
        seq=row[1],
        event_type=row[2],
        event_schema_version=row[3],
        ts=row[4],
        step_id=row[5],
        attempt=row[6],
        state=row[7],
        elapsed_ms=row[8],
        provider=row[9],
        agent_profile=row[10],
        engine=row[11],
        terminal_id=row[12],
        terminal_offset_start=row[13],
        terminal_offset_len=row[14],
        error_kind=row[15],
        reason=row[16],
        validation_result=row[17],
        output_ref=row[18],
        iteration=row[19],
        which_guard_fired=row[20],
    )


def read_events(run_id: str, after_seq: Optional[int] = None) -> List[EventRow]:
    """Return a run's events ordered by ``seq`` (the sole ordering authority, BR-5).

    When ``after_seq`` is given, only events strictly after it are returned — the
    durable analogue of the in-memory ``event_log_service`` after-id replay cursor
    (FR-5.2), so a disconnected follower resumes without gaps or duplicates.
    """
    select = f"SELECT {', '.join(_EVENT_COLUMNS)} FROM workflow_run_event WHERE run_id = ?"
    with _connect_event() as conn:
        if after_seq is None:
            rows = conn.execute(f"{select} ORDER BY seq", (run_id,)).fetchall()
        else:
            rows = conn.execute(
                f"{select} AND seq > ? ORDER BY seq", (run_id, after_seq)
            ).fetchall()
    return [_event_row(r) for r in rows]


_TERMINAL_RUN_STATES = frozenset({"completed", "failed", "cancelled"})


def _is_terminal_run(run_id: str) -> bool:
    """Whether the run has durably ended — the gate for declaring a trailing gap.

    A missing/unreadable run row answers ``False`` (declare nothing) rather than
    raising into the read path: a gap is an assertion about lost data, so the
    quiet answer is the safe default when the run's state cannot be established.

    Uses the MEMOIZED ``_connect_event`` rather than ``_connect`` (PR #526
    review): this runs on every ``read_events_with_gaps`` call, which the SSE
    follow arm invokes 4x/second per follower, and ``_connect`` re-runs
    ``_migrate_workflow_run`` + ``_migrate_workflow_run_step`` on EVERY call.
    ``_connect_event`` migrates the event/seq tables at most once per process per
    DB path, so the poll no longer pays two migrations per tick.

    The table-coverage difference is not a behaviour change here. ``_connect``
    would CREATE an absent ``workflow_run`` table and then read no row -> False;
    ``_connect_event`` does not create it, so an absent table raises
    ``sqlite3.Error``, which the except-arm below also answers False. Both paths
    reach the identical answer, and the only path that produces a True (a real
    stored run row) requires ``insert_run`` to have run through ``_connect``
    already.
    """
    try:
        with _connect_event() as conn:
            row = conn.execute(
                "SELECT state FROM workflow_run WHERE run_id = ?",
                (run_id,),
            ).fetchone()
    except sqlite3.Error as e:
        logger.debug(f"trailing-gap state read failed for run '{run_id}': {e}")
        return False
    return row is not None and str(row[0]) in _TERMINAL_RUN_STATES


def read_events_with_gaps(
    run_id: str, after_seq: Optional[int] = None
) -> Tuple[List[EventRow], List[GapMarker]]:
    """Read a run's events and DECLARE any sequence gaps (Algorithm 2, BR-4).

    The stored per-run sequence is contiguous when complete (BR-1), so a hole is
    detectable: wherever a row's ``seq`` exceeds the previous ``seq`` by more than
    one, a ``GapMarker`` is synthesized spanning the missing range. The sequence
    is never renumbered to hide the gap — the marker lets a client tell "nothing
    happened" from "an event was lost" (FR-3.3). Returns ``(rows, gaps)``.

    A hole between two stored rows is found by the adjacency scan below. A
    TRAILING hole — the last append(s) before a crash were swallowed, so the
    missing seqs sit past the final stored row with no successor to compare
    against — is invisible to that scan, and is exactly the forward-fault shape
    the two-term design exists to catch. It is recovered from the durable
    high-water: the counter is advanced and persisted BEFORE each fallible
    append, so ``persisted_high_water`` is the last seq ever ALLOCATED while the
    last stored row is the last seq that actually LANDED. When the former
    exceeds the latter, the difference is a real declared hole at the end.

    The trailing marker uses ``before_seq = high_water + 1`` — one past the last
    allocated seq — because no stored event bounds it on the right. That is a
    deliberate sentinel: it keeps ``missing_count == before_seq - after_seq - 1``
    arithmetic identical to an interior gap, and it never collides with a real
    event's seq (nothing has been allocated at ``high_water + 1`` yet).

    A trailing gap is declared ONLY for a run in a terminal state. Because the
    high-water is persisted BEFORE each fallible append, a RUNNING run
    legitimately sits one seq ahead for the duration of every in-flight append —
    declaring on that window would emit a phantom gap on essentially every poll
    of every live run. Once the run is terminal no further append can land, so
    the excess is a real, permanent hole. This makes the check quiet by
    construction rather than relying on a timing tolerance.

    That invariant depends on ``_drive`` appending a run's terminal EVENT before
    it writes the terminal STATE. The reverse order (state first) opened a window
    in which a perfectly healthy completed run read as a trailing loss, because
    the run row said ``completed`` while its final append was still in flight —
    see the ordering comment in ``workflow_service._drive``. Do not reorder those
    two writes without re-reading this guard.

    A from-start read (``after_seq is None``) seeds the comparison at ``0`` —
    one below the first allocatable seq — unconditionally, so the from-start and
    cursor-at-0 reads agree on every shape: leading, interior, trailing, and
    total loss. Healthy runs are unaffected (first seq is 1, and an empty run has
    ``high_water == 0``), so the quiet cases stay quiet.
    """
    rows = read_events(run_id, after_seq)
    gaps: List[GapMarker] = []
    if after_seq is not None:
        # CLAMPED at 0 (PR #526 review round 3). Seeding a NEGATIVE cursor verbatim
        # fabricated a GapMarker on a perfectly healthy, lossless run: with rows at
        # seqs 1..3, `after_seq=-5` declared `(after_seq=-5, before_seq=1,
        # missing_count=5, reason="append_failed")` — a phantom loss, which inverts
        # this module's central contract that a declared gap means an event was
        # actually lost.
        #
        # The clamp lives HERE, not only on the route's `ge=0` bound, because this
        # reader is SHARED: the batch and SSE arms of the events route, /compare and
        # /diagnostics all call it. A route-only bound would leave every other caller
        # (present and future) able to reintroduce the phantom.
        #
        # Clamp to 0, NEVER to None: 0 is exactly what the from-start branch below
        # seeds, so a clamped negative cursor and a from-start read agree on every
        # shape. Clamping to None would skip the trailing-gap block and hide a real
        # total-loss run — reintroducing the defect an earlier round fixed.
        prev: Optional[int] = max(after_seq, 0)
    else:
        # A from-start read seeds at 0 — one below the first allocatable seq
        # (seqs start at 1) — UNCONDITIONALLY, whether or not rows came back.
        #
        # This replaced two earlier forms, each of which hid a real hole (PR #526
        # review). `prev = None` on the empty-rows path skipped the trailing block
        # entirely, so the MOST severe shape — every append swallowed, nothing
        # landed — declared nothing on the default read while a cursor read of the
        # same run DID declare it. And `prev = rows[0].seq - 1` on the non-empty
        # path was SELF-REFERENTIAL: it defined "previous" as one below the first
        # SURVIVING row, which makes any LEADING hole invisible by construction
        # (seqs 1,2 lost with 3,4,5 landed read back as no gap at all, while the
        # equivalent cursor-at-0 read declared it).
        #
        # Seeding 0 makes the from-start and cursor-at-0 reads agree on every
        # shape — leading, interior, trailing, and total loss — which is the
        # contract this function documents. A healthy run is unaffected: its
        # first row is seq 1, and 1 > 0 + 1 is false, so no phantom leading gap.
        # A healthy EMPTY run has high_water == 0, so 0 > 0 is false and the
        # trailing block stays silent too — the quiet cases stay quiet.
        prev = 0
    for row in rows:
        if prev is not None and row.seq > prev + 1:
            gaps.append(
                GapMarker(
                    after_seq=prev,
                    before_seq=row.seq,
                    missing_count=row.seq - prev - 1,
                    reason="append_failed",
                )
            )
        prev = row.seq

    # Trailing hole: allocated past the last row that landed. `prev` is the last
    # delivered seq (or the cursor when this page is empty); a read failure in
    # persisted_high_water degrades to 0, which simply declares no trailing gap.
    # TERMINAL-only, and that is sufficient ONLY because `_drive` appends a run's
    # terminal EVENT before it writes the terminal STATE. With the writes in the
    # other order a healthy completed run spent the whole duration of its final
    # append looking exactly like a lost trailing write, and every live follower
    # was told it had lost an event (PR #526 review, BLOCKING — fixed by the
    # reorder, see the ORDER IS LOAD-BEARING comments in workflow_service._drive).
    # Do not reorder those two writes without re-reading this guard.
    if prev is not None and _is_terminal_run(run_id):
        high_water = persisted_high_water(run_id)
        if high_water > prev:
            gaps.append(
                GapMarker(
                    after_seq=prev,
                    before_seq=high_water + 1,
                    missing_count=high_water - prev,
                    reason="append_failed_trailing",
                )
            )
    return rows, gaps


def persisted_high_water(run_id: str) -> int:
    """Return the durable high-water ``seq`` for a run, or ``0`` if unknown/unreadable.

    One of the two rebuild re-seed terms consumed by U2: the counter is restored
    to ``max(persisted_high_water, max_event_seq)`` so a resume never reuses an
    allocated slot (BR-3). Read failures degrade to ``0`` rather than raising into
    the rebuild path.
    """
    try:
        with _connect_event() as conn:
            row = conn.execute(
                "SELECT high_water FROM workflow_run_seq WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row[0]) if row is not None else 0
    except sqlite3.Error as e:
        logger.debug(f"persisted_high_water read failed for run '{run_id}': {e}")
        return 0


def max_event_seq(run_id: str) -> int:
    """Return the largest event ``seq`` durably appended for a run, or ``0`` if none.

    The co-floor of the rebuild re-seed ``max(persisted_high_water, max_event_seq)``
    (consumed by U2): the term that preserves an event whose high-water write was
    swallowed but whose append succeeded — that durable event is the floor, so its
    seq is not clobbered (BR-3). Read failures degrade to ``0``.
    """
    try:
        with _connect_event() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM workflow_run_event WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row[0]) if row is not None else 0
    except sqlite3.Error as e:
        logger.debug(f"max_event_seq read failed for run '{run_id}': {e}")
        return 0


# ---------------------------------------------------------------------------
# U7 addition (issue #504, retention sweep enumeration) — additive read only.
# INV-1: no existing helper above is modified.
# ---------------------------------------------------------------------------
def list_run_ids_by_age() -> List[Tuple[str, str]]:
    """Return ``(run_id, started_at)`` for every run, most-recent first (U7, NFR-SEC-3).

    A minimal read used only by ``workflow_retention.sweep_runs`` for its age +
    run-count bounds — NOT the ``list_runs`` full-run-listing surface owned by
    sibling intent #505 (that returns rich rows and its own indexes; this returns
    just the two fields the sweep needs). Ordered by ``started_at`` descending so a
    "keep the most-recent N" slice is a simple ``rows[N:]`` (the sweep's count
    bound). ``run_id`` is the tie-break so the order is deterministic when two runs
    share a ``started_at``. Uses the same self-connecting ``_connect`` as the other
    ``workflow_run`` reads (run-table only; no event-table migration needed).
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT run_id, started_at FROM workflow_run ORDER BY started_at DESC, run_id DESC"
        ).fetchall()
    return [(r[0], r[1]) for r in rows]


# ---------------------------------------------------------------------------
# Per-run deletion cascade (FR-11 / NFR-SEC-5, Algorithm 5).
# ---------------------------------------------------------------------------
_DELETE_EVENTS_SQL = "DELETE FROM workflow_run_event WHERE run_id = ?"


def delete_run_events(run_id: str, conn: Optional[sqlite3.Connection] = None) -> None:
    """DELETE every ``workflow_run_event`` row for a run (append-only table cleared).

    ``conn`` lets a caller that already holds a connection run this statement
    INSIDE its own transaction; omitted, the function opens and commits its own.
    ``delete_run`` passes its connection so the helper has a real production
    caller (PR #526 review — it was reachable only from a test) WITHOUT splitting
    the four-statement cascade across two transactions, which is what a naive
    ``delete_run_events(run_id)`` call from inside ``delete_run`` would have done.
    """
    if conn is not None:
        conn.execute(_DELETE_EVENTS_SQL, (run_id,))
        return
    with _connect_event() as own:
        own.execute(_DELETE_EVENTS_SQL, (run_id,))


def delete_run(run_id: str) -> None:
    """Remove a run and all rows this substrate owns, in one connection (FR-11).

    Deletes the ``workflow_run`` row, its ``workflow_run_step`` rows, its
    ``workflow_run_event`` rows, and its ``workflow_run_seq`` row — an
    application-layer cascade (the substrate enforces no DB foreign keys).
    Deleting an unknown run id is a well-defined no-op that raises nothing
    (BR-12). Retained-output removal via ``output_ref`` is U7's concern; U1
    deletes only the rows it owns. Parameterized SQL only (BR-9).

    The cascade spans the run/step tables as well as the event/seq tables, so
    the ``workflow_run`` / ``workflow_run_step`` migrators are ensured first
    (idempotent ``CREATE TABLE IF NOT EXISTS``, exactly as ``_connect`` does) —
    otherwise deleting an unknown id on a cold DB, before any ``insert_run``,
    would fault on a missing table rather than being the BR-12 no-op.
    """
    from cli_agent_orchestrator.clients.database import (
        _migrate_workflow_run,
        _migrate_workflow_run_step,
    )

    _migrate_workflow_run()
    _migrate_workflow_run_step()
    with _connect_event() as conn:
        # The event delete goes through the shared helper (DRY, one definition of
        # "clear a run's events"); the connection is threaded in so all four
        # statements stay in ONE transaction.
        delete_run_events(run_id, conn)
        conn.execute("DELETE FROM workflow_run_seq WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM workflow_run_step WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM workflow_run WHERE run_id = ?", (run_id,))

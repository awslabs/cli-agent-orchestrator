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
from typing import List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)


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


def _connect() -> sqlite3.Connection:
    """Open a connection to the shared SQLite file (self-connecting, like B2).

    Ensures the ``workflow_run`` / ``workflow_run_step`` tables exist first
    (idempotent ``CREATE TABLE IF NOT EXISTS`` via the shared migrators) so a
    read/write here never races ``init_db()`` — a process that never went
    through the FastAPI lifespan (e.g. a test that instantiates the app
    without entering it as a context manager) still finds its schema.
    """
    from cli_agent_orchestrator.clients.database import (
        _migrate_workflow_run,
        _migrate_workflow_run_step,
    )
    from cli_agent_orchestrator.constants import DATABASE_FILE

    _migrate_workflow_run()
    _migrate_workflow_run_step()
    return sqlite3.connect(str(DATABASE_FILE))


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
    """
    with _connect() as conn:
        conn.execute(
            "UPDATE workflow_run SET state = ?, finished_at = ? WHERE run_id = ?",
            (state, finished_at, run_id),
        )


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
    them back as ``None`` (behavior otherwise unchanged, SEAM #1).
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT run_id, step_id, state, attempts, output_json, error, updated_at, "
            "call_fingerprint, terminal_id, reprompted, error_kind "
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
        )
        for r in rows
    ]


def get_step(run_id: str, step_id: str) -> Optional[StepRow]:
    """Return the single ``workflow_run_step`` row for ``(run_id, step_id)`` (E2).

    U3 addition: the read primitive ``lookup_replay`` (A2) is built on. Returns
    ``None`` when the row is absent — a script call that has never arrived.

    U1 (issue #504) grows the SELECT list to surface the additive
    ``terminal_id`` / ``reprompted`` / ``error_kind`` columns (``None`` on a
    pre-U1 row); behavior is otherwise unchanged.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT run_id, step_id, state, attempts, output_json, error, updated_at, "
            "call_fingerprint, terminal_id, reprompted, error_kind "
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
    )


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
    ``update_step`` UNCHANGED (INV-1); this function is the sole write path for
    ``call_fingerprint`` (VR-4).

    ``ON CONFLICT ... DO UPDATE`` upserts ``state``/``updated_at`` only — a
    re-executed tail step (e.g. a second resume attempt over the same call)
    already has a prior-attempt row; this is NOT a swallowed IntegrityError, it
    is the documented A1 upsert. ``call_fingerprint`` is deliberately excluded
    from the ``DO UPDATE`` clause so it stays stable across attempts (VR-4) —
    the fingerprint recorded at the FIRST arrival of this ``(run_id, step_id)``
    is the one ``lookup_replay`` compares against on every subsequent attempt.
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

    Imported lazily from ``workflow_service`` to avoid a circular import
    (``workflow_service`` already imports this module).
    """
    from cli_agent_orchestrator.services.workflow_service import ReplayDivergenceError

    row = get_step(run_id, step_id)
    if row is None:
        return None
    if row.state != "completed":
        return None
    if row.call_fingerprint != call_fingerprint:
        raise ReplayDivergenceError(
            f"run '{run_id}' step '{step_id}': call fingerprint diverged on replay "
            "(the script changed between runs at the same key)"
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
    """
    try:
        with _connect() as conn:
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
    high-water is persisted BEFORE its append, a RUNNING run legitimately sits
    one seq ahead for the duration of every in-flight append — declaring on that
    window would emit a phantom gap on essentially every poll of every live run.
    Once the run is terminal no further append can land, so the excess is a real,
    permanent hole. This makes the check quiet by construction rather than
    relying on a timing tolerance.
    """
    rows = read_events(run_id, after_seq)
    gaps: List[GapMarker] = []
    if after_seq is not None:
        prev: Optional[int] = after_seq
    else:
        prev = rows[0].seq - 1 if rows else None
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
    # Terminal-only (see the docstring): a live run is legitimately one ahead
    # mid-append, so checking it there would emit a phantom gap every poll.
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
def delete_run_events(run_id: str) -> None:
    """DELETE every ``workflow_run_event`` row for a run (append-only table cleared)."""
    with _connect_event() as conn:
        conn.execute("DELETE FROM workflow_run_event WHERE run_id = ?", (run_id,))


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
        conn.execute("DELETE FROM workflow_run_event WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM workflow_run_seq WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM workflow_run_step WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM workflow_run WHERE run_id = ?", (run_id,))

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

import sqlite3
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple


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
    """One ``workflow_run_step`` row (E2, domain-entities)."""

    run_id: str
    step_id: str
    state: str
    attempts: int
    output_json: Optional[str]
    error: Optional[str]
    updated_at: str
    call_fingerprint: Optional[str] = None


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
) -> None:
    """UPDATE a step's durable state/attempts/output/error (lifecycle table, E2)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE workflow_run_step "
            "SET state = ?, attempts = ?, output_json = ?, error = ?, updated_at = ? "
            "WHERE run_id = ? AND step_id = ?",
            (state, attempts, output_json, error, updated_at, run_id, step_id),
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
    """Return all ``workflow_run_step`` rows for ``run_id`` (E2)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT run_id, step_id, state, attempts, output_json, error, updated_at, "
            "call_fingerprint "
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
        )
        for r in rows
    ]


def get_step(run_id: str, step_id: str) -> Optional[StepRow]:
    """Return the single ``workflow_run_step`` row for ``(run_id, step_id)`` (E2).

    U3 addition: the read primitive ``lookup_replay`` (A2) is built on. Returns
    ``None`` when the row is absent — a script call that has never arrived.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT run_id, step_id, state, attempts, output_json, error, updated_at, "
            "call_fingerprint "
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

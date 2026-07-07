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


def _connect() -> sqlite3.Connection:
    """Open a connection to the shared SQLite file (self-connecting, like B2)."""
    from cli_agent_orchestrator.constants import DATABASE_FILE

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
) -> None:
    """INSERT the ``workflow_run`` row at ``start_run`` (lifecycle table, E1).

    A plain ``INSERT``: a re-INSERT for an already-journaled ``run_id`` raises
    ``sqlite3.IntegrityError`` rather than silently overwriting the durable row
    (a resume never calls this — it only UPDATEs). The engine both pre-checks the
    journal in ``start_run`` and wraps this call best-effort, so a lost race
    logs instead of clobbering history.
    """
    with _connect() as conn:
        conn.execute(
            "INSERT INTO workflow_run "
            "(run_id, workflow_name, spec_snapshot, inputs_json, state, "
            " current_step_id, started_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?, NULL)",
            (run_id, workflow_name, spec_snapshot, inputs_json, state, started_at),
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
    later resume's ``lookup_replay`` (A2) has something to compare against. The
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

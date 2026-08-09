"""Transactional v2 vintage migration, rollback, WAL preservation, and drain.

The v2 vintage surface (``managed_launch_v2_reservations``,
``managed_launch_v2_terminals``) is created by a real transactional
migration — never a bare ``CREATE TABLE IF NOT EXISTS`` with no
rollback story.  Every migration/rollback runs in one IMMEDIATE
transaction with ``synchronous=FULL``, is journaled append-only in
``v2_migration_journal``, and a rollback refuses until a *complete* v2
drain (zero v2-owned rows anywhere), so no old query can ever observe
half-rolled-back v2 state.  The migration never switches the database's
journal mode: the application engine owns it, and any pre-existing
WAL/SHM siblings must survive migration untouched.

Invariant: pre-existing v1 rows are never touched by migration; a
rollback that would strand live v2 generations refuses with zero
mutation; committed (including WAL-resident) state is checkpointed and
verified preserved across rollback.

Failure mode prevented: a create-only "migration" gives no way back and
lets old binaries see v2 rows mid-rollout; a rollback without a drain
gate destroys live generation state.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

V2_TABLES = ("managed_launch_v2_reservations", "managed_launch_v2_terminals")
JOURNAL_TABLE = "v2_migration_journal"
MIGRATION_ID = "v2-vintage-surface-1"

_V2_RESERVATIONS_DDL = (
    "CREATE TABLE IF NOT EXISTS managed_launch_v2_reservations ("
    "reservation_id TEXT PRIMARY KEY, "
    "terminal_id TEXT NOT NULL UNIQUE, "
    "generation TEXT NOT NULL UNIQUE, "
    "protocol_vintage TEXT NOT NULL DEFAULT 'v2' "
    "CHECK (protocol_vintage = 'v2'), "
    "session_name TEXT NOT NULL, "
    "provider TEXT NOT NULL, "
    "agent_profile TEXT NOT NULL, "
    "caller_id TEXT NOT NULL, "
    "working_directory TEXT NOT NULL, "
    "trusted_project_root TEXT, "
    "obligation_generation TEXT NOT NULL, "
    "task_id TEXT, "
    "run_id TEXT NOT NULL, "
    "launch_nonce_digest TEXT NOT NULL, "
    "stable_agent_id TEXT, "
    "state TEXT NOT NULL, "
    "request_json TEXT NOT NULL, "
    "binding_json TEXT, "
    "bind_intent_json TEXT, "
    "admission_json TEXT, "
    "cleanup_json TEXT, "
    "execution_mode TEXT, "
    "execution_mode_source TEXT, "
    "preflight_failure_json TEXT, "
    "created_at TEXT NOT NULL, "
    "updated_at TEXT NOT NULL"
    ")"
)

#: Columns added to the v2 reservation table after its first release.
#: Each is nullable and additive, so an existing row keeps its bytes and
#: reads back as the legacy default rather than being rewritten.  They
#: are applied with the same PRAGMA-guarded ALTER used for
#: ``bind_intent_json`` and inside the same transaction as the DDL.
_V2_RESERVATIONS_ADDITIVE_COLUMNS = (
    ("bind_intent_json", "TEXT"),
    ("execution_mode", "TEXT"),
    ("execution_mode_source", "TEXT"),
    ("preflight_failure_json", "TEXT"),
    ("cleanup_json", "TEXT"),
    ("stable_agent_id", "TEXT"),
)

_V2_TERMINALS_DDL = (
    "CREATE TABLE IF NOT EXISTS managed_launch_v2_terminals ("
    "id TEXT PRIMARY KEY, "
    "tmux_session TEXT NOT NULL, "
    "tmux_window TEXT NOT NULL, "
    "provider TEXT NOT NULL, "
    "agent_profile TEXT, "
    "allowed_tools TEXT, "
    "caller_id TEXT, "
    "generation TEXT NOT NULL UNIQUE, "
    "protocol_vintage TEXT NOT NULL DEFAULT 'v2' "
    "CHECK (protocol_vintage = 'v2'), "
    "pane_id TEXT, "
    "window_id TEXT, "
    "server_socket_path TEXT, "
    "v2_session_id TEXT, "
    "v2_pane_pid INTEGER, "
    "v2_native_session_id TEXT, "
    "v2_lifecycle_state TEXT, "
    "v2_lifecycle_reason TEXT, "
    "v2_liveness_checked_at TEXT, "
    "v2_superseded_by_terminal_id TEXT, "
    "v2_superseded_by_generation TEXT, "
    "last_active TEXT"
    ")"
)

#: Columns added to the v2 terminal table after its first release, on the
#: same PRAGMA-guarded terms as the reservation ones above.
#:
#: The identity and lifecycle columns mirror the ones on the shared
#: ``terminals`` table, deliberately duplicated rather than shared: the
#: two stores stay separate so that old-binary machine paths keep zero v2
#: visibility, and a managed row still has to be able to answer the same
#: identity questions a human view asks of any other terminal.  Names are
#: prefixed because the receipt records bare column names and asserts they
#: are unique across the whole v2 surface.
_V2_TERMINALS_ADDITIVE_COLUMNS = (
    ("server_socket_path", "TEXT"),
    ("v2_session_id", "TEXT"),
    ("v2_pane_pid", "INTEGER"),
    ("v2_native_session_id", "TEXT"),
    ("v2_lifecycle_state", "TEXT"),
    ("v2_lifecycle_reason", "TEXT"),
    ("v2_liveness_checked_at", "TEXT"),
    ("v2_superseded_by_terminal_id", "TEXT"),
    ("v2_superseded_by_generation", "TEXT"),
)

#: Every additive column, by the table it belongs to.  The receipt records
#: bare column names, which stays unambiguous only while the names are
#: distinct across the whole v2 surface — asserted here rather than left
#: as a convention, because a silent collision would make a schema-change
#: receipt name a change to a table it did not touch.
_V2_ADDITIVE_COLUMNS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("managed_launch_v2_reservations", _V2_RESERVATIONS_ADDITIVE_COLUMNS),
    ("managed_launch_v2_terminals", _V2_TERMINALS_ADDITIVE_COLUMNS),
)
assert len({column for _, columns in _V2_ADDITIVE_COLUMNS for column, _ in columns}) == sum(
    len(columns) for _, columns in _V2_ADDITIVE_COLUMNS
), "v2 additive column names must be unique across the vintage surface"

_JOURNAL_DDL = (
    f"CREATE TABLE IF NOT EXISTS {JOURNAL_TABLE} ("
    "event_id TEXT PRIMARY KEY, "
    "migration_id TEXT NOT NULL, "
    "action TEXT NOT NULL CHECK (action IN ('migrate','rollback')), "
    "at TEXT NOT NULL, "
    "detail TEXT NOT NULL"
    ")"
)


class VintageMigrationError(RuntimeError):
    """Base error for v2 vintage migration operations."""


class RollbackRefused(VintageMigrationError):
    """A rollback was attempted before a complete v2 drain."""


class OldBinaryGateRefused(VintageMigrationError):
    """The exact-old-binary gate observed v2 access or mutation."""


def _run_old_binary_gate(gate: Callable[[], Any]) -> dict[str, Any]:
    """Execute the exact-old-binary proof; refuse on any violation."""
    verdict = gate()
    report = {
        "zero_visibility": bool(verdict.zero_visibility),
        "surfaces_checked": verdict.surfaces_checked,
        "violations": list(verdict.violations),
    }
    if not report["zero_visibility"]:
        raise OldBinaryGateRefused(
            "exact-old-binary gate observed v2 access/mutation; "
            f"migration/rollout/rollback refused: {report['violations']}"
        )
    return report


def configured_old_binary_gate() -> Optional[Callable[[], Any]]:
    """The production exact-old-binary gate, when configured.

    ``CAO_OLD_BINARY_GATE=require`` activates it; ``CAO_OLD_BINARY_REPO``
    names the git repository holding the old binary's exact source
    (default: this package's source tree when it is a git checkout) and
    ``CAO_OLD_BINARY_REF`` pins the old ref (default: the deployed-base
    H_B constant). With no configuration the gate is absent and the
    journal records ``not-configured`` — never a proof claim.
    """
    import os
    import tempfile

    if os.environ.get("CAO_OLD_BINARY_GATE") != "require":
        return None
    from cli_agent_orchestrator.services import old_binary_rig

    repo = Path(os.environ.get("CAO_OLD_BINARY_REPO") or Path(__file__).resolve().parents[2])
    ref = os.environ.get("CAO_OLD_BINARY_REF") or old_binary_rig.DEFAULT_OLD_BINARY_REF

    def _gate() -> Any:
        with tempfile.TemporaryDirectory(prefix="cao-old-binary-gate-") as tmp:
            return old_binary_rig.prove_old_binary_invisibility(
                repo=repo, ref=ref, workdir=Path(tmp)
            )

    return _gate


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _connect(db_path: Path) -> sqlite3.Connection:
    # Do NOT force a journal-mode switch here: the application engine owns
    # the database's journal mode, and adopting WAL on this connection
    # would make SQLite checkpoint-and-delete any pre-existing -wal/-shm
    # siblings on close (init_db is responsible for restricting those
    # siblings' permissions, so they must survive migration untouched).
    # Transactionality comes from BEGIN IMMEDIATE; when the database is
    # already WAL (production posture), this connection simply uses it.
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def migrate_v2(
    db_path: Path, *, old_binary_gate: Optional[Callable[[], Any]] = None
) -> dict[str, Any]:
    """Apply the v2 vintage surface migration transactionally (idempotent).

    A second run is a no-op beyond the journaled observation; v1 rows are
    never read or written.  When ``old_binary_gate`` is supplied, the
    exact-old-binary invisibility proof runs FIRST and any observed v2
    access/mutation refuses the migration with zero mutation; the gate
    outcome (or ``not-configured``) is journaled with the event.  Returns
    the migration receipt.
    """
    gate_report: Any = "not-configured"
    if old_binary_gate is not None:
        gate_report = _run_old_binary_gate(old_binary_gate)
    path = Path(db_path)
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        already = all(_table_exists(conn, table) for table in V2_TABLES)
        conn.execute(_V2_RESERVATIONS_DDL)
        conn.execute(_V2_TERMINALS_DDL)
        added_columns = []
        for table, columns in _V2_ADDITIVE_COLUMNS:
            cursor = conn.execute(f"PRAGMA table_info({table})")
            present = {row[1] for row in cursor.fetchall()}
            for column, column_type in columns:
                if column not in present:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
                    added_columns.append(column)
        conn.execute(_JOURNAL_DDL)
        event_id = str(uuid.uuid4())
        conn.execute(
            f"INSERT INTO {JOURNAL_TABLE}(event_id, migration_id, action, at, detail) "
            "VALUES (?,?,?,?,?)",
            (
                event_id,
                MIGRATION_ID,
                "migrate",
                _now(),
                json.dumps(
                    {
                        "already_present": already,
                        # Journaled separately because ``already_present``
                        # answers "did the tables exist", not "was the
                        # schema changed". An additive column lands on an
                        # existing table, so without this the receipt for
                        # a real schema change would read as a no-op.
                        "added_columns": added_columns,
                        "old_binary_gate": gate_report,
                    },
                    sort_keys=True,
                ),
            ),
        )
        conn.commit()
        return {
            "migration_id": MIGRATION_ID,
            "action": "migrate",
            "already_present": already,
            "added_columns": added_columns,
            "tables": list(V2_TABLES),
            "event_id": event_id,
            "at": _now(),
        }
    except sqlite3.Error as exc:
        conn.rollback()
        raise VintageMigrationError(f"v2 migration failed; transaction rolled back: {exc}") from exc
    finally:
        conn.close()


def drain_report(db_path: Path) -> dict[str, Any]:
    """Count v2-owned rows; ``drained`` is true only at a complete drain.

    A WAL checkpoint runs first so WAL-resident committed rows are
    visible to the drain decision (WAL preservation: nothing committed
    is invisible to, or destroyed by, rollback).
    """
    path = Path(db_path)
    conn = _connect(path)
    try:
        conn.execute("PRAGMA wal_checkpoint(FULL)")
        counts: dict[str, int] = {}
        for table in V2_TABLES:
            if _table_exists(conn, table):
                counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            else:
                counts[table] = 0
        return {
            "tables": counts,
            "drained": all(count == 0 for count in counts.values()),
        }
    finally:
        conn.close()


def rollback_v2(
    db_path: Path, *, old_binary_gate: Optional[Callable[[], Any]] = None
) -> dict[str, Any]:
    """Drop the v2 surface transactionally — only after a complete drain.

    Refuses with zero mutation while any v2-owned row exists; when
    ``old_binary_gate`` is supplied the exact-old-binary invisibility
    proof must also pass (the old binary becomes the deployed binary on
    rollback, so any v2 access/mutation it can perform refuses the
    rollback); a drained rollback drops the v2 tables, journals the event
    (including the gate outcome), and leaves every pre-existing v1 row
    and WAL-committed state intact.
    """
    gate_report: Any = "not-configured"
    if old_binary_gate is not None:
        gate_report = _run_old_binary_gate(old_binary_gate)
    report = drain_report(db_path)
    if not report["drained"]:
        raise RollbackRefused(f"v2 rollback refused until a complete drain: {report['tables']}")
    path = Path(db_path)
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(_JOURNAL_DDL)
        for table in V2_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        event_id = str(uuid.uuid4())
        conn.execute(
            f"INSERT INTO {JOURNAL_TABLE}(event_id, migration_id, action, at, detail) "
            "VALUES (?,?,?,?,?)",
            (
                event_id,
                MIGRATION_ID,
                "rollback",
                _now(),
                json.dumps(
                    {"tables": report["tables"], "old_binary_gate": gate_report},
                    sort_keys=True,
                ),
            ),
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(FULL)")
        return {
            "migration_id": MIGRATION_ID,
            "action": "rollback",
            "dropped": list(V2_TABLES),
            "event_id": event_id,
            "at": _now(),
        }
    except sqlite3.Error as exc:
        conn.rollback()
        raise VintageMigrationError(f"v2 rollback failed; transaction rolled back: {exc}") from exc
    finally:
        conn.close()

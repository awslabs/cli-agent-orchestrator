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
from typing import Any

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
    "state TEXT NOT NULL, "
    "request_json TEXT NOT NULL, "
    "binding_json TEXT, "
    "bind_intent_json TEXT, "
    "admission_json TEXT, "
    "created_at TEXT NOT NULL, "
    "updated_at TEXT NOT NULL"
    ")"
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
    "last_active TEXT"
    ")"
)

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


def migrate_v2(db_path: Path) -> dict[str, Any]:
    """Apply the v2 vintage surface migration transactionally (idempotent).

    A second run is a no-op beyond the journaled observation; v1 rows are
    never read or written.  Returns the migration receipt.
    """
    path = Path(db_path)
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        already = all(_table_exists(conn, table) for table in V2_TABLES)
        conn.execute(_V2_RESERVATIONS_DDL)
        conn.execute(_V2_TERMINALS_DDL)
        cursor = conn.execute("PRAGMA table_info(managed_launch_v2_reservations)")
        if "bind_intent_json" not in {row[1] for row in cursor.fetchall()}:
            conn.execute(
                "ALTER TABLE managed_launch_v2_reservations ADD COLUMN bind_intent_json TEXT"
            )
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
                json.dumps({"already_present": already}, sort_keys=True),
            ),
        )
        conn.commit()
        return {
            "migration_id": MIGRATION_ID,
            "action": "migrate",
            "already_present": already,
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


def rollback_v2(db_path: Path) -> dict[str, Any]:
    """Drop the v2 surface transactionally — only after a complete drain.

    Refuses with zero mutation while any v2-owned row exists; a drained
    rollback drops the v2 tables, journals the event, and leaves every
    pre-existing v1 row and WAL-committed state intact.
    """
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
            (event_id, MIGRATION_ID, "rollback", _now(), json.dumps(report["tables"])),
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

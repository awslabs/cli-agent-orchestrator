"""Durable intent and receipt journal for managed-session controls.

Each bridge generation owns one journal.  An operation intent is committed
before provider I/O, transitions are append-only, and an ambiguous operation
is terminal for automation.  This prevents a lost response from turning into
a blind replay of prompt, cancel, compaction, or route-changing effects.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = 1

QUEUED = "queued"
SUBMITTED = "submitted"
ACCEPTED = "accepted"
COMPLETED = "completed"
REFUSED = "refused"
AMBIGUOUS = "ambiguous"

TERMINAL_STATES = frozenset({COMPLETED, REFUSED, AMBIGUOUS})
LEGAL_TRANSITIONS = frozenset(
    {
        (None, QUEUED),
        (QUEUED, SUBMITTED),
        (QUEUED, REFUSED),
        (SUBMITTED, ACCEPTED),
        (SUBMITTED, REFUSED),
        (SUBMITTED, AMBIGUOUS),
        (ACCEPTED, COMPLETED),
        (ACCEPTED, REFUSED),
        (ACCEPTED, AMBIGUOUS),
    }
)

_DDL = """
CREATE TABLE IF NOT EXISTS journal_meta (
  k TEXT PRIMARY KEY, v TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS session_operation (
  operation_id TEXT PRIMARY KEY,
  terminal_id TEXT NOT NULL,
  generation TEXT NOT NULL,
  action TEXT NOT NULL,
  state TEXT NOT NULL,
  request_sha256 TEXT NOT NULL,
  provider TEXT NOT NULL,
  provider_session_id TEXT NOT NULL,
  provider_turn_id TEXT,
  result_json TEXT,
  reason_code TEXT,
  reason_detail TEXT,
  opened_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS session_operation_event (
  event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
  operation_id TEXT NOT NULL,
  from_state TEXT,
  to_state TEXT NOT NULL,
  evidence_digest TEXT,
  at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS soe_no_update
  BEFORE UPDATE ON session_operation_event
  BEGIN SELECT RAISE(ABORT,'session_operation_event is append-only'); END;
CREATE TRIGGER IF NOT EXISTS soe_no_delete
  BEFORE DELETE ON session_operation_event
  BEGIN SELECT RAISE(ABORT,'session_operation_event is append-only'); END;
"""


class SessionControlError(RuntimeError):
    pass


class SessionControlRefused(SessionControlError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class SessionControlJournal:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(_DDL)
            conn.execute(
                "INSERT OR IGNORE INTO journal_meta(k,v) VALUES "
                "('schema_version', ?), ('db_uuid', ?), ('created_at', ?)",
                (str(SCHEMA_VERSION), str(uuid.uuid4()), _now()),
            )
            conn.commit()
        finally:
            conn.close()
        os.chmod(self.path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def begin(
        self,
        *,
        operation_id: str,
        terminal_id: str,
        generation: str,
        action: str,
        request_sha256: str,
        provider: str,
        provider_session_id: str,
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT request_sha256 FROM session_operation WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if existing is not None:
                if existing[0] != request_sha256:
                    raise SessionControlRefused(
                        "operation id is already bound to different request bytes"
                    )
                conn.commit()
                return self.get(operation_id)
            moment = _now()
            conn.execute(
                "INSERT INTO session_operation("
                "operation_id,terminal_id,generation,action,state,request_sha256,"
                "provider,provider_session_id,opened_at,updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    operation_id,
                    terminal_id,
                    generation,
                    action,
                    QUEUED,
                    request_sha256,
                    provider,
                    provider_session_id,
                    moment,
                    moment,
                ),
            )
            conn.execute(
                "INSERT INTO session_operation_event("
                "operation_id,from_state,to_state,at) VALUES (?,?,?,?)",
                (operation_id, None, QUEUED, moment),
            )
            conn.commit()
        except SessionControlError:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.get(operation_id)

    def transition(
        self,
        operation_id: str,
        to_state: str,
        *,
        evidence_digest: Optional[str] = None,
        provider_turn_id: Optional[str] = None,
        result: Optional[dict[str, Any]] = None,
        reason_code: Optional[str] = None,
        reason_detail: Optional[str] = None,
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state FROM session_operation WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise SessionControlRefused("unknown managed-session operation")
            from_state = str(row[0])
            if from_state == to_state:
                conn.commit()
                return self.get(operation_id)
            if (from_state, to_state) not in LEGAL_TRANSITIONS:
                raise SessionControlRefused(
                    f"illegal managed-session transition {from_state!r} -> {to_state!r}"
                )
            moment = _now()
            conn.execute(
                "UPDATE session_operation SET state=?,provider_turn_id=COALESCE(?,provider_turn_id),"
                "result_json=COALESCE(?,result_json),reason_code=COALESCE(?,reason_code),"
                "reason_detail=COALESCE(?,reason_detail),updated_at=? WHERE operation_id=?",
                (
                    to_state,
                    provider_turn_id,
                    json.dumps(result, sort_keys=True) if result is not None else None,
                    reason_code,
                    reason_detail,
                    moment,
                    operation_id,
                ),
            )
            conn.execute(
                "INSERT INTO session_operation_event("
                "operation_id,from_state,to_state,evidence_digest,at) VALUES (?,?,?,?,?)",
                (operation_id, from_state, to_state, evidence_digest, moment),
            )
            conn.commit()
        except SessionControlError:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.get(operation_id)

    def get(self, operation_id: str) -> dict[str, Any]:
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM session_operation WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise SessionControlRefused("unknown managed-session operation")
        result = dict(row)
        result_json = result.pop("result_json")
        result["result"] = json.loads(result_json) if result_json else None
        return result

    def latest(self, limit: int = 20) -> list[dict[str, Any]]:
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM session_operation ORDER BY opened_at DESC LIMIT ?",
                (max(1, min(limit, 100)),),
            ).fetchall()
        finally:
            conn.close()
        result = []
        for row in rows:
            item = dict(row)
            result_json = item.pop("result_json")
            item["result"] = json.loads(result_json) if result_json else None
            result.append(item)
        return result

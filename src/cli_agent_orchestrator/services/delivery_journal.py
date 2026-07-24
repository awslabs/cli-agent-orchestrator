"""Server delivery journal: honest at-most-once callback submission.

The journal records transport/submission milestones for one logical
callback: ``accepted → terminal_queued → submitted →
submit-acked | submit-ambiguous → consumer-acked``.  It reduces
duplication risk but never eliminates the accept-then-bridge-death
window: no pinned provider supplies a durable client-chosen idempotency
key with an exact query, so this plane never claims exactly one eventual
provider submission.

Invariant: every milestone is one SQLite transaction (WAL,
``synchronous=FULL``, 0600, owner-only); transitions follow the legal
set only; a ``submit-ambiguous`` record is terminal for automated
handling — it is never blindly re-submitted, and resolution happens by
consumer/human reconciliation against the logical identity.

Failure mode prevented: without a durable intent-before-I/O journal, a
crash between provider accept and receipt persistence makes the
submission state unknowable, and a blind retry can deliver the same
logical callback twice (duplicate effect) or strand it silently.

Why this guard exists: collection truth depends on the difference
between "delivered", "submitted", "acknowledged by the receiver", and
"possibly submitted"; those must be distinct durable states, never
inferred from file existence, mtimes, or provider UI.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

JOURNAL_SCHEMA_VERSION = 1

STATE_ACCEPTED = "accepted"
STATE_TERMINAL_QUEUED = "terminal_queued"
STATE_SUBMITTED = "submitted"
STATE_SUBMIT_ACKED = "submit-acked"
STATE_SUBMIT_AMBIGUOUS = "submit-ambiguous"
STATE_SUBMIT_REFUSED = "submit-refused"
STATE_CONSUMER_ACKED = "consumer-acked"

# The legal transition set; anything else is refused with zero mutation.
# (terminal_queued, submit-ambiguous) is the honest ambiguous-accept edge:
# the provider may have accepted while the journal still reads
# terminal_queued (response lost before the submitted milestone could be
# persisted). It never claims submission and is terminal for automated
# handling, exactly like the post-submitted ambiguity.
LEGAL_TRANSITIONS = frozenset(
    {
        (None, STATE_ACCEPTED),
        (STATE_ACCEPTED, STATE_TERMINAL_QUEUED),
        (STATE_TERMINAL_QUEUED, STATE_SUBMITTED),
        (STATE_TERMINAL_QUEUED, STATE_SUBMIT_AMBIGUOUS),
        # A provider-native busy/refusal proven to occur before submission is
        # retryable. Events remain append-only; only the current state cycles
        # back to queued when the same logical message is tried later.
        (STATE_TERMINAL_QUEUED, STATE_SUBMIT_REFUSED),
        (STATE_SUBMIT_REFUSED, STATE_TERMINAL_QUEUED),
        (STATE_SUBMITTED, STATE_SUBMIT_ACKED),
        (STATE_SUBMITTED, STATE_SUBMIT_AMBIGUOUS),
        (STATE_SUBMIT_ACKED, STATE_CONSUMER_ACKED),
    }
)

_DDL = """
CREATE TABLE IF NOT EXISTS journal_meta (
  k TEXT PRIMARY KEY, v TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS delivery (
  obligation_generation TEXT NOT NULL,
  logical_callback_id TEXT NOT NULL,
  state TEXT NOT NULL,
  request_sha256 TEXT NOT NULL,
  opened_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (obligation_generation, logical_callback_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS delivery_event (
  event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
  obligation_generation TEXT NOT NULL,
  logical_callback_id TEXT NOT NULL,
  from_state TEXT,
  to_state TEXT NOT NULL,
  evidence_digest TEXT,
  at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS de_no_update BEFORE UPDATE ON delivery_event
  BEGIN SELECT RAISE(ABORT,'delivery_event is append-only'); END;
CREATE TRIGGER IF NOT EXISTS de_no_delete BEFORE DELETE ON delivery_event
  BEGIN SELECT RAISE(ABORT,'delivery_event is append-only'); END;
"""


class DeliveryJournalError(RuntimeError):
    """Base error for delivery-journal operations."""


class DeliveryTransitionRefused(DeliveryJournalError):
    """An illegal, regressive, or replaying transition was refused."""


class DeliveryNotFound(DeliveryJournalError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class DeliveryJournal:
    """The per-callback delivery journal (one DB per fork state root)."""

    def __init__(self, db_path: Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        created = not self._path.exists()
        # Concurrent first-open constructors race on the create
        # transaction; creation is idempotent, so a locked loser retries.
        last_error: Optional[sqlite3.Error] = None
        for _ in range(20):
            try:
                conn = self._connect()
                try:
                    conn.executescript(_DDL)
                    if (
                        created
                        or conn.execute(
                            "SELECT 1 FROM journal_meta WHERE k='journal_schema_version'"
                        ).fetchone()
                        is None
                    ):
                        conn.execute(
                            "INSERT OR IGNORE INTO journal_meta(k,v) VALUES "
                            "('journal_schema_version', ?), ('db_uuid', ?), ('created_at', ?)",
                            (str(JOURNAL_SCHEMA_VERSION), str(uuid.uuid4()), _now()),
                        )
                    conn.commit()
                finally:
                    conn.close()
                break
            except sqlite3.OperationalError as exc:
                last_error = exc
                import time

                time.sleep(0.05)
        else:
            raise DeliveryJournalError(f"journal creation failed under contention: {last_error}")
        os.chmod(self._path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _transition(
        self,
        obligation_generation: str,
        logical_callback_id: str,
        *,
        to_state: str,
        request_sha256: Optional[str] = None,
        evidence_digest: Optional[str] = None,
    ) -> dict[str, Any]:
        # A concurrent opener can win the unique insert between the
        # existence read and the INSERT; the loser retries and converges
        # to the idempotent path rather than surfacing a false failure.
        last_integrity: Optional[sqlite3.IntegrityError] = None
        for _ in range(10):
            try:
                return self._transition_once(
                    obligation_generation,
                    logical_callback_id,
                    to_state=to_state,
                    request_sha256=request_sha256,
                    evidence_digest=evidence_digest,
                )
            except sqlite3.IntegrityError as exc:
                last_integrity = exc
                import time

                time.sleep(0.02)
        raise DeliveryJournalError(f"journal write lost repeated races: {last_integrity}")

    def _transition_once(
        self,
        obligation_generation: str,
        logical_callback_id: str,
        *,
        to_state: str,
        request_sha256: Optional[str] = None,
        evidence_digest: Optional[str] = None,
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state, request_sha256, opened_at FROM delivery "
                "WHERE obligation_generation=? AND logical_callback_id=?",
                (obligation_generation, logical_callback_id),
            ).fetchone()
            from_state = row[0] if row else None
            if row is not None and request_sha256 is not None and row[1] != request_sha256:
                # Same logical identity with different request bytes is
                # corruption outside the conductor's lawful revision chain.
                raise DeliveryTransitionRefused(
                    "logical callback already bound to a different request_sha256"
                )
            if from_state == to_state:
                # Idempotent re-arrival of the same milestone (crash
                # between effect and journal write): record nothing twice.
                conn.commit()
                return self.get(obligation_generation, logical_callback_id)
            if (from_state, to_state) not in LEGAL_TRANSITIONS:
                raise DeliveryTransitionRefused(
                    f"illegal delivery transition {from_state!r} -> {to_state!r}; "
                    "submit-ambiguous is terminal for automated handling (no blind replay)"
                )
            moment = _now()
            if row is None:
                if request_sha256 is None:
                    raise DeliveryJournalError("opening a delivery requires request_sha256")
                conn.execute(
                    "INSERT INTO delivery(obligation_generation, logical_callback_id, "
                    "state, request_sha256, opened_at, updated_at) VALUES (?,?,?,?,?,?)",
                    (
                        obligation_generation,
                        logical_callback_id,
                        to_state,
                        request_sha256,
                        moment,
                        moment,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE delivery SET state=?, updated_at=? "
                    "WHERE obligation_generation=? AND logical_callback_id=? AND state=?",
                    (to_state, moment, obligation_generation, logical_callback_id, from_state),
                )
            conn.execute(
                "INSERT INTO delivery_event(obligation_generation, logical_callback_id, "
                "from_state, to_state, evidence_digest, at) VALUES (?,?,?,?,?,?)",
                (
                    obligation_generation,
                    logical_callback_id,
                    from_state,
                    to_state,
                    evidence_digest,
                    moment,
                ),
            )
            conn.commit()
        except DeliveryJournalError:
            conn.rollback()
            raise
        except sqlite3.IntegrityError:
            # Propagated to the race-retry wrapper in _transition.
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise DeliveryJournalError(f"journal write failed: {exc}") from exc
        finally:
            conn.close()
        return self.get(obligation_generation, logical_callback_id)

    def open_intent(
        self, obligation_generation: str, logical_callback_id: str, request_sha256: str
    ) -> dict[str, Any]:
        """Persist the delivery intent before any provider I/O."""
        return self._transition(
            obligation_generation,
            logical_callback_id,
            to_state=STATE_ACCEPTED,
            request_sha256=request_sha256,
        )

    def mark_terminal_queued(
        self,
        obligation_generation: str,
        logical_callback_id: str,
        evidence_digest: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._transition(
            obligation_generation,
            logical_callback_id,
            to_state=STATE_TERMINAL_QUEUED,
            evidence_digest=evidence_digest,
        )

    def mark_submitted(
        self,
        obligation_generation: str,
        logical_callback_id: str,
        evidence_digest: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._transition(
            obligation_generation,
            logical_callback_id,
            to_state=STATE_SUBMITTED,
            evidence_digest=evidence_digest,
        )

    def mark_submit_acked(
        self,
        obligation_generation: str,
        logical_callback_id: str,
        evidence_digest: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._transition(
            obligation_generation,
            logical_callback_id,
            to_state=STATE_SUBMIT_ACKED,
            evidence_digest=evidence_digest,
        )

    def mark_submit_ambiguous(
        self,
        obligation_generation: str,
        logical_callback_id: str,
        evidence_digest: Optional[str] = None,
    ) -> dict[str, Any]:
        """The provider may have accepted but the bridge died before persisting.

        Terminal for automated handling: never blindly re-submitted;
        resolved by consumer/human reconciliation against the logical
        identity.
        """
        return self._transition(
            obligation_generation,
            logical_callback_id,
            to_state=STATE_SUBMIT_AMBIGUOUS,
            evidence_digest=evidence_digest,
        )

    def mark_submit_refused(
        self,
        obligation_generation: str,
        logical_callback_id: str,
        evidence_digest: Optional[str] = None,
    ) -> dict[str, Any]:
        """Record a certain pre-submission refusal; the same message may retry."""
        return self._transition(
            obligation_generation,
            logical_callback_id,
            to_state=STATE_SUBMIT_REFUSED,
            evidence_digest=evidence_digest,
        )

    def mark_consumer_acked(
        self,
        obligation_generation: str,
        logical_callback_id: str,
        evidence_digest: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._transition(
            obligation_generation,
            logical_callback_id,
            to_state=STATE_CONSUMER_ACKED,
            evidence_digest=evidence_digest,
        )

    def get(self, obligation_generation: str, logical_callback_id: str) -> dict[str, Any]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT state, request_sha256, opened_at, updated_at FROM delivery "
                "WHERE obligation_generation=? AND logical_callback_id=?",
                (obligation_generation, logical_callback_id),
            ).fetchone()
            if row is None:
                raise DeliveryNotFound(
                    f"no delivery record for {(obligation_generation, logical_callback_id)}"
                )
            events = [
                {
                    "from_state": event[0],
                    "to_state": event[1],
                    "evidence_digest": event[2],
                    "at": event[3],
                }
                for event in conn.execute(
                    "SELECT from_state, to_state, evidence_digest, at FROM delivery_event "
                    "WHERE obligation_generation=? AND logical_callback_id=? "
                    "ORDER BY event_seq",
                    (obligation_generation, logical_callback_id),
                )
            ]
            return {
                "obligation_generation": obligation_generation,
                "logical_callback_id": logical_callback_id,
                "state": row[0],
                "request_sha256": row[1],
                "opened_at": row[2],
                "updated_at": row[3],
                "events": events,
            }
        finally:
            conn.close()

    def is_ambiguous_preserved(self, obligation_generation: str, logical_callback_id: str) -> bool:
        try:
            return bool(
                self.get(obligation_generation, logical_callback_id)["state"]
                == STATE_SUBMIT_AMBIGUOUS
            )
        except DeliveryNotFound:
            return False

    def event_log(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            return [
                {
                    "obligation_generation": row[0],
                    "logical_callback_id": row[1],
                    "from_state": row[2],
                    "to_state": row[3],
                    "at": row[4],
                }
                for row in conn.execute(
                    "SELECT obligation_generation, logical_callback_id, from_state, "
                    "to_state, at FROM delivery_event ORDER BY event_seq"
                )
            ]
        finally:
            conn.close()


def journal_digest(records_json: bytes) -> str:
    """Digest helper for evidence binding (e.g. negative proofs)."""
    import hashlib

    return hashlib.sha256(records_json).hexdigest()


def _serialize(record: dict[str, Any]) -> str:
    return json.dumps(record, sort_keys=True)

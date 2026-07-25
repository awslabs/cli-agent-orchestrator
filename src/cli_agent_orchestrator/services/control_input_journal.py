"""Durable request journal for identity-bound control input.

One record per control request, keyed by the client-chosen request id.
The record is what makes an honest answer possible after the answer is
lost: a client that never saw a response asks again by the same request
id and is told what happened, instead of guessing and sending the
control a second time.

The states are deliberately few, and the transition set is deliberately
missing an edge:

    (none)  -> intent                request recorded, nothing written
    intent  -> refused               decided before any byte left
    intent  -> writing               this caller claimed the write
    writing -> delivered             tmux acked every write
    writing -> ambiguous             a write failed, or the owner died
    refused -> intent                the same control, re-attempted

There is no ``writing -> refused``.  Once the write is claimed, no
evidence available afterwards can prove the pane received nothing, so
the journal is structurally incapable of recording the comfortable
answer.  That absence is the point: ``refused`` means zero bytes, and
callers are entitled to act on it.

``refused -> intent`` is the other side of that same coin.  A refusal
tells the caller it may send the control again; if the record then
refused every re-arrival by replaying the old answer, that permission
would be worthless and the transient refusals — a busy pane above all —
would be permanent.  The edge is safe for exactly the reason the
refusal is actionable: a refused record proves zero bytes, so re-arming
it cannot resurrect a write that already happened.  It is taken only
when the re-arriving binding is byte-identical, so it re-attempts the
same control rather than admitting a new one under a used id, and the
append-only event log keeps every refusal that preceded it.
``delivered`` and ``ambiguous`` stay terminal, because neither can make
that proof.

At-most-once comes from ``claim_write``, which is a compare-and-swap on
the durable record inside one ``BEGIN IMMEDIATE`` transaction.  Exactly
one caller — across threads and across processes — transitions
``intent -> writing``; every other caller is told the claim is already
taken and must not write.  This is what makes the pane arbiter and the
journal complementary rather than redundant: the arbiter guarantees one
writer at a time, the claim guarantees one writer ever.

The ordering discipline callers must follow, and the reason for it:

1. ``open_intent`` commits before anything else happens, so a request
   that exists at all has a durable identity to answer about.
2. Acquire the pane lease (:mod:`pane_input_arbiter`).  A busy pane is a
   refusal, and nothing has been written.
3. Re-verify the pane identity under the lease.  A mismatch is a
   refusal, and nothing has been written.
4. ``claim_write`` commits, and only then may the first byte be sent.
5. ``mark_delivered`` or ``mark_ambiguous``.

Step 4's commit precedes the first byte, which is what lets a crash
sweep resolve a record still in ``intent`` to ``refused`` with proof
rather than with optimism.  The residual window is narrow and real: a
process that dies between the ``writing`` commit and the first write has
written nothing, but nothing durable says so, so it resolves to
``ambiguous``.  Narrowing that further would require journaling each
chunk, which moves the window without closing it; reporting it honestly
is worth more than shrinking it.

Failure mode prevented: without this record, a lost response leaves a
caller with two choices that are both wrong — re-send a control that may
have already run, or drop a control that may never have run.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from cli_agent_orchestrator.services.control_input_contract import (
    ACCEPTED,
    AMBIGUOUS,
    REASON_OWNER_LOST_BEFORE_WRITE,
    REASON_OWNER_LOST_MID_WRITE,
    REFUSED,
    is_reattemptable,
    is_valid_pane_id,
    outcome_for_reason,
)

#: 2 adds ``control_input_request.server_socket_path`` (§24.7). Bumped
#: rather than left at 1 because the recorded version is re-stamped by the
#: additive migration below, so it describes the shape a journal actually
#: has now — not the shape it was born with.
CONTROL_INPUT_JOURNAL_SCHEMA_VERSION = 2

# --- Record states --------------------------------------------------------

INTENT = "intent"
WRITING = "writing"
DELIVERED = "delivered"
STATE_REFUSED = "refused"
STATE_AMBIGUOUS = "ambiguous"

TERMINAL_STATES = frozenset({DELIVERED, STATE_REFUSED, STATE_AMBIGUOUS})

# Note the absent (writing, refused) edge, and that (refused, intent) is
# the only edge leaving a terminal state; see the module docstring.
LEGAL_TRANSITIONS = frozenset(
    {
        (None, INTENT),
        (INTENT, STATE_REFUSED),
        (INTENT, WRITING),
        (WRITING, DELIVERED),
        (WRITING, STATE_AMBIGUOUS),
        (STATE_REFUSED, INTENT),
    }
)

# The wire outcome each terminal state licenses.  A non-terminal state
# has no outcome yet: "in flight" is a truthful answer and inventing one
# of the four outcomes for it would be a lie in whichever direction the
# caller happened to need.
_STATE_OUTCOMES: Dict[str, str] = {
    DELIVERED: ACCEPTED,
    STATE_REFUSED: REFUSED,
    STATE_AMBIGUOUS: AMBIGUOUS,
}

_DDL = """
CREATE TABLE IF NOT EXISTS journal_meta (
  k TEXT PRIMARY KEY, v TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS control_input_request (
  request_id TEXT PRIMARY KEY,
  terminal_id TEXT NOT NULL,
  pane_id TEXT NOT NULL,
  window_id TEXT NOT NULL,
  pane_pid INTEGER NOT NULL,
  server_socket_path TEXT,
  generation TEXT,
  request_sha256 TEXT NOT NULL,
  state TEXT NOT NULL,
  reason_code TEXT,
  chunks_sent INTEGER,
  enter_attempted INTEGER,
  owner_pid INTEGER NOT NULL,
  owner_token TEXT NOT NULL,
  opened_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS control_input_event (
  event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL,
  from_state TEXT,
  to_state TEXT NOT NULL,
  reason_code TEXT,
  evidence_digest TEXT,
  at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS cie_no_update BEFORE UPDATE ON control_input_event
  BEGIN SELECT RAISE(ABORT,'control_input_event is append-only'); END;
CREATE TRIGGER IF NOT EXISTS cie_no_delete BEFORE DELETE ON control_input_event
  BEGIN SELECT RAISE(ABORT,'control_input_event is append-only'); END;
"""

#: Columns added to ``control_input_request`` after its first release.
#: ``_DDL`` alone cannot introduce them: it is ``CREATE TABLE IF NOT
#: EXISTS`` and the journal has no schema-version gate on open, so an
#: existing journal would silently keep the old shape and every write
#: naming the new column would fail at runtime rather than at migration.
#: Each is nullable — an existing row records a request that was bound
#: before this identity existed, and inventing a value for it would
#: manufacture a binding nobody ever observed.
_ADDITIVE_REQUEST_COLUMNS: Tuple[Tuple[str, str], ...] = (("server_socket_path", "TEXT"),)


class ControlInputJournalError(RuntimeError):
    """Base error for control-input journal operations."""


class ControlInputTransitionRefused(ControlInputJournalError):
    """An illegal or regressive transition was refused with zero mutation."""


class ControlInputRebound(ControlInputTransitionRefused):
    """A request id was re-used for a different control or a different target.

    Never treated as a retry.  Two different controls sharing one request
    id would make the id useless as the handle a lost response is
    resolved by, which is the only reason the id exists.
    """


class ControlInputNotFound(ControlInputJournalError):
    """No record exists for this request id."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _pid_is_alive(pid: int) -> bool:
    """Whether ``pid`` names a live process.

    A PermissionError means the process exists and is owned by someone
    else, which is still alive.  Erring towards "alive" keeps the sweep
    conservative: it may leave a dead owner's record unresolved, but it
    never resolves a live owner's record out from under it.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def outcome_for_state(state: str) -> Optional[str]:
    """The wire outcome a state licenses, or None while still in flight."""
    return _STATE_OUTCOMES.get(state)


def _add_missing_request_columns(conn: sqlite3.Connection) -> None:
    """Bring an existing ``control_input_request`` up to the current shape.

    Idempotent and additive only. Runs inside the caller's creation
    transaction so a journal is never observed half-migrated.
    """
    cursor = conn.execute("PRAGMA table_info(control_input_request)")
    present = {row[1] for row in cursor.fetchall()}
    for column, column_type in _ADDITIVE_REQUEST_COLUMNS:
        if column not in present:
            conn.execute(f"ALTER TABLE control_input_request ADD COLUMN {column} {column_type}")


@dataclass(frozen=True)
class ControlInputBinding:
    """Everything one request id is permanently bound to.

    Identity is bound at intent time and never re-negotiated.  A later
    call presenting the same id with any of these fields changed is a
    different control wearing a borrowed id, and is refused.
    """

    request_id: str
    terminal_id: str
    pane_id: str
    window_id: str
    pane_pid: int
    request_sha256: str
    generation: Optional[str] = None
    # The tmux server the pane id belongs to (§24.7). Optional because a
    # terminal recorded before this identity existed has none, and such a
    # request is refused at the writer boundary rather than here — the
    # journal's job is to record what a request was bound to, including
    # that it was bound to no server, so the refusal is evidenced.
    server_socket_path: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("A control-input request requires a request_id")
        if not is_valid_pane_id(self.pane_id):
            raise ValueError(f"Invalid pane_id: {self.pane_id!r}")
        if not self.window_id.startswith("@"):
            raise ValueError(f"Invalid window_id: {self.window_id!r}")
        if self.pane_pid <= 0:
            raise ValueError(f"Invalid pane_pid: {self.pane_pid!r}")
        if not self.request_sha256:
            raise ValueError("A control-input request requires a request_sha256")


@dataclass(frozen=True)
class ControlInputRecord:
    """One request's durable state, as of the read that produced it."""

    request_id: str
    terminal_id: str
    pane_id: str
    window_id: str
    pane_pid: int
    server_socket_path: Optional[str]
    generation: Optional[str]
    request_sha256: str
    state: str
    reason_code: Optional[str]
    chunks_sent: Optional[int]
    enter_attempted: Optional[bool]
    owner_pid: int
    owner_token: str
    opened_at: str
    updated_at: str
    events: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def outcome(self) -> Optional[str]:
        """The typed wire outcome, or None while the request is in flight."""
        return outcome_for_state(self.state)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def as_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "terminal_id": self.terminal_id,
            "pane_id": self.pane_id,
            "window_id": self.window_id,
            "pane_pid": self.pane_pid,
            "server_socket_path": self.server_socket_path,
            "generation": self.generation,
            "request_sha256": self.request_sha256,
            "state": self.state,
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "chunks_sent": self.chunks_sent,
            "enter_attempted": self.enter_attempted,
            "opened_at": self.opened_at,
            "updated_at": self.updated_at,
            "events": [dict(event) for event in self.events],
        }


@dataclass(frozen=True)
class ControlInputClaim:
    """The result of asking to be the one writer for a request.

    ``granted`` is True for exactly one caller across every thread and
    process for the life of the request id.  A caller holding a False
    claim must not write under any circumstance, including when the
    record's state suggests the previous claimant failed — that owner may
    still be mid-write, and a second write is precisely the duplication
    this journal exists to prevent.
    """

    granted: bool
    record: ControlInputRecord


class ControlInputJournal:
    """The durable control-input request journal (one DB per state root)."""

    def __init__(
        self,
        db_path: Path,
        *,
        owner_pid: Optional[int] = None,
        owner_token: Optional[str] = None,
    ) -> None:
        self._path = Path(db_path)
        self._owner_pid = os.getpid() if owner_pid is None else owner_pid
        # A fresh token per journal instance, so a restarted server never
        # mistakes a previous incarnation's stranded record for its own.
        self._owner_token = str(uuid.uuid4()) if owner_token is None else owner_token
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Concurrent first-open constructors race on the create
        # transaction; creation is idempotent, so a locked loser retries.
        last_error: Optional[sqlite3.Error] = None
        for _ in range(20):
            try:
                conn = self._connect()
                try:
                    conn.executescript(_DDL)
                    _add_missing_request_columns(conn)
                    # Birth facts: written once and never revised. A
                    # journal that reported a new db_uuid or creation time
                    # after a migration would be claiming to be a
                    # different journal than the one holding the records.
                    conn.execute(
                        "INSERT OR IGNORE INTO journal_meta(k,v) VALUES "
                        "('db_uuid', ?), ('created_at', ?)",
                        (str(uuid.uuid4()), _now()),
                    )
                    # Current shape, so it stays true across a migration
                    # instead of describing the shape this journal was
                    # born with. Stamped in the same transaction as the
                    # ALTERs above: the version and the columns it names
                    # commit together or not at all.
                    conn.execute(
                        "INSERT INTO journal_meta(k,v) VALUES ('journal_schema_version', ?) "
                        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                        (str(CONTROL_INPUT_JOURNAL_SCHEMA_VERSION),),
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
            raise ControlInputJournalError(
                f"control-input journal creation failed under contention: {last_error}"
            )
        os.chmod(self._path, 0o600)

    @property
    def owner_token(self) -> str:
        return self._owner_token

    @property
    def owner_pid(self) -> int:
        return self._owner_pid

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    # --- Intent -----------------------------------------------------------

    def open_intent(self, binding: ControlInputBinding) -> ControlInputRecord:
        """Commit the request's identity before any pane I/O.

        Idempotent for an identical re-arrival: a client that retried the
        HTTP request after a lost response gets the existing record, with
        no second event and no second write claim.  The one exception is
        a record in ``refused``, which is re-armed to ``intent`` so the
        re-attempt the refusal promised can actually happen; see the
        module docstring for why that is safe and why no other state
        gets the same treatment.

        Raises:
            ControlInputRebound: The id is bound to a different control.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT terminal_id, pane_id, window_id, pane_pid, server_socket_path, "
                "generation, request_sha256, state FROM control_input_request "
                "WHERE request_id=?",
                (binding.request_id,),
            ).fetchone()
            if row is not None:
                existing = (
                    row[0],
                    row[1],
                    row[2],
                    int(row[3]),
                    row[4],
                    row[5],
                    row[6],
                )
                incoming = (
                    binding.terminal_id,
                    binding.pane_id,
                    binding.window_id,
                    binding.pane_pid,
                    # In the comparison, so a re-arrival naming the same
                    # pane id on a *different* tmux server is a rebound
                    # rather than a retry. Without it the one identity that
                    # distinguishes two panes that share an id would be the
                    # one identity a re-used request id could change.
                    binding.server_socket_path,
                    binding.generation,
                    binding.request_sha256,
                )
                if existing != incoming:
                    raise ControlInputRebound(
                        f"request id {binding.request_id!r} is already bound to a "
                        "different control target or different request bytes"
                    )
                if str(row[7]) == STATE_REFUSED:
                    moment = _now()
                    # The old refusal's evidence is cleared from the live
                    # row rather than carried forward: it describes an
                    # attempt that is over, and leaving it attached would
                    # make the re-armed request look like it had already
                    # failed.  The event log keeps it.
                    conn.execute(
                        "UPDATE control_input_request SET state=?, reason_code=NULL, "
                        "chunks_sent=NULL, enter_attempted=NULL, owner_pid=?, "
                        "owner_token=?, updated_at=? WHERE request_id=? AND state=?",
                        (
                            INTENT,
                            self._owner_pid,
                            self._owner_token,
                            moment,
                            binding.request_id,
                            STATE_REFUSED,
                        ),
                    )
                    self._append_event(
                        conn, binding.request_id, STATE_REFUSED, INTENT, None, moment
                    )
                conn.commit()
                return self.get(binding.request_id)
            moment = _now()
            conn.execute(
                "INSERT INTO control_input_request(request_id, terminal_id, pane_id, "
                "window_id, pane_pid, server_socket_path, generation, request_sha256, "
                "state, reason_code, chunks_sent, enter_attempted, owner_pid, "
                "owner_token, opened_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,?,?,?)",
                (
                    binding.request_id,
                    binding.terminal_id,
                    binding.pane_id,
                    binding.window_id,
                    binding.pane_pid,
                    binding.server_socket_path,
                    binding.generation,
                    binding.request_sha256,
                    INTENT,
                    self._owner_pid,
                    self._owner_token,
                    moment,
                    moment,
                ),
            )
            self._append_event(conn, binding.request_id, None, INTENT, None, moment)
            conn.commit()
        except ControlInputJournalError:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise ControlInputJournalError(f"journal write failed: {exc}") from exc
        finally:
            conn.close()
        return self.get(binding.request_id)

    # --- The at-most-once claim ------------------------------------------

    def claim_write(self, request_id: str) -> ControlInputClaim:
        """Ask to be the one writer for this request.

        Granted to exactly one caller.  The transition commits before the
        caller sends a byte, so a record left in ``writing`` by a dead
        owner is the durable evidence that a write may have happened.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state FROM control_input_request WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise ControlInputNotFound(f"no control-input record for {request_id!r}")
            from_state = str(row[0])
            if from_state != INTENT:
                # Already claimed, already finished, or already refused.
                conn.commit()
                return ControlInputClaim(granted=False, record=self.get(request_id))
            moment = _now()
            conn.execute(
                "UPDATE control_input_request SET state=?, owner_pid=?, owner_token=?, "
                "updated_at=? WHERE request_id=? AND state=?",
                (WRITING, self._owner_pid, self._owner_token, moment, request_id, INTENT),
            )
            self._append_event(conn, request_id, INTENT, WRITING, None, moment)
            conn.commit()
        except ControlInputJournalError:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise ControlInputJournalError(f"journal write failed: {exc}") from exc
        finally:
            conn.close()
        return ControlInputClaim(granted=True, record=self.get(request_id))

    # --- Outcomes ---------------------------------------------------------

    def mark_delivered(
        self,
        request_id: str,
        *,
        chunks_sent: Optional[int] = None,
        enter_attempted: Optional[bool] = None,
        evidence_digest: Optional[str] = None,
    ) -> ControlInputRecord:
        """tmux accepted every write, including any submitting Enter.

        ``enter_attempted`` is recorded here and not inferred, because a
        control sent with ``enter=False`` is delivered without ever being
        submitted.  A record that omitted it could not answer the only
        question a caller replaying a lost response actually has: whether
        the provider has already started acting on the control.
        """
        return self._transition(
            request_id,
            to_state=DELIVERED,
            chunks_sent=chunks_sent,
            enter_attempted=enter_attempted,
            evidence_digest=evidence_digest,
        )

    def mark_refused(
        self,
        request_id: str,
        *,
        reason_code: str,
        evidence_digest: Optional[str] = None,
    ) -> ControlInputRecord:
        """Record a refusal decided before any byte was written.

        Legal only from ``intent``.  Calling it after the write is
        claimed is refused rather than recorded, because by then no
        evidence can support the claim that nothing was written.
        """
        return self._transition(
            request_id,
            to_state=STATE_REFUSED,
            reason_code=reason_code,
            evidence_digest=evidence_digest,
        )

    def mark_ambiguous(
        self,
        request_id: str,
        *,
        reason_code: str,
        chunks_sent: Optional[int] = None,
        enter_attempted: Optional[bool] = None,
        evidence_digest: Optional[str] = None,
    ) -> ControlInputRecord:
        """Record that the pane's state is unknowable for this request.

        Terminal for automation.  It is never re-driven and never
        upgraded to delivered by a later observation, because the pane
        cannot distinguish this control's bytes from any other's.
        """
        return self._transition(
            request_id,
            to_state=STATE_AMBIGUOUS,
            reason_code=reason_code,
            chunks_sent=chunks_sent,
            enter_attempted=enter_attempted,
            evidence_digest=evidence_digest,
        )

    def _transition(
        self,
        request_id: str,
        *,
        to_state: str,
        reason_code: Optional[str] = None,
        chunks_sent: Optional[int] = None,
        enter_attempted: Optional[bool] = None,
        evidence_digest: Optional[str] = None,
    ) -> ControlInputRecord:
        # A reason is bound to exactly one outcome, so a reason that
        # disagrees with the state being recorded is refused before it
        # can reach the record.  The dangerous direction is a post-attempt
        # reason ('response-lost', 'write-incomplete') recorded as
        # 'refused': the record would then license a caller to re-send
        # bytes that may already have landed.  Checked here rather than at
        # each mark_* so no future entry point can bypass it.
        if reason_code is not None:
            expected = outcome_for_reason(reason_code)
            actual = _STATE_OUTCOMES.get(to_state)
            if actual is not None and expected != actual:
                hazard = (
                    "a post-attempt uncertainty recorded as a refusal would license a "
                    "duplicate write"
                    if is_reattemptable(actual)
                    else "a provable refusal recorded as uncertain strands a request "
                    "that never reached the pane"
                )
                raise ControlInputTransitionRefused(
                    f"reason {reason_code!r} carries outcome {expected!r} and cannot be "
                    f"recorded as {to_state!r} (outcome {actual!r}); {hazard}"
                )
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state FROM control_input_request WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise ControlInputNotFound(f"no control-input record for {request_id!r}")
            from_state = str(row[0])
            if from_state == to_state:
                # Idempotent re-arrival of the same milestone (a crash
                # between the effect and the journal write): record once.
                conn.commit()
                return self.get(request_id)
            if (from_state, to_state) not in LEGAL_TRANSITIONS:
                raise ControlInputTransitionRefused(
                    f"illegal control-input transition {from_state!r} -> {to_state!r}; "
                    "a claimed write can never be recorded as refused, and an "
                    "ambiguous request is terminal"
                )
            moment = _now()
            conn.execute(
                "UPDATE control_input_request SET state=?, reason_code=?, "
                "chunks_sent=COALESCE(?, chunks_sent), "
                "enter_attempted=COALESCE(?, enter_attempted), updated_at=? "
                "WHERE request_id=? AND state=?",
                (
                    to_state,
                    reason_code,
                    chunks_sent,
                    None if enter_attempted is None else int(enter_attempted),
                    moment,
                    request_id,
                    from_state,
                ),
            )
            self._append_event(conn, request_id, from_state, to_state, reason_code, moment)
            conn.commit()
        except ControlInputJournalError:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise ControlInputJournalError(f"journal write failed: {exc}") from exc
        finally:
            conn.close()
        return self.get(request_id)

    @staticmethod
    def _append_event(
        conn: sqlite3.Connection,
        request_id: str,
        from_state: Optional[str],
        to_state: str,
        reason_code: Optional[str],
        moment: str,
    ) -> None:
        conn.execute(
            "INSERT INTO control_input_event(request_id, from_state, to_state, "
            "reason_code, evidence_digest, at) VALUES (?,?,?,?,NULL,?)",
            (request_id, from_state, to_state, reason_code, moment),
        )

    # --- Reads ------------------------------------------------------------

    def get(self, request_id: str) -> ControlInputRecord:
        """The record for ``request_id``, with its full event history.

        This is the exact-request-id query a client uses after a lost
        response, and the only supported way to learn what happened.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT request_id, terminal_id, pane_id, window_id, pane_pid, "
                "server_socket_path, generation, request_sha256, state, reason_code, "
                "chunks_sent, enter_attempted, owner_pid, owner_token, opened_at, "
                "updated_at FROM control_input_request WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise ControlInputNotFound(f"no control-input record for {request_id!r}")
            events = tuple(
                {
                    "from_state": event[0],
                    "to_state": event[1],
                    "reason_code": event[2],
                    "at": event[3],
                }
                for event in conn.execute(
                    "SELECT from_state, to_state, reason_code, at FROM control_input_event "
                    "WHERE request_id=? ORDER BY event_seq",
                    (request_id,),
                )
            )
        finally:
            conn.close()
        return ControlInputRecord(
            request_id=row[0],
            terminal_id=row[1],
            pane_id=row[2],
            window_id=row[3],
            pane_pid=int(row[4]),
            server_socket_path=row[5],
            generation=row[6],
            request_sha256=row[7],
            state=row[8],
            reason_code=row[9],
            chunks_sent=row[10],
            enter_attempted=None if row[11] is None else bool(row[11]),
            owner_pid=int(row[12]),
            owner_token=row[13],
            opened_at=row[14],
            updated_at=row[15],
            events=events,
        )

    def find(self, request_id: str) -> Optional[ControlInputRecord]:
        """``get`` that answers None instead of raising for an absent id."""
        try:
            return self.get(request_id)
        except ControlInputNotFound:
            return None

    def in_flight(self) -> List[ControlInputRecord]:
        """Every request that has not reached a terminal state."""
        conn = self._connect()
        try:
            ids = [
                str(row[0])
                for row in conn.execute(
                    "SELECT request_id FROM control_input_request WHERE state IN (?,?) "
                    "ORDER BY opened_at",
                    (INTENT, WRITING),
                )
            ]
        finally:
            conn.close()
        return [self.get(request_id) for request_id in ids]

    # --- Crash recovery ---------------------------------------------------

    def sweep_stranded(
        self,
        *,
        owner_alive: Optional[Callable[[int], bool]] = None,
    ) -> List[ControlInputRecord]:
        """Resolve requests whose owning process is gone.

        Two different resolutions, because two different facts are
        available:

        - ``intent`` becomes ``refused``.  The claim commits before the
          first byte, so a record that never reached ``writing`` proves
          the pane was never touched.
        - ``writing`` becomes ``ambiguous``.  The owner had the right to
          write and may have used it; nothing durable says whether it
          did.

        Records owned by a live process are left alone, including this
        journal's own.  A recycled pid can make a dead owner look alive,
        which leaves a record unresolved rather than resolving it wrongly
        — the safe direction, since an unresolved record still answers
        the exact-id query truthfully with "in flight".
        """
        alive = _pid_is_alive if owner_alive is None else owner_alive
        resolved: List[ControlInputRecord] = []
        for record in self.in_flight():
            if record.owner_token == self._owner_token:
                continue
            if alive(record.owner_pid):
                continue
            try:
                if record.state == INTENT:
                    resolved.append(
                        self.mark_refused(
                            record.request_id, reason_code=REASON_OWNER_LOST_BEFORE_WRITE
                        )
                    )
                else:
                    resolved.append(
                        self.mark_ambiguous(
                            record.request_id, reason_code=REASON_OWNER_LOST_MID_WRITE
                        )
                    )
            except ControlInputTransitionRefused:
                # A concurrent sweeper or the owner itself resolved this
                # record first; its answer stands.
                continue
        return resolved

    def event_log(self) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            return [
                {
                    "request_id": row[0],
                    "from_state": row[1],
                    "to_state": row[2],
                    "reason_code": row[3],
                    "at": row[4],
                }
                for row in conn.execute(
                    "SELECT request_id, from_state, to_state, reason_code, at "
                    "FROM control_input_event ORDER BY event_seq"
                )
            ]
        finally:
            conn.close()

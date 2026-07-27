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
    SEQUENCE_EVENT_OUTCOMES,
    SUBMISSION_OBSERVED_VALUES,
    is_reattemptable,
    is_valid_pane_id,
    normalize_sequence_events,
    outcome_for_reason,
)

#: 2 adds ``control_input_request.server_socket_path`` (§24.7). Bumped
#: rather than left at 1 because the recorded version is re-stamped by the
#: additive migration below, so it describes the shape a journal actually
#: has now — not the shape it was born with.
#: 3 adds ``chord``, ``chord_attempted``, ``chord_sent`` for schema-v2
#: chord controls (the chord mirrors the enter fields so a replay of a
#: lost response can report how far a text-then-chord write got).
#: 4 adds ``submission_observed`` and ``submission_evidence_ref`` for the
#: cond-0026 provider-visible submission boundary: what the composer was
#: observed to do with the control, and where that observation's evidence
#: lives.  Both stay NULL on rows sealed before v4 — an observation that
#: was never recorded is projected as exactly that, never backfilled or
#: inferred.  The v3 -> v4 migration snapshots the journal file before the
#: first ALTER (see ``_snapshot_before_v4_migration``).
#: 5 adds ``control_input_sequence_event``: the ordered structured events
#: of a schema-v3 sequence control (cond-0175) and their per-event
#: outcomes, stored as typed columns — never flattened into an escaped
#: string.  One sequence is one request row and one at-most-once
#: operation; the child rows carry the ordered detail.  The v4 -> v5
#: migration creates the table and snapshots the journal file first (see
#: ``_snapshot_before_v5_migration``).
CONTROL_INPUT_JOURNAL_SCHEMA_VERSION = 5

#: Where the pre-migration copy of a v3 journal is written before the v4
#: ALTERs run.  Sibling of the journal file, created at most once: an
#: existing snapshot is the original pre-migration evidence and is never
#: overwritten by a later open.
V4_MIGRATION_SNAPSHOT_SUFFIX = ".pre-v4-migration.sqlite3"

#: Same rule for the v4 -> v5 migration: the pre-migration copy is taken
#: before the sequence-event table is created, at most once.
V5_MIGRATION_SNAPSHOT_SUFFIX = ".pre-v5-migration.sqlite3"

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
  chord TEXT,
  chord_attempted INTEGER,
  chord_sent INTEGER,
  submission_observed TEXT,
  submission_evidence_ref TEXT,
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
CREATE TABLE IF NOT EXISTS control_input_sequence_event (
  request_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  type TEXT NOT NULL,
  text TEXT,
  key TEXT,
  chord TEXT,
  outcome TEXT,
  PRIMARY KEY (request_id, ordinal)
) WITHOUT ROWID;
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
_ADDITIVE_REQUEST_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("server_socket_path", "TEXT"),
    ("chord", "TEXT"),
    ("chord_attempted", "INTEGER"),
    ("chord_sent", "INTEGER"),
    ("submission_observed", "TEXT"),
    ("submission_evidence_ref", "TEXT"),
)

#: Columns whose absence from an existing journal marks it as pre-v4, so
#: their addition is the v4 migration the snapshot below protects.  Both
#: are added by one ``ALTER`` pass; checking one would do, but naming both
#: keeps the marker honest if a journal is ever found half-migrated.
_V4_REQUEST_COLUMNS: Tuple[str, ...] = ("submission_observed", "submission_evidence_ref")


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


def _journal_needs_v4_migration(db_path: Path) -> bool:
    """Whether opening ``db_path`` will ALTER a pre-v4 journal.

    False for a journal that does not exist yet (it is born at the current
    shape and there is nothing to preserve), for one whose request table
    was never created (same), and for one already carrying the v4 columns
    (the migration has already run; re-snapshotting now would overwrite
    the original pre-migration evidence with a post-migration copy).
    """
    if not db_path.exists():
        return False
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        cursor = conn.execute("PRAGMA table_info(control_input_request)")
        present = {row[1] for row in cursor.fetchall()}
    finally:
        conn.close()
    if not present:
        return False
    return any(column not in present for column in _V4_REQUEST_COLUMNS)


def _snapshot_before_v4_migration(db_path: Path) -> Optional[Path]:
    """Copy a pre-v4 journal aside before its first v4 ALTER.

    Uses the SQLite backup API rather than a file copy so the snapshot is
    a consistent database even while the live journal is in WAL mode with
    transactions in flight.  Written at most once: an existing snapshot is
    left untouched, because the first copy is the pre-migration evidence
    and a later one would describe a journal that had already migrated.

    Returns the snapshot path when one was written, else None.
    """
    if not _journal_needs_v4_migration(db_path):
        return None
    snapshot_path = db_path.with_name(db_path.name + V4_MIGRATION_SNAPSHOT_SUFFIX)
    if snapshot_path.exists():
        return None
    source = sqlite3.connect(str(db_path), timeout=30)
    try:
        destination = sqlite3.connect(str(snapshot_path))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    os.chmod(snapshot_path, 0o600)
    return snapshot_path


def _journal_needs_v5_migration(db_path: Path) -> bool:
    """Whether opening ``db_path`` will create the v5 sequence table.

    Same rules as the v4 check: False for a journal that does not exist
    yet (it is born at the current shape), for one whose request table was
    never created, and for one already carrying the sequence-event table
    (the migration has already run, and re-snapshotting would overwrite
    the original pre-migration evidence).
    """
    if not db_path.exists():
        return False
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    finally:
        conn.close()
    if "control_input_request" not in tables:
        return False
    return "control_input_sequence_event" not in tables


def _snapshot_before_v5_migration(db_path: Path) -> Optional[Path]:
    """Copy a pre-v5 journal aside before its sequence table is created.

    Same discipline as the v4 snapshot: a consistent backup API copy,
    written at most once, before the first DDL that changes the shape.
    Returns the snapshot path when one was written, else None.
    """
    if not _journal_needs_v5_migration(db_path):
        return None
    snapshot_path = db_path.with_name(db_path.name + V5_MIGRATION_SNAPSHOT_SUFFIX)
    if snapshot_path.exists():
        return None
    source = sqlite3.connect(str(db_path), timeout=30)
    try:
        destination = sqlite3.connect(str(snapshot_path))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    os.chmod(snapshot_path, 0o600)
    return snapshot_path


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
    chord: Optional[str] = None
    chord_attempted: Optional[bool] = None
    chord_sent: Optional[bool] = None
    # The provider-visible submission observation and where its evidence
    # lives (v4).  ``None`` is a typed null meaning "no observation was
    # recorded for this request" — pre-v4 rows, providers with no
    # submission barrier, controls sent with ``enter=False``, and every
    # refusal — and is projected as exactly that, never as "unknown",
    # which is a recorded observation that could not be classified.
    submission_observed: Optional[str] = None
    submission_evidence_ref: Optional[str] = None
    # The ordered events of a schema-v3 sequence control (v5), each with
    # its per-event outcome — ``None`` for v1/v2 records, which have no
    # events.  A NULL outcome on a stored event is the typed null: the
    # record was sealed (or swept) before an outcome was recorded for
    # that event, and it is projected as exactly that, never replaced by
    # an invented one.
    sequence_events: Optional[Tuple[Dict[str, Any], ...]] = None
    owner_pid: int = 0
    owner_token: str = ""
    opened_at: str = ""
    updated_at: str = ""
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
            "chord": self.chord,
            "chord_attempted": self.chord_attempted,
            "chord_sent": self.chord_sent,
            "submission_observed": self.submission_observed,
            "submission_evidence_ref": self.submission_evidence_ref,
            "sequence_events": (
                None
                if self.sequence_events is None
                else [dict(event) for event in self.sequence_events]
            ),
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
        # Before anything else: if this is a pre-v4 journal, preserve it.
        # The snapshot must precede the creation transaction below, which
        # is where the v4 ALTERs run, and it is taken at most once so the
        # pre-migration evidence is never overwritten by a later open.
        _snapshot_before_v4_migration(self._path)
        # Same discipline for the v5 sequence table: snapshot before the
        # DDL that creates it.
        _snapshot_before_v5_migration(self._path)
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

    def open_intent(
        self,
        binding: ControlInputBinding,
        sequence_events: Optional[List[Dict[str, Any]]] = None,
    ) -> ControlInputRecord:
        """Commit the request's identity before any pane I/O.

        Idempotent for an identical re-arrival: a client that retried the
        HTTP request after a lost response gets the existing record, with
        no second event and no second write claim.  The one exception is
        a record in ``refused``, which is re-armed to ``intent`` so the
        re-attempt the refusal promised can actually happen; see the
        module docstring for why that is safe and why no other state
        gets the same treatment.

        ``sequence_events`` is the ordered payload of a schema-v3
        sequence control (v5): the events are stored structured, one row
        per event, inside the same transaction as the intent — a sequence
        whose events were only half-stored must never exist.  They are
        stored with a NULL outcome ("no outcome recorded yet"); outcomes
        are written by the terminal transitions.  v1/v2 requests pass
        nothing and get no rows.  The request digest already binds the
        exact events, so an identical re-arrival needs no event
        re-comparison: same digest, same events.

        Raises:
            ControlInputRebound: The id is bound to a different control.
            ValueError: The events are not a well-formed v3 sequence.
        """
        normalised_events = (
            None if sequence_events is None else normalize_sequence_events(sequence_events)
        )
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
                existing = (row[0], row[1], row[2], int(row[3]), row[5], row[6])
                incoming = (
                    binding.terminal_id,
                    binding.pane_id,
                    binding.window_id,
                    binding.pane_pid,
                    binding.generation,
                    binding.request_sha256,
                )
                if existing != incoming:
                    raise ControlInputRebound(
                        f"request id {binding.request_id!r} is already bound to a "
                        "different control target or different request bytes"
                    )
                stored_socket, state = row[4], str(row[7])
                # The socket is compared apart from the tuple above because
                # a stored NULL is not a value that failed to match — it is
                # a row written before schema 2 existed, when no request
                # recorded a server at all. Comparing it as a value turns
                # every migrated row into a rebound the moment the same
                # request re-arrives from a client that can now state one,
                # which would present an already-delivered request as a
                # fresh one and invite the second write this journal exists
                # to prevent.
                #
                # A stored socket that *is* present still has to match
                # exactly: that is the case where two panes sharing an id on
                # different servers would otherwise be one request id, and
                # it is the reason the column was added.
                if stored_socket is not None and stored_socket != binding.server_socket_path:
                    raise ControlInputRebound(
                        f"request id {binding.request_id!r} is bound to a pane on tmux "
                        f"server {stored_socket!r}, and this re-arrival names "
                        f"{binding.server_socket_path!r}; a pane id is unique only "
                        "within one server, so this is a different pane"
                    )
                if stored_socket is None and binding.server_socket_path is not None:
                    # Adopted only while nothing has been written yet. In
                    # those states the socket describes the write this row
                    # is about to authorize, so recording it qualifies the
                    # row before it matters and closes the gap above for
                    # every later re-arrival. On a row that has already
                    # written -- delivered, writing, or ambiguous -- it is
                    # withheld: the write went to whichever server answered
                    # at the time, which this migrated row never recorded,
                    # and stamping it now would turn an unknown into a
                    # claim that a later exact comparison would trust.
                    if state in (INTENT, STATE_REFUSED):
                        conn.execute(
                            "UPDATE control_input_request SET server_socket_path=? "
                            "WHERE request_id=? AND server_socket_path IS NULL",
                            (binding.server_socket_path, binding.request_id),
                        )
                if state == STATE_REFUSED:
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
                    # The per-event refusal outcomes clear with the live
                    # row for the same reason: they belong to the attempt
                    # that is over, and the re-armed request must not look
                    # pre-failed.  NULL is "no outcome recorded yet".
                    conn.execute(
                        "UPDATE control_input_sequence_event SET outcome=NULL "
                        "WHERE request_id=?",
                        (binding.request_id,),
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
            if normalised_events is not None:
                for ordinal, event in enumerate(normalised_events):
                    conn.execute(
                        "INSERT INTO control_input_sequence_event(request_id, ordinal, "
                        "type, text, key, chord, outcome) VALUES (?,?,?,?,?,?,NULL)",
                        (
                            binding.request_id,
                            ordinal,
                            event["type"],
                            event.get("text"),
                            event.get("key"),
                            event.get("chord"),
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
        chord: Optional[str] = None,
        chord_attempted: Optional[bool] = None,
        chord_sent: Optional[bool] = None,
        submission_observed: Optional[str] = None,
        submission_evidence_ref: Optional[str] = None,
        sequence_event_outcomes: Optional[List[Tuple[int, str]]] = None,
        evidence_digest: Optional[str] = None,
    ) -> ControlInputRecord:
        """tmux accepted every write, including any submitting Enter.

        ``enter_attempted`` is recorded here and not inferred, because a
        control sent with ``enter=False`` is delivered without ever being
        submitted.  A record that omitted it could not answer the only
        question a caller replaying a lost response actually has: whether
        the provider has already started acting on the control.

        The ``chord`` fields mirror the enter ones for v2 chord controls,
        where the chord replaces Enter as the submit/steer effect: a
        delivered chord control recorded ``chord_sent`` so a replay knows
        the steer effect landed, not just the text.

        ``submission_observed`` is the provider-visible half (v4): on a
        provider with a submission barrier, ``delivered`` is only reached
        when the composer was seen to take the control, and the stored
        observation plus its evidence reference is what a replay reports
        verbatim.  It is never inferred from transport acknowledgement.
        """
        return self._transition(
            request_id,
            to_state=DELIVERED,
            chunks_sent=chunks_sent,
            enter_attempted=enter_attempted,
            chord=chord,
            chord_attempted=chord_attempted,
            chord_sent=chord_sent,
            submission_observed=submission_observed,
            submission_evidence_ref=submission_evidence_ref,
            sequence_event_outcomes=sequence_event_outcomes,
            evidence_digest=evidence_digest,
        )

    def mark_refused(
        self,
        request_id: str,
        *,
        reason_code: str,
        sequence_event_outcomes: Optional[List[Tuple[int, str]]] = None,
        evidence_digest: Optional[str] = None,
    ) -> ControlInputRecord:
        """Record a refusal decided before any byte was written.

        Legal only from ``intent``.  Calling it after the write is
        claimed is refused rather than recorded, because by then no
        evidence can support the claim that nothing was written.

        For a sequence control the caller passes every event's
        ``refused`` outcome explicitly: the refusal was decided before
        any write, so zero bytes are proven per event, and the stored
        rows say so individually rather than by implication.
        """
        return self._transition(
            request_id,
            to_state=STATE_REFUSED,
            reason_code=reason_code,
            sequence_event_outcomes=sequence_event_outcomes,
            evidence_digest=evidence_digest,
        )

    def mark_ambiguous(
        self,
        request_id: str,
        *,
        reason_code: str,
        chunks_sent: Optional[int] = None,
        enter_attempted: Optional[bool] = None,
        chord: Optional[str] = None,
        chord_attempted: Optional[bool] = None,
        chord_sent: Optional[bool] = None,
        submission_observed: Optional[str] = None,
        submission_evidence_ref: Optional[str] = None,
        sequence_event_outcomes: Optional[List[Tuple[int, str]]] = None,
        evidence_digest: Optional[str] = None,
    ) -> ControlInputRecord:
        """Record that the pane's state is unknowable for this request.

        Terminal for automation.  It is never re-driven and never
        upgraded to delivered by a later observation, because the pane
        cannot distinguish this control's bytes from any other's.

        ``submission_observed`` records what the submission barrier saw
        before the ambiguity was declared — including ``unsubmitted``,
        the positive observation that the composer kept the control.  An
        ``unsubmitted`` observation never downgrades the record to a
        refusal: the text may have reached the pane, so no zero-byte
        proof exists and no re-attempt licence is granted.
        """
        return self._transition(
            request_id,
            to_state=STATE_AMBIGUOUS,
            reason_code=reason_code,
            chunks_sent=chunks_sent,
            enter_attempted=enter_attempted,
            chord=chord,
            chord_attempted=chord_attempted,
            chord_sent=chord_sent,
            submission_observed=submission_observed,
            submission_evidence_ref=submission_evidence_ref,
            sequence_event_outcomes=sequence_event_outcomes,
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
        chord: Optional[str] = None,
        chord_attempted: Optional[bool] = None,
        chord_sent: Optional[bool] = None,
        submission_observed: Optional[str] = None,
        submission_evidence_ref: Optional[str] = None,
        sequence_event_outcomes: Optional[List[Tuple[int, str]]] = None,
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
        # The observation vocabulary is closed for the same reason the
        # outcome vocabulary is: a value outside it is not an observation
        # but a claim nobody verified, and storing it would let a replay
        # report it as fact.  None is always legal — it is the typed null
        # for "no observation was recorded", never a shorthand for one of
        # the three recorded values.
        if submission_observed is not None and submission_observed not in (
            SUBMISSION_OBSERVED_VALUES
        ):
            raise ControlInputJournalError(
                f"unknown submission observation {submission_observed!r}; the recorded "
                f"values are {sorted(SUBMISSION_OBSERVED_VALUES)} and None means no "
                "observation was taken"
            )
        # The per-event outcome vocabulary is closed under the same rule:
        # validated before the transaction so a bad value fails with zero
        # mutation.  An outcome not passed is not written — the stored row
        # keeps what it has, which for a swept record is the honest NULL.
        if sequence_event_outcomes is not None:
            for ordinal, outcome in sequence_event_outcomes:
                if outcome not in SEQUENCE_EVENT_OUTCOMES:
                    raise ControlInputJournalError(
                        f"unknown per-event outcome {outcome!r} at ordinal {ordinal}; "
                        f"the recorded values are {sorted(SEQUENCE_EVENT_OUTCOMES)} and "
                        "an unrecorded outcome stays NULL"
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
                "enter_attempted=COALESCE(?, enter_attempted), "
                "chord=COALESCE(?, chord), "
                "chord_attempted=COALESCE(?, chord_attempted), "
                "chord_sent=COALESCE(?, chord_sent), "
                "submission_observed=COALESCE(?, submission_observed), "
                "submission_evidence_ref=COALESCE(?, submission_evidence_ref), "
                "updated_at=? "
                "WHERE request_id=? AND state=?",
                (
                    to_state,
                    reason_code,
                    chunks_sent,
                    None if enter_attempted is None else int(enter_attempted),
                    chord,
                    None if chord_attempted is None else int(chord_attempted),
                    None if chord_sent is None else int(chord_sent),
                    submission_observed,
                    submission_evidence_ref,
                    moment,
                    request_id,
                    from_state,
                ),
            )
            if sequence_event_outcomes is not None:
                # Same transaction as the state transition: the per-event
                # outcomes and the request's terminal state are one durable
                # fact, never two writes that a crash could split.
                for ordinal, outcome in sequence_event_outcomes:
                    conn.execute(
                        "UPDATE control_input_sequence_event SET outcome=? "
                        "WHERE request_id=? AND ordinal=?",
                        (outcome, request_id, ordinal),
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
                "chunks_sent, enter_attempted, chord, chord_attempted, chord_sent, "
                "submission_observed, submission_evidence_ref, "
                "owner_pid, owner_token, opened_at, updated_at "
                "FROM control_input_request WHERE request_id=?",
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
            sequence_rows = conn.execute(
                "SELECT ordinal, type, text, key, chord, outcome "
                "FROM control_input_sequence_event WHERE request_id=? ORDER BY ordinal",
                (request_id,),
            ).fetchall()
            # None — not an empty tuple — marks a v1/v2 record: it has no
            # events at all, which is a different fact from a sequence
            # whose events are all present.  A NULL outcome stays None:
            # the typed null for "no outcome was recorded for this event",
            # never replaced by an invented one.
            sequence_events = (
                None
                if not sequence_rows
                else tuple(
                    {
                        "ordinal": seq_row[0],
                        "type": seq_row[1],
                        **(
                            {"text": seq_row[2]}
                            if seq_row[1] == "text"
                            else (
                                {"key": seq_row[3]}
                                if seq_row[1] == "key"
                                else {"chord": seq_row[4]} if seq_row[1] == "chord" else {}
                            )
                        ),
                        "outcome": seq_row[5],
                    }
                    for seq_row in sequence_rows
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
            chord=row[12],
            chord_attempted=None if row[13] is None else bool(row[13]),
            chord_sent=None if row[14] is None else bool(row[14]),
            submission_observed=row[15],
            submission_evidence_ref=row[16],
            sequence_events=sequence_events,
            owner_pid=int(row[17]),
            owner_token=row[18],
            opened_at=row[19],
            updated_at=row[20],
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

"""The reversible A -> B -> A worker handback seam (cond-0381 M3-E).

A provider handoff moves a task from worker A to worker B without destroying
A's native conversation. When quota or capability later makes A preferable
again, the supervisor hands the task *back*: it steers B to a safe boundary,
resumes A's original native session exactly, delivers a catch-up packet
describing what B did, and transfers the task. M3-E is the durable seam that
makes that reversible and keeps exactly one worker holding the task at every
boundary.

Four decisions shape this module, and each of them is a bug that has a name.

**A pending handoff is its own row, not a state on the occurrence.** The
accepted design named the state ``handoff-pending``; putting it on
``task_occurrences.state`` is what makes it dangerous. The donor's row would
leave ``open_occurrence_for_agent``, and ``supervisor_drain``'s observer reads
*no open occurrence* as positive ``parked`` — so a concurrent safe drain would
record the donor's **previous** round's digests into its receipt, hand them to
the cohort as a proven boundary, and under Stop announce teardown for the very
pane this milestone keeps alive as rollback insurance. The occurrence stays
``open`` for the donor's whole life; this row carries the handoff.

**Rollback restores the donor by having changed nothing.** Because the
occurrence is untouched while a handoff is pending, restoring the donor is one
compare-and-swap on this row. There is no compensating write to get wrong, no
window where a half-undone transfer is visible, and no way for a rollback to
fail *after* mutating the task.

**A transfer mints a new occurrence; it never rewrites the donor's row.**
``agent_id``, ``round_index`` and the incarnation columns are the immutable
content ``open_occurrence`` adopts a response-loss retry against; two unique
indexes are keyed on them; and both ``retire_worker_pane`` and
``admit_fresh_successor`` read by agent and then finalize by occurrence id with
no ``agent_id`` predicate — so a moved row lets one worker's teardown finalize
another worker's live round. Completing a handoff finalizes the donor
``superseded`` and opens a fresh occurrence for the recipient, in one
transaction, exactly as lost-pane recovery already does for a successor.

**Nothing here carries a native session id.** A handback never moves a native
conversation between workers: the recipient's own session is resumed exactly by
M3-B, whose roster predicates already refuse a cross-harness bind. Recording a
native id here would be a second, unenforced copy of a fact that is only ever
true of one side of the handoff.

What this module stores is references, digests and evidence: occurrence and
incarnation ids, a SHA-256 digest of the catch-up packet, an observed turn
state, and a derived control id. It stores no packet body, no task text, no
provider output, and no secrets. The packet bytes belong to the caller.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, TypeVar

from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError, OperationalError

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import session_lifecycle as sl
from cli_agent_orchestrator.services import task_occurrence as occurrence

_T = TypeVar("_T")

SCHEMA_VERSION = "cao-m3e-task-handoff-v1"

#: The handoff's own lifecycle. ``pending`` is the held window; both terminal
#: states release the holds and leave the three partial unique indexes, so the
#: same pair may hand back again later.
STATE_PENDING = "pending"
STATE_TRANSFERRED = "transferred"
STATE_ROLLED_BACK = "rolled-back"
STATES = frozenset({STATE_PENDING, STATE_TRANSFERRED, STATE_ROLLED_BACK})

#: Whether the one catch-up packet has been handed to the recipient yet.
DELIVERY_PENDING = "pending"
DELIVERY_DELIVERED = "delivered"
DELIVERY_REFUSED = "refused"
DELIVERY_STATES = frozenset({DELIVERY_PENDING, DELIVERY_DELIVERED, DELIVERY_REFUSED})

#: Which side of a pending handoff an agent is on. Both are held; only the
#: recipient has an exemption, and only for the exact packet control id.
ROLE_DONOR = "donor"
ROLE_RECIPIENT = "recipient"

#: The donor's observed managed-turn state, taken from the heartbeat record.
#: ``active`` is refused: a worker mid-turn is not at a safe boundary, and the
#: packet digest would describe a state the donor is still changing.
#: ``unknown`` is *accepted and recorded*. Not every installed provider runs a
#: heartbeat producer, and refusing an unproven turn would make handback
#: impossible on exactly the routes that most need it. The receipt and every
#: read surface carry the value, so a handback taken on unproven quiescence is
#: visible rather than silently equivalent to a proven one.
TURN_ACTIVE = "active"
TURN_TERMINAL = "terminal"
TURN_UNKNOWN = "unknown"
TURN_STATES = frozenset({TURN_ACTIVE, TURN_TERMINAL, TURN_UNKNOWN})
TURN_QUIESCENT = frozenset({TURN_TERMINAL, TURN_UNKNOWN})

#: Nothing in this system tracks processes a worker itself spawned. The
#: containment surface that would prove their absence is permanently
#: ``unproven`` and fails closed, so gating a handback on it would forbid every
#: handback forever. The evidence says what is true — that none are tracked —
#: rather than claiming an absence nobody observed.
CHILD_EFFECTS_NONE_TRACKED = "none-tracked"
CHILD_EFFECT_STATES = frozenset({CHILD_EFFECTS_NONE_TRACKED})

#: Deriving the ids rather than accepting them is what makes every mutating
#: call replay-safe: a caller that lost the response retries the identical
#: request and adopts its own earlier effect.
_PACKET_NAMESPACE = uuid.UUID("03810000-e000-4e00-b7e3-000000000001")
_SUCCESSOR_NAMESPACE = uuid.UUID("03810000-e000-4e00-b7e3-000000000002")

MAX_TEXT_LEN = 512
MAX_DETAIL_LEN = 2000
MAX_SESSION_LEN = 128
MAX_ROUND_INDEX = 1_000_000

_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class TaskHandoffError(RuntimeError):
    code = "task-handoff-error"


class TaskHandoffInvalid(TaskHandoffError):
    code = "task-handoff-invalid"


class TaskHandoffConflict(TaskHandoffError):
    code = "task-handoff-conflict"


class TaskHandoffNotFound(TaskHandoffError):
    code = "task-handoff-not-found"


class TaskHandoffUnavailable(TaskHandoffError):
    code = "task-handoff-unavailable"


class TaskHandoffHeld(TaskHandoffError):
    """Task input was addressed to an agent whose authority is suspended.

    Deliberately not a ``generation_fence.FencedError``. A fence is permanent
    and instructs the caller to advance to a successor generation; this hold is
    *reversible* and applies to the generation the caller already has. A caller
    that read one as the other would abandon the pane a pending handoff is
    keeping alive as its rollback insurance.
    """

    code = "task-handoff-held"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _parse_json(raw: Optional[str]) -> Optional[Any]:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _require_text(value: Any, *, field_name: str, max_len: int = MAX_TEXT_LEN) -> str:
    if not isinstance(value, str) or not value:
        raise TaskHandoffInvalid(f"{field_name} must be a non-empty string; got {value!r}")
    if len(value) > max_len:
        raise TaskHandoffInvalid(f"{field_name} must be at most {max_len} characters")
    return value


def _optional_text(value: Any, *, field_name: str, max_len: int = MAX_TEXT_LEN) -> Optional[str]:
    if value is None:
        return None
    return _require_text(value, field_name=field_name, max_len=max_len)


def _require_uuid(value: Any, *, field_name: str) -> str:
    text_value = _require_text(value, field_name=field_name, max_len=64)
    try:
        if str(uuid.UUID(text_value)) != text_value:
            raise ValueError
    except ValueError as exc:
        raise TaskHandoffInvalid(
            f"{field_name} must be a canonical lowercase UUID; got {text_value!r}"
        ) from exc
    return text_value


def _require_digest(value: Any, *, field_name: str) -> str:
    text_value = _require_text(value, field_name=field_name, max_len=64)
    if _SHA256_RE.fullmatch(text_value) is None:
        raise TaskHandoffInvalid(
            f"{field_name} must be 64 lowercase hex characters; got {text_value!r}"
        )
    return text_value


def _normalise_session_name(value: Any) -> str:
    raw = _require_text(value, field_name="session_name", max_len=MAX_SESSION_LEN)
    name = sl.normalise_session_name(raw)
    if len(name) > MAX_SESSION_LEN:
        raise TaskHandoffInvalid(
            f"session_name normalises to more than {MAX_SESSION_LEN} characters"
        )
    return name


def _anchor_caller_transaction(db: Any, *, immediate: bool = False) -> None:
    """Give SQLite a real outer transaction before a savepoint.

    Same reason as M3-D's copy: SQLAlchemy's Session transaction is lazy on
    SQLite, so a ``begin_nested`` issued first becomes the outer transaction and
    RELEASE commits it — which would make the caller's later rollback unable to
    undo this write. Load-bearing here beyond hygiene: ``complete_handoff``
    finalizes one occurrence and opens another inside one savepoint, and a
    RELEASE that committed would make a failure between them observable.

    ``immediate`` takes SQLite's write lock up front. A deferred transaction
    acquires it only at the first write, so two callers can both complete their
    *reads* — including a uniqueness precondition — before either writes. That
    is exactly how two concurrent ``begin_handoff`` calls could each observe no
    pending handoff for one agent and both insert.
    """
    connection = db.connection()
    if connection.dialect.name != "sqlite":
        return
    driver_connection = connection.connection.driver_connection
    if not driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")


def _with_session(
    fn: Callable[[Any], _T], db: Any, *, unavailable: str, immediate: bool = False
) -> _T:
    if db is not None:
        try:
            _anchor_caller_transaction(db, immediate=immediate)
            with db.begin_nested():
                return fn(db)
        except TaskHandoffError:
            raise
        except occurrence.TaskOccurrenceUnavailable as exc:
            raise TaskHandoffUnavailable(f"{unavailable}: {exc}") from exc
        except occurrence.TaskOccurrenceError:
            raise
        except (IntegrityError, OperationalError) as exc:
            raise TaskHandoffUnavailable(f"{unavailable}: {exc}") from exc
    last_error: Optional[BaseException] = None
    for _attempt in range(5):
        try:
            with database.SessionLocal() as session:
                _anchor_caller_transaction(session, immediate=immediate)
                result = fn(session)
                session.commit()
                return result
        except TaskHandoffError:
            raise
        except occurrence.TaskOccurrenceUnavailable as exc:
            # Contention inside a nested occurrence call arrives typed as an
            # occurrence error, so the generic clause below never sees it.
            # Retrying here keeps routine SQLite contention from surfacing as a
            # 503 on the one call that is hardest to re-drive.
            last_error = exc
            time.sleep(0.05)
        except occurrence.TaskOccurrenceError:
            raise
        except (IntegrityError, OperationalError) as exc:
            last_error = exc
            time.sleep(0.05)
    raise TaskHandoffUnavailable(f"{unavailable}: {last_error}")


# ---------------------------------------------------------------------------
# derived ids
# ---------------------------------------------------------------------------


def packet_control_id_for(handoff_id: str, to_agent_id: str) -> str:
    """The one control id the catch-up packet is ever delivered under.

    Derived rather than minted so delivery is exactly-once without a second
    ledger: the control-input contract already adopts a replay of a known
    control id, so a caller that lost the response retries the identical id and
    gets its own earlier outcome instead of typing the packet twice.
    """
    return str(uuid.uuid5(_PACKET_NAMESPACE, f"{handoff_id}:{to_agent_id}"))


def successor_occurrence_id_for(handoff_id: str) -> str:
    """The occurrence id a completed handoff opens for the recipient.

    Derived from the handoff alone, so a retried ``complete_handoff`` opens the
    same id and M3-D's own adopt-on-replay returns the existing row rather than
    a second round for the same transfer.
    """
    return str(uuid.uuid5(_SUCCESSOR_NAMESPACE, handoff_id))


# ---------------------------------------------------------------------------
# quiescence evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuiescenceEvidence:
    """What was observed about the donor at the moment the handoff began.

    Bound to the donor's *exact* incarnation on purpose. "The agent was idle"
    is not a fact about a task — a replacement pane for the same stable agent
    is a different physical worker, and evidence that does not name which one
    it watched cannot refuse a stale observation.
    """

    incarnation_id: str
    terminal_id: str
    turn_state: str
    #: When the observation was made. Required, and never defaulted to "now":
    #: this payload is digested into the handoff's immutable content, so a
    #: service-invented timestamp would make a caller's response-loss retry
    #: hash differently and conflict with its own earlier effect. The caller
    #: watched the turn; it can say when.
    observed_at: str
    generation: Optional[str] = None
    known_child_effects: str = CHILD_EFFECTS_NONE_TRACKED
    boundary_digest: Optional[str] = None
    report_digest: Optional[str] = None
    checkpoint_digest: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "incarnation_id", _require_text(self.incarnation_id, field_name="incarnation_id")
        )
        object.__setattr__(
            self, "terminal_id", _require_text(self.terminal_id, field_name="terminal_id")
        )
        object.__setattr__(
            self, "generation", _optional_text(self.generation, field_name="generation")
        )
        if self.turn_state not in TURN_STATES:
            raise TaskHandoffInvalid(
                f"turn_state must be one of {sorted(TURN_STATES)}; got {self.turn_state!r}"
            )
        if self.known_child_effects not in CHILD_EFFECT_STATES:
            raise TaskHandoffInvalid(
                f"known_child_effects must be one of {sorted(CHILD_EFFECT_STATES)}; "
                f"got {self.known_child_effects!r}"
            )
        for name in ("boundary_digest", "report_digest", "checkpoint_digest"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _require_digest(value, field_name=name))
        object.__setattr__(
            self, "observed_at", _require_text(self.observed_at, field_name="observed_at")
        )

    @property
    def quiescent(self) -> bool:
        return self.turn_state in TURN_QUIESCENT

    @property
    def turn_proven(self) -> bool:
        """Whether quiescence was *observed* rather than merely not contradicted."""
        return self.turn_state == TURN_TERMINAL

    def payload(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "incarnation_id": self.incarnation_id,
            "terminal_id": self.terminal_id,
            "generation": self.generation,
            "turn_state": self.turn_state,
            "turn_proven": self.turn_proven,
            "known_child_effects": self.known_child_effects,
            "boundary_digest": self.boundary_digest,
            "report_digest": self.report_digest,
            "checkpoint_digest": self.checkpoint_digest,
            "observed_at": self.observed_at,
        }


# The conditions a rollback needs are re-derived at rollback time by
# ``_donor_restoration`` from ``task_occurrence_id`` and ``from_incarnation_id``,
# both first-class columns. A serialised copy of them would be a second
# statement of the same facts that nothing reads and nothing keeps in step.


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------


def _row_dict(row: Any) -> dict[str, Any]:
    return {
        "handoff_id": row.handoff_id,
        "schema_version": row.schema_version,
        "session_name": row.session_name,
        "task_occurrence_id": row.task_occurrence_id,
        "from_agent_id": row.from_agent_id,
        "to_agent_id": row.to_agent_id,
        "from_incarnation_id": row.from_incarnation_id,
        "from_terminal_id": row.from_terminal_id,
        "from_generation": row.from_generation,
        "donor_revision": int(row.donor_revision or 0),
        "packet_digest": row.packet_digest,
        "packet_control_id": row.packet_control_id,
        "quiescence": _parse_json(row.quiescence_json),
        "quiescence_digest": row.quiescence_digest,
        "delivery_state": row.delivery_state,
        "delivery_outcome": row.delivery_outcome,
        "delivery_receipt": row.delivery_receipt,
        "to_incarnation_id": row.to_incarnation_id,
        "to_terminal_id": row.to_terminal_id,
        "to_generation": row.to_generation,
        # Derived, not stored: the recipient has been briefed exactly when the
        # packet reached it. A rollback after that point releases a worker that
        # already holds an instruction to continue the task, so every reader —
        # and the receipt — has to be able to see it.
        "recipient_briefed": row.delivery_state == DELIVERY_DELIVERED,
        "successor_occurrence_id": row.successor_occurrence_id,
        "state": row.state,
        "receipt_digest": row.receipt_digest,
        "detail": row.detail,
        "initiated_by": row.initiated_by,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "settled_at": row.settled_at,
    }


def _row_by_id(db: Any, handoff_id: str) -> Any:
    return (
        db.query(database.TaskOccurrenceHandoffModel)
        .filter(database.TaskOccurrenceHandoffModel.handoff_id == handoff_id)
        .one_or_none()
    )


def _pending_rows_for_agent(db: Any, agent_id: str) -> list[Any]:
    """Every pending handoff naming this agent, oldest first.

    Deliberately not ``one_or_none()``. The three partial unique indexes make
    one row per *slot*, not one row per agent — an agent can legally appear
    once as donor and once as recipient as far as SQLite is concerned, and this
    query runs on the byte-admission hot path where an escaping
    ``MultipleResultsFound`` would be an untyped 500 in a subsystem whose whole
    contract is that every path produces a typed outcome. The precondition in
    ``_begin_once`` is what prevents the second row; this read stays total.
    """
    rows: list[Any] = (
        db.query(database.TaskOccurrenceHandoffModel)
        .filter(
            database.TaskOccurrenceHandoffModel.state == STATE_PENDING,
            (database.TaskOccurrenceHandoffModel.from_agent_id == agent_id)
            | (database.TaskOccurrenceHandoffModel.to_agent_id == agent_id),
        )
        .order_by(database.TaskOccurrenceHandoffModel.created_at)
        .all()
    )
    return rows


def _pending_row_for_agent(db: Any, agent_id: str) -> Any:
    rows = _pending_rows_for_agent(db, agent_id)
    return rows[0] if rows else None


def _pending_row_for_occurrence(db: Any, task_occurrence_id: str) -> Any:
    return (
        db.query(database.TaskOccurrenceHandoffModel)
        .filter(
            database.TaskOccurrenceHandoffModel.task_occurrence_id == task_occurrence_id,
            database.TaskOccurrenceHandoffModel.state == STATE_PENDING,
        )
        .one_or_none()
    )


def receipt_digest(record: Mapping[str, Any]) -> str:
    """The settled handoff's content digest.

    Includes the turn state so a receipt cannot be read as proof of a
    quiescence that was only ever unproven.
    """
    return digest(
        {
            "schema": SCHEMA_VERSION,
            "effect": "task-handoff",
            "handoff_id": record["handoff_id"],
            "session_name": record["session_name"],
            "task_occurrence_id": record["task_occurrence_id"],
            "from_agent_id": record["from_agent_id"],
            "to_agent_id": record["to_agent_id"],
            "packet_digest": record["packet_digest"],
            "quiescence_digest": record["quiescence_digest"],
            "delivery_state": record["delivery_state"],
            # A rollback that released an already-briefed recipient is a
            # materially different outcome from one that released nobody, and a
            # receipt that could not tell them apart would let E2 skip the
            # cancellation the first case requires.
            "recipient_briefed": record.get("recipient_briefed", False),
            "to_incarnation_id": record.get("to_incarnation_id"),
            "successor_occurrence_id": record.get("successor_occurrence_id"),
            "state": record["state"],
        }
    )


# ---------------------------------------------------------------------------
# beginning a handoff
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BeginRequest:
    """Enter the held window for one occurrence.

    The caller mints ``handoff_id``. Every other id this handoff needs — the
    packet's control id and the successor occurrence id — is derived from it,
    so one retried request reproduces the whole operation exactly.
    """

    handoff_id: str
    session_name: str
    task_occurrence_id: str
    to_agent_id: str
    packet_digest: str
    evidence: QuiescenceEvidence
    initiated_by: str
    detail: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "handoff_id", _require_uuid(self.handoff_id, field_name="handoff_id")
        )
        object.__setattr__(self, "session_name", _normalise_session_name(self.session_name))
        object.__setattr__(
            self,
            "task_occurrence_id",
            _require_uuid(self.task_occurrence_id, field_name="task_occurrence_id"),
        )
        object.__setattr__(
            self, "to_agent_id", _require_uuid(self.to_agent_id, field_name="to_agent_id")
        )
        object.__setattr__(
            self, "packet_digest", _require_digest(self.packet_digest, field_name="packet_digest")
        )
        object.__setattr__(
            self, "initiated_by", _require_text(self.initiated_by, field_name="initiated_by")
        )
        object.__setattr__(
            self, "detail", _optional_text(self.detail, field_name="detail", max_len=MAX_DETAIL_LEN)
        )
        if not isinstance(self.evidence, QuiescenceEvidence):
            raise TaskHandoffInvalid(
                f"evidence must be a QuiescenceEvidence; got {type(self.evidence).__name__}"
            )


def _begin_once(db: Any, request: BeginRequest) -> dict[str, Any]:
    existing = _row_by_id(db, request.handoff_id)
    if existing is not None:
        stored = {
            "session_name": existing.session_name,
            "task_occurrence_id": existing.task_occurrence_id,
            "to_agent_id": existing.to_agent_id,
            "packet_digest": existing.packet_digest,
            "quiescence_digest": existing.quiescence_digest,
        }
        requested = {
            "session_name": request.session_name,
            "task_occurrence_id": request.task_occurrence_id,
            "to_agent_id": request.to_agent_id,
            "packet_digest": request.packet_digest,
            "quiescence_digest": digest(request.evidence.payload()),
        }
        if stored != requested:
            raise TaskHandoffConflict(
                f"handoff {request.handoff_id} already exists with different immutable content"
            )
        record = _row_dict(existing)
        record["adopted"] = True
        return record

    donor = occurrence.get_occurrence(request.task_occurrence_id, db=db)
    if donor["state"] != occurrence.STATE_OPEN:
        raise TaskHandoffConflict(
            f"task occurrence {request.task_occurrence_id} is {donor['state']!r}; only an open "
            "occurrence can be handed back"
        )
    if donor["session_name"] != request.session_name:
        raise TaskHandoffConflict(
            f"task occurrence {request.task_occurrence_id} belongs to session "
            f"{donor['session_name']!r}, not {request.session_name!r}"
        )
    from_agent_id = str(donor["agent_id"])
    if from_agent_id == request.to_agent_id:
        raise TaskHandoffInvalid(
            f"stable agent {from_agent_id} already holds occurrence {request.task_occurrence_id}; "
            "a handback needs two different agents"
        )

    # The evidence must describe *this* occurrence's executing incarnation. An
    # observation of some other pane would let a handoff begin against a donor
    # nobody actually watched go quiet.
    if request.evidence.incarnation_id != donor["incarnation_id"]:
        raise TaskHandoffConflict(
            f"quiescence evidence names incarnation {request.evidence.incarnation_id!r}, but "
            f"occurrence {request.task_occurrence_id} is executing on {donor['incarnation_id']!r}"
        )
    if not request.evidence.quiescent:
        raise TaskHandoffConflict(
            f"stable agent {from_agent_id} has an active managed turn; steer it to a safe "
            "boundary before beginning a handback"
        )

    # The recipient must be free to take a round. Checked here because here is
    # the last point at which refusing is free: past this the recipient is
    # held, its own unrelated round's input is refused as `handoff-held`, and
    # the catch-up packet is typed into its context by the exemption. The
    # conflict would otherwise surface only at transfer, after the resume and
    # after the packet landed, and typed bytes are not reversible.
    recipient_round = occurrence.open_occurrence_for_agent(request.to_agent_id, db=db)
    if recipient_round is not None:
        raise TaskHandoffConflict(
            f"stable agent {request.to_agent_id} already holds open task occurrence "
            f"{recipient_round['task_occurrence_id']} (round {recipient_round['round_index']}); "
            "a handback recipient must be free to take the round"
        )

    # Read-then-insert on the three pending slots. The partial unique indexes
    # forbid a second row per *slot*, but they are independent, so nothing in
    # the schema stops one agent being donor of one handoff and recipient of
    # another. This read-then-insert is the only thing that does, which is why
    # the surrounding transaction takes SQLite's write lock up front — two
    # deferred transactions would both complete this read before either wrote.
    for agent_id, role in ((from_agent_id, ROLE_DONOR), (request.to_agent_id, ROLE_RECIPIENT)):
        held = _pending_row_for_agent(db, agent_id)
        if held is not None:
            raise TaskHandoffConflict(
                f"stable agent {agent_id} is already the {role} of pending handoff "
                f"{held.handoff_id}; one agent is party to one handoff at a time"
            )
    busy = _pending_row_for_occurrence(db, request.task_occurrence_id)
    if busy is not None:
        raise TaskHandoffConflict(
            f"task occurrence {request.task_occurrence_id} already has pending handoff "
            f"{busy.handoff_id}"
        )

    stamp = _now()
    evidence_payload = request.evidence.payload()
    row = database.TaskOccurrenceHandoffModel(
        handoff_id=request.handoff_id,
        schema_version=SCHEMA_VERSION,
        session_name=request.session_name,
        task_occurrence_id=request.task_occurrence_id,
        from_agent_id=from_agent_id,
        to_agent_id=request.to_agent_id,
        from_incarnation_id=request.evidence.incarnation_id,
        from_terminal_id=request.evidence.terminal_id,
        from_generation=request.evidence.generation,
        # The revision the packet digest describes. A transfer compares it.
        donor_revision=int(donor["revision"]),
        packet_digest=request.packet_digest,
        packet_control_id=packet_control_id_for(request.handoff_id, request.to_agent_id),
        quiescence_json=canonical_json(evidence_payload),
        quiescence_digest=digest(evidence_payload),
        delivery_state=DELIVERY_PENDING,
        delivery_outcome=None,
        delivery_receipt=None,
        to_incarnation_id=None,
        to_terminal_id=None,
        to_generation=None,
        successor_occurrence_id=None,
        state=STATE_PENDING,
        receipt_digest=None,
        detail=request.detail,
        initiated_by=request.initiated_by,
        created_at=stamp,
        updated_at=stamp,
        settled_at=None,
    )
    db.add(row)
    db.flush()
    record = _row_dict(row)
    record["adopted"] = False
    return record


def begin_handoff(request: BeginRequest, db: Any = None) -> dict[str, Any]:
    """Enter the held window. Performs no effect and touches no occurrence."""
    if not isinstance(request, BeginRequest):
        raise TaskHandoffInvalid(f"request must be a BeginRequest; got {type(request).__name__}")
    return _with_session(
        lambda session: _begin_once(session, request),
        db,
        unavailable="concurrent handoff begins kept conflicting",
        # The per-agent invariant is a read-then-insert that no index can
        # express; the write lock is what makes it hold under two concurrent
        # supervisor actions.
        immediate=True,
    )


# ---------------------------------------------------------------------------
# delivering the one packet
# ---------------------------------------------------------------------------


def _record_delivery_once(
    db: Any,
    handoff_id: str,
    *,
    state: str,
    outcome: Optional[str],
    receipt: Optional[str],
    incarnation: Optional[occurrence.EffectIncarnation],
) -> dict[str, Any]:
    row = _row_by_id(db, handoff_id)
    if row is None:
        raise TaskHandoffNotFound(f"handoff {handoff_id} is not recorded")
    delivered_to = incarnation.incarnation_id if incarnation is not None else None
    if (
        row.delivery_state == state
        and row.delivery_outcome == outcome
        and row.to_incarnation_id == delivered_to
    ):
        record = _row_dict(row)
        record["adopted"] = True
        return record
    if row.state != STATE_PENDING:
        raise TaskHandoffConflict(
            f"handoff {handoff_id} is {row.state!r}; a settled handoff never records a "
            "later delivery"
        )
    if row.delivery_state == DELIVERY_DELIVERED:
        raise TaskHandoffConflict(
            f"handoff {handoff_id} already delivered its packet to incarnation "
            f"{row.to_incarnation_id!r} under control {row.packet_control_id}; the packet is "
            "delivered exactly once"
        )
    if state == DELIVERY_DELIVERED and incarnation is None:
        raise TaskHandoffInvalid(
            "a delivered packet must name the recipient incarnation that received it"
        )
    if incarnation is not None and incarnation.incarnation_id == row.from_incarnation_id:
        raise TaskHandoffConflict(
            f"handoff {handoff_id} would record its packet as delivered to the donor's own "
            f"incarnation {row.from_incarnation_id!r}"
        )
    stamp = _now()
    # The CAS is on the two states that actually gate this transition. A
    # refused delivery stays retryable (the packet never landed); a delivered
    # one is final, which is what makes "exactly once" a property of the row
    # rather than of the caller remembering a counter.
    result = db.execute(
        sa_update(database.TaskOccurrenceHandoffModel)
        .where(
            database.TaskOccurrenceHandoffModel.handoff_id == handoff_id,
            database.TaskOccurrenceHandoffModel.state == STATE_PENDING,
            database.TaskOccurrenceHandoffModel.delivery_state != DELIVERY_DELIVERED,
        )
        .values(
            delivery_state=state,
            delivery_outcome=outcome,
            delivery_receipt=receipt,
            to_incarnation_id=delivered_to,
            to_terminal_id=incarnation.terminal_id if incarnation is not None else None,
            to_generation=incarnation.generation if incarnation is not None else None,
            updated_at=stamp,
        )
    )
    if result.rowcount != 1:
        raise TaskHandoffConflict(
            f"handoff {handoff_id} moved concurrently; read and retry the delivery record"
        )
    db.refresh(row)
    record = _row_dict(row)
    record["adopted"] = False
    return record


def record_packet_delivery(
    handoff_id: str,
    *,
    delivered: bool,
    incarnation: Optional[occurrence.EffectIncarnation] = None,
    outcome: Optional[str] = None,
    receipt: Optional[str] = None,
    db: Any = None,
) -> dict[str, Any]:
    """Bind the outcome of the one packet delivery, and to whom.

    The delivery itself happens through the ordinary control-input path under
    the derived ``packet_control_id``; this only records what that path
    answered. A refused delivery leaves the handoff pending and rollback-able,
    which is the point: the recipient never received context, so the donor is
    still the right worker.

    ``incarnation`` is required for a delivered packet and is the reason this
    call exists at all rather than a boolean flag. The derived control id binds
    no terminal, and the control-input journal refuses to re-deliver a known
    control id to a replacement pane, so the packet is unreachable to any
    incarnation but the one that first received it. Recording which one that
    was is what lets the transfer refuse a recipient that never read it.
    """
    handoff_id = _require_uuid(handoff_id, field_name="handoff_id")
    state = DELIVERY_DELIVERED if delivered else DELIVERY_REFUSED
    outcome = _optional_text(outcome, field_name="outcome")
    receipt = _optional_text(receipt, field_name="receipt")
    if incarnation is not None and not isinstance(incarnation, occurrence.EffectIncarnation):
        raise TaskHandoffInvalid(
            f"incarnation must be an EffectIncarnation; got {type(incarnation).__name__}"
        )
    return _with_session(
        lambda session: _record_delivery_once(
            session,
            handoff_id,
            state=state,
            outcome=outcome,
            receipt=receipt,
            incarnation=incarnation,
        ),
        db,
        unavailable="concurrent packet-delivery records kept conflicting",
    )


# ---------------------------------------------------------------------------
# completing the transfer
# ---------------------------------------------------------------------------


def _next_round_index(db: Any, session_name: str, agent_id: str) -> int:
    """The recipient's own next round number.

    Derived rather than accepted because the caller cannot see the collision it
    would cause: ``ix_task_occurrences_round`` is UNIQUE over
    ``(session_name, agent_id, round_index)`` and is *not* partial, so a
    finalized round of the recipient's own past occupies the slot just as much
    as a live one.
    """
    records = occurrence.list_occurrences(session_name, agent_id=agent_id, db=db)
    if not records:
        return 0
    highest = max(int(item["round_index"]) for item in records)
    if highest + 1 > MAX_ROUND_INDEX:
        raise TaskHandoffInvalid(
            f"stable agent {agent_id} has reached round {highest}; refusing to mint another"
        )
    return highest + 1


def _complete_once(
    db: Any,
    handoff_id: str,
    *,
    incarnation: occurrence.EffectIncarnation,
    expected_revision: int,
    completed_by: str,
) -> dict[str, Any]:
    row = _row_by_id(db, handoff_id)
    if row is None:
        raise TaskHandoffNotFound(f"handoff {handoff_id} is not recorded")
    successor_id = successor_occurrence_id_for(handoff_id)
    if row.state == STATE_TRANSFERRED and row.successor_occurrence_id == successor_id:
        # A replay must agree with what actually happened. Adopting a retry
        # that names a different incarnation would let a caller believe it
        # transferred to a pane the transfer never touched.
        if incarnation.incarnation_id != row.to_incarnation_id:
            raise TaskHandoffConflict(
                f"handoff {handoff_id} already transferred to incarnation "
                f"{row.to_incarnation_id!r}; this replay names {incarnation.incarnation_id!r}"
            )
        record = _row_dict(row)
        record["adopted"] = True
        record["successor_occurrence"] = occurrence.get_occurrence(successor_id, db=db)
        return record
    if row.state != STATE_PENDING:
        raise TaskHandoffConflict(
            f"handoff {handoff_id} is {row.state!r}; only a pending handoff transfers"
        )
    if row.delivery_state != DELIVERY_DELIVERED:
        raise TaskHandoffConflict(
            f"handoff {handoff_id} has not delivered its catch-up packet "
            f"({row.delivery_state!r}); the recipient never received the context this transfer "
            "assumes it has"
        )
    if incarnation.incarnation_id == row.from_incarnation_id:
        raise TaskHandoffConflict(
            f"handoff {handoff_id} would transfer to the donor's own incarnation "
            f"{row.from_incarnation_id!r}; the recipient executes on its own pane"
        )
    # The packet reached one exact incarnation and can never reach another: the
    # derived control id is stable across incarnations, and the control-input
    # journal refuses a known id whose target moved. Transferring to a
    # replacement pane would leave the sole task authority holding no context
    # while the record and the receipt both assert that it read the packet.
    # A replaced recipient is a rollback and a new handoff, not a transfer.
    if incarnation.incarnation_id != row.to_incarnation_id:
        raise TaskHandoffConflict(
            f"handoff {handoff_id} delivered its packet to incarnation "
            f"{row.to_incarnation_id!r}, not {incarnation.incarnation_id!r}; the packet cannot "
            "be re-delivered to a replacement pane, so roll this handoff back and begin a new "
            "one rather than transferring to an incarnation that never read it"
        )

    donor = occurrence.get_occurrence(row.task_occurrence_id, db=db)
    if donor["agent_id"] != row.from_agent_id:
        raise TaskHandoffConflict(
            f"task occurrence {row.task_occurrence_id} is held by {donor['agent_id']!r}, not the "
            f"handoff's donor {row.from_agent_id!r}"
        )
    # The packet describes the donor at one exact revision. If the donor moved
    # while held, something wrote to it after the digest was taken, and the
    # context the recipient read is not the round it is being given. Refusing
    # is recoverable -- roll back and begin again with a fresh packet -- and
    # transferring is not.
    if int(donor["revision"]) != int(row.donor_revision or 0):
        raise TaskHandoffConflict(
            f"task occurrence {row.task_occurrence_id} moved from revision {row.donor_revision} "
            f"to {donor['revision']} while held; the catch-up packet no longer describes the "
            "round being transferred, so roll this handoff back and begin a new one"
        )

    # One transaction, two occurrence writes. Between them there is no state a
    # reader can observe: the donor is finalized and the recipient is opened, or
    # neither happened. A gap here would be zero task authority with no record
    # of who should hold it.
    # Carried forward verbatim rather than re-derived. The seed says what
    # durable artifacts the round produced; the catch-up packet is what the
    # recipient reads. Inventing a seed here would let the transfer describe
    # context nobody produced, and an undecodable carrier is honestly empty
    # rather than optimistically complete.
    seed = occurrence.decode_seed((donor.get("current") or {}).get("seed")) or occurrence.EMPTY_SEED
    occurrence.finalize_occurrence(
        occurrence.FinalizeRequest(
            task_occurrence_id=row.task_occurrence_id,
            expected_revision=expected_revision,
            disposition=occurrence.DISPOSITION_SUPERSEDED,
            finalized_by=completed_by,
            note=f"handed back to {row.to_agent_id} by handoff {handoff_id}",
        ),
        db=db,
    )
    opened = occurrence.open_occurrence(
        occurrence.OpenRequest(
            task_occurrence_id=successor_id,
            session_name=row.session_name,
            agent_id=row.to_agent_id,
            round_index=_next_round_index(db, row.session_name, row.to_agent_id),
            dispatch_digest=donor["dispatch_digest"],
            incarnation=incarnation,
            seed=seed,
            # The only link the goal track, the auditor, or a later
            # reconstruction has back to the round this one continues. It is an
            # existing opaque slot; M3-E adds no column to M3-D's table.
            dispatch_provenance={
                "kind": "m3e-handback",
                "handoff_id": handoff_id,
                "predecessor_occurrence_id": row.task_occurrence_id,
                "predecessor_agent_id": row.from_agent_id,
                "packet_digest": row.packet_digest,
                "quiescence_digest": row.quiescence_digest,
            },
        ),
        db=db,
    )

    stamp = _now()
    result = db.execute(
        sa_update(database.TaskOccurrenceHandoffModel)
        .where(
            database.TaskOccurrenceHandoffModel.handoff_id == handoff_id,
            database.TaskOccurrenceHandoffModel.state == STATE_PENDING,
        )
        .values(
            state=STATE_TRANSFERRED,
            successor_occurrence_id=successor_id,
            updated_at=stamp,
            settled_at=stamp,
        )
    )
    if result.rowcount != 1:
        raise TaskHandoffConflict(
            f"handoff {handoff_id} moved concurrently; read and retry the transfer"
        )
    db.refresh(row)
    record = _row_dict(row)
    receipt = receipt_digest(record)
    db.execute(
        sa_update(database.TaskOccurrenceHandoffModel)
        .where(database.TaskOccurrenceHandoffModel.handoff_id == handoff_id)
        .values(receipt_digest=receipt)
    )
    db.refresh(row)
    record = _row_dict(row)
    record["adopted"] = False
    record["successor_occurrence"] = opened
    return record


def complete_handoff(
    handoff_id: str,
    *,
    incarnation: occurrence.EffectIncarnation,
    expected_revision: int,
    completed_by: str,
    db: Any = None,
) -> dict[str, Any]:
    """Transfer the task: finalize the donor's round and open the recipient's.

    Atomic by construction rather than by compensation. The donor occurrence is
    finalized ``superseded`` and a fresh occurrence is opened for the recipient
    inside one savepoint, so a crash between them leaves the handoff pending and
    still rollback-able.
    """
    handoff_id = _require_uuid(handoff_id, field_name="handoff_id")
    if not isinstance(incarnation, occurrence.EffectIncarnation):
        raise TaskHandoffInvalid(
            f"incarnation must be an EffectIncarnation; got {type(incarnation).__name__}"
        )
    if not isinstance(expected_revision, int) or expected_revision < 0:
        raise TaskHandoffInvalid(
            f"expected_revision must be a non-negative int; got {expected_revision!r}"
        )
    completed_by = _require_text(completed_by, field_name="completed_by")
    return _with_session(
        lambda session: _complete_once(
            session,
            handoff_id,
            incarnation=incarnation,
            expected_revision=expected_revision,
            completed_by=completed_by,
        ),
        db,
        unavailable="concurrent handoff transfers kept conflicting",
    )


# ---------------------------------------------------------------------------
# rolling back
# ---------------------------------------------------------------------------


def _rollback_once(db: Any, handoff_id: str, *, rolled_back_by: str, reason: str) -> dict[str, Any]:
    row = _row_by_id(db, handoff_id)
    if row is None:
        raise TaskHandoffNotFound(f"handoff {handoff_id} is not recorded")
    if row.state == STATE_ROLLED_BACK:
        record = _row_dict(row)
        record["adopted"] = True
        record.update(_donor_restoration(db, row))
        return record
    if row.state == STATE_TRANSFERRED:
        raise TaskHandoffConflict(
            f"handoff {handoff_id} already transferred occurrence {row.task_occurrence_id} to "
            f"{row.to_agent_id}; a completed transfer is not rolled back — the recipient is the "
            "task's authority and a further move is a new handoff"
        )

    restoration = _donor_restoration(db, row)
    stamp = _now()
    detail = reason if not row.detail else f"{row.detail}; {reason}"
    result = db.execute(
        sa_update(database.TaskOccurrenceHandoffModel)
        .where(
            database.TaskOccurrenceHandoffModel.handoff_id == handoff_id,
            database.TaskOccurrenceHandoffModel.state == STATE_PENDING,
        )
        .values(
            state=STATE_ROLLED_BACK,
            detail=detail[:MAX_DETAIL_LEN],
            updated_at=stamp,
            settled_at=stamp,
        )
    )
    if result.rowcount != 1:
        raise TaskHandoffConflict(
            f"handoff {handoff_id} moved concurrently; read and retry the rollback"
        )
    db.refresh(row)
    record = _row_dict(row)
    receipt = receipt_digest(record)
    db.execute(
        sa_update(database.TaskOccurrenceHandoffModel)
        .where(database.TaskOccurrenceHandoffModel.handoff_id == handoff_id)
        .values(receipt_digest=receipt)
    )
    db.refresh(row)
    record = _row_dict(row)
    record["adopted"] = False
    record["rolled_back_by"] = rolled_back_by
    record.update(restoration)
    return record


def _donor_restoration(db: Any, row: Any) -> dict[str, Any]:
    """Whether releasing the hold actually gives the donor back a usable worker.

    Reported, never enforced. A rollback that refused because the donor's pane
    had died would leave both agents held with no forward path — strictly worse
    than releasing them and saying plainly that the donor now needs lost-pane
    recovery.
    """
    try:
        donor = occurrence.get_occurrence(row.task_occurrence_id, db=db)
    except occurrence.TaskOccurrenceError as exc:
        return {
            "donor_restored": False,
            "donor_detail": f"the donor occurrence is unreadable: {exc}",
        }
    if donor["state"] != occurrence.STATE_OPEN:
        return {
            "donor_restored": False,
            "donor_detail": (
                f"the donor occurrence is {donor['state']!r}; the donor holds no task to resume"
            ),
        }
    if donor["incarnation_id"] != row.from_incarnation_id:
        return {
            "donor_restored": False,
            "donor_detail": (
                "the donor occurrence moved to incarnation "
                f"{donor['incarnation_id']!r}; recover it rather than assuming the observed pane"
            ),
        }
    return {"donor_restored": True, "donor_detail": "the donor holds its open occurrence"}


def rollback_handoff(
    handoff_id: str, *, rolled_back_by: str, reason: str, db: Any = None
) -> dict[str, Any]:
    """Release the holds and leave the donor exactly as it was.

    There is nothing to compensate: a pending handoff never wrote to the task,
    so restoring the donor's authority and its managed input is this one
    compare-and-swap.

    A rollback after the packet was delivered is still permitted, and still
    should be — a rollback that could refuse would leave both agents held with
    no forward path. But it is not the same outcome as one that released
    nobody: the recipient has already been told, in its own context, that it is
    taking over the task. ``recipient_briefed`` says so in the result and is
    folded into the receipt, so the caller that owns the pane is obliged to
    send the cancellation and a later reader can tell that it was owed.
    """
    handoff_id = _require_uuid(handoff_id, field_name="handoff_id")
    rolled_back_by = _require_text(rolled_back_by, field_name="rolled_back_by")
    reason = _require_text(reason, field_name="reason", max_len=MAX_DETAIL_LEN)
    return _with_session(
        lambda session: _rollback_once(
            session, handoff_id, rolled_back_by=rolled_back_by, reason=reason
        ),
        db,
        unavailable="concurrent handoff rollbacks kept conflicting",
    )


# ---------------------------------------------------------------------------
# the admission hold
# ---------------------------------------------------------------------------


def hold_for_agent(agent_id: str, db: Any = None) -> Optional[dict[str, Any]]:
    """The pending handoff this agent is party to, if any."""
    if not isinstance(agent_id, str) or not agent_id:
        return None
    try:
        agent_id = _require_uuid(agent_id, field_name="agent_id")
    except TaskHandoffInvalid:
        return None

    def _read(session: Any) -> Optional[dict[str, Any]]:
        row = _pending_row_for_agent(session, agent_id)
        if row is None:
            return None
        record = _row_dict(row)
        record["role"] = ROLE_DONOR if row.from_agent_id == agent_id else ROLE_RECIPIENT
        return record

    return _with_session(_read, db, unavailable="handoff hold could not be read")


def hold_refusal(
    agent_id: Optional[str], *, control_id: Optional[str] = None, db: Any = None
) -> Optional[str]:
    """The one predicate every task-byte path asks before admitting input.

    Returns a refusal sentence, or ``None`` when the agent may be written to.

    Both sides of a pending handoff are held, for different named reasons. The
    donor is held because the catch-up packet describes a state it must stop
    changing: a steer that lands after the digest was taken means the packet
    the recipient reads is already wrong. The recipient is held because the
    supervisor's roster view can be stale — it believes that agent dormant —
    and task input that arrives before the packet has the recipient acting on
    context it does not have, for an occurrence it does not yet hold.

    The recipient's exemption is exact: the one derived control id the packet
    is delivered under, and nothing else. Without it the hold would refuse its
    own packet and no handback could ever complete.

    **Blast radius when the store cannot be read.** This fails closed, so a
    persistently unreadable database refuses every managed steer, tell and
    operator message in every session on the installation — including one where
    no handoff has ever been created. That is deliberate (admitting bytes the
    hold exists to refuse is the one outcome this seam must never produce, and
    the neighbouring reservation read already refuses on the same grounds), but
    it is a global failure mode introduced by a feature nothing invokes yet,
    and an operator diagnosing a total input outage should find it named here.
    """
    if not agent_id:
        return None
    try:
        held = hold_for_agent(agent_id, db=db)
    except TaskHandoffError:
        return "the task-handoff store could not be read; task input is held until it can"
    if held is None:
        return None
    if held["role"] == ROLE_RECIPIENT and control_id and control_id == held["packet_control_id"]:
        return None
    if held["role"] == ROLE_DONOR:
        return (
            f"stable agent {agent_id} is the donor of pending handoff {held['handoff_id']}; "
            "its task authority is suspended until the handoff transfers or rolls back"
        )
    return (
        f"stable agent {agent_id} is the recipient of pending handoff {held['handoff_id']}; "
        "only that handoff's catch-up packet is admitted until the transfer completes"
    )


def hold_refusal_for_terminal(
    terminal_id: Optional[str],
    generation: Optional[str],
    *,
    control_id: Optional[str] = None,
    db: Any = None,
) -> Optional[str]:
    """``hold_refusal``, resolved from the roster rather than a reservation.

    This is the form the byte-admission path must use, and the reason is the
    recipient. A handback resumes the recipient's own native session through
    M3-B, and the exact executor deliberately allocates a terminal id it has
    *proven absent* from the managed-launch reservation table — so the restored
    pane is not "managed" at all. A hold keyed on a reservation would therefore
    be inert for every recipient, which is to say inert in exactly the flow the
    recipient-side hold exists for. The same is true of any donor that was ever
    recovered, which is the ordinary steady state after a pane loss.

    The roster is the one authority that knows both populations: a managed
    launch binds its generation under the reservation's stable agent id, and an
    exact reincarnation rebinds the pre-existing agent to the successor
    incarnation. One indexed read answers for both.

    A terminal with no roster incarnation is not held. That is not a bypass —
    an agent with no incarnation cannot be a handoff party, because a handoff
    names agents that hold or will hold a task occurrence.
    """
    if not terminal_id:
        return None
    from cli_agent_orchestrator.services import stable_agent_roster as roster

    try:
        incarnation = roster.get_incarnation_by_terminal(
            str(terminal_id), generation if generation else None, db=db
        )
    except Exception:
        # A roster that cannot name this pane's agent cannot prove it is
        # unheld. Same posture as an unreadable handoff store, and the same
        # blast radius caveat applies.
        return "the stable-agent roster could not be read; task input is held until it can"
    if not incarnation:
        return None
    return hold_refusal(incarnation.get("agent_id"), control_id=control_id, db=db)


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def get_handoff(handoff_id: str, db: Any = None) -> dict[str, Any]:
    handoff_id = _require_uuid(handoff_id, field_name="handoff_id")

    def _read(session: Any) -> dict[str, Any]:
        row = _row_by_id(session, handoff_id)
        if row is None:
            raise TaskHandoffNotFound(f"handoff {handoff_id} is not recorded")
        return _row_dict(row)

    return _with_session(_read, db, unavailable="handoff could not be read")


def list_handoffs(
    session_name: str, *, state: Optional[str] = None, db: Any = None
) -> list[dict[str, Any]]:
    name = _normalise_session_name(session_name)
    if state is not None and state not in STATES:
        raise TaskHandoffInvalid(f"state must be one of {sorted(STATES)}; got {state!r}")

    def _read(session: Any) -> list[dict[str, Any]]:
        query = session.query(database.TaskOccurrenceHandoffModel).filter(
            database.TaskOccurrenceHandoffModel.session_name == name
        )
        if state is not None:
            query = query.filter(database.TaskOccurrenceHandoffModel.state == state)
        rows = query.order_by(database.TaskOccurrenceHandoffModel.created_at).all()
        return [_row_dict(row) for row in rows]

    return _with_session(_read, db, unavailable="handoffs could not be listed")


# ---------------------------------------------------------------------------
# the capability, and what is and is not wired
# ---------------------------------------------------------------------------

_PRODUCER_REASON = (
    "M3-E is invoked explicitly by a supervisor. No detector, recovery ladder, "
    "or lifecycle transition begins a handback on its own, and no conductor "
    "client calls these routes yet."
)


def capability() -> dict[str, Any]:
    """What this build actually does, published as a block rather than inferred.

    An operator reading "the routes exist" would conclude handback is a live
    automatic behaviour. It is not: the store and the admission hold are real
    and enforced, and every handback is one explicit supervisor decision.
    """
    return {
        "capability": "m3e-task-handback",
        "schema_version": SCHEMA_VERSION,
        "store_installed": True,
        "admission_hold_enforced": True,
        "automatic_producer": False,
        "reason": _PRODUCER_REASON,
        "states": sorted(STATES),
        "delivery_states": sorted(DELIVERY_STATES),
        "child_effect_states": sorted(CHILD_EFFECT_STATES),
    }

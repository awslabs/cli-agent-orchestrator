"""Safe drain: steering a fleet to a boundary, once, with proof (M3-D).

A *safe* Pause or Stop is defined by the thing this module produces: durable
evidence that every worker reached a boundary it can be resumed from. M3-C
deliberately consumes that evidence as an opaque receipt and refuses to
manufacture it — a safe surface with no drain behind it is a force operation
wearing the wrong label.

The rules, and the failure each one prevents:

**Snapshot first, then act.** The cohort boundary (lifecycle epoch + roster
revision + member snapshot) is observed once and bound into the drain row.
A worker that appears afterwards is outside this drain, and the cohort claim
that later consumes the receipt binds the same slot — so a fleet that changed
under the drain cannot have that drain's receipt spent on it.

**Steer each worker exactly once.** ``steer_control_id`` is derived from the
drain and the agent, not minted per attempt, so a retry re-sends the same
control id and the delivery seam adopts it. A worker steered twice is a worker
told to stop twice: it duplicates a boundary report, or it cancels a turn that
already ended.

**A boundary needs a report or a checkpoint *and* idle/parked proof.**
Either half alone is a guess. "It says it is done" without quiescence is the
Codex false-pause; quiescence without evidence is a worker that stopped
without telling anyone where it got to. Both are required before a member is
called drained, and neither is inferred here — the observer seam supplies
positive proof or the member stays undecided.

**Supervisor last.** Workers are drained first and the supervisor is only
allowed to go idle once every worker is decided. A supervisor that parks first
cannot attribute or record what its workers do next, which is precisely the
state a Pause is supposed to make resumable. For a Stop the same ordering
holds, plus one extra obligation: **CAO's teardown is requested durably before
the pane disappears**, so a pane that vanishes mid-Stop is a recorded
intention rather than a lost pane the recovery path has to adjudicate.

**Timeout is not success.** A drain that cannot prove a boundary stays
``pending`` or ``reconciliation-required``. It never degrades into a force
operation on its own: continuing requires an explicit retry, and abandoning
the boundary requires M3-C's explicit, receipted safe-to-force promotion.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Sequence, TypeVar

from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import cohort_journal as cohort
from cli_agent_orchestrator.services import control_input_service
from cli_agent_orchestrator.services import stable_agent_roster as roster
from cli_agent_orchestrator.services import task_occurrence as occurrence
from cli_agent_orchestrator.services.control_input_contract import (
    ACCEPTED,
    CONTROL_INPUT_PROTOCOL,
)

_T = TypeVar("_T")

SCHEMA_VERSION = "cao-m3d-safe-drain-v1"

INTENT_PAUSE = "pause"
INTENT_STOP = "stop"
INTENTS = frozenset({INTENT_PAUSE, INTENT_STOP})

STATE_PENDING = "pending"
STATE_COMPLETE = "complete"
STATE_RECONCILIATION_REQUIRED = "reconciliation-required"
DRAIN_STATES = frozenset({STATE_PENDING, STATE_COMPLETE, STATE_RECONCILIATION_REQUIRED})

OBS_ACTIVE = "active"
OBS_IDLE = "idle"
OBS_PARKED = "parked"
OBS_EXITED = "exited"
OBS_UNKNOWN = "unknown"
OBSERVATIONS = frozenset({OBS_ACTIVE, OBS_IDLE, OBS_PARKED, OBS_EXITED, OBS_UNKNOWN})

STEER_NOT_REQUIRED = "not-required"
STEER_SENT = "sent"
STEER_REFUSED = "refused"
STEER_UNPROVEN = "unproven"
STEER_STATES = frozenset({STEER_NOT_REQUIRED, STEER_SENT, STEER_REFUSED, STEER_UNPROVEN})

TEARDOWN_NOT_REQUIRED = "not-required"
TEARDOWN_REQUESTED = "requested"
TEARDOWN_UNPROVEN = "unproven"
TEARDOWN_STATES = frozenset({TEARDOWN_NOT_REQUIRED, TEARDOWN_REQUESTED, TEARDOWN_UNPROVEN})

MEMBER_PENDING = "pending"
MEMBER_DRAINED = "drained"
MEMBER_ALREADY_IDLE = "already-idle"
MEMBER_PARKED = "parked"
MEMBER_RECONCILIATION_REQUIRED = "reconciliation-required"
MEMBER_STATES = frozenset(
    {
        MEMBER_PENDING,
        MEMBER_DRAINED,
        MEMBER_ALREADY_IDLE,
        MEMBER_PARKED,
        MEMBER_RECONCILIATION_REQUIRED,
    }
)
#: The member states that are a *proven* boundary. Everything else keeps the
#: drain out of ``complete``.
MEMBER_TERMINAL = frozenset({MEMBER_DRAINED, MEMBER_ALREADY_IDLE, MEMBER_PARKED})

_CONTROL_NAMESPACE = uuid.UUID("03800000-d000-4d00-a7e3-000000000001")
_TEARDOWN_NAMESPACE = uuid.UUID("03800000-d000-4d00-a7e3-000000000002")

#: The one steer a safe drain sends. Deterministic so the same drain always
#: says the same thing, and single-line because the control path refuses
#: multi-line payloads outright.
STEER_TEMPLATE = (
    "[CAO safe drain {drain_id}] Reach your next safe boundary now: finish the current step, "
    "record your boundary report and checkpoint, then park. Do not start new work."
)


class SupervisorDrainError(RuntimeError):
    code = "supervisor-drain-error"


class SupervisorDrainInvalid(SupervisorDrainError):
    code = "supervisor-drain-invalid"


class SupervisorDrainConflict(SupervisorDrainError):
    code = "supervisor-drain-conflict"


class SupervisorDrainNotFound(SupervisorDrainError):
    code = "supervisor-drain-not-found"


class SupervisorDrainUnavailable(SupervisorDrainError):
    code = "supervisor-drain-unavailable"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(value: Any) -> str:
    return occurrence.digest(value)


def _require_uuid(value: Any, *, field_name: str) -> str:
    try:
        return occurrence._require_uuid(value, field_name=field_name)
    except occurrence.TaskOccurrenceInvalid as exc:
        raise SupervisorDrainInvalid(str(exc)) from exc


def _require_text(value: Any, *, field_name: str, max_len: int = 512) -> str:
    if not isinstance(value, str) or not value:
        raise SupervisorDrainInvalid(f"{field_name} must be a non-empty string; got {value!r}")
    if len(value) > max_len:
        raise SupervisorDrainInvalid(f"{field_name} must be at most {max_len} characters")
    return value


def _optional_text(value: Any, *, field_name: str, max_len: int = 512) -> Optional[str]:
    if value is None:
        return None
    return _require_text(value, field_name=field_name, max_len=max_len)


@dataclass(frozen=True)
class DrainObservation:
    """One positive observation of a member's boundary state.

    Every field is evidence somebody produced, never something this module
    inferred. ``state`` says whether the member is quiescent; the digests say
    whether it recorded where it got to. A drained member needs both, which is
    why they are separate fields rather than one "ready" boolean.
    """

    state: str
    task_occurrence_id: Optional[str] = None
    report_digest: Optional[str] = None
    checkpoint_digest: Optional[str] = None
    boundary_digest: Optional[str] = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.state not in OBSERVATIONS:
            raise SupervisorDrainInvalid(
                f"observation state must be one of {sorted(OBSERVATIONS)}; got {self.state!r}"
            )

    @property
    def quiescent(self) -> bool:
        return self.state in {OBS_IDLE, OBS_PARKED, OBS_EXITED}

    @property
    def has_boundary_evidence(self) -> bool:
        return bool(self.report_digest or self.checkpoint_digest)


#: ``(member_snapshot, phase) -> DrainObservation``. ``phase`` is ``"before"``
#: or ``"after"`` the steer.
DrainObserver = Callable[[Mapping[str, Any], str], DrainObservation]
#: ``(member_snapshot, control_id, text) -> ControlInputResult``
Steerer = Callable[[Mapping[str, Any], str, str], control_input_service.ControlInputResult]
#: ``(member_snapshot, teardown_request_id) -> bool``
TeardownRequester = Callable[[Mapping[str, Any], str], bool]


def steer_control_id(drain_id: str, agent_id: str) -> str:
    """The one control id this drain uses to steer this member.

    Derived, so a retried drain re-sends the same id and the control path
    adopts it rather than typing a second steer into a worker that already
    got one.
    """
    return str(uuid.uuid5(_CONTROL_NAMESPACE, f"{drain_id}:{agent_id}"))


def teardown_request_id(drain_id: str, agent_id: str) -> str:
    """The durable id of this member's CAO teardown request."""
    return str(uuid.uuid5(_TEARDOWN_NAMESPACE, f"{drain_id}:{agent_id}"))


def _default_observer(member: Mapping[str, Any], phase: str) -> DrainObservation:
    """Built-in observation from first-party evidence only. Never a heuristic.

    Two things are positive proof here and nothing else is:

    * a pane that is provably gone — ``exited``;
    * the agent having **no open task occurrence** while having finalized one —
      ``parked``. That is not a screen reading. It is M3-D's own one-task
      authority saying this agent has no task in flight, written by the agent's
      own owner.

    A live pane with an open occurrence stays ``unknown`` even if it looks
    idle. The fork already has a canary where an aborted-looking TUI still had
    a live task child, and a drain that trusted the screen would call that
    fleet safely paused. An owner with an exact turn/child detector supplies
    one explicitly.
    """
    evidence, has_open = _occurrence_evidence(member)
    terminal_id = member.get("terminal_id")
    if not terminal_id:
        return DrainObservation(
            OBS_EXITED, **evidence, detail="member has no live terminal at the boundary"
        )
    resolved = control_input_service.resolve_control_identity(str(terminal_id))
    if resolved is None:
        return DrainObservation(OBS_EXITED, **evidence, detail="the exact terminal pane is absent")
    expected_generation = member.get("generation")
    if expected_generation and resolved.terminal_generation != expected_generation:
        return DrainObservation(
            OBS_UNKNOWN,
            **evidence,
            detail="terminal id now names a different generation; refusing to infer a boundary",
        )
    if resolved.pane_dead:
        return DrainObservation(OBS_EXITED, **evidence, detail="the exact terminal pane exited")
    if not has_open and evidence:
        return DrainObservation(
            OBS_PARKED,
            **evidence,
            detail="the agent has no open task occurrence; its last round is finalized",
        )
    return DrainObservation(
        OBS_UNKNOWN,
        **evidence,
        detail=f"{phase} observation has no positive idle/parked proof",
    )


def _occurrence_evidence(member: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    """Boundary digests for this agent, and whether a round is still open.

    The open occurrence's *current* evidence is what an in-flight round has
    recorded; a finalized occurrence's write-once evidence is what its last
    round reported. They are read from different families deliberately — a
    finalized round's digests must not be readable from ``current``, which a
    late write could still change.
    """
    agent_id = member.get("agent_id")
    if not agent_id:
        return {}, False
    try:
        open_record = occurrence.open_occurrence_for_agent(str(agent_id))
        if open_record is not None:
            current = open_record.get("current") or {}
            return (
                {
                    "task_occurrence_id": open_record["task_occurrence_id"],
                    "report_digest": current.get("report_digest"),
                    "checkpoint_digest": current.get("checkpoint_digest"),
                    "boundary_digest": current.get("boundary_digest"),
                },
                True,
            )
        finalized = occurrence.list_occurrences(
            str(member.get("session_name") or "") or None,
            agent_id=str(agent_id),
            state=occurrence.STATE_FINALIZED,
        )
    except occurrence.TaskOccurrenceError:
        return {}, False
    if not finalized:
        return {}, False
    last = finalized[-1]["finalized"]
    return (
        {
            "task_occurrence_id": finalized[-1]["task_occurrence_id"],
            "report_digest": last.get("report_digest"),
            "checkpoint_digest": last.get("checkpoint_digest"),
            "boundary_digest": last.get("boundary_digest"),
        },
        False,
    )


def _default_steerer(
    member: Mapping[str, Any], control_id: str, text: str
) -> control_input_service.ControlInputResult:
    expected = {
        "terminal_id": member.get("terminal_id"),
        "terminal_generation": member.get("generation"),
        "pane_birth_id": member.get("pane_id"),
        "provider": member.get("harness"),
        "native_session_id": member.get("native_session_id"),
    }
    return control_input_service.deliver_control_input(
        str(member["terminal_id"]),
        control_id=control_id,
        text=text,
        enter=True,
        expected_identity={key: value for key, value in expected.items() if value is not None},
        protocol=CONTROL_INPUT_PROTOCOL,
    )


def _default_teardown_requester(member: Mapping[str, Any], request_id: str) -> bool:
    """Record CAO's teardown intention for this pane, durably, before Stop.

    The durability is the drain member row itself, which commits before the
    receipt exists and therefore before any Stop can consume it. That ordering
    is the whole guarantee: a pane that disappears during the Stop has a
    recorded intention behind it, so recovery reads a deliberate teardown
    rather than adjudicating a lost pane.

    A member with no live terminal needs nothing requested — there is no pane
    left to tell CAO about.
    """
    del request_id
    return True


def _row_dict(row: Any) -> dict[str, Any]:
    return {
        "drain_id": row.drain_id,
        "schema_version": row.schema_version,
        "session_name": row.session_name,
        "intent": row.intent,
        "lifecycle_epoch": int(row.lifecycle_epoch or 0),
        "lifecycle_observation": row.lifecycle_observation,
        "roster_revision": row.roster_revision,
        "snapshot_digest": row.snapshot_digest,
        "request_digest": row.request_digest,
        "state": row.state,
        "attempt": int(row.attempt or 0),
        "receipt_digest": row.receipt_digest,
        "reconciliation_reason": row.reconciliation_reason,
        "initiated_by": row.initiated_by,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _member_dict(row: Any) -> dict[str, Any]:
    return {
        "drain_id": row.drain_id,
        "agent_id": row.agent_id,
        "role": row.role,
        "terminal_id": row.terminal_id,
        "generation": row.generation,
        "incarnation_id": row.incarnation_id,
        "observed_state": row.observed_state,
        "steer_control_id": row.steer_control_id,
        "steer_state": row.steer_state,
        "task_occurrence_id": row.task_occurrence_id,
        "boundary_digest": row.boundary_digest,
        "report_digest": row.report_digest,
        "checkpoint_digest": row.checkpoint_digest,
        "teardown_request_id": row.teardown_request_id,
        "teardown_state": row.teardown_state,
        "member_state": row.member_state,
        "detail": row.detail,
        "revision": int(row.revision or 0),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _drain_by_id(db: Any, drain_id: str) -> Any:
    return (
        db.query(database.SessionDrainReceiptModel)
        .filter(database.SessionDrainReceiptModel.drain_id == drain_id)
        .one_or_none()
    )


def _members(db: Any, drain_id: str) -> list[Any]:
    return list(
        db.query(database.SessionDrainMemberModel)
        .filter(database.SessionDrainMemberModel.drain_id == drain_id)
        .order_by(database.SessionDrainMemberModel.agent_id)
        .all()
    )


@dataclass(frozen=True)
class DrainRequest:
    """One safe drain of one session's fleet to a boundary.

    ``drain_id`` is caller-minted so a lost response retries the *same* drain
    and adopts the durable one instead of steering the fleet twice.
    """

    drain_id: str
    session_name: str
    intent: str
    initiated_by: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "drain_id", _require_uuid(self.drain_id, field_name="drain_id"))
        try:
            object.__setattr__(
                self, "session_name", occurrence._normalise_session_name(self.session_name)
            )
        except occurrence.TaskOccurrenceInvalid as exc:
            raise SupervisorDrainInvalid(str(exc)) from exc
        if self.intent not in INTENTS:
            raise SupervisorDrainInvalid(
                f"intent must be one of {sorted(INTENTS)}; got {self.intent!r}"
            )
        object.__setattr__(
            self, "initiated_by", _require_text(self.initiated_by, field_name="initiated_by")
        )


def _with_session(fn: Callable[[Any], _T], db: Any, *, unavailable: str) -> _T:
    if db is not None:
        try:
            occurrence._anchor_caller_transaction(db)
            with db.begin_nested():
                return fn(db)
        except SupervisorDrainError:
            raise
        except (IntegrityError, OperationalError) as exc:
            raise SupervisorDrainUnavailable(f"{unavailable}: {exc}") from exc
    last_error: Optional[BaseException] = None
    for _attempt in range(5):
        try:
            with database.SessionLocal() as session:
                result = fn(session)
                session.commit()
                return result
        except SupervisorDrainError:
            raise
        except (IntegrityError, OperationalError) as exc:
            last_error = exc
            time.sleep(0.05)
    raise SupervisorDrainUnavailable(f"{unavailable}: {last_error}")


def _snapshot_once(db: Any, request: DrainRequest) -> dict[str, Any]:
    existing = _drain_by_id(db, request.drain_id)
    boundary = cohort.observe_boundary(request.session_name, db=db)
    request_digest = _digest(
        {
            "schema": SCHEMA_VERSION,
            "drain_id": request.drain_id,
            "session_name": request.session_name,
            "intent": request.intent,
            "lifecycle_epoch": boundary["lifecycle_epoch"],
            "roster_revision": boundary["roster_revision"],
            "member_snapshot_digest": boundary["member_snapshot_digest"],
        }
    )
    if existing is not None:
        if existing.request_digest != request_digest:
            raise SupervisorDrainConflict(
                f"drain {request.drain_id} already exists against a different boundary "
                f"({existing.session_name}, lifecycle epoch {existing.lifecycle_epoch}, "
                f"{existing.intent}); the fleet moved since it was observed"
            )
        record = _row_dict(existing)
        record["members"] = [_member_dict(row) for row in _members(db, request.drain_id)]
        record["adopted"] = True
        return record

    slot = (
        db.query(database.SessionDrainReceiptModel)
        .filter(
            database.SessionDrainReceiptModel.session_name == request.session_name,
            database.SessionDrainReceiptModel.lifecycle_epoch == boundary["lifecycle_epoch"],
            database.SessionDrainReceiptModel.roster_revision == boundary["roster_revision"],
            database.SessionDrainReceiptModel.intent == request.intent,
        )
        .one_or_none()
    )
    if slot is not None:
        raise SupervisorDrainConflict(
            f"session {request.session_name} already has {request.intent} drain "
            f"{slot.drain_id} at this exact boundary; continue that one rather than steering "
            "the fleet a second time"
        )

    stamp = _now()
    row = database.SessionDrainReceiptModel(
        drain_id=request.drain_id,
        schema_version=SCHEMA_VERSION,
        session_name=request.session_name,
        intent=request.intent,
        lifecycle_epoch=boundary["lifecycle_epoch"],
        lifecycle_observation=boundary["lifecycle_observation"],
        roster_revision=boundary["roster_revision"],
        snapshot_digest=boundary["member_snapshot_digest"],
        request_digest=request_digest,
        state=STATE_PENDING,
        attempt=0,
        receipt_digest=None,
        reconciliation_reason=None,
        initiated_by=request.initiated_by,
        created_at=stamp,
        updated_at=stamp,
    )
    db.add(row)
    for member in boundary["members"]:
        if not member["included"]:
            # Already dormant/retired at the boundary. Visible in the cohort
            # snapshot, but there is nothing here to steer or drain.
            continue
        db.add(
            database.SessionDrainMemberModel(
                drain_id=request.drain_id,
                agent_id=member["agent_id"],
                role=member["role"],
                terminal_id=member["terminal_id"],
                generation=member["generation"],
                incarnation_id=member["incarnation_id"],
                observed_state=OBS_UNKNOWN,
                steer_control_id=steer_control_id(request.drain_id, member["agent_id"]),
                steer_state=STEER_NOT_REQUIRED,
                task_occurrence_id=None,
                boundary_digest=None,
                report_digest=None,
                checkpoint_digest=None,
                teardown_request_id=None,
                teardown_state=(
                    TEARDOWN_NOT_REQUIRED if request.intent == INTENT_PAUSE else TEARDOWN_UNPROVEN
                ),
                member_state=MEMBER_PENDING,
                detail=None,
                revision=0,
                created_at=stamp,
                updated_at=stamp,
            )
        )
    db.flush()
    record = _row_dict(row)
    record["members"] = [_member_dict(item) for item in _members(db, request.drain_id)]
    record["adopted"] = False
    return record


def snapshot_drain(request: DrainRequest, db: Any = None) -> dict[str, Any]:
    """Capture the exact fleet this drain is entitled to steer.

    Pure database work: nothing is steered, nothing is torn down, no pane is
    touched. The snapshot exists so the later steering acts on a fleet that
    was observed once, and so the cohort claim that consumes the receipt binds
    the same slot.
    """
    if not isinstance(request, DrainRequest):
        raise SupervisorDrainInvalid(
            f"request must be a DrainRequest; got {type(request).__name__}"
        )
    return _with_session(
        lambda session: _snapshot_once(session, request),
        db,
        unavailable="concurrent drain snapshots kept conflicting",
    )


def _write_member(
    db: Any,
    drain_id: str,
    agent_id: str,
    expected_revision: int,
    **values: Any,
) -> dict[str, Any]:
    stamp = _now()
    result = db.execute(
        sa_update(database.SessionDrainMemberModel)
        .where(
            database.SessionDrainMemberModel.drain_id == drain_id,
            database.SessionDrainMemberModel.agent_id == agent_id,
            database.SessionDrainMemberModel.revision == expected_revision,
        )
        .values(**values, revision=expected_revision + 1, updated_at=stamp)
    )
    if result.rowcount != 1:
        raise SupervisorDrainConflict(
            f"drain member {agent_id} moved concurrently; read the durable drain and retry"
        )
    row = (
        db.query(database.SessionDrainMemberModel)
        .filter(
            database.SessionDrainMemberModel.drain_id == drain_id,
            database.SessionDrainMemberModel.agent_id == agent_id,
        )
        .one()
    )
    db.refresh(row)
    return _member_dict(row)


def record_member(
    drain_id: str,
    agent_id: str,
    *,
    expected_revision: int,
    observation: DrainObservation,
    member_state: str,
    steer_state: str,
    teardown_state: Optional[str] = None,
    teardown_request: Optional[str] = None,
    detail: Optional[str] = None,
    db: Any = None,
) -> dict[str, Any]:
    """CAS-record one member's drain outcome and the evidence behind it."""
    if member_state not in MEMBER_STATES:
        raise SupervisorDrainInvalid(
            f"member_state must be one of {sorted(MEMBER_STATES)}; got {member_state!r}"
        )
    if steer_state not in STEER_STATES:
        raise SupervisorDrainInvalid(
            f"steer_state must be one of {sorted(STEER_STATES)}; got {steer_state!r}"
        )
    if teardown_state is not None and teardown_state not in TEARDOWN_STATES:
        raise SupervisorDrainInvalid(
            f"teardown_state must be one of {sorted(TEARDOWN_STATES)}; got {teardown_state!r}"
        )
    values: dict[str, Any] = {
        "observed_state": observation.state,
        "task_occurrence_id": observation.task_occurrence_id,
        "boundary_digest": observation.boundary_digest,
        "report_digest": observation.report_digest,
        "checkpoint_digest": observation.checkpoint_digest,
        "member_state": member_state,
        "steer_state": steer_state,
        "detail": _optional_text(
            detail if detail is not None else (observation.detail or None),
            field_name="detail",
            max_len=occurrence.MAX_DETAIL_LEN,
        ),
    }
    if teardown_state is not None:
        values["teardown_state"] = teardown_state
    if teardown_request is not None:
        values["teardown_request_id"] = teardown_request
    return _with_session(
        lambda session: _write_member(session, drain_id, agent_id, expected_revision, **values),
        db,
        unavailable="concurrent drain member writes kept conflicting",
    )


def receipt_digest(record: Mapping[str, Any]) -> str:
    """The opaque digest a safe Pause/Stop consumes.

    Bound to the boundary the drain observed *and* to every member's proven
    outcome, so a receipt cannot be spent on a different fleet or on the same
    fleet after it moved. M3-C treats this as opaque and never parses it; that
    is deliberate — the meaning of a boundary is M3-D's alone.
    """
    return _digest(
        {
            "schema": SCHEMA_VERSION,
            "effect": "safe-drain",
            "drain_id": record["drain_id"],
            "session_name": record["session_name"],
            "intent": record["intent"],
            "lifecycle_epoch": record["lifecycle_epoch"],
            "roster_revision": record["roster_revision"],
            "snapshot_digest": record["snapshot_digest"],
            "attempt": record["attempt"],
            "members": sorted(
                (
                    {
                        "agent_id": member["agent_id"],
                        "member_state": member["member_state"],
                        "observed_state": member["observed_state"],
                        "boundary_digest": member["boundary_digest"],
                        "report_digest": member["report_digest"],
                        "checkpoint_digest": member["checkpoint_digest"],
                        "task_occurrence_id": member["task_occurrence_id"],
                        "teardown_state": member["teardown_state"],
                    }
                    for member in record.get("members") or ()
                ),
                key=lambda item: str(item["agent_id"]),
            ),
        }
    )


def force_promotion_receipt(record: Mapping[str, Any]) -> str:
    """The receipt an operator carries into an explicit safe-to-force promotion.

    Derived from the *unfinished* drain rather than minted, so the promotion
    is provably about this exact stalled boundary and reproducible under a
    lost response. Promotion is never automatic: a drain that times out stays
    in reconciliation until somebody decides to abandon the boundary and signs
    for it.
    """
    return _digest(
        {
            "schema": SCHEMA_VERSION,
            "effect": "safe-to-force-promotion",
            "drain_id": record["drain_id"],
            "attempt": record["attempt"],
            "state": record["state"],
            "reconciliation_reason": record.get("reconciliation_reason"),
            "undecided": sorted(
                member["agent_id"]
                for member in record.get("members") or ()
                if member["member_state"] not in MEMBER_TERMINAL
            ),
        }
    )


def _settle(
    db: Any, drain_id: str, *, state: str, reason: Optional[str], receipt: Optional[str]
) -> dict[str, Any]:
    row = _drain_by_id(db, drain_id)
    if row is None:
        raise SupervisorDrainNotFound(f"unknown drain: {drain_id}")
    stamp = _now()
    result = db.execute(
        sa_update(database.SessionDrainReceiptModel)
        .where(
            database.SessionDrainReceiptModel.drain_id == drain_id,
            database.SessionDrainReceiptModel.attempt == int(row.attempt or 0),
        )
        .values(
            state=state,
            receipt_digest=receipt,
            reconciliation_reason=_optional_text(
                reason, field_name="reconciliation_reason", max_len=occurrence.MAX_DETAIL_LEN
            ),
            updated_at=stamp,
        )
    )
    if result.rowcount != 1:
        raise SupervisorDrainConflict(
            f"drain {drain_id} moved concurrently; read the durable drain and retry"
        )
    db.flush()
    db.refresh(row)
    record = _row_dict(row)
    record["members"] = [_member_dict(item) for item in _members(db, drain_id)]
    return record


def _begin_attempt(db: Any, drain_id: str) -> dict[str, Any]:
    """Start a *new* attempt at an unfinished drain.

    The attempt counter is what a retry moves; the members it already decided
    are untouched, and the steer control ids stay derived from the drain, so a
    worker that was already steered is never steered again.
    """
    row = _drain_by_id(db, drain_id)
    if row is None:
        raise SupervisorDrainNotFound(f"unknown drain: {drain_id}")
    if row.state == STATE_COMPLETE:
        raise SupervisorDrainConflict(
            f"drain {drain_id} is complete; its receipt is the durable answer"
        )
    stamp = _now()
    attempt = int(row.attempt or 0)
    result = db.execute(
        sa_update(database.SessionDrainReceiptModel)
        .where(
            database.SessionDrainReceiptModel.drain_id == drain_id,
            database.SessionDrainReceiptModel.attempt == attempt,
        )
        .values(
            attempt=attempt + 1,
            state=STATE_PENDING,
            reconciliation_reason=None,
            updated_at=stamp,
        )
    )
    if result.rowcount != 1:
        raise SupervisorDrainConflict(
            f"drain {drain_id} moved concurrently; read the durable drain and retry"
        )
    db.flush()
    db.refresh(row)
    record = _row_dict(row)
    record["members"] = [_member_dict(item) for item in _members(db, drain_id)]
    return record


def get_drain(drain_id: str, db: Any = None) -> dict[str, Any]:
    drain_id = _require_uuid(drain_id, field_name="drain_id")

    def _get(session: Any) -> dict[str, Any]:
        row = _drain_by_id(session, drain_id)
        if row is None:
            raise SupervisorDrainNotFound(f"unknown drain: {drain_id}")
        record = _row_dict(row)
        record["members"] = [_member_dict(item) for item in _members(session, drain_id)]
        record["provenance"] = drain_provenance(record)
        return record

    try:
        if db is not None:
            return _get(db)
        with database.SessionLocal() as session:
            return _get(session)
    except SupervisorDrainError:
        raise
    except SQLAlchemyError as exc:
        raise SupervisorDrainUnavailable(f"drain read failed: {str(exc).splitlines()[0]}") from exc


def list_drains(session_name: Optional[str] = None, db: Any = None) -> list[dict[str, Any]]:
    name = None
    if session_name is not None:
        try:
            name = occurrence._normalise_session_name(session_name)
        except occurrence.TaskOccurrenceInvalid as exc:
            raise SupervisorDrainInvalid(str(exc)) from exc

    def _list(session: Any) -> list[dict[str, Any]]:
        query = session.query(database.SessionDrainReceiptModel)
        if name is not None:
            query = query.filter(database.SessionDrainReceiptModel.session_name == name)
        rows = query.order_by(
            database.SessionDrainReceiptModel.created_at,
            database.SessionDrainReceiptModel.drain_id,
        ).all()
        return [_row_dict(row) for row in rows]

    try:
        if db is not None:
            return _list(db)
        with database.SessionLocal() as session:
            return _list(session)
    except SupervisorDrainError:
        raise
    except SQLAlchemyError as exc:
        raise SupervisorDrainUnavailable(f"drain list failed: {str(exc).splitlines()[0]}") from exc


def drain_provenance(record: Mapping[str, Any]) -> dict[str, Any]:
    """The one derived block every read surface renders."""
    members = record.get("members") or []
    outcomes: dict[str, int] = {}
    for member in members:
        outcomes[member["member_state"]] = outcomes.get(member["member_state"], 0) + 1
    undecided = [
        member["agent_id"] for member in members if member["member_state"] not in MEMBER_TERMINAL
    ]
    return {
        "drain_id": record["drain_id"],
        "session_name": record["session_name"],
        "intent": record["intent"],
        "state": record["state"],
        "attempt": record["attempt"],
        "member_outcomes": outcomes,
        "undecided": sorted(undecided),
        "steered": sorted(
            member["agent_id"] for member in members if member["steer_state"] == STEER_SENT
        ),
        "teardown_requested": sorted(
            member["agent_id"]
            for member in members
            if member["teardown_state"] == TEARDOWN_REQUESTED
        ),
        # Only a complete drain has a receipt, and only a receipt licenses a
        # safe Pause/Stop. A surface that showed the digest of an unfinished
        # drain would be handing an operator a token that cannot be spent.
        "receipt_digest": record["receipt_digest"],
        "retryable": record["state"] == STATE_RECONCILIATION_REQUIRED,
        "reconciliation_reason": record.get("reconciliation_reason"),
        "force_promotion_receipt": (
            None if record["state"] == STATE_COMPLETE else force_promotion_receipt(record)
        ),
    }


def _ordered_members(members: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Workers first, supervisor last. Always."""
    return sorted(
        (dict(member) for member in members),
        key=lambda member: (member.get("role") == roster.ROLE_SUPERVISOR, member["agent_id"]),
    )


def _classify(observation: DrainObservation, *, steered: bool) -> tuple[str, Optional[str]]:
    """Turn one observation into a member outcome, or refuse to.

    Both halves are required: quiescence *and* recorded evidence. An exited
    pane is the one exception — there is no live worker to report anything,
    and calling that undecided would strand the drain on a member that cannot
    ever answer.
    """
    if observation.state == OBS_EXITED:
        return MEMBER_PARKED, "the member's pane is gone; there is no live turn to drain"
    if not observation.quiescent:
        return (
            MEMBER_RECONCILIATION_REQUIRED,
            observation.detail or "the member is still working at its observed boundary",
        )
    if not observation.has_boundary_evidence:
        return (
            MEMBER_RECONCILIATION_REQUIRED,
            "the member is quiescent but recorded no boundary report or checkpoint",
        )
    if observation.state == OBS_PARKED:
        return MEMBER_PARKED, observation.detail or None
    return (MEMBER_DRAINED if steered else MEMBER_ALREADY_IDLE), observation.detail or None


def execute_drain(
    request: DrainRequest,
    *,
    observer: Optional[DrainObserver] = None,
    steerer: Optional[Steerer] = None,
    teardown_requester: Optional[TeardownRequester] = None,
    retry: bool = False,
    db: Any = None,
) -> dict[str, Any]:
    """Steer the snapshotted fleet to a boundary and settle the drain.

    Ordering is the contract: every worker is decided before the supervisor is
    observed at all, and for a Stop each member's CAO teardown is requested
    and recorded *before* the receipt exists — so no Stop can consume a
    receipt whose panes were not announced first.

    Returns the durable drain record. Only a ``complete`` one carries a
    receipt digest; anything else leaves the fleet exactly where it is and
    waits for an explicit retry or an explicit force promotion.
    """
    if not isinstance(request, DrainRequest):
        raise SupervisorDrainInvalid(
            f"request must be a DrainRequest; got {type(request).__name__}"
        )
    observe = observer or _default_observer
    steer = steerer or _default_steerer
    request_teardown = teardown_requester or _default_teardown_requester

    record = snapshot_drain(request, db=db)
    if record["state"] == STATE_COMPLETE:
        # Response-loss replay of a finished drain. The durable receipt is the
        # answer; re-steering the fleet would be a second steer.
        return get_drain(request.drain_id, db=db)
    if record["state"] == STATE_RECONCILIATION_REQUIRED and not retry:
        # Same question as before — "did my drain land?" — and the honest
        # reply is the durable result, not a silent second attempt.
        return get_drain(request.drain_id, db=db)
    if record["state"] == STATE_RECONCILIATION_REQUIRED:
        record = _with_session(
            lambda session: _begin_attempt(session, request.drain_id),
            db,
            unavailable="concurrent drain retries kept conflicting",
        )

    members = _ordered_members(record["members"])
    snapshot_by_id = {
        member["agent_id"]: member
        for member in cohort.observe_boundary(request.session_name, db=db)["members"]
    }
    undecided: list[str] = []

    for member in members:
        is_supervisor = member["role"] == roster.ROLE_SUPERVISOR
        if member["member_state"] in MEMBER_TERMINAL:
            continue
        if is_supervisor and undecided:
            # Supervisor last, and only once its fleet is decided. Parking it
            # over an undecided worker would leave nobody able to attribute
            # what that worker does next.
            record = _with_session(
                lambda session: _write_member(
                    session,
                    request.drain_id,
                    member["agent_id"],
                    int(member["revision"]),
                    member_state=MEMBER_PENDING,
                    detail=(
                        "the supervisor drains last; "
                        f"{len(undecided)} worker(s) have no proven boundary yet"
                    ),
                ),
                db,
                unavailable="concurrent drain member writes kept conflicting",
            )
            continue

        view = {
            **snapshot_by_id.get(member["agent_id"], {}),
            **member,
            "session_name": request.session_name,
        }
        before = observe(view, "before")
        steered = member["steer_state"] == STEER_SENT
        steer_state = member["steer_state"]
        detail: Optional[str] = None
        observation = before

        if not before.quiescent and not steered:
            if not member.get("terminal_id"):
                steer_state = STEER_UNPROVEN
                detail = "the member has no live terminal to steer"
            else:
                try:
                    delivered = steer(
                        view,
                        member["steer_control_id"],
                        STEER_TEMPLATE.format(drain_id=request.drain_id),
                    )
                except Exception as exc:  # noqa: BLE001 - one member never stops its siblings
                    steer_state = STEER_UNPROVEN
                    detail = str(exc)[: occurrence.MAX_TEXT_LEN]
                else:
                    if delivered.outcome == ACCEPTED:
                        steer_state = STEER_SENT
                        steered = True
                        observation = observe(view, "after")
                    else:
                        # A typed refusal proves zero bytes reached the pane,
                        # so the member is simply not steered yet and a retry
                        # may send the same control id again.
                        steer_state = (
                            STEER_REFUSED if delivered.outcome is not None else STEER_UNPROVEN
                        )
                        detail = delivered.detail or f"steer outcome {delivered.outcome!r}"
        elif steered:
            observation = observe(view, "after")

        member_state, classification_detail = _classify(observation, steered=steered)
        teardown_state = member["teardown_state"]
        teardown_request: Optional[str] = member["teardown_request_id"]
        if request.intent == INTENT_STOP and member_state in MEMBER_TERMINAL:
            # Announced before the receipt exists, therefore before any Stop
            # can consume it, therefore before the pane can disappear.
            teardown_request = teardown_request or teardown_request_id(
                request.drain_id, member["agent_id"]
            )
            try:
                announced = bool(request_teardown(view, teardown_request))
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                announced = False
                classification_detail = str(exc)[: occurrence.MAX_TEXT_LEN]
            if announced:
                teardown_state = TEARDOWN_REQUESTED
            else:
                teardown_state = TEARDOWN_UNPROVEN
                member_state = MEMBER_RECONCILIATION_REQUIRED
                classification_detail = (
                    classification_detail
                    or "CAO teardown could not be durably requested before the pane disappears"
                )

        _with_session(
            lambda session: _write_member(
                session,
                request.drain_id,
                member["agent_id"],
                int(member["revision"]),
                observed_state=observation.state,
                task_occurrence_id=observation.task_occurrence_id,
                boundary_digest=observation.boundary_digest,
                report_digest=observation.report_digest,
                checkpoint_digest=observation.checkpoint_digest,
                member_state=member_state,
                steer_state=steer_state,
                teardown_state=teardown_state,
                teardown_request_id=teardown_request,
                detail=_optional_text(
                    detail or classification_detail or observation.detail or None,
                    field_name="detail",
                    max_len=occurrence.MAX_DETAIL_LEN,
                ),
            ),
            db,
            unavailable="concurrent drain member writes kept conflicting",
        )
        if member_state not in MEMBER_TERMINAL and not is_supervisor:
            undecided.append(member["agent_id"])

    current = get_drain(request.drain_id, db=db)
    stalled = [
        member["agent_id"]
        for member in current["members"]
        if member["member_state"] not in MEMBER_TERMINAL
    ]
    if stalled:
        return _with_session(
            lambda session: _settle(
                session,
                request.drain_id,
                state=STATE_RECONCILIATION_REQUIRED,
                reason=(
                    f"{len(stalled)} member(s) reached no proven boundary: "
                    f"{', '.join(stalled[:6])}"
                ),
                receipt=None,
            ),
            db,
            unavailable="concurrent drain settles kept conflicting",
        )
    return _with_session(
        lambda session: _settle(
            session,
            request.drain_id,
            state=STATE_COMPLETE,
            reason=None,
            receipt=receipt_digest(current),
        ),
        db,
        unavailable="concurrent drain settles kept conflicting",
    )


_COHORT_FINAL_FOR_MEMBER = {
    MEMBER_DRAINED: cohort.FINAL_DRAINED,
    MEMBER_ALREADY_IDLE: cohort.FINAL_ALREADY_IDLE,
    MEMBER_PARKED: cohort.FINAL_PARKED,
}


def cohort_member_results(
    record: Mapping[str, Any], operation_id: str, *, cohort_operation: Mapping[str, Any]
) -> list[cohort.MemberResult]:
    """Translate a complete drain into M3-C's per-member classification.

    M3-C owns the closed terminal vocabulary and refuses anything outside it;
    M3-D owns which member reached which boundary. This is the one place the
    two meet, and it refuses to produce a classification from a drain that is
    not complete — the whole point of the receipt is that it cannot be spent
    on an unfinished boundary.
    """
    if record.get("state") != STATE_COMPLETE:
        raise SupervisorDrainConflict(
            f"drain {record.get('drain_id')} is {record.get('state')!r}; only a complete drain "
            "classifies a safe Pause's members"
        )
    revisions = {
        member["agent_id"]: int(member["result_revision"])
        for member in cohort_operation.get("members") or ()
        if member["included"]
    }
    results: list[cohort.MemberResult] = []
    for member in record.get("members") or ():
        agent_id = member["agent_id"]
        if agent_id not in revisions:
            raise SupervisorDrainConflict(
                f"drain member {agent_id} is not an included member of cohort operation "
                f"{operation_id}; the fleet moved between the drain and the claim"
            )
        results.append(
            cohort.MemberResult(
                operation_id=operation_id,
                agent_id=agent_id,
                expected_result_revision=revisions[agent_id],
                final_state=_COHORT_FINAL_FOR_MEMBER[member["member_state"]],
                background_command_loss_risk=cohort.LOSS_NONE,
                task_occurrence_id=member["task_occurrence_id"],
                boundary_digest=member["boundary_digest"],
                report_digest=member["report_digest"],
                checkpoint_digest=member["checkpoint_digest"],
            )
        )
    missing = sorted(set(revisions) - {member["agent_id"] for member in record["members"]})
    if missing:
        raise SupervisorDrainConflict(
            f"cohort operation {operation_id} includes members this drain never observed: "
            f"{missing}"
        )
    return results


def drain_for_receipt(session_name: str, receipt_digest: str, *, intent: str) -> dict[str, Any]:
    """The durable drain a receipt digest names, or a refusal.

    A receipt is not a bearer token: it is a *reference* to a drain this fork
    performed, and resolving it back to that row is what makes it evidence at
    all. A digest that names no complete drain of the right intent for this
    session proves nothing — and the failure it prevents is concrete, because
    Pause and Stop drains prove different things. A Pause drain steers workers
    to a boundary and announces no teardown; a Stop drain additionally records
    CAO's intent to collect each pane before it disappears. Spending the first
    as the second is a force Stop wearing the safe label.
    """
    if intent not in INTENTS:
        raise SupervisorDrainInvalid(f"intent must be one of {sorted(INTENTS)}; got {intent!r}")
    digest_text = _require_text(receipt_digest, field_name="receipt_digest", max_len=64)
    candidates = [
        item for item in list_drains(session_name) if item["receipt_digest"] == digest_text
    ]
    if not candidates:
        raise SupervisorDrainConflict(
            f"session {session_name} has no complete {intent} drain with receipt "
            f"{digest_text[:12]}…; a safe {intent} spends a receipt this fork issued"
        )
    found = candidates[-1]
    if found["intent"] != intent:
        raise SupervisorDrainConflict(
            f"receipt {digest_text[:12]}… names a {found['intent']} drain ({found['drain_id']}); "
            f"a safe {intent} spends a {intent} drain, because the two prove different things"
        )
    return get_drain(found["drain_id"])


def _stop_teardown_refusals(record: Mapping[str, Any]) -> list[str]:
    """Members a Stop would collect without having announced them first."""
    return sorted(
        member["agent_id"]
        for member in record.get("members") or ()
        if member["teardown_state"] != TEARDOWN_REQUESTED
    )


def _later_work_refusals(record: Mapping[str, Any]) -> list[dict[str, str]]:
    """Members whose task record moved after the boundary this drain proved.

    The roster does not move when a supervisor dispatches another round —
    opening an occurrence touches no stable-agent revision — so boundary
    binding alone cannot see it. Quiescence has to be re-proved against M3-D's
    own record at the moment the receipt is spent, or a Pause commits on a
    classification that describes a moment that has passed.
    """
    refusals: list[dict[str, str]] = []
    for member in record.get("members") or ():
        agent_id = str(member["agent_id"])
        recorded = member.get("task_occurrence_id")
        try:
            live = occurrence.open_occurrence_for_agent(agent_id)
        except occurrence.TaskOccurrenceError as exc:
            refusals.append({"agent_id": agent_id, "reason": str(exc).splitlines()[0]})
            continue
        if recorded is None:
            # Nothing was in flight at the boundary. Anything open now started
            # afterwards.
            if live is not None:
                refusals.append(
                    {"agent_id": agent_id, "reason": "a new round opened after the boundary"}
                )
            continue
        if live is not None and live["task_occurrence_id"] != recorded:
            refusals.append(
                {"agent_id": agent_id, "reason": "a different round is open than the one drained"}
            )
            continue
        try:
            current = occurrence.get_occurrence(str(recorded))
        except occurrence.TaskOccurrenceError as exc:
            refusals.append({"agent_id": agent_id, "reason": str(exc).splitlines()[0]})
            continue
        family = "finalized" if current["state"] == occurrence.STATE_FINALIZED else "current"
        if (current[family] or {}).get("boundary_digest") != member.get("boundary_digest"):
            refusals.append(
                {"agent_id": agent_id, "reason": "the drained round recorded a later boundary"}
            )
    return refusals


def assert_spendable(
    record: Mapping[str, Any], *, session_name: str, intent: str, db: Any = None
) -> Mapping[str, Any]:
    """Refuse a receipt that no longer describes the fleet it will be spent on.

    A drain proves a boundary the fleet reached *then*. Three things can make
    that stale between earning the receipt and spending it, and all three are
    checked here rather than at three call sites:

    * the declared lifecycle moved, so the epoch this operation would claim is
      not the one drained;
    * the roster moved, so the cohort is not the one observed;
    * a member did more work, which the roster cannot see.

    The first two are also re-validated atomically by ``claim_operation`` —
    the caller passes *this* drain's boundary into the claim rather than a
    fresh observation, so the journal performs the compare-and-swap. Checking
    here first turns that race into a precise refusal instead of a generic
    "the session moved" from two layers down.

    Refusing is the whole behaviour: nothing is promoted, nothing falls back
    to force, and the answer is always "earn a fresh drain".
    """
    if record.get("state") != STATE_COMPLETE:
        raise SupervisorDrainConflict(
            f"drain {record.get('drain_id')} is {record.get('state')!r} and has no receipt; "
            f"a safe {intent} needs proof the fleet reached a boundary"
        )
    normalized = occurrence._normalise_session_name(session_name)
    if record.get("session_name") != normalized:
        raise SupervisorDrainConflict(
            f"drain {record.get('drain_id')} belongs to session {record.get('session_name')!r}, "
            f"not {session_name!r}"
        )
    if record.get("intent") != intent:
        raise SupervisorDrainConflict(
            f"drain {record.get('drain_id')} is a {record.get('intent')} drain; "
            f"a {intent} spends a {intent} drain"
        )
    if intent == INTENT_STOP:
        missing = _stop_teardown_refusals(record)
        if missing:
            raise SupervisorDrainConflict(
                f"drain {record.get('drain_id')} never announced CAO teardown for "
                f"{len(missing)} member(s) ({', '.join(item[:8] for item in missing[:4])}); a "
                "safe Stop may only collect panes it declared first"
            )

    boundary = cohort.observe_boundary(normalized, db=db)
    moved = {
        field: {"drained": record.get(drained_field), "observed": boundary[field]}
        for field, drained_field in (
            ("lifecycle_epoch", "lifecycle_epoch"),
            ("roster_revision", "roster_revision"),
            ("member_snapshot_digest", "snapshot_digest"),
        )
        if record.get(drained_field) != boundary[field]
    }
    if moved:
        raise SupervisorDrainConflict(
            f"session {normalized} moved since drain {record.get('drain_id')} proved its "
            f"boundary ({sorted(moved)}); earn a fresh drain rather than pausing on a "
            "classification of a moment that has passed"
        )

    later = _later_work_refusals(record)
    if later:
        raise SupervisorDrainConflict(
            f"drain {record.get('drain_id')} is stale: intervening later work on "
            f"{len(later)} member(s) "
            f"({', '.join(item['agent_id'][:8] + ': ' + item['reason'] for item in later[:3])}); "
            "earn a fresh drain"
        )
    return boundary


def latest_complete_drain(
    session_name: str, intent: str, db: Any = None
) -> Optional[dict[str, Any]]:
    """The newest spendable receipt for this session and intent."""
    if intent not in INTENTS:
        raise SupervisorDrainInvalid(f"intent must be one of {sorted(INTENTS)}; got {intent!r}")
    drains = [
        item
        for item in list_drains(session_name, db=db)
        if item["intent"] == intent and item["state"] == STATE_COMPLETE
    ]
    if not drains:
        return None
    return get_drain(drains[-1]["drain_id"], db=db)


def render_steer(drain_id: str) -> str:
    """The exact single-line steer this drain sends. Exposed for surfaces."""
    return STEER_TEMPLATE.format(drain_id=_require_uuid(drain_id, field_name="drain_id"))


__all__ = [
    "DrainObservation",
    "DrainRequest",
    "SupervisorDrainConflict",
    "SupervisorDrainError",
    "SupervisorDrainInvalid",
    "SupervisorDrainNotFound",
    "SupervisorDrainUnavailable",
    "assert_spendable",
    "cohort_member_results",
    "drain_for_receipt",
    "drain_provenance",
    "execute_drain",
    "force_promotion_receipt",
    "get_drain",
    "latest_complete_drain",
    "list_drains",
    "receipt_digest",
    "record_member",
    "render_steer",
    "snapshot_drain",
    "steer_control_id",
    "teardown_request_id",
]

"""Dark durable cohort journal for fleet Pause/Stop (cond-0379 C1).

C1 records the closed cohort boundary that later M3-C slices will execute. It
does not claim the M3-B Stop barrier, change ``session_lifecycle``, touch tmux or
a provider, interrupt a worker, terminate a wait, or send conductor input.
Shipping this module therefore cannot activate Pause/Stop merely because a
server imports it.

The journal provides four integrity seams:

* ``observe_boundary`` returns the exact declared lifecycle epoch, an opaque
  digest of the sorted stable-agent id/revision vector, and a digest-bound
  snapshot of every stable agent in the session. Live and identity-missing
  agents are included; already dormant/retired agents remain visible but are
  excluded from fleet restore.
* ``claim_operation`` atomically revalidates that observation and creates one
  winning Pause/Stop operation for the exact lifecycle/roster slot. Exact
  response-loss replays adopt; changed requests or competing operation ids
  surface the durable winner.
* ``transition_operation`` advances a closed, action-specific state machine by
  state-epoch CAS. Safe never becomes force implicitly: promotion requires an
  explicit flag and receipt digest and is preserved as an append-only
  transition. C1 cannot enter terminal ``paused``/``stopped`` through this
  journal-only function; the later executor must pair those states atomically
  with the corresponding session-lifecycle CAS.
* ``record_member_result`` CAS-records the bounded per-member evidence carriers
  named by the accepted design. It stores references/digests and concise
  outcomes, never task text, provider output, environment values, or secrets.

All mutation is short SQLite work. No transaction or Python lock crosses
future provider, tmux, network, or conductor I/O.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import session_lifecycle as sl
from cli_agent_orchestrator.services import stable_agent_roster as roster

SCHEMA_VERSION = "cao-m3-cohort-journal-v1"

KIND_PAUSE = "pause"
KIND_STOP = "stop"
OPERATION_KINDS = frozenset({KIND_PAUSE, KIND_STOP})

MODE_SAFE = "safe"
MODE_FORCE = "force"
MODES = frozenset({MODE_SAFE, MODE_FORCE})

INITIATOR_OPERATOR = "operator"
INITIATOR_SUPERVISOR = "supervisor"
INITIATOR_KINDS = frozenset({INITIATOR_OPERATOR, INITIATOR_SUPERVISOR})

STATE_PREPARING = "preparing"
STATE_DRAINING = "draining-to-boundary"
STATE_INTERRUPTING = "interrupting"
STATE_TEARING_DOWN = "tearing-down"
STATE_PAUSED = "paused"
STATE_STOPPED = "stopped"
STATE_RESTORING = "restoring"
STATE_RECONCILIATION_REQUIRED = "reconciliation-required"
STATE_SETTLED = "settled"
STATES = frozenset(
    {
        STATE_PREPARING,
        STATE_DRAINING,
        STATE_INTERRUPTING,
        STATE_TEARING_DOWN,
        STATE_PAUSED,
        STATE_STOPPED,
        STATE_RESTORING,
        STATE_RECONCILIATION_REQUIRED,
        STATE_SETTLED,
    }
)

FINAL_PENDING = "pending"
FINAL_EXCLUDED_HISTORICAL = "excluded-historical"
FINAL_DRAINED = "drained"
FINAL_INTERRUPTED = "interrupted"
FINAL_ALREADY_IDLE = "already-idle"
FINAL_PARKED = "parked"
FINAL_EXITED = "exited"
FINAL_STOPPED = "stopped"
FINAL_RESTORED_EXACT = "restored-exact"
FINAL_RESTORED_FRESH = "restored-fresh"
FINAL_FAILED = "failed"
FINAL_UNRESUMABLE = "unresumable"
FINAL_RECONCILIATION_REQUIRED = "reconciliation-required"
MEMBER_FINAL_STATES = frozenset(
    {
        FINAL_PENDING,
        FINAL_EXCLUDED_HISTORICAL,
        FINAL_DRAINED,
        FINAL_INTERRUPTED,
        FINAL_ALREADY_IDLE,
        FINAL_PARKED,
        FINAL_EXITED,
        FINAL_STOPPED,
        FINAL_RESTORED_EXACT,
        FINAL_RESTORED_FRESH,
        FINAL_FAILED,
        FINAL_UNRESUMABLE,
        FINAL_RECONCILIATION_REQUIRED,
    }
)

LOSS_UNKNOWN = "unknown"
LOSS_NONE = "none"
LOSS_POSSIBLE = "possible"
LOSS_KNOWN = "known"
BACKGROUND_LOSS_RISKS = frozenset({LOSS_UNKNOWN, LOSS_NONE, LOSS_POSSIBLE, LOSS_KNOWN})

MAX_TEXT_LEN = 512
MAX_DETAIL_LEN = 2000
MAX_SESSION_LEN = 128
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class CohortJournalError(RuntimeError):
    code = "cohort-journal-error"


class CohortJournalInvalid(CohortJournalError):
    code = "cohort-journal-invalid"


class CohortJournalConflict(CohortJournalError):
    code = "cohort-journal-conflict"


class CohortJournalNotFound(CohortJournalError):
    code = "cohort-journal-not-found"


class CohortJournalUnavailable(CohortJournalError):
    code = "cohort-journal-unavailable"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _parse_json(raw: Optional[str]) -> Optional[Any]:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _require_text(value: Any, *, field: str, max_len: int = MAX_TEXT_LEN) -> str:
    if not isinstance(value, str) or not value:
        raise CohortJournalInvalid(f"{field} must be a non-empty string; got {value!r}")
    if len(value) > max_len:
        raise CohortJournalInvalid(f"{field} must be at most {max_len} characters")
    return value


def _optional_text(value: Any, *, field: str, max_len: int = MAX_TEXT_LEN) -> Optional[str]:
    if value is None:
        return None
    return _require_text(value, field=field, max_len=max_len)


def _require_uuid(value: Any, *, field: str) -> str:
    text_value = _require_text(value, field=field, max_len=64)
    try:
        if str(uuid.UUID(text_value)) != text_value:
            raise ValueError
    except ValueError as exc:
        raise CohortJournalInvalid(
            f"{field} must be a canonical lowercase UUID; got {text_value!r}"
        ) from exc
    return text_value


def _require_digest(value: Any, *, field: str) -> str:
    text_value = _require_text(value, field=field, max_len=64)
    if _SHA256_RE.fullmatch(text_value) is None:
        raise CohortJournalInvalid(
            f"{field} must be 64 lowercase hex characters; got {text_value!r}"
        )
    return text_value


def _optional_digest(value: Any, *, field: str) -> Optional[str]:
    if value is None:
        return None
    return _require_digest(value, field=field)


def _non_negative_int(value: Any, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CohortJournalInvalid(f"{field} must be a non-negative integer; got {value!r}")
    return value


def _normalise_session_name(value: Any) -> str:
    raw = _require_text(value, field="session_name", max_len=MAX_SESSION_LEN)
    name = sl.normalise_session_name(raw)
    if len(name) > MAX_SESSION_LEN:
        raise CohortJournalInvalid(
            f"session_name normalises to more than {MAX_SESSION_LEN} characters"
        )
    return name


def _lifecycle_observation(db: Any, session_name: str) -> dict[str, Any]:
    row = (
        db.query(database.SessionLifecycleModel)
        .filter(database.SessionLifecycleModel.session_name == session_name)
        .one_or_none()
    )
    if row is None:
        return {"lifecycle": sl.WORKING, "epoch": 0, "declared": False}
    return {"lifecycle": row.lifecycle, "epoch": int(row.epoch or 0), "declared": True}


def _member_snapshot(db: Any, agent: Any) -> dict[str, Any]:
    lineage = None
    if agent.current_lineage_id is not None:
        lineage = (
            db.query(database.StableAgentLineageModel)
            .filter(database.StableAgentLineageModel.lineage_id == agent.current_lineage_id)
            .one_or_none()
        )
    incarnation = None
    if agent.current_incarnation_id is not None:
        incarnation = (
            db.query(database.StableAgentIncarnationModel)
            .filter(
                database.StableAgentIncarnationModel.incarnation_id == agent.current_incarnation_id
            )
            .one_or_none()
        )
    restore_contract = None
    if incarnation is not None and incarnation.terminal_id is not None:
        contract_query = db.query(database.RestoreContractModel).filter(
            database.RestoreContractModel.terminal_id == incarnation.terminal_id
        )
        if incarnation.generation is None:
            contract_query = contract_query.filter(
                database.RestoreContractModel.generation.is_(None)
            )
        else:
            contract_query = contract_query.filter(
                database.RestoreContractModel.generation == incarnation.generation
            )
        restore_contract = contract_query.one_or_none()

    included = agent.disposition in {
        roster.DISPOSITION_LIVE,
        roster.DISPOSITION_IDENTITY_MISSING,
    }
    exclusion_reason = None if included else f"pre-disposition:{agent.disposition}"
    snapshot = {
        "agent_id": agent.agent_id,
        "role": agent.role,
        "profile_family": agent.profile_family,
        "pre_disposition": agent.disposition,
        "agent_revision": int(agent.revision or 0),
        "included": included,
        "exclusion_reason": exclusion_reason,
        "lineage_id": lineage.lineage_id if lineage is not None else None,
        "harness": lineage.harness if lineage is not None else None,
        "native_session_id": lineage.native_session_id if lineage is not None else None,
        "incarnation_id": incarnation.incarnation_id if incarnation is not None else None,
        "terminal_id": incarnation.terminal_id if incarnation is not None else None,
        "generation": incarnation.generation if incarnation is not None else None,
        "pane_id": incarnation.pane_id if incarnation is not None else None,
        "restore_contract_id": (
            restore_contract.contract_id if restore_contract is not None else None
        ),
        "restore_contract_digest": (
            restore_contract.contract_digest if restore_contract is not None else None
        ),
        # M3-D is the sole task-occurrence/boundary authority. C1 carries the
        # fields truthfully empty until that integration records its receipt.
        "task_occurrence_id": None,
        "boundary_digest": None,
        "report_digest": None,
        "checkpoint_digest": None,
        "interrupt_action": None,
        "interrupt_outcome": None,
        "background_command_loss_risk": LOSS_UNKNOWN if included else LOSS_NONE,
        "final_state": FINAL_PENDING if included else FINAL_EXCLUDED_HISTORICAL,
    }
    return {**snapshot, "snapshot_digest": _digest(snapshot)}


def _observe_boundary(db: Any, session_name: str) -> dict[str, Any]:
    name = _normalise_session_name(session_name)
    lifecycle = _lifecycle_observation(db, name)
    agents = (
        db.query(database.StableAgentModel)
        .filter(database.StableAgentModel.session_name == name)
        .order_by(database.StableAgentModel.agent_id)
        .all()
    )
    members = [_member_snapshot(db, agent) for agent in agents]
    revision_vector = [
        {"agent_id": member["agent_id"], "revision": member["agent_revision"]} for member in members
    ]
    snapshot_payload = [
        {key: value for key, value in member.items() if key != "snapshot_digest"}
        for member in members
    ]
    return {
        "session_name": name,
        "lifecycle_observation": lifecycle["lifecycle"],
        "lifecycle_epoch": lifecycle["epoch"],
        "lifecycle_declared": lifecycle["declared"],
        "roster_revision": _digest(revision_vector),
        "member_snapshot_digest": _digest(snapshot_payload),
        "members": members,
    }


def observe_boundary(session_name: str, db: Any = None) -> dict[str, Any]:
    """Read the exact lifecycle/roster boundary a claim must revalidate."""
    try:
        if db is not None:
            return _observe_boundary(db, session_name)
        with database.SessionLocal() as session:
            return _observe_boundary(session, session_name)
    except CohortJournalError:
        raise
    except SQLAlchemyError as exc:
        raise CohortJournalUnavailable(
            f"cohort boundary read failed: {str(exc).splitlines()[0]}"
        ) from exc


@dataclass(frozen=True)
class OperationRequest:
    operation_id: str
    session_name: str
    operation_kind: str
    requested_mode: str
    initiator_kind: str
    initiated_by: str
    lifecycle_epoch: int
    lifecycle_observation: str
    roster_revision: str
    member_snapshot_digest: str
    source_operation_id: Optional[str] = None
    resume_target: Optional[str] = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise CohortJournalInvalid(
                f"schema_version must be {SCHEMA_VERSION!r}; got {self.schema_version!r}"
            )
        object.__setattr__(
            self, "operation_id", _require_uuid(self.operation_id, field="operation_id")
        )
        object.__setattr__(self, "session_name", _normalise_session_name(self.session_name))
        if self.operation_kind not in OPERATION_KINDS:
            raise CohortJournalInvalid(
                f"operation_kind must be one of {sorted(OPERATION_KINDS)}; "
                f"got {self.operation_kind!r}"
            )
        if self.requested_mode not in MODES:
            raise CohortJournalInvalid(
                f"requested_mode must be one of {sorted(MODES)}; got {self.requested_mode!r}"
            )
        if self.initiator_kind not in INITIATOR_KINDS:
            raise CohortJournalInvalid(
                f"initiator_kind must be one of {sorted(INITIATOR_KINDS)}; "
                f"got {self.initiator_kind!r}"
            )
        object.__setattr__(
            self, "initiated_by", _require_text(self.initiated_by, field="initiated_by")
        )
        _non_negative_int(self.lifecycle_epoch, field="lifecycle_epoch")
        if self.lifecycle_observation not in sl.LIFECYCLES:
            raise CohortJournalInvalid(
                f"lifecycle_observation must be one of {sorted(sl.LIFECYCLES)}; "
                f"got {self.lifecycle_observation!r}"
            )
        object.__setattr__(
            self,
            "roster_revision",
            _require_digest(self.roster_revision, field="roster_revision"),
        )
        object.__setattr__(
            self,
            "member_snapshot_digest",
            _require_digest(self.member_snapshot_digest, field="member_snapshot_digest"),
        )
        if self.source_operation_id is not None or self.resume_target is not None:
            raise CohortJournalInvalid(
                "C1 claims Pause/Stop boundaries only; Resume source/target fields must be absent"
            )

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "operation_id": self.operation_id,
            "session_name": self.session_name,
            "operation_kind": self.operation_kind,
            "requested_mode": self.requested_mode,
            "initiator_kind": self.initiator_kind,
            "initiated_by": self.initiated_by,
            "source_operation_id": self.source_operation_id,
            "resume_target": self.resume_target,
            "lifecycle_epoch": self.lifecycle_epoch,
            "lifecycle_observation": self.lifecycle_observation,
            "roster_revision": self.roster_revision,
            "member_snapshot_digest": self.member_snapshot_digest,
        }

    @property
    def request_digest(self) -> str:
        return _digest(self.payload())


def _operation_by_id(db: Any, operation_id: str) -> Any:
    return (
        db.query(database.SessionCohortOperationModel)
        .filter(database.SessionCohortOperationModel.operation_id == operation_id)
        .one_or_none()
    )


def _operation_by_slot(db: Any, request: OperationRequest) -> Any:
    return (
        db.query(database.SessionCohortOperationModel)
        .filter(
            database.SessionCohortOperationModel.session_name == request.session_name,
            database.SessionCohortOperationModel.lifecycle_epoch == request.lifecycle_epoch,
            database.SessionCohortOperationModel.roster_revision == request.roster_revision,
        )
        .one_or_none()
    )


def _operation_dict(row: Any) -> dict[str, Any]:
    return {
        "operation_id": row.operation_id,
        "request_digest": row.request_digest,
        "schema_version": row.schema_version,
        "session_name": row.session_name,
        "operation_kind": row.operation_kind,
        "requested_mode": row.requested_mode,
        "current_mode": row.current_mode,
        "initiator_kind": row.initiator_kind,
        "initiated_by": row.initiated_by,
        "source_operation_id": row.source_operation_id,
        "resume_target": row.resume_target,
        "lifecycle_epoch": row.lifecycle_epoch,
        "lifecycle_observation": row.lifecycle_observation,
        "roster_revision": row.roster_revision,
        "member_snapshot_digest": row.member_snapshot_digest,
        "state": row.state,
        "state_epoch": row.state_epoch,
        "request": _parse_json(row.request_json),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _member_dict(row: Any) -> dict[str, Any]:
    return {
        "operation_id": row.operation_id,
        "agent_id": row.agent_id,
        "snapshot_digest": row.snapshot_digest,
        "snapshot": _parse_json(row.snapshot_json),
        "role": row.role,
        "profile_family": row.profile_family,
        "pre_disposition": row.pre_disposition,
        "agent_revision": row.agent_revision,
        "included": bool(row.included),
        "exclusion_reason": row.exclusion_reason,
        "lineage_id": row.lineage_id,
        "harness": row.harness,
        "native_session_id": row.native_session_id,
        "incarnation_id": row.incarnation_id,
        "terminal_id": row.terminal_id,
        "generation": row.generation,
        "pane_id": row.pane_id,
        "restore_contract_id": row.restore_contract_id,
        "restore_contract_digest": row.restore_contract_digest,
        "task_occurrence_id": row.task_occurrence_id,
        "boundary_digest": row.boundary_digest,
        "report_digest": row.report_digest,
        "checkpoint_digest": row.checkpoint_digest,
        "interrupt_action": row.interrupt_action,
        "interrupt_outcome": row.interrupt_outcome,
        "background_command_loss_risk": row.background_command_loss_risk,
        "final_state": row.final_state,
        "result_detail": row.result_detail,
        "result_revision": row.result_revision,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _transition_dict(row: Any) -> dict[str, Any]:
    return {
        "transition_id": row.transition_id,
        "operation_id": row.operation_id,
        "transition_digest": row.transition_digest,
        "transition": _parse_json(row.transition_json),
        "from_state": row.from_state,
        "to_state": row.to_state,
        "from_mode": row.from_mode,
        "to_mode": row.to_mode,
        "from_state_epoch": row.from_state_epoch,
        "actor": row.actor,
        "reason": row.reason,
        "receipt_digest": row.receipt_digest,
        "created_at": row.created_at,
    }


def _claim_once(db: Any, request: OperationRequest) -> dict[str, Any]:
    existing = _operation_by_id(db, request.operation_id)
    if existing is not None:
        if existing.request_digest != request.request_digest:
            raise CohortJournalConflict(
                f"cohort operation {request.operation_id} already exists with different "
                "immutable request content"
            )
        record = _operation_dict(existing)
        record["adopted"] = True
        return record

    winner = _operation_by_slot(db, request)
    if winner is not None:
        raise CohortJournalConflict(
            f"cohort boundary (session {request.session_name}, lifecycle epoch "
            f"{request.lifecycle_epoch}, roster revision {request.roster_revision}) is "
            f"already claimed by winning operation {winner.operation_id}"
        )

    boundary = _observe_boundary(db, request.session_name)
    mismatches = {
        field: {"observed": boundary[field], "requested": getattr(request, field)}
        for field in (
            "lifecycle_epoch",
            "lifecycle_observation",
            "roster_revision",
            "member_snapshot_digest",
        )
        if boundary[field] != getattr(request, field)
    }
    if mismatches:
        raise CohortJournalConflict(
            f"session {request.session_name} moved since the cohort boundary was observed: "
            f"{mismatches}"
        )
    if request.lifecycle_observation == sl.STOPPED:
        raise CohortJournalConflict(
            f"session {request.session_name} is already stopped; C1 admits only a new "
            "Pause/Stop boundary, not Resume"
        )
    if request.operation_kind == KIND_PAUSE and request.lifecycle_observation == sl.COMPLETE:
        raise CohortJournalConflict(
            f"session {request.session_name} is complete; there is no working fleet to pause"
        )

    stamp = _now()
    row = database.SessionCohortOperationModel(
        operation_id=request.operation_id,
        request_digest=request.request_digest,
        schema_version=request.schema_version,
        session_name=request.session_name,
        operation_kind=request.operation_kind,
        requested_mode=request.requested_mode,
        current_mode=request.requested_mode,
        initiator_kind=request.initiator_kind,
        initiated_by=request.initiated_by,
        source_operation_id=None,
        resume_target=None,
        lifecycle_epoch=request.lifecycle_epoch,
        lifecycle_observation=request.lifecycle_observation,
        roster_revision=request.roster_revision,
        member_snapshot_digest=request.member_snapshot_digest,
        state=STATE_PREPARING,
        state_epoch=0,
        request_json=_canonical_json(request.payload()),
        created_at=stamp,
        updated_at=stamp,
    )
    db.add(row)
    for member in boundary["members"]:
        snapshot = {key: value for key, value in member.items() if key != "snapshot_digest"}
        db.add(
            database.SessionCohortMemberModel(
                operation_id=request.operation_id,
                agent_id=member["agent_id"],
                snapshot_digest=member["snapshot_digest"],
                snapshot_json=_canonical_json(snapshot),
                role=member["role"],
                profile_family=member["profile_family"],
                pre_disposition=member["pre_disposition"],
                agent_revision=member["agent_revision"],
                included=int(member["included"]),
                exclusion_reason=member["exclusion_reason"],
                lineage_id=member["lineage_id"],
                harness=member["harness"],
                native_session_id=member["native_session_id"],
                incarnation_id=member["incarnation_id"],
                terminal_id=member["terminal_id"],
                generation=member["generation"],
                pane_id=member["pane_id"],
                restore_contract_id=member["restore_contract_id"],
                restore_contract_digest=member["restore_contract_digest"],
                task_occurrence_id=None,
                boundary_digest=None,
                report_digest=None,
                checkpoint_digest=None,
                interrupt_action=None,
                interrupt_outcome=None,
                background_command_loss_risk=member["background_command_loss_risk"],
                final_state=member["final_state"],
                result_detail=None,
                result_revision=0,
                created_at=stamp,
                updated_at=stamp,
            )
        )
    db.flush()
    record = _operation_dict(row)
    record["adopted"] = False
    return record


def claim_operation(request: OperationRequest, db: Any = None) -> dict[str, Any]:
    """Claim one exact Pause/Stop boundary; performs no physical effect."""
    if not isinstance(request, OperationRequest):
        raise CohortJournalInvalid(
            f"request must be an OperationRequest; got {type(request).__name__}"
        )
    if db is not None:
        try:
            with db.begin_nested():
                return _claim_once(db, request)
        except (IntegrityError, OperationalError) as exc:
            raise CohortJournalUnavailable(
                f"concurrent cohort claim refused; roll back and retry to adopt: {exc}"
            ) from exc

    last_error: Optional[BaseException] = None
    for _attempt in range(5):
        try:
            with database.SessionLocal() as session:
                result = _claim_once(session, request)
                session.commit()
                return result
        except IntegrityError as exc:
            last_error = exc
            time.sleep(0.05)
        except OperationalError as exc:
            last_error = exc
            time.sleep(0.05)
    raise CohortJournalUnavailable(
        f"concurrent cohort claims kept conflicting; refusing after retry: {last_error}"
    )


@dataclass(frozen=True)
class TransitionRequest:
    transition_id: str
    operation_id: str
    expected_state_epoch: int
    to_state: str
    actor: str
    reason: Optional[str] = None
    receipt_digest: Optional[str] = None
    promote_to_force: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "transition_id", _require_uuid(self.transition_id, field="transition_id")
        )
        object.__setattr__(
            self, "operation_id", _require_uuid(self.operation_id, field="operation_id")
        )
        _non_negative_int(self.expected_state_epoch, field="expected_state_epoch")
        if self.to_state not in STATES:
            raise CohortJournalInvalid(
                f"to_state must be one of {sorted(STATES)}; got {self.to_state!r}"
            )
        object.__setattr__(self, "actor", _require_text(self.actor, field="actor"))
        object.__setattr__(self, "reason", _optional_text(self.reason, field="reason"))
        object.__setattr__(
            self,
            "receipt_digest",
            _optional_digest(self.receipt_digest, field="receipt_digest"),
        )
        if not isinstance(self.promote_to_force, bool):
            raise CohortJournalInvalid("promote_to_force must be a boolean")
        if self.promote_to_force and self.receipt_digest is None:
            raise CohortJournalInvalid(
                "an explicit safe-to-force promotion requires a receipt_digest"
            )

    def payload(self) -> dict[str, Any]:
        return {
            "transition_id": self.transition_id,
            "operation_id": self.operation_id,
            "expected_state_epoch": self.expected_state_epoch,
            "to_state": self.to_state,
            "actor": self.actor,
            "reason": self.reason,
            "receipt_digest": self.receipt_digest,
            "promote_to_force": self.promote_to_force,
        }

    @property
    def transition_digest(self) -> str:
        return _digest(self.payload())


def _allowed_target(operation_kind: str, current_mode: str, state: str) -> frozenset[str]:
    if operation_kind == KIND_PAUSE:
        if state == STATE_PREPARING:
            return frozenset({STATE_DRAINING if current_mode == MODE_SAFE else STATE_INTERRUPTING})
        if state in {STATE_DRAINING, STATE_INTERRUPTING}:
            # C1 cannot record journal-only completion. A later executor must
            # add the paired lifecycle-CAS function that commits ``paused`` in
            # the same transaction as the terminal cohort state.
            return frozenset({STATE_RECONCILIATION_REQUIRED})
        if state == STATE_RECONCILIATION_REQUIRED:
            return frozenset({STATE_DRAINING if current_mode == MODE_SAFE else STATE_INTERRUPTING})
        return frozenset()
    if operation_kind == KIND_STOP:
        if state == STATE_PREPARING:
            return frozenset({STATE_DRAINING if current_mode == MODE_SAFE else STATE_TEARING_DOWN})
        if state == STATE_DRAINING:
            return frozenset({STATE_TEARING_DOWN, STATE_RECONCILIATION_REQUIRED})
        if state == STATE_TEARING_DOWN:
            # As above, C1 never records ``stopped`` without the later paired
            # lifecycle/barrier commit.
            return frozenset({STATE_RECONCILIATION_REQUIRED})
        if state == STATE_RECONCILIATION_REQUIRED:
            if current_mode == MODE_SAFE:
                # A safe-stop reconciliation may have been entered while
                # draining or while tearing down after a valid drain receipt.
                # The retry receipt disambiguates the operator/supervisor's
                # chosen continuation; force promotion stays a distinct path.
                return frozenset({STATE_DRAINING, STATE_TEARING_DOWN})
            return frozenset({STATE_TEARING_DOWN})
    return frozenset()


def _transition_once(db: Any, request: TransitionRequest) -> dict[str, Any]:
    existing = (
        db.query(database.SessionCohortTransitionModel)
        .filter(database.SessionCohortTransitionModel.transition_id == request.transition_id)
        .one_or_none()
    )
    if existing is not None:
        if existing.transition_digest != request.transition_digest:
            raise CohortJournalConflict(
                f"cohort transition {request.transition_id} already exists with different "
                "immutable request content"
            )
        operation = _operation_by_id(db, request.operation_id)
        return {
            "transition": _transition_dict(existing),
            "operation": _operation_dict(operation),
            "adopted": True,
        }

    operation = _operation_by_id(db, request.operation_id)
    if operation is None:
        raise CohortJournalNotFound(f"unknown cohort operation: {request.operation_id}")
    if int(operation.state_epoch or 0) != request.expected_state_epoch:
        raise CohortJournalConflict(
            f"cohort operation {request.operation_id} moved to state epoch "
            f"{operation.state_epoch}; expected {request.expected_state_epoch}"
        )
    epoch_winner = (
        db.query(database.SessionCohortTransitionModel)
        .filter(
            database.SessionCohortTransitionModel.operation_id == request.operation_id,
            database.SessionCohortTransitionModel.from_state_epoch == request.expected_state_epoch,
        )
        .one_or_none()
    )
    if epoch_winner is not None:
        raise CohortJournalConflict(
            f"cohort operation {request.operation_id} state epoch "
            f"{request.expected_state_epoch} was already advanced by transition "
            f"{epoch_winner.transition_id}"
        )

    from_state = operation.state
    from_mode = operation.current_mode
    to_mode = from_mode
    if request.promote_to_force:
        expected_target = (
            STATE_INTERRUPTING if operation.operation_kind == KIND_PAUSE else STATE_TEARING_DOWN
        )
        if operation.requested_mode != MODE_SAFE or from_mode != MODE_SAFE:
            raise CohortJournalConflict(
                "only a still-safe operation may be explicitly promoted to force"
            )
        if from_state not in {STATE_DRAINING, STATE_RECONCILIATION_REQUIRED}:
            raise CohortJournalConflict(
                f"safe {operation.operation_kind} promotion must start from "
                f"{STATE_DRAINING!r} or {STATE_RECONCILIATION_REQUIRED!r}"
            )
        if request.to_state != expected_target:
            raise CohortJournalConflict(
                f"safe {operation.operation_kind} promotion must move to " f"{expected_target!r}"
            )
        to_mode = MODE_FORCE
    else:
        allowed = _allowed_target(operation.operation_kind, from_mode, from_state)
        if request.to_state not in allowed:
            raise CohortJournalConflict(
                f"{from_mode} {operation.operation_kind} cannot move from "
                f"{from_state!r} to {request.to_state!r}; allowed={sorted(allowed)}"
            )
        if from_state == STATE_RECONCILIATION_REQUIRED and request.receipt_digest is None:
            raise CohortJournalConflict(
                "an explicit retry from reconciliation-required needs a receipt_digest"
            )

    stamp = _now()
    result = db.execute(
        sa_update(database.SessionCohortOperationModel)
        .where(
            database.SessionCohortOperationModel.operation_id == request.operation_id,
            database.SessionCohortOperationModel.state_epoch == request.expected_state_epoch,
            database.SessionCohortOperationModel.state == from_state,
            database.SessionCohortOperationModel.current_mode == from_mode,
        )
        .values(
            state=request.to_state,
            current_mode=to_mode,
            state_epoch=request.expected_state_epoch + 1,
            updated_at=stamp,
        )
    )
    if result.rowcount != 1:
        raise CohortJournalConflict(
            f"cohort operation {request.operation_id} moved concurrently; retry by reading "
            "the durable operation"
        )
    transition = database.SessionCohortTransitionModel(
        transition_id=request.transition_id,
        operation_id=request.operation_id,
        transition_digest=request.transition_digest,
        transition_json=_canonical_json(request.payload()),
        from_state=from_state,
        to_state=request.to_state,
        from_mode=from_mode,
        to_mode=to_mode,
        from_state_epoch=request.expected_state_epoch,
        actor=request.actor,
        reason=request.reason,
        receipt_digest=request.receipt_digest,
        created_at=stamp,
    )
    db.add(transition)
    db.flush()
    db.refresh(operation)
    return {
        "transition": _transition_dict(transition),
        "operation": _operation_dict(operation),
        "adopted": False,
    }


def transition_operation(request: TransitionRequest, db: Any = None) -> dict[str, Any]:
    """Advance the dark cohort state machine; performs no external effect."""
    if not isinstance(request, TransitionRequest):
        raise CohortJournalInvalid(
            f"request must be a TransitionRequest; got {type(request).__name__}"
        )
    if db is not None:
        try:
            with db.begin_nested():
                return _transition_once(db, request)
        except (IntegrityError, OperationalError) as exc:
            raise CohortJournalUnavailable(
                f"concurrent cohort transition refused; retry by reading the winner: {exc}"
            ) from exc
    with database.SessionLocal() as session:
        try:
            result = _transition_once(session, request)
            session.commit()
            return result
        except (IntegrityError, OperationalError) as exc:
            session.rollback()
            raise CohortJournalUnavailable(
                f"concurrent cohort transition refused; retry by reading the winner: {exc}"
            ) from exc


@dataclass(frozen=True)
class MemberResult:
    operation_id: str
    agent_id: str
    expected_result_revision: int
    final_state: str
    background_command_loss_risk: str
    task_occurrence_id: Optional[str] = None
    boundary_digest: Optional[str] = None
    report_digest: Optional[str] = None
    checkpoint_digest: Optional[str] = None
    interrupt_action: Optional[str] = None
    interrupt_outcome: Optional[str] = None
    result_detail: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "operation_id", _require_uuid(self.operation_id, field="operation_id")
        )
        object.__setattr__(self, "agent_id", _require_uuid(self.agent_id, field="agent_id"))
        _non_negative_int(self.expected_result_revision, field="expected_result_revision")
        if self.final_state not in MEMBER_FINAL_STATES:
            raise CohortJournalInvalid(
                f"final_state must be one of {sorted(MEMBER_FINAL_STATES)}; "
                f"got {self.final_state!r}"
            )
        if self.background_command_loss_risk not in BACKGROUND_LOSS_RISKS:
            raise CohortJournalInvalid(
                "background_command_loss_risk must be one of " f"{sorted(BACKGROUND_LOSS_RISKS)}"
            )
        object.__setattr__(
            self,
            "task_occurrence_id",
            _optional_text(self.task_occurrence_id, field="task_occurrence_id"),
        )
        for field in ("boundary_digest", "report_digest", "checkpoint_digest"):
            object.__setattr__(self, field, _optional_digest(getattr(self, field), field=field))
        object.__setattr__(
            self,
            "interrupt_action",
            _optional_text(self.interrupt_action, field="interrupt_action"),
        )
        object.__setattr__(
            self,
            "interrupt_outcome",
            _optional_text(self.interrupt_outcome, field="interrupt_outcome"),
        )
        if (self.interrupt_action is None) != (self.interrupt_outcome is None):
            raise CohortJournalInvalid(
                "interrupt_action and interrupt_outcome must be both present or both absent"
            )
        object.__setattr__(
            self,
            "result_detail",
            _optional_text(self.result_detail, field="result_detail", max_len=MAX_DETAIL_LEN),
        )

    def values(self) -> dict[str, Any]:
        return {
            "task_occurrence_id": self.task_occurrence_id,
            "boundary_digest": self.boundary_digest,
            "report_digest": self.report_digest,
            "checkpoint_digest": self.checkpoint_digest,
            "interrupt_action": self.interrupt_action,
            "interrupt_outcome": self.interrupt_outcome,
            "background_command_loss_risk": self.background_command_loss_risk,
            "final_state": self.final_state,
            "result_detail": self.result_detail,
        }


def _record_member_result_once(db: Any, request: MemberResult) -> dict[str, Any]:
    operation = _operation_by_id(db, request.operation_id)
    if operation is None:
        raise CohortJournalNotFound(f"unknown cohort operation: {request.operation_id}")
    member = (
        db.query(database.SessionCohortMemberModel)
        .filter(
            database.SessionCohortMemberModel.operation_id == request.operation_id,
            database.SessionCohortMemberModel.agent_id == request.agent_id,
        )
        .one_or_none()
    )
    if member is None:
        raise CohortJournalNotFound(
            f"stable agent {request.agent_id} is not a member of cohort " f"{request.operation_id}"
        )
    values = request.values()
    current = {field: getattr(member, field) for field in values}
    if current == values and int(member.result_revision or 0) >= request.expected_result_revision:
        record = _member_dict(member)
        record["adopted"] = True
        return record
    if int(member.result_revision or 0) != request.expected_result_revision:
        raise CohortJournalConflict(
            f"cohort member {request.agent_id} moved to result revision "
            f"{member.result_revision}; expected {request.expected_result_revision}"
        )
    if not bool(member.included) and request.final_state != FINAL_EXCLUDED_HISTORICAL:
        raise CohortJournalConflict(
            f"cohort member {request.agent_id} was excluded as historical and cannot be "
            f"recorded as {request.final_state!r}"
        )
    if bool(member.included) and request.final_state == FINAL_EXCLUDED_HISTORICAL:
        raise CohortJournalConflict(
            f"cohort member {request.agent_id} was live/identity-missing at the boundary "
            "and cannot be relabeled excluded-historical"
        )
    if request.interrupt_action is not None and operation.current_mode != MODE_FORCE:
        raise CohortJournalConflict(
            "provider interrupt evidence belongs only to an explicitly force-mode operation"
        )
    stamp = _now()
    result = db.execute(
        sa_update(database.SessionCohortMemberModel)
        .where(
            database.SessionCohortMemberModel.operation_id == request.operation_id,
            database.SessionCohortMemberModel.agent_id == request.agent_id,
            database.SessionCohortMemberModel.result_revision == request.expected_result_revision,
        )
        .values(
            **values,
            result_revision=request.expected_result_revision + 1,
            updated_at=stamp,
        )
    )
    if result.rowcount != 1:
        raise CohortJournalConflict(
            f"cohort member {request.agent_id} moved concurrently; read and retry"
        )
    db.refresh(member)
    record = _member_dict(member)
    record["adopted"] = False
    return record


def record_member_result(request: MemberResult, db: Any = None) -> dict[str, Any]:
    """CAS-record one member's bounded evidence and current/final result."""
    if not isinstance(request, MemberResult):
        raise CohortJournalInvalid(f"request must be a MemberResult; got {type(request).__name__}")
    if db is not None:
        try:
            with db.begin_nested():
                return _record_member_result_once(db, request)
        except (IntegrityError, OperationalError) as exc:
            raise CohortJournalUnavailable(
                f"concurrent cohort member write refused; read and retry: {exc}"
            ) from exc
    with database.SessionLocal() as session:
        try:
            result = _record_member_result_once(session, request)
            session.commit()
            return result
        except (IntegrityError, OperationalError) as exc:
            session.rollback()
            raise CohortJournalUnavailable(
                f"concurrent cohort member write refused; read and retry: {exc}"
            ) from exc


def get_operation(operation_id: str, db: Any = None) -> dict[str, Any]:
    """Return an operation with its exact member snapshot and transition log."""
    operation_id = _require_uuid(operation_id, field="operation_id")

    def _get(session: Any) -> dict[str, Any]:
        row = _operation_by_id(session, operation_id)
        if row is None:
            raise CohortJournalNotFound(f"unknown cohort operation: {operation_id}")
        members = (
            session.query(database.SessionCohortMemberModel)
            .filter(database.SessionCohortMemberModel.operation_id == operation_id)
            .order_by(database.SessionCohortMemberModel.agent_id)
            .all()
        )
        transitions = (
            session.query(database.SessionCohortTransitionModel)
            .filter(database.SessionCohortTransitionModel.operation_id == operation_id)
            .order_by(
                database.SessionCohortTransitionModel.from_state_epoch,
                database.SessionCohortTransitionModel.transition_id,
            )
            .all()
        )
        return {
            **_operation_dict(row),
            "members": [_member_dict(member) for member in members],
            "transitions": [_transition_dict(transition) for transition in transitions],
        }

    try:
        if db is not None:
            return _get(db)
        with database.SessionLocal() as session:
            return _get(session)
    except CohortJournalError:
        raise
    except SQLAlchemyError as exc:
        raise CohortJournalUnavailable(
            f"cohort operation read failed: {str(exc).splitlines()[0]}"
        ) from exc


def list_operations(session_name: Optional[str] = None, db: Any = None) -> list[dict[str, Any]]:
    """List cohort operation projections, oldest first; never consults tmux."""
    name = _normalise_session_name(session_name) if session_name is not None else None

    def _list(session: Any) -> list[dict[str, Any]]:
        query = session.query(database.SessionCohortOperationModel)
        if name is not None:
            query = query.filter(database.SessionCohortOperationModel.session_name == name)
        rows = query.order_by(
            database.SessionCohortOperationModel.created_at,
            database.SessionCohortOperationModel.operation_id,
        ).all()
        return [_operation_dict(row) for row in rows]

    try:
        if db is not None:
            return _list(db)
        with database.SessionLocal() as session:
            return _list(session)
    except CohortJournalError:
        raise
    except SQLAlchemyError as exc:
        raise CohortJournalUnavailable(
            f"cohort operation list failed: {str(exc).splitlines()[0]}"
        ) from exc

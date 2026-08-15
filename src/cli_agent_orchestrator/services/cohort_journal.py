"""Dark durable cohort journal for fleet Pause/Stop (cond-0379 C1-C2).

C1 records the closed cohort boundary that later M3-C slices execute. C2 adds
the two database-only atomic seams that execution needs: Stop enters teardown
in the same transaction that claims the M3-B barrier, and a proven terminal
cohort commits ``session_lifecycle`` in the same transaction as its terminal
state. Neither seam touches tmux or a provider, interrupts a worker, terminates
a wait, or sends conductor input. No public mutation route invokes them yet,
so importing or reading this module cannot activate Pause/Stop.

The journal provides six integrity seams:

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
  transition. The generic path cannot enter Stop teardown or terminal
  ``paused``/``stopped`` states.
* ``begin_stop_teardown`` atomically revalidates the exact lifecycle/roster
  boundary, claims the M3-B Stop barrier for this operation, and appends the
  teardown transition. Safe Stop requires the opaque M3-D receipt; explicit
  safe-to-force promotion stays visible and receipted.
* ``commit_terminal`` validates the bounded member result set and atomically
  commits the cohort plus declared session lifecycle to ``paused``/``stopped``.
  Stop additionally requires the exact operation-owned barrier. Terminal
  member evidence is immutable except for exact response-loss replay.
* ``begin_resume_restore`` is Stop's mirror image and the C4 addition: it
  atomically releases the exact Stop barrier that operation claimed and
  declares the recorded Resume target, so restore effects become legal in the
  same transaction that stops the session reading ``stopped``. It is reachable
  only once, from ``preparing``, and only for an operator-initiated Resume
  whose source is a terminally stopped cohort of the same session.
* ``record_member_result`` CAS-records the bounded per-member evidence carriers
  named by the accepted design. It stores references/digests and concise
  outcomes, never task text, provider output, environment values, or secrets.

All mutation is short SQLite work. Caller-owned transactions remain caller
owned, including on SQLite's lazy transaction driver. No transaction or Python
lock crosses future provider, tmux, network, or conductor I/O.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import exists as sa_exists
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import operation_journal as oj
from cli_agent_orchestrator.services import session_lifecycle as sl
from cli_agent_orchestrator.services import stable_agent_roster as roster

SCHEMA_VERSION = "cao-m3-cohort-journal-v1"

KIND_PAUSE = "pause"
KIND_STOP = "stop"
KIND_RESUME = "resume"
OPERATION_KINDS = frozenset({KIND_PAUSE, KIND_STOP, KIND_RESUME})

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

PAUSE_TERMINAL_MEMBER_STATES = {
    MODE_SAFE: frozenset({FINAL_DRAINED, FINAL_ALREADY_IDLE, FINAL_PARKED}),
    MODE_FORCE: frozenset({FINAL_INTERRUPTED, FINAL_ALREADY_IDLE, FINAL_PARKED, FINAL_EXITED}),
}
STOP_TERMINAL_MEMBER_STATES = frozenset({FINAL_STOPPED, FINAL_EXITED, FINAL_UNRESUMABLE})
#: A Resume settles once every included member reached a *decided* restore
#: outcome, and ``failed`` is one of them.
#:
#: This is load-bearing and easy to get backwards. A worker that could not be
#: brought back is a decided fact about that worker, not an unknown about the
#: fleet: its siblings are running, its supervisor is up, and the campaign can
#: make progress. Blocking the terminal commit on it would strand the whole
#: restored fleet in ``reconciliation-required`` — supervisor included — over
#: one pane, which is the opposite of what a partial restore should cost. The
#: failure is not swallowed: it is durable per-member evidence, and
#: Resume-and-start's single wake describes every exact/fresh/failed outcome
#: so the supervisor reconciles against the truth.
#:
#: ``reconciliation-required`` stays for outcomes that are genuinely *not*
#: decided — pending, ambiguous, or otherwise nonterminal.
RESUME_TERMINAL_MEMBER_STATES = frozenset(
    {FINAL_RESTORED_EXACT, FINAL_RESTORED_FRESH, FINAL_FAILED, FINAL_UNRESUMABLE}
)

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


class SessionEffectRefused(CohortJournalConflict):
    """A provider/input effect lost to the durable Stop boundary."""

    code = "session-effect-refused"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _anchor_caller_transaction(db: Any) -> None:
    """Ensure SQLite has a physical outer transaction before a savepoint.

    SQLAlchemy's Session transaction is lazy on SQLite. If ``begin_nested`` is
    the first statement, SQLite treats that SAVEPOINT as the outer transaction
    and RELEASE commits it, so the caller's later rollback cannot undo the
    journal write. An explicit deferred BEGIN only when the DB-API connection
    is not already in a transaction preserves the caller-owned boundary. Other
    dialects already give a nested transaction a real outer transaction.
    """
    connection = db.connection()
    if connection.dialect.name != "sqlite":
        return
    driver_connection = connection.connection.driver_connection
    if not driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN")


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


def _resumed_cohort_agent_ids(db: Any, source_operation_id: str) -> frozenset[str]:
    """The stable agents an operator Resume is entitled to bring back.

    Inclusion cannot be read off the live roster here the way a Pause/Stop
    boundary reads it. A Stop retires every incarnation it collects, so by the
    time a Resume observes the session *every* agent is dormant and the
    disposition rule would exclude the entire fleet. The exact set is instead
    the one the Stop cohort captured: stable agent identity is what survives a
    stop, while incarnations do not.

    This is also what keeps Resume from resurrecting an agent that was already
    dormant *before* the Stop — that agent was excluded then and is not in
    this set now.
    """
    rows = (
        db.query(database.SessionCohortMemberModel.agent_id)
        .filter(
            database.SessionCohortMemberModel.operation_id == source_operation_id,
            database.SessionCohortMemberModel.included == 1,
        )
        .all()
    )
    return frozenset(row[0] for row in rows)


def _member_snapshot(
    db: Any, agent: Any, resumed: Optional[frozenset[str]] = None
) -> dict[str, Any]:
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

    if resumed is None:
        included = agent.disposition in {
            roster.DISPOSITION_LIVE,
            roster.DISPOSITION_IDENTITY_MISSING,
        }
        exclusion_reason = None if included else f"pre-disposition:{agent.disposition}"
    else:
        included = agent.agent_id in resumed
        exclusion_reason = None if included else "outside-resumed-cohort"
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
        # M3-D is the sole task-occurrence/boundary authority. The journal
        # carries these fields truthfully empty until M3-D records its receipt.
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


def _observe_boundary(
    db: Any, session_name: str, resume_source_operation_id: Optional[str] = None
) -> dict[str, Any]:
    name = _normalise_session_name(session_name)
    lifecycle = _lifecycle_observation(db, name)
    agents = (
        db.query(database.StableAgentModel)
        .filter(database.StableAgentModel.session_name == name)
        .order_by(database.StableAgentModel.agent_id)
        .all()
    )
    resumed = (
        None
        if resume_source_operation_id is None
        else _resumed_cohort_agent_ids(db, resume_source_operation_id)
    )
    members = [_member_snapshot(db, agent, resumed) for agent in agents]
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


def observe_boundary(
    session_name: str, db: Any = None, resume_source_operation_id: Optional[str] = None
) -> dict[str, Any]:
    """Read the exact lifecycle/roster boundary a claim must revalidate.

    Pass ``resume_source_operation_id`` to observe a *Resume* boundary: the
    included set then comes from that Stop cohort's captured membership rather
    than from live dispositions, which a completed Stop has already retired.
    """
    try:
        if db is not None:
            return _observe_boundary(db, session_name, resume_source_operation_id)
        with database.SessionLocal() as session:
            return _observe_boundary(session, session_name, resume_source_operation_id)
    except CohortJournalError:
        raise
    except SQLAlchemyError as exc:
        raise CohortJournalUnavailable(
            f"cohort boundary read failed: {str(exc).splitlines()[0]}"
        ) from exc


@contextmanager
def session_effect_admission(session_name: Any):
    """Serialize one provider/input effect against the Stop linearization.

    The claim taken here is the *shared* mode of the same lifecycle-write claim
    held exclusively while C2 installs the durable Stop barrier.  An effect that
    entered first finishes before Stop can claim; once Stop wins, every later
    effect refuses before its own write/bridge claim.  The lock spans external
    I/O by necessity: a check released before the pane write would recreate the
    exact check/use race this boundary exists to close.

    Shared rather than exclusive because this boundary linearizes effects
    against *lifecycle writes*, not against each other.  Concurrent effects are
    arbitrated where they actually collide — the per-pane input lease, which
    refuses a busy pane with zero bytes.  Holding this claim exclusively put
    every effect in one per-session queue, so a second control arriving
    mid-write waited out the whole write instead of being refused, and was then
    typed after it.
    """
    if not isinstance(session_name, str) or not session_name:
        raise SessionEffectRefused("provider effect has no durable CAO session identity")
    name = _normalise_session_name(session_name)
    from cli_agent_orchestrator.services.callback_recovery import (
        session_effect_claim,
    )

    with session_effect_claim(name):
        barrier = oj.get_session_barrier(name)
        if barrier is not None and barrier["state"] == oj.BARRIER_CLAIMED:
            raise SessionEffectRefused(
                f"session {name} Stop barrier is claimed by {barrier['claimed_by']!r}; "
                "no later provider/input effect is admitted"
            )
        lifecycle = sl.describe(name)
        if lifecycle.get("unreadable"):
            raise SessionEffectRefused(
                f"session {name} lifecycle is unreadable; refusing a new provider/input effect"
            )
        if lifecycle["lifecycle"] == sl.STOPPED:
            raise SessionEffectRefused(
                f"session {name} is stopped; only an operator Resume may reopen effects"
            )
        yield


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
        if self.operation_kind == KIND_RESUME:
            # Resume provenance is mandatory and immutable: the exact Stop
            # cohort this Resume descends from, and the declared lifecycle it
            # is authorized to restore.  Without both, a later reader could
            # not tell which stopped fleet a restore belongs to.
            object.__setattr__(
                self,
                "source_operation_id",
                _require_uuid(self.source_operation_id, field="source_operation_id"),
            )
            if self.resume_target not in sl.RESTORE_TARGETS:
                raise CohortJournalInvalid(
                    f"resume_target must be one of {sorted(sl.RESTORE_TARGETS)}; "
                    f"got {self.resume_target!r}"
                )
            if self.initiator_kind != INITIATOR_OPERATOR:
                raise CohortJournalInvalid(
                    "only an operator may claim a Resume boundary for a stopped session"
                )
            if self.requested_mode != MODE_SAFE:
                raise CohortJournalInvalid(
                    "Resume has no force mode: there is no live work to interrupt in a "
                    "stopped session"
                )
        elif self.source_operation_id is not None or self.resume_target is not None:
            raise CohortJournalInvalid(
                f"a {self.operation_kind} boundary carries no Resume source/target fields"
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


def _validate_resume_source(db: Any, request: OperationRequest) -> Any:
    """Bind a Resume to the exact stopped cohort it descends from.

    A Resume is only meaningful against a session this fork actually stopped
    through a durable cohort operation.  Requiring the source row here — at
    claim, before any state exists — means a Resume can never be pointed at
    another session's Stop, at a Pause, or at a Stop that never reached its
    terminal commit, and the provenance a later reader needs is written once
    and never recomputed.
    """
    if request.lifecycle_observation != sl.STOPPED:
        raise CohortJournalConflict(
            f"session {request.session_name} is {request.lifecycle_observation!r}; a Resume "
            "boundary exists only for a stopped session"
        )
    source = _operation_by_id(db, str(request.source_operation_id))
    if source is None:
        raise CohortJournalNotFound(
            f"unknown Resume source cohort operation: {request.source_operation_id}"
        )
    if source.session_name != request.session_name:
        raise CohortJournalConflict(
            f"Resume source {request.source_operation_id} belongs to session "
            f"{source.session_name!r}, not {request.session_name!r}"
        )
    if source.operation_kind != KIND_STOP or source.state != STATE_STOPPED:
        raise CohortJournalConflict(
            f"Resume source {request.source_operation_id} is a {source.operation_kind} in state "
            f"{source.state!r}; only a terminally stopped cohort can be resumed"
        )
    row = (
        db.query(database.SessionLifecycleModel)
        .filter(database.SessionLifecycleModel.session_name == request.session_name)
        .one_or_none()
    )
    restore_to = row.restore_to if row is not None else None
    # Resume-paused is always available: it restores the panes and stops
    # there, which is a strictly smaller act than whatever the session was
    # doing before. Any other target must be the one the Stop recorded.
    if request.resume_target != sl.PAUSED and request.resume_target != restore_to:
        raise CohortJournalConflict(
            f"session {request.session_name} recorded restore_to={restore_to!r}; a Resume may "
            f"target that or {sl.PAUSED!r}, not {request.resume_target!r}"
        )
    return source


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

    if request.operation_kind == KIND_RESUME:
        # Validated first: the Resume boundary's membership is *derived* from
        # the source Stop cohort, so an unusable source has to be refused
        # before the observation it would define.
        _validate_resume_source(db, request)
    elif request.lifecycle_observation == sl.STOPPED:
        raise CohortJournalConflict(
            f"session {request.session_name} is stopped; only an operator Resume may claim a "
            "boundary on it"
        )

    boundary = _observe_boundary(db, request.session_name, request.source_operation_id)
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
    if request.operation_kind == KIND_PAUSE and request.lifecycle_observation == sl.PAUSED:
        raise CohortJournalConflict(
            f"session {request.session_name} is already paused; "
            "there is no working fleet to pause again"
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
        source_operation_id=request.source_operation_id,
        resume_target=request.resume_target,
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
            _anchor_caller_transaction(db)
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
            # The generic path cannot record journal-only completion. C2's
            # paired lifecycle-CAS function owns terminal commit.
            return frozenset({STATE_RECONCILIATION_REQUIRED})
        if state == STATE_RECONCILIATION_REQUIRED:
            return frozenset({STATE_DRAINING if current_mode == MODE_SAFE else STATE_INTERRUPTING})
        return frozenset()
    if operation_kind == KIND_STOP:
        if state == STATE_PREPARING:
            # Entering teardown is Stop's linearization point and must claim
            # the M3-B barrier in the same transaction. The paired C2 entry
            # point owns that transition; the generic journal path may only
            # begin a safe drain.
            return frozenset({STATE_DRAINING}) if current_mode == MODE_SAFE else frozenset()
        if state == STATE_DRAINING:
            return frozenset({STATE_RECONCILIATION_REQUIRED})
        if state == STATE_TEARING_DOWN:
            # As above, the generic path never records ``stopped`` without
            # C2's paired lifecycle/barrier commit.
            return frozenset({STATE_RECONCILIATION_REQUIRED})
        if state == STATE_RECONCILIATION_REQUIRED:
            if current_mode == MODE_SAFE:
                return frozenset({STATE_DRAINING})
            return frozenset()
    if operation_kind == KIND_RESUME:
        if state == STATE_PREPARING:
            # Entering ``restoring`` releases the Stop barrier and rewrites the
            # declared lifecycle in one transaction. ``begin_resume_restore``
            # owns that pairing; the generic path must never reopen a stopped
            # campaign as a side effect of an ordinary state advance.
            return frozenset()
        if state == STATE_RESTORING:
            return frozenset({STATE_RECONCILIATION_REQUIRED})
        if state == STATE_RECONCILIATION_REQUIRED:
            # A retry re-enters restore through the generic receipted path:
            # the barrier is already open and the lifecycle already written, so
            # re-running the paired entry would be a second release.
            return frozenset({STATE_RESTORING})
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
        if operation.operation_kind == KIND_RESUME:
            raise CohortJournalConflict("a Resume operation has no force mode to promote to")
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
        if operation.operation_kind == KIND_STOP:
            raise CohortJournalConflict(
                "safe Stop promotion must use begin_stop_teardown(); entering teardown and "
                "claiming the Stop barrier are one atomic transition"
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
            _anchor_caller_transaction(db)
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
class StopTeardownRequest:
    """Enter Stop teardown while atomically claiming the M3-B barrier.

    Safe Stop supplies the M3-D drain receipt. A safe-to-force promotion is
    explicit and uses the same receipt-bearing transition rather than changing
    mode as a side effect of timeout or retry. Force Stop starts directly from
    ``preparing`` and never waits for provider I/O.
    """

    transition_id: str
    operation_id: str
    expected_state_epoch: int
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
                "an explicit safe-to-force Stop promotion requires a receipt_digest"
            )

    def payload(self) -> dict[str, Any]:
        return {
            "transition_id": self.transition_id,
            "operation_id": self.operation_id,
            "expected_state_epoch": self.expected_state_epoch,
            "to_state": STATE_TEARING_DOWN,
            "actor": self.actor,
            "reason": self.reason,
            "receipt_digest": self.receipt_digest,
            "promote_to_force": self.promote_to_force,
            "paired_effect": "claim-stop-barrier",
        }

    @property
    def transition_digest(self) -> str:
        return _digest(self.payload())


def _claim_exact_stop_barrier(
    db: Any, operation: Any, request: StopTeardownRequest
) -> dict[str, Any]:
    try:
        barrier = oj.claim_session_barrier(
            operation.session_name,
            claimed_by=operation.operation_id,
            reason=request.reason or f"{operation.current_mode} cohort Stop",
            db=db,
        )
    except oj.OperationJournalInvalid as exc:
        raise CohortJournalInvalid(str(exc)) from exc
    except oj.OperationJournalConflict as exc:
        raise CohortJournalConflict(str(exc)) from exc
    except oj.OperationJournalError as exc:
        raise CohortJournalUnavailable(str(exc)) from exc
    return _require_exact_stop_barrier(operation, barrier)


def _require_exact_stop_barrier(
    operation: Any, barrier: Optional[dict[str, Any]]
) -> dict[str, Any]:
    if barrier is None or barrier["state"] != oj.BARRIER_CLAIMED:
        raise CohortJournalConflict(
            f"session {operation.session_name} Stop barrier is not durably claimed"
        )
    if barrier["claimed_by"] != operation.operation_id:
        raise CohortJournalConflict(
            f"session {operation.session_name} Stop barrier was claimed by "
            f"{barrier['claimed_by']!r}, not cohort operation {operation.operation_id}"
        )
    return barrier


def _begin_stop_teardown_once(db: Any, request: StopTeardownRequest) -> dict[str, Any]:
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
        if operation is None:
            raise CohortJournalNotFound(
                f"cohort transition {request.transition_id} references missing operation "
                f"{request.operation_id}"
            )
        barrier = _require_exact_stop_barrier(
            operation,
            oj.get_session_barrier(operation.session_name, db=db),
        )
        return {
            "transition": _transition_dict(existing),
            "operation": _operation_dict(operation),
            "barrier": barrier,
            "adopted": True,
        }

    operation = _operation_by_id(db, request.operation_id)
    if operation is None:
        raise CohortJournalNotFound(f"unknown cohort operation: {request.operation_id}")
    if operation.operation_kind != KIND_STOP:
        raise CohortJournalConflict(
            f"cohort operation {request.operation_id} is {operation.operation_kind!r}, not Stop"
        )
    if int(operation.state_epoch or 0) != request.expected_state_epoch:
        raise CohortJournalConflict(
            f"cohort operation {request.operation_id} moved to state epoch "
            f"{operation.state_epoch}; expected {request.expected_state_epoch}"
        )

    from_state = operation.state
    from_mode = operation.current_mode
    to_mode = from_mode
    if from_mode == MODE_SAFE:
        if from_state not in {STATE_DRAINING, STATE_RECONCILIATION_REQUIRED}:
            raise CohortJournalConflict(
                f"safe Stop teardown must start from {STATE_DRAINING!r} or "
                f"{STATE_RECONCILIATION_REQUIRED!r}; got {from_state!r}"
            )
        if not request.promote_to_force and request.receipt_digest is None:
            raise CohortJournalConflict(
                "safe Stop teardown requires the exact M3-D drain receipt_digest"
            )
        if request.promote_to_force:
            to_mode = MODE_FORCE
    elif from_mode == MODE_FORCE:
        if request.promote_to_force:
            raise CohortJournalConflict("a force Stop cannot be promoted to force again")
        if from_state not in {STATE_PREPARING, STATE_RECONCILIATION_REQUIRED}:
            raise CohortJournalConflict(
                f"force Stop teardown must start from {STATE_PREPARING!r} or "
                f"{STATE_RECONCILIATION_REQUIRED!r}; got {from_state!r}"
            )
    else:  # pragma: no cover - persisted rows are validated at claim
        raise CohortJournalConflict(f"unknown cohort mode {from_mode!r}")

    boundary = _observe_boundary(db, operation.session_name)
    mismatches = {
        field: {"observed": boundary[field], "claimed": getattr(operation, field)}
        for field in (
            "lifecycle_epoch",
            "lifecycle_observation",
            "roster_revision",
            "member_snapshot_digest",
        )
        if boundary[field] != getattr(operation, field)
    }
    if mismatches:
        raise CohortJournalConflict(
            f"session {operation.session_name} moved before Stop teardown: {mismatches}"
        )

    barrier = _claim_exact_stop_barrier(db, operation, request)
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
            state=STATE_TEARING_DOWN,
            current_mode=to_mode,
            state_epoch=request.expected_state_epoch + 1,
            updated_at=stamp,
        )
    )
    if result.rowcount != 1:
        raise CohortJournalConflict(
            f"cohort operation {request.operation_id} moved concurrently; retry by reading "
            "the durable operation and Stop barrier"
        )
    transition = database.SessionCohortTransitionModel(
        transition_id=request.transition_id,
        operation_id=request.operation_id,
        transition_digest=request.transition_digest,
        transition_json=_canonical_json(request.payload()),
        from_state=from_state,
        to_state=STATE_TEARING_DOWN,
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
        "barrier": barrier,
        "adopted": False,
    }


def begin_stop_teardown(request: StopTeardownRequest, db: Any = None) -> dict[str, Any]:
    """Atomically enter Stop teardown and claim the exact M3-B barrier.

    This is a short database transaction. It performs no provider, tmux,
    wait-runner, inbox, lifecycle-terminal, or conductor effect.
    """
    if not isinstance(request, StopTeardownRequest):
        raise CohortJournalInvalid(
            f"request must be a StopTeardownRequest; got {type(request).__name__}"
        )
    from cli_agent_orchestrator.services.callback_recovery import (
        session_lifecycle_write_claim,
    )

    def _run(session: Any) -> dict[str, Any]:
        operation = _operation_by_id(session, request.operation_id)
        if operation is None:
            raise CohortJournalNotFound(f"unknown cohort operation: {request.operation_id}")
        with session_lifecycle_write_claim(operation.session_name):
            return _begin_stop_teardown_once(session, request)

    def _run_with_fresh_locked_snapshot(session: Any) -> dict[str, Any]:
        # Resolve the immutable lock key, then end the implicit db=None read
        # transaction before waiting on the cross-process claim.  Otherwise a
        # waiter can carry a pre-winner SQLite snapshot through the wait and
        # miss the transition/barrier that just committed.  The fresh read
        # under the claim adopts or refuses that durable winner truthfully.
        operation = _operation_by_id(session, request.operation_id)
        if operation is None:
            raise CohortJournalNotFound(f"unknown cohort operation: {request.operation_id}")
        session_name = operation.session_name
        session.rollback()
        with session_lifecycle_write_claim(session_name):
            return _begin_stop_teardown_once(session, request)

    if db is not None:
        try:
            _anchor_caller_transaction(db)
            with db.begin_nested():
                return _run(db)
        except CohortJournalError:
            raise
        except (IntegrityError, OperationalError) as exc:
            raise CohortJournalUnavailable(
                f"concurrent Stop-barrier transition refused; roll back and retry: {exc}"
            ) from exc

    last_error: Optional[BaseException] = None
    for _attempt in range(5):
        try:
            with database.SessionLocal() as session:
                result = _run_with_fresh_locked_snapshot(session)
                session.commit()
                return result
        except CohortJournalUnavailable as exc:
            last_error = exc
            time.sleep(0.05)
        except CohortJournalError:
            raise
        except (IntegrityError, OperationalError) as exc:
            last_error = exc
            time.sleep(0.05)
    raise CohortJournalUnavailable(
        f"concurrent Stop-barrier transitions kept conflicting; refusing after retry: "
        f"{last_error}"
    )


@dataclass(frozen=True)
class ResumeRestoreRequest:
    """Reopen one stopped campaign: release its barrier and declare its target.

    This is Resume's linearization point and the mirror image of
    ``begin_stop_teardown``.  Stop writes ``stopped`` *before* collecting a
    pane so a failure can never leave a collected fleet declared live; Resume
    writes the target and reopens the barrier *before* creating a pane for the
    same reason in reverse — a restore that dies half way leaves a session
    declared live with missing panes, which ``session_lifecycle.divergence``
    surfaces, rather than a fleet quietly running under a ``stopped`` row that
    refuses every effect it needs.
    """

    transition_id: str
    operation_id: str
    expected_state_epoch: int
    actor: str
    reason: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "transition_id", _require_uuid(self.transition_id, field="transition_id")
        )
        object.__setattr__(
            self, "operation_id", _require_uuid(self.operation_id, field="operation_id")
        )
        _non_negative_int(self.expected_state_epoch, field="expected_state_epoch")
        object.__setattr__(self, "actor", _require_text(self.actor, field="actor"))
        object.__setattr__(self, "reason", _optional_text(self.reason, field="reason"))

    def payload(self) -> dict[str, Any]:
        return {
            "transition_id": self.transition_id,
            "operation_id": self.operation_id,
            "expected_state_epoch": self.expected_state_epoch,
            "to_state": STATE_RESTORING,
            "actor": self.actor,
            "reason": self.reason,
            "receipt_digest": None,
            "promote_to_force": False,
            "paired_effect": "release-stop-barrier-and-declare-resume-target",
        }

    @property
    def transition_digest(self) -> str:
        return _digest(self.payload())


def _resume_lifecycle_row(db: Any, operation: Any) -> Any:
    row = (
        db.query(database.SessionLifecycleModel)
        .filter(database.SessionLifecycleModel.session_name == operation.session_name)
        .one_or_none()
    )
    if row is None:
        raise CohortJournalConflict(
            f"session {operation.session_name} has no declared lifecycle row; a stopped "
            "session always has one, so this Resume has lost its boundary"
        )
    return row


def _begin_resume_restore_once(db: Any, request: ResumeRestoreRequest) -> dict[str, Any]:
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
        if operation is None:
            raise CohortJournalNotFound(
                f"cohort transition {request.transition_id} references missing operation "
                f"{request.operation_id}"
            )
        return {
            "transition": _transition_dict(existing),
            "operation": _operation_dict(operation),
            "lifecycle": _lifecycle_row_dict(_resume_lifecycle_row(db, operation)),
            "barrier": oj.get_session_barrier(operation.session_name, db=db),
            "adopted": True,
        }

    operation = _operation_by_id(db, request.operation_id)
    if operation is None:
        raise CohortJournalNotFound(f"unknown cohort operation: {request.operation_id}")
    if operation.operation_kind != KIND_RESUME:
        raise CohortJournalConflict(
            f"cohort operation {request.operation_id} is {operation.operation_kind!r}, not Resume"
        )
    if operation.initiator_kind != INITIATOR_OPERATOR:
        raise CohortJournalConflict("only an operator may reopen a stopped campaign")
    if int(operation.state_epoch or 0) != request.expected_state_epoch:
        raise CohortJournalConflict(
            f"cohort operation {request.operation_id} moved to state epoch "
            f"{operation.state_epoch}; expected {request.expected_state_epoch}"
        )
    if operation.state != STATE_PREPARING:
        raise CohortJournalConflict(
            f"Resume may release the Stop barrier only once, from {STATE_PREPARING!r}; got "
            f"{operation.state!r}. A retry re-enters {STATE_RESTORING!r} through the receipted "
            "transition path."
        )

    boundary = _observe_boundary(db, operation.session_name, operation.source_operation_id)
    mismatches = {
        field: {"observed": boundary[field], "claimed": getattr(operation, field)}
        for field in (
            "lifecycle_epoch",
            "lifecycle_observation",
            "roster_revision",
            "member_snapshot_digest",
        )
        if boundary[field] != getattr(operation, field)
    }
    if mismatches:
        raise CohortJournalConflict(
            f"session {operation.session_name} moved before Resume restore: {mismatches}"
        )

    source = _operation_by_id(db, str(operation.source_operation_id))
    if source is None or source.operation_kind != KIND_STOP or source.state != STATE_STOPPED:
        raise CohortJournalConflict(
            f"Resume source {operation.source_operation_id} is no longer a terminally stopped "
            "cohort for this session"
        )

    # Release *then* write, both inside this one transaction. The barrier is
    # what refuses provider/input effects while stopped, so it must be open
    # before anything can restore a member; the lifecycle CAS immediately
    # after it means no reader ever observes an open barrier on a stopped row.
    try:
        barrier = oj.release_session_barrier(
            operation.session_name,
            claimed_by=str(operation.source_operation_id),
            reason=request.reason or f"operator Resume {operation.operation_id}",
            db=db,
        )
    except oj.OperationJournalNotFound as exc:
        raise CohortJournalNotFound(str(exc)) from exc
    except oj.OperationJournalConflict as exc:
        raise CohortJournalConflict(str(exc)) from exc
    except oj.OperationJournalError as exc:
        raise CohortJournalUnavailable(str(exc)) from exc

    row = _resume_lifecycle_row(db, operation)
    stamp = _now()
    result = db.execute(
        sa_update(database.SessionLifecycleModel)
        .where(
            database.SessionLifecycleModel.session_name == operation.session_name,
            database.SessionLifecycleModel.epoch == int(operation.lifecycle_epoch),
            database.SessionLifecycleModel.lifecycle == sl.STOPPED,
        )
        .values(
            lifecycle=operation.resume_target,
            declared_by=request.actor,
            note=request.reason,
            pause_requested_at=None,
            pause_deadline_at=None,
            epoch=int(operation.lifecycle_epoch) + 1,
            updated_at=stamp,
        )
    )
    if result.rowcount != 1:
        raise CohortJournalConflict(
            f"session {operation.session_name} lifecycle moved concurrently; the Resume "
            "barrier release was rolled back with it"
        )
    # ``restore_to`` is deliberately preserved rather than cleared: it is the
    # durable record of what this session was doing when it was stopped, and a
    # Resume-paused that later needs a start still needs to read it.
    updated = db.execute(
        sa_update(database.SessionCohortOperationModel)
        .where(
            database.SessionCohortOperationModel.operation_id == request.operation_id,
            database.SessionCohortOperationModel.state_epoch == request.expected_state_epoch,
            database.SessionCohortOperationModel.state == STATE_PREPARING,
        )
        .values(
            state=STATE_RESTORING,
            state_epoch=request.expected_state_epoch + 1,
            updated_at=stamp,
        )
    )
    if updated.rowcount != 1:
        raise CohortJournalConflict(
            f"cohort operation {request.operation_id} moved concurrently; the Resume lifecycle "
            "write and barrier release were rolled back"
        )
    transition = database.SessionCohortTransitionModel(
        transition_id=request.transition_id,
        operation_id=request.operation_id,
        transition_digest=request.transition_digest,
        transition_json=_canonical_json(request.payload()),
        from_state=STATE_PREPARING,
        to_state=STATE_RESTORING,
        from_mode=operation.current_mode,
        to_mode=operation.current_mode,
        from_state_epoch=request.expected_state_epoch,
        actor=request.actor,
        reason=request.reason,
        receipt_digest=None,
        created_at=stamp,
    )
    db.add(transition)
    db.flush()
    db.refresh(operation)
    db.refresh(row)
    return {
        "transition": _transition_dict(transition),
        "operation": _operation_dict(operation),
        "lifecycle": _lifecycle_row_dict(row),
        "barrier": barrier,
        "adopted": False,
    }


def begin_resume_restore(request: ResumeRestoreRequest, db: Any = None) -> dict[str, Any]:
    """Atomically release the Stop barrier and declare the Resume target.

    A short database transaction. It creates no pane, launches no provider,
    sends no input, and wakes no supervisor: it only makes those acts legal
    again for this session.
    """
    if not isinstance(request, ResumeRestoreRequest):
        raise CohortJournalInvalid(
            f"request must be a ResumeRestoreRequest; got {type(request).__name__}"
        )
    from cli_agent_orchestrator.services.callback_recovery import (
        session_lifecycle_write_claim,
    )

    def _run_with_fresh_locked_snapshot(session: Any) -> dict[str, Any]:
        operation = _operation_by_id(session, request.operation_id)
        if operation is None:
            raise CohortJournalNotFound(f"unknown cohort operation: {request.operation_id}")
        session_name = operation.session_name
        session.rollback()
        with session_lifecycle_write_claim(session_name):
            return _begin_resume_restore_once(session, request)

    if db is not None:
        try:
            _anchor_caller_transaction(db)
            with db.begin_nested():
                operation = _operation_by_id(db, request.operation_id)
                if operation is None:
                    raise CohortJournalNotFound(f"unknown cohort operation: {request.operation_id}")
                with session_lifecycle_write_claim(operation.session_name):
                    return _begin_resume_restore_once(db, request)
        except CohortJournalError:
            raise
        except (IntegrityError, OperationalError) as exc:
            raise CohortJournalUnavailable(
                f"concurrent Resume restore refused; roll back and retry: {exc}"
            ) from exc

    last_error: Optional[BaseException] = None
    for _attempt in range(5):
        try:
            with database.SessionLocal() as session:
                result = _run_with_fresh_locked_snapshot(session)
                session.commit()
                return result
        except CohortJournalUnavailable as exc:
            last_error = exc
            time.sleep(0.05)
        except CohortJournalError:
            raise
        except (IntegrityError, OperationalError) as exc:
            last_error = exc
            time.sleep(0.05)
    raise CohortJournalUnavailable(
        f"concurrent Resume restores kept conflicting; refusing after retry: {last_error}"
    )


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
    if operation.state in {STATE_PAUSED, STATE_STOPPED, STATE_SETTLED}:
        raise CohortJournalConflict(
            f"cohort operation {request.operation_id} is terminal in state "
            f"{operation.state!r}; its member evidence is immutable"
        )
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
            sa_exists().where(
                database.SessionCohortOperationModel.operation_id == request.operation_id,
                database.SessionCohortOperationModel.state == operation.state,
                database.SessionCohortOperationModel.state_epoch == int(operation.state_epoch or 0),
            ),
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
            _anchor_caller_transaction(db)
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


@dataclass(frozen=True)
class TerminalCommitRequest:
    """Commit one cohort and its declared session lifecycle atomically."""

    transition_id: str
    operation_id: str
    expected_state_epoch: int
    actor: str
    receipt_digest: str
    reason: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "transition_id", _require_uuid(self.transition_id, field="transition_id")
        )
        object.__setattr__(
            self, "operation_id", _require_uuid(self.operation_id, field="operation_id")
        )
        _non_negative_int(self.expected_state_epoch, field="expected_state_epoch")
        object.__setattr__(self, "actor", _require_text(self.actor, field="actor"))
        object.__setattr__(
            self,
            "receipt_digest",
            _require_digest(self.receipt_digest, field="receipt_digest"),
        )
        object.__setattr__(self, "reason", _optional_text(self.reason, field="reason"))

    def payload(self, *, to_state: str) -> dict[str, Any]:
        return {
            "transition_id": self.transition_id,
            "operation_id": self.operation_id,
            "expected_state_epoch": self.expected_state_epoch,
            "to_state": to_state,
            "actor": self.actor,
            "reason": self.reason,
            "receipt_digest": self.receipt_digest,
            "paired_effect": "commit-session-lifecycle",
        }

    def transition_digest(self, *, to_state: str) -> str:
        return _digest(self.payload(to_state=to_state))


def _lifecycle_row_dict(row: Any) -> dict[str, Any]:
    return {
        "session_name": row.session_name,
        "lifecycle": row.lifecycle,
        "restore_to": row.restore_to,
        "archived": bool(row.archived),
        "kind": row.kind,
        "declared_by": row.declared_by,
        "note": row.note,
        "pause_requested_at": row.pause_requested_at,
        "pause_deadline_at": row.pause_deadline_at,
        "epoch": int(row.epoch or 0),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "declared": True,
    }


def _terminal_member_refusals(db: Any, operation: Any) -> list[dict[str, str]]:
    members = (
        db.query(database.SessionCohortMemberModel)
        .filter(database.SessionCohortMemberModel.operation_id == operation.operation_id)
        .order_by(database.SessionCohortMemberModel.agent_id)
        .all()
    )
    if operation.operation_kind == KIND_PAUSE:
        allowed = PAUSE_TERMINAL_MEMBER_STATES[operation.current_mode]
    elif operation.operation_kind == KIND_RESUME:
        allowed = RESUME_TERMINAL_MEMBER_STATES
    else:
        allowed = STOP_TERMINAL_MEMBER_STATES
    refusals: list[dict[str, str]] = []
    for member in members:
        if not bool(member.included):
            if member.final_state != FINAL_EXCLUDED_HISTORICAL:
                refusals.append({"agent_id": member.agent_id, "final_state": member.final_state})
            continue
        if member.final_state not in allowed:
            refusals.append({"agent_id": member.agent_id, "final_state": member.final_state})
    return refusals


def _verify_resume_lifecycle(db: Any, operation: Any) -> Any:
    """Resume's terminal commit reads the lifecycle; it never rewrites it.

    ``begin_resume_restore`` already declared the target before any pane was
    created. Settling only asserts that nothing moved the row since — a
    concurrent Stop or an operator declaration means this Resume no longer
    describes the session it is about to call settled.
    """
    row = _resume_lifecycle_row(db, operation)
    expected_epoch = int(operation.lifecycle_epoch) + 1
    if row.lifecycle != operation.resume_target or int(row.epoch or 0) != expected_epoch:
        raise CohortJournalConflict(
            f"session {operation.session_name} lifecycle is {row.lifecycle!r} epoch "
            f"{int(row.epoch or 0)}; this Resume restored {operation.resume_target!r} epoch "
            f"{expected_epoch}"
        )
    return row


def _commit_session_lifecycle(db: Any, operation: Any, request: TerminalCommitRequest) -> Any:
    row = (
        db.query(database.SessionLifecycleModel)
        .filter(database.SessionLifecycleModel.session_name == operation.session_name)
        .one_or_none()
    )
    observed_lifecycle = row.lifecycle if row is not None else sl.WORKING
    observed_epoch = int(row.epoch or 0) if row is not None else 0
    if observed_lifecycle != operation.lifecycle_observation or observed_epoch != int(
        operation.lifecycle_epoch
    ):
        raise CohortJournalConflict(
            f"session {operation.session_name} lifecycle moved to "
            f"{observed_lifecycle!r} epoch {observed_epoch}; cohort claimed "
            f"{operation.lifecycle_observation!r} epoch {operation.lifecycle_epoch}"
        )

    target = STATE_PAUSED if operation.operation_kind == KIND_PAUSE else STATE_STOPPED
    if observed_lifecycle == sl.STOPPED:
        raise CohortJournalConflict(
            f"session {operation.session_name} is already stopped outside this terminal commit"
        )
    if target == STATE_PAUSED and observed_lifecycle in {sl.PAUSED, sl.COMPLETE}:
        raise CohortJournalConflict(
            f"session {operation.session_name} is {observed_lifecycle!r}; a new cohort Pause "
            "cannot terminal-commit it"
        )

    stamp = _now()
    if row is None:
        restore_to = sl.WORKING if target == STATE_STOPPED else None
        row = database.SessionLifecycleModel(
            session_name=operation.session_name,
            lifecycle=target,
            restore_to=restore_to,
            archived=0,
            kind=sl.CAMPAIGN,
            declared_by=request.actor,
            note=request.reason,
            pause_requested_at=None,
            pause_deadline_at=None,
            epoch=1,
            created_at=stamp,
            updated_at=stamp,
        )
        db.add(row)
        db.flush()
        return row

    values: dict[str, Any] = {
        "lifecycle": target,
        "declared_by": request.actor,
        "note": request.reason,
        "pause_requested_at": None,
        "pause_deadline_at": None,
        "epoch": observed_epoch + 1,
        "updated_at": stamp,
    }
    if target == STATE_STOPPED:
        values["restore_to"] = (
            observed_lifecycle if observed_lifecycle in sl.RESTORE_TARGETS else sl.WORKING
        )
    result = db.execute(
        sa_update(database.SessionLifecycleModel)
        .where(
            database.SessionLifecycleModel.session_name == operation.session_name,
            database.SessionLifecycleModel.epoch == observed_epoch,
            database.SessionLifecycleModel.lifecycle == observed_lifecycle,
        )
        .values(**values)
    )
    if result.rowcount != 1:
        raise CohortJournalConflict(
            f"session {operation.session_name} lifecycle moved concurrently; read and retry"
        )
    db.flush()
    db.refresh(row)
    return row


def _terminal_state_for(operation_kind: str) -> str:
    if operation_kind == KIND_PAUSE:
        return STATE_PAUSED
    if operation_kind == KIND_RESUME:
        return STATE_SETTLED
    return STATE_STOPPED


def _commit_terminal_once(db: Any, request: TerminalCommitRequest) -> dict[str, Any]:
    operation = _operation_by_id(db, request.operation_id)
    if operation is None:
        raise CohortJournalNotFound(f"unknown cohort operation: {request.operation_id}")
    to_state = _terminal_state_for(operation.operation_kind)
    transition_digest = request.transition_digest(to_state=to_state)
    existing = (
        db.query(database.SessionCohortTransitionModel)
        .filter(database.SessionCohortTransitionModel.transition_id == request.transition_id)
        .one_or_none()
    )
    if existing is not None:
        if existing.transition_digest != transition_digest:
            raise CohortJournalConflict(
                f"cohort transition {request.transition_id} already exists with different "
                "immutable request content"
            )
        lifecycle = (
            db.query(database.SessionLifecycleModel)
            .filter(database.SessionLifecycleModel.session_name == operation.session_name)
            .one()
        )
        return {
            "transition": _transition_dict(existing),
            "operation": _operation_dict(operation),
            "lifecycle": _lifecycle_row_dict(lifecycle),
            "barrier": oj.get_session_barrier(operation.session_name, db=db),
            "adopted": True,
        }

    if int(operation.state_epoch or 0) != request.expected_state_epoch:
        raise CohortJournalConflict(
            f"cohort operation {request.operation_id} moved to state epoch "
            f"{operation.state_epoch}; expected {request.expected_state_epoch}"
        )
    if operation.operation_kind == KIND_PAUSE:
        required_state = (
            STATE_DRAINING if operation.current_mode == MODE_SAFE else STATE_INTERRUPTING
        )
    elif operation.operation_kind == KIND_RESUME:
        required_state = STATE_RESTORING
    else:
        required_state = STATE_TEARING_DOWN
    if operation.state != required_state:
        raise CohortJournalConflict(
            f"{operation.current_mode} {operation.operation_kind} terminal commit requires "
            f"state {required_state!r}; got {operation.state!r}"
        )

    refusals = _terminal_member_refusals(db, operation)
    if refusals:
        raise CohortJournalConflict(
            f"cohort operation {request.operation_id} has members outside the terminal "
            f"result set: {refusals}"
        )
    barrier = oj.get_session_barrier(operation.session_name, db=db)
    if operation.operation_kind == KIND_STOP:
        if (
            barrier is None
            or barrier["state"] != oj.BARRIER_CLAIMED
            or barrier["claimed_by"] != operation.operation_id
        ):
            raise CohortJournalConflict(
                f"Stop terminal commit requires the exact barrier claimed by cohort "
                f"operation {operation.operation_id}"
            )
    if operation.operation_kind == KIND_RESUME and (
        barrier is not None and barrier["state"] == oj.BARRIER_CLAIMED
    ):
        # A Stop claimed the barrier again while this Resume was restoring.
        # Settling now would declare a fleet live that a newer Stop is
        # already tearing down.
        raise CohortJournalConflict(
            f"session {operation.session_name} Stop barrier was reclaimed by "
            f"{barrier['claimed_by']!r}; this Resume cannot settle"
        )

    if operation.operation_kind == KIND_RESUME:
        lifecycle = _verify_resume_lifecycle(db, operation)
    else:
        lifecycle = _commit_session_lifecycle(db, operation, request)
    stamp = _now()
    result = db.execute(
        sa_update(database.SessionCohortOperationModel)
        .where(
            database.SessionCohortOperationModel.operation_id == request.operation_id,
            database.SessionCohortOperationModel.state_epoch == request.expected_state_epoch,
            database.SessionCohortOperationModel.state == operation.state,
            database.SessionCohortOperationModel.current_mode == operation.current_mode,
        )
        .values(
            state=to_state,
            state_epoch=request.expected_state_epoch + 1,
            updated_at=stamp,
        )
    )
    if result.rowcount != 1:
        raise CohortJournalConflict(
            f"cohort operation {request.operation_id} moved concurrently; terminal lifecycle "
            "commit was rolled back"
        )
    transition = database.SessionCohortTransitionModel(
        transition_id=request.transition_id,
        operation_id=request.operation_id,
        transition_digest=transition_digest,
        transition_json=_canonical_json(request.payload(to_state=to_state)),
        from_state=operation.state,
        to_state=to_state,
        from_mode=operation.current_mode,
        to_mode=operation.current_mode,
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
        "lifecycle": _lifecycle_row_dict(lifecycle),
        "barrier": barrier,
        "adopted": False,
    }


def commit_terminal(request: TerminalCommitRequest, db: Any = None) -> dict[str, Any]:
    """Atomically commit terminal cohort and declared lifecycle state.

    The receipt and per-member results are evidence carriers only. This
    function does not infer task completion, inspect provider output, touch a
    pane, stop a wait, wake a supervisor, or perform any other external I/O.
    """
    if not isinstance(request, TerminalCommitRequest):
        raise CohortJournalInvalid(
            f"request must be a TerminalCommitRequest; got {type(request).__name__}"
        )
    from cli_agent_orchestrator.services.callback_recovery import (
        session_lifecycle_write_claim,
    )

    def _run(session: Any) -> dict[str, Any]:
        operation = _operation_by_id(session, request.operation_id)
        if operation is None:
            raise CohortJournalNotFound(f"unknown cohort operation: {request.operation_id}")
        with session_lifecycle_write_claim(operation.session_name):
            return _commit_terminal_once(session, request)

    def _run_with_fresh_locked_snapshot(session: Any) -> dict[str, Any]:
        operation = _operation_by_id(session, request.operation_id)
        if operation is None:
            raise CohortJournalNotFound(f"unknown cohort operation: {request.operation_id}")
        session_name = operation.session_name
        session.rollback()
        with session_lifecycle_write_claim(session_name):
            return _commit_terminal_once(session, request)

    if db is not None:
        try:
            _anchor_caller_transaction(db)
            with db.begin_nested():
                return _run(db)
        except CohortJournalError:
            raise
        except (IntegrityError, OperationalError) as exc:
            raise CohortJournalUnavailable(
                f"concurrent terminal cohort commit refused; roll back and retry: {exc}"
            ) from exc

    last_error: Optional[BaseException] = None
    for _attempt in range(5):
        try:
            with database.SessionLocal() as session:
                result = _run_with_fresh_locked_snapshot(session)
                session.commit()
                return result
        except CohortJournalUnavailable as exc:
            last_error = exc
            time.sleep(0.05)
        except CohortJournalError:
            raise
        except (IntegrityError, OperationalError) as exc:
            last_error = exc
            time.sleep(0.05)
    raise CohortJournalUnavailable(
        f"concurrent terminal cohort commits kept conflicting; refusing after retry: "
        f"{last_error}"
    )


def operation_provenance(record: dict[str, Any]) -> dict[str, Any]:
    """The single derived block every read surface renders.

    Derived here rather than in the API, the CLI, and the dashboard, because
    three copies of "was this safe operation promoted to force?" would drift,
    and they would drift in the direction of a surface that shows an operator
    a *safe* Stop that actually reaped panes.

    Everything in it is already durable; nothing is inferred beyond counting.
    """
    transitions = record.get("transitions") or []
    promotion = next(
        (
            transition
            for transition in transitions
            if transition["from_mode"] == MODE_SAFE and transition["to_mode"] == MODE_FORCE
        ),
        None,
    )
    members = record.get("members") or []
    outcomes: dict[str, int] = {}
    for member in members:
        outcomes[member["final_state"]] = outcomes.get(member["final_state"], 0) + 1
    return {
        "operation_id": record["operation_id"],
        "session_name": record["session_name"],
        "operation_kind": record["operation_kind"],
        "state": record["state"],
        "state_epoch": record["state_epoch"],
        "lifecycle_epoch": record["lifecycle_epoch"],
        "lifecycle_observation": record["lifecycle_observation"],
        "roster_revision": record["roster_revision"],
        "member_snapshot_digest": record["member_snapshot_digest"],
        "requested_mode": record["requested_mode"],
        "current_mode": record["current_mode"],
        # Safe never becomes force by accident, so "was it promoted" is a fact
        # with a receipt behind it rather than a mode comparison.
        "promoted_to_force": promotion is not None,
        "promotion_receipt_digest": promotion["receipt_digest"] if promotion else None,
        "promoted_by": promotion["actor"] if promotion else None,
        "initiator_kind": record["initiator_kind"],
        "initiated_by": record["initiated_by"],
        "source_operation_id": record["source_operation_id"],
        "resume_target": record["resume_target"],
        "member_outcomes": outcomes,
        # Continuity provenance: which stable agent kept which native session
        # across the operation. Stable agent, incarnation, and native lineage
        # stay three separate identities here on purpose.
        "continuity": [
            {
                "agent_id": member["agent_id"],
                "role": member["role"],
                "included": member["included"],
                "exclusion_reason": member["exclusion_reason"],
                "lineage_id": member["lineage_id"],
                "harness": member["harness"],
                "native_session_id": member["native_session_id"],
                "incarnation_id": member["incarnation_id"],
                "terminal_id": member["terminal_id"],
                "generation": member["generation"],
                "final_state": member["final_state"],
            }
            for member in members
        ],
    }


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
        record = {
            **_operation_dict(row),
            "members": [_member_dict(member) for member in members],
            "transitions": [_transition_dict(transition) for transition in transitions],
        }
        record["provenance"] = operation_provenance(record)
        return record

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

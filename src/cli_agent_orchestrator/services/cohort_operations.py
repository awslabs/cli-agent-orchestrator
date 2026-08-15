"""The operator-facing entry points for fleet Pause, Stop, and Resume.

C1-C3 built the durable journal and the physical executors but exposed no
mutation route at all. This module is the seam that makes them reachable, and
its shape is the accepted design's rule rather than convenience:

**Safe and force are separate calls, never one call with a mode argument.**
A flag is something a retry can carry forward, a client library can default,
and a script can set once and forget. Force Pause interrupts a running turn
and force Stop reaps panes without waiting for a boundary — both are choices
an operator makes about *this* fleet at *this* moment, and both must be
unreachable by a caller who only meant to ask nicely. So ``pause_safe`` and
``pause_force`` are different functions behind different routes with different
scopes, and the journal enforces the same separation underneath: a safe
operation only becomes a force one through an explicit, receipted promotion.

Every entry point is one durable claim followed by the matching executor. The
claim is minted by the caller, so a lost response is retried with the same
operation id and adopts the winner instead of starting a second fleet
operation. Nothing here interprets task meaning, composes a supervisor
message, or infers an occurrence: M3-D owns all three.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from cli_agent_orchestrator.services import cohort_effects
from cli_agent_orchestrator.services import cohort_journal as cohort
from cli_agent_orchestrator.services import cohort_resume
from cli_agent_orchestrator.services import session_lifecycle as sl

SCHEMA_VERSION = "cao-m3-cohort-operations-v1"


class CohortOperationError(RuntimeError):
    code = "cohort-operation-error"


class CohortOperationInvalid(CohortOperationError):
    code = "cohort-operation-invalid"


class CohortOperationConflict(CohortOperationError):
    code = "cohort-operation-conflict"


@dataclass(frozen=True)
class OperatorRequest:
    """What every operator surface needs, and nothing else.

    ``operation_id`` is caller-minted on purpose. The operator's client owns
    the identity of the act, so a timeout followed by a retry is the *same*
    Pause rather than a second one, and the durable journal can say so.
    """

    session_name: str
    initiated_by: str
    operation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    reason: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.initiated_by, str) or not self.initiated_by.strip():
            raise CohortOperationInvalid("initiated_by must name the operator; it is recorded")
        # Validated here rather than left to the journal because this module
        # also derives its transition ids *from* the operation id: a
        # non-canonical value would fail as an untyped ValueError inside
        # ``_transition_ids`` before any typed refusal could be returned.
        try:
            if str(uuid.UUID(str(self.operation_id))) != self.operation_id:
                raise ValueError
        except (AttributeError, TypeError, ValueError) as exc:
            raise CohortOperationInvalid(
                f"operation_id must be a canonical lowercase UUID; got {self.operation_id!r}"
            ) from exc


def _transition_ids(operation_id: str, *labels: str) -> dict[str, str]:
    """Derive this attempt's transition ids from the operation id.

    Derived rather than random so a retried request replays the *same*
    transitions and adopts them, which is what turns "the response was lost"
    into "read the durable winner" instead of a second effect.
    """
    namespace = uuid.UUID(operation_id)
    return {label: str(uuid.uuid5(namespace, label)) for label in labels}


def _claim(
    request: OperatorRequest,
    *,
    kind: str,
    mode: str,
    resume_target: Optional[str] = None,
    source_operation_id: Optional[str] = None,
) -> dict[str, Any]:
    try:
        boundary = cohort.observe_boundary(
            request.session_name, resume_source_operation_id=source_operation_id
        )
        return cohort.claim_operation(
            cohort.OperationRequest(
                operation_id=request.operation_id,
                session_name=request.session_name,
                operation_kind=kind,
                requested_mode=mode,
                initiator_kind=cohort.INITIATOR_OPERATOR,
                initiated_by=request.initiated_by,
                lifecycle_epoch=boundary["lifecycle_epoch"],
                lifecycle_observation=boundary["lifecycle_observation"],
                roster_revision=boundary["roster_revision"],
                member_snapshot_digest=boundary["member_snapshot_digest"],
                source_operation_id=source_operation_id,
                resume_target=resume_target,
            )
        )
    except cohort.CohortJournalError as exc:
        raise _translated(exc) from exc


def _translated(exc: cohort.CohortJournalError) -> CohortOperationError:
    if isinstance(exc, cohort.CohortJournalInvalid):
        return CohortOperationInvalid(str(exc))
    return CohortOperationConflict(str(exc))


def latest_stop(session_name: str) -> Optional[dict[str, Any]]:
    """The most recent terminally stopped cohort for this session, if any.

    Resume needs a source and an operator should not have to paste a UUID to
    reopen the session they just stopped. Newest-first, and only a cohort that
    actually reached ``stopped`` — a Stop that landed in reconciliation is not
    something to resume past.
    """
    operations = cohort.list_operations(session_name)
    stops = [
        operation
        for operation in operations
        if operation["operation_kind"] == cohort.KIND_STOP
        and operation["state"] == cohort.STATE_STOPPED
    ]
    return stops[-1] if stops else None


def pause_safe(
    request: OperatorRequest,
    *,
    drain_receipt_digest: str,
    member_results: Sequence[cohort.MemberResult],
) -> dict[str, Any]:
    """Terminalize a Pause at a boundary M3-D proved the fleet reached.

    Safe Pause performs no interrupt. It consumes M3-D's opaque drain receipt
    and the member evidence M3-D classified; this path never inspects a pane
    or decides for itself that a worker is idle.
    """
    operation = _claim(request, kind=cohort.KIND_PAUSE, mode=cohort.MODE_SAFE)
    ids = _transition_ids(request.operation_id, "drain", "commit")
    if operation["state"] == cohort.STATE_PREPARING:
        try:
            operation = cohort.transition_operation(
                cohort.TransitionRequest(
                    transition_id=ids["drain"],
                    operation_id=request.operation_id,
                    expected_state_epoch=int(operation["state_epoch"]),
                    to_state=cohort.STATE_DRAINING,
                    actor=request.initiated_by,
                    reason=request.reason,
                )
            )["operation"]
        except cohort.CohortJournalError as exc:
            raise _translated(exc) from exc
    try:
        return cohort_effects.execute_safe_pause(
            cohort_effects.SafePauseRequest(
                operation_id=request.operation_id,
                commit_transition_id=ids["commit"],
                actor=request.initiated_by,
                drain_receipt_digest=drain_receipt_digest,
                member_results=tuple(member_results),
                reason=request.reason,
            )
        )
    except cohort.CohortJournalError as exc:
        raise _translated(exc) from exc
    except cohort_effects.CohortEffectsError as exc:
        raise CohortOperationConflict(str(exc)) from exc


def pause_force(request: OperatorRequest, **executor_kwargs: Any) -> dict[str, Any]:
    """Interrupt every member's current turn, workers before the supervisor.

    Distinct from ``pause_safe`` all the way down: a different claimed mode,
    a different state path, and a commit that requires positive per-member
    quiescence proof rather than a drain receipt.
    """
    operation = _claim(request, kind=cohort.KIND_PAUSE, mode=cohort.MODE_FORCE)
    ids = _transition_ids(request.operation_id, "interrupt", "commit", "reconcile")
    try:
        return cohort_effects.execute_force_pause(
            cohort_effects.ForcePauseRequest(
                operation_id=request.operation_id,
                expected_state_epoch=int(operation["state_epoch"]),
                interrupt_transition_id=ids["interrupt"],
                commit_transition_id=ids["commit"],
                reconciliation_transition_id=ids["reconcile"],
                actor=request.initiated_by,
                reason=request.reason,
            ),
            **executor_kwargs,
        )
    except cohort.CohortJournalError as exc:
        raise _translated(exc) from exc
    except cohort_effects.CohortEffectsError as exc:
        raise CohortOperationConflict(str(exc)) from exc


def _stop(
    request: OperatorRequest,
    *,
    mode: str,
    drain_receipt_digest: Optional[str],
    executor_kwargs: Mapping[str, Any],
) -> dict[str, Any]:
    operation = _claim(request, kind=cohort.KIND_STOP, mode=mode)
    ids = _transition_ids(request.operation_id, "drain", "teardown", "commit", "reconcile")
    if mode == cohort.MODE_SAFE and operation["state"] == cohort.STATE_PREPARING:
        try:
            operation = cohort.transition_operation(
                cohort.TransitionRequest(
                    transition_id=ids["drain"],
                    operation_id=request.operation_id,
                    expected_state_epoch=int(operation["state_epoch"]),
                    to_state=cohort.STATE_DRAINING,
                    actor=request.initiated_by,
                    reason=request.reason,
                )
            )["operation"]
        except cohort.CohortJournalError as exc:
            raise _translated(exc) from exc
    try:
        return cohort_effects.execute_stop(
            cohort_effects.StopEffectsRequest(
                operation_id=request.operation_id,
                expected_state_epoch=int(operation["state_epoch"]),
                teardown_transition_id=ids["teardown"],
                commit_transition_id=ids["commit"],
                reconciliation_transition_id=ids["reconcile"],
                actor=request.initiated_by,
                drain_receipt_digest=drain_receipt_digest,
                reason=request.reason,
            ),
            **executor_kwargs,
        )
    except cohort.CohortJournalError as exc:
        raise _translated(exc) from exc
    except cohort_effects.CohortEffectsError as exc:
        raise CohortOperationConflict(str(exc)) from exc


def stop_safe(
    request: OperatorRequest, *, drain_receipt_digest: str, **executor_kwargs: Any
) -> dict[str, Any]:
    """Stop only once M3-D has proved the fleet drained to a boundary."""
    if not drain_receipt_digest:
        raise CohortOperationInvalid(
            "safe Stop requires M3-D's opaque drain receipt; a Stop with no proof the fleet "
            "reached a boundary is a force Stop and must be asked for as one"
        )
    return _stop(
        request,
        mode=cohort.MODE_SAFE,
        drain_receipt_digest=drain_receipt_digest,
        executor_kwargs=executor_kwargs,
    )


def stop_force(request: OperatorRequest, **executor_kwargs: Any) -> dict[str, Any]:
    """Reap the captured cohort now, without waiting for any provider I/O."""
    return _stop(
        request, mode=cohort.MODE_FORCE, drain_receipt_digest=None, executor_kwargs=executor_kwargs
    )


def _adopted_resume(request: OperatorRequest) -> Optional[dict[str, Any]]:
    """The durable Resume this exact operation id already created, if any.

    Checked before any precondition, because the preconditions describe a
    session that is *about* to be resumed and a replay arrives after it
    already was. Re-deriving them on a retry would answer "this session is
    working, not stopped" — technically true, and precisely the wrong answer
    to "did my Resume land?". The operation id is the question being asked.
    """
    try:
        operation = cohort.get_operation(request.operation_id)
    except cohort.CohortJournalNotFound:
        return None
    except cohort.CohortJournalError as exc:
        raise _translated(exc) from exc
    if operation["operation_kind"] != cohort.KIND_RESUME:
        raise CohortOperationConflict(
            f"operation {request.operation_id} is already a {operation['operation_kind']} for "
            f"session {operation['session_name']}; mint a new id for a Resume"
        )
    if operation["session_name"] != sl.normalise_session_name(request.session_name):
        raise CohortOperationConflict(
            f"operation {request.operation_id} belongs to session "
            f"{operation['session_name']!r}, not {request.session_name!r}"
        )
    return operation


def _resume_claim(request: OperatorRequest, *, target: str) -> dict[str, Any]:
    source = latest_stop(request.session_name)
    if source is None:
        raise CohortOperationConflict(
            f"session {request.session_name} has no terminally stopped cohort to resume; only a "
            "Stop this fork recorded can be reopened"
        )
    return _claim(
        request,
        kind=cohort.KIND_RESUME,
        mode=cohort.MODE_SAFE,
        resume_target=target,
        source_operation_id=source["operation_id"],
    )


def _resume_request(request: OperatorRequest, operation: Mapping[str, Any]):
    ids = _transition_ids(request.operation_id, "restore", "commit", "reconcile")
    return cohort_resume.ResumeRequest(
        operation_id=request.operation_id,
        expected_state_epoch=int(operation["state_epoch"]),
        restore_transition_id=ids["restore"],
        commit_transition_id=ids["commit"],
        reconciliation_transition_id=ids["reconcile"],
        actor=request.initiated_by,
        reason=request.reason,
    )


def _resume_target(request: OperatorRequest) -> str:
    """The lifecycle the Stop recorded — never a new one chosen here.

    Resume returns a session to what it was doing. Choosing a different
    destination would be setting a goal, and goals are not M3-C's to set.
    """
    record = sl.describe(request.session_name)
    if record.get("lifecycle") != sl.STOPPED:
        raise CohortOperationConflict(
            f"session {request.session_name} is {record.get('lifecycle')!r}, not stopped"
        )
    target = record.get("restore_to")
    if target == sl.PAUSED:
        raise CohortOperationConflict(
            f"session {request.session_name} was paused when it was stopped; resume it paused "
            "and let an operator start it deliberately"
        )
    if target not in sl.RESTORE_TARGETS:
        raise CohortOperationConflict(
            f"session {request.session_name} recorded no usable restore target "
            f"({target!r}); resume it paused instead"
        )
    return str(target)


async def resume_paused(request: OperatorRequest, **executor_kwargs: Any) -> dict[str, Any]:
    """Bring the stopped fleet's panes back and leave it paused. Zero input."""
    operation = _adopted_resume(request) or _resume_claim(request, target=sl.PAUSED)
    try:
        return await cohort_resume.execute_resume_paused(
            _resume_request(request, operation), **executor_kwargs
        )
    except cohort.CohortJournalError as exc:
        raise _translated(exc) from exc
    except cohort_resume.CohortResumeError as exc:
        raise CohortOperationConflict(str(exc)) from exc


async def resume_and_start(request: OperatorRequest, **executor_kwargs: Any) -> dict[str, Any]:
    """Restore the stopped fleet and wake its supervisor exactly once."""
    operation = _adopted_resume(request)
    if operation is None:
        operation = _resume_claim(request, target=_resume_target(request))
    try:
        return await cohort_resume.execute_resume_and_start(
            _resume_request(request, operation), **executor_kwargs
        )
    except cohort.CohortJournalError as exc:
        raise _translated(exc) from exc
    except cohort_resume.CohortResumeError as exc:
        raise CohortOperationConflict(str(exc)) from exc

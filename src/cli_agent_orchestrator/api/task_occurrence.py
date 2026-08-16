"""HTTP surface for task occurrences, safe drains, and supervisor wakes (M3-D).

Kept out of ``api/session_lifecycle.py`` for the reason that module is itself
kept out of ``api/main.py``: a subsystem nobody can read alongside its
neighbours is one nobody will extend.

**Every read route here is tmux-free.** An occurrence outlives the pane that
executed it — that is the whole point of the occurrence id not being a
generation — so a route that failed closed on ``session_exists`` would make
the record of a finished round unreadable exactly when somebody needs it.

The write routes are narrow on purpose. There is one that steers panes (the
safe drain), one that spends a drain receipt (safe Pause), and one that wakes
a supervisor whose fleet was started out of ``paused``. Everything else here
reads.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from cli_agent_orchestrator.security.auth import (
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
    require_any_scope,
)
from cli_agent_orchestrator.services import cohort_journal as cohort
from cli_agent_orchestrator.services import cohort_operations, supervisor_drain
from cli_agent_orchestrator.services import supervisor_reconciliation as reconciliation
from cli_agent_orchestrator.services import task_occurrence as occurrence

router = APIRouter(tags=["task-occurrence"])

_READ = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN))
_WRITE = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN))
_ADMIN = Depends(require_any_scope(SCOPE_ADMIN))

_STATUS_FOR_CODE = {
    "task-occurrence-invalid": status.HTTP_400_BAD_REQUEST,
    "task-occurrence-conflict": status.HTTP_409_CONFLICT,
    "task-occurrence-not-found": status.HTTP_404_NOT_FOUND,
    "task-occurrence-unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "supervisor-drain-invalid": status.HTTP_400_BAD_REQUEST,
    "supervisor-drain-conflict": status.HTTP_409_CONFLICT,
    "supervisor-drain-not-found": status.HTTP_404_NOT_FOUND,
    "supervisor-drain-unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "supervisor-reconciliation-invalid": status.HTTP_400_BAD_REQUEST,
    "supervisor-reconciliation-conflict": status.HTTP_409_CONFLICT,
    "supervisor-reconciliation-not-found": status.HTTP_404_NOT_FOUND,
    "supervisor-reconciliation-unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "cohort-journal-invalid": status.HTTP_400_BAD_REQUEST,
    "cohort-journal-conflict": status.HTTP_409_CONFLICT,
    "cohort-journal-not-found": status.HTTP_404_NOT_FOUND,
    "cohort-journal-unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "cohort-operation-invalid": status.HTTP_400_BAD_REQUEST,
    "cohort-operation-conflict": status.HTTP_409_CONFLICT,
    "cohort-operation-error": status.HTTP_409_CONFLICT,
}

_ERRORS = (
    occurrence.TaskOccurrenceError,
    supervisor_drain.SupervisorDrainError,
    reconciliation.SupervisorReconciliationError,
    cohort.CohortJournalError,
    cohort_operations.CohortOperationError,
)


def _http(exc: Any) -> HTTPException:
    # First line only: a store failure wraps the driver's exception, which
    # SQLAlchemy renders as a sentence followed by the whole generated query.
    return HTTPException(
        status_code=_STATUS_FOR_CODE.get(getattr(exc, "code", ""), status.HTTP_400_BAD_REQUEST),
        detail=str(exc).splitlines()[0],
    )


class StrictBody(BaseModel):
    """Reject unknown fields instead of ignoring them.

    Load-bearing here: a dropped ``seed_quality`` would be a fresh worker
    started on context nobody characterised, and a dropped
    ``expected_revision`` would turn a compare-and-swap into last-write-wins.
    """

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# occurrences
# ---------------------------------------------------------------------------


class ArtifactBody(StrictBody):
    artifact_id: str = Field(min_length=1, max_length=512)
    kind: str = Field(min_length=1, max_length=512)
    reference: str = Field(min_length=1, max_length=4096)
    content_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    bytes_len: Optional[int] = Field(default=None, ge=0)


class SeedBody(StrictBody):
    """The seed a fresh successor would inherit, and how complete it is.

    ``quality`` has no default. A default would be chosen once, in a client,
    and then every seed would claim it — including the truncated ones.
    """

    quality: str = Field(pattern=r"^(complete|truncated|empty)$")
    summary_digest: Optional[str] = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    artifacts: List[ArtifactBody] = Field(default_factory=list)
    produced_by: Optional[str] = Field(default=None, max_length=512)
    note: Optional[str] = Field(default=None, max_length=2000)


class OpenOccurrenceBody(StrictBody):
    task_occurrence_id: str = Field(min_length=36, max_length=36)
    agent_id: str = Field(min_length=36, max_length=36)
    round_index: int = Field(ge=0, le=occurrence.MAX_ROUND_INDEX)
    dispatch_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    incarnation_id: str = Field(min_length=1, max_length=512)
    terminal_id: str = Field(min_length=1, max_length=512)
    generation: Optional[str] = Field(default=None, max_length=512)
    lineage_id: Optional[str] = Field(default=None, max_length=512)
    native_session_id: Optional[str] = Field(default=None, max_length=512)
    seed: Optional[SeedBody] = None


class BoundaryBody(StrictBody):
    expected_revision: int = Field(ge=0)
    recorded_by: str = Field(min_length=1, max_length=200)
    report_digest: Optional[str] = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    checkpoint_digest: Optional[str] = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    seed: Optional[SeedBody] = None
    note: Optional[str] = Field(default=None, max_length=2000)


class FinalizeBody(StrictBody):
    expected_revision: int = Field(ge=0)
    disposition: str = Field(pattern=r"^(reported|abandoned|superseded|lost)$")
    finalized_by: str = Field(min_length=1, max_length=200)
    note: Optional[str] = Field(default=None, max_length=2000)


class ExtensionBody(StrictBody):
    """An opaque versioned extension. The payload is never parsed here."""

    extension_id: str = Field(min_length=1, max_length=128)
    extension_kind: str = Field(min_length=1, max_length=128)
    extension_version: str = Field(min_length=1, max_length=64)
    decider: str = Field(min_length=1, max_length=512)
    payload: Dict[str, Any] = Field(default_factory=dict)
    claims_final: bool = False


class RouteExtensionBody(StrictBody):
    routed_by: str = Field(min_length=1, max_length=200)


def _seed(body: Optional[SeedBody]) -> Optional[occurrence.TaskSeed]:
    if body is None:
        return None
    return occurrence.seed_from_artifacts(
        quality=body.quality,
        summary_digest=body.summary_digest,
        artifacts=[artifact.model_dump() for artifact in body.artifacts],
        produced_by=body.produced_by,
        note=body.note,
    )


@router.get("/task-occurrences/{task_occurrence_id}")
async def get_task_occurrence(
    task_occurrence_id: str,
    _scopes: List[str] = _READ,
) -> Dict[str, Any]:
    """One occurrence, its extensions, and the derived seed verdict."""
    try:
        return await asyncio.to_thread(occurrence.get_occurrence, task_occurrence_id)
    except _ERRORS as exc:
        raise _http(exc)


@router.get("/sessions/{session_name}/task-occurrences")
async def list_task_occurrences(
    session_name: str,
    agent_id: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None, pattern=r"^(open|finalized)$"),
    _scopes: List[str] = _READ,
) -> Dict[str, Any]:
    """Occurrence projections for one session. Never consults tmux."""
    try:
        records = await asyncio.to_thread(
            occurrence.list_occurrences, session_name, agent_id=agent_id, state=state
        )
    except _ERRORS as exc:
        raise _http(exc)
    return {"occurrences": records, "count": len(records)}


@router.get("/sessions/{session_name}/agents/{agent_id}/task-occurrences")
async def get_agent_occurrence_history(
    session_name: str,
    agent_id: str,
    _scopes: List[str] = _READ,
) -> Dict[str, Any]:
    """This agent's live round and its finalized history, kept separate.

    Two keys rather than one list, because a caller that has to filter a
    mixed list will eventually forget to, and the failure mode is a finished
    round being treated as the live one.
    """
    try:
        return await asyncio.to_thread(occurrence.occurrence_history, session_name, agent_id)
    except _ERRORS as exc:
        raise _http(exc)


@router.post("/sessions/{session_name}/task-occurrences")
async def open_task_occurrence(
    session_name: str,
    body: OpenOccurrenceBody,
    _scopes: List[str] = _WRITE,
) -> Dict[str, Any]:
    """Open one task/round occurrence. Dispatches nothing.

    Refuses to reopen a finalized occurrence, and refuses a second open
    occurrence for a stable agent that already has one — the two ways a
    reused agent silently re-runs finished work.
    """
    try:
        return await asyncio.to_thread(
            occurrence.open_occurrence,
            occurrence.OpenRequest(
                task_occurrence_id=body.task_occurrence_id,
                session_name=session_name,
                agent_id=body.agent_id,
                round_index=body.round_index,
                dispatch_digest=body.dispatch_digest,
                incarnation=occurrence.EffectIncarnation(
                    incarnation_id=body.incarnation_id,
                    terminal_id=body.terminal_id,
                    generation=body.generation,
                    lineage_id=body.lineage_id,
                    native_session_id=body.native_session_id,
                ),
                seed=_seed(body.seed) or occurrence.EMPTY_SEED,
            ),
        )
    except _ERRORS as exc:
        raise _http(exc)


@router.post("/task-occurrences/{task_occurrence_id}/boundary")
async def record_task_boundary(
    task_occurrence_id: str,
    body: BoundaryBody,
    _scopes: List[str] = _WRITE,
) -> Dict[str, Any]:
    """Record one boundary observation onto an open occurrence."""
    try:
        return await asyncio.to_thread(
            occurrence.record_boundary,
            occurrence.BoundaryRecord(
                task_occurrence_id=task_occurrence_id,
                expected_revision=body.expected_revision,
                recorded_by=body.recorded_by,
                report_digest=body.report_digest,
                checkpoint_digest=body.checkpoint_digest,
                seed=_seed(body.seed),
                note=body.note,
            ),
        )
    except _ERRORS as exc:
        raise _http(exc)


@router.post("/task-occurrences/{task_occurrence_id}/finalize")
async def finalize_task_occurrence(
    task_occurrence_id: str,
    body: FinalizeBody,
    _scopes: List[str] = _WRITE,
) -> Dict[str, Any]:
    """Close one occurrence, preserving its current evidence write-once."""
    try:
        return await asyncio.to_thread(
            occurrence.finalize_occurrence,
            occurrence.FinalizeRequest(
                task_occurrence_id=task_occurrence_id,
                expected_revision=body.expected_revision,
                disposition=body.disposition,
                finalized_by=body.finalized_by,
                note=body.note,
            ),
        )
    except _ERRORS as exc:
        raise _http(exc)


@router.post("/task-occurrences/{task_occurrence_id}/extensions")
async def attach_task_extension(
    task_occurrence_id: str,
    body: ExtensionBody,
    _scopes: List[str] = _WRITE,
) -> Dict[str, Any]:
    """Preserve one opaque extension verbatim and mark it for its decider.

    An unrecognised kind — including a future build's completion claim — is
    stored and routed, never interpreted and never turned back into work.
    """
    try:
        return await asyncio.to_thread(
            occurrence.attach_extension,
            occurrence.ExtensionRecord(
                task_occurrence_id=task_occurrence_id,
                extension_id=body.extension_id,
                extension_kind=body.extension_kind,
                extension_version=body.extension_version,
                decider=body.decider,
                payload=body.payload,
                claims_final=body.claims_final,
            ),
        )
    except _ERRORS as exc:
        raise _http(exc)


@router.post("/task-occurrences/{task_occurrence_id}/extensions/{extension_id}/route")
async def route_task_extension(
    task_occurrence_id: str,
    extension_id: str,
    body: RouteExtensionBody,
    _scopes: List[str] = _WRITE,
) -> Dict[str, Any]:
    """Record that this extension reached its decider. Never a redispatch."""
    try:
        return await asyncio.to_thread(
            occurrence.route_extension,
            task_occurrence_id,
            extension_id,
            routed_by=body.routed_by,
        )
    except _ERRORS as exc:
        raise _http(exc)


@router.get("/task-occurrence-extensions/pending")
async def list_pending_extensions(
    decider: Optional[str] = Query(default=None),
    session_name: Optional[str] = Query(default=None),
    _scopes: List[str] = _READ,
) -> Dict[str, Any]:
    """Extensions still waiting on the decider that owns them."""
    try:
        records = await asyncio.to_thread(
            occurrence.pending_extensions, decider, session_name=session_name
        )
    except _ERRORS as exc:
        raise _http(exc)
    return {"extensions": records, "count": len(records)}


# ---------------------------------------------------------------------------
# safe drain
# ---------------------------------------------------------------------------


class DrainBody(StrictBody):
    """One safe drain of one fleet to a boundary.

    ``drain_id`` is caller-minted so a lost response retries the same drain
    and adopts it rather than steering every worker a second time.
    """

    drain_id: str = Field(min_length=36, max_length=36)
    intent: str = Field(pattern=r"^(pause|stop)$")
    initiated_by: str = Field(min_length=1, max_length=200)
    retry: bool = False


class SafePauseFromDrainBody(StrictBody):
    operation_id: str = Field(min_length=36, max_length=36)
    drain_id: str = Field(min_length=36, max_length=36)
    initiated_by: str = Field(min_length=1, max_length=200)
    reason: Optional[str] = Field(default=None, max_length=500)


@router.get("/drains/{drain_id}")
async def get_drain(drain_id: str, _scopes: List[str] = _READ) -> Dict[str, Any]:
    """One drain with its members and the derived provenance block."""
    try:
        return await asyncio.to_thread(supervisor_drain.get_drain, drain_id)
    except _ERRORS as exc:
        raise _http(exc)


@router.get("/sessions/{session_name}/drains")
async def list_drains(session_name: str, _scopes: List[str] = _READ) -> Dict[str, Any]:
    try:
        records = await asyncio.to_thread(supervisor_drain.list_drains, session_name)
    except _ERRORS as exc:
        raise _http(exc)
    return {"drains": records, "count": len(records)}


@router.post("/sessions/{session_name}/drain/safe")
async def run_safe_drain(
    session_name: str,
    body: DrainBody,
    _scopes: List[str] = _ADMIN,
) -> Dict[str, Any]:
    """Steer the fleet to its next safe boundary, once, and settle the drain.

    Admin-scoped: this types into every non-idle worker's pane. It is the only
    write in this router that touches a pane at all.

    A drain that cannot prove a boundary returns ``reconciliation-required``
    with no receipt. That is the honest answer and it is deliberately not
    retried automatically — re-sending is an explicit ``retry``, and giving up
    on the boundary is M3-C's explicit, receipted force promotion.
    """
    try:
        request = supervisor_drain.DrainRequest(
            drain_id=body.drain_id,
            session_name=session_name,
            intent=body.intent,
            initiated_by=body.initiated_by,
        )
        return await asyncio.to_thread(supervisor_drain.execute_drain, request, retry=body.retry)
    except _ERRORS as exc:
        raise _http(exc)


@router.post("/sessions/{session_name}/cohort/pause/safe-drained")
async def cohort_pause_safe_from_drain(
    session_name: str,
    body: SafePauseFromDrainBody,
    _scopes: List[str] = _ADMIN,
) -> Dict[str, Any]:
    """Spend one complete drain receipt on a safe Pause.

    The operator-facing half of ``POST /cohort/pause/safe``. That route takes
    the receipt digest and the member classification separately, which is the
    right shape for the component that produced both and the wrong shape for
    anybody else: a client that carried the digest forward while editing the
    member list would spend a real receipt on a claim it does not describe.
    Naming the drain means the server reads both from the same durable row.
    """
    try:
        request = cohort_operations.OperatorRequest(
            session_name=session_name,
            initiated_by=body.initiated_by,
            operation_id=body.operation_id,
            reason=body.reason,
        )
        operation = await asyncio.to_thread(
            cohort_operations.pause_safe_from_drain, request, drain_id=body.drain_id
        )
    except _ERRORS as exc:
        raise _http(exc)
    try:
        return await asyncio.to_thread(cohort.get_operation, operation["operation_id"])
    except cohort.CohortJournalError:
        # The write is durable; a failed re-read must not turn a successful
        # Pause into an error the operator retries.
        return operation


# ---------------------------------------------------------------------------
# supervisor reconciliation wakes
# ---------------------------------------------------------------------------


class PausedToWorkingBody(StrictBody):
    source_operation_id: str = Field(min_length=36, max_length=36)
    initiated_by: str = Field(min_length=1, max_length=200)


@router.get("/sessions/{session_name}/reconciliation-wakes")
async def list_reconciliation_wakes(
    session_name: str, _scopes: List[str] = _READ
) -> Dict[str, Any]:
    """Every supervisor reconciliation wake recorded for this session.

    Includes the exact message each supervisor was sent, because "the
    supervisor was told" is only checkable if a reader can see what it was
    told.
    """
    try:
        records = await asyncio.to_thread(reconciliation.list_wakes, session_name)
    except _ERRORS as exc:
        raise _http(exc)
    return {"wakes": records, "count": len(records)}


@router.get("/cohort-operations/{operation_id}/reconciliation-wake")
async def get_reconciliation_wake(operation_id: str, _scopes: List[str] = _READ) -> Dict[str, Any]:
    """The one wake for this operation, or a 404 if none was ever claimed."""
    try:
        record = await asyncio.to_thread(reconciliation.get_wake, operation_id)
    except _ERRORS as exc:
        raise _http(exc)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no supervisor reconciliation wake is recorded for operation {operation_id}",
        )
    return record


@router.post("/sessions/{session_name}/reconciliation-wakes/paused-to-working")
async def wake_paused_to_working(
    session_name: str,
    body: PausedToWorkingBody,
    _scopes: List[str] = _ADMIN,
) -> Dict[str, Any]:
    """Tell a started-from-paused supervisor how its fleet came back. Once.

    A fleet resumed paused has live panes and a supervisor that was
    deliberately told nothing. Declaring it working is the moment that
    changes, and it gets exactly the same one-wake guarantee: the id is
    derived from the source Resume, so a retried start adopts the delivered
    wake rather than bumping the supervisor twice.
    """
    try:
        return await asyncio.to_thread(
            reconciliation.wake_paused_to_working,
            session_name,
            source_operation_id=body.source_operation_id,
        )
    except _ERRORS as exc:
        raise _http(exc)

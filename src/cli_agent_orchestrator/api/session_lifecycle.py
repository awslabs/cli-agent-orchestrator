"""HTTP surface for what a session is doing.

Kept out of ``api/main.py`` for the reason the tracker and the attachment
routers already are: that module is 5,500 lines and a subsystem that must
be read alongside the managed-launch state machine to be understood is one
nobody will extend.

**The read routes must not consult tmux.** The existing ``GET /sessions/{name}``
fails closed on ``session_exists`` first, which is correct for a route that
returns live terminals and fatal for one that returns declared state: a
``stopped`` session is *defined* by its tmux session being gone, so
inheriting that guard would make the state of a stopped session
unreadable — exactly the state whose whole purpose is to outlive the panes.
``POST /lifecycle/stop`` (and archive, which forces a stop) is the one
exception: it is an *action*, not a read, and a stop that did not collect
would leave a fleet running under a declaration that says it is hibernated.
It collects through ``session_service.stop_session`` and leaves the row, the
forwarded env, and the snapshots intact — so the read routes stay readable
once tmux is gone.

Routers are registered before the app-level ``/sessions`` routes and match
first, so ``/sessions/{name}/lifecycle`` is safely owned here.

On vocabulary: the session field is ``lifecycle`` and the block is
``session_lifecycle``, never abbreviated to ``state``. A terminal already
has a ``lifecycle_state`` — *observed* liveness, values
``live|superseded|dead|unknown-liveness`` — which will sit in adjacent JSON
with a disjoint value set. And ``session_lifecycle_claim`` in
``callback_recovery`` is a *lock* over a terminal generation, unrelated to
any of this.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from cli_agent_orchestrator.security.auth import (
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
    require_any_scope,
)
from cli_agent_orchestrator.services import session_lifecycle as sl
from cli_agent_orchestrator.services import session_resumability, session_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["session-lifecycle"])

_READ = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN))
_WRITE = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN))

_STATUS_FOR_CODE = {
    "session-lifecycle-invalid": status.HTTP_400_BAD_REQUEST,
    "session-lifecycle-conflict": status.HTTP_409_CONFLICT,
    "session-lifecycle-unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _http(exc: sl.SessionLifecycleError) -> HTTPException:
    # First line only. A store failure wraps the driver's exception, which
    # SQLAlchemy renders as a readable sentence followed by the whole
    # generated query.
    return HTTPException(
        status_code=_STATUS_FOR_CODE.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail=str(exc).splitlines()[0],
    )


class StrictBody(BaseModel):
    """Reject unknown fields instead of ignoring them.

    Especially load-bearing here: a dropped ``declared_by`` would record a
    transition with nobody's name on it, and a dropped ``expected_epoch``
    would silently turn a compare-and-swap into a last-write-wins.
    """

    model_config = ConfigDict(extra="forbid")


class DeclareBody(StrictBody):
    lifecycle: str = Field(pattern=r"^(working|complete)$")
    declared_by: str = Field(min_length=1, max_length=200)
    note: Optional[str] = Field(default=None, max_length=2000)
    expected_epoch: Optional[int] = Field(default=None, ge=0)


class PauseRequestBody(StrictBody):
    requested_by: str = Field(min_length=1, max_length=200)
    deadline_seconds: int = Field(default=sl.DEFAULT_PAUSE_DEADLINE_SECONDS, gt=0, le=86400)
    note: Optional[str] = Field(default=None, max_length=2000)
    expected_epoch: Optional[int] = Field(default=None, ge=0)


class SettleBody(StrictBody):
    declared_by: str = Field(min_length=1, max_length=200)
    note: Optional[str] = Field(default=None, max_length=2000)
    expected_epoch: Optional[int] = Field(default=None, ge=0)


class StopBody(StrictBody):
    """Collecting every pane in a session.

    ``acknowledged_one_way`` exists because stop is currently irreversible
    for every worker — no provider has a working resume path — and the
    spec's rule is that proceeding is allowed while proceeding *unknowingly*
    is not. A client that has not read the impact cannot set it truthfully,
    which is the point.
    """

    declared_by: str = Field(min_length=1, max_length=200)
    acknowledged_one_way: bool
    restore_to: Optional[str] = Field(default=None, pattern=r"^(working|complete|paused)$")
    note: Optional[str] = Field(default=None, max_length=2000)
    expected_epoch: Optional[int] = Field(default=None, ge=0)


class ArchiveBody(StrictBody):
    """Archiving forces a stop, so it carries a stop's obligations.

    It reached the same `stopped` state as `/lifecycle/stop` while asking
    for less: write scope instead of admin, and no acknowledgement. One
    resulting state behind two different gates is how an operator learns
    to reach it by the cheap door.
    """

    archived: bool
    declared_by: str = Field(min_length=1, max_length=200)
    acknowledged_one_way: bool = False
    note: Optional[str] = Field(default=None, max_length=2000)
    expected_epoch: Optional[int] = Field(default=None, ge=0)


class KindBody(StrictBody):
    kind: str = Field(pattern=r"^(campaign|service)$")
    declared_by: str = Field(min_length=1, max_length=200)
    expected_epoch: Optional[int] = Field(default=None, ge=0)


@router.get("/session-lifecycle")
async def list_session_lifecycle(
    lifecycle: Optional[List[str]] = Query(default=None),
    include_archived: bool = Query(default=False),
    _scopes: List[str] = _READ,
) -> Dict[str, Any]:
    """Every session anybody has declared anything about.

    Deliberately does not enumerate live tmux sessions. This table knows
    nothing about which sessions exist, which is precisely the fact it is
    not allowed to infer — a caller wanting "live sessions and their
    declared state" joins the two itself.
    """
    try:
        records = await asyncio.to_thread(
            sl.list_sessions,
            lifecycles=frozenset(lifecycle) if lifecycle else None,
            include_archived=include_archived,
        )
    except sl.SessionLifecycleError as exc:
        raise _http(exc)
    return {"sessions": records, "count": len(records)}


@router.get("/sessions/{session_name}/lifecycle")
async def get_session_lifecycle(
    session_name: str,
    _scopes: List[str] = _READ,
) -> Dict[str, Any]:
    """The declared state, defaulting to ``working`` for an undeclared session.

    Never 404s. Every session that has ever run predates this table, and an
    undeclared session is a working one rather than a missing one — the
    default has to be the answer that keeps the marshal watching.

    Carries the suppression verdict rather than leaving each caller to
    re-derive it from ``lifecycle``. The conductor is the caller that
    matters, and a second copy of the suppressing set living over there
    would drift from this one silently — in the direction of a marshal
    that stays quiet, which is the failure nobody notices.
    """
    # One read, then derive. Reading twice would let the verdict describe a
    # different version of the row than the record it is attached to — a
    # small window, and the wrong half of it is a payload that says
    # `pausing` and `suppresses_marshal: true`.
    record = await asyncio.to_thread(sl.describe, session_name)
    diverges = await asyncio.to_thread(sl.divergence, record)
    return {
        **record,
        "diverges": diverges,
        "suppresses_marshal": (
            False if record.get("unreadable") else record["lifecycle"] in sl.MARSHAL_SUPPRESSING
        ),
        "pause_overdue": sl.pause_is_overdue(record),
    }


@router.get("/sessions/{session_name}/stop-impact")
async def get_stop_impact(
    session_name: str,
    _scopes: List[str] = _READ,
) -> Dict[str, Any]:
    """What stopping this session would cost, computed per live worker.

    The confirmation dialog's source of truth. Computed rather than looked
    up, because the design's own provider table overstates resumability
    four different ways and every one of them fails toward understating
    what an operator is about to lose.
    """
    return await asyncio.to_thread(session_resumability.stop_impact, session_name)


@router.post("/sessions/{session_name}/lifecycle")
async def declare_session_lifecycle(
    session_name: str,
    body: DeclareBody,
    _scopes: List[str] = _WRITE,
) -> Dict[str, Any]:
    """Declare ``working`` or ``complete``.

    ``complete`` is a declaration, not a teardown: nothing is collected and
    no worker is retired. A mistaken ``complete`` that tore the fleet down
    would destroy the evidence needed to tell that it was mistaken.

    The other transitions have their own routes because each carries an
    obligation this one cannot express — a pause needs a deadline, a stop
    needs an acknowledgement.
    """
    try:
        return await asyncio.to_thread(
            sl.declare,
            session_name,
            body.lifecycle,
            declared_by=body.declared_by,
            note=body.note,
            expected_epoch=body.expected_epoch,
        )
    except sl.SessionLifecycleError as exc:
        raise _http(exc)


@router.post("/sessions/{session_name}/lifecycle/pause-request")
async def request_session_pause(
    session_name: str,
    body: PauseRequestBody,
    _scopes: List[str] = _WRITE,
) -> Dict[str, Any]:
    """Ask for a pause. Does not grant one.

    The session enters ``pausing`` immediately so the UI can disable the
    button — settling takes minutes and a button that still looks pressable
    invites a second press — but only the supervisor can say the fleet
    settled, because only the supervisor knows whether the work is at a
    resumable boundary.
    """
    try:
        return await asyncio.to_thread(
            sl.request_pause,
            session_name,
            requested_by=body.requested_by,
            deadline_seconds=body.deadline_seconds,
            note=body.note,
            expected_epoch=body.expected_epoch,
        )
    except sl.SessionLifecycleError as exc:
        raise _http(exc)


@router.post("/sessions/{session_name}/lifecycle/pause-settled")
async def settle_session_pause(
    session_name: str,
    body: SettleBody,
    _scopes: List[str] = _WRITE,
) -> Dict[str, Any]:
    """The supervisor's half: the fleet is settled, the session is paused."""
    try:
        return await asyncio.to_thread(
            sl.settle_pause,
            session_name,
            declared_by=body.declared_by,
            note=body.note,
            expected_epoch=body.expected_epoch,
        )
    except sl.SessionLifecycleError as exc:
        raise _http(exc)


def _stop_registry(request: Request):
    # Imported lazily to avoid a load-time cycle: api.main imports this router.
    from cli_agent_orchestrator.api.main import get_plugin_registry

    return get_plugin_registry(request)


@router.post("/sessions/{session_name}/lifecycle/stop")
async def stop_session_lifecycle(
    request: Request,
    session_name: str,
    body: StopBody,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_ADMIN)),
) -> Dict[str, Any]:
    """Collect a session's panes and record it stopped, preserving the truth.

    Admin-only and gated on an explicit acknowledgement, because today this
    is irreversible for every worker in the session. It records
    ``restore_to`` anyway: that is the only moment the fact is known, and
    keeping it costs one column while discarding it would mean a session
    stopped this week cannot be brought back correctly once resume lands.

    Collection and the declaration are one operation, not two: the panes are
    torn down through the same event-driven teardown ``DELETE`` uses
    (snapshotted first, so recovery artifacts survive) and the lifecycle row
    is left in ``stopped`` with its ``restore_to`` target and forwarded env
    intact. Each collected pane emits the normal completion hooks via the live
    plugin registry. Releasing the name stays the destructive ``DELETE``
    path's job; a stop never forgets the row or clears the env. The lifecycle
    *read* routes stay tmux-free by design — only this action collects.
    """
    if not body.acknowledged_one_way:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "stopping is currently one-way for every worker; read "
                f"GET /sessions/{session_name}/stop-impact and set acknowledged_one_way"
            ),
        )
    try:
        return await asyncio.to_thread(
            session_service.stop_session,
            session_name,
            declared_by=body.declared_by,
            restore_to=body.restore_to,
            note=body.note,
            expected_epoch=body.expected_epoch,
            registry=_stop_registry(request),
        )
    except sl.SessionLifecycleError as exc:
        raise _http(exc)
    except session_service.SessionStopRefused as exc:
        # Zero-effect precondition refusal (e.g. open callback recovery):
        # deliberate, redacted operational guidance the operator can act on.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc).splitlines()[0],
        )
    except session_service.SessionStopPartial as exc:
        logger.warning(
            "session stop partially collected for %s: collected=%d errors=%s",
            session_name,
            len(exc.collected_terminal_ids),
            exc.errors,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="session stop partially collected; lifecycle preserved; retry converges",
        )
    except Exception:
        logger.exception("session stop failed for %s", session_name)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="session stop failed; see server logs",
        )


@router.post("/sessions/{session_name}/lifecycle/archived")
async def set_session_archived(
    request: Request,
    session_name: str,
    body: ArchiveBody,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_ADMIN)),
) -> Dict[str, Any]:
    """Hide or unhide a session without losing what it was.

    Archiving forces a stop and is therefore gated exactly as stopping is,
    and it carries a stop's full obligation: it collects the panes and
    preserves the row, the forwarded env, and the snapshots. Un-archiving
    only clears the flag — it does not restore the lifecycle, because that
    is resume's job and resume would have to relaunch panes.
    """
    if body.archived and not body.acknowledged_one_way:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "archiving forces a stop, which is currently one-way for every worker; read "
                f"GET /sessions/{session_name}/stop-impact and set acknowledged_one_way"
            ),
        )
    try:
        if body.archived:
            return await asyncio.to_thread(
                session_service.stop_session,
                session_name,
                declared_by=body.declared_by,
                note=body.note,
                expected_epoch=body.expected_epoch,
                archived=True,
                registry=_stop_registry(request),
            )
        return await asyncio.to_thread(
            sl.set_archived,
            session_name,
            False,
            declared_by=body.declared_by,
            note=body.note,
            expected_epoch=body.expected_epoch,
        )
    except sl.SessionLifecycleError as exc:
        raise _http(exc)
    except session_service.SessionStopRefused as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc).splitlines()[0],
        )
    except session_service.SessionStopPartial as exc:
        logger.warning(
            "session archive partially collected for %s: collected=%d errors=%s",
            session_name,
            len(exc.collected_terminal_ids),
            exc.errors,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="session stop partially collected; lifecycle preserved; retry converges",
        )
    except Exception:
        logger.exception("session archive failed for %s", session_name)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="session archive failed; see server logs",
        )


@router.post("/sessions/{session_name}/lifecycle/kind")
async def set_session_kind(
    session_name: str,
    body: KindBody,
    _scopes: List[str] = _WRITE,
) -> Dict[str, Any]:
    """Declare how this session's health should be judged at all."""
    try:
        return await asyncio.to_thread(
            sl.set_kind,
            session_name,
            body.kind,
            declared_by=body.declared_by,
            expected_epoch=body.expected_epoch,
        )
    except sl.SessionLifecycleError as exc:
        raise _http(exc)

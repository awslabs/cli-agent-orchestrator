"""HTTP surface for reversible A -> B -> A worker handbacks (M3-E).

Four write routes, one per boundary a handback actually has: enter the held
window, record the one packet delivery, transfer, roll back. There is no
"perform a handback" route, because a handback is not one effect — it spans an
exact resume and a packet delivery that this service deliberately does not
own, and collapsing them would hide the two points where a supervisor can still
change its mind.

Every route here is tmux-free. The holds these routes install are read by the
provider-byte admission path, not enforced from here.

Nothing calls these routes automatically. A handback is one explicit supervisor
decision; ``GET /task-handoffs/capability`` says so in a form an operator can
read rather than leaving it to be inferred from the routes' existence.
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
from cli_agent_orchestrator.services import task_handoff as handoff
from cli_agent_orchestrator.services import task_occurrence as occurrence

router = APIRouter(tags=["task-handoff"])

_READ = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN))
_WRITE = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN))

_STATUS_FOR_CODE = {
    "task-handoff-invalid": status.HTTP_400_BAD_REQUEST,
    "task-handoff-conflict": status.HTTP_409_CONFLICT,
    "task-handoff-not-found": status.HTTP_404_NOT_FOUND,
    "task-handoff-unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "task-occurrence-invalid": status.HTTP_400_BAD_REQUEST,
    "task-occurrence-conflict": status.HTTP_409_CONFLICT,
    "task-occurrence-not-found": status.HTTP_404_NOT_FOUND,
    "task-occurrence-unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
}

_ERRORS = (handoff.TaskHandoffError, occurrence.TaskOccurrenceError)


def _http(exc: Any) -> HTTPException:
    # First line only: a store failure wraps the driver's exception, which
    # SQLAlchemy renders as a sentence followed by the whole generated query.
    return HTTPException(
        status_code=_STATUS_FOR_CODE.get(getattr(exc, "code", ""), status.HTTP_400_BAD_REQUEST),
        detail=str(exc).splitlines()[0],
    )


class StrictBody(BaseModel):
    """Reject unknown fields instead of ignoring them.

    Load-bearing here: a dropped ``expected_epoch`` or ``expected_revision``
    would turn a compare-and-swap into last-write-wins, and a dropped
    ``turn_state`` would let a handback begin against a worker mid-turn.
    """

    model_config = ConfigDict(extra="forbid")


class QuiescenceBody(StrictBody):
    incarnation_id: str = Field(min_length=1, max_length=512)
    terminal_id: str = Field(min_length=1, max_length=512)
    turn_state: str = Field(pattern="^(active|terminal|unknown)$")
    #: Required, and never defaulted server-side: it is digested into the
    #: handoff's immutable content, so a server-invented timestamp would make
    #: the caller's own retry hash differently and conflict with itself.
    observed_at: str = Field(min_length=1, max_length=512)
    generation: Optional[str] = Field(default=None, max_length=512)
    known_child_effects: str = Field(default="none-tracked", pattern="^(none-tracked)$")
    boundary_digest: Optional[str] = Field(default=None, min_length=64, max_length=64)
    report_digest: Optional[str] = Field(default=None, min_length=64, max_length=64)
    checkpoint_digest: Optional[str] = Field(default=None, min_length=64, max_length=64)


class BeginHandoffBody(StrictBody):
    handoff_id: str = Field(min_length=1, max_length=64)
    task_occurrence_id: str = Field(min_length=1, max_length=64)
    to_agent_id: str = Field(min_length=1, max_length=64)
    packet_digest: str = Field(min_length=64, max_length=64)
    evidence: QuiescenceBody
    initiated_by: str = Field(min_length=1, max_length=512)
    #: The donor revision the packet digest describes. Required: the digest is
    #: taken before begin, and a caller that cannot pin the revision has no way
    #: to keep a stale packet from passing the transfer check (cond-0439).
    expected_donor_revision: int = Field(ge=0)
    detail: Optional[str] = Field(default=None, max_length=2000)


class DeliveryBody(StrictBody):
    delivered: bool
    outcome: Optional[str] = Field(default=None, max_length=512)
    receipt: Optional[str] = Field(default=None, max_length=512)
    #: The recipient incarnation that received the packet. Required whenever
    #: ``delivered`` is true: the packet is unreachable to any other
    #: incarnation, so a transfer has to be able to refuse one that never read
    #: it.
    incarnation_id: Optional[str] = Field(default=None, max_length=512)
    terminal_id: Optional[str] = Field(default=None, max_length=512)
    generation: Optional[str] = Field(default=None, max_length=512)


class CompleteHandoffBody(StrictBody):
    expected_revision: int = Field(ge=0)
    completed_by: str = Field(min_length=1, max_length=512)
    incarnation_id: str = Field(min_length=1, max_length=512)
    terminal_id: str = Field(min_length=1, max_length=512)
    generation: Optional[str] = Field(default=None, max_length=512)
    lineage_id: Optional[str] = Field(default=None, max_length=512)


class RollbackHandoffBody(StrictBody):
    rolled_back_by: str = Field(min_length=1, max_length=512)
    reason: str = Field(min_length=1, max_length=2000)


def _evidence(body: QuiescenceBody) -> handoff.QuiescenceEvidence:
    return handoff.QuiescenceEvidence(
        incarnation_id=body.incarnation_id,
        terminal_id=body.terminal_id,
        turn_state=body.turn_state,
        observed_at=body.observed_at,
        generation=body.generation,
        known_child_effects=body.known_child_effects,
        boundary_digest=body.boundary_digest,
        report_digest=body.report_digest,
        checkpoint_digest=body.checkpoint_digest,
    )


# ---------------------------------------------------------------------------
# the four boundaries
# ---------------------------------------------------------------------------


@router.post("/sessions/{session_name}/task-handoffs")
async def begin_task_handoff(
    session_name: str,
    body: BeginHandoffBody,
    _scopes: List[str] = _WRITE,
) -> Dict[str, Any]:
    """Enter the held window. Touches no occurrence and sends no bytes.

    From here both agents refuse ordinary task input until the handoff settles,
    and the response carries the one control id the catch-up packet may be
    delivered under.
    """
    try:
        return await asyncio.to_thread(
            handoff.begin_handoff,
            handoff.BeginRequest(
                handoff_id=body.handoff_id,
                session_name=session_name,
                task_occurrence_id=body.task_occurrence_id,
                to_agent_id=body.to_agent_id,
                packet_digest=body.packet_digest,
                evidence=_evidence(body.evidence),
                initiated_by=body.initiated_by,
                expected_donor_revision=body.expected_donor_revision,
                detail=body.detail,
            ),
        )
    except _ERRORS as exc:
        raise _http(exc)


@router.post("/task-handoffs/{handoff_id}/packet-delivery")
async def record_task_handoff_delivery(
    handoff_id: str,
    body: DeliveryBody,
    _scopes: List[str] = _WRITE,
) -> Dict[str, Any]:
    """Bind the outcome of the one catch-up-packet delivery.

    The delivery itself goes through the ordinary control-input path under the
    derived ``packet_control_id``; this records what that path answered.
    """
    incarnation = None
    if body.incarnation_id and body.terminal_id:
        incarnation = occurrence.EffectIncarnation(
            incarnation_id=body.incarnation_id,
            terminal_id=body.terminal_id,
            generation=body.generation,
        )
    try:
        return await asyncio.to_thread(
            handoff.record_packet_delivery,
            handoff_id,
            delivered=body.delivered,
            incarnation=incarnation,
            outcome=body.outcome,
            receipt=body.receipt,
        )
    except _ERRORS as exc:
        raise _http(exc)


@router.post("/task-handoffs/{handoff_id}/transfer")
async def complete_task_handoff(
    handoff_id: str,
    body: CompleteHandoffBody,
    _scopes: List[str] = _WRITE,
) -> Dict[str, Any]:
    """Transfer the task: finalize the donor's round and open the recipient's.

    One transaction. A crash between the two writes leaves the handoff pending
    and still rollback-able rather than leaving the task with no owner.

    One refusal does not raise: if an unrelated dispatch opened a round for
    the recipient while the handoff was pending, this returns 200 with
    ``state='failed'`` — nothing transferred, both holds released, and the
    record's ``recipient_briefed`` says whether the caller owes the recipient
    a cancellation (cond-0440).
    """
    try:
        return await asyncio.to_thread(
            handoff.complete_handoff,
            handoff_id,
            # No ``native_session_id``. A handback never moves a native
            # conversation, and an unvalidated one here would let an ordinary
            # field-name slip in the caller stamp the *donor's* native session
            # onto the recipient's occurrence -- a durably false harness
            # binding that every later reader of that provenance would trust.
            # M3-B's roster predicates own that binding; nothing here needs it.
            incarnation=occurrence.EffectIncarnation(
                incarnation_id=body.incarnation_id,
                terminal_id=body.terminal_id,
                generation=body.generation,
                lineage_id=body.lineage_id,
            ),
            expected_revision=body.expected_revision,
            completed_by=body.completed_by,
        )
    except _ERRORS as exc:
        raise _http(exc)


@router.post("/task-handoffs/{handoff_id}/rollback")
async def rollback_task_handoff(
    handoff_id: str,
    body: RollbackHandoffBody,
    _scopes: List[str] = _WRITE,
) -> Dict[str, Any]:
    """Release both holds and leave the donor exactly as it was.

    Always releases. ``donor_restored`` reports whether the donor is still a
    usable worker; a rollback that refused because the donor's pane had died
    would leave both agents held with no forward path.
    """
    try:
        return await asyncio.to_thread(
            handoff.rollback_handoff,
            handoff_id,
            rolled_back_by=body.rolled_back_by,
            reason=body.reason,
        )
    except _ERRORS as exc:
        raise _http(exc)


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


@router.get("/task-handoffs/capability")
async def task_handoff_capability(_scopes: List[str] = _READ) -> Dict[str, Any]:
    """What this build does, published rather than inferred from route presence."""
    return handoff.capability()


@router.get("/task-handoffs/{handoff_id}")
async def get_task_handoff(handoff_id: str, _scopes: List[str] = _READ) -> Dict[str, Any]:
    try:
        return await asyncio.to_thread(handoff.get_handoff, handoff_id)
    except _ERRORS as exc:
        raise _http(exc)


@router.get("/sessions/{session_name}/task-handoffs")
async def list_task_handoffs(
    session_name: str,
    state: Optional[str] = Query(default=None),
    _scopes: List[str] = _READ,
) -> List[Dict[str, Any]]:
    try:
        return await asyncio.to_thread(handoff.list_handoffs, session_name, state=state)
    except _ERRORS as exc:
        raise _http(exc)


@router.get("/agents/{agent_id}/task-handoff-hold")
async def get_agent_handoff_hold(agent_id: str, _scopes: List[str] = _READ) -> Dict[str, Any]:
    """Whether this agent is currently held, and on which side.

    The diagnostic answer to "why was my steer refused". Absence is a normal
    answer, not a 404: most agents are not party to a handoff.
    """
    try:
        held = await asyncio.to_thread(handoff.hold_for_agent, agent_id)
    except _ERRORS as exc:
        raise _http(exc)
    return {"agent_id": agent_id, "held": held is not None, "handoff": held}

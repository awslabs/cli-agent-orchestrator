"""HTTP surface for provider-native session claims.

Kept out of ``api/main.py`` for the same reason the tracker is: that
module is already 5,500 lines, and this is a small self-contained
subsystem with its own vocabulary.

Note the name. ``/terminals/{id}/attachments`` already exists and means
*image* attachments on operator messages — an unrelated feature that
unfortunately owns the shorter word. These routes are mounted under
``/native-attachments`` so the two are never confusable in a log line.

The mutating routes demand ``cao:admin`` alone rather than write-or-admin.
Releasing a claim decides who may drive a provider session next, which is
the same class of decision as resolving a callback ambiguity — and that
route made the same choice.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from cli_agent_orchestrator.security.auth import (
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
    require_any_scope,
)
from cli_agent_orchestrator.services import native_attachment as na
from cli_agent_orchestrator.services import native_attachment_recovery as recovery

logger = logging.getLogger(__name__)

router = APIRouter(tags=["native-attachments"])

_READ = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN))
_ADMIN = Depends(require_any_scope(SCOPE_ADMIN))

# A refusal the service classified keeps that classification at the HTTP
# boundary. In particular a conflict is not an error in the client's
# request — it is the store correctly declining to hand over a session
# somebody still holds — and 409 is the only status that says so.
_STATUS_FOR_CODE = {
    "native-attachment-invalid": status.HTTP_400_BAD_REQUEST,
    "native-attachment-not-found": status.HTTP_404_NOT_FOUND,
    "native-attachment-conflict": status.HTTP_409_CONFLICT,
    "native-attachment-unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _http(exc: na.NativeAttachmentError) -> HTTPException:
    # First line only, matching the CLI. A store failure wraps the driver's
    # exception, and SQLAlchemy renders that as a readable sentence followed
    # by the entire generated query — which is noise in a JSON error body and,
    # on a 503, tells a client the schema of a table it could not read.
    return HTTPException(
        status_code=_STATUS_FOR_CODE.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail=str(exc).splitlines()[0],
    )


class StrictBody(BaseModel):
    """Reject unknown fields instead of ignoring them.

    A body that silently drops what it does not recognise is especially
    bad here: an adjudication is a permanent record of what somebody
    asserted, and a dropped field means the record says less than the
    operator thought they were signing.
    """

    model_config = ConfigDict(extra="forbid")


class SweepBody(StrictBody):
    apply: bool = Field(
        default=False,
        description="Release the claims whose owner is gone. Default is a pure-read dry run.",
    )


class AdjudicationBody(StrictBody):
    """One operator's judgement that a frozen claim's owner is gone.

    Shaped after the callback-recovery resolution body, which solved this
    exact problem for the other ambiguous state in this system: a closed
    ``outcome`` literal the caller must name exactly, a digest naming the
    evidence, and a bounded free-text reason with somebody's name on it.
    """

    outcome: str = Field(pattern=r"^owner-proven-gone$")
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    detail: str = Field(min_length=1, max_length=500)
    operator: str = Field(min_length=1, max_length=200)
    attests_live_process_is_not_the_owner: bool = Field(
        default=False,
        description=(
            "Attest that the process still bearing the recorded pid is somebody else. "
            "Required to decide past a live pid, and accepted only when its start marker "
            "was read and genuinely differs from the recorded one."
        ),
    )


@router.get("/native-attachments")
async def list_native_attachments(
    state: Optional[List[str]] = Query(default=None),
    provider: Optional[List[str]] = Query(default=None),
    owner_terminal_id: Optional[str] = Query(default=None),
    _scopes: List[str] = _READ,
) -> Dict[str, Any]:
    """Every claim matching the filters, oldest first.

    Defaults to the states that still hold a session. A detached row is
    history; the operative question is what is currently blocked.
    """
    states = frozenset(state) if state else na.LIVE_STATES | {na.AMBIGUOUS}
    try:
        records = await asyncio.to_thread(
            na.list_attachments,
            states=states,
            providers=frozenset(provider) if provider else None,
            owner_terminal_id=owner_terminal_id,
        )
    except na.NativeAttachmentError as exc:
        raise _http(exc)
    return {"attachments": records, "count": len(records)}


@router.post("/native-attachments/sweep")
async def sweep_native_attachments(
    body: SweepBody,
    _scopes: List[str] = _ADMIN,
) -> Dict[str, Any]:
    """Release every claim whose owning process is provably gone."""
    try:
        return await asyncio.to_thread(recovery.sweep, apply=body.apply)
    except na.NativeAttachmentError as exc:
        raise _http(exc)


# Declared before the two-segment routes below. Their arity already keeps
# `sweep` from matching a `{provider}` — but a later route added here with
# one path segment would silently be shadowed, and the ordering is the
# habit that prevents it rather than the comment.
@router.get("/native-attachments/{provider}/{native_session_id}")
async def get_native_attachment(
    provider: str,
    native_session_id: str,
    observe: bool = Query(default=True),
    _scopes: List[str] = _READ,
) -> Dict[str, Any]:
    """One claim, and by default a fresh look for the process holding it.

    The observation is the part a reader cannot get from the row: the row
    records who took the claim, never whether that owner is still there.
    """
    try:
        record = await asyncio.to_thread(na.get, provider, native_session_id)
    except na.NativeAttachmentError as exc:
        raise _http(exc)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no claim on {provider} session {native_session_id}",
        )
    payload = dict(record)
    if observe:
        payload["observation"] = await asyncio.to_thread(recovery.observe_owner, record)
    return payload


class FreezeBody(StrictBody):
    """A named operator declaring one claim's ownership unresolvable."""

    detail: str = Field(min_length=1, max_length=500)
    operator: str = Field(min_length=1, max_length=200)
    attest_live_pid_is_not_the_owner: bool = Field(
        default=False,
        description=(
            "Attest that the process now holding the recorded pid is not the recorded owner. "
            "Accepted only when the live start marker differs from the recorded one."
        ),
    )


@router.post("/native-attachments/{provider}/{native_session_id}/freeze")
async def freeze_native_attachment(
    provider: str,
    native_session_id: str,
    body: FreezeBody,
    _scopes: List[str] = _ADMIN,
) -> Dict[str, Any]:
    """Move a claim the sweep can never settle into the adjudicable state.

    The first of two deliberate steps. A provably-gone owner is released
    by the sweep and never needs this; a running one is refused.
    """
    try:
        return await asyncio.to_thread(
            recovery.freeze_for_adjudication,
            provider=provider,
            native_session_id=native_session_id,
            operator=body.operator,
            detail=body.detail,
            attest_live_pid_is_not_the_owner=body.attest_live_pid_is_not_the_owner,
        )
    except na.NativeAttachmentError as exc:
        raise _http(exc)


@router.post("/native-attachments/{provider}/{native_session_id}/adjudicate")
async def adjudicate_native_attachment(
    provider: str,
    native_session_id: str,
    body: AdjudicationBody,
    _scopes: List[str] = _ADMIN,
) -> Dict[str, Any]:
    """Resolve a frozen claim on a named operator's judgement.

    The observation is taken here, server-side, rather than accepted from
    the caller. A client-supplied survivor list would let the one party
    with a motive to release the session also supply the evidence that it
    is safe to.
    """
    try:
        record = await asyncio.to_thread(na.get, provider, native_session_id)
    except na.NativeAttachmentError as exc:
        raise _http(exc)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no claim on {provider} session {native_session_id}",
        )

    observation = await asyncio.to_thread(recovery.observe_owner, record)
    try:
        return await asyncio.to_thread(
            na.adjudicate,
            provider=provider,
            native_session_id=native_session_id,
            record=na.adjudication(
                outcome=body.outcome,
                evidence_sha256=body.evidence_sha256,
                detail=body.detail,
                operator=body.operator,
                observed_at=observation["observed_at"],
                observation=observation,
                attests_live_process_is_not_the_owner=body.attests_live_process_is_not_the_owner,
            ),
            live_survivors=recovery.survivors_blocking_adjudication(observation),
            observed_epoch=record["epoch"],
        )
    except na.NativeAttachmentError as exc:
        raise _http(exc)

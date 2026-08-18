"""Bounded HTTP surface for the conductor's communications catalog (design §7).

This router is deliberately separate from ``api/main.py`` so that the only
merge collision with live lanes is one import line and one
``include_router`` call.

The three endpoints read the conductor's published projection:

* ``GET /communications`` — metadata list, never bodies;
* ``GET /communications/{communication_id}`` — one communication with body content;
* ``GET /communications/attachments/{attachment_id}`` — one attachment with content.

All detail responses carry ``Cache-Control: no-store``.  The service never
accepts a ``path``, ``root``, or ``project_dir`` parameter, never turns an
identifier into a filename, and verifies digests before serving bytes.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from cli_agent_orchestrator.models.communications_catalog import (
    CommunicationsListResponse,
)
from cli_agent_orchestrator.security.auth import (
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
    require_any_scope,
)
from cli_agent_orchestrator.services import communications_catalog as catalog
from cli_agent_orchestrator.services.communications_catalog import (
    CommunicationsCatalogError,
    CommunicationsCatalogInvalid,
)

router = APIRouter(tags=["communications-catalog"])

_READ = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN))

_STATUS_FOR_CODE = {
    "communications-catalog-invalid": status.HTTP_400_BAD_REQUEST,
    "communications-catalog-not-found": status.HTTP_404_NOT_FOUND,
    "communications-catalog-unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
}

#: Caller-supplied filesystem overrides are refused at the HTTP boundary so the
#: property is obtained by construction, not by sanitising.
_FORBIDDEN_QUERY_PARAMS = frozenset({"path", "root", "project_dir"})


def _http(exc: CommunicationsCatalogError) -> HTTPException:
    status_code = _STATUS_FOR_CODE.get(getattr(exc, "code", ""), status.HTTP_400_BAD_REQUEST)
    message = str(exc).splitlines()[0]
    if isinstance(exc, CommunicationsCatalogInvalid) and getattr(exc, "reason", None):
        detail: Any = {"reason": exc.reason, "message": message}
    else:
        detail = message
    return HTTPException(status_code=status_code, detail=detail)


async def _refuse_path_params(request: Request) -> None:
    for name in request.query_params:
        if name.lower() in _FORBIDDEN_QUERY_PARAMS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"parameter '{name}' is not allowed",
            )


@router.get("/communications", dependencies=[Depends(_refuse_path_params)])
async def list_communications(
    task_occurrence_id: str = Query(..., min_length=1, max_length=catalog.MAX_ID_LEN),
    cursor: Optional[str] = Query(default=None, max_length=512),
    _scopes: List[str] = _READ,
) -> CommunicationsListResponse:
    """Bounded metadata for one task occurrence's communications.

    The response never contains document bodies.  ``cursor`` is an opaque
    keyset over the publisher's total order
    ``recorded_at DESC, communication_id``; offsets are not used because the
    index is republished.
    """
    try:
        payload = await asyncio.to_thread(catalog.list_communications, task_occurrence_id, cursor)
    except CommunicationsCatalogError as exc:
        raise _http(exc)
    return CommunicationsListResponse(**payload)


@router.get("/communications/{communication_id}", dependencies=[Depends(_refuse_path_params)])
async def get_communication(
    communication_id: str,
    _scopes: List[str] = _READ,
) -> JSONResponse:
    """One communication, with its body content when the publisher says it is present.

    The response is JSON with ``Cache-Control: no-store`` so agent-authored
    bytes never arrive in an executable response context.
    """
    try:
        payload = await asyncio.to_thread(catalog.get_communication, communication_id)
    except CommunicationsCatalogError as exc:
        raise _http(exc)
    return JSONResponse(content=payload, headers={"Cache-Control": "no-store"})


@router.get(
    "/communications/attachments/{attachment_id}", dependencies=[Depends(_refuse_path_params)]
)
async def get_attachment(
    attachment_id: str,
    _scopes: List[str] = _READ,
) -> JSONResponse:
    """One attachment, with its content when the publisher says it is present."""
    try:
        payload = await asyncio.to_thread(catalog.get_attachment, attachment_id)
    except CommunicationsCatalogError as exc:
        raise _http(exc)
    return JSONResponse(content=payload, headers={"Cache-Control": "no-store"})

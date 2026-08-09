"""Read/audit HTTP surface for the M3-A stable-agent roster (cond-0377).

Kept out of ``api/main.py`` for the same reason the tracker and native
attachments routers are: this is a small self-contained subsystem with its
own vocabulary, and main.py is already thousands of lines.

The roster is dark and read-mostly in M3-A: the durable records are written
by the launch seams (``bind_native``, unmanaged terminal creation, admission
completion, teardown), and this surface exists so later migration and
status-repair lanes can enumerate it.  Every route is read-only; the
dry-run audit never mutates.  Legacy, missing, corrupt, or unknown-version
rows are reported truthfully (``problems`` / ``identity_missing`` / unknown
dispositions) and never crash a list or audit.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from cli_agent_orchestrator.security.auth import (
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
    require_any_scope,
)
from cli_agent_orchestrator.services import stable_agent_roster as roster

logger = logging.getLogger(__name__)

router = APIRouter(tags=["roster"])

_READ = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN))

_STATUS_FOR_CODE = {
    "stable-agent-invalid": status.HTTP_400_BAD_REQUEST,
    "stable-agent-not-found": status.HTTP_404_NOT_FOUND,
    "stable-agent-conflict": status.HTTP_409_CONFLICT,
    "stable-agent-admission-refused": status.HTTP_409_CONFLICT,
    "stable-agent-unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _http(exc: roster.StableAgentError) -> HTTPException:
    code = getattr(exc, "code", "stable-agent-error")
    detail = str(exc).splitlines()[0] if exc.args else str(exc)
    return HTTPException(status_code=_STATUS_FOR_CODE.get(code, 500), detail=detail)


@router.get("/roster/agents")
async def list_roster_agents(
    session_name: Optional[str] = Query(default=None, description="Scope to one CAO session"),
    _: Any = _READ,
) -> dict[str, Any]:
    """Every stable agent, oldest first; optionally scoped to a session."""

    def _list() -> dict[str, Any]:
        return {
            "schema": "cao-m3-roster-list-v1",
            "agents": roster.list_agents(session_name=session_name),
        }

    try:
        return await asyncio.to_thread(_list)
    except roster.StableAgentError as exc:
        raise _http(exc) from exc


@router.get("/roster/agents/{agent_id}")
async def get_roster_agent(agent_id: str, _: Any = _READ) -> dict[str, Any]:
    """One stable agent with its lineage and incarnation history."""

    def _get() -> dict[str, Any]:
        agent = roster.get_agent(agent_id)
        return {
            "schema": "cao-m3-roster-agent-v1",
            "agent": agent,
            "lineages": roster.list_lineages(agent_id=agent_id),
            "incarnations": roster.list_incarnations(agent_id=agent_id),
        }

    try:
        return await asyncio.to_thread(_get)
    except roster.StableAgentError as exc:
        raise _http(exc) from exc


@router.get("/roster/terminals/{terminal_id}")
async def get_roster_terminal_incarnation(
    terminal_id: str,
    generation: Optional[str] = Query(default=None, description="Exact generation to read"),
    _: Any = _READ,
) -> dict[str, Any]:
    """The roster incarnation bound to one terminal, or an explicit null.

    With ``generation``: that exact generation's incarnation, or null.
    Without it: the unique LIVE incarnation (two live incarnations
    sharing a terminal id refuse as ambiguous rather than picking a
    historical row).
    """

    def _get() -> dict[str, Any]:
        incarnation = roster.get_incarnation_by_terminal(terminal_id, generation=generation)
        return {"schema": "cao-m3-roster-incarnation-v1", "incarnation": incarnation}

    try:
        return await asyncio.to_thread(_get)
    except roster.StableAgentError as exc:
        raise _http(exc) from exc


@router.get("/roster/audit")
async def roster_audit_dry_run(_: Any = _READ) -> dict[str, Any]:
    """A truthful, non-crashing roster-wide dry-run audit.

    Read-only: reports counts, ``identity_missing`` agents, legacy
    migration candidates, and problems (corrupt provenance JSON, unknown
    dispositions, unknown resume-contract versions, dangling pointers).
    Later migration and status-repair lanes consume this without any
    mutation happening here.
    """

    def _audit() -> dict[str, Any]:
        return roster.audit_dry_run()

    try:
        return await asyncio.to_thread(_audit)
    except roster.StableAgentError as exc:
        raise _http(exc) from exc

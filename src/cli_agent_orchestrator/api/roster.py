"""Read/audit HTTP surface for the stable-agent roster.

Kept out of ``api/main.py`` for the same reason the tracker and native
attachments routers are: this is a small self-contained subsystem with its
own vocabulary, and main.py is already thousands of lines.

The roster is dark and read-mostly: the durable records are written
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
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from cli_agent_orchestrator.security.auth import (
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
    require_any_scope,
)
from cli_agent_orchestrator.services import (
    legacy_identity_migration,
    native_status_repair,
    pane_identity_resolution,
    provider_capabilities,
)
from cli_agent_orchestrator.services import stable_agent_roster as roster

logger = logging.getLogger(__name__)

router = APIRouter(tags=["roster"])

_READ = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN))
_WRITE = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN))

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


class NativeIdentityRepairBody(BaseModel):
    """The immutable repair request: an explicit operation id, and the
    expected incarnation and build the caller believes the terminal runs.

    ``generation`` is the *expected model generation* — required for
    managed/v2 terminals, and refused for legacy rows (whose physical
    occurrence is the callback-target generation).  ``provider_version``
    is caller/provider metadata that selects the pre-status interaction
    plan only; the panel-attested build is what gets recorded, so a legacy
    row with no durable version metadata may omit it.
    """

    operation_id: str = Field(
        min_length=1,
        description="Explicit canonical UUID for crash/retry truth; an exact "
        "retry with the same id adopts the recorded evidence",
    )
    generation: Optional[str] = Field(
        default=None,
        description="Expected model generation of a managed terminal; omit for legacy rows",
    )
    provider_version: Optional[str] = Field(
        default=None,
        description=(
            "Installed provider build (a banner or a bare semver) that selects the "
            "interaction plan; the panel-attested build is what is recorded"
        ),
    )
    physical_occurrence: Optional[str] = Field(
        default=None,
        description=(
            "The durable physical identity of the terminal (its callback-target "
            "generation for a legacy row, or its model generation for a managed "
            "row). Required for legacy rows and bound into the operation identity."
        ),
    )


#: HTTP mapping for the repair's typed refusal reasons.  Everything not
#: listed defaults to 409 (a conflict the caller may not silently retry
#: against different state); transient persistence failures are 503.
_REPAIR_REFUSED_HTTP: dict[str, int] = {
    "invalid-input": status.HTTP_400_BAD_REQUEST,
    "provider-unsupported": status.HTTP_400_BAD_REQUEST,
    "unsupported-build": status.HTTP_400_BAD_REQUEST,
    "generation-required": status.HTTP_400_BAD_REQUEST,
    "physical-occurrence-required": status.HTTP_400_BAD_REQUEST,
    "version-drift": status.HTTP_409_CONFLICT,
    "binding-unreadable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "terminal-not-found": status.HTTP_404_NOT_FOUND,
    "no-roster-incarnation": status.HTTP_404_NOT_FOUND,
    "roster-unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "persistence-failed": status.HTTP_503_SERVICE_UNAVAILABLE,
    "attachment-unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "attachment-reconcile": status.HTTP_409_CONFLICT,
    # cond-0427.  Listed explicitly rather than left to the 409 default, so
    # the choice is reviewable: 409 and deliberately NOT 503.  The operation
    # is at-most-once under one operation id, and an exact retry answers
    # ``observation-attempt-ambiguous`` rather than re-sending /status, so a
    # 503 would invite a retry this endpoint will never honour.
    "submission-unproven": status.HTTP_409_CONFLICT,
}


@router.post("/roster/terminals/{terminal_id}/native-identity-repair")
async def repair_terminal_native_identity(
    terminal_id: str,
    body: NativeIdentityRepairBody,
    _: Any = _WRITE,
) -> Any:
    """Repair one currently live rostered terminal's missing native session id.

    The bounded cond-0377C health operation: proves the exact stored
    pane/session/window/process identity live, types literal ``/status``
    once under the canonical lifecycle claim set (model-generation,
    callback-target-generation, pane) and the per-pane input lease, parses
    only the panel-attested branded provider/build identity fields, and
    commits the terminal row, the roster lineage, and a bounded evidence
    digest atomically with an exclusive native-session attachment owner.
    Never a task/control message.

    The typed outcome is returned as the body: ``repaired``,
    ``already-known``, and ``identity-still-missing`` (Kimi, before its
    first session-creating action) map to 200; refusals map to their typed
    HTTP code.  The body never contains raw pane output or secrets — only
    the bounded digest and typed details.
    """

    def _run() -> dict[str, Any]:
        return native_status_repair.repair_terminal_native_identity(
            terminal_id=terminal_id,
            generation=body.generation,
            provider_version=body.provider_version,
            physical_occurrence=body.physical_occurrence,
            operation_id=body.operation_id,
        )

    outcome = await asyncio.to_thread(_run)
    if outcome.get("status") in (
        native_status_repair.STATUS_REPAIRED,
        native_status_repair.STATUS_ALREADY_KNOWN,
        native_status_repair.STATUS_IDENTITY_STILL_MISSING,
    ):
        return outcome
    if outcome.get("status") == native_status_repair.STATUS_ERRORED:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=outcome,
        )
    code = _REPAIR_REFUSED_HTTP.get(outcome.get("reason") or "", status.HTTP_409_CONFLICT)
    # The typed outcome is the body at the mapped HTTP code (never wrapped
    # in a generic error envelope), so a caller branches on ``reason``
    # without re-deciding what happened here.
    return JSONResponse(status_code=code, content=outcome)


# ---------------------------------------------------------------------------
# cond-0377D: live legacy audit, one-candidate migration, capability reads
# ---------------------------------------------------------------------------


class LegacyMigrationBody(BaseModel):
    """The explicit one-candidate migration request (cond-0377D).

    The caller supplies a stable migration ``operation_id`` and the exact
    audit occurrence/candidate/digest plus the exact terminal occurrence/
    provider/build facts it observed.  The intent is persisted before any
    repair interaction; exact retries query-adopt the same migration and
    repair operations; a changed request under the same id conflicts; a
    response loss without adoptable evidence is typed ambiguous/unresolved
    and never resent.  Role/profile are never accepted here — they come
    only from the authoritative roster agent row.
    """

    operation_id: str = Field(
        min_length=1,
        description="Explicit canonical UUID for crash/retry truth; an exact "
        "retry with the same id query-adopts the recorded outcome",
    )
    terminal_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    generation: Optional[str] = Field(
        default=None,
        description="Exact expected model generation of a managed terminal; omit for legacy rows",
    )
    physical_occurrence: Optional[str] = Field(
        default=None,
        description="The durable physical occurrence (callback-target generation for a "
        "legacy row, or the model generation for a managed row) the audit observed",
    )
    provider_version: Optional[str] = Field(
        default=None,
        description="Installed provider build the audit observed; selects the interaction plan only",
    )
    audit_occurrence_id: str = Field(min_length=1)
    audit_candidate_digest: str = Field(min_length=64, max_length=64)


#: HTTP mapping for the migration's typed refusal reasons.  Terminal outcome
#: statuses map to 200; everything not listed defaults to 409; transient
#: persistence failures are 503.
_MIGRATION_REFUSED_HTTP: dict[str, int] = {
    "invalid-input": status.HTTP_400_BAD_REQUEST,
    "unsupported-provider": status.HTTP_400_BAD_REQUEST,
    "missing-occurrence": status.HTTP_400_BAD_REQUEST,
    "producer-disabled": status.HTTP_409_CONFLICT,
    "operation-conflict": status.HTTP_409_CONFLICT,
    "candidate-drift": status.HTTP_409_CONFLICT,
    "provider-drift": status.HTTP_409_CONFLICT,
    "generation-mismatch": status.HTTP_409_CONFLICT,
    "occurrence-mismatch": status.HTTP_409_CONFLICT,
    "seam-drift": status.HTTP_409_CONFLICT,
    "repair-attempt-ambiguous": status.HTTP_409_CONFLICT,
    "repair-attempt-unresolved": status.HTTP_409_CONFLICT,
    "in-progress": status.HTTP_409_CONFLICT,
    "missing-agent": status.HTTP_409_CONFLICT,
    "terminal-not-found": status.HTTP_404_NOT_FOUND,
    "no-roster-incarnation": status.HTTP_404_NOT_FOUND,
    "roster-unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "attachment-unreadable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "binding-unreadable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "persistence-unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
}


@router.get("/roster/legacy-audit")
async def roster_legacy_live_audit(_: Any = _READ) -> dict[str, Any]:
    """The truthful read-only live legacy audit (cond-0377D).

    Read-only: classifies every currently live terminal row as an eligible
    migration candidate or a typed refusal (dead, ambiguous,
    missing-occurrence, unreadable, corrupt, unsupported, known-id, retired,
    conflicting-owner, missing-agent, …) using live pane/server/process
    observations — never a DB row alone.  A missing-ID Kimi candidate is
    eligible with its session state truthfully unknown before the bounded
    probe (/status never creates a session).  No bytes are typed, no
    provider session is initialized, no self-healing metadata reader is
    called, and nothing is written.
    """

    def _audit() -> dict[str, Any]:
        return legacy_identity_migration.run_live_legacy_audit()

    try:
        return await asyncio.to_thread(_audit)
    except Exception as exc:  # noqa: BLE001 - the audit never crashes by contract
        logger.exception("live legacy audit failed unexpectedly")
        raise HTTPException(
            status_code=500, detail="the live legacy audit failed unexpectedly"
        ) from exc


@router.post("/roster/legacy-migrations")
async def migrate_legacy_terminal_identity(
    body: LegacyMigrationBody,
    _: Any = _WRITE,
) -> Any:
    """The explicit opt-in one-candidate migration coordinator (cond-0377D).

    Consumes exactly one eligible audit candidate and invokes the exact
    cond-0377C repair operation under a repair operation derived from the
    migration operation id, so response loss queries the same repair
    operation and never triggers a second status interaction.  The read-only
    audit endpoint is not a migration switch; this write endpoint is the
    only producer.

    Typed outcomes: ``migrated``, ``already-known``, and
    ``identity-still-missing`` map to 200; refusals map to their typed HTTP
    code.  The body never contains raw pane output or secrets.
    """

    def _run() -> dict[str, Any]:
        return legacy_identity_migration.migrate_terminal_native_identity(
            operation_id=body.operation_id,
            terminal_id=body.terminal_id,
            provider=body.provider,
            generation=body.generation,
            physical_occurrence=body.physical_occurrence,
            provider_version=body.provider_version,
            audit_occurrence_id=body.audit_occurrence_id,
            audit_candidate_digest=body.audit_candidate_digest,
        )

    outcome = await asyncio.to_thread(_run)
    if outcome.get("status") in (
        legacy_identity_migration.MIGRATION_MIGRATED,
        legacy_identity_migration.MIGRATION_ALREADY_KNOWN,
        legacy_identity_migration.MIGRATION_IDENTITY_STILL_MISSING,
    ):
        return outcome
    if outcome.get("status") == legacy_identity_migration.MIGRATION_ERRORED:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=outcome,
        )
    code = _MIGRATION_REFUSED_HTTP.get(outcome.get("reason") or "", status.HTTP_409_CONFLICT)
    return JSONResponse(status_code=code, content=outcome)


@router.get("/roster/provider-capabilities")
async def roster_provider_capabilities(_: Any = _READ) -> dict[str, Any]:
    """The versioned truthful provider capability read (cond-0377D).

    One cell per provider (Claude Code, Codex, Kimi, Muse; DeepSeek/Z.ai are
    Claude route provenance) distinguishing durable build identity from
    parser/interaction-plan support and reporting the installed live canary
    receipt state.  Static parser support without a matching live receipt is
    never reported green.
    """

    def _cells() -> dict[str, Any]:
        return provider_capabilities.provider_capability_cells()

    return await asyncio.to_thread(_cells)


@router.get("/roster/pane-identity")
async def roster_pane_identity(
    pane_id: str = Query(min_length=1, description="The immutable tmux pane id to resolve"),
    server_socket_path: str = Query(
        min_length=1,
        description="The canonical identity of the tmux server the caller observed "
        "the pane on; the service re-observes and binds it",
    ),
    _: Any = _READ,
) -> dict[str, Any]:
    """Bounded exact-live-pane identity resolution (cond-0377D M3-A read
    seam).  Resolves one exact live tmux pane to its registered CAO
    terminal, unique LIVE stable-agent incarnation, and stable
    agent/lineage identity.

    Read-scoped and strictly read-only: nothing is mutated and no write
    lease is taken.  Only the exact physical pane facts are accepted —
    a caller-supplied terminal id or environment label never overrides the
    pane mapping.  Outcomes are the versioned closed set ``resolved`` or
    ``pane-unreadable-or-dead`` / ``pane-unregistered`` /
    ``terminal-pane-mismatch-or-superseded`` /
    ``roster-incarnation-missing`` /
    ``roster-incarnation-ambiguous-or-invalid`` (all 200: absence and
    ambiguity are normal typed answers, never guessed identities).

    This is deterministic cooperative-local routing, not a security gate:
    it does not authenticate the human or process that asked the question.
    """

    def _lookup() -> dict[str, Any]:
        return pane_identity_resolution.resolve_pane_identity(
            pane_id=pane_id, server_socket_path=server_socket_path
        )

    try:
        return await asyncio.to_thread(_lookup)
    except Exception as exc:  # noqa: BLE001 - the resolver never crashes by contract
        logger.exception("pane identity resolution failed unexpectedly")
        raise HTTPException(
            status_code=500, detail="pane identity resolution failed unexpectedly"
        ) from exc

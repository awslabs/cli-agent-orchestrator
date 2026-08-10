"""Truthful live legacy identity audit and explicit opt-in one-candidate
migration (cond-0377D).

The stable-agent roster audit (``stable_agent_roster.audit_dry_run``) is the
DB-only dry-run surface; this module adds the LIVE, read-only audit that
classifies currently live legacy terminals that may need enrollment (rows
with no native session id), and the explicit write coordinator that consumes
exactly one eligible candidate and invokes PR #99's exact repair operation.

Identity model
==============

The audit binds each candidate to the repository's own identity primitives —
never a parallel identity:

* terminal id plus the exact model generation, or the authoritative legacy
  callback-target generation (the physical occurrence) when the generation
  is absent.  The legacy occurrence split is preserved: the callback-target
  generation is the physical occurrence while the roster incarnation
  generation stays ``NULL``; the migration never writes the callback
  occurrence into the roster generation and never keys an operation by
  terminal id alone.
* CAO session, stable agent/roster incarnation presence, and the
  authoritative role/profile provenance from the roster agent row — never
  inferred from a profile or from caller input.  A candidate whose roster
  agent row is missing has no authoritative role/profile mapping and is
  refused explicitly.
* provider/harness and the pinned durable build provenance (the repair's own
  plan catalog).
* pane/server/process observability with the exact live start marker.
  Liveness is never inferred from a DB row or pane existence alone: it binds
  the DB lifecycle state, the exact stored/live tmux tuple, and PID
  start-marker equality; unreadable, dead, ambiguous, or mismatched
  observations refuse explicitly.
* native identity state (terminal row, roster lineage, managed-v2 binding)
  and the current attachment owner/state.
* a closed eligibility/refusal/unresolved reason, the audit schema/version,
  an audit occurrence id, and a canonical evidence digest of the bounded
  candidate facts, suitable for binding one later migration request.

The audit is strictly read-only: it never types bytes, never initializes a
provider session, never reserves an attachment, never calls self-healing
metadata readers (``get_terminal_metadata``), and never persists an audit
receipt.

Migration coordinator
=====================

``migrate_terminal_native_identity`` consumes exactly one eligible audit
candidate.  The intent row (with the audit digest and the deterministic
repair operation id) is persisted BEFORE any repair interaction, and a
durable ``attempt-started`` marker is persisted before the first ``/status``
byte.  Response loss is resolved by the explicit operation ids: an exact
retry query-adopts the same migration and repair operations; a retry that
finds no adoptable repair evidence is typed ambiguous/unresolved and NEVER
resends ``/status``.  The candidate is revalidated from current facts before
invoking the repair and again at the irreversible persistence seam.  The
repair service is called, never reimplemented.

No task input is replayed, no fresh conversation is created, no pane is
reincarnated, no task/role/profile changes, Kimi's lazy session is never
auto-created, a known native id is never overwritten, and no legacy row is
deleted or down-migrated.  Rollback (``CAO_LEGACY_MIGRATION_PRODUCER_ENABLED=0``)
disables only NEW operation production; already-started operations remain
queryable/adoptable and every additive row is retained.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import native_attachment
from cli_agent_orchestrator.services import native_status_repair as nsr
from cli_agent_orchestrator.services import stable_agent_roster as roster
from cli_agent_orchestrator.services.control_input_contract import normalize_server_identity
from cli_agent_orchestrator.services.provider_contracts import normalized_version

logger = logging.getLogger(__name__)

LEGACY_AUDIT_SCHEMA = "cao-m3-legacy-audit-v1"
MIGRATION_SCHEMA = "cao-m3-legacy-migration-v1"
MIGRATION_BATCH_SCHEMA = "cao-m3-legacy-migration-batch-v1"

#: Candidate classification: eligible, or one closed refusal reason.
CANDIDATE_ELIGIBLE = "eligible"
REFUSAL_UNSUPPORTED_PROVIDER = "unsupported-provider"
REFUSAL_TERMINAL_NOT_LIVE = "terminal-not-live"
REFUSAL_MISSING_OCCURRENCE = "missing-occurrence"
REFUSAL_UNREADABLE = "unreadable"
REFUSAL_CORRUPT = "corrupt"
REFUSAL_DEAD = "dead"
REFUSAL_UNKNOWN_LIVENESS = "unknown-liveness"
REFUSAL_PANE_DRIFT = "pane-identity-drift"
REFUSAL_SERVER_DRIFT = "server-identity-drift"
REFUSAL_PROCESS_UNOBSERVABLE = "process-unobservable"
REFUSAL_PROCESS_DRIFT = "process-identity-drift"
REFUSAL_NO_ROSTER = "no-roster-incarnation"
REFUSAL_ROSTER_UNAVAILABLE = "roster-unavailable"
REFUSAL_ALREADY_RETIRED = "already-retired"
REFUSAL_INCARNATION_NOT_LIVE = "incarnation-not-live"
REFUSAL_AMBIGUOUS = "ambiguous"
REFUSAL_ALREADY_KNOWN = "already-known"
REFUSAL_IDENTITY_CONFLICT = "identity-conflict"
REFUSAL_CONFLICTING_OWNER = "conflicting-owner"
REFUSAL_ATTACHMENT_UNREADABLE = "attachment-unreadable"
REFUSAL_BINDING_UNREADABLE = "binding-unreadable"
#: No authoritative role/profile provenance: the roster agent row is missing.
REFUSAL_MISSING_AGENT = "missing-agent"

#: Migration operation outcomes (the migration row status vocabulary).
MIGRATION_PENDING = "pending"
MIGRATION_ATTEMPT_STARTED = "attempt-started"
MIGRATION_MIGRATED = "migrated"
MIGRATION_ALREADY_KNOWN = "already-known"
MIGRATION_IDENTITY_STILL_MISSING = "identity-still-missing"
MIGRATION_REFUSED = "refused"
MIGRATION_ERRORED = "errored"

#: Migration refusal reasons of its own (the rest reuse the audit refusal
#: vocabulary and the repair refusal vocabulary).
MIGRATION_REFUSED_OPERATION_CONFLICT = "operation-conflict"
MIGRATION_REFUSED_PRODUCER_DISABLED = "producer-disabled"
MIGRATION_REFUSED_CANDIDATE_DRIFT = "candidate-drift"
MIGRATION_REFUSED_PROVIDER_DRIFT = "provider-drift"
MIGRATION_REFUSED_GENERATION_MISMATCH = "generation-mismatch"
MIGRATION_REFUSED_OCCURRENCE_MISMATCH = "occurrence-mismatch"
MIGRATION_REFUSED_SEAM_DRIFT = "seam-drift"
#: Response loss without adoptable repair evidence: the repair attempt
#: started but never produced adoptable evidence; never resent.
MIGRATION_REFUSED_REPAIR_AMBIGUOUS = "repair-attempt-ambiguous"
MIGRATION_REFUSED_REPAIR_UNRESOLVED = "repair-attempt-unresolved"
#: Another executor holds the operation (the atomic execution claim was
#: lost); this caller performed no repair interaction.
MIGRATION_REFUSED_IN_PROGRESS = "in-progress"

#: Durable provider-build provenance vocabulary: where the build fact came
#: from.  ``managed-v2-binding`` is a durable binding fact; the legacy plan
#: fallback is a static plan pin and is never described as observed proof.
BUILD_PROVENANCE_SOURCE_BINDING = "managed-v2-binding"
BUILD_PROVENANCE_SOURCE_PLAN_FALLBACK = "pinned-legacy-plan-fallback"

#: Immutable terminal migration states: an exact retry query-adopts them.
_TERMINAL_MIGRATION_STATUSES = frozenset(
    {MIGRATION_MIGRATED, MIGRATION_ALREADY_KNOWN, MIGRATION_IDENTITY_STILL_MISSING}
)
#: Every state that will never re-run under the same operation id.
_FINAL_MIGRATION_STATUSES = frozenset(
    _TERMINAL_MIGRATION_STATUSES | {MIGRATION_REFUSED, MIGRATION_ERRORED}
)

#: Fixed namespace for the deterministic repair operation derived from a
#: migration operation id, so response loss queries the same repair op.
_REPAIR_OP_NAMESPACE = uuid.UUID("03770000-0000-4000-8000-000000000077")

#: The bounded candidate fact set the evidence digest binds.  Excludes every
#: time-varying or run-varying field (timestamps, observation verdicts,
#: occurrence ids) so the digest reproduces from current facts at migration
#: time — a changed terminal/roster/pane/identity/attachment fact changes it.
_CANDIDATE_DIGEST_FIELDS = (
    "terminal_id",
    "vintage",
    "managed",
    "generation",
    "physical_occurrence",
    "provider",
    "session_name",
    "pane_id",
    "window_id",
    "session_id",
    "server_socket_path",
    "pane_pid",
    "process_identity",
    "agent_id",
    "agent_role",
    "agent_profile_family",
    "lineage_id",
    "incarnation_id",
    "incarnation_disposition",
    "terminal_native_session_id",
    "lineage_native_session_id",
    "binding_native_session_id",
    "attachment_state",
    "attachment_owner_generation",
    "attachment_native_session_id",
    "build_provenance",
)

_MIGRATION_REQUEST_KEYS = (
    "operation_id",
    "terminal_id",
    "provider",
    "generation",
    "physical_occurrence",
    "provider_version",
    "audit_occurrence_id",
    "audit_candidate_digest",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _bounded(detail: str) -> str:
    detail = (detail or "").strip()
    return detail if len(detail) <= 500 else detail[:500] + "…"


def _live_start_marker(pid: int) -> Optional[str]:
    """The pid's current start marker through the exact stored-marker
    producer (``ps -o lstart=``), so the comparison is format-identical."""
    from cli_agent_orchestrator.services.native_attachment_recovery import (
        _live_start_marker as _observed_live_start_marker,
    )

    try:
        return _observed_live_start_marker(pid)
    except Exception:  # noqa: BLE001 - evidence is best-effort by definition
        return None


def candidate_evidence_digest(facts: Mapping[str, Any]) -> str:
    """The sha256 digest binding one audit candidate to one migration request.

    Deterministic over the closed ``_CANDIDATE_DIGEST_FIELDS`` subset of the
    candidate record, so the migration coordinator recomputes it from current
    facts and refuses when it no longer matches the audited value.
    """
    payload = {key: facts.get(key) for key in _CANDIDATE_DIGEST_FIELDS}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def migration_request_digest(
    *,
    terminal_id: str,
    provider: str,
    generation: Optional[str],
    physical_occurrence: Optional[str],
    provider_version: Optional[str],
    audit_occurrence_id: str,
    audit_candidate_digest: str,
) -> str:
    """The deterministic digest of the immutable migration request facts.

    Version spelling differences normalize away; a genuinely different
    terminal, provider, occurrence, audit binding, or build is a different
    request under the same operation id.
    """
    return hashlib.sha256(
        "\x00".join(
            (
                terminal_id,
                provider,
                generation or "",
                physical_occurrence or "",
                normalized_version(provider_version) if provider_version else "",
                audit_occurrence_id,
                audit_candidate_digest,
            )
        ).encode("utf-8")
    ).hexdigest()


def migration_producer_enabled() -> bool:
    """Whether the migration producer accepts NEW operations.

    Rollback switch: ``CAO_LEGACY_MIGRATION_PRODUCER_ENABLED=0`` disables new
    operation production only.  Already-started operations remain
    queryable/adoptable and every additive row is retained for roll-forward.
    """
    return os.environ.get("CAO_LEGACY_MIGRATION_PRODUCER_ENABLED", "1").strip() not in (
        "0",
        "false",
        "False",
    )


def _repair_operation_id(migration_operation_id: str) -> str:
    """The deterministic repair operation for one migration operation, so a
    response-lost retry queries the SAME repair operation (whose own
    idempotency guarantees no second status interaction)."""
    return str(uuid.uuid5(_REPAIR_OP_NAMESPACE, migration_operation_id))


# ---------------------------------------------------------------------------
# The read-only live audit
# ---------------------------------------------------------------------------


def _incarnation_row_for(session: Any, terminal_id: str, generation: Optional[str]) -> Any:
    """The exact (terminal_id, generation) incarnation row, or None."""
    query = session.query(database.StableAgentIncarnationModel).filter(
        database.StableAgentIncarnationModel.terminal_id == terminal_id
    )
    if generation is None:
        query = query.filter(database.StableAgentIncarnationModel.generation.is_(None))
    else:
        query = query.filter(database.StableAgentIncarnationModel.generation == generation)
    return query.one_or_none()


def _observe_live_pane_facts(
    *,
    pane_id: str,
    window_id: str,
    session_id: str,
    server_socket_path: str,
    pane_pid: int,
) -> dict[str, Any]:
    """Read-only pane/server/process observation for the audit.

    Every failure is a typed classification, never an exception: an
    unobservable pane is ``unknown-liveness`` (never inferred dead), an
    observed-dead pane is ``dead``, a moved/recycled tuple is
    ``pane-identity-drift``, an unprovable or different server is
    ``server-identity-drift``, and an unreadable process marker is
    ``process-unobservable``.  No bytes are ever typed.
    """
    from cli_agent_orchestrator.clients.tmux import TmuxClient

    def _result(
        classification: Optional[str], detail: Optional[str], **facts: Any
    ) -> dict[str, Any]:
        return {
            "classification": classification,
            "detail": detail,
            "pane_live": facts.get("pane_live"),
            "server_live": facts.get("server_live"),
            "process_live": facts.get("process_live"),
            "live_start_marker": facts.get("live_start_marker"),
        }

    try:
        client = TmuxClient()
        live = client.pane_control_identity(pane_id=pane_id)
    except Exception as exc:  # noqa: BLE001 - an unobservable pane is a refusal
        logger.warning("legacy audit: pane identity observation failed: %s", exc)
        return _result(REFUSAL_UNKNOWN_LIVENESS, "the pane's live identity could not be observed")
    if live is None:
        return _result(
            REFUSAL_UNKNOWN_LIVENESS,
            f"pane {pane_id} is not provably on the tmux server this process reaches",
        )
    if live.dead:
        return _result(REFUSAL_DEAD, f"pane {pane_id} is observed dead")
    if (live.pane_id, live.window_id, live.session_id, live.pane_pid) != (
        pane_id,
        window_id,
        session_id,
        pane_pid,
    ):
        return _result(
            REFUSAL_PANE_DRIFT,
            "the live pane tuple does not match the stored tuple; the pane moved or "
            "was recycled",
        )
    try:
        server = client.observe_pane_server_identity(pane_id)
    except Exception as exc:  # noqa: BLE001 - an unobservable server is a refusal
        logger.warning("legacy audit: server identity observation failed: %s", exc)
        return _result(REFUSAL_SERVER_DRIFT, "the pane's server identity could not be observed")
    if server is None:
        return _result(
            REFUSAL_SERVER_DRIFT,
            f"pane {pane_id} could not be proven to sit on the bound tmux server",
        )
    if normalize_server_identity(server_socket_path) != server:
        return _result(REFUSAL_SERVER_DRIFT, f"pane {pane_id} sits on a different tmux server")
    marker = _live_start_marker(pane_pid)
    if marker is None:
        return _result(
            REFUSAL_PROCESS_UNOBSERVABLE, f"the start marker of pid {pane_pid} could not be read"
        )
    return _result(
        None,
        None,
        pane_live=True,
        server_live=True,
        process_live=True,
        live_start_marker=marker,
    )


def _audit_row_dicts(session: Any) -> list[dict[str, Any]]:
    """Every terminal row (v2 vintage first, then the shared table) through
    the public occurrence-snapshot seam."""
    rows: list[dict[str, Any]] = []
    for model in (database.ManagedLaunchV2TerminalModel, database.TerminalModel):
        for row in session.query(model).all():
            snapshot = nsr.terminal_occurrence_snapshot(row.id, db=session)
            if snapshot is not None:
                rows.append(snapshot)
    return rows


def _classify_terminal(session: Any, row: Mapping[str, Any], occurrence_id: str) -> dict[str, Any]:
    """Read-only classification of one terminal row for the legacy audit.

    Never mutates, never types bytes, never initializes a provider session,
    never reserves an attachment, never calls self-healing metadata readers,
    and never persists an audit receipt.  Every failure mode is a typed
    refusal with a closed reason; an eligible candidate carries the exact
    evidence — terminal + generation/occurrence, CAO session and roster
    presence with authoritative role/profile provenance, pane/server/process
    observability, native identity state, attachment state — plus its
    binding digest.
    """
    terminal_id = row["id"]
    provider = row["provider"]
    managed = row["generation"] is not None
    candidate: dict[str, Any] = {
        "terminal_id": terminal_id,
        "vintage": row["vintage"],
        "managed": managed,
        "generation": row["generation"],
        "physical_occurrence": (
            row["generation"] if managed else row["callback_target_generation"]
        ),
        "provider": provider,
        "session_name": row["tmux_session"],
        "pane_id": row["pane_id"],
        "window_id": row["window_id"],
        "session_id": row["session_id"],
        "server_socket_path": row["server_socket_path"],
        "pane_pid": row["pane_pid"],
        "process_identity": None,
        "pane_live": None,
        "server_live": None,
        "process_live": None,
        "agent_id": None,
        "agent_role": None,
        "agent_profile_family": None,
        "lineage_id": None,
        "incarnation_id": None,
        "incarnation_disposition": None,
        "terminal_native_session_id": row["native_session_id"],
        "lineage_native_session_id": None,
        "binding_native_session_id": None,
        "attachment_state": None,
        "attachment_owner_terminal_id": None,
        "attachment_owner_generation": None,
        "attachment_native_session_id": None,
        #: Truthful pre-probe session state for providers whose panel must be
        #: observed to learn the session (Kimi's session is lazily created by
        #: real task work; /status never creates one).
        "session_probe_required": provider == "kimi_cli",
        #: Durable provider-build provenance: a durable managed binding fact,
        #: or an explicit unaudited pinned-legacy-plan fallback.  Never
        #: described as observed build proof.
        "build_provenance": {
            "source": BUILD_PROVENANCE_SOURCE_PLAN_FALLBACK,
            "observed": False,
            "provider_version": None,
            "plan_pin": None,
        },
        "occurrence_id": occurrence_id,
        "classification": CANDIDATE_ELIGIBLE,
        "reason": None,
        "observed_at": _now(),
    }

    def refused(reason: str, detail: str) -> dict[str, Any]:
        candidate["classification"] = reason
        candidate["reason"] = _bounded(detail)
        candidate["evidence_digest"] = candidate_evidence_digest(candidate)
        return candidate

    if provider not in nsr.repair_parser_plans():
        return refused(
            REFUSAL_UNSUPPORTED_PROVIDER,
            f"provider {provider!r} has no pinned native /status repair parser",
        )
    plan = nsr.repair_parser_plans()[provider]
    candidate["build_provenance"]["plan_pin"] = plan["supported_versions"][0]
    if row["lifecycle_state"] != "live":
        return refused(
            REFUSAL_TERMINAL_NOT_LIVE,
            f"terminal {terminal_id} is {row['lifecycle_state']!r}, not live",
        )
    if not candidate["physical_occurrence"]:
        return refused(
            REFUSAL_MISSING_OCCURRENCE,
            f"terminal {terminal_id} has no durable physical occurrence to bind a " "migration to",
        )
    if row["vintage"] == "v2":
        # The managed-v2 reservation binding is a durable, authoritative
        # native-identity fact: a valid binding already records the session,
        # so a v2 terminal is never an enrollment candidate (the repair
        # endpoint fills missing projections); an unusable binding cannot be
        # repaired and is refused explicitly.
        try:
            binding = nsr.managed_binding_snapshot(
                session,
                terminal_id=terminal_id,
                model_generation=row["generation"],
                provider=provider,
            )
        except nsr.NativeStatusRepairError as exc:
            return refused(
                REFUSAL_BINDING_UNREADABLE,
                f"the managed-v2 binding cannot be used: {_bounded(str(exc))}",
            )
        if binding is None:  # pragma: no cover - require_binding=True never returns None
            return refused(REFUSAL_BINDING_UNREADABLE, "the managed-v2 binding could not be read")
        candidate["binding_native_session_id"] = binding["native_session_id"]
        candidate["build_provenance"] = {
            "source": BUILD_PROVENANCE_SOURCE_BINDING,
            "observed": True,
            "provider_version": binding["provider_version"],
            "plan_pin": plan["supported_versions"][0],
        }
        return refused(
            REFUSAL_ALREADY_KNOWN,
            "the managed-v2 reservation binding already records the native session identity",
        )

    pane_id = row["pane_id"]
    window_id = row["window_id"]
    tmux_session_id = row["session_id"]
    server_socket_path = row["server_socket_path"]
    pane_pid = row["pane_pid"]
    if not (
        pane_id
        and window_id
        and tmux_session_id
        and server_socket_path
        and isinstance(pane_pid, int)
        and pane_pid > 0
    ):
        return refused(
            REFUSAL_UNREADABLE,
            "the terminal row does not carry the complete exact pane/session/window/"
            "process tuple, so nothing can be proven about the pane",
        )
    obs = _observe_live_pane_facts(
        pane_id=pane_id,
        window_id=window_id,
        session_id=tmux_session_id,
        server_socket_path=server_socket_path,
        pane_pid=pane_pid,
    )
    candidate["pane_live"] = obs["pane_live"]
    candidate["server_live"] = obs["server_live"]
    candidate["process_live"] = obs["process_live"]
    if obs["classification"] is not None:
        return refused(obs["classification"], obs["detail"] or obs["classification"])

    try:
        incarnation = roster.get_incarnation_by_terminal(
            terminal_id, generation=row["generation"], db=session
        )
    except roster.StableAgentConflict as exc:
        return refused(REFUSAL_AMBIGUOUS, str(exc))
    except roster.StableAgentError as exc:
        logger.warning("legacy audit %s: roster read failed: %s", occurrence_id, exc)
        return refused(REFUSAL_ROSTER_UNAVAILABLE, "the roster could not be read")
    if incarnation is None:
        exact = _incarnation_row_for(session, terminal_id, row["generation"])
        if exact is not None:
            if exact.disposition == roster.INCARNATION_RETIRED:
                return refused(
                    REFUSAL_ALREADY_RETIRED,
                    f"incarnation {exact.incarnation_id} of terminal {terminal_id} is retired",
                )
            return refused(
                REFUSAL_INCARNATION_NOT_LIVE,
                f"incarnation {exact.incarnation_id} of terminal {terminal_id} is "
                f"{exact.disposition!r}, not live",
            )
        return refused(
            REFUSAL_NO_ROSTER,
            f"no stable-agent incarnation is recorded for terminal {terminal_id} "
            "for this occurrence",
        )
    if incarnation["disposition"] == roster.INCARNATION_RETIRED:
        return refused(
            REFUSAL_ALREADY_RETIRED,
            f"incarnation {incarnation['incarnation_id']} is retired",
        )
    if incarnation["disposition"] not in roster.LIVE_INCARNATION_DISPOSITIONS:
        return refused(
            REFUSAL_INCARNATION_NOT_LIVE,
            f"incarnation {incarnation['incarnation_id']} is "
            f"{incarnation['disposition']!r}, not live",
        )

    candidate["agent_id"] = incarnation["agent_id"]
    candidate["lineage_id"] = incarnation["lineage_id"]
    candidate["incarnation_id"] = incarnation["incarnation_id"]
    candidate["incarnation_disposition"] = incarnation["disposition"]

    # Authoritative role/profile provenance: the stable agent row is the only
    # source.  A missing agent row means no authoritative mapping exists —
    # an explicit refusal, never an inference from profile or caller input.
    agent_row = (
        session.query(database.StableAgentModel)
        .filter(database.StableAgentModel.agent_id == incarnation["agent_id"])
        .one_or_none()
    )
    if agent_row is None:
        return refused(
            REFUSAL_MISSING_AGENT,
            "the roster incarnation names no stable agent; role/profile provenance "
            "is not authoritatively known",
        )
    candidate["agent_role"] = agent_row.role
    candidate["agent_profile_family"] = agent_row.profile_family

    if incarnation["pane_id"] != pane_id or incarnation["pane_pid"] != pane_pid:
        return refused(
            REFUSAL_PANE_DRIFT,
            "the roster incarnation's pane/pid do not match the stored terminal row",
        )
    proc = incarnation.get("process_identity")
    raw_inc = _incarnation_row_for(session, terminal_id, row["generation"])
    raw_json = raw_inc.process_identity_json if raw_inc is not None else None
    if raw_json is not None and not isinstance(proc, Mapping):
        return refused(
            REFUSAL_CORRUPT,
            "the roster incarnation's process identity is present but unreadable",
        )
    if not isinstance(proc, Mapping) or not proc.get("start_marker"):
        return refused(
            REFUSAL_PROCESS_UNOBSERVABLE,
            "the roster incarnation never published a process identity",
        )
    if proc.get("pid") != pane_pid:
        return refused(
            REFUSAL_PANE_DRIFT,
            "the roster incarnation's process pid does not match the stored pane pid",
        )
    candidate["process_identity"] = {"pid": proc["pid"], "start_marker": proc["start_marker"]}
    if obs["live_start_marker"] != proc["start_marker"]:
        return refused(
            REFUSAL_PROCESS_DRIFT,
            "pid is alive but its start marker no longer matches the recorded incarnation",
        )

    lineage = None
    if incarnation.get("lineage_id") is not None:
        lineage = (
            session.query(database.StableAgentLineageModel)
            .filter(database.StableAgentLineageModel.lineage_id == incarnation["lineage_id"])
            .one_or_none()
        )
        if lineage is None:
            return refused(REFUSAL_ROSTER_UNAVAILABLE, "the incarnation names a missing lineage")
        if lineage.harness != provider:
            return refused(
                REFUSAL_IDENTITY_CONFLICT,
                "the lineage belongs to a different harness; native ids never cross "
                "harness domains",
            )
        candidate["lineage_native_session_id"] = lineage.native_session_id

    known = [
        value
        for value in (
            candidate["terminal_native_session_id"],
            candidate["lineage_native_session_id"],
        )
        if value is not None
    ]
    if len(set(known)) > 1:
        return refused(
            REFUSAL_IDENTITY_CONFLICT,
            "the terminal row and the roster lineage know different native session ids",
        )
    if known:
        return refused(
            REFUSAL_ALREADY_KNOWN,
            "the native session identity is already known; migration does not re-enroll "
            "a known identity",
        )

    try:
        attachments = native_attachment.list_attachments(owner_terminal_id=terminal_id)
    except native_attachment.NativeAttachmentError as exc:
        logger.warning("legacy audit %s: attachment listing failed: %s", occurrence_id, exc)
        return refused(REFUSAL_ATTACHMENT_UNREADABLE, "the attachment store could not be read")
    for record in attachments:
        if record["provider"] != provider or record["state"] not in native_attachment.LIVE_STATES:
            continue
        owner = record.get("owner") or {}
        candidate["attachment_state"] = record["state"]
        candidate["attachment_owner_terminal_id"] = owner.get("terminal_id")
        candidate["attachment_owner_generation"] = owner.get("generation")
        candidate["attachment_native_session_id"] = record["native_session_id"]
        if owner.get("generation") == candidate["physical_occurrence"]:
            return refused(
                REFUSAL_ALREADY_KNOWN,
                "a live attachment already records this terminal's native session",
            )
        return refused(
            REFUSAL_CONFLICTING_OWNER,
            "a live attachment claims this terminal under a different occurrence",
        )

    # A missing-ID Kimi pane is an explicit eligible candidate whose session
    # existence is unknown before the bounded probe: /status never creates a
    # session, so the probe is not a synthetic turn.  A pristine pane returns
    # the typed identity-still-missing outcome; a pane that already processed
    # real task work exposes its lazily-created session and is repaired.

    candidate["evidence_digest"] = candidate_evidence_digest(candidate)
    return candidate


def _unclassifiable_candidate(row: Mapping[str, Any], occurrence_id: str) -> dict[str, Any]:
    """A typed fallback when one terminal cannot be classified at all."""
    return {
        "terminal_id": row.get("id"),
        "vintage": row.get("vintage"),
        "managed": row.get("generation") is not None,
        "generation": row.get("generation"),
        "physical_occurrence": row.get("generation") or row.get("callback_target_generation"),
        "provider": row.get("provider"),
        "session_name": row.get("tmux_session"),
        "pane_id": row.get("pane_id"),
        "window_id": row.get("window_id"),
        "session_id": row.get("session_id"),
        "server_socket_path": row.get("server_socket_path"),
        "pane_pid": row.get("pane_pid"),
        "process_identity": None,
        "pane_live": None,
        "server_live": None,
        "process_live": None,
        "agent_id": None,
        "agent_role": None,
        "agent_profile_family": None,
        "lineage_id": None,
        "incarnation_id": None,
        "incarnation_disposition": None,
        "terminal_native_session_id": row.get("native_session_id"),
        "lineage_native_session_id": None,
        "binding_native_session_id": None,
        "attachment_state": None,
        "attachment_owner_terminal_id": None,
        "attachment_owner_generation": None,
        "attachment_native_session_id": None,
        "session_probe_required": row.get("provider") == "kimi_cli",
        "build_provenance": {
            "source": BUILD_PROVENANCE_SOURCE_PLAN_FALLBACK,
            "observed": False,
            "provider_version": None,
            "plan_pin": None,
        },
        "occurrence_id": occurrence_id,
        "classification": REFUSAL_UNREADABLE,
        "reason": "the terminal could not be classified",
        "observed_at": _now(),
    }


def run_live_legacy_audit(db: Any = None) -> dict[str, Any]:
    """The truthful read-only live legacy audit (cond-0377D).

    Enumerates every terminal row (v2 and shared-table vintages) and
    classifies each as an eligible migration candidate or a typed refusal.
    Strictly read-only: no bytes are typed, no provider session is
    initialized, no attachment is reserved, no self-healing metadata reader
    is called, and no audit receipt or roster/evidence/journal state is
    written.  Liveness is never inferred from a row alone: every candidate's
    pane/server/process facts are observed through the same read seams the
    repair uses.
    """

    def _audit(session: Any) -> dict[str, Any]:
        occurrence_id = str(uuid.uuid4())
        rows = _audit_row_dicts(session)
        candidates: list[dict[str, Any]] = []
        for row in rows:
            try:
                candidates.append(_classify_terminal(session, row, occurrence_id))
            except Exception:  # noqa: BLE001 - one unclassifiable row never crashes the audit
                logger.exception(
                    "legacy audit %s: terminal %s could not be classified",
                    occurrence_id,
                    row.get("id"),
                )
                candidates.append(_unclassifiable_candidate(row, occurrence_id))
        eligible = [c for c in candidates if c["classification"] == CANDIDATE_ELIGIBLE]
        return {
            "schema": LEGACY_AUDIT_SCHEMA,
            "occurrence_id": occurrence_id,
            "generated_at": _now(),
            "terminals_total": len(candidates),
            "eligible_count": len(eligible),
            "refusals_count": len(candidates) - len(eligible),
            "candidates": candidates,
        }

    if db is not None:
        return _audit(db)
    with database.SessionLocal() as session:
        return _audit(session)


# ---------------------------------------------------------------------------
# The explicit one-candidate migration coordinator
# ---------------------------------------------------------------------------


class _MigrationRefusal(RuntimeError):
    """A typed refusal raised by migration revalidation (never escapes the
    coordinator; it is converted into the bounded outcome)."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _migration_row(db: Any, operation_id: str) -> Any:
    return (
        db.query(database.LegacyIdentityMigrationModel)
        .filter(database.LegacyIdentityMigrationModel.migration_operation_id == operation_id)
        .one_or_none()
    )


def _read_migration(operation_id: str) -> Optional[Any]:
    with database.SessionLocal() as db:
        return _migration_row(db, operation_id)


def _recorded_outcome(row: Any) -> dict[str, Any]:
    """The bounded recorded outcome of a completed migration operation."""
    try:
        parsed = json.loads(row.outcome_json) if row.outcome_json else None
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict) and parsed.get("schema") == MIGRATION_SCHEMA:
        return parsed
    return {
        "schema": MIGRATION_SCHEMA,
        "status": row.status,
        "reason": row.repair_reason,
        "detail": "the recorded migration outcome is unreadable; the typed columns "
        "still bind the operation",
        "operation_id": row.migration_operation_id,
        "request_digest": row.request_digest,
        "repair_operation_id": row.repair_operation_id,
        "repair_status": row.repair_status,
        "repair_reason": row.repair_reason,
        "terminal_id": row.terminal_id,
        "provider": row.provider,
        "generation": row.generation,
        "physical_occurrence": row.physical_occurrence,
        "provider_version": row.provider_version,
        "build_provenance": None,
        "audit_occurrence_id": row.audit_occurrence_id,
        "audit_candidate_digest": row.audit_candidate_digest,
        "native_session_id": row.native_session_id,
        "evidence_sha256": row.evidence_sha256,
        "parser_key": row.parser_key,
        "attachment": None,
        "task_bytes_submitted": False,
    }


def _persist_migration_intent(
    *,
    operation_id: str,
    request_digest: str,
    terminal_id: str,
    provider: str,
    generation: Optional[str],
    physical_occurrence: Optional[str],
    provider_version: Optional[str],
    audit_occurrence_id: str,
    audit_candidate_digest: str,
    repair_operation_id: str,
) -> bool:
    """Persist the migration intent BEFORE any repair interaction.

    Returns True when the caller may proceed; False when a concurrent exact
    duplicate won the slot and the caller must adopt its verdict.  A changed
    request racing under the same id is never adopted silently — the caller
    re-reads and conflicts on the digest.
    """
    with database.SessionLocal() as db:
        if _migration_row(db, operation_id) is not None:
            return False
        stamp = _now()
        db.add(
            database.LegacyIdentityMigrationModel(
                migration_operation_id=operation_id,
                request_digest=request_digest,
                terminal_id=terminal_id,
                provider=provider,
                generation=generation,
                physical_occurrence=physical_occurrence,
                provider_version=provider_version,
                audit_occurrence_id=audit_occurrence_id,
                audit_candidate_digest=audit_candidate_digest,
                repair_operation_id=repair_operation_id,
                status=MIGRATION_PENDING,
                created_at=stamp,
                updated_at=stamp,
            )
        )
        try:
            db.commit()
            return True
        except Exception:  # noqa: BLE001 - a concurrent duplicate resolves below
            db.rollback()
            if _migration_row(db, operation_id) is None:
                raise
            return False


def _cas_pending_to_attempt_started(operation_id: str) -> bool:
    """The atomic ``pending -> attempt-started`` execution claim.

    Exactly one migration caller may win the transition; every loser returns
    False and must resolve the row (query/adopt or a typed
    in-progress/unresolved outcome) with zero repair invocation.  The
    transition is an UPDATE guarded on the pending status, which is atomic
    under SQLite's single writer.
    """
    with database.SessionLocal() as db:
        updated = (
            db.query(database.LegacyIdentityMigrationModel)
            .filter(
                database.LegacyIdentityMigrationModel.migration_operation_id == operation_id,
                database.LegacyIdentityMigrationModel.status == MIGRATION_PENDING,
            )
            .update(
                {
                    database.LegacyIdentityMigrationModel.status: MIGRATION_ATTEMPT_STARTED,
                    database.LegacyIdentityMigrationModel.updated_at: _now(),
                }
            )
        )
        db.commit()
        return updated == 1


def _record_migration_outcome(
    *, operation_id: str, status: str, outcome: Mapping[str, Any]
) -> None:
    """Record the bounded migration outcome on the intent row (additive)."""
    with database.SessionLocal() as db:
        row = _migration_row(db, operation_id)
        if row is None:
            return  # the intent row vanished; nothing to record against
        row.status = status
        row.repair_status = outcome.get("repair_status")
        row.repair_reason = outcome.get("repair_reason")
        row.native_session_id = outcome.get("native_session_id")
        row.evidence_sha256 = outcome.get("evidence_sha256")
        row.parser_key = outcome.get("parser_key")
        row.outcome_json = json.dumps(dict(outcome), sort_keys=True, separators=(",", ":"))
        row.updated_at = _now()
        db.commit()


def _partial_repair_evidence(
    *, provider: str, terminal_id: str, occurrence: str, repair_operation_id: str
) -> bool:
    """Whether a live attachment records a partial repair for this exact
    operation (a conservative adoption whose receipt names the derived
    repair operation, without a committed repair evidence row)."""
    try:
        records = native_attachment.list_attachments(owner_terminal_id=terminal_id)
    except native_attachment.NativeAttachmentError as exc:
        logger.warning(
            "migration %s: attachment listing failed during attempt resolution: %s",
            repair_operation_id,
            exc,
        )
        return False
    for record in records:
        if record["provider"] != provider or record["state"] not in native_attachment.LIVE_STATES:
            continue
        owner = record.get("owner") or {}
        if owner.get("generation") != occurrence:
            continue
        receipt = record.get("adoption_receipt")
        if isinstance(receipt, dict) and receipt.get("operation_id") == repair_operation_id:
            return True
    return False


def _resolve_existing_row(row: Any, base: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """Resolve an exact-retry against the recorded intent row.

    Returns the bounded outcome to return, or None when the operation is
    still pending and may proceed.  A changed request conflicts; a final
    status query-adopts; an attempt-started row derives completion from the
    repair evidence and, without adoptable evidence, is typed
    ambiguous/unresolved — never resent.
    """
    operation_id = row.migration_operation_id
    if row.request_digest != base["request_digest"]:
        outcome = dict(base)
        outcome.update(
            status=MIGRATION_REFUSED,
            reason=MIGRATION_REFUSED_OPERATION_CONFLICT,
            detail=(
                "the migration operation id is already bound to a different request "
                "digest; a changed request under the same id conflicts before any "
                "provider or roster effect"
            ),
        )
        return outcome
    if row.status in _FINAL_MIGRATION_STATUSES:
        return _recorded_outcome(row)
    if row.status != MIGRATION_ATTEMPT_STARTED:
        return None  # pending: proceed (no /status was ever sent)

    # The repair attempt started.  Adopt only exact, committed truth: the
    # repair evidence first, then the observation-attempt journal (Kimi's
    # identity-still-missing verdict is a journaled terminal outcome because
    # PR #99 writes no normal evidence for it).  A journal that recorded an
    # observation without a committed verdict is ambiguous; a total absence
    # of journal and evidence is unresolved.  Never resent.
    evidence = nsr.repair_outcome_by_operation(row.repair_operation_id)
    occurrence = row.generation if row.generation else row.physical_occurrence
    if evidence is not None and (
        evidence.get("terminal_id") == row.terminal_id
        and evidence.get("provider") == row.provider
        and evidence.get("generation") == occurrence
    ):
        outcome = _outcome_from_evidence(base, evidence)
        _record_migration_outcome(
            operation_id=operation_id, status=MIGRATION_MIGRATED, outcome=outcome
        )
        return outcome
    journal = nsr.repair_observation_attempt(row.repair_operation_id)
    if journal is not None and (
        journal.get("terminal_id") == row.terminal_id
        and journal.get("provider") == row.provider
        and journal.get("generation") == occurrence
    ):
        if journal["status"] == nsr.OBSERVATION_IDENTITY_STILL_MISSING:
            outcome = dict(base)
            outcome.update(
                status=MIGRATION_IDENTITY_STILL_MISSING,
                reason=MIGRATION_IDENTITY_STILL_MISSING,
                detail=(
                    "the exact repair observation already rendered identity-still-missing; "
                    "no session was fabricated and /status will not be resent"
                ),
                repair_status=nsr.STATUS_IDENTITY_STILL_MISSING,
                repair_reason=nsr.STATUS_IDENTITY_STILL_MISSING,
            )
            _record_migration_outcome(
                operation_id=operation_id,
                status=MIGRATION_IDENTITY_STILL_MISSING,
                outcome=outcome,
            )
            return outcome
        # observed/attempted without a committed verdict: bytes were sent,
        # no evidence exists — conservative ambiguous.
        outcome = dict(base)
        outcome.update(
            status=MIGRATION_REFUSED,
            reason=MIGRATION_REFUSED_REPAIR_AMBIGUOUS,
            detail=(
                "the repair observation for this operation was already attempted but "
                "no committed evidence exists; this operation will never resend "
                "/status"
            ),
        )
        return outcome
    if _partial_repair_evidence(
        provider=row.provider,
        terminal_id=row.terminal_id,
        occurrence=occurrence,
        repair_operation_id=row.repair_operation_id,
    ):
        outcome = dict(base)
        outcome.update(
            status=MIGRATION_REFUSED,
            reason=MIGRATION_REFUSED_REPAIR_AMBIGUOUS,
            detail=(
                "the repair attempt started but only a partial adoption is recorded; "
                "no committed repair evidence exists.  Resolve via the repair endpoint "
                f"with repair operation {row.repair_operation_id}, which adopts the "
                "exact evidence without a second /status"
            ),
        )
        return outcome
    outcome = dict(base)
    outcome.update(
        status=MIGRATION_REFUSED,
        reason=MIGRATION_REFUSED_REPAIR_UNRESOLVED,
        detail=(
            "the repair attempt started but no adoptable repair evidence exists; "
            "this operation will never resend /status.  Retry only after the state "
            "is resolved"
        ),
    )
    return outcome


def _revalidate_migration_candidate(
    *,
    terminal_id: str,
    provider: str,
    generation: Optional[str],
    physical_occurrence: Optional[str],
    audit_candidate_digest: str,
    operation_id: str,
) -> dict[str, Any]:
    """Revalidate the exact candidate from CURRENT facts before any repair
    interaction.  Raises :class:`_MigrationRefusal` on any drift."""
    with database.SessionLocal() as db:
        row = nsr.terminal_occurrence_snapshot(terminal_id, db=db)
        if row is None:
            raise _MigrationRefusal(
                "terminal-not-found", f"no terminal row is recorded for {terminal_id}"
            )
        if row["provider"] != provider:
            raise _MigrationRefusal(
                MIGRATION_REFUSED_PROVIDER_DRIFT,
                f"terminal {terminal_id} now runs provider {row['provider']!r}, "
                f"not {provider!r}",
            )
        managed = row["generation"] is not None
        if managed:
            if generation is None or row["generation"] != generation:
                raise _MigrationRefusal(
                    MIGRATION_REFUSED_GENERATION_MISMATCH,
                    "the terminal's model generation does not match the audited request",
                )
            if physical_occurrence is not None and physical_occurrence != generation:
                raise _MigrationRefusal(
                    MIGRATION_REFUSED_OCCURRENCE_MISMATCH,
                    "a supplied physical occurrence must equal the managed model generation",
                )
        else:
            if generation is not None:
                raise _MigrationRefusal(
                    MIGRATION_REFUSED_GENERATION_MISMATCH,
                    "a legacy terminal has no model generation; a supplied generation "
                    "is refused",
                )
            if not physical_occurrence:
                raise _MigrationRefusal(
                    MIGRATION_REFUSED_OCCURRENCE_MISMATCH,
                    "a legacy terminal requires its durable callback-target occurrence",
                )
            if row["callback_target_generation"] != physical_occurrence:
                raise _MigrationRefusal(
                    MIGRATION_REFUSED_OCCURRENCE_MISMATCH,
                    "the legacy callback-target generation does not match the audited " "request",
                )
        candidate = _classify_terminal(db, row, str(uuid.uuid4()))
        if candidate["classification"] != CANDIDATE_ELIGIBLE:
            raise _MigrationRefusal(
                candidate["classification"], candidate["reason"] or candidate["classification"]
            )
        if candidate["evidence_digest"] != audit_candidate_digest:
            raise _MigrationRefusal(
                MIGRATION_REFUSED_CANDIDATE_DRIFT,
                "the candidate facts changed since the audit; the audited digest no "
                "longer matches",
            )
        return candidate


def _validate_request_occurrence(
    *,
    terminal_id: str,
    provider: str,
    generation: Optional[str],
    physical_occurrence: Optional[str],
) -> str:
    """Pre-intent validation of the durable physical occurrence.

    The intent row's ``physical_occurrence`` column is NOT NULL, so the
    occurrence must be resolvable BEFORE any intent row is inserted: a
    missing occurrence is a typed refusal, never a raw database integrity
    error.  Returns the resolved occurrence (the model generation for a
    managed row, the callback-target generation for a legacy row).
    Raises :class:`_MigrationRefusal`."""
    with database.SessionLocal() as db:
        row = nsr.terminal_occurrence_snapshot(terminal_id, db=db)
        if row is None:
            raise _MigrationRefusal(
                "terminal-not-found", f"no terminal row is recorded for {terminal_id}"
            )
        if row["provider"] != provider:
            raise _MigrationRefusal(
                MIGRATION_REFUSED_PROVIDER_DRIFT,
                f"terminal {terminal_id} now runs provider {row['provider']!r}, "
                f"not {provider!r}",
            )
        managed = row["generation"] is not None
        if managed:
            if generation is None or row["generation"] != generation:
                raise _MigrationRefusal(
                    MIGRATION_REFUSED_GENERATION_MISMATCH,
                    "the terminal's model generation does not match the audited request",
                )
            if physical_occurrence is not None and physical_occurrence != generation:
                raise _MigrationRefusal(
                    MIGRATION_REFUSED_OCCURRENCE_MISMATCH,
                    "a supplied physical occurrence must equal the managed model generation",
                )
            return generation
        if generation is not None:
            raise _MigrationRefusal(
                MIGRATION_REFUSED_GENERATION_MISMATCH,
                "a legacy terminal has no model generation; a supplied generation is refused",
            )
        if not physical_occurrence:
            raise _MigrationRefusal(
                "missing-occurrence",
                "a legacy terminal requires its durable callback-target occurrence, "
                "and none was supplied",
            )
        if row["callback_target_generation"] != physical_occurrence:
            raise _MigrationRefusal(
                MIGRATION_REFUSED_OCCURRENCE_MISMATCH,
                "the legacy callback-target generation does not match the audited request",
            )
        return physical_occurrence


def _verify_committed_evidence(*, operation_id: str, base: Mapping[str, Any]) -> None:
    """The persistence-seam validation for a repair that reported success.

    PR #99 already revalidated the exact occurrence/pane/process at its own
    irreversible seam and atomically committed terminal/lineage/evidence, so
    the COMMITTED evidence is the authoritative truth: the wrapper validates
    the exact repair operation/request/evidence identity and records
    ``migrated``.  A pane that exits after that commit is a separate
    lifecycle fact and never downgrades committed success.  Raises
    :class:`_MigrationRefusal` only when the committed evidence itself is
    missing or does not bind this exact occurrence."""
    evidence = nsr.repair_outcome_by_operation(base["repair_operation_id"])
    if evidence is None:
        raise _MigrationRefusal(
            MIGRATION_REFUSED_SEAM_DRIFT,
            "the repair reported success but no committed evidence exists",
        )
    with database.SessionLocal() as db:
        row = _migration_row(db, operation_id)
    occurrence = row.generation if row.generation else row.physical_occurrence
    if (
        evidence.get("terminal_id") != base["terminal_id"]
        or evidence.get("provider") != base["provider"]
        or evidence.get("generation") != occurrence
    ):
        raise _MigrationRefusal(
            MIGRATION_REFUSED_SEAM_DRIFT,
            "the committed repair evidence does not bind this exact terminal/provider/"
            "occurrence",
        )


def _migration_outcome_from_repair(
    base: Mapping[str, Any],
    repair_outcome: Mapping[str, Any],
    *,
    status: str,
    reason: Optional[str] = None,
) -> dict[str, Any]:
    """The bounded migration outcome projected from the repair's typed
    outcome.  Never raw pane output; only the bounded digest and typed facts."""
    outcome = dict(base)
    outcome.update(
        status=status,
        reason=reason,
        detail=repair_outcome.get("detail"),
        repair_status=repair_outcome.get("status"),
        repair_reason=repair_outcome.get("reason"),
        native_session_id=repair_outcome.get("native_session_id"),
        evidence_sha256=repair_outcome.get("evidence_sha256"),
        parser_key=repair_outcome.get("parser_key"),
        attachment=repair_outcome.get("attachment"),
        task_bytes_submitted=bool(repair_outcome.get("task_bytes_submitted", False)),
    )
    return outcome


def _outcome_from_evidence(base: Mapping[str, Any], evidence: Mapping[str, Any]) -> dict[str, Any]:
    """The bounded migrated outcome derived from committed repair evidence."""
    outcome = dict(base)
    outcome.update(
        status=MIGRATION_MIGRATED,
        reason=None,
        detail=None,
        repair_status=nsr.STATUS_REPAIRED,
        repair_reason=None,
        native_session_id=evidence["native_session_id"],
        evidence_sha256=evidence["evidence_sha256"],
        parser_key=evidence["parser_key"],
        attachment=None,
        task_bytes_submitted=False,
    )
    return outcome


def migrate_terminal_native_identity(
    *,
    operation_id: str,
    terminal_id: str,
    provider: str,
    generation: Optional[str] = None,
    physical_occurrence: Optional[str] = None,
    provider_version: Optional[str] = None,
    audit_occurrence_id: str,
    audit_candidate_digest: str,
    caller: str = "cao.legacy-identity-migration",
) -> dict[str, Any]:
    """The explicit opt-in one-candidate migration coordinator (cond-0377D).

    Consumes exactly one ELIGIBLE audit candidate and invokes the exact
    cond-0377C repair operation (never reimplementing status parsing, modal
    cleanup, attachment adoption, or identity persistence).  The intent row
    is persisted BEFORE any repair interaction and a durable
    ``attempt-started`` marker before the first ``/status`` byte; an exact
    duplicate retry query-adopts the same migration AND repair operations;
    a changed request under the same id conflicts before any provider or
    roster effect; response loss without adoptable repair evidence is typed
    ambiguous/unresolved and never resends.  The candidate is revalidated
    from current facts before the repair and again at the irreversible
    persistence seam.

    Never replays task input, never creates a fresh conversation, never
    reincarnates a pane, never changes task/role/profile, and never
    auto-creates Kimi's lazy session.  Additive history only: a known
    native id is never overwritten and no legacy row is deleted.
    """
    base: dict[str, Any] = {
        "schema": MIGRATION_SCHEMA,
        "status": None,
        "reason": None,
        "detail": None,
        "operation_id": operation_id,
        "request_digest": None,
        "repair_operation_id": _repair_operation_id(operation_id),
        "repair_status": None,
        "repair_reason": None,
        "terminal_id": terminal_id,
        "provider": provider,
        "generation": generation,
        "physical_occurrence": physical_occurrence,
        "provider_version": normalized_version(provider_version) if provider_version else None,
        #: The audit-derived build provenance (durable binding fact or the
        #: explicit unaudited pinned-legacy-plan fallback).  The caller's
        #: ``provider_version`` is plan selection only and is never described
        #: as observed build proof.
        "build_provenance": None,
        "audit_occurrence_id": audit_occurrence_id,
        "audit_candidate_digest": audit_candidate_digest,
        "native_session_id": None,
        "evidence_sha256": None,
        "parser_key": None,
        "attachment": None,
        "task_bytes_submitted": False,
    }

    def refused(reason: str, detail: str) -> dict[str, Any]:
        outcome = dict(base)
        outcome.update(status=MIGRATION_REFUSED, reason=reason, detail=_bounded(detail))
        return outcome

    if not isinstance(operation_id, str) or len(operation_id) != 36:
        return refused("invalid-input", "operation_id must be a canonical lowercase UUID")
    import uuid as _uuid_mod

    try:
        if str(_uuid_mod.UUID(operation_id)) != operation_id:
            return refused("invalid-input", "operation_id must be a canonical lowercase UUID")
    except ValueError:
        return refused("invalid-input", "operation_id must be a canonical lowercase UUID")
    if not terminal_id or not provider or not audit_occurrence_id:
        return refused(
            "invalid-input", "terminal_id, provider, and audit_occurrence_id are required"
        )
    if (
        not isinstance(audit_candidate_digest, str)
        or len(audit_candidate_digest) != 64
        or any(ch not in "0123456789abcdef" for ch in audit_candidate_digest)
    ):
        return refused(
            "invalid-input", "audit_candidate_digest must be 64 lowercase hex characters"
        )

    base["request_digest"] = migration_request_digest(
        terminal_id=terminal_id,
        provider=provider,
        generation=generation,
        physical_occurrence=physical_occurrence,
        provider_version=provider_version,
        audit_occurrence_id=audit_occurrence_id,
        audit_candidate_digest=audit_candidate_digest,
    )

    # Intent-first resolution: an exact retry adopts; a changed request
    # conflicts; a fresh operation validates its occurrence and persists its
    # intent before any repair interaction.
    existing = _read_migration(operation_id)
    if existing is not None:
        resolved = _resolve_existing_row(existing, base)
        if resolved is not None:
            return resolved
    if existing is None:
        if not migration_producer_enabled():
            return refused(
                MIGRATION_REFUSED_PRODUCER_DISABLED,
                "the migration producer is disabled; new migration operations are "
                "refused (prior rows are retained and existing operations can still "
                "be queried)",
            )
        # The durable physical occurrence must resolve BEFORE the intent row
        # (its column is NOT NULL): a missing occurrence is a typed refusal,
        # never a raw database integrity error.
        try:
            resolved_occurrence = _validate_request_occurrence(
                terminal_id=terminal_id,
                provider=provider,
                generation=generation,
                physical_occurrence=physical_occurrence,
            )
        except _MigrationRefusal as exc:
            return refused(exc.reason, exc.detail)
        base["physical_occurrence"] = resolved_occurrence
        if not _persist_migration_intent(
            operation_id=operation_id,
            request_digest=base["request_digest"],
            terminal_id=terminal_id,
            provider=provider,
            generation=generation,
            physical_occurrence=resolved_occurrence,
            provider_version=base["provider_version"],
            audit_occurrence_id=audit_occurrence_id,
            audit_candidate_digest=audit_candidate_digest,
            repair_operation_id=base["repair_operation_id"],
        ):
            raced = _read_migration(operation_id)
            if raced is not None:
                resolved = _resolve_existing_row(raced, base)
                if resolved is not None:
                    return resolved

    # Revalidation pass 1: the exact candidate, lifecycle, pane/server/
    # process, and attachment facts must still match the audited candidate
    # before any repair interaction.
    try:
        candidate = _revalidate_migration_candidate(
            terminal_id=terminal_id,
            provider=provider,
            generation=generation,
            physical_occurrence=physical_occurrence,
            audit_candidate_digest=audit_candidate_digest,
            operation_id=operation_id,
        )
    except _MigrationRefusal as exc:
        outcome = refused(exc.reason, exc.detail)
        _record_migration_outcome(
            operation_id=operation_id, status=MIGRATION_REFUSED, outcome=outcome
        )
        return outcome
    base["build_provenance"] = candidate.get("build_provenance")

    # The atomic pending -> attempt-started execution claim: exactly one
    # caller wins and may invoke PR #99; every loser resolves the row with
    # zero repair invocation (query/adopt or a typed in-progress/unresolved
    # outcome).
    if not _cas_pending_to_attempt_started(operation_id):
        raced = _read_migration(operation_id)
        if raced is not None:
            resolved = _resolve_existing_row(raced, base)
            if resolved is not None:
                return resolved
            if raced.status == MIGRATION_PENDING:
                # A concurrent transition race: retry the claim once.
                if _cas_pending_to_attempt_started(operation_id):
                    pass
                else:
                    raced = _read_migration(operation_id)
                    if raced is not None:
                        resolved = _resolve_existing_row(raced, base)
                        if resolved is not None:
                            return resolved
                    return refused(
                        MIGRATION_REFUSED_IN_PROGRESS,
                        "another executor holds this migration operation; this caller "
                        "performed no repair interaction",
                    )
        return refused(
            MIGRATION_REFUSED_IN_PROGRESS,
            "another executor holds this migration operation; this caller performed "
            "no repair interaction",
        )

    # A concurrent exact duplicate may have finished while we revalidated.
    after = _read_migration(operation_id)
    if after is not None and after.status in _FINAL_MIGRATION_STATUSES:
        return _recorded_outcome(after)

    # Invoke the exact cond-0377C repair operation (idempotent under its own
    # derived operation id).
    repair_outcome = nsr.repair_terminal_native_identity(
        terminal_id=terminal_id,
        generation=generation,
        provider_version=provider_version,
        physical_occurrence=physical_occurrence,
        operation_id=base["repair_operation_id"],
        caller=caller,
    )
    repair_status = repair_outcome.get("status")

    if repair_status == nsr.STATUS_REPAIRED:
        # The persistence seam: PR #99 already revalidated the exact
        # occurrence/pane/process at its own irreversible seam and atomically
        # committed terminal/lineage/evidence, so the COMMITTED evidence is
        # the authoritative truth.  Later pane loss is a separate lifecycle
        # fact and never downgrades committed success.
        try:
            _verify_committed_evidence(operation_id=operation_id, base=base)
        except _MigrationRefusal as exc:
            outcome = refused(
                MIGRATION_REFUSED_SEAM_DRIFT,
                f"the committed repair evidence could not be validated: {exc.detail}",
            )
            outcome.update(
                repair_status=repair_status,
                repair_reason=repair_outcome.get("reason"),
                native_session_id=repair_outcome.get("native_session_id"),
                evidence_sha256=repair_outcome.get("evidence_sha256"),
                parser_key=repair_outcome.get("parser_key"),
            )
            _record_migration_outcome(
                operation_id=operation_id, status=MIGRATION_REFUSED, outcome=outcome
            )
            return outcome
        outcome = _migration_outcome_from_repair(base, repair_outcome, status=MIGRATION_MIGRATED)
        _record_migration_outcome(
            operation_id=operation_id, status=MIGRATION_MIGRATED, outcome=outcome
        )
        return outcome
    if repair_status == nsr.STATUS_ALREADY_KNOWN:
        outcome = _migration_outcome_from_repair(
            base, repair_outcome, status=MIGRATION_ALREADY_KNOWN
        )
        _record_migration_outcome(
            operation_id=operation_id, status=MIGRATION_ALREADY_KNOWN, outcome=outcome
        )
        return outcome
    if repair_status == nsr.STATUS_IDENTITY_STILL_MISSING:
        outcome = _migration_outcome_from_repair(
            base, repair_outcome, status=MIGRATION_IDENTITY_STILL_MISSING
        )
        _record_migration_outcome(
            operation_id=operation_id, status=MIGRATION_IDENTITY_STILL_MISSING, outcome=outcome
        )
        return outcome
    if repair_status == nsr.STATUS_REFUSED:
        outcome = _migration_outcome_from_repair(
            base,
            repair_outcome,
            status=MIGRATION_REFUSED,
            reason=repair_outcome.get("reason") or "refused",
        )
        _record_migration_outcome(
            operation_id=operation_id, status=MIGRATION_REFUSED, outcome=outcome
        )
        return outcome
    if repair_status == nsr.STATUS_ERRORED:
        outcome = _migration_outcome_from_repair(
            base, repair_outcome, status=MIGRATION_ERRORED, reason="errored"
        )
        _record_migration_outcome(
            operation_id=operation_id, status=MIGRATION_ERRORED, outcome=outcome
        )
        return outcome
    outcome = refused("errored", "the repair returned an unknown status")
    _record_migration_outcome(operation_id=operation_id, status=MIGRATION_ERRORED, outcome=outcome)
    return outcome


def iterate_migration_candidates(
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Explicit-request-only batch helper over the one-candidate primitive.

    Iterates the given explicit candidate requests in order, stops at the
    first refused/errored outcome, and projects partial results truthfully
    (each result is the one-candidate outcome verbatim).  Every candidate is
    its own independent durable operation.  Never scans the fleet and never
    runs at launch.
    """
    results: list[dict[str, Any]] = []
    stopped_after: Optional[int] = None
    for index, request in enumerate(candidates):
        kwargs: dict[str, Any] = {
            key: request.get(key) for key in _MIGRATION_REQUEST_KEYS if key in request
        }
        outcome = migrate_terminal_native_identity(**kwargs)
        results.append(outcome)
        if outcome["status"] in (MIGRATION_REFUSED, MIGRATION_ERRORED):
            stopped_after = index
            break
    return {
        "schema": MIGRATION_BATCH_SCHEMA,
        "results": results,
        "completed": len(results),
        "stopped": stopped_after is not None,
        "stopped_after": stopped_after,
    }

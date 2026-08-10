"""Versioned truthful provider capability/read cells (cond-0377D).

One cell per provider (``claude_code``, ``codex``, ``kimi_cli``,
``muse_cli``); DeepSeek/Z.ai are Claude Code route provenance, not separate
harness identity domains.  Each cell keeps three facts independent:

* installed/durable build identity — the exact builds with proven
  status-observation evidence (the repair's own plan catalog is the
  authority, not the broader provider supported-version table), plus the
  FULL installed provider banner and the resolved executable SHA-256
  recorded by a live canary receipt (Muse's ``R`` revision is never
  normalized away);
* parser/interaction-plan support — whether status observation/repair is
  code-supported for the build, with the exact parser key and capability
  schema version;
* installed live canary state — present and matching, stale, failed, or
  absent.

A cell is ``enabled`` ONLY when the parser is code-supported AND an exact
installed live receipt is DERIVED from the actual committed records: the
migration operation/request, its deterministic repair operation, the
observation-attempt journal (exactly one status action), the repair
evidence/request/evidence digest, provider/parser/plan, native identity,
and the attachment/adoption outcome.  Caller-supplied values are never
trusted for fields that can be derived; a static parser fixture, an
executable banner, or a unit test never qualifies.  Zero-action
already-known results and Kimi ``identity-still-missing`` are useful
truthful outcomes but are non-green.  Kimi without a session stays
``unresolved``/``disabled`` and receives no synthetic turn.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import legacy_identity_migration as lim
from cli_agent_orchestrator.services import native_attachment
from cli_agent_orchestrator.services import native_status_repair as nsr
from cli_agent_orchestrator.services.managed_provider_bridge import (
    BridgeError,
    provider_version_banner,
)

logger = logging.getLogger(__name__)

PROVIDER_CAPABILITY_SCHEMA = "cao-m3-provider-capabilities-v1"
CANARY_RECEIPT_SCHEMA = "cao-m3-provider-canary-receipt-v1"

CANARY_STATE_OK = "ok"
CANARY_STATE_FAILED = "failed"
CANARY_STATES = frozenset({CANARY_STATE_OK, CANARY_STATE_FAILED})

CELL_ENABLED = "enabled"
CELL_DISABLED = "disabled"
CELL_UNSUPPORTED = "unsupported"
CELL_UNAVAILABLE = "unavailable"
CELL_UNRESOLVED = "unresolved"

_BANNER_MAX = 200
_ATTACHMENT_OUTCOME_MAX = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _bounded(detail: str) -> str:
    detail = (detail or "").strip()
    return detail if len(detail) <= 300 else detail[:300] + "…"


def _is_invalid_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) != value
    except (ValueError, AttributeError, TypeError):
        return True


def _observe_installed_executable(*, provider: str, executable_path: str) -> dict[str, Any]:
    """Observe the exact installed executable: canonical path, computed
    SHA-256 of the file itself, and the bounded ``--version`` banner.

    The digest and banner are never caller assertions: the service hashes
    the exact file and runs the bounded provider version probe against it
    in the bounded child environment, retaining the complete banner
    (Muse's full ``R`` revision survives).  A canonical absolute existing
    non-symlink-resolved executable is required; anything else keeps the
    cell non-green.  Returns ``canonical_path``, ``banner``, ``sha256``,
    and the normalized version used for parser-plan matching.
    """
    if not isinstance(executable_path, str) or not executable_path:
        raise nsr.NativeStatusRepairConflict(
            "canary-invalid", "executable_path is required and must be a canonical absolute path"
        )
    if not os.path.isabs(executable_path) or os.path.realpath(executable_path) != executable_path:
        raise nsr.NativeStatusRepairConflict(
            "canary-invalid",
            "executable_path must be a canonical absolute path with no unresolved symlinks",
        )
    if not os.path.isfile(executable_path) or not os.access(executable_path, os.X_OK):
        raise nsr.NativeStatusRepairConflict(
            "canary-invalid",
            "executable_path must name an existing executable file",
        )
    digest = hashlib.sha256()
    with open(executable_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    sha256 = digest.hexdigest()
    try:
        banner = provider_version_banner(
            {
                "provider_executable": executable_path,
                "provider": provider,
                # The bounded child environment needs a pinned effort for a
                # Kimi probe; provider-default adds no override variable.
                "effort": "provider-default",
            }
        )
    except (BridgeError, OSError, subprocess.TimeoutExpired) as exc:
        raise nsr.NativeStatusRepairConflict(
            "canary-invalid",
            f"the bounded provider --version probe of the exact executable failed: "
            f"{_bounded(str(exc))}",
        ) from exc
    if not isinstance(banner, str) or not banner or len(banner) > _BANNER_MAX:
        raise nsr.NativeStatusRepairConflict(
            "canary-invalid", "the observed --version banner is not a bounded provider banner"
        )
    match = re.search(r"\d+\.\d+\.\d+", banner)
    normalized = match.group(0) if match else ""
    if not normalized:
        raise nsr.NativeStatusRepairConflict(
            "canary-invalid",
            "the observed --version banner must name a semver-shaped provider build",
        )
    plan = nsr.repair_parser_plans().get(provider)
    if plan is None:
        raise nsr.NativeStatusRepairConflict(
            "canary-invalid", f"provider {provider!r} has no status-observation plan"
        )
    if normalized not in plan["supported_versions"]:
        raise nsr.NativeStatusRepairConflict(
            "canary-invalid",
            f"installed build {normalized!r} has no proven status-observation "
            "evidence; an unproven build can never be green",
        )
    return {
        "canonical_path": executable_path,
        "banner": banner,
        "sha256": sha256,
        "normalized": normalized,
    }


def _derived_receipt_request_digest(facts: dict[str, Any]) -> str:
    """The digest of the DERIVED receipt content, for response-loss-safe
    exact-duplicate adoption vs changed-content conflict."""
    payload = {key: facts[key] for key in sorted(facts)}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _derive_green_receipt(
    *,
    provider: str,
    migration_operation_id: str,
    installed: dict[str, Any],
) -> dict[str, Any]:
    """Derive a successful receipt from the ACTUAL committed records.

    Every field that can be derived is derived: the repair operation is the
    deterministic uuid5 of the migration operation; the request/evidence
    digests, native identity, parser, and action count come from the repair
    evidence and the observation-attempt journal; the attachment/adoption
    outcome comes from the attachment store.  Missing or mismatched backing
    rows raise a typed refusal — a green cell is never asserted from
    caller-supplied values.
    """
    if _is_invalid_uuid(migration_operation_id):
        raise nsr.NativeStatusRepairConflict(
            "canary-invalid", "migration_operation_id must be a canonical lowercase UUID"
        )
    repair_op = lim._repair_operation_id(migration_operation_id)
    with database.SessionLocal() as db:
        migration = (
            db.query(database.LegacyIdentityMigrationModel)
            .filter(
                database.LegacyIdentityMigrationModel.migration_operation_id
                == migration_operation_id
            )
            .one_or_none()
        )
    if migration is None:
        raise nsr.NativeStatusRepairConflict(
            "canary-invalid",
            "no migration operation backs this canary; caller-supplied values alone "
            "can never make a cell green",
        )
    occurrence = migration.generation if migration.generation else migration.physical_occurrence
    if migration.status != lim.MIGRATION_MIGRATED:
        raise nsr.NativeStatusRepairConflict(
            "canary-invalid",
            "the backing migration operation did not reach migrated; "
            "already-known/zero-action and identity-still-missing verdicts are "
            "truthful but non-green",
        )
    if migration.repair_operation_id != repair_op:
        raise nsr.NativeStatusRepairConflict(
            "canary-invalid",
            "the migration operation's recorded repair operation does not match the " "derived one",
        )
    journal = nsr.repair_observation_attempt(repair_op)
    if journal is None:
        raise nsr.NativeStatusRepairConflict(
            "canary-invalid",
            "no observation-attempt journal backs this canary; a green receipt "
            "requires an actual status observation",
        )
    if (
        journal["terminal_id"] != migration.terminal_id
        or journal["provider"] != migration.provider
        or journal["generation"] != occurrence
    ):
        raise nsr.NativeStatusRepairConflict(
            "canary-invalid", "the observation-attempt journal does not bind this occurrence"
        )
    if journal["status"] != nsr.OBSERVATION_OBSERVED or journal["status_action_count"] != 1:
        raise nsr.NativeStatusRepairConflict(
            "canary-invalid",
            "the backing observation never produced a verdict with exactly one "
            "status action; zero-action results are non-green",
        )
    evidence = nsr.repair_outcome_by_operation(repair_op)
    if evidence is None:
        raise nsr.NativeStatusRepairConflict(
            "canary-invalid", "no committed repair evidence backs this canary"
        )
    if evidence["request_digest"] != journal["request_digest"]:
        raise nsr.NativeStatusRepairConflict(
            "canary-invalid", "the repair evidence request digest does not match the journal"
        )
    if (
        evidence["terminal_id"] != migration.terminal_id
        or evidence["provider"] != migration.provider
        or evidence["generation"] != occurrence
    ):
        raise nsr.NativeStatusRepairConflict(
            "canary-invalid", "the repair evidence does not bind this occurrence"
        )
    if evidence["provider_version"] != installed["normalized"]:
        raise nsr.NativeStatusRepairConflict(
            "canary-invalid",
            "the installed banner version does not match the panel-attested build "
            "the evidence recorded",
        )
    plan = nsr.repair_parser_plans()[provider]
    if evidence["parser_key"] != plan["parser_key"]:
        raise nsr.NativeStatusRepairConflict(
            "canary-invalid",
            "the repair evidence names a parser the current plan does not run",
        )
    attachment = native_attachment.get(provider, evidence["native_session_id"])
    if attachment is None or attachment["state"] != native_attachment.ATTACHED:
        raise nsr.NativeStatusRepairConflict(
            "canary-invalid", "no attached native session backs this canary"
        )
    owner = attachment.get("owner") or {}
    if owner.get("terminal_id") != migration.terminal_id or owner.get("generation") != occurrence:
        raise nsr.NativeStatusRepairConflict(
            "canary-invalid", "the attachment owner does not bind this occurrence"
        )
    receipt = attachment.get("adoption_receipt")
    if not isinstance(receipt, dict) or receipt.get("operation_id") != repair_op:
        raise nsr.NativeStatusRepairConflict(
            "canary-invalid", "the attachment adoption receipt does not bind this repair operation"
        )
    facts = {
        "provider": provider,
        "migration_operation_id": migration_operation_id,
        "operation_id": repair_op,
        "installed_build_banner": installed["banner"],
        "installed_build_sha256": installed["sha256"],
        "executable_path": installed["canonical_path"],
        "state": CANARY_STATE_OK,
        "status_action_count": 1,
        "migration_request_digest": migration.request_digest,
        "evidence_request_digest": evidence["request_digest"],
        "evidence_sha256": evidence["evidence_sha256"],
        "native_session_id": evidence["native_session_id"],
        "parser_key": evidence["parser_key"],
        "attachment_outcome": "attached",
    }
    facts["request_digest"] = _derived_receipt_request_digest(
        {key: value for key, value in facts.items() if key != "request_digest"}
    )
    return facts


def record_provider_canary_receipt(
    *,
    canary_id: str,
    provider: str,
    migration_operation_id: Optional[str] = None,
    executable_path: str,
    state: str = CANARY_STATE_OK,
    recorded_at: Optional[str] = None,
    db: Any = None,
) -> dict[str, Any]:
    """The write seam for installed live-repair canaries (cond-0377D).

    Only an installed canary that really ran the bounded status observation
    against a live pane may call this.  A successful (``ok``) receipt is
    DERIVED from the actual committed records — the migration operation/
    request, its deterministic repair operation, the observation-attempt
    journal (exactly one status action), the repair evidence/request/
    evidence digest, provider/parser/plan, native identity, and the
    attachment/adoption outcome — and requires the full installed provider
    banner plus the resolved executable SHA-256.  A static parser fixture,
    an executable banner, or a unit test never qualifies.  A ``failed``
    receipt is negative evidence and needs no backing rows.

    Response-loss safe: an exact duplicate canary id with the exact derived
    content adopts the recorded receipt; changed content conflicts.

    The schema is fixed so later installed canaries can record without
    changing the read surface.
    """
    if _is_invalid_uuid(canary_id):
        raise nsr.NativeStatusRepairConflict(
            "canary-invalid", "canary_id must be a canonical lowercase UUID"
        )
    if provider not in nsr.repair_parser_plans():
        raise nsr.NativeStatusRepairConflict(
            "canary-invalid", f"provider {provider!r} has no status-observation plan"
        )
    if state not in CANARY_STATES:
        raise nsr.NativeStatusRepairConflict(
            "canary-invalid", f"state must be one of {sorted(CANARY_STATES)}"
        )
    installed = _observe_installed_executable(provider=provider, executable_path=executable_path)
    normalized_build = installed["normalized"]
    stamp = recorded_at or _now()

    if state == CANARY_STATE_FAILED:
        facts = {
            "provider": provider,
            "migration_operation_id": migration_operation_id or "",
            "operation_id": (
                lim._repair_operation_id(migration_operation_id) if migration_operation_id else ""
            ),
            "installed_build_banner": installed["banner"],
            "installed_build_sha256": installed["sha256"],
            "executable_path": installed["canonical_path"],
            "state": CANARY_STATE_FAILED,
            "status_action_count": 0,
            "request_digest": "",
            "migration_request_digest": None,
            "evidence_request_digest": None,
            "evidence_sha256": None,
            "native_session_id": None,
            "parser_key": None,
            "attachment_outcome": None,
        }
        facts["request_digest"] = _derived_receipt_request_digest(
            {key: value for key, value in facts.items() if key != "request_digest"}
        )
    else:
        if migration_operation_id is None:
            raise nsr.NativeStatusRepairConflict(
                "canary-invalid",
                "a successful canary receipt requires the migration operation it ran "
                "through; the repair operation and every digest are derived from it",
            )
        facts = _derive_green_receipt(
            provider=provider,
            migration_operation_id=migration_operation_id,
            installed=installed,
        )

    def _record(session: Any) -> dict[str, Any]:
        existing = (
            session.query(database.ProviderCanaryReceiptModel)
            .filter(database.ProviderCanaryReceiptModel.canary_id == canary_id)
            .one_or_none()
        )
        if existing is not None:
            if existing.request_digest == facts["request_digest"]:
                # Response-loss retry of the exact derived receipt: adopt.
                return _receipt_dict(existing)
            raise nsr.NativeStatusRepairConflict(
                "canary-conflict",
                f"canary id {canary_id} is already recorded with different content",
            )
        session.add(
            database.ProviderCanaryReceiptModel(
                canary_id=canary_id,
                provider=provider,
                build=normalized_build,
                receipt_schema=CANARY_RECEIPT_SCHEMA,
                operation_id=facts["operation_id"],
                migration_operation_id=facts["migration_operation_id"],
                request_digest=facts["request_digest"],
                migration_request_digest=facts.get("migration_request_digest"),
                evidence_request_digest=facts.get("evidence_request_digest"),
                evidence_sha256=facts["evidence_sha256"],
                native_session_id=facts["native_session_id"],
                status_action_count=facts["status_action_count"],
                parser_key=facts["parser_key"],
                attachment_outcome=facts["attachment_outcome"],
                installed_build_banner=installed["banner"],
                installed_build_sha256=installed["sha256"],
                executable_path=installed["canonical_path"],
                state=state,
                recorded_at=stamp,
                created_at=_now(),
            )
        )
        try:
            session.commit()
        except IntegrityError:
            # A concurrent exact duplicate won the primary key between our
            # read and insert: converge by adopting the winner's row when its
            # request digest matches exactly; changed content conflicts.
            session.rollback()
            winner = (
                session.query(database.ProviderCanaryReceiptModel)
                .filter(database.ProviderCanaryReceiptModel.canary_id == canary_id)
                .one_or_none()
            )
            if winner is not None and winner.request_digest == facts["request_digest"]:
                return _receipt_dict(winner)
            raise nsr.NativeStatusRepairConflict(
                "canary-conflict",
                f"canary id {canary_id} is already recorded with different content",
            ) from None
        return _receipt_dict(
            session.query(database.ProviderCanaryReceiptModel)
            .filter(database.ProviderCanaryReceiptModel.canary_id == canary_id)
            .one()
        )

    if db is not None:
        return _record(db)
    with database.SessionLocal() as session:
        return _record(session)


def _receipt_dict(row: Any) -> dict[str, Any]:
    return {
        "canary_id": row.canary_id,
        "provider": row.provider,
        "build": row.build,
        "receipt_schema": row.receipt_schema,
        "operation_id": row.operation_id,
        "migration_operation_id": row.migration_operation_id,
        "request_digest": row.request_digest,
        "migration_request_digest": row.migration_request_digest,
        "evidence_request_digest": row.evidence_request_digest,
        "evidence_sha256": row.evidence_sha256,
        "native_session_id": row.native_session_id,
        "status_action_count": row.status_action_count,
        "parser_key": row.parser_key,
        "attachment_outcome": row.attachment_outcome,
        "installed_build_banner": row.installed_build_banner,
        "installed_build_sha256": row.installed_build_sha256,
        "executable_path": row.executable_path,
        "state": row.state,
        "recorded_at": row.recorded_at,
        "created_at": row.created_at,
    }


def _latest_canary_receipt(provider: str) -> Optional[dict[str, Any]]:
    """The newest recorded installed canary receipt for one provider.

    Newest-first by SERVER-authored ``created_at`` with a deterministic
    canary-id tie-break only: a caller-dated ``recorded_at`` is evidence,
    never ordering authority, so a future-dated older receipt can never
    shadow later evidence.  An absent table reads as no receipts (the true
    answer for a store no server has ever written to); a present but
    unreadable store raises.
    """
    from sqlalchemy import inspect as sa_inspect

    with database.SessionLocal() as db:
        table = database.ProviderCanaryReceiptModel.__tablename__
        if not sa_inspect(db.get_bind()).has_table(table):
            return None
        row = (
            db.query(database.ProviderCanaryReceiptModel)
            .filter(database.ProviderCanaryReceiptModel.provider == provider)
            .order_by(
                database.ProviderCanaryReceiptModel.created_at.desc(),
                database.ProviderCanaryReceiptModel.canary_id.desc(),
            )
            .first()
        )
        if row is None:
            return None
        return _receipt_dict(row)


def _canary_block(receipt: Optional[dict[str, Any]]) -> dict[str, Any]:
    empty: dict[str, Any] = {
        "present": False,
        "state": "absent",
        "build": None,
        "operation_id": None,
        "migration_operation_id": None,
        "evidence_sha256": None,
        "native_session_id": None,
        "status_action_count": None,
        "parser_key": None,
        "attachment_outcome": None,
        "recorded_at": None,
    }
    if receipt is None:
        return empty
    return {
        "present": True,
        "state": "absent",
        "build": receipt["build"],
        "operation_id": receipt["operation_id"],
        "migration_operation_id": receipt["migration_operation_id"],
        "evidence_sha256": receipt["evidence_sha256"],
        "native_session_id": receipt["native_session_id"],
        "status_action_count": receipt["status_action_count"],
        "parser_key": receipt["parser_key"],
        "attachment_outcome": receipt["attachment_outcome"],
        "recorded_at": receipt["recorded_at"],
    }


def _provider_capability_cell(provider: str) -> dict[str, Any]:
    """One truthful provider cell for the versioned capability read."""
    plan = nsr.repair_parser_plans().get(provider)
    route_domains = ["deepseek", "zai"] if provider == "claude_code" else []
    if plan is None:
        return {
            "provider": provider,
            "harness_domain": provider,
            "route_provenance_domains": [],
            "build_identity": {
                "installed_build": None,
                "installed_build_source": None,
            },
            "parser_support": {
                "code_supported": False,
                "parser_key": None,
                "capability_schema": None,
                "supported_builds": [],
                "escape": None,
            },
            "status_observation_repair_code_supported": False,
            "canary": _canary_block(None),
            "installed_live_repair_proven": False,
            "cell_state": CELL_UNSUPPORTED,
            "reason": "no pinned status-observation plan exists for this provider",
        }
    parser_support = {
        "code_supported": True,
        "parser_key": plan["parser_key"],
        "capability_schema": nsr.REPAIR_SCHEMA,
        "supported_builds": list(plan["supported_versions"]),
        "escape": bool(plan["escape"]),
    }
    build_identity: dict[str, Any] = {
        "installed_build": None,
        "installed_build_source": None,
    }

    def _cell(
        *,
        canary: dict[str, Any],
        installed_live_repair_proven: bool,
        cell_state: str,
        reason: str,
        receipt: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        identity = dict(build_identity)
        if receipt is not None:
            identity["installed_build"] = {
                "banner": receipt["installed_build_banner"],
                "normalized": receipt["build"],
                "sha256": receipt["installed_build_sha256"],
                "executable_path": receipt["executable_path"],
            }
            identity["installed_build_source"] = "canary-receipt"
        return {
            "provider": provider,
            "harness_domain": provider,
            "route_provenance_domains": route_domains,
            "build_identity": identity,
            "parser_support": parser_support,
            "status_observation_repair_code_supported": True,
            "canary": canary,
            "installed_live_repair_proven": installed_live_repair_proven,
            "cell_state": cell_state,
            "reason": reason,
        }

    try:
        receipt = _latest_canary_receipt(provider)
    except Exception as exc:  # noqa: BLE001 - an unreadable store is a typed state
        logger.warning("capability read: canary receipt store unreadable for %s: %s", provider, exc)
        return _cell(
            canary=_canary_block(None),
            installed_live_repair_proven=False,
            cell_state=CELL_UNAVAILABLE,
            reason="the canary receipt store could not be read",
        )
    if receipt is None:
        if provider == "kimi_cli":
            return _cell(
                canary=_canary_block(None),
                installed_live_repair_proven=False,
                cell_state=CELL_UNRESOLVED,
                reason=(
                    "no installed live canary receipt exists and Kimi's session cannot "
                    "be proven without one; Kimi remains unresolved where no session "
                    "is recorded"
                ),
            )
        return _cell(
            canary=_canary_block(None),
            installed_live_repair_proven=False,
            cell_state=CELL_DISABLED,
            reason=(
                "code-supported, but no installed live canary receipt proves the "
                "build on a live pane; static parser support is never green"
            ),
        )

    canary = _canary_block(receipt)
    if receipt["state"] == CANARY_STATE_FAILED:
        canary["state"] = "failed"
        note = receipt["attachment_outcome"] or ""
        reason = "the last installed live canary did not produce a green observation"
        if note:
            reason += f" ({note})"
        return _cell(
            canary=canary,
            installed_live_repair_proven=False,
            cell_state=CELL_DISABLED,
            reason=reason,
            receipt=receipt,
        )
    if (
        not isinstance(receipt["installed_build_banner"], str)
        or not receipt["installed_build_banner"]
        or not isinstance(receipt["installed_build_sha256"], str)
        or len(receipt["installed_build_sha256"]) != 64
        or any(ch not in "0123456789abcdef" for ch in receipt["installed_build_sha256"])
        or not isinstance(receipt["build"], str)
        or not receipt["build"]
        or not isinstance(receipt["operation_id"], str)
        or not receipt["operation_id"]
        or not isinstance(receipt["evidence_sha256"], str)
        or len(receipt["evidence_sha256"]) != 64
        or any(ch not in "0123456789abcdef" for ch in receipt["evidence_sha256"])
        or not isinstance(receipt["status_action_count"], int)
        or isinstance(receipt["status_action_count"], bool)
        or receipt["status_action_count"] not in (0, 1)
    ):
        canary["state"] = "failed"
        return _cell(
            canary=canary,
            installed_live_repair_proven=False,
            cell_state=CELL_DISABLED,
            reason="the canary receipt row is present but unreadable/corrupt",
            receipt=receipt,
        )
    if receipt["build"] not in plan["supported_versions"]:
        canary["state"] = "stale"
        return _cell(
            canary=canary,
            installed_live_repair_proven=False,
            cell_state=CELL_DISABLED,
            reason=(
                f"the canary receipt names build {receipt['build']!r}, which has no "
                "proven status-observation evidence"
            ),
            receipt=receipt,
        )
    if receipt["parser_key"] is not None and receipt["parser_key"] != plan["parser_key"]:
        canary["state"] = "stale"
        return _cell(
            canary=canary,
            installed_live_repair_proven=False,
            cell_state=CELL_DISABLED,
            reason="the canary receipt names a different parser than the current plan",
            receipt=receipt,
        )
    if receipt["status_action_count"] != 1:
        canary["state"] = "stale"
        return _cell(
            canary=canary,
            installed_live_repair_proven=False,
            cell_state=CELL_DISABLED,
            reason=(
                "the canary receipt did not perform exactly one status observation; "
                "zero-action results are truthful but non-green"
            ),
            receipt=receipt,
        )
    # Read-time revalidation: the CURRENT executable must still be the exact
    # bytes the receipt observed.  A deleted, replaced, or drifted executable
    # (same semver banner but different bytes, or a full-banner drift) makes
    # the cell typed stale/disabled — never green, never an exception.
    # Only the existing bounded --version probe plus file hash; no provider
    # model turn.
    try:
        current = _observe_installed_executable(
            provider=provider, executable_path=receipt["executable_path"]
        )
    except nsr.NativeStatusRepairConflict as exc:
        canary["state"] = "stale"
        return _cell(
            canary=canary,
            installed_live_repair_proven=False,
            cell_state=CELL_DISABLED,
            reason=(
                "the installed executable recorded by the canary can no longer be "
                f"observed: {_bounded(str(exc))}"
            ),
            receipt=receipt,
        )
    if (
        current["canonical_path"] != receipt["executable_path"]
        or current["banner"] != receipt["installed_build_banner"]
        or current["sha256"] != receipt["installed_build_sha256"]
        or current["normalized"] != receipt["build"]
    ):
        canary["state"] = "stale"
        return _cell(
            canary=canary,
            installed_live_repair_proven=False,
            cell_state=CELL_DISABLED,
            reason=(
                "the installed executable no longer matches the canary receipt's "
                "observed path/banner/digest; the build changed since the canary ran"
            ),
            receipt=receipt,
        )
    canary["state"] = "matching"
    return _cell(
        canary=canary,
        installed_live_repair_proven=True,
        cell_state=CELL_ENABLED,
        reason="code-supported and the installed executable currently matches the "
        "canary receipt's observed path/banner/digest",
        receipt=receipt,
    )


def provider_capability_cells(db: Any = None) -> dict[str, Any]:
    """The versioned truthful provider capability read (cond-0377D).

    One cell per provider (``claude_code``, ``codex``, ``kimi_cli``,
    ``muse_cli``); DeepSeek/Z.ai are Claude Code route provenance, not
    separate harness identity domains.  A cell is ``enabled`` ONLY with code
    support AND a matching live receipt DERIVED from the committed records;
    parser support alone is never green.
    """

    def _cells() -> dict[str, Any]:
        providers = sorted(nsr.repair_parser_plans())
        return {
            "schema": PROVIDER_CAPABILITY_SCHEMA,
            "generated_at": _now(),
            "providers": [_provider_capability_cell(p) for p in providers],
            "route_provenance_note": (
                "deepseek and zai run Claude Code with route provenance; they are "
                "not separate harness identity domains"
            ),
        }

    if db is not None:
        return _cells()
    with database.SessionLocal() as session:
        return _cells()

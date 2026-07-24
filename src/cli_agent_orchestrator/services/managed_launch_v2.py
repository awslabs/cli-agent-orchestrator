"""Managed-launch protocol v2: generation-private zero-task launch/bind/admit.

v2 adds the bind-before-admit seam on top of the v1 two-phase shape:
``reserve → launching → bound → admitting → admitted``.  No task bytes
reach the provider until the journaled ``native_bound`` reference — the
provider-native pre-turn identity receipts (creation + binding payload
digests), the fork-owned binding record, and the producer fencing token
— exists and matches the digest the caller presents.

Invariant: ``protocol_vintage`` is first-class and immutable; v1
reservations never gain v2 semantics; v2 rows live in the isolated
``managed_launch_v2_reservations`` table so every v1 query/deleter has
zero visibility into them; the launch nonce is stored only as a digest.

Failure mode prevented: admitting a task against an ambient or
recency-derived provider identity (wrong session after any other
activity), or against a generation whose native identity was lost in a
crash window — the bind step makes the exact pre-turn identity a
durable precondition of admission, so crash-before-bind always yields
zero task bytes.

Why this guard exists: the resume/admission contracts downstream can
only bind what was provably bound at launch; an unbound admission would
be unrecoverable except by blind respawn.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.constants import COMPANION_DIR
from cli_agent_orchestrator.models.managed_launch_v2 import (
    PROTOCOL_VERSION_V2,
    ManagedLaunchV2AdmitRequest,
    ManagedLaunchV2BindRequest,
    ManagedLaunchV2ReserveRequest,
)
from cli_agent_orchestrator.services import execution_mode as em
from cli_agent_orchestrator.services import (
    generation_fence,
    heartbeat_store,
    native_attachment,
    recovery_receipts,
)
from cli_agent_orchestrator.services.destructive_endpoint import write_binding_record
from cli_agent_orchestrator.services.managed_launch import (
    ManagedLaunchConflict,
    ManagedLaunchError,
    ManagedLaunchNotFound,
    ManagedLaunchUnavailable,
)
from cli_agent_orchestrator.services.provider_contracts import (
    ProviderContractError,
    check_pinned_version,
)
from cli_agent_orchestrator.utils.terminal import generate_terminal_id

logger = logging.getLogger(__name__)

_READINESS_RECEIPT_KINDS = {
    "codex": "codex-thread-start",
    "kimi_cli": "kimi-acp-session-new",
}
_ISSUANCE_SOURCES = {
    "codex": "app_server_thread_start",
    "kimi_cli": "acp_session_new",
}
_PINNED_PROVIDER = {
    "codex": "codex",
    "kimi_cli": "kimi",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _parse_json(value: Optional[str], default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ManagedLaunchUnavailable("managed-launch v2 record contains invalid JSON") from exc


def _mode_record(row: Any) -> dict[str, Any]:
    """The execution-mode fields of a row, tolerant of pre-contract rows.

    ``getattr`` with a ``None`` default rather than attribute access: a
    row loaded from a database that predates these columns has no such
    attribute, and that absence is exactly the legacy case the mode
    readers are built to classify as ACP.
    """
    return {
        "execution_mode": getattr(row, "execution_mode", None),
        "execution_mode_source": getattr(row, "execution_mode_source", None),
    }


def _row_dict(row: Any) -> dict[str, Any]:
    admission = _parse_json(row.admission_json, None)
    mode_record = _mode_record(row)
    return {
        # Projected through the mode readers, never read raw: every
        # public and durable surface therefore shows a concrete mode,
        # and a legacy row projects as ACP with source 'legacy' instead
        # of a null that a downstream guard might read as "either".
        "execution_mode": em.mode_of_record(mode_record),
        "execution_mode_source": em.source_of_record(mode_record),
        "is_legacy_execution_mode": em.is_legacy_row(mode_record),
        "protocol_version": PROTOCOL_VERSION_V2,
        "protocol_vintage": row.protocol_vintage,
        "reservation_id": row.reservation_id,
        "terminal_id": row.terminal_id,
        "generation": row.generation,
        "session_name": row.session_name,
        "provider": row.provider,
        "agent_profile": row.agent_profile,
        "caller_id": row.caller_id,
        "working_directory": row.working_directory,
        "trusted_project_root": row.trusted_project_root,
        "obligation_generation": row.obligation_generation,
        "task_id": row.task_id,
        "run_id": row.run_id,
        "launch_nonce_digest": row.launch_nonce_digest,
        "state": row.state,
        "request": _parse_json(row.request_json, {}),
        "binding": _parse_json(row.binding_json, None),
        "bind_intent": _parse_json(getattr(row, "bind_intent_json", None), None),
        "admission": admission,
        "launch_failure": (
            admission.get("launch_failure")
            if row.state == "launch-failed-bridge" and isinstance(admission, dict)
            else None
        ),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _query(db: Any, reservation_id: str) -> Any:
    return (
        db.query(database.ManagedLaunchV2ReservationModel)
        .filter(database.ManagedLaunchV2ReservationModel.reservation_id == reservation_id)
        .first()
    )


def native_binding_digest(record: dict[str, Any]) -> Optional[str]:
    """The digest an admit call must present (over the journaled binding)."""
    binding = record.get("binding")
    if not binding:
        return None
    return hashlib.sha256(_canonical_json(binding).encode()).hexdigest()


def _validate_reserve_identity(request: ManagedLaunchV2ReserveRequest) -> dict[str, Any]:
    worktree = os.path.realpath(request.working_directory)
    if worktree != request.working_directory or not os.path.isdir(worktree):
        raise ManagedLaunchConflict(
            "working_directory must be an existing canonical absolute directory"
        )
    if request.provider == "codex":
        if request.trusted_project_root is None:
            raise ManagedLaunchConflict("Codex managed launches require trusted_project_root")
        if os.path.realpath(request.trusted_project_root) != request.trusted_project_root:
            raise ManagedLaunchConflict("trusted_project_root must be canonical")
    elif request.trusted_project_root is not None:
        raise ManagedLaunchConflict("trusted_project_root is valid only for provider=codex")
    if not os.path.isabs(request.provider_executable):
        raise ManagedLaunchConflict("provider_executable must be an absolute path")
    payload = request.model_dump(mode="json")
    # The raw nonce never persists; only its digest is stored.
    payload.pop("launch_nonce")
    return payload


#: Request keys introduced after the v2 reservation surface shipped.  A
#: reservation written before they existed simply has no such key.
_ADDITIVE_REQUEST_KEYS = ("execution_mode", "worker_class")


def _request_matches(stored_json: str, incoming: dict[str, Any]) -> bool:
    """Whether a replayed reserve carries the same immutable request.

    An exact byte match is the normal case.  The one accommodation is
    the upgrade boundary: a reservation written before the
    execution-mode contract has no mode keys at all, and treating that
    absence as "a different request" would turn an ordinary idempotent
    replay into a hard conflict for every in-flight reservation across a
    deploy.  So an absent stored key compares equal to an *unspecified*
    incoming value — and only to that.  A caller that now explicitly
    asks for a mode really is presenting a different request, and still
    conflicts.
    """
    if stored_json == _canonical_json(incoming):
        return True
    stored = _parse_json(stored_json, None)
    if not isinstance(stored, dict):
        return False
    normalized = dict(stored)
    for key in _ADDITIVE_REQUEST_KEYS:
        if key not in normalized and incoming.get(key) is None:
            normalized[key] = None
    return _canonical_json(normalized) == _canonical_json(incoming)


#: Modes ``launch_reserved`` has a real launch branch for.
#:
#: Reserving a mode and launching one are separate questions here, and
#: only the second is gated.  A reservation is a durable statement of
#: intent that ``bind_native`` and the run manifest are entitled to make
#: about a session whose process this surface did not start; refusing
#: native *reservations* would delete that vocabulary.  What must never
#: happen is a reservation that says ``native_tui`` whose process is the
#: ACP bridge, so the refusal sits at the one call that would otherwise
#: start that process.
#:
#: The distinction matters because the resolver defaults several worker
#: classes to native: without this gate, reserving
#: ``worker_class="persistent"`` and calling ``launch_reserved`` would be
#: enough to get an ACP bridge under a reservation row, binding receipt,
#: run manifest, and public status that all say ``native_tui`` — and
#: every one of those is evidence a consumer is entitled to trust.
#:
#: Adding ``em.NATIVE_TUI`` here is therefore the *last* step of building
#: the native branch below, never a precondition for it.
LAUNCHABLE_EXECUTION_MODES: tuple[str, ...] = (em.ACP,)


def _resolve_reserve_mode(request: ManagedLaunchV2ReserveRequest) -> em.ExecutionModeResolution:
    """Resolve the mode at reservation time — before any provider I/O.

    Reserve is the earliest point at which the mode is knowable and the
    last one that is still free of provider effects, so an unresolvable
    or contradictory mode fails here with nothing launched.  Resolution
    failures surface as ``ManagedLaunchConflict`` so callers keep the
    single managed-launch error family instead of having to catch a
    second, unrelated one.
    """
    try:
        return em.resolve(
            launch_input=request.execution_mode,
            worker_class=request.worker_class,
        )
    except em.ExecutionModeError as exc:
        raise ManagedLaunchConflict(str(exc)) from exc


def _allocate_terminal_id(db: Any) -> str:
    for _ in range(128):
        candidate = generate_terminal_id()
        v1_taken = (
            db.query(database.TerminalModel).filter(database.TerminalModel.id == candidate).first()
            is not None
        )
        v2_taken = (
            db.query(database.ManagedLaunchV2ReservationModel)
            .filter(database.ManagedLaunchV2ReservationModel.terminal_id == candidate)
            .first()
            is not None
        )
        if not v1_taken and not v2_taken:
            return candidate
    raise ManagedLaunchUnavailable("could not allocate a unique terminal id")


def reserve(request: ManagedLaunchV2ReserveRequest) -> tuple[dict[str, Any], bool]:
    """Create or idempotently return one immutable v2 reservation."""
    payload = _validate_reserve_identity(request)
    request_json = _canonical_json(payload)
    resolution = _resolve_reserve_mode(request)
    nonce_digest = hashlib.sha256(request.launch_nonce.encode()).hexdigest()
    try:
        with database.SessionLocal() as db:
            existing = _query(db, request.reservation_id)
            if existing is not None:
                if not _request_matches(existing.request_json, payload):
                    raise ManagedLaunchConflict(
                        "reservation_id is already bound to a different request"
                    )
                # The reserved mode is immutable. A replay that restates
                # a different mode is refused rather than adopted, so a
                # reservation can never change branch under a retry.
                em_existing = _mode_record(existing)
                if not em.is_legacy_row(em_existing):
                    try:
                        em.assert_immutable(em.mode_of_record(em_existing), request.execution_mode)
                    except em.ExecutionModeError as exc:
                        raise ManagedLaunchConflict(str(exc)) from exc
                return _row_dict(existing), False
            now = _now()
            row = database.ManagedLaunchV2ReservationModel(
                reservation_id=request.reservation_id,
                terminal_id=_allocate_terminal_id(db),
                generation=str(uuid.uuid4()),
                protocol_vintage="v2",
                session_name=request.session_name,
                provider=request.provider,
                agent_profile=request.agent_profile,
                caller_id=request.caller_id,
                working_directory=request.working_directory,
                trusted_project_root=request.trusted_project_root,
                obligation_generation=request.obligation_generation,
                task_id=request.task_id,
                run_id=request.run_id,
                launch_nonce_digest=nonce_digest,
                state="reserved",
                request_json=request_json,
                execution_mode=resolution.mode,
                execution_mode_source=resolution.source,
                created_at=now,
                updated_at=now,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return _row_dict(row), True
    except ManagedLaunchError:
        raise
    except IntegrityError:
        with database.SessionLocal() as db:
            existing = _query(db, request.reservation_id)
            if existing is None or not _request_matches(existing.request_json, payload):
                raise ManagedLaunchConflict("concurrent reservation conflict")
            return _row_dict(existing), False
    except Exception as exc:  # noqa: BLE001 - fail closed at the store boundary
        raise ManagedLaunchUnavailable(f"managed-launch v2 reservation failed: {exc}") from exc


def get(reservation_id: str) -> dict[str, Any]:
    try:
        with database.SessionLocal() as db:
            row = _query(db, reservation_id)
            if row is None:
                raise ManagedLaunchNotFound(f"v2 reservation not found: {reservation_id}")
            return _row_dict(row)
    except ManagedLaunchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ManagedLaunchUnavailable(f"managed-launch v2 query failed: {exc}") from exc


def claim_launch(reservation_id: str) -> tuple[dict[str, Any], bool]:
    """Atomically claim the one no-task provider launch."""
    try:
        with database.SessionLocal() as db:
            updated = (
                db.query(database.ManagedLaunchV2ReservationModel)
                .filter(
                    database.ManagedLaunchV2ReservationModel.reservation_id == reservation_id,
                    database.ManagedLaunchV2ReservationModel.state == "reserved",
                )
                .update({"state": "launching", "updated_at": _now()}, synchronize_session=False)
            )
            db.commit()
            row = _query(db, reservation_id)
            if row is None:
                raise ManagedLaunchNotFound(f"v2 reservation not found: {reservation_id}")
            if updated == 1:
                return _row_dict(row), True
            if row.state in {
                "launching",
                "bound",
                "admitting",
                "admitted",
                "preflight_blocked",
                "launch-failed-bridge",
            }:
                return _row_dict(row), False
            raise ManagedLaunchUnavailable(f"unknown managed-launch v2 state: {row.state!r}")
    except ManagedLaunchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ManagedLaunchUnavailable(f"managed-launch v2 claim failed: {exc}") from exc


def _mark_preflight_blocked(reservation_id: str, detail: str) -> dict[str, Any]:
    try:
        with database.SessionLocal() as db:
            row = _query(db, reservation_id)
            if row is None:
                raise ManagedLaunchNotFound(f"v2 reservation not found: {reservation_id}")
            if row.state == "preflight_blocked":
                return _row_dict(row)
            if row.state not in {"reserved", "launching"}:
                raise ManagedLaunchConflict(f"preflight cannot block state {row.state!r}")
            row.state = "preflight_blocked"
            row.updated_at = _now()
            db.commit()
            db.refresh(row)
            return _row_dict(row)
    except ManagedLaunchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ManagedLaunchUnavailable(f"preflight persistence failed: {exc}") from exc


def _mark_launch_failed_bridge(
    reservation_id: str,
    bridge_state: dict[str, Any],
) -> dict[str, Any]:
    """CAS the v2 fork-owned launch/delivery records to never-submitted."""
    from cli_agent_orchestrator.services.managed_provider_bridge import (
        BridgeError,
        validate_launch_failure,
    )

    try:
        with database.SessionLocal() as db:
            row = _query(db, reservation_id)
            if row is None:
                raise ManagedLaunchNotFound(f"v2 reservation not found: {reservation_id}")
            request = _parse_json(row.request_json, {})
            delivery_id = request.get("delivery_id")
            if not isinstance(delivery_id, str) or not delivery_id:
                raise ManagedLaunchConflict(
                    "v2 launch failure requires the immutable reservation delivery_id"
                )
            try:
                failure = validate_launch_failure(
                    bridge_state,
                    reservation_id=row.reservation_id,
                    terminal_id=row.terminal_id,
                    generation=row.generation,
                    delivery_id=delivery_id,
                    provider=row.provider,
                )
            except BridgeError as exc:
                raise ManagedLaunchConflict(str(exc)) from exc
            delivery = {
                "schema": "cao-managed-launch-delivery-terminal-v1",
                "delivery_id": delivery_id,
                "status": "never-submitted",
                "reservation_id": row.reservation_id,
                "terminal_id": row.terminal_id,
                "generation": row.generation,
                "failure_evidence_sha256": failure["evidence_sha256"],
                "finalized_at": failure["failed_at"],
                "launch_failure": failure,
            }
            if row.state == "launch-failed-bridge":
                if _parse_json(row.admission_json, None) != delivery:
                    raise ManagedLaunchConflict(
                        "v2 launch-failed-bridge evidence changed after finalization"
                    )
                return _row_dict(row)
            if row.state != "launching":
                raise ManagedLaunchConflict(
                    f"v2 bridge launch failure cannot finalize state {row.state!r}"
                )
            updated = (
                db.query(database.ManagedLaunchV2ReservationModel)
                .filter(
                    database.ManagedLaunchV2ReservationModel.reservation_id == reservation_id,
                    database.ManagedLaunchV2ReservationModel.terminal_id == row.terminal_id,
                    database.ManagedLaunchV2ReservationModel.generation == row.generation,
                    database.ManagedLaunchV2ReservationModel.state == "launching",
                    database.ManagedLaunchV2ReservationModel.binding_json.is_(None),
                    database.ManagedLaunchV2ReservationModel.bind_intent_json.is_(None),
                    database.ManagedLaunchV2ReservationModel.admission_json.is_(None),
                )
                .update(
                    {
                        "state": "launch-failed-bridge",
                        "admission_json": _canonical_json(delivery),
                        "updated_at": _now(),
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            if updated != 1:
                raise ManagedLaunchConflict(
                    "v2 launch failure lost the exact reservation/generation/delivery CAS"
                )
            return _row_dict(_query(db, reservation_id))
    except ManagedLaunchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ManagedLaunchUnavailable(
            f"v2 bridge launch failure finalization failed: {exc}"
        ) from exc


def _validate_readiness_for_bind(row: Any, receipt: dict[str, Any]) -> None:
    """Accept only the exact provider-native readiness receipt for this row."""
    request = _parse_json(row.request_json, {})
    expected_kind = _READINESS_RECEIPT_KINDS.get(row.provider)
    if expected_kind is None or receipt.get("provider_receipt_kind") != expected_kind:
        raise ManagedLaunchConflict(
            f"readiness receipt kind is not the allowlisted provider-native kind: "
            f"{receipt.get('provider_receipt_kind')!r}"
        )
    expected = {
        "reservation_id": row.reservation_id,
        "terminal_id": row.terminal_id,
        "generation": row.generation,
        "provider": row.provider,
        "agent_profile": row.agent_profile,
        "model": request.get("expected_model"),
        "effort": request.get("expected_effort"),
        "working_directory": row.working_directory,
    }
    mismatches = {
        key: {"expected": value, "observed": receipt.get(key)}
        for key, value in expected.items()
        if receipt.get(key) != value
    }
    for field in ("receipt_id", "provider_session_id", "provider_version"):
        if not isinstance(receipt.get(field), str) or not receipt[field]:
            mismatches[field] = {"expected": "non-empty string", "observed": receipt.get(field)}
    if receipt.get("receipt_id") != receipt.get("provider_session_id"):
        mismatches["receipt_id"] = {
            "expected": receipt.get("provider_session_id"),
            "observed": receipt.get("receipt_id"),
        }
    if receipt.get("model_input_ready") is not True:
        mismatches["model_input_ready"] = {
            "expected": True,
            "observed": receipt.get("model_input_ready"),
        }
    if mismatches:
        raise ManagedLaunchConflict(
            "readiness receipt is not bound to the exact v2 reservation: "
            + _canonical_json(mismatches)
        )
    try:
        check_pinned_version(_PINNED_PROVIDER[row.provider], receipt["provider_version"])
    except ProviderContractError as exc:
        raise ManagedLaunchConflict(str(exc)) from exc


def bind_native(reservation_id: str, request: ManagedLaunchV2BindRequest) -> dict[str, Any]:
    """Journal the native-bound identity receipts; zero task bytes before this.

    Reads the provider-native readiness receipt from the generation's
    bridge (a fork-owned fact), derives the creation/binding payload
    digests, publishes the fork-owned binding record, issues the
    producer fencing token, and transitions ``launching → bound``.

    Crash safety: the bind intent — the exact canonical creation/binding/
    route payload bytes, the binding record, and the fencing token — is
    journaled to the reservation row and committed BEFORE any immutable
    external publication.  A crash on either side of the SQL/filesystem
    boundary is reconciled on retry against those journaled bytes: an
    already-published binding record that matches the intent is adopted
    and the row converges to ``bound``; a mismatch is a conflict, never
    a silent re-publication.
    """
    from cli_agent_orchestrator.services.managed_provider_bridge import read_state

    try:
        with database.SessionLocal() as db:
            row = _query(db, reservation_id)
            if row is None:
                raise ManagedLaunchNotFound(f"v2 reservation not found: {reservation_id}")
            if row.terminal_id != request.terminal_id or row.generation != request.generation:
                raise ManagedLaunchConflict("bind identity does not match the reservation")
            if row.state == "bound":
                existing = _parse_json(row.binding_json, None)
                if existing and existing.get("attempt_id") == request.attempt_id:
                    return _row_dict(row)
                raise ManagedLaunchConflict("reservation is already bound to a different attempt")
            if row.state != "launching":
                raise ManagedLaunchConflict(
                    f"native bind requires state 'launching', not {row.state!r}"
                )
            # The reserved mode is the mode of record for this bind. A
            # caller that restates a different one is refused here,
            # before any binding bytes are computed: modes are separate
            # launch branches and a bind must never cross them.
            try:
                bound_mode = em.assert_immutable(
                    em.mode_of_record(_mode_record(row)), request.execution_mode
                )
            except em.ExecutionModeError as exc:
                raise ManagedLaunchConflict(str(exc)) from exc

            intent = _parse_json(row.bind_intent_json, None)
            if intent is not None:
                if intent.get("schema") != "cao-managed-v2-bind-intent-v1":
                    raise ManagedLaunchUnavailable("bind intent journal has an unknown schema")
                if intent.get("attempt_id") != request.attempt_id:
                    raise ManagedLaunchConflict(
                        "a bind intent for a different attempt is already journaled"
                    )
            if intent is None:
                state = read_state(reservation_id)
                receipt = (state or {}).get("readiness")
                if not isinstance(receipt, dict) or (state or {}).get("state") != "ready":
                    raise ManagedLaunchConflict(
                        "native bind requires the bridge's durable ready state with a "
                        "provider-native readiness receipt"
                    )
                _validate_readiness_for_bind(row, receipt)
                intent = _build_bind_intent(db, row, reservation_id, request, receipt, bound_mode)
                # Journal the intent BEFORE the immutable external
                # publication; this commit is the recoverable boundary.
                row.bind_intent_json = _canonical_json(intent)
                row.updated_at = _now()
                db.commit()
                db.refresh(row)
            # Checked on both the fresh and the reconciled path, from the
            # journaled binding rather than the live receipt, so a retry
            # re-validates the exact session it is about to publish.
            _assert_session_not_foreign_held(
                row, intent["binding"]["native_session_id"], bound_mode
            )
            _reconcile_and_complete_bind(db, row, reservation_id, intent)
            return _row_dict(row)
    except ManagedLaunchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ManagedLaunchUnavailable(f"native bind failed: {exc}") from exc


def _assert_session_not_foreign_held(row: Any, native_session_id: str, bound_mode: str) -> None:
    """Refuse a bind whose provider session another owner already holds.

    A provider session is a single-writer resource: two attachments
    interleave turns undetectably, and neither side can tell that the
    transcript it is reading contains another controller's work.  This is
    the bind-time half of that guarantee — it refuses to bind a
    generation onto a session that a *different* live owner holds, and
    refuses in either direction across modes, since ACP and the native
    TUI must never attach to one provider session at the same time.

    Deliberately a *check*, not a claim.  This function does not declare
    an attachment, because at bind time the ACP bootstrap that minted the
    session is still holding it — admission itself flows through that
    bridge.  Declaring ownership here would require asserting
    ``bootstrap_detached_before_launch``, which is false at this point,
    so the claim belongs to the later native-launch step where that
    obligation is genuinely true.  Recording a false obligation would
    corrupt the very evidence the attachment store exists to hold.

    A frozen ``ambiguous`` row refuses unconditionally: ambiguity means
    the owner is precisely what could not be established, and binding
    into that is the double-attach this exists to prevent.
    """
    held = native_attachment.get(row.provider, native_session_id)
    if held is None:
        return
    if held["state"] == native_attachment.AMBIGUOUS:
        raise ManagedLaunchConflict(
            f"{row.provider} session {native_session_id} has a frozen ambiguous "
            f"attachment ({held.get('ambiguity_reason')!r}); refusing to bind onto a "
            "session whose owner could not be established"
        )
    if held["state"] not in native_attachment.LIVE_STATES:
        return
    owner = held["owner"]
    if owner["terminal_id"] == row.terminal_id and owner["generation"] == row.generation:
        # The same generation re-binding its own session. Still not a free
        # pass: the recorded owner mode must match the mode being bound,
        # so a generation cannot switch branch under a retry.
        try:
            em.assert_same_mode(
                owner["execution_mode"],
                bound_mode,
                context=f"bind of {row.provider} session {native_session_id}",
            )
        except em.ExecutionModeError as exc:
            raise ManagedLaunchConflict(str(exc)) from exc
        return
    raise ManagedLaunchConflict(
        f"{row.provider} session {native_session_id} is already held in "
        f"{owner['execution_mode']!r} mode by terminal={owner['terminal_id']} "
        f"generation={owner['generation']}; a {bound_mode!r} bind for "
        f"terminal={row.terminal_id} generation={row.generation} would be a second "
        "concurrent attachment to one provider session"
    )


def _build_bind_intent(
    db: Any,
    row: Any,
    reservation_id: str,
    request: ManagedLaunchV2BindRequest,
    receipt: dict[str, Any],
    bound_mode: str,
) -> dict[str, Any]:
    """Compute the exact bind intent (payload bytes + token) for one attempt."""
    terminal = (
        db.query(database.TerminalModel)
        .filter(database.TerminalModel.id == row.terminal_id)
        .first()
    )
    if terminal is None:
        terminal = (
            db.query(database.ManagedLaunchV2TerminalModel)
            .filter(database.ManagedLaunchV2TerminalModel.id == row.terminal_id)
            .first()
        )
    tmux_incarnation = (
        f"tmux:{terminal.tmux_session}:{terminal.window_id}:{terminal.pane_id}"
        if terminal is not None
        else f"tmux:{row.session_name}:unknown:unknown"
    )
    route_digest = hashlib.sha256(
        _canonical_json(
            {
                "model": receipt["model"],
                "effort": receipt["effort"],
                "agent_profile": row.agent_profile,
            }
        ).encode()
    ).hexdigest()
    created_at = _now()
    # The worktree head is a fork-observable fact (read-only git);
    # a worktree that cannot report its head fails the bind closed.
    import subprocess

    try:
        head_proc = subprocess.run(
            ["git", "-C", row.working_directory, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ManagedLaunchConflict(
            f"worktree head is not readable for the binding: {exc}"
        ) from exc
    head = head_proc.stdout.strip()
    if (
        head_proc.returncode != 0
        or len(head) != 40
        or any(ch not in "0123456789abcdef" for ch in head)
    ):
        raise ManagedLaunchConflict(
            "worktree head is not a full lowercase hex OID; the binding " "requires the exact head"
        )
    creation_payload = recovery_receipts.creation_payload(
        provider=_PINNED_PROVIDER[row.provider],
        native_id=receipt["provider_session_id"],
        provider_version=receipt["provider_version"],
        issuance_source=_ISSUANCE_SOURCES[row.provider],
        obligation_generation=row.obligation_generation,
        task_id=row.task_id,
        run_id=row.run_id,
        created_at=created_at,
    )
    binding_payload = recovery_receipts.binding_payload(
        provider=_PINNED_PROVIDER[row.provider],
        native_id=receipt["provider_session_id"],
        launch_nonce_digest=row.launch_nonce_digest,
        provider_process_identity=f"bridge:{reservation_id}",
        tmux_incarnation=tmux_incarnation,
        terminal_generation=row.generation,
        worktree_realpath=row.working_directory,
        repository=os.path.basename(row.working_directory),
        head=head,
        assigned_route_digest=route_digest,
        bound_at=created_at,
    )
    token = heartbeat_store.issue_fencing_token(
        COMPANION_DIR, row.terminal_id, row.generation, request.attempt_id
    )
    # The fork-side route fact: an unobserved route payload (PF-2 is
    # red for every pinned provider — no provider-observed fields may
    # be claimed).  The conductor binds the §6.3 segment chain from
    # these facts; the heartbeat's route field binds this digest.
    route_payload = recovery_receipts.route_payload(
        provider=_PINNED_PROVIDER[row.provider],
        native_id=receipt["provider_session_id"],
        authority_status="unobserved",
        assigned_model=receipt["model"],
        assigned_effort=receipt["effort"],
        assigned_policy_sha256=receipt.get("profile_sha256") or ("0" * 64),
        assigned_profile_sha256=receipt.get("profile_sha256") or ("0" * 64),
        assigned_config_sha256=receipt.get("protected_config_sha256") or ("0" * 64),
        requested_model=receipt["model"],
        requested_effort=receipt["effort"],
        observed_model=None,
        observed_effort=None,
        protocol_version=None,
        event_sequence=None,
        native_turn_id=None,
        attested_at=created_at,
    )
    route_payload_digest = recovery_receipts.payload_digest(route_payload)
    # Both receipts carry the execution mode, and the binding digest an
    # admit call must present is computed over the binding — so a
    # receipt minted under one mode cannot satisfy admission under the
    # other.  This is the concrete reason the tag lives in the receipt
    # rather than only in the row: the row can be re-read, but the
    # digest is what the admission actually checks.
    binding_record = {
        "schema": "cao-generation-binding-v1",
        "reservation_id": reservation_id,
        "terminal_id": row.terminal_id,
        "generation": row.generation,
        "attempt_id": request.attempt_id,
        "launch_nonce_digest": row.launch_nonce_digest,
        "fencing_token_id": token.id,
        "provider": row.provider,
        "execution_mode": bound_mode,
        "native_session_id": receipt["provider_session_id"],
        "assigned_policy_sha256": receipt.get("profile_sha256"),
        "route_payload_sha256": route_payload_digest,
        "bound_at": created_at,
    }
    binding = {
        "schema": "cao-managed-v2-native-binding-v1",
        "attempt_id": request.attempt_id,
        "execution_mode": bound_mode,
        "native_session_id": receipt["provider_session_id"],
        "provider_version": receipt["provider_version"],
        "issuance_source": _ISSUANCE_SOURCES[row.provider],
        "creation_payload_sha256": recovery_receipts.payload_digest(creation_payload),
        "binding_payload_sha256": recovery_receipts.payload_digest(binding_payload),
        "fencing_token_id": token.id,
        "fence_no": token.fence_no,
        "assigned_route_digest": route_digest,
        "bound_at": created_at,
    }
    import base64

    return {
        "schema": "cao-managed-v2-bind-intent-v1",
        "attempt_id": request.attempt_id,
        "fencing_token": token.as_dict(),
        # The exact canonical payload bytes (base64 for JSON durability),
        # not only their digests: a reconciled retry verifies the journaled
        # bytes still digest to the journaled binding's payload digests.
        "creation_payload_b64": base64.b64encode(creation_payload).decode("ascii"),
        "binding_payload_b64": base64.b64encode(binding_payload).decode("ascii"),
        "route_payload_b64": base64.b64encode(route_payload).decode("ascii"),
        "binding_record": binding_record,
        "binding": binding,
    }


def _reconcile_and_complete_bind(
    db: Any, row: Any, reservation_id: str, intent: dict[str, Any]
) -> None:
    """Converge the journaled intent across the SQL/filesystem boundary.

    The binding record may already exist (crash after publication,
    before the SQL commit): it must match the journaled bytes exactly
    and the journaled fencing token must still be the registered one;
    then the row commits ``bound``.  Absent the record, it is published
    now from the journaled bytes.
    """
    from cli_agent_orchestrator.services.destructive_endpoint import binding_record_path

    record_path = binding_record_path(COMPANION_DIR, row.terminal_id, row.generation)
    expected_record = intent["binding_record"]
    if record_path.exists():
        try:
            existing = json.loads(record_path.read_bytes())
        except (OSError, json.JSONDecodeError) as exc:
            raise ManagedLaunchConflict(
                f"existing binding record is unreadable; cannot reconcile: {exc}"
            ) from exc
        if existing != expected_record:
            raise ManagedLaunchConflict(
                "existing binding record does not match the journaled bind "
                "intent; refusing to overwrite immutable publication"
            )
    registered = heartbeat_store.current_fencing_token(COMPANION_DIR, row.terminal_id)
    if registered is None or registered.id != intent["fencing_token"]["id"]:
        raise ManagedLaunchConflict(
            "journaled fencing token is not the registered one; refusing to bind"
        )
    # The journaled canonical payload bytes must still digest to the
    # journaled binding's payload digests (journal tamper check).
    import base64

    binding = intent["binding"]
    for field, digest_key in (
        ("creation_payload_b64", "creation_payload_sha256"),
        ("binding_payload_b64", "binding_payload_sha256"),
    ):
        if recovery_receipts.payload_digest(base64.b64decode(intent[field])) != binding[digest_key]:
            raise ManagedLaunchConflict(
                "journaled bind payload bytes do not match the journaled "
                "binding digests; refusing to reconcile"
            )
    if not record_path.exists():
        write_binding_record(
            COMPANION_DIR,
            terminal_id=row.terminal_id,
            generation=row.generation,
            reservation_id=reservation_id,
            attempt_id=intent["attempt_id"],
            launch_nonce_digest=row.launch_nonce_digest,
            fencing_token_id=intent["fencing_token"]["id"],
            provider=row.provider,
            native_session_id=expected_record["native_session_id"],
            assigned_policy_sha256=expected_record["assigned_policy_sha256"],
            route_payload_sha256=expected_record["route_payload_sha256"],
            bound_at=expected_record["bound_at"],
            # Read from the journaled record, not recomputed, so the
            # published bytes always equal the bytes a later reconcile
            # compares against.  ``.get`` because an intent journaled
            # before the execution-mode contract has no such key: that
            # record must publish exactly as it was journaled, without a
            # mode grafted on.
            execution_mode=expected_record.get("execution_mode"),
        )
    row.binding_json = _canonical_json(intent["binding"])
    row.state = "bound"
    row.updated_at = _now()
    db.commit()
    db.refresh(row)


def claim_admission(
    reservation_id: str, request: ManagedLaunchV2AdmitRequest
) -> tuple[dict[str, Any], bool]:
    """Claim the one task admission — only against a matching native_bound."""
    actual_digest = hashlib.sha256(request.message.encode("utf-8")).hexdigest()
    if actual_digest != request.message_sha256:
        raise ManagedLaunchConflict("message_sha256 does not match message bytes")
    try:
        with database.SessionLocal() as db:
            row = _query(db, reservation_id)
            if row is None:
                raise ManagedLaunchNotFound(f"v2 reservation not found: {reservation_id}")
            if request.delivery_id != _parse_json(row.request_json, {}).get("delivery_id"):
                raise ManagedLaunchConflict(
                    "delivery_id does not match the immutable v2 reservation delivery identity"
                )
            record = _row_dict(row)
            expected_digest = native_binding_digest(record)
            if expected_digest is None or request.native_binding_digest != expected_digest:
                # Bind-before-admit: without the journaled native_bound
                # reference the admission never happens — zero task bytes.
                raise ManagedLaunchConflict(
                    "task admission requires the journaled native_bound reference; "
                    "zero task bytes are sent without it"
                )
            admission = {
                "delivery_id": request.delivery_id,
                "message_sha256": request.message_sha256,
                "sender_id": request.sender_id,
                "orchestration_type": request.orchestration_type,
                "context": request.context.model_dump(mode="json"),
                "native_binding_digest": request.native_binding_digest,
                "status": "io-attempted",
                "attempted_at": _now(),
            }
            updated = (
                db.query(database.ManagedLaunchV2ReservationModel)
                .filter(
                    database.ManagedLaunchV2ReservationModel.reservation_id == reservation_id,
                    database.ManagedLaunchV2ReservationModel.state == "bound",
                    database.ManagedLaunchV2ReservationModel.binding_json.is_not(None),
                    database.ManagedLaunchV2ReservationModel.admission_json.is_(None),
                )
                .update(
                    {
                        "admission_json": _canonical_json(admission),
                        "state": "admitting",
                        "updated_at": _now(),
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            row = _query(db, reservation_id)
            if updated == 1:
                return _row_dict(row), True
            existing = _parse_json(row.admission_json, None)
            if existing is not None:
                # Replay binds the FULL admission identity: the same
                # delivery id with a changed message, sender, context,
                # orchestration type, or binding is a different immutable
                # identity and is refused — never treated as a safe replay.
                replay_identity = {
                    "delivery_id": request.delivery_id,
                    "message_sha256": request.message_sha256,
                    "sender_id": request.sender_id,
                    "orchestration_type": request.orchestration_type,
                    "context": request.context.model_dump(mode="json"),
                    "native_binding_digest": request.native_binding_digest,
                }
                mismatches = [
                    key for key, value in replay_identity.items() if existing.get(key) != value
                ]
                if mismatches:
                    raise ManagedLaunchConflict(
                        "delivery_id is already bound to a different admission "
                        f"identity: {sorted(mismatches)}"
                    )
                return _row_dict(row), False
            raise ManagedLaunchConflict(f"task admission requires state 'bound', not {row.state!r}")
    except ManagedLaunchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ManagedLaunchUnavailable(f"task admission claim failed: {exc}") from exc


def complete_admission(
    reservation_id: str,
    delivery_id: str,
    provider_receipt: dict[str, Any],
) -> dict[str, Any]:
    """Complete admission from the provider-native submission receipt."""
    try:
        with database.SessionLocal() as db:
            row = _query(db, reservation_id)
            if row is None:
                raise ManagedLaunchNotFound(f"v2 reservation not found: {reservation_id}")
            admission = _parse_json(row.admission_json, None)
            if not admission or admission.get("delivery_id") != delivery_id:
                raise ManagedLaunchConflict("delivery_id does not match the admission claim")
            if admission.get("status") == "admitted":
                if admission.get("provider_submission_receipt") != provider_receipt:
                    raise ManagedLaunchConflict(
                        "provider submission receipt changed after admission"
                    )
                return _row_dict(row)
            if row.state != "admitting":
                raise ManagedLaunchConflict(f"admission cannot complete from state {row.state!r}")
            expected_kind = {
                "codex": "codex-turn-start",
                "kimi_cli": "kimi-session-update",
            }.get(row.provider)
            if (
                expected_kind is None
                or provider_receipt.get("provider_receipt_kind") != expected_kind
            ):
                raise ManagedLaunchConflict("submission receipt kind is not provider-native")
            for field in ("receipt_id", "provider_session_id", "provider_turn_id"):
                if not isinstance(provider_receipt.get(field), str) or not provider_receipt[field]:
                    raise ManagedLaunchConflict(f"submission receipt lacks {field}")
            binding = _parse_json(row.binding_json, {})
            if provider_receipt["provider_session_id"] != binding.get("native_session_id"):
                raise ManagedLaunchConflict(
                    "submission receipt session does not match the bound native identity"
                )
            admission["provider_submission_receipt"] = provider_receipt
            admission["status"] = "admitted"
            admission["admitted_at"] = _now()
            row.admission_json = _canonical_json(admission)
            row.state = "admitted"
            row.updated_at = _now()
            db.commit()
            db.refresh(row)
            return _row_dict(row)
    except ManagedLaunchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ManagedLaunchUnavailable(f"task admission completion failed: {exc}") from exc


def mark_admission_ambiguous(reservation_id: str, delivery_id: str, detail: str) -> dict[str, Any]:
    """The honest at-most-once state: possibly submitted, never replayed."""
    try:
        with database.SessionLocal() as db:
            row = _query(db, reservation_id)
            if row is None:
                raise ManagedLaunchNotFound(f"v2 reservation not found: {reservation_id}")
            admission = _parse_json(row.admission_json, None)
            if not admission or admission.get("delivery_id") != delivery_id:
                raise ManagedLaunchConflict("delivery_id does not match the admission claim")
            if admission.get("status") == "admitted":
                return _row_dict(row)
            admission["status"] = "ambiguous_preserved"
            admission["detail"] = detail
            admission["updated_at"] = _now()
            row.admission_json = _canonical_json(admission)
            row.state = "admitting"
            row.updated_at = _now()
            db.commit()
            db.refresh(row)
            return _row_dict(row)
    except ManagedLaunchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ManagedLaunchUnavailable(f"ambiguous admission persistence failed: {exc}") from exc


async def launch_reserved(reservation_id: str, *, registry=None) -> dict[str, Any]:
    """Launch a reserved v2 generation without carrying task bytes.

    Mirrors the v1 zero-keystroke managed spawn; the native bind is a
    separate explicit step so a crash anywhere before bind yields zero
    task bytes by construction.
    """
    import asyncio

    from cli_agent_orchestrator.services import terminal_service
    from cli_agent_orchestrator.services.managed_provider_bridge import (
        BRIDGE_VERSION,
        launch_binding_identity,
        profile_digest,
        read_state,
        request_bridge,
        write_request,
    )

    # Gate the mode before claiming, not after.  ``claim_launch`` moves
    # the reservation to ``launching``, and a refusal raised past that
    # point would strand the row in a state that says a launch is in
    # flight when none is.  Read-then-claim leaves an unlaunchable
    # reservation exactly as it was.
    # ``get`` projects through the mode readers, so this is always a
    # concrete mode — a legacy row reads ACP rather than null.
    reserved_mode = str(get(reservation_id)["execution_mode"])
    if reserved_mode not in LAUNCHABLE_EXECUTION_MODES:
        raise ManagedLaunchConflict(
            f"reservation {reservation_id} is bound to execution_mode {reserved_mode!r}, "
            f"which this surface has no launch branch for; launchable modes are "
            f"{list(LAUNCHABLE_EXECUTION_MODES)}. Refusing rather than starting the ACP "
            "bridge under a reservation that says otherwise"
        )

    record, should_launch = claim_launch(reservation_id)
    if not should_launch:
        return record
    request = record["request"]
    try:
        executable = request["provider_executable"]
        digest = request["provider_executable_sha256"]
        if os.path.realpath(executable) != executable or not os.path.isfile(executable):
            raise ManagedLaunchConflict("provider executable must be a canonical absolute path")
        with open(executable, "rb") as handle:
            if hashlib.sha256(handle.read()).hexdigest() != digest:
                raise ManagedLaunchConflict("provider executable digest drifted from the pin")
        rendezvous_identity = launch_binding_identity(
            project=request.get("project") or record["run_id"],
            task_id=record["task_id"] or record["run_id"],
            terminal_id=record["terminal_id"],
            terminal_generation=record["generation"],
            working_directory=record["working_directory"],
            actor=record["caller_id"],
        )
        bridge_request = {
            "bridge_version": BRIDGE_VERSION,
            "controller_pid": os.getpid(),
            "reservation_id": reservation_id,
            "terminal_id": record["terminal_id"],
            "generation": record["generation"],
            "provider": record["provider"],
            "agent_profile": record["agent_profile"],
            "profile_sha256": profile_digest(record["agent_profile"]),
            "model": request["expected_model"],
            "effort": request["expected_effort"],
            "working_directory": record["working_directory"],
            "provider_executable": executable,
            "provider_executable_sha256": digest,
            # v2 identity fields: the bridge emits fenced heartbeats only
            # when these are present (v1 requests never carry them).  The
            # immutable project identity persists from the reservation; it
            # is never silently dropped (model_dump includes the key with
            # a None value when unset, so a plain .get default would not
            # fall back).
            "project": request.get("project") or record["run_id"],
            "task_id": record["task_id"],
            "delivery_id": request["delivery_id"],
            "run_id": record["run_id"],
            "obligation_generation": record["obligation_generation"],
            "assigned_policy_sha256": profile_digest(record["agent_profile"]),
            "rendezvous_identity": rendezvous_identity,
        }
        write_request(reservation_id, bridge_request)
    except Exception as exc:  # noqa: BLE001 - no provider I/O occurred
        return _mark_preflight_blocked(reservation_id, str(exc))

    try:
        await terminal_service.create_terminal(
            provider=record["provider"],
            agent_profile=record["agent_profile"],
            session_name=record["session_name"],
            new_session=False,
            working_directory=record["working_directory"],
            registry=registry,
            caller_id=record["caller_id"],
            defer_init=False,
            initial_message=None,
            reserved_terminal_id=record["terminal_id"],
            terminal_generation=record["generation"],
            trusted_project_root=record["trusted_project_root"],
            expected_model=request["expected_model"],
            expected_effort=request["expected_effort"],
            preserve_on_init_failure=True,
            managed_native_command=[
                os.path.abspath(sys.executable),
                "-I",
                "-m",
                "cli_agent_orchestrator.services.managed_provider_bridge",
                "--reservation-id",
                reservation_id,
            ],
            # v2 terminals persist only to the isolated v2 surface; the
            # shared terminals table (and every old cleanup/list path)
            # never sees them.
            protocol_vintage="v2",
        )
    except Exception as exc:  # noqa: BLE001 - preserve and expose, never cleanup/retry
        try:
            state = read_state(reservation_id)
        except Exception:  # noqa: BLE001 - generic startup evidence remains truthful
            state = None
        if state and state.get("state") == "launch-failed-bridge":
            return _mark_launch_failed_bridge(reservation_id, state)
        return _mark_preflight_blocked(reservation_id, str(exc))

    try:
        status = await asyncio.to_thread(
            request_bridge, reservation_id, {"op": "status"}, timeout=120.0
        )
    except Exception as exc:  # noqa: BLE001 - query durable state before classifying
        try:
            state = read_state(reservation_id)
        except Exception:  # noqa: BLE001
            state = None
        if state and state.get("state") == "launch-failed-bridge":
            return _mark_launch_failed_bridge(reservation_id, state)
        detail = str((state or {}).get("error") or exc)
        return _mark_preflight_blocked(
            reservation_id, f"exact provider readiness was not established: {detail}"
        )
    if status.get("state") != "ready" or not isinstance(status.get("readiness"), dict):
        return _mark_preflight_blocked(
            reservation_id, "exact provider session did not return a readiness receipt"
        )
    return get(reservation_id)


async def admit_reserved(
    reservation_id: str,
    request: ManagedLaunchV2AdmitRequest,
    *,
    registry=None,
) -> dict[str, Any]:
    """Admit one task after native bind, with no blind retry on ambiguity."""
    import asyncio

    from cli_agent_orchestrator.services.managed_provider_bridge import (
        read_state,
        request_bridge,
    )

    record = get(reservation_id)
    # The fence check is the admission boundary: a sealed generation
    # rejects every post-fence input with zero provider I/O.
    generation_fence.assert_admission_open(
        COMPANION_DIR, record["terminal_id"], record["generation"]
    )
    record, should_send = claim_admission(reservation_id, request)
    if not should_send:
        if record["state"] == "admitting":
            state = read_state(reservation_id)
            receipt = state.get("submission") if state else None
            if isinstance(receipt, dict):
                return complete_admission(reservation_id, request.delivery_id, receipt)
        return record
    command = {
        "op": "admit",
        "reservation_id": reservation_id,
        "terminal_id": record["terminal_id"],
        "generation": record["generation"],
        "delivery_id": request.delivery_id,
        "message": request.message,
        "message_sha256": request.message_sha256,
        "sender_id": request.sender_id,
        "orchestration_type": request.orchestration_type,
        "context": request.context.model_dump(mode="json"),
    }
    try:
        response = await asyncio.to_thread(request_bridge, reservation_id, command, timeout=120.0)
    except Exception as exc:  # noqa: BLE001 - delivery may have crossed the boundary
        try:
            state = read_state(reservation_id)
        except Exception:  # noqa: BLE001
            state = None
        receipt = state.get("submission") if state else None
        if isinstance(receipt, dict):
            return complete_admission(reservation_id, request.delivery_id, receipt)
        return mark_admission_ambiguous(reservation_id, request.delivery_id, str(exc))
    receipt = response.get("receipt")
    if not isinstance(receipt, dict):
        return mark_admission_ambiguous(
            reservation_id,
            request.delivery_id,
            "provider bridge returned no structured submission receipt",
        )
    return complete_admission(reservation_id, request.delivery_id, receipt)


def attempt_resume(reservation_id: str, *, containment_proven: bool = False) -> dict[str, Any]:
    """The v2 resume seam — fail-closed while prior-generation proof is red.

    Prior-generation quiescence requires the containment-backed
    no-survivor proof, which is PF-1b-gated; while unproven every resume
    attempt is refused (45) and journaled as a refusal payload fact.
    """
    record = get(reservation_id)
    attempt_id = str(uuid.uuid4())
    binding = record.get("binding") or {}
    payload = recovery_receipts.resume_refusal_payload(
        provider=_PINNED_PROVIDER.get(record["provider"], "codex"),
        native_id=binding.get("native_session_id", "unbound"),
        resume_attempt_id=attempt_id,
        refusal_code=45,
        reason="prior-generation-unproven" if not containment_proven else "containment-lost",
        at=_now(),
    )
    digest = recovery_receipts.payload_digest(payload)
    if not containment_proven:
        raise ManagedLaunchConflict(
            "resume refused (45 prior-generation-unproven): prior-generation "
            "quiescence requires the containment-backed no-survivor proof, "
            f"which is PF-1b-gated and not green; refusal payload {digest}"
        )
    raise ManagedLaunchConflict(
        "resume refused (45): the resume admission state machine is conductor-owned; "
        "the fork seam records facts only"
    )

"""Durable reserve/launch/observe/admit state for managed task delivery.

The store is the response-loss boundary between a conductor and CAO.  A
caller-chosen reservation UUID is persisted before provider I/O; the immutable
terminal id and generation can always be queried by that UUID.  Admission is a
separate, idempotent operation and is refused until a generation-bound
readiness receipt exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.managed_launch import (
    PROTOCOL_VERSION,
    ManagedLaunchAdmitRequest,
    ManagedLaunchCleanupRequest,
    ManagedLaunchObservationRequest,
    ManagedLaunchReserveRequest,
    ManagedLaunchRouteAttestRequest,
)
from cli_agent_orchestrator.utils.terminal import generate_terminal_id


class ManagedLaunchError(RuntimeError):
    """Base error for the managed-launch protocol."""


class ManagedLaunchNotFound(ManagedLaunchError):
    pass


class ManagedLaunchConflict(ManagedLaunchError):
    pass


class ManagedLaunchUnavailable(ManagedLaunchError):
    pass


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
        raise ManagedLaunchUnavailable("managed-launch record contains invalid JSON") from exc


def _row_dict(row: Any) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "reservation_id": row.reservation_id,
        "terminal_id": row.terminal_id,
        "generation": row.generation,
        "session_name": row.session_name,
        "provider": row.provider,
        "agent_profile": row.agent_profile,
        "caller_id": row.caller_id,
        "working_directory": row.working_directory,
        "trusted_project_root": row.trusted_project_root,
        "state": row.state,
        "request": _parse_json(row.request_json, {}),
        "observations": _parse_json(row.observations_json, []),
        "readiness": _parse_json(row.readiness_json, None),
        "admission": _parse_json(row.admission_json, None),
        "negative": _parse_json(row.negative_json, None),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _query(db: Any, reservation_id: str) -> Any:
    return (
        db.query(database.ManagedLaunchReservationModel)
        .filter(database.ManagedLaunchReservationModel.reservation_id == reservation_id)
        .first()
    )


def _assert_bound_evidence(row: Any, evidence: dict[str, Any]) -> None:
    """Reject evidence not bound to the reservation's exact generation/route."""
    request = _parse_json(row.request_json, {})
    expected = {
        "terminal_id": row.terminal_id,
        "generation": row.generation,
        "provider": row.provider,
        "agent_profile": row.agent_profile,
        "model": request.get("expected_model"),
        "effort": request.get("expected_effort"),
    }
    mismatches = {
        key: {"expected": value, "observed": evidence.get(key)}
        for key, value in expected.items()
        if evidence.get(key) != value
    }
    if mismatches:
        raise ManagedLaunchConflict(
            f"evidence identity does not match reservation: {_canonical_json(mismatches)}"
        )


def _validate_request_identity(request: ManagedLaunchReserveRequest) -> dict[str, Any]:
    worktree = os.path.realpath(request.working_directory)
    if worktree != request.working_directory or not os.path.isdir(worktree):
        raise ManagedLaunchConflict(
            "working_directory must be an existing canonical absolute directory"
        )
    trusted = request.trusted_project_root
    if request.provider == "codex":
        if trusted is None:
            raise ManagedLaunchConflict("Codex managed launches require trusted_project_root")
        if os.path.realpath(trusted) != trusted or trusted != worktree:
            raise ManagedLaunchConflict(
                "trusted_project_root must equal the canonical working_directory"
            )
    elif trusted is not None:
        raise ManagedLaunchConflict("trusted_project_root is valid only for provider=codex")
    return request.model_dump(mode="json")


def _allocate_terminal_id(db) -> str:
    for _ in range(128):
        candidate = generate_terminal_id()
        terminal_exists = (
            db.query(database.TerminalModel).filter(database.TerminalModel.id == candidate).first()
            is not None
        )
        reserved = (
            db.query(database.ManagedLaunchReservationModel)
            .filter(database.ManagedLaunchReservationModel.terminal_id == candidate)
            .first()
            is not None
        )
        if not terminal_exists and not reserved:
            return candidate
    raise ManagedLaunchUnavailable("could not allocate a unique terminal id")


def reserve(request: ManagedLaunchReserveRequest) -> tuple[dict[str, Any], bool]:
    """Create or idempotently return one immutable reservation.

    Returns ``(record, created)``.  A reused reservation id with any changed
    request field is a conflict rather than a mutable update.
    """
    payload = _validate_request_identity(request)
    request_json = _canonical_json(payload)
    try:
        with database.SessionLocal() as db:
            existing = _query(db, request.reservation_id)
            if existing is not None:
                if existing.request_json != request_json:
                    raise ManagedLaunchConflict(
                        "reservation_id is already bound to a different request"
                    )
                return _row_dict(existing), False
            now = _now()
            row = database.ManagedLaunchReservationModel(
                reservation_id=request.reservation_id,
                terminal_id=_allocate_terminal_id(db),
                generation=str(uuid.uuid4()),
                session_name=request.session_name,
                provider=request.provider,
                agent_profile=request.agent_profile,
                caller_id=request.caller_id,
                working_directory=request.working_directory,
                trusted_project_root=request.trusted_project_root,
                state="reserved",
                request_json=request_json,
                observations_json="[]",
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
        # A concurrent identical reserve may win the unique insert.  Re-query
        # by the caller's idempotency key; never allocate a second generation.
        with database.SessionLocal() as db:
            existing = _query(db, request.reservation_id)
            if existing is None or existing.request_json != request_json:
                raise ManagedLaunchConflict("concurrent reservation conflict")
            return _row_dict(existing), False
    except Exception as exc:  # noqa: BLE001 - fail closed at the store boundary
        raise ManagedLaunchUnavailable(f"managed-launch reservation failed: {exc}") from exc


def get(reservation_id: str) -> dict[str, Any]:
    try:
        with database.SessionLocal() as db:
            row = _query(db, reservation_id)
            if row is None:
                raise ManagedLaunchNotFound(f"reservation not found: {reservation_id}")
            return _row_dict(row)
    except ManagedLaunchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ManagedLaunchUnavailable(f"managed-launch query failed: {exc}") from exc


def claim_launch(reservation_id: str) -> tuple[dict[str, Any], bool]:
    """Atomically claim the one no-task provider launch."""
    try:
        with database.SessionLocal() as db:
            updated = (
                db.query(database.ManagedLaunchReservationModel)
                .filter(
                    database.ManagedLaunchReservationModel.reservation_id == reservation_id,
                    database.ManagedLaunchReservationModel.state == "reserved",
                )
                .update(
                    {"state": "launching", "updated_at": _now()},
                    synchronize_session=False,
                )
            )
            db.commit()
            row = _query(db, reservation_id)
            if row is None:
                raise ManagedLaunchNotFound(f"reservation not found: {reservation_id}")
            if updated == 1:
                return _row_dict(row), True
            if row.state in {
                "launching",
                "ready",
                "preflight_blocked",
                "admitting",
                "admitted",
                "cancelled",
                "negative",
            }:
                return _row_dict(row), False
            raise ManagedLaunchUnavailable(f"unknown managed-launch state: {row.state!r}")
    except ManagedLaunchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ManagedLaunchUnavailable(f"managed-launch claim failed: {exc}") from exc


def mark_ready(
    reservation_id: str,
    *,
    terminal_id: str,
    generation: str,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    try:
        with database.SessionLocal() as db:
            row = _query(db, reservation_id)
            if row is None:
                raise ManagedLaunchNotFound(f"reservation not found: {reservation_id}")
            if row.terminal_id != terminal_id or row.generation != generation:
                raise ManagedLaunchConflict("readiness identity does not match the reservation")
            _assert_bound_evidence(row, receipt)
            if row.state == "ready":
                if _parse_json(row.readiness_json, None) != receipt:
                    raise ManagedLaunchConflict("readiness receipt changed after attestation")
                return _row_dict(row)
            if row.state != "launching":
                raise ManagedLaunchConflict(
                    f"readiness cannot be recorded from state {row.state!r}"
                )
            updated = (
                db.query(database.ManagedLaunchReservationModel)
                .filter(
                    database.ManagedLaunchReservationModel.reservation_id == reservation_id,
                    database.ManagedLaunchReservationModel.state == "launching",
                    database.ManagedLaunchReservationModel.readiness_json.is_(None),
                )
                .update(
                    {
                        "readiness_json": _canonical_json(receipt),
                        "state": "ready",
                        "updated_at": _now(),
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            current = _query(db, reservation_id)
            if updated == 1:
                return _row_dict(current)
            if current is not None and current.state == "ready":
                if _parse_json(current.readiness_json, None) == receipt:
                    return _row_dict(current)
            state = current.state if current is not None else "missing"
            raise ManagedLaunchConflict(
                f"readiness lost a concurrent transition to state {state!r}"
            )
    except ManagedLaunchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ManagedLaunchUnavailable(f"readiness persistence failed: {exc}") from exc


def mark_preflight_blocked(
    reservation_id: str,
    *,
    preflight_class: str,
    detail: str,
    evidence: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    observation = {
        "observation_id": str(uuid.uuid4()),
        "kind": "preflight",
        "preflight_class": preflight_class,
        "detail": detail,
        "evidence": evidence,
        "observed_at": _now(),
    }
    try:
        for _ in range(16):
            with database.SessionLocal() as db:
                row = _query(db, reservation_id)
                if row is None:
                    raise ManagedLaunchNotFound(f"reservation not found: {reservation_id}")
                if row.state == "preflight_blocked":
                    return _row_dict(row)
                if row.state not in {"reserved", "launching"}:
                    raise ManagedLaunchConflict(f"preflight cannot block state {row.state!r}")
                prior_observations = row.observations_json
                observations = _parse_json(prior_observations, [])
                observations.append(observation)
                updated = (
                    db.query(database.ManagedLaunchReservationModel)
                    .filter(
                        database.ManagedLaunchReservationModel.reservation_id == reservation_id,
                        database.ManagedLaunchReservationModel.state == row.state,
                        database.ManagedLaunchReservationModel.observations_json
                        == prior_observations,
                    )
                    .update(
                        {
                            "observations_json": _canonical_json(observations),
                            "state": "preflight_blocked",
                            "updated_at": _now(),
                        },
                        synchronize_session=False,
                    )
                )
                db.commit()
                if updated == 1:
                    return _row_dict(_query(db, reservation_id))
        raise ManagedLaunchUnavailable("preflight evidence update contention")
    except ManagedLaunchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ManagedLaunchUnavailable(f"preflight evidence persistence failed: {exc}") from exc


def append_observation(
    reservation_id: str, request: ManagedLaunchObservationRequest
) -> dict[str, Any]:
    identity_payload = request.model_dump(mode="json")
    payload = {**identity_payload, "observed_at": _now()}
    try:
        for _ in range(16):
            with database.SessionLocal() as db:
                row = _query(db, reservation_id)
                if row is None:
                    raise ManagedLaunchNotFound(f"reservation not found: {reservation_id}")
                _assert_bound_evidence(row, identity_payload)
                prior_observations = row.observations_json
                prior_admission = row.admission_json
                prior_state = row.state
                observations = _parse_json(prior_observations, [])
                for existing in observations:
                    if existing.get("observation_id") == request.observation_id:
                        if {key: existing.get(key) for key in identity_payload} != identity_payload:
                            raise ManagedLaunchConflict(
                                "observation_id is already bound to different evidence"
                            )
                        return _row_dict(row)
                observations.append(payload)
                updates: dict[Any, Any] = {
                    "observations_json": _canonical_json(observations),
                    "updated_at": _now(),
                }
                if request.kind in {"negative", "cancelled"}:
                    if row.state in {"admitting", "admitted"}:
                        raise ManagedLaunchConflict(
                            f"{request.kind} evidence cannot supersede task admission"
                        )
                    if row.state in {"negative", "cancelled"} and row.state != request.kind:
                        raise ManagedLaunchConflict(
                            f"terminal state {row.state!r} cannot change to {request.kind!r}"
                        )
                    if row.state not in {
                        "reserved",
                        "launching",
                        "ready",
                        "preflight_blocked",
                        "negative",
                        "cancelled",
                    }:
                        raise ManagedLaunchConflict(
                            f"{request.kind} evidence is invalid from state {row.state!r}"
                        )
                    updates["negative_json"] = _canonical_json(payload)
                    updates["state"] = request.kind
                updated = (
                    db.query(database.ManagedLaunchReservationModel)
                    .filter(
                        database.ManagedLaunchReservationModel.reservation_id == reservation_id,
                        database.ManagedLaunchReservationModel.state == prior_state,
                        database.ManagedLaunchReservationModel.observations_json
                        == prior_observations,
                        database.ManagedLaunchReservationModel.admission_json == prior_admission,
                    )
                    .update(updates, synchronize_session=False)
                )
                db.commit()
                if updated == 1:
                    return _row_dict(_query(db, reservation_id))
        raise ManagedLaunchUnavailable("observation append contention")
    except ManagedLaunchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ManagedLaunchUnavailable(f"observation persistence failed: {exc}") from exc


def reconcile(reservation_id: str) -> dict[str, Any]:
    """Return recovery facts without relaunching, sending, or deleting anything."""
    record = get(reservation_id)
    try:
        with database.SessionLocal() as db:
            terminal_present = (
                db.query(database.TerminalModel)
                .filter(database.TerminalModel.id == record["terminal_id"])
                .first()
                is not None
            )
    except Exception as exc:  # noqa: BLE001
        raise ManagedLaunchUnavailable(f"managed-launch reconcile failed: {exc}") from exc
    return {
        **record,
        "terminal_record_present": terminal_present,
        "recovery_only": record["state"] != "reserved",
    }


def claim_admission(
    reservation_id: str, request: ManagedLaunchAdmitRequest
) -> tuple[dict[str, Any], bool]:
    actual_digest = hashlib.sha256(request.message.encode("utf-8")).hexdigest()
    if actual_digest != request.message_sha256:
        raise ManagedLaunchConflict("message_sha256 does not match message bytes")
    identity = {
        "delivery_id": request.delivery_id,
        "message_sha256": request.message_sha256,
        "sender_id": request.sender_id,
        "orchestration_type": request.orchestration_type,
        "context": request.context.model_dump(mode="json"),
    }
    try:
        with database.SessionLocal() as db:
            admission = {
                **identity,
                "status": "io-attempted",
                "attempted_at": _now(),
            }
            updated = (
                db.query(database.ManagedLaunchReservationModel)
                .filter(
                    database.ManagedLaunchReservationModel.reservation_id == reservation_id,
                    database.ManagedLaunchReservationModel.state == "ready",
                    database.ManagedLaunchReservationModel.readiness_json.is_not(None),
                    database.ManagedLaunchReservationModel.admission_json.is_(None),
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
            if row is None:
                raise ManagedLaunchNotFound(f"reservation not found: {reservation_id}")
            if updated == 1:
                return _row_dict(row), True
            existing = _parse_json(row.admission_json, None)
            if existing is not None:
                existing_identity = {key: existing.get(key) for key in identity}
                if existing_identity != identity:
                    raise ManagedLaunchConflict(
                        "reservation already carries a different task admission"
                    )
                return _row_dict(row), False
            if row.state != "ready" or row.readiness_json is None:
                raise ManagedLaunchConflict(
                    "task admission requires an authoritative readiness receipt"
                )
            raise ManagedLaunchConflict("task admission state changed concurrently")
    except ManagedLaunchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ManagedLaunchUnavailable(f"task admission claim failed: {exc}") from exc


def complete_admission(reservation_id: str, delivery_id: str) -> dict[str, Any]:
    try:
        with database.SessionLocal() as db:
            row = _query(db, reservation_id)
            if row is None:
                raise ManagedLaunchNotFound(f"reservation not found: {reservation_id}")
            admission = _parse_json(row.admission_json, None)
            if not admission or admission.get("delivery_id") != delivery_id:
                raise ManagedLaunchConflict("delivery_id does not match the admission claim")
            if admission.get("status") == "admitted":
                return _row_dict(row)
            if row.state != "admitting" or admission.get("status") != "io-attempted":
                raise ManagedLaunchConflict(f"admission cannot complete from state {row.state!r}")
            request = _parse_json(row.request_json, {})
            admitted_at = _now()
            admission["provider_submission_receipt"] = {
                "receipt_id": str(uuid.uuid4()),
                "reservation_id": reservation_id,
                "delivery_id": delivery_id,
                "terminal_id": row.terminal_id,
                "receiver_id": row.terminal_id,
                "generation": row.generation,
                "provider": row.provider,
                "agent_profile": row.agent_profile,
                "model": request.get("expected_model"),
                "effort": request.get("expected_effort"),
                "message_sha256": admission["message_sha256"],
                "sender_id": admission["sender_id"],
                "context": admission["context"],
                "submitted_at": admitted_at,
            }
            admission["status"] = "admitted"
            admission["admitted_at"] = admitted_at
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
    try:
        with database.SessionLocal() as db:
            row = _query(db, reservation_id)
            if row is None:
                raise ManagedLaunchNotFound(f"reservation not found: {reservation_id}")
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


def attest_route(request: ManagedLaunchRouteAttestRequest) -> dict[str, Any]:
    """Produce a zero-task, provider-native route receipt.

    This is intentionally independent of reservations and terminal creation so
    an external launch breaker can prove that a failed route is healthy before
    permitting exactly one new launch attempt.
    """
    from cli_agent_orchestrator.services.codex_trust import (
        CodexTrustProbeError,
        attest_trusted_project,
    )
    from cli_agent_orchestrator.services.kimi_route import (
        KimiRouteProbeError,
        attest_kimi_route,
    )

    worktree = os.path.realpath(request.working_directory)
    if worktree != request.working_directory or not os.path.isdir(worktree):
        raise ManagedLaunchConflict(
            "working_directory must be an existing canonical absolute directory"
        )
    if request.provider == "codex":
        if request.trusted_project_root != worktree:
            raise ManagedLaunchConflict(
                "Codex route attestation requires trusted_project_root to equal working_directory"
            )
        try:
            provider_receipt = attest_trusted_project(
                worktree,
                expected_model=request.expected_model,
                expected_effort=request.expected_effort,
            )
        except CodexTrustProbeError as exc:
            raise ManagedLaunchConflict(str(exc)) from exc
    else:
        if request.trusted_project_root is not None:
            raise ManagedLaunchConflict("trusted_project_root is valid only for provider=codex")
        try:
            provider_receipt = attest_kimi_route(
                worktree,
                expected_model=request.expected_model,
                expected_effort=request.expected_effort,
            )
        except KimiRouteProbeError as exc:
            raise ManagedLaunchConflict(str(exc)) from exc
    return {
        "protocol_version": PROTOCOL_VERSION,
        "attestation_id": str(uuid.uuid4()),
        "provider": request.provider,
        "agent_profile": request.agent_profile,
        "working_directory": worktree,
        "trusted_project_root": request.trusted_project_root,
        "model": request.expected_model,
        "effort": request.expected_effort,
        "no_task_admitted": True,
        "provider_route_receipt": provider_receipt,
        "attested_at": _now(),
    }


def cleanup_reserved(
    reservation_id: str,
    request: ManagedLaunchCleanupRequest,
    *,
    registry=None,
) -> dict[str, Any]:
    """Delete only the exact non-admitted reservation generation.

    The durable ``cleanup_intended`` intermediate state makes a lost HTTP
    response recoverable. A retry checks the same terminal id and generation;
    it never selects a terminal by a mutable label or launches a replacement.
    """
    from cli_agent_orchestrator.services import terminal_service

    try:
        with database.SessionLocal() as db:
            row = _query(db, reservation_id)
            if row is None:
                raise ManagedLaunchNotFound(f"reservation not found: {reservation_id}")
            if row.terminal_id != request.terminal_id or row.generation != request.generation:
                raise ManagedLaunchConflict("cleanup identity does not match the reservation")
            observations = _parse_json(row.observations_json, [])
            existing = next(
                (
                    item
                    for item in observations
                    if item.get("kind") == "cleanup"
                    and item.get("cleanup_id") == request.cleanup_id
                ),
                None,
            )
            if row.state == "cleaned":
                if existing is None:
                    raise ManagedLaunchUnavailable("cleaned reservation lacks cleanup proof")
                return _row_dict(row)
            if row.state not in {
                "preflight_blocked",
                "negative",
                "cancelled",
                "cleanup_intended",
            }:
                raise ManagedLaunchConflict(
                    f"cleanup requires terminal negative evidence, not state {row.state!r}"
                )
            if row.state != "cleanup_intended":
                row.state = "cleanup_intended"
                row.updated_at = _now()
                db.commit()

        # delete_terminal is idempotent for a missing DB record. It also owns
        # provider and tmux cleanup for this exact reserved terminal id.
        terminal_service.delete_terminal(request.terminal_id, registry=registry)

        with database.SessionLocal() as db:
            row = _query(db, reservation_id)
            if row is None:
                raise ManagedLaunchNotFound(f"reservation not found: {reservation_id}")
            terminal_present = (
                db.query(database.TerminalModel)
                .filter(database.TerminalModel.id == request.terminal_id)
                .first()
                is not None
            )
            if terminal_present:
                raise ManagedLaunchUnavailable("exact terminal still exists after cleanup")
            observations = _parse_json(row.observations_json, [])
            existing = next(
                (
                    item
                    for item in observations
                    if item.get("kind") == "cleanup"
                    and item.get("cleanup_id") == request.cleanup_id
                ),
                None,
            )
            if existing is None:
                existing = {
                    "kind": "cleanup",
                    "cleanup_id": request.cleanup_id,
                    "reservation_id": reservation_id,
                    "terminal_id": row.terminal_id,
                    "generation": row.generation,
                    "terminal_record_present": False,
                    "cleaned_at": _now(),
                }
                observations.append(existing)
                row.observations_json = _canonical_json(observations)
            row.state = "cleaned"
            row.updated_at = _now()
            db.commit()
            db.refresh(row)
            return {**_row_dict(row), "cleanup": existing}
    except ManagedLaunchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ManagedLaunchUnavailable(f"managed-launch cleanup failed: {exc}") from exc


async def launch_reserved(reservation_id: str, *, registry=None) -> dict[str, Any]:
    """Launch a reserved generation without carrying task bytes.

    The trust/route probe and provider initialization happen only for the
    caller that atomically changes ``reserved`` to ``launching``.  Every retry
    returns the queryable record and never starts a second provider.
    """
    import asyncio

    from cli_agent_orchestrator.providers.base import ProviderPreflightBlocked
    from cli_agent_orchestrator.services import terminal_service
    from cli_agent_orchestrator.services.codex_trust import (
        CodexTrustProbeError,
        attest_trusted_project,
    )
    from cli_agent_orchestrator.services.kimi_route import (
        KimiRouteProbeError,
        attest_kimi_route,
    )

    record, should_launch = claim_launch(reservation_id)
    if not should_launch:
        return record
    request = record["request"]
    if record["provider"] not in {"codex", "kimi_cli"}:
        return mark_preflight_blocked(
            reservation_id,
            preflight_class="unsupported-provider-readiness",
            detail="managed-launch v1 has no authoritative readiness adapter for this provider",
        )

    route_receipt: dict[str, Any]
    if record["provider"] == "codex":
        try:
            route_receipt = await asyncio.to_thread(
                attest_trusted_project,
                record["trusted_project_root"],
                expected_model=request["expected_model"],
                expected_effort=request["expected_effort"],
            )
        except CodexTrustProbeError as exc:
            return mark_preflight_blocked(
                reservation_id,
                preflight_class="trust-preauthorization",
                detail=str(exc),
            )
    else:
        try:
            route_receipt = await asyncio.to_thread(
                attest_kimi_route,
                record["working_directory"],
                expected_model=request["expected_model"],
                expected_effort=request["expected_effort"],
            )
        except KimiRouteProbeError as exc:
            return mark_preflight_blocked(
                reservation_id,
                preflight_class="provider-route-attestation",
                detail=str(exc),
            )

    try:
        terminal = await terminal_service.create_terminal(
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
            trusted_project_root=record["trusted_project_root"],
            expected_model=request["expected_model"],
            expected_effort=request["expected_effort"],
            preserve_on_init_failure=True,
        )
    except ProviderPreflightBlocked as exc:
        return mark_preflight_blocked(
            reservation_id,
            preflight_class=exc.preflight_class,
            detail=str(exc),
            evidence=exc.evidence,
        )
    except Exception as exc:  # noqa: BLE001 - preserve and expose, never cleanup/retry
        return mark_preflight_blocked(
            reservation_id,
            preflight_class="provider-startup-error",
            detail=str(exc),
        )

    receipt = {
        "receipt_id": str(uuid.uuid4()),
        "reservation_id": reservation_id,
        "terminal_id": record["terminal_id"],
        "generation": record["generation"],
        "provider": record["provider"],
        "agent_profile": record["agent_profile"],
        "model": route_receipt.get("model"),
        "effort": route_receipt.get("reasoning_effort"),
        "working_directory": record["working_directory"],
        "provider_initialized": True,
        "terminal_status": terminal.status,
        "provider_route_receipt": route_receipt,
        "attested_at": _now(),
    }
    return mark_ready(
        reservation_id,
        terminal_id=record["terminal_id"],
        generation=record["generation"],
        receipt=receipt,
    )


async def admit_reserved(
    reservation_id: str,
    request: ManagedLaunchAdmitRequest,
    *,
    registry=None,
) -> dict[str, Any]:
    """Admit one task after readiness, with no blind retry on ambiguity."""
    import asyncio

    from cli_agent_orchestrator.models.inbox import OrchestrationType
    from cli_agent_orchestrator.services import terminal_service

    record, should_send = claim_admission(reservation_id, request)
    if not should_send:
        return record
    try:
        await asyncio.to_thread(
            terminal_service.send_input,
            record["terminal_id"],
            request.message,
            registry=registry,
            sender_id=request.sender_id,
            orchestration_type=OrchestrationType(request.orchestration_type),
        )
    except Exception as exc:  # noqa: BLE001 - delivery may have crossed the boundary
        return mark_admission_ambiguous(reservation_id, request.delivery_id, str(exc))
    return complete_admission(reservation_id, request.delivery_id)

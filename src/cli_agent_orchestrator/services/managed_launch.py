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
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError, OperationalError

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.managed_launch import (
    PROTOCOL_VERSION,
    ManagedLaunchAdmitRequest,
    ManagedLaunchCleanupRequest,
    ManagedLaunchObservationRequest,
    ManagedLaunchReserveRequest,
    ManagedLaunchRouteAttestRequest,
)
from cli_agent_orchestrator.services import companion_receipts
from cli_agent_orchestrator.services import execution_mode as em
from cli_agent_orchestrator.services import provider_contracts

logger = logging.getLogger(__name__)
from cli_agent_orchestrator.utils.terminal import generate_terminal_id, managed_window_name


class ManagedLaunchError(RuntimeError):
    """Base error for the managed-launch protocol."""


class ManagedLaunchNotFound(ManagedLaunchError):
    pass


class ManagedLaunchConflict(ManagedLaunchError):
    pass


class ManagedLaunchUnavailable(ManagedLaunchError):
    pass


#: The closed vocabulary of transient bind refusals. Exactly one member,
#: and adding a second is a paired change: a consumer treats these as
#: "retry the same attempt", so a value it does not recognise must be
#: refused permanently rather than retried on the strength of being new.
REASON_BIND_BRIDGE_NOT_DURABLY_READY = "bind-bridge-not-durably-ready"


class ManagedLaunchNotReady(ManagedLaunchError):
    """The operation cannot succeed *yet*, and asking again may change it.

    Deliberately **not** a subclass of :class:`ManagedLaunchConflict`. A
    conflict is permanent and a consumer is right to fail closed on it;
    this is the one refusal where retrying the same attempt is the correct
    behaviour, and it must be separable on the wire or a consumer has to
    infer transience from something else. Inferring it from the row state
    is what produced the failure this exists to remove: every permanent
    conflict that happened to leave the row ``launching`` — an identity
    mismatch, a mode violation, a foreign single-writer holder — was read
    as "not yet", polled, and reported with the breaker untripped.

    ``reason`` carries a closed-vocabulary token so the distinction
    survives to the consumer as data rather than as prose it would have to
    pattern-match.
    """

    def __init__(self, message: str, *, reason: str):
        super().__init__(message)
        self.reason = reason


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


#: The execution modes this surface can actually *run* today.
#:
#: This is deliberately the set of modes with a real launch path, not the
#: set of modes the vocabulary can name.  It is what the capability
#: endpoint advertises, so the advertisement cannot claim a branch that
#: does not exist: a caller which trusts the advertisement and asks for a
#: mode it names must get that mode, not a silent substitution.
#:
#: Native TUI is absent because this surface has no native launch branch —
#: it mints a provider session over the bounded ACP bridge and has no step
#: that starts, owns, or resumes a human-visible provider terminal.  Adding
#: ``execution_mode.NATIVE_TUI`` here is the last step of building that
#: branch, never a precondition for it, and doing so early would turn every
#: native request into an ACP launch wearing a native label.
SUPPORTED_EXECUTION_MODES: tuple[str, ...] = (em.ACP,)

#: Request keys introduced after this surface shipped.  A reservation
#: written before they existed simply has no such key.
_ADDITIVE_REQUEST_KEYS = ("execution_mode", "worker_class")


def _resolve_execution_mode(
    request: ManagedLaunchReserveRequest,
) -> em.ExecutionModeResolution:
    """Resolve and admit the requested mode, before any provider effect.

    Reserve is the earliest point at which the mode is knowable and the
    last one still free of provider effects, so a mode this surface
    cannot honour is refused here with nothing launched and nothing
    persisted.

    Refusing is the whole point.  The alternative — accepting a native
    request and running ACP — would produce a reservation whose recorded
    request says one thing and whose behaviour is another, and the
    request echo a caller verifies against would confirm the lie rather
    than catch it.  A caller can only rely on that echo if an accepted
    mode is a mode that will actually run.
    """
    try:
        resolution = em.resolve(
            launch_input=request.execution_mode,
            worker_class=request.worker_class,
        )
    except em.ExecutionModeError as exc:
        raise ManagedLaunchConflict(str(exc)) from exc
    if resolution.mode not in SUPPORTED_EXECUTION_MODES:
        raise ManagedLaunchConflict(
            f"execution_mode {resolution.mode!r} (from {resolution.source}) is not "
            f"supported by this managed-launch surface; supported modes are "
            f"{list(SUPPORTED_EXECUTION_MODES)}"
        )
    return resolution


def _mode_projection(request: Any) -> dict[str, Any]:
    """The mode of record for a reservation, derived from its request.

    Derived rather than stored: the request is immutable once reserved,
    so resolving from it is deterministic and can never drift from what
    the caller actually asked for.

    A request persisted before this contract existed has neither key.
    That row is **legacy ACP** and can never be reinterpreted as native —
    a guard that read "mode absent" as native would treat every
    historical generation as an attachable native session.  It is
    reported with source ``legacy`` so a consumer can tell "predates the
    contract" from "the caller named nothing", which resolve to the same
    mode by different routes.
    """
    if not isinstance(request, dict) or not any(k in request for k in _ADDITIVE_REQUEST_KEYS):
        return {
            "execution_mode": em.ACP,
            "execution_mode_source": em.SOURCE_LEGACY,
            "is_legacy_execution_mode": True,
        }
    try:
        resolution = em.resolve(
            launch_input=request.get("execution_mode"),
            worker_class=request.get("worker_class"),
        )
    except em.ExecutionModeError as exc:
        # A stored request that no longer resolves means durable
        # corruption or a rollback from a newer binary.  Fail closed
        # rather than fall back to ACP: the reservation may have been
        # accepted, and answered for, under a mode this binary cannot
        # reconstruct.
        raise ManagedLaunchUnavailable(
            f"managed-launch record has an unresolvable execution mode: {exc}"
        ) from exc
    return {
        "execution_mode": resolution.mode,
        "execution_mode_source": resolution.source,
        "is_legacy_execution_mode": False,
    }


def _request_matches(stored_json: str, incoming: dict[str, Any]) -> bool:
    """Whether a replayed reserve carries the same immutable request.

    An exact byte match is the normal case.  The one accommodation is the
    upgrade boundary: a reservation written before the execution-mode
    fields existed has no such keys at all, and treating that absence as
    "a different request" would turn an ordinary idempotent replay into a
    hard conflict for every reservation in flight across a deploy.  So an
    absent stored key compares equal to an *unspecified* incoming value —
    and only to that.  A caller that now explicitly asks for a mode
    really is presenting a different request, and still conflicts.
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


def _row_dict(row: Any) -> dict[str, Any]:
    negative = _parse_json(row.negative_json, None)
    request = _parse_json(row.request_json, {})
    return {
        # Projected on every public read so a consumer never has to infer
        # a mode from a provider name or from an absent field.  Always
        # concrete: a legacy reservation reads as ACP, never as null.
        **_mode_projection(request),
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
        # The faithful echo of the immutable request as the caller sent
        # it: an omitted mode echoes as null, never as the resolved
        # default.  A caller verifies this against what it sent; the
        # resolved mode in force is the top-level ``execution_mode``.
        "request": request,
        "observations": _parse_json(row.observations_json, []),
        "readiness": _parse_json(row.readiness_json, None),
        "admission": _parse_json(row.admission_json, None),
        "negative": negative,
        "launch_failure": negative if row.state == "launch-failed-bridge" else None,
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


# P1-5 (spec §20.2d(1)/§20.2e): provider-specific allowlisted receipt schemas.
# Readiness is the exact provider session/thread start; submission is the
# exact provider turn start. Locally minted (`pane-id`), wrong-kind, and
# unknown-provider provenance is rejected before any fork state advance.
_READINESS_RECEIPT_KINDS = {
    "codex": "codex-thread-start",
    "kimi_cli": "kimi-acp-session-new",
}
_SUBMISSION_RECEIPT_KINDS = {
    "codex": "codex-turn-start",
    "kimi_cli": "kimi-session-update",
}

#: The providers *this* surface can prove readiness for, derived from the
#: allowlist above rather than restated.  Every place that used to write
#: the pair out by hand was a second source of truth, and one of them
#: drifted: the published capability list said which providers a caller
#: may probe, and stayed behind when a provider gained an adapter
#: elsewhere.  Deriving it means a provider appears the moment its
#: receipt kind exists and never before.
READINESS_PROVIDERS: frozenset[str] = frozenset(_READINESS_RECEIPT_KINDS)


def _validate_native_receipt(
    row: Any,
    receipt: dict[str, Any],
    *,
    admission: Optional[dict[str, Any]] = None,
) -> None:
    """Accept only exact-session, opaque provider receipt identities."""
    from cli_agent_orchestrator.services.managed_provider_bridge import BRIDGE_VERSION

    _assert_bound_evidence(row, receipt)
    kinds = _SUBMISSION_RECEIPT_KINDS if admission is not None else _READINESS_RECEIPT_KINDS
    expected_kind = kinds.get(row.provider)
    if expected_kind is None or receipt.get("provider_receipt_kind") != expected_kind:
        raise ManagedLaunchConflict(
            "provider receipt kind is not the allowlisted provider-native kind "
            f"for {row.provider!r}: {receipt.get('provider_receipt_kind')!r}"
        )
    if admission is not None and receipt.get("receipt_id") != receipt.get("provider_turn_id"):
        raise ManagedLaunchConflict(
            "provider submission receipt id is not the provider turn identity"
        )
    if admission is None and receipt.get("receipt_id") != receipt.get("provider_session_id"):
        raise ManagedLaunchConflict(
            "provider readiness receipt id is not the provider session identity"
        )
    request = _parse_json(row.request_json, {})
    expected = {
        "bridge_version": BRIDGE_VERSION,
        "reservation_id": row.reservation_id,
        "terminal_id": row.terminal_id,
        "generation": row.generation,
        "provider": row.provider,
        "agent_profile": row.agent_profile,
        "model": request.get("expected_model"),
        "effort": request.get("expected_effort"),
        "working_directory": row.working_directory,
    }
    if admission is not None:
        expected.update(
            {
                "delivery_id": admission["delivery_id"],
                "receiver_id": row.terminal_id,
                "message_sha256": admission["message_sha256"],
                "sender_id": admission["sender_id"],
                "context": admission["context"],
                "provider_accepted": True,
            }
        )
    mismatches = {
        key: {"expected": value, "observed": receipt.get(key)}
        for key, value in expected.items()
        if receipt.get(key) != value
    }
    required_strings = {
        "receipt_id",
        "provider_session_id",
        "provider_receipt_kind",
        "provider_transcript_sha256",
    }
    if admission is not None:
        required_strings.update({"provider_turn_id", "submitted_at"})
    else:
        # P1-8 (final conformance §20.2f): the readiness schema is complete —
        # the provider version and an explicit model-input-ready flag are
        # mandatory before ready is persisted, identical to the conductor
        # boundary. Omission fails closed.
        required_strings.add("provider_version")
        if receipt.get("model_input_ready") is not True:
            mismatches["model_input_ready"] = {
                "expected": True,
                "observed": receipt.get("model_input_ready"),
            }
    missing = sorted(
        key for key in required_strings if not isinstance(receipt.get(key), str) or not receipt[key]
    )
    transcript = receipt.get("provider_transcript_sha256")
    if isinstance(transcript, str) and (
        len(transcript) != 64 or any(ch not in "0123456789abcdef" for ch in transcript)
    ):
        missing.append("provider_transcript_sha256")
    if mismatches or missing:
        raise ManagedLaunchConflict(
            "provider-native receipt is not bound to the exact reservation: "
            + _canonical_json({"mismatches": mismatches, "invalid": sorted(set(missing))})
        )


def _executable_identity(request_payload: dict) -> tuple[str, str]:
    """Verify the reservation-pinned provider executable identity.

    P1-9 (final conformance §20.2f): managed campaign execution never
    resolves a provider from the ambient PATH. The conductor pins the
    absolute canonical path and digest at reservation time; the fork verifies
    both before any provider effect and fails closed on absence or drift.
    """
    pinned_path = request_payload.get("provider_executable")
    pinned_digest = request_payload.get("provider_executable_sha256")
    if not pinned_path or not pinned_digest:
        raise ManagedLaunchConflict(
            "managed campaign execution requires the reservation-pinned "
            "provider executable identity; ambient PATH resolution is refused"
        )
    if not os.path.isabs(pinned_path) or os.path.realpath(pinned_path) != pinned_path:
        raise ManagedLaunchConflict("provider executable must be a canonical absolute path")
    if not os.path.isfile(pinned_path) or not os.access(pinned_path, os.X_OK):
        raise ManagedLaunchConflict(f"provider executable is not executable: {pinned_path}")
    try:
        with open(pinned_path, "rb") as provider_file:
            digest = hashlib.sha256(provider_file.read()).hexdigest()
    except OSError as exc:
        raise ManagedLaunchConflict(f"provider executable is unreadable: {exc}") from exc
    if digest != pinned_digest:
        raise ManagedLaunchConflict("provider executable digest drifted from the reservation pin")
    return pinned_path, digest


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
    # P1-9 (final conformance §20.2f): managed campaign execution fails closed
    # without the reservation-pinned provider executable identity; ambient
    # PATH resolution is never a fallback.
    if not request.provider_executable or not request.provider_executable_sha256:
        raise ManagedLaunchConflict(
            "managed campaign execution requires the pinned provider executable "
            "identity (provider_executable + provider_executable_sha256)"
        )
    if not os.path.isabs(request.provider_executable):
        raise ManagedLaunchConflict("provider_executable must be an absolute path")
    # Refused before the payload is built, so a route the provider cannot
    # honor never becomes a durable reservation. Reaching the provider with
    # it instead costs an allocated terminal id and a reservation that must
    # then be finalized, to learn something knowable here.
    try:
        provider_contracts.validate_route_effort(request.expected_model, request.expected_effort)
    except provider_contracts.ProviderContractError as exc:
        raise ManagedLaunchConflict(str(exc)) from exc
    # Resolved and admitted before the payload is built, so an
    # unsupported or contradictory mode fails with nothing persisted.
    _resolve_execution_mode(request)
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
                if not _request_matches(existing.request_json, payload):
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
            if existing is None or not _request_matches(existing.request_json, payload):
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
                "launch-failed-bridge",
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
            _validate_native_receipt(row, receipt)
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
                # P1-10 (final conformance §20.2f): publish the provider-native
                # route receipt to the generation-bound companion store. At
                # readiness the provider session start IS the route turn; the
                # per-turn identity is refined at each admission.
                companion_receipts.record_route_receipt(
                    terminal_id,
                    generation,
                    provider=receipt["provider"],
                    model=receipt["model"],
                    effort=receipt["effort"],
                    receipt_id=receipt["provider_session_id"],
                    turn_id=receipt["provider_session_id"],
                    provider_version=receipt.get("provider_version"),
                )
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


def mark_launch_failed_bridge(
    reservation_id: str,
    bridge_state: dict[str, Any],
) -> dict[str, Any]:
    """Atomically finalize the fork-owned launch and bound delivery identity.

    The bridge state is durable before this transaction.  The CAS binds all
    four immutable identities and writes the reservation outcome plus the
    fork-owned never-submitted delivery record together.
    """
    from cli_agent_orchestrator.services.managed_provider_bridge import (
        BridgeError,
        validate_launch_failure,
    )

    try:
        with database.SessionLocal() as db:
            row = _query(db, reservation_id)
            if row is None:
                raise ManagedLaunchNotFound(f"reservation not found: {reservation_id}")
            request = _parse_json(row.request_json, {})
            delivery_id = request.get("delivery_id")
            if not isinstance(delivery_id, str) or not delivery_id:
                raise ManagedLaunchConflict(
                    "launch failure finalization requires the immutable reservation delivery_id"
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
            }
            observation = {
                "kind": "launch-failed-bridge",
                "reservation_id": row.reservation_id,
                "terminal_id": row.terminal_id,
                "generation": row.generation,
                "delivery_id": delivery_id,
                "failure": failure,
            }
            if row.state == "launch-failed-bridge":
                observations = _parse_json(row.observations_json, [])
                if (
                    _parse_json(row.admission_json, None) != delivery
                    or not observations
                    or observations[-1] != observation
                ):
                    raise ManagedLaunchConflict(
                        "launch-failed-bridge evidence changed after finalization"
                    )
                return _row_dict(row)
            if row.state != "launching":
                raise ManagedLaunchConflict(
                    f"bridge launch failure cannot finalize state {row.state!r}"
                )
            prior_observations = row.observations_json
            observations = _parse_json(prior_observations, [])
            observations.append(observation)
            updated = (
                db.query(database.ManagedLaunchReservationModel)
                .filter(
                    database.ManagedLaunchReservationModel.reservation_id == reservation_id,
                    database.ManagedLaunchReservationModel.terminal_id == row.terminal_id,
                    database.ManagedLaunchReservationModel.generation == row.generation,
                    database.ManagedLaunchReservationModel.state == "launching",
                    database.ManagedLaunchReservationModel.readiness_json.is_(None),
                    database.ManagedLaunchReservationModel.admission_json.is_(None),
                    database.ManagedLaunchReservationModel.observations_json == prior_observations,
                )
                .update(
                    {
                        "state": "launch-failed-bridge",
                        "admission_json": _canonical_json(delivery),
                        "negative_json": _canonical_json(failure),
                        "observations_json": _canonical_json(observations),
                        "updated_at": _now(),
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            if updated != 1:
                raise ManagedLaunchConflict(
                    "launch failure lost the exact reservation/generation/delivery CAS"
                )
            return _row_dict(_query(db, reservation_id))
    except ManagedLaunchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ManagedLaunchUnavailable(f"bridge launch failure finalization failed: {exc}") from exc


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
                if (
                    request.kind in {"negative", "cancelled"}
                    and row.state != "launch-failed-bridge"
                ):
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


def _adopt_durable_provider_fact(record: dict[str, Any]) -> dict[str, Any]:
    """Persist an exact-generation provider fact after a lost CAO response.

    This recovery path is deliberately read-only at the provider boundary: it
    may adopt an already durable readiness/submission receipt, but it never
    launches a provider, sends task bytes, or deletes a terminal.
    """
    from cli_agent_orchestrator.services.managed_provider_bridge import read_state

    if record["state"] not in {"launching", "admitting"}:
        return record
    state = read_state(record["reservation_id"])
    if not state:
        return record

    if record["state"] == "launching":
        receipt = state.get("readiness")
        if state.get("state") == "ready" and isinstance(receipt, dict):
            return mark_ready(
                record["reservation_id"],
                terminal_id=record["terminal_id"],
                generation=record["generation"],
                receipt=receipt,
            )
        if state.get("state") == "launch-failed-bridge":
            return mark_launch_failed_bridge(record["reservation_id"], state)
        if state.get("state") == "preflight_blocked":
            return mark_preflight_blocked(
                record["reservation_id"],
                preflight_class="provider-native-readiness",
                detail=str(state.get("error") or "provider bridge blocked before readiness"),
                evidence=state,
            )
        if state.get("state") == "admitted":
            raise ManagedLaunchConflict(
                "provider bridge admitted input before the fork recorded an admission claim"
            )
        return record

    receipt = state.get("submission")
    admission = record.get("admission") or {}
    delivery_id = admission.get("delivery_id")
    if state.get("state") == "admitted" and isinstance(receipt, dict) and delivery_id:
        return complete_admission(record["reservation_id"], delivery_id, receipt)
    return record


def reconcile(reservation_id: str) -> dict[str, Any]:
    """Adopt durable facts without relaunching, sending, or deleting anything."""
    record = _adopt_durable_provider_fact(get(reservation_id))
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
            row = _query(db, reservation_id)
            if row is None:
                raise ManagedLaunchNotFound(f"reservation not found: {reservation_id}")
            reservation_request = _parse_json(row.request_json, {})
            if request.delivery_id != reservation_request.get("delivery_id"):
                raise ManagedLaunchConflict(
                    "delivery_id does not match the immutable reservation delivery identity"
                )
            expected_context = {
                "project": reservation_request.get("project"),
                "task_id": reservation_request.get("task_id"),
            }
            observed_context = {
                "project": request.context.project,
                "task_id": request.context.task_id,
            }
            if observed_context != expected_context:
                raise ManagedLaunchConflict(
                    "admission project/task identity does not match reservation: "
                    + _canonical_json(
                        {
                            "expected": expected_context,
                            "observed": observed_context,
                        }
                    )
                )
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


def complete_admission(
    reservation_id: str,
    delivery_id: str,
    provider_receipt: dict[str, Any],
) -> dict[str, Any]:
    try:
        with database.SessionLocal() as db:
            row = _query(db, reservation_id)
            if row is None:
                raise ManagedLaunchNotFound(f"reservation not found: {reservation_id}")
            admission = _parse_json(row.admission_json, None)
            if not admission or admission.get("delivery_id") != delivery_id:
                raise ManagedLaunchConflict("delivery_id does not match the admission claim")
            if admission.get("status") == "admitted":
                if admission.get("provider_submission_receipt") != provider_receipt:
                    raise ManagedLaunchConflict(
                        "provider submission receipt changed after admission"
                    )
                return _row_dict(row)
            if row.state != "admitting" or admission.get("status") != "io-attempted":
                raise ManagedLaunchConflict(f"admission cannot complete from state {row.state!r}")
            _validate_native_receipt(row, provider_receipt, admission=admission)
            admitted_at = _now()
            admission["provider_submission_receipt"] = provider_receipt
            admission["status"] = "admitted"
            admission["admitted_at"] = admitted_at
            row.admission_json = _canonical_json(admission)
            row.state = "admitted"
            row.updated_at = _now()
            db.commit()
            db.refresh(row)
            # P1-7/P1-10 (final conformance §20.2f): publish the exact
            # provider/model-turn submission acknowledgement and the per-turn
            # route identity to the generation-bound companion store. The ack
            # binds message id + digest, the receiver's exact generation, and
            # the provider session/turn — identities and digests only, never
            # the message body (redaction).
            companion_receipts.record_route_receipt(
                row.terminal_id,
                row.generation,
                provider=provider_receipt["provider"],
                model=provider_receipt["model"],
                effort=provider_receipt["effort"],
                receipt_id=provider_receipt["receipt_id"],
                turn_id=provider_receipt["provider_turn_id"],
                provider_version=provider_receipt.get("provider_version"),
            )
            companion_receipts.record_message_ack(
                row.terminal_id,
                row.generation,
                message_id=delivery_id,
                ack={
                    "kind": "submitted",
                    "message_id": delivery_id,
                    "message_sha256": admission.get("message_sha256"),
                    "sender_id": provider_receipt.get("sender_id"),
                    "receiver_id": row.terminal_id,
                    "receiver_generation": row.generation,
                    "provider": row.provider,
                    "provider_session_id": provider_receipt["provider_session_id"],
                    "provider_turn_id": provider_receipt["provider_turn_id"],
                    "submitted_at": provider_receipt.get("submitted_at"),
                },
            )
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

    Dispatch is exhaustive by provider, never "codex or else". The accepted
    provider set was widened to include ``claude_code`` when the reserve
    request was, and an ``else`` branch silently made that a *Kimi* probe:
    it ran the Kimi binary, produced a Kimi route receipt, and returned it
    under ``provider: "claude_code"``. A breaker reading that receipt would
    open a Claude route on evidence gathered from a different provider —
    the one failure mode a route attestation exists to prevent. A provider
    with no attestor is refused; there is no honest receipt to give.
    """
    from cli_agent_orchestrator.services.claude_route import (
        ClaudeRouteProbeError,
        attest_claude_route,
    )
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
    elif request.provider == "kimi_cli":
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
    elif request.provider == "claude_code":
        if request.trusted_project_root is not None:
            raise ManagedLaunchConflict("trusted_project_root is valid only for provider=codex")
        try:
            provider_receipt = attest_claude_route(
                worktree,
                expected_model=request.expected_model,
                expected_effort=request.expected_effort,
            )
        except ClaudeRouteProbeError as exc:
            raise ManagedLaunchConflict(str(exc)) from exc
    else:
        # Unreachable through the typed request, which is the point: the
        # branch exists so that widening the Literal without adding an
        # attestor fails here rather than silently reaching whichever
        # probe happened to be last.
        raise ManagedLaunchConflict(
            f"no route attestor exists for provider {request.provider!r}; refusing rather "
            "than returning another provider's receipt under this provider's name"
        )
    return {
        "protocol_version": PROTOCOL_VERSION,
        "attestation_id": str(uuid.uuid4()),
        "provider": request.provider,
        "agent_profile": request.agent_profile,
        "working_directory": worktree,
        "trusted_project_root": request.trusted_project_root,
        # These two echo the request verbatim. They name the *failure
        # domain* being attested — "the route that failed, and is now being
        # checked, was this model at this effort" — and are never a claim
        # that a provider resolved to them. The distinction matters because
        # the two providers answer it differently: Kimi's nested receipt
        # reports a genuinely observed model/effort, while Claude has no
        # pre-turn resolution surface at all and states its own as
        # requested-only with observed values explicitly null. A reader that
        # took these outer keys as resolution would therefore read a Claude
        # attestation as proving something no probe looked at. Provider-
        # observed facts live in ``provider_route_receipt`` and nowhere else.
        "model": request.expected_model,
        "effort": request.expected_effort,
        "no_task_admitted": True,
        "provider_route_receipt": provider_receipt,
        "attested_at": _now(),
    }


def deliver_inbox_via_bridge(
    terminal_id: str,
    *,
    message_id: Any,
    message: str,
    sender_id: Optional[str],
) -> bool:
    """P1-7 (final conformance §20.2f): deliver one exact queued inbox message
    through the receiver's live managed provider bridge, producing the
    provider-native ``terminal_queued → submitted`` acknowledgement (recorded
    by the bridge into the generation-bound companion store).

    Returns True only when the exact provider turn accepted the message.
    Returns False when the terminal is not a live managed session or the
    bridge is unavailable — the caller then uses the ordinary delivery path
    and NO acknowledgement is inferred from it.
    """
    from cli_agent_orchestrator.services.managed_provider_bridge import request_bridge

    try:
        identity = managed_control_identity(terminal_id)
        if identity is None or identity["state"] != "admitted":
            return False
        reservation_id = identity["reservation_id"]
        request_bridge(
            reservation_id,
            {
                "bridge_version": "cao-native-provider-bridge-v1",
                "op": "deliver",
                "reservation_id": reservation_id,
                "terminal_id": identity["terminal_id"],
                "generation": identity["generation"],
                "message_id": str(message_id),
                "message": message,
                "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
                "sender_id": sender_id,
            },
            timeout=30.0,
        )
        return True
    except Exception:  # noqa: BLE001 - fall back to the ordinary delivery path
        logger.warning(
            "managed bridge inbox delivery unavailable for %s; using ordinary path",
            terminal_id,
            exc_info=True,
        )
        return False


#: The kinds of native-identity refusal a projection can carry, kept
#: apart all the way to the caller.  "We looked and it is wrong" and "we
#: could not look" license opposite handling: reporting the second as the
#: first closes a delivery that is still open, and reporting the first as
#: the second leaves a permanently-wrong binding looking retryable.
NATIVE_IDENTITY_CONFLICT = "conflict"
NATIVE_IDENTITY_UNAVAILABLE = "unavailable"


def _v2_native_control_projection(reservation_id: str) -> dict[str, Any]:
    """The control-relevant identity of one v2 generation, from evidence.

    Every field here is read from an authoritative durable source rather
    than restated from the reservation row: the mode through
    ``em.mode_of_record`` so a legacy NULL projects as the ACP it is, and
    the native session and provider process through the *same* validator
    native admission uses, so the control path and the admission path can
    never disagree about who holds a session.

    Deliberately not sourced from the published readiness sibling.  Kimi's
    sibling omits provider-authored identity keys by design — its
    readiness is an observed attached pane rather than a claim the
    provider makes about itself — so a projection reading it would find
    nothing for every healthy Kimi generation and refuse them all.  The
    binding and the attachment are the evidence; the sibling is a
    published statement about proof class.

    An identity that cannot be resolved is reported as absent *with the
    reason*, never as a weaker binding: the caller refuses on it rather
    than proceeding on the pane tuple alone.
    """
    from cli_agent_orchestrator.services import managed_launch_v2 as v2

    projection: dict[str, Any] = {
        "execution_mode": None,
        "execution_mode_source": None,
        "native_session_id": None,
        "provider_process_id": None,
        "provider_version": None,
        "native_identity_refusal": None,
    }
    try:
        record = v2.get(reservation_id)
    except ManagedLaunchError as exc:
        projection["native_identity_refusal"] = {
            "kind": NATIVE_IDENTITY_UNAVAILABLE,
            "detail": f"the v2 reservation could not be read: {exc}",
        }
        return projection

    projection["execution_mode"] = record["execution_mode"]
    projection["execution_mode_source"] = record["execution_mode_source"]
    if record["execution_mode"] != em.NATIVE_TUI:
        # An ACP generation has no native composer identity to publish,
        # and inventing absences as a refusal would misreport a mode that
        # is behaving exactly as it should.
        return projection

    # The version the generation was *bound* to, not one probed now: which
    # keystroke a composer accepts is a fact about the build that is
    # running, and the binding is the record of which build that is.
    binding = record.get("binding") or {}
    projection["provider_version"] = binding.get("provider_version")

    try:
        proven = v2.resolve_native_identity_of_record(record)
    except ManagedLaunchConflict as exc:
        projection["native_identity_refusal"] = {
            "kind": NATIVE_IDENTITY_CONFLICT,
            "detail": str(exc),
        }
        return projection
    except Exception as exc:  # noqa: BLE001 - an unread store, not a verdict
        projection["native_identity_refusal"] = {
            "kind": NATIVE_IDENTITY_UNAVAILABLE,
            "detail": f"the native identity of this generation could not be read: {exc}",
        }
        return projection

    projection["native_session_id"] = proven["native_session_id"]
    # Rendered through the producer the readiness sibling already uses, so
    # the pid travels with its start marker. A bare pid is the forgeable
    # half on its own: pids recycle, so a stale one can match an unrelated
    # live process and forge a survivor, or match nothing and forge a
    # no-survivor.
    projection["provider_process_id"] = v2.published_process_id(proven["process_identity"])
    if projection["provider_process_id"] is None:
        projection["native_identity_refusal"] = {
            "kind": NATIVE_IDENTITY_CONFLICT,
            "detail": (
                f"the attachment for session {proven['native_session_id']} carries a "
                f"process identity that cannot be published as a non-recyclable scalar; "
                f"a bare pid is refused rather than published as identity"
            ),
        }
        projection["native_session_id"] = None
    return projection


def verify_managed_native_identity(reservation_id: str) -> dict[str, Any]:
    """Re-prove, live, that this generation still holds its provider session.

    The projection a control was resolved from is a statement about the
    past: a control arrives arbitrarily later than the bind that
    authorised it, and in between the pane can die and be replaced, or
    the session can be taken over by a successor generation.  So the
    writer re-asks the same question immediately before it claims the
    write, through the same validator native admission uses.

    Nothing here writes, and every refusal leaves the pane untouched.

    Raises:
        ManagedLaunchConflict: The generation does not hold this session,
            or the process that held it has been replaced.
        ManagedLaunchUnavailable: The pane could not be observed at all.
            Distinct from a conflict on purpose — "the pane is gone" and
            "we could not look" license opposite handling.
    """
    from cli_agent_orchestrator.services import managed_launch_v2 as v2

    record = v2.get(reservation_id)
    if record["execution_mode"] != em.NATIVE_TUI:
        raise ManagedLaunchConflict(
            f"reservation {reservation_id} runs in {record['execution_mode']!r}, not "
            f"{em.NATIVE_TUI!r}; only a native generation has a composer to type into"
        )
    return v2.validate_native_identity_against_live_pane(record)


def managed_control_identity(terminal_id: str) -> Optional[dict[str, Any]]:
    """Resolve an exact managed generation without pane-name inference."""
    with database.SessionLocal() as db:
        row = (
            db.query(database.ManagedLaunchReservationModel)
            .filter(database.ManagedLaunchReservationModel.terminal_id == terminal_id)
            .one_or_none()
        )
        try:
            row_v2 = (
                db.query(database.ManagedLaunchV2ReservationModel)
                .filter(database.ManagedLaunchV2ReservationModel.terminal_id == terminal_id)
                .one_or_none()
            )
        except OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
            row_v2 = None
        if row is not None and row_v2 is not None:
            raise ManagedLaunchConflict(
                f"ambiguous managed terminal identity across protocol vintages: {terminal_id}"
            )
        if row is not None:
            return {
                "reservation_id": str(row.reservation_id),
                "terminal_id": str(row.terminal_id),
                "generation": str(row.generation),
                "provider": str(row.provider),
                "state": str(row.state),
                "controllable": str(row.state) == "admitted",
                "vintage": "v1",
            }
        if row_v2 is not None:
            identity = {
                "reservation_id": str(row_v2.reservation_id),
                "terminal_id": str(row_v2.terminal_id),
                "generation": str(row_v2.generation),
                "provider": str(row_v2.provider),
                "state": str(row_v2.state),
                "controllable": str(row_v2.state) == "admitted",
                "vintage": "v2",
            }
            # The three fields a control caller needs and this projection
            # used to omit entirely. Without them every managed generation
            # read as ACP with both identities null, so a managed native
            # pane was refused by the generic control path and could not
            # be reached at all -- not even once it was admitted.
            identity.update(_v2_native_control_projection(identity["reservation_id"]))
            return identity
    return None


def begin_managed_session_operation(
    terminal_id: str,
    *,
    operation_id: str,
    action: str,
    generation: Optional[str] = None,
    timeout: float = 45.0,
    **payload: Any,
) -> dict[str, Any]:
    """Submit one semantic human control to the exact provider generation."""
    from cli_agent_orchestrator.services.managed_provider_bridge import request_bridge

    identity = managed_control_identity(terminal_id)
    if identity is None:
        raise ManagedLaunchNotFound(f"live managed terminal not found: {terminal_id}")
    if identity["controllable"] is not True:
        raise ManagedLaunchConflict(
            f"managed terminal is not controllable from state {identity['state']!r}"
        )
    if generation is not None and generation != identity["generation"]:
        raise ManagedLaunchConflict("stale managed terminal generation")
    if not operation_id or not action:
        raise ManagedLaunchConflict("operation_id and action are required")
    command = {
        "bridge_version": "cao-native-provider-bridge-v1",
        "op": "session.op.begin",
        "operation_id": operation_id,
        "action": action,
        "reservation_id": identity["reservation_id"],
        "terminal_id": identity["terminal_id"],
        "generation": identity["generation"],
        **payload,
    }
    try:
        response = request_bridge(identity["reservation_id"], command, timeout=timeout)
    except Exception as exc:
        raise ManagedLaunchUnavailable(f"managed session control unavailable: {exc}") from exc
    receipt = response.get("receipt")
    if not isinstance(receipt, dict):
        raise ManagedLaunchUnavailable("managed bridge omitted the session-operation receipt")
    return receipt


def query_managed_session_operation(
    terminal_id: str,
    *,
    operation_id: str,
    generation: Optional[str] = None,
) -> dict[str, Any]:
    """Passively query an operation; never retry its provider effect."""
    from cli_agent_orchestrator.services.managed_provider_bridge import request_bridge

    identity = managed_control_identity(terminal_id)
    if identity is None:
        raise ManagedLaunchNotFound(f"live managed terminal not found: {terminal_id}")
    if identity["controllable"] is not True:
        raise ManagedLaunchConflict(
            f"managed terminal is not controllable from state {identity['state']!r}"
        )
    if generation is not None and generation != identity["generation"]:
        raise ManagedLaunchConflict("stale managed terminal generation")
    try:
        response = request_bridge(
            identity["reservation_id"],
            {
                "bridge_version": "cao-native-provider-bridge-v1",
                "op": "session.op.query",
                "operation_id": operation_id,
                "reservation_id": identity["reservation_id"],
                "terminal_id": identity["terminal_id"],
                "generation": identity["generation"],
            },
            timeout=30.0,
        )
    except Exception as exc:
        raise ManagedLaunchUnavailable(f"managed session control query unavailable: {exc}") from exc
    receipt = response.get("receipt")
    if not isinstance(receipt, dict):
        raise ManagedLaunchUnavailable("managed bridge omitted the session-operation receipt")
    return receipt


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
                "launch-failed-bridge",
                "negative",
                "cancelled",
                "cleanup_intended",
            }:
                raise ManagedLaunchConflict(
                    f"cleanup requires terminal negative evidence, not state {row.state!r}"
                )
            expected_window = managed_window_name(row.terminal_id, row.generation)
            intent = next(
                (
                    item
                    for item in observations
                    if item.get("kind") == "cleanup-intent"
                    and item.get("cleanup_id") == request.cleanup_id
                ),
                None,
            )
            expected_intent = {
                "cleanup_id": request.cleanup_id,
                "reservation_id": reservation_id,
                "terminal_id": row.terminal_id,
                "generation": row.generation,
                "session_name": row.session_name,
                "window_name": expected_window,
            }
            if intent is not None:
                observed_intent = {key: intent.get(key) for key in expected_intent}
                if observed_intent != expected_intent:
                    raise ManagedLaunchConflict("cleanup intent changed after it was persisted")
            else:
                terminal = (
                    db.query(database.TerminalModel)
                    .filter(database.TerminalModel.id == request.terminal_id)
                    .first()
                )
                if terminal is not None and (
                    terminal.generation != request.generation
                    or terminal.tmux_session != row.session_name
                    or terminal.tmux_window != expected_window
                ):
                    raise ManagedLaunchConflict(
                        "cleanup target is not the reserved terminal incarnation"
                    )
                intent = {
                    "kind": "cleanup-intent",
                    **expected_intent,
                    "intended_at": _now(),
                }
                observations.append(intent)
                row.observations_json = _canonical_json(observations)
            row.state = "cleanup_intended"
            row.updated_at = _now()
            db.commit()

        # delete_terminal is idempotent for a missing DB record. It also owns
        # provider and tmux cleanup for this exact reserved terminal id.
        deleted = terminal_service.delete_terminal(
            request.terminal_id,
            registry=registry,
            expected_generation=request.generation,
            expected_session=expected_intent["session_name"],
        )
        if not deleted:
            raise ManagedLaunchUnavailable(
                "exact terminal cleanup returned without a no-survivor proof"
            )

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
            from cli_agent_orchestrator.backends.registry import get_backend

            if get_backend().window_exists(
                expected_intent["session_name"], expected_intent["window_name"]
            ):
                raise ManagedLaunchUnavailable("exact terminal window still exists after cleanup")
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
                    "terminal_window_present": False,
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

    from cli_agent_orchestrator.services import terminal_service
    from cli_agent_orchestrator.services.managed_provider_bridge import (
        BRIDGE_VERSION,
        launch_binding_identity,
        profile_digest,
        read_state,
        request_bridge,
        write_request,
    )

    record, should_launch = claim_launch(reservation_id)
    if not should_launch:
        return _adopt_durable_provider_fact(record)
    request = record["request"]
    if record["provider"] not in READINESS_PROVIDERS:
        return mark_preflight_blocked(
            reservation_id,
            preflight_class="unsupported-provider-readiness",
            detail="managed-launch v1 has no authoritative readiness adapter for this provider",
        )

    try:
        # P1-9 (§20.2f): the provider executable is the reservation-pinned
        # absolute identity — never a PATH resolution.
        provider_executable, provider_digest = _executable_identity(request)
        rendezvous_identity = launch_binding_identity(
            project=request["project"],
            task_id=request["task_id"],
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
            "provider_executable": provider_executable,
            "provider_executable_sha256": provider_digest,
            "project": request["project"],
            "task_id": request["task_id"],
            "delivery_id": request["delivery_id"],
            "rendezvous_identity": rendezvous_identity,
        }
        write_request(reservation_id, bridge_request)
    except Exception as exc:  # noqa: BLE001 - no provider I/O occurred
        return mark_preflight_blocked(
            reservation_id,
            preflight_class="provider-native-preparation",
            detail=str(exc),
        )

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
        )
    except Exception as exc:  # noqa: BLE001 - preserve and expose, never cleanup/retry
        try:
            state = read_state(reservation_id)
        except Exception:  # noqa: BLE001 - generic startup evidence remains truthful
            state = None
        if state and state.get("state") == "launch-failed-bridge":
            return mark_launch_failed_bridge(reservation_id, state)
        return mark_preflight_blocked(
            reservation_id,
            preflight_class="provider-startup-error",
            detail=str(exc),
        )

    try:
        status = await asyncio.to_thread(
            request_bridge,
            reservation_id,
            {"op": "status"},
            timeout=120.0,
        )
    except Exception as exc:  # noqa: BLE001 - query durable state before classifying
        try:
            state = read_state(reservation_id)
        except Exception as state_exc:  # noqa: BLE001 - preserve both failures
            state = None
            exc = ManagedLaunchUnavailable(f"{exc}; durable bridge state unreadable: {state_exc}")
        if state and state.get("state") == "launch-failed-bridge":
            return mark_launch_failed_bridge(reservation_id, state)
        if state and state.get("state") == "preflight_blocked":
            detail = str(state.get("error") or exc)
        else:
            detail = f"exact provider readiness was not established: {exc}"
        return mark_preflight_blocked(
            reservation_id,
            preflight_class="provider-native-readiness",
            detail=detail,
            evidence=state,
        )
    receipt = status.get("readiness")
    if status.get("state") != "ready" or not isinstance(receipt, dict):
        return mark_preflight_blocked(
            reservation_id,
            preflight_class="provider-native-readiness",
            detail="exact provider session did not return a readiness receipt",
            evidence=status,
        )
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

    from cli_agent_orchestrator.services.managed_provider_bridge import (
        read_state,
        request_bridge,
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
        response = await asyncio.to_thread(
            request_bridge,
            reservation_id,
            command,
            timeout=120.0,
        )
    except Exception as exc:  # noqa: BLE001 - delivery may have crossed the boundary
        try:
            state = read_state(reservation_id)
        except Exception as state_exc:  # noqa: BLE001 - ambiguity must remain durable
            state = None
            exc = ManagedLaunchUnavailable(f"{exc}; durable bridge state unreadable: {state_exc}")
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

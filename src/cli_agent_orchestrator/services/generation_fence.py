"""Provider-generation input/effect fence (the fork half of the W13 seal).

On accepted-report finalization the generation is sealed *before* the
callback takes effect: the conductor issues a fence-install RPC over the
generation-private bridge socket, and the fork rejects every post-fence
input/tool admission for the sealed generation at its admission boundary
— queued unsubmitted input is rejected, never silently drained, and no
post-report model/tool entry is authenticated after the fence.

Invariant: the fork holds the per-generation lock (lock class 4) from
request validation through the fence CAS; ``fenced``/``already-fenced``
are the only success outcomes and are idempotent on ``intent_id``; a
distinct ``intent_id`` (or any changed field) for the same generation is
a single-use violation and is refused, so no second distinct fence can
ever be installed.

Failure mode prevented: without the fence, queued input or a post-report
tool call can still reach the provider after the report was sealed,
mutating the tree the callback claims as final — the completed-
generation mutation class that made report truth unverifiable.

Why this guard exists: report finalization is only collectable when the
tree the report describes is provably the tree the callback binds; the
fence is what makes post-report mutation *prevented* rather than merely
detected.

State: ``<COMPANION_DIR>/<terminal_id>/<generation>/fence.json`` (0600,
P-MUT).  The fence receipt is
``{"schema":"cao-w13-fence-receipt-v1","intent_id",…}`` and its digest
is computed over the canonical receipt bytes.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from cli_agent_orchestrator.services.canonical_json import encode_canonical
from cli_agent_orchestrator.services.durable_publish import (
    ABSENT,
    PublicationError,
    publish_mutable,
)

FENCE_REQUEST_SCHEMA = "cao-w13-fence-req-v1"
FENCE_RESPONSE_SCHEMA = "cao-w13-fence-resp-v1"
FENCE_RECEIPT_SCHEMA = "cao-w13-fence-receipt-v1"
FENCE_STATE_SCHEMA = "cao-w13-fence-state-v1"
SEAL_INTENT_SCHEMA = "cao-w13-seal-intent-v1"

# M3 uses the same per-generation state file and, critically, the same lock
# as W13.  A parked generation is an absorbing provider-byte fence, not a
# second, merely advisory, lifecycle marker.
PARK_REQUEST_SCHEMA = "cao-m3-park-req-v1"
PARK_RESPONSE_SCHEMA = "cao-m3-park-resp-v1"
PARK_RECEIPT_SCHEMA = "cao-m3-park-receipt-v1"

OUTCOME_FENCED = "fenced"
OUTCOME_ALREADY_FENCED = "already-fenced"
OUTCOME_UNKNOWN_GENERATION = "unknown-generation"
OUTCOME_VINTAGE_MISMATCH = "vintage-mismatch"
OUTCOME_SUPERSEDED = "superseded-generation"

REQUEST_FIELDS = (
    "terminal_generation",
    "obligation_generation",
    "attempt_id",
    "intent_id",
    "report_sha256",
)


class FenceError(RuntimeError):
    """Base error for fence operations."""


class FenceRequestError(FenceError):
    """The fence-install request is malformed or single-use-violating."""


class FencedError(FenceError):
    """Input/tool admission was attempted against a sealed generation."""


class ParkRequestError(FenceRequestError):
    """An M3 park operation is malformed or conflicts with its receipt."""


def _rfc3339_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def fence_state_path(companion_dir: Path, terminal_id: str, generation: str) -> Path:
    return Path(companion_dir) / terminal_id / generation / "fence.json"


@contextmanager
def _generation_lock(directory: Path) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory / ".fence.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def validate_seal_intent(intent: dict[str, Any]) -> None:
    """Validate the conductor's journaled seal-intent record shape."""
    if intent.get("schema") != SEAL_INTENT_SCHEMA:
        raise FenceRequestError(f"seal intent schema must be {SEAL_INTENT_SCHEMA}")
    for field_name in (
        "project",
        "run_id",
        "terminal_generation",
        "obligation_generation",
        "attempt_id",
        "report_sha256",
        "intent_id",
        "at",
    ):
        if not isinstance(intent.get(field_name), str) or not intent[field_name]:
            raise FenceRequestError(f"seal intent missing field: {field_name}")
    if intent.get("task_id") is not None and not isinstance(intent.get("task_id"), str):
        raise FenceRequestError("seal intent task_id must be a string or null")
    digest = intent["report_sha256"]
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise FenceRequestError("seal intent report_sha256 must be 64 lowercase hex")


def _validate_request(request: dict[str, Any]) -> None:
    if request.get("schema") != FENCE_REQUEST_SCHEMA:
        raise FenceRequestError(f"fence request schema must be {FENCE_REQUEST_SCHEMA}")
    for field_name in REQUEST_FIELDS:
        if not isinstance(request.get(field_name), str) or not request[field_name]:
            raise FenceRequestError(f"fence request missing field: {field_name}")
    digest = request["report_sha256"]
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise FenceRequestError("report_sha256 must be 64 lowercase hex")


RECEIPT_FIELD_ORDER = (
    "schema",
    "intent_id",
    "terminal_generation",
    "fencing_token_id",
    "installed_at",
)


def receipt_digest(receipt: dict[str, Any]) -> str:
    """Digest over the canonical receipt bytes in the fixed field order.

    The receipt survives a JSON round trip through the state store, so
    the digest is computed from an explicitly ordered reconstruction —
    never from whatever key order a deserialized mapping happens to hold.
    """
    ordered = {field: receipt.get(field) for field in RECEIPT_FIELD_ORDER}
    return hashlib.sha256(encode_canonical(ordered)).hexdigest()


def _response(outcome: str, receipt: Optional[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema": FENCE_RESPONSE_SCHEMA,
        "outcome": outcome,
        "fence_receipt_sha256": receipt_digest(receipt) if receipt else None,
    }


def _read_state(path: Path) -> Optional[dict[str, Any]]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FenceError(f"fence state at {path} is not valid JSON") from exc
    if not isinstance(parsed, dict) or parsed.get("schema") != FENCE_STATE_SCHEMA:
        raise FenceError(f"fence state at {path} has an unknown schema")
    return parsed


def _state_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_state(path: Path, state: dict[str, Any], *, old_digest: str | object) -> None:
    try:
        publish_mutable(
            path,
            json.dumps(state, sort_keys=True).encode() + b"\n",
            expected_old_sha256=old_digest,  # type: ignore[arg-type]
        )
    except PublicationError as exc:
        raise FenceError(str(exc)) from exc


def install_fence(
    companion_dir: Path,
    *,
    terminal_id: str,
    generation: str,
    vintage: str,
    request: dict[str, Any],
    fencing_token_id: str,
    superseded: bool = False,
) -> dict[str, Any]:
    """Handle one fence-install RPC; returns the response object.

    ``superseded`` is supplied by the caller's generation registry when
    the generation has been replaced by a newer attempt (the old fencing
    token revoked); a superseded generation can never be fenced anew.
    """
    _validate_request(request)
    if request["terminal_generation"] != generation:
        return _response(OUTCOME_UNKNOWN_GENERATION, None)
    if vintage != "v2":
        return _response(OUTCOME_VINTAGE_MISMATCH, None)
    directory = Path(companion_dir) / terminal_id / generation
    with _generation_lock(directory):
        path = fence_state_path(companion_dir, terminal_id, generation)
        state = _read_state(path)
        if state is not None:
            stored_request = state.get("request") or {}
            if state.get("receipt", {}).get("intent_id") == request["intent_id"] and all(
                stored_request.get(field) == request[field] for field in REQUEST_FIELDS
            ):
                # Crash-after-CAS-before-response reconciliation: the
                # re-issued RPC returns the identical receipt.
                return _response(OUTCOME_ALREADY_FENCED, state["receipt"])
            raise FenceRequestError(
                "a fence is already installed for this generation under a "
                "different intent or identity; intent_id is single-use"
            )
        # Idempotency is deliberately evaluated before current-generation
        # status. A response-lost retry remains entitled to its stored receipt
        # even if a successor has since issued a new fencing token. Only a new
        # intent for that old generation is superseded.
        if superseded:
            return _response(OUTCOME_SUPERSEDED, None)
        receipt = {
            "schema": FENCE_RECEIPT_SCHEMA,
            "intent_id": request["intent_id"],
            "terminal_generation": generation,
            "fencing_token_id": fencing_token_id,
            "installed_at": _rfc3339_now(),
        }
        new_state = {
            "schema": FENCE_STATE_SCHEMA,
            "request": {field: request[field] for field in REQUEST_FIELDS},
            "receipt": receipt,
            "updated_seq": 1,
        }
        _write_state(path, new_state, old_digest=ABSENT)
        return _response(OUTCOME_FENCED, receipt)


_PARK_FIELDS = (
    "operation_id",
    "reservation_id",
    "terminal_id",
    "terminal_generation",
    "logical_task_id",
    "retained_round",
    "obligation_generation",
    "attempt_id",
    "report_sha256",
)


def _validate_park_request(request: dict[str, Any]) -> None:
    if request.get("schema") != PARK_REQUEST_SCHEMA:
        raise ParkRequestError(f"park request schema must be {PARK_REQUEST_SCHEMA}")
    for field in _PARK_FIELDS:
        value = request.get(field)
        if field == "retained_round":
            if not isinstance(value, int) or value < 0:
                raise ParkRequestError("park request retained_round must be a non-negative integer")
        elif not isinstance(value, str) or not value:
            raise ParkRequestError(f"park request missing field: {field}")
    digest = request["report_sha256"]
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise ParkRequestError("park request report_sha256 must be 64 lowercase hex")


def park_receipt_digest(receipt: dict[str, Any]) -> str:
    """Digest a complete M3 receipt by canonical bytes, independent of JSON order."""
    fence = receipt.get("fence_receipt") or {}
    ordered_fence = {field: fence.get(field) for field in RECEIPT_FIELD_ORDER}
    ordered = {
        field: (ordered_fence if field == "fence_receipt" else receipt.get(field))
        for field in (
            "schema",
            "operation_id",
            "reservation_id",
            "terminal_id",
            "terminal_generation",
            "logical_task_id",
            "retained_round",
            "obligation_generation",
            "attempt_id",
            "report_sha256",
            "fence_receipt",
            "fence_receipt_sha256",
            "installed_at",
        )
    }
    return hashlib.sha256(encode_canonical(ordered)).hexdigest()


def _park_response(outcome: str, receipt: Optional[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema": PARK_RESPONSE_SCHEMA,
        "outcome": outcome,
        "park_receipt": receipt,
        "park_receipt_sha256": park_receipt_digest(receipt) if receipt else None,
    }


def install_park(
    companion_dir: Path,
    *,
    request: dict[str, Any],
    fencing_token_id: str,
    superseded: bool = False,
) -> dict[str, Any]:
    """Persist and install one immutable/adoptable M3 park receipt.

    The caller has already re-proven the reservation row.  This function owns
    the durable operation state and the provider-byte linearization point.
    A compatible pre-existing W13 fence is adopted rather than cleared or
    replaced; a later retry reads this exact receipt even after succession.
    """
    _validate_park_request(request)
    terminal_id = request["terminal_id"]
    generation = request["terminal_generation"]
    directory = Path(companion_dir) / terminal_id / generation
    with _generation_lock(directory):
        path = fence_state_path(companion_dir, terminal_id, generation)
        state = _read_state(path)
        old_digest: str | object = _state_digest(path) if state is not None else ABSENT
        existing_park = (state or {}).get("park")
        if existing_park is not None:
            stored_request = existing_park.get("request") or {}
            if all(stored_request.get(field) == request[field] for field in _PARK_FIELDS):
                return _park_response(OUTCOME_ALREADY_FENCED, existing_park["receipt"])
            raise ParkRequestError(
                "park operation already exists with a different immutable intent"
            )

        # There may already be a W13 seal. It is compatible only when it
        # fenced this exact generation/obligation/attempt/report; M3 adopts
        # its token and digest, never writes a second fence.
        if state is not None:
            stored = state.get("request") or {}
            compatible = all(
                stored.get(field) == request[field]
                for field in (
                    "terminal_generation",
                    "obligation_generation",
                    "attempt_id",
                    "report_sha256",
                )
            )
            if not compatible:
                raise ParkRequestError("existing W13 fence is incompatible with this park intent")
            fence_receipt = state["receipt"]
        else:
            if superseded:
                return _park_response(OUTCOME_SUPERSEDED, None)
            fence_receipt = {
                "schema": FENCE_RECEIPT_SCHEMA,
                "intent_id": request["operation_id"],
                "terminal_generation": generation,
                "fencing_token_id": fencing_token_id,
                "installed_at": _rfc3339_now(),
            }
            state = {
                "schema": FENCE_STATE_SCHEMA,
                "request": {
                    "terminal_generation": generation,
                    "obligation_generation": request["obligation_generation"],
                    "attempt_id": request["attempt_id"],
                    "intent_id": request["operation_id"],
                    "report_sha256": request["report_sha256"],
                },
                "receipt": fence_receipt,
                "updated_seq": 0,
            }
        receipt = {
            "schema": PARK_RECEIPT_SCHEMA,
            "operation_id": request["operation_id"],
            "reservation_id": request["reservation_id"],
            "terminal_id": terminal_id,
            "terminal_generation": generation,
            "logical_task_id": request["logical_task_id"],
            "retained_round": request["retained_round"],
            "obligation_generation": request["obligation_generation"],
            "attempt_id": request["attempt_id"],
            "report_sha256": request["report_sha256"],
            "fence_receipt": fence_receipt,
            "fence_receipt_sha256": receipt_digest(fence_receipt),
            "installed_at": _rfc3339_now(),
        }
        state = dict(state)
        state["park"] = {
            "request": {field: request[field] for field in _PARK_FIELDS},
            "receipt": receipt,
        }
        state["updated_seq"] = int(state.get("updated_seq") or 0) + 1
        _write_state(path, state, old_digest=old_digest)
        return _park_response(OUTCOME_FENCED, receipt)


def query_park(
    companion_dir: Path, *, terminal_id: str, generation: str, operation_id: str
) -> dict[str, Any]:
    """Read-only exact-operation reconciliation after response loss/restart."""
    if (
        not isinstance(terminal_id, str)
        or len(terminal_id) != 8
        or any(ch not in "0123456789abcdef" for ch in terminal_id)
    ):
        raise ParkRequestError("park terminal_id must be 8 lowercase hex characters")
    if (
        not isinstance(generation, str)
        or not generation
        or generation in {".", ".."}
        or any(separator in generation for separator in ("/", "\\", "\x00"))
    ):
        raise ParkRequestError("park terminal_generation is not a safe path segment")
    if not isinstance(operation_id, str):
        raise ParkRequestError("park operation_id must be a canonical lowercase UUID")
    try:
        if str(uuid.UUID(operation_id)) != operation_id:
            raise ValueError
    except ValueError as exc:
        raise ParkRequestError("park operation_id must be a canonical lowercase UUID") from exc
    state = _read_state(fence_state_path(companion_dir, terminal_id, generation))
    park = (state or {}).get("park")
    if park is None:
        return _park_response("not-found", None)
    receipt = park.get("receipt")
    if not isinstance(receipt, dict) or receipt.get("operation_id") != operation_id:
        return _park_response("not-found", None)
    return _park_response(OUTCOME_ALREADY_FENCED, receipt)


def installed_receipt(
    companion_dir: Path, terminal_id: str, generation: str
) -> Optional[dict[str, Any]]:
    """The installed fence receipt, or None when the generation is open."""
    state = _read_state(fence_state_path(companion_dir, terminal_id, generation))
    return state["receipt"] if state is not None else None


def verify_fence(
    companion_dir: Path,
    *,
    terminal_id: str,
    generation: str,
    expected_receipt_sha256: str,
) -> bool:
    """Re-verify an installed fence (the final-verified freshness check).

    A fork restart that lost the fence shows up here as a missing or
    digest-mismatched receipt; the caller then re-installs (idempotent by
    intent_id) or refuses finalization.
    """
    receipt = installed_receipt(companion_dir, terminal_id, generation)
    if receipt is None:
        return False
    return receipt_digest(receipt) == expected_receipt_sha256


def assert_admission_open(companion_dir: Path, terminal_id: str, generation: str) -> None:
    """The admission-boundary check: refuse post-fence input/tool entry.

    Every provider-bound input path (task admission, inbox delivery, tool
    admission for the remainder of the reporting turn) must call this
    immediately before submission; a sealed generation rejects the entry
    with zero provider I/O.
    """
    if installed_receipt(companion_dir, terminal_id, generation) is not None:
        raise FencedError(
            f"generation {generation} is sealed by an installed fence; "
            "post-report input/tool admission is prevented"
        )


@contextmanager
def admission_critical_section(
    companion_dir: Path, terminal_id: str, generation: str
) -> Iterator[None]:
    """Hold the generation fence lock across the final recheck AND the I/O.

    A bare ``assert_admission_open`` is a check-then-act seam: a fence
    installed between the check and the provider submission would still
    admit the input.  This context manager takes the same per-generation
    lock ``install_fence`` uses, re-verifies the generation is open under
    that lock, and holds it until the caller's provider/model/tool-entry
    I/O completes — a fence install cannot interleave with an admission.
    """
    directory = Path(companion_dir) / terminal_id / generation
    with _generation_lock(directory):
        assert_admission_open(companion_dir, terminal_id, generation)
        yield


@contextmanager
def managed_admission_critical_section(
    companion_dir: Path,
    terminal_id: str,
    generation: str,
    *,
    attempt_id: str,
    fencing_token_id: str,
) -> Iterator[None]:
    """Canonical managed-byte lock order: successor → generation → effect.

    The successor registry decides which exact producer generation is current,
    so a managed writer must hold it while it revalidates the binding and then
    retain it through the generation fence and provider I/O.  This prevents a
    successor token issuance from racing a native admission after its last
    readiness check but before its first provider byte.
    """
    from cli_agent_orchestrator.services import heartbeat_store

    with heartbeat_store.successor_critical_section(companion_dir, terminal_id):
        try:
            heartbeat_store.assert_current_fencing_binding(
                companion_dir,
                terminal_id=terminal_id,
                generation=generation,
                attempt_id=attempt_id,
                fencing_token_id=fencing_token_id,
            )
        except heartbeat_store.FencingRefused as exc:
            # Every byte lane consumes the fence module's typed refusal. Do
            # not let a successor/token race escape as an implementation
            # error after the caller has already durably claimed an intent.
            raise FencedError(f"managed generation binding is no longer current: {exc}") from exc
        with admission_critical_section(companion_dir, terminal_id, generation):
            yield


class _AsyncAcquireHandoff:
    """Exactly-once ownership transfer for a blocking context-manager enter."""

    __slots__ = ("abandoned", "entered", "lock")

    def __init__(self) -> None:
        self.abandoned = False
        self.entered = False
        self.lock = threading.Lock()


# Flock waiters must never occupy asyncio's shared default executor: native
# holders use that executor for readiness and tmux work before releasing the
# same lock.  A bounded, separate pool keeps cross-process flock semantics
# intact while isolating lock contention from provider-byte progress.
_ASYNC_FLOCK_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cao-fence-flock")


def _enter_or_abandon(manager: Any, handoff: _AsyncAcquireHandoff) -> None:
    """Run blocking enter off-loop; release if cancellation abandoned it."""
    manager.__enter__()
    release = False
    with handoff.lock:
        if handoff.abandoned:
            release = True
        else:
            handoff.entered = True
    if release:
        manager.__exit__(None, None, None)


async def _async_exit(manager: Any) -> None:
    import asyncio

    try:
        await asyncio.shield(asyncio.to_thread(manager.__exit__, None, None, None))
    except asyncio.CancelledError:
        # The worker still owns and completes release; preserve the caller's
        # cancellation after the context manager has been scheduled to close.
        pass


@asynccontextmanager
async def async_admission_critical_section(
    companion_dir: Path,
    terminal_id: str,
    generation: str,
    *,
    attempt_id: Optional[str] = None,
    fencing_token_id: Optional[str] = None,
):
    """Cancellation-safe off-loop wrapper for generation/managed admission.

    Supplying the exact attempt/token selects the managed successor→generation
    lock order.  Omitting both retains the legacy generation-only W13 use for
    unmanaged callers.  Supplying only one is a programming error.
    """
    import asyncio

    if (attempt_id is None) != (fencing_token_id is None):
        raise FenceRequestError("managed admission requires both attempt_id and fencing_token_id")
    manager = (
        managed_admission_critical_section(
            companion_dir,
            terminal_id,
            generation,
            attempt_id=attempt_id,
            fencing_token_id=fencing_token_id,
        )
        if attempt_id is not None and fencing_token_id is not None
        else admission_critical_section(companion_dir, terminal_id, generation)
    )
    entered = False
    handoff = _AsyncAcquireHandoff()
    try:
        try:
            await asyncio.get_running_loop().run_in_executor(
                _ASYNC_FLOCK_EXECUTOR, _enter_or_abandon, manager, handoff
            )
        except BaseException:
            with handoff.lock:
                handoff.abandoned = True
                entered = handoff.entered
            if entered:
                await _async_exit(manager)
                entered = False
            raise
        entered = handoff.entered
        yield
    finally:
        if entered:
            await _async_exit(manager)


@contextmanager
def reconciliation_critical_section(
    companion_dir: Path, terminal_id: str, generation: str
) -> Iterator[None]:
    """Serialize zero-effect reconciliation with provider admission.

    Unlike :func:`admission_critical_section`, this lock remains usable after
    a W13 fence is installed.  Its caller is observing/finalizing an exact
    message outcome rather than attempting new provider I/O.
    """
    directory = Path(companion_dir) / terminal_id / generation
    with _generation_lock(directory):
        yield

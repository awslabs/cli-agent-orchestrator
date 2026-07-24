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
from contextlib import contextmanager
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
    if superseded:
        return _response(OUTCOME_SUPERSEDED, None)

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
        try:
            publish_mutable(
                path,
                json.dumps(new_state, sort_keys=True).encode() + b"\n",
                expected_old_sha256=ABSENT,
            )
        except PublicationError as exc:
            raise FenceError(str(exc)) from exc
        return _response(OUTCOME_FENCED, receipt)


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

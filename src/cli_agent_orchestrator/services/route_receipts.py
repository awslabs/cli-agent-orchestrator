"""Provider-generated authenticated durable route receipts (cond-0069).

The managed provider bridge is the only component that observes the
provider's own route facts: the native session id, the native turn id,
the provider-resolved model/effort (verified equal to the reserved route
at exact-session initialization), the per-session turn sequence, and the
exact model input.  When a managed turn is admitted, the bridge writes a
durable receipt binding those facts, HMAC-authenticated with a
generation-private key and published at an immutable content address.

``GET /managed/recovery-capabilities`` never accepts caller-shaped
dictionaries: it loads these receipts and verifies each one against the
authority boundary — the live v2 reservation's journaled request (the
pinned generation/model/effort) and the delivery journal's journaled
request digest (the expected model input).  Malformed, drifted, missing,
or unsupported receipt evidence exposes no automated authority.

Invariant: a receipt only exists for a turn the provider natively
accepted; its HMAC proves generation-key authorship; its content address
proves immutable publication; validation requires typed identity fields,
a positive event sequence, pinned resolved model/effort, and a 64-hex
model-input digest present in the generation's delivery journal.

Failure mode prevented: capability authority derived from caller-supplied
route dictionaries — structurally malformed or model/input-drifted
"proofs" would advertise automated recovery/strongest-route authority
that no provider ever observed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from cli_agent_orchestrator.services.durable_publish import publish_immutable
from cli_agent_orchestrator.services.provider_contracts import (
    normalized_version,
    validate_route_proof,
)

ROUTE_RECEIPT_SCHEMA = "cao-route-receipt-v1"

# Bridge provider names → capability-surface provider names.
_CAPABILITY_PROVIDER = {"codex": "codex", "kimi_cli": "kimi", "claude_code": "claude"}

# The provider wire protocol each managed bridge speaks (receipt field).
_PROTOCOL_VERSION = {"codex": "app-server/1", "kimi_cli": "acp/1", "claude_code": "stream-json/1"}


class RouteReceiptError(RuntimeError):
    """A route receipt could not be generated or authenticated."""


def capability_provider(provider: str) -> Optional[str]:
    """The capability-surface provider name for a bridge provider name."""
    return _CAPABILITY_PROVIDER.get(provider)


def protocol_version(provider: str) -> str:
    """The pinned wire-protocol label for a managed bridge provider."""
    version = _PROTOCOL_VERSION.get(provider)
    if version is None:
        raise RouteReceiptError(f"no managed route protocol for provider {provider!r}")
    return version


def _canonical(receipt: dict[str, Any]) -> bytes:
    return json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _key_path(state_dir: Path, generation: str) -> Path:
    return Path(state_dir) / "route-keys" / f"{generation}.key"


def receipt_key(state_dir: Path, generation: str) -> bytes:
    """The generation-private HMAC key (created on first use, 0o600).

    Only the managed bridge for this exact generation writes receipts with
    it; readers (the capability endpoint) verify with it.  A key that
    cannot be read fails closed at load time (no authority).
    """
    path = _key_path(state_dir, generation)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="ascii") as fh:
            os.chmod(path, 0o600)
            fh.write(os.urandom(32).hex())
            fh.write("\n")
    except FileExistsError:
        pass
    return bytes.fromhex(path.read_text(encoding="ascii").strip())


def _read_existing_key(state_dir: Path, generation: str) -> Optional[bytes]:
    """The generation key iff it already exists (readers never create)."""
    path = _key_path(state_dir, generation)
    try:
        return bytes.fromhex(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def _receipt_hmac(key: bytes, receipt: dict[str, Any]) -> str:
    unsigned = {k: v for k, v in receipt.items() if k != "receipt_hmac"}
    return hmac.new(key, _canonical(unsigned), hashlib.sha256).hexdigest()


def write_route_receipt(
    *,
    state_dir: Path,
    provider: str,
    native_session_id: str,
    native_turn_id: str,
    generation: str,
    terminal_id: str,
    delivery_id: str,
    expected_model: str,
    expected_effort: str,
    observed_model: str,
    observed_effort: str,
    protocol: str,
    event_sequence: int,
    model_input_digest: str,
    provider_version: str,
) -> dict[str, Any]:
    """Generate, authenticate, and durably publish one route receipt.

    Called by the managed bridge only after the provider natively accepted
    the exact turn.  The receipt is self-checked against the pinned route
    contract before publication; a receipt that would not validate is
    never published.
    """
    capability = capability_provider(provider)
    if capability is None:
        raise RouteReceiptError(f"no route-receipt capability for provider {provider!r}")
    receipt: dict[str, Any] = {
        "schema": ROUTE_RECEIPT_SCHEMA,
        "provider": capability,
        "native_session_id": native_session_id,
        "native_turn_id": native_turn_id,
        "generation": generation,
        "terminal_id": terminal_id,
        "delivery_id": delivery_id,
        "expected_model": expected_model,
        "expected_effort": expected_effort,
        "observed_model": observed_model,
        "observed_effort": observed_effort,
        "protocol_version": protocol,
        "event_sequence": event_sequence,
        "model_input_digest": model_input_digest,
        "non_echo": True,
        "provider_version": provider_version,
        "attested_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if not validate_route_proof(
        capability,
        receipt,
        expected_model=expected_model,
        expected_effort=expected_effort,
        expected_model_input_digest=model_input_digest,
    ):
        raise RouteReceiptError(
            f"refusing to publish a route receipt that fails its own contract: {receipt!r}"
        )
    key = receipt_key(state_dir, generation)
    receipt["receipt_hmac"] = _receipt_hmac(key, receipt)
    state = Path(state_dir)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    publish_immutable(
        state,
        lambda digest: f"route-receipt.{digest[:16]}.json",
        json.dumps(receipt, sort_keys=True).encode() + b"\n",
    )
    return receipt


def journaled_request_digests(reservation_root: Path, obligation_generation: str) -> frozenset:
    """The delivery journal's journaled request digests for one generation.

    The journal is the authority boundary's durable record of the exact
    model input admitted for this generation; a route receipt's
    ``model_input_digest`` must be one of these digests.  A missing or
    unreadable journal yields the empty set (no authority), never a guess.
    """
    journal_path = Path(reservation_root) / "delivery-journal.db"
    if not journal_path.exists():
        return frozenset()
    try:
        conn = sqlite3.connect(f"file:{journal_path}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute(
                "SELECT request_sha256 FROM delivery WHERE obligation_generation=?",
                (obligation_generation,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return frozenset()
    return frozenset(row[0] for row in rows if isinstance(row[0], str) and row[0])


def load_valid_route_proofs(
    *,
    state_dir: Path,
    expected_routes: dict[str, dict[str, Any]],
    expected_input_digests: dict[str, frozenset],
) -> dict[str, dict[str, Any]]:
    """Validated provider route receipts for the capability surface.

    ``expected_routes`` maps capability provider → the authority
    boundary's pinned route (``generation``/``model``/``effort`` from the
    live v2 reservation's journaled request); ``expected_input_digests``
    maps provider → the delivery journal's journaled request digests.  A
    receipt is returned only when it sits at its immutable content
    address, its HMAC verifies against the generation-private key, its
    provider version is a successful observation (parseable — listing is
    not required), its generation matches the live reservation, its
    model-input digest is a journaled digest, and the full typed contract
    validates against the pinned expectation.  Anything else exposes no
    automated authority.
    """
    proofs: dict[str, dict[str, Any]] = {}
    state = Path(state_dir)
    for candidate in sorted(state.glob("route-receipt.*.json")):
        try:
            raw = candidate.read_bytes()
            receipt = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(receipt, dict):
            continue
        # Immutable-publication check: the file must sit at its content address.
        if candidate.name != f"route-receipt.{hashlib.sha256(raw).hexdigest()[:16]}.json":
            continue
        provider = receipt.get("provider")
        expectation = expected_routes.get(provider) if isinstance(provider, str) else None
        if expectation is None:
            continue
        generation = receipt.get("generation")
        if generation != expectation.get("generation"):
            continue
        key = _read_existing_key(state_dir, str(generation))
        if key is None:
            continue
        supplied = receipt.get("receipt_hmac")
        if not isinstance(supplied, str) or not hmac.compare_digest(
            supplied, _receipt_hmac(key, receipt)
        ):
            continue
        version = receipt.get("provider_version")
        if not isinstance(version, str):
            continue
        # Route authority comes from this receipt's own evidence: the HMAC
        # against the generation-private key, the live-reservation
        # generation match, and the journaled model-input digest below.  A
        # build being *unlisted* withdraws none of that — the receipt was
        # minted against the build that actually served the generation.
        # What still exposes no authority is a failed observation: a
        # version that does not parse.
        normalized = normalized_version(version)
        if not normalized:
            continue
        digest = receipt.get("model_input_digest")
        journaled = expected_input_digests.get(str(provider), frozenset())
        if not isinstance(digest, str) or digest not in journaled:
            continue
        if not validate_route_proof(
            str(provider),
            receipt,
            expected_model=expectation.get("model"),
            expected_effort=expectation.get("effort"),
            expected_model_input_digest=digest,
        ):
            continue
        proofs[str(provider)] = receipt
    return proofs

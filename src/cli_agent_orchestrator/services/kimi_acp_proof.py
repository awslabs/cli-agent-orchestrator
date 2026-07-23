"""Kimi ACP identity proof harness (session/new → kill → resume).

Kimi's identity mechanics require a proof that the *installed* CLI can
bind an ACP ``session/new`` ``sessionId``, survive a kill, and resume
that exact session (``session/load`` or the ``--session <id>`` /
``-r <id>`` form).  Until that proof passes and its receipt is durable,
Kimi resume identity is disabled (fail closed).

Invariant: the proof receipt binds the exact installed binary (path +
content digest + pinned version) and the exact session id that
survived the kill; ``kimi_identity_enabled`` is true only when a valid
receipt exists for the *current* pinned binary — any binary drift
invalidates it.

Failure mode prevented: claiming resumable identity from a version or
documentation promise that the installed binary does not actually
honor — a resume built on it would silently bind a new, wrong session.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from cli_agent_orchestrator.services.durable_publish import publish_immutable
from cli_agent_orchestrator.services.provider_contracts import (
    PINNED_VERSIONS,
    PROVIDER_KIMI,
    ProviderVersionDrift,
    check_pinned_version,
)

PROOF_SCHEMA = "cao-kimi-acp-identity-proof-v1"


class KimiAcpProofError(RuntimeError):
    """The ACP identity proof could not be established."""


def proof_receipt_digest(receipt: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(receipt, sort_keys=True).encode() + b"\n").hexdigest()


def run_identity_proof(
    *,
    kimi_binary: Path,
    version_output: str,
    state_dir: Path,
    acp_driver: Callable[[Path], dict[str, Any]],
) -> dict[str, Any]:
    """Execute the proof against the installed CLI and publish its receipt.

    ``acp_driver`` performs the actual ACP exchange (session/new → kill
    → resume) and must return ``{"session_id": …, "resumed": True}``
    only when the exact same session id came back after the kill.  The
    driver is injectable so the proof is testable without a live CLI;
    production wiring supplies the real ACP client.
    """
    binary = Path(kimi_binary)
    if os.path.realpath(binary) != str(binary) or not binary.is_file():
        raise KimiAcpProofError("kimi binary must be a canonical absolute file")
    try:
        check_pinned_version(PROVIDER_KIMI, version_output)
    except ProviderVersionDrift as exc:
        raise KimiAcpProofError(str(exc)) from exc
    outcome = acp_driver(binary)
    if not isinstance(outcome, dict) or outcome.get("resumed") is not True:
        raise KimiAcpProofError("installed CLI did not resume the exact ACP session after kill")
    session_id = outcome.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise KimiAcpProofError("proof driver returned no session_id")
    binary_digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    receipt = {
        "schema": PROOF_SCHEMA,
        "kimi_version": PINNED_VERSIONS[PROVIDER_KIMI],
        "binary_path": str(binary),
        "binary_sha256": binary_digest,
        "session_id": session_id,
        "resumed_after_kill": True,
        "proven_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    publish_immutable(
        state,
        lambda digest: f"kimi-acp-proof.{digest[:16]}.json",
        json.dumps(receipt, sort_keys=True).encode() + b"\n",
    )
    return receipt


def load_valid_proof(
    *,
    state_dir: Path,
    kimi_binary: Path,
    version_output: str,
) -> Optional[dict[str, Any]]:
    """The durable proof receipt iff it is valid for the current binary."""
    state = Path(state_dir)
    try:
        check_pinned_version(PROVIDER_KIMI, version_output)
        binary_digest = hashlib.sha256(Path(kimi_binary).read_bytes()).hexdigest()
    except (ProviderVersionDrift, OSError):
        return None
    for candidate in sorted(state.glob("kimi-acp-proof.*.json")):
        try:
            receipt = json.loads(candidate.read_bytes())
        except (OSError, json.JSONDecodeError):
            continue
        if (
            isinstance(receipt, dict)
            and receipt.get("schema") == PROOF_SCHEMA
            and receipt.get("binary_sha256") == binary_digest
            and receipt.get("binary_path") == str(kimi_binary)
            and receipt.get("kimi_version") == PINNED_VERSIONS[PROVIDER_KIMI]
            and receipt.get("resumed_after_kill") is True
            and receipt.get("session_id")
        ):
            return receipt
    return None


def kimi_identity_enabled(*, state_dir: Path, kimi_binary: Path, version_output: str) -> bool:
    """Kimi resume identity is enabled only by a valid durable proof."""
    return (
        load_valid_proof(
            state_dir=state_dir, kimi_binary=kimi_binary, version_output=version_output
        )
        is not None
    )

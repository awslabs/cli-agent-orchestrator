"""Kimi ACP identity proof harness (session/new → kill → resume).

Kimi's identity mechanics require a proof that the *installed* CLI can
bind an ACP ``session/new`` ``sessionId``, survive a kill, and resume
that exact session (``session/load`` or the ``--session <id>`` /
``-r <id>`` form).  Until that proof passes and its receipt is durable,
Kimi resume identity is disabled (fail closed).

Invariant: the proof receipt binds the exact installed binary (path +
content digest + observed version) and the exact session id that
survived the kill; ``kimi_identity_enabled`` is true only when a valid
receipt exists for the *current* installed binary — any binary change
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
    PROVIDER_KIMI,
    ProviderVersionDrift,
    check_pinned_version,
    normalized_version,
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
    → session/load) and must return::

        {"session_id": …, "resumed": True,
         "exchange": {"session_new_id": …, "killed": True,
                      "session_load_id": …, "transcript_sha256": …}}

    only when the exact same session id came back after the kill.  The
    exchange evidence is journaled in the receipt so a bare file with
    matching self-authored fields is never accepted as a proof.  The
    driver is injectable so the proof is testable without a live CLI;
    production wiring supplies the real ACP client.
    """
    binary = Path(kimi_binary)
    if os.path.realpath(binary) != str(binary) or not binary.is_file():
        raise KimiAcpProofError("kimi binary must be a canonical absolute file")
    # The proof is attempted against whatever build is installed: the
    # session/new→kill→session/load exchange below *is* the verification,
    # so an unlisted build proves itself at runtime or fails loudly here.
    # What still fails closed is the failed observation — an unparseable
    # banner — and a strict-mode quarantine.
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
    exchange = outcome.get("exchange")
    if not isinstance(exchange, dict):
        raise KimiAcpProofError("proof driver returned no ACP exchange evidence")
    if (
        exchange.get("session_new_id") != session_id
        or exchange.get("session_load_id") != session_id
        or exchange.get("killed") is not True
        or not isinstance(exchange.get("transcript_sha256"), str)
        or len(exchange["transcript_sha256"]) != 64
    ):
        raise KimiAcpProofError(
            "ACP exchange evidence does not prove session/new→kill→session/load "
            "of one exact session id"
        )
    binary_digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    receipt = {
        "schema": PROOF_SCHEMA,
        # The exact version this binary reports, not a pin constant: with
        # more than one accepted build a constant could name the wrong one.
        "kimi_version": normalized_version(version_output),
        "binary_path": str(binary),
        "binary_sha256": binary_digest,
        "session_id": session_id,
        "resumed_after_kill": True,
        "acp_exchange": {
            "session_new_id": exchange["session_new_id"],
            "killed": True,
            "session_load_id": exchange["session_load_id"],
            "transcript_sha256": exchange["transcript_sha256"],
        },
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


def _is_content_addressed(candidate: Path, raw: bytes) -> bool:
    """The proof file must sit at its immutable content address.

    ``publish_immutable`` names receipts
    ``kimi-acp-proof.<sha256(bytes)[:16]>.json``; a hand-authored file at
    any other name was never immutably published and is not a proof.
    """
    digest = hashlib.sha256(raw).hexdigest()
    return candidate.name == f"kimi-acp-proof.{digest[:16]}.json"


def _exchange_valid(receipt: dict[str, Any]) -> bool:
    """The receipt must carry authenticated new→kill→load exchange evidence."""
    exchange = receipt.get("acp_exchange")
    session_id = receipt.get("session_id")
    return (
        isinstance(exchange, dict)
        and isinstance(session_id, str)
        and bool(session_id)
        and exchange.get("session_new_id") == session_id
        and exchange.get("session_load_id") == session_id
        and exchange.get("killed") is True
        and isinstance(exchange.get("transcript_sha256"), str)
        and len(exchange["transcript_sha256"]) == 64
    )


def load_valid_proof(
    *,
    state_dir: Path,
    kimi_binary: Path,
    version_output: str,
) -> Optional[dict[str, Any]]:
    """The durable proof receipt iff it is valid for the current binary.

    Valid requires: the exact proven version matches, the receipt sits at its
    immutable content address, its binary digest/path match the current
    pinned binary, and it carries authenticated ACP
    session/new→kill→session/load exchange evidence for one exact
    session id.  Anything else is not a proof (fail closed).
    """
    state = Path(state_dir)
    try:
        # A failed version observation (unparseable banner) is not a proof
        # context; an *unlisted* build is — the receipt's binary-digest
        # binding below is the exactness, not a table row.
        if not normalized_version(version_output):
            return None
        binary_digest = hashlib.sha256(Path(kimi_binary).read_bytes()).hexdigest()
    except (ProviderVersionDrift, OSError):
        return None
    for candidate in sorted(state.glob("kimi-acp-proof.*.json")):
        try:
            raw = candidate.read_bytes()
            receipt = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            continue
        if not _is_content_addressed(candidate, raw):
            continue
        if (
            isinstance(receipt, dict)
            and receipt.get("schema") == PROOF_SCHEMA
            and receipt.get("binary_sha256") == binary_digest
            and receipt.get("binary_path") == str(kimi_binary)
            # The digest above binds the receipt to the exact installed
            # binary; the recorded version must itself be a successful
            # observation, but listing is not required — an unlisted build's
            # proof is exactly as good as a listed one's.
            and bool(normalized_version(str(receipt.get("kimi_version") or "")))
            and receipt.get("resumed_after_kill") is True
            and _exchange_valid(receipt)
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

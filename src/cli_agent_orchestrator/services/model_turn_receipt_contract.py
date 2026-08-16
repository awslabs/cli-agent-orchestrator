"""Strict v1 wire contract for provider model-turn receipts (single facade).

This module is the fork's compatibility facade over the canonical
``conductor_sentinel.model_turn_receipt_contract``:

- When the installed ``conductor_sentinel`` distribution is present, the
  canonical constants, exception class, functions, and named timestamp
  vectors are re-exported here as the exact same objects — so the fork's
  producer/consumer paths and the conductor's consumer run one definition
  and can never silently drift.
- When the top-level ``conductor_sentinel`` package is genuinely absent, the
  standalone v1 implementation below provides the fork's historic behavior.
- A broken installed package (its contract submodule missing, or any internal
  canonical import failing) is NEVER a reason to fall back: those failures
  propagate visibly instead.

``CONTRACT_SOURCE`` is a static diagnostic discriminator whose only values are
``"conductor-sentinel"`` and ``"standalone"``. It reports which contract was
selected; it grants no authority and is never a gate or override.
"""

from __future__ import annotations

import importlib
import re
from datetime import datetime, timezone
from typing import Any, Mapping

SCHEMA = "cao-model-turn-receipt-v1"
KIND = "submitted"
SOURCE = "provider-adapter"
FIELDS = (
    "schema",
    "kind",
    "source",
    "message_id",
    "message_sha256",
    "message_created_at",
    "sender_id",
    "sender_generation",
    "receiver_id",
    "receiver_generation",
    "provider",
    "provider_session_id",
    "provider_turn_id",
    "submitted_at",
)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class ReceiptValidationError(ValueError):
    pass


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ReceiptValidationError("receipt timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def validate_receipt(payload: Any, *, expected: Mapping[str, Any] | None = None) -> dict[str, str]:
    if not isinstance(payload, dict) or set(payload) != set(FIELDS):
        raise ReceiptValidationError("receipt must have the exact v1 field set")
    if payload["schema"] != SCHEMA or payload["kind"] != KIND or payload["source"] != SOURCE:
        raise ReceiptValidationError("receipt literal is not strict v1")
    for field in FIELDS:
        value = payload[field]
        if not isinstance(value, str) or not value or value.strip() != value:
            raise ReceiptValidationError(f"receipt field {field} is not a nonempty string")
    if _DIGEST.fullmatch(payload["message_sha256"]) is None:
        raise ReceiptValidationError("receipt message digest is invalid")
    if not payload["message_created_at"].endswith("Z") or not payload["submitted_at"].endswith("Z"):
        raise ReceiptValidationError("receipt timestamps must use canonical UTC Z form")
    try:
        created = datetime.fromisoformat(payload["message_created_at"].removesuffix("Z") + "+00:00")
        submitted = datetime.fromisoformat(payload["submitted_at"].removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ReceiptValidationError("receipt timestamp is invalid") from exc
    if submitted < created:
        raise ReceiptValidationError("receipt submission predates message creation")
    if expected:
        for field, value in expected.items():
            if field not in FIELDS or str(payload[field]) != str(value):
                raise ReceiptValidationError(f"receipt field {field} contradicts context")
    return {field: payload[field] for field in FIELDS}


def build_receipt(
    *,
    message_id: Any,
    message_sha256: str,
    message_created_at: datetime,
    sender_id: str,
    sender_generation: str,
    receiver_id: str,
    receiver_generation: str,
    provider: str,
    provider_session_id: str,
    provider_turn_id: str,
    submitted_at: datetime,
) -> dict[str, str]:
    payload = {
        "schema": SCHEMA,
        "kind": KIND,
        "source": SOURCE,
        "message_id": str(message_id),
        "message_sha256": message_sha256,
        "message_created_at": _timestamp(message_created_at),
        "sender_id": sender_id,
        "sender_generation": sender_generation,
        "receiver_id": receiver_id,
        "receiver_generation": receiver_generation,
        "provider": provider,
        "provider_session_id": provider_session_id,
        "provider_turn_id": provider_turn_id,
        "submitted_at": _timestamp(submitted_at),
    }
    return validate_receipt(payload)


_canonical_contract: Any
try:
    _canonical_contract = importlib.import_module("conductor_sentinel.model_turn_receipt_contract")
except ModuleNotFoundError as exc:
    if exc.name != "conductor_sentinel":
        # Installed but broken (submodule or internal import missing): fail
        # visibly rather than silently running the standalone implementation.
        raise
    _canonical_contract = None

if _canonical_contract is not None:
    CONTRACT_SOURCE = "conductor-sentinel"
    # Installed mode: swap the standalone definitions for the canonical
    # objects themselves (identity preserved; nothing is wrapped or copied).
    SCHEMA = _canonical_contract.SCHEMA
    KIND_SUBMITTED = _canonical_contract.KIND_SUBMITTED
    SOURCE_PROVIDER_ADAPTER = _canonical_contract.SOURCE_PROVIDER_ADAPTER
    FIELDS = _canonical_contract.FIELDS
    TIMESTAMP_FIELDS = _canonical_contract.TIMESTAMP_FIELDS
    TIMESTAMP_VECTORS = _canonical_contract.TIMESTAMP_VECTORS
    ReceiptValidationError = _canonical_contract.ReceiptValidationError  # type: ignore[misc]
    message_digest = _canonical_contract.message_digest
    is_message_digest = _canonical_contract.is_message_digest
    canonical_message_id = _canonical_contract.canonical_message_id
    parse_rfc3339_utc = _canonical_contract.parse_rfc3339_utc
    format_rfc3339_utc = _canonical_contract.format_rfc3339_utc
    receipt_endpoint_path = _canonical_contract.receipt_endpoint_path
    validate_receipt = _canonical_contract.validate_receipt
    build_receipt = _canonical_contract.build_receipt
    # Legacy fork aliases point at the canonical objects.
    KIND = KIND_SUBMITTED
    SOURCE = SOURCE_PROVIDER_ADAPTER
else:
    CONTRACT_SOURCE = "standalone"

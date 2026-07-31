"""Strict v1 wire contract for provider model-turn receipts."""

from __future__ import annotations

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

"""Closed, secret-free receipt schema for one M3-B4 exact-restore canary."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

RECEIPT_SCHEMA = "cao-m3-exact-restore-canary-receipt-v1"
EXECUTION_MODE = "native_tui"
OUTCOMES = frozenset(
    {"accepted", "refused", "conflict", "reconciliation-required", "unavailable", "error"}
)
SESSION_PROOFS = frozenset({"argv", "kimi-native-header-v1"})
EXPECTED_EFFECT_STEPS = (
    "fence_prior",
    "reap_prior",
    "release_attachment",
    "acquire_native",
    "create_pane",
    "launch_resume",
    "verify_identity",
)
_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema",
        "canary_id",
        "recorded_at",
        "provider",
        "harness",
        "execution_mode",
        "installed",
        "operation_id",
        "restore_contract_id",
        "restore_contract_digest",
        "launch_material_digest",
        "native_session_id_sha256",
        "session_proof",
        "prior_generation_sha256",
        "successor_generation_sha256",
        "generation_changed",
        "effect_steps_observed",
        "admit_input_absent",
        "task_bytes_sent",
        "outcome",
        "error_class",
        "environment",
    }
)
_INSTALLED_FIELDS = frozenset(
    {
        "executable_path_sha256",
        "executable_sha256",
        "version_banner_sha256",
        "normalized_version",
    }
)
_ENVIRONMENT_FIELDS = frozenset(
    {
        "tmux_server_socket_sha256",
        "state_root_sha256",
        "private_tmux",
        "shared_server_untouched",
    }
)
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


class CanaryReceiptInvalid(ValueError):
    """A receipt is malformed or claims more than the canary proved."""


def _closed_mapping(value: Any, fields: frozenset[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CanaryReceiptInvalid(f"{label} must be a mapping")
    copied = dict(value)
    unknown = sorted(set(copied) - fields)
    missing = sorted(fields - set(copied))
    if unknown or missing:
        raise CanaryReceiptInvalid(
            f"{label} must use the closed field set; unknown={unknown}, missing={missing}"
        )
    return copied


def _text(value: Any, *, label: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise CanaryReceiptInvalid(f"{label} must be non-empty bounded text")
    return value


def _uuid(value: Any, *, label: str) -> str:
    text = _text(value, label=label, maximum=64)
    try:
        if str(uuid.UUID(text)) != text:
            raise ValueError
    except ValueError as exc:
        raise CanaryReceiptInvalid(f"{label} must be a canonical lowercase UUID") from exc
    return text


def _digest(value: Any, *, label: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise CanaryReceiptInvalid(f"{label} must be a lowercase sha256 digest")
    return value


def validate_receipt(value: Any) -> dict[str, Any]:
    """Validate and return a detached JSON-safe copy of one receipt.

    A successful receipt is intentionally strict: it proves the entire B3
    effect sequence, a changed generation, no admission, and no task bytes.
    Failed receipts may carry a proper prefix of the sequence but may never
    claim an admission or task delivery.
    """

    receipt = _closed_mapping(value, _TOP_LEVEL_FIELDS, label="receipt")
    if receipt["schema"] != RECEIPT_SCHEMA:
        raise CanaryReceiptInvalid(f"schema must be {RECEIPT_SCHEMA!r}")
    _uuid(receipt["canary_id"], label="canary_id")
    if (
        not isinstance(receipt["recorded_at"], str)
        or _UTC_RE.fullmatch(receipt["recorded_at"]) is None
    ):
        raise CanaryReceiptInvalid("recorded_at must be an RFC3339 UTC timestamp")
    _text(receipt["provider"], label="provider", maximum=128)
    _text(receipt["harness"], label="harness", maximum=128)
    if receipt["execution_mode"] != EXECUTION_MODE:
        raise CanaryReceiptInvalid(f"execution_mode must be {EXECUTION_MODE!r}")

    installed = _closed_mapping(receipt["installed"], _INSTALLED_FIELDS, label="installed")
    for field in (
        "executable_path_sha256",
        "executable_sha256",
        "version_banner_sha256",
    ):
        _digest(installed[field], label=f"installed.{field}")
    _text(installed["normalized_version"], label="installed.normalized_version", maximum=128)

    _uuid(receipt["operation_id"], label="operation_id")
    _uuid(receipt["restore_contract_id"], label="restore_contract_id")
    for field in (
        "restore_contract_digest",
        "launch_material_digest",
        "native_session_id_sha256",
        "prior_generation_sha256",
        "successor_generation_sha256",
    ):
        _digest(receipt[field], label=field)
    if receipt["session_proof"] not in SESSION_PROOFS:
        raise CanaryReceiptInvalid(f"session_proof must be one of {sorted(SESSION_PROOFS)}")
    if not isinstance(receipt["generation_changed"], bool):
        raise CanaryReceiptInvalid("generation_changed must be a boolean")

    steps = receipt["effect_steps_observed"]
    if not isinstance(steps, list) or any(not isinstance(step, str) for step in steps):
        raise CanaryReceiptInvalid("effect_steps_observed must be a list of strings")
    if tuple(steps) != EXPECTED_EFFECT_STEPS[: len(steps)]:
        raise CanaryReceiptInvalid("effect_steps_observed must be an ordered B3 prefix")
    if "admit_input" in steps or receipt["admit_input_absent"] is not True:
        raise CanaryReceiptInvalid("a B4 receipt may never claim the admit_input effect")
    if receipt["task_bytes_sent"] is not False:
        raise CanaryReceiptInvalid("a B4 canary must send zero task bytes")

    outcome = receipt["outcome"]
    if outcome not in OUTCOMES:
        raise CanaryReceiptInvalid(f"outcome must be one of {sorted(OUTCOMES)}")
    if receipt["error_class"] is not None:
        _text(receipt["error_class"], label="error_class", maximum=128)
    if outcome == "accepted":
        if tuple(steps) != EXPECTED_EFFECT_STEPS:
            raise CanaryReceiptInvalid("accepted requires the complete B3 effect sequence")
        if receipt["generation_changed"] is not True:
            raise CanaryReceiptInvalid("accepted requires a fresh successor generation")
        if receipt["error_class"] is not None:
            raise CanaryReceiptInvalid("accepted cannot carry error_class")

    environment = _closed_mapping(receipt["environment"], _ENVIRONMENT_FIELDS, label="environment")
    _digest(
        environment["tmux_server_socket_sha256"],
        label="environment.tmux_server_socket_sha256",
    )
    _digest(environment["state_root_sha256"], label="environment.state_root_sha256")
    if environment["private_tmux"] is not True:
        raise CanaryReceiptInvalid("a receipt is valid only for a proven private tmux server")
    if environment["shared_server_untouched"] is not True:
        raise CanaryReceiptInvalid("the shared-server survival check must pass")

    encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 16_384:
        raise CanaryReceiptInvalid("receipt exceeds the 16 KiB evidence bound")
    return cast(dict[str, Any], json.loads(encoded))


def receipt_digest(value: Any) -> str:
    receipt = validate_receipt(value)
    payload = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def write_receipt(path: Path, value: Any) -> Path:
    receipt = validate_receipt(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path

"""Fail-closed admission against an already-verified private policy snapshot."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import NoReturn, Optional, cast

from cli_agent_orchestrator.constants import PROVIDERS
from cli_agent_orchestrator.services.launch_policy import (
    EffectiveLaunchPolicy,
    decode_launch_policy_material,
)
from cli_agent_orchestrator.services.private_plan_snapshot import (
    VerifiedPrivateSnapshot,
    structured_material_bytes,
)

_POLICY_SCHEMA = "execution-policy-private-v1"
_SAFE_LABEL = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ENTRY_KEYS = frozenset({"target_key", "agent_profile", "policy", "policy_hash"})


class AgentAdmissionError(ValueError):
    """Safe public refusal raised before an agent launch can begin."""

    retryable = False

    def __init__(self, message: str, *, code: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class LaunchAdmissionError(AgentAdmissionError):
    """Safe refusal for an already-admitted binding's frozen policy read."""


def _refuse_unavailable() -> NoReturn:
    raise AgentAdmissionError(
        "agent admission policy is unavailable",
        code="agent_policy_unavailable",
        status_code=409,
    ) from None


def _is_safe_label(value: object) -> bool:
    return isinstance(value, str) and _SAFE_LABEL.fullmatch(value) is not None


def _decode_policy_entries(snapshot: VerifiedPrivateSnapshot) -> list[dict[str, object]]:
    failed = False
    entries: list[dict[str, object]] = []
    try:
        aggregate = snapshot.decode_material("policy")
        if (
            not isinstance(aggregate, dict)
            or set(aggregate) != {"schema", "policies"}
            or aggregate.get("schema") != _POLICY_SCHEMA
            or not isinstance(aggregate.get("policies"), list)
        ):
            _refuse_unavailable()

        raw_entries = aggregate["policies"]
        previous_pair: Optional[tuple[str, str]] = None
        for raw_entry in raw_entries:
            if (
                not isinstance(raw_entry, dict)
                or set(raw_entry) != _ENTRY_KEYS
                or not _is_safe_label(raw_entry.get("target_key"))
                or not _is_safe_label(raw_entry.get("agent_profile"))
                or not isinstance(raw_entry.get("policy"), dict)
                or not isinstance(raw_entry.get("policy_hash"), str)
                or _DIGEST.fullmatch(raw_entry["policy_hash"]) is None
            ):
                _refuse_unavailable()
            entry = cast(dict[str, object], raw_entry)
            pair = (
                cast(str, entry["target_key"]),
                cast(str, entry["agent_profile"]),
            )
            if previous_pair is not None and pair <= previous_pair:
                _refuse_unavailable()
            previous_pair = pair
            entries.append(entry)
        for entry in entries:
            _decode_entry(entry)
        return entries
    except AgentAdmissionError:
        raise
    except Exception:
        failed = True
    if failed:
        _refuse_unavailable()
    raise AssertionError("unreachable")


def _decode_entry(entry: dict[str, object]) -> EffectiveLaunchPolicy:
    try:
        material = structured_material_bytes(entry["policy"])
        policy = decode_launch_policy_material(
            material,
            expected_policy_hash=cast(str, entry["policy_hash"]),
        )
        if (
            policy.binding.target_key != entry["target_key"]
            or policy.binding.agent_profile != entry["agent_profile"]
            or not _is_safe_label(policy.binding.target_key)
            or not _is_safe_label(policy.binding.agent_profile)
            or policy.derived.provider not in PROVIDERS
        ):
            _refuse_unavailable()
        return policy
    except AgentAdmissionError:
        raise
    except Exception:
        pass
    _refuse_unavailable()


def _normalise_requested_tools(
    requested: Optional[Sequence[str]],
) -> tuple[bool, Optional[tuple[str, ...]]]:
    if requested is None:
        return True, None
    if type(requested) is not list and type(requested) is not tuple:
        return False, None
    requested_snapshot = tuple(requested)
    if not all(type(tool) is str for tool in requested_snapshot):
        return False, None
    return True, requested_snapshot


def _refuse_not_declared() -> NoReturn:
    raise AgentAdmissionError(
        "requested agent is not declared",
        code="agent_not_declared",
        status_code=403,
    ) from None


def _refuse_provider_shape() -> NoReturn:
    raise AgentAdmissionError(
        "requested provider is not a recognised provider id",
        code="agent_provider_mismatch",
        status_code=403,
    ) from None


def _refuse_model() -> NoReturn:
    raise AgentAdmissionError(
        "requested model does not match the approved declaration",
        code="agent_model_mismatch",
        status_code=403,
    ) from None


def _refuse_tools() -> NoReturn:
    raise AgentAdmissionError(
        "requested tools do not match the approved declaration",
        code="agent_tools_mismatch",
        status_code=403,
    ) from None


def _refuse_frozen_policy() -> NoReturn:
    raise LaunchAdmissionError(
        "frozen launch policy is unavailable",
        code="frozen_policy_unavailable",
        status_code=409,
    ) from None


def _refuse_policy_hash() -> NoReturn:
    raise LaunchAdmissionError(
        "frozen launch policy hash does not match the expected reference",
        code="policy_hash_mismatch",
        status_code=409,
    ) from None


def policy_for_binding(
    snapshot: VerifiedPrivateSnapshot,
    *,
    target_key: str,
    agent_profile: str,
    expected_policy_hash: str,
) -> EffectiveLaunchPolicy:
    """Read one already-admitted policy using an external immutable hash."""
    if type(target_key) is not str or not _is_safe_label(target_key):
        _refuse_frozen_policy()
    if type(agent_profile) is not str or not _is_safe_label(agent_profile):
        _refuse_frozen_policy()
    if type(expected_policy_hash) is not str or _DIGEST.fullmatch(expected_policy_hash) is None:
        _refuse_policy_hash()

    entries = _decode_policy_entries(snapshot)
    matching = [
        entry
        for entry in entries
        if entry["target_key"] == target_key and entry["agent_profile"] == agent_profile
    ]
    if len(matching) != 1:
        _refuse_frozen_policy()

    try:
        material = structured_material_bytes(matching[0]["policy"])
        return decode_launch_policy_material(
            material,
            expected_policy_hash=expected_policy_hash,
        )
    except Exception:
        pass
    _refuse_policy_hash()


def admit_agent_launch(
    snapshot: VerifiedPrivateSnapshot,
    *,
    target_key: str,
    agent_profile: str,
    requested_provider: str,
    requested_model: Optional[str] = None,
    requested_allowed_tools: Optional[Sequence[str]] = None,
) -> EffectiveLaunchPolicy:
    """Return the frozen policy only when the raw launch declarations agree."""
    if type(target_key) is not str or not _is_safe_label(target_key):
        _refuse_not_declared()
    if type(agent_profile) is not str or not _is_safe_label(agent_profile):
        _refuse_not_declared()
    if type(requested_provider) is not str:
        _refuse_provider_shape()
    if requested_model is not None and type(requested_model) is not str:
        _refuse_model()
    tools_valid, requested_tools = _normalise_requested_tools(requested_allowed_tools)
    if not tools_valid:
        _refuse_tools()

    entries = _decode_policy_entries(snapshot)
    matching = [
        entry
        for entry in entries
        if entry["target_key"] == target_key and entry["agent_profile"] == agent_profile
    ]
    if len(matching) != 1:
        if len(matching) > 1:
            _refuse_unavailable()
        _refuse_not_declared()

    policy = _decode_entry(matching[0])
    if requested_provider != policy.derived.provider:
        if requested_provider not in PROVIDERS:
            _refuse_provider_shape()
        else:
            message = (
                f"requested provider '{requested_provider}' does not match "
                f"approved provider '{policy.derived.provider}'"
            )
        raise AgentAdmissionError(
            message,
            code="agent_provider_mismatch",
            status_code=403,
        )
    if requested_model != policy.binding.declared_model:
        _refuse_model()
    if requested_tools != policy.binding.declared_allowed_tools:
        _refuse_tools()
    return policy


__all__ = [
    "AgentAdmissionError",
    "LaunchAdmissionError",
    "admit_agent_launch",
    "policy_for_binding",
]

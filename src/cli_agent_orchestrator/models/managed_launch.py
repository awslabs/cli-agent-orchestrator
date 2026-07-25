"""Typed request models for the managed-launch companion protocol."""

from __future__ import annotations

import re
import uuid
from typing import Literal, Optional, TypeAlias

from pydantic import BaseModel, Field, field_validator

PROTOCOL_VERSION = "cao-managed-launch-v1"
ProtocolVersion: TypeAlias = Literal["cao-managed-launch-v1"]
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


def _uuid_text(value: str, field_name: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{field_name} must be a canonical UUID") from exc
    canonical = str(parsed)
    if value != canonical:
        raise ValueError(f"{field_name} must use canonical lowercase UUID text")
    return canonical


class ManagedLaunchReserveRequest(BaseModel):
    protocol_version: ProtocolVersion
    reservation_id: str
    session_name: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    agent_profile: str = Field(min_length=1)
    caller_id: str = Field(pattern=r"^[a-f0-9]{8}$")
    # Immutable orchestration identity is present before the no-task launch
    # so the bridge rendezvous binds the exact later admission context.
    project: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    # The caller preallocates delivery identity before launch so a fatal
    # post-allocation bridge exit can finalize that exact delivery as never
    # submitted in the same reservation transaction. Missing identity fails
    # request validation before reservation/provider effects.
    delivery_id: str
    working_directory: str = Field(min_length=1)
    trusted_project_root: Optional[str] = None
    expected_model: str = Field(min_length=1)
    expected_effort: str = Field(min_length=1)
    # P1-9 (final conformance §20.2f): the conductor pins the provider
    # executable's absolute canonical path and digest at reservation time;
    # the fork verifies and executes exactly this identity and never
    # PATH-resolves a provider on the managed campaign path.
    provider_executable: Optional[str] = None
    provider_executable_sha256: Optional[str] = None
    #: The execution mode the caller asks this reservation to run in, or
    #: ``None`` when it names none.  ``None`` is *unspecified*, never
    #: "either": it defers to the worker-class default, which keeps a
    #: caller that names neither on the historical ACP branch.  An older
    #: client that has never heard of this field therefore behaves
    #: exactly as it did before.  A value outside the closed enum is
    #: rejected here rather than coerced, so a malformed mode can never
    #: reach a store, a launch, or a receipt.
    execution_mode: Optional[Literal["native_tui", "acp"]] = None
    #: The worker class that supplies the default when no explicit mode
    #: is given.  Absent a class the default stays ACP; a class is never
    #: a way to *override* an explicit mode, only to fill in a missing
    #: one.
    worker_class: Optional[
        Literal[
            "persistent",
            "long_running",
            "human_monitored",
            "one_shot",
            "hands_off",
            "unspecified",
        ]
    ] = None

    @field_validator("reservation_id")
    @classmethod
    def _validate_reservation_id(cls, value: str) -> str:
        return _uuid_text(value, "reservation_id")

    @field_validator("delivery_id")
    @classmethod
    def _validate_delivery_id(cls, value: str) -> str:
        return _uuid_text(value, "delivery_id")

    @field_validator("provider_executable_sha256")
    @classmethod
    def _validate_executable_digest(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not _SHA256_RE.fullmatch(value):
            raise ValueError("provider_executable_sha256 must be 64 lowercase hex characters")
        return value


class ManagedLaunchAdmitRequest(BaseModel):
    protocol_version: ProtocolVersion
    delivery_id: str
    message: str = Field(min_length=1)
    message_sha256: str
    sender_id: str = Field(pattern=r"^[a-f0-9]{8}$")
    orchestration_type: Literal["assign", "handoff"]
    context: "ManagedLaunchAdmissionContext"

    @field_validator("delivery_id")
    @classmethod
    def _validate_delivery_id(cls, value: str) -> str:
        return _uuid_text(value, "delivery_id")

    @field_validator("message_sha256")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("message_sha256 must be 64 lowercase hex characters")
        return value


class ManagedLaunchAdmissionContext(BaseModel):
    boot_id: str
    project: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    task_sha256: str
    plan_sha256: str
    dossier_sha256: str
    lease_sha256: str
    command_packet_sha256: str
    source_chain_sha256: str

    @field_validator("boot_id")
    @classmethod
    def _validate_boot_id(cls, value: str) -> str:
        return _uuid_text(value, "boot_id")

    @field_validator(
        "task_sha256",
        "plan_sha256",
        "dossier_sha256",
        "lease_sha256",
        "command_packet_sha256",
        "source_chain_sha256",
    )
    @classmethod
    def _validate_context_digest(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("admission context digests must be 64 lowercase hex characters")
        return value


class ManagedLaunchObservationRequest(BaseModel):
    protocol_version: ProtocolVersion
    observation_id: str
    kind: Literal["preflight", "negative", "cancelled"]
    terminal_id: str = Field(pattern=r"^[a-f0-9]{8}$")
    generation: str
    provider: str = Field(min_length=1)
    agent_profile: str = Field(min_length=1)
    model: Optional[str] = None
    effort: Optional[str] = None
    preflight_class: Optional[str] = None
    evidence_digest: str
    detail: Optional[str] = None

    @field_validator("observation_id")
    @classmethod
    def _validate_observation_id(cls, value: str) -> str:
        return _uuid_text(value, "observation_id")

    @field_validator("generation")
    @classmethod
    def _validate_generation(cls, value: str) -> str:
        return _uuid_text(value, "generation")

    @field_validator("evidence_digest")
    @classmethod
    def _validate_evidence_digest(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("evidence_digest must be 64 lowercase hex characters")
        return value


class ManagedLaunchCleanupRequest(BaseModel):
    protocol_version: ProtocolVersion
    cleanup_id: str
    terminal_id: str = Field(pattern=r"^[a-f0-9]{8}$")
    generation: str

    @field_validator("cleanup_id", "generation")
    @classmethod
    def _validate_uuid_fields(cls, value: str, info) -> str:
        return _uuid_text(value, info.field_name)


class ManagedLaunchRouteAttestRequest(BaseModel):
    protocol_version: ProtocolVersion
    # Widened alongside the v2 reserve request: the route receipt names
    # the same canonical provider a reservation does, so the two surfaces
    # accepting different sets would make a lawful launch unattestable.
    provider: Literal["codex", "kimi_cli", "claude_code"]
    agent_profile: str = Field(min_length=1)
    working_directory: str = Field(min_length=1)
    trusted_project_root: Optional[str] = None
    expected_model: str = Field(min_length=1)
    expected_effort: str = Field(min_length=1)

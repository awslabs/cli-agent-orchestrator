"""Pydantic wire models for managed-launch protocol v2.

v2 is a distinct protocol vintage: the generation-private zero-task
launch/bind/admit seam with a journaled ``native_bound`` reference
required before any task bytes.  v1 reservations never gain v2
semantics, and v2 rows live in an isolated store.
"""

from __future__ import annotations

import re
import uuid as _uuid_module
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cli_agent_orchestrator.models.managed_launch import ManagedLaunchAdmissionContext

PROTOCOL_VERSION_V2 = "cao-managed-launch-v2"
ProtocolVersionV2 = Literal["cao-managed-launch-v2"]

_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_CALLER_RE = re.compile(r"^[a-f0-9]{8}$")


def _uuid_text(value: str) -> str:
    parsed = _uuid_module.UUID(value)
    if str(parsed) != value:
        raise ValueError("must be a canonical lowercase UUID")
    return value


class ManagedLaunchV2ReserveRequest(BaseModel):
    protocol_version: ProtocolVersionV2
    reservation_id: str
    session_name: str
    provider: Literal["codex", "kimi_cli"]
    agent_profile: str
    caller_id: str
    working_directory: str
    trusted_project_root: Optional[str] = None
    expected_model: str
    expected_effort: str
    provider_executable: str
    provider_executable_sha256: str
    obligation_generation: str
    task_id: Optional[str] = None
    run_id: str
    project: Optional[str] = None
    delivery_id: str
    launch_nonce: str
    #: The explicit launch-level execution mode, or ``None`` when the
    #: caller states none.  ``None`` is *unspecified*, never "either":
    #: it defers to the worker-class default, which keeps a caller that
    #: names neither on the historical ACP branch.  A value outside the
    #: closed enum is rejected here rather than coerced, so a malformed
    #: mode can never reach a store or a receipt.
    execution_mode: Optional[Literal["native_tui", "acp"]] = None
    #: The worker class that supplies the default when no explicit mode
    #: is given.  Native is the default only for the long-lived,
    #: human-observable classes; absent a class the default stays ACP.
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

    @field_validator("project")
    @classmethod
    def _project_non_empty(cls, value: Optional[str]) -> Optional[str]:
        # The immutable project identity: when supplied it is persisted
        # verbatim and binds every heartbeat/bridge fact for the
        # generation; it may never silently fall back to another field.
        if value is not None and not value:
            raise ValueError("project must be non-empty when supplied")
        return value

    @field_validator("reservation_id")
    @classmethod
    def _reservation_uuid(cls, value: str) -> str:
        return _uuid_text(value)

    @field_validator("delivery_id")
    @classmethod
    def _delivery_uuid(cls, value: str) -> str:
        return _uuid_text(value)

    @field_validator("caller_id")
    @classmethod
    def _caller_hex(cls, value: str) -> str:
        if not _CALLER_RE.fullmatch(value):
            raise ValueError("caller_id must be 8 lowercase hex characters")
        return value

    @field_validator("provider_executable_sha256")
    @classmethod
    def _digest_hex(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("must be 64 lowercase hex characters")
        return value

    @field_validator("agent_profile", "expected_model", "expected_effort", "run_id")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value:
            raise ValueError("must be non-empty")
        return value

    @field_validator("obligation_generation")
    @classmethod
    def _obligation_non_empty(cls, value: str) -> str:
        if not value:
            raise ValueError("obligation_generation must be non-empty")
        return value

    @field_validator("launch_nonce")
    @classmethod
    def _nonce_strong(cls, value: str) -> str:
        # The nonce is stored only as a digest; a minimum length keeps the
        # digest meaningful.  The raw value never reaches any store.
        if len(value) < 32:
            raise ValueError("launch_nonce must be at least 32 characters")
        return value


class ManagedLaunchV2BindRequest(BaseModel):
    protocol_version: ProtocolVersionV2
    terminal_id: str
    generation: str
    attempt_id: str
    #: The mode the caller believes this generation is bound to, when it
    #: chooses to restate it.  ``None`` means "not restated" and is
    #: accepted — silence is not a change — but a *present* value that
    #: disagrees with the reserved mode is refused.  This is the seam
    #: that stops a caller which thinks it is binding ACP from binding a
    #: native generation (or the reverse); the mode of a live generation
    #: is immutable, and changing it means a new generation.
    execution_mode: Optional[Literal["native_tui", "acp"]] = None

    @field_validator("terminal_id")
    @classmethod
    def _terminal_hex(cls, value: str) -> str:
        if not _CALLER_RE.fullmatch(value):
            raise ValueError("terminal_id must be 8 lowercase hex characters")
        return value

    @field_validator("generation", "attempt_id")
    @classmethod
    def _uuid_fields(cls, value: str) -> str:
        return _uuid_text(value)


class ManagedLaunchV2AdmitRequest(BaseModel):
    protocol_version: ProtocolVersionV2
    delivery_id: str
    message: str
    message_sha256: str
    sender_id: str
    orchestration_type: Literal["assign", "handoff"]
    context: ManagedLaunchAdmissionContext
    native_binding_digest: str

    @field_validator("delivery_id")
    @classmethod
    def _delivery_uuid(cls, value: str) -> str:
        return _uuid_text(value)

    @field_validator("message")
    @classmethod
    def _message_non_empty(cls, value: str) -> str:
        if not value:
            raise ValueError("message must be non-empty")
        return value

    @field_validator("message_sha256", "native_binding_digest")
    @classmethod
    def _hex_digests(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("must be 64 lowercase hex characters")
        return value

    @field_validator("sender_id")
    @classmethod
    def _sender_hex(cls, value: str) -> str:
        if not _CALLER_RE.fullmatch(value):
            raise ValueError("sender_id must be 8 lowercase hex characters")
        return value


class ManagedV2FenceInstallRequest(BaseModel):
    """The conductor's fence-install RPC (W13 seal/fence)."""

    model_config = ConfigDict(populate_by_name=True)

    # Named schema_id because BaseModel reserves the `schema` attribute;
    # the wire field stays `schema` via the alias.
    schema_id: Literal["cao-w13-fence-req-v1"] = Field(alias="schema")
    terminal_id: str
    terminal_generation: str
    obligation_generation: str
    attempt_id: str
    intent_id: str
    report_sha256: str

    @field_validator("terminal_id")
    @classmethod
    def _fence_terminal_hex(cls, value: str) -> str:
        if not _CALLER_RE.fullmatch(value):
            raise ValueError("terminal_id must be 8 lowercase hex characters")
        return value

    @field_validator("intent_id", "attempt_id")
    @classmethod
    def _fence_uuids(cls, value: str) -> str:
        return _uuid_text(value)

    @field_validator("report_sha256")
    @classmethod
    def _fence_digest(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("report_sha256 must be 64 lowercase hex characters")
        return value


class ManagedDestructiveRequest(BaseModel):
    """One single-use conditional destructive intent for the fork endpoint.

    Whether the effect class requires proven containment is derived
    server-side by the fork; the request carries no containment bit.
    """

    intent_id: str
    kind: Literal["terminal-teardown"]
    terminal_id: str
    generation: str
    reservation_id: str
    attempt_id: str
    fencing_token_id: str

    @field_validator("intent_id", "attempt_id", "reservation_id")
    @classmethod
    def _destructive_uuids(cls, value: str) -> str:
        return _uuid_text(value)

    @field_validator("terminal_id")
    @classmethod
    def _destructive_terminal_hex(cls, value: str) -> str:
        if not _CALLER_RE.fullmatch(value):
            raise ValueError("terminal_id must be 8 lowercase hex characters")
        return value

    @field_validator("generation")
    @classmethod
    def _destructive_generation_uuid(cls, value: str) -> str:
        return _uuid_text(value)

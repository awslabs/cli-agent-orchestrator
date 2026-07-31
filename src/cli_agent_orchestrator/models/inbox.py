"""Inbox message models."""

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class OrchestrationType(str, Enum):
    """Orchestration mode for a message delivery."""

    SEND_MESSAGE = "send_message"
    HANDOFF = "handoff"
    ASSIGN = "assign"


class MessageStatus(str, Enum):
    """Message status enumeration."""

    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


class InboxMessage(BaseModel):
    """Inbox message model."""

    id: int = Field(..., description="Message ID")
    sender_id: str = Field(..., description="Sender terminal ID")
    receiver_id: str = Field(..., description="Receiver terminal ID")
    message: str = Field(..., description="Message content")
    status: MessageStatus = Field(..., description="Message status")
    created_at: datetime = Field(..., description="Creation timestamp")
    message_sha256: Optional[str] = Field(
        default=None, description="Digest of the exact queued message bytes"
    )
    sender_generation: Optional[str] = Field(
        default=None, description="Immutable sender generation captured at enqueue"
    )
    expected_receiver_generation: Optional[str] = Field(
        default=None, description="Receiver generation required for provider delivery"
    )
    expected_provider_session_id: Optional[str] = Field(
        default=None, description="Provider session required for provider delivery"
    )
    expected_execution_mode: Optional[str] = Field(
        default=None, description="Execution mode required for provider delivery"
    )
    expected_provider: Optional[str] = Field(
        default=None, description="Provider required for provider delivery"
    )
    callback_recovery_key: Optional[str] = Field(
        default=None, description="Dedicated refusal/callback recovery operation key"
    )
    callback_completion_key: Optional[str] = Field(
        default=None,
        description="Server-authenticated callback producer correlation",
    )

    @property
    def is_identity_bound(self) -> bool:
        """Whether this row belongs to the narrow managed-message protocol."""
        return self.callback_recovery_key is not None or self.callback_completion_key is not None


class CallbackRecoveryRequest(BaseModel):
    """One refusal-bound attempt to recover an already-authored callback."""

    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(min_length=1, max_length=96)
    project: str = Field(min_length=1, max_length=200)
    task_id: str = Field(min_length=1, max_length=300)
    run_id: str = Field(min_length=1, max_length=300)
    source_terminal_id: str = Field(min_length=1)
    source_generation: str = Field(min_length=1)
    # Lifecycle-v2 is deliberately narrower than the generic provider
    # surface.  A stale client must fail Pydantic validation before this request
    # reaches the operation store, inbox, or any provider bridge.
    expected_provider: Literal["codex", "kimi_cli"]
    expected_provider_session_id: str = Field(min_length=1)
    expected_execution_mode: Literal["acp"]
    supervisor_id: str = Field(min_length=1)
    supervisor_session: str = Field(min_length=1)
    supervisor_generation: str = Field(min_length=1)
    supervisor_pane_id: str = Field(min_length=1)
    refusal_control_id: str = Field(min_length=1, max_length=160)
    refusal_occurrence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    refusal_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    callback_occurrence_id: str = Field(min_length=1, max_length=400)
    callback_status: Literal["done", "blocked", "failed"]
    callback_summary: str = Field(min_length=1, max_length=700)
    callback_message_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_path: str = Field(min_length=1, max_length=2000)
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_head: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    publishing_lease_state: Literal["held", "absent"]
    publishing_lease_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_path: str = Field(min_length=1, max_length=2000)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    finalization_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CallbackRecoveryCompletionRequest(BaseModel):
    """Exact callback row that completes a previously submitted recovery."""

    model_config = ConfigDict(extra="forbid")

    callback_message_id: int = Field(gt=0)
    callback_message_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    callback_created_at: str = Field(min_length=1)
    finalization_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CallbackRecoveryCallbackRequest(BaseModel):
    """Worker-produced callback authenticated by the one-shot recovery secret."""

    model_config = ConfigDict(extra="forbid")

    callback_token: str = Field(min_length=32, max_length=256)
    sender_id: str = Field(min_length=1)
    receiver_id: str = Field(min_length=1)
    callback_occurrence_id: str = Field(min_length=1, max_length=400)
    message: str = Field(min_length=1, max_length=900)


class CallbackRecoveryResolutionRequest(BaseModel):
    """Governed manual disposition for a provider-effect ambiguity."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["proven-zero-provider-effect"]
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    detail: str = Field(min_length=1, max_length=500)


class CallbackRecoveryDispositionRequest(BaseModel):
    """ADMIN-only terminal disposition for an undeliverable callback."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["provider-effect-proven-callback-undeliverable"]
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    detail: str = Field(min_length=1, max_length=500)

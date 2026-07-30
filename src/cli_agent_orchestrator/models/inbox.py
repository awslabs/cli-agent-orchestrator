"""Inbox message models."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


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
    operation_id: Optional[str] = Field(
        default=None, description="Caller idempotency key for an identity-bound message"
    )
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

    @property
    def is_identity_bound(self) -> bool:
        """Whether this row belongs to the narrow managed-message protocol."""
        return self.operation_id is not None


class BoundInboxMessageRequest(BaseModel):
    """One idempotent message for an exact managed ACP generation."""

    operation_id: str = Field(min_length=1, max_length=96)
    sender_id: str = Field(min_length=1)
    sender_generation: str = Field(min_length=1)
    message: str = Field(min_length=1, max_length=2000)
    message_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_receiver_generation: str = Field(min_length=1)
    expected_provider_session_id: str = Field(min_length=1)
    expected_execution_mode: str = Field(pattern=r"^acp$")

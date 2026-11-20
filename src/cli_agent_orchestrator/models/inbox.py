"""Inbox message models."""

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


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


class DeliveryOutcome(str, Enum):
    """What one delivery attempt did with one message (reply-delivery-failure D3)."""

    DELIVERED = "delivered"  # the paste reached the terminal
    FAILED = "failed"  # nothing was typed
    UNCERTAIN = "uncertain"  # some or all of it may have been typed
    RETRYING = "retrying"  # nothing was typed; back to PENDING for another attempt


class InboxMessage(BaseModel):
    """Inbox message model."""

    id: int = Field(..., description="Message ID")
    sender_id: str = Field(..., description="Sender terminal ID")
    receiver_id: str = Field(..., description="Receiver terminal ID")
    message: str = Field(..., description="Message content")
    status: MessageStatus = Field(..., description="Message status")
    created_at: datetime = Field(..., description="Creation timestamp (UTC)")
    delivery_attempts: int = Field(default=0, description="Failed delivery attempts so far")
    last_error: Optional[str] = Field(default=None, description="Error of the last failed attempt")
    is_system_notice: bool = Field(
        default=False, description="True for a notice CAO queued itself, not a peer's message"
    )

    @field_validator("created_at")
    @classmethod
    def _created_at_is_utc(cls, v: datetime) -> datetime:
        return v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v

"""Inbox message models."""

from enum import Enum
from datetime import datetime
from pydantic import BaseModel, Field


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

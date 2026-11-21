"""Delegation edge models: which caller is owed a callback by which worker."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel


class EdgeState(str, Enum):
    OPEN = "open"
    REPLIED = "replied"
    AUTO_FORWARDED = "auto_forwarded"
    WORKER_ENDED = "worker_ended"


class EdgeSource(str, Enum):
    ASSIGN = "assign"
    SEND_MESSAGE = "send_message"


class EdgeReason(str, Enum):
    """Why an auto-forward would happen for this edge."""

    NO_CALLBACK = "no_callback"


class DelegationEdge(BaseModel):
    id: int
    caller_id: str
    worker_id: str
    source: EdgeSource
    state: EdgeState
    reason: EdgeReason
    message_id: Optional[int] = None
    # Inbox id of a reply from the worker that is stored but not yet delivered
    # (reply-delivery-failure D1). The edge stays OPEN while this is set.
    reply_message_id: Optional[int] = None
    created_at: datetime
    delivered_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None

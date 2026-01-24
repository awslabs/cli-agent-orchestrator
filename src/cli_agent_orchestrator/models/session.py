from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class SessionStatus(str, Enum):
    """Session status enumeration."""

    ACTIVE = "active"
    DETACHED = "detached"
    TERMINATED = "terminated"


class Session(BaseModel):
    """Session domain model."""

    model_config = ConfigDict(use_enum_values=True)

    id: str = Field(..., description="Unique session identifier")
    name: str = Field(..., description="Human-readable session name")
    status: SessionStatus = Field(..., description="Current session status")
    parent_session: Optional[str] = Field(None, description="Parent supervisor session ID for worker sessions")

from enum import Enum
from pydantic import BaseModel, Field, ConfigDict


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

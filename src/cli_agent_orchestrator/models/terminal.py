from enum import Enum
from typing import Optional, Annotated
from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict, StringConstraints
from cli_agent_orchestrator.models.provider import ProviderType

# Terminal ID validation (8 character hex string)
TerminalId = Annotated[str, StringConstraints(pattern=r'^[a-f0-9]{8}$')]

# TODO: remove WAITING_PERMISSION
class TerminalStatus(str, Enum):
    """Terminal status enumeration with provider-aware states."""
    IDLE = "idle"
    PROCESSING = "processing"
    COMPLETED = "completed"
    WAITING_PERMISSION = "waiting_permission"
    WAITING_USER_ANSWER = "waiting_user_answer"
    ERROR = "error"


class Terminal(BaseModel):
    """Terminal model - represents a tmux window."""
    model_config = ConfigDict(use_enum_values=True)
    
    id: str = Field(..., description="Unique terminal identifier")
    name: str = Field(..., description="Terminal/window name")
    provider: ProviderType = Field(..., description="CLI tool provider")
    session_name: str = Field(..., description="Session name")
    agent_profile: Optional[str] = Field(None, description="Agent profile")
    status: Optional[TerminalStatus] = Field(None, description="Current terminal status (live only)")
    last_active: Optional[datetime] = Field(None, description="Last active timestamp")

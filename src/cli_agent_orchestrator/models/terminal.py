from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, ConfigDict
from cli_agent_orchestrator.models.provider import ProviderType


class TerminalStatus(str, Enum):
    """Terminal status enumeration with provider-aware states."""
    IDLE = "idle"
    PROCESSING = "processing"
    COMPLETED = "completed"
    WAITING_PERMISSION = "waiting_permission"
    ERROR = "error"


class Terminal(BaseModel):
    """Terminal domain model - represents a tmux window."""
    model_config = ConfigDict(use_enum_values=True)
    
    id: str = Field(..., description="Unique terminal identifier (also used as tmux window index)")
    name: Optional[str] = Field(None, description="Human-readable terminal name (used as tmux window name)")
    session_id: str = Field(..., description="Parent session identifier")
    provider: Optional[ProviderType] = Field(None, description="CLI tool provider")
    status: TerminalStatus = Field(..., description="Current terminal status")

"""Flow model."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from cli_agent_orchestrator.constants import DEFAULT_PROVIDER


class Flow(BaseModel):
    """Flow model - represents a scheduled agent session."""

    name: str = Field(..., description="Unique flow identifier")
    file_path: str = Field(..., description="Path to flow definition file")
    schedule: str = Field(..., description="Cron expression")
    agent_profile: str = Field(..., description="Agent profile to use")
    provider: str = Field(default=DEFAULT_PROVIDER, description="Provider to use")
    script: str = Field("", description="Path to poll script (optional)")
    last_run: Optional[datetime] = Field(None, description="Last execution time")
    next_run: Optional[datetime] = Field(None, description="Next scheduled execution time")
    enabled: bool = Field(True, description="Whether flow is enabled")

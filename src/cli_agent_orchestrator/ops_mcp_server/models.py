"""Models for the CAO operations MCP server."""

from typing import Optional

from pydantic import BaseModel, Field

from cli_agent_orchestrator.services.install_service import InstallResult


class LaunchResult(BaseModel):
    """Result for session launch operations."""

    success: bool = Field(description="Whether the launch operation succeeded")
    message: str = Field(description="A message describing the launch result")
    session_name: Optional[str] = Field(
        default=None,
        description="The created or selected session name",
    )
    terminal_id: Optional[str] = Field(
        default=None,
        description="The created terminal ID",
    )


__all__ = ["InstallResult", "LaunchResult"]

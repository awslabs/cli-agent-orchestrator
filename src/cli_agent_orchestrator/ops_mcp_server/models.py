"""Models for the CAO operations MCP server."""

from typing import Any, Dict, List, Optional

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


class ProfileListResult(BaseModel):
    """Result for profile discovery operations."""

    success: bool = Field(description="Whether the discovery operation succeeded")
    message: Optional[str] = Field(
        default=None,
        description="Error message when success is False",
    )
    profiles: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Available profiles with name, description, and source",
    )


class SessionListResult(BaseModel):
    """Result for session list operations."""

    success: bool = Field(description="Whether the list operation succeeded")
    message: Optional[str] = Field(
        default=None,
        description="Error message when success is False",
    )
    sessions: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Active CAO sessions with terminal counts and statuses",
    )


class SendMessageResult(BaseModel):
    """Result for inbox message delivery operations."""

    success: bool = Field(description="Whether the message was queued successfully")
    message: str = Field(description="A message describing the delivery result")
    terminal_id: str = Field(description="The target terminal ID")


__all__ = [
    "InstallResult",
    "LaunchResult",
    "ProfileListResult",
    "SendMessageResult",
    "SessionListResult",
]

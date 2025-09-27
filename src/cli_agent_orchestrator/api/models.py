"""
API Request/Response Models

These are API-specific models for request/response validation,
separate from domain models in cli_agent_orchestrator/models/ folder.
"""
from typing import List, Optional
from pydantic import BaseModel, Field
from cli_agent_orchestrator.models.provider import ProviderType


# Session API Models
class CreateSessionRequest(BaseModel):
    """Request model for creating a session."""
    name: str = Field(..., description="Session name", min_length=1)
    terminals: List["CreateTerminalRequest"] = Field(..., description="Terminals to create in session (required)", min_length=1)


class SessionResponse(BaseModel):
    """Response model for session operations."""
    id: str = Field(..., description="Session identifier")
    name: str = Field(..., description="Session name")
    status: str = Field(..., description="Session status")


# Terminal API Models
class CreateTerminalRequest(BaseModel):
    """Request model for creating a terminal."""
    provider: Optional[ProviderType] = Field(None, description="CLI tool provider")
    name: Optional[str] = Field(None, description="Terminal name")
    agent_profile: Optional[str] = Field(None, description="Agent profile for Q CLI provider")


class TerminalResponse(BaseModel):
    """Response model for terminal operations."""
    id: str = Field(..., description="Terminal identifier")
    name: Optional[str] = Field(None, description="Terminal name")
    provider: Optional[str] = Field(None, description="Provider type")
    status: str = Field(..., description="Terminal status")


class TerminalInputRequest(BaseModel):
    """Request model for sending input to terminal."""
    message: str = Field(..., description="Message to send to terminal")


class TerminalOutputResponse(BaseModel):
    """Response model for terminal output."""
    output: str = Field(..., description="Terminal output content")
    mode: str = Field(..., description="Output mode (full or last)")


class TerminalScriptResponse(BaseModel):
    """Response model for terminal script."""
    script: str = Field(..., description="Full terminal script content")
    terminal_id: str = Field(..., description="Terminal identifier")


# Bulk Operations
class BulkSessionResponse(BaseModel):
    """Response model for bulk session creation."""
    id: str = Field(..., description="Session identifier")
    terminals: List[TerminalResponse] = Field(..., description="Created terminals")


# Pydantic v2: model_rebuild() resolves forward references for self-referencing models
# Required when using string annotations like "CreateTerminalRequest" in CreateSessionRequest
CreateSessionRequest.model_rebuild()

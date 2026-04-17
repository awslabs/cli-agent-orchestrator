"""Memory model for CAO memory system (Phase 1 — file-based, no SQLite)."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class MemoryScope(str, Enum):
    """Valid memory scopes."""

    GLOBAL = "global"
    PROJECT = "project"
    SESSION = "session"
    AGENT = "agent"


class MemoryType(str, Enum):
    """Valid memory types."""

    USER = "user"
    FEEDBACK = "feedback"
    PROJECT = "project"
    REFERENCE = "reference"


class Memory(BaseModel):
    """Memory model — represents a stored memory entry."""

    id: str = Field(..., description="Unique memory identifier")
    key: str = Field(..., description="Slug identifier, e.g. 'prefer-pytest'")
    memory_type: str = Field(..., description="One of: user, feedback, project, reference")
    scope: str = Field(..., description="One of: global, project, session, agent")
    scope_id: Optional[str] = Field(None, description="Auto-resolved scope identifier")
    file_path: str = Field(..., description="Path to wiki topic file")
    tags: str = Field(default="", description="Comma-separated tags")
    source_provider: Optional[str] = Field(None, description="Provider that created this memory")
    source_terminal_id: Optional[str] = Field(
        None, description="Terminal ID that created this memory"
    )
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")
    content: str = Field(default="", description="Memory content loaded from wiki file")

    @field_validator("scope")
    @classmethod
    def validate_scope(cls, v: str) -> str:
        valid = {s.value for s in MemoryScope}
        if v not in valid:
            raise ValueError(f"scope must be one of {valid}, got '{v}'")
        return v

    @field_validator("memory_type")
    @classmethod
    def validate_memory_type(cls, v: str) -> str:
        valid = {t.value for t in MemoryType}
        if v not in valid:
            raise ValueError(f"memory_type must be one of {valid}, got '{v}'")
        return v

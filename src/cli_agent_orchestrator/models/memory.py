"""Memory model for CAO memory system (Phase 1 — file-based, no SQLite)."""

from datetime import datetime
from enum import Enum
from typing import Annotated, Optional

from pydantic import (
    AfterValidator,
    BaseModel,
    Field,
    StringConstraints,
    field_validator,
)


def _reject_control_chars(value: str) -> str:
    """Reject control characters to prevent newline/NULL bypasses of `$`-anchored regexes."""
    if any(ch in value for ch in ("\n", "\r", "\x00")):
        raise ValueError("must not contain control characters")
    return value


# Mirrors the CLI validation rule (cli/commands/memory.py) — reject-first, so
# malformed keys 422 at the API layer instead of being silently sanitized.
MemoryKey = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9-]{1,60}$"),
    AfterValidator(_reject_control_chars),
]


def _reject_all_dots(value: str) -> str:
    """Reject '.', '..', '...' so traversal tokens 422 at the boundary instead
    of relying on the get_wiki_path guard deeper down."""
    _reject_control_chars(value)
    if set(value) == {"."}:
        raise ValueError("scope_id must not consist solely of dots")
    return value


# Same charset as _PROJECT_ID_OVERRIDE_PATTERN in memory_service.py; also valid
# for session/agent scope_ids (which pass _sanitize_scope_id).
MemoryScopeId = Annotated[
    str,
    StringConstraints(pattern=r"^[a-zA-Z0-9._-]{1,128}$"),
    AfterValidator(_reject_all_dots),
]


class MemoryScope(str, Enum):
    """Valid memory scopes."""

    GLOBAL = "global"
    PROJECT = "project"
    SESSION = "session"
    AGENT = "agent"
    FEDERATED = "federated"


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
    scope: str = Field(..., description="One of: global, project, session, agent, federated")
    scope_id: Optional[str] = Field(None, description="Auto-resolved scope identifier")
    file_path: str = Field(..., description="Path to wiki topic file")
    tags: str = Field(default="", description="Comma-separated tags")
    source_provider: Optional[str] = Field(None, description="Provider that created this memory")
    source_terminal_id: Optional[str] = Field(
        None, description="Terminal ID that created this memory"
    )
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")
    access_count: int = Field(
        default=0, description="Recall hit count; feeds the usage scoring factor"
    )
    last_compiled_at: Optional[datetime] = Field(
        default=None,
        description="UTC time of last successful LLM compile; None if never",
    )
    related_keys: Optional[str] = Field(
        default=None,
        description=(
            "Comma-separated cross-reference keys. " "None=never computed; ''=computed empty."
        ),
    )
    is_related: bool = Field(
        default=False,
        exclude=True,
        description=(
            "Internal render-only label: True iff this Memory was loaded via "
            "one-level cross-reference traversal. Never persisted or serialised."
        ),
    )
    content: str = Field(default="", description="Memory content loaded from wiki file")
    action: Optional[str] = Field(
        default=None,
        exclude=True,
        description="Set by store() to 'created' or 'updated'; not persisted on disk.",
    )
    timestamp_clamped: bool = Field(
        default=False,
        exclude=True,
        description=(
            "Set by store() when a supplied occurred_at was out-of-order or "
            "in the future and the section heading was clamped to now() "
            "(#345 D5). Never persisted or serialised."
        ),
    )

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

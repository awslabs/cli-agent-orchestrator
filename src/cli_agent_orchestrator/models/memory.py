"""Memory model for CAO memory system (Phase 1 — file-based, no SQLite)."""

import os
import re
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Dict, Optional, Tuple

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


def _reject_keys_that_sanitize_to_empty(value: str) -> str:
    """Reject a key with no slug characters at all (``"..."``, ``"###"``, ``"///"``).

    Lenient does not mean "accept anything". These pass the length and
    control-character checks, then reach ``MemoryService._sanitize_key``, which
    strips everything outside ``[a-z0-9-]`` and raises ``ValueError`` on the empty
    result. Neither internal handler catches that, so the client saw an HTTP 500
    where the strict ``MemoryKey`` had returned a clean 422 — and gateway vs
    in-process then disagreed about the same key, which is exactly the divergence
    ``LenientMemoryKey`` exists to remove.

    Rejecting at the boundary keeps the 422 and keeps the two paths identical:
    a key that cannot survive sanitization is not a key either path accepts.
    Mirrors ``_sanitize_key``'s charset deliberately; if that ever widens, widen
    this with it.
    """
    _reject_control_chars(value)
    if not re.sub(r"[^a-z0-9\-]", "", os.path.basename(value).lower()).strip("-"):
        raise ValueError(
            "key must contain at least one alphanumeric character "
            "(it sanitizes to an empty slug)"
        )
    return value


# The /internal/memory/* routes NORMALIZE where the operator routes REJECT.
#
# Those routes serve the MCP memory tools via memory_gateway, and the tools have
# always silently sanitized through MemoryService._sanitize_key: memory_store(
# key="Prefer Pytest") stores "preferpytest" when running in-process. Validating
# the wire key as the strict MemoryKey made the identical call 422 as soon as
# CAO_MEMORY_API_URL was set, so turning the gateway on broke keys that work
# without it. Control characters are still rejected here (via the validator
# below) so a newline or NUL cannot bypass a `$`-anchored regex further down.
LenientMemoryKey = Annotated[
    str,
    StringConstraints(min_length=1, max_length=256),
    AfterValidator(_reject_keys_that_sanitize_to_empty),
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
    lifecycle_status: Optional[str] = Field(
        default=None,
        exclude=True,
        description="Transient lifecycle label (e.g. superseded); never persisted or serialized by model_dump.",
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
    source_kind: str = Field(
        default="native", description="Storage backend that supplied this memory"
    )
    source_path: Optional[str] = Field(
        default=None,
        description="Backend-relative source path; never an absolute vault path",
    )
    indexed_at: Optional[datetime] = Field(
        default=None, description="When a vault projection was last reconciled"
    )
    index_freshness: Optional[str] = Field(
        default=None, description="Vault stat comparison result: fresh or stale"
    )
    content_truncated: bool = Field(
        default=False, description="True when recall body budget truncated content"
    )
    token_estimate: Optional[int] = Field(
        default=None, description="Approximate recall token count"
    )
    redacted_patterns: list[str] = Field(
        default_factory=list,
        exclude=True,
        description=(
            "Names of secret patterns whose matches were replaced by "
            "[REDACTED:<pattern>] before this content left the vault read "
            "boundary (#644). Pattern names only; never the matched bytes. "
            "An in-process receipt from the vault reader to the injection "
            "renderer, which turns it into the injection_redaction counters; "
            "no recall surface consumes it. Never persisted or serialised."
        ),
    )
    action: Optional[str] = Field(
        default=None,
        exclude=True,
        description=(
            "Set by store() to 'created' or 'updated'; not persisted on disk. "
            "Never persisted or serialised; memory_gateway re-attaches it "
            "out-of-band after model_validate."
        ),
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


# ---------------------------------------------------------------------------
# The recalled-memory wire shape (#968, #1034)
# ---------------------------------------------------------------------------
#
# Two client surfaces return recalled memories: the MCP ``memory_recall`` tool
# and ``POST /internal/memory/recall``. Both used to decide the shape at their
# own call site -- the route dumped the model, the tool hand-built a dict from a
# key allowlist -- so a field added to ``Memory`` widened one surface and not
# the other. #1034 is that bug: ``redacted_patterns`` appeared on the route and
# was invisible on the tool, and only the route's #968 pin caught it.
#
# Both surfaces now call into this section, so there is one place to edit. The
# serialized field list itself stays where it belongs, on the model: adding a
# field to ``Memory`` without ``exclude=True`` puts it on the wire, and
# ``test/models/test_memory_wire_shape.py`` is the pin that makes that
# deliberate.

#: Keys the MCP ``memory_recall`` tool projects out of the full wire dict, in
#: the order the tool emits them (#968 ``MCP_PIN``). A strict subset of
#: :func:`recall_wire_dict`'s keys -- projecting a key the model does not
#: serialize is a ``KeyError``, not a silently missing field.
MCP_RECALL_KEYS: Tuple[str, ...] = (
    "key",
    "content",
    "memory_type",
    "scope",
    "tags",
    "file_path",
    "source_kind",
    "source_path",
    "indexed_at",
    "index_freshness",
    "content_truncated",
    "updated_at",
)

#: Second-precision Zulu, the stamp format the MCP tool has always emitted.
#: Narrower than pydantic's JSON encoding (which keeps microseconds and renders
#: a non-UTC offset as an offset), so the tool cannot adopt the shared dict's
#: stamps without changing its wire shape.
_MCP_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _mcp_stamp(value: Optional[datetime]) -> Optional[str]:
    """Render a timestamp the way the MCP recall tool renders it."""
    return value.strftime(_MCP_STAMP_FORMAT) if value else None


def recall_wire_dict(memory: "Memory") -> Dict[str, Any]:
    """Serialize one recalled memory for a client.

    Every field ``Memory`` declares without ``exclude=True``, JSON-encoded by
    pydantic, plus the transient ``lifecycle_status`` label when it is set (it
    is ``exclude=True`` because it is not persisted, but recall does report it).

    This is the payload ``POST /internal/memory/recall`` returns verbatim and
    the dict :func:`mcp_recall_wire_dict` projects.
    """
    wire: Dict[str, Any] = memory.model_dump(mode="json")
    if memory.lifecycle_status is not None:
        wire["lifecycle_status"] = memory.lifecycle_status
    return wire


def mcp_recall_wire_dict(memory: "Memory") -> Dict[str, Any]:
    """Project :func:`recall_wire_dict` down to the MCP ``memory_recall`` shape.

    ``MCP_RECALL_KEYS`` selects the fields; the values come from the shared
    dict, except for three the MCP contract has always rendered differently and
    #968 pins that way:

    * ``file_path`` carries the vault-relative source path for a vault-backed
      memory, never the absolute vault path the model stores;
    * ``indexed_at`` and ``updated_at`` are second-precision Zulu.

    The overrides are applied after the projection, so they land in their
    projected key positions and cannot add a key.
    """
    wire = recall_wire_dict(memory)
    projected: Dict[str, Any] = {}
    # lifecycle_status leads on this surface and trails on the route's; both
    # appear only when set.
    if memory.lifecycle_status is not None:
        projected["lifecycle_status"] = memory.lifecycle_status
    projected.update({key: wire[key] for key in MCP_RECALL_KEYS})
    if memory.source_kind == "vault":
        projected["file_path"] = memory.source_path
    projected["indexed_at"] = _mcp_stamp(memory.indexed_at)
    projected["updated_at"] = _mcp_stamp(memory.updated_at)
    return projected

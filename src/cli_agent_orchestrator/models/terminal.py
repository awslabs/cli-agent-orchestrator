from datetime import datetime
from enum import Enum
from typing import Annotated, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from cli_agent_orchestrator.models.provider import ProviderType

# Terminal ID validation (8 character hex string)
TerminalId = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{8}$")]


class TerminalStatus(str, Enum):
    """Terminal status enumeration with provider-aware states."""

    UNKNOWN = "unknown"
    IDLE = "idle"
    PROCESSING = "processing"
    COMPLETED = "completed"
    WAITING_USER_ANSWER = "waiting_user_answer"
    ERROR = "error"
    #: A live pane whose status no FIFO monitor observes. A native TUI owns
    #: its pane's argv and renders a full-screen interface, so there is no
    #: line-oriented stream to classify and no legacy provider to classify
    #: it. Distinct from UNKNOWN, which means "live pane, state not yet
    #: detected" and so promises a detection that for this terminal is never
    #: coming -- reporting it here is what left the dashboard showing every
    #: native worker as pending forever.
    NOT_FIFO_MONITORED = "not_fifo_monitored"


class TerminalLifecycleState(str, Enum):
    """The observed-lifecycle vocabulary a projected row reports.

    Mirrors the durable lifecycle vocabulary stored on terminal rows (the
    ``lifecycle_state`` column documented in ``clients/database.py``). A row
    whose recorded identity no longer resolves — or could not be observed at
    all — has no provider state to report, so the projection reports its
    lifecycle in ``status`` instead. This boundary must admit those values:
    while it did not, every non-live row failed response validation on
    ``GET /terminals/{id}``, and a legacy ``unknown-liveness`` row read as
    a 404 — absent — instead of as the historical record it is (cond-0173).
    """

    LIVE = "live"
    SUPERSEDED = "superseded"
    DEAD = "dead"
    UNKNOWN_LIVENESS = "unknown-liveness"


class Terminal(BaseModel):
    """Terminal model - represents a tmux window."""

    model_config = ConfigDict(use_enum_values=True)

    id: str = Field(..., description="Unique terminal identifier")
    name: str = Field(..., description="Terminal/window name")
    provider: ProviderType = Field(..., description="CLI tool provider")
    session_name: str = Field(..., description="Session name")
    agent_profile: Optional[str] = Field(None, description="Agent profile")
    caller_id: Optional[str] = Field(
        None, description="Terminal that created this one via handoff/assign (callback target)"
    )
    pane_id: Optional[str] = Field(None, description="Server-recorded immutable tmux pane identity")
    window_id: Optional[str] = Field(
        None, description="Server-recorded immutable tmux window identity"
    )
    allowed_tools: Optional[List[str]] = Field(None, description="Allowed CAO tools")
    shell_command: Optional[str] = Field(
        None, description="Shell process name captured before kiro launch"
    )
    status: Optional[Union[TerminalStatus, TerminalLifecycleState]] = Field(
        None,
        description=(
            "Provider status for a live pane; the observed lifecycle state "
            "for a row whose identity does not resolve"
        ),
    )
    last_active: Optional[datetime] = Field(None, description="Last active timestamp")

    # --- Projection fields ---
    #
    # Additive and all optional, so every existing consumer keeps working
    # against the same response. They exist because this model is the
    # boundary a projected terminal has to cross: without a declared field
    # a projected dict is silently stripped here, which is how a per-terminal
    # reader ends up unable to tell a superseded terminal from a live one
    # even though the service below it knows.
    #
    # ``status`` stays provider-valued for a live pane — machine
    # orchestration polls this route waiting on provider status — and
    # carries the lifecycle only for a row whose identity does not resolve,
    # where there is no provider state to report and "unknown" would read
    # as "alive, not yet detected".
    generation: Optional[str] = Field(None, description="Non-reusable incarnation identifier")
    callback_target_generation: Optional[str] = Field(
        None,
        description="Non-reusable callback-target incarnation identifier",
    )
    protocol_vintage: Optional[str] = Field(None, description="Which store holds this row (v1/v2)")
    lifecycle_state: Optional[str] = Field(
        None, description="Observed liveness: live/superseded/dead/unknown-liveness"
    )
    lifecycle_reason: Optional[str] = Field(
        None, description="Why the lifecycle state was recorded"
    )
    superseded_by_terminal_id: Optional[str] = Field(
        None, description="The terminal that now holds this row's pane"
    )
    superseded_by_generation: Optional[str] = Field(
        None, description="The generation of the superseding terminal"
    )
    server_socket_path: Optional[str] = Field(
        None, description="The tmux server that owns this row's pane and window ids"
    )
    session_id: Optional[str] = Field(None, description="Immutable tmux session identity ($N)")
    pane_pid: Optional[int] = Field(None, description="The pane's primary process id")
    native_session_id: Optional[str] = Field(
        None, description="The provider-native session running in this pane"
    )


class AgentStepResult(BaseModel):
    """Transient result of one agent step (issue #312, C3b). Not persisted.

    ``run_agent_step`` returns this ONLY on success (status COMPLETED); all
    failure modes raise narrow exceptions instead. It lives here in the terminal
    layer (not the workflow module) because it is the generic step substrate's
    return type and is conceptually workflow-independent — keeping it out of
    ``models/workflow.py`` lets ``services/agent_step.py`` avoid importing the
    workflow module (and its jsonschema/yaml deps).
    """

    terminal_id: str
    last_message: str
    status: TerminalStatus

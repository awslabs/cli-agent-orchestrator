from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from cli_agent_orchestrator.models.kiro_engine import KiroEngine
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


class TerminalInputBlockedReason(str, Enum):
    """Machine-readable cause of a ``TerminalInputBlockedError``.

    Lets a catcher like inbox_service's delivery loop decide PENDING-vs-FAILED from the raise
    site's own classification instead of taking a second, possibly stale or unreliable (e.g.
    herdr's CLI-backed ``get_status`` returning UNKNOWN on any error) status read (#896).
    """

    PROVIDER_ERROR = "provider_error"
    WAITING_USER_ANSWER = "waiting_user_answer"


class TerminalInputBlockedError(Exception):
    """Raised when a terminal is alive but blocked on an interactive prompt that needs a human
    (or an explicit answer_user_prompt call) to proceed -- never auto-answered.

    Defined here (not in services/terminal_service.py, its original home) because
    providers/claude_code.py (and any other provider) needs to raise it from initialize() and
    services/terminal_service.py already imports providers/manager.py, which imports the concrete
    provider modules -- a provider importing back from terminal_service would be a circular
    import. models/terminal.py has no such dependency. terminal_service.py re-exports this name
    unchanged so every existing `from cli_agent_orchestrator.services.terminal_service import
    TerminalInputBlockedError` caller keeps working without edits.

    Two raise sites share this one exception, both in services/terminal_service.py's
    send_input(), and each sets ``reason`` so the catcher can tell them apart:
    1. The terminal's provider process has exited (status ERROR) -- refuse to type into what is
       now a bare shell, since queued input would execute as arbitrary commands. Raised with
       ``reason=TerminalInputBlockedReason.PROVIDER_ERROR``. Permanent: no later sweep can
       deliver into a bare shell, so inbox_service marks the batch FAILED.
    2. An orchestrated message -- any ``OrchestrationType`` (assign, handoff, send_message, and
       any member added later; the set is derived from the enum, see #896) -- would answer a
       prompt still showing WAITING_USER_ANSWER -- refuse, since only a real answer_user_prompt
       call (or the operator themselves) should resolve it. This guard only actually fires for a
       given provider once that provider's own
       ``blocks_orchestrated_input_while_waiting_user_answer`` property opts in (see
       providers/base.py) -- claude_code did not opt in until PR #539's round-2 fix, so before
       that an orchestrated message reaching a claude_code terminal parked on
       WAITING_USER_ANSWER was NOT refused here; it was pasted straight into the live prompt.
       Raised with ``reason=TerminalInputBlockedReason.WAITING_USER_ANSWER``. Transient: the
       dialog will close, so inbox_service keeps the batch PENDING for the next sweep.
    No provider raises this from initialize() today (providers/claude_code.py's init timeout
    raises a bare TimeoutError on purpose); ``reason`` defaults to ``None`` only so a caller
    outside this tree can still construct the exception without classifying it.
    Both sites are "this terminal needs a human, not a teardown" -- see terminal_service.py's own
    _schedule_deferred_init, which catches this exception specifically to leave the terminal
    running instead of tearing it down the way any other exception from initialize() (or from the
    send_input it triggers on the deferred-init path) would.
    """

    def __init__(self, message: str, reason: Optional[TerminalInputBlockedReason] = None) -> None:
        super().__init__(message)
        self.reason = reason


class ProviderExitedInputBlockedError(TerminalInputBlockedError):
    """Input was refused because the provider process is gone, not because it asked a question.

    Raised by ``services/terminal_service.py``'s delivery guards when the
    terminal's status is ERROR or its pane's foreground process is the shell that
    was running before the CLI launched (#1018). Still a
    ``TerminalInputBlockedError`` so every existing handler keeps its answer --
    409 at ``/terminals/{id}/input``, 504 at ``run_step``, FAILED in the inbox --
    but the deferred-init path catches this subclass FIRST and tears the worker
    down: there is no prompt to answer, ``answer_user_prompt`` refuses because
    the status is not WAITING_USER_ANSWER, and a re-send is refused again, so
    "leave it alive for the operator" would keep a dead worker registered and
    tell the supervisor to wait on a prompt that does not exist.
    """


class TerminalLimitError(Exception):
    """Raised when creating a terminal would exceed this node's tracked-terminal cap.

    The cap comes from ``settings_service.get_max_terminals()`` (the
    ``CAO_MAX_TERMINALS`` env var / ``server.max_terminals`` setting; unset =
    unlimited, preserving pre-cap behavior). It exists for the one-agent-per-pod
    Kubernetes topology: worker pods set ``CAO_MAX_TERMINALS=1`` so each pod
    hosts exactly one agent and a second placement is rejected loudly instead
    of silently oversubscribing the pod. Deliberately NOT a ``ValueError``
    subclass — the API layer maps ValueError from terminal creation to 404
    ("session not found"), whereas a capacity rejection must surface as its own
    status (429) so a scheduler/supervisor can retry on a different node.
    """


class Terminal(BaseModel):
    """Terminal model - represents a tmux window."""

    model_config = ConfigDict(use_enum_values=True)

    id: str = Field(..., description="Unique terminal identifier")
    name: str = Field(..., description="Terminal/window name")
    provider: ProviderType = Field(..., description="CLI tool provider")
    session_name: str = Field(..., description="Session name")
    agent_profile: Optional[str] = Field(None, description="Agent profile")
    model: Optional[str] = Field(None, description="Model requested at launch")
    model_honored: Optional[bool] = Field(
        None, description="Whether the provider applies the requested launch model"
    )
    ephemeral: bool = Field(
        default=False, description="Server-computed ephemeral registry membership"
    )
    caller_id: Optional[str] = Field(
        None, description="Terminal that created this one via handoff/assign (callback target)"
    )
    callback_terminal_id: Optional[str] = Field(
        default=None,
        description=(
            "For a worker created from ANOTHER node (assign/handoff with target_host): "
            "the creating supervisor's terminal id, read back from the "
            "CAO_CALLBACK_TERMINAL_ID the supervisor injected into this terminal's "
            "session environment. caller_id is None for such rows because the "
            "supervisor has no row on this node. None for locally created terminals, "
            "and None again after a cao-server restart (the session-env store is "
            "process-local)."
        ),
    )
    allowed_tools: Optional[List[str]] = Field(None, description="Allowed CAO tools")
    delegate_tools: Optional[List[str]] = Field(
        None,
        description=(
            "Delegation ceiling: the most this terminal may grant to children it "
            "creates via assign/handoff/workflow steps. Resolved from the profile's "
            "delegateTools (or the role default) and capped by the creator's own "
            "ceiling at creation time. None for rows written before the column existed."
        ),
    )
    engine: Optional[KiroEngine] = Field(None, description="Resolved Kiro engine")
    shell_command: Optional[str] = Field(
        None, description="Shell process name captured before kiro launch"
    )
    group: Optional[List[str]] = Field(
        None,
        description=(
            "Ordered, general-to-specific grouping array (e.g. "
            '["tenant_1", "project_5", "folder_12"]). CAO does ordered-prefix '
            "matching only; consumers own what the levels mean. None = this "
            "terminal participates in no group-based discovery (see "
            "list_siblings)."
        ),
    )
    description: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = Field(
        None, description="Free-form, consumer-defined JSON describing what this terminal is doing"
    )
    deferred_init_failure: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "CAO-owned durable deferred-initialization failure metadata. "
            "Separate from consumer metadata so clients cannot overwrite lifecycle truth."
        ),
    )
    memory_manager: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Outcome of the memory_manager sidecar a POST /sessions request asked for "
            "(memory_manager=true): {state: 'scheduled' | 'refused', detail}. 'refused' "
            "means the sidecar's tools exceed this primary's recorded delegation ceiling "
            "and it was not launched; detail carries the refusal text. Response-only, "
            "never persisted; None when no sidecar was requested."
        ),
    )
    initial_delivery: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Durable outcome of the initial message this terminal was created with "
            "(deferred init): state pending/delivered/failed plus kind and message. "
            "None when no initial message was accepted at creation."
        ),
    )
    session_incarnation_id: Optional[str] = Field(
        None,
        description=(
            "Durable CAO session incarnation identifier. Session names may be reused; "
            "this value distinguishes retained rows from a later replacement."
        ),
    )
    status: Optional[TerminalStatus] = Field(
        None, description="Current terminal status (live only)"
    )
    status_generation: Optional[int] = Field(
        default=None,
        description=(
            "Output generation that ``status`` was EARNED at, from "
            "StatusMonitor.status_observation(). A status counts as evidence for a "
            "particular send only when this is strictly greater than the dispatch "
            "boundary that POST /terminals/{id}/input returned for that send: a status "
            "VALUE alone cannot carry recency, so a ready status cached before a send is "
            "otherwise indistinguishable from one earned after it (#735, #407). None for "
            "event-inbox backends (herdr), which derive status on demand and never advance "
            "the generation, and None on a server that does not report it."
        ),
    )
    output_generation: Optional[int] = Field(
        default=None,
        description=(
            "The terminal's CURRENT output generation (StatusMonitor.output_generation()), "
            "reported alongside ``status_generation`` so a client can tell three cases "
            "apart against the dispatch boundary of a send: equal to it, nothing has "
            "landed since the send; above it while ``status_generation`` is still at or "
            "below it, output landed but the status was never re-earned, which is either "
            "a turn so fast its PROCESSING was never sampled or a repaint of the previous "
            "frame and the server cannot tell which, so a client must bound its wait rather "
            "than refuse until timeout (#735 review, P2-2); below it, the server's counter "
            "was reset (restart) and the boundary no longer shares its timeline. None for "
            "event-inbox backends (herdr) and on a server that does not report it."
        ),
    )
    last_active: Optional[datetime] = Field(None, description="Last active timestamp (UTC)")

    @field_validator("last_active")
    @classmethod
    def _last_active_is_utc(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is not None and v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v


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

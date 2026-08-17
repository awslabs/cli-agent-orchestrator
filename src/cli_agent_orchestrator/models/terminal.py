from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

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

    Three distinct raise sites share this one exception:
    1. services/terminal_service.py's send_input(): the terminal's provider process has exited
       (status ERROR) -- refuse to type into what is now a bare shell, since queued input would
       execute as arbitrary commands.
    2. services/terminal_service.py's send_input(), separately: an orchestrated (assign/handoff)
       message would answer a prompt still showing WAITING_USER_ANSWER -- refuse, since only a
       real answer_user_prompt call (or the operator themselves) should resolve it. This guard
       only actually fires for a given provider once that provider's own
       ``blocks_orchestrated_input_while_waiting_user_answer`` property opts in (see
       providers/base.py) -- claude_code did not opt in until PR #539's round-2 fix, so before
       that an orchestrated message reaching a claude_code terminal parked on
       WAITING_USER_ANSWER was NOT refused here; it was pasted straight into the live prompt.
    3. providers/*.py's own initialize(): the CLI reached a real, alive, interactive state that
       isn't {IDLE, COMPLETED} but IS a recognized "something is asking a question" signal
       (WAITING_USER_ANSWER) -- or, for the outermost fallback, genuinely never resolved within
       the init timeout despite the terminal staying alive and producing output the whole time.
    All three are "this terminal needs a human, not a teardown" -- see terminal_service.py's own
    _schedule_deferred_init, which catches this exception specifically to leave the terminal
    running instead of tearing it down the way any other exception from initialize() (or from the
    send_input it triggers on the deferred-init path) would.
    """


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
    allowed_tools: Optional[List[str]] = Field(None, description="Allowed CAO tools")
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
    metadata: Optional[Dict[str, Any]] = Field(
        None, description="Free-form, consumer-defined JSON describing what this terminal is doing"
    )
    status: Optional[TerminalStatus] = Field(
        None, description="Current terminal status (live only)"
    )
    last_active: Optional[datetime] = Field(None, description="Last active timestamp")


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

"""Single FastAPI entry point for all HTTP routes."""

import asyncio
import fcntl
import json
import logging
import os
import pty
import re
import signal
import struct
import subprocess
import termios
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Dict, List, Optional, cast

from fastapi import (
    BackgroundTasks,
    Body,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field, field_validator

from cli_agent_orchestrator.backends import TerminalNotFoundError
from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend
from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.clients.database import (
    create_inbox_message,
    get_inbox_messages,
    get_terminal_metadata,
    init_db,
)
from cli_agent_orchestrator.constants import (
    ALLOWED_HOSTS,
    API_BASE_URL,
    CAO_HOME_DIR,
    CORS_ORIGINS,
    DEFAULT_PROVIDER,
    INBOX_POLLING_INTERVAL,
    INBOX_RECONCILE_INTERVAL,
    SERVER_HOST,
    SERVER_PORT,
    SERVER_VERSION,
    TERMINALS_RUN_STEP_ROUTE,
    TRUSTED_FORWARDER_IPS,
    WS_ALLOWED_CLIENTS,
    add_local_cors_origins,
)
from cli_agent_orchestrator.ext_apps import mount_widget_static
from cli_agent_orchestrator.models.flow import Flow
from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType
from cli_agent_orchestrator.models.memory import (
    MemoryKey,
    MemoryScope,
    MemoryScopeId,
    MemoryType,
)
from cli_agent_orchestrator.models.terminal import Terminal, TerminalId
from cli_agent_orchestrator.plugins import PluginRegistry
from cli_agent_orchestrator.security.auth import (
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
    SCOPES_SUPPORTED,
    get_authorization_servers,
    get_current_scopes,
    is_auth_enabled,
    require_any_scope,
)
from cli_agent_orchestrator.services import (
    flow_service,
    session_service,
    terminal_service,
)
from cli_agent_orchestrator.services.agent_step import StepExecutionError, run_agent_step
from cli_agent_orchestrator.services.cleanup_service import (
    cleanup_expired_memories,
    cleanup_old_data,
)
from cli_agent_orchestrator.services.event_bus import bus
from cli_agent_orchestrator.services.event_log_service import RING_CAPACITY
from cli_agent_orchestrator.services.event_primitives import KINDS as EVENT_KINDS
from cli_agent_orchestrator.services.herdr_inbox_registry import set_herdr_inbox_service
from cli_agent_orchestrator.services.herdr_inbox_service import HerdrInboxService
from cli_agent_orchestrator.services.inbox_service import inbox_service
from cli_agent_orchestrator.services.install_service import InstallResult, install_agent
from cli_agent_orchestrator.services.log_writer import log_writer
from cli_agent_orchestrator.services.status_monitor import status_monitor
from cli_agent_orchestrator.services.terminal_service import OutputMode, TerminalInputBlockedError
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile, resolve_provider
from cli_agent_orchestrator.utils.logging import setup_logging
from cli_agent_orchestrator.utils.skills import (
    SkillNameError,
    load_skill_content,
    validate_skill_name,
)
from cli_agent_orchestrator.utils.terminal import validate_tmux_name

logger = logging.getLogger(__name__)

TMUX_KEY_PATTERN = re.compile(
    r"^(?:Up|Down|Left|Right|Enter|Tab|Escape|Space|[A-Za-z0-9]|[CMS]-[A-Za-z0-9])$"
)


async def flow_daemon():
    """Background task to check and execute flows."""
    logger.info("Flow daemon started")
    while True:
        try:
            flows = flow_service.get_flows_to_run()
            for flow in flows:
                try:
                    executed = await flow_service.execute_flow(flow.name)
                    if executed:
                        logger.info(f"Flow '{flow.name}' executed successfully")
                    else:
                        logger.info(f"Flow '{flow.name}' skipped (execute=false)")
                except Exception as e:
                    logger.error(f"Flow '{flow.name}' failed: {e}")
        except Exception as e:
            logger.error(f"Flow daemon error: {e}")

        await asyncio.sleep(60)


async def opencode_inbox_delivery_daemon(registry: PluginRegistry) -> None:
    """Background task to wake OpenCode inbox delivery for pending messages."""
    logger.info("OpenCode inbox delivery poller started")
    while True:
        await asyncio.sleep(INBOX_POLLING_INTERVAL)
        try:
            await asyncio.to_thread(inbox_service.poll_opencode_pending_messages, registry)
        except Exception:
            logger.exception("OpenCode inbox delivery poller error")


async def inbox_reconciliation_daemon(registry: PluginRegistry) -> None:
    """Background task that recovers inbox messages the fast paths missed.

    Safety net for issue #131: the immediate (on POST) delivery path and the
    event-driven StatusMonitor pipeline can both miss a message when the receiver
    is already idle, leaving it PENDING forever. This sweep runs on a slower
    interval and re-attempts delivery for anything left pending past the grace
    window.
    """
    logger.info("Inbox reconciliation daemon started")
    while True:
        await asyncio.sleep(INBOX_RECONCILE_INTERVAL)
        try:
            await asyncio.to_thread(inbox_service.reconcile_orphaned_messages, registry)
        except Exception:
            logger.exception("Inbox reconciliation daemon error")


# Response Models
class TerminalOutputResponse(BaseModel):
    output: str
    mode: str


class RunStepRequest(BaseModel):
    """Request body for the combined step-execution endpoint (N0, #312)."""

    provider: str = Field(description="Provider type (e.g. 'kiro_cli', 'claude_code')")
    agent: str = Field(description="Agent profile name")
    prompt: str = Field(description="Prompt to send (caller applies any prompt shaping first)")
    session_name: Optional[str] = Field(
        default=None,
        description="Existing session to create the terminal in; auto-generated if None",
    )
    reuse_terminal_id: Optional[str] = Field(
        default=None, description="Reuse an existing terminal (skips create + teardown)"
    )
    teardown: bool = Field(
        default=True,
        description="Delete the created terminal after the step (ignored when reusing)",
    )
    timeout: float = Field(default=600.0, description="Max seconds to wait for completion", gt=0)
    working_directory: Optional[str] = Field(
        default=None, description="Working directory for a freshly created terminal"
    )
    caller_id: Optional[str] = Field(
        default=None,
        description="Supervisor terminal ID to record for structural callback routing (#284)",
    )
    allowed_tools: Optional[list[str]] = Field(
        default=None,
        description="Resolved allowed-tools list for a freshly created terminal (handoff inheritance)",
    )


class RunStepResponse(BaseModel):
    """Response wrapping an ``AgentStepResult`` from ``run_agent_step``."""

    terminal_id: str
    last_message: str
    status: str


class WorkflowValidateRequest(BaseModel):
    """Request body for ``POST /workflows/validate`` (Bolt 2, N2)."""

    path: str = Field(description="Filesystem path to the workflow spec YAML file")


class StepOutputRequest(BaseModel):
    """Request body for the structured-return endpoint (Bolt 2, N4, C5).

    For the synthetic-key MVP there is no run record, so the step's
    ``output_schema`` arrives WITH the request (F2) rather than being re-resolved
    from a run aggregate.
    """

    output: Dict = Field(description="The worker-emitted JSON output for the step")
    output_schema: Optional[Dict] = Field(
        default=None, description="The step's JSON-Schema (Draft 2020-12); None = no validation"
    )


class WorkflowRunRequest(BaseModel):
    """Request body for ``POST /workflows/runs`` (Bolt 3, N5, C5)."""

    name_or_path: str = Field(description="Workflow name (indexed) or path to a spec YAML file")
    inputs: Dict = Field(
        default_factory=dict, description="Run inputs validated against spec.inputs"
    )
    run_id: Optional[str] = Field(
        default=None,
        description="Optional run id (matches WORKFLOW_NAME_RE); auto-generated if omitted",
    )


class StepOutputResponse(BaseModel):
    """Response for the structured-return endpoint — mirrors the stored record."""

    validated: bool
    errors: List[str]
    state: str


class SkillContentResponse(BaseModel):
    """Response model for a skill content lookup."""

    name: str
    content: str


class WorkingDirectoryResponse(BaseModel):
    """Response model for terminal working directory."""

    working_directory: Optional[str] = Field(
        description="Current working directory of the terminal, or None if unavailable"
    )


class InstallAgentProfileRequest(BaseModel):
    """Request body for installing an agent profile.

    ``env_vars`` travels in the JSON body rather than as a query parameter so
    that any secrets callers inject are not written to HTTP access logs.
    """

    source: str
    provider: str = DEFAULT_PROVIDER
    env_vars: Optional[Dict[str, str]] = None


class MemorySummary(BaseModel):
    """Memory list entry. Excludes file_path (absolute server filesystem path)."""

    key: str
    scope: str
    scope_id: Optional[str] = Field(
        description="Native for session/agent, derived from storage path for project, None for global"
    )
    memory_type: str
    tags: str
    created_at: datetime
    updated_at: datetime


class MemoryDetail(MemorySummary):
    """Full memory view — adds the latest wiki section content."""

    content: str


class CreateFlowRequest(BaseModel):
    """Request model for creating a flow."""

    name: str
    schedule: str
    agent_profile: str
    provider: str = "kiro_cli"
    prompt_template: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        """Prevent path traversal — flow name becomes a filename."""
        if "/" in v or "\\" in v or ".." in v:
            raise ValueError("Flow name must not contain '/', '\\', or '..'")
        return v


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    logger.info("Starting CLI Agent Orchestrator server...")
    setup_logging()
    init_db()
    registry = PluginRegistry()
    await registry.load()
    app.state.plugin_registry = registry

    # Run cleanup in background
    asyncio.create_task(asyncio.to_thread(cleanup_old_data))
    asyncio.create_task(cleanup_expired_memories())

    # Start flow daemon as background task
    daemon_task = asyncio.create_task(flow_daemon())

    # Register event loop with event bus for thread-safe publishing
    loop = asyncio.get_running_loop()
    bus.set_loop(loop)

    # Start event bus consumers as background tasks
    status_monitor_task = asyncio.create_task(status_monitor.run())
    log_writer_task = asyncio.create_task(log_writer.run())
    inbox_service_task = asyncio.create_task(inbox_service.run(registry))
    logger.info("Event bus consumers started (StatusMonitor, LogWriter, InboxService)")

    # Start temporary OpenCode inbox poller. GH #115 tracks replacing this
    # provider-specific wakeup path with a unified delivery engine.
    opencode_inbox_task = asyncio.create_task(opencode_inbox_delivery_daemon(registry))

    # Start provider-agnostic reconciliation sweep for orphaned PENDING messages
    # the immediate and event-driven status paths missed (issue #131).
    inbox_reconcile_task = asyncio.create_task(inbox_reconciliation_daemon(registry))

    # Herdr delivers inbox via its own socket events; the tmux backend uses the
    # FIFO -> EventBus pipeline (StatusMonitor / LogWriter / InboxService) started
    # above. Start the herdr inbox service only when the herdr backend is active
    # (additive; no-op for tmux). See #271.
    herdr_inbox_task: Optional[asyncio.Task] = None
    backend = get_backend()
    if isinstance(backend, HerdrBackend):

        def deliver_inbox(terminal_id: str) -> None:
            inbox_service.deliver_pending(terminal_id, registry=registry)

        svc = HerdrInboxService(
            herdr_session=backend.herdr_session,
            delivery_callback=deliver_inbox,
        )
        set_herdr_inbox_service(svc)
        herdr_inbox_task = asyncio.create_task(svc.start())
        logger.info("Herdr inbox service started")

    yield

    # Stop herdr inbox service on shutdown
    if herdr_inbox_task is not None:
        herdr_inbox_task.cancel()
        try:
            await herdr_inbox_task
        except asyncio.CancelledError:
            pass
        set_herdr_inbox_service(None)
        logger.info("Herdr inbox service stopped")

    # Cancel consumer tasks on shutdown
    status_monitor_task.cancel()
    log_writer_task.cancel()
    inbox_service_task.cancel()
    # Cancel daemon on shutdown
    daemon_task.cancel()

    try:
        await asyncio.gather(
            status_monitor_task,
            log_writer_task,
            inbox_service_task,
            daemon_task,
            return_exceptions=True,
        )
    except asyncio.CancelledError:
        pass

    # Cancel OpenCode inbox poller on shutdown
    opencode_inbox_task.cancel()
    try:
        await opencode_inbox_task
    except asyncio.CancelledError:
        pass

    # Cancel inbox reconciliation sweep on shutdown
    inbox_reconcile_task.cancel()
    try:
        await inbox_reconcile_task
    except asyncio.CancelledError:
        pass

    await registry.teardown()
    logger.info("Shutting down CLI Agent Orchestrator server...")


def get_plugin_registry(request: Request) -> PluginRegistry:
    """Return the plugin registry stored on the FastAPI application state."""

    return cast(PluginRegistry, request.app.state.plugin_registry)


# Values that indicate ``TERM`` is effectively unusable and must be overridden
# rather than inherited by the tmux attach subprocess. ``dumb`` is the common
# fallback that containers and devcontainers ship with when no real terminal
# is attached. Empty string and missing key behave the same way.
_UNUSABLE_TERM_VALUES = frozenset({"", "dumb"})
_DEFAULT_PTY_TERM = "xterm-256color"


def _build_pty_env() -> Dict[str, str]:
    """Build the env handed to the tmux PTY attach subprocess.

    Copies the parent process environment so cao-server's normal config
    (PATH, HOME, AWS_*, etc.) reaches tmux, and forces ``TERM`` to a usable
    value when the inherited one would break terminal rendering. Explicit
    non-dumb ``TERM`` values from the operator are preserved verbatim. See
    issue #150.
    """
    env = os.environ.copy()
    if env.get("TERM", "") in _UNUSABLE_TERM_VALUES:
        env["TERM"] = _DEFAULT_PTY_TERM
    return env


app = FastAPI(
    title="CLI Agent Orchestrator",
    description="Simplified CLI Agent Orchestrator API",
    version=SERVER_VERSION,
    lifespan=lifespan,
)

# Security: DNS Rebinding Protection
# Validate Host header to prevent DNS rebinding attacks (CVE mitigation)
# Only allow requests with localhost Host headers
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=ALLOWED_HOSTS,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource_metadata():
    """RFC 9728 Protected Resource Metadata.

    Advertises the resource audience, the authorization server(s), the supported
    scopes (``cao:read``/``cao:write``/``cao:admin``), and the supported bearer
    methods so OAuth clients can discover how to obtain access. Returns HTTP 404
    when auth is disabled (default-off), so the localhost-only posture is
    byte-for-byte unchanged.
    """
    if not is_auth_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="auth disabled")

    audience = (
        os.getenv("CAO_AUTH_AUDIENCE", "").strip()
        or os.getenv("AUTH0_AUDIENCE", "").strip()
        or API_BASE_URL
    )
    return {
        "resource": audience,
        "authorization_servers": get_authorization_servers(),
        "scopes_supported": SCOPES_SUPPORTED,
        "bearer_methods_supported": ["header"],
    }


@app.get("/health")
async def health_check():
    import shutil

    from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend

    def _probe(binary: str) -> str:
        return "ok" if shutil.which(binary) else "unavailable"

    backend = get_backend()
    backend_name = "herdr" if isinstance(backend, HerdrBackend) else "tmux"

    return {
        "status": "ok",
        "service": "cli-agent-orchestrator",
        "terminal_backend": backend_name,
        "components": {
            "cao": "ok",
            "herdr": _probe("herdr"),
            "claude": _probe("claude"),
        },
    }


def _mcp_apps_enabled() -> bool:
    """Whether the MCP Apps HTTP surface (event stream + widget) is enabled.

    Mirrors the ``CAO_MCP_APPS_ENABLED`` gate used by the ``mcp_apps`` plugin,
    ``app_tools``, ``sep2133`` and the ``event_log_publisher`` observer so the
    whole surface is consistently default-off.
    """

    return os.getenv("CAO_MCP_APPS_ENABLED", "false").lower() in ("1", "true", "yes")


def _require_mcp_apps_enabled() -> None:
    """Raise 404 when the MCP Apps surface is disabled (default-off).

    The ``/events`` SSE stream and ``/events/history`` replay expose fleet
    metadata (terminal ids, session names, routing/launch/kill topology), so
    they must not be reachable unless an operator opts in via
    ``CAO_MCP_APPS_ENABLED`` — matching the default-off posture of the rest of
    the surface (tools, resources, widget, capability advertisement).
    """

    if not _mcp_apps_enabled():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="MCP Apps surface disabled"
        )


@app.get("/events")
async def events_stream(
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)),
):
    """Stream live, normalized fleet events to the iframe as Server-Sent Events.

    Events come from the in-process ``SseBus`` (fed by the ``EventLogPublisher``
    plugin). The bus is drop-on-slow with a bounded per-subscriber queue, so one
    stalled iframe never applies back-pressure to the orchestration core; gaps are
    backfilled by the client via ``/events/history`` / ``cao_fetch_history``.

    Default-off: returns 404 unless ``CAO_MCP_APPS_ENABLED`` is set, so the fleet
    event timeline (terminal ids, session names, routing/topology metadata) is
    never exposed when the surface is disabled. When auth is enabled, any of
    ``cao:read`` / ``cao:write`` / ``cao:admin`` is required (read is the floor).
    """
    _require_mcp_apps_enabled()

    from fastapi.responses import StreamingResponse

    from cli_agent_orchestrator.services.sse_bus import get_bus

    async def event_generator():
        async for event in get_bus().subscribe():
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/events/history")
async def events_history(
    limit: int = Query(default=RING_CAPACITY, ge=0, le=RING_CAPACITY),
    since: Optional[str] = None,
    kinds: Optional[str] = None,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """Replay recent fleet events from the ring buffer (JSON, newest-last).

    Events are already normalized to the six-primitive vocabulary at append time.
    ``kinds`` is an optional comma-separated filter; ``since`` is an ISO-8601
    timestamp lower bound (exclusive).

    Input hardening: ``limit`` is clamped to ``[0, RING_CAPACITY]`` (the buffer is
    bounded anyway, so a larger value can never return more) and each ``kinds``
    token is validated against the closed event vocabulary — an unknown kind is
    rejected with 400 rather than silently matching nothing.

    Default-off: returns 404 unless ``CAO_MCP_APPS_ENABLED`` is set; when auth is
    enabled, any of ``cao:read`` / ``cao:write`` / ``cao:admin`` is required.
    """
    _require_mcp_apps_enabled()

    from cli_agent_orchestrator.services.event_log_service import get_event_log

    kinds_filter = [k.strip() for k in kinds.split(",") if k.strip()] if kinds else None
    if kinds_filter:
        invalid = [k for k in kinds_filter if k not in EVENT_KINDS]
        if invalid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Invalid event kind(s): {', '.join(invalid)}. "
                    f"Valid kinds: {', '.join(EVENT_KINDS)}"
                ),
            )
    events = get_event_log().history(limit=limit, since=since, kinds=kinds_filter)
    return {"events": events}


# Topology widget static bundle at /widgets/topology/ — the vanilla SSE-driven
# view consumed alongside the /events stream above. The mount is default-off
# (no-op unless CAO_MCP_APPS_ENABLED is set) and idempotent, so re-importing this
# module under dev/reload is safe.
mount_widget_static(app)


@app.get("/agents/profiles")
async def list_agent_profiles_endpoint() -> List[Dict]:
    """List all available agent profiles from all configured directories."""
    try:
        from cli_agent_orchestrator.utils.agent_profiles import list_agent_profiles

        return list_agent_profiles()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list agent profiles: {str(e)}",
        )


@app.get("/agents/profiles/{name}")
async def get_agent_profile_endpoint(name: str) -> Dict:
    """Return the full parsed content of a named agent profile."""
    try:
        profile = load_agent_profile(name)
        return profile.model_dump(exclude_none=True)
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@app.post("/agents/profiles/install")
async def install_agent_profile_endpoint(
    request: InstallAgentProfileRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> InstallResult:
    """Install an agent profile for a target provider.

    HTTP (and transitively ``cao-ops-mcp``, which calls this endpoint) is an
    untrusted surface. ``install_agent()`` only accepts bare profile names or
    https:// URLs; local filesystem paths are handled by the CLI entry point
    alone. A remote caller therefore cannot coerce the server into reading
    arbitrary ``.md`` files from disk.
    """
    result = install_agent(
        source=request.source,
        provider=request.provider,
        env_vars=request.env_vars,
    )
    if not result.success:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=result.message)

    return result


@app.get("/agents/providers")
async def list_providers_endpoint() -> List[Dict]:
    """List available providers with installation status."""
    import shutil

    provider_binaries = {
        "kiro_cli": "kiro-cli",
        "claude_code": "claude",
        "codex": "codex",
        "hermes": "hermes",
        "kimi_cli": "kimi",
        "copilot_cli": "copilot",
        "opencode_cli": "opencode",
        "cursor_cli": "agent",
        "antigravity_cli": "agy",
    }
    result = []
    for provider, binary in provider_binaries.items():
        installed = shutil.which(binary) is not None
        result.append({"name": provider, "binary": binary, "installed": installed})
    return result


@app.get("/settings/agent-dirs")
async def get_agent_dirs_endpoint() -> Dict:
    """Get configured agent directories per provider."""
    from cli_agent_orchestrator.services.settings_service import (
        get_agent_dirs,
        get_extra_agent_dirs,
    )

    return {"agent_dirs": get_agent_dirs(), "extra_dirs": get_extra_agent_dirs()}


class AgentDirsUpdate(BaseModel):
    agent_dirs: Optional[Dict[str, str]] = None
    extra_dirs: Optional[List[str]] = None


@app.get("/settings/memory")
async def get_memory_settings_endpoint() -> Dict:
    """Return whether the memory subsystem is enabled (for UI feature discovery)."""
    from cli_agent_orchestrator.services.settings_service import is_memory_enabled

    return {"enabled": is_memory_enabled()}


@app.post("/settings/agent-dirs")
async def set_agent_dirs_endpoint(
    body: AgentDirsUpdate,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """Update agent directories per provider."""
    from cli_agent_orchestrator.services.settings_service import (
        get_extra_agent_dirs,
        set_agent_dirs,
        set_extra_agent_dirs,
    )

    result_dirs = {}
    result_extra = []
    if body.agent_dirs:
        result_dirs = set_agent_dirs(body.agent_dirs)
    if body.extra_dirs is not None:
        result_extra = set_extra_agent_dirs(body.extra_dirs)
    return {
        "agent_dirs": result_dirs or {},
        "extra_dirs": result_extra or get_extra_agent_dirs(),
    }


@app.get("/settings/skill-dirs")
async def get_skill_dirs_endpoint() -> Dict:
    """Get the global skill store path and user-added extra skill directories."""
    from cli_agent_orchestrator.constants import SKILLS_DIR
    from cli_agent_orchestrator.services.settings_service import get_extra_skill_dirs

    return {"skills_dir": str(SKILLS_DIR), "extra_dirs": get_extra_skill_dirs()}


class SkillDirsUpdate(BaseModel):
    extra_dirs: Optional[List[str]] = None


@app.post("/settings/skill-dirs")
async def set_skill_dirs_endpoint(
    body: SkillDirsUpdate,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """Update user-added extra skill directories."""
    from cli_agent_orchestrator.constants import SKILLS_DIR
    from cli_agent_orchestrator.services.settings_service import (
        get_extra_skill_dirs,
        set_extra_skill_dirs,
    )

    result_extra: List[str] = []
    if body.extra_dirs is not None:
        result_extra = set_extra_skill_dirs(body.extra_dirs)
    return {
        "skills_dir": str(SKILLS_DIR),
        "extra_dirs": result_extra or get_extra_skill_dirs(),
    }


@app.get("/skills/{name}", response_model=SkillContentResponse)
async def get_skill_content(name: str) -> SkillContentResponse:
    """Return the full Markdown body for an installed skill."""
    try:
        skill_name = validate_skill_name(name)
        content = load_skill_content(skill_name)
        return SkillContentResponse(name=name, content=content)
    except SkillNameError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid skill name: {name}",
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to load skill: {str(e)}",
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill not found: {name}",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to load skill: {str(e)}",
        )


@app.post("/sessions", response_model=Terminal, status_code=status.HTTP_201_CREATED)
async def create_session(
    request: Request,
    background_tasks: BackgroundTasks,
    agent_profile: str,
    provider: Optional[str] = None,
    session_name: Optional[str] = None,
    working_directory: Optional[str] = None,
    allowed_tools: Optional[str] = None,
    memory_manager: Optional[str] = None,
    env_vars: Optional[Dict[str, str]] = Body(default=None, embed=True),
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Terminal:
    """Create a new session with exactly one terminal.

    When ``memory_manager`` is truthy, a sidecar ``memory_manager`` terminal is
    spawned asynchronously in the same tmux session — provider initialization
    can take 15-30s and would otherwise block the HTTP response past the
    client's request timeout. The worker's first message may arrive before
    the curator reaches IDLE; ``get_curated_memory_context`` falls back to
    Phase 1 in that window.

    ``env_vars`` (request body, optional) is the operator-forwarded env map
    from ``cao launch --env``. It travels in the JSON body — not the query
    string — so values potentially containing secrets do not land in
    cao-server's HTTP access log. See issue #248.
    """
    try:
        if session_name is not None:
            # terminal_service.create_terminal prepends SESSION_PREFIX
            # ("cao-") if missing, so an API caller's 64-char valid name
            # would become 68 chars and fail downstream validation. Check
            # the *effective* prefixed value here so the rejection happens
            # at the boundary with a clear message.
            from cli_agent_orchestrator.constants import SESSION_PREFIX

            effective = (
                session_name
                if session_name.startswith(SESSION_PREFIX)
                else f"{SESSION_PREFIX}{session_name}"
            )
            validate_tmux_name(effective, "session_name")
        # Parse comma-separated allowed_tools string into list
        allowed_tools_list = allowed_tools.split(",") if allowed_tools else None

        result = await session_service.create_session(
            provider=provider,
            agent_profile=agent_profile,
            session_name=session_name,
            working_directory=working_directory,
            allowed_tools=allowed_tools_list,
            registry=get_plugin_registry(request),
            env_vars=env_vars,
        )

        if memory_manager and str(memory_manager).lower() in ("true", "1", "yes"):
            registry = get_plugin_registry(request)
            sidecar_provider = provider or DEFAULT_PROVIDER
            sidecar_session = result.session_name

            async def _spawn_sidecar() -> None:
                try:
                    from cli_agent_orchestrator.services import terminal_service

                    await terminal_service.create_terminal(
                        provider=sidecar_provider,
                        agent_profile="memory_manager",
                        session_name=sidecar_session,
                        working_directory=working_directory,
                        registry=registry,
                    )
                except Exception as e:
                    logger.warning(f"Failed to spawn memory_manager sidecar: {e}")

            background_tasks.add_task(_spawn_sidecar)

        return result

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create session: {str(e)}",
        )


@app.get("/sessions")
async def list_sessions() -> List[Dict]:
    try:
        return session_service.list_sessions()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list sessions: {str(e)}",
        )


@app.get("/sessions/{session_name}")
async def get_session(session_name: str) -> Dict:
    # Validate before entering the try block so a malformed name surfaces
    # as 400 instead of being mapped to 404 by the not-found handler below.
    try:
        validate_tmux_name(session_name, "session_name")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    try:
        return session_service.get_session(session_name)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get session: {str(e)}",
        )


@app.delete("/sessions/{session_name}")
async def delete_session(
    request: Request,
    session_name: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_ADMIN)),
) -> Dict:
    try:
        validate_tmux_name(session_name, "session_name")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    try:
        result = session_service.delete_session(session_name, registry=get_plugin_registry(request))
        return {"success": True, **result}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete session: {str(e)}",
        )


@app.post(
    "/sessions/{session_name}/terminals",
    response_model=Terminal,
    status_code=status.HTTP_201_CREATED,
)
async def create_terminal_in_session(
    request: Request,
    session_name: str,
    agent_profile: str,
    provider: Optional[str] = None,
    working_directory: Optional[str] = None,
    allowed_tools: Optional[str] = None,
    caller_id: Optional[TerminalId] = None,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Terminal:
    """Create additional terminal in existing session."""
    try:
        validate_tmux_name(session_name, "session_name")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    try:
        if provider is None:
            resolved_provider = resolve_provider(agent_profile, fallback_provider="kiro_cli")
        else:
            resolved_provider = provider

        # Parse comma-separated allowed_tools string into list
        allowed_tools_list = allowed_tools.split(",") if allowed_tools else None

        result = await terminal_service.create_terminal(
            provider=resolved_provider,
            agent_profile=agent_profile,
            session_name=session_name,
            new_session=False,
            working_directory=working_directory,
            allowed_tools=allowed_tools_list,
            registry=get_plugin_registry(request),
            caller_id=caller_id,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create terminal: {str(e)}",
        )


@app.get("/sessions/{session_name}/terminals")
async def list_terminals_in_session(session_name: str) -> List[Dict]:
    """List all terminals in a session."""
    try:
        validate_tmux_name(session_name, "session_name")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    try:
        from cli_agent_orchestrator.clients.database import list_terminals_by_session

        return list_terminals_by_session(session_name)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list terminals: {str(e)}",
        )


@app.get("/terminals/{terminal_id}", response_model=Terminal)
async def get_terminal(terminal_id: TerminalId) -> Terminal:
    try:
        terminal = terminal_service.get_terminal(terminal_id)
        return Terminal(**terminal)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except TerminalNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get terminal: {str(e)}",
        )


@app.get("/terminals/{terminal_id}/memory-context")
async def get_terminal_memory_context(terminal_id: TerminalId):
    """Return the CAO memory context block for a terminal as plain text.

    Used by the Kiro AgentSpawn hook to inject memory into agent context.
    Returns empty 200 if no memories exist for this terminal.
    """
    from fastapi.responses import PlainTextResponse

    try:
        from cli_agent_orchestrator.services.memory_service import MemoryService

        svc = MemoryService()
        context = svc.get_memory_context_for_terminal(terminal_id)
        return PlainTextResponse(content=context)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get memory context: {str(e)}",
        )


@app.get("/terminals/{terminal_id}/working-directory", response_model=WorkingDirectoryResponse)
async def get_terminal_working_directory(terminal_id: TerminalId) -> WorkingDirectoryResponse:
    """Get the current working directory of a terminal's pane."""
    try:
        working_directory = terminal_service.get_working_directory(terminal_id)
        return WorkingDirectoryResponse(working_directory=working_directory)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get working directory: {str(e)}",
        )


@app.post("/terminals/{terminal_id}/input")
async def send_terminal_input(
    request: Request,
    terminal_id: TerminalId,
    message: str,
    sender_id: Optional[str] = None,
    orchestration_type: Optional[OrchestrationType] = None,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    try:
        success = terminal_service.send_input(
            terminal_id,
            message,
            registry=get_plugin_registry(request),
            sender_id=sender_id,
            orchestration_type=orchestration_type,
        )
        return {"success": success}
    except TerminalInputBlockedError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to send input: {str(e)}",
        )


@app.post("/terminals/{terminal_id}/key")
async def send_terminal_key(
    terminal_id: TerminalId,
    key: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """Send a tmux special key to a terminal."""
    if not TMUX_KEY_PATTERN.fullmatch(key):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Invalid tmux key name. Allowed keys are arrow keys, Enter, Tab, "
                "Escape, Space, single alphanumeric keys, and C-/M-/S- modifier combos."
            ),
        )

    try:
        success = terminal_service.send_special_key(terminal_id, key)
        return {"success": success}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to send key: {str(e)}",
        )


@app.get("/terminals/{terminal_id}/output", response_model=TerminalOutputResponse)
async def get_terminal_output(
    terminal_id: TerminalId, mode: OutputMode = OutputMode.FULL
) -> TerminalOutputResponse:
    try:
        output = terminal_service.get_output(terminal_id, mode)
        return TerminalOutputResponse(output=output, mode=mode)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get output: {str(e)}",
        )


@app.post("/terminals/{terminal_id}/exit")
async def exit_terminal(
    terminal_id: TerminalId,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """Send provider-specific exit command to terminal."""
    try:
        terminal_service.exit_terminal_cli(terminal_id)
        return {"success": True}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to exit terminal: {str(e)}",
        )


@app.post(
    TERMINALS_RUN_STEP_ROUTE,
    response_model=RunStepResponse,
    summary="Run one agent step (shared substrate)",
    description=(
        "Failure contract: a non-2xx body is a structured object "
        "`{message, kind, terminal_id}`. **`kind` is authoritative** — "
        '`kind="error"` means the worker CRASHED (terminal reached ERROR), '
        '`kind="timeout"` means it RAN LONG. The HTTP status mirrors `kind` '
        "(502 = crashed, 504 = ran long) for transport-layer consumers, but a "
        "caller MUST branch on `kind`, not the status code. `terminal_id` names "
        "the live terminal (read it as a field; never regex-scrape `message`)."
    ),
)
async def run_step(
    request: Request,
    body: RunStepRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> RunStepResponse:
    """Run a single agent step through the shared substrate (N0, #312).

    This is the combined server-side endpoint both step callers converge on:
    the handoff MCP client reaches it over HTTP (one call replacing its former
    six granular round-trips); the run engine (N5) calls ``run_agent_step``
    directly in-process and never round-trips here (single-seam rule, ADR-3).

    The handler body is ``await run_agent_step(...)``. Domain failures from the
    substrate are mapped to ``HTTPException`` at this boundary (project Mandated
    boundary-map rule).

    Failure contract (the future engine caller depends on this, so it is spelled
    out, not just inferable from the handler):

    - A failed step returns a STRUCTURED detail object
      ``{"message": str, "kind": "timeout"|"error", "terminal_id": str|None}``.
    - ``kind`` is the AUTHORITATIVE discriminator. ``kind="error"`` => the worker
      CRASHED (the terminal reached ``TerminalStatus.ERROR``); ``kind="timeout"``
      => the worker RAN LONG (readiness/completion wait elapsed). The HTTP status
      is derived FROM ``kind`` (``error`` -> 502 Bad Gateway, ``timeout`` -> 504
      Gateway Timeout) as a convenience for transport-layer consumers — a client
      that can read the body MUST branch on ``kind``, not the status code.
    - ``terminal_id`` names the live terminal the step ran on (when known) so a
      caller can report/clean it up without regex-scraping ``message``.
    - A bad terminal reference -> 404; any other failure -> 500 (plain-string
      detail, no ``kind`` — these are not step-execution outcomes).

    The plugin registry is threaded so teardown's ``post_kill_terminal`` hooks
    fire (parity with the DELETE endpoint).
    """
    try:
        result = await run_agent_step(
            provider=body.provider,
            agent=body.agent,
            prompt=body.prompt,
            session_name=body.session_name,
            reuse_terminal_id=body.reuse_terminal_id,
            teardown=body.teardown,
            timeout=body.timeout,
            working_directory=body.working_directory,
            caller_id=body.caller_id,
            allowed_tools=body.allowed_tools,
            registry=get_plugin_registry(request),
        )
        return RunStepResponse(
            terminal_id=result.terminal_id,
            last_message=result.last_message,
            status=(result.status.value if hasattr(result.status, "value") else str(result.status)),
        )
    except StepExecutionError as e:
        # The step did not complete successfully. Distinguish a worker that
        # CRASHED (kind="error" -> 502 Bad Gateway) from one that RAN LONG
        # (kind="timeout" -> 504 Gateway Timeout) so the caller can tell them
        # apart instead of reporting every failure as a timeout. The detail is a
        # structured object carrying terminal_id, so callers read it as a field
        # rather than regex-scraping the message (the future engine reads it too).
        code = status.HTTP_502_BAD_GATEWAY if e.kind == "error" else status.HTTP_504_GATEWAY_TIMEOUT
        raise HTTPException(
            status_code=code,
            detail={"message": str(e), "kind": e.kind, "terminal_id": e.terminal_id},
        )
    except TimeoutError as e:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={"message": str(e), "kind": "timeout", "terminal_id": None},
        )
    except ValueError as e:
        # Unknown terminal / bad input surfaced by the terminal layer.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to run step: {str(e)}",
        )


# =============================================================================
# Workflow authoring + structured-return endpoints (issue #312, Bolt 2)
# =============================================================================
# Single integration seam for the `cao workflow` CLI verbs and the
# `workflow_return` MCP tool (B2-BR-10). Core services raise narrow exceptions;
# this boundary maps them to HTTPException (B2-BR-9): ValueError -> 400,
# FileNotFoundError/KeyError -> 404. The run/cancel/status endpoints are Bolt 3.


@app.post("/workflows/validate")
async def validate_workflow_endpoint(body: WorkflowValidateRequest) -> Dict:
    """Validate a workflow spec without running it (FR-1.3). Returns ValidationResult."""
    from cli_agent_orchestrator.services import workflow_spec_service

    try:
        result = workflow_spec_service.validate_only(body.path)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return result.model_dump()


@app.get("/workflows")
async def list_workflows_endpoint(dir: Optional[str] = Query(default=None)) -> List[Dict]:
    """List indexed workflows, rebuilt from the spec files on disk (FR-2.1)."""
    from cli_agent_orchestrator.services import workflow_spec_service

    try:
        rows = workflow_spec_service.list_workflows(scan_dir=dir)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return [row.model_dump() for row in rows]


@app.get("/workflows/{name}")
async def get_workflow_endpoint(name: str) -> Dict:
    """Return the parsed/validated spec for a workflow name (FR-2.1)."""
    from cli_agent_orchestrator.services import workflow_spec_service

    try:
        spec = workflow_spec_service.get_workflow(name)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown workflow '{name}'"
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return spec.model_dump()


@app.delete("/workflows/{name}")
async def delete_workflow_endpoint(
    name: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_ADMIN)),
) -> Dict:
    """Delete a workflow's spec file and its index row (FR-2.4)."""
    from cli_agent_orchestrator.services import workflow_spec_service

    try:
        workflow_spec_service.delete_workflow(name)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown workflow '{name}'"
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return {"success": True, "name": name}


@app.post(
    "/workflows/runs/{run_id}/steps/{step_id}/output",
    response_model=StepOutputResponse,
)
async def record_step_output_endpoint(
    run_id: str,
    step_id: str,
    body: StepOutputRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> StepOutputResponse:
    """Record a worker's structured output for a step (FR-4.1, C5).

    Validation lives at this seam (ADR-4). A schema-invalid output does NOT 500 —
    it is stored with ``validated=False`` / state ``COMPLETED_UNVALIDATED`` and
    returned as a 200 (the engine acts on the flag in Bolt 3). A malformed
    ``run_id`` / ``step_id`` (failing the name regex) maps to 400.
    """
    from cli_agent_orchestrator.services.step_output_store import record_step_output

    try:
        record = record_step_output(
            run_id=run_id,
            step_id=step_id,
            output=body.output,
            output_schema=body.output_schema,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return StepOutputResponse(
        validated=record.validated,
        errors=record.errors,
        state=record.state.value,
    )


# Run-engine endpoints (Bolt 3, N5). ``start_run`` is awaited INLINE (Q1=A): the
# HTTP request is the blocking wait, matching the synchronous ``workflow_run`` MCP
# tool. Error mapping (C5 / B3-BR-14): unknown run/spec -> 404, invalid spec/inputs
# -> 400, cancel-of-finished -> 409, NotBuiltYetError (reserved seam) -> 501,
# WorkflowEngineError -> 500. Narrow exceptions in the service; mapped here.


@app.post("/workflows/runs")
async def start_workflow_run_endpoint(
    body: WorkflowRunRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """Resolve a spec, run it to completion inline, return the WorkflowRunResult."""
    import uuid

    from cli_agent_orchestrator.models.workflow import NotBuiltYetError
    from cli_agent_orchestrator.services import workflow_service, workflow_spec_service

    try:
        spec = workflow_spec_service.get_workflow(body.name_or_path)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown workflow '{body.name_or_path}'",
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    run_id = body.run_id or f"run-{uuid.uuid4().hex[:16]}"
    try:
        result = await workflow_service.start_run(spec, body.inputs, run_id)
    except NotBuiltYetError as e:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(e))
    except KeyError as e:
        # Duplicate run_id is a conflict, not a 404.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except workflow_service.WorkflowEngineError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
    return result.model_dump()


@app.get("/workflows/runs/{run_id}")
async def get_workflow_run_endpoint(run_id: str) -> Dict:
    """Return a point-in-time status snapshot for a run (FR-5.5)."""
    from cli_agent_orchestrator.services import workflow_service

    try:
        status_snapshot = workflow_service.get_run_status(run_id)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown run '{run_id}'")
    return status_snapshot.model_dump()


@app.post("/workflows/runs/{run_id}/cancel")
async def cancel_workflow_run_endpoint(
    run_id: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """Cooperatively cancel a running workflow (FR-5.4)."""
    from cli_agent_orchestrator.services import workflow_service

    try:
        workflow_service.cancel_run(run_id)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown run '{run_id}'")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    return {"success": True, "run_id": run_id}


@app.delete("/terminals/{terminal_id}")
async def delete_terminal(
    request: Request,
    terminal_id: TerminalId,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_ADMIN)),
) -> Dict:
    """Delete a terminal."""
    try:
        success = terminal_service.delete_terminal(
            terminal_id, registry=get_plugin_registry(request)
        )
        return {"success": success}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete terminal: {str(e)}",
        )


@app.post("/terminals/{receiver_id}/inbox/messages")
async def create_inbox_message_endpoint(
    request: Request,
    receiver_id: TerminalId,
    sender_id: str,
    message: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """Create inbox message and attempt immediate delivery."""
    try:
        inbox_msg = create_inbox_message(
            sender_id,
            receiver_id,
            message,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create inbox message: {str(e)}",
        )

    # Attempt immediate delivery if terminal is already IDLE.
    # If not, InboxService will deliver on next IDLE status event.
    try:
        inbox_service.deliver_pending(receiver_id, registry=get_plugin_registry(request))
    except Exception as e:
        logger.warning(f"Immediate delivery attempt failed for {receiver_id}: {e}")

    return {
        "success": True,
        "message_id": inbox_msg.id,
        "sender_id": inbox_msg.sender_id,
        "receiver_id": inbox_msg.receiver_id,
        "created_at": inbox_msg.created_at.isoformat(),
    }


@app.get("/terminals/{terminal_id}/inbox/messages")
async def get_inbox_messages_endpoint(
    terminal_id: TerminalId,
    limit: int = Query(default=10, le=100, description="Maximum number of messages to retrieve"),
    status_param: Optional[str] = Query(
        default=None, alias="status", description="Filter by message status"
    ),
) -> List[Dict]:
    """Get inbox messages for a terminal.

    Args:
        terminal_id: Terminal ID to get messages for
        limit: Maximum number of messages to return (default: 10, max: 100)
        status_param: Optional filter by message status ('pending', 'delivered', 'failed')

    Returns:
        List of inbox messages with sender_id, message, created_at, status
    """
    try:
        # Convert status filter if provided
        status_filter = None
        if status_param:
            try:
                status_filter = MessageStatus(status_param)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid status: {status_param}. Valid values: pending, delivered, failed",
                )

        # Get messages using existing database function
        messages = get_inbox_messages(terminal_id, limit=limit, status=status_filter)

        # Convert to response format
        result = []
        for msg in messages:
            result.append(
                {
                    "id": msg.id,
                    "sender_id": msg.sender_id,
                    "receiver_id": msg.receiver_id,
                    "message": msg.message,
                    "status": msg.status.value,
                    "created_at": msg.created_at.isoformat() if msg.created_at else None,
                }
            )

        return result

    except HTTPException:
        # Re-raise HTTPException (validation errors)
        raise
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve inbox messages: {str(e)}",
        )


@app.websocket("/terminals/{terminal_id}/ws")
async def terminal_ws(websocket: WebSocket, terminal_id: str):
    """WebSocket endpoint for live terminal streaming via tmux attach.

    Security: This endpoint provides full PTY access with no authentication.
    It is intended for localhost-only use. Do NOT expose the server to
    untrusted networks (e.g. --host 0.0.0.0) without adding authentication.
    """
    # Reject connections from clients outside the configured allowlist.
    # Defaults to loopback; operators running cao-server inside a container can
    # extend the allowlist with the ``CAO_WS_ALLOWED_CLIENTS`` env var so the
    # host browser (reaching the container via a bridge IP) can attach.
    # A literal ``*`` in the allowlist disables the IP check (Codespaces /
    # devcontainers / remote setups where the WS client originates from an
    # IP the operator cannot enumerate ahead of time).
    client_host = websocket.client.host if websocket.client else None
    if (
        "*" not in WS_ALLOWED_CLIENTS
        and client_host is not None
        and client_host not in WS_ALLOWED_CLIENTS
    ):
        await websocket.close(code=4003, reason="WebSocket access is restricted to allowed clients")
        return

    await websocket.accept()

    metadata = get_terminal_metadata(terminal_id)
    if not metadata:
        await websocket.close(code=4004, reason="Terminal not found")
        return

    # Defence-in-depth: re-validate the names from the DB before they
    # flow into a tmux subprocess argument. The POST /sessions handler
    # now validates user-supplied session_name, but pre-existing rows
    # or future code paths could still bypass that, and tmux parses
    # ':' / '.' as target delimiters. Bind the validator return values
    # so the sanitization is explicit at the actual sink below.
    try:
        session_name = validate_tmux_name(metadata["tmux_session"], "session_name")
        window_name = validate_tmux_name(metadata["tmux_window"], "window_name")
    except ValueError:
        await websocket.close(code=4003, reason="Invalid tmux target name")
        return

    # Create PTY pair for tmux attach
    master_fd, slave_fd = pty.openpty()

    # Set initial terminal size
    winsize = struct.pack("HHHH", 24, 80, 0, 0)
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

    # Start tmux attach inside the PTY.
    # Container/devcontainer environments often leave TERM unset or set to
    # ``dumb``, which strips colours, breaks cursor positioning and corrupts
    # the Ink-based TUIs that agent CLIs render. Force a sane default so the
    # browser-side xterm.js renderer sees the escape sequences it expects.
    # Any explicit non-dumb TERM the operator set is preserved.
    pty_env = _build_pty_env()
    proc = subprocess.Popen(
        ["tmux", "-u", "attach-session", "-t", f"{session_name}:{window_name}"],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
        preexec_fn=os.setsid,
        env=pty_env,
    )
    os.close(slave_fd)

    # Make master_fd non-blocking for event-driven reads
    flag = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flag | os.O_NONBLOCK)

    loop = asyncio.get_event_loop()
    output_queue: asyncio.Queue[bytes] = asyncio.Queue()
    done = asyncio.Event()

    def _on_pty_data():
        """Callback when PTY has data available."""
        try:
            data = os.read(master_fd, 65536)
            if data:
                output_queue.put_nowait(data)
            else:
                done.set()
        except BlockingIOError:
            pass
        except OSError:
            done.set()

    loop.add_reader(master_fd, _on_pty_data)

    async def _forward_output():
        """Read from PTY queue and send to WebSocket."""
        while not done.is_set():
            try:
                data = await asyncio.wait_for(output_queue.get(), timeout=1.0)
                # Drain any additional pending data for batching
                while not output_queue.empty():
                    try:
                        data += output_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                await websocket.send_bytes(data)
            except asyncio.TimeoutError:
                if proc.poll() is not None:
                    break
            except (Exception, asyncio.CancelledError):
                break

    async def _forward_input():
        """Receive from WebSocket and write to PTY."""
        try:
            while not done.is_set():
                msg = await websocket.receive_text()
                payload = json.loads(msg)
                if payload.get("type") == "input":
                    raw = payload["data"].encode()
                    # Write in chunks to avoid overflowing the PTY buffer
                    chunk_size = 1024
                    for i in range(0, len(raw), chunk_size):
                        os.write(master_fd, raw[i : i + chunk_size])
                        if i + chunk_size < len(raw):
                            await asyncio.sleep(0.01)
                elif payload.get("type") == "resize":
                    rows = payload.get("rows", 24)
                    cols = payload.get("cols", 80)
                    winsize_data = struct.pack("HHHH", rows, cols, 0, 0)
                    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize_data)
                    # Explicitly notify tmux of the size change —
                    # TIOCSWINSZ on the master doesn't always deliver
                    # SIGWINCH to the child process group.
                    try:
                        os.kill(proc.pid, signal.SIGWINCH)
                    except OSError:
                        pass
        except WebSocketDisconnect:
            pass
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            done.set()

    try:
        await asyncio.gather(_forward_output(), _forward_input())
    except (Exception, asyncio.CancelledError):
        pass
    finally:
        done.set()
        try:
            loop.remove_reader(master_fd)
        except Exception:
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass
        # Terminate tmux attach (just detaches, doesn't kill the session)
        proc.terminate()
        try:
            await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=3.0)
        except asyncio.TimeoutError:
            proc.kill()
            await asyncio.to_thread(proc.wait)


# ── Flow management endpoints ────────────────────────────────────────


@app.get("/flows", response_model=List[Flow])
async def list_flows() -> List[Flow]:
    """List all flows."""
    try:
        return flow_service.list_flows()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list flows: {str(e)}",
        )


@app.get("/flows/{name}", response_model=Flow)
async def get_flow(name: str) -> Flow:
    """Get a specific flow by name."""
    try:
        return flow_service.get_flow(name)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get flow: {str(e)}",
        )


@app.post("/flows", response_model=Flow, status_code=status.HTTP_201_CREATED)
async def create_flow(
    body: CreateFlowRequest,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Flow:
    """Create a new flow.

    Writes a .flow.md file with YAML frontmatter and prompt body, then
    registers it via flow_service.add_flow().
    """
    try:
        flows_dir = CAO_HOME_DIR / "flows"
        flows_dir.mkdir(parents=True, exist_ok=True)

        file_path = flows_dir / f"{body.name}.flow.md"

        # Build YAML frontmatter content
        frontmatter_lines = [
            "---",
            f"name: {body.name}",
            f'schedule: "{body.schedule}"',
            f"agent_profile: {body.agent_profile}",
            f"provider: {body.provider}",
            "---",
        ]
        file_content = "\n".join(frontmatter_lines) + "\n" + body.prompt_template

        file_path.write_text(file_content)

        return flow_service.add_flow(str(file_path))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create flow: {str(e)}",
        )


@app.delete("/flows/{name}")
async def remove_flow(
    name: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_ADMIN)),
) -> Dict:
    """Remove a flow."""
    try:
        flow_service.remove_flow(name)
        return {"success": True}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to remove flow: {str(e)}",
        )


@app.post("/flows/{name}/enable")
async def enable_flow(
    name: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """Enable a flow."""
    try:
        flow_service.enable_flow(name)
        return {"success": True}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to enable flow: {str(e)}",
        )


@app.post("/flows/{name}/disable")
async def disable_flow(
    name: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """Disable a flow."""
    try:
        flow_service.disable_flow(name)
        return {"success": True}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to disable flow: {str(e)}",
        )


@app.post("/flows/{name}/run")
async def run_flow(
    name: str,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_WRITE, SCOPE_ADMIN)),
) -> Dict:
    """Manually execute a flow."""
    try:
        executed = await flow_service.execute_flow(name)
        return {"executed": executed}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to execute flow: {str(e)}",
        )


# ── Memory endpoints ─────────────────────────────────────────────────
# REST mirror of `cao memory list/show/delete/clear` (issue #286). The server
# has no meaningful cwd, so project scope is addressed by an explicit scope_id
# query param instead of terminal_context — passing a client cwd would be
# routed through resolve_project_id(), whose CAO_PROJECT_ID override applies
# unconditionally and could silently target the wrong project.


def _get_memory_service():
    """Build a MemoryService (lazy import mirrors the circular-import guard
    in memory_service._is_memory_enabled; module-level factory so tests can
    patch it like the CLI's _get_memory_service)."""
    from cli_agent_orchestrator.services.memory_service import MemoryService

    return MemoryService()


def _require_memory_enabled() -> None:
    """Raise 404 when the memory subsystem is disabled.

    recall() silently returns [] when disabled, so the gate must be explicit
    rather than inferred from empty results.
    """
    from cli_agent_orchestrator.services.settings_service import is_memory_enabled

    if not is_memory_enabled():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Memory system is disabled"
        )


def _memory_scope_id(mem, base_dir: Path) -> Optional[str]:
    """Resolve the response scope_id for a recalled memory.

    session/agent results carry scope_id natively; project membership is only
    recoverable from the storage path (base_dir/<project_id>/wiki/project/...);
    global has none.
    """
    if mem.scope_id:
        return str(mem.scope_id)
    if mem.scope != MemoryScope.PROJECT.value:
        return None
    try:
        relative = Path(mem.file_path).resolve().relative_to(base_dir.resolve())
        return relative.parts[0]
    except (ValueError, IndexError):
        return None


def _memory_matches_scope_id(mem, scope_id: str, base_dir: Path) -> bool:
    """True when a recalled memory belongs to the given scope_id.

    Global memories have no scope_id (resolved as None), so they never match —
    scope_id strictly narrows to one project/session/agent.
    """
    return _memory_scope_id(mem, base_dir) == scope_id


def _to_memory_summary(mem, base_dir: Path) -> MemorySummary:
    return MemorySummary(
        key=mem.key,
        scope=mem.scope,
        scope_id=_memory_scope_id(mem, base_dir),
        memory_type=mem.memory_type,
        tags=mem.tags,
        created_at=mem.created_at,
        updated_at=mem.updated_at,
    )


@app.get("/memory", response_model=List[MemorySummary])
async def list_memories_endpoint(
    scope: Optional[MemoryScope] = None,
    memory_type: Optional[MemoryType] = Query(default=None, alias="type"),
    scope_id: Optional[MemoryScopeId] = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> List[MemorySummary]:
    """List stored memories across all projects (mirrors `cao memory list --all`)."""
    _require_memory_enabled()
    svc = _get_memory_service()
    try:
        # Internal limit 1000: recall truncates BEFORE the scope_id filter
        # below, so filtering a small page could return an under-filled result.
        # metadata mode: no query to rank, and it avoids the BM25 path.
        memories = await svc.recall(
            scope=scope.value if scope else None,
            memory_type=memory_type.value if memory_type else None,
            limit=1000,
            scan_all=True,
            search_mode="metadata",
        )
        if scope_id is not None:
            memories = [m for m in memories if _memory_matches_scope_id(m, scope_id, svc.base_dir)]
        return [_to_memory_summary(m, svc.base_dir) for m in memories[:limit]]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list memories: {str(e)}",
        )


@app.get("/memory/{key}", response_model=MemoryDetail)
async def get_memory_endpoint(
    key: MemoryKey,
    scope: Optional[MemoryScope] = None,
    scope_id: Optional[MemoryScopeId] = None,
) -> MemoryDetail:
    """Show a memory by key (mirrors `cao memory show`; first match wins)."""
    _require_memory_enabled()
    svc = _get_memory_service()
    try:
        memories = await svc.recall(
            query=key,
            scope=scope.value if scope else None,
            limit=1000,
            scan_all=True,
            search_mode="metadata",
        )
        for mem in memories:
            if mem.key != key:
                continue
            if scope_id is not None and not _memory_matches_scope_id(mem, scope_id, svc.base_dir):
                continue
            return MemoryDetail(
                content=mem.content,
                **_to_memory_summary(mem, svc.base_dir).model_dump(),
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Memory '{key}' not found"
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get memory: {str(e)}",
        )


@app.delete("/memory/{key}")
async def delete_memory_endpoint(
    key: MemoryKey,
    scope: MemoryScope = MemoryScope.PROJECT,
    scope_id: Optional[MemoryScopeId] = None,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_ADMIN)),
) -> Dict:
    """Delete a memory by key (mirrors `cao memory delete`).

    Unlike the MCP memory_forget tool (which resolves context from
    CAO_TERMINAL_ID), non-global scopes require an explicit scope_id.
    """
    from cli_agent_orchestrator.services.memory_service import MemoryDisabledError

    _require_memory_enabled()
    if scope != MemoryScope.GLOBAL and scope_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"scope '{scope.value}' requires scope_id",
        )
    svc = _get_memory_service()
    try:
        deleted = await svc.forget(key=key, scope=scope.value, scope_id=scope_id)
    except MemoryDisabledError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Memory system is disabled"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete memory: {str(e)}",
        )
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Memory '{key}' not found in scope '{scope.value}'",
        )
    return {"success": True}


@app.delete("/memory")
async def clear_memories_endpoint(
    scope: MemoryScope,
    scope_id: Optional[MemoryScopeId] = None,
    _scopes: List[str] = Depends(require_any_scope(SCOPE_ADMIN)),
) -> Dict:
    """Clear all memories in a scope (mirrors `cao memory clear`).

    Best-effort per-item loop (warn-and-continue), reporting deleted_count —
    deliberately not all-or-nothing.
    """
    from cli_agent_orchestrator.services.memory_service import MemoryDisabledError

    _require_memory_enabled()
    if scope != MemoryScope.GLOBAL and scope_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"scope '{scope.value}' requires scope_id",
        )
    svc = _get_memory_service()
    try:
        memories = await svc.recall(
            scope=scope.value, limit=1000, scan_all=True, search_mode="metadata"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to clear memories: {str(e)}",
        )
    if scope_id is not None:
        memories = [m for m in memories if _memory_matches_scope_id(m, scope_id, svc.base_dir)]

    deleted_count = 0
    for mem in memories:
        try:
            # session/agent results carry scope_id natively; project results
            # need the query param (their recalled scope_id is None).
            if await svc.forget(key=mem.key, scope=scope.value, scope_id=mem.scope_id or scope_id):
                deleted_count += 1
        except MemoryDisabledError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Memory system is disabled"
            )
        except Exception as e:
            logger.warning("Failed to delete memory %r during clear: %s", mem.key, e)
    return {"success": True, "deleted_count": deleted_count}


# Static file serving for built web UI.
# Anchored to the package via importlib.resources so it works for both
# editable installs (uv sync) and wheel installs (uv tool install, pip install).
from importlib.resources import files as _pkg_files

WEB_DIST = Path(str(_pkg_files("cli_agent_orchestrator") / "web_ui"))
if (WEB_DIST / "index.html").exists():
    from starlette.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(WEB_DIST), html=True), name="web")


def main():
    """Entry point for cao-server command."""
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="CLI Agent Orchestrator Server")
    parser.add_argument(
        "--agents-dir",
        type=str,
        default=None,
        help="Path to agents directory (overrides CAO_AGENTS_DIR env var)",
    )
    parser.add_argument("--host", type=str, default=None, help="Server host")
    parser.add_argument("--port", type=int, default=None, help="Server port")
    parser.add_argument(
        "--terminal",
        type=str,
        choices=["tmux", "herdr"],
        default=None,
        help="Terminal backend to use, overriding terminal_backend in config.json",
    )
    args = parser.parse_args()

    if args.agents_dir:
        os.environ["CAO_AGENTS_DIR"] = args.agents_dir
        import cli_agent_orchestrator.constants as constants

        constants.KIRO_AGENTS_DIR = Path(args.agents_dir)
        logger.info(f"Using agents directory: {args.agents_dir}")

    # Resolve the backend before the server starts so the lifespan (and every
    # get_backend() consumer) sees the CLI-selected backend. Without --terminal,
    # the singleton stays lazy and BackendFactory reads config.json on first use.
    if args.terminal:
        from cli_agent_orchestrator.backends.factory import BackendFactory
        from cli_agent_orchestrator.backends.registry import set_backend

        set_backend(BackendFactory.create(backend_override=args.terminal))
        logger.info(f"Terminal backend overridden via --terminal: {args.terminal}")

    host = args.host or SERVER_HOST
    port = args.port or SERVER_PORT
    # Extend the CORS allowlist so a custom --host/--port still permits
    # same-host browser access without requiring CAO_CORS_ORIGINS. The
    # already-installed CORSMiddleware reads the list by reference, so
    # mutating it before uvicorn starts is sufficient. See issue #151.
    add_local_cors_origins(host, port)
    # --proxy-headers: trust X-Forwarded-Proto / X-Forwarded-For from
    # an upstream reverse proxy (Codespaces / devcontainers / nginx in
    # front of cao-server). Required for the WebSocket terminal viewer
    # over an HTTPS tunnel — without it uvicorn sees the raw HTTP
    # request and the browser's WSS upgrade fails. See issue #149.
    #
    # The forwarded-allow-ips list defaults to loopback (see
    # constants.TRUSTED_FORWARDER_IPS); operators behind a reverse
    # proxy opt into a wider range with CAO_FORWARDED_ALLOW_IPS. A
    # literal ``*`` is honoured and disables the check (matches the
    # existing CAO_WS_ALLOWED_CLIENTS="*" semantics).
    forwarded_ips = "*" if "*" in TRUSTED_FORWARDER_IPS else ",".join(TRUSTED_FORWARDER_IPS)
    uvicorn.run(
        app,
        host=host,
        port=port,
        proxy_headers=True,
        forwarded_allow_ips=forwarded_ips,
    )


if __name__ == "__main__":
    main()

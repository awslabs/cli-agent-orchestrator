"""CLI Agent Orchestrator MCP Server implementation."""

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple, Union

import requests
from fastmcp import FastMCP
from pydantic import Field

from cli_agent_orchestrator.constants import API_BASE_URL, DEFAULT_PROVIDER
from cli_agent_orchestrator.mcp_server.models import HandoffResult
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.utils.terminal import generate_session_name, wait_until_terminal_status

logger = logging.getLogger(__name__)

# Environment variable to enable/disable working_directory parameter
ENABLE_WORKING_DIRECTORY = os.getenv("CAO_ENABLE_WORKING_DIRECTORY", "false").lower() == "true"

# Environment variable to enable/disable automatic sender terminal ID injection
ENABLE_SENDER_ID_INJECTION = os.getenv("CAO_ENABLE_SENDER_ID_INJECTION", "false").lower() == "true"

# Create MCP server
mcp = FastMCP(
    "cao-mcp-server",
    instructions="""
    # CLI Agent Orchestrator MCP Server

    This server provides tools to facilitate terminal delegation within CLI Agent Orchestrator sessions.

    ## Best Practices

    - Use specific agent profiles and providers
    - Provide clear and concise messages
    - Ensure you're running within a CAO terminal (CAO_TERMINAL_ID must be set)
    """,
)

LOAD_SKILL_TOOL_DESCRIPTION = """Retrieve the full Markdown body of an available skill from cao-server.

Use this tool when your prompt lists a CAO skill and you need its full instructions at runtime.

Args:
    name: Name of the skill to retrieve

Returns:
    The skill content on success, or a dict with success=False and an error message on failure
"""


def _resolve_child_allowed_tools(
    parent_allowed_tools: Optional[list], child_profile_name: str
) -> Optional[str]:
    """Resolve allowed_tools for a child terminal via intersection.

    The child gets at most the union of: what the parent allows + what the
    child profile specifies. If the parent is unrestricted ("*"), the child
    profile's allowedTools are used as-is.

    Returns:
        Comma-separated string of allowed tools, or None for unrestricted.
    """
    from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
    from cli_agent_orchestrator.utils.tool_mapping import resolve_allowed_tools

    try:
        child_profile = load_agent_profile(child_profile_name)
        mcp_server_names = (
            list(child_profile.mcpServers.keys()) if child_profile.mcpServers else None
        )
        child_allowed = resolve_allowed_tools(
            child_profile.allowedTools, child_profile.role, mcp_server_names
        )
    except FileNotFoundError:
        child_allowed = None

    # If parent is unrestricted or has no restrictions, use child's tools
    if parent_allowed_tools is None or "*" in parent_allowed_tools:
        if child_allowed:
            return ",".join(child_allowed)
        return None

    # If child has no opinion (None), inherit parent's restrictions
    if child_allowed is None:
        return ",".join(parent_allowed_tools)

    # If child explicitly requests unrestricted ("*"), honor it
    if "*" in child_allowed:
        return None

    # Both have restrictions: child gets its own profile tools
    # (the child profile defines what it needs; parent's restrictions
    # are enforced by the parent not delegating unauthorized work)
    return ",".join(child_allowed)


def _create_terminal(
    agent_profile: str, working_directory: Optional[str] = None
) -> Tuple[str, str]:
    """Create a new terminal with the specified agent profile.

    Args:
        agent_profile: Agent profile for the terminal
        working_directory: Optional working directory for the terminal

    Returns:
        Tuple of (terminal_id, provider)

    Raises:
        Exception: If terminal creation fails
    """
    provider = DEFAULT_PROVIDER
    parent_allowed_tools = None

    # Get current terminal ID from environment
    current_terminal_id = os.environ.get("CAO_TERMINAL_ID")
    if current_terminal_id:
        # Get terminal metadata via API
        response = requests.get(f"{API_BASE_URL}/terminals/{current_terminal_id}")
        response.raise_for_status()
        terminal_metadata = response.json()

        provider = terminal_metadata["provider"]
        session_name = terminal_metadata["session_name"]
        parent_allowed_tools = terminal_metadata.get("allowed_tools")

        # If no working_directory specified, get conductor's current directory
        if working_directory is None:
            try:
                response = requests.get(
                    f"{API_BASE_URL}/terminals/{current_terminal_id}/working-directory"
                )
                if response.status_code == 200:
                    working_directory = response.json().get("working_directory")
                    logger.info(f"Inherited working directory from conductor: {working_directory}")
                else:
                    logger.warning(
                        f"Failed to get conductor's working directory (status {response.status_code}), "
                        "will use server default"
                    )
            except Exception as e:
                logger.warning(
                    f"Error fetching conductor's working directory: {e}, will use server default"
                )

        # Resolve child's allowed_tools via inheritance
        child_allowed_tools = _resolve_child_allowed_tools(parent_allowed_tools, agent_profile)

        # Create new terminal in existing session - always pass working_directory
        params = {"provider": provider, "agent_profile": agent_profile}
        if working_directory:
            params["working_directory"] = working_directory
        if child_allowed_tools:
            params["allowed_tools"] = child_allowed_tools

        response = requests.post(f"{API_BASE_URL}/sessions/{session_name}/terminals", params=params)
        response.raise_for_status()
        terminal = response.json()
    else:
        # Create new session with terminal
        session_name = generate_session_name()
        params = {
            "provider": provider,
            "agent_profile": agent_profile,
            "session_name": session_name,
        }
        if working_directory:
            params["working_directory"] = working_directory

        response = requests.post(f"{API_BASE_URL}/sessions", params=params)
        response.raise_for_status()
        terminal = response.json()

    return terminal["id"], provider


def _send_direct_input(terminal_id: str, message: str) -> None:
    """Send input directly to a terminal (bypasses inbox).

    Args:
        terminal_id: Terminal ID
        message: Message to send

    Raises:
        Exception: If sending fails
    """
    response = requests.post(
        f"{API_BASE_URL}/terminals/{terminal_id}/input", params={"message": message}
    )
    response.raise_for_status()


def _send_direct_input_handoff(terminal_id: str, provider: str, message: str) -> None:
    """Send handoff payload to an agent, prepending orchestrator instructions if needed."""
    # For Codex provider: prepend handoff context so the worker agent knows
    # this is a blocking handoff and should simply output results rather than
    # attempting to call send_message back to the supervisor.
    if provider == "codex":
        supervisor_id = os.environ.get("CAO_TERMINAL_ID", "unknown")
        handoff_message = (
            f"[CAO Handoff] Supervisor terminal ID: {supervisor_id}. "
            "This is a blocking handoff — the orchestrator will automatically "
            "capture your response when you finish. Complete the task and output "
            "your results directly. Do NOT use send_message to notify the supervisor "
            "unless explicitly needed — just do the work and present your deliverables.\n\n"
            f"{message}"
        )
    else:
        handoff_message = message

    _send_direct_input(terminal_id, handoff_message)


def _send_direct_input_assign(terminal_id: str, message: str) -> None:
    """Send assign payload to a worker agent, appending callback instructions."""
    # Auto-inject sender terminal ID suffix when enabled
    if ENABLE_SENDER_ID_INJECTION:
        sender_id = os.environ.get("CAO_TERMINAL_ID", "unknown")
        message += (
            f"\n\n[Assigned by terminal {sender_id}. "
            f"When done, send results back to terminal {sender_id} using send_message]"
        )

    _send_direct_input(terminal_id, message)


def _send_to_inbox(receiver_id: str, message: str) -> Dict[str, Any]:
    """Send message to another terminal's inbox (queued delivery when IDLE).

    Args:
        receiver_id: Target terminal ID
        message: Message content

    Returns:
        Dict with message details

    Raises:
        ValueError: If CAO_TERMINAL_ID not set
        Exception: If API call fails
    """
    sender_id = os.getenv("CAO_TERMINAL_ID")
    if not sender_id:
        raise ValueError("CAO_TERMINAL_ID not set - cannot determine sender")

    response = requests.post(
        f"{API_BASE_URL}/terminals/{receiver_id}/inbox/messages",
        params={"sender_id": sender_id, "message": message},
    )
    response.raise_for_status()
    return response.json()


def _extract_error_detail(response: requests.Response, fallback: str) -> str:
    """Extract a human-readable error detail from an API response."""
    try:
        payload = response.json()
    except ValueError:
        return fallback

    detail = payload.get("detail")
    if isinstance(detail, str) and detail:
        return detail
    return fallback


def _load_skill_impl(name: str) -> Union[str, Dict[str, Any]]:
    """Fetch a skill body from cao-server and return content or a structured error."""
    try:
        response = requests.get(f"{API_BASE_URL}/skills/{name}")
        response.raise_for_status()
        return response.json()["content"]
    except requests.HTTPError as exc:
        detail = str(exc)
        if exc.response is not None:
            detail = _extract_error_detail(exc.response, detail)
        return {"success": False, "error": detail}
    except requests.ConnectionError:
        return {
            "success": False,
            "error": "Failed to connect to cao-server. The server may not be running.",
        }
    except Exception as exc:
        return {"success": False, "error": f"Failed to retrieve skill: {str(exc)}"}


# Implementation functions
async def _handoff_impl(
    agent_profile: str, message: str, timeout: int = 600, working_directory: Optional[str] = None
) -> HandoffResult:
    """Implementation of handoff logic."""
    start_time = time.time()

    try:
        # Create terminal
        terminal_id, provider = _create_terminal(agent_profile, working_directory)

        # Wait for terminal to be ready (IDLE or COMPLETED) before sending
        # the handoff message. Accept COMPLETED in addition to IDLE because
        # providers that use an initial prompt flag process the system prompt
        # as the first user message and produce a response, reaching COMPLETED
        # without ever showing a bare IDLE state.
        # Both states indicate the provider is ready to accept input.
        #
        # Use a generous timeout (120s) because provider initialization can be
        # slow: shell warm-up (~5s), CLI startup with MCP server registration
        # (~10-30s), and API authentication (~5-10s). If the provider's own
        # initialize() timed out (60-90s), this acts as a fallback to catch
        # cases where the CLI starts slightly after the provider timeout.
        # Provider initialization can be slow (~15-45s depending on provider).
        if not wait_until_terminal_status(
            terminal_id,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=120.0,
        ):
            return HandoffResult(
                success=False,
                message=f"Terminal {terminal_id} did not reach ready status within 120 seconds",
                output=None,
                terminal_id=terminal_id,
            )

        await asyncio.sleep(2)  # wait another 2s

        # Send message to terminal (injects handoff instructions for codex if needed)
        _send_direct_input_handoff(terminal_id, provider, message)

        # Monitor until completion with timeout
        if not wait_until_terminal_status(
            terminal_id, TerminalStatus.COMPLETED, timeout=timeout, polling_interval=1.0
        ):
            return HandoffResult(
                success=False,
                message=f"Handoff timed out after {timeout} seconds",
                output=None,
                terminal_id=terminal_id,
            )

        # Extract session context before cleanup (non-blocking).
        # Captures what the worker did so the supervisor has structured context.
        session_context: dict = {}
        try:
            from cli_agent_orchestrator.providers.manager import provider_manager

            worker_provider = provider_manager.get_provider(terminal_id)
            if worker_provider:
                session_context = await worker_provider.extract_session_context()
        except NotImplementedError:
            pass  # Kimi deferred to Phase 3
        except Exception as e:
            logger.debug(f"Non-blocking session context extraction failed: {e}")

        # Log task_completed event with session context (non-blocking)
        try:
            import json as _json

            from cli_agent_orchestrator.clients.database import get_terminal_metadata as _get_meta
            from cli_agent_orchestrator.clients.database import log_session_event as _log_evt

            _meta = _get_meta(terminal_id)
            if _meta:
                _log_evt(
                    session_name=_meta["tmux_session"],
                    terminal_id=terminal_id,
                    provider=provider,
                    event_type="task_completed",
                    summary=f"Task completed by {agent_profile}",
                    metadata_json=_json.dumps(session_context) if session_context else "{}",
                )
        except Exception as e:
            logger.debug(f"Non-blocking event log failed: {e}")

        # Get the response
        response = requests.get(
            f"{API_BASE_URL}/terminals/{terminal_id}/output", params={"mode": "last"}
        )
        response.raise_for_status()
        output_data = response.json()
        output = output_data["output"]

        # Send provider-specific exit command to cleanup terminal
        response = requests.post(f"{API_BASE_URL}/terminals/{terminal_id}/exit")
        response.raise_for_status()

        execution_time = time.time() - start_time

        # Log handoff_returned event (non-blocking)
        try:
            from cli_agent_orchestrator.clients.database import (
                get_terminal_metadata,
                log_session_event,
            )

            caller_tid = os.environ.get("CAO_TERMINAL_ID", "")
            caller_meta = get_terminal_metadata(caller_tid) if caller_tid else None
            session_name = caller_meta["tmux_session"] if caller_meta else ""
            log_session_event(
                session_name=session_name,
                terminal_id=terminal_id,
                provider=provider,
                event_type="handoff_returned",
                summary=f"Handoff to {agent_profile} completed in {execution_time:.2f}s",
            )
        except Exception as e:
            logger.debug(f"Non-blocking event log failed: {e}")

        return HandoffResult(
            success=True,
            message=f"Successfully handed off to {agent_profile} ({provider}) in {execution_time:.2f}s",
            output=output,
            terminal_id=terminal_id,
        )

    except Exception as e:
        return HandoffResult(
            success=False, message=f"Handoff failed: {str(e)}", output=None, terminal_id=None
        )


# Conditional tool registration based on environment variable
if ENABLE_WORKING_DIRECTORY:

    @mcp.tool()
    async def handoff(
        agent_profile: str = Field(
            description='The agent profile to hand off to (e.g., "developer", "analyst")'
        ),
        message: str = Field(description="The message/task to send to the target agent"),
        timeout: int = Field(
            default=600,
            description="Maximum time to wait for the agent to complete the task (in seconds)",
            ge=1,
            le=3600,
        ),
        working_directory: Optional[str] = Field(
            default=None,
            description='Optional working directory where the agent should execute (e.g., "/path/to/workspace/src/Package")',
        ),
    ) -> HandoffResult:
        """Hand off a task to another agent via CAO terminal and wait for completion.

        This tool allows handing off tasks to other agents by creating a new terminal
        in the same session. It sends the message, waits for completion, and captures the output.

        ## Usage

        Use this tool to hand off tasks to another agent and wait for the results.
        The tool will:
        1. Create a new terminal with the specified agent profile and provider
        2. Set the working directory for the terminal (defaults to supervisor's cwd)
        3. Send the message to the terminal
        4. Monitor until completion
        5. Return the agent's response
        6. Clean up the terminal with /exit

        ## Working Directory

        - By default, agents start in the supervisor's current working directory
        - You can specify a custom directory via working_directory parameter
        - Directory must exist and be accessible

        ## Requirements

        - Must be called from within a CAO terminal (CAO_TERMINAL_ID environment variable)
        - Target session must exist and be accessible
        - If working_directory is provided, it must exist and be accessible

        Args:
            agent_profile: The agent profile for the new terminal
            message: The task/message to send
            timeout: Maximum wait time in seconds
            working_directory: Optional directory path where agent should execute

        Returns:
            HandoffResult with success status, message, and agent output
        """
        return await _handoff_impl(agent_profile, message, timeout, working_directory)

else:

    @mcp.tool()
    async def handoff(
        agent_profile: str = Field(
            description='The agent profile to hand off to (e.g., "developer", "analyst")'
        ),
        message: str = Field(description="The message/task to send to the target agent"),
        timeout: int = Field(
            default=600,
            description="Maximum time to wait for the agent to complete the task (in seconds)",
            ge=1,
            le=3600,
        ),
    ) -> HandoffResult:
        """Hand off a task to another agent via CAO terminal and wait for completion.

        This tool allows handing off tasks to other agents by creating a new terminal
        in the same session. It sends the message, waits for completion, and captures the output.

        ## Usage

        Use this tool to hand off tasks to another agent and wait for the results.
        The tool will:
        1. Create a new terminal with the specified agent profile and provider
        2. Send the message to the terminal (starts in supervisor's current directory)
        3. Monitor until completion
        4. Return the agent's response
        5. Clean up the terminal with /exit

        ## Requirements

        - Must be called from within a CAO terminal (CAO_TERMINAL_ID environment variable)
        - Target session must exist and be accessible

        Args:
            agent_profile: The agent profile for the new terminal
            message: The task/message to send
            timeout: Maximum wait time in seconds

        Returns:
            HandoffResult with success status, message, and agent output
        """
        return await _handoff_impl(agent_profile, message, timeout, None)


# Implementation function for assign
def _assign_impl(
    agent_profile: str, message: str, working_directory: Optional[str] = None
) -> Dict[str, Any]:
    """Implementation of assign logic."""
    try:
        # Create terminal
        terminal_id, _ = _create_terminal(agent_profile, working_directory)

        # Send message immediately (auto-injects sender terminal ID suffix when enabled)
        _send_direct_input_assign(terminal_id, message)

        return {
            "success": True,
            "terminal_id": terminal_id,
            "message": f"Task assigned to {agent_profile} (terminal: {terminal_id})",
        }

    except Exception as e:
        return {"success": False, "terminal_id": None, "message": f"Assignment failed: {str(e)}"}


def _build_assign_description(enable_sender_id: bool, enable_workdir: bool) -> str:
    """Build the assign tool description based on feature flags."""
    # Build tool description overview.
    if enable_sender_id:
        desc = """\
Assigns a task to another agent without blocking.

The sender's terminal ID and callback instructions will automatically be appended to the message."""
    else:
        desc = """\
Assigns a task to another agent without blocking.

In the message to the worker agent include instruction to send results back via send_message tool.
**IMPORTANT**: The terminal id of each agent is available in environment variable CAO_TERMINAL_ID.
When assigning, first find out your own CAO_TERMINAL_ID value, then include the terminal_id value in the message to the worker agent to allow callback.
Example message: "Analyze the logs. When done, send results back to terminal ee3f93b3 using send_message tool.\""""

    if enable_workdir:
        desc += """

## Working Directory

- By default, agents start in the supervisor's current working directory
- You can specify a custom directory via working_directory parameter
- Directory must exist and be accessible"""

    desc += """

Args:
    agent_profile: Agent profile for the worker terminal
    message: Task message (include callback instructions)"""

    if enable_workdir:
        desc += """
    working_directory: Optional working directory where the agent should execute"""

    desc += """

Returns:
    Dict with success status, worker terminal_id, and message"""

    return desc


_assign_description = _build_assign_description(
    ENABLE_SENDER_ID_INJECTION, ENABLE_WORKING_DIRECTORY
)
_assign_message_field_desc = (
    "The task message to send to the worker agent."
    if ENABLE_SENDER_ID_INJECTION
    else "The task message to send. Include callback instructions for the worker to send results back."
)

if ENABLE_WORKING_DIRECTORY:

    @mcp.tool(description=_assign_description)
    async def assign(
        agent_profile: str = Field(
            description='The agent profile for the worker agent (e.g., "developer", "analyst")'
        ),
        message: str = Field(description=_assign_message_field_desc),
        working_directory: Optional[str] = Field(
            default=None, description="Optional working directory where the agent should execute"
        ),
    ) -> Dict[str, Any]:
        return _assign_impl(agent_profile, message, working_directory)

else:

    @mcp.tool(description=_assign_description)
    async def assign(
        agent_profile: str = Field(
            description='The agent profile for the worker agent (e.g., "developer", "analyst")'
        ),
        message: str = Field(description=_assign_message_field_desc),
    ) -> Dict[str, Any]:
        return _assign_impl(agent_profile, message, None)


# Implementation function for send_message
def _send_message_impl(receiver_id: str, message: str) -> Dict[str, Any]:
    """Implementation of send_message logic."""
    try:
        # Auto-inject sender terminal ID suffix when enabled
        if ENABLE_SENDER_ID_INJECTION:
            sender_id = os.environ.get("CAO_TERMINAL_ID", "unknown")
            message += (
                f"\n\n[Message from terminal {sender_id}. "
                "Use send_message MCP tool for any follow-up work.]"
            )

        return _send_to_inbox(receiver_id, message)
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
async def send_message(
    receiver_id: str = Field(description="Target terminal ID to send message to"),
    message: str = Field(description="Message content to send"),
) -> Dict[str, Any]:
    """Send a message to another terminal's inbox.

    The message will be delivered when the destination terminal is IDLE.
    Messages are delivered in order (oldest first).

    Args:
        receiver_id: Terminal ID of the receiver
        message: Message content to send

    Returns:
        Dict with success status and message details
    """
    return _send_message_impl(receiver_id, message)


@mcp.tool(description=LOAD_SKILL_TOOL_DESCRIPTION)
async def load_skill(
    name: str = Field(description="Name of the skill to retrieve"),
) -> Any:
    """Retrieve skill content from cao-server."""
    return _load_skill_impl(name)


# =============================================================================
# Memory Tools
# =============================================================================

# U5 / SC-6: Explicit disabled message surfaced to agents when the memory
# subsystem is turned off via `memory.enabled=false` in settings.json.
# Kept deliberately short + actionable so it shows up verbatim in the tool
# result and the agent can relay it to the user.
MEMORY_DISABLED_MESSAGE = (
    "memory disabled — set memory.enabled=true in "
    "~/.aws/cli-agent-orchestrator/settings.json to enable"
)


def _get_terminal_context_from_env() -> Optional[Dict[str, Any]]:
    """Build terminal context dict from the calling terminal's CAO_TERMINAL_ID."""
    terminal_id = os.environ.get("CAO_TERMINAL_ID")
    if not terminal_id:
        return None

    try:
        response = requests.get(f"{API_BASE_URL}/terminals/{terminal_id}")
        response.raise_for_status()
        meta = response.json()
        ctx: Dict[str, Any] = {
            "terminal_id": meta["id"],
            "session_name": meta["session_name"],
            "provider": meta["provider"],
            "agent_profile": meta.get("agent_profile"),
        }
        # Try to get working directory for project scope resolution
        try:
            wd_resp = requests.get(f"{API_BASE_URL}/terminals/{terminal_id}/working-directory")
            if wd_resp.status_code == 200:
                ctx["cwd"] = wd_resp.json().get("working_directory")
        except Exception:
            pass
        return ctx
    except Exception as e:
        logger.warning(f"Failed to get terminal context for memory tools: {e}")
        return None


@mcp.tool()
async def memory_store(
    content: str = Field(description="Memory content to store (markdown supported)"),
    scope: str = Field(
        default="project",
        description='Memory scope: "global", "project", "session", or "agent"',
    ),
    memory_type: str = Field(
        default="project",
        description='Memory type: "user", "feedback", "project", or "reference"',
    ),
    key: Optional[str] = Field(
        default=None,
        description="Slug identifier (e.g. 'prefer-pytest'). Auto-generated from content if omitted.",
    ),
    tags: Optional[str] = Field(
        default=None,
        description="Comma-separated tags for search (e.g. 'testing,pytest')",
    ),
) -> Dict[str, Any]:
    """Store a persistent memory. Content is saved to a wiki file and indexed.

    Identical key+scope combinations are updated (upsert) — new content is appended
    as a timestamped entry. If key is omitted, it is auto-generated as a slug of the
    first 6 words of content.

    Use this to persist facts, decisions, user preferences, and project conventions
    that should be available across agent sessions.
    """
    from cli_agent_orchestrator.services.memory_service import (
        MemoryDisabledError,
        MemoryService,
    )

    try:
        service = MemoryService()
        terminal_context = _get_terminal_context_from_env()
        memory = await service.store(
            content=content,
            scope=scope,
            memory_type=memory_type,
            key=key,
            tags=tags or "",
            terminal_context=terminal_context,
        )
        return {
            "success": True,
            "key": memory.key,
            "scope": memory.scope,
            "scope_id": memory.scope_id,
            "file_path": memory.file_path,
            "action": "updated" if memory.created_at != memory.updated_at else "created",
        }
    except MemoryDisabledError:
        return {"success": False, "disabled": True, "error": MEMORY_DISABLED_MESSAGE}
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
async def memory_recall(
    query: Optional[str] = Field(
        default=None,
        description="Search query matched against memory content (case-insensitive)",
    ),
    scope: Optional[str] = Field(
        default=None,
        description='Filter by scope: "global", "project", "session", "agent". Omit to search all.',
    ),
    memory_type: Optional[str] = Field(
        default=None,
        description='Filter by type: "user", "feedback", "project", "reference". Omit for all types.',
    ),
    limit: int = Field(
        default=10,
        description="Maximum number of results to return",
        ge=1,
        le=100,
    ),
    search_mode: str = Field(
        default="hybrid",
        description='Search strategy: "metadata" (key/tag only), "bm25" (full-text content), "hybrid" (metadata first, BM25 to fill remaining slots)',
    ),
) -> Dict[str, Any]:
    """Retrieve memories matching a query and optional filters.

    Returns content from matching wiki files, sorted by recency.
    When no scope is specified, results follow scope precedence: session > project > global.

    Use this to check if relevant knowledge already exists before asking the user.
    """
    from cli_agent_orchestrator.services.memory_service import MemoryService
    from cli_agent_orchestrator.services.settings_service import is_memory_enabled

    if not is_memory_enabled():
        return {
            "success": False,
            "disabled": True,
            "error": MEMORY_DISABLED_MESSAGE,
            "memories": [],
        }

    try:
        service = MemoryService()
        terminal_context = _get_terminal_context_from_env()
        resolved_mode = search_mode if isinstance(search_mode, str) else "hybrid"
        memories = await service.recall(
            query=query,
            scope=scope,
            memory_type=memory_type,
            limit=limit,
            search_mode=resolved_mode,
            terminal_context=terminal_context,
        )
        return {
            "memories": [
                {
                    "key": m.key,
                    "content": m.content,
                    "memory_type": m.memory_type,
                    "scope": m.scope,
                    "tags": m.tags,
                    "file_path": m.file_path,
                    "updated_at": m.updated_at.isoformat() + "Z",
                }
                for m in memories
            ]
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
async def memory_forget(
    key: str = Field(description="Key of the memory to remove (e.g. 'prefer-pytest')"),
    scope: str = Field(
        default="project",
        description='Scope of the memory to remove: "global", "project", "session", or "agent"',
    ),
) -> Dict[str, Any]:
    """Remove a memory by key and scope.

    Deletes the wiki topic file and removes the entry from index.md.
    """
    from cli_agent_orchestrator.services.memory_service import (
        MemoryDisabledError,
        MemoryService,
    )

    try:
        service = MemoryService()
        terminal_context = _get_terminal_context_from_env()
        deleted = await service.forget(
            key=key,
            scope=scope,
            terminal_context=terminal_context,
        )
        return {
            "success": True,
            "deleted": deleted,
            "key": key,
            "scope": scope,
        }
    except MemoryDisabledError:
        return {"success": False, "disabled": True, "error": MEMORY_DISABLED_MESSAGE}
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
async def memory_consolidate(
    keys: List[str] = Field(
        description="List of memory keys to merge (at least 2). Originals are deleted after merge."
    ),
    new_content: str = Field(
        description="The combined/merged content for the new consolidated memory entry."
    ),
    scope: str = Field(
        default="project",
        description='Scope of the memories: "global", "project", "session", or "agent"',
    ),
    new_key: Optional[str] = Field(
        default=None,
        description="Key for the merged entry. Defaults to the first key in the list if omitted.",
    ),
    memory_type: str = Field(
        default="project",
        description='Type of the merged memory: "user", "feedback", "project", or "reference".',
    ),
    tags: str = Field(
        default="",
        description="Comma-separated tags for the merged memory entry.",
    ),
) -> Dict[str, Any]:
    """Merge two or more memory entries into one.

    Provide the keys to merge and the combined content.
    Original entries are deleted; a new entry is created with new_key
    (or the first key if omitted).
    Returns { success, merged_from, new_key, scope }.
    """
    from cli_agent_orchestrator.services.memory_service import (
        MemoryDisabledError,
        MemoryService,
    )

    if len(keys) < 2:
        return {"success": False, "error": "At least 2 keys are required for consolidation."}

    resolved_key = new_key if isinstance(new_key, str) else keys[0]

    try:
        service = MemoryService()
        terminal_context = _get_terminal_context_from_env()

        # Step 1: Store the merged entry (upserts if key already exists)
        await service.store(
            content=new_content,
            scope=scope,
            memory_type=memory_type,
            key=resolved_key,
            tags=tags,
            terminal_context=terminal_context,
        )

        # Step 2: Delete all original keys (except the resolved_key if it was reused)
        keys_to_delete = [k for k in keys if k != resolved_key]
        deleted_keys = []
        errors = []
        for k in keys_to_delete:
            try:
                deleted = await service.forget(
                    key=k, scope=scope, terminal_context=terminal_context
                )
                if deleted:
                    deleted_keys.append(k)
                else:
                    errors.append(f"{k} not found")
            except Exception as e:
                errors.append(f"{k} failed: {e}")

        # success only if ALL deletes succeeded
        success = len(deleted_keys) == len(keys_to_delete)
        return {
            "success": success,
            "merged_from": keys,
            "deleted_originals": deleted_keys,
            "errors": errors if errors else None,
            "new_key": resolved_key,
            "scope": scope,
        }
    except MemoryDisabledError:
        return {"success": False, "disabled": True, "error": MEMORY_DISABLED_MESSAGE}
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
async def session_context(
    session_name: Optional[str] = Field(
        default=None,
        description="CAO session name. Defaults to current session (CAO_SESSION_NAME env var).",
    ),
    limit: int = Field(
        default=20,
        description="Maximum number of recent events to return.",
    ),
) -> Dict[str, Any]:
    """Return the event timeline for a CAO session.

    Includes task_started, task_completed, handoff_returned, memory_stored events.
    Use this to understand what previous agents did before taking over.
    """
    from cli_agent_orchestrator.clients.database import get_session_timeline

    # Resolve session_name: use parameter if provided, else CAO_SESSION_NAME env var
    resolved_name = session_name if isinstance(session_name, str) else None
    if not resolved_name:
        resolved_name = os.environ.get("CAO_SESSION_NAME", "")
    if not resolved_name:
        return {"success": False, "error": "No session_name provided and CAO_SESSION_NAME not set"}

    # Resolve limit (FieldInfo guard for direct calls)
    resolved_limit = limit if isinstance(limit, int) and 0 < limit <= 1000 else 20

    try:
        events = get_session_timeline(resolved_name, limit=resolved_limit)
        return {
            "success": True,
            "session_name": resolved_name,
            "events": [
                {
                    "event_type": e["event_type"],
                    "terminal_id": e["terminal_id"],
                    "provider": e["provider"],
                    "summary": e["summary"],
                    "created_at": str(e["created_at"]) if e["created_at"] else None,
                }
                for e in events
            ],
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def main():
    """Main entry point for the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()

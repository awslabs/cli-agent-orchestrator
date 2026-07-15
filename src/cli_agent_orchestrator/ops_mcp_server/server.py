"""CAO operations MCP server implementation."""

from typing import Annotated, Any, Dict, List, Optional

import requests  # type: ignore[import-untyped]
from fastmcp import FastMCP
from pydantic import Field

from cli_agent_orchestrator.constants import API_BASE_URL
from cli_agent_orchestrator.ops_mcp_server.models import (
    InstallResult,
    LaunchResult,
    ProfileListResult,
    SendMessageResult,
    SessionListResult,
)
from cli_agent_orchestrator.utils.terminal import generate_session_name

JsonDict = Dict[str, Any]

mcp = FastMCP(
    "cao-ops-mcp",
    instructions="""
    # CAO Operations MCP Server

    Manage CLI Agent Orchestrator profiles and sessions from outside a CAO session.
    Requires the CAO API server running at localhost:9889.

    ## Typical Workflow
    1. list_profiles to inspect available profiles
    2. get_profile_details to review a profile's full prompt and metadata
    3. install_profile to install a profile for a target provider
    4. launch_session to start a new CAO session
    5. send_session_message to deliver a prompt to a running terminal
    6. get_terminal_status to poll a worker until it finishes a task
    7. get_terminal_output to read a worker's result (or review its files/git diff)
    8. read_session_output to read a terminal's captured output by session name
    9. get_session_info or list_sessions to monitor overall progress
    10. shutdown_session to clean up when done
    """,
)


def _response_detail(response: requests.Response) -> str:
    """Extract the most useful error detail from an API response."""
    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        return text or f"HTTP {response.status_code}"

    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("message")
        if isinstance(detail, str) and detail:
            return detail

    text = response.text.strip()
    return text or f"HTTP {response.status_code}"


def _request_json(
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    json: Optional[Any] = None,
    operation: str,
) -> tuple[Optional[Any], Optional[str]]:
    """Execute an API request and return either JSON data or an error message."""
    try:
        response = requests.request(
            method,
            f"{API_BASE_URL}{path}",
            params=params,
            json=json,
        )
    except requests.RequestException as exc:
        return None, f"{operation} failed: {exc}"

    if response.status_code >= 400:
        return None, f"{operation} failed: {_response_detail(response)}"

    try:
        return response.json(), None
    except ValueError as exc:
        return None, f"{operation} failed: invalid JSON response ({exc})"


def _serialize_allowed_tools(allowed_tools: Optional[List[str]]) -> Optional[str]:
    """Serialize allowed tools for the session creation API."""
    if not allowed_tools:
        return None
    return ",".join(allowed_tools)


async def _launch_session_impl(
    agent_profile: str,
    provider: Optional[str] = None,
    session_name: Optional[str] = None,
    working_directory: Optional[str] = None,
    allowed_tools: Optional[List[str]] = None,
) -> LaunchResult:
    """Create a new CAO session and return the session identifiers."""
    resolved_session_name = session_name or generate_session_name()
    params: Dict[str, Any] = {
        "agent_profile": agent_profile,
        "session_name": resolved_session_name,
    }
    if provider is not None:
        params["provider"] = provider
    if working_directory:
        params["working_directory"] = working_directory

    serialized_allowed_tools = _serialize_allowed_tools(allowed_tools)
    if serialized_allowed_tools:
        params["allowed_tools"] = serialized_allowed_tools

    session_data, error = _request_json(
        "post", "/sessions", params=params, operation="Launch session"
    )
    if error:
        return LaunchResult(
            success=False,
            message=error,
            session_name=resolved_session_name,
            terminal_id=None,
        )

    if not isinstance(session_data, dict) or "id" not in session_data:
        return LaunchResult(
            success=False,
            message="Launch session failed: invalid session response",
            session_name=resolved_session_name,
            terminal_id=None,
        )

    terminal_id = str(session_data["id"])
    return LaunchResult(
        success=True,
        message=f"Session '{resolved_session_name}' launched successfully",
        session_name=resolved_session_name,
        terminal_id=terminal_id,
    )


@mcp.tool()
async def list_profiles() -> ProfileListResult:
    """List available agent profiles.

    Scans built-in store, local store, and all configured provider agent
    directories. Profiles are deduplicated by name with source metadata.

    Returns:
        ProfileListResult with success status and profiles list
    """
    data, error = _request_json("get", "/agents/profiles", operation="List profiles")
    if error:
        return ProfileListResult(success=False, message=error)
    if isinstance(data, list):
        return ProfileListResult(success=True, profiles=data)
    return ProfileListResult(
        success=False,
        message="List profiles failed: invalid response payload",
    )


@mcp.tool()
async def get_profile_details(
    name: Annotated[str, Field(description="The agent profile name to inspect")],
) -> JsonDict:
    """Get the full parsed content of a specific agent profile.

    Returns all AgentProfile fields (name, description, system_prompt, role,
    provider, allowedTools, mcpServers, model) with None-valued fields excluded.

    Args:
        name: Agent profile name to inspect

    Returns:
        Dict with profile fields, or {"success": False, "message": ...} on error
    """
    data, error = _request_json(
        "get",
        f"/agents/profiles/{name}",
        operation=f"Get profile details for '{name}'",
    )
    if error:
        return {"success": False, "message": error}
    if isinstance(data, dict):
        return data
    return {"success": False, "message": "Get profile details failed: invalid response payload"}


@mcp.tool()
async def install_profile(
    source: Annotated[str, Field(description="Agent name or https:// URL to install")],
    provider: Annotated[
        Optional[str],
        Field(
            description=(
                "Target provider for the installed profile. Omit to honour the "
                "profile's frontmatter provider, falling back to the default."
            )
        ),
    ] = None,
    env_vars: Annotated[
        Optional[Dict[str, str]],
        Field(description="Optional environment variables to inject before install"),
    ] = None,
) -> InstallResult:
    """Install an agent profile for a target provider.

    ## Source Resolution

    Remote callers (HTTP API / MCP) may install by either:
    1. https:// URL from an allow-listed host (``github.com``,
       ``raw.githubusercontent.com`` by default; extend via the
       ``CAO_PROFILE_ALLOWED_HOSTS`` env var on ``cao-server``).
    2. Profile name matching ``[A-Za-z0-9_-]{1,64}`` — looked up in the local
       store, provider dirs, then the built-in store.

    Installing by local filesystem path is CLI-only and is rejected from the
    HTTP API and this MCP tool.

    ## Provider Config

    - kiro_cli: JSON config written to the provider's agents directory
    - copilot_cli: frontmatter markdown written to the Copilot agents directory
    - claude_code, codex: context file only, no provider-specific config

    Args:
        source: Agent name or https:// URL from an allow-listed host
        provider: Target provider. Precedence: explicit value > the profile's
            frontmatter ``provider:`` key > the server default (kiro_cli)
        env_vars: Optional env vars written to the managed .env before install

    Returns:
        InstallResult with success status, file paths, and unresolved env vars
    """
    body: Dict[str, Any] = {"source": source}
    if provider is not None:
        body["provider"] = provider
    if env_vars:
        body["env_vars"] = env_vars

    data, error = _request_json(
        "post",
        "/agents/profiles/install",
        json=body,
        operation=f"Install profile '{source}'",
    )
    if error:
        return InstallResult(success=False, message=error)
    if isinstance(data, dict):
        return InstallResult(**data)
    return InstallResult(success=False, message="Install profile failed: invalid response payload")


@mcp.tool()
async def launch_session(
    agent_profile: Annotated[str, Field(description="The agent profile to launch")],
    provider: Annotated[
        Optional[str],
        Field(description="The provider to use for the launched session"),
    ] = None,
    session_name: Annotated[
        Optional[str],
        Field(description="Optional custom CAO session name"),
    ] = None,
    working_directory: Annotated[
        Optional[str],
        Field(description="Optional working directory for the launched session"),
    ] = None,
    allowed_tools: Annotated[
        Optional[List[str]],
        Field(description="Optional list of allowed tool restrictions"),
    ] = None,
) -> LaunchResult:
    """Create a new CAO session with the given provider and agent profile.

    Returns immediately with session_name and terminal_id. Use
    send_session_message to deliver an initial prompt once the session is
    running, and get_session_info or list_sessions to monitor progress.

    Args:
        agent_profile: Agent profile for the new session
        provider: CLI provider (default: profile provider or kiro_cli)
        session_name: Optional custom session name (auto-generated if omitted)
        working_directory: Optional working directory for the session
        allowed_tools: Optional list of tool restrictions

    Returns:
        LaunchResult with success status, session_name, and terminal_id
    """
    return await _launch_session_impl(
        agent_profile=agent_profile,
        provider=provider,
        session_name=session_name,
        working_directory=working_directory,
        allowed_tools=allowed_tools,
    )


@mcp.tool()
async def send_session_message(
    terminal_id: Annotated[str, Field(description="The terminal ID to deliver the message to")],
    message: Annotated[str, Field(description="The message text to deliver")],
) -> SendMessageResult:
    """Queue a message for delivery to a running CAO terminal via the inbox service.

    Messages are delivered by the CAO inbox service when the terminal reaches
    IDLE or COMPLETED status. Use get_session_info to retrieve terminal IDs
    from an active session.

    Args:
        terminal_id: Target terminal ID (from launch_session or get_session_info)
        message: Message text to deliver

    Returns:
        SendMessageResult with success status and target terminal_id
    """
    _, error = _request_json(
        "post",
        f"/terminals/{terminal_id}/inbox/messages",
        params={"sender_id": "cao-ops-mcp", "message": message},
        operation=f"Send message to terminal '{terminal_id}'",
    )
    if error:
        return SendMessageResult(success=False, message=error, terminal_id=terminal_id)
    return SendMessageResult(
        success=True,
        message=f"Message queued for terminal '{terminal_id}'",
        terminal_id=terminal_id,
    )


def _read_session_output_impl(
    terminal_id: Optional[str],
    session_name: Optional[str],
    mode: Optional[str],
    max_chars: Optional[int],
) -> JsonDict:
    """Resolve a terminal and return its captured output (sync; mirrors other helpers)."""
    normalized = (mode or "full").lower()
    if normalized not in ("full", "last"):
        return {"success": False, "message": f"Invalid mode '{mode}'; expected 'full' or 'last'"}

    resolved_terminal_id = terminal_id
    if not resolved_terminal_id:
        if not session_name:
            return {"success": False, "message": "Provide either terminal_id or session_name"}
        info, error = _request_json(
            "get",
            f"/sessions/{session_name}",
            operation=f"Resolve terminals for session '{session_name}'",
        )
        if error:
            return {"success": False, "message": error}
        if not isinstance(info, dict):
            return {
                "success": False,
                "message": f"Session '{session_name}' returned an invalid response payload",
            }
        terminals = info.get("terminals", [])
        if not isinstance(terminals, list) or any(
            not isinstance(terminal, dict) for terminal in terminals
        ):
            return {
                "success": False,
                "message": f"Session '{session_name}' returned an invalid terminals payload",
            }
        if len(terminals) == 1:
            terminal = terminals[0]
            if not terminal.get("id"):
                return {
                    "success": False,
                    "message": f"Session '{session_name}' returned a terminal without an id",
                }
            resolved_terminal_id = str(terminal["id"])
        elif not terminals:
            return {"success": False, "message": f"Session '{session_name}' has no terminals"}
        else:
            return {
                "success": False,
                "message": (
                    f"Session '{session_name}' has {len(terminals)} terminals; "
                    "specify terminal_id"
                ),
                "terminals": terminals,
            }

    data, error = _request_json(
        "get",
        f"/terminals/{resolved_terminal_id}/output",
        params={"mode": normalized},
        operation=f"Read output for terminal '{resolved_terminal_id}'",
    )
    if error:
        return {"success": False, "message": error}
    if not isinstance(data, dict) or not isinstance(data.get("output"), str):
        return {"success": False, "message": "Read output failed: invalid response payload"}

    output = data["output"]
    total_chars = len(output)
    truncated = False
    if max_chars is not None and max_chars > 0 and total_chars > max_chars:
        output = output[-max_chars:]
        truncated = True

    return {
        "success": True,
        "terminal_id": resolved_terminal_id,
        "mode": normalized,
        "output": output,
        "truncated": truncated,
        "total_chars": total_chars,
    }


@mcp.tool()
async def read_session_output(
    terminal_id: Annotated[
        Optional[str],
        Field(
            description="Target terminal ID (from list_sessions / get_session_info). "
            "Primary key; either terminal_id or session_name is required."
        ),
    ] = None,
    session_name: Annotated[
        Optional[str],
        Field(
            description="CAO session name; convenience alternative to terminal_id. "
            "Resolved to a terminal when the session has exactly one; if it has more "
            "than one, the terminal list is returned and terminal_id is required."
        ),
    ] = None,
    mode: Annotated[
        str,
        Field(
            description="'full' (default) returns the raw rolling buffer: deterministic and "
            "best for scrollback/debugging. 'last' returns the provider-extracted final "
            "response: best for a completed worker's final message, but can be flaky on "
            "redraw-heavy TUIs."
        ),
    ] = "full",
    max_chars: Annotated[
        Optional[int],
        Field(
            description="Optional cap: return only the last max_chars of output "
            "(guards against flooding the caller's context). Truncation is flagged "
            "in the result. Values <= 0 are treated as no cap."
        ),
    ] = None,
) -> JsonDict:
    """Read a CAO terminal's captured scrollback with a deterministic full-buffer default.

    Defaults to mode='full' because raw rolling-buffer output is deterministic and
    best for scrollback/debugging. Use get_terminal_output, which defaults to
    mode='last', to read a completed worker's provider-extracted final message;
    'last' can be flaky on redraw-heavy TUIs. This tool adds session_name addressing
    (when the session has exactly one terminal) and max_chars tail-capping, which
    get_terminal_output does not provide.

    Args:
        terminal_id: Target terminal ID (primary key)
        session_name: Convenience alternative; resolved to a terminal when unambiguous
        mode: 'full' (default, rolling buffer) or 'last' (provider-extracted)
        max_chars: Optional tail cap on returned characters

    Returns:
        Dict {success, terminal_id, mode, output, truncated, total_chars}, or
        {success: False, message[, terminals]} on error / ambiguous session
    """
    return _read_session_output_impl(terminal_id, session_name, mode, max_chars)


@mcp.tool()
async def get_terminal_status(
    terminal_id: Annotated[str, Field(description="The terminal ID to inspect")],
) -> JsonDict:
    """Get a single terminal's live status and metadata.

    Use this to poll a worker an external supervisor launched: it returns the
    current status (one of unknown / idle / processing / completed /
    waiting_user_answer / error) so the supervisor knows when a delegated task
    has finished before reading its output.

    Args:
        terminal_id: Target terminal ID (from launch_session or get_session_info)

    Returns:
        Dict with id, name, provider, session_name, agent_profile, status,
        last_active — or {"success": False, "message": ...} on error
    """
    data, error = _request_json(
        "get",
        f"/terminals/{terminal_id}",
        operation=f"Get terminal status for '{terminal_id}'",
    )
    if error:
        return {"success": False, "message": error}
    if isinstance(data, dict):
        return data
    return {"success": False, "message": "Get terminal status failed: invalid response payload"}


@mcp.tool()
async def get_terminal_output(
    terminal_id: Annotated[str, Field(description="The terminal ID to read output from")],
    mode: Annotated[
        str,
        Field(
            description=(
                "'last' (default) returns the provider-extracted final response: best for "
                "a completed worker's final message, but can be flaky on redraw-heavy "
                "TUIs. 'full' returns the raw rolling buffer: deterministic and best for "
                "scrollback/debugging."
            )
        ),
    ] = "last",
) -> JsonDict:
    """Read a worker terminal's output with a completed-message-oriented default.

    Defaults to mode='last' because this tool is optimized for reading a completed
    worker's provider-extracted final message, though redraw-heavy TUIs can make
    extraction flaky. For deterministic raw rolling-buffer scrollback/debugging,
    use read_session_output, which defaults to mode='full' and also supports
    session_name addressing and max_chars tail-capping. For code review, prefer
    inspecting the worker's files / git diff directly rather than relying solely
    on terminal text.

    Args:
        terminal_id: Target terminal ID
        mode: 'last' (final response, default) or 'full' (rolling buffer)

    Returns:
        Dict with output and mode, or {"success": False, "message": ...} on error
    """
    normalized = (mode or "last").lower()
    if normalized not in ("last", "full"):
        return {
            "success": False,
            "message": f"Get terminal output failed: mode must be 'last' or 'full', got '{mode}'",
        }
    data, error = _request_json(
        "get",
        f"/terminals/{terminal_id}/output",
        params={"mode": normalized},
        operation=f"Get terminal output for '{terminal_id}'",
    )
    if error:
        return {"success": False, "message": error}
    if isinstance(data, dict):
        return data
    return {"success": False, "message": "Get terminal output failed: invalid response payload"}


@mcp.tool()
async def list_sessions() -> SessionListResult:
    """List active CAO sessions with terminal counts and statuses.

    Returns:
        SessionListResult with success status and sessions list
    """
    data, error = _request_json("get", "/sessions", operation="List sessions")
    if error:
        return SessionListResult(success=False, message=error)
    if isinstance(data, list):
        return SessionListResult(success=True, sessions=data)
    return SessionListResult(
        success=False,
        message="List sessions failed: invalid response payload",
    )


@mcp.tool()
async def get_session_info(
    session_name: Annotated[str, Field(description="The CAO session name to inspect")],
) -> JsonDict:
    """Get detailed session metadata including per-terminal status.

    Returns session fields along with a terminals array containing each
    terminal's status, provider, profile, and last activity.

    Args:
        session_name: CAO session name to inspect

    Returns:
        Dict with session fields, or {"success": False, "message": ...} on error
    """
    data, error = _request_json(
        "get",
        f"/sessions/{session_name}",
        operation=f"Get session info for '{session_name}'",
    )
    if error:
        return {"success": False, "message": error}
    if isinstance(data, dict):
        return data
    return {"success": False, "message": "Get session info failed: invalid response payload"}


@mcp.tool()
async def shutdown_session(
    session_name: Annotated[str, Field(description="The CAO session name to shut down")],
) -> JsonDict:
    """Cleanly shut down a CAO session.

    Exits all providers, kills the tmux session, and removes database records.

    Args:
        session_name: CAO session name to shut down

    Returns:
        Dict with success status and cleanup details, or failure dict on error
    """
    data, error = _request_json(
        "delete",
        f"/sessions/{session_name}",
        operation=f"Shutdown session '{session_name}'",
    )
    if error:
        return {"success": False, "message": error}
    if isinstance(data, dict):
        return data
    return {"success": False, "message": "Shutdown session failed: invalid response payload"}


def main() -> None:
    """Run the operations MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()

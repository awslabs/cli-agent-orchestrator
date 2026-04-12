"""CAO operations MCP server implementation."""

import asyncio
from typing import Annotated, Any, Dict, List, Optional

import requests  # type: ignore[import-untyped]
from fastmcp import FastMCP
from pydantic import Field

from cli_agent_orchestrator.constants import API_BASE_URL, DEFAULT_PROVIDER, TERMINAL_READY_TIMEOUT
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.ops_mcp_server.models import (
    InstallResult,
    LaunchResult,
    ProfileListResult,
    SessionListResult,
)
from cli_agent_orchestrator.utils.terminal import generate_session_name, wait_until_terminal_status

JsonDict = Dict[str, Any]

mcp = FastMCP(
    "cao-ops-mcp",
    instructions="""
    # CAO Operations MCP Server

    Manage CLI Agent Orchestrator profiles and sessions from outside a CAO session.
    Requires the CAO API server running at localhost:9889.

    ## Typical Workflow
    1. discover_profiles to inspect available profiles
    2. get_profile_details to review a profile's full prompt and metadata
    3. install_profile to install a profile for a target provider
    4. launch_session to start a new CAO session with an optional initial prompt
    5. get_session_info or list_sessions to monitor progress
    6. shutdown_session to clean up when done
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
    operation: str,
) -> tuple[Optional[Any], Optional[str]]:
    """Execute an API request and return either JSON data or an error message."""
    try:
        response = requests.request(method, f"{API_BASE_URL}{path}", params=params)
    except requests.RequestException as exc:
        return None, f"{operation} failed: {exc}"

    if response.status_code >= 400:
        return None, f"{operation} failed: {_response_detail(response)}"

    try:
        return response.json(), None
    except ValueError as exc:
        return None, f"{operation} failed: invalid JSON response ({exc})"


def _serialize_env_vars(env_vars: Optional[Dict[str, str]]) -> Optional[str]:
    """Serialize env var mappings into the API's comma-separated format."""
    if not env_vars:
        return None
    return ",".join(f"{key}={value}" for key, value in env_vars.items())


def _serialize_allowed_tools(allowed_tools: Optional[List[str]]) -> Optional[str]:
    """Serialize allowed tools for the session creation API."""
    if not allowed_tools:
        return None
    return ",".join(allowed_tools)


def _install_result_from_error(message: str) -> InstallResult:
    """Build a failed InstallResult."""
    return InstallResult(success=False, message=message)


async def _launch_session_impl(
    agent_profile: str,
    prompt: Optional[str] = None,
    provider: str = DEFAULT_PROVIDER,
    session_name: Optional[str] = None,
    working_directory: Optional[str] = None,
    allowed_tools: Optional[List[str]] = None,
) -> LaunchResult:
    """Launch a session and optionally deliver an initial prompt.

    If readiness or prompt delivery fails after session creation, the returned
    failure still includes ``session_name`` and ``terminal_id`` so callers can
    decide whether to inspect or explicitly shut down the partially launched
    session.
    """
    resolved_session_name = session_name or generate_session_name()
    params: Dict[str, Any] = {
        "provider": provider,
        "agent_profile": agent_profile,
        "session_name": resolved_session_name,
    }
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

    if prompt:
        # The underlying wait helper is synchronous; move it off the event loop
        # so prompt-bearing launches do not block other async MCP work.
        is_ready = await asyncio.to_thread(
            wait_until_terminal_status,
            terminal_id,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=TERMINAL_READY_TIMEOUT,
        )
        if not is_ready:
            return LaunchResult(
                success=False,
                message=(
                    f"Terminal {terminal_id} did not reach ready status within "
                    f"{int(TERMINAL_READY_TIMEOUT)} seconds"
                ),
                session_name=resolved_session_name,
                terminal_id=terminal_id,
            )

        await asyncio.sleep(2)

        _, prompt_error = _request_json(
            "post",
            f"/terminals/{terminal_id}/input",
            params={"message": prompt},
            operation="Send initial prompt",
        )
        if prompt_error:
            return LaunchResult(
                success=False,
                message=prompt_error,
                session_name=resolved_session_name,
                terminal_id=terminal_id,
            )

    return LaunchResult(
        success=True,
        message=f"Session '{resolved_session_name}' launched successfully",
        session_name=resolved_session_name,
        terminal_id=terminal_id,
    )


@mcp.tool()
async def discover_profiles() -> ProfileListResult:
    """List available agent profiles.

    Scans built-in store, local store, and all configured provider agent
    directories. Profiles are deduplicated by name with source metadata.

    Returns:
        ProfileListResult with success status and profiles list
    """
    data, error = _request_json("get", "/agents/profiles", operation="Discover profiles")
    if error:
        return ProfileListResult(success=False, message=error)
    if isinstance(data, list):
        return ProfileListResult(success=True, profiles=data)
    return ProfileListResult(
        success=False,
        message="Discover profiles failed: invalid response payload",
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
    source: Annotated[str, Field(description="Agent name, file path, or URL to install")],
    provider: Annotated[
        str,
        Field(description="Target provider for the installed profile"),
    ] = DEFAULT_PROVIDER,
    env_vars: Annotated[
        Optional[Dict[str, str]],
        Field(description="Optional environment variables to inject before install"),
    ] = None,
) -> InstallResult:
    """Install an agent profile for a target provider.

    ## Source Resolution

    The source is resolved in order:
    1. URL (http:// or https://) — downloaded into the local agent store
    2. Existing file on disk — copied into the local agent store
    3. Agent name — looked up in local store, provider dirs, then built-in store

    Path resolution checks the current working directory, so a bare agent
    name that collides with a file or directory in CWD will route to path
    resolution and fail. Use an explicit ``./`` prefix for file paths, or
    ensure agent names do not collide with CWD contents.

    ## Provider Config

    - q_cli, kiro_cli: JSON config written to the provider's agents directory
    - copilot_cli: frontmatter markdown written to the Copilot agents directory
    - claude_code, codex: context file only, no provider-specific config

    Args:
        source: Agent name, file path, or URL
        provider: Target provider (default: kiro_cli)
        env_vars: Optional env vars written to the managed .env before install

    Returns:
        InstallResult with success status, file paths, and unresolved env vars
    """
    params: Dict[str, Any] = {"source": source, "provider": provider}
    serialized_env_vars = _serialize_env_vars(env_vars)
    if serialized_env_vars:
        params["env_vars"] = serialized_env_vars

    data, error = _request_json(
        "post",
        "/agents/profiles/install",
        params=params,
        operation=f"Install profile '{source}'",
    )
    if error:
        return _install_result_from_error(error)
    if isinstance(data, dict):
        return InstallResult(**data)
    return _install_result_from_error("Install profile failed: invalid response payload")


@mcp.tool()
async def launch_session(
    agent_profile: Annotated[str, Field(description="The agent profile to launch")],
    prompt: Annotated[
        Optional[str],
        Field(description="Optional initial prompt to send after the session becomes ready"),
    ] = None,
    provider: Annotated[
        str,
        Field(description="The provider to use for the launched session"),
    ] = DEFAULT_PROVIDER,
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
    """Create a new CAO session and optionally deliver an initial prompt.

    Creates a fresh session with the given provider and agent profile. If a
    prompt is provided, waits for the terminal to reach IDLE or COMPLETED
    before sending it, then returns immediately without waiting for the
    agent's response.

    ## Usage

    1. Create a new CAO session with the specified provider and profile
    2. If prompt is provided: wait for readiness, then send the prompt
    3. Return with session_name and terminal_id (non-blocking)

    Use get_session_info or list_sessions to monitor progress, and
    shutdown_session to clean up.

    ## Partial Launch

    On readiness timeout or prompt delivery failure, the returned
    LaunchResult still carries session_name and terminal_id so the caller
    can inspect or explicitly shut down the partially launched session.

    Args:
        agent_profile: Agent profile for the new session
        prompt: Optional initial prompt sent after the session becomes ready
        provider: CLI provider (default: kiro_cli)
        session_name: Optional custom session name (auto-generated if omitted)
        working_directory: Optional working directory for the session
        allowed_tools: Optional list of tool restrictions

    Returns:
        LaunchResult with success status, session_name, and terminal_id
    """
    return await _launch_session_impl(
        agent_profile=agent_profile,
        prompt=prompt,
        provider=provider,
        session_name=session_name,
        working_directory=working_directory,
        allowed_tools=allowed_tools,
    )


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

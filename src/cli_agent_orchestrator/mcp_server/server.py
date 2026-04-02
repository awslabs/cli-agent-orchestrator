"""CLI Agent Orchestrator MCP Server implementation.

Provides tools for agent delegation, bead/epic management, and orchestration.
All delegation (handoff/assign) automatically creates beads for traceability.
"""

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from fastmcp import FastMCP
from pydantic import Field

from cli_agent_orchestrator.constants import API_BASE_URL, DEFAULT_PROVIDER
from cli_agent_orchestrator.mcp_server.models import HandoffResult
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.utils.terminal import generate_session_name, wait_until_terminal_status

logger = logging.getLogger(__name__)

# Create MCP server
mcp = FastMCP(
    "cao-mcp-server",
    instructions="""
    # CLI Agent Orchestrator MCP Server

    This server provides tools for multi-agent orchestration:
    - **handoff/assign**: Delegate tasks to other agents (automatically creates beads for tracking)
    - **Bead management**: Create, list, close beads and epics
    - **Session management**: List sessions, read output, kill sessions
    - **send_message**: Send messages between agents via inbox

    All work is tracked via beads for full traceability.
    """,
)


def _api_get(path: str, params: dict = None) -> Any:
    """GET request to CAO API."""
    res = requests.get(f"{API_BASE_URL}{path}", params=params, timeout=10)
    res.raise_for_status()
    return res.json()


def _api_post(path: str, json: dict = None, params: dict = None) -> Any:
    """POST request to CAO API."""
    res = requests.post(f"{API_BASE_URL}{path}", json=json, params=params, timeout=10)
    res.raise_for_status()
    return res.json()


def _api_delete(path: str) -> Any:
    """DELETE request to CAO API."""
    res = requests.delete(f"{API_BASE_URL}{path}", timeout=10)
    res.raise_for_status()
    return res.json()


def _create_terminal(agent_profile: str) -> Tuple[str, str]:
    """Create a new terminal with the specified agent profile."""
    provider = DEFAULT_PROVIDER
    current_terminal_id = os.environ.get("CAO_TERMINAL_ID")
    if current_terminal_id:
        terminal_metadata = _api_get(f"/terminals/{current_terminal_id}")
        provider = terminal_metadata["provider"]
        session_name = terminal_metadata["session_name"]
        terminal = _api_post(
            f"/sessions/{session_name}/terminals",
            params={"provider": provider, "agent_profile": agent_profile, "parent_terminal_id": current_terminal_id},
        )
    else:
        session_name = generate_session_name()
        terminal = _api_post(
            "/sessions",
            params={"provider": provider, "agent_profile": agent_profile, "session_name": session_name},
        )
    return terminal["id"], provider


def _create_bead_for_task(title: str, description: str = "") -> Optional[str]:
    """Create a bead to track a delegated task. Returns bead_id or None."""
    try:
        result = _api_post("/api/tasks", json={"title": title, "description": description, "priority": 2})
        return result.get("id")
    except Exception as e:
        logger.warning(f"Failed to create tracking bead: {e}")
        return None


def _close_bead(bead_id: str) -> None:
    """Close a bead after task completion."""
    try:
        _api_post(f"/api/tasks/{bead_id}/close")
    except Exception as e:
        logger.warning(f"Failed to close bead {bead_id}: {e}")


def _send_direct_input(terminal_id: str, message: str) -> None:
    """Send input directly to a terminal."""
    requests.post(f"{API_BASE_URL}/terminals/{terminal_id}/input", params={"message": message}).raise_for_status()


def _send_to_inbox(receiver_id: str, message: str) -> Dict[str, Any]:
    """Send message to another terminal's inbox."""
    sender_id = os.getenv("CAO_TERMINAL_ID")
    if not sender_id:
        raise ValueError("CAO_TERMINAL_ID not set - cannot determine sender")
    return _api_post(
        f"/terminals/{receiver_id}/inbox/messages",
        params={"sender_id": sender_id, "message": message},
    )


# ==================== Delegation Tools (bead-aware) ====================


@mcp.tool()
async def handoff(
    agent_profile: str = Field(description='Agent profile to hand off to (e.g., "developer")'),
    message: str = Field(description="The task/message to send to the target agent"),
    timeout: int = Field(default=600, description="Max wait time in seconds", ge=1, le=3600),
) -> HandoffResult:
    """Hand off a task to another agent and wait for completion.

    Creates a bead to track the task, delegates to the agent, waits for
    completion, then closes the bead. Returns the agent's output.
    """
    start_time = time.time()
    bead_id = None

    try:
        # Create tracking bead
        bead_id = _create_bead_for_task(f"Handoff: {message[:80]}", message)

        # Create terminal
        terminal_id, provider = _create_terminal(agent_profile)

        # Wait for IDLE
        if not wait_until_terminal_status(terminal_id, TerminalStatus.IDLE, timeout=30.0):
            return HandoffResult(
                success=False,
                message=f"Terminal did not reach IDLE within 30s",
                output=None, terminal_id=terminal_id,
            )

        await asyncio.sleep(2)
        _send_direct_input(terminal_id, message)

        # Wait for completion
        if not wait_until_terminal_status(terminal_id, TerminalStatus.COMPLETED, timeout=timeout, polling_interval=1.0):
            return HandoffResult(
                success=False,
                message=f"Handoff timed out after {timeout}s",
                output=None, terminal_id=terminal_id,
            )

        # Get output
        output_data = _api_get(f"/terminals/{terminal_id}/output", params={"mode": "last"})
        output = output_data["output"]

        # Close bead on success
        if bead_id:
            _close_bead(bead_id)

        # Cleanup terminal
        try:
            requests.post(f"{API_BASE_URL}/terminals/{terminal_id}/exit").raise_for_status()
        except Exception:
            pass

        return HandoffResult(
            success=True,
            message=f"Handed off to {agent_profile} ({provider}) in {time.time() - start_time:.1f}s. Bead: {bead_id}",
            output=output,
            terminal_id=terminal_id,
        )

    except Exception as e:
        return HandoffResult(success=False, message=f"Handoff failed: {e}", output=None, terminal_id=None)


@mcp.tool()
async def assign(
    agent_profile: str = Field(description='Agent profile for the worker (e.g., "developer")'),
    message: str = Field(description="Task message. Include callback instructions with your terminal ID."),
) -> Dict[str, Any]:
    """Assign a task to another agent without blocking.

    Creates a bead for tracking, spawns the agent, sends the task.
    Returns bead_id and terminal_id for monitoring.

    Include callback instructions in the message so the worker can report back.
    Your terminal ID is in the CAO_TERMINAL_ID environment variable.
    """
    try:
        bead_id = _create_bead_for_task(f"Assign: {message[:80]}", message)
        terminal_id, _ = _create_terminal(agent_profile)
        _send_direct_input(terminal_id, message)

        return {
            "success": True,
            "terminal_id": terminal_id,
            "bead_id": bead_id,
            "message": f"Task assigned to {agent_profile} (terminal: {terminal_id}, bead: {bead_id})",
        }
    except Exception as e:
        return {"success": False, "terminal_id": None, "bead_id": None, "message": f"Assignment failed: {e}"}


@mcp.tool()
async def send_message(
    receiver_id: str = Field(description="Target terminal ID"),
    message: str = Field(description="Message content"),
) -> Dict[str, Any]:
    """Send a message to another terminal's inbox. Delivered when target is IDLE."""
    try:
        return _send_to_inbox(receiver_id, message)
    except Exception as e:
        return {"success": False, "error": str(e)}


# ==================== Session Management Tools ====================


@mcp.tool()
async def list_sessions() -> List[Dict[str, Any]]:
    """List all active CAO sessions with status, agent profile, and bead info."""
    try:
        return _api_get("/api/v2/sessions")
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool()
async def get_session_output(
    session_id: str = Field(description="Session ID to read output from"),
    lines: int = Field(default=100, description="Number of lines to return"),
) -> Dict[str, Any]:
    """Read recent terminal output from a session."""
    try:
        return _api_get(f"/api/v2/sessions/{session_id}/output", params={"lines": lines})
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def kill_session(
    session_id: str = Field(description="Session ID to terminate"),
) -> Dict[str, Any]:
    """Terminate a session and cleanup its bead assignment."""
    try:
        return _api_delete(f"/api/v2/sessions/{session_id}")
    except Exception as e:
        return {"error": str(e)}


# ==================== Bead Management Tools ====================


@mcp.tool()
async def list_beads(
    status: Optional[str] = Field(default=None, description="Filter by status: open, wip, closed"),
) -> List[Dict[str, Any]]:
    """List all beads, optionally filtered by status."""
    try:
        params = {}
        if status:
            params["status"] = status
        return _api_get("/api/tasks", params=params)
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool()
async def create_bead(
    title: str = Field(description="Bead title"),
    description: str = Field(default="", description="Bead description"),
    priority: int = Field(default=2, description="Priority 1-3 (1=critical)"),
) -> Dict[str, Any]:
    """Create a new bead (task)."""
    try:
        return _api_post("/api/tasks", json={"title": title, "description": description, "priority": priority})
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def create_epic(
    title: str = Field(description="Epic title"),
    steps: List[str] = Field(description="List of step titles for child beads"),
    sequential: bool = Field(default=True, description="If true, each step depends on the previous"),
    max_concurrent: int = Field(default=3, description="Max concurrent agents for parallel execution"),
) -> Dict[str, Any]:
    """Create an epic with child beads. If sequential, each step depends on the previous."""
    try:
        return _api_post("/api/v2/epics", json={
            "title": title, "steps": steps, "sequential": sequential, "max_concurrent": max_concurrent
        })
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def get_epic_status(
    epic_id: str = Field(description="Epic bead ID"),
) -> Dict[str, Any]:
    """Get epic progress: children, completion count, active agents, ready tasks."""
    try:
        return _api_get(f"/api/v2/epics/{epic_id}")
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def get_ready_beads(
    epic_id: Optional[str] = Field(default=None, description="Scope to an epic's children"),
) -> List[Dict[str, Any]]:
    """Get beads ready for assignment (no blockers). Optionally scoped to an epic."""
    try:
        if epic_id:
            return _api_get(f"/api/v2/epics/{epic_id}/ready")
        return _api_get("/api/tasks/next")
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool()
async def assign_bead(
    bead_id: str = Field(description="Bead ID to assign"),
    agent_profile: str = Field(description="Agent profile for the worker"),
    provider: str = Field(default="q_cli", description="CLI provider to use"),
) -> Dict[str, Any]:
    """Assign an existing bead to an agent — spawns a new session and starts work."""
    try:
        return _api_post(f"/api/v2/beads/{bead_id}/assign-agent", json={
            "agent_name": agent_profile, "provider": provider
        })
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def close_bead(
    bead_id: str = Field(description="Bead ID to close"),
) -> Dict[str, Any]:
    """Close a completed bead."""
    try:
        return _api_post(f"/api/tasks/{bead_id}/close")
    except Exception as e:
        return {"error": str(e)}


def main():
    """Main entry point for the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()

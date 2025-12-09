"""CLI Agent Orchestrator MCP Server implementation."""

import asyncio
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple

import requests
from fastmcp import FastMCP
from pydantic import Field

from cli_agent_orchestrator.constants import API_BASE_URL, DEFAULT_PROVIDER
from cli_agent_orchestrator.mcp_server.models import HandoffResult
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.utils.terminal import generate_session_name, wait_until_terminal_status

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


def _create_terminal(
    agent_profile: str,
    provider_override: Optional[str] = None,
    provider_args: Optional[str] = None,
    no_profile: bool = False,
) -> Tuple[str, str]:
    """Create a new terminal with the specified agent profile.

    Args:
        agent_profile: Agent profile for the terminal
        provider_override: Optional provider override (uses caller's provider if not set)
        provider_args: Optional extra CLI arguments to pass to the provider
        no_profile: Skip injecting agent profile (system prompt, MCP config)

    Returns:
        Tuple of (terminal_id, provider)

    Raises:
        Exception: If terminal creation fails
    """
    provider = provider_override or DEFAULT_PROVIDER

    # Get current terminal ID from environment - this becomes parent_id for the new terminal
    current_terminal_id = os.environ.get("CAO_TERMINAL_ID")
    if current_terminal_id:
        # Get terminal metadata via API
        response = requests.get(f"{API_BASE_URL}/terminals/{current_terminal_id}")
        response.raise_for_status()
        terminal_metadata = response.json()

        # Use override provider or inherit from parent
        if not provider_override:
            provider = terminal_metadata["provider"]
        session_name = terminal_metadata["session_name"]

        # Create new terminal in existing session with parent_id set
        params: Dict[str, Any] = {
            "provider": provider,
            "agent_profile": agent_profile,
            "parent_id": current_terminal_id,
        }
        if provider_args:
            params["provider_args"] = provider_args
        if no_profile:
            params["no_profile"] = "true"
        response = requests.post(
            f"{API_BASE_URL}/sessions/{session_name}/terminals",
            params=params,
        )
        response.raise_for_status()
        terminal = response.json()
    else:
        # Create new session with terminal (no parent)
        session_name = generate_session_name()
        params = {
            "provider": provider,
            "agent_profile": agent_profile,
            "session_name": session_name,
        }
        if provider_args:
            params["provider_args"] = provider_args
        if no_profile:
            params["no_profile"] = "true"
        response = requests.post(
            f"{API_BASE_URL}/sessions",
            params=params,
        )
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
    provider: Optional[str] = Field(
        default=None,
        description="Override the CLI provider (e.g., 'claude_code', 'q_cli'). Defaults to caller's provider.",
    ),
    provider_args: Optional[str] = Field(
        default=None,
        description="Extra CLI arguments to pass to the provider (e.g., '--dangerously-skip-permissions --verbose')",
    ),
    no_profile: bool = Field(
        default=False,
        description="Skip injecting agent profile (system prompt, MCP config). Useful when resuming sessions.",
    ),
) -> HandoffResult:
    """Hand off a task to another agent via CAO terminal and wait for completion.

    This tool allows handing off tasks to other agents by creating a new terminal
    in the same session. It sends the message, waits for completion, and captures the output.

    ## Usage

    Use this tool to hand off tasks to another agent and wait for the results.
    The tool will:
    1. Create a new terminal with the specified agent profile and provider
    2. Send the message to the terminal
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
        provider: Optional provider override

    Returns:
        HandoffResult with success status, message, and agent output
    """
    start_time = time.time()

    try:
        # Create terminal with optional provider override and args
        terminal_id, used_provider = _create_terminal(
            agent_profile, provider, provider_args, no_profile
        )

        # Wait for terminal to be IDLE before sending message
        if not wait_until_terminal_status(terminal_id, TerminalStatus.IDLE, timeout=30.0):
            return HandoffResult(
                success=False,
                message=f"Terminal {terminal_id} did not reach IDLE status within 30 seconds",
                output=None,
                terminal_id=terminal_id,
            )

        await asyncio.sleep(2)  # wait another 2s

        # Send message to terminal
        _send_direct_input(terminal_id, message)

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

        return HandoffResult(
            success=True,
            message=f"Successfully handed off to {agent_profile} ({used_provider}) in {execution_time:.2f}s",
            output=output,
            terminal_id=terminal_id,
        )

    except Exception as e:
        return HandoffResult(
            success=False, message=f"Handoff failed: {str(e)}", output=None, terminal_id=None
        )


@mcp.tool()
async def assign(
    agent_profile: str = Field(
        description='The agent profile for the worker agent (e.g., "developer", "analyst")'
    ),
    message: str = Field(
        description="The task message to send. The worker can use reply() tool to send results back automatically."
    ),
    provider: Optional[str] = Field(
        default=None,
        description="Override the CLI provider (e.g., 'claude_code', 'q_cli'). Defaults to caller's provider.",
    ),
    provider_args: Optional[str] = Field(
        default=None,
        description="Extra CLI arguments to pass to the provider (e.g., '--dangerously-skip-permissions --verbose')",
    ),
    no_profile: bool = Field(
        default=False,
        description="Skip injecting agent profile (system prompt, MCP config). Useful when resuming sessions.",
    ),
) -> Dict[str, Any]:
    """Assigns a task to another agent without blocking.

    The worker agent automatically has CAO_PARENT_TERMINAL_ID set, so it can use
    the reply() tool to send results back without needing to know the parent's terminal ID.

    Args:
        agent_profile: Agent profile for the worker terminal
        message: Task message to send
        provider: Optional provider override
        provider_args: Optional extra CLI arguments for the provider
        no_profile: Skip injecting agent profile

    Returns:
        Dict with success status, worker terminal_id, and message
    """
    try:
        # Create terminal with optional provider override and args
        terminal_id, used_provider = _create_terminal(
            agent_profile, provider, provider_args, no_profile
        )

        # Send message immediately
        _send_direct_input(terminal_id, message)

        return {
            "success": True,
            "terminal_id": terminal_id,
            "provider": used_provider,
            "message": f"Task assigned to {agent_profile} (terminal: {terminal_id})",
        }

    except Exception as e:
        return {"success": False, "terminal_id": None, "message": f"Assignment failed: {str(e)}"}


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
    try:
        return _send_to_inbox(receiver_id, message)
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
async def reply(
    message: str = Field(description="Message content to send to parent terminal"),
) -> Dict[str, Any]:
    """Reply to the parent terminal that spawned this agent.

    Automatically uses CAO_PARENT_TERMINAL_ID environment variable.
    Only works for agents spawned via assign() or handoff().

    Args:
        message: Message content to send

    Returns:
        Dict with success status and message details
    """
    parent_id = os.environ.get("CAO_PARENT_TERMINAL_ID")
    if not parent_id:
        return {
            "success": False,
            "error": "No parent terminal - this agent was not spawned via assign() or handoff(). "
            "CAO_PARENT_TERMINAL_ID environment variable is not set.",
        }
    try:
        return _send_to_inbox(parent_id, message)
    except Exception as e:
        return {"success": False, "error": str(e)}


def main():
    """Main entry point for the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()

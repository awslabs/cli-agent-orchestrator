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

logger = logging.getLogger(__name__)

# Environment variable to enable/disable working_directory parameter
ENABLE_WORKING_DIRECTORY = os.getenv("CAO_ENABLE_WORKING_DIRECTORY", "false").lower() == "true"

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

    # Get current terminal ID from environment
    current_terminal_id = os.environ.get("CAO_TERMINAL_ID")
    if current_terminal_id:
        # Get terminal metadata via API
        response = requests.get(f"{API_BASE_URL}/terminals/{current_terminal_id}")
        response.raise_for_status()
        terminal_metadata = response.json()

        provider = terminal_metadata["provider"]
        session_name = terminal_metadata["session_name"]

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

        # Create new terminal in existing session - always pass working_directory
        params = {"provider": provider, "agent_profile": agent_profile}
        if working_directory:
            params["working_directory"] = working_directory

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


# Implementation functions
async def _handoff_impl(
    agent_profile: str, message: str, timeout: int = 600, working_directory: Optional[str] = None
) -> HandoffResult:
    """Implementation of handoff logic."""
    start_time = time.time()

    try:
        # Create terminal
        terminal_id, provider = _create_terminal(agent_profile, working_directory)

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

        # Send message immediately
        _send_direct_input(terminal_id, message)

        return {
            "success": True,
            "terminal_id": terminal_id,
            "message": f"Task assigned to {agent_profile} (terminal: {terminal_id})",
        }

    except Exception as e:
        return {"success": False, "terminal_id": None, "message": f"Assignment failed: {str(e)}"}


# Conditional tool registration for assign
if ENABLE_WORKING_DIRECTORY:

    @mcp.tool()
    async def assign(
        agent_profile: str = Field(
            description='The agent profile for the worker agent (e.g., "developer", "analyst")'
        ),
        message: str = Field(
            description="The task message to send. Include callback instructions for the worker to send results back."
        ),
        working_directory: Optional[str] = Field(
            default=None, description="Optional working directory where the agent should execute"
        ),
    ) -> Dict[str, Any]:
        """Assigns a task to another agent without blocking.

        In the message to the worker agent include instruction to send results back via send_message tool.
        **IMPORTANT**: The terminal id of each agent is available in environment variable CAO_TERMINAL_ID.
        When assigning, first find out your own CAO_TERMINAL_ID value, then include the terminal_id value in the message to the worker agent to allow callback.
        Example message: "Analyze the logs. When done, send results back to terminal ee3f93b3 using send_message tool."

        ## Working Directory

        - By default, agents start in the supervisor's current working directory
        - You can specify a custom directory via working_directory parameter
        - Directory must exist and be accessible

        Args:
            agent_profile: Agent profile for the worker terminal
            message: Task message (include callback instructions)
            working_directory: Optional directory path where agent should execute

        Returns:
            Dict with success status, worker terminal_id, and message
        """
        return _assign_impl(agent_profile, message, working_directory)

else:

    @mcp.tool()
    async def assign(
        agent_profile: str = Field(
            description='The agent profile for the worker agent (e.g., "developer", "analyst")'
        ),
        message: str = Field(
            description="The task message to send. Include callback instructions for the worker to send results back."
        ),
    ) -> Dict[str, Any]:
        """Assigns a task to another agent without blocking.

        In the message to the worker agent include instruction to send results back via send_message tool.
        **IMPORTANT**: The terminal id of each agent is available in environment variable CAO_TERMINAL_ID.
        When assigning, first find out your own CAO_TERMINAL_ID value, then include the terminal_id value in the message to the worker agent to allow callback.
        Example message: "Analyze the logs. When done, send results back to terminal ee3f93b3 using send_message tool."

        Args:
            agent_profile: Agent profile for the worker terminal
            message: Task message (include callback instructions)

        Returns:
            Dict with success status, worker terminal_id, and message
        """
        return _assign_impl(agent_profile, message, None)


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


def main():
    """Main entry point for the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()

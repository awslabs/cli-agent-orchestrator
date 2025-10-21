"""CLI Agent Orchestrator MCP Server implementation."""

import asyncio
import logging
import os
import time
from typing import Dict, Any, Tuple
import requests

from fastmcp import FastMCP
from pydantic import Field

from cli_agent_orchestrator.mcp_server.models import HandoffResult
from cli_agent_orchestrator.constants import DEFAULT_PROVIDER, API_BASE_URL
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.utils.terminal import generate_session_name, wait_until_terminal_status

# Create MCP server
mcp = FastMCP(
    'cao-mcp-server',
    instructions="""
    # CLI Agent Orchestrator MCP Server

    This server provides tools to facilitate terminal delegation within CLI Agent Orchestrator sessions.

    ## Best Practices

    - Use specific agent profiles and providers
    - Provide clear and concise messages
    - Ensure you're running within a CAO terminal (CAO_TERMINAL_ID must be set)
    """
)


def _create_terminal(agent_profile: str) -> Tuple[str, str]:
    """Create a new terminal with the specified agent profile.
    
    Args:
        agent_profile: Agent profile for the terminal
        
    Returns:
        Tuple of (terminal_id, provider)
        
    Raises:
        Exception: If terminal creation fails
    """
    provider = DEFAULT_PROVIDER
    
    # Get current terminal ID from environment
    current_terminal_id = os.environ.get('CAO_TERMINAL_ID')
    if current_terminal_id:
        # Get terminal metadata via API
        response = requests.get(f"{API_BASE_URL}/terminals/{current_terminal_id}")
        response.raise_for_status()
        terminal_metadata = response.json()
        
        provider = terminal_metadata["provider"]
        session_name = terminal_metadata["session_name"]
        
        # Create new terminal in existing session
        response = requests.post(
            f"{API_BASE_URL}/sessions/{session_name}/terminals",
            params={"provider": provider, "agent_profile": agent_profile}
        )
        response.raise_for_status()
        terminal = response.json()
    else:
        # Create new session with terminal
        session_name = generate_session_name()
        response = requests.post(
            f"{API_BASE_URL}/sessions",
            params={"provider": provider, "agent_profile": agent_profile, "session_name": session_name}
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
        f"{API_BASE_URL}/terminals/{terminal_id}/input",
        params={"message": message}
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
        params={"sender_id": sender_id, "message": message}
    )
    response.raise_for_status()
    return response.json()


@mcp.tool()
async def handoff(
    agent_profile: str = Field(
        description='The agent profile to hand off to (e.g., "developer", "analyst")'
    ),
    message: str = Field(
        description='The message/task to send to the target agent'
    ),
    timeout: int = Field(
        default=600,
        description='Maximum time to wait for the agent to complete the task (in seconds)',
        ge=1,
        le=3600,
    )
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

    Returns:
        HandoffResult with success status, message, and agent output
    """
    start_time = time.time()
    
    try:
        # Create terminal
        terminal_id, provider = _create_terminal(agent_profile)
        
        # Wait for terminal to be IDLE before sending message
        if not wait_until_terminal_status(terminal_id, TerminalStatus.IDLE, timeout=30.0):
            return HandoffResult(
                success=False,
                message=f"Terminal {terminal_id} did not reach IDLE status within 30 seconds",
                output=None,
                terminal_id=terminal_id
            )
        
        await asyncio.sleep(2)  # wait another 2s
        
        # Send message to terminal
        _send_direct_input(terminal_id, message)
        
        # Monitor until completion with timeout
        if not wait_until_terminal_status(terminal_id, TerminalStatus.COMPLETED, timeout=timeout, polling_interval=1.0):
            return HandoffResult(
                success=False,
                message=f"Handoff timed out after {timeout} seconds",
                output=None,
                terminal_id=terminal_id
            )
        
        # Get the response
        response = requests.get(
            f"{API_BASE_URL}/terminals/{terminal_id}/output",
            params={"mode": "last"}
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
            terminal_id=terminal_id
        )
        
    except Exception as e:
        return HandoffResult(
            success=False,
            message=f"Handoff failed: {str(e)}",
            output=None,
            terminal_id=None
        )


@mcp.tool()
async def assign(
    agent_profile: str = Field(
        description='The agent profile for the worker agent (e.g., "developer", "analyst")'
    ),
    message: str = Field(
        description='The task message to send. Include callback instructions for the worker to send results back.'
    )
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
    try:
        # Create terminal
        terminal_id, _ = _create_terminal(agent_profile)
        
        # Send message immediately
        _send_direct_input(terminal_id, message)
        
        return {
            "success": True,
            "terminal_id": terminal_id,
            "message": f"Task assigned to {agent_profile} (terminal: {terminal_id})"
        }
        
    except Exception as e:
        return {
            "success": False,
            "terminal_id": None,
            "message": f"Assignment failed: {str(e)}"
        }


@mcp.tool()
async def send_message(
    receiver_id: str = Field(description="Target terminal ID to send message to"),
    message: str = Field(description="Message content to send")
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
        return {
            "success": False,
            "error": str(e)
        }


def main():
    """Main entry point for the MCP server."""
    mcp.run()


if __name__ == '__main__':
    main()

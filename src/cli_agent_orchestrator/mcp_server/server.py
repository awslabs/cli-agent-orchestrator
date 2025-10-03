"""CLI Agent Orchestrator MCP Server implementation."""

import asyncio
import os
import time

from fastmcp import FastMCP
from pydantic import Field

from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.clients.database import get_terminal_metadata
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.mcp_server.models import HandoffResult
from cli_agent_orchestrator.constants import DEFAULT_PROVIDER
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
        provider = DEFAULT_PROVIDER
        
        # Get current terminal ID from environment
        current_terminal_id = os.environ.get('CAO_TERMINAL_ID')
        if current_terminal_id:
            # Get terminal metadata from database
            terminal_metadata = get_terminal_metadata(current_terminal_id)
            if not terminal_metadata:
                return HandoffResult(
                    success=False,
                    message=f"Could not find terminal record for {current_terminal_id}",
                    output=None,
                    terminal_id=current_terminal_id
                )
            
            provider = terminal_metadata["provider"]
            session_name = terminal_metadata["tmux_session"]
            
            # Create new terminal in existing session
            terminal = terminal_service.create_terminal(
                session_name=session_name,
                provider=provider,
                agent_profile=agent_profile,
                new_session=False
            )
        else:
            # Create new session with terminal
            session_name = generate_session_name()
            terminal = terminal_service.create_terminal(
                session_name=session_name,
                provider=provider,
                agent_profile=agent_profile,
                new_session=True
            )
        
        # Wait for terminal to be IDLE before sending message
        if not wait_until_terminal_status(terminal.id, TerminalStatus.IDLE, timeout=30.0):
            return HandoffResult(
                success=False,
                message=f"Terminal {terminal.id} did not reach IDLE status within 30 seconds",
                output=None,
                terminal_id=terminal.id
            )
        
        await asyncio.sleep(2)  # wait another 2s
        
        # Send message to terminal
        terminal_service.send_input(terminal.id, message)
        
        # Monitor until completion with timeout
        if not wait_until_terminal_status(terminal.id, TerminalStatus.COMPLETED, timeout=timeout, polling_interval=0.5):
            return HandoffResult(
                success=False,
                message=f"Handoff timed out after {timeout} seconds",
                output=None,
                terminal_id=terminal.id
            )
        
        # Get the response
        output = terminal_service.get_output(terminal.id, terminal_service.OutputMode.LAST)
        
        # Send provider-specific exit command to cleanup terminal
        provider_instance = provider_manager.get_provider(terminal.id)
        if provider_instance:
            exit_command = provider_instance.exit_cli()
            terminal_service.send_input(terminal.id, exit_command)
        else:
            raise ValueError(f"No provider found for terminal {terminal.id}")
        
        execution_time = time.time() - start_time
        
        return HandoffResult(
            success=True,
            message=f"Successfully handed off to {agent_profile} ({provider}) in {execution_time:.2f}s",
            output=output,
            terminal_id=terminal.id
        )
        
    except Exception as e:
        return HandoffResult(
            success=False,
            message=f"Handoff failed: {str(e)}",
            output=None,
            terminal_id=None
        )


def main():
    """Main entry point for the MCP server."""
    mcp.run()


if __name__ == '__main__':
    main()

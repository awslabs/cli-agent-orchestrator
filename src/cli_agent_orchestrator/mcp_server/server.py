"""CLI Agent Orchestrator MCP Server implementation."""

import asyncio
import os
import time
import uuid

from fastmcp import FastMCP
from pydantic import Field

from cli_agent_orchestrator.core.session_manager import session_manager
from cli_agent_orchestrator.mcp_server.models import HandoffResult
from cli_agent_orchestrator.mcp_server.utils import get_terminal_record
from cli_agent_orchestrator.providers.registry import provider_registry
from cli_agent_orchestrator.constants import DEFAULT_PROVIDER

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
        # Default to DEFAULT_PROVIDER, will be updated based on current terminal
        provider = DEFAULT_PROVIDER
        
        # Get current terminal ID from environment
        current_terminal_id = os.environ.get('CAO_TERMINAL_ID')
        if current_terminal_id:
            # Get terminal record from database
            terminal_record = get_terminal_record(current_terminal_id)
            if not terminal_record:
                return HandoffResult(
                    success=False,
                    message=f"Could not find terminal record for {current_terminal_id}",
                    output=None,
                    terminal_id=current_terminal_id
                )
            
            # Use the same provider as current terminal
            provider = terminal_record.provider
            session_id = terminal_record.session_id
            
            # Create new terminal in existing session with same provider
            terminal = await session_manager.create_terminal(
                session_id=session_id,
                provider=provider,
                agent_profile=agent_profile
            )
        else:
            # No terminal ID found, create new session with terminal using default provider
            session_uuid = uuid.uuid4().hex[:4]
            session, terminals = await session_manager.create_session_with_terminals(
                f"cao-{session_uuid}",
                [{"provider": provider, "agent_profile": agent_profile}]
            )
            terminal = terminals[0]
        
        # Send message to terminal
        await session_manager.send_terminal_input(terminal.id, message)
        
        # Monitor until completion with timeout
        elapsed = 0
        while elapsed < timeout:
            terminal_status = await session_manager.get_terminal(terminal.id)
            if terminal_status.status == "completed":
                break
            elif terminal_status.status == "error":
                return HandoffResult(
                    success=False,
                    message=f"Terminal {terminal.id} encountered an error",
                    output=None,
                    terminal_id=terminal.id
                )
            await asyncio.sleep(0.5)
            elapsed = time.time() - start_time
        
        if elapsed >= timeout:
            return HandoffResult(
                success=False,
                message=f"Handoff timed out after {timeout} seconds",
                output=None,
                terminal_id=terminal.id
            )
        
        # Get the response
        output = await session_manager.get_terminal_output(terminal.id, "last")
        
        # Send provider-specific exit command to cleanup terminal
        provider = provider_registry.get_provider(terminal.id)
        if provider:
            exit_command = provider.exit_cli()
            await session_manager.send_terminal_input(terminal.id, exit_command)
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

"""MCP server command for CLI Agent Orchestrator CLI."""

import click

from cli_agent_orchestrator.mcp_server.server import main as run_mcp_server


@click.command(name="mcp-server")
def mcp_server():
    """Start the CAO MCP server."""
    click.echo("Starting CAO MCP server...")
    run_mcp_server()

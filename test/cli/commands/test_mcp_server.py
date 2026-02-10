"""Tests for mcp-server command."""

from unittest.mock import patch

from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.mcp_server import mcp_server


def test_mcp_server_command():
    """Test that mcp-server command calls run_mcp_server."""
    runner = CliRunner()

    with patch("cli_agent_orchestrator.cli.commands.mcp_server.run_mcp_server") as mock_run:
        result = runner.invoke(mcp_server)

        assert result.exit_code == 0
        assert "Starting CAO MCP server..." in result.output
        mock_run.assert_called_once()

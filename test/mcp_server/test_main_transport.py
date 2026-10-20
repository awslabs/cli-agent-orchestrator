"""The in-session MCP server runs over stdio only."""

import os
from unittest.mock import patch

from cli_agent_orchestrator.mcp_server.server import main


def test_main_pins_stdio_transport():
    """FASTMCP_TRANSPORT leaking into a pane's environment must not open a listener."""
    with patch("cli_agent_orchestrator.mcp_server.server.mcp.run") as mock_run:
        with patch.dict(os.environ, {"FASTMCP_TRANSPORT": "http"}):
            main()
    mock_run.assert_called_once_with(transport="stdio")

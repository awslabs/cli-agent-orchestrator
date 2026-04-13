"""Tests for the load_skill MCP tool."""

import asyncio
from unittest.mock import MagicMock, patch

import requests

from cli_agent_orchestrator.mcp_server.server import load_skill, mcp


def _run_coroutine(coroutine):
    """Run a coroutine in an isolated event loop for test stability."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coroutine)
    finally:
        loop.close()


class TestLoadSkillImpl:
    """Tests for the _load_skill_impl helper."""

    @patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    def test_returns_skill_content_on_success(self, mock_get):
        """Successful responses should return the skill content string."""
        from cli_agent_orchestrator.mcp_server.server import _load_skill_impl

        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"name": "python-testing", "content": "# Use pytest"}
        mock_get.return_value = response

        result = _load_skill_impl("python-testing")

        assert result == "# Use pytest"
        mock_get.assert_called_once_with("http://127.0.0.1:9889/skills/python-testing")

    @patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    def test_returns_error_dict_for_404(self, mock_get):
        """A 404 response should surface the API detail message."""
        from cli_agent_orchestrator.mcp_server.server import _load_skill_impl

        response = MagicMock()
        response.json.return_value = {"detail": "Skill not found: missing-skill"}
        http_error = requests.HTTPError("404 Client Error")
        http_error.response = response
        response.raise_for_status.side_effect = http_error
        mock_get.return_value = response

        result = _load_skill_impl("missing-skill")

        assert result == {"success": False, "error": "Skill not found: missing-skill"}

    @patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    def test_returns_error_dict_for_400(self, mock_get):
        """A 400 response should surface the invalid name detail."""
        from cli_agent_orchestrator.mcp_server.server import _load_skill_impl

        response = MagicMock()
        response.json.return_value = {"detail": "Invalid skill name: ../secret"}
        http_error = requests.HTTPError("400 Client Error")
        http_error.response = response
        response.raise_for_status.side_effect = http_error
        mock_get.return_value = response

        result = _load_skill_impl("../secret")

        assert result == {"success": False, "error": "Invalid skill name: ../secret"}

    @patch("cli_agent_orchestrator.mcp_server.server.requests.get")
    def test_returns_error_dict_for_500(self, mock_get):
        """A 500 response should surface the server error detail."""
        from cli_agent_orchestrator.mcp_server.server import _load_skill_impl

        response = MagicMock()
        response.json.return_value = {"detail": "Failed to load skill: bad frontmatter"}
        http_error = requests.HTTPError("500 Server Error")
        http_error.response = response
        response.raise_for_status.side_effect = http_error
        mock_get.return_value = response

        result = _load_skill_impl("broken-skill")

        assert result == {"success": False, "error": "Failed to load skill: bad frontmatter"}

    @patch(
        "cli_agent_orchestrator.mcp_server.server.requests.get",
        side_effect=requests.ConnectionError("connection refused"),
    )
    def test_returns_error_dict_for_connection_error(self, mock_get):
        """Connection failures should tell the agent the server may not be running."""
        from cli_agent_orchestrator.mcp_server.server import _load_skill_impl

        result = _load_skill_impl("python-testing")

        assert result == {
            "success": False,
            "error": "Failed to connect to cao-server. The server may not be running.",
        }


class TestLoadSkillTool:
    """Tests for the load_skill MCP tool registration and wrapper."""

    def test_tool_is_registered_with_clear_description(self):
        """The MCP server should expose a load_skill tool with descriptive text."""
        # FastMCP 2.x: mcp.get_tools() returns dict
        # FastMCP 3.x: get_tools() removed, use mcp.get_tool("load_skill")
        if hasattr(mcp, "get_tools"):
            tools = _run_coroutine(mcp.get_tools())
            if isinstance(tools, dict):
                tool = tools["load_skill"]
            else:
                tool = {t.name: t for t in tools}["load_skill"]
        else:
            tool = _run_coroutine(mcp.get_tool("load_skill"))
        description = tool.description
        assert "full Markdown body of an available skill" in description
        assert "need its full instructions at runtime" in description

    @patch(
        "cli_agent_orchestrator.mcp_server.server._load_skill_impl", return_value="# Loaded skill"
    )
    def test_tool_delegates_to_impl(self, mock_load_skill_impl):
        """The public MCP tool should delegate to the helper implementation."""
        # FastMCP 2.x exposes .fn, 3.x the decorated function is directly callable
        fn = load_skill.fn if hasattr(load_skill, "fn") else load_skill
        result = _run_coroutine(fn(name="python-testing"))

        assert result == "# Loaded skill"
        mock_load_skill_impl.assert_called_once_with("python-testing")

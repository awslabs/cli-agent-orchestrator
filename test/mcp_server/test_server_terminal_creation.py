"""Tests for MCP server terminal creation provider selection."""

import os
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.mcp_server.server import _create_terminal


@pytest.fixture(autouse=True)
def clear_env():
    original = os.environ.get("CAO_TERMINAL_ID")
    try:
        if "CAO_TERMINAL_ID" in os.environ:
            del os.environ["CAO_TERMINAL_ID"]
        yield
    finally:
        if original is not None:
            os.environ["CAO_TERMINAL_ID"] = original
        elif "CAO_TERMINAL_ID" in os.environ:
            del os.environ["CAO_TERMINAL_ID"]


@patch("cli_agent_orchestrator.mcp_server.server.requests.post")
@patch("cli_agent_orchestrator.mcp_server.server.requests.get")
@patch("cli_agent_orchestrator.mcp_server.server.load_agent_profile")
def test_create_terminal_respects_agent_provider(mock_load, mock_get, mock_post):
    """Agent-level provider should drive worker creation inside an existing session."""
    os.environ["CAO_TERMINAL_ID"] = "super123"

    profile = MagicMock()
    profile.provider = "codex_cli"
    mock_load.return_value = profile

    mock_get.return_value.raise_for_status.return_value = None
    mock_get.return_value.json.return_value = {
        "provider": "q_cli",
        "session_name": "cao-test-session"
    }

    mock_post.return_value.raise_for_status.return_value = None
    mock_post.return_value.json.return_value = {
        "id": "worker456"
    }

    terminal_id, provider = _create_terminal("log_analyst_codex")

    assert terminal_id == "worker456"
    assert provider == "codex_cli"

    mock_post.assert_called_once_with(
        "http://localhost:9889/sessions/cao-test-session/terminals",
        params={"provider": "codex_cli", "agent_profile": "log_analyst_codex"}
    )


@patch("cli_agent_orchestrator.mcp_server.server.generate_session_name", return_value="cao-test-session")
@patch("cli_agent_orchestrator.mcp_server.server.requests.post")
@patch("cli_agent_orchestrator.mcp_server.server.load_agent_profile")
def test_create_terminal_defaults_when_provider_missing(mock_load, mock_post, mock_session_name):
    """Use DEFAULT_PROVIDER when the profile omits provider metadata."""
    profile = MagicMock()
    profile.provider = None
    mock_load.return_value = profile

    mock_post.return_value.raise_for_status.return_value = None
    mock_post.return_value.json.return_value = {
        "id": "worker789"
    }

    terminal_id, provider = _create_terminal("legacy_agent")

    assert terminal_id == "worker789"
    assert provider == "q_cli"

    mock_post.assert_called_once_with(
        "http://localhost:9889/sessions",
        params={
            "provider": "q_cli",
            "agent_profile": "legacy_agent",
            "session_name": "cao-test-session"
        }
    )


@patch("cli_agent_orchestrator.mcp_server.server.load_agent_profile", side_effect=RuntimeError("missing profile"))
def test_create_terminal_errors_when_profile_missing(mock_load):
    """Raise a helpful error when the agent profile is not installed."""
    with pytest.raises(RuntimeError, match="Agent profile 'unknown_agent' is not installed"):
        _create_terminal("unknown_agent")

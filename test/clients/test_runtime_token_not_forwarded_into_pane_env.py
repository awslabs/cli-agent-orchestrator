"""TmuxClient never forwards the runtime token value into a pane.

``create_session`` forwards non-blocked ``CAO_*`` variables from the server's
environment into the provider's pane. ``CAO_RUNTIME_TOKEN`` is excluded by an
exact-name blocklist, so a third-party MCP child the provider spawns cannot
inherit the value. ``CAO_RUNTIME_TOKEN_FILE`` is a path, not the value, and is
still forwarded. ``CAO_AUTH_LOCAL_TOKEN`` is still forwarded because the in-pane
``cao-mcp-server`` authenticates its API calls with it.
"""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def tmux():
    with patch("cli_agent_orchestrator.clients.tmux.libtmux") as mock_libtmux:
        mock_server = MagicMock()
        mock_server.cmd.return_value = MagicMock(returncode=0, stdout=[], stderr=[])
        mock_libtmux.Server.return_value = mock_server

        from cli_agent_orchestrator.clients.tmux import TmuxClient

        client = TmuxClient()
        client.server = mock_server
        yield client


def _captured_pane_env(tmux, tmp_path):
    mock_window = MagicMock()
    mock_window.name = "w"
    mock_session = MagicMock()
    mock_session.windows = [mock_window]
    tmux.server.new_session.return_value = mock_session

    tmux.create_session("ses", "w", "tid1", str(tmp_path))
    _, kwargs = tmux.server.new_session.call_args
    return kwargs["environment"]


def test_runtime_token_value_is_not_forwarded_into_the_pane(tmux, tmp_path, monkeypatch):
    monkeypatch.setenv("CAO_RUNTIME_TOKEN", "s3cret-value")
    env = _captured_pane_env(tmux, tmp_path)
    assert "CAO_RUNTIME_TOKEN" not in env
    assert "s3cret-value" not in env.values()


def test_the_token_file_path_is_still_forwarded(tmux, tmp_path, monkeypatch):
    monkeypatch.setenv("CAO_RUNTIME_TOKEN_FILE", "/run/tok/runtime-token")
    env = _captured_pane_env(tmux, tmp_path)
    assert env["CAO_RUNTIME_TOKEN_FILE"] == "/run/tok/runtime-token"


def test_the_mcp_servers_api_credential_is_still_forwarded(tmux, tmp_path, monkeypatch):
    monkeypatch.setenv("CAO_AUTH_LOCAL_TOKEN", "local-api-token")
    env = _captured_pane_env(tmux, tmp_path)
    assert env["CAO_AUTH_LOCAL_TOKEN"] == "local-api-token"


def test_is_blocked_env_key_covers_the_runtime_token_only():
    from cli_agent_orchestrator.clients.tmux import TmuxClient

    assert TmuxClient._is_blocked_env_key("CAO_RUNTIME_TOKEN") is True
    # The path variable and other CAO_* vars stay forwardable.
    assert TmuxClient._is_blocked_env_key("CAO_RUNTIME_TOKEN_FILE") is False
    assert TmuxClient._is_blocked_env_key("CAO_AUTH_LOCAL_TOKEN") is False
    assert TmuxClient._is_blocked_env_key("CAO_TERMINAL_ID") is False

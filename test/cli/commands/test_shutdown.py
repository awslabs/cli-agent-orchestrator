"""Tests for the shutdown CLI command."""

from unittest.mock import MagicMock, patch

import pytest
import requests
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.shutdown import shutdown


class TestShutdownCommand:
    """Tests for the shutdown command."""

    @pytest.fixture
    def runner(self):
        """Create a CLI test runner."""
        return CliRunner()

    def test_shutdown_no_options(self, runner):
        """Test shutdown without any options raises error."""
        result = runner.invoke(shutdown)

        assert result.exit_code != 0
        assert "Must specify either --all or --session" in result.output

    def test_shutdown_both_options(self, runner):
        """Test shutdown with both --all and --session raises error."""
        result = runner.invoke(shutdown, ["--all", "--session", "test-session"])

        assert result.exit_code != 0
        assert "Cannot use --all and --session together" in result.output

    @patch("cli_agent_orchestrator.cli.commands.shutdown.requests.get")
    @patch("cli_agent_orchestrator.cli.commands.shutdown.requests.delete")
    def test_shutdown_all_success(self, mock_delete, mock_get, runner):
        """Test shutdown all sessions successfully."""
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: [
                {"name": "cao-session1"},
                {"name": "cao-session2"},
            ],
        )
        mock_delete.return_value = MagicMock(status_code=200)

        result = runner.invoke(shutdown, ["--all"])

        assert result.exit_code == 0
        assert "Shutdown session 'cao-session1'" in result.output
        assert "Shutdown session 'cao-session2'" in result.output
        assert mock_delete.call_count == 2

    @patch("cli_agent_orchestrator.cli.commands.shutdown.requests.get")
    def test_shutdown_all_no_sessions(self, mock_get, runner):
        """Test shutdown all when no sessions exist."""
        mock_get.return_value = MagicMock(status_code=200, json=lambda: [])

        result = runner.invoke(shutdown, ["--all"])

        assert result.exit_code == 0
        assert "No cao sessions found to shutdown" in result.output

    @patch("cli_agent_orchestrator.cli.commands.shutdown.requests.delete")
    def test_shutdown_specific_session(self, mock_delete, runner):
        """Test shutdown specific session."""
        mock_delete.return_value = MagicMock(status_code=200)

        result = runner.invoke(shutdown, ["--session", "cao-test"])

        assert result.exit_code == 0
        assert "Shutdown session 'cao-test'" in result.output

    @patch("cli_agent_orchestrator.cli.commands.shutdown.requests.get")
    def test_shutdown_all_server_not_running(self, mock_get, runner):
        """Test shutdown all when server is not running raises ClickException."""
        mock_get.side_effect = requests.exceptions.ConnectionError("Connection refused")

        result = runner.invoke(shutdown, ["--all"])

        assert result.exit_code != 0
        assert "Failed to connect to cao-server" in result.output

    @patch("cli_agent_orchestrator.cli.commands.shutdown.requests.delete")
    def test_shutdown_session_server_not_running(self, mock_delete, runner):
        """Test shutdown specific session when server is not running raises ClickException."""
        mock_delete.side_effect = requests.exceptions.ConnectionError("Connection refused")

        result = runner.invoke(shutdown, ["--session", "cao-test"])

        assert result.exit_code != 0
        assert "Failed to connect to cao-server" in result.output

    @patch("cli_agent_orchestrator.cli.commands.shutdown.requests.delete")
    def test_shutdown_session_404_already_removed(self, mock_delete, runner):
        """Test delete returns 404 — warns and continues without error."""
        mock_delete.return_value = MagicMock(status_code=404)

        result = runner.invoke(shutdown, ["--session", "cao-gone"])

        assert result.exit_code == 0
        assert "already removed" in result.output
        assert "Shutdown session" not in result.output

    @patch("cli_agent_orchestrator.cli.commands.shutdown.requests.get")
    @patch("cli_agent_orchestrator.cli.commands.shutdown.requests.delete")
    def test_shutdown_all_partial_failure(self, mock_delete, mock_get, runner):
        """Test --all continues on per-session failures, reporting mixed results."""
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: [
                {"name": "session-1"},
                {"name": "session-2"},
                {"name": "session-3"},
            ],
        )
        ok_response = MagicMock(status_code=200)
        err_response = MagicMock(status_code=500)
        err_response.raise_for_status.side_effect = requests.exceptions.HTTPError(
            "500 Server Error"
        )
        mock_delete.side_effect = [ok_response, err_response, ok_response]

        result = runner.invoke(shutdown, ["--all"])

        assert mock_delete.call_count == 3
        assert result.exit_code == 0
        assert "Shutdown session 'session-1'" in result.output
        assert "Shutdown session 'session-3'" in result.output
        assert "Failed to connect to cao-server" in result.output

    @patch("cli_agent_orchestrator.cli.commands.shutdown.requests.delete")
    def test_shutdown_session_http_error(self, mock_delete, runner):
        """Test delete returns 500 — raises ClickException."""
        mock_response = MagicMock(status_code=500)
        mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError(
            "500 Server Error"
        )
        mock_delete.return_value = mock_response

        result = runner.invoke(shutdown, ["--session", "cao-test"])

        assert result.exit_code != 0
        assert "Failed to connect to cao-server" in result.output

"""Tests for the shutdown CLI command."""

from unittest.mock import patch

import pytest
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

    @patch("cli_agent_orchestrator.cli.commands.shutdown.list_sessions")
    @patch("cli_agent_orchestrator.cli.commands.shutdown.delete_session")
    def test_shutdown_all_success(self, mock_delete, mock_list, runner):
        """Test shutdown all sessions successfully."""
        mock_list.return_value = [
            {"id": "cao-session1"},
            {"id": "cao-session2"},
        ]
        mock_delete.return_value = None

        result = runner.invoke(shutdown, ["--all"])

        assert result.exit_code == 0
        assert "Shutdown session 'cao-session1'" in result.output
        assert "Shutdown session 'cao-session2'" in result.output
        assert mock_delete.call_count == 2

    @patch("cli_agent_orchestrator.cli.commands.shutdown.list_sessions")
    def test_shutdown_all_no_sessions(self, mock_list, runner):
        """Test shutdown all when no sessions exist."""
        mock_list.return_value = []

        result = runner.invoke(shutdown, ["--all"])

        assert result.exit_code == 0
        assert "No cao sessions found to shutdown" in result.output

    @patch("cli_agent_orchestrator.cli.commands.shutdown.delete_session")
    def test_shutdown_specific_session(self, mock_delete, runner):
        """Test shutdown specific session."""
        mock_delete.return_value = None

        result = runner.invoke(shutdown, ["--session", "cao-test"])

        assert result.exit_code == 0
        assert "Shutdown session 'cao-test'" in result.output
        mock_delete.assert_called_once_with("cao-test")

    @patch("cli_agent_orchestrator.cli.commands.shutdown.delete_session")
    def test_shutdown_session_error(self, mock_delete, runner):
        """Test shutdown session with error."""
        mock_delete.side_effect = Exception("Session not found")

        result = runner.invoke(shutdown, ["--session", "cao-nonexistent"])

        assert result.exit_code == 0  # Command completes but reports error
        assert "Error shutting down session 'cao-nonexistent'" in result.output
        assert "Session not found" in result.output

    @patch("cli_agent_orchestrator.cli.commands.shutdown.list_sessions")
    @patch("cli_agent_orchestrator.cli.commands.shutdown.delete_session")
    def test_shutdown_all_partial_failure(self, mock_delete, mock_list, runner):
        """Test shutdown all with partial failure."""
        mock_list.return_value = [
            {"id": "cao-session1"},
            {"id": "cao-session2"},
        ]
        # First call succeeds, second fails
        mock_delete.side_effect = [None, Exception("Error deleting session")]

        result = runner.invoke(shutdown, ["--all"])

        assert result.exit_code == 0
        assert "Shutdown session 'cao-session1'" in result.output
        assert "Error shutting down session 'cao-session2'" in result.output

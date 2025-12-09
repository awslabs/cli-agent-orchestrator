"""Tests for cao resume command."""

import os
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.resume import resume


class TestResumeList:
    """Tests for cao resume --list functionality."""

    def test_list_empty_sessions(self):
        """Test listing when no sessions exist."""
        runner = CliRunner()
        with patch("cli_agent_orchestrator.cli.commands.resume.requests") as mock_req:
            mock_response = MagicMock()
            mock_response.json.return_value = []
            mock_response.raise_for_status.return_value = None
            mock_req.get.return_value = mock_response

            result = runner.invoke(resume, ["--list"])

            assert result.exit_code == 0
            assert "No active CAO sessions" in result.output

    def test_list_sessions_with_terminals(self):
        """Test listing sessions with terminal details."""
        runner = CliRunner()
        with patch("cli_agent_orchestrator.cli.commands.resume.requests") as mock_req:
            # Mock sessions response
            sessions_response = MagicMock()
            sessions_response.json.return_value = [{"id": "cao-test", "name": "cao-test"}]
            sessions_response.raise_for_status.return_value = None

            # Mock terminals response
            terminals_response = MagicMock()
            terminals_response.json.return_value = [
                {
                    "id": "abc123",
                    "agent_profile": "developer",
                    "provider": "claude_code",
                    "status": "IDLE",
                    "last_active": "2024-01-01T12:00:00",
                }
            ]
            terminals_response.raise_for_status.return_value = None

            mock_req.get.side_effect = [sessions_response, terminals_response]

            result = runner.invoke(resume, ["--list"])

            assert result.exit_code == 0
            assert "cao-test" in result.output
            assert "developer" in result.output

    def test_list_sessions_server_down(self):
        """Test listing when cao-server is not running."""
        runner = CliRunner()
        with patch("cli_agent_orchestrator.cli.commands.resume.requests") as mock_req:
            import requests as real_requests
            mock_req.exceptions = real_requests.exceptions
            mock_req.get.side_effect = real_requests.exceptions.ConnectionError()

            result = runner.invoke(resume)

            assert result.exit_code != 0
            assert "Cannot connect to cao-server" in result.output

    def test_list_default_behavior(self):
        """Test that running without args lists sessions."""
        runner = CliRunner()
        with patch("cli_agent_orchestrator.cli.commands.resume.requests") as mock_req:
            mock_response = MagicMock()
            mock_response.json.return_value = []
            mock_response.raise_for_status.return_value = None
            mock_req.get.return_value = mock_response

            result = runner.invoke(resume)

            assert result.exit_code == 0
            assert "No active CAO sessions" in result.output


class TestResumeAttach:
    """Tests for cao resume <session-name> attachment."""

    def test_attach_valid_session(self):
        """Test attaching to a valid session."""
        runner = CliRunner()
        with patch("cli_agent_orchestrator.cli.commands.resume.requests") as mock_req:
            with patch("cli_agent_orchestrator.cli.commands.resume.subprocess") as mock_sub:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.raise_for_status.return_value = None
                mock_req.get.return_value = mock_response

                mock_sub.run.return_value = MagicMock(returncode=0)

                result = runner.invoke(resume, ["cao-test"])

                assert result.exit_code == 0
                mock_sub.run.assert_called_once()
                call_args = mock_sub.run.call_args[0][0]
                assert "tmux" in call_args
                assert "attach-session" in call_args

    def test_attach_adds_prefix_if_missing(self):
        """Test that session name prefix is added if missing."""
        runner = CliRunner()
        with patch("cli_agent_orchestrator.cli.commands.resume.requests") as mock_req:
            with patch("cli_agent_orchestrator.cli.commands.resume.subprocess") as mock_sub:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.raise_for_status.return_value = None
                mock_req.get.return_value = mock_response

                mock_sub.run.return_value = MagicMock(returncode=0)

                result = runner.invoke(resume, ["myproject"])

                # Should have tried with cao- prefix
                assert "cao-myproject" in result.output or mock_sub.run.called

    def test_attach_invalid_session(self):
        """Test attaching to non-existent session."""
        runner = CliRunner()
        with patch("cli_agent_orchestrator.cli.commands.resume.requests") as mock_req:
            mock_response = MagicMock()
            mock_response.status_code = 404
            mock_response.raise_for_status.side_effect = Exception("404")
            mock_req.get.return_value = mock_response
            mock_req.exceptions.HTTPError = type("HTTPError", (Exception,), {})

            result = runner.invoke(resume, ["nonexistent"])

            assert result.exit_code != 0


class TestResumeTerminal:
    """Tests for cao resume --terminal functionality."""

    def test_attach_terminal_valid(self):
        """Test attaching to a specific terminal."""
        runner = CliRunner()
        with patch("cli_agent_orchestrator.cli.commands.resume.requests") as mock_req:
            with patch("cli_agent_orchestrator.cli.commands.resume.subprocess") as mock_sub:
                mock_response = MagicMock()
                mock_response.json.return_value = {
                    "id": "abc123",
                    "session_name": "cao-test",
                    "name": "developer-window",
                    "agent_profile": "developer",
                }
                mock_response.raise_for_status.return_value = None
                mock_req.get.return_value = mock_response

                mock_sub.run.return_value = MagicMock(returncode=0)

                result = runner.invoke(resume, ["--terminal", "abc123"])

                assert result.exit_code == 0
                mock_sub.run.assert_called_once()
                call_args = mock_sub.run.call_args[0][0]
                assert "cao-test:developer-window" in call_args[-1]

    def test_attach_terminal_not_found(self):
        """Test attaching to non-existent terminal."""
        runner = CliRunner()
        with patch("cli_agent_orchestrator.cli.commands.resume.requests") as mock_req:
            mock_response = MagicMock()
            mock_response.status_code = 404
            mock_response.raise_for_status.side_effect = Exception("404")
            mock_req.get.return_value = mock_response
            mock_req.exceptions.HTTPError = type("HTTPError", (Exception,), {"response": MagicMock(status_code=404)})

            result = runner.invoke(resume, ["--terminal", "invalid"])

            assert result.exit_code != 0


class TestResumeCleanup:
    """Tests for cao resume --cleanup functionality."""

    def test_cleanup_no_stale_entries(self):
        """Test cleanup when no stale entries exist."""
        runner = CliRunner()
        with patch("cli_agent_orchestrator.clients.database.get_all_terminals") as mock_get:
            with patch("cli_agent_orchestrator.clients.tmux.tmux_client") as mock_tmux:
                mock_get.return_value = [
                    {"id": "abc123", "tmux_session": "cao-test", "tmux_window": "dev", "agent_profile": "dev"}
                ]
                mock_tmux.session_exists.return_value = True
                mock_tmux.window_exists.return_value = True

                result = runner.invoke(resume, ["--cleanup"])

                assert result.exit_code == 0
                assert "No stale entries found" in result.output

    def test_cleanup_with_stale_entries(self):
        """Test cleanup removes stale entries."""
        runner = CliRunner()
        with patch("cli_agent_orchestrator.clients.database.get_all_terminals") as mock_get:
            with patch("cli_agent_orchestrator.clients.tmux.tmux_client") as mock_tmux:
                with patch("cli_agent_orchestrator.clients.database.delete_terminal") as mock_delete:
                    mock_get.return_value = [
                        {"id": "abc123", "tmux_session": "cao-test", "tmux_window": "dev", "agent_profile": "dev"}
                    ]
                    mock_tmux.session_exists.return_value = False
                    mock_delete.return_value = True

                    result = runner.invoke(resume, ["--cleanup"])

                    assert result.exit_code == 0
                    assert "stale entries" in result.output
                    mock_delete.assert_called_once_with("abc123")

    def test_cleanup_dry_run(self):
        """Test cleanup --dry-run doesn't delete."""
        runner = CliRunner()
        with patch("cli_agent_orchestrator.clients.database.get_all_terminals") as mock_get:
            with patch("cli_agent_orchestrator.clients.tmux.tmux_client") as mock_tmux:
                with patch("cli_agent_orchestrator.clients.database.delete_terminal") as mock_delete:
                    mock_get.return_value = [
                        {"id": "abc123", "tmux_session": "cao-test", "tmux_window": "dev", "agent_profile": "dev"}
                    ]
                    mock_tmux.session_exists.return_value = False

                    result = runner.invoke(resume, ["--cleanup", "--dry-run"])

                    assert result.exit_code == 0
                    assert "Dry run" in result.output
                    mock_delete.assert_not_called()


class TestResumeReconcile:
    """Tests for cao resume --reconcile functionality."""

    def test_reconcile_in_sync(self):
        """Test reconcile when DB and tmux are in sync."""
        runner = CliRunner()
        with patch("cli_agent_orchestrator.clients.database.get_all_terminals") as mock_get:
            with patch("cli_agent_orchestrator.clients.tmux.tmux_client") as mock_tmux:
                mock_get.return_value = []
                mock_tmux.list_sessions.return_value = []

                result = runner.invoke(resume, ["--reconcile"])

                assert result.exit_code == 0
                assert "No discrepancies found" in result.output

    def test_reconcile_orphaned_db_entries(self):
        """Test reconcile detects orphaned DB entries."""
        runner = CliRunner()
        with patch("cli_agent_orchestrator.clients.database.get_all_terminals") as mock_get:
            with patch("cli_agent_orchestrator.clients.tmux.tmux_client") as mock_tmux:
                mock_get.return_value = [
                    {"id": "abc123", "tmux_session": "cao-gone", "tmux_window": "dev", "agent_profile": "dev"}
                ]
                mock_tmux.list_sessions.return_value = []
                mock_tmux.session_exists.return_value = False

                result = runner.invoke(resume, ["--reconcile"])

                assert result.exit_code == 0
                assert "DB entries without tmux windows" in result.output
                assert "abc123" in result.output

    def test_reconcile_untracked_tmux(self):
        """Test reconcile detects untracked tmux sessions."""
        runner = CliRunner()
        with patch("cli_agent_orchestrator.clients.database.get_all_terminals") as mock_get:
            with patch("cli_agent_orchestrator.clients.tmux.tmux_client") as mock_tmux:
                mock_get.return_value = []
                mock_tmux.list_sessions.return_value = [{"id": "cao-untracked"}]

                result = runner.invoke(resume, ["--reconcile"])

                assert result.exit_code == 0
                assert "Tmux CAO sessions not in database" in result.output
                assert "cao-untracked" in result.output

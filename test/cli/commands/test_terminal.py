"""Tests for terminal snapshot-on-delete and restore command."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.terminal import terminal

# ---------------------------------------------------------------------------
# Snapshot-on-delete tests
# ---------------------------------------------------------------------------


class TestSnapshotOnDelete:
    @patch("cli_agent_orchestrator.services.terminal_service.tmux_client")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.db_delete_terminal")
    @patch("cli_agent_orchestrator.services.terminal_service.TERMINAL_LOG_DIR")
    def test_snapshot_written_on_delete(
        self, mock_log_dir, mock_db_delete, mock_pm, mock_meta, mock_tmux, tmp_path
    ):
        """Snapshot files are written before the window is killed."""
        from cli_agent_orchestrator.services.terminal_service import delete_terminal

        mock_log_dir.__truediv__ = lambda self, name: tmp_path / name
        mock_meta.return_value = {
            "id": "abc12345",
            "tmux_session": "cao-test",
            "tmux_window": "dev-abc1",
            "provider": "kiro_cli",
            "agent_profile": "developer",
            "allowed_tools": None,
        }
        mock_tmux.get_history.return_value = "line1\nline2\nline3"
        mock_tmux.get_pane_working_directory.return_value = "/home/user/project"
        mock_db_delete.return_value = True

        delete_terminal("abc12345")

        mock_tmux.get_history.assert_called_once_with(
            "cao-test", "dev-abc1", strip_escapes=True, full_history=True
        )
        scrollback = (tmp_path / "abc12345.scrollback").read_text()
        assert scrollback == "line1\nline2\nline3"

        snapshot = json.loads((tmp_path / "abc12345.snapshot.json").read_text())
        assert snapshot["terminal_id"] == "abc12345"
        assert snapshot["session_name"] == "cao-test"
        assert snapshot["agent_profile"] == "developer"
        assert snapshot["working_directory"] == "/home/user/project"

    @patch("cli_agent_orchestrator.services.terminal_service.tmux_client")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.db_delete_terminal")
    @patch("cli_agent_orchestrator.services.terminal_service.TERMINAL_LOG_DIR")
    def test_snapshot_failure_is_nonfatal(
        self, mock_log_dir, mock_db_delete, mock_pm, mock_meta, mock_tmux, tmp_path
    ):
        """Snapshot failure does not prevent terminal deletion."""
        from cli_agent_orchestrator.services.terminal_service import delete_terminal

        mock_log_dir.__truediv__ = lambda self, name: tmp_path / name
        mock_meta.return_value = {
            "id": "abc12345",
            "tmux_session": "cao-test",
            "tmux_window": "dev-abc1",
            "provider": "kiro_cli",
            "agent_profile": "developer",
            "allowed_tools": None,
        }
        mock_tmux.get_history.side_effect = RuntimeError("tmux error")
        mock_db_delete.return_value = True

        # Should not raise
        result = delete_terminal("abc12345")
        assert result is True
        mock_tmux.kill_window.assert_called_once()


# ---------------------------------------------------------------------------
# Cleanup service tests
# ---------------------------------------------------------------------------


class TestCleanupSnapshots:
    @patch("cli_agent_orchestrator.services.cleanup_service.SessionLocal")
    @patch("cli_agent_orchestrator.services.cleanup_service.RETENTION_DAYS", 7)
    def test_cleanup_deletes_scrollback_and_snapshot(self, mock_session_local, tmp_path):
        """cleanup_old_data removes *.scrollback and *.snapshot.json older than retention."""
        from datetime import datetime, timedelta

        from cli_agent_orchestrator.services.cleanup_service import cleanup_old_data

        mock_db = MagicMock()
        mock_session_local.return_value.__enter__.return_value = mock_db
        mock_db.query.return_value.filter.return_value.delete.return_value = 0

        old_time = (datetime.now() - timedelta(days=10)).timestamp()
        new_time = (datetime.now() - timedelta(days=1)).timestamp()

        old_scrollback = tmp_path / "abc12345.scrollback"
        old_snapshot = tmp_path / "abc12345.snapshot.json"
        new_scrollback = tmp_path / "def67890.scrollback"

        for f in (old_scrollback, old_snapshot, new_scrollback):
            f.write_text("data")

        import os

        os.utime(old_scrollback, (old_time, old_time))
        os.utime(old_snapshot, (old_time, old_time))
        os.utime(new_scrollback, (new_time, new_time))

        with patch("cli_agent_orchestrator.services.cleanup_service.TERMINAL_LOG_DIR", tmp_path):
            with patch("cli_agent_orchestrator.services.cleanup_service.LOG_DIR", tmp_path):
                cleanup_old_data()

        assert not old_scrollback.exists()
        assert not old_snapshot.exists()
        assert new_scrollback.exists()


# ---------------------------------------------------------------------------
# CLI restore command tests
# ---------------------------------------------------------------------------


@pytest.fixture
def runner():
    return CliRunner()


class TestRestoreCommand:
    def test_restore_no_snapshot(self, runner, tmp_path):
        """Fails with clear error when no snapshot exists."""
        with patch("cli_agent_orchestrator.cli.commands.terminal.TERMINAL_LOG_DIR", tmp_path):
            result = runner.invoke(terminal, ["restore", "abc12345"])
        assert result.exit_code != 0
        assert "No snapshot found" in result.output

    def test_restore_session_not_found(self, runner, tmp_path):
        """Fails when session no longer exists."""
        snapshot = {
            "terminal_id": "abc12345",
            "session_name": "cao-gone",
            "window_name": "dev-abc1",
            "agent_profile": "developer",
            "provider": "kiro_cli",
            "working_directory": "/home/user",
            "allowed_tools": None,
        }
        (tmp_path / "abc12345.snapshot.json").write_text(json.dumps(snapshot))

        mock_resp = MagicMock()
        mock_resp.status_code = 404

        with patch("cli_agent_orchestrator.cli.commands.terminal.TERMINAL_LOG_DIR", tmp_path):
            with patch(
                "cli_agent_orchestrator.cli.commands.terminal.requests.get", return_value=mock_resp
            ):
                result = runner.invoke(terminal, ["restore", "abc12345"])

        assert result.exit_code != 0
        assert "no longer exists" in result.output

    def test_restore_connection_error(self, runner, tmp_path):
        """Fails with clear error when cao-server is not running."""
        snapshot = {
            "terminal_id": "abc12345",
            "session_name": "cao-test",
            "window_name": "dev-abc1",
            "agent_profile": "developer",
            "provider": "kiro_cli",
            "working_directory": "/home/user",
            "allowed_tools": None,
        }
        (tmp_path / "abc12345.snapshot.json").write_text(json.dumps(snapshot))

        with patch("cli_agent_orchestrator.cli.commands.terminal.TERMINAL_LOG_DIR", tmp_path):
            with patch(
                "cli_agent_orchestrator.cli.commands.terminal.requests.get",
                side_effect=requests.exceptions.ConnectionError(),
            ):
                result = runner.invoke(terminal, ["restore", "abc12345"])

        assert result.exit_code != 0
        assert "Failed to connect" in result.output

    def test_restore_bad_snapshot_json(self, runner, tmp_path):
        """Fails with clear error when snapshot file is corrupt."""
        (tmp_path / "abc12345.snapshot.json").write_text("not valid json {{{")

        with patch("cli_agent_orchestrator.cli.commands.terminal.TERMINAL_LOG_DIR", tmp_path):
            result = runner.invoke(terminal, ["restore", "abc12345"])

        assert result.exit_code != 0
        assert "Failed to read snapshot" in result.output

    def test_restore_create_window_exception(self, runner, tmp_path):
        """Fails with clear error when tmux window creation fails."""
        snapshot = {
            "terminal_id": "abc12345",
            "session_name": "cao-test",
            "window_name": "dev-abc1",
            "agent_profile": "developer",
            "provider": "kiro_cli",
            "working_directory": "/home/user",
            "allowed_tools": None,
        }
        (tmp_path / "abc12345.snapshot.json").write_text(json.dumps(snapshot))

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_tmux = MagicMock()
        mock_tmux.create_window.side_effect = Exception("tmux session gone")

        with patch("cli_agent_orchestrator.cli.commands.terminal.TERMINAL_LOG_DIR", tmp_path):
            with patch(
                "cli_agent_orchestrator.cli.commands.terminal.requests.get", return_value=mock_resp
            ):
                with patch("cli_agent_orchestrator.cli.commands.terminal.tmux_client", mock_tmux):
                    result = runner.invoke(terminal, ["restore", "abc12345"])

        assert result.exit_code != 0
        assert "Failed to create window" in result.output

    def test_restore_prints_working_directory(self, runner, tmp_path):
        """Working directory is printed when present."""
        snapshot = {
            "terminal_id": "abc12345",
            "session_name": "cao-test",
            "window_name": "dev-abc1",
            "agent_profile": "developer",
            "provider": "kiro_cli",
            "working_directory": "/home/user/project",
            "allowed_tools": None,
        }
        (tmp_path / "abc12345.snapshot.json").write_text(json.dumps(snapshot))

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_tmux = MagicMock()

        with patch("cli_agent_orchestrator.cli.commands.terminal.TERMINAL_LOG_DIR", tmp_path):
            with patch(
                "cli_agent_orchestrator.cli.commands.terminal.requests.get", return_value=mock_resp
            ):
                with patch("cli_agent_orchestrator.cli.commands.terminal.tmux_client", mock_tmux):
                    result = runner.invoke(terminal, ["restore", "abc12345"])

        assert result.exit_code == 0
        assert "/home/user/project" in result.output

    def test_restore_success(self, runner, tmp_path):
        """Creates window with cat+exec shell command on success."""
        snapshot = {
            "terminal_id": "abc12345",
            "session_name": "cao-test",
            "window_name": "dev-abc1",
            "agent_profile": "developer",
            "provider": "kiro_cli",
            "working_directory": "/home/user/project",
            "allowed_tools": None,
        }
        (tmp_path / "abc12345.snapshot.json").write_text(json.dumps(snapshot))
        scrollback_path = tmp_path / "abc12345.scrollback"
        scrollback_path.write_text("prior output here")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        mock_tmux = MagicMock()

        with patch("cli_agent_orchestrator.cli.commands.terminal.TERMINAL_LOG_DIR", tmp_path):
            with patch(
                "cli_agent_orchestrator.cli.commands.terminal.requests.get", return_value=mock_resp
            ):
                with patch("cli_agent_orchestrator.cli.commands.terminal.tmux_client", mock_tmux):
                    with patch.dict("os.environ", {"SHELL": "/bin/zsh"}):
                        result = runner.invoke(terminal, ["restore", "abc12345"])

        assert result.exit_code == 0, result.output
        assert "restored-dev-abc1" in result.output
        assert "cao-test" in result.output
        mock_tmux.create_window.assert_called_once_with(
            "cao-test",
            "restored-dev-abc1",
            "abc12345",
            "/home/user/project",
            window_shell=f"cat '{scrollback_path}'; exec /bin/zsh -l",
        )

    def test_restore_scrollback_missing_still_succeeds(self, runner, tmp_path):
        """Restore succeeds with no window_shell when scrollback file is missing."""
        snapshot = {
            "terminal_id": "abc12345",
            "session_name": "cao-test",
            "window_name": "dev-abc1",
            "agent_profile": "developer",
            "provider": "kiro_cli",
            "working_directory": None,
            "allowed_tools": None,
        }
        (tmp_path / "abc12345.snapshot.json").write_text(json.dumps(snapshot))
        # No scrollback file

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_tmux = MagicMock()

        with patch("cli_agent_orchestrator.cli.commands.terminal.TERMINAL_LOG_DIR", tmp_path):
            with patch(
                "cli_agent_orchestrator.cli.commands.terminal.requests.get", return_value=mock_resp
            ):
                with patch("cli_agent_orchestrator.cli.commands.terminal.tmux_client", mock_tmux):
                    result = runner.invoke(terminal, ["restore", "abc12345"])

        assert result.exit_code == 0
        mock_tmux.create_window.assert_called_once_with(
            "cao-test",
            "restored-dev-abc1",
            "abc12345",
            None,
            window_shell=mock_tmux.create_window.call_args[1]["window_shell"],
        )
        # window_shell should be exec <shell> -l (no cat since no scrollback)
        call_kwargs = mock_tmux.create_window.call_args[1]
        assert call_kwargs["window_shell"].startswith("exec ")
        assert call_kwargs["window_shell"].endswith(" -l")

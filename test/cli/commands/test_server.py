"""Tests for the ``cao server`` command group."""

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.server import server

_SP = "cli_agent_orchestrator.cli.commands.server"


@pytest.fixture
def runner():
    return CliRunner()


class TestServerStart:
    @patch(f"{_SP}.read_pidfile", return_value=4321)
    @patch(f"{_SP}.is_server_running", return_value=True)
    @patch(f"{_SP}.start_server_detached")
    def test_start_noop_when_already_running(self, mock_start, _running, _pid, runner):
        """Single-instance guard: start is a no-op when /health answers."""
        result = runner.invoke(server, ["start"])

        assert result.exit_code == 0
        assert "already running" in result.output
        assert "4321" in result.output
        mock_start.assert_not_called()

    @patch(f"{_SP}.read_pidfile", return_value=None)
    @patch(f"{_SP}.is_server_running", return_value=False)
    @patch(f"{_SP}.start_server_detached", return_value=999)
    def test_start_spawns_when_down(self, mock_start, _running, _pid, runner):
        result = runner.invoke(server, ["start"])

        assert result.exit_code == 0
        assert "started" in result.output
        assert "999" in result.output
        mock_start.assert_called_once()

    @patch(f"{_SP}.read_pidfile", return_value=None)
    @patch(f"{_SP}.is_server_running", return_value=False)
    @patch(f"{_SP}.start_server_detached", side_effect=RuntimeError("did not become healthy"))
    def test_start_reports_failure(self, _start, _running, _pid, runner):
        result = runner.invoke(server, ["start"])

        assert result.exit_code != 0
        assert "did not become healthy" in result.output


class TestServerStop:
    @patch(f"{_SP}.clear_pidfile")
    @patch(f"{_SP}.read_pidfile", return_value=None)
    @patch(f"{_SP}.is_server_running", return_value=False)
    def test_stop_when_not_running(self, _running, _pid, _clear, runner):
        result = runner.invoke(server, ["stop"])

        assert result.exit_code == 0
        assert "not running" in result.output

    @patch(f"{_SP}.stop_server", return_value=True)
    @patch(f"{_SP}.read_pidfile", return_value=4321)
    @patch(f"{_SP}.is_server_running", return_value=True)
    def test_stop_success(self, _running, _pid, mock_stop, runner):
        result = runner.invoke(server, ["stop"])

        assert result.exit_code == 0
        assert "stopped" in result.output
        mock_stop.assert_called_once()

    @patch(f"{_SP}.stop_server", return_value=False)
    @patch(f"{_SP}.read_pidfile", return_value=4321)
    @patch(f"{_SP}.is_server_running", return_value=True)
    def test_stop_failure(self, _running, _pid, _stop, runner):
        result = runner.invoke(server, ["stop"])

        assert result.exit_code != 0
        assert "Failed to stop" in result.output


class TestServerStatus:
    @patch(f"{_SP}.read_pidfile", return_value=None)
    @patch(f"{_SP}.health", return_value=None)
    def test_status_not_running(self, _health, _pid, runner):
        result = runner.invoke(server, ["status"])

        assert result.exit_code == 0
        assert "not running" in result.output

    @patch(f"{_SP}.read_pidfile", return_value=4321)
    @patch(
        f"{_SP}.health",
        return_value={
            "terminal_backend": "tmux",
            "components": {"cao": "ok", "herdr": "unavailable"},
        },
    )
    def test_status_running_shows_components(self, _health, _pid, runner):
        result = runner.invoke(server, ["status"])

        assert result.exit_code == 0
        assert "running" in result.output
        assert "4321" in result.output
        assert "tmux" in result.output
        assert "cao" in result.output
        assert "herdr" in result.output


class TestServerRestart:
    @patch(f"{_SP}.start_server_detached", return_value=999)
    @patch(f"{_SP}.stop_server", return_value=True)
    @patch(f"{_SP}.read_pidfile", return_value=4321)
    @patch(f"{_SP}.is_server_running", return_value=True)
    def test_restart_stops_then_starts(self, _running, _pid, mock_stop, mock_start, runner):
        result = runner.invoke(server, ["restart"])

        assert result.exit_code == 0
        assert "restarted" in result.output
        mock_stop.assert_called_once()
        mock_start.assert_called_once()

"""Tests for the ``cao logs`` command."""

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.logs import logs

_LP = "cli_agent_orchestrator.cli.commands.logs"


@pytest.fixture
def runner():
    return CliRunner()


class TestServerLogs:
    @patch(f"{_LP}.latest_server_log_path")
    def test_tails_latest_server_log(self, mock_latest, runner, tmp_path):
        log = tmp_path / "cao_2026.log"
        log.write_text("line1\nline2\nline3\n")
        mock_latest.return_value = log

        result = runner.invoke(logs, ["--server"])

        assert result.exit_code == 0
        assert "line3" in result.output
        assert str(log) in result.output

    @patch(f"{_LP}.latest_server_log_path")
    def test_respects_lines_limit(self, mock_latest, runner, tmp_path):
        log = tmp_path / "cao_2026.log"
        log.write_text("\n".join(f"line{i}" for i in range(100)) + "\n")
        mock_latest.return_value = log

        result = runner.invoke(logs, ["-n", "5"])

        assert result.exit_code == 0
        assert "line99" in result.output
        assert "line94" not in result.output  # outside last 5

    @patch(f"{_LP}.latest_server_log_path", return_value=None)
    def test_no_server_logs(self, _latest, runner):
        result = runner.invoke(logs, [])

        assert result.exit_code != 0
        assert "No server logs found" in result.output


class TestTerminalLogs:
    def test_tails_terminal_log(self, runner, tmp_path):
        term_log = tmp_path / "abc123.log"
        term_log.write_text("terminal output here\n")

        with patch(f"{_LP}.TERMINAL_LOG_DIR", Path(tmp_path)):
            result = runner.invoke(logs, ["--terminal", "abc123"])

        assert result.exit_code == 0
        assert "terminal output here" in result.output

    def test_missing_terminal_log(self, runner, tmp_path):
        with patch(f"{_LP}.TERMINAL_LOG_DIR", Path(tmp_path)):
            result = runner.invoke(logs, ["--terminal", "nope"])

        assert result.exit_code != 0
        assert "No log for terminal" in result.output

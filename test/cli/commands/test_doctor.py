"""Tests for the ``cao doctor`` command."""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.doctor import doctor

_DP = "cli_agent_orchestrator.cli.commands.doctor"


@pytest.fixture
def runner():
    return CliRunner()


class TestDoctorStatic:
    @patch(f"{_DP}.read_pidfile", return_value=4321)
    @patch(
        f"{_DP}.health",
        return_value={"components": {"cao": "ok", "herdr": "unavailable"}},
    )
    @patch(f"{_DP}.provider_binary_installed", return_value=True)
    @patch(f"{_DP}.provider_binary", return_value="opencode")
    @patch(f"{_DP}.requests.get")
    def test_static_table_assembly(self, mock_get, _binary, _installed, _health, _pid, runner):
        """Static table shows server, providers, profiles, and timeouts."""
        profiles_resp = MagicMock(status_code=200)
        profiles_resp.json.return_value = [
            {"name": "code_supervisor", "source": "built-in"},
        ]
        profiles_resp.raise_for_status.return_value = None
        mock_get.return_value = profiles_resp

        result = runner.invoke(doctor, [])

        assert result.exit_code == 0
        assert "static checks" in result.output
        assert "server: running" in result.output
        # Preferred providers listed
        assert "opencode_cli" in result.output
        assert "claude_code" in result.output
        assert "codex" in result.output
        # Profiles
        assert "code_supervisor" in result.output
        # Effective settings (new defaults)
        assert "provider_init_timeout" in result.output

    @patch(f"{_DP}.read_pidfile", return_value=None)
    @patch(f"{_DP}.health", return_value=None)
    @patch(f"{_DP}.provider_binary_installed", return_value=False)
    @patch(f"{_DP}.provider_binary", return_value="opencode")
    def test_static_server_down(self, _binary, _installed, _health, _pid, runner):
        result = runner.invoke(doctor, [])

        assert result.exit_code == 0
        assert "not reachable" in result.output
        assert "cao server start" in result.output


class TestDoctorLive:
    @patch(f"{_DP}.read_pidfile", return_value=None)
    @patch(f"{_DP}.health", return_value=None)
    @patch(f"{_DP}.provider_binary_installed", return_value=False)
    @patch(f"{_DP}.provider_binary", return_value="opencode")
    def test_live_requires_server(self, _binary, _installed, _health, _pid, runner):
        result = runner.invoke(doctor, ["--live"])

        assert result.exit_code != 0
        assert "Server not reachable" in result.output

    @patch(f"{_DP}.read_pidfile", return_value=4321)
    @patch(f"{_DP}.health", return_value={"components": {}})
    @patch(f"{_DP}.provider_binary", return_value="opencode")
    @patch(f"{_DP}.provider_binary_installed")
    @patch(f"{_DP}._probe_provider", return_value=(True, 4.2, ""))
    def test_live_probes_installed_provider(
        self, mock_probe, mock_installed, _binary, _health, _pid, runner
    ):
        """--live with a single installed provider reports time-to-IDLE."""
        mock_installed.side_effect = lambda name: name == "opencode_cli"

        result = runner.invoke(doctor, ["--live", "--provider", "opencode_cli"])

        assert result.exit_code == 0
        assert "reached IDLE in 4.2s" in result.output
        mock_probe.assert_called_once()

    @patch(f"{_DP}.read_pidfile", return_value=4321)
    @patch(f"{_DP}.health", return_value={"components": {}})
    @patch(f"{_DP}.provider_binary", return_value="opencode")
    @patch(f"{_DP}.provider_binary_installed", return_value=False)
    @patch(f"{_DP}._probe_provider")
    def test_live_skips_uninstalled_provider(
        self, mock_probe, _installed, _binary, _health, _pid, runner
    ):
        result = runner.invoke(doctor, ["--live", "--provider", "opencode_cli"])

        assert result.exit_code == 0
        assert "not installed" in result.output
        mock_probe.assert_not_called()


class TestProbeProvider:
    @patch(f"{_DP}._terminal_status", return_value="idle")
    @patch(f"{_DP}.requests")
    def test_probe_success(self, mock_requests, _status):
        from cli_agent_orchestrator.cli.commands.doctor import _probe_provider

        # requests.exceptions must remain a real exception namespace for except.
        mock_requests.exceptions = __import__("requests").exceptions
        create_resp = MagicMock(status_code=201)
        create_resp.json.return_value = {"id": "t1", "session_name": "doctor-x"}
        mock_requests.post.return_value = create_resp

        ok, elapsed, detail = _probe_provider("opencode_cli", init_timeout=10)

        assert ok is True
        assert detail == ""

    @patch(f"{_DP}.requests")
    def test_probe_create_failure(self, mock_requests):
        from cli_agent_orchestrator.cli.commands.doctor import _probe_provider

        mock_requests.exceptions = __import__("requests").exceptions
        create_resp = MagicMock(status_code=500, text="boom")
        mock_requests.post.return_value = create_resp

        ok, _elapsed, detail = _probe_provider("opencode_cli", init_timeout=10)

        assert ok is False
        assert "session creation failed" in detail

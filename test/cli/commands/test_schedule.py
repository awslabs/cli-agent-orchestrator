"""Tests for the schedule CLI command (and its deprecated flow alias)."""

import re
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.schedule import schedule
from cli_agent_orchestrator.cli.main import cli


class TestFlowGroup:
    """Tests for the flow command group."""

    @pytest.fixture
    def runner(self):
        """Create a CLI test runner."""
        return CliRunner()

    @patch("cli_agent_orchestrator.cli.commands.schedule.init_db")
    def test_flow_group_initializes_db(self, mock_init_db, runner):
        """Test flow group initializes database."""
        result = runner.invoke(schedule, ["--help"])

        # Just checking it doesn't fail
        assert result.exit_code == 0


class TestFlowAddCommand:
    """Tests for the flow add command."""

    @pytest.fixture
    def runner(self):
        """Create a CLI test runner."""
        return CliRunner()

    @patch("cli_agent_orchestrator.cli.commands.schedule.init_db")
    @patch("cli_agent_orchestrator.cli.commands.schedule.flow_service")
    def test_add_success(self, mock_service, mock_init_db, runner):
        """Test successful flow addition."""
        mock_flow = MagicMock()
        mock_flow.name = "test-flow"
        mock_flow.schedule = "0 9 * * *"
        mock_flow.agent_profile = "developer"
        mock_flow.next_run = datetime(2024, 1, 1, 9, 0)
        mock_service.add_flow.return_value = mock_flow

        with runner.isolated_filesystem():
            with open("test-flow.md", "w") as f:
                f.write("---\nname: test-flow\n---\n")

            result = runner.invoke(schedule, ["add", "test-flow.md"])

        assert result.exit_code == 0
        assert "test-flow" in result.output
        assert "added successfully" in result.output

    @patch("cli_agent_orchestrator.cli.commands.schedule.init_db")
    @patch("cli_agent_orchestrator.cli.commands.schedule.flow_service")
    def test_add_error(self, mock_service, mock_init_db, runner):
        """Test flow addition with error."""
        mock_service.add_flow.side_effect = Exception("Invalid flow format")

        with runner.isolated_filesystem():
            with open("test-flow.md", "w") as f:
                f.write("invalid content")

            result = runner.invoke(schedule, ["add", "test-flow.md"])

        assert result.exit_code != 0
        assert "Invalid flow format" in result.output


class TestFlowListCommand:
    """Tests for the flow list command."""

    @pytest.fixture
    def runner(self):
        """Create a CLI test runner."""
        return CliRunner()

    @patch("cli_agent_orchestrator.cli.commands.schedule.init_db")
    @patch("cli_agent_orchestrator.cli.commands.schedule.flow_service")
    def test_list_empty(self, mock_service, mock_init_db, runner):
        """Test listing flows when none exist."""
        mock_service.list_flows.return_value = []

        result = runner.invoke(schedule, ["list"])

        assert result.exit_code == 0
        assert "No flows found" in result.output

    @patch("cli_agent_orchestrator.cli.commands.schedule.init_db")
    @patch("cli_agent_orchestrator.cli.commands.schedule.flow_service")
    def test_list_with_flows(self, mock_service, mock_init_db, runner):
        """Test listing flows with data."""
        mock_flow = MagicMock()
        mock_flow.name = "test-flow"
        mock_flow.schedule = "0 9 * * *"
        mock_flow.agent_profile = "developer"
        mock_flow.last_run = datetime(2024, 1, 1, 8, 0)
        mock_flow.next_run = datetime(2024, 1, 2, 9, 0)
        mock_flow.enabled = True
        mock_service.list_flows.return_value = [mock_flow]

        result = runner.invoke(schedule, ["list"])

        assert result.exit_code == 0
        assert "test-flow" in result.output
        assert "developer" in result.output

    @patch("cli_agent_orchestrator.cli.commands.schedule.init_db")
    @patch("cli_agent_orchestrator.cli.commands.schedule.flow_service")
    def test_list_with_never_run(self, mock_service, mock_init_db, runner):
        """Test listing flows that have never run."""
        mock_flow = MagicMock()
        mock_flow.name = "new-flow"
        mock_flow.schedule = "0 9 * * *"
        mock_flow.agent_profile = "developer"
        mock_flow.last_run = None
        mock_flow.next_run = None
        mock_flow.enabled = False
        mock_service.list_flows.return_value = [mock_flow]

        result = runner.invoke(schedule, ["list"])

        assert result.exit_code == 0
        assert "Never" in result.output
        assert "N/A" in result.output

    @patch("cli_agent_orchestrator.cli.commands.schedule.init_db")
    @patch("cli_agent_orchestrator.cli.commands.schedule.flow_service")
    def test_list_error(self, mock_service, mock_init_db, runner):
        """Test listing flows with error."""
        mock_service.list_flows.side_effect = Exception("Database error")

        result = runner.invoke(schedule, ["list"])

        assert result.exit_code != 0


class TestFlowRemoveCommand:
    """Tests for the flow remove command."""

    @pytest.fixture
    def runner(self):
        """Create a CLI test runner."""
        return CliRunner()

    @patch("cli_agent_orchestrator.cli.commands.schedule.init_db")
    @patch("cli_agent_orchestrator.cli.commands.schedule.flow_service")
    def test_remove_success(self, mock_service, mock_init_db, runner):
        """Test successful flow removal."""
        result = runner.invoke(schedule, ["remove", "test-flow"])

        assert result.exit_code == 0
        assert "removed successfully" in result.output
        mock_service.remove_flow.assert_called_once_with("test-flow")

    @patch("cli_agent_orchestrator.cli.commands.schedule.init_db")
    @patch("cli_agent_orchestrator.cli.commands.schedule.flow_service")
    def test_remove_error(self, mock_service, mock_init_db, runner):
        """Test flow removal with error."""
        mock_service.remove_flow.side_effect = Exception("Flow not found")

        result = runner.invoke(schedule, ["remove", "nonexistent"])

        assert result.exit_code != 0
        assert "Flow not found" in result.output


class TestFlowDisableCommand:
    """Tests for the flow disable command."""

    @pytest.fixture
    def runner(self):
        """Create a CLI test runner."""
        return CliRunner()

    @patch("cli_agent_orchestrator.cli.commands.schedule.init_db")
    @patch("cli_agent_orchestrator.cli.commands.schedule.flow_service")
    def test_disable_success(self, mock_service, mock_init_db, runner):
        """Test successful flow disable."""
        result = runner.invoke(schedule, ["disable", "test-flow"])

        assert result.exit_code == 0
        assert "disabled" in result.output
        mock_service.disable_flow.assert_called_once_with("test-flow")

    @patch("cli_agent_orchestrator.cli.commands.schedule.init_db")
    @patch("cli_agent_orchestrator.cli.commands.schedule.flow_service")
    def test_disable_error(self, mock_service, mock_init_db, runner):
        """Test flow disable with error."""
        mock_service.disable_flow.side_effect = Exception("Flow not found")

        result = runner.invoke(schedule, ["disable", "nonexistent"])

        assert result.exit_code != 0


class TestFlowEnableCommand:
    """Tests for the flow enable command."""

    @pytest.fixture
    def runner(self):
        """Create a CLI test runner."""
        return CliRunner()

    @patch("cli_agent_orchestrator.cli.commands.schedule.init_db")
    @patch("cli_agent_orchestrator.cli.commands.schedule.flow_service")
    def test_enable_success(self, mock_service, mock_init_db, runner):
        """Test successful flow enable."""
        result = runner.invoke(schedule, ["enable", "test-flow"])

        assert result.exit_code == 0
        assert "enabled" in result.output
        mock_service.enable_flow.assert_called_once_with("test-flow")

    @patch("cli_agent_orchestrator.cli.commands.schedule.init_db")
    @patch("cli_agent_orchestrator.cli.commands.schedule.flow_service")
    def test_enable_error(self, mock_service, mock_init_db, runner):
        """Test flow enable with error."""
        mock_service.enable_flow.side_effect = Exception("Flow not found")

        result = runner.invoke(schedule, ["enable", "nonexistent"])

        assert result.exit_code != 0


class TestFlowRunCommand:
    """Tests for the flow run command."""

    @pytest.fixture(autouse=True)
    def _local_server(self):
        """A server on a local backend (or none reachable): the flow runs in this process."""
        with patch(
            "cli_agent_orchestrator.cli.commands.schedule.server_terminal_backend_or_absent",
            return_value=None,
        ):
            yield

    @pytest.fixture
    def runner(self):
        """Create a CLI test runner."""
        return CliRunner()

    @patch("cli_agent_orchestrator.cli.commands.schedule.init_db")
    @patch("cli_agent_orchestrator.cli.commands.schedule.flow_service")
    def test_run_executed(self, mock_service, mock_init_db, runner):
        """Test flow run that executes."""
        # execute_flow is async; the run command drives it via asyncio.run().
        mock_service.execute_flow = AsyncMock(return_value=True)

        result = runner.invoke(schedule, ["run", "test-flow"])

        assert result.exit_code == 0
        assert "executed successfully" in result.output

    @patch("cli_agent_orchestrator.cli.commands.schedule.init_db")
    @patch("cli_agent_orchestrator.cli.commands.schedule.flow_service")
    def test_run_skipped(self, mock_service, mock_init_db, runner):
        """Test flow run that is skipped."""
        # execute_flow is async; the run command drives it via asyncio.run().
        mock_service.execute_flow = AsyncMock(return_value=False)

        result = runner.invoke(schedule, ["run", "test-flow"])

        assert result.exit_code == 0
        assert "skipped" in result.output

    @patch("cli_agent_orchestrator.cli.commands.schedule.init_db")
    @patch("cli_agent_orchestrator.cli.commands.schedule.flow_service")
    def test_run_error(self, mock_service, mock_init_db, runner):
        """Test flow run with error."""
        mock_service.execute_flow = AsyncMock(side_effect=Exception("Flow not found"))

        result = runner.invoke(schedule, ["run", "nonexistent"])

        assert result.exit_code != 0


class TestFlowRunOnARemoteServer:
    """``cao-server --terminal remote``: the CLI's own backend is still tmux (#745).

    Running the flow in the CLI process would put its pre-script and its agent
    on the server host, so the run goes to ``POST /flows/{name}/run`` instead.
    """

    @pytest.fixture(autouse=True)
    def _remote_server(self):
        with patch(
            "cli_agent_orchestrator.cli.commands.schedule.server_terminal_backend_or_absent",
            return_value="remote",
        ):
            yield

    @staticmethod
    def _response(status_code, body):
        resp = MagicMock()
        resp.status_code = status_code
        resp.ok = 200 <= status_code < 300
        resp.json.return_value = body
        resp.text = str(body)
        return resp

    @patch("cli_agent_orchestrator.cli.commands.schedule.init_db")
    @patch("cli_agent_orchestrator.cli.commands.schedule.flow_service")
    @patch("cli_agent_orchestrator.cli.commands.schedule.api_http")
    def test_the_server_runs_the_flow_not_this_process(self, mock_http, mock_service, _db):
        mock_http.post.return_value = self._response(200, {"executed": True})
        mock_service.execute_flow = AsyncMock(return_value=True)

        result = CliRunner().invoke(schedule, ["run", "nightly"])

        assert result.exit_code == 0, result.output
        assert "executed successfully" in result.output
        (url,), _ = mock_http.post.call_args
        assert url.endswith("/flows/nightly/run")
        mock_service.execute_flow.assert_not_called()

    @patch("cli_agent_orchestrator.cli.commands.schedule.init_db")
    @patch("cli_agent_orchestrator.cli.commands.schedule.flow_service")
    @patch("cli_agent_orchestrator.cli.commands.schedule.api_http")
    def test_a_refused_pre_script_fails_the_command(self, mock_http, mock_service, _db):
        mock_http.post.return_value = self._response(
            409, {"detail": "flow 'pre': its pre-script refused"}
        )
        mock_service.execute_flow = AsyncMock(return_value=True)

        result = CliRunner().invoke(schedule, ["run", "pre"])

        assert result.exit_code != 0
        assert "409" in result.output and "pre-script refused" in result.output
        mock_service.execute_flow.assert_not_called()

    @patch("cli_agent_orchestrator.cli.commands.schedule.init_db")
    @patch("cli_agent_orchestrator.cli.commands.schedule.flow_service")
    @patch("cli_agent_orchestrator.cli.commands.schedule.api_http")
    def test_a_skipped_flow_says_so(self, mock_http, mock_service, _db):
        mock_http.post.return_value = self._response(200, {"executed": False})

        result = CliRunner().invoke(schedule, ["run", "nightly"])

        assert result.exit_code == 0
        assert "skipped" in result.output


class TestFlowRunWhenTheServerBackendIsUnknown:
    """A server that may be remote but does not say so: refuse, never run locally."""

    @patch("cli_agent_orchestrator.cli.commands.schedule.init_db")
    @patch("cli_agent_orchestrator.cli.commands.schedule.flow_service")
    @patch("cli_agent_orchestrator.cli.commands.schedule.api_http")
    def test_an_unanswered_health_check_refuses_the_run(self, mock_http, mock_service, _db):
        from cli_agent_orchestrator.utils.terminal import ServerBackendUnknown

        mock_service.execute_flow = AsyncMock(return_value=True)
        with patch(
            "cli_agent_orchestrator.cli.commands.schedule.server_terminal_backend_or_absent",
            side_effect=ServerBackendUnknown("cao-server did not answer"),
        ):
            result = CliRunner().invoke(schedule, ["run", "pre"])

        assert result.exit_code != 0
        assert "did not answer" in result.output
        mock_service.execute_flow.assert_not_called()
        mock_http.post.assert_not_called()


class TestServerTerminalBackendOrAbsent:
    """Only "nothing listening" reads as no server; every other failure is unknown."""

    @staticmethod
    def _probe(get):
        from cli_agent_orchestrator.utils import terminal as terminal_utils

        with patch.object(terminal_utils.api_http, "get", get):
            return terminal_utils.server_terminal_backend_or_absent()

    def test_a_refused_connection_is_no_server(self):
        """A real refused connect, as requests raises it, not a synthetic one."""
        import socket

        from cli_agent_orchestrator.utils import terminal as terminal_utils

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]  # bound, never listening: connect is refused
            with patch.object(terminal_utils, "API_BASE_URL", f"http://127.0.0.1:{port}"):
                assert terminal_utils.server_terminal_backend_or_absent() is None

    def test_a_proxy_that_refuses_the_connect_is_not_no_server(self):
        """The proxy refused, not the server: nested inside is a real refusal."""
        import requests

        from cli_agent_orchestrator.utils.terminal import ServerBackendUnknown

        try:
            raise ConnectionRefusedError(111, "Connection refused")
        except ConnectionRefusedError as refused:
            error = requests.exceptions.ProxyError(OSError("Unable to connect to proxy", refused))
        with pytest.raises(ServerBackendUnknown):
            self._probe(MagicMock(side_effect=error))

    @pytest.mark.parametrize("cause", ["dns", "reset", "tls", "proxy", "bare"])
    def test_a_connection_error_other_than_refused_is_unknown(self, cause):
        import socket
        import ssl

        import requests

        from cli_agent_orchestrator.utils.terminal import ServerBackendUnknown

        inner = {
            "dns": socket.gaierror(-2, "Name or service not known"),
            "reset": ConnectionResetError(104, "Connection reset by peer"),
            "tls": ssl.SSLError("handshake failure"),
            "proxy": OSError("proxy refused"),
            "bare": None,
        }[cause]
        if cause == "proxy":
            error = requests.exceptions.ProxyError(inner)
        elif inner is None:
            error = requests.exceptions.ConnectionError("connection failed")
        else:
            error = requests.exceptions.ConnectionError(inner)
        with pytest.raises(ServerBackendUnknown):
            self._probe(MagicMock(side_effect=error))

    @pytest.mark.parametrize(
        "failure",
        ["connect-timeout", "read-timeout", "http-503", "no-backend"],
    )
    def test_any_other_failure_is_unknown(self, failure):
        import requests

        from cli_agent_orchestrator.utils.terminal import ServerBackendUnknown

        if failure == "connect-timeout":
            get = MagicMock(side_effect=requests.exceptions.ConnectTimeout("slow"))
        elif failure == "read-timeout":
            get = MagicMock(side_effect=requests.exceptions.ReadTimeout("slow"))
        else:
            resp = MagicMock()
            if failure == "http-503":
                resp.raise_for_status.side_effect = requests.exceptions.HTTPError("503")
            else:
                resp.json.return_value = {"status": "ok"}
            get = MagicMock(return_value=resp)
        with pytest.raises(ServerBackendUnknown):
            self._probe(get)

    def test_a_reported_backend_is_returned(self):
        resp = MagicMock()
        resp.json.return_value = {"terminal_backend": "remote"}
        assert self._probe(MagicMock(return_value=resp)) == "remote"


class TestFlowDeprecatedAlias:
    """Tests for the deprecated 'cao flow' alias (issue #378)."""

    @pytest.fixture
    def runner(self):
        """Create a CLI test runner."""
        return CliRunner()

    @patch("cli_agent_orchestrator.cli.commands.schedule.init_db")
    @patch("cli_agent_orchestrator.cli.commands.schedule.flow_service")
    def test_flow_alias_works_and_warns_once(self, mock_service, mock_init_db, runner):
        """Alias runs the same subcommand and emits one deprecation warning on stderr."""
        mock_service.list_flows.return_value = []

        result = runner.invoke(cli, ["flow", "list"])

        assert result.exit_code == 0
        assert "No flows found" in result.output
        assert result.stderr.count("deprecated") == 1
        assert "cao schedule" in result.stderr

    @patch("cli_agent_orchestrator.cli.commands.schedule.init_db")
    def test_flow_alias_help_works(self, mock_init_db, runner):
        """'cao flow --help' still lists all subcommands."""
        result = runner.invoke(cli, ["flow", "--help"])

        assert result.exit_code == 0
        for subcommand in ["add", "list", "remove", "disable", "enable", "run"]:
            assert subcommand in result.output

    @patch("cli_agent_orchestrator.cli.commands.schedule.init_db")
    @patch("cli_agent_orchestrator.cli.commands.schedule.flow_service")
    def test_schedule_does_not_warn(self, mock_service, mock_init_db, runner):
        """'cao schedule' emits no deprecation warning."""
        mock_service.list_flows.return_value = []

        result = runner.invoke(cli, ["schedule", "list"])

        assert result.exit_code == 0
        assert "deprecated" not in result.stderr

    def test_flow_hidden_from_root_help(self, runner):
        """'cao --help' lists 'schedule' but hides the 'flow' alias."""
        result = runner.invoke(cli, ["--help"])

        assert result.exit_code == 0
        assert re.search(r"^\s+schedule\b", result.output, re.MULTILINE)
        assert not re.search(r"^\s+flow\b", result.output, re.MULTILINE)
        assert re.search(r"^\s+workflow\b", result.output, re.MULTILINE)

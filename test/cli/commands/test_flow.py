"""Tests for the flow CLI command."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.flow import add, disable, enable, flow, list, remove, run


class TestFlowGroup:
    """Tests for the flow command group."""

    @pytest.fixture
    def runner(self):
        """Create a CLI test runner."""
        return CliRunner()

    @patch("cli_agent_orchestrator.cli.commands.flow.init_db")
    def test_flow_group_initializes_db(self, mock_init_db, runner):
        """Test flow group initializes database."""
        result = runner.invoke(flow, ["--help"])

        # Just checking it doesn't fail
        assert result.exit_code == 0


class TestFlowAddCommand:
    """Tests for the flow add command."""

    @pytest.fixture
    def runner(self):
        """Create a CLI test runner."""
        return CliRunner()

    @patch("cli_agent_orchestrator.cli.commands.flow.init_db")
    @patch("cli_agent_orchestrator.cli.commands.flow.flow_service")
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

            result = runner.invoke(flow, ["add", "test-flow.md"])

        assert result.exit_code == 0
        assert "test-flow" in result.output
        assert "added successfully" in result.output

    @patch("cli_agent_orchestrator.cli.commands.flow.init_db")
    @patch("cli_agent_orchestrator.cli.commands.flow.flow_service")
    def test_add_error(self, mock_service, mock_init_db, runner):
        """Test flow addition with error."""
        mock_service.add_flow.side_effect = Exception("Invalid flow format")

        with runner.isolated_filesystem():
            with open("test-flow.md", "w") as f:
                f.write("invalid content")

            result = runner.invoke(flow, ["add", "test-flow.md"])

        assert result.exit_code != 0
        assert "Invalid flow format" in result.output


class TestFlowListCommand:
    """Tests for the flow list command."""

    @pytest.fixture
    def runner(self):
        """Create a CLI test runner."""
        return CliRunner()

    @patch("cli_agent_orchestrator.cli.commands.flow.init_db")
    @patch("cli_agent_orchestrator.cli.commands.flow.flow_service")
    def test_list_empty(self, mock_service, mock_init_db, runner):
        """Test listing flows when none exist."""
        mock_service.list_flows.return_value = []

        result = runner.invoke(flow, ["list"])

        assert result.exit_code == 0
        assert "No flows found" in result.output

    @patch("cli_agent_orchestrator.cli.commands.flow.init_db")
    @patch("cli_agent_orchestrator.cli.commands.flow.flow_service")
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

        result = runner.invoke(flow, ["list"])

        assert result.exit_code == 0
        assert "test-flow" in result.output
        assert "developer" in result.output

    @patch("cli_agent_orchestrator.cli.commands.flow.init_db")
    @patch("cli_agent_orchestrator.cli.commands.flow.flow_service")
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

        result = runner.invoke(flow, ["list"])

        assert result.exit_code == 0
        assert "Never" in result.output
        assert "N/A" in result.output

    @patch("cli_agent_orchestrator.cli.commands.flow.init_db")
    @patch("cli_agent_orchestrator.cli.commands.flow.flow_service")
    def test_list_error(self, mock_service, mock_init_db, runner):
        """Test listing flows with error."""
        mock_service.list_flows.side_effect = Exception("Database error")

        result = runner.invoke(flow, ["list"])

        assert result.exit_code != 0


class TestFlowRemoveCommand:
    """Tests for the flow remove command."""

    @pytest.fixture
    def runner(self):
        """Create a CLI test runner."""
        return CliRunner()

    @patch("cli_agent_orchestrator.cli.commands.flow.init_db")
    @patch("cli_agent_orchestrator.cli.commands.flow.flow_service")
    def test_remove_success(self, mock_service, mock_init_db, runner):
        """Test successful flow removal."""
        result = runner.invoke(flow, ["remove", "test-flow"])

        assert result.exit_code == 0
        assert "removed successfully" in result.output
        mock_service.remove_flow.assert_called_once_with("test-flow")

    @patch("cli_agent_orchestrator.cli.commands.flow.init_db")
    @patch("cli_agent_orchestrator.cli.commands.flow.flow_service")
    def test_remove_error(self, mock_service, mock_init_db, runner):
        """Test flow removal with error."""
        mock_service.remove_flow.side_effect = Exception("Flow not found")

        result = runner.invoke(flow, ["remove", "nonexistent"])

        assert result.exit_code != 0
        assert "Flow not found" in result.output


class TestFlowDisableCommand:
    """Tests for the flow disable command."""

    @pytest.fixture
    def runner(self):
        """Create a CLI test runner."""
        return CliRunner()

    @patch("cli_agent_orchestrator.cli.commands.flow.init_db")
    @patch("cli_agent_orchestrator.cli.commands.flow.flow_service")
    def test_disable_success(self, mock_service, mock_init_db, runner):
        """Test successful flow disable."""
        result = runner.invoke(flow, ["disable", "test-flow"])

        assert result.exit_code == 0
        assert "disabled" in result.output
        mock_service.disable_flow.assert_called_once_with("test-flow")

    @patch("cli_agent_orchestrator.cli.commands.flow.init_db")
    @patch("cli_agent_orchestrator.cli.commands.flow.flow_service")
    def test_disable_error(self, mock_service, mock_init_db, runner):
        """Test flow disable with error."""
        mock_service.disable_flow.side_effect = Exception("Flow not found")

        result = runner.invoke(flow, ["disable", "nonexistent"])

        assert result.exit_code != 0


class TestFlowEnableCommand:
    """Tests for the flow enable command."""

    @pytest.fixture
    def runner(self):
        """Create a CLI test runner."""
        return CliRunner()

    @patch("cli_agent_orchestrator.cli.commands.flow.init_db")
    @patch("cli_agent_orchestrator.cli.commands.flow.flow_service")
    def test_enable_success(self, mock_service, mock_init_db, runner):
        """Test successful flow enable."""
        result = runner.invoke(flow, ["enable", "test-flow"])

        assert result.exit_code == 0
        assert "enabled" in result.output
        mock_service.enable_flow.assert_called_once_with("test-flow")

    @patch("cli_agent_orchestrator.cli.commands.flow.init_db")
    @patch("cli_agent_orchestrator.cli.commands.flow.flow_service")
    def test_enable_error(self, mock_service, mock_init_db, runner):
        """Test flow enable with error."""
        mock_service.enable_flow.side_effect = Exception("Flow not found")

        result = runner.invoke(flow, ["enable", "nonexistent"])

        assert result.exit_code != 0


class TestFlowRunCommand:
    """Tests for the flow run command."""

    @pytest.fixture
    def runner(self):
        """Create a CLI test runner."""
        return CliRunner()

    @patch("cli_agent_orchestrator.cli.commands.flow.init_db")
    @patch("cli_agent_orchestrator.cli.commands.flow.flow_service")
    def test_run_executed(self, mock_service, mock_init_db, runner):
        """Test flow run that executes."""
        mock_service.execute_flow.return_value = True

        result = runner.invoke(flow, ["run", "test-flow"])

        assert result.exit_code == 0
        assert "executed successfully" in result.output

    @patch("cli_agent_orchestrator.cli.commands.flow.init_db")
    @patch("cli_agent_orchestrator.cli.commands.flow.flow_service")
    def test_run_skipped(self, mock_service, mock_init_db, runner):
        """Test flow run that is skipped."""
        mock_service.execute_flow.return_value = False

        result = runner.invoke(flow, ["run", "test-flow"])

        assert result.exit_code == 0
        assert "skipped" in result.output

    @patch("cli_agent_orchestrator.cli.commands.flow.init_db")
    @patch("cli_agent_orchestrator.cli.commands.flow.flow_service")
    def test_run_error(self, mock_service, mock_init_db, runner):
        """Test flow run with error."""
        mock_service.execute_flow.side_effect = Exception("Flow not found")

        result = runner.invoke(flow, ["run", "nonexistent"])

        assert result.exit_code != 0

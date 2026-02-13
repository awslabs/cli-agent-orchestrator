"""Unit tests for flow service."""

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.flow import Flow
from cli_agent_orchestrator.services import flow_service


@pytest.fixture
def mock_db():
    """Mock database client."""
    with patch("cli_agent_orchestrator.services.flow_service.db_create_flow") as create, \
         patch("cli_agent_orchestrator.services.flow_service.db_list_flows") as list_flows, \
         patch("cli_agent_orchestrator.services.flow_service.db_get_flow") as get_flow, \
         patch("cli_agent_orchestrator.services.flow_service.db_delete_flow") as delete, \
         patch("cli_agent_orchestrator.services.flow_service.db_update_flow_enabled") as update_enabled, \
         patch("cli_agent_orchestrator.services.flow_service.db_get_flows_to_run") as get_to_run, \
         patch("cli_agent_orchestrator.services.flow_service.db_update_flow_run_times") as update_times:
        yield {
            "create": create,
            "list": list_flows,
            "get": get_flow,
            "delete": delete,
            "update_enabled": update_enabled,
            "get_to_run": get_to_run,
            "update_times": update_times
        }


@pytest.fixture
def mock_terminal_service():
    """Mock terminal service."""
    with patch("cli_agent_orchestrator.services.flow_service.create_terminal") as create, \
         patch("cli_agent_orchestrator.services.flow_service.send_input") as send:
        yield {"create": create, "send": send}


@pytest.fixture
def mock_template():
    """Mock template rendering."""
    with patch("cli_agent_orchestrator.services.flow_service.render_template") as mock:
        mock.return_value = "rendered prompt"
        yield mock


@pytest.fixture
def sample_flow():
    """Sample flow for testing."""
    return Flow(
        name="test-flow",
        file_path="/path/to/flow.md",
        schedule="0 9 * * *",
        agent_profile="developer"
    )


class TestAddFlow:
    """Test add_flow function."""

    @patch("cli_agent_orchestrator.services.flow_service._parse_flow_file")
    @patch("cli_agent_orchestrator.services.flow_service._get_next_run_time")
    def test_add_flow_success(self, mock_next_run, mock_parse, mock_db):
        """Test successful flow addition."""
        mock_parse.return_value = ({"name": "test", "schedule": "0 9 * * *", "agent_profile": "dev"}, "content")
        mock_next_run.return_value = datetime.now()
        mock_db["create"].return_value = Flow(name="test", file_path="/test", schedule="0 9 * * *", agent_profile="dev")
        
        result = flow_service.add_flow("/path/to/flow.md")
        
        assert result.name == "test"
        mock_db["create"].assert_called_once()

    @patch("cli_agent_orchestrator.services.flow_service._parse_flow_file")
    def test_add_flow_missing_name(self, mock_parse, mock_db):
        """Test flow addition with missing name."""
        mock_parse.return_value = ({"schedule": "0 9 * * *", "agent_profile": "dev"}, "content")
        
        with pytest.raises(ValueError, match="Missing required field: name"):
            flow_service.add_flow("/path/to/flow.md")

    @patch("cli_agent_orchestrator.services.flow_service._parse_flow_file")
    def test_add_flow_missing_agent_profile(self, mock_parse, mock_db):
        """Test flow addition with missing agent profile."""
        mock_parse.return_value = ({"name": "test", "schedule": "0 9 * * *"}, "content")
        
        with pytest.raises(ValueError, match="Missing required field: agent_profile"):
            flow_service.add_flow("/path/to/flow.md")

    @patch("cli_agent_orchestrator.services.flow_service._parse_flow_file")
    @patch("cli_agent_orchestrator.services.flow_service._get_next_run_time")
    def test_add_flow_invalid_cron(self, mock_next_run, mock_parse, mock_db):
        """Test flow addition with invalid cron expression."""
        mock_parse.return_value = ({"name": "test", "schedule": "invalid", "agent_profile": "dev"}, "content")
        mock_next_run.side_effect = ValueError("Invalid cron")
        
        with pytest.raises(ValueError, match="Invalid cron expression"):
            flow_service.add_flow("/path/to/flow.md")


class TestListFlows:
    """Test list_flows function."""

    def test_list_flows_empty(self, mock_db):
        """Test listing flows when none exist."""
        mock_db["list"].return_value = []
        
        result = flow_service.list_flows()
        
        assert result == []
        mock_db["list"].assert_called_once()

    def test_list_flows_multiple(self, mock_db, sample_flow):
        """Test listing multiple flows."""
        flows = [sample_flow, sample_flow]
        mock_db["list"].return_value = flows
        
        result = flow_service.list_flows()
        
        assert result == flows
        mock_db["list"].assert_called_once()


class TestGetFlow:
    """Test get_flow function."""

    def test_get_flow_success(self, mock_db, sample_flow):
        """Test successful flow retrieval."""
        mock_db["get"].return_value = sample_flow
        
        result = flow_service.get_flow("test-flow")
        
        assert result == sample_flow
        mock_db["get"].assert_called_once_with("test-flow")

    def test_get_flow_not_found(self, mock_db):
        """Test flow retrieval when not found."""
        mock_db["get"].return_value = None
        
        with pytest.raises(ValueError, match="Flow 'nonexistent' not found"):
            flow_service.get_flow("nonexistent")


class TestRemoveFlow:
    """Test remove_flow function."""

    def test_remove_flow_success(self, mock_db):
        """Test successful flow removal."""
        mock_db["delete"].return_value = True
        
        result = flow_service.remove_flow("test-flow")
        
        assert result is True
        mock_db["delete"].assert_called_once_with("test-flow")

    def test_remove_flow_not_found(self, mock_db):
        """Test flow removal when not found."""
        mock_db["delete"].return_value = False
        
        with pytest.raises(ValueError, match="Flow 'nonexistent' not found"):
            flow_service.remove_flow("nonexistent")


class TestEnableDisableFlow:
    """Test enable_flow and disable_flow functions."""

    @patch("cli_agent_orchestrator.services.flow_service.get_flow")
    @patch("cli_agent_orchestrator.services.flow_service._get_next_run_time")
    def test_enable_flow(self, mock_next_run, mock_get_flow, mock_db, sample_flow):
        """Test enabling a flow."""
        mock_get_flow.return_value = sample_flow
        mock_next_run.return_value = datetime.now()
        mock_db["update_enabled"].return_value = True
        
        result = flow_service.enable_flow("test-flow")
        
        assert result is True
        mock_db["update_enabled"].assert_called_once()

    def test_disable_flow(self, mock_db):
        """Test disabling a flow."""
        mock_db["update_enabled"].return_value = True
        
        result = flow_service.disable_flow("test-flow")
        
        assert result is True
        mock_db["update_enabled"].assert_called_once_with("test-flow", enabled=False)


class TestExecuteFlow:
    """Test execute_flow function."""

    @patch("cli_agent_orchestrator.services.flow_service.get_flow")
    @patch("cli_agent_orchestrator.services.flow_service._parse_flow_file")
    @patch("cli_agent_orchestrator.services.flow_service._get_next_run_time")
    @patch("cli_agent_orchestrator.services.flow_service.generate_session_name")
    def test_execute_flow_no_script(self, mock_session_name, mock_next_run, mock_parse, mock_get_flow, 
                                   mock_db, mock_terminal_service, mock_template, sample_flow):
        """Test flow execution without script."""
        mock_get_flow.return_value = sample_flow
        mock_parse.return_value = ({}, "template content")
        mock_next_run.return_value = datetime.now()
        mock_session_name.return_value = "session-123"
        mock_terminal = MagicMock()
        mock_terminal.id = "terminal-123"
        mock_terminal_service["create"].return_value = mock_terminal
        
        result = flow_service.execute_flow("test-flow")
        
        assert result is True
        mock_terminal_service["create"].assert_called_once()
        mock_terminal_service["send"].assert_called_once()

    @patch("cli_agent_orchestrator.services.flow_service.get_flow")
    @patch("cli_agent_orchestrator.services.flow_service._parse_flow_file")
    @patch("cli_agent_orchestrator.services.flow_service.subprocess.run")
    @patch("cli_agent_orchestrator.services.flow_service.Path")
    def test_execute_flow_with_script_success(self, mock_path_cls, mock_subprocess, mock_parse, mock_get_flow, 
                                             mock_db, mock_terminal_service, mock_template, sample_flow):
        """Test flow execution with successful script."""
        sample_flow.script = "script.py"
        mock_get_flow.return_value = sample_flow
        mock_parse.return_value = ({}, "template content")
        mock_subprocess.return_value.returncode = 0
        mock_subprocess.return_value.stdout = '{"execute": true, "output": {}}'
        
        # Mock Path to make script exist
        mock_file_path = MagicMock()
        mock_file_path.parent = MagicMock()
        mock_script_path = MagicMock()
        mock_script_path.is_absolute.return_value = False
        mock_script_path.exists.return_value = True
        mock_file_path.parent.__truediv__.return_value = mock_script_path
        mock_path_cls.side_effect = lambda x: mock_file_path if x == sample_flow.file_path else mock_script_path
        
        with patch("cli_agent_orchestrator.services.flow_service._get_next_run_time"), \
             patch("cli_agent_orchestrator.services.flow_service.generate_session_name"):
            mock_terminal = MagicMock()
            mock_terminal.id = "terminal-123"
            mock_terminal_service["create"].return_value = mock_terminal
            
            result = flow_service.execute_flow("test-flow")
            
            assert result is True

    @patch("cli_agent_orchestrator.services.flow_service.get_flow")
    @patch("cli_agent_orchestrator.services.flow_service._parse_flow_file")
    @patch("cli_agent_orchestrator.services.flow_service.subprocess.run")
    @patch("cli_agent_orchestrator.services.flow_service.Path")
    def test_execute_flow_script_returns_false(self, mock_path_cls, mock_subprocess, mock_parse, mock_get_flow, 
                                              mock_db, mock_terminal_service, mock_template, sample_flow):
        """Test flow execution when script returns false."""
        sample_flow.script = "script.py"
        mock_get_flow.return_value = sample_flow
        mock_parse.return_value = ({}, "template content")
        mock_subprocess.return_value.returncode = 0
        mock_subprocess.return_value.stdout = '{"execute": false, "output": {}}'
        
        # Mock Path to make script exist
        mock_file_path = MagicMock()
        mock_file_path.parent = MagicMock()
        mock_script_path = MagicMock()
        mock_script_path.is_absolute.return_value = False
        mock_script_path.exists.return_value = True
        mock_file_path.parent.__truediv__.return_value = mock_script_path
        mock_path_cls.side_effect = lambda x: mock_file_path if x == sample_flow.file_path else mock_script_path
        
        with patch("cli_agent_orchestrator.services.flow_service._get_next_run_time"):
            result = flow_service.execute_flow("test-flow")
            
            assert result is False
            mock_terminal_service["create"].assert_not_called()

    @patch("cli_agent_orchestrator.services.flow_service.get_flow")
    @patch("cli_agent_orchestrator.services.flow_service._parse_flow_file")
    @patch("cli_agent_orchestrator.services.flow_service.subprocess.run")
    @patch("cli_agent_orchestrator.services.flow_service.Path")
    def test_execute_flow_script_fails(self, mock_path_cls, mock_subprocess, mock_parse, mock_get_flow, 
                                      mock_db, mock_terminal_service, mock_template, sample_flow):
        """Test flow execution when script fails."""
        sample_flow.script = "script.py"
        mock_get_flow.return_value = sample_flow
        mock_parse.return_value = ({}, "template content")
        mock_subprocess.return_value.returncode = 1
        mock_subprocess.return_value.stderr = "Script error"
        
        # Mock Path to make script exist
        mock_file_path = MagicMock()
        mock_file_path.parent = MagicMock()
        mock_script_path = MagicMock()
        mock_script_path.is_absolute.return_value = False
        mock_script_path.exists.return_value = True
        mock_file_path.parent.__truediv__.return_value = mock_script_path
        mock_path_cls.side_effect = lambda x: mock_file_path if x == sample_flow.file_path else mock_script_path
        
        with pytest.raises(ValueError, match="Script failed"):
            flow_service.execute_flow("test-flow")


class TestGetFlowsToRun:
    """Test get_flows_to_run function."""

    def test_get_flows_to_run(self, mock_db, sample_flow):
        """Test getting flows to run."""
        flows = [sample_flow]
        mock_db["get_to_run"].return_value = flows
        
        result = flow_service.get_flows_to_run()
        
        assert result == flows
        mock_db["get_to_run"].assert_called_once()
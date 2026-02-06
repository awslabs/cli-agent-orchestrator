"""Tests for flow service."""

import json
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.flow import Flow
from cli_agent_orchestrator.services.flow_service import (
    _get_next_run_time,
    _parse_flow_file,
    add_flow,
    disable_flow,
    enable_flow,
    execute_flow,
    get_flow,
    get_flows_to_run,
    list_flows,
    remove_flow,
)


class TestGetNextRunTime:
    """Tests for _get_next_run_time function."""

    def test_valid_cron_expression(self):
        """Test that valid cron expression returns a datetime."""
        # Every minute
        result = _get_next_run_time("* * * * *")
        assert isinstance(result, datetime)
        # Compare without timezone info
        assert result.replace(tzinfo=None) > datetime.now()

    def test_specific_time_cron(self):
        """Test cron expression for specific time."""
        # Every day at midnight
        result = _get_next_run_time("0 0 * * *")
        assert isinstance(result, datetime)

    def test_invalid_cron_expression(self):
        """Test that invalid cron expression raises error."""
        with pytest.raises(Exception):
            _get_next_run_time("invalid cron")

    def test_weekday_cron(self):
        """Test cron expression for weekdays only."""
        # 9am on weekdays
        result = _get_next_run_time("0 9 * * 1-5")
        assert isinstance(result, datetime)


class TestParseFlowFile:
    """Tests for _parse_flow_file function."""

    def test_parse_valid_flow_file(self):
        """Test parsing a valid flow file with frontmatter."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("""---
name: test-flow
schedule: "* * * * *"
agent_profile: developer
provider: kiro_cli
---

This is the prompt template.
""")
            f.flush()

            metadata, content = _parse_flow_file(Path(f.name))

            assert metadata["name"] == "test-flow"
            assert metadata["schedule"] == "* * * * *"
            assert metadata["agent_profile"] == "developer"
            assert metadata["provider"] == "kiro_cli"
            assert "prompt template" in content

    def test_parse_flow_file_not_found(self):
        """Test that non-existent file raises error."""
        with pytest.raises(ValueError, match="Flow file not found"):
            _parse_flow_file(Path("/nonexistent/path/flow.md"))

    def test_parse_flow_file_with_script(self):
        """Test parsing flow file with optional script field."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("""---
name: scripted-flow
schedule: "0 * * * *"
agent_profile: developer
script: ./check.sh
---

Prompt with [[variable]].
""")
            f.flush()

            metadata, content = _parse_flow_file(Path(f.name))

            assert metadata["script"] == "./check.sh"
            assert "[[variable]]" in content


class TestAddFlow:
    """Tests for add_flow function."""

    @patch("cli_agent_orchestrator.services.flow_service.db_create_flow")
    def test_add_flow_success(self, mock_db_create):
        """Test adding a valid flow."""
        mock_flow = Flow(
            name="test-flow",
            file_path="/path/to/flow.md",
            schedule="* * * * *",
            agent_profile="developer",
            provider="kiro_cli",
            enabled=True,
            next_run=datetime.now(),
        )
        mock_db_create.return_value = mock_flow

        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("""---
name: test-flow
schedule: "* * * * *"
agent_profile: developer
---

Test prompt.
""")
            f.flush()

            result = add_flow(f.name)

            assert result.name == "test-flow"
            mock_db_create.assert_called_once()

    def test_add_flow_missing_required_field(self):
        """Test that missing required field raises error."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("""---
name: incomplete-flow
---

Missing schedule and agent_profile.
""")
            f.flush()

            with pytest.raises(ValueError, match="Missing required field"):
                add_flow(f.name)

    def test_add_flow_invalid_cron(self):
        """Test that invalid cron expression raises error."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("""---
name: bad-cron-flow
schedule: "not a cron"
agent_profile: developer
---

Test prompt.
""")
            f.flush()

            with pytest.raises(ValueError, match="Invalid cron expression"):
                add_flow(f.name)

    @patch("cli_agent_orchestrator.services.flow_service.db_create_flow")
    def test_add_flow_with_optional_provider(self, mock_db_create):
        """Test adding flow with custom provider."""
        mock_flow = Flow(
            name="custom-provider-flow",
            file_path="/path/to/flow.md",
            schedule="0 9 * * *",
            agent_profile="developer",
            provider="claude_code",
            enabled=True,
            next_run=datetime.now(),
        )
        mock_db_create.return_value = mock_flow

        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("""---
name: custom-provider-flow
schedule: "0 9 * * *"
agent_profile: developer
provider: claude_code
---

Test prompt.
""")
            f.flush()

            result = add_flow(f.name)
            assert result.provider == "claude_code"


class TestListFlows:
    """Tests for list_flows function."""

    @patch("cli_agent_orchestrator.services.flow_service.db_list_flows")
    def test_list_flows_returns_all(self, mock_db_list):
        """Test that list_flows returns all flows."""
        mock_flows = [
            Flow(
                name="flow1",
                file_path="/path1",
                schedule="* * * * *",
                agent_profile="dev",
                provider="kiro_cli",
                enabled=True,
                next_run=datetime.now(),
            ),
            Flow(
                name="flow2",
                file_path="/path2",
                schedule="0 * * * *",
                agent_profile="dev",
                provider="kiro_cli",
                enabled=False,
                next_run=datetime.now(),
            ),
        ]
        mock_db_list.return_value = mock_flows

        result = list_flows()

        assert len(result) == 2
        assert result[0].name == "flow1"
        assert result[1].name == "flow2"

    @patch("cli_agent_orchestrator.services.flow_service.db_list_flows")
    def test_list_flows_empty(self, mock_db_list):
        """Test list_flows with no flows."""
        mock_db_list.return_value = []

        result = list_flows()

        assert result == []


class TestGetFlow:
    """Tests for get_flow function."""

    @patch("cli_agent_orchestrator.services.flow_service.db_get_flow")
    def test_get_flow_exists(self, mock_db_get):
        """Test getting an existing flow."""
        mock_flow = Flow(
            name="test-flow",
            file_path="/path/flow.md",
            schedule="* * * * *",
            agent_profile="developer",
            provider="kiro_cli",
            enabled=True,
            next_run=datetime.now(),
        )
        mock_db_get.return_value = mock_flow

        result = get_flow("test-flow")

        assert result.name == "test-flow"
        mock_db_get.assert_called_once_with("test-flow")

    @patch("cli_agent_orchestrator.services.flow_service.db_get_flow")
    def test_get_flow_not_found(self, mock_db_get):
        """Test getting a non-existent flow raises error."""
        mock_db_get.return_value = None

        with pytest.raises(ValueError, match="Flow 'nonexistent' not found"):
            get_flow("nonexistent")


class TestRemoveFlow:
    """Tests for remove_flow function."""

    @patch("cli_agent_orchestrator.services.flow_service.db_delete_flow")
    def test_remove_flow_success(self, mock_db_delete):
        """Test removing an existing flow."""
        mock_db_delete.return_value = True

        result = remove_flow("test-flow")

        assert result is True
        mock_db_delete.assert_called_once_with("test-flow")

    @patch("cli_agent_orchestrator.services.flow_service.db_delete_flow")
    def test_remove_flow_not_found(self, mock_db_delete):
        """Test removing a non-existent flow raises error."""
        mock_db_delete.return_value = False

        with pytest.raises(ValueError, match="Flow 'nonexistent' not found"):
            remove_flow("nonexistent")


class TestDisableFlow:
    """Tests for disable_flow function."""

    @patch("cli_agent_orchestrator.services.flow_service.db_update_flow_enabled")
    def test_disable_flow_success(self, mock_db_update):
        """Test disabling an existing flow."""
        mock_db_update.return_value = True

        result = disable_flow("test-flow")

        assert result is True
        mock_db_update.assert_called_once_with("test-flow", enabled=False)

    @patch("cli_agent_orchestrator.services.flow_service.db_update_flow_enabled")
    def test_disable_flow_not_found(self, mock_db_update):
        """Test disabling a non-existent flow raises error."""
        mock_db_update.return_value = False

        with pytest.raises(ValueError, match="Flow 'nonexistent' not found"):
            disable_flow("nonexistent")


class TestEnableFlow:
    """Tests for enable_flow function."""

    @patch("cli_agent_orchestrator.services.flow_service.db_update_flow_enabled")
    @patch("cli_agent_orchestrator.services.flow_service.db_get_flow")
    def test_enable_flow_success(self, mock_db_get, mock_db_update):
        """Test enabling an existing flow."""
        mock_flow = Flow(
            name="test-flow",
            file_path="/path/flow.md",
            schedule="* * * * *",
            agent_profile="developer",
            provider="kiro_cli",
            enabled=False,
            next_run=datetime.now(),
        )
        mock_db_get.return_value = mock_flow
        mock_db_update.return_value = True

        result = enable_flow("test-flow")

        assert result is True
        mock_db_update.assert_called_once()
        # Verify enabled=True was passed
        call_args = mock_db_update.call_args
        assert call_args[1]["enabled"] is True

    @patch("cli_agent_orchestrator.services.flow_service.db_get_flow")
    def test_enable_flow_not_found(self, mock_db_get):
        """Test enabling a non-existent flow raises error."""
        mock_db_get.return_value = None

        with pytest.raises(ValueError, match="Flow 'nonexistent' not found"):
            enable_flow("nonexistent")


class TestExecuteFlow:
    """Tests for execute_flow function."""

    @patch("cli_agent_orchestrator.services.flow_service.send_input")
    @patch("cli_agent_orchestrator.services.flow_service.create_terminal")
    @patch("cli_agent_orchestrator.services.flow_service.generate_session_name")
    @patch("cli_agent_orchestrator.services.flow_service.db_update_flow_run_times")
    @patch("cli_agent_orchestrator.services.flow_service.db_get_flow")
    def test_execute_flow_without_script(
        self,
        mock_db_get,
        mock_update_times,
        mock_gen_session,
        mock_create_terminal,
        mock_send_input,
    ):
        """Test executing a flow without a script."""
        # Create temp flow file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("""---
name: simple-flow
schedule: "* * * * *"
agent_profile: developer
---

Simple prompt without variables.
""")
            f.flush()
            flow_path = f.name

        mock_flow = Flow(
            name="simple-flow",
            file_path=flow_path,
            schedule="* * * * *",
            agent_profile="developer",
            provider="kiro_cli",
            script="",
            enabled=True,
            next_run=datetime.now(),
        )
        mock_db_get.return_value = mock_flow
        mock_gen_session.return_value = "cao-test-session"

        mock_terminal = MagicMock()
        mock_terminal.id = "terminal-123"
        mock_create_terminal.return_value = mock_terminal

        result = execute_flow("simple-flow")

        assert result is True
        mock_create_terminal.assert_called_once()
        mock_send_input.assert_called_once()

    @patch("cli_agent_orchestrator.services.flow_service.subprocess.run")
    @patch("cli_agent_orchestrator.services.flow_service.send_input")
    @patch("cli_agent_orchestrator.services.flow_service.create_terminal")
    @patch("cli_agent_orchestrator.services.flow_service.generate_session_name")
    @patch("cli_agent_orchestrator.services.flow_service.db_update_flow_run_times")
    @patch("cli_agent_orchestrator.services.flow_service.db_get_flow")
    def test_execute_flow_with_script_execute_true(
        self,
        mock_db_get,
        mock_update_times,
        mock_gen_session,
        mock_create_terminal,
        mock_send_input,
        mock_subprocess,
    ):
        """Test executing a flow with script that returns execute=true."""
        # Create temp flow file and script
        with tempfile.TemporaryDirectory() as tmpdir:
            flow_path = Path(tmpdir) / "flow.md"
            script_path = Path(tmpdir) / "check.sh"

            flow_path.write_text("""---
name: scripted-flow
schedule: "* * * * *"
agent_profile: developer
script: ./check.sh
---

Value is [[value]].
""")
            script_path.write_text("#!/bin/bash\necho 'test'")
            script_path.chmod(0o755)

            mock_flow = Flow(
                name="scripted-flow",
                file_path=str(flow_path),
                schedule="* * * * *",
                agent_profile="developer",
                provider="kiro_cli",
                script="./check.sh",
                enabled=True,
                next_run=datetime.now(),
            )
            mock_db_get.return_value = mock_flow
            mock_gen_session.return_value = "cao-test-session"

            # Mock script output
            mock_subprocess.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({"execute": True, "output": {"value": "42"}}),
                stderr="",
            )

            mock_terminal = MagicMock()
            mock_terminal.id = "terminal-123"
            mock_create_terminal.return_value = mock_terminal

            result = execute_flow("scripted-flow")

            assert result is True
            mock_subprocess.assert_called_once()
            mock_send_input.assert_called_once()
            # Verify the rendered prompt contains the variable value
            call_args = mock_send_input.call_args
            assert "42" in call_args[0][1]

    @patch("cli_agent_orchestrator.services.flow_service.subprocess.run")
    @patch("cli_agent_orchestrator.services.flow_service.db_update_flow_run_times")
    @patch("cli_agent_orchestrator.services.flow_service.db_get_flow")
    def test_execute_flow_with_script_execute_false(
        self, mock_db_get, mock_update_times, mock_subprocess
    ):
        """Test executing a flow with script that returns execute=false."""
        with tempfile.TemporaryDirectory() as tmpdir:
            flow_path = Path(tmpdir) / "flow.md"
            script_path = Path(tmpdir) / "check.sh"

            flow_path.write_text("""---
name: skip-flow
schedule: "* * * * *"
agent_profile: developer
script: ./check.sh
---

Prompt.
""")
            script_path.write_text("#!/bin/bash\necho 'test'")
            script_path.chmod(0o755)

            mock_flow = Flow(
                name="skip-flow",
                file_path=str(flow_path),
                schedule="* * * * *",
                agent_profile="developer",
                provider="kiro_cli",
                script="./check.sh",
                enabled=True,
                next_run=datetime.now(),
            )
            mock_db_get.return_value = mock_flow

            # Mock script output with execute=false
            mock_subprocess.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({"execute": False, "output": {}}),
                stderr="",
            )

            result = execute_flow("skip-flow")

            assert result is False  # Flow was skipped

    @patch("cli_agent_orchestrator.services.flow_service.db_get_flow")
    def test_execute_flow_not_found(self, mock_db_get):
        """Test executing a non-existent flow raises error."""
        mock_db_get.return_value = None

        with pytest.raises(ValueError, match="Flow 'nonexistent' not found"):
            execute_flow("nonexistent")

    @patch("cli_agent_orchestrator.services.flow_service.subprocess.run")
    @patch("cli_agent_orchestrator.services.flow_service.db_get_flow")
    def test_execute_flow_script_fails(self, mock_db_get, mock_subprocess):
        """Test that script failure raises error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            flow_path = Path(tmpdir) / "flow.md"
            script_path = Path(tmpdir) / "check.sh"

            flow_path.write_text("""---
name: fail-flow
schedule: "* * * * *"
agent_profile: developer
script: ./check.sh
---

Prompt.
""")
            script_path.write_text("#!/bin/bash\nexit 1")
            script_path.chmod(0o755)

            mock_flow = Flow(
                name="fail-flow",
                file_path=str(flow_path),
                schedule="* * * * *",
                agent_profile="developer",
                provider="kiro_cli",
                script="./check.sh",
                enabled=True,
                next_run=datetime.now(),
            )
            mock_db_get.return_value = mock_flow

            mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="Script error")

            with pytest.raises(ValueError, match="Script failed"):
                execute_flow("fail-flow")

    @patch("cli_agent_orchestrator.services.flow_service.subprocess.run")
    @patch("cli_agent_orchestrator.services.flow_service.db_get_flow")
    def test_execute_flow_script_invalid_json(self, mock_db_get, mock_subprocess):
        """Test that invalid JSON from script raises error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            flow_path = Path(tmpdir) / "flow.md"
            script_path = Path(tmpdir) / "check.sh"

            flow_path.write_text("""---
name: bad-json-flow
schedule: "* * * * *"
agent_profile: developer
script: ./check.sh
---

Prompt.
""")
            script_path.write_text("#!/bin/bash\necho 'not json'")
            script_path.chmod(0o755)

            mock_flow = Flow(
                name="bad-json-flow",
                file_path=str(flow_path),
                schedule="* * * * *",
                agent_profile="developer",
                provider="kiro_cli",
                script="./check.sh",
                enabled=True,
                next_run=datetime.now(),
            )
            mock_db_get.return_value = mock_flow

            mock_subprocess.return_value = MagicMock(
                returncode=0, stdout="not valid json", stderr=""
            )

            with pytest.raises(ValueError, match="not valid JSON"):
                execute_flow("bad-json-flow")


class TestGetFlowsToRun:
    """Tests for get_flows_to_run function."""

    @patch("cli_agent_orchestrator.services.flow_service.db_get_flows_to_run")
    def test_get_flows_to_run_returns_due_flows(self, mock_db_get):
        """Test that get_flows_to_run returns flows that are due."""
        mock_flows = [
            Flow(
                name="due-flow",
                file_path="/path/flow.md",
                schedule="* * * * *",
                agent_profile="developer",
                provider="kiro_cli",
                enabled=True,
                next_run=datetime.now(),
            )
        ]
        mock_db_get.return_value = mock_flows

        result = get_flows_to_run()

        assert len(result) == 1
        assert result[0].name == "due-flow"

    @patch("cli_agent_orchestrator.services.flow_service.db_get_flows_to_run")
    def test_get_flows_to_run_empty(self, mock_db_get):
        """Test get_flows_to_run with no due flows."""
        mock_db_get.return_value = []

        result = get_flows_to_run()

        assert result == []

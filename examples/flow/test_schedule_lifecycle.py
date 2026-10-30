"""Focused tests for the examples/flow `cao schedule` lifecycle demo.

This file lives under examples/, not test/, so it is outside the project's
default pytest ``testpaths`` (see pyproject.toml) and the main suite never
collects it. Run it explicitly:

    uv run pytest --no-cov examples/flow/test_schedule_lifecycle.py -v

Scope: verify that *this example's* artifacts (local-task.md + gate.sh) are
well-formed and behave correctly through the real flow_service/CLI code --
not re-derive generic flow_service behavior, which test/services/test_flow_service.py
and test/cli/commands/test_schedule.py already cover exhaustively. Nothing
here waits on wall-clock cron timing, a running cao-server, or a live agent
provider CLI: the terminal-launch boundary (create_terminal/send_input) is
mocked, exactly as the main suite mocks it.
"""

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.schedule import schedule
from cli_agent_orchestrator.models.flow import Flow
from cli_agent_orchestrator.services.flow_service import (
    _parse_flow_file,
    add_flow,
    disable_flow,
    enable_flow,
    execute_flow,
    list_flows,
    remove_flow,
)
from cli_agent_orchestrator.utils.template import render_template

EXAMPLE_DIR = Path(__file__).resolve().parent
FLOW_FILE = EXAMPLE_DIR / "local-task.md"
GATE_SCRIPT = EXAMPLE_DIR / "gate.sh"

ISO8601_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _real_flow(**overrides) -> Flow:
    """Build a Flow from the real local-task.md, with test-only overrides."""
    metadata, _ = _parse_flow_file(FLOW_FILE)
    defaults = dict(
        name=metadata["name"],
        file_path=str(FLOW_FILE),
        schedule=metadata["schedule"],
        agent_profile=metadata["agent_profile"],
        script=metadata.get("script", ""),
        enabled=True,
        next_run=datetime.now(),
    )
    defaults.update(overrides)
    return Flow(**defaults)


class TestLocalTaskFlowDefinition:
    """Parsing/registration: local-task.md is a valid flow definition."""

    def test_frontmatter_has_required_fields(self):
        metadata, prompt = _parse_flow_file(FLOW_FILE)

        assert metadata["name"] == "local-task-demo"
        assert metadata["schedule"] == "*/10 * * * *"
        assert metadata["agent_profile"] == "developer"
        assert metadata["script"] == "./gate.sh"
        assert "[[timestamp]]" in prompt
        assert "[[log_file]]" in prompt

    @patch("cli_agent_orchestrator.services.flow_service.db_create_flow")
    def test_add_flow_registers_local_task(self, mock_db_create):
        mock_db_create.return_value = _real_flow()

        result = add_flow(str(FLOW_FILE))

        assert result.name == "local-task-demo"
        assert result.agent_profile == "developer"
        assert result.script == "./gate.sh"
        mock_db_create.assert_called_once()

    def test_prompt_template_placeholders_match_gate_output(self):
        """The template's [[placeholders]] must match gate.sh's "output" keys
        exactly, or execute_flow's render_template() call raises."""
        _, prompt = _parse_flow_file(FLOW_FILE)

        rendered = render_template(
            prompt, {"timestamp": "2026-01-01T00:00:00Z", "log_file": "/tmp/x.log"}
        )

        assert "2026-01-01T00:00:00Z" in rendered
        assert "/tmp/x.log" in rendered


class TestGateScript:
    """Gating contract: gate.sh emits {"execute": bool, "output": dict} on both paths."""

    def _run_gate(self, tmp_path, skip: bool):
        env = {
            "CAO_EXAMPLE_SKIP_FLAG": str(tmp_path / "skip"),
            "CAO_EXAMPLE_LOG_FILE": str(tmp_path / "local-task.log"),
        }
        if skip:
            (tmp_path / "skip").touch()
        return subprocess.run(
            [str(GATE_SCRIPT)], capture_output=True, text=True, timeout=10, env=env
        )

    def test_allow_path_returns_valid_contract(self, tmp_path):
        result = self._run_gate(tmp_path, skip=False)

        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert set(payload.keys()) == {"execute", "output"}
        assert payload["execute"] is True
        assert set(payload["output"].keys()) == {"timestamp", "log_file"}
        assert ISO8601_RE.match(payload["output"]["timestamp"])
        assert payload["output"]["log_file"] == str(tmp_path / "local-task.log")

    def test_skip_path_when_flag_present(self, tmp_path):
        result = self._run_gate(tmp_path, skip=True)

        assert result.returncode == 0
        assert json.loads(result.stdout) == {"execute": False, "output": {}}

    def test_output_is_single_json_line(self, tmp_path):
        """execute_flow() does json.loads(result.stdout) on the whole stream --
        any extra output (banners, warnings) would break the contract."""
        result = self._run_gate(tmp_path, skip=False)

        assert len(result.stdout.strip().splitlines()) == 1

    def test_flag_removal_restores_allow_path(self, tmp_path):
        """Toggling the flag file is reversible: skip, then allow again."""
        skipped = self._run_gate(tmp_path, skip=True)
        assert json.loads(skipped.stdout)["execute"] is False

        (tmp_path / "skip").unlink()
        allowed = self._run_gate(tmp_path, skip=False)
        assert json.loads(allowed.stdout)["execute"] is True


class TestScheduleLifecycleCommands:
    """Lifecycle commands (add/list/disable/enable/remove/run) against local-task.md."""

    @patch("cli_agent_orchestrator.services.flow_service.db_create_flow")
    @patch("cli_agent_orchestrator.services.flow_service.db_list_flows")
    def test_add_then_list_reflects_flow(self, mock_db_list, mock_db_create):
        added = _real_flow()
        mock_db_create.return_value = added
        mock_db_list.return_value = [added]

        add_flow(str(FLOW_FILE))
        flows = list_flows()

        assert len(flows) == 1
        assert flows[0].name == "local-task-demo"
        # list_flows() enriches from the real file on disk.
        assert "[[timestamp]]" in flows[0].prompt_template

    @patch("cli_agent_orchestrator.services.flow_service.db_update_flow_enabled")
    def test_disable_flow(self, mock_db_update):
        mock_db_update.return_value = True

        assert disable_flow("local-task-demo") is True
        mock_db_update.assert_called_once_with("local-task-demo", enabled=False)

    @patch("cli_agent_orchestrator.services.flow_service.db_update_flow_enabled")
    @patch("cli_agent_orchestrator.services.flow_service.db_get_flow")
    def test_enable_flow_recalculates_next_run(self, mock_db_get, mock_db_update):
        mock_db_get.return_value = _real_flow(enabled=False)
        mock_db_update.return_value = True

        assert enable_flow("local-task-demo") is True
        _, kwargs = mock_db_update.call_args
        assert kwargs["enabled"] is True
        assert kwargs["next_run"].replace(tzinfo=None) >= datetime.now().replace(microsecond=0)

    @patch("cli_agent_orchestrator.services.flow_service.db_delete_flow")
    def test_remove_flow(self, mock_db_delete):
        mock_db_delete.return_value = True

        assert remove_flow("local-task-demo") is True
        mock_db_delete.assert_called_once_with("local-task-demo")

    @patch("cli_agent_orchestrator.cli.commands.schedule.init_db")
    @patch("cli_agent_orchestrator.cli.commands.schedule.flow_service")
    def test_cli_lifecycle_sequence_matches_run_lifecycle_script(self, mock_service, mock_init_db):
        """Same command order as run-lifecycle.sh: add, list, run, disable,
        list, enable, run, remove -- driven through the CLI layer only."""
        runner = CliRunner()
        added = MagicMock()
        added.name = "local-task-demo"
        added.schedule = "*/10 * * * *"
        added.agent_profile = "developer"
        added.next_run = datetime.now()
        mock_service.add_flow.return_value = added
        mock_service.list_flows.return_value = []
        mock_service.execute_flow = AsyncMock(side_effect=[False, True])

        with runner.isolated_filesystem():
            Path("local-task.md").write_text(FLOW_FILE.read_text())

            assert runner.invoke(schedule, ["add", "local-task.md"]).exit_code == 0
            assert runner.invoke(schedule, ["list"]).exit_code == 0
            run1 = runner.invoke(schedule, ["run", "local-task-demo"])
            assert run1.exit_code == 0
            assert "skipped" in run1.output
            assert runner.invoke(schedule, ["disable", "local-task-demo"]).exit_code == 0
            assert runner.invoke(schedule, ["list"]).exit_code == 0
            assert runner.invoke(schedule, ["enable", "local-task-demo"]).exit_code == 0
            run2 = runner.invoke(schedule, ["run", "local-task-demo"])
            assert run2.exit_code == 0
            assert "executed successfully" in run2.output
            assert runner.invoke(schedule, ["remove", "local-task-demo"]).exit_code == 0

        assert mock_service.execute_flow.await_count == 2


class TestExecuteFlowGatingIntegration:
    """execute_flow() driving the real gate.sh, with only the terminal-launch
    boundary (create_terminal/send_input/backend) mocked."""

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.flow_service.send_input")
    @patch("cli_agent_orchestrator.services.flow_service.create_terminal")
    @patch("cli_agent_orchestrator.services.flow_service.get_backend")
    @patch("cli_agent_orchestrator.services.flow_service.db_update_flow_run_times")
    @patch("cli_agent_orchestrator.services.flow_service.db_get_flow")
    async def test_allow_path_launches_session_with_rendered_prompt(
        self,
        mock_db_get,
        mock_update_times,
        mock_get_backend,
        mock_create_terminal,
        mock_send_input,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.setenv("CAO_EXAMPLE_SKIP_FLAG", str(tmp_path / "skip"))
        monkeypatch.setenv("CAO_EXAMPLE_LOG_FILE", str(tmp_path / "local-task.log"))
        mock_db_get.return_value = _real_flow()
        mock_get_backend.return_value.session_exists.return_value = False
        mock_terminal = MagicMock()
        mock_terminal.id = "terminal-123"
        mock_create_terminal.return_value = mock_terminal

        result = await execute_flow("local-task-demo")

        assert result is True
        mock_create_terminal.assert_called_once()
        mock_send_input.assert_called_once()
        rendered_prompt = mock_send_input.call_args[0][1]
        assert str(tmp_path / "local-task.log") in rendered_prompt
        assert re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", rendered_prompt)

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.flow_service.send_input")
    @patch("cli_agent_orchestrator.services.flow_service.create_terminal")
    @patch("cli_agent_orchestrator.services.flow_service.db_update_flow_run_times")
    @patch("cli_agent_orchestrator.services.flow_service.db_get_flow")
    async def test_skip_path_never_launches_session(
        self,
        mock_db_get,
        mock_update_times,
        mock_create_terminal,
        mock_send_input,
        tmp_path,
        monkeypatch,
    ):
        skip_flag = tmp_path / "skip"
        skip_flag.touch()
        monkeypatch.setenv("CAO_EXAMPLE_SKIP_FLAG", str(skip_flag))
        monkeypatch.setenv("CAO_EXAMPLE_LOG_FILE", str(tmp_path / "local-task.log"))
        mock_db_get.return_value = _real_flow()

        result = await execute_flow("local-task-demo")

        assert result is False
        mock_create_terminal.assert_not_called()
        mock_send_input.assert_not_called()


class TestCleanupContract:
    """The example's cleanup guarantees: reversible gating state, and
    run-lifecycle.sh actually wires up an exit trap."""

    def test_gate_writes_are_confined_to_configured_log_file(self, tmp_path):
        log_file = tmp_path / "nested" / "local-task.log"
        result = subprocess.run(
            [str(GATE_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=10,
            env={
                "CAO_EXAMPLE_SKIP_FLAG": str(tmp_path / "skip"),
                "CAO_EXAMPLE_LOG_FILE": str(log_file),
            },
        )

        payload = json.loads(result.stdout)
        assert payload["output"]["log_file"] == str(log_file)
        # gate.sh only mkdir -p's the log file's parent; it does not create
        # the log file itself (the agent's task is to write it).
        assert log_file.parent.exists()
        assert not log_file.exists()

    def test_run_lifecycle_script_has_exit_trap_and_removes_flow(self):
        script = (EXAMPLE_DIR / "run-lifecycle.sh").read_text()

        assert "trap cleanup EXIT" in script
        assert "cao schedule remove" in script
        assert "cao shutdown --session" in script

"""Unit tests for Copilot CLI provider."""

from __future__ import annotations

import json
import shlex
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.copilot_cli import CopilotCliProvider


class TestCopilotCliProviderCommand:
    @patch("cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._ensure_copilot_config")
    @patch(
        "cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._select_permissive_flag"
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.subprocess.run")
    @patch.dict("os.environ", {}, clear=True)
    def test_command_builds_default(self, mock_run, mock_permissive, mock_ensure_config):
        mock_run.return_value.stdout = (
            "--no-custom-instructions --disable-builtin-mcps --no-auto-update "
            "--no-ask-user --autopilot"
        )
        mock_permissive.return_value = "--allow-all"
        provider = CopilotCliProvider("test1234", "test-session", "window-0")

        command = provider._command()

        assert "copilot" in command
        assert "--allow-all" in command
        assert "--no-custom-instructions" in command
        assert "--no-ask-user" in command
        assert "--model gpt-5-mini" in command
        assert "--config-dir" in command
        assert "--autopilot" in command
        assert "--no-auto-update" not in command
        assert "--add-dir" in command
        mock_ensure_config.assert_called_once()

    @patch.dict("os.environ", {"CAO_COPILOT_COMMAND": "copilot --model gpt-4.1"}, clear=True)
    def test_command_honors_full_command_override(self):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider._command() == "copilot --model gpt-4.1"

    @patch("cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._ensure_copilot_config")
    @patch(
        "cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._select_permissive_flag"
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.load_agent_profile")
    @patch("cli_agent_orchestrator.providers.copilot_cli.subprocess.run")
    def test_command_uses_native_agent_flag_with_runtime_profile(
        self, mock_run, mock_load_profile, mock_permissive, _mock_ensure_config
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_run.return_value.stdout = "--agent --no-custom-instructions --autopilot"
            mock_permissive.return_value = ""
            mock_profile = MagicMock()
            mock_profile.description = "Runtime Copilot profile"
            mock_profile.system_prompt = "You are a refactor specialist."
            mock_load_profile.return_value = mock_profile

            with patch.dict(
                "os.environ",
                {
                    "CAO_COPILOT_CONFIG_DIR": tmpdir,
                    "CAO_COPILOT_ALLOW_ALL": "0",
                },
                clear=True,
            ):
                provider = CopilotCliProvider(
                    "test1234", "test-session", "window-0", agent_profile="refactor-agent"
                )
                command = provider._command()

            parts = shlex.split(command)
            assert "--agent" in parts
            runtime_agent_name = parts[parts.index("--agent") + 1]
            assert runtime_agent_name.startswith("cao-refactor-agent-test1234")
            runtime_agent_file = Path(tmpdir) / "agents" / f"{runtime_agent_name}.agent.md"
            assert runtime_agent_file.exists()
            text = runtime_agent_file.read_text(encoding="utf-8")
            assert 'description: "Runtime Copilot profile"' in text
            assert "You are a refactor specialist." in text

    @patch("cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._ensure_copilot_config")
    @patch(
        "cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._select_permissive_flag"
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.load_agent_profile")
    @patch("cli_agent_orchestrator.providers.copilot_cli.subprocess.run")
    def test_analysis_supervisor_runtime_profile_includes_three_dataset_guardrails(
        self, mock_run, mock_load_profile, mock_permissive, _mock_ensure_config
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_run.return_value.stdout = "--agent --no-custom-instructions --autopilot"
            mock_permissive.return_value = ""
            mock_profile = MagicMock()
            mock_profile.description = "Supervisor profile"
            mock_profile.system_prompt = "Coordinate workers."
            mock_load_profile.return_value = mock_profile

            with patch.dict(
                "os.environ",
                {
                    "CAO_COPILOT_CONFIG_DIR": tmpdir,
                    "CAO_COPILOT_ALLOW_ALL": "0",
                },
                clear=True,
            ):
                provider = CopilotCliProvider(
                    "test1234", "test-session", "window-0", agent_profile="analysis_supervisor"
                )
                command = provider._command()

            parts = shlex.split(command)
            runtime_agent_name = parts[parts.index("--agent") + 1]
            runtime_agent_file = Path(tmpdir) / "agents" / f"{runtime_agent_name}.agent.md"
            text = runtime_agent_file.read_text(encoding="utf-8")
            assert "exactly three assign calls" in text
            assert "wait for three distinct analyst callbacks" in text
            assert "Your supervisor terminal ID is: test1234" in text
            assert "Never run shell commands to discover CAO_TERMINAL_ID" in text
            assert "Report Summary" in text

    @patch("cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._ensure_copilot_config")
    @patch(
        "cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._select_permissive_flag"
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.load_agent_profile")
    @patch("cli_agent_orchestrator.providers.copilot_cli.subprocess.run")
    def test_report_generator_runtime_profile_includes_required_headings(
        self, mock_run, mock_load_profile, mock_permissive, _mock_ensure_config
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_run.return_value.stdout = "--agent --no-custom-instructions --autopilot"
            mock_permissive.return_value = ""
            mock_profile = MagicMock()
            mock_profile.description = "Report profile"
            mock_profile.system_prompt = "Generate report templates."
            mock_load_profile.return_value = mock_profile

            with patch.dict(
                "os.environ",
                {
                    "CAO_COPILOT_CONFIG_DIR": tmpdir,
                    "CAO_COPILOT_ALLOW_ALL": "0",
                },
                clear=True,
            ):
                provider = CopilotCliProvider(
                    "test1234", "test-session", "window-0", agent_profile="report_generator"
                )
                command = provider._command()

            parts = shlex.split(command)
            runtime_agent_name = parts[parts.index("--agent") + 1]
            runtime_agent_file = Path(tmpdir) / "agents" / f"{runtime_agent_name}.agent.md"
            text = runtime_agent_file.read_text(encoding="utf-8")
            assert "Report Template" in text
            assert "'Summary', 'Analysis', and 'Conclusion'" in text

    @patch("cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._ensure_copilot_config")
    @patch(
        "cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._select_permissive_flag"
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.load_agent_profile")
    @patch("cli_agent_orchestrator.providers.copilot_cli.subprocess.run")
    def test_command_uses_passed_agent_when_profile_load_fails(
        self, mock_run, mock_load_profile, mock_permissive, _mock_ensure_config
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_run.return_value.stdout = "--agent"
            mock_permissive.return_value = ""
            mock_load_profile.side_effect = RuntimeError("not found")
            with patch.dict(
                "os.environ",
                {
                    "CAO_COPILOT_CONFIG_DIR": tmpdir,
                    "CAO_COPILOT_ALLOW_ALL": "0",
                },
                clear=True,
            ):
                provider = CopilotCliProvider(
                    "test1234", "test-session", "window-0", agent_profile="repo-agent"
                )
                command = provider._command()

            parts = shlex.split(command)
            assert parts[parts.index("--agent") + 1] == "repo-agent"

    @patch("cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._ensure_copilot_config")
    @patch(
        "cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._select_permissive_flag"
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.subprocess.run")
    @patch.dict(
        "os.environ",
        {
            "CAO_COPILOT_AUTOPILOT": "0",
            "CAO_COPILOT_ALLOW_ALL": "0",
            "CAO_COPILOT_NO_CUSTOM_INSTRUCTIONS": "0",
            "CAO_COPILOT_DISABLE_BUILTIN_MCPS": "1",
            "CAO_COPILOT_MODEL": "gpt-4.1",
            "CAO_COPILOT_CONFIG_DIR": "/tmp/copilot_cfg",
            "CAO_COPILOT_ADD_DIRS": "/repo/a:/repo/b",
            "CAO_COPILOT_ADDITIONAL_MCP_CONFIG": "/tmp/mcp.json",
        },
        clear=True,
    )
    def test_command_respects_config_flags(self, mock_run, mock_permissive, mock_ensure_config):
        mock_run.return_value.stdout = (
            "--no-custom-instructions --disable-builtin-mcps --no-auto-update --autopilot"
        )
        mock_permissive.return_value = "--allow-all"
        provider = CopilotCliProvider("test1234", "test-session", "window-0")

        command = provider._command()

        assert "--allow-all" not in command
        assert "--autopilot" not in command
        assert "--no-custom-instructions" not in command
        assert "--no-auto-update" in command
        assert "--disable-builtin-mcps" in command
        assert "--model gpt-4.1" in command
        assert "--config-dir /tmp/copilot_cfg" in command
        assert "--add-dir /repo/a" in command
        assert "--add-dir /repo/b" in command
        parts = shlex.split(command)
        mcp_idx = parts.index("--additional-mcp-config")
        runtime_cfg = parts[mcp_idx + 1].lstrip("@")
        assert runtime_cfg.endswith("/cao_copilot_mcp_test1234.json")
        payload = json.loads(Path(runtime_cfg).read_text(encoding="utf-8"))
        assert "cao-mcp-server" in payload.get("mcpServers", {})
        assert payload["mcpServers"]["cao-mcp-server"]["env"]["CAO_TERMINAL_ID"] == "test1234"
        mock_ensure_config.assert_called_once_with(Path("/tmp/copilot_cfg"), "gpt-4.1", "high")

    @patch.dict("os.environ", {"CAO_COPILOT_PERMISSIVE_FLAG": "allow-all"}, clear=True)
    @patch("cli_agent_orchestrator.providers.copilot_cli.subprocess.run")
    def test_select_permissive_flag_allow_all(self, mock_run):
        mock_run.return_value.stdout = "options: --allow-all --yolo"
        assert CopilotCliProvider._select_permissive_flag() == "--allow-all"

    @patch.dict("os.environ", {"CAO_COPILOT_PERMISSIVE_FLAG": "yolo"}, clear=True)
    @patch("cli_agent_orchestrator.providers.copilot_cli.subprocess.run")
    def test_select_permissive_flag_yolo(self, mock_run):
        mock_run.return_value.stdout = "options: --yolo"
        assert CopilotCliProvider._select_permissive_flag() == "--yolo"

    @patch.dict("os.environ", {"CAO_COPILOT_PERMISSIVE_FLAG": "none"}, clear=True)
    @patch("cli_agent_orchestrator.providers.copilot_cli.subprocess.run")
    def test_select_permissive_flag_none(self, mock_run):
        mock_run.return_value.stdout = "options: --allow-all --yolo"
        assert CopilotCliProvider._select_permissive_flag() == ""


class TestCopilotCliProviderInitialization:
    @patch("cli_agent_orchestrator.providers.copilot_cli.wait_for_shell")
    def test_initialize_shell_timeout(self, mock_wait_shell):
        mock_wait_shell.return_value = False
        provider = CopilotCliProvider("test1234", "test-session", "window-0")

        with pytest.raises(TimeoutError, match="Shell initialization timed out"):
            provider.initialize()

    @patch("cli_agent_orchestrator.providers.copilot_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    @patch.object(CopilotCliProvider, "_accept_trust_prompts")
    @patch.object(CopilotCliProvider, "_safe_history")
    def test_initialize_success_with_ui_probe(
        self, mock_history, mock_accept, mock_tmux, mock_wait_shell
    ):
        mock_wait_shell.return_value = True
        mock_history.return_value = "GitHub Copilot v0.0.415\n❯ Type @ to mention files"
        provider = CopilotCliProvider("test1234", "test-session", "window-0")

        assert provider.initialize() is True
        mock_tmux.send_keys.assert_called_once()
        mock_accept.assert_called_once()

    @patch("cli_agent_orchestrator.providers.copilot_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.copilot_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    @patch.object(CopilotCliProvider, "_accept_trust_prompts")
    @patch.object(CopilotCliProvider, "_safe_history")
    def test_initialize_does_not_short_circuit_while_loading_environment(
        self, mock_history, mock_accept, mock_tmux, mock_wait_shell, mock_wait_status
    ):
        mock_wait_shell.return_value = True
        mock_history.return_value = (
            "GitHub Copilot v0.0.415\n" "∙ Loading environment:\n" "❯ Type @ to mention files"
        )
        mock_wait_status.return_value = True
        provider = CopilotCliProvider("test1234", "test-session", "window-0")

        assert provider.initialize() is True
        mock_tmux.send_keys.assert_called_once()
        mock_accept.assert_called_once()
        mock_wait_status.assert_called()

    @patch("cli_agent_orchestrator.providers.copilot_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.copilot_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    @patch.object(CopilotCliProvider, "_accept_trust_prompts")
    @patch.object(CopilotCliProvider, "_safe_history")
    def test_initialize_timeout_when_no_ui_and_not_idle(
        self,
        mock_history,
        mock_accept,
        mock_tmux,
        mock_wait_shell,
        mock_wait_status,
    ):
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = False
        mock_history.return_value = "still initializing..."
        provider = CopilotCliProvider("test1234", "test-session", "window-0")

        with pytest.raises(TimeoutError, match="Copilot initialization timed out"):
            provider.initialize()
        assert mock_accept.call_count == 2
        mock_tmux.send_keys.assert_called_once()


class TestCopilotCliProviderTrustPrompts:
    @patch("cli_agent_orchestrator.providers.copilot_cli.time.sleep")
    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    def test_accept_trust_prompts_answers_yes_then_returns(self, mock_tmux, _mock_sleep):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        with (
            patch.object(
                provider,
                "_safe_history",
                side_effect=[
                    "Do you trust all the actions in this folder? [y/N]",
                    "GitHub Copilot v0.0.415\n❯ Type @ to mention files",
                ],
            ),
            patch.object(provider, "_send_enter") as mock_enter,
        ):
            provider._accept_trust_prompts(timeout=2.0)

        mock_tmux.send_keys.assert_called_with("test-session", "window-0", "Y")
        assert mock_enter.call_count >= 1


class TestCopilotCliProviderStatusDetection:
    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    def test_get_status_waiting_user_answer(self, mock_tmux):
        mock_tmux.get_history.return_value = "confirm folder trust [y/n]"
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status() == TerminalStatus.WAITING_USER_ANSWER

    def test_get_status_recovers_from_waiting_prompt_after_runtime_auto_accept(self):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        provider._initialized = True
        waiting = "confirm folder trust\nDo you trust the files in this folder?"
        idle = "GitHub Copilot v0.0.415\n❯ Type @ to mention files"

        with (
            patch.object(provider, "_safe_history", side_effect=[waiting, idle]),
            patch.object(provider, "_accept_trust_prompts") as mock_accept,
        ):
            assert provider.get_status() == TerminalStatus.IDLE
            mock_accept.assert_called_once()

    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    def test_get_status_completed_when_response_is_markdown_quote(self, mock_tmux):
        mock_tmux.get_history.return_value = "❯ summarize findings\n> key point from analysis\n❯ "
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status() == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    def test_get_status_not_idle_when_tail_is_markdown_quote_without_prompt(self, mock_tmux):
        mock_tmux.get_history.return_value = "assistant note\n> quoted explanation"
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status() == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    def test_get_status_idle_with_no_user_message(self, mock_tmux):
        mock_tmux.get_history.return_value = "GitHub Copilot v0.0.415\n❯ Type @ to mention files"
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status() == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    def test_get_status_processing_while_loading_environment(self, mock_tmux):
        mock_tmux.get_history.return_value = (
            "GitHub Copilot v0.0.415\n" "∙ Loading environment:\n" "❯ Type @ to mention files"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status() == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    def test_get_status_idle_with_remaining_reqs_footer(self, mock_tmux):
        mock_tmux.get_history.return_value = (
            "❯  Type @ to mention files, / for commands, or ? for shortcuts\n"
            " shift+tab switch mode                      Remaining reqs.: 79.66666666666666%\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status() == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    def test_get_status_idle_with_wrapped_shortcuts_footer(self, mock_tmux):
        mock_tmux.get_history.return_value = (
            "❯  Type @ to mention files, # for issues/PRs, / for commands, or ? for\n"
            "  shortcuts\n"
            " shift+tab switch mode                      Remaining reqs.: 79.66666666666666%\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status() == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    def test_get_status_idle_when_startup_has_auto_update_error_noise(self, mock_tmux):
        mock_tmux.get_history.return_value = (
            "● Error auto updating: Failed to fetch latest release: API rate limit exceeded\n"
            "● Environment loaded: 1 MCP server\n"
            "❯  Type @ to mention files, / for commands, or ? for shortcuts\n"
            " shift+tab switch mode                      Remaining reqs.: 79.66666666666666%\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status() == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    def test_get_status_completed_without_user_line_when_response_present(self, mock_tmux):
        mock_tmux.get_history.return_value = (
            "● Mean = 3; Median = 3; Standard deviation = 1.41\n"
            "❯  Type @ to mention files, / for commands, or ? for shortcuts\n"
            " shift+tab switch mode\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status() == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.copilot_cli.time.time")
    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    def test_get_status_completed_requires_stable_output_when_initialized(
        self, mock_tmux, mock_time
    ):
        mock_tmux.get_history.return_value = (
            "● Mean = 3; Median = 3; Standard deviation = 1.41\n"
            "❯  Type @ to mention files, / for commands, or ? for shortcuts\n"
            " shift+tab switch mode\n"
        )
        mock_time.side_effect = [100.0, 101.0, 104.5]

        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        provider._initialized = True

        assert provider.get_status() == TerminalStatus.PROCESSING
        assert provider.get_status() == TerminalStatus.PROCESSING
        assert provider.get_status() == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.copilot_cli.time.time")
    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    def test_get_status_stability_ignores_footer_churn(self, mock_tmux, mock_time):
        mock_tmux.get_history.side_effect = [
            (
                "❯ analyze dataset\n"
                "DONE\n"
                "❯  Type @ to mention files, / for commands, or ? for shortcuts\n"
                " shift+tab switch mode                      Remaining reqs.: 79.66666666666666%\n"
            ),
            (
                "❯ analyze dataset\n"
                "DONE\n"
                "❯  Type @ to mention files, / for commands, or ? for shortcuts\n"
                " shift+tab switch mode                      Remaining reqs.: 79.33333333333333%\n"
            ),
            (
                "❯ analyze dataset\n"
                "DONE\n"
                "❯  Type @ to mention files, / for commands, or ? for shortcuts\n"
                " shift+tab switch mode                      Remaining reqs.: 79.00000000000000%\n"
            ),
        ]
        mock_time.side_effect = [100.0, 101.0, 104.5]

        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        provider._initialized = True
        provider._awaiting_user_response = True

        assert provider.get_status() == TerminalStatus.PROCESSING
        assert provider.get_status() == TerminalStatus.PROCESSING
        assert provider.get_status() == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.copilot_cli.time.time")
    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    def test_get_status_stability_ignores_prompt_line_churn(self, mock_tmux, mock_time):
        mock_tmux.get_history.side_effect = [
            (
                "● Selected custom agent: cao-analysis_supervisor-test1234\n"
                "❯ Type @ to mention files, # for issues/PRs, / for commands, or ? for\n"
                "  shortcuts\n"
                " shift+tab switch mode                      Remaining reqs.: 79.66666666666666%\n"
            ),
            (
                "● Selected custom agent: cao-analysis_supervisor-test1234\n"
                "❯  Type @ to mention files, # for issues/PRs, / for commands, or ? for\n"
                "  shortcuts\n"
                " shift+tab switch mode                      Remaining reqs.: 79.33333333333333%\n"
            ),
            (
                "● Selected custom agent: cao-analysis_supervisor-test1234\n"
                "❯ Type @ to mention files, # for issues/PRs, / for commands, or ? for\n"
                "  shortcuts\n"
                " shift+tab switch mode                      Remaining reqs.: 79.00000000000000%\n"
            ),
        ]
        mock_time.side_effect = [100.0, 101.0, 104.5]

        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        provider._initialized = True

        assert provider.get_status() == TerminalStatus.PROCESSING
        assert provider.get_status() == TerminalStatus.PROCESSING
        assert provider.get_status() == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.copilot_cli.time.time")
    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    def test_get_status_stability_ignores_usage_churn(self, mock_tmux, mock_time):
        mock_tmux.get_history.side_effect = [
            (
                "❯ analyze dataset\n"
                "Summary complete.\n"
                "Total usage est: 0 Premium requests\n"
                "❯  Type @ to mention files, / for commands, or ? for shortcuts\n"
            ),
            (
                "❯ analyze dataset\n"
                "Summary complete.\n"
                "Total usage est: 1 Premium requests\n"
                "❯  Type @ to mention files, / for commands, or ? for shortcuts\n"
            ),
            (
                "❯ analyze dataset\n"
                "Summary complete.\n"
                "Total usage est: 2 Premium requests\n"
                "❯  Type @ to mention files, / for commands, or ? for shortcuts\n"
            ),
        ]
        mock_time.side_effect = [100.0, 101.0, 104.5]

        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        provider._initialized = True
        provider._awaiting_user_response = True

        assert provider.get_status() == TerminalStatus.PROCESSING
        assert provider.get_status() == TerminalStatus.PROCESSING
        assert provider.get_status() == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    def test_get_status_processing_when_no_idle_prompt(self, mock_tmux):
        mock_tmux.get_history.return_value = "Working on edits..."
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status() == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    def test_get_status_error_while_processing_if_error_after_user(self, mock_tmux):
        mock_tmux.get_history.return_value = "❯ refactor this\nError: failed to parse"
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status() == TerminalStatus.ERROR

    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    def test_get_status_completed_when_response_present_and_idle(self, mock_tmux):
        mock_tmux.get_history.return_value = "❯ refactor this\n● Edit file.py (+1 -1)\n❯ "
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status() == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    def test_get_status_error_when_idle_and_error_without_assistant_marker(self, mock_tmux):
        mock_tmux.get_history.return_value = "❯ refactor this\nError: failed to parse\n❯ "
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status() == TerminalStatus.ERROR

    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    def test_get_status_completed_when_idle_and_error_with_assistant_marker(self, mock_tmux):
        mock_tmux.get_history.return_value = "❯ refactor this\nassistant: note\nError: sample\n❯ "
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status() == TerminalStatus.COMPLETED


class TestCopilotCliProviderMessageExtraction:
    def test_extract_last_message_from_post_user_lines(self):
        output = "❯ refactor\n● Edit x.py (+2 -1)\nDONE\n❯ "
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)
        assert "Edit x.py" in message
        assert "DONE" in message

    def test_extract_last_message_keeps_markdown_quote_line(self):
        output = "❯ summarize\n> key point from analysis\n❯ "
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.extract_last_message_from_script(output) == "> key point from analysis"

    def test_extract_last_message_strips_footer_chrome(self):
        output = (
            "❯ analyze dataset\n"
            "Population std dev = 1.41\n"
            "~/cli-agent-orchestrator[⎇ feat/copilot-provider*]      gpt-5-mini (high) (0x)\n"
            "──────────────────────────────────────────────────────────────────────────────\n"
            "❯  Type @ to mention files, / for commands, or ? for shortcuts\n"
            "──────────────────────────────────────────────────────────────────────────────\n"
            " shift+tab switch mode\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.extract_last_message_from_script(output) == "Population std dev = 1.41"

    def test_extract_last_message_from_assistant_fallback(self):
        output = "assistant: Completed task successfully."
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.extract_last_message_from_script(output) == "Completed task successfully."

    def test_extract_last_message_from_fallback_strips_footer_chrome(self):
        output = (
            "● Completed task successfully.\n"
            "~/cli-agent-orchestrator[⎇ feat/copilot-provider*]      gpt-5-mini (high) (0x)\n"
            "──────────────────────────────────────────────────────────────────────────────\n"
            "❯  Type @ to mention files, / for commands, or ? for shortcuts\n"
            "──────────────────────────────────────────────────────────────────────────────\n"
            " shift+tab switch mode\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.extract_last_message_from_script(output) == "Completed task successfully."

    def test_extract_last_message_from_meaningful_tail_without_markers(self):
        output = (
            "SUMMARY\n"
            "Key finding: values are stable.\n"
            "ANALYSIS\n"
            "Mean=3, Median=3, StdDev=1.41\n"
            "CONCLUSIONS\n"
            "Recommend continuing current approach.\n"
            "Total usage est: 0 Premium requests\n"
            "❯  Type @ to mention files, / for commands, or ? for shortcuts\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)
        assert "SUMMARY" in message
        assert "StdDev=1.41" in message
        assert "Total usage est" not in message

    def test_extract_last_message_raises_when_only_chrome(self):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        with pytest.raises(ValueError, match="No provider response content found"):
            provider.extract_last_message_from_script(
                "❯  Type @ to mention files, / for commands, or ? for shortcuts\n"
                " shift+tab switch mode\n"
            )


class TestCopilotCliProviderMisc:
    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    def test_send_enter_uses_tmux_client(self, mock_tmux):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        provider._send_enter()
        mock_tmux.send_special_key.assert_called_once_with("test-session", "window-0", "Enter")

    def test_get_idle_pattern_for_log(self):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert "type @ to mention files" in provider.get_idle_pattern_for_log().lower()

    def test_exit_cli(self):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.exit_cli() == "/exit"

    def test_cleanup(self):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        provider._initialized = True
        provider.cleanup()
        assert provider._initialized is False

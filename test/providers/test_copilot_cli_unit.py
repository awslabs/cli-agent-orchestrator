"""Unit tests for Copilot CLI provider."""

from __future__ import annotations

import json
import shlex
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.copilot_cli import CopilotCliProvider


class TestCopilotCliProviderCommand:
    @patch("cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._supports_flag")
    @patch(
        "cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._build_runtime_mcp_config"
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    @patch.dict("os.environ", {}, clear=True)
    def test_command_builds_default(self, mock_tmux, mock_build_mcp, mock_supports_flag):
        mock_supports_flag.return_value = True
        mock_build_mcp.return_value = '{"mcpServers":{"cao-mcp-server":{"command":"x"}}}'
        mock_tmux.get_pane_working_directory.return_value = "/tmp/project"

        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        command = provider._command()
        parts = shlex.split(command)

        assert parts[0] == "copilot"
        assert "--allow-all" in parts
        assert "--model" not in parts
        assert "--config-dir" in parts
        assert "--add-dir" in parts
        assert parts[parts.index("--add-dir") + 1] == "/tmp/project"
        assert "--additional-mcp-config" in parts
        assert "--autopilot" in parts
        mock_build_mcp.assert_called_once_with()

    @patch("cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._supports_flag")
    @patch(
        "cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._build_runtime_mcp_config"
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    @patch.dict("os.environ", {}, clear=True)
    def test_command_does_not_use_model_env(self, mock_tmux, mock_build_mcp, mock_supports_flag):
        mock_supports_flag.return_value = True
        mock_build_mcp.return_value = '{"mcpServers":{"cao-mcp-server":{"command":"x"}}}'
        mock_tmux.get_pane_working_directory.return_value = "/tmp/project"

        with patch.dict("os.environ", {"CAO_COPILOT_MODEL": "gpt-4.1"}, clear=False):
            provider = CopilotCliProvider("test1234", "test-session", "window-0")
            parts = shlex.split(provider._command())

        assert "--model" not in parts
        assert "--allow-all" in parts
        assert "--autopilot" in parts
        assert "--config-dir" in parts
        assert "--add-dir" in parts
        assert parts[parts.index("--add-dir") + 1] == "/tmp/project"
        assert "--additional-mcp-config" in parts
        mock_build_mcp.assert_called_once_with()

    @patch("cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._supports_flag")
    @patch(
        "cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._build_runtime_mcp_config"
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    @patch.dict("os.environ", {}, clear=True)
    def test_command_passes_agent_name_directly(
        self,
        mock_tmux,
        mock_build_mcp,
        mock_supports_flag,
    ):
        mock_supports_flag.return_value = True
        mock_build_mcp.return_value = '{"mcpServers":{"cao-mcp-server":{"command":"x"}}}'
        mock_tmux.get_pane_working_directory.return_value = "/tmp/project"

        provider = CopilotCliProvider(
            "test1234", "test-session", "window-0", agent_profile="repo-agent"
        )
        parts = shlex.split(provider._command())
        assert parts[parts.index("--agent") + 1] == "repo-agent"

    @patch("cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._supports_flag")
    @patch(
        "cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._build_runtime_mcp_config"
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    @patch.dict("os.environ", {}, clear=True)
    def test_command_skips_mcp_flag_if_unsupported(
        self,
        mock_tmux,
        mock_build_mcp,
        mock_supports_flag,
    ):
        mock_supports_flag.return_value = False
        mock_tmux.get_pane_working_directory.return_value = "/tmp/project"
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        command = provider._command()
        assert "--additional-mcp-config" not in command
        mock_build_mcp.assert_not_called()

    @patch("cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._supports_flag")
    @patch(
        "cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._build_runtime_mcp_config"
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    @patch.dict("os.environ", {}, clear=True)
    def test_command_falls_back_to_process_cwd_when_pane_dir_missing(
        self,
        mock_tmux,
        mock_build_mcp,
        mock_supports_flag,
    ):
        mock_supports_flag.return_value = True
        mock_build_mcp.return_value = '{"mcpServers":{"cao-mcp-server":{"command":"x"}}}'
        mock_tmux.get_pane_working_directory.return_value = None

        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        parts = shlex.split(provider._command())

        assert "--add-dir" in parts
        assert parts[parts.index("--add-dir") + 1]


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
    @patch("cli_agent_orchestrator.providers.copilot_cli.time.sleep")
    def test_initialize_success_with_ui_probe(
        self,
        _mock_sleep,
        mock_accept,
        mock_tmux,
        mock_wait_shell,
    ):
        mock_wait_shell.return_value = True
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        with patch.object(provider, "get_status", return_value=TerminalStatus.IDLE):
            assert provider.initialize() is True

        mock_tmux.send_keys.assert_called_once()
        mock_accept.assert_called_once()

    @patch("cli_agent_orchestrator.providers.copilot_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    @patch.object(CopilotCliProvider, "_accept_trust_prompts")
    @patch("cli_agent_orchestrator.providers.copilot_cli.time.sleep")
    @patch("cli_agent_orchestrator.providers.copilot_cli.time.time")
    def test_initialize_timeout_when_no_ui_and_not_idle(
        self,
        mock_time,
        _mock_sleep,
        mock_accept,
        _mock_tmux,
        mock_wait_shell,
    ):
        mock_wait_shell.return_value = True
        mock_time.side_effect = [0.0, 0.0, 61.0]
        provider = CopilotCliProvider("test1234", "test-session", "window-0")

        with (
            patch.object(provider, "get_status", return_value=TerminalStatus.PROCESSING),
            pytest.raises(TimeoutError, match="Copilot initialization timed out after 60 seconds"),
        ):
            provider.initialize()

        assert mock_accept.call_count == 1

    @patch("cli_agent_orchestrator.providers.copilot_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    @patch.object(CopilotCliProvider, "_accept_trust_prompts")
    @patch("cli_agent_orchestrator.providers.copilot_cli.time.sleep")
    def test_initialize_reaccepts_trust_when_waiting(
        self,
        _mock_sleep,
        mock_accept,
        _mock_tmux,
        mock_wait_shell,
    ):
        mock_wait_shell.return_value = True
        provider = CopilotCliProvider("test1234", "test-session", "window-0")

        with patch.object(
            provider,
            "get_status",
            side_effect=[TerminalStatus.WAITING_USER_ANSWER] + [TerminalStatus.IDLE] * 12,
        ):
            assert provider.initialize() is True

        assert mock_accept.call_count == 2


class TestCopilotCliProviderTrustPrompts:
    @patch("cli_agent_orchestrator.providers.copilot_cli.time.sleep")
    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    def test_accept_trust_prompts_answers_yes_then_returns(self, mock_tmux, _mock_sleep):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")

        with (
            patch.object(
                provider,
                "_history",
                side_effect=[
                    "Do you trust all the actions in this folder? [y/N]",
                    "GitHub Copilot v0.0.415\n❯ Type @ to mention files",
                ],
            ),
            patch.object(provider, "_send_enter") as mock_enter,
        ):
            provider._accept_trust_prompts(timeout=2.0)

        mock_tmux.send_special_key.assert_any_call("test-session", "window-0", "y")
        assert mock_enter.call_count >= 1

    @patch("cli_agent_orchestrator.providers.copilot_cli.logger")
    @patch("cli_agent_orchestrator.providers.copilot_cli.time.sleep")
    @patch("cli_agent_orchestrator.providers.copilot_cli.time.time")
    def test_accept_trust_prompts_logs_warning_on_timeout(
        self, mock_time, _mock_sleep, mock_logger
    ):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        mock_time.side_effect = [0.0, 0.0, 3.0]

        with patch.object(provider, "_history", return_value="still waiting"):
            provider._accept_trust_prompts(timeout=2.0)

        mock_logger.warning.assert_called_once_with(
            "Trust prompt handler timed out for %s:%s",
            "test-session",
            "window-0",
        )

    @patch("cli_agent_orchestrator.providers.copilot_cli.time.sleep")
    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    def test_accept_trust_prompts_answers_yes_for_spaced_yes_no_prompt(
        self, mock_tmux, _mock_sleep
    ):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")

        with (
            patch.object(
                provider,
                "_history",
                side_effect=[
                    "Do you trust all the actions in this folder? [y / N]",
                    "GitHub Copilot v0.0.415\n❯ Type @ to mention files",
                ],
            ),
            patch.object(provider, "_send_enter") as mock_enter,
        ):
            provider._accept_trust_prompts(timeout=2.0)

        mock_tmux.send_special_key.assert_any_call("test-session", "window-0", "y")
        assert mock_enter.call_count >= 1


class TestCopilotCliProviderStatusDetection:
    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    def test_get_status_waiting_user_answer(self, mock_tmux):
        mock_tmux.get_history.return_value = "confirm folder trust [y/n]"
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status() == TerminalStatus.WAITING_USER_ANSWER

    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    def test_get_status_idle_with_no_user_message(self, mock_tmux):
        mock_tmux.get_history.return_value = "GitHub Copilot v0.0.415\n❯ Type @ to mention files"
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status() == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    def test_get_status_idle_with_wrapped_prompt_helper(self, mock_tmux):
        mock_tmux.get_history.return_value = (
            "● Environment loaded: 2 MCP servers, 3 agents\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            "❯  Type @ to mention files, # for issues/PRs, / for commands, or ? for \n"
            "  shortcuts\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            " shift+tab switch mode                     Remaining reqs.: 99.66666666666667%\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status() == TerminalStatus.IDLE

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
    def test_get_status_processing_when_spinner_visible_with_idle_prompt(self, mock_tmux):
        mock_tmux.get_history.return_value = (
            "❯ Analyze the dataset and calculate standard \n"
            "  deviation.\n"
            "∙ Thinking (Esc to cancel)\n"
            " ~/repo [⎇ branch*] gpt-5-mini (medium) (0x) data_analyst\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            "❯ Type @ to mention files, # for issues/PRs, / for commands, or ? for \n"
            "  shortcuts\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status() == TerminalStatus.PROCESSING

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

    # ------------------------------------------------------------------
    # Copilot v1.0.31+ layout: bare ❯ followed by the status bar line
    # ------------------------------------------------------------------

    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    def test_get_status_idle_with_v1031_autopilot_status_bar(self, mock_tmux):
        """Bare ❯ + 'autopilot · / commands ...' status bar → IDLE (no prior user turn)."""
        mock_tmux.get_history.return_value = (
            "● Selected custom agent: developer\n"
            "\n"
            "● Environment loaded: 3 custom instructions, 1 hook, 2 MCP servers\n"
            "\n"
            " ~/repo [⎇ main*%]\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            "❯\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            " autopilot · / commands \u200b                        Claude Sonnet 4.6 · (0%)\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status() == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    def test_get_status_idle_with_v1031_plan_status_bar(self, mock_tmux):
        """Bare ❯ + 'plan · / commands ...' status bar → IDLE."""
        mock_tmux.get_history.return_value = (
            " ~/repo [⎇ main]\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            "❯\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            " plan · / commands \u200b                             Claude Sonnet 4.6 · (0%)\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status() == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    def test_get_status_idle_with_v1031_interactive_status_bar(self, mock_tmux):
        """Bare ❯ + 'interactive · / commands ...' status bar → IDLE."""
        mock_tmux.get_history.return_value = (
            " ~/repo [⎇ main]\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            "❯\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            " interactive · / commands \u200b                      Claude Sonnet 4.6 · (0%)\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status() == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    def test_get_status_completed_with_v1031_status_bar_after_user_turn(self, mock_tmux):
        """User turn + agent response + bare ❯ + status bar → COMPLETED."""
        mock_tmux.get_history.return_value = (
            "❯ fix the bug\n"
            "● Edit src/main.py (+3 -1)\n"
            " ~/repo [⎇ main*%]\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            "❯\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            " autopilot · / commands \u200b                        Claude Sonnet 4.6 · (0%)\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status() == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.copilot_cli.tmux_client")
    def test_get_status_processing_with_spinner_and_v1031_status_bar(self, mock_tmux):
        """Spinner line present alongside status bar → still PROCESSING."""
        mock_tmux.get_history.return_value = (
            "❯ refactor utils.py\n"
            "∙ Thinking (Esc to cancel)\n"
            " ~/repo [⎇ main*%]\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            "❯\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            " autopilot · / commands \u200b                        Claude Sonnet 4.6 · (0%)\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status() == TerminalStatus.PROCESSING


class TestCopilotCliProviderMessageExtraction:
    def test_extract_last_message_from_post_user_lines(self):
        output = "❯ refactor\n● Edit x.py (+2 -1)\nDONE\n❯ "
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)
        assert "Edit x.py" in message
        assert "DONE" in message

    def test_extract_last_message_from_assistant_fallback(self):
        output = "assistant: Completed task successfully."
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.extract_last_message_from_script(output) == "Completed task successfully."

    def test_extract_last_message_raises_when_not_found(self):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        with pytest.raises(ValueError, match="No provider response content found"):
            provider.extract_last_message_from_script("just ui chrome")

    def test_extract_last_message_ignores_processing_spinner_tail(self):
        output = (
            "❯ Analyze the dataset and calculate standard \n"
            "  deviation.\n"
            "∙ Thinking (Esc to cancel)\n"
            " ~/repo [⎇ branch*] gpt-5-mini (medium) (0x) data_analyst\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            "❯ Type @ to mention files, # for issues/PRs, / for commands, or ? for \n"
            "  shortcuts\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        with pytest.raises(ValueError, match="No provider response content found"):
            provider.extract_last_message_from_script(output)


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

    def test_build_runtime_mcp_config_includes_terminal_id(self):
        provider = CopilotCliProvider("abc12345", "test-session", "window-0")
        runtime_cfg = json.loads(provider._build_runtime_mcp_config())
        assert "cao-mcp-server" in runtime_cfg["mcpServers"]
        assert runtime_cfg["mcpServers"]["cao-mcp-server"]["env"]["CAO_TERMINAL_ID"] == "abc12345"

    def test_cleanup_resets_initialized_state(self):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        provider._initialized = True
        provider.cleanup()
        assert provider._initialized is False

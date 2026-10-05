"""Unit tests for Copilot CLI provider."""

from __future__ import annotations

import asyncio
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
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    @patch.dict("os.environ", {}, clear=True)
    def test_command_builds_default(self, mock_tmux, mock_build_mcp, mock_supports_flag):
        mock_supports_flag.return_value = True
        mock_build_mcp.return_value = '{"mcpServers":{"cao-mcp-server":{"command":"x"}}}'
        mock_tmux.return_value.get_pane_working_directory.return_value = "/tmp/project"

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
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    @patch.dict("os.environ", {}, clear=True)
    def test_empty_allowlist_emits_deny_tool(self, mock_tmux, mock_build_mcp, mock_supports_flag):
        """allowed_tools=[] must pass --deny-tool, not skip restrictions."""
        mock_supports_flag.return_value = True
        mock_build_mcp.return_value = '{"mcpServers":{"cao-mcp-server":{"command":"x"}}}'
        mock_tmux.return_value.get_pane_working_directory.return_value = "/tmp/project"

        provider = CopilotCliProvider("test1234", "test-session", "window-0", allowed_tools=[])
        parts = shlex.split(provider._command())

        assert "--deny-tool" in parts
        assert "shell" in parts

    @patch("cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._supports_flag")
    @patch(
        "cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._build_runtime_mcp_config"
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    @patch.dict("os.environ", {}, clear=True)
    def test_command_does_not_use_model_env(self, mock_tmux, mock_build_mcp, mock_supports_flag):
        mock_supports_flag.return_value = True
        mock_build_mcp.return_value = '{"mcpServers":{"cao-mcp-server":{"command":"x"}}}'
        mock_tmux.return_value.get_pane_working_directory.return_value = "/tmp/project"

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
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    @patch.dict("os.environ", {}, clear=True)
    def test_command_passes_agent_name_directly(
        self,
        mock_tmux,
        mock_build_mcp,
        mock_supports_flag,
    ):
        mock_supports_flag.return_value = True
        mock_build_mcp.return_value = '{"mcpServers":{"cao-mcp-server":{"command":"x"}}}'
        mock_tmux.return_value.get_pane_working_directory.return_value = "/tmp/project"

        provider = CopilotCliProvider(
            "test1234", "test-session", "window-0", agent_profile="repo-agent"
        )
        parts = shlex.split(provider._command())
        assert parts[parts.index("--agent") + 1] == "repo-agent"

    @patch("cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._supports_flag")
    @patch(
        "cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._build_runtime_mcp_config"
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    @patch.dict("os.environ", {}, clear=True)
    def test_command_skips_mcp_flag_if_unsupported(
        self,
        mock_tmux,
        mock_build_mcp,
        mock_supports_flag,
    ):
        mock_supports_flag.return_value = False
        mock_tmux.return_value.get_pane_working_directory.return_value = "/tmp/project"
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        command = provider._command()
        assert "--additional-mcp-config" not in command
        mock_build_mcp.assert_not_called()

    @patch("cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._supports_flag")
    @patch(
        "cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._build_runtime_mcp_config"
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    @patch.dict("os.environ", {}, clear=True)
    def test_command_falls_back_to_process_cwd_when_pane_dir_missing(
        self,
        mock_tmux,
        mock_build_mcp,
        mock_supports_flag,
    ):
        mock_supports_flag.return_value = True
        mock_build_mcp.return_value = '{"mcpServers":{"cao-mcp-server":{"command":"x"}}}'
        mock_tmux.return_value.get_pane_working_directory.return_value = None

        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        parts = shlex.split(provider._command())

        assert "--add-dir" in parts
        assert parts[parts.index("--add-dir") + 1]


class TestCopilotCliProviderModelFlag:
    """Tests that the model kwarg is forwarded to Copilot CLI via --model.

    The Copilot provider takes the model value directly from the constructor
    (populated by terminal_service from the already-loaded AgentProfile) to
    avoid re-loading the profile.
    """

    @patch("cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._supports_flag")
    @patch(
        "cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._build_runtime_mcp_config"
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    @patch.dict("os.environ", {}, clear=True)
    def test_command_appends_model_when_set(self, mock_tmux, mock_build_mcp, mock_supports_flag):
        mock_supports_flag.return_value = True
        mock_build_mcp.return_value = '{"mcpServers":{"cao-mcp-server":{"command":"x"}}}'
        mock_tmux.return_value.get_pane_working_directory.return_value = "/tmp/project"

        provider = CopilotCliProvider(
            "test1234",
            "test-session",
            "window-0",
            agent_profile="repo-agent",
            model="claude-sonnet-4.5",
        )
        parts = shlex.split(provider._command())

        assert "--model" in parts
        assert parts[parts.index("--model") + 1] == "claude-sonnet-4.5"

    @patch("cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._supports_flag")
    @patch(
        "cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._build_runtime_mcp_config"
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    @patch.dict("os.environ", {}, clear=True)
    def test_command_omits_model_when_unset(self, mock_tmux, mock_build_mcp, mock_supports_flag):
        mock_supports_flag.return_value = True
        mock_build_mcp.return_value = '{"mcpServers":{"cao-mcp-server":{"command":"x"}}}'
        mock_tmux.return_value.get_pane_working_directory.return_value = "/tmp/project"

        provider = CopilotCliProvider(
            "test1234", "test-session", "window-0", agent_profile="repo-agent"
        )
        parts = shlex.split(provider._command())

        assert "--model" not in parts

    @patch("cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._supports_flag")
    @patch(
        "cli_agent_orchestrator.providers.copilot_cli.CopilotCliProvider._build_runtime_mcp_config"
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    @patch.dict("os.environ", {}, clear=True)
    def test_command_omits_model_when_no_agent_profile(
        self, mock_tmux, mock_build_mcp, mock_supports_flag
    ):
        # --model is only meaningful alongside --agent; without an agent
        # profile the flag is not emitted even if a model is passed.
        mock_supports_flag.return_value = True
        mock_build_mcp.return_value = '{"mcpServers":{"cao-mcp-server":{"command":"x"}}}'
        mock_tmux.return_value.get_pane_working_directory.return_value = "/tmp/project"

        provider = CopilotCliProvider(
            "test1234", "test-session", "window-0", model="claude-sonnet-4.5"
        )
        parts = shlex.split(provider._command())

        assert "--model" not in parts


class TestCopilotCliProviderInitialization:
    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.copilot_cli.wait_for_shell")
    async def test_initialize_shell_timeout(self, mock_wait_shell):
        mock_wait_shell.return_value = False
        provider = CopilotCliProvider("test1234", "test-session", "window-0")

        with pytest.raises(TimeoutError, match="Shell initialization timed out"):
            await provider.initialize()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor")
    @patch("cli_agent_orchestrator.providers.copilot_cli.asyncio.sleep")
    @patch("cli_agent_orchestrator.providers.copilot_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    @patch.object(CopilotCliProvider, "_accept_trust_prompts")
    async def test_initialize_success(
        self,
        mock_accept,
        mock_tmux,
        mock_wait_shell,
        _mock_async_sleep,
        mock_status_monitor,
    ):
        """initialize() launches the CLI and returns once the terminal is IDLE."""
        mock_wait_shell.return_value = True
        mock_status_monitor.get_status.return_value = TerminalStatus.IDLE

        provider = CopilotCliProvider("test1234", "test-session", "window-0")

        result = await provider.initialize()

        assert result is True
        assert provider._initialized is True
        # The CLI command is typed into the pane after the shell is ready.
        mock_tmux.return_value.send_keys.assert_called_once()
        mock_accept.assert_called_once()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor")
    @patch("cli_agent_orchestrator.providers.copilot_cli.asyncio.sleep")
    @patch("cli_agent_orchestrator.providers.copilot_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    @patch.object(CopilotCliProvider, "_accept_trust_prompts")
    async def test_initialize_handles_trust_prompt_then_idle(
        self,
        mock_accept,
        mock_tmux,
        mock_wait_shell,
        _mock_async_sleep,
        mock_status_monitor,
    ):
        """A WAITING_USER_ANSWER trust prompt is handled before reaching IDLE."""
        mock_wait_shell.return_value = True
        mock_status_monitor.get_status.side_effect = [
            TerminalStatus.WAITING_USER_ANSWER,
            TerminalStatus.IDLE,
        ]

        provider = CopilotCliProvider("test1234", "test-session", "window-0")

        result = await provider.initialize()

        assert result is True
        # Initial trust handling + the in-loop WAITING_USER_ANSWER handling.
        assert mock_accept.call_count >= 2

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor")
    @patch("cli_agent_orchestrator.providers.copilot_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    @patch.object(CopilotCliProvider, "_accept_trust_prompts")
    async def test_initialize_polls_get_status_via_to_thread(
        self,
        mock_accept,
        mock_tmux,
        mock_wait_shell,
        mock_status_monitor,
    ):
        """#558: status_monitor.get_status() is no longer in-memory only -- for a
        PROCESSING terminal it can fork a real tmux capture-pane subprocess (the
        stale-PROCESSING fallback), and copilot init is exactly the regime that trips it
        (cached PROCESSING, pane quiet during auth/MCP boot). Pin the asyncio.to_thread
        dispatch of the init poll -- the other init tests mock the status monitor and
        cannot see HOW it was called."""
        mock_wait_shell.return_value = True
        mock_status_monitor.get_status.return_value = TerminalStatus.IDLE

        provider = CopilotCliProvider("test1234", "test-session", "window-0")

        with patch(
            "cli_agent_orchestrator.providers.copilot_cli.asyncio.to_thread",
            wraps=asyncio.to_thread,
        ) as mock_to_thread:
            result = await provider.initialize()
            # initialize() also offloads _command and send_keys -- count only the
            # get_status dispatches.
            get_status_calls = [
                c
                for c in mock_to_thread.call_args_list
                if c.args[0] == mock_status_monitor.get_status
            ]

        assert result is True
        assert get_status_calls, "status_monitor.get_status was never dispatched via to_thread"
        assert all(c.args[1] == "test1234" for c in get_status_calls)

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.copilot_cli.logger")
    @patch("cli_agent_orchestrator.providers.copilot_cli.time.time")
    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor")
    @patch("cli_agent_orchestrator.providers.copilot_cli.asyncio.sleep")
    @patch("cli_agent_orchestrator.providers.copilot_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    @patch.object(CopilotCliProvider, "_accept_trust_prompts")
    async def test_initialize_timeout_logs_unrecognized_row(
        self,
        _mock_accept,
        _mock_tmux,
        mock_wait_shell,
        _mock_async_sleep,
        mock_status_monitor,
        mock_time,
        mock_logger,
    ):
        mock_wait_shell.return_value = True
        mock_status_monitor.get_status.return_value = TerminalStatus.PROCESSING
        mock_time.side_effect = [0.0, 61.0]
        provider = CopilotCliProvider("test1234", "test-session", "window-0")

        with patch.object(provider, "_history", return_value="● Ready\n❯\n\nNEW ROW alpha\n"):
            with pytest.raises(TimeoutError, match="Copilot initialization timed out"):
                await provider.initialize()

        mock_logger.warning.assert_called_once_with(
            "Copilot idle prompt for %s:%s is followed by unrecognized row %r",
            "test-session",
            "window-0",
            "NEW ROW alpha",
        )

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.copilot_cli.logger")
    @patch("cli_agent_orchestrator.providers.copilot_cli.time.time")
    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor")
    @patch("cli_agent_orchestrator.providers.copilot_cli.asyncio.sleep")
    @patch("cli_agent_orchestrator.providers.copilot_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    @patch.object(CopilotCliProvider, "_accept_trust_prompts")
    async def test_initialize_timeout_names_footer_fragment_from_narrow_pane(
        self,
        _mock_accept,
        _mock_tmux,
        mock_wait_shell,
        _mock_async_sleep,
        mock_status_monitor,
        mock_time,
        mock_logger,
    ):
        mock_wait_shell.return_value = True
        mock_status_monitor.get_status.return_value = TerminalStatus.PROCESSING
        mock_time.side_effect = [0.0, 61.0]
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        narrow_screen = (
            "❯\n"
            "← open · Autopilot · Allow · / commands · tab next\n"
            "sidebar              All                  tab\n"
        )

        with patch.object(provider, "_history", return_value=narrow_screen):
            with pytest.raises(TimeoutError, match="Copilot initialization timed out"):
                await provider.initialize()

        mock_logger.warning.assert_called_once_with(
            "Copilot idle prompt for %s:%s is followed by unrecognized row %r",
            "test-session",
            "window-0",
            "sidebar              All                  tab",
        )

    @pytest.mark.parametrize("screen", ["Loading environment...\n", "", "\n  \n"])
    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.copilot_cli.logger")
    @patch("cli_agent_orchestrator.providers.copilot_cli.time.time")
    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor")
    @patch("cli_agent_orchestrator.providers.copilot_cli.asyncio.sleep")
    @patch("cli_agent_orchestrator.providers.copilot_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    @patch.object(CopilotCliProvider, "_accept_trust_prompts")
    async def test_initialize_timeout_logs_missing_prompt(
        self,
        _mock_accept,
        _mock_tmux,
        mock_wait_shell,
        _mock_async_sleep,
        mock_status_monitor,
        mock_time,
        mock_logger,
        screen,
    ):
        mock_wait_shell.return_value = True
        mock_status_monitor.get_status.return_value = TerminalStatus.PROCESSING
        mock_time.side_effect = [0.0, 61.0]
        provider = CopilotCliProvider("test1234", "test-session", "window-0")

        with patch.object(provider, "_history", return_value=screen):
            with pytest.raises(TimeoutError, match="Copilot initialization timed out"):
                await provider.initialize()

        mock_logger.warning.assert_called_once_with(
            "Copilot idle prompt not found for %s:%s", "test-session", "window-0"
        )

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.copilot_cli.logger")
    @patch("cli_agent_orchestrator.providers.copilot_cli.time.time")
    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor")
    @patch("cli_agent_orchestrator.providers.copilot_cli.asyncio.sleep")
    @patch("cli_agent_orchestrator.providers.copilot_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    @patch.object(CopilotCliProvider, "_accept_trust_prompts")
    async def test_initialize_timeout_logs_recognized_prompt_that_did_not_settle(
        self,
        _mock_accept,
        _mock_tmux,
        mock_wait_shell,
        _mock_async_sleep,
        mock_status_monitor,
        mock_time,
        mock_logger,
    ):
        mock_wait_shell.return_value = True
        mock_status_monitor.get_status.return_value = TerminalStatus.PROCESSING
        mock_time.side_effect = [0.0, 61.0]
        provider = CopilotCliProvider("test1234", "test-session", "window-0")

        with patch.object(provider, "_history", return_value="● Ready\n❯\n"):
            with pytest.raises(TimeoutError, match="Copilot initialization timed out"):
                await provider.initialize()

        mock_logger.warning.assert_called_once_with(
            "Copilot idle prompt recognized for %s:%s but status did not settle to idle",
            "test-session",
            "window-0",
        )


class TestCopilotCliProviderTrustPrompts:
    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.copilot_cli.asyncio.sleep")
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    async def test_accept_trust_prompts_answers_yes_then_returns(self, mock_tmux, _mock_sleep):
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
            await provider._accept_trust_prompts(timeout=2.0)

        mock_tmux.return_value.send_special_key.assert_any_call("test-session", "window-0", "y")
        assert mock_enter.call_count >= 1

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.copilot_cli.logger")
    @patch("cli_agent_orchestrator.providers.copilot_cli.asyncio.sleep")
    @patch("cli_agent_orchestrator.providers.copilot_cli.time.time")
    async def test_accept_trust_prompts_logs_warning_on_timeout(
        self, mock_time, _mock_sleep, mock_logger
    ):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        mock_time.side_effect = [0.0, 0.0, 3.0]

        with patch.object(provider, "_history", return_value="still waiting"):
            await provider._accept_trust_prompts(timeout=2.0)

        mock_logger.warning.assert_called_once_with(
            "Trust prompt handler timed out for %s:%s",
            "test-session",
            "window-0",
        )

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.copilot_cli.asyncio.sleep")
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    async def test_accept_trust_prompts_answers_yes_for_spaced_yes_no_prompt(
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
            await provider._accept_trust_prompts(timeout=2.0)

        mock_tmux.return_value.send_special_key.assert_any_call("test-session", "window-0", "y")
        assert mock_enter.call_count >= 1

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.copilot_cli.logger")
    @patch("cli_agent_orchestrator.providers.copilot_cli.asyncio.sleep")
    async def test_accept_trust_prompts_returns_at_v1091_idle_prompt(
        self, _mock_sleep, mock_logger
    ):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        idle_screen = (
            "● MCP Servers reloaded: 2 servers connected\n"
            "\n"
            "❯\n"
            "\n"
            "← open sidebar · Autopilot · Allow All · / commands · tab next tab\n"
            "data_analyst · GitHub Copilot • GPT-5.6 Terra\n"
        )

        with (
            patch.object(provider, "_history", return_value=idle_screen) as mock_history,
            patch.object(provider, "_send_enter") as mock_enter,
        ):
            await provider._accept_trust_prompts(timeout=2.0)

        mock_history.assert_called_once()
        mock_enter.assert_not_called()
        mock_logger.warning.assert_not_called()


class TestCopilotCliProviderStatusDetection:
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_waiting_user_answer(self, mock_tmux):
        output = "confirm folder trust [y/n]"
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.WAITING_USER_ANSWER

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_idle_with_no_user_message(self, mock_tmux):
        output = "GitHub Copilot v0.0.415\n❯ Type @ to mention files"
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_idle_with_wrapped_prompt_helper(self, mock_tmux):
        output = (
            "● Environment loaded: 2 MCP servers, 3 agents\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            "❯  Type @ to mention files, # for issues/PRs, / for commands, or ? for \n"
            "  shortcuts\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            " shift+tab switch mode                     Remaining reqs.: 99.66666666666667%\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_processing_when_no_idle_prompt(self, mock_tmux):
        output = "Working on edits..."
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_error_while_processing_if_error_after_user(self, mock_tmux):
        output = "❯ refactor this\nError: failed to parse"
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.ERROR

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_completed_when_response_present_and_idle(self, mock_tmux):
        output = "❯ refactor this\n● Edit file.py (+1 -1)\n❯ "
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_processing_when_spinner_visible_with_idle_prompt(self, mock_tmux):
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
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_error_when_idle_and_error_without_assistant_marker(self, mock_tmux):
        output = "❯ refactor this\nError: failed to parse\n❯ "
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.ERROR

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_completed_when_idle_and_error_with_assistant_marker(self, mock_tmux):
        output = "❯ refactor this\nassistant: note\nError: sample\n❯ "
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    # ------------------------------------------------------------------
    # Copilot v1.0.31+ layout: bare ❯ followed by the status bar line
    # ------------------------------------------------------------------

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_idle_with_v1031_autopilot_status_bar(self, mock_tmux):
        """Bare ❯ + 'autopilot · / commands ...' status bar → IDLE (no prior user turn)."""
        output = (
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
        assert provider.get_status(output) == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_idle_with_v1031_plan_status_bar(self, mock_tmux):
        """Bare ❯ + 'plan · / commands ...' status bar → IDLE."""
        output = (
            " ~/repo [⎇ main]\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            "❯\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            " plan · / commands \u200b                             Claude Sonnet 4.6 · (0%)\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_idle_with_v1031_interactive_status_bar(self, mock_tmux):
        """Bare ❯ + 'interactive · / commands ...' status bar → IDLE."""
        output = (
            " ~/repo [⎇ main]\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            "❯\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            " interactive · / commands \u200b                      Claude Sonnet 4.6 · (0%)\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_completed_with_v1031_status_bar_after_user_turn(self, mock_tmux):
        """User turn + agent response + bare ❯ + status bar → COMPLETED."""
        output = (
            "❯ fix the bug\n"
            "● Edit src/main.py (+3 -1)\n"
            " ~/repo [⎇ main*%]\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            "❯\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            " autopilot · / commands \u200b                        Claude Sonnet 4.6 · (0%)\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_processing_with_spinner_and_v1031_status_bar(self, mock_tmux):
        """Spinner line present alongside status bar → still PROCESSING."""
        output = (
            "❯ refactor utils.py\n"
            "∙ Thinking (Esc to cancel)\n"
            " ~/repo [⎇ main*%]\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            "❯\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            " autopilot · / commands \u200b                        Claude Sonnet 4.6 · (0%)\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_idle_with_v1031_absolute_path_breadcrumb(self, mock_tmux):
        """Bare ❯ + absolute-path breadcrumb (CWD outside $HOME) → IDLE."""
        output = (
            " /tmp/pr184-e2e [⎇ pr-184]\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            "❯\n"
            "────────────────────────────────────────────────────────────────────────────────\n"
            " autopilot · / commands \u200b                        Claude Sonnet 4.6 · (0%)\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.IDLE

    # ------------------------------------------------------------------
    # Copilot v1.0.91 layout: bare ❯ followed by hint bar and agent/model bar
    # ------------------------------------------------------------------

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_idle_with_v1091_footer(self, mock_tmux):
        """Bare ❯ + hint bar + agent/model bar → IDLE (no prior user turn)."""
        output = (
            "Copilot v1.0.91 uses AI.\n"
            "\n"
            "● Selected custom agent: data_analyst\n"
            "\n"
            "● MCP Servers reloaded: 2 servers connected\n"
            "\n"
            "/Users/me/work\n"
            "\n"
            "❯\n"
            "\n"
            "← open sidebar · Autopilot · Allow All · / commands · tab next tab\n"
            "data_analyst · GitHub Copilot • GPT-5.6 Terra\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_completed_with_v1091_footer_after_user_turn(self, mock_tmux):
        """User turn + agent response + bare ❯ + v1.0.91 footer → COMPLETED."""
        output = (
            "❯ fix the bug\n"
            "● Edit src/main.py (+3 -1)\n"
            "\n"
            "❯\n"
            "\n"
            "← open sidebar · Autopilot · Allow All · / commands · tab next tab\n"
            "data_analyst · GitHub Copilot • GPT-5.6 Terra\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_processing_with_spinner_and_v1091_footer(self, mock_tmux):
        """Spinner line present alongside v1.0.91 footer → still PROCESSING."""
        output = (
            "❯ refactor utils.py\n"
            "∙ Thinking (Esc to cancel)\n"
            "\n"
            "❯\n"
            "\n"
            "← open sidebar · Autopilot · Allow All · / commands · tab next tab\n"
            "data_analyst · GitHub Copilot • GPT-5.6 Terra\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.PROCESSING


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

    def test_extract_last_message_excludes_v1091_footer(self):
        output = (
            "❯ fix the bug\n"
            "● Fixed the off-by-one in main.py\n"
            "\n"
            "❯\n"
            "\n"
            "← open sidebar · Autopilot · Allow All · / commands · tab next tab\n"
            "data_analyst · GitHub Copilot • GPT-5.6 Terra\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.extract_last_message_from_script(output) == (
            "● Fixed the off-by-one in main.py"
        )

    def test_extract_last_message_keeps_reply_lines_resembling_v1091_footer(self):
        output = (
            "❯ explain\n"
            "● Here is the plan\n"
            "Use the menu · / commands are listed\n"
            "GitHub Copilot · supports MCP\n"
            "Done.\n"
            "\n"
            "❯\n"
            "\n"
            "← open sidebar · Autopilot · Allow All · / commands · tab next tab\n"
            "data_analyst · GitHub Copilot • GPT-5.6 Terra\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.extract_last_message_from_script(output) == (
            "● Here is the plan\n"
            "Use the menu · / commands are listed\n"
            "GitHub Copilot · supports MCP\n"
            "Done."
        )


class TestCopilotCliProviderMisc:
    @pytest.mark.parametrize(
        "reply",
        [
            "vscode · GitHub Copilot • AI assistant",
            "GitHub Copilot • GPT-5.6 Terra",
            "Claude Sonnet 5",
        ],
    )
    @pytest.mark.parametrize("trailing_prompt", ["", "\n❯\n"])
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_model_like_reply_without_hint_context_is_preserved(
        self, mock_tmux, reply, trailing_prompt
    ):
        mock_tmux.return_value.get_native_status.return_value = None
        output = f"❯ who are you\n{reply}{trailing_prompt}"
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        expected = TerminalStatus.COMPLETED if trailing_prompt else TerminalStatus.PROCESSING

        assert provider.get_status(output) == expected
        assert provider.probe_stale_processing_capture(output) == expected
        assert provider.extract_last_message_from_script(output) == reply

    @pytest.mark.parametrize("prior_turn", ["", "❯ previous task\n● Previous reply\n"])
    @pytest.mark.parametrize(
        "footer",
        [
            pytest.param(
                "← open sidebar · Autopilot · Allow All · / commands · tab next tab"
                "            data_analyst · Claude Sonnet 5\n",
                id="wide",
            ),
            pytest.param(
                "← open sidebar · Autopilot · Allow All · / commands · tab next tab\n"
                "data_analyst · Claude Sonnet 5\n",
                id="separate-model-row",
            ),
            pytest.param(
                "autopilot · / commands    Claude Sonnet 4.6 · (0%)\n",
                id="old-status-bar",
            ),
        ],
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_unsent_composer_text_is_idle_without_response(self, mock_tmux, footer, prior_turn):
        mock_tmux.return_value.get_native_status.return_value = None
        rule = "─" * 100
        output = f"{prior_turn}{rule}\n❯ summarize the repo\n{rule}\n{footer}"
        provider = CopilotCliProvider("test1234", "test-session", "window-0")

        assert provider.get_status(output) == TerminalStatus.IDLE
        assert provider.probe_stale_processing_capture(output) == TerminalStatus.IDLE
        assert provider.commit_stale_processing_capture(output, TerminalStatus.IDLE) is True
        assert provider.commit_stale_processing_capture(output, TerminalStatus.COMPLETED) is False
        with pytest.raises(ValueError, match="No provider response content found"):
            provider.extract_last_message_from_script(output)

    def test_opts_into_stale_capture_but_not_direct_status_probe(self):
        assert CopilotCliProvider.supports_stale_processing_capture is True
        assert CopilotCliProvider.supports_direct_status_probe is False

    def test_stale_capture_probe_classifies_without_side_effects(self):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        output = "❯\n← open sidebar · Autopilot · / commands · tab next tab\n"
        assert provider.probe_stale_processing_capture(output) == TerminalStatus.IDLE
        assert provider.commit_stale_processing_capture(output, TerminalStatus.IDLE) is True

    @pytest.mark.parametrize("snapshot", ["", " \n  \n", "\x1b[2J\x1b[H\n"])
    def test_blank_stale_capture_never_reads_history(self, snapshot):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        old_ready_transcript = (
            "❯ previous task\n● Previous reply\n❯\n"
            "← open sidebar · Autopilot · / commands · tab next tab\n"
        )
        with (
            patch.object(provider, "_history", return_value=old_ready_transcript) as history,
            patch.object(provider, "_resolve_native_status", return_value=None),
        ):
            assert provider.probe_stale_processing_capture(snapshot) == TerminalStatus.UNKNOWN
            assert provider.probe_stale_processing_capture(snapshot) == TerminalStatus.UNKNOWN
            assert provider.commit_stale_processing_capture(snapshot, TerminalStatus.IDLE) is False
            assert (
                provider.commit_stale_processing_capture(snapshot, TerminalStatus.COMPLETED)
                is False
            )
            history.assert_not_called()

    def test_extract_last_message_keeps_sole_reply_resembling_agent_model_bar(self):
        output = (
            "❯ who are you\n"
            "vscode · GitHub Copilot · AI assistant\n"
            "\n"
            "❯\n"
            "← open sidebar · Autopilot · / commands · tab next tab\n"
            "data_analyst · GitHub Copilot • GPT-5.6 Terra\n"
        )
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.extract_last_message_from_script(output) == (
            "vscode · GitHub Copilot · AI assistant"
        )

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_idle_with_model_row_lacking_agent_prefix(self, mock_tmux):
        rule = "─" * 120
        output = (
            f"{rule}\n❯\n{rule}\n"
            " ← open sidebar · Autopilot · Allow All · / commands · tab next tab"
            "            GitHub Copilot • Claude Sonnet 5\n"
        )
        mock_tmux.return_value.get_native_status.return_value = None
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_processing_when_hint_segments_wrap_in_narrow_pane(self, mock_tmux):
        """Below ~69 columns Copilot wraps each hint segment inside its own column, so
        the rows interleave. They are deliberately left unrecognized: not ready."""
        rule = "─" * 50
        output = (
            f"{rule}\n❯\n{rule}\n"
            "← open · Autopilot · Allow · / commands · tab next\n"
            "sidebar              All                  tab\n"
            "data_analyst · Claude Sonnet 5\n"
        )
        mock_tmux.return_value.get_native_status.return_value = None
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    @pytest.mark.parametrize(
        "agent_model_row",
        [
            "data_analyst · Claude Sonnet 5",
            "Claude Sonnet 5",
            "GitHub Copilot • GPT-5.6 Terra",
            "data_analyst · GitHub Copilot • AI assistant",
        ],
    )
    @pytest.mark.parametrize(
        "hint_row",
        [
            "← open sidebar · Autopilot · Allow All · / commands · tab next tab",
            "Autopilot · Allow All · / commands · tab next tab",
        ],
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_idle_with_agent_model_row_below_hint_row(
        self, mock_tmux, hint_row, agent_model_row
    ):
        """Below ~115 columns the agent/model box moves to its own last row. It may
        carry no provider label, and the hint row lacks "← open sidebar" when
        Copilot's sidebar feature is off."""
        rule = "─" * 100
        output = f"{rule}\n❯\n{rule}\n {hint_row}\n {agent_model_row}\n"
        mock_tmux.return_value.get_native_status.return_value = None
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.IDLE

    @pytest.mark.parametrize(
        "footer_rows",
        [
            pytest.param(["{row}", "{hint}", "{model}"], id="above-footer"),
            pytest.param(["{hint}", "{row}", "{model}"], id="between-hint-and-model"),
            pytest.param(["{hint}", "{model}", "{row}"], id="below-two-row-footer"),
            pytest.param(["{wide}", "{row}"], id="below-one-row-footer"),
            pytest.param(["{hint}   {row}", "{model}"], id="on-hint-row"),
        ],
    )
    @pytest.mark.parametrize(
        "row, expected",
        [
            ("∙ Thinking (Esc to cancel)", TerminalStatus.PROCESSING),
            ("⠋ Working · esc interrupt · enqueue", TerminalStatus.PROCESSING),
            ("Allow this tool to run? [y/n]", TerminalStatus.WAITING_USER_ANSWER),
        ],
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_not_ready_with_busy_or_waiting_row_near_footer(
        self, mock_tmux, footer_rows, row, expected
    ):
        hint = "← open sidebar · Autopilot · Allow All · / commands · tab next tab"
        model = "data_analyst · Claude Sonnet 5"
        rule = "─" * 120
        rows = [
            template.format(row=row, hint=hint, model=model, wide=f"{hint}            {model}")
            for template in footer_rows
        ]
        output = "❯ do the thing\n● Working on it\n" + f"{rule}\n❯\n{rule}\n" + "\n".join(rows)
        mock_tmux.return_value.get_native_status.return_value = None
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == expected

    @pytest.mark.parametrize(
        "footer_rows",
        [
            pytest.param(
                [
                    "← open sidebar · Autopilot · Allow All · / commands · tab next tab",
                    "Queued",
                    "data_analyst · GitHub Copilot • GPT-5.6 Terra",
                ],
                id="unknown-row-before-agent-model-row",
            ),
            pytest.param(
                [
                    "← open sidebar · Autopilot · Allow All · / commands · tab next tab"
                    "            data_analyst · Claude Sonnet 5",
                    "NEW ROW alpha",
                ],
                id="unknown-row-below-one-row-footer",
            ),
            pytest.param(["← Back to chat"], id="arrow-row-that-is-not-the-hint-row"),
        ],
    )
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_processing_with_unrecognized_row_near_footer(self, mock_tmux, footer_rows):
        rule = "─" * 120
        output = (
            "❯ do the thing\n● Working on it\n" + f"{rule}\n❯\n{rule}\n" + "\n".join(footer_rows)
        )
        mock_tmux.return_value.get_native_status.return_value = None
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    @pytest.mark.parametrize("row", ["Queued", "Waiting", "Compacting context"])
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_processing_with_versionless_row_below_hint_row(self, mock_tmux, row):
        hint = "← open sidebar · Autopilot · Allow All · / commands · tab next tab"
        rule = "─" * 100
        output = f"❯ do the thing\n● Working on it\n{rule}\n❯\n{rule}\n{hint}\n{row}\n"
        mock_tmux.return_value.get_native_status.return_value = None
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.PROCESSING

    def test_extract_last_message_without_trailing_prompt_is_not_duplicated(self):
        output = "❯ summarize\n● First point\nSecond line\n"
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.extract_last_message_from_script(output) == "● First point\nSecond line"

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_get_status_idle_on_v1091_capture_pane_with_shared_footer_row(self, mock_tmux):
        """Real 1.0.91 capture-pane shape: separator rules around ❯ and, in a wide
        pane, the hint bar and the agent/model bar rendered on one row."""
        rule = "─" * 120
        output = (
            " ● Selected custom agent: data_analyst\n"
            " /tmp/repo [⎇ main*]\n"
            f"{rule}\n"
            "❯\n"
            f"{rule}\n"
            " ← open sidebar · Autopilot · Allow All · / commands · tab next tab"
            "            data_analyst · Claude Sonnet 5\n"
        )
        mock_tmux.return_value.get_native_status.return_value = None
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.copilot_cli.get_backend")
    def test_send_enter_uses_tmux_client(self, mock_tmux):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        provider._send_enter()
        mock_tmux.return_value.send_special_key.assert_called_once_with(
            "test-session", "window-0", "Enter"
        )

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

    def test_build_runtime_mcp_config_resolves_bundled_command(self):
        """The bundled cao-mcp-server is resolved to a PATH-independent
        invocation (wiring guard: a refactor that drops the
        resolve_cao_mcp_command call must fail this test)."""
        provider = CopilotCliProvider("abc12345", "test-session", "window-0")
        MOD = "cli_agent_orchestrator.utils.mcp_resolution"
        with (
            patch(f"{MOD}._sibling_script", return_value="/venv/bin/cao-mcp-server"),
            patch(f"{MOD}.shutil.which", return_value=None),
        ):
            runtime_cfg = json.loads(provider._build_runtime_mcp_config())
        assert runtime_cfg["mcpServers"]["cao-mcp-server"]["command"] == "/venv/bin/cao-mcp-server"

    def test_cleanup_resets_initialized_state(self):
        provider = CopilotCliProvider("test1234", "test-session", "window-0")
        provider._initialized = True
        provider.cleanup()
        assert provider._initialized is False


class TestCopilotCliServerSettings:
    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.copilot_cli.get_server_settings")
    @patch("cli_agent_orchestrator.providers.copilot_cli.wait_for_shell")
    async def test_initialize_uses_provider_init_timeout(self, mock_wait_shell, mock_settings):
        """initialize() reads provider_init_timeout from server settings."""
        mock_settings.return_value = {
            "mcp_request_timeout": 120,
            "event_bus_max_queue_size": 8192,
            "provider_init_timeout": 45,
            "startup_prompt_handler_timeout": 5,
        }
        mock_wait_shell.return_value = False
        provider = CopilotCliProvider("test1234", "test-session", "window-0")

        with pytest.raises(TimeoutError):
            await provider.initialize()

        mock_wait_shell.assert_called_once_with("test1234", timeout=45)

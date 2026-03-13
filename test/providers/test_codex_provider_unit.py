"""Unit tests for Codex provider."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.codex import CodexProvider, ProviderError

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(filename: str) -> str:
    with open(FIXTURES_DIR / filename, "r") as f:
        return f.read()


class TestCodexProviderInitialization:
    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.codex.wait_until_status")
    @patch("cli_agent_orchestrator.providers.codex.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.codex.tmux_client")
    async def test_initialize_success(self, mock_tmux, mock_wait_shell, mock_wait_status):
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_tmux.get_history.return_value = "OpenAI Codex (v0.98.0)"

        provider = CodexProvider("test1234", "test-session", "window-0", None)
        result = await provider.initialize()

        assert result is True
        mock_wait_shell.assert_called_once()
        # Two send_keys calls: warm-up echo + codex with tmux-compatible flags
        assert mock_tmux.send_keys.call_count == 2
        mock_tmux.send_keys.assert_any_call("test-session", "window-0", "echo ready")
        mock_tmux.send_keys.assert_any_call(
            "test-session",
            "window-0",
            "codex --no-alt-screen --disable shell_snapshot",
        )
        mock_wait_status.assert_called_once()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.codex.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.codex.tmux_client")
    async def test_initialize_shell_timeout(self, mock_tmux, mock_wait_shell):
        mock_wait_shell.return_value = False

        provider = CodexProvider("test1234", "test-session", "window-0", None)

        with pytest.raises(TimeoutError, match="Shell initialization timed out"):
            await provider.initialize()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.codex.wait_until_status")
    @patch("cli_agent_orchestrator.providers.codex.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.codex.tmux_client")
    async def test_initialize_codex_timeout(self, mock_tmux, mock_wait_shell, mock_wait_status):
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = False
        mock_tmux.get_history.return_value = "OpenAI Codex (v0.98.0)"

        provider = CodexProvider("test1234", "test-session", "window-0", None)

        with pytest.raises(TimeoutError, match="Codex initialization timed out"):
            await provider.initialize()


class TestCodexBuildCommand:
    def test_build_command_no_profile(self):
        provider = CodexProvider("test1234", "test-session", "window-0", None)
        command = provider._build_codex_command()
        assert command == "codex --no-alt-screen --disable shell_snapshot"

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_build_command_with_agent_profile(self, mock_load_profile):
        mock_profile = MagicMock()
        mock_profile.system_prompt = "You are a code supervisor agent."
        mock_profile.mcpServers = None
        mock_load_profile.return_value = mock_profile

        provider = CodexProvider("test1234", "test-session", "window-0", "code_supervisor")
        command = provider._build_codex_command()

        mock_load_profile.assert_called_once_with("code_supervisor")
        assert "codex --no-alt-screen --disable shell_snapshot" in command
        assert "-c" in command
        assert "developer_instructions=" in command
        assert "You are a code supervisor agent." in command

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_build_command_escapes_quotes(self, mock_load_profile):
        mock_profile = MagicMock()
        mock_profile.system_prompt = 'Use "double quotes" carefully.'
        mock_profile.mcpServers = None
        mock_load_profile.return_value = mock_profile

        provider = CodexProvider("test1234", "test-session", "window-0", "test_agent")
        command = provider._build_codex_command()

        assert '\\"double quotes\\"' in command

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_build_command_escapes_newlines(self, mock_load_profile):
        mock_profile = MagicMock()
        mock_profile.system_prompt = "Line one.\nLine two.\n\n## Section\n- Item"
        mock_profile.mcpServers = None
        mock_load_profile.return_value = mock_profile

        provider = CodexProvider("test1234", "test-session", "window-0", "test_agent")
        command = provider._build_codex_command()

        # Literal newlines must be escaped to \n for TOML and tmux compatibility
        assert "\n" not in command
        assert "\\n" in command
        assert "Line one.\\nLine two.\\n\\n## Section\\n- Item" in command

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_build_command_with_mcp_servers(self, mock_load_profile):
        mock_profile = MagicMock()
        mock_profile.system_prompt = "You are a supervisor."
        mock_profile.mcpServers = {
            "cao-mcp-server": {
                "type": "stdio",
                "command": "uvx",
                "args": ["--from", "git+https://example.com/repo.git@main", "cao-mcp-server"],
            }
        }
        mock_load_profile.return_value = mock_profile

        provider = CodexProvider("test1234", "test-session", "window-0", "code_supervisor")
        command = provider._build_codex_command()

        assert "mcp_servers.cao-mcp-server.command=" in command
        assert "uvx" in command
        assert "mcp_servers.cao-mcp-server.args=" in command
        assert "cao-mcp-server" in command
        # CAO_TERMINAL_ID must be forwarded for handoff to work
        assert "mcp_servers.cao-mcp-server.env_vars=" in command
        assert "CAO_TERMINAL_ID" in command
        # Tool timeout must be a TOML float (600.0) for Codex's f64 deserializer
        assert "mcp_servers.cao-mcp-server.tool_timeout_sec=600.0" in command

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_build_command_with_mcp_servers_env(self, mock_load_profile):
        mock_profile = MagicMock()
        mock_profile.system_prompt = ""
        mock_profile.mcpServers = {
            "test-server": {
                "command": "npx",
                "args": ["-y", "test-server"],
                "env": {"API_KEY": "secret123"},
            }
        }
        mock_load_profile.return_value = mock_profile

        provider = CodexProvider("test1234", "test-session", "window-0", "test_agent")
        command = provider._build_codex_command()

        assert "mcp_servers.test-server.command=" in command
        assert "mcp_servers.test-server.env.API_KEY=" in command
        assert "secret123" in command
        # CAO_TERMINAL_ID always forwarded even without explicit env_vars
        assert "mcp_servers.test-server.env_vars=" in command
        assert "CAO_TERMINAL_ID" in command

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_build_command_mcp_preserves_existing_env_vars(self, mock_load_profile):
        mock_profile = MagicMock()
        mock_profile.system_prompt = ""
        mock_profile.mcpServers = {
            "my-server": {
                "command": "node",
                "args": ["server.js"],
                "env_vars": ["HOME", "PATH"],
            }
        }
        mock_load_profile.return_value = mock_profile

        provider = CodexProvider("test1234", "test-session", "window-0", "test_agent")
        command = provider._build_codex_command()

        # Existing env_vars preserved and CAO_TERMINAL_ID appended
        assert "HOME" in command
        assert "PATH" in command
        assert "CAO_TERMINAL_ID" in command

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_build_command_empty_system_prompt(self, mock_load_profile):
        mock_profile = MagicMock()
        mock_profile.system_prompt = ""
        mock_profile.mcpServers = None
        mock_load_profile.return_value = mock_profile

        provider = CodexProvider("test1234", "test-session", "window-0", "empty_agent")
        command = provider._build_codex_command()

        assert command == "codex --no-alt-screen --disable shell_snapshot"
        assert "developer_instructions" not in command

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_build_command_none_system_prompt(self, mock_load_profile):
        mock_profile = MagicMock()
        mock_profile.system_prompt = None
        mock_profile.mcpServers = None
        mock_load_profile.return_value = mock_profile

        provider = CodexProvider("test1234", "test-session", "window-0", "none_agent")
        command = provider._build_codex_command()

        assert command == "codex --no-alt-screen --disable shell_snapshot"

    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    def test_build_command_profile_load_failure(self, mock_load_profile):
        mock_load_profile.side_effect = RuntimeError("Profile not found")

        provider = CodexProvider("test1234", "test-session", "window-0", "bad_agent")

        with pytest.raises(ProviderError, match="Failed to load agent profile"):
            provider._build_codex_command()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.codex.wait_until_status")
    @patch("cli_agent_orchestrator.providers.codex.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.codex.load_agent_profile")
    @patch("cli_agent_orchestrator.providers.codex.tmux_client")
    async def test_initialize_with_agent_profile(
        self, mock_tmux, mock_load_profile, mock_wait_shell, mock_wait_status
    ):
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_tmux.get_history.return_value = "OpenAI Codex (v0.98.0)"
        mock_profile = MagicMock()
        mock_profile.system_prompt = "You are a supervisor."
        mock_profile.mcpServers = None
        mock_load_profile.return_value = mock_profile

        provider = CodexProvider("test1234", "test-session", "window-0", "code_supervisor")
        result = await provider.initialize()

        assert result is True
        # The second send_keys call should contain developer_instructions
        codex_call = mock_tmux.send_keys.call_args_list[1]
        assert "developer_instructions=" in codex_call.args[2]
        assert "You are a supervisor." in codex_call.args[2]


class TestCodexProviderStatusDetection:
    def test_get_status_idle(self):
        output = load_fixture("codex_idle_output.txt")

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.IDLE

    def test_get_status_completed(self):
        output = load_fixture("codex_completed_output.txt")

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED

    def test_get_status_processing(self):
        output = load_fixture("codex_processing_output.txt")

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.PROCESSING

    def test_get_status_waiting_user_answer(self):
        output = load_fixture("codex_permission_output.txt")

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.WAITING_USER_ANSWER

    def test_get_status_error(self):
        output = load_fixture("codex_error_output.txt")

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.ERROR

    def test_get_status_empty_output(self):
        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status("")

        assert status == TerminalStatus.UNKNOWN

    def test_get_status_processing_when_old_prompt_present(self):
        # If the captured history contains an earlier prompt but the *latest* output is processing,
        # we should report PROCESSING. The old prompt should be far enough from the bottom
        # (more than IDLE_PROMPT_TAIL_LINES) to avoid false idle detection.
        output = (
            "Welcome to Codex\n"
            "❯ \n"
            "You Fix the failing tests\n"
            "assistant: Working on it...\n"
            "Reading file src/main.py...\n"
            "Analyzing code structure...\n"
            "Checking dependencies...\n"
            "Codex is thinking…\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.PROCESSING

    def test_get_status_not_error_on_failed_in_message(self):
        # "failed" is commonly used in normal assistant output; it should not automatically
        # force ERROR.
        output = (
            "You Explain why the test failed\n"
            "assistant: The test failed because the assertion is incorrect.\n"
            "\n"
            "❯ \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED

    def test_get_status_idle_if_no_assistant_after_last_user(self):
        # If there is a user message but no assistant response after it, we should not
        # treat the session as COMPLETED.
        output = "assistant: Welcome\n" "You Do the thing\n" "\n" "❯ \n"

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.IDLE

    def test_get_status_processing_when_no_prompt_and_no_keywords(self):
        # Codex output may not always include explicit "thinking/processing" keywords.
        # Without an idle prompt at the end, we should assume it's still processing.
        output = "You Run the command\nWorking...\n"

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.PROCESSING

    def test_get_status_not_error_when_assistant_mentions_error_text(self):
        output = (
            "You Explain the failure\n"
            "assistant: Here's an example error:\n"
            "Error: example only\n"
            "\n"
            "❯ \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED

    def test_get_status_not_waiting_when_assistant_mentions_approval_text(self):
        output = (
            "You Explain approvals\n"
            "assistant: You might see this prompt:\n"
            "Approve this command? [y/n]\n"
            "\n"
            "❯ \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED

    def test_get_status_error_when_error_after_user_and_prompt(self):
        output = "You Run thing\nError: failed\n\n❯ \n"

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.ERROR

    def test_get_status_waiting_user_answer_when_no_user_prefix(self):
        output = "Approve this command? [y/n]\n"

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.WAITING_USER_ANSWER

    def test_get_status_error_when_no_user_prefix(self):
        output = "Error: something failed\n"

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.ERROR

    def test_get_status_idle_tui_with_status_bar(self):
        """Test IDLE detection with realistic TUI output (status bar after prompt)."""
        output = (
            "╭───────────────────────────────────────────╮\n"
            "│ >_ OpenAI Codex (v0.98.0)                 │\n"
            "│ model: gpt-5.3-codex high                 │\n"
            "│ directory: ~/project                      │\n"
            "╰───────────────────────────────────────────╯\n"
            "  Tip: Try the Codex App\n"
            "› Use /skills to list available skills\n"
            "  ? for shortcuts                     100% context left\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.IDLE

    def test_get_status_completed_tui_with_status_bar(self):
        """Test COMPLETED detection with TUI output (status bar after prompt)."""
        output = (
            "You Fix the bug\n"
            "assistant: I've fixed the issue in main.py.\n"
            "\n"
            "› \n"
            "  ? for shortcuts                     100% context left\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED


class TestCodexBulletFormatStatusDetection:
    """Tests for Codex's real interactive output format using › prompt and • bullets."""

    def test_get_status_completed_bullet_format(self):
        """COMPLETED when › user message followed by • response and idle prompt."""
        output = (
            "› what is your role?\n"
            "• I am the Coding Supervisor Agent.\n"
            "• I coordinate tasks between developer and reviewer agents.\n"
            "\n"
            "› \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED

    def test_get_status_processing_bullet_format(self):
        """PROCESSING when • response started but no idle prompt at bottom."""
        output = (
            "› fix the failing tests\n"
            "• Let me look at the test files.\n"
            "Reading src/test_main.py...\n"
            "Analyzing code structure...\n"
            "Checking dependencies...\n"
            "Running unit tests...\n"
            "Codex is thinking…\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.PROCESSING

    def test_get_status_idle_bullet_format_no_response(self):
        """IDLE when › user message but no • response yet and idle prompt at bottom."""
        output = "› hello\n\n› \n"

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.IDLE

    def test_get_status_completed_bullet_with_code_block(self):
        """COMPLETED with • response containing code blocks."""
        output = (
            "› show me a function\n"
            "• Here's the function:\n"
            "\n"
            "  ```python\n"
            "  def hello():\n"
            "      print('hello')\n"
            "  ```\n"
            "\n"
            "• Let me know if you need changes.\n"
            "\n"
            "› \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED

    def test_get_status_error_not_masked_by_bullet_pattern(self):
        """ERROR still detected when no • response and error after › user message."""
        output = "› do something\nError: connection refused\n"

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.ERROR

    def test_get_status_completed_multi_turn_bullet(self):
        """COMPLETED uses last user message in multi-turn bullet format."""
        output = (
            "› first question\n"
            "• First answer.\n"
            "\n"
            "› second question\n"
            "• Second answer with details.\n"
            "\n"
            "› \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED

    def test_get_status_completed_bullet_with_tui_status_bar(self):
        """COMPLETED with bullet format and TUI status bar after prompt."""
        output = (
            "› fix the bug\n"
            "• I've fixed the issue in main.py by correcting the import.\n"
            "\n"
            "› \n"
            "  ? for shortcuts                     98% context left\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED

    def test_get_status_processing_tui_spinner(self):
        """PROCESSING when TUI shows • Working spinner, not false COMPLETED."""
        output = (
            "› [CAO Handoff] Supervisor terminal ID: sup-123. Do the task.\n"
            "\n"
            "• Working (0s • esc to interrupt)\n"
            "\n"
            "› Use /skills to list available skills\n"
            "\n"
            "  ? for shortcuts                     100% context left\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.PROCESSING

    def test_get_status_processing_tui_thinking_spinner(self):
        """PROCESSING when TUI shows • Thinking spinner."""
        output = (
            "› Implement feature X\n"
            "\n"
            "• Thinking (3s • esc to interrupt)\n"
            "\n"
            "› Run /review on my current changes\n"
            "\n"
            "  ? for shortcuts                     95% context left\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.PROCESSING

    def test_get_status_processing_dynamic_spinner_text(self):
        """PROCESSING when TUI shows spinner with dynamic prefix text."""
        output = (
            "› [CAO Handoff] Do the task.\n"
            "\n"
            "• Creating /tmp/file.py\n"
            "\n"
            "• Starting script creation (10s • esc to interrupt)\n"
            "\n"
            "› Use /skills to list available skills\n"
            "\n"
            "  ? for shortcuts                     100% context left\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        assert status == TerminalStatus.PROCESSING


class TestCodexV0111FooterFormat:
    """Tests for Codex v0.111.0+ TUI footer format.

    v0.111.0 (PR #13202 'tui: restore draft footer hints') changed the footer:
    - Old: "› Use /skills to list available skills\\n  ? for shortcuts  100% context left"
    - New: "› Find and fix a bug in @filename\\n  gpt-5.3-codex high · 100% left · ~/path"
    The new format uses "N% left" instead of "N% context left" and removes "? for shortcuts".
    """

    def test_get_status_idle_v0111_footer(self):
        """IDLE with v0.111.0 footer format (no '? for shortcuts')."""
        output = (
            "╭───────────────────────────────────────────╮\n"
            "│ >_ OpenAI Codex (v0.111.0)                │\n"
            "│ model: gpt-5.3-codex high                 │\n"
            "│ directory: ~/project                      │\n"
            "╰───────────────────────────────────────────╯\n"
            "  Tip: You can run any shell command from Codex using ! (e.g. !ls)\n"
            "\n"
            "› Find and fix a bug in @filename\n"
            "\n"
            "  gpt-5.3-codex high · 100% left · ~/project\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.IDLE

    def test_get_status_completed_v0111_footer(self):
        """COMPLETED with v0.111.0 footer (suggestion hint must not be treated as user input)."""
        output = (
            "› fix the bug\n"
            "• I've fixed the issue in main.py by correcting the import.\n"
            "\n"
            "› Find and fix a bug in @filename\n"
            "\n"
            "  gpt-5.3-codex high · 98% left · ~/project\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_get_status_completed_v0111_multi_turn(self):
        """COMPLETED in multi-turn with v0.111.0 footer."""
        output = (
            "› first question\n"
            "• First answer.\n"
            "\n"
            "› second question\n"
            "• Second answer with details.\n"
            "\n"
            "› Write tests for @main.py\n"
            "\n"
            "  gpt-5.3-codex high · 95% left · ~/project\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.COMPLETED

    def test_get_status_processing_v0111_spinner(self):
        """PROCESSING when TUI shows spinner with v0.111.0 footer."""
        output = (
            "› [CAO Handoff] Do the task.\n"
            "\n"
            "• Working (0s • esc to interrupt)\n"
            "\n"
            "› Find and fix a bug in @filename\n"
            "\n"
            "  gpt-5.3-codex high · 100% left · ~/project\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        assert provider.get_status(output) == TerminalStatus.PROCESSING


class TestCodexProviderMessageExtraction:
    def test_extract_last_message_success(self):
        output = load_fixture("codex_completed_output.txt")

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "Here's the fix" in message
        assert "All tests now pass." in message

    def test_extract_complex_message(self):
        output = load_fixture("codex_complex_response.txt")

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "def add(a, b):" in message
        assert "Let me know" in message

    def test_extract_message_no_marker(self):
        output = "No assistant prefix here"

        provider = CodexProvider("test1234", "test-session", "window-0")

        with pytest.raises(ValueError, match="No Codex response found"):
            provider.extract_last_message_from_script(output)

    def test_extract_message_empty_response(self):
        output = "assistant:   \n\n❯ "

        provider = CodexProvider("test1234", "test-session", "window-0")

        with pytest.raises(ValueError, match="Empty Codex response"):
            provider.extract_last_message_from_script(output)


class TestCodexBulletFormatExtraction:
    """Tests for message extraction from Codex's real • bullet format."""

    def test_extract_bullet_format_single_line(self):
        """Extract single-line • response."""
        output = "› what is your role?\n• I am the Coding Supervisor Agent.\n\n› \n"

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "I am the Coding Supervisor Agent." in message

    def test_extract_bullet_format_multi_line(self):
        """Extract multi-line • response with all bullets preserved."""
        output = (
            "› describe your capabilities\n"
            "• I can coordinate development tasks.\n"
            "• I assign work to developer agents.\n"
            "• I review results from workers.\n"
            "\n"
            "› \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "coordinate development tasks" in message
        assert "assign work" in message
        assert "review results" in message

    def test_extract_bullet_format_with_code_block(self):
        """Extract • response containing code blocks."""
        output = (
            "› show me the fix\n"
            "• Here's the corrected code:\n"
            "\n"
            "  ```python\n"
            "  def add(a, b):\n"
            "      return a + b\n"
            "  ```\n"
            "\n"
            "• All tests pass now.\n"
            "\n"
            "› \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "def add(a, b):" in message
        assert "All tests pass now." in message

    def test_extract_bullet_format_multi_turn(self):
        """Extract only the last response from multi-turn • format."""
        output = (
            "› first question\n"
            "• First answer.\n"
            "\n"
            "› second question\n"
            "• Second answer with more detail.\n"
            "• Additional context here.\n"
            "\n"
            "› \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        # Should only contain the second response
        assert "First answer" not in message
        assert "Second answer with more detail." in message
        assert "Additional context here." in message

    def test_extract_bullet_format_without_trailing_prompt(self):
        """Extract • response when no trailing idle prompt (output still streaming)."""
        output = "› fix the bug\n• I've fixed the import issue in main.py.\n"

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "I've fixed the import issue" in message


class TestCodexV0111Extraction:
    """Extraction tests for Codex v0.111.0+ footer format."""

    def test_extract_bullet_with_v0111_footer(self):
        """Extract response when v0.111.0 footer (suggestion hint) is present."""
        output = (
            "› fix the bug\n"
            "• I've fixed the issue in main.py by correcting the import.\n"
            "\n"
            "› Find and fix a bug in @filename\n"
            "\n"
            "  gpt-5.3-codex high · 98% left · ~/project\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "I've fixed the issue" in message
        # Suggestion hint should not leak into extracted output
        assert "Find and fix a bug" not in message
        assert "gpt-5.3-codex" not in message

    def test_extract_multi_turn_with_v0111_footer(self):
        """Extract last response from multi-turn with v0.111.0 footer."""
        output = (
            "› first question\n"
            "• First answer.\n"
            "\n"
            "› second question\n"
            "• Second answer with details.\n"
            "\n"
            "› Write tests for @main.py\n"
            "\n"
            "  gpt-5.3-codex high · 95% left · ~/project\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "First answer" not in message
        assert "Second answer with details." in message
        assert "Write tests" not in message

    def test_extract_double_blank_between_hint_and_status(self):
        """Suggestion hint must not leak when 2 blank lines separate it from status bar."""
        output = (
            "› fix the bug\n"
            "• I've fixed the issue in main.py by correcting the import.\n"
            "\n"
            "› Find and fix a bug in @filename\n"
            "\n"
            "\n"
            "  gpt-5.3-codex high · 98% left · ~/project\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "I've fixed the issue" in message
        assert "Find and fix a bug" not in message


class TestCodexProviderMisc:
    def test_exit_cli(self):
        provider = CodexProvider("test1234", "test-session", "window-0")
        assert provider.exit_cli() == "/exit"

    def test_cleanup(self):
        provider = CodexProvider("test1234", "test-session", "window-0")
        provider._initialized = True
        provider.cleanup()
        assert provider._initialized is False

    def test_extract_last_message_without_trailing_prompt(self):
        output = "You do thing\nassistant: Hello\nSecond line\n"
        provider = CodexProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)
        assert message == "Hello\nSecond line"


class TestCodexProviderTrustPrompt:
    """Tests for Codex workspace trust prompt handling."""

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.codex.tmux_client")
    async def test_handle_trust_prompt_detected_and_accepted(self, mock_tmux):
        """Test that trust prompt is detected and auto-accepted."""
        mock_tmux.get_history.return_value = (
            "> You are running Codex in /Users/test/project\n"
            "\n"
            "  Since this folder is version controlled, you may wish to "
            "allow Codex to work in this folder without asking for approval.\n"
            "\n"
            "› 1. Yes, allow Codex to work in this folder without asking for approval\n"
            "  2. No, ask me to approve edits and commands\n"
        )
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_tmux.server.sessions.get.return_value = mock_session
        mock_session.windows.get.return_value = mock_window
        mock_window.active_pane = mock_pane

        provider = CodexProvider("test1234", "test-session", "window-0")
        await provider._handle_trust_prompt(timeout=2.0)

        mock_pane.send_keys.assert_called_once_with("", enter=True)

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.codex.tmux_client")
    async def test_handle_trust_prompt_not_needed(self, mock_tmux):
        """Test early return when Codex starts without trust prompt."""
        mock_tmux.get_history.return_value = "OpenAI Codex (v0.98.0)\n› "

        provider = CodexProvider("test1234", "test-session", "window-0")
        await provider._handle_trust_prompt(timeout=2.0)

        mock_tmux.server.sessions.get.assert_not_called()

    def test_get_status_trust_prompt_is_waiting_user_answer(self):
        """Test that trust prompt reports WAITING_USER_ANSWER, not PROCESSING."""
        output = (
            "> You are running Codex in /Users/test/project\n"
            "allow Codex to work in this folder without asking for approval.\n"
            "› 1. Yes\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(output)

        # Should be WAITING_USER_ANSWER (not PROCESSING despite "running" in text)
        assert status == TerminalStatus.WAITING_USER_ANSWER

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.codex.wait_until_status")
    @patch("cli_agent_orchestrator.providers.codex.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.codex.tmux_client")
    async def test_initialize_with_trust_prompt(self, mock_tmux, mock_wait_shell, mock_wait_status):
        """Test that initialize handles trust prompt during startup."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_tmux.get_history.return_value = (
            "allow Codex to work in this folder without asking for approval.\n"
        )
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_tmux.server.sessions.get.return_value = mock_session
        mock_session.windows.get.return_value = mock_window
        mock_window.active_pane = mock_pane

        provider = CodexProvider("test1234", "test-session", "window-0")
        result = await provider.initialize()

        assert result is True
        mock_pane.send_keys.assert_called_with("", enter=True)

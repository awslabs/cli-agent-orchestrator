"""Unit tests for Devin CLI provider."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.devin_cli import DevinCliProvider

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(filename: str) -> str:
    with open(FIXTURES_DIR / filename, "r") as f:
        return f.read()


class TestDevinCliProviderInitialization:
    """Test Devin CLI provider initialization."""

    @patch("cli_agent_orchestrator.providers.devin_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.devin_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.devin_cli.tmux_client")
    @pytest.mark.asyncio
    async def test_initialize_success(self, mock_tmux, mock_wait_status, mock_wait_shell):
        """Test successful initialization."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True
        mock_tmux.get_history.return_value = ""

        provider = DevinCliProvider("test1234", "test-session", "window-0")
        result = await provider.initialize()

        assert result is True
        mock_wait_shell.assert_called_once()
        mock_tmux.send_keys.assert_called_once()
        mock_wait_status.assert_called_once()

    def test_paste_enter_count_is_1(self):
        """Devin TUI accepts input with a single Enter after paste."""
        provider = DevinCliProvider("test1234", "test-session", "window-0")
        assert provider.paste_enter_count == 1

    def test_exit_cli_returns_slash_exit(self):
        """Verify exit_cli() returns the correct exit command for Devin CLI."""
        provider = DevinCliProvider("test1234", "test-session", "window-0")
        assert provider.exit_cli() == "/exit"


class TestDevinCliProviderStatusDetection:
    """Test status detection from terminal output."""

    @patch("cli_agent_orchestrator.providers.devin_cli.tmux_client")
    def test_get_status_idle(self, mock_tmux):
        """IDLE: status bar + input prompt visible, no user-input line."""
        buffer = load_fixture("devin_cli_idle_output.txt")

        provider = DevinCliProvider("test1234", "test-session", "window-0")
        status = provider.get_status(buffer)

        assert status == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.devin_cli.tmux_client")
    def test_get_status_processing(self, mock_tmux):
        """PROCESSING: spinner text visible ('Running tools')."""
        buffer = load_fixture("devin_cli_processing_output.txt")

        provider = DevinCliProvider("test1234", "test-session", "window-0")
        status = provider.get_status(buffer)

        assert status == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.providers.devin_cli.tmux_client")
    def test_get_status_completed(self, mock_tmux):
        """COMPLETED: user input + response + idle prompt visible."""
        buffer = load_fixture("devin_cli_completed_output.txt")

        provider = DevinCliProvider("test1234", "test-session", "window-0")
        status = provider.get_status(buffer)

        assert status == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.devin_cli.tmux_client")
    def test_get_status_empty_output(self, mock_tmux):
        """PROCESSING: empty/blank output → still starting up."""
        buffer = ""

        provider = DevinCliProvider("test1234", "test-session", "window-0")
        status = provider.get_status(buffer)

        assert status == TerminalStatus.ERROR

    @patch("cli_agent_orchestrator.providers.devin_cli.tmux_client")
    def test_get_status_user_input_no_response(self, mock_tmux):
        """PROCESSING: user input sent but no response lines yet."""
        buffer = (
            "> what is 2+2\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "#\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "Mode: chat  Model: devin-v1\n"
        )

        provider = DevinCliProvider("test1234", "test-session", "window-0")
        status = provider.get_status(buffer)

        assert status == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.devin_cli.tmux_client")
    def test_get_status_esc_to_interrupt(self, mock_tmux):
        """PROCESSING: 'esc to interrupt' spinner is present."""
        buffer = "> write some code\n" "esc to interrupt\n" "#\n" "Mode: chat  Model: devin-v1\n"

        provider = DevinCliProvider("test1234", "test-session", "window-0")
        status = provider.get_status(buffer)

        assert status == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.providers.devin_cli.tmux_client")
    def test_get_status_completed_with_markdown_heading_response(self, mock_tmux):
        """COMPLETED even when the response begins with a Markdown heading (Bug #1 regression)."""
        buffer = load_fixture("devin_cli_heading_response.txt")

        provider = DevinCliProvider("test1234", "test-session", "window-0")
        status = provider.get_status(buffer)

        assert status == TerminalStatus.COMPLETED


class TestDevinCliResponseExtraction:
    """Test response extraction from script output."""

    def test_extract_simple_response(self):
        """Basic extraction between user input and horizontal rule."""
        provider = DevinCliProvider("test1234", "test-session", "window-0")
        output = load_fixture("devin_cli_completed_output.txt")
        message = provider.extract_last_message_from_script(output)

        assert "README.md" in message
        assert "src/" in message

    def test_extract_complex_response(self):
        """Extraction of a multi-line response."""
        provider = DevinCliProvider("test1234", "test-session", "window-0")
        output = load_fixture("devin_cli_complex_response.txt")
        message = provider.extract_last_message_from_script(output)

        assert "orchestrator" in message.lower()
        assert "providers" in message.lower()

    def test_extract_no_user_input_raises(self):
        """Raises ValueError when no user-input line is present."""
        provider = DevinCliProvider("test1234", "test-session", "window-0")
        output = load_fixture("devin_cli_idle_output.txt")

        with pytest.raises(ValueError, match="No user input found"):
            provider.extract_last_message_from_script(output)

    def test_extract_uses_last_user_input(self):
        """Extraction is anchored to the LAST user-input line."""
        provider = DevinCliProvider("test1234", "test-session", "window-0")
        output = (
            "> first question\n"
            "First answer.\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "#\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "Mode: chat  Model: devin-v1\n"
            "> second question\n"
            "Second answer.\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "#\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "Mode: chat  Model: devin-v1\n"
        )
        message = provider.extract_last_message_from_script(output)
        assert message == "Second answer."

    def test_extract_strips_whitespace(self):
        """Leading/trailing blank lines are stripped from the response."""
        provider = DevinCliProvider("test1234", "test-session", "window-0")
        output = (
            "> hello\n"
            "\n"
            "   \n"
            "Hello there!\n"
            "\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "#\n"
            "Mode: chat  Model: devin-v1\n"
        )
        message = provider.extract_last_message_from_script(output)
        assert message == "Hello there!"

    def test_extract_empty_response_raises(self):
        """Raises ValueError when response section is empty."""
        provider = DevinCliProvider("test1234", "test-session", "window-0")
        output = (
            "> hello\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "#\n"
            "Mode: chat  Model: devin-v1\n"
        )
        with pytest.raises(ValueError, match="No response found"):
            provider.extract_last_message_from_script(output)

    def test_extract_response_with_markdown_heading(self):
        """Response starting with a Markdown heading is extracted in full (Bug #1 regression)."""
        provider = DevinCliProvider("test1234", "test-session", "window-0")
        output = load_fixture("devin_cli_heading_response.txt")
        message = provider.extract_last_message_from_script(output)

        # The full response including the "# Overview" heading must be returned.
        assert "# Overview" in message
        assert "Supported providers" in message

    def test_extract_response_with_markdown_heading_inline(self):
        """Markdown headings inside the response are not treated as terminators."""
        provider = DevinCliProvider("test1234", "test-session", "window-0")
        output = (
            "> summarise\n"
            "# Summary\n"
            "Here is the summary.\n"
            "## Details\n"
            "Some details here.\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "#\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "Mode: chat  Model: devin-v1\n"
        )
        message = provider.extract_last_message_from_script(output)
        assert "# Summary" in message
        assert "## Details" in message
        assert "Some details here." in message


class TestDevinCliToolRestrictions:
    """Test that allowed_tools restrictions are enforced via the prompt file."""

    def test_allowed_tools_constraint_prepended_to_prompt(self):
        """Security constraint is prepended when allowed_tools is restricted."""
        provider = DevinCliProvider(
            "test1234", "test-session", "window-0", allowed_tools=["fs_read", "execute_bash"]
        )
        command = provider._build_command()

        # A --prompt-file flag must be present.
        assert "--prompt-file" in command

        # Verify the temp file contains the security constraint and tool list.
        assert provider._temp_prompt_file is not None
        content = open(provider._temp_prompt_file).read()
        assert "fs_read" in content
        assert "execute_bash" in content
        assert "SECURITY CONSTRAINTS" in content

        # Cleanup
        provider.cleanup()

    def test_no_prompt_file_when_unrestricted(self):
        """No prompt file is written when allowed_tools is unrestricted ('*')."""
        provider = DevinCliProvider("test1234", "test-session", "window-0", allowed_tools=["*"])
        provider._build_command()

        assert provider._temp_prompt_file is None
        provider.cleanup()

    def test_no_prompt_file_when_no_profile_and_no_restrictions(self):
        """No prompt file written when there is no profile and no restrictions."""
        provider = DevinCliProvider("test1234", "test-session", "window-0")
        command = provider._build_command()

        assert "--prompt-file" not in command
        assert provider._temp_prompt_file is None
        provider.cleanup()

    def test_tool_restriction_with_agent_profile(self):
        """Security constraint is prepended before the profile system prompt."""
        mock_profile = MagicMock()
        mock_profile.system_prompt = "You are a helpful assistant."

        with patch(
            "cli_agent_orchestrator.utils.agent_profiles.load_agent_profile",
            return_value=mock_profile,
        ):
            provider = DevinCliProvider(
                "test1234",
                "test-session",
                "window-0",
                agent_profile="my-agent",
                allowed_tools=["fs_read"],
            )
            provider._build_command()

        assert provider._temp_prompt_file is not None
        content = open(provider._temp_prompt_file).read()
        # Security constraint must come BEFORE the profile system prompt.
        security_pos = content.find("SECURITY CONSTRAINTS")
        profile_pos = content.find("You are a helpful assistant.")
        assert security_pos < profile_pos
        provider.cleanup()


class TestDevinCliProviderRegistration:
    """Test that Devin CLI is properly registered in the system."""

    def test_provider_type_exists(self):
        """ProviderType enum has DEVIN_CLI entry."""
        from cli_agent_orchestrator.models.provider import ProviderType

        assert hasattr(ProviderType, "DEVIN_CLI")
        assert ProviderType.DEVIN_CLI.value == "devin_cli"

    def test_provider_in_providers_list(self):
        """devin_cli appears in the PROVIDERS constant."""
        from cli_agent_orchestrator.constants import PROVIDERS

        assert "devin_cli" in PROVIDERS

    def test_manager_creates_devin_cli_provider(self):
        """ProviderManager can create a DevinCliProvider."""
        from cli_agent_orchestrator.models.provider import ProviderType
        from cli_agent_orchestrator.providers.manager import ProviderManager

        manager = ProviderManager()
        provider = manager.create_provider(
            ProviderType.DEVIN_CLI.value,
            terminal_id="t1",
            tmux_session="s1",
            tmux_window="w1",
            agent_profile=None,
        )

        assert isinstance(provider, DevinCliProvider)
        assert manager.get_provider("t1") is provider

    def test_devin_cli_in_workspace_access_set(self):
        """devin_cli is in PROVIDERS_REQUIRING_WORKSPACE_ACCESS."""
        from cli_agent_orchestrator.cli.commands.launch import PROVIDERS_REQUIRING_WORKSPACE_ACCESS

        assert "devin_cli" in PROVIDERS_REQUIRING_WORKSPACE_ACCESS

    def test_tool_mapping_has_devin_cli(self):
        """tool_mapping.py defines a mapping for devin_cli."""
        from cli_agent_orchestrator.utils.tool_mapping import TOOL_MAPPING

        assert "devin_cli" in TOOL_MAPPING
        mapping = TOOL_MAPPING["devin_cli"]
        assert "execute_bash" in mapping
        assert "fs_read" in mapping
        assert "fs_write" in mapping

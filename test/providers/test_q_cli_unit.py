"""Unit tests for Q CLI provider."""

import re
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.q_cli import QCliProvider

# Test fixtures directory
FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(filename: str) -> str:
    """Load a fixture file and return its contents."""
    with open(FIXTURES_DIR / filename, "r") as f:
        return f.read()


class TestQCliProviderInitialization:
    """Test Q CLI provider initialization."""

    @patch("cli_agent_orchestrator.providers.q_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.q_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.q_cli.tmux_client")
    def test_initialize_success(self, mock_tmux, mock_wait_status, mock_wait_shell):
        """Test successful initialization."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        result = provider.initialize()

        assert result is True
        mock_wait_shell.assert_called_once()
        mock_tmux.send_keys.assert_called_once_with(
            "test-session", "window-0", "q chat --agent developer"
        )
        mock_wait_status.assert_called_once()

    @patch("cli_agent_orchestrator.providers.q_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.q_cli.tmux_client")
    def test_initialize_shell_timeout(self, mock_tmux, mock_wait_shell):
        """Test initialization with shell timeout."""
        mock_wait_shell.return_value = False

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")

        with pytest.raises(TimeoutError, match="Shell initialization timed out"):
            provider.initialize()

    @patch("cli_agent_orchestrator.providers.q_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.q_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.q_cli.tmux_client")
    def test_initialize_q_cli_timeout(self, mock_tmux, mock_wait_status, mock_wait_shell):
        """Test initialization with Q CLI timeout."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = False

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")

        with pytest.raises(TimeoutError, match="Q CLI initialization timed out"):
            provider.initialize()

    def test_initialization_with_different_agent_profiles(self):
        """Test initialization with various agent profile names."""
        test_profiles = ["developer", "code-reviewer", "test_agent", "agent123"]

        for profile in test_profiles:
            provider = QCliProvider("test1234", "test-session", "window-0", profile)
            assert provider._agent_profile == profile
            # Verify dynamic prompt pattern includes the profile
            assert re.escape(profile) in provider._idle_prompt_pattern


class TestQCliProviderStatusDetection:
    """Test status detection logic."""

    @patch("cli_agent_orchestrator.providers.q_cli.tmux_client")
    def test_get_status_idle(self, mock_tmux):
        """Test IDLE status detection."""
        mock_tmux.get_history.return_value = load_fixture("q_cli_idle_output.txt")

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        status = provider.get_status()

        assert status == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.q_cli.tmux_client")
    def test_get_status_completed(self, mock_tmux):
        """Test COMPLETED status detection."""
        mock_tmux.get_history.return_value = load_fixture("q_cli_completed_output.txt")

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        status = provider.get_status()

        assert status == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.q_cli.tmux_client")
    def test_get_status_processing(self, mock_tmux):
        """Test PROCESSING status detection."""
        mock_tmux.get_history.return_value = load_fixture("q_cli_processing_output.txt")

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        status = provider.get_status()

        assert status == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.providers.q_cli.tmux_client")
    def test_get_status_waiting_user_answer(self, mock_tmux):
        """Test WAITING_USER_ANSWER status detection."""
        mock_tmux.get_history.return_value = load_fixture("q_cli_permission_output.txt")

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        status = provider.get_status()

        assert status == TerminalStatus.WAITING_USER_ANSWER

    @patch("cli_agent_orchestrator.providers.q_cli.tmux_client")
    def test_get_status_error(self, mock_tmux):
        """Test ERROR status detection."""
        mock_tmux.get_history.return_value = load_fixture("q_cli_error_output.txt")

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        status = provider.get_status()

        assert status == TerminalStatus.ERROR

    @patch("cli_agent_orchestrator.providers.q_cli.tmux_client")
    def test_get_status_with_empty_output(self, mock_tmux):
        """Test status detection with empty output."""
        mock_tmux.get_history.return_value = ""

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        status = provider.get_status()

        assert status == TerminalStatus.ERROR

    @patch("cli_agent_orchestrator.providers.q_cli.tmux_client")
    def test_get_status_with_tail_lines(self, mock_tmux):
        """Test status detection with tail_lines parameter."""
        mock_tmux.get_history.return_value = load_fixture("q_cli_idle_output.txt")

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        status = provider.get_status(tail_lines=50)

        assert status == TerminalStatus.IDLE
        mock_tmux.get_history.assert_called_once_with("test-session", "window-0", tail_lines=50)


class TestQCliProviderMessageExtraction:
    """Test message extraction from terminal output."""

    def test_extract_last_message_success(self):
        """Test successful message extraction."""
        output = load_fixture("q_cli_completed_output.txt")

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        message = provider.extract_last_message_from_script(output)

        # Verify ANSI codes are cleaned
        assert "\x1b[" not in message
        # Verify message content is present
        assert "comprehensive response" in message
        assert "multiple paragraphs" in message

    def test_extract_complex_message(self):
        """Test extraction of complex message with code blocks."""
        output = load_fixture("q_cli_complex_response.txt")

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        message = provider.extract_last_message_from_script(output)

        # Verify content
        assert "Python Example" in message
        assert "JavaScript Example" in message
        assert "def hello_world():" in message
        assert "function helloWorld()" in message
        # Verify ANSI codes are cleaned
        assert "\x1b[" not in message

    def test_extract_message_no_green_arrow(self):
        """Test extraction fails when no green arrow is present."""
        output = "\x1b[36m[developer]\x1b[35m>\x1b[39m "

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")

        with pytest.raises(ValueError, match="No Q CLI response found"):
            provider.extract_last_message_from_script(output)

    def test_extract_message_no_final_prompt(self):
        """Test extraction fails when no final prompt is present."""
        output = "\x1b[38;5;10m> \x1b[39mSome response text"

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")

        with pytest.raises(ValueError, match="Incomplete Q CLI response"):
            provider.extract_last_message_from_script(output)

    def test_extract_message_empty_response(self):
        """Test extraction fails when response is empty."""
        output = "\x1b[38;5;10m> \x1b[39m\x1b[36m[developer]\x1b[35m>\x1b[39m"

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")

        with pytest.raises(
            ValueError, match="Incomplete Q CLI response - no final prompt detected after response"
        ):
            provider.extract_last_message_from_script(output)

    def test_extract_message_multiple_responses(self):
        """Test extraction uses the last response when multiple are present."""
        output = (
            "\x1b[38;5;10m> \x1b[39mFirst response\n"
            "\x1b[36m[developer]\x1b[35m>\x1b[39m\n"
            "\x1b[38;5;10m> \x1b[39mSecond response\n"
            "\x1b[36m[developer]\x1b[35m>\x1b[39m"
        )

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        message = provider.extract_last_message_from_script(output)

        assert "Second response" in message
        assert "First response" not in message

    def test_extract_message_with_trailing_text(self):
        """Test extraction works when prompt has trailing text."""
        output = (
            "[developer] 4% λ > User message here\n"
            "\n"
            "> Response text here\n"
            "More response content\n"
            "\n"
            "[developer] 5% λ > How can I help?"
        )

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        message = provider.extract_last_message_from_script(output)

        assert "Response text here" in message
        assert "More response content" in message
        assert "How can I help?" not in message
        assert "User message" not in message


class TestQCliProviderRegexPatterns:
    """Test regex pattern matching."""

    def test_green_arrow_pattern(self):
        """Test green arrow pattern detection."""
        from cli_agent_orchestrator.providers.q_cli import GREEN_ARROW_PATTERN

        # Should match (test with ANSI-cleaned input)
        assert re.search(GREEN_ARROW_PATTERN, "> ")
        assert re.search(GREEN_ARROW_PATTERN, ">")

        # Should not match (not at start of line)
        assert not re.search(GREEN_ARROW_PATTERN, "text > ", re.MULTILINE)
        assert not re.search(GREEN_ARROW_PATTERN, "some>", re.MULTILINE)

    def test_idle_prompt_pattern_with_profile(self):
        """Test idle prompt pattern with different profiles."""
        provider = QCliProvider("test1234", "test-session", "window-0", "developer")

        # Should match (test with ANSI-cleaned input)
        assert re.search(provider._idle_prompt_pattern, "[developer]>")
        assert re.search(provider._idle_prompt_pattern, "[developer]> ")
        assert re.search(provider._idle_prompt_pattern, "[developer]>\n")

        # Should not match different profile
        assert not re.search(provider._idle_prompt_pattern, "\x1b[36m[reviewer]\x1b[35m>\x1b[39m")

    def test_idle_prompt_pattern_with_percentage(self):
        """Test idle prompt pattern with usage percentage."""
        provider = QCliProvider("test1234", "test-session", "window-0", "developer")

        # Should match with percentage (test with ANSI-cleaned input)
        assert re.search(
            provider._idle_prompt_pattern,
            "[developer] 45%>",
        )
        assert re.search(
            provider._idle_prompt_pattern,
            "[developer] 100%>",
        )
        # Should match when an optional U+03BB lambda character appears before >
        assert re.search(provider._idle_prompt_pattern, "[developer] 45%\u03bb>")
        assert re.search(provider._idle_prompt_pattern, "[developer] 45%\u03bb >")
        assert re.search(provider._idle_prompt_pattern, "[developer] 100%\u03bb>")

    def test_idle_prompt_pattern_with_trailing_text(self):
        """Test idle prompt pattern matches with trailing text."""
        provider = QCliProvider("test1234", "test-session", "window-0", "developer")

        # Should match with various trailing text
        assert re.search(provider._idle_prompt_pattern, "[developer]> How can I help?")
        assert re.search(provider._idle_prompt_pattern, "[developer] 16% λ > How can I help?")
        assert re.search(
            provider._idle_prompt_pattern, "[developer]> What would you like to do next?"
        )
        assert re.search(provider._idle_prompt_pattern, "[developer] 5% > Ready for next task")

    def test_permission_prompt_pattern(self):
        """Test permission prompt pattern detection."""
        provider = QCliProvider("test1234", "test-session", "window-0", "developer")

        permission_text = "Allow this action? [y/n/t]: [developer]>"
        assert re.search(provider._permission_prompt_pattern, permission_text)

    def test_ansi_code_cleaning(self):
        """Test ANSI code pattern cleaning."""
        from cli_agent_orchestrator.providers.q_cli import ANSI_CODE_PATTERN

        text = "\x1b[36mColored text\x1b[39m normal text"
        cleaned = re.sub(ANSI_CODE_PATTERN, "", text)

        assert cleaned == "Colored text normal text"
        assert "\x1b[" not in cleaned


class TestQCliProviderPromptPatterns:
    """Test various prompt pattern combinations."""

    @patch("cli_agent_orchestrator.providers.q_cli.tmux_client")
    def test_basic_prompt(self, mock_tmux):
        """Test basic prompt without extras."""
        mock_tmux.get_history.return_value = "\x1b[36m[developer]\x1b[35m>\x1b[39m "

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        status = provider.get_status()

        assert status == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.q_cli.tmux_client")
    def test_prompt_with_percentage(self, mock_tmux):
        """Test prompt with usage percentage."""
        mock_tmux.get_history.return_value = "\x1b[36m[developer] \x1b[32m75%\x1b[35m>\x1b[39m "

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        status = provider.get_status()

        assert status == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.q_cli.tmux_client")
    def test_prompt_with_special_profile_characters(self, mock_tmux):
        """Test prompt with special characters in profile name."""
        mock_tmux.get_history.return_value = "\x1b[36m[code-reviewer_v2]\x1b[35m>\x1b[39m "

        provider = QCliProvider("test1234", "test-session", "window-0", "code-reviewer_v2")
        status = provider.get_status()

        assert status == TerminalStatus.IDLE


class TestQCliProviderHandoffScenarios:
    """Test handoff scenarios between agents."""

    @patch("cli_agent_orchestrator.providers.q_cli.tmux_client")
    def test_handoff_successful_status(self, mock_tmux):
        """Test COMPLETED status detection with successful handoff."""
        mock_tmux.get_history.return_value = load_fixture("q_cli_handoff_successful.txt")

        provider = QCliProvider("test1234", "test-session", "window-0", "supervisor")
        status = provider.get_status()

        assert status == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.q_cli.tmux_client")
    def test_handoff_successful_message_extraction(self, mock_tmux):
        """Test message extraction from successful handoff output."""
        output = load_fixture("q_cli_handoff_successful.txt")
        mock_tmux.get_history.return_value = output

        provider = QCliProvider("test1234", "test-session", "window-0", "supervisor")
        message = provider.extract_last_message_from_script(output)

        # Verify message extraction works (extracts LAST response only)
        assert len(message) > 0
        assert "\x1b[" not in message  # ANSI codes cleaned
        assert "handoff" in message.lower()
        assert "completed successfully" in message.lower()
        assert "developer agent" in message.lower()

    @patch("cli_agent_orchestrator.providers.q_cli.tmux_client")
    def test_handoff_error_status(self, mock_tmux):
        """Test ERROR status detection with failed handoff."""
        mock_tmux.get_history.return_value = load_fixture("q_cli_handoff_error.txt")

        provider = QCliProvider("test1234", "test-session", "window-0", "supervisor")
        status = provider.get_status()

        assert status == TerminalStatus.ERROR

    @patch("cli_agent_orchestrator.providers.q_cli.tmux_client")
    def test_handoff_error_message_extraction(self, mock_tmux):
        """Test message extraction from failed handoff output."""
        output = load_fixture("q_cli_handoff_error.txt")
        mock_tmux.get_history.return_value = output

        provider = QCliProvider("test1234", "test-session", "window-0", "supervisor")

        # Even with error, should be able to extract the message
        message = provider.extract_last_message_from_script(output)

        assert len(message) > 0
        assert "\x1b[" not in message
        assert "error" in message.lower() or "unable" in message.lower()

    @patch("cli_agent_orchestrator.providers.q_cli.tmux_client")
    def test_handoff_with_permission_prompt(self, mock_tmux):
        """Test WAITING_USER_ANSWER status during handoff requiring permission."""
        mock_tmux.get_history.return_value = load_fixture("q_cli_handoff_with_permission.txt")

        provider = QCliProvider("test1234", "test-session", "window-0", "supervisor")
        status = provider.get_status()

        assert status == TerminalStatus.WAITING_USER_ANSWER

    @patch("cli_agent_orchestrator.providers.q_cli.tmux_client")
    def test_handoff_message_preserves_content(self, mock_tmux):
        """Test that handoff message extraction preserves all content without truncation."""
        output = load_fixture("q_cli_handoff_successful.txt")

        provider = QCliProvider("test1234", "test-session", "window-0", "supervisor")
        message = provider.extract_last_message_from_script(output)

        # Verify the last message is complete (method extracts LAST response only)
        assert "developer agent" in message.lower()
        assert "handoff completed successfully" in message.lower()
        assert "will handle the implementation" in message.lower()
        # Verify it's not truncated or corrupted
        assert len(message.split()) >= 8  # Should have multiple words

    @patch("cli_agent_orchestrator.providers.q_cli.tmux_client")
    def test_handoff_indices_not_corrupted(self, mock_tmux):
        """Test that ANSI code cleaning doesn't corrupt index-based extraction."""
        output = load_fixture("q_cli_handoff_successful.txt")

        provider = QCliProvider("test1234", "test-session", "window-0", "supervisor")

        # This test validates the core concern: indices work correctly
        # even with ANSI codes present in the original string
        message = provider.extract_last_message_from_script(output)

        # Message should be complete and well-formed
        assert len(message) > 0
        assert "\x1b[" not in message  # All ANSI codes removed
        assert not message.startswith("[")  # No partial ANSI codes
        assert not message.endswith("\x1b")  # No trailing escape chars


class TestQCliProviderEdgeCases:
    """Test edge cases and error handling."""

    def test_exit_cli_command(self):
        """Test exit CLI command."""
        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        exit_cmd = provider.exit_cli()

        assert exit_cmd == "/exit"

    def test_get_idle_pattern_for_log(self):
        """Test idle pattern for log files."""
        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        pattern = provider.get_idle_pattern_for_log()

        from cli_agent_orchestrator.providers.q_cli import IDLE_PROMPT_PATTERN_LOG

        assert pattern == IDLE_PROMPT_PATTERN_LOG

    def test_cleanup(self):
        """Test cleanup method."""
        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        provider._initialized = True

        provider.cleanup()

        assert provider._initialized is False

    @patch("cli_agent_orchestrator.providers.q_cli.tmux_client")
    def test_long_agent_profile_name(self, mock_tmux):
        """Test with very long agent profile name."""
        long_profile = "very_long_agent_profile_name_that_exceeds_normal_length"
        mock_tmux.get_history.return_value = f"\x1b[36m[{long_profile}]\x1b[35m>\x1b[39m "

        provider = QCliProvider("test1234", "test-session", "window-0", long_profile)
        status = provider.get_status()

        assert status == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.q_cli.tmux_client")
    def test_output_with_unicode_characters(self, mock_tmux):
        """Test handling of unicode characters in output."""
        mock_tmux.get_history.return_value = (
            "\x1b[38;5;10m> \x1b[39mResponse with unicode: 日本語 café naïve 🚀\n"
            "\x1b[36m[developer]\x1b[35m>\x1b[39m"
        )

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        status = provider.get_status()

        assert status == TerminalStatus.COMPLETED

        # Test message extraction
        message = provider.extract_last_message_from_script(mock_tmux.get_history.return_value)
        assert "日本語" in message
        assert "café" in message
        assert "🚀" in message

    @patch("cli_agent_orchestrator.providers.q_cli.tmux_client")
    def test_output_with_control_characters(self, mock_tmux):
        """Test handling of control characters."""
        mock_tmux.get_history.return_value = (
            "\x1b[38;5;10m> \x1b[39mResponse\x07with\x1bcontrol\x00chars\n"
            "\x1b[36m[developer]\x1b[35m>\x1b[39m"
        )

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        message = provider.extract_last_message_from_script(mock_tmux.get_history.return_value)

        # Control characters should be cleaned
        assert "\x07" not in message  # Bell
        assert "\x00" not in message  # Null

    @patch("cli_agent_orchestrator.providers.q_cli.tmux_client")
    def test_multiple_error_indicators(self, mock_tmux):
        """Test detection with multiple error indicators."""
        mock_tmux.get_history.return_value = (
            "Amazon Q is having trouble responding right now\n"
            "Amazon Q is having trouble responding right now\n"
            "\x1b[36m[developer]\x1b[35m>\x1b[39m"
        )

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        status = provider.get_status()

        assert status == TerminalStatus.ERROR

    def test_terminal_attributes(self):
        """Test terminal provider attributes."""
        provider = QCliProvider("test1234", "test-session", "window-0", "developer")

        assert provider.terminal_id == "test1234"
        assert provider.session_name == "test-session"
        assert provider.window_name == "window-0"
        assert provider._agent_profile == "developer"

    @patch("cli_agent_orchestrator.providers.q_cli.tmux_client")
    def test_whitespace_variations_in_prompt(self, mock_tmux):
        """Test various whitespace scenarios in prompts."""
        test_cases = [
            "\x1b[36m[developer]\x1b[35m>\x1b[39m",
            "\x1b[36m[developer]\x1b[35m>\x1b[39m ",
            "\x1b[36m[developer]\x1b[35m>\x1b[39m\n",
            "\x1b[36m[developer]\x1b[35m>\x1b[39m  \n",
        ]

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")

        for test_output in test_cases:
            mock_tmux.get_history.return_value = test_output
            status = provider.get_status()
            assert status == TerminalStatus.IDLE

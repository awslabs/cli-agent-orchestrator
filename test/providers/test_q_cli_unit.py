"""Unit tests for Q CLI provider."""

import re
from pathlib import Path
from unittest.mock import patch

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

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.q_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.q_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.q_cli.tmux_client")
    async def test_initialize_success(self, mock_tmux, mock_wait_status, mock_wait_shell):
        """Test successful initialization."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        result = await provider.initialize()

        assert result is True
        mock_wait_shell.assert_called_once()
        mock_tmux.send_keys.assert_called_once_with(
            "test-session", "window-0", "q chat --agent developer"
        )
        mock_wait_status.assert_called_once()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.q_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.q_cli.tmux_client")
    async def test_initialize_shell_timeout(self, mock_tmux, mock_wait_shell):
        """Test initialization with shell timeout."""
        mock_wait_shell.return_value = False

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")

        with pytest.raises(TimeoutError, match="Shell initialization timed out"):
            await provider.initialize()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.providers.q_cli.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.q_cli.wait_until_status")
    @patch("cli_agent_orchestrator.providers.q_cli.tmux_client")
    async def test_initialize_q_cli_timeout(self, mock_tmux, mock_wait_status, mock_wait_shell):
        """Test initialization with Q CLI timeout."""
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = False

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")

        with pytest.raises(TimeoutError, match="Q CLI initialization timed out"):
            await provider.initialize()

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

    def test_get_status_idle(self):
        """Test IDLE status detection."""
        output = load_fixture("q_cli_idle_output.txt")

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        status = provider.get_status(output)

        assert status == TerminalStatus.IDLE

    def test_get_status_completed(self):
        """Test COMPLETED status detection."""
        output = load_fixture("q_cli_completed_output.txt")

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED

    def test_get_status_processing(self):
        """Test PROCESSING status detection."""
        output = load_fixture("q_cli_processing_output.txt")

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        status = provider.get_status(output)

        assert status == TerminalStatus.PROCESSING

    def test_get_status_waiting_user_answer(self):
        """Test WAITING_USER_ANSWER status detection."""
        output = load_fixture("q_cli_permission_output.txt")

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        status = provider.get_status(output)

        assert status == TerminalStatus.WAITING_USER_ANSWER

    def test_get_status_error(self):
        """Test ERROR status detection."""
        output = load_fixture("q_cli_error_output.txt")

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        status = provider.get_status(output)

        assert status == TerminalStatus.ERROR

    def test_get_status_with_empty_output(self):
        """Test status detection with empty output."""
        output = ""

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        status = provider.get_status(output)

        assert status == TerminalStatus.UNKNOWN

    def test_status_processing_response_started_no_final_prompt(self):
        """Test status returns PROCESSING when response started but no final prompt."""
        # Response started (green arrow) but no idle prompt after it
        output = (
            "\x1b[36m[developer]\x1b[35m>\x1b[39m user question\n"
            "\x1b[38;5;10m> \x1b[39mPartial response being generated..."
        )

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        status = provider.get_status(output)

        assert status == TerminalStatus.PROCESSING

    def test_status_completed_prompt_after_response(self):
        """Test status returns COMPLETED when prompt appears after response."""
        # Complete response with idle prompt after green arrow
        output = (
            "\x1b[36m[developer]\x1b[35m>\x1b[39m user question\n"
            "\x1b[38;5;10m> \x1b[39mComplete response here\n"
            "\x1b[36m[developer]\x1b[35m>\x1b[39m"
        )

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED

    def test_extraction_succeeds_when_status_completed(self):
        """Test extraction succeeds when status is COMPLETED."""
        output = (
            "\x1b[36m[developer]\x1b[35m>\x1b[39m user question\n"
            "\x1b[38;5;10m> \x1b[39mComplete response here\n"
            "\x1b[36m[developer]\x1b[35m>\x1b[39m"
        )

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")

        # Verify status is COMPLETED
        status = provider.get_status(output)
        assert status == TerminalStatus.COMPLETED

        # Verify extraction succeeds
        message = provider.extract_last_message_from_script(output)
        assert "Complete response here" in message

    def test_multiple_prompts_in_buffer_edge_case(self):
        """Test with multiple prompts in buffer (edge case)."""
        # Multiple interactions in buffer - should use last response
        output = (
            "\x1b[36m[developer]\x1b[35m>\x1b[39m first question\n"
            "\x1b[38;5;10m> \x1b[39mFirst response\n"
            "\x1b[36m[developer]\x1b[35m>\x1b[39m second question\n"
            "\x1b[38;5;10m> \x1b[39mSecond response\n"
            "\x1b[36m[developer]\x1b[35m>\x1b[39m"
        )

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED

        # Verify extraction gets the last response
        message = provider.extract_last_message_from_script(output)
        assert "Second response" in message
        assert "First response" not in message

    def test_status_processing_multiple_green_arrows_no_final_prompt(self):
        """Test PROCESSING status with multiple green arrows but no final prompt."""
        # Multiple responses but still processing (no final prompt after last arrow)
        output = (
            "\x1b[36m[developer]\x1b[35m>\x1b[39m question\n"
            "\x1b[38;5;10m> \x1b[39mFirst part of response\n"
            "\x1b[38;5;10m> \x1b[39mSecond part still generating..."
        )

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        status = provider.get_status(output)

        assert status == TerminalStatus.PROCESSING

    def test_status_idle_only_prompt_no_response(self):
        """Test IDLE status when only prompt present, no response."""
        # Just the idle prompt, no green arrow response
        output = "\x1b[36m[developer]\x1b[35m>\x1b[39m"

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        status = provider.get_status(output)

        assert status == TerminalStatus.IDLE

    def test_status_synchronization_guarantee(self):
        """Test that COMPLETED status guarantees extraction will succeed."""
        test_cases = [
            # Case 1: Simple complete response
            (
                "\x1b[36m[developer]\x1b[35m>\x1b[39m test\n"
                "\x1b[38;5;10m> \x1b[39mResponse\n"
                "\x1b[36m[developer]\x1b[35m>\x1b[39m",
                "Response",
            ),
            # Case 2: Multi-line response (newlines get stripped during cleaning)
            (
                "\x1b[36m[developer]\x1b[35m>\x1b[39m test\n"
                "\x1b[38;5;10m> \x1b[39mLine 1\nLine 2\nLine 3\n"
                "\x1b[36m[developer]\x1b[35m>\x1b[39m",
                "Line 1",  # Check for first line since newlines are processed
            ),
            # Case 3: Response with trailing text in prompt
            (
                "\x1b[36m[developer]\x1b[35m>\x1b[39m test\n"
                "\x1b[38;5;10m> \x1b[39mResponse content\n"
                "\x1b[36m[developer]\x1b[35m>\x1b[39m How can I help?",
                "Response content",
            ),
        ]

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")

        for output, expected_content in test_cases:
            # Status must be COMPLETED
            status = provider.get_status(output)
            assert status == TerminalStatus.COMPLETED, f"Status not COMPLETED for: {output}"

            # Extraction must succeed
            message = provider.extract_last_message_from_script(output)
            assert expected_content in message, f"Expected content not found in: {message}"


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

    def test_basic_prompt(self):
        """Test basic prompt without extras."""
        output = "\x1b[36m[developer]\x1b[35m>\x1b[39m "

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        status = provider.get_status(output)

        assert status == TerminalStatus.IDLE

    def test_prompt_with_percentage(self):
        """Test prompt with usage percentage."""
        output = "\x1b[36m[developer] \x1b[32m75%\x1b[35m>\x1b[39m "

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        status = provider.get_status(output)

        assert status == TerminalStatus.IDLE

    def test_prompt_with_special_profile_characters(self):
        """Test prompt with special characters in profile name."""
        output = "\x1b[36m[code-reviewer_v2]\x1b[35m>\x1b[39m "

        provider = QCliProvider("test1234", "test-session", "window-0", "code-reviewer_v2")
        status = provider.get_status(output)

        assert status == TerminalStatus.IDLE


class TestQCliProviderHandoffScenarios:
    """Test handoff scenarios between agents."""

    def test_handoff_successful_status(self):
        """Test COMPLETED status detection with successful handoff."""
        output = load_fixture("q_cli_handoff_successful.txt")

        provider = QCliProvider("test1234", "test-session", "window-0", "supervisor")
        status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED

    def test_handoff_successful_message_extraction(self):
        """Test message extraction from successful handoff output."""
        output = load_fixture("q_cli_handoff_successful.txt")

        provider = QCliProvider("test1234", "test-session", "window-0", "supervisor")
        message = provider.extract_last_message_from_script(output)

        # Verify message extraction works (extracts LAST response only)
        assert len(message) > 0
        assert "\x1b[" not in message  # ANSI codes cleaned
        assert "handoff" in message.lower()
        assert "completed successfully" in message.lower()
        assert "developer agent" in message.lower()

    def test_handoff_error_status(self):
        """Test ERROR status detection with failed handoff."""
        output = load_fixture("q_cli_handoff_error.txt")

        provider = QCliProvider("test1234", "test-session", "window-0", "supervisor")
        status = provider.get_status(output)

        assert status == TerminalStatus.ERROR

    def test_handoff_error_message_extraction(self):
        """Test message extraction from failed handoff output."""
        output = load_fixture("q_cli_handoff_error.txt")

        provider = QCliProvider("test1234", "test-session", "window-0", "supervisor")

        # Even with error, should be able to extract the message
        message = provider.extract_last_message_from_script(output)

        assert len(message) > 0
        assert "\x1b[" not in message
        assert "error" in message.lower() or "unable" in message.lower()

    def test_handoff_with_permission_prompt(self):
        """Test WAITING_USER_ANSWER status during handoff requiring permission."""
        output = load_fixture("q_cli_handoff_with_permission.txt")

        provider = QCliProvider("test1234", "test-session", "window-0", "supervisor")
        status = provider.get_status(output)

        assert status == TerminalStatus.WAITING_USER_ANSWER

    def test_handoff_message_preserves_content(self):
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

    def test_handoff_indices_not_corrupted(self):
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

    def test_cleanup(self):
        """Test cleanup method."""
        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        provider._initialized = True

        provider.cleanup()

        assert provider._initialized is False

    def test_long_agent_profile_name(self):
        """Test with very long agent profile name."""
        long_profile = "very_long_agent_profile_name_that_exceeds_normal_length"
        output = f"\x1b[36m[{long_profile}]\x1b[35m>\x1b[39m "

        provider = QCliProvider("test1234", "test-session", "window-0", long_profile)
        status = provider.get_status(output)

        assert status == TerminalStatus.IDLE

    def test_output_with_unicode_characters(self):
        """Test handling of unicode characters in output."""
        output = (
            "\x1b[38;5;10m> \x1b[39mResponse with unicode: 日本語 café naïve 🚀\n"
            "\x1b[36m[developer]\x1b[35m>\x1b[39m"
        )

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        status = provider.get_status(output)

        assert status == TerminalStatus.COMPLETED

        # Test message extraction
        message = provider.extract_last_message_from_script(output)
        assert "日本語" in message
        assert "café" in message
        assert "🚀" in message

    def test_output_with_control_characters(self):
        """Test handling of control characters."""
        output = (
            "\x1b[38;5;10m> \x1b[39mResponse\x07with\x1bcontrol\x00chars\n"
            "\x1b[36m[developer]\x1b[35m>\x1b[39m"
        )

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        message = provider.extract_last_message_from_script(output)

        # Control characters should be cleaned
        assert "\x07" not in message  # Bell
        assert "\x00" not in message  # Null

    def test_multiple_error_indicators(self):
        """Test detection with multiple error indicators."""
        output = (
            "Amazon Q is having trouble responding right now\n"
            "Amazon Q is having trouble responding right now\n"
            "\x1b[36m[developer]\x1b[35m>\x1b[39m"
        )

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")
        status = provider.get_status(output)

        assert status == TerminalStatus.ERROR

    def test_terminal_attributes(self):
        """Test terminal provider attributes."""
        provider = QCliProvider("test1234", "test-session", "window-0", "developer")

        assert provider.terminal_id == "test1234"
        assert provider.session_name == "test-session"
        assert provider.window_name == "window-0"
        assert provider._agent_profile == "developer"

    def test_whitespace_variations_in_prompt(self):
        """Test various whitespace scenarios in prompts."""
        test_cases = [
            "\x1b[36m[developer]\x1b[35m>\x1b[39m",
            "\x1b[36m[developer]\x1b[35m>\x1b[39m ",
            "\x1b[36m[developer]\x1b[35m>\x1b[39m\n",
            "\x1b[36m[developer]\x1b[35m>\x1b[39m  \n",
        ]

        provider = QCliProvider("test1234", "test-session", "window-0", "developer")

        for test_output in test_cases:
            status = provider.get_status(test_output)
            assert status == TerminalStatus.IDLE

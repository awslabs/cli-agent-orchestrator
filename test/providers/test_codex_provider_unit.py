"""Unit tests for Codex provider."""

from pathlib import Path
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.codex import CodexProvider

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(filename: str) -> str:
    with open(FIXTURES_DIR / filename, "r") as f:
        return f.read()


class TestCodexProviderInitialization:
    @patch("cli_agent_orchestrator.providers.codex.wait_until_status")
    @patch("cli_agent_orchestrator.providers.codex.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.codex.tmux_client")
    def test_initialize_success(self, mock_tmux, mock_wait_shell, mock_wait_status):
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = True

        provider = CodexProvider("test1234", "test-session", "window-0", None)
        result = provider.initialize()

        assert result is True
        mock_wait_shell.assert_called_once()
        mock_tmux.send_keys.assert_called_once_with("test-session", "window-0", "codex")
        mock_wait_status.assert_called_once()

    @patch("cli_agent_orchestrator.providers.codex.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.codex.tmux_client")
    def test_initialize_shell_timeout(self, mock_tmux, mock_wait_shell):
        mock_wait_shell.return_value = False

        provider = CodexProvider("test1234", "test-session", "window-0", None)

        with pytest.raises(TimeoutError, match="Shell initialization timed out"):
            provider.initialize()

    @patch("cli_agent_orchestrator.providers.codex.wait_until_status")
    @patch("cli_agent_orchestrator.providers.codex.wait_for_shell")
    @patch("cli_agent_orchestrator.providers.codex.tmux_client")
    def test_initialize_codex_timeout(self, mock_tmux, mock_wait_shell, mock_wait_status):
        mock_wait_shell.return_value = True
        mock_wait_status.return_value = False

        provider = CodexProvider("test1234", "test-session", "window-0", None)

        with pytest.raises(TimeoutError, match="Codex initialization timed out"):
            provider.initialize()


class TestCodexProviderStatusDetection:
    @patch("cli_agent_orchestrator.providers.codex.tmux_client")
    def test_get_status_idle(self, mock_tmux):
        mock_tmux.get_history.return_value = load_fixture("codex_idle_output.txt")

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status()

        assert status == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.codex.tmux_client")
    def test_get_status_completed(self, mock_tmux):
        mock_tmux.get_history.return_value = load_fixture("codex_completed_output.txt")

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status()

        assert status == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.codex.tmux_client")
    def test_get_status_processing(self, mock_tmux):
        mock_tmux.get_history.return_value = load_fixture("codex_processing_output.txt")

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status()

        assert status == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.providers.codex.tmux_client")
    def test_get_status_waiting_user_answer(self, mock_tmux):
        mock_tmux.get_history.return_value = load_fixture("codex_permission_output.txt")

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status()

        assert status == TerminalStatus.WAITING_USER_ANSWER

    @patch("cli_agent_orchestrator.providers.codex.tmux_client")
    def test_get_status_error(self, mock_tmux):
        mock_tmux.get_history.return_value = load_fixture("codex_error_output.txt")

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status()

        assert status == TerminalStatus.ERROR

    @patch("cli_agent_orchestrator.providers.codex.tmux_client")
    def test_get_status_empty_output(self, mock_tmux):
        mock_tmux.get_history.return_value = ""

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status()

        assert status == TerminalStatus.ERROR

    @patch("cli_agent_orchestrator.providers.codex.tmux_client")
    def test_get_status_with_tail_lines(self, mock_tmux):
        mock_tmux.get_history.return_value = load_fixture("codex_idle_output.txt")

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status(tail_lines=50)

        assert status == TerminalStatus.IDLE
        mock_tmux.get_history.assert_called_once_with("test-session", "window-0", tail_lines=50)

    @patch("cli_agent_orchestrator.providers.codex.tmux_client")
    def test_get_status_processing_when_old_prompt_present(self, mock_tmux):
        # If the captured history contains an earlier prompt but the *latest* output is processing,
        # we should report PROCESSING.
        mock_tmux.get_history.return_value = (
            "Welcome to Codex\n" "❯ \n" "You Fix the failing tests\n" "Codex is thinking…\n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status()

        assert status == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.providers.codex.tmux_client")
    def test_get_status_not_error_on_failed_in_message(self, mock_tmux):
        # "failed" is commonly used in normal assistant output; it should not automatically
        # force ERROR.
        mock_tmux.get_history.return_value = (
            "You Explain why the test failed\n"
            "assistant: The test failed because the assertion is incorrect.\n"
            "\n"
            "❯ \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status()

        assert status == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.codex.tmux_client")
    def test_get_status_idle_if_no_assistant_after_last_user(self, mock_tmux):
        # If there is a user message but no assistant response after it, we should not
        # treat the session as COMPLETED.
        mock_tmux.get_history.return_value = "assistant: Welcome\n" "You Do the thing\n" "\n" "❯ \n"

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status()

        assert status == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.codex.tmux_client")
    def test_get_status_processing_when_no_prompt_and_no_keywords(self, mock_tmux):
        # Codex output may not always include explicit "thinking/processing" keywords.
        # Without an idle prompt at the end, we should assume it's still processing.
        mock_tmux.get_history.return_value = "You Run the command\nWorking...\n"

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status()

        assert status == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.providers.codex.tmux_client")
    def test_get_status_not_error_when_assistant_mentions_error_text(self, mock_tmux):
        mock_tmux.get_history.return_value = (
            "You Explain the failure\n"
            "assistant: Here's an example error:\n"
            "Error: example only\n"
            "\n"
            "❯ \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status()

        assert status == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.codex.tmux_client")
    def test_get_status_not_waiting_when_assistant_mentions_approval_text(self, mock_tmux):
        mock_tmux.get_history.return_value = (
            "You Explain approvals\n"
            "assistant: You might see this prompt:\n"
            "Approve this command? [y/n]\n"
            "\n"
            "❯ \n"
        )

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status()

        assert status == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.codex.tmux_client")
    def test_get_status_error_when_error_after_user_and_prompt(self, mock_tmux):
        mock_tmux.get_history.return_value = "You Run thing\nError: failed\n\n❯ \n"

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status()

        assert status == TerminalStatus.ERROR

    @patch("cli_agent_orchestrator.providers.codex.tmux_client")
    def test_get_status_waiting_user_answer_when_no_user_prefix(self, mock_tmux):
        mock_tmux.get_history.return_value = "Approve this command? [y/n]\n"

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status()

        assert status == TerminalStatus.WAITING_USER_ANSWER

    @patch("cli_agent_orchestrator.providers.codex.tmux_client")
    def test_get_status_error_when_no_user_prefix(self, mock_tmux):
        mock_tmux.get_history.return_value = "Error: something failed\n"

        provider = CodexProvider("test1234", "test-session", "window-0")
        status = provider.get_status()

        assert status == TerminalStatus.ERROR


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


class TestCodexProviderMisc:
    def test_get_idle_pattern_for_log(self):
        provider = CodexProvider("test1234", "test-session", "window-0")
        assert provider.get_idle_pattern_for_log() == "❯"

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

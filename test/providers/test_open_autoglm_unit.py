"""Unit tests for OpenAutoGLM provider."""

import re
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.open_autoglm import OpenAutoGLMProvider

# Test fixtures directory
FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(filename: str) -> str:
    """Load a fixture file and return its contents."""
    with open(FIXTURES_DIR / filename, "r") as f:
        return f.read()


class TestOpenAutoGLMProviderInitialization:
    """Test OpenAutoGLM provider initialization."""

    @patch("cli_agent_orchestrator.providers.open_autoglm.wait_until_status")
    @patch("cli_agent_orchestrator.providers.open_autoglm.tmux_client")
    def test_initialize_success(self, mock_tmux, mock_wait_status):
        """Test successful initialization."""
        mock_wait_status.return_value = True

        provider = OpenAutoGLMProvider("test1234", "test-session", "window-0")
        result = provider.initialize()

        assert result is True
        mock_tmux.send_keys.assert_called_once()
        mock_wait_status.assert_called_once()

    @patch("cli_agent_orchestrator.providers.open_autoglm.wait_until_status")
    @patch("cli_agent_orchestrator.providers.open_autoglm.tmux_client")
    def test_initialize_with_agent_profile(self, mock_tmux, mock_wait_status):
        """Test initialization with agent profile."""
        mock_wait_status.return_value = True

        provider = OpenAutoGLMProvider("test1234", "test-session", "window-0", "mobile_agent")

        # Verify agent profile is set before initialization
        assert provider._agent_profile == "mobile_agent"

        result = provider.initialize()

        assert result is True

    @patch("cli_agent_orchestrator.providers.open_autoglm.wait_until_status")
    @patch("cli_agent_orchestrator.providers.open_autoglm.tmux_client")
    def test_initialize_timeout(self, mock_tmux, mock_wait_status):
        """Test initialization with timeout."""
        mock_wait_status.return_value = False

        provider = OpenAutoGLMProvider("test1234", "test-session", "window-0")

        with pytest.raises(TimeoutError, match="OpenAutoGLM initialization timed out"):
            provider.initialize()

    @patch("cli_agent_orchestrator.providers.open_autoglm.load_agent_profile")
    def test_build_command_with_profile(self, mock_load_profile):
        """Test command building with agent profile."""
        # Mock agent profile with OpenAutoGLM config
        mock_profile = MagicMock()
        mock_profile.open_autoglm_config = {
            "api_endpoint": "http://localhost:8080/api",
            "model_name": "autoglm-v2",
            "device_id": "emulator-5554",
        }
        mock_load_profile.return_value = mock_profile

        provider = OpenAutoGLMProvider("test1234", "test-session", "window-0", "mobile_agent")
        command_parts = provider._build_autoglm_command()

        assert "python3" in command_parts
        assert "--device" in command_parts
        assert "emulator-5554" in command_parts


class TestOpenAutoGLMProviderStatusDetection:
    """Test status detection logic."""

    @patch("cli_agent_orchestrator.providers.open_autoglm.tmux_client")
    def test_get_status_idle(self, mock_tmux):
        """Test IDLE status detection."""
        mock_tmux.get_history.return_value = "OpenAutoGLM> "

        provider = OpenAutoGLMProvider("test1234", "test-session", "window-0")
        status = provider.get_status()

        assert status == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.open_autoglm.tmux_client")
    def test_get_status_processing(self, mock_tmux):
        """Test PROCESSING status detection."""
        mock_tmux.get_history.return_value = "thinking about the request..."

        provider = OpenAutoGLMProvider("test1234", "test-session", "window-0")
        status = provider.get_status()

        assert status == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.providers.open_autoglm.tmux_client")
    def test_get_status_executing(self, mock_tmux):
        """Test PROCESSING status when executing action."""
        mock_tmux.get_history.return_value = "executing action: tap_button"

        provider = OpenAutoGLMProvider("test1234", "test-session", "window-0")
        status = provider.get_status()

        assert status == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.providers.open_autoglm.tmux_client")
    def test_get_status_completed(self, mock_tmux):
        """Test COMPLETED status detection."""
        mock_tmux.get_history.return_value = "task completed successfully\nOpenAutoGLM> "

        provider = OpenAutoGLMProvider("test1234", "test-session", "window-0")
        status = provider.get_status()

        assert status == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.open_autoglm.tmux_client")
    def test_get_status_adb_error(self, mock_tmux):
        """Test ERROR status detection with ADB errors."""
        mock_tmux.get_history.return_value = "adb error: device not found"

        provider = OpenAutoGLMProvider("test1234", "test-session", "window-0")
        status = provider.get_status()

        assert status == TerminalStatus.ERROR

    @patch("cli_agent_orchestrator.providers.open_autoglm.tmux_client")
    def test_get_status_general_error(self, mock_tmux):
        """Test ERROR status detection with general errors."""
        mock_tmux.get_history.return_value = "error: failed to execute command"

        provider = OpenAutoGLMProvider("test1234", "test-session", "window-0")
        status = provider.get_status()

        assert status == TerminalStatus.ERROR

    @patch("cli_agent_orchestrator.providers.open_autoglm.tmux_client")
    def test_get_status_empty_output(self, mock_tmux):
        """Test status detection with empty output."""
        mock_tmux.get_history.return_value = ""

        provider = OpenAutoGLMProvider("test1234", "test-session", "window-0")
        status = provider.get_status()

        assert status == TerminalStatus.ERROR

    @patch("cli_agent_orchestrator.providers.open_autoglm.tmux_client")
    def test_get_status_with_tail_lines(self, mock_tmux):
        """Test status detection with tail_lines parameter."""
        mock_tmux.get_history.return_value = "OpenAutoGLM> "

        provider = OpenAutoGLMProvider("test1234", "test-session", "window-0")
        status = provider.get_status(tail_lines=50)

        assert status == TerminalStatus.IDLE
        mock_tmux.get_history.assert_called_once_with("test-session", "window-0", tail_lines=50)


class TestOpenAutoGLMProviderMessageExtraction:
    """Test message extraction from terminal output."""

    def test_extract_last_message_with_completion(self):
        """Test successful message extraction with completion pattern."""
        output = (
            "executing action: scroll\n"
            "task completed\n"
            "Result: Successfully scrolled the screen\n"
            "OpenAutoGLM> "
        )

        provider = OpenAutoGLMProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "Successfully scrolled the screen" in message

    def test_extract_last_message_result_indicator(self):
        """Test message extraction with result indicator."""
        output = (
            "processing request...\n"
            "result: Tapped the Settings button\n"
            "Action completed successfully\n"
            "OpenAutoGLM> "
        )

        provider = OpenAutoGLMProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "Tapped the Settings button" in message

    def test_extract_last_message_fallback(self):
        """Test message extraction fallback to last meaningful output."""
        output = (
            "executing command...\n"
            "[INFO] Some debug info\n"
            "Clicked on the submit button\n"
            "[DEBUG] More debug\n"
            "OpenAutoGLM> "
        )

        provider = OpenAutoGLMProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "Clicked on the submit button" in message

    def test_extract_last_message_no_response(self):
        """Test extraction fails when no response is found."""
        output = "OpenAutoGLM> "

        provider = OpenAutoGLMProvider("test1234", "test-session", "window-0")

        with pytest.raises(ValueError, match="No OpenAutoGLM response found"):
            provider.extract_last_message_from_script(output)

    def test_extract_last_message_with_complex_output(self):
        """Test extraction from complex multi-line output."""
        output = (
            "thinking...\n"
            "executing multi-step action:\n"
            "1. Open Settings app\n"
            "2. Navigate to Display\n"
            "3. Adjust brightness\n"
            "task completed\n"
            "Successfully adjusted brightness to 75%\n"
            "OpenAutoGLM> "
        )

        provider = OpenAutoGLMProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "Successfully adjusted brightness to 75%" in message
        assert "thinking..." not in message  # Should exclude processing info


class TestOpenAutoGLMProviderRegexPatterns:
    """Test regex pattern matching."""

    def test_adb_device_pattern(self):
        """Test ADB device pattern detection."""
        # Test case insensitive matching by converting pattern to case insensitive
        import re

        from cli_agent_orchestrator.providers.open_autoglm import ADB_DEVICE_PATTERN

        pattern = re.compile(ADB_DEVICE_PATTERN, re.IGNORECASE)

        assert pattern.search("adb device found: emulator-5554")
        assert pattern.search("adb devices List of devices attached")

    def test_adb_error_pattern(self):
        """Test ADB error pattern detection."""
        from cli_agent_orchestrator.providers.open_autoglm import ADB_ERROR_PATTERN

        assert re.search(ADB_ERROR_PATTERN, "adb error: no devices found")
        assert re.search(ADB_ERROR_PATTERN, "device not found")
        assert re.search(ADB_ERROR_PATTERN, "no devices connected")

    def test_thinking_pattern(self):
        """Test thinking pattern detection."""
        from cli_agent_orchestrator.providers.open_autoglm import THINKING_PATTERN

        assert re.search(THINKING_PATTERN, "thinking about the request...")
        assert re.search(THINKING_PATTERN, "processing your command...")

    def test_executing_pattern(self):
        """Test executing pattern detection."""
        from cli_agent_orchestrator.providers.open_autoglm import EXECUTING_PATTERN

        assert re.search(EXECUTING_PATTERN, "executing action: tap_button")
        assert re.search(EXECUTING_PATTERN, "running command: scroll")

    def test_completion_pattern(self):
        """Test completion pattern detection."""
        from cli_agent_orchestrator.providers.open_autoglm import COMPLETION_PATTERN

        assert re.search(COMPLETION_PATTERN, "task completed successfully")
        assert re.search(COMPLETION_PATTERN, "action finished")
        assert re.search(COMPLETION_PATTERN, "done")

    def test_idle_prompt_pattern(self):
        """Test idle prompt pattern detection."""
        from cli_agent_orchestrator.providers.open_autoglm import IDLE_PROMPT_PATTERN

        assert re.search(IDLE_PROMPT_PATTERN, "OpenAutoGLM> ")
        assert re.search(IDLE_PROMPT_PATTERN, "[autoglm]> ")

    def test_error_pattern(self):
        """Test error pattern detection."""
        # Test case insensitive matching by converting pattern to case insensitive
        import re

        from cli_agent_orchestrator.providers.open_autoglm import ERROR_PATTERN

        pattern = re.compile(ERROR_PATTERN, re.IGNORECASE)

        assert pattern.search("error: something went wrong")
        assert pattern.search("exception: null pointer")
        assert pattern.search("failed: command timeout")
        assert pattern.search("Traceback (most recent call last):")


class TestOpenAutoGLMProviderEdgeCases:
    """Test edge cases and error handling."""

    def test_exit_cli_command(self):
        """Test exit CLI command."""
        provider = OpenAutoGLMProvider("test1234", "test-session", "window-0")
        exit_cmd = provider.exit_cli()

        assert exit_cmd == "exit"

    def test_get_idle_pattern_for_log(self):
        """Test idle pattern for log files."""
        provider = OpenAutoGLMProvider("test1234", "test-session", "window-0")
        pattern = provider.get_idle_pattern_for_log()

        from cli_agent_orchestrator.providers.open_autoglm import IDLE_PROMPT_PATTERN

        assert pattern == IDLE_PROMPT_PATTERN

    def test_cleanup(self):
        """Test cleanup method."""
        provider = OpenAutoGLMProvider("test1234", "test-session", "window-0")
        provider._initialized = True

        with patch("cli_agent_orchestrator.providers.open_autoglm.tmux_client") as mock_tmux:
            provider.cleanup()

            assert provider._initialized is False
            mock_tmux.send_keys.assert_called_once_with("test-session", "window-0", "exit")

    def test_cleanup_with_exception(self):
        """Test cleanup handles exceptions gracefully."""
        provider = OpenAutoGLMProvider("test1234", "test-session", "window-0")
        provider._initialized = True

        with patch("cli_agent_orchestrator.providers.open_autoglm.tmux_client") as mock_tmux:
            mock_tmux.send_keys.side_effect = Exception("tmux error")

            # Should not raise exception
            provider.cleanup()

            assert provider._initialized is False

    def test_set_device_id(self):
        """Test setting device ID."""
        provider = OpenAutoGLMProvider("test1234", "test-session", "window-0")
        provider.set_device_id("emulator-5554")

        assert provider._device_id == "emulator-5554"

    def test_set_api_endpoint(self):
        """Test setting API endpoint."""
        provider = OpenAutoGLMProvider("test1234", "test-session", "window-0")
        provider.set_api_endpoint("http://localhost:8080/api")

        assert provider._api_endpoint == "http://localhost:8080/api"

    @patch("cli_agent_orchestrator.providers.open_autoglm.tmux_client")
    @patch("time.sleep")
    def test_check_device_connection_success(self, mock_sleep, mock_tmux):
        """Test successful device connection check."""
        mock_tmux.get_history.return_value = "List of devices attached\nemulator-5554\tdevice"

        provider = OpenAutoGLMProvider("test1234", "test-session", "window-0")
        result = provider.check_device_connection()

        assert result is True
        mock_tmux.send_keys.assert_called_once_with("test-session", "window-0", "adb devices")
        mock_sleep.assert_called_once_with(2)

    @patch("cli_agent_orchestrator.providers.open_autoglm.tmux_client")
    @patch("time.sleep")
    def test_check_device_connection_failure(self, mock_sleep, mock_tmux):
        """Test failed device connection check."""
        mock_tmux.get_history.return_value = "List of devices attached\n"

        provider = OpenAutoGLMProvider("test1234", "test-session", "window-0")
        result = provider.check_device_connection()

        assert result is False

    @patch("cli_agent_orchestrator.providers.open_autoglm.tmux_client")
    @patch("time.sleep")
    def test_check_device_connection_exception(self, mock_sleep, mock_tmux):
        """Test device connection check with exception."""
        mock_tmux.send_keys.side_effect = Exception("tmux error")

        provider = OpenAutoGLMProvider("test1234", "test-session", "window-0")
        result = provider.check_device_connection()

        assert result is False

    def test_terminal_attributes(self):
        """Test terminal provider attributes."""
        provider = OpenAutoGLMProvider("test1234", "test-session", "window-0", "mobile_agent")

        assert provider.terminal_id == "test1234"
        assert provider.session_name == "test-session"
        assert provider.window_name == "window-0"
        assert provider._agent_profile == "mobile_agent"

    @patch("cli_agent_orchestrator.providers.open_autoglm.tmux_client")
    def test_case_insensitive_status_detection(self, mock_tmux):
        """Test case insensitive status detection."""
        test_cases = [
            "THINKING about request...",
            "PROCESSING command...",
            "Task COMPLETED successfully",
            "ERROR: adb not found",
            "OpenAutoGLM> ",
        ]

        expected_statuses = [
            TerminalStatus.PROCESSING,
            TerminalStatus.PROCESSING,
            TerminalStatus.COMPLETED,
            TerminalStatus.ERROR,
            TerminalStatus.IDLE,
        ]

        provider = OpenAutoGLMProvider("test1234", "test-session", "window-0")

        for output, expected_status in zip(test_cases, expected_statuses):
            mock_tmux.get_history.return_value = output
            status = provider.get_status()
            assert status == expected_status

    def test_extract_message_with_unicode(self):
        """Test message extraction with unicode characters."""
        output = "executing action...\n" "result: 操作成功完成 - 日本語\n" "OpenAutoGLM> "

        provider = OpenAutoGLMProvider("test1234", "test-session", "window-0")
        message = provider.extract_last_message_from_script(output)

        assert "操作成功完成" in message
        assert "日本語" in message

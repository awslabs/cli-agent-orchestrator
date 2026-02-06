"""Tests for terminal utilities."""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.utils.terminal import (
    generate_session_name,
    generate_terminal_id,
    generate_window_name,
    wait_for_shell,
    wait_until_status,
    wait_until_terminal_status,
)


class TestGenerateFunctions:
    """Tests for ID generation functions."""

    def test_generate_session_name(self):
        """Test session name generation."""
        name = generate_session_name()

        assert name.startswith("cao-")
        assert len(name) == 12  # cao- (4) + uuid (8)

    def test_generate_session_name_unique(self):
        """Test session names are unique."""
        names = [generate_session_name() for _ in range(100)]

        assert len(set(names)) == 100

    def test_generate_terminal_id(self):
        """Test terminal ID generation."""
        terminal_id = generate_terminal_id()

        assert len(terminal_id) == 8

    def test_generate_terminal_id_unique(self):
        """Test terminal IDs are unique."""
        ids = [generate_terminal_id() for _ in range(100)]

        assert len(set(ids)) == 100

    def test_generate_window_name(self):
        """Test window name generation."""
        name = generate_window_name("developer")

        assert name.startswith("developer-")
        assert len(name) == 14  # developer- (10) + uuid (4)

    def test_generate_window_name_unique(self):
        """Test window names are unique."""
        names = [generate_window_name("test") for _ in range(100)]

        assert len(set(names)) == 100


class TestWaitForShell:
    """Tests for wait_for_shell function."""

    def test_wait_for_shell_success(self):
        """Test successful shell wait."""
        mock_tmux = MagicMock()
        # Return same output twice to indicate shell is ready
        mock_tmux.get_history.side_effect = ["prompt $", "prompt $"]

        result = wait_for_shell(
            mock_tmux, "test-session", "window-0", timeout=2.0, polling_interval=0.1
        )

        assert result is True

    def test_wait_for_shell_timeout(self):
        """Test shell wait timeout."""
        mock_tmux = MagicMock()
        # Return different outputs each time
        call_count = [0]

        def get_history_side_effect(*args, **kwargs):
            call_count[0] += 1
            return f"output {call_count[0]}"

        mock_tmux.get_history.side_effect = get_history_side_effect

        result = wait_for_shell(
            mock_tmux, "test-session", "window-0", timeout=0.5, polling_interval=0.1
        )

        assert result is False

    def test_wait_for_shell_empty_output(self):
        """Test shell wait with empty output."""
        mock_tmux = MagicMock()
        mock_tmux.get_history.return_value = ""

        result = wait_for_shell(
            mock_tmux, "test-session", "window-0", timeout=0.5, polling_interval=0.1
        )

        assert result is False


class TestWaitUntilStatus:
    """Tests for wait_until_status function."""

    def test_wait_until_status_success(self):
        """Test successful status wait."""
        mock_provider = MagicMock()
        mock_provider.get_status.return_value = TerminalStatus.IDLE

        result = wait_until_status(
            mock_provider, TerminalStatus.IDLE, timeout=1.0, polling_interval=0.1
        )

        assert result is True

    def test_wait_until_status_timeout(self):
        """Test status wait timeout."""
        mock_provider = MagicMock()
        mock_provider.get_status.return_value = TerminalStatus.PROCESSING

        result = wait_until_status(
            mock_provider, TerminalStatus.IDLE, timeout=0.5, polling_interval=0.1
        )

        assert result is False

    def test_wait_until_status_eventually_succeeds(self):
        """Test status wait that eventually succeeds."""
        mock_provider = MagicMock()
        # First few calls return PROCESSING, then IDLE
        mock_provider.get_status.side_effect = [
            TerminalStatus.PROCESSING,
            TerminalStatus.PROCESSING,
            TerminalStatus.IDLE,
        ]

        result = wait_until_status(
            mock_provider, TerminalStatus.IDLE, timeout=2.0, polling_interval=0.1
        )

        assert result is True


class TestWaitUntilTerminalStatus:
    """Tests for wait_until_terminal_status function."""

    @patch("cli_agent_orchestrator.utils.terminal.httpx.get")
    def test_wait_until_terminal_status_success(self, mock_get):
        """Test successful terminal status wait."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": TerminalStatus.IDLE.value}
        mock_get.return_value = mock_response

        result = wait_until_terminal_status(
            "test-terminal", TerminalStatus.IDLE, timeout=1.0, polling_interval=0.1
        )

        assert result is True

    @patch("cli_agent_orchestrator.utils.terminal.httpx.get")
    def test_wait_until_terminal_status_timeout(self, mock_get):
        """Test terminal status wait timeout."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "PROCESSING"}
        mock_get.return_value = mock_response

        result = wait_until_terminal_status(
            "test-terminal", TerminalStatus.IDLE, timeout=0.5, polling_interval=0.1
        )

        assert result is False

    @patch("cli_agent_orchestrator.utils.terminal.httpx.get")
    def test_wait_until_terminal_status_api_error(self, mock_get):
        """Test terminal status wait with API error."""
        mock_get.side_effect = Exception("Connection error")

        result = wait_until_terminal_status(
            "test-terminal", TerminalStatus.IDLE, timeout=0.5, polling_interval=0.1
        )

        assert result is False

    @patch("cli_agent_orchestrator.utils.terminal.httpx.get")
    def test_wait_until_terminal_status_non_200(self, mock_get):
        """Test terminal status wait with non-200 response."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_get.return_value = mock_response

        result = wait_until_terminal_status(
            "test-terminal", TerminalStatus.IDLE, timeout=0.5, polling_interval=0.1
        )

        assert result is False

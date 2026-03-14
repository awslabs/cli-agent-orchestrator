"""Tests for terminal utilities."""

import asyncio
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
        """Test window names are mostly unique (4 hex chars = 65536 values, collisions possible)."""
        names = [generate_window_name("test") for _ in range(10)]

        assert len(set(names)) == 10


class TestWaitForShell:
    """Tests for wait_for_shell function."""

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor")
    async def test_wait_for_shell_success(self, mock_monitor):
        """Test successful shell wait - buffer is non-empty and stable."""
        mock_monitor.get_buffer.return_value = "prompt $"

        result = await wait_for_shell(
            "test-terminal", timeout=2.0, stable_duration=0.3, polling_interval=0.1
        )

        assert result is True

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor")
    async def test_wait_for_shell_timeout(self, mock_monitor):
        """Test shell wait timeout - buffer keeps changing."""
        call_count = [0]

        def get_buffer_side_effect(terminal_id):
            call_count[0] += 1
            return f"output {call_count[0]}"

        mock_monitor.get_buffer.side_effect = get_buffer_side_effect

        result = await wait_for_shell(
            "test-terminal", timeout=0.5, stable_duration=0.3, polling_interval=0.1
        )

        assert result is False

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor")
    async def test_wait_for_shell_empty_output(self, mock_monitor):
        """Test shell wait with empty output."""
        mock_monitor.get_buffer.return_value = ""

        result = await wait_for_shell(
            "test-terminal", timeout=0.5, stable_duration=0.3, polling_interval=0.1
        )

        assert result is False


class TestWaitUntilStatus:
    """Tests for wait_until_status function."""

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor")
    async def test_wait_until_status_success(self, mock_monitor):
        """Test successful status wait."""
        mock_monitor.get_status.return_value = TerminalStatus.IDLE

        result = await wait_until_status(
            "test-terminal", TerminalStatus.IDLE, timeout=1.0, polling_interval=0.1
        )

        assert result is True

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor")
    async def test_wait_until_status_timeout(self, mock_monitor):
        """Test status wait timeout."""
        mock_monitor.get_status.return_value = TerminalStatus.PROCESSING

        result = await wait_until_status(
            "test-terminal", TerminalStatus.IDLE, timeout=0.5, polling_interval=0.1
        )

        assert result is False

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor")
    async def test_wait_until_status_with_set(self, mock_monitor):
        """Test status wait accepts a set of target statuses."""
        mock_monitor.get_status.return_value = TerminalStatus.COMPLETED

        result = await wait_until_status(
            "test-terminal",
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=1.0,
            polling_interval=0.1,
        )

        assert result is True

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor")
    async def test_wait_until_status_eventually_succeeds(self, mock_monitor):
        """Test status wait that eventually succeeds."""
        mock_monitor.get_status.side_effect = [
            TerminalStatus.PROCESSING,
            TerminalStatus.PROCESSING,
            TerminalStatus.IDLE,
        ]

        result = await wait_until_status(
            "test-terminal", TerminalStatus.IDLE, timeout=2.0, polling_interval=0.1
        )

        assert result is True


class TestWaitUntilTerminalStatus:
    """Tests for wait_until_terminal_status function."""

    @patch("cli_agent_orchestrator.utils.terminal.requests.get")
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

    @patch("cli_agent_orchestrator.utils.terminal.requests.get")
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

    @patch("cli_agent_orchestrator.utils.terminal.requests.get")
    def test_wait_until_terminal_status_api_error(self, mock_get):
        """Test terminal status wait with API error."""
        mock_get.side_effect = Exception("Connection error")

        result = wait_until_terminal_status(
            "test-terminal", TerminalStatus.IDLE, timeout=0.5, polling_interval=0.1
        )

        assert result is False

    @patch("cli_agent_orchestrator.utils.terminal.requests.get")
    def test_wait_until_terminal_status_non_200(self, mock_get):
        """Test terminal status wait with non-200 response."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_get.return_value = mock_response

        result = wait_until_terminal_status(
            "test-terminal", TerminalStatus.IDLE, timeout=0.5, polling_interval=0.1
        )

        assert result is False

    @patch("cli_agent_orchestrator.utils.terminal.requests.get")
    def test_wait_until_terminal_status_multi_status_set(self, mock_get):
        """Test waiting for multiple target statuses (set)."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": TerminalStatus.COMPLETED.value}
        mock_get.return_value = mock_response

        result = wait_until_terminal_status(
            "test-terminal",
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=1.0,
            polling_interval=0.1,
        )

        assert result is True

    @patch("cli_agent_orchestrator.utils.terminal.requests.get")
    def test_wait_until_terminal_status_multi_status_no_match(self, mock_get):
        """Test multi-status wait times out when status doesn't match any target."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": TerminalStatus.PROCESSING.value}
        mock_get.return_value = mock_response

        result = wait_until_terminal_status(
            "test-terminal",
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=0.5,
            polling_interval=0.1,
        )

        assert result is False

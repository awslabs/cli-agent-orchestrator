"""Unit tests for inbox service."""

from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import inbox_service


@pytest.fixture
def mock_db():
    """Mock database client."""
    with patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages") as get_pending, \
         patch("cli_agent_orchestrator.services.inbox_service.update_message_status") as update_status:
        yield {
            "get_pending": get_pending,
            "update_status": update_status
        }


@pytest.fixture
def mock_terminal_service():
    """Mock terminal service."""
    with patch("cli_agent_orchestrator.services.inbox_service.terminal_service") as mock:
        yield mock


@pytest.fixture
def mock_provider_manager():
    """Mock provider manager."""
    with patch("cli_agent_orchestrator.services.inbox_service.provider_manager") as mock:
        yield mock


class TestGetLogTail:
    """Test _get_log_tail function."""

    @patch("cli_agent_orchestrator.services.inbox_service.subprocess.run")
    def test_get_log_tail_success(self, mock_subprocess):
        """Test successful log tail retrieval."""
        mock_subprocess.return_value.stdout = "line1\nline2\nline3\n"
        
        result = inbox_service._get_log_tail("terminal-123", 2)
        
        assert result == "line1\nline2\nline3\n"
        mock_subprocess.assert_called_once()

    @patch("cli_agent_orchestrator.services.inbox_service.subprocess.run")
    def test_get_log_tail_file_not_found(self, mock_subprocess):
        """Test log tail when subprocess fails."""
        mock_subprocess.side_effect = Exception("File not found")
        
        result = inbox_service._get_log_tail("terminal-123", 2)
        
        assert result == ""


class TestHasIdlePattern:
    """Test _has_idle_pattern function."""

    @patch("cli_agent_orchestrator.services.inbox_service._get_log_tail")
    def test_has_idle_pattern_true(self, mock_get_tail, mock_provider_manager):
        """Test idle pattern detection returns true."""
        mock_get_tail.return_value = "some output\nWaiting for input...\nmore output"
        mock_provider = MagicMock()
        mock_provider.get_idle_pattern_for_log.return_value = r"Waiting for input"
        mock_provider_manager.get_provider.return_value = mock_provider
        
        result = inbox_service._has_idle_pattern("terminal-123")
        
        assert result is True

    @patch("cli_agent_orchestrator.services.inbox_service._get_log_tail")
    def test_has_idle_pattern_false_processing(self, mock_get_tail, mock_provider_manager):
        """Test idle pattern detection returns false when processing."""
        mock_get_tail.return_value = "some output\nProcessing...\nmore output"
        mock_provider = MagicMock()
        mock_provider.get_idle_pattern_for_log.return_value = r"Waiting for input"
        mock_provider_manager.get_provider.return_value = mock_provider
        
        result = inbox_service._has_idle_pattern("terminal-123")
        
        assert result is False


class TestCheckAndSendPending:
    """Test check_and_send_pending_messages function."""

    def test_check_and_send_pending_no_messages(self, mock_db, mock_terminal_service):
        """Test check and send when no pending messages."""
        mock_db["get_pending"].return_value = []
        
        result = inbox_service.check_and_send_pending_messages("terminal-123")
        
        assert result is False
        mock_terminal_service.send_input.assert_not_called()

    def test_check_and_send_pending_terminal_busy(self, mock_db, mock_terminal_service, mock_provider_manager):
        """Test check and send when terminal is busy."""
        message = MagicMock()
        message.id = 1
        message.message = "test message"
        mock_db["get_pending"].return_value = [message]
        
        mock_provider = MagicMock()
        mock_provider.get_status.return_value = "PROCESSING"
        mock_provider_manager.get_provider.return_value = mock_provider
        
        result = inbox_service.check_and_send_pending_messages("terminal-123")
        
        assert result is False
        mock_terminal_service.send_input.assert_not_called()

    def test_check_and_send_pending_success(self, mock_db, mock_terminal_service, mock_provider_manager):
        """Test successful check and send pending."""
        message = MagicMock()
        message.id = 1
        message.message = "test message"
        mock_db["get_pending"].return_value = [message]
        
        mock_provider = MagicMock()
        mock_provider.get_status.return_value = TerminalStatus.IDLE
        mock_provider_manager.get_provider.return_value = mock_provider
        
        result = inbox_service.check_and_send_pending_messages("terminal-123")
        
        assert result is True
        mock_terminal_service.send_input.assert_called_once_with("terminal-123", "test message")
        mock_db["update_status"].assert_called_once()


class TestLogFileHandler:
    """Test LogFileHandler class."""

    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    @patch("cli_agent_orchestrator.services.inbox_service._has_idle_pattern")
    @patch("cli_agent_orchestrator.services.inbox_service.check_and_send_pending_messages")
    def test_log_file_handler_on_modified(self, mock_check_send, mock_has_idle, mock_get_pending):
        """Test log file handler on modified event."""
        from watchdog.events import FileModifiedEvent
        
        handler = inbox_service.LogFileHandler()
        mock_get_pending.return_value = [MagicMock()]
        mock_has_idle.return_value = True
        
        # Create a real FileModifiedEvent
        event = FileModifiedEvent("/path/to/terminal-123.log")
        
        handler.on_modified(event)
        
        mock_check_send.assert_called_once_with("terminal-123")

    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_log_file_handler_ignores_non_log(self, mock_get_pending):
        """Test log file handler ignores non-log files."""
        from watchdog.events import FileModifiedEvent
        
        handler = inbox_service.LogFileHandler()
        
        # Create event for non-log file
        event = FileModifiedEvent("/path/to/file.txt")
        
        handler.on_modified(event)
        
        mock_get_pending.assert_not_called()
        event.src_path = "/path/to/other-file.txt"
        event.is_directory = False
        
        handler.on_modified(event)
        
        mock_get_pending.assert_not_called()
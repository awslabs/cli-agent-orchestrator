"""Tests for the inbox service."""

from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.inbox_service import (
    LogFileHandler,
    _get_log_tail,
    _has_idle_pattern,
    check_and_send_pending_messages,
    poll_opencode_pending_messages,
)


class TestGetLogTail:
    """Tests for _get_log_tail function."""

    @patch("cli_agent_orchestrator.services.inbox_service.subprocess.run")
    @patch("cli_agent_orchestrator.services.inbox_service.TERMINAL_LOG_DIR")
    def test_get_log_tail_success(self, mock_log_dir, mock_run):
        """Test getting log tail successfully."""
        mock_log_dir.__truediv__ = lambda self, x: Path("/tmp") / x
        mock_run.return_value = MagicMock(stdout="last line\n")

        result = _get_log_tail("test-terminal", lines=5)

        assert result == "last line\n"
        mock_run.assert_called_once()

    @patch("cli_agent_orchestrator.services.inbox_service.subprocess.run")
    @patch("cli_agent_orchestrator.services.inbox_service.TERMINAL_LOG_DIR")
    def test_get_log_tail_exception(self, mock_log_dir, mock_run):
        """Test getting log tail with exception."""
        mock_log_dir.__truediv__ = lambda self, x: Path("/tmp") / x
        mock_run.side_effect = Exception("Subprocess error")

        result = _get_log_tail("test-terminal")

        assert result == ""


class TestHasIdlePattern:
    """Tests for _has_idle_pattern function."""

    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service._get_log_tail")
    def test_has_idle_pattern_true(self, mock_tail, mock_provider_manager):
        """Test idle pattern detection returns True."""
        mock_tail.return_value = "[developer]> "
        mock_provider = MagicMock()
        mock_provider.get_idle_pattern_for_log.return_value = r"\[developer\]>"
        mock_provider_manager.get_provider.return_value = mock_provider

        result = _has_idle_pattern("test-terminal")

        assert result is True

    @patch("cli_agent_orchestrator.services.inbox_service._get_log_tail")
    def test_has_idle_pattern_empty_tail(self, mock_tail):
        """Test idle pattern detection with empty tail."""
        mock_tail.return_value = ""

        result = _has_idle_pattern("test-terminal")

        assert result is False

    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service._get_log_tail")
    def test_has_idle_pattern_no_provider(self, mock_tail, mock_provider_manager):
        """Test idle pattern detection with no provider."""
        mock_tail.return_value = "some content"
        mock_provider_manager.get_provider.return_value = None

        result = _has_idle_pattern("test-terminal")

        assert result is False

    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service._get_log_tail")
    def test_has_idle_pattern_exception(self, mock_tail, mock_provider_manager):
        """Test idle pattern detection with exception."""
        mock_tail.return_value = "some content"
        mock_provider_manager.get_provider.side_effect = Exception("Error")

        result = _has_idle_pattern("test-terminal")

        assert result is False


class TestCheckAndSendPendingMessages:
    """Tests for check_and_send_pending_messages function."""

    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_no_pending_messages(self, mock_get_messages):
        """Test when no pending messages exist."""
        mock_get_messages.return_value = []

        result = check_and_send_pending_messages("test-terminal")

        assert result is False

    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_provider_not_found(self, mock_get_messages, mock_provider_manager):
        """Test when provider not found."""
        mock_message = MagicMock()
        mock_message.id = 1
        mock_message.message = "test message"
        mock_get_messages.return_value = [mock_message]
        mock_provider_manager.get_provider.return_value = None

        with pytest.raises(ValueError, match="Provider not found"):
            check_and_send_pending_messages("test-terminal")

    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_terminal_not_ready(self, mock_get_messages, mock_provider_manager):
        """Test when terminal not ready."""
        mock_message = MagicMock()
        mock_get_messages.return_value = [mock_message]
        mock_provider = MagicMock()
        mock_provider.get_status.return_value = TerminalStatus.PROCESSING
        mock_provider_manager.get_provider.return_value = mock_provider

        result = check_and_send_pending_messages("test-terminal")

        assert result is False

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_message_sent_successfully(
        self, mock_get_messages, mock_provider_manager, mock_terminal_service, mock_update_status
    ):
        """Test successful message delivery."""
        mock_message = MagicMock()
        mock_message.id = 1
        mock_message.message = "test message"
        mock_get_messages.return_value = [mock_message]
        mock_provider = MagicMock()
        mock_provider.get_status.return_value = TerminalStatus.IDLE
        mock_provider_manager.get_provider.return_value = mock_provider

        result = check_and_send_pending_messages("test-terminal")

        assert result is True
        mock_terminal_service.send_input.assert_called_once_with("test-terminal", "test message")
        mock_update_status.assert_called_once_with(1, MessageStatus.DELIVERED)

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_message_send_failure(
        self, mock_get_messages, mock_provider_manager, mock_terminal_service, mock_update_status
    ):
        """Test message delivery failure."""
        mock_message = MagicMock()
        mock_message.id = 1
        mock_message.message = "test message"
        mock_get_messages.return_value = [mock_message]
        mock_provider = MagicMock()
        mock_provider.get_status.return_value = TerminalStatus.IDLE
        mock_provider_manager.get_provider.return_value = mock_provider
        mock_terminal_service.send_input.side_effect = Exception("Send failed")

        with pytest.raises(Exception, match="Send failed"):
            check_and_send_pending_messages("test-terminal")

        mock_update_status.assert_called_once_with(1, MessageStatus.FAILED)


class TestEagerInboxDelivery:
    """Tests for eager inbox delivery (CAO_EAGER_INBOX_DELIVERY).

    Covers the relaxed status gate in check_and_send_pending_messages() that
    allows PROCESSING and WAITING_USER_ANSWER delivery when the env var is
    enabled and the provider declares accepts_input_while_processing=True.
    """

    def _make_message(self):
        msg = MagicMock()
        msg.id = 42
        msg.message = "eager test"
        msg.sender_id = "sender-1"
        return msg

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivery_idle_status_always_works(
        self, mock_get_messages, mock_pm, mock_ts, mock_update
    ):
        """IDLE delivers regardless of env var or provider capability."""
        mock_get_messages.return_value = [self._make_message()]
        provider = MagicMock()
        provider.get_status.return_value = TerminalStatus.IDLE
        provider.accepts_input_while_processing = False
        mock_pm.get_provider.return_value = provider

        with patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", False):
            result = check_and_send_pending_messages("t1")

        assert result is True
        mock_ts.send_input.assert_called_once()

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivery_completed_status_always_works(
        self, mock_get_messages, mock_pm, mock_ts, mock_update
    ):
        """COMPLETED delivers regardless of env var or provider capability."""
        mock_get_messages.return_value = [self._make_message()]
        provider = MagicMock()
        provider.get_status.return_value = TerminalStatus.COMPLETED
        provider.accepts_input_while_processing = False
        mock_pm.get_provider.return_value = provider

        with patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", False):
            result = check_and_send_pending_messages("t1")

        assert result is True

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivery_processing_with_eager_enabled_and_capable_provider(
        self, mock_get_messages, mock_pm, mock_ts, mock_update
    ):
        """PROCESSING + eager ON + capable provider -> delivers."""
        mock_get_messages.return_value = [self._make_message()]
        provider = MagicMock()
        provider.get_status.return_value = TerminalStatus.PROCESSING
        provider.accepts_input_while_processing = True
        mock_pm.get_provider.return_value = provider

        with patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", True):
            result = check_and_send_pending_messages("t1")

        assert result is True
        mock_ts.send_input.assert_called_once()

    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivery_processing_with_eager_enabled_and_non_capable_provider(
        self, mock_get_messages, mock_pm
    ):
        """PROCESSING + eager ON + non-capable provider -> skips."""
        mock_get_messages.return_value = [self._make_message()]
        provider = MagicMock()
        provider.get_status.return_value = TerminalStatus.PROCESSING
        provider.accepts_input_while_processing = False
        mock_pm.get_provider.return_value = provider

        with patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", True):
            result = check_and_send_pending_messages("t1")

        assert result is False

    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivery_processing_with_eager_disabled(self, mock_get_messages, mock_pm):
        """PROCESSING + eager OFF -> skips even for capable provider."""
        mock_get_messages.return_value = [self._make_message()]
        provider = MagicMock()
        provider.get_status.return_value = TerminalStatus.PROCESSING
        provider.accepts_input_while_processing = True
        mock_pm.get_provider.return_value = provider

        with patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", False):
            result = check_and_send_pending_messages("t1")

        assert result is False

    @patch("cli_agent_orchestrator.services.inbox_service.update_message_status")
    @patch("cli_agent_orchestrator.services.inbox_service.terminal_service")
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivery_waiting_user_answer_with_eager_enabled_and_capable_provider(
        self, mock_get_messages, mock_pm, mock_ts, mock_update
    ):
        """WAITING_USER_ANSWER + eager ON + capable provider -> delivers."""
        mock_get_messages.return_value = [self._make_message()]
        provider = MagicMock()
        provider.get_status.return_value = TerminalStatus.WAITING_USER_ANSWER
        provider.accepts_input_while_processing = True
        mock_pm.get_provider.return_value = provider

        with patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", True):
            result = check_and_send_pending_messages("t1")

        assert result is True
        mock_ts.send_input.assert_called_once()

    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_delivery_error_status_never_delivers(self, mock_get_messages, mock_pm):
        """ERROR -> never delivers regardless of flags."""
        mock_get_messages.return_value = [self._make_message()]
        provider = MagicMock()
        provider.get_status.return_value = TerminalStatus.ERROR
        provider.accepts_input_while_processing = True
        mock_pm.get_provider.return_value = provider

        with patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", True):
            result = check_and_send_pending_messages("t1")

        assert result is False


class TestEagerInboxDeliveryWatchdog:
    """Tests for eager delivery in the watchdog path (LogFileHandler)."""

    @patch("cli_agent_orchestrator.services.inbox_service.check_and_send_pending_messages")
    @patch("cli_agent_orchestrator.services.inbox_service._has_idle_pattern")
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_watchdog_skips_idle_check_for_eager_capable_provider(
        self, mock_get_messages, mock_pm, mock_has_idle, mock_check_send
    ):
        """Watchdog proceeds past idle check when eager + capable provider."""
        mock_get_messages.return_value = [MagicMock()]
        provider = MagicMock()
        provider.accepts_input_while_processing = True
        mock_pm.get_provider.return_value = provider

        handler = LogFileHandler()
        with patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", True):
            handler._handle_log_change("t1")

        # _has_idle_pattern should NOT be called (skipped)
        mock_has_idle.assert_not_called()
        mock_check_send.assert_called_once_with("t1", registry=None)

    @patch("cli_agent_orchestrator.services.inbox_service.check_and_send_pending_messages")
    @patch("cli_agent_orchestrator.services.inbox_service._has_idle_pattern")
    @patch("cli_agent_orchestrator.services.inbox_service.provider_manager")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_watchdog_requires_idle_check_for_non_capable_provider(
        self, mock_get_messages, mock_pm, mock_has_idle, mock_check_send
    ):
        """Watchdog still requires idle check for non-capable provider even with eager ON."""
        mock_get_messages.return_value = [MagicMock()]
        provider = MagicMock()
        provider.accepts_input_while_processing = False
        mock_pm.get_provider.return_value = provider
        mock_has_idle.return_value = False  # Not idle

        handler = LogFileHandler()
        with patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", True):
            handler._handle_log_change("t1")

        mock_has_idle.assert_called_once_with("t1")
        mock_check_send.assert_not_called()


class TestPollOpenCodePendingMessages:
    """Tests for the temporary OpenCode inbox poller."""

    @patch("cli_agent_orchestrator.services.inbox_service.check_and_send_pending_messages")
    @patch("cli_agent_orchestrator.services.inbox_service.list_pending_receiver_ids_by_provider")
    def test_polls_pending_opencode_receivers(self, mock_list_receivers, mock_check_send):
        """Test poller attempts delivery for each pending OpenCode receiver."""
        mock_list_receivers.return_value = ["receiver-1", "receiver-2"]

        poll_opencode_pending_messages()

        mock_list_receivers.assert_called_once_with("opencode_cli")
        assert mock_check_send.call_args_list == [
            call("receiver-1", registry=None),
            call("receiver-2", registry=None),
        ]

    @patch("cli_agent_orchestrator.services.inbox_service.check_and_send_pending_messages")
    @patch("cli_agent_orchestrator.services.inbox_service.list_pending_receiver_ids_by_provider")
    def test_survives_per_receiver_failure(self, mock_list_receivers, mock_check_send):
        """Test one failed receiver does not stop the poll loop."""
        mock_list_receivers.return_value = ["receiver-1", "receiver-2"]
        mock_check_send.side_effect = [Exception("tmux busy"), False]

        poll_opencode_pending_messages()

        assert mock_check_send.call_count == 2


class TestLogFileHandler:
    """Tests for LogFileHandler class."""

    @patch("cli_agent_orchestrator.services.inbox_service.check_and_send_pending_messages")
    @patch("cli_agent_orchestrator.services.inbox_service._has_idle_pattern")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_on_modified_triggers_delivery(self, mock_get_messages, mock_has_idle, mock_check_send):
        """Test on_modified triggers message delivery."""
        from watchdog.events import FileModifiedEvent

        mock_get_messages.return_value = [MagicMock()]
        mock_has_idle.return_value = True

        handler = LogFileHandler()
        event = FileModifiedEvent("/path/to/test-terminal.log")

        handler.on_modified(event)

        mock_check_send.assert_called_once_with("test-terminal", registry=None)

    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_handle_log_change_no_pending_messages(self, mock_get_messages):
        """Test _handle_log_change with no pending messages (covers lines 105-107)."""
        mock_get_messages.return_value = []

        handler = LogFileHandler()

        # Should return early - covers lines 105-107
        handler._handle_log_change("test-terminal")

        mock_get_messages.assert_called_once_with("test-terminal", limit=1)

    @patch("cli_agent_orchestrator.services.inbox_service._has_idle_pattern")
    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_handle_log_change_not_idle(self, mock_get_messages, mock_has_idle):
        """Test _handle_log_change when terminal not idle (covers lines 110-114)."""
        mock_get_messages.return_value = [MagicMock()]
        mock_has_idle.return_value = False

        handler = LogFileHandler()

        # Should return early - covers lines 110-114
        handler._handle_log_change("test-terminal")

        mock_has_idle.assert_called_once_with("test-terminal")

    def test_on_modified_non_log_file(self):
        """Test on_modified ignores non-log files."""
        from watchdog.events import FileModifiedEvent

        handler = LogFileHandler()
        # Create a non-.log file event
        event = MagicMock(spec=FileModifiedEvent)
        event.src_path = "/path/to/test-terminal.txt"

        # Should not process non-log files
        handler.on_modified(event)

    def test_on_modified_not_file_modified_event(self):
        """Test on_modified ignores non-FileModifiedEvent."""
        handler = LogFileHandler()
        event = MagicMock()  # Not a FileModifiedEvent
        event.src_path = "/path/to/test-terminal.log"

        # Should not process non-FileModifiedEvent
        handler.on_modified(event)

    @patch("cli_agent_orchestrator.services.inbox_service.get_pending_messages")
    def test_handle_log_change_exception(self, mock_get_messages):
        """Test _handle_log_change handles exceptions (covers line 119-120)."""
        mock_get_messages.side_effect = Exception("Database error")

        handler = LogFileHandler()

        # Should not raise exception - handles it gracefully
        handler._handle_log_change("test-terminal")

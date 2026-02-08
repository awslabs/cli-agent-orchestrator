"""Unit tests for TmuxClient send_keys method."""

from unittest.mock import MagicMock, Mock, PropertyMock, call, patch

import pytest

from cli_agent_orchestrator.clients.tmux import TmuxClient


class TestTmuxClientSendKeys:
    """Test TmuxClient send_keys behavior."""

    @pytest.fixture(autouse=True)
    def mock_tmux_server(self):
        """Mock libtmux.Server for all tests in this class."""
        with patch("cli_agent_orchestrator.clients.tmux.libtmux.Server") as mock_server_class:
            self.mock_server_class = mock_server_class
            self.mock_server = MagicMock()
            mock_server_class.return_value = self.mock_server

            # Set up session/window/pane chain
            self.mock_session = Mock()
            self.mock_window = Mock()
            self.mock_pane = Mock()

            self.mock_server.sessions.get.return_value = self.mock_session
            self.mock_session.windows.get.return_value = self.mock_window
            type(self.mock_window).active_pane = PropertyMock(return_value=self.mock_pane)

            yield mock_server_class

    def test_send_keys_uses_literal_for_chunks(self):
        """Test that text chunks are sent with literal=True to prevent tmux key interpretation."""
        client = TmuxClient()
        client.send_keys("test-session", "test-window", "echo hello")

        # Find all send_keys calls that are NOT the final C-m
        chunk_calls = [
            c for c in self.mock_pane.send_keys.call_args_list if c != call("C-m", enter=False)
        ]

        # All chunk calls must use literal=True
        for c in chunk_calls:
            assert c.kwargs.get("literal", False) is True or (
                len(c.args) >= 3 and c.args[2] is True
            ), f"Chunk send_keys call missing literal=True: {c}"

    def test_send_keys_enter_not_literal(self):
        """Test that the final C-m (Enter) is sent without literal flag."""
        client = TmuxClient()
        client.send_keys("test-session", "test-window", "echo hello")

        # Last call should be C-m without literal=True
        last_call = self.mock_pane.send_keys.call_args_list[-1]
        assert last_call == call("C-m", enter=False)

    def test_send_keys_with_quotes(self):
        """Test sending command with single quotes (the original bug scenario)."""
        client = TmuxClient()
        command = "claude --append-system-prompt 'test prompt with spaces' --mcp-config '{\"key\": \"value\"}'"
        client.send_keys("test-session", "test-window", command)

        # Verify chunks are sent with literal=True
        chunk_calls = [
            c for c in self.mock_pane.send_keys.call_args_list if c != call("C-m", enter=False)
        ]
        for c in chunk_calls:
            assert (
                c.kwargs.get("literal", False) is True
            ), f"Chunk with quotes must use literal=True to prevent tmux mangling: {c}"

    def test_send_keys_long_command_chunked(self):
        """Test that long commands are split into chunks."""
        client = TmuxClient()
        # Create a command longer than 100 chars with whitespace for chunking
        long_command = "claude --append-system-prompt " + " ".join(["word"] * 50)
        client.send_keys("test-session", "test-window", long_command)

        # Should have multiple chunk calls plus the final C-m
        all_calls = self.mock_pane.send_keys.call_args_list
        chunk_calls = [c for c in all_calls if c != call("C-m", enter=False)]

        assert len(chunk_calls) > 1, "Long command should be split into multiple chunks"

    def test_send_keys_short_command_single_chunk(self):
        """Test that short commands are sent as a single chunk."""
        client = TmuxClient()
        client.send_keys("test-session", "test-window", "ls -la")

        all_calls = self.mock_pane.send_keys.call_args_list
        chunk_calls = [c for c in all_calls if c != call("C-m", enter=False)]

        assert len(chunk_calls) == 1, "Short command should be a single chunk"

    def test_send_keys_preserves_full_command(self):
        """Test that all chunks together reconstruct the original command."""
        client = TmuxClient()
        command = "echo 'hello world' && echo 'foo bar baz' && echo 'done'"
        client.send_keys("test-session", "test-window", command)

        # Reconstruct from chunk calls
        chunk_calls = [
            c for c in self.mock_pane.send_keys.call_args_list if c != call("C-m", enter=False)
        ]
        reconstructed = "".join(c.args[0] for c in chunk_calls)

        assert reconstructed == command

    def test_send_keys_session_not_found(self):
        """Test error when session not found."""
        self.mock_server.sessions.get.return_value = None

        client = TmuxClient()
        with pytest.raises(ValueError, match="not found"):
            client.send_keys("nonexistent", "test-window", "echo hello")

    def test_send_keys_window_not_found(self):
        """Test error when window not found."""
        self.mock_session.windows.get.return_value = None

        client = TmuxClient()
        with pytest.raises(ValueError, match="not found"):
            client.send_keys("test-session", "nonexistent", "echo hello")

"""Unit tests for terminal service get_working_directory and send_special_key functions."""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.services.terminal_service import (
    exit_terminal_cli,
    get_working_directory,
    send_special_key,
)

_TS = "cli_agent_orchestrator.services.terminal_service"


class TestTerminalServiceWorkingDirectory:
    """Test terminal service working directory functionality."""

    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_working_directory_success(self, mock_get_metadata, mock_tmux_client):
        """Test successful working directory retrieval."""
        # Arrange
        terminal_id = "test-terminal-123"
        expected_dir = "/home/user/project"
        mock_get_metadata.return_value = {
            "tmux_session": "test-session",
            "tmux_window": "test-window",
        }
        mock_tmux_client.get_pane_working_directory.return_value = expected_dir

        # Act
        result = get_working_directory(terminal_id)

        # Assert
        assert result == expected_dir
        mock_get_metadata.assert_called_once_with(terminal_id)
        mock_tmux_client.get_pane_working_directory.assert_called_once_with(
            "test-session", "test-window"
        )

    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_working_directory_terminal_not_found(self, mock_get_metadata, mock_tmux_client):
        """Test ValueError when terminal not found."""
        # Arrange
        terminal_id = "nonexistent-terminal"
        mock_get_metadata.return_value = None

        # Act & Assert
        with pytest.raises(ValueError, match="Terminal 'nonexistent-terminal' not found"):
            get_working_directory(terminal_id)

        mock_get_metadata.assert_called_once_with(terminal_id)
        mock_tmux_client.get_pane_working_directory.assert_not_called()

    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_working_directory_returns_none(self, mock_get_metadata, mock_tmux_client):
        """Test when pane has no working directory."""
        # Arrange
        terminal_id = "test-terminal-456"
        mock_get_metadata.return_value = {
            "tmux_session": "test-session",
            "tmux_window": "test-window",
        }
        mock_tmux_client.get_pane_working_directory.return_value = None

        # Act
        result = get_working_directory(terminal_id)

        # Assert
        assert result is None
        mock_get_metadata.assert_called_once_with(terminal_id)
        mock_tmux_client.get_pane_working_directory.assert_called_once_with(
            "test-session", "test-window"
        )

    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_working_directory_returns_directory_from_tmux_pane(
        self, mock_get_metadata, mock_tmux_client
    ):
        """Test that get_working_directory returns the directory obtained from tmux pane."""
        # Arrange
        terminal_id = "test-terminal-789"
        pane_dir = "/workspace/my-project/src"
        mock_get_metadata.return_value = {
            "tmux_session": "cao-workspace",
            "tmux_window": "developer-xyz",
        }
        mock_tmux_client.get_pane_working_directory.return_value = pane_dir

        # Act
        result = get_working_directory(terminal_id)

        # Assert
        assert result == pane_dir
        mock_tmux_client.get_pane_working_directory.assert_called_once_with(
            "cao-workspace", "developer-xyz"
        )

    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_working_directory_raises_for_nonexistent_terminal(
        self, mock_get_metadata, mock_tmux_client
    ):
        """Test that get_working_directory raises ValueError for a terminal that does not exist."""
        # Arrange
        mock_get_metadata.return_value = None

        # Act & Assert
        with pytest.raises(ValueError, match="Terminal 'does-not-exist' not found"):
            get_working_directory("does-not-exist")

        mock_tmux_client.get_pane_working_directory.assert_not_called()


class TestSendSpecialKey:
    """Tests for send_special_key function."""

    @patch("cli_agent_orchestrator.services.terminal_service.update_last_active")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_send_special_key_sends_key_via_tmux_client(
        self, mock_get_metadata, mock_tmux_client, mock_update_last_active
    ):
        """Test that send_special_key sends the key via tmux client."""
        # Arrange
        terminal_id = "test-terminal-001"
        mock_get_metadata.return_value = {
            "tmux_session": "cao-session",
            "tmux_window": "developer-abcd",
        }

        # Act
        result = send_special_key(terminal_id, "C-d")

        # Assert
        assert result is True
        mock_tmux_client.send_special_key.assert_called_once_with(
            "cao-session", "developer-abcd", "C-d"
        )
        mock_update_last_active.assert_called_once_with(terminal_id)

    @patch("cli_agent_orchestrator.services.terminal_service.update_last_active")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_send_special_key_ctrl_c(
        self, mock_get_metadata, mock_tmux_client, mock_update_last_active
    ):
        """Test that send_special_key can send C-c (Ctrl+C) to a terminal."""
        # Arrange
        terminal_id = "test-terminal-002"
        mock_get_metadata.return_value = {
            "tmux_session": "cao-session",
            "tmux_window": "reviewer-efgh",
        }

        # Act
        result = send_special_key(terminal_id, "C-c")

        # Assert
        assert result is True
        mock_tmux_client.send_special_key.assert_called_once_with(
            "cao-session", "reviewer-efgh", "C-c"
        )

    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_send_special_key_terminal_not_found(self, mock_get_metadata, mock_tmux_client):
        """Test that send_special_key raises ValueError when terminal not found."""
        # Arrange
        mock_get_metadata.return_value = None

        # Act & Assert
        with pytest.raises(ValueError, match="Terminal 'nonexistent' not found"):
            send_special_key("nonexistent", "C-d")

        mock_tmux_client.send_special_key.assert_not_called()

    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_send_special_key_propagates_tmux_errors(self, mock_get_metadata, mock_tmux_client):
        """Test that send_special_key propagates exceptions from tmux client."""
        # Arrange
        terminal_id = "test-terminal-003"
        mock_get_metadata.return_value = {
            "tmux_session": "cao-session",
            "tmux_window": "developer-ijkl",
        }
        mock_tmux_client.send_special_key.side_effect = Exception("Tmux send error")

        # Act & Assert
        with pytest.raises(Exception, match="Tmux send error"):
            send_special_key(terminal_id, "Escape")

    @patch("cli_agent_orchestrator.services.terminal_service.update_last_active")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_send_special_key_escape(
        self, mock_get_metadata, mock_tmux_client, mock_update_last_active
    ):
        """Test that send_special_key can send Escape key."""
        # Arrange
        terminal_id = "test-terminal-004"
        mock_get_metadata.return_value = {
            "tmux_session": "cao-session",
            "tmux_window": "developer-mnop",
        }

        # Act
        result = send_special_key(terminal_id, "Escape")

        # Assert
        assert result is True
        mock_tmux_client.send_special_key.assert_called_once_with(
            "cao-session", "developer-mnop", "Escape"
        )


class TestExitTerminalCli:
    """Tests for exit_terminal_cli — the graceful CLI shutdown helper shared by
    the exit endpoint and run_agent_step teardown (issue #312 review fix #4)."""

    @patch(f"{_TS}.send_input")
    @patch(f"{_TS}.send_special_key")
    @patch(f"{_TS}.provider_manager")
    def test_text_command_uses_send_input(self, mock_pm, mock_special, mock_input):
        """A text exit command (e.g. /exit) is sent via send_input."""
        provider = MagicMock()
        provider.exit_cli.return_value = "/exit"
        mock_pm.get_provider.return_value = provider

        exit_terminal_cli("abcd1234")

        mock_input.assert_called_once_with("abcd1234", "/exit")
        mock_special.assert_not_called()

    @patch(f"{_TS}.send_input")
    @patch(f"{_TS}.send_special_key")
    @patch(f"{_TS}.provider_manager")
    def test_ctrl_key_uses_send_special_key(self, mock_pm, mock_special, mock_input):
        """A tmux key sequence (e.g. C-d) is sent via send_special_key."""
        provider = MagicMock()
        provider.exit_cli.return_value = "C-d"
        mock_pm.get_provider.return_value = provider

        exit_terminal_cli("abcd1234")

        mock_special.assert_called_once_with("abcd1234", "C-d")
        mock_input.assert_not_called()

    @patch(f"{_TS}.send_input")
    @patch(f"{_TS}.send_special_key")
    @patch(f"{_TS}.provider_manager")
    def test_meta_key_uses_send_special_key(self, mock_pm, mock_special, mock_input):
        """A meta key sequence (M-x) also routes via send_special_key."""
        provider = MagicMock()
        provider.exit_cli.return_value = "M-x"
        mock_pm.get_provider.return_value = provider

        exit_terminal_cli("abcd1234")

        mock_special.assert_called_once_with("abcd1234", "M-x")
        mock_input.assert_not_called()

    @patch(f"{_TS}.provider_manager")
    def test_no_provider_raises_value_error(self, mock_pm):
        """No registered provider -> ValueError (mapped to 404 at the boundary)."""
        mock_pm.get_provider.return_value = None
        with pytest.raises(ValueError, match="Provider not found"):
            exit_terminal_cli("deadbeef")

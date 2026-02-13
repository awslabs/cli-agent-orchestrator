"""Unit tests for terminal service."""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import Terminal, TerminalStatus
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.terminal_service import OutputMode, get_working_directory


@pytest.fixture
def mock_db():
    """Mock database client."""
    with patch("cli_agent_orchestrator.services.terminal_service.db_create_terminal") as create, \
         patch("cli_agent_orchestrator.services.terminal_service.db_delete_terminal") as delete, \
         patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata") as get_metadata, \
         patch("cli_agent_orchestrator.services.terminal_service.update_last_active") as update_active:
        yield {
            "create": create,
            "delete": delete,
            "get_metadata": get_metadata,
            "update_active": update_active
        }


@pytest.fixture
def mock_tmux():
    """Mock tmux client."""
    with patch("cli_agent_orchestrator.services.terminal_service.tmux_client") as mock:
        yield mock


@pytest.fixture
def mock_provider_manager():
    """Mock provider manager."""
    with patch("cli_agent_orchestrator.services.terminal_service.provider_manager") as mock:
        yield mock


@pytest.fixture
def sample_terminal():
    """Sample terminal for testing."""
    return Terminal(
        id="test-terminal-123",
        name="test-window",
        provider=ProviderType.Q_CLI,
        session_name="test-session",
        agent_profile="developer",
        status=TerminalStatus.IDLE
    )


class TestCreateTerminal:
    """Test create_terminal function."""

    def test_create_terminal_new_session(self, mock_db, mock_tmux, mock_provider_manager):
        """Test creating terminal with new session."""
        mock_tmux.session_exists.return_value = False
        mock_provider_manager.get_provider.return_value = MagicMock()
        mock_db["create"].return_value = None
        
        with patch("cli_agent_orchestrator.services.terminal_service.generate_terminal_id") as mock_gen_id, \
             patch("cli_agent_orchestrator.services.terminal_service.generate_session_name") as mock_gen_session, \
             patch("cli_agent_orchestrator.services.terminal_service.generate_window_name") as mock_gen_window:
            mock_gen_id.return_value = "terminal-123"
            mock_gen_session.return_value = "session-123"
            mock_gen_window.return_value = "window-123"
            
            result = terminal_service.create_terminal("q_cli", "developer", new_session=True)
            
            assert result.id == "terminal-123"
            mock_tmux.create_session.assert_called_once()

    def test_create_terminal_existing_session(self, mock_db, mock_tmux, mock_provider_manager):
        """Test creating terminal with existing session."""
        mock_tmux.session_exists.return_value = True
        mock_tmux.create_window.return_value = "window-123"
        mock_provider_manager.create_provider.return_value = MagicMock()
        mock_db["create"].return_value = None
        
        with patch("cli_agent_orchestrator.services.terminal_service.generate_terminal_id") as mock_gen_id, \
             patch("cli_agent_orchestrator.services.terminal_service.generate_session_name") as mock_gen_session, \
             patch("cli_agent_orchestrator.services.terminal_service.generate_window_name") as mock_gen_window, \
             patch("cli_agent_orchestrator.services.terminal_service.TERMINAL_LOG_DIR") as mock_log_dir:
            mock_gen_id.return_value = "terminal-123"
            mock_gen_session.return_value = "session-123"
            mock_gen_window.return_value = "window-123"
            mock_log_dir.__truediv__.return_value = MagicMock()
            
            result = terminal_service.create_terminal("q_cli", "developer", session_name="existing-session")
            
            assert result.id == "terminal-123"
            mock_tmux.create_session.assert_not_called()
            mock_tmux.create_window.assert_called_once()

    def test_create_terminal_with_working_directory(self, mock_db, mock_tmux, mock_provider_manager):
        """Test creating terminal with working directory."""
        mock_tmux.session_exists.return_value = False
        mock_provider_manager.get_provider.return_value = MagicMock()
        mock_db["create"].return_value = None
        
        with patch("cli_agent_orchestrator.services.terminal_service.generate_terminal_id") as mock_gen_id, \
             patch("cli_agent_orchestrator.services.terminal_service.generate_session_name") as mock_gen_session, \
             patch("cli_agent_orchestrator.services.terminal_service.generate_window_name") as mock_gen_window:
            mock_gen_id.return_value = "terminal-123"
            mock_gen_session.return_value = "session-123"
            mock_gen_window.return_value = "window-123"
            
            result = terminal_service.create_terminal("q_cli", "developer", working_directory="/home/user", new_session=True)
            
            assert result.id == "terminal-123"
            mock_tmux.create_session.assert_called_once()

    def test_create_terminal_invalid_provider(self, mock_db, mock_tmux, mock_provider_manager):
        """Test creating terminal with invalid provider."""
        with pytest.raises(ValueError, match="'invalid_provider' is not a valid ProviderType"):
            terminal_service.create_terminal("invalid_provider", "developer")


class TestGetTerminal:
    """Test get_terminal function."""

    def test_get_terminal_success(self, mock_db, mock_provider_manager, sample_terminal):
        """Test successful terminal retrieval."""
        mock_db["get_metadata"].return_value = {
            "id": "test-terminal-123",
            "tmux_window": "test-window",
            "provider": "q_cli",
            "tmux_session": "test-session",
            "agent_profile": "developer",
            "last_active": "2024-01-01T00:00:00"
        }
        mock_provider = MagicMock()
        mock_provider.get_status.return_value = TerminalStatus.IDLE
        mock_provider_manager.get_provider.return_value = mock_provider
        
        result = terminal_service.get_terminal("test-terminal-123")
        
        assert result is not None
        assert result["id"] == "test-terminal-123"

    def test_get_terminal_not_found(self, mock_db):
        """Test terminal retrieval when not found."""
        mock_db["get_metadata"].return_value = None
        
        with pytest.raises(ValueError, match="Terminal 'nonexistent' not found"):
            terminal_service.get_terminal("nonexistent")


class TestSendInput:
    """Test send_input function."""

    def test_send_input_success(self, mock_db, mock_tmux, sample_terminal):
        """Test successful input sending."""
        mock_db["get_metadata"].return_value = {
            "id": "test-terminal-123",
            "tmux_session": "test-session",
            "tmux_window": "test-window"
        }
        
        terminal_service.send_input("test-terminal-123", "test message")
        
        mock_tmux.send_keys.assert_called_once_with("test-session", "test-window", "test message")
        mock_db["update_active"].assert_called_once()

    def test_send_input_terminal_not_found(self, mock_db, mock_tmux):
        """Test input sending when terminal not found."""
        mock_db["get_metadata"].return_value = None
        
        with pytest.raises(ValueError, match="Terminal 'nonexistent' not found"):
            terminal_service.send_input("nonexistent", "test message")


class TestGetOutput:
    """Test get_output function."""

    def test_get_output_full_mode(self, mock_db, mock_tmux, sample_terminal):
        """Test getting full output."""
        mock_db["get_metadata"].return_value = {
            "id": "test-terminal-123",
            "tmux_session": "test-session",
            "tmux_window": "test-window"
        }
        mock_tmux.get_history.return_value = "full output"
        
        result = terminal_service.get_output("test-terminal-123", OutputMode.FULL)
        
        assert result == "full output"
        mock_tmux.get_history.assert_called_once()

    def test_get_output_last_mode(self, mock_db, mock_tmux, mock_provider_manager, sample_terminal):
        """Test getting last output."""
        mock_db["get_metadata"].return_value = {
            "id": "test-terminal-123",
            "tmux_session": "test-session",
            "tmux_window": "test-window"
        }
        mock_tmux.get_history.return_value = "line1\nline2\nlast line"
        mock_provider = MagicMock()
        mock_provider.extract_last_message_from_script.return_value = "last line"
        mock_provider_manager.get_provider.return_value = mock_provider
        
        result = terminal_service.get_output("test-terminal-123", OutputMode.LAST)
        
        assert result == "last line"


class TestDeleteTerminal:
    """Test delete_terminal function."""

    def test_delete_terminal_success(self, mock_db, mock_tmux, mock_provider_manager, sample_terminal):
        """Test successful terminal deletion."""
        mock_db["get_metadata"].return_value = {
            "id": "test-terminal-123",
            "tmux_session": "test-session",
            "tmux_window": "test-window"
        }
        mock_db["delete"].return_value = True
        
        result = terminal_service.delete_terminal("test-terminal-123")
        
        assert result is True
        mock_db["delete"].assert_called_once()

    def test_delete_terminal_stops_pipe_pane(self, mock_db, mock_tmux, mock_provider_manager, sample_terminal):
        """Test terminal deletion stops pipe pane."""
        mock_db["get_metadata"].return_value = {
            "id": "test-terminal-123",
            "tmux_session": "test-session",
            "tmux_window": "test-window"
        }
        mock_db["delete"].return_value = True
        
        terminal_service.delete_terminal("test-terminal-123")
        
        mock_tmux.stop_pipe_pane.assert_called_once_with("test-session", "test-window")


class TestTerminalServiceWorkingDirectory:
    """Test terminal service working directory functionality."""

    @patch("cli_agent_orchestrator.services.terminal_service.tmux_client")
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

    @patch("cli_agent_orchestrator.services.terminal_service.tmux_client")
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

    @patch("cli_agent_orchestrator.services.terminal_service.tmux_client")
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

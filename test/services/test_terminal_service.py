"""Unit tests for terminal service multiplexer integration."""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.multiplexers.base import LaunchSpec
from cli_agent_orchestrator.services.terminal_service import (
    create_terminal,
    get_working_directory,
    send_special_key,
)


@pytest.fixture
def mock_multiplexer(monkeypatch):
    """Install a multiplexer mock via the accessor seam."""
    multiplexer = MagicMock()
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.get_multiplexer",
        lambda: multiplexer,
    )
    return multiplexer


@pytest.fixture
def create_terminal_dependencies(monkeypatch):
    """Patch create_terminal collaborators outside the multiplexer seam."""
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.generate_terminal_id",
        lambda: "deadbeef",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.generate_window_name",
        lambda agent_profile: f"{agent_profile}-window",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.generate_session_name",
        lambda: "generated-session",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.db_create_terminal",
        MagicMock(),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.load_agent_profile",
        MagicMock(side_effect=FileNotFoundError()),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.build_skill_catalog",
        MagicMock(return_value=None),
    )
    provider_instance = MagicMock()
    provider_instance.get_launch_spec.return_value = None
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.provider_manager.create_provider",
        MagicMock(return_value=provider_instance),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.provider_manager.cleanup_provider",
        MagicMock(),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.dispatch_plugin_event",
        MagicMock(),
    )
    log_touch = MagicMock()
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.TERMINAL_LOG_DIR",
        MagicMock(__truediv__=MagicMock(return_value=MagicMock(touch=log_touch))),
    )
    return {
        "provider_instance": provider_instance,
        "db_create_terminal": create_terminal.__globals__["db_create_terminal"],
        "create_provider": create_terminal.__globals__["provider_manager"].create_provider,
    }


class TestTerminalServiceWorkingDirectory:
    """Test terminal service working directory functionality."""

    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_working_directory_success(self, mock_get_metadata, mock_multiplexer):
        """Test successful working directory retrieval."""
        # Arrange
        terminal_id = "test-terminal-123"
        expected_dir = "/home/user/project"
        mock_get_metadata.return_value = {
            "tmux_session": "test-session",
            "tmux_window": "test-window",
        }
        mock_multiplexer.get_pane_working_directory.return_value = expected_dir

        # Act
        result = get_working_directory(terminal_id)

        # Assert
        assert result == expected_dir
        mock_get_metadata.assert_called_once_with(terminal_id)
        mock_multiplexer.get_pane_working_directory.assert_called_once_with(
            "test-session", "test-window"
        )

    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_working_directory_terminal_not_found(self, mock_get_metadata, mock_multiplexer):
        """Test ValueError when terminal not found."""
        # Arrange
        terminal_id = "nonexistent-terminal"
        mock_get_metadata.return_value = None

        # Act & Assert
        with pytest.raises(ValueError, match="Terminal 'nonexistent-terminal' not found"):
            get_working_directory(terminal_id)

        mock_get_metadata.assert_called_once_with(terminal_id)
        mock_multiplexer.get_pane_working_directory.assert_not_called()

    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_working_directory_returns_none(self, mock_get_metadata, mock_multiplexer):
        """Test when pane has no working directory."""
        # Arrange
        terminal_id = "test-terminal-456"
        mock_get_metadata.return_value = {
            "tmux_session": "test-session",
            "tmux_window": "test-window",
        }
        mock_multiplexer.get_pane_working_directory.return_value = None

        # Act
        result = get_working_directory(terminal_id)

        # Assert
        assert result is None
        mock_get_metadata.assert_called_once_with(terminal_id)
        mock_multiplexer.get_pane_working_directory.assert_called_once_with(
            "test-session", "test-window"
        )

    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_working_directory_returns_directory_from_tmux_pane(
        self, mock_get_metadata, mock_multiplexer
    ):
        """Test that get_working_directory returns the directory obtained from tmux pane."""
        # Arrange
        terminal_id = "test-terminal-789"
        pane_dir = "/workspace/my-project/src"
        mock_get_metadata.return_value = {
            "tmux_session": "cao-workspace",
            "tmux_window": "developer-xyz",
        }
        mock_multiplexer.get_pane_working_directory.return_value = pane_dir

        # Act
        result = get_working_directory(terminal_id)

        # Assert
        assert result == pane_dir
        mock_multiplexer.get_pane_working_directory.assert_called_once_with(
            "cao-workspace", "developer-xyz"
        )

    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_working_directory_raises_for_nonexistent_terminal(
        self, mock_get_metadata, mock_multiplexer
    ):
        """Test that get_working_directory raises ValueError for a terminal that does not exist."""
        # Arrange
        mock_get_metadata.return_value = None

        # Act & Assert
        with pytest.raises(ValueError, match="Terminal 'does-not-exist' not found"):
            get_working_directory("does-not-exist")

        mock_multiplexer.get_pane_working_directory.assert_not_called()


class TestSendSpecialKey:
    """Tests for send_special_key function."""

    @patch("cli_agent_orchestrator.services.terminal_service.update_last_active")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_send_special_key_sends_key_via_tmux_client(
        self, mock_get_metadata, mock_update_last_active, mock_multiplexer
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
        mock_multiplexer.send_special_key.assert_called_once_with(
            "cao-session", "developer-abcd", "C-d"
        )
        mock_update_last_active.assert_called_once_with(terminal_id)

    @patch("cli_agent_orchestrator.services.terminal_service.update_last_active")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_send_special_key_ctrl_c(
        self, mock_get_metadata, mock_update_last_active, mock_multiplexer
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
        mock_multiplexer.send_special_key.assert_called_once_with(
            "cao-session", "reviewer-efgh", "C-c"
        )

    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_send_special_key_terminal_not_found(self, mock_get_metadata, mock_multiplexer):
        """Test that send_special_key raises ValueError when terminal not found."""
        # Arrange
        mock_get_metadata.return_value = None

        # Act & Assert
        with pytest.raises(ValueError, match="Terminal 'nonexistent' not found"):
            send_special_key("nonexistent", "C-d")

        mock_multiplexer.send_special_key.assert_not_called()

    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_send_special_key_propagates_tmux_errors(self, mock_get_metadata, mock_multiplexer):
        """Test that send_special_key propagates exceptions from tmux client."""
        # Arrange
        terminal_id = "test-terminal-003"
        mock_get_metadata.return_value = {
            "tmux_session": "cao-session",
            "tmux_window": "developer-ijkl",
        }
        mock_multiplexer.send_special_key.side_effect = Exception("Tmux send error")

        # Act & Assert
        with pytest.raises(Exception, match="Tmux send error"):
            send_special_key(terminal_id, "Escape")

    @patch("cli_agent_orchestrator.services.terminal_service.update_last_active")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_send_special_key_escape(
        self, mock_get_metadata, mock_update_last_active, mock_multiplexer
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
        mock_multiplexer.send_special_key.assert_called_once_with(
            "cao-session", "developer-mnop", "Escape"
        )


class TestCreateTerminalLaunchSpec:
    """Tests for LaunchSpec pass-through in create_terminal."""

    def test_create_terminal_new_session_forwards_launch_spec(
        self, mock_multiplexer, create_terminal_dependencies
    ):
        """New-session path should forward the provided launch_spec verbatim."""
        spec = LaunchSpec(argv=["codex", "--yolo"], env={"A": "1"}, provider="codex")
        mock_multiplexer.session_exists.return_value = False

        create_terminal(
            provider="codex",
            agent_profile="developer",
            session_name="alpha",
            new_session=True,
            working_directory="/workspace",
            launch_spec=spec,
        )

        mock_multiplexer.create_session.assert_called_once_with(
            "cao-alpha",
            "developer-window",
            "deadbeef",
            "/workspace",
            launch_spec=spec,
        )

    def test_create_terminal_existing_session_forwards_launch_spec(
        self, mock_multiplexer, create_terminal_dependencies
    ):
        """Existing-session path should forward the provided launch_spec verbatim."""
        spec = LaunchSpec(argv=["q"], provider="q_cli")
        mock_multiplexer.session_exists.return_value = True
        mock_multiplexer.create_window.return_value = "renamed-window"

        terminal = create_terminal(
            provider="q_cli",
            agent_profile="developer",
            session_name="cao-existing",
            new_session=False,
            working_directory="/workspace",
            launch_spec=spec,
        )

        mock_multiplexer.create_window.assert_called_once_with(
            "cao-existing",
            "developer-window",
            "deadbeef",
            "/workspace",
            launch_spec=spec,
        )
        assert terminal.name == "renamed-window"

    def test_create_terminal_defaults_launch_spec_to_none(
        self, mock_multiplexer, create_terminal_dependencies
    ):
        """Default create_terminal path should preserve launch_spec=None."""
        mock_multiplexer.session_exists.return_value = False

        create_terminal(
            provider="codex",
            agent_profile="developer",
            session_name="beta",
            new_session=True,
        )

        mock_multiplexer.create_session.assert_called_once_with(
            "cao-beta",
            "developer-window",
            "deadbeef",
            None,
            launch_spec=None,
        )

    def test_create_terminal_uses_provider_launch_spec_when_not_explicit(
        self, mock_multiplexer, create_terminal_dependencies
    ):
        """When no explicit launch_spec is provided, service should ask the provider."""
        mock_multiplexer.session_exists.return_value = False
        provider_instance = create_terminal_dependencies["provider_instance"]
        spec = LaunchSpec(argv=["codex.cmd", "--yolo"], provider="codex")
        provider_instance.get_launch_spec.return_value = spec

        create_terminal(
            provider="codex",
            agent_profile="developer",
            session_name="gamma",
            new_session=True,
        )

        provider_instance.get_launch_spec.assert_called_once_with(mock_multiplexer)
        mock_multiplexer.create_session.assert_called_once_with(
            "cao-gamma",
            "developer-window",
            "deadbeef",
            None,
            launch_spec=spec,
        )

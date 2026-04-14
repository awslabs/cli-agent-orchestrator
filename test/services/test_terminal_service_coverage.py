"""Additional terminal_service tests for coverage gaps.

Covers: create_terminal error cleanup, delete_terminal internals,
and the SESSION_PREFIX branch.
"""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile


class TestCreateTerminalCleanup:
    """Test error cleanup paths in create_terminal."""

    @patch("cli_agent_orchestrator.services.terminal_service.TERMINAL_LOG_DIR")
    @patch("cli_agent_orchestrator.services.terminal_service.tmux_client")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.db_create_terminal")
    @patch(
        "cli_agent_orchestrator.services.terminal_service.generate_window_name", return_value="w1"
    )
    @patch(
        "cli_agent_orchestrator.services.terminal_service.generate_terminal_id",
        return_value="tid1",
    )
    @patch("cli_agent_orchestrator.services.terminal_service.load_agent_profile")
    def test_cleanup_on_provider_init_failure(
        self,
        mock_load_profile,
        mock_tid,
        mock_wname,
        mock_db_create,
        mock_pm,
        mock_tmux,
        mock_log_dir,
    ):
        """When provider.initialize() fails, cleanup should kill session and cleanup provider."""
        from cli_agent_orchestrator.services.terminal_service import create_terminal

        mock_tmux.session_exists.return_value = False
        mock_tmux.create_session.return_value = "w1"
        mock_load_profile.return_value = AgentProfile(name="dev", description="Dev")

        mock_provider = MagicMock()
        mock_provider.initialize.side_effect = Exception("Provider init failed")
        mock_pm.create_provider.return_value = mock_provider

        with pytest.raises(Exception, match="Provider init failed"):
            create_terminal(
                provider="kiro_cli",
                agent_profile="dev",
                session_name="test-ses",
                new_session=True,
                allowed_tools=["*"],
            )

        mock_pm.cleanup_provider.assert_called_once_with("tid1")
        mock_tmux.kill_session.assert_called_once()

    @patch("cli_agent_orchestrator.services.terminal_service.TERMINAL_LOG_DIR")
    @patch("cli_agent_orchestrator.services.terminal_service.tmux_client")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.db_create_terminal")
    @patch(
        "cli_agent_orchestrator.services.terminal_service.generate_window_name", return_value="w1"
    )
    @patch(
        "cli_agent_orchestrator.services.terminal_service.generate_terminal_id",
        return_value="tid1",
    )
    @patch("cli_agent_orchestrator.services.terminal_service.load_agent_profile")
    def test_cleanup_on_failure_does_not_kill_session_if_not_new(
        self,
        mock_load_profile,
        mock_tid,
        mock_wname,
        mock_db_create,
        mock_pm,
        mock_tmux,
        mock_log_dir,
    ):
        """When new_session=False, cleanup should NOT kill the session."""
        from cli_agent_orchestrator.services.terminal_service import create_terminal

        mock_tmux.session_exists.return_value = True
        mock_tmux.create_window.return_value = "w1"
        mock_load_profile.return_value = AgentProfile(name="dev", description="Dev")

        mock_provider = MagicMock()
        mock_provider.initialize.side_effect = Exception("fail")
        mock_pm.create_provider.return_value = mock_provider

        with pytest.raises(Exception):
            create_terminal(
                provider="kiro_cli",
                agent_profile="dev",
                session_name="cao-existing",
                new_session=False,
                allowed_tools=["*"],
            )

        mock_pm.cleanup_provider.assert_called_once()
        mock_tmux.kill_session.assert_not_called()

    @patch("cli_agent_orchestrator.services.terminal_service.TERMINAL_LOG_DIR")
    @patch("cli_agent_orchestrator.services.terminal_service.tmux_client")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.db_create_terminal")
    @patch(
        "cli_agent_orchestrator.services.terminal_service.generate_window_name", return_value="w1"
    )
    @patch(
        "cli_agent_orchestrator.services.terminal_service.generate_terminal_id",
        return_value="tid1",
    )
    @patch("cli_agent_orchestrator.services.terminal_service.load_agent_profile")
    def test_cleanup_ignores_cleanup_errors(
        self,
        mock_load_profile,
        mock_tid,
        mock_wname,
        mock_db_create,
        mock_pm,
        mock_tmux,
        mock_log_dir,
    ):
        """Cleanup errors should be swallowed, original error re-raised."""
        from cli_agent_orchestrator.services.terminal_service import create_terminal

        mock_tmux.session_exists.return_value = False
        mock_tmux.create_session.return_value = "w1"
        mock_load_profile.return_value = AgentProfile(name="dev", description="Dev")

        mock_provider = MagicMock()
        mock_provider.initialize.side_effect = Exception("original error")
        mock_pm.create_provider.return_value = mock_provider
        mock_pm.cleanup_provider.side_effect = Exception("cleanup error")
        mock_tmux.kill_session.side_effect = Exception("kill error")

        with pytest.raises(Exception, match="original error"):
            create_terminal(
                provider="kiro_cli",
                agent_profile="dev",
                session_name="test-ses",
                new_session=True,
                allowed_tools=["*"],
            )

    @patch("cli_agent_orchestrator.services.terminal_service.TERMINAL_LOG_DIR")
    @patch("cli_agent_orchestrator.services.terminal_service.tmux_client")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.db_create_terminal")
    @patch(
        "cli_agent_orchestrator.services.terminal_service.generate_window_name", return_value="w1"
    )
    @patch(
        "cli_agent_orchestrator.services.terminal_service.generate_terminal_id",
        return_value="tid1",
    )
    @patch("cli_agent_orchestrator.services.terminal_service.load_agent_profile")
    def test_session_prefix_added_for_new_session(
        self,
        mock_load_profile,
        mock_tid,
        mock_wname,
        mock_db_create,
        mock_pm,
        mock_tmux,
        mock_log_dir,
    ):
        """New sessions without the prefix get it added automatically."""
        from cli_agent_orchestrator.services.terminal_service import create_terminal

        mock_tmux.session_exists.return_value = False
        mock_tmux.create_session.return_value = "w1"
        mock_load_profile.return_value = AgentProfile(name="dev", description="Dev")
        mock_provider = MagicMock()
        mock_pm.create_provider.return_value = mock_provider
        mock_log_dir.__truediv__ = MagicMock(return_value=MagicMock())

        result = create_terminal(
            provider="kiro_cli",
            agent_profile="dev",
            session_name="myses",
            new_session=True,
            allowed_tools=["*"],
        )

        # session_name should have been prefixed with "cao-"
        args = mock_tmux.create_session.call_args
        assert args[0][0] == "cao-myses"


class TestDeleteTerminal:
    """Test delete_terminal coverage including pipe-pane and kill_window."""

    @patch("cli_agent_orchestrator.services.terminal_service.db_delete_terminal", return_value=True)
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.tmux_client")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_delete_terminal_full_path(self, mock_meta, mock_tmux, mock_pm, mock_db_del):
        """Delete should stop pipe-pane, kill window, cleanup provider, delete DB record."""
        from cli_agent_orchestrator.services.terminal_service import delete_terminal

        mock_meta.return_value = {"tmux_session": "ses", "tmux_window": "win"}

        result = delete_terminal("tid1")

        assert result is True
        mock_tmux.stop_pipe_pane.assert_called_once_with("ses", "win")
        mock_tmux.kill_window.assert_called_once_with("ses", "win")
        mock_pm.cleanup_provider.assert_called_once_with("tid1")

    @patch("cli_agent_orchestrator.services.terminal_service.db_delete_terminal", return_value=True)
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.tmux_client")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_delete_terminal_pipe_pane_failure_continues(
        self, mock_meta, mock_tmux, mock_pm, mock_db_del
    ):
        """Pipe-pane failure should be logged and not block deletion."""
        from cli_agent_orchestrator.services.terminal_service import delete_terminal

        mock_meta.return_value = {"tmux_session": "ses", "tmux_window": "win"}
        mock_tmux.stop_pipe_pane.side_effect = Exception("pipe error")

        result = delete_terminal("tid1")

        assert result is True
        mock_tmux.kill_window.assert_called_once()

    @patch("cli_agent_orchestrator.services.terminal_service.db_delete_terminal", return_value=True)
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.tmux_client")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_delete_terminal_kill_window_failure_continues(
        self, mock_meta, mock_tmux, mock_pm, mock_db_del
    ):
        """Kill-window failure should be logged and not block deletion."""
        from cli_agent_orchestrator.services.terminal_service import delete_terminal

        mock_meta.return_value = {"tmux_session": "ses", "tmux_window": "win"}
        mock_tmux.kill_window.side_effect = Exception("kill error")

        result = delete_terminal("tid1")

        assert result is True
        mock_pm.cleanup_provider.assert_called_once()

    @patch("cli_agent_orchestrator.services.terminal_service.db_delete_terminal")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.tmux_client")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_delete_terminal_db_failure_raises(self, mock_meta, mock_tmux, mock_pm, mock_db_del):
        """DB delete failure should propagate."""
        from cli_agent_orchestrator.services.terminal_service import delete_terminal

        mock_meta.return_value = {"tmux_session": "ses", "tmux_window": "win"}
        mock_db_del.side_effect = Exception("DB error")

        with pytest.raises(Exception, match="DB error"):
            delete_terminal("tid1")

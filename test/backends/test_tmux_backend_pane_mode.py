"""spawn_mode decides where a new terminal lands, and what a failure means."""

from unittest.mock import MagicMock

import pytest

from cli_agent_orchestrator.backends.base import TerminalBackendError
from cli_agent_orchestrator.backends.tmux_backend import TmuxBackend
from cli_agent_orchestrator.clients.tmux import PaneSpawnUnavailable


@pytest.fixture
def client():
    return MagicMock()


class TestSpawnModeRouting:
    def test_window_mode_is_the_default_and_never_splits(self, client):
        TmuxBackend(client=client).create_window("ses", "coder-3", "tid")

        client.create_window.assert_called_once()
        client.create_pane.assert_not_called()

    def test_pane_mode_splits_the_configured_window(self, client):
        backend = TmuxBackend(client=client, spawn_mode="pane", pane_window="fleet")

        backend.create_window("ses", "coder-3", "tid")

        client.create_window.assert_not_called()
        assert client.create_pane.call_args.args[:4] == ("ses", "fleet", "coder-3", "tid")


class TestFallbackIsNarrow:
    def test_a_full_host_window_falls_back_to_a_window(self, client):
        """tmux having no room is recoverable: the terminal still gets created."""
        client.create_pane.side_effect = PaneSpawnUnavailable("no space for new pane")
        backend = TmuxBackend(client=client, spawn_mode="pane")

        backend.create_window("ses", "coder-3", "tid")

        client.create_window.assert_called_once()

    def test_a_duplicate_name_does_not_fall_back(self, client):
        """Falling back here would create a second terminal under one name."""
        client.create_pane.side_effect = ValueError("Terminal 'coder-3' already exists")
        backend = TmuxBackend(client=client, spawn_mode="pane")

        with pytest.raises(TerminalBackendError):
            backend.create_window("ses", "coder-3", "tid")
        client.create_window.assert_not_called()


class TestWebAttach:
    def test_window_mode_target_stays_a_pure_function(self, client):
        backend = TmuxBackend(client=client)

        assert backend.prepare_web_attach("ses", "win") == [
            "tmux",
            "-u",
            "attach-session",
            "-t",
            "ses:win",
        ]
        client.attach_command.assert_not_called()

    def test_pane_mode_asks_tmux_where_the_terminal_is(self, client):
        backend = TmuxBackend(client=client, spawn_mode="pane")

        backend.prepare_web_attach("ses", "coder-3")

        client.attach_command.assert_called_once_with("ses", "coder-3")

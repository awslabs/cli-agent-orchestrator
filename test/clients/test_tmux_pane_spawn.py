"""A terminal may live as a pane among siblings, and stay addressable.

Window mode gives every terminal its own window, so the window's name is the
terminal's name. Pane mode puts several terminals in one window, where that
name no longer distinguishes them -- each pane carries its own mark instead.
These tests pin the resolution, so a message meant for one agent cannot be
delivered to whichever pane happens to be focused.
"""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def tmux():
    with patch("cli_agent_orchestrator.clients.tmux.libtmux") as mock_libtmux:
        mock_server = MagicMock()
        mock_libtmux.Server.return_value = mock_server

        from cli_agent_orchestrator.clients.tmux import TmuxClient

        client = TmuxClient()
        client.server = mock_server
        yield client


def marked_pane(mark, pane_id="%7"):
    pane = MagicMock()
    pane.show_option.return_value = mark
    pane.pane_id = pane_id
    return pane


def session_with(panes=(), window=None):
    session = MagicMock()
    session.windows.get.return_value = window
    session.panes = list(panes)
    return session


# ── resolution ───────────────────────────────────────────────────────


class TestResolvePane:
    def test_a_window_of_that_name_still_wins(self, tmux):
        """Window mode is untouched: the mark is never consulted."""
        window = MagicMock()
        session = session_with(panes=[marked_pane("other")], window=window)
        tmux.server.sessions.get.return_value = session

        assert tmux._resolve_pane(session, "ses", "win") is window.active_pane

    def test_falls_back_to_the_marked_pane(self, tmux):
        wanted = marked_pane("coder-3", "%4")
        session = session_with(panes=[marked_pane("reviewer-7", "%3"), wanted])
        tmux.server.sessions.get.return_value = session

        assert tmux._resolve_pane(session, "ses", "coder-3") is wanted

    def test_a_sibling_mark_is_not_accepted(self, tmux):
        """The failure this whole change exists to prevent."""
        session = session_with(panes=[marked_pane("reviewer-7", "%3")])
        tmux.server.sessions.get.return_value = session

        assert tmux._resolve_pane(session, "ses", "coder-3") is None

    def test_required_raises_when_neither_exists(self, tmux):
        session = session_with()
        tmux.server.sessions.get.return_value = session

        with pytest.raises(ValueError, match="Window 'coder-3' not found"):
            tmux._resolve_pane(session, "ses", "coder-3", required=True)


class TestSendTargetAddressesThePane:
    def test_marked_terminal_is_addressed_by_pane_id(self, tmux):
        session = session_with(panes=[marked_pane("coder-3", "%4")])
        tmux.server.sessions.get.return_value = session

        assert tmux._send_target("ses", "coder-3") == "%4"

    def test_window_terminal_keeps_the_window_target(self, tmux):
        session = session_with(window=MagicMock())
        tmux.server.sessions.get.return_value = session

        assert tmux._send_target("ses", "win") == "ses:win"


# ── creation ─────────────────────────────────────────────────────────


class TestCreatePane:
    def _session(self, tmux, existing_marks=()):
        host_window = MagicMock()
        session = session_with(panes=[marked_pane(m) for m in existing_marks], window=host_window)
        tmux.server.sessions.get.return_value = session
        return session, host_window

    def test_marks_the_new_pane_with_the_terminal_name(self, tmux, tmp_path):
        from cli_agent_orchestrator.clients.tmux import TERMINAL_MARK_OPTION

        _, host_window = self._session(tmux)
        new_pane = host_window.split.return_value

        result = tmux.create_pane("ses", "cao-agents", "coder-3", "tid", str(tmp_path))

        assert result == "coder-3"
        new_pane.set_option.assert_called_once_with(TERMINAL_MARK_OPTION, "coder-3")

    def test_rebalances_the_window(self, tmux, tmp_path):
        _, host_window = self._session(tmux)

        tmux.create_pane("ses", "cao-agents", "coder-3", "tid", str(tmp_path))

        host_window.select_layout.assert_called_once_with("tiled")

    def test_carries_the_terminal_id_into_the_pane_environment(self, tmux, tmp_path):
        _, host_window = self._session(tmux)

        tmux.create_pane("ses", "cao-agents", "coder-3", "tid-42", str(tmp_path))

        assert host_window.split.call_args.kwargs["environment"]["CAO_TERMINAL_ID"] == "tid-42"

    def test_refuses_a_name_already_marked(self, tmux, tmp_path):
        """Two panes with one mark would make every later lookup ambiguous."""
        _, host_window = self._session(tmux, existing_marks=["coder-3"])

        with pytest.raises(ValueError, match="already exists"):
            tmux.create_pane("ses", "cao-agents", "coder-3", "tid", str(tmp_path))
        host_window.split.assert_not_called()

    def test_absent_host_window_is_its_own_error(self, tmux, tmp_path):
        from cli_agent_orchestrator.clients.tmux import HostWindowMissing

        session = session_with()
        tmux.server.sessions.get.return_value = session

        with pytest.raises(HostWindowMissing):
            tmux.create_pane("ses", "cao-agents", "coder-3", "tid", str(tmp_path))


# ── teardown and attach ──────────────────────────────────────────────


class TestKillReachesThePane:
    def test_kills_the_marked_pane_not_the_window(self, tmux):
        pane = marked_pane("coder-3", "%4")
        tmux.server.sessions.get.return_value = session_with(panes=[pane])

        assert tmux.kill_window("ses", "coder-3") is True
        pane.kill.assert_called_once()

    def test_unknown_terminal_is_not_a_kill(self, tmux):
        pane = marked_pane("reviewer-7", "%3")
        tmux.server.sessions.get.return_value = session_with(panes=[pane])

        assert tmux.kill_window("ses", "coder-3") is False
        pane.kill.assert_not_called()


class TestAttachCommand:
    def test_selects_the_window_before_the_pane(self, tmux):
        """select-pane alone does not move the active window."""
        pane = marked_pane("coder-3", "%4")
        pane.window.window_id = "@2"
        tmux.server.sessions.get.return_value = session_with(panes=[pane])

        argv = tmux.attach_command("ses", "coder-3")

        assert argv.index("select-window") < argv.index("select-pane")
        assert argv[argv.index("select-window") + 2] == "@2"
        assert argv[argv.index("select-pane") + 2] == "%4"

    def test_window_terminal_attaches_as_before(self, tmux):
        tmux.server.sessions.get.return_value = session_with(window=MagicMock())

        assert tmux.attach_command("ses", "win") == [
            "tmux",
            "-u",
            "attach-session",
            "-t",
            "ses:win",
        ]

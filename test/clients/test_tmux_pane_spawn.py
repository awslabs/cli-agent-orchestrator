"""A terminal may live as a pane among siblings, and stay addressable.

Window mode gives every terminal its own window, so the window's name is the
terminal's name. Pane mode puts several terminals in one window, where that
name no longer distinguishes them -- each pane carries its own mark instead.
These tests pin the resolution, so a message meant for one agent cannot be
delivered to whichever pane happens to be focused.
"""

from unittest.mock import MagicMock, patch

import pytest
from libtmux.exc import LibTmuxException

from cli_agent_orchestrator.clients.tmux import TERMINAL_MARK_OPTION


@pytest.fixture
def tmux():
    with patch("cli_agent_orchestrator.clients.tmux.libtmux") as mock_libtmux:
        mock_server = MagicMock()
        mock_libtmux.Server.return_value = mock_server

        from cli_agent_orchestrator.clients.tmux import TmuxClient

        client = TmuxClient(pane_mode=True)
        client.server = mock_server
        yield client


@pytest.fixture
def window_mode_tmux():
    """A client in the default mode, which must ask tmux nothing to address a terminal."""
    with patch("cli_agent_orchestrator.clients.tmux.libtmux") as mock_libtmux:
        mock_server = MagicMock()
        mock_libtmux.Server.return_value = mock_server

        from cli_agent_orchestrator.clients.tmux import TmuxClient

        client = TmuxClient()
        client.server = mock_server
        yield client


def marked_pane(mark, pane_id="%7", inherited=None):
    """A pane, the mark it carries, and a mark its window would lend it.

    Models ``show-options``: without ``-A`` tmux lists only what this pane sets
    itself, and with ``-A`` it adds what the pane would inherit. A double that
    ignores the flag cannot see an implementation that asks for inheritance.

    ``show_option`` is refused outright, because libtmux raises there for an
    unmarked pane and converts the value for a marked one — the two behaviours
    that let the first version of this through.
    """
    pane = MagicMock()
    pane.pane_id = pane_id

    def cmd(*args):
        result = MagicMock()
        if mark is not None:
            result.stdout = [f"{TERMINAL_MARK_OPTION} {mark}"]
        elif "-A" in args and inherited is not None:
            result.stdout = [f"{TERMINAL_MARK_OPTION} {inherited}"]
        else:
            result.stdout = []
        return result

    pane.cmd.side_effect = cmd
    pane.show_option.side_effect = AssertionError(
        "show_option raises for an unmarked pane and converts the value it returns"
    )
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

    def test_an_unmarked_pane_does_not_break_the_scan(self, tmux):
        """The reported blocker: an ordinary host window's shell pane has no mark.

        Asking such a pane for the option by name raises, so a per-pane read
        aborted `create_pane` before the first split.
        """
        session = session_with(panes=[marked_pane(None, "%0"), marked_pane("coder-3", "%4")])
        tmux.server.sessions.get.return_value = session

        assert tmux._resolve_pane(session, "ses", "coder-3").pane_id == "%4"
        assert tmux._resolve_pane(session, "ses", "nobody") is None

    def test_a_mark_on_the_window_does_not_answer_for_its_panes(self, tmux):
        """A pane option falls back to its window, so an inherited mark would match all."""
        session = session_with(panes=[marked_pane(None, "%0", inherited="coder-3")])
        tmux.server.sessions.get.return_value = session

        assert tmux._resolve_pane(session, "ses", "coder-3") is None

    def test_a_lookup_that_fails_for_another_reason_stays_visible(self, tmux):
        """Only "this pane has no such option" means no match. Anything else is real."""
        pane = marked_pane("coder-3", "%4")
        pane.cmd.side_effect = LibTmuxException("server gone")
        session = session_with(panes=[pane])
        tmux.server.sessions.get.return_value = session

        with pytest.raises(LibTmuxException):
            tmux._resolve_pane(session, "ses", "coder-3")

    @pytest.mark.parametrize("name", ["123", "on", "off"])
    def test_a_name_libtmux_would_convert_still_resolves(self, tmux, name):
        """`validate_tmux_name` accepts these, and libtmux turns them into int/bool."""
        wanted = marked_pane(name, "%4")
        session = session_with(panes=[marked_pane(None, "%0"), wanted])
        tmux.server.sessions.get.return_value = session

        assert tmux._resolve_pane(session, "ses", name) is wanted

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

    def test_window_mode_asks_tmux_nothing(self, window_mode_tmux):
        """The send path built this string with zero tmux calls before panes existed."""
        assert window_mode_tmux._send_target("ses", "win") == "ses:win"
        window_mode_tmux.server.sessions.get.assert_not_called()

    def test_an_unreadable_listing_still_delivers(self, tmux):
        """A listing that will not read must not abort the send."""
        tmux.server.sessions.get.side_effect = LibTmuxException("server gone")

        assert tmux._send_target("ses", "coder-3") == "ses:coder-3"


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

    def test_an_absent_host_window_is_created(self, tmux, tmp_path):
        """Nothing in CAO makes this window, so the first pane terminal must."""
        from cli_agent_orchestrator.clients.tmux import TERMINAL_MARK_OPTION

        session = session_with()
        tmux.server.sessions.get.return_value = session
        new_pane = session.new_window.return_value.panes[0]

        assert tmux.create_pane("ses", "cao-agents", "coder-3", "tid", str(tmp_path)) == "coder-3"
        assert session.new_window.call_args.kwargs["window_name"] == "cao-agents"
        new_pane.set_option.assert_called_once_with(TERMINAL_MARK_OPTION, "coder-3")

    def test_a_full_host_window_asks_for_a_window_instead(self, tmux, tmp_path):
        """tmux refuses a split for want of space with a plain LibTmuxException."""
        from cli_agent_orchestrator.clients.tmux import PaneSpawnUnavailable

        _, host_window = self._session(tmux)
        host_window.split.side_effect = LibTmuxException("no space for new pane")

        with pytest.raises(PaneSpawnUnavailable):
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

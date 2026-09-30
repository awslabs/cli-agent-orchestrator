"""Exact-identity teardown for tmux: close THIS terminal, never a namesake.

A retained deferred-init tombstone shares its session (and often its window)
name with whatever replaced it. These tests pin the property that makes teardown
safe: the target is chosen by the ``@cao_terminal_id`` stamped at creation, so a
replacement that reuses the old name -- or the old pane mark -- is never touched,
and every uncertain answer stays UNKNOWN rather than reading as "already gone".
"""

from unittest.mock import MagicMock, call, patch

import pytest

from cli_agent_orchestrator.backends.base import TerminalCleanupOutcome
from cli_agent_orchestrator.clients.tmux import TERMINAL_ID_OPTION, TERMINAL_MARK_OPTION

SESSION = "cao-x"
WINDOW = "coder-3"


@pytest.fixture
def tmux():
    with patch("cli_agent_orchestrator.clients.tmux.libtmux") as mock_libtmux:
        mock_server = MagicMock()
        mock_libtmux.Server.return_value = mock_server

        from cli_agent_orchestrator.clients.tmux import TmuxClient

        client = TmuxClient()
        client.server = mock_server
        yield client


def _show_options(*lines):
    """A ``cmd`` side_effect that reports exactly these option lines."""

    def cmd(*args):
        result = MagicMock()
        result.returncode = 0
        result.stderr = []
        result.stdout = list(lines)
        return result

    return cmd


def pane(pane_id="%1", terminal_id=None, mark=None):
    """A pane carrying its own (non-inherited) scoped options."""
    obj = MagicMock()
    obj.pane_id = pane_id
    lines = []
    if terminal_id is not None:
        lines.append(f"{TERMINAL_ID_OPTION} {terminal_id}")
    if mark is not None:
        lines.append(f"{TERMINAL_MARK_OPTION} {mark}")
    obj.cmd.side_effect = _show_options(*lines)
    return obj


def window(name=WINDOW, window_id="@1", terminal_id=None, panes=()):
    obj = MagicMock()
    obj.name = name
    obj.window_id = window_id
    obj.panes = list(panes)
    lines = [f"{TERMINAL_ID_OPTION} {terminal_id}"] if terminal_id is not None else []
    obj.cmd.side_effect = _show_options(*lines)
    return obj


def session(name=SESSION, windows=()):
    obj = MagicMock()
    obj.name = name
    obj.windows = list(windows)
    return obj


def use(tmux, sess):
    tmux.server.sessions.get.return_value = sess


class SwitchableSession:
    """A session whose window listing can be made to fail parsing.

    Deliberately not a MagicMock: ``list(mock)`` iterates an empty default and
    would silently answer "no windows" where libtmux would raise. This reproduces
    the real mid-listing failure instead.
    """

    def __init__(self, windows=()):
        self.name = SESSION
        self._windows = list(windows)
        self.unreadable = False

    @property
    def windows(self):
        if self.unreadable:
            raise ValueError("zip() argument 2 is shorter than argument 1")
        return self._windows


class TestReplacementIsNeverTouched:
    @pytest.mark.parametrize("scope", ["window", "pane"])
    def test_unreadable_scoped_identity_is_unknown_even_after_rename(self, tmux, scope):
        renamed = window(name="renamed-live-window")
        target = renamed
        if scope == "pane":
            target = pane()
            renamed.panes = [target]
        failure = MagicMock()
        failure.returncode = 1
        failure.stderr = ["server temporarily unavailable"]
        failure.stdout = []
        target.cmd.side_effect = None
        target.cmd.return_value = failure
        use(tmux, session(windows=[renamed]))

        result = tmux.cleanup_terminal_exact("existing-terminal", SESSION, WINDOW, close=False)

        assert result.outcome is TerminalCleanupOutcome.UNKNOWN
        renamed.kill.assert_not_called()
        target.kill.assert_not_called()

    def test_a_replacement_reusing_the_window_name_is_not_closed(self, tmux):
        replacement = window(name=WINDOW, terminal_id="tid-new", panes=[pane("%9")])
        use(tmux, session(windows=[replacement]))

        result = tmux.cleanup_terminal_exact("tid-old", SESSION, WINDOW)

        assert result.outcome is TerminalCleanupOutcome.ABSENT
        replacement.kill.assert_not_called()

    def test_a_replacement_reusing_the_pane_mark_is_not_closed(self, tmux):
        replacement_pane = pane("%5", terminal_id="tid-new", mark=WINDOW)
        host = window(name="cao-agents", panes=[replacement_pane])
        use(tmux, session(windows=[host]))

        result = tmux.cleanup_terminal_exact("tid-old", SESSION, WINDOW)

        assert result.outcome is TerminalCleanupOutcome.ABSENT
        replacement_pane.kill.assert_not_called()


class TestAbsence:
    def test_absent_when_no_object_carries_the_id(self, tmux):
        other = window(name="someone-else", terminal_id="tid-other", panes=[pane("%2")])
        use(tmux, session(windows=[other]))

        result = tmux.cleanup_terminal_exact("tid-old", SESSION, WINDOW)

        assert result.outcome is TerminalCleanupOutcome.ABSENT

    def test_absent_when_the_session_is_gone(self, tmux):
        tmux.server.sessions.get.return_value = None

        result = tmux.cleanup_terminal_exact("tid-1", SESSION, WINDOW)

        assert result.outcome is TerminalCleanupOutcome.ABSENT

    def test_absent_when_the_label_is_free_and_no_id_matches(self, tmux):
        use(tmux, session(windows=[window(name="unrelated", terminal_id="tid-other")]))

        result = tmux.cleanup_terminal_exact("tid-1", SESSION, WINDOW)

        assert result.outcome is TerminalCleanupOutcome.ABSENT


class TestUnknown:
    def test_unknown_when_the_identity_scan_cannot_be_read(self, tmux):
        use(tmux, SwitchableSession())
        tmux.server.sessions.get.return_value.unreadable = True

        result = tmux.cleanup_terminal_exact("tid-1", SESSION, WINDOW)

        assert result.outcome is TerminalCleanupOutcome.UNKNOWN

    def test_unknown_when_more_than_one_object_carries_the_id(self, tmux):
        first = pane("%1", terminal_id="tid-1", mark=WINDOW)
        second = pane("%2", terminal_id="tid-1", mark="coder-4")
        host = window(name="cao-agents", panes=[first, second])
        use(tmux, session(windows=[host]))

        result = tmux.cleanup_terminal_exact("tid-1", SESSION, WINDOW)

        assert result.outcome is TerminalCleanupOutcome.UNKNOWN
        first.kill.assert_not_called()
        second.kill.assert_not_called()

    def test_unknown_when_the_label_is_held_by_an_object_with_no_terminal_id(self, tmux):
        """A pre-identity terminal is not proof of absence, so it is not torn down."""
        legacy = window(name=WINDOW, panes=[pane("%1")])
        use(tmux, session(windows=[legacy]))

        result = tmux.cleanup_terminal_exact("tid-legacy", SESSION, WINDOW)

        assert result.outcome is TerminalCleanupOutcome.UNKNOWN
        legacy.kill.assert_not_called()

    def test_unknown_when_the_close_cannot_be_confirmed(self, tmux):
        target = pane("%5", terminal_id="tid-1", mark=WINDOW)
        host = window(name="cao-agents", panes=[target])
        sess = SwitchableSession(windows=[host])
        use(tmux, sess)

        def kill_then_break_windows():
            # The pane goes, but the confirming listing cannot be read.
            sess.unreadable = True

        target.kill.side_effect = kill_then_break_windows

        result = tmux.cleanup_terminal_exact("tid-1", SESSION, WINDOW)

        assert result.outcome is TerminalCleanupOutcome.UNKNOWN
        target.kill.assert_called_once()


class TestClose:
    def test_closes_and_confirms_the_exact_pane(self, tmux):
        target = pane("%5", terminal_id="tid-1", mark=WINDOW)
        host = window(name="cao-agents", panes=[target])
        sess = session(windows=[host])
        use(tmux, sess)
        target.kill.side_effect = lambda: sess.windows[0].panes.remove(target)

        result = tmux.cleanup_terminal_exact("tid-1", SESSION, WINDOW)

        assert result.outcome is TerminalCleanupOutcome.DELETED
        target.kill.assert_called_once()

    def test_closes_and_confirms_the_exact_window(self, tmux):
        target = window(name=WINDOW, terminal_id="tid-1", panes=[pane("%1")])
        sess = session(windows=[target])
        use(tmux, sess)
        target.kill.side_effect = lambda: sess.windows.remove(target)

        result = tmux.cleanup_terminal_exact("tid-1", SESSION, WINDOW)

        assert result.outcome is TerminalCleanupOutcome.DELETED
        target.kill.assert_called_once()

    def test_reports_still_present_when_the_post_close_scan_still_sees_it(self, tmux):
        target = pane("%5", terminal_id="tid-1", mark=WINDOW)
        host = window(name="cao-agents", panes=[target])
        use(tmux, session(windows=[host]))

        result = tmux.cleanup_terminal_exact("tid-1", SESSION, WINDOW)

        assert result.outcome is TerminalCleanupOutcome.STILL_PRESENT
        target.kill.assert_called_once()

    def test_proof_only_reports_still_present_without_closing(self, tmux):
        target = pane("%5", terminal_id="tid-1", mark=WINDOW)
        host = window(name="cao-agents", panes=[target])
        use(tmux, session(windows=[host]))

        result = tmux.cleanup_terminal_exact("tid-1", SESSION, WINDOW, close=False)

        assert result.outcome is TerminalCleanupOutcome.STILL_PRESENT
        target.kill.assert_not_called()

    def test_proof_only_reports_absent_without_touching_anything(self, tmux):
        replacement = window(name=WINDOW, terminal_id="tid-new")
        use(tmux, session(windows=[replacement]))

        result = tmux.cleanup_terminal_exact("tid-old", SESSION, WINDOW, close=False)

        assert result.outcome is TerminalCleanupOutcome.ABSENT
        replacement.kill.assert_not_called()


class TestCreationStampsIdentity:
    def test_create_session_stamps_the_first_window(self, tmux, tmp_path):
        first = window(name=WINDOW)
        tmux.server.new_session.return_value = session(windows=[first])

        assert tmux.create_session(SESSION, WINDOW, "tid-1", str(tmp_path)) == WINDOW
        assert call(TERMINAL_ID_OPTION, "tid-1") in first.set_option.call_args_list

    def test_create_window_stamps_the_new_window(self, tmux, tmp_path):
        sess = session(windows=[])
        new_window = window(name=WINDOW)
        sess.new_window.return_value = new_window
        use(tmux, sess)

        assert tmux.create_window(SESSION, WINDOW, "tid-1", str(tmp_path)) == WINDOW
        assert call(TERMINAL_ID_OPTION, "tid-1") in new_window.set_option.call_args_list

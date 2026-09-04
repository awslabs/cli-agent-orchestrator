"""Tests for the libtmux listing boundary in TmuxClient (no real tmux required).

Covers the `libtmux.neo.parse_output` failure mode: libtmux >= 0.53.1 zips a fixed
125-name format list against a row's values with ``strict=True``, so any row
short of 125 values raises ``ValueError('zip() argument 2 is shorter than
argument 1')`` instead of degrading. A pane/session vanishing mid-listing does
exactly that, and it used to surface as a raw ValueError — indistinguishable
from this module's own "session not found" ValueError, and fatal to launches
and to the FIFO pipe-liveness watchdog.
"""

import inspect
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.clients.tmux import TmuxClient, TmuxLookupError

# The exact message CPython's strict zip produces, i.e. what libtmux's
# parse_output raises for a one-field-short row.
ZIP_ERROR = "zip() argument 2 is shorter than argument 1"


@pytest.fixture
def tmux():
    """Create a TmuxClient with a mocked libtmux.Server."""
    with patch("cli_agent_orchestrator.clients.tmux.libtmux") as mock_libtmux:
        mock_server = MagicMock()
        mock_libtmux.Server.return_value = mock_server
        client = TmuxClient()
        client.server = mock_server
        yield client


def parse_failure(*, times: int, then=None):
    """side_effect that raises the strict-zip ValueError ``times`` times."""
    return [ValueError(ZIP_ERROR)] * times + ([then] if then is not None else [])


def list_sessions_result(*names, returncode=0):
    """Stand-in for libtmux's ``tmux_cmd`` result for a ``list-sessions`` call.

    ``kill_session``'s verification poll asks ``session_exists_strict``, which
    runs its own ``list-sessions`` through ``server.cmd`` rather than reading
    ``server.sessions`` — so the parse failures these tests inject never reach
    it and it needs an answer of its own (#498).
    """
    result = MagicMock()
    result.returncode = returncode
    result.stdout = list(names)
    result.stderr = []
    return result


def make_window(pane=None):
    window = MagicMock()
    if pane is not None:
        window.active_pane = pane
        window.panes = [pane]
    return window


# ── upstream behavior this module wraps ──────────────────────────────


class TestUpstreamParseOutput:
    """Pin the real libtmux failure, when the installed libtmux has that path.

    Gated on ``parse_output``'s own source rather than on a version number: the
    strict zip landed in 0.53.1, the whole ``parse_output`` /
    ``get_output_format`` pair does not exist before 0.53.1, and
    ``get_output_format`` grew parameters after it. The pinned dependency range
    resolves a non-strict libtmux, so this normally skips — the wrapper itself
    is exercised against a simulated failure by every other test in this file.
    It exists so raising the bound over a still-broken release fails loudly here.
    """

    def test_one_field_short_row_raises_value_error(self):
        neo = pytest.importorskip("libtmux.neo")
        parse_output = getattr(neo, "parse_output", None)
        get_output_format = getattr(neo, "get_output_format", None)
        if parse_output is None or get_output_format is None:
            pytest.skip("this libtmux predates the neo.parse_output listing parser")
        if "strict=True" not in inspect.getsource(parse_output):
            pytest.skip("this libtmux zips non-strict: parse_output degrades instead of raising")

        # Every affected signature defaults to (list-panes, tmux 3.2a) and
        # 0.53.1's takes no arguments at all, so call with none for portability.
        separator = neo.FORMAT_SEPARATOR
        formats, _ = get_output_format()

        # A full-width row parses; one field short raises instead of degrading.
        parse_output(separator.join(["x"] * len(formats)) + separator)
        with pytest.raises(ValueError, match="shorter than"):
            parse_output(separator.join(["x"] * (len(formats) - 1)) + separator)


# ── _read_listing: retry once, then classify ─────────────────────────


class TestReadListing:
    def test_retries_once_and_succeeds(self):
        read = MagicMock(side_effect=parse_failure(times=1, then="ok"))

        assert TmuxClient._read_listing("list-panes", read) == "ok"
        assert read.call_count == 2

    def test_no_retry_when_first_read_succeeds(self):
        read = MagicMock(return_value="ok")

        assert TmuxClient._read_listing("list-panes", read) == "ok"
        assert read.call_count == 1

    def test_raises_lookup_error_after_second_failure(self):
        read = MagicMock(side_effect=parse_failure(times=2))

        with pytest.raises(TmuxLookupError) as excinfo:
            TmuxClient._read_listing("list-panes", read)

        # Bounded: exactly two attempts, never an unbounded loop.
        assert read.call_count == 2
        assert ZIP_ERROR in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, ValueError)

    def test_lookup_error_is_not_a_value_error(self):
        """The whole point: `except ValueError` for "not found" must not catch it."""
        assert not issubclass(TmuxLookupError, ValueError)
        assert issubclass(TmuxLookupError, RuntimeError)


# ── get_history: the watchdog / status-polling hot path ──────────────


class TestGetHistoryParseFailure:
    def _wire(self, tmux, session_side_effect):
        pane = MagicMock()
        pane.cmd.return_value = MagicMock(stdout=["line one", "line two"])
        window = make_window(pane)
        session = MagicMock()
        session.windows.get.return_value = window
        tmux.server.sessions.get.side_effect = session_side_effect(session)
        return pane

    def test_one_field_short_row_does_not_abort_the_call(self, tmux):
        """A transient parse failure retries and the probe still returns content."""
        self._wire(tmux, lambda session: parse_failure(times=1, then=session))

        assert tmux.get_history("ses", "win") == "line one\nline two"
        assert tmux.server.sessions.get.call_count == 2

    def test_persistent_parse_failure_raises_lookup_error(self, tmux):
        tmux.server.sessions.get.side_effect = parse_failure(times=2)

        with pytest.raises(TmuxLookupError):
            tmux.get_history("ses", "win")

    def test_parse_failure_in_pane_listing_raises_lookup_error(self, tmux):
        """`window.panes` is a listing too — not just `sessions`/`windows`."""
        window = MagicMock()
        type(window).panes = property(lambda self: (_ for _ in ()).throw(ValueError(ZIP_ERROR)))
        session = MagicMock()
        session.windows.get.return_value = window
        tmux.server.sessions.get.return_value = session

        with pytest.raises(TmuxLookupError):
            tmux.get_history("ses", "win")


# ── criterion 3: gone vs. could-not-look are distinguishable ─────────


class TestGenuineAbsenceVersusParseFailure:
    """Both outcomes pinned: a caller can always tell them apart."""

    def test_missing_session_raises_plain_value_error(self, tmux):
        tmux.server.sessions.get.return_value = None

        with pytest.raises(ValueError, match="Session 'ses' not found") as excinfo:
            tmux.get_history("ses", "win")
        assert not isinstance(excinfo.value, TmuxLookupError)

    def test_missing_window_raises_plain_value_error(self, tmux):
        session = MagicMock()
        session.windows.get.return_value = None
        tmux.server.sessions.get.return_value = session

        with pytest.raises(ValueError, match="Window 'win' not found") as excinfo:
            tmux.get_history("ses", "win")
        assert not isinstance(excinfo.value, TmuxLookupError)

    def test_parse_failure_raises_lookup_error_not_value_error(self, tmux):
        tmux.server.sessions.get.side_effect = parse_failure(times=2)

        with pytest.raises(TmuxLookupError) as excinfo:
            tmux.get_history("ses", "win")
        assert not isinstance(excinfo.value, ValueError)


# ── session_exists: must never answer "gone" on a parse failure ──────


class TestSessionExistsParseFailure:
    def test_retry_recovers_without_touching_the_cli(self, tmux):
        session = MagicMock()
        tmux.server.sessions.get.side_effect = parse_failure(times=1, then=session)

        with patch("cli_agent_orchestrator.clients.tmux.subprocess") as mock_subprocess:
            assert tmux.session_exists("ses") is True
        mock_subprocess.run.assert_not_called()

    def test_falls_back_to_has_session_for_a_live_session(self, tmux):
        """The bug's worst answer: reporting a LIVE session as gone."""
        tmux.server.sessions.get.side_effect = parse_failure(times=2)

        with patch("cli_agent_orchestrator.clients.tmux.subprocess") as mock_subprocess:
            mock_subprocess.run.return_value = MagicMock(returncode=0, stderr="")
            assert tmux.session_exists("ses") is True

        assert mock_subprocess.run.call_args[0][0] == [
            "tmux",
            "has-session",
            "-t",
            "=ses",
        ]

    def test_falls_back_to_has_session_for_a_dead_session(self, tmux):
        tmux.server.sessions.get.side_effect = parse_failure(times=2)

        with patch("cli_agent_orchestrator.clients.tmux.subprocess") as mock_subprocess:
            mock_subprocess.run.return_value = MagicMock(returncode=1, stderr="can't find session")
            assert tmux.session_exists("ses") is False

    def test_raises_when_neither_path_can_answer(self, tmux):
        tmux.server.sessions.get.side_effect = parse_failure(times=2)

        with patch("cli_agent_orchestrator.clients.tmux.subprocess") as mock_subprocess:
            mock_subprocess.run.side_effect = OSError("no tmux binary")
            with pytest.raises(TmuxLookupError):
                tmux.session_exists("ses")

    def test_unrelated_failure_still_returns_false(self, tmux):
        """Pre-existing behavior for non-parse errors is unchanged."""
        tmux.server.sessions.get.side_effect = RuntimeError("boom")

        assert tmux.session_exists("ses") is False


# ── criterion 4: no half-state left behind by a failed launch ────────


class TestCreateSessionHalfState:
    def test_parse_failure_in_new_session_removes_the_partial_session(self, tmux, tmp_path):
        """tmux may have created the session before libtmux failed to parse it.

        `launch_session` only learns a session was created once create_session
        RETURNS, so its own rollback never fires here — without this cleanup the
        retry is blocked by "Session already exists" with a dead pane.
        """
        tmux.server.new_session.side_effect = ValueError(ZIP_ERROR)

        with patch("cli_agent_orchestrator.clients.tmux.subprocess") as mock_subprocess:
            mock_subprocess.run.return_value = MagicMock(returncode=0, stderr="")
            with pytest.raises(TmuxLookupError):
                tmux.create_session("ses", "win", "tid1", str(tmp_path))

        assert mock_subprocess.run.call_args[0][0] == [
            "tmux",
            "kill-session",
            "-t",
            "=ses",
        ]

    def test_parse_failure_reading_the_new_window_removes_the_session(self, tmux, tmp_path):
        session = MagicMock()
        type(session).windows = property(lambda self: (_ for _ in ()).throw(ValueError(ZIP_ERROR)))
        tmux.server.new_session.return_value = session

        with patch("cli_agent_orchestrator.clients.tmux.subprocess") as mock_subprocess:
            mock_subprocess.run.return_value = MagicMock(returncode=0, stderr="")
            with pytest.raises(TmuxLookupError):
                tmux.create_session("ses", "win", "tid1", str(tmp_path))

        assert mock_subprocess.run.call_args[0][0] == [
            "tmux",
            "kill-session",
            "-t",
            "=ses",
        ]

    def test_transient_parse_failure_reading_the_new_window_still_succeeds(self, tmux, tmp_path):
        window = MagicMock()
        window.name = "win"
        session = MagicMock()
        windows = MagicMock()
        windows.__getitem__.side_effect = [ValueError(ZIP_ERROR), window]
        session.windows = windows
        tmux.server.new_session.return_value = session

        with patch("cli_agent_orchestrator.clients.tmux.subprocess") as mock_subprocess:
            assert tmux.create_session("ses", "win", "tid1", str(tmp_path)) == "win"
        mock_subprocess.run.assert_not_called()

    def test_other_post_creation_failure_rolls_the_session_back(self, tmux, tmp_path):
        """A None window name is not the parse bug, but still orphans a session."""
        window = MagicMock()
        window.name = None
        session = MagicMock()
        session.windows = [window]
        tmux.server.new_session.return_value = session

        with pytest.raises(ValueError, match="Window name is None"):
            tmux.create_session("ses", "win", "tid1", str(tmp_path))

        session.kill.assert_called_once()


# ── kill paths: "could not look" is not "nothing to kill" ────────────


class TestKillParseFailure:
    def test_kill_session_falls_back_to_the_cli(self, tmux):
        tmux.server.sessions.get.side_effect = parse_failure(times=2)
        # The CLI kill exiting 0 is not on its own a True: the verification poll
        # still has to see the session gone. Exit 0 with "ses" absent from the
        # name list is that authoritative "gone" (#498).
        tmux.server.cmd.return_value = list_sessions_result()

        with patch("cli_agent_orchestrator.clients.tmux.subprocess") as mock_subprocess:
            mock_subprocess.run.return_value = MagicMock(returncode=0, stderr="")
            assert tmux.kill_session("ses") is True

        assert mock_subprocess.run.call_args[0][0] == [
            "tmux",
            "kill-session",
            "-t",
            "=ses",
        ]

    def test_cli_fallback_still_has_to_confirm_the_session_is_gone(self, tmux, monkeypatch):
        """A dispatched CLI kill is not a confirmed kill.

        The fallback is exempt from the parse-prone listing, not from the
        confirmation contract: with the session still listed after the CLI kill
        exits 0, kill_session must report False so teardown leaves the registry
        intact instead of dropping rows for a live session (#498).
        """
        tmux.server.sessions.get.side_effect = parse_failure(times=2)
        tmux.server.cmd.return_value = list_sessions_result("ses")
        monkeypatch.setattr(tmux, "_KILL_SESSION_VERIFY_TIMEOUT_SECONDS", 0)

        with patch("cli_agent_orchestrator.clients.tmux.subprocess") as mock_subprocess:
            mock_subprocess.run.return_value = MagicMock(returncode=0, stderr="")
            assert tmux.kill_session("ses") is False

        mock_subprocess.run.assert_called_once()

    def test_kill_session_reports_absence_without_calling_the_cli(self, tmux):
        tmux.server.sessions.get.return_value = None

        with patch("cli_agent_orchestrator.clients.tmux.subprocess") as mock_subprocess:
            assert tmux.kill_session("ses") is False
        mock_subprocess.run.assert_not_called()

    def test_kill_window_falls_back_to_the_cli(self, tmux):
        session = MagicMock()
        session.windows.get.side_effect = parse_failure(times=2)
        tmux.server.sessions.get.return_value = session

        with patch("cli_agent_orchestrator.clients.tmux.subprocess") as mock_subprocess:
            mock_subprocess.run.return_value = MagicMock(returncode=0, stderr="")
            assert tmux.kill_window("ses", "win") is True

        assert mock_subprocess.run.call_args[0][0] == [
            "tmux",
            "kill-window",
            "-t",
            "=ses:win",
        ]

    def test_cli_fallback_reports_failure_rather_than_success(self, tmux):
        tmux.server.sessions.get.side_effect = parse_failure(times=2)

        with patch("cli_agent_orchestrator.clients.tmux.subprocess") as mock_subprocess:
            mock_subprocess.run.return_value = MagicMock(returncode=1, stderr="server not found")
            assert tmux.kill_session("ses") is False

    def test_cli_fallback_rejects_an_invalid_name(self, tmux):
        tmux.server.sessions.get.side_effect = parse_failure(times=2)

        with patch("cli_agent_orchestrator.clients.tmux.subprocess") as mock_subprocess:
            assert tmux.kill_session("bad:name") is False
        mock_subprocess.run.assert_not_called()


# ── listing methods must not degrade to "empty" ──────────────────────


class TestListingsDoNotDegradeToEmpty:
    def test_list_sessions_raises_instead_of_returning_empty(self, tmux):
        type(tmux.server).sessions = property(
            lambda self: (_ for _ in ()).throw(ValueError(ZIP_ERROR))
        )

        with pytest.raises(TmuxLookupError):
            tmux.list_sessions()

    def test_list_sessions_retry_recovers(self, tmux):
        session = MagicMock()
        session.name = "ses"
        session.attached_sessions = []
        calls = {"n": 0}

        def sessions(_self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError(ZIP_ERROR)
            return [session]

        type(tmux.server).sessions = property(sessions)

        assert tmux.list_sessions() == [{"id": "ses", "name": "ses", "status": "detached"}]

    def test_get_session_windows_raises_instead_of_returning_empty(self, tmux):
        session = MagicMock()
        type(session).windows = property(lambda self: (_ for _ in ()).throw(ValueError(ZIP_ERROR)))
        tmux.server.sessions.get.return_value = session

        with pytest.raises(TmuxLookupError):
            tmux.get_session_windows("ses")

    def test_get_session_windows_returns_empty_for_a_missing_session(self, tmux):
        tmux.server.sessions.get.return_value = None

        assert tmux.get_session_windows("ses") == []

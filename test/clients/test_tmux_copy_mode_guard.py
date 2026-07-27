"""Tests for the copy-mode guard's tmux primitives (cond-0178).

The two properties asserted structurally: the mode read and the exit
control run the same bound-server proof as every other control write
(``%3`` on the wrong tmux server is a stranger's pane), and the exit
control's exact argv is the one non-payload keystroke the managed write
boundary may ever emit — ``send-keys -X cancel``, to the exact pane, and
nothing else.
"""

from subprocess import CompletedProcess
from typing import Union
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.clients.tmux import TmuxClient, TmuxServerIdentityError
from cli_agent_orchestrator.services.control_input_contract import (
    REASON_SERVER_IDENTITY_MISMATCH,
    REASON_SERVER_IDENTITY_UNBOUND,
)

TMUX = "/usr/local/bin/tmux"

# The tmux server every call in this file is bound to, in realpath form.
SOCKET = "/private/tmp/tmux-501/cao-fixture.sock"
OTHER_SOCKET = "/private/tmp/tmux-501/somebody-elses.sock"

SERVER_FORMAT = "#{pane_id}\t#{socket_path}"


def _server_line(pane_id: str = "%40", socket: str = SOCKET) -> str:
    return f"{pane_id}\t{socket}"


def _ok(stdout: str = "") -> CompletedProcess:
    return CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _fail(stderr: str = "can't find pane: %40") -> CompletedProcess:
    return CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)


_Answer = Union[CompletedProcess, BaseException]


class _TmuxAnswers:
    """What mocked tmux replies, keyed by which question was asked.

    The three questions this module asks — probe the pane's server, read
    the pane's mode, send the exit control — are answered from separate
    slots, so a queued mode answer is never silently consumed by the probe.
    """

    def __init__(self) -> None:
        # The pane is on the bound server unless a test says otherwise.
        self.probe: _Answer = _ok(_server_line())
        self.mode: _Answer = _ok("1\n")
        self.cancel: _Answer = _ok()

    def __call__(self, argv, **kwargs):
        if "display-message" in argv:
            answer = self.mode
        elif "send-keys" in argv:
            answer = self.cancel
        else:
            answer = self.probe
        if isinstance(answer, BaseException):
            raise answer
        return answer


@pytest.fixture
def client():
    with patch("cli_agent_orchestrator.clients.tmux.libtmux"):
        yield TmuxClient()


@pytest.fixture
def answers():
    return _TmuxAnswers()


@pytest.fixture
def mock_subprocess(answers):
    with (
        patch("cli_agent_orchestrator.clients.tmux.subprocess") as mock,
        patch("cli_agent_orchestrator.clients.tmux.tmux_binary", return_value=TMUX),
    ):
        mock.run.side_effect = answers
        yield mock


def _all_argv(mock_subprocess) -> list:
    return [invocation[0][0] for invocation in mock_subprocess.run.call_args_list]


class TestPaneInCopyMode:
    """The detection read: proven 1, proven 0, or no reading at all."""

    def test_mode_one_is_true_and_the_argv_is_the_exact_pane_query(self, client, mock_subprocess):
        reading = client.pane_in_copy_mode("%40", expected_server_identity=SOCKET)

        assert reading is True
        assert _all_argv(mock_subprocess) == [
            # The server proof comes first: the mode that matters is the
            # one on the bound server.
            [TMUX, "list-panes", "-a", "-F", SERVER_FORMAT],
            [TMUX, "display-message", "-p", "-t", "%40", "#{pane_in_mode}"],
        ]

    def test_mode_zero_is_false(self, client, mock_subprocess, answers):
        answers.mode = _ok("0\n")

        assert client.pane_in_copy_mode("%40", expected_server_identity=SOCKET) is False

    def test_a_failed_query_is_no_reading_never_inactive(self, client, mock_subprocess, answers):
        """ "Could not look" must not become "proven not in copy mode"."""
        answers.mode = _fail()

        assert client.pane_in_copy_mode("%40", expected_server_identity=SOCKET) is None

    def test_an_empty_expansion_is_no_reading(self, client, mock_subprocess, answers):
        """A tmux that does not know the format expands it to nothing."""
        answers.mode = _ok("")

        assert client.pane_in_copy_mode("%40", expected_server_identity=SOCKET) is None

    def test_a_pane_on_another_server_is_refused_before_any_query(
        self, client, mock_subprocess, answers
    ):
        answers.probe = _ok(_server_line(socket=OTHER_SOCKET))

        with pytest.raises(TmuxServerIdentityError) as excinfo:
            client.pane_in_copy_mode("%40", expected_server_identity=SOCKET)

        assert excinfo.value.reason_code == REASON_SERVER_IDENTITY_MISMATCH
        # The refusal happened at the proof; no mode query was ever issued.
        assert _all_argv(mock_subprocess) == [[TMUX, "list-panes", "-a", "-F", SERVER_FORMAT]]

    def test_no_binding_refuses_the_read(self, client, mock_subprocess):
        """Passing None is 'I have no binding' — a statement, not an omission."""
        with pytest.raises(TmuxServerIdentityError) as excinfo:
            client.pane_in_copy_mode("%40", expected_server_identity=None)

        assert excinfo.value.reason_code == REASON_SERVER_IDENTITY_UNBOUND

    def test_an_invalid_pane_id_is_a_caller_error(self, client, mock_subprocess):
        with pytest.raises(ValueError):
            client.pane_in_copy_mode("cao:worker.0", expected_server_identity=SOCKET)


class TestSendCopyModeCancel:
    """The exit control: the sole non-payload keystroke, exact-pane only."""

    def test_the_argv_is_the_exit_control_and_nothing_else(self, client, mock_subprocess):
        accepted = client.send_copy_mode_cancel("%40", expected_server_identity=SOCKET)

        assert accepted is True
        assert _all_argv(mock_subprocess) == [
            [TMUX, "list-panes", "-a", "-F", SERVER_FORMAT],
            [TMUX, "send-keys", "-t", "%40", "-X", "cancel"],
        ]

    def test_a_rejected_exit_is_not_proven_not_an_exception(self, client, mock_subprocess, answers):
        answers.cancel = _fail("not in a mode")

        assert client.send_copy_mode_cancel("%40", expected_server_identity=SOCKET) is False

    def test_a_pane_on_another_server_gets_no_exit_control(self, client, mock_subprocess, answers):
        answers.probe = _ok(_server_line(socket=OTHER_SOCKET))

        with pytest.raises(TmuxServerIdentityError) as excinfo:
            client.send_copy_mode_cancel("%40", expected_server_identity=SOCKET)

        assert excinfo.value.reason_code == REASON_SERVER_IDENTITY_MISMATCH
        # Nothing was sent: the proof failed before the keystroke existed.
        assert _all_argv(mock_subprocess) == [[TMUX, "list-panes", "-a", "-F", SERVER_FORMAT]]

    def test_no_binding_refuses_the_exit(self, client, mock_subprocess):
        with pytest.raises(TmuxServerIdentityError) as excinfo:
            client.send_copy_mode_cancel("%40", expected_server_identity=None)

        assert excinfo.value.reason_code == REASON_SERVER_IDENTITY_UNBOUND
        # The probe ran; the keystroke never did.
        assert _all_argv(mock_subprocess) == [[TMUX, "list-panes", "-a", "-F", SERVER_FORMAT]]

    def test_an_invalid_pane_id_is_a_caller_error(self, client, mock_subprocess):
        with pytest.raises(ValueError):
            client.send_copy_mode_cancel("worker-1", expected_server_identity=SOCKET)

"""Tests for the literal, bracket-free control-input primitives.

Three properties are asserted structurally rather than by sampling: the
control write path emits no paste buffer and no bracketed-paste sentinel
for *any* input, identity is never resolved through a tmux ``-t`` target
(which silently falls back to a different pane), and no byte is written
until the target pane has been proven to sit on the tmux server the
caller named.
"""

import logging
import os
import time
from pathlib import Path
from subprocess import CompletedProcess
from typing import List, Optional, Union
from unittest.mock import call, patch

import pytest

from cli_agent_orchestrator.clients import tmux as tmux_module
from cli_agent_orchestrator.clients.tmux import (
    TMUX_CALL_TIMEOUT_SECONDS,
    TmuxClient,
    TmuxLiteralSendError,
    TmuxServerIdentityError,
)
from cli_agent_orchestrator.services import control_input_contract
from cli_agent_orchestrator.services.control_input_contract import (
    REASON_SERVER_IDENTITY_MISMATCH,
    REASON_SERVER_IDENTITY_UNBOUND,
    REASON_SERVER_IDENTITY_UNREADABLE,
)

TMUX = "/usr/local/bin/tmux"

# The tmux server every write in this file is bound to.  Written in its
# realpath form so a test asserting on it is asserting on the value the
# client actually compares, not on one a normalisation step would change.
SOCKET = "/private/tmp/tmux-501/cao-fixture.sock"
OTHER_SOCKET = "/private/tmp/tmux-501/somebody-elses.sock"

PANE_FORMAT = (
    "#{pane_id}\t#{window_id}\t#{session_id}\t#{pane_pid}\t"
    "#{bracket_paste_flag}\t#{pane_dead}\t#{socket_path}\t"
    "#{session_name}\t#{window_name}"
)

# The narrow query the write primitive makes immediately before its first
# byte.  Deliberately not the full record: the writer boundary asks one
# question and must not depend on fields it would then ignore.
SERVER_FORMAT = "#{pane_id}\t#{socket_path}"


def _pane_line(
    pane_id: str = "%263",
    window_id: str = "@261",
    session_id: str = "$7",
    pane_pid: str = "74654",
    bracket: str = "1",
    dead: str = "0",
    socket: str = SOCKET,
    session: str = "cao-1a2b3c4d",
    window: str = "claude-9f8e",
) -> str:
    return "\t".join(
        [pane_id, window_id, session_id, pane_pid, bracket, dead, socket, session, window]
    )


def _server_line(pane_id: str = "%263", socket: str = SOCKET) -> str:
    return f"{pane_id}\t{socket}"


def _ok(stdout: str = "") -> CompletedProcess:
    return CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _fail(stderr: str = "can't find pane: %999999") -> CompletedProcess:
    return CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)


_Answer = Union[CompletedProcess, BaseException]


class _TmuxAnswers:
    """What mocked tmux replies, keyed by which question was asked.

    The three questions this module's code asks — enumerate every pane,
    probe one pane's server, write to a pane — are answered from three
    separate slots rather than from one queue.  A test that queues two
    write outcomes is describing two writes; letting the server probe
    silently consume the first slot would make every such test assert
    something other than what it reads like.
    """

    def __init__(self) -> None:
        # The pane is on the bound server unless a test says otherwise,
        # so a test about argv is not also a test about identity.
        self.probe: _Answer = _ok(_server_line())
        self.panes: _Answer = _ok(_pane_line())
        # None means "every write succeeds"; a list is consumed in order
        # and runs out into success.
        self.writes: Optional[List[_Answer]] = None

    def __call__(self, argv, **kwargs):
        if "send-keys" in argv:
            queued = self.writes
            answer: _Answer = _ok() if not queued else queued.pop(0)
        elif SERVER_FORMAT in argv:
            answer = self.probe
        else:
            answer = self.panes
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


@pytest.fixture
def aliased_socket(tmp_path):
    """``(canonical, aliased)`` — one socket path spelled two ways.

    A symlinked directory somewhere in a socket's path is the ordinary
    case, not a curiosity: macOS reaches every ``/tmp`` socket through
    one.  The two spellings must compare equal, or a write would be
    refused to the very server it was bound to.

    Built here rather than borrowed from the host.  Naming the platform's
    own alias makes the property unprovable anywhere that alias does not
    exist — on Linux ``/tmp`` and ``/private/tmp`` are simply two
    different paths, so the assertion was false on the one platform CI
    runs.  A real directory with a real symlink beside it holds
    everywhere.
    """
    base = Path(os.path.realpath(tmp_path))
    real = base / "tmux-real"
    real.mkdir()
    alias = base / "tmux-alias"
    alias.symlink_to(real, target_is_directory=True)
    canonical = str(real / "cao-fixture.sock")
    aliased = str(alias / "cao-fixture.sock")
    # Asserted, not assumed.  Were the two spellings ever one string, or
    # were they to resolve apart, every test below would pass while
    # proving nothing at all about normalisation.
    assert aliased != canonical
    assert os.path.realpath(aliased) == canonical
    return canonical, aliased


def _all_argv(mock_subprocess) -> list[list[str]]:
    return [invocation[0][0] for invocation in mock_subprocess.run.call_args_list]


def _write_argv(mock_subprocess) -> list[list[str]]:
    """Only the writes.

    The server probe that precedes them is a read, and is asserted in
    :class:`TestSendLiteralLineChecksTheServer` where it is the subject
    rather than the setup.
    """
    return [argv for argv in _all_argv(mock_subprocess) if "send-keys" in argv]


def _send(client, pane_id: str = "%263", text: str = "/compact", **kwargs) -> int:
    """A write bound to :data:`SOCKET` unless the test names another.

    Every call in this file goes through here so that ``expected_server_identity``
    is always stated.  It is never defaulted at the call site, because the
    signature under test deliberately refuses to default it: a helper that
    quietly supplied one would reintroduce exactly the omission §24.7 exists
    to make impossible.
    """
    kwargs.setdefault("expected_server_identity", SOCKET)
    return client.send_literal_line(pane_id, text, **kwargs)


class TestSendLiteralLineArgv:
    """The exact argv is the contract: text as literal bytes, Enter as a key."""

    def test_text_then_explicit_enter(self, client, mock_subprocess):
        _send(client, "%263", "/compact")

        assert mock_subprocess.run.call_args_list == [
            # The server probe comes first and is part of the contract:
            # the write is not permitted to be the invocation that finds
            # out which server it reached.  Every call is bounded by the
            # shared tmux-call timeout so a hung call can never hold the
            # pane lease forever.
            call(
                [TMUX, "list-panes", "-a", "-F", SERVER_FORMAT],
                capture_output=True,
                text=True,
                check=False,
                timeout=TMUX_CALL_TIMEOUT_SECONDS,
            ),
            call(
                [TMUX, "send-keys", "-t", "%263", "-l", "--", "/compact"],
                capture_output=True,
                text=True,
                check=False,
                timeout=TMUX_CALL_TIMEOUT_SECONDS,
            ),
            call(
                [TMUX, "send-keys", "-t", "%263", "Enter"],
                capture_output=True,
                text=True,
                check=False,
                timeout=TMUX_CALL_TIMEOUT_SECONDS,
            ),
        ]

    def test_printable_text_is_sent_verbatim(self, client, mock_subprocess):
        message = """He said "hello" and ran `cmd` with $VAR and a \\n backslash"""
        _send(client, "%263", message, submit=False)

        assert _write_argv(mock_subprocess) == [
            [TMUX, "send-keys", "-t", "%263", "-l", "--", message]
        ]

    def test_dash_leading_text_stays_text(self, client, mock_subprocess):
        """'--' must precede the payload or tmux parses '-l' as an option."""
        _send(client, "%263", "-l --literal", submit=False)

        argv = _write_argv(mock_subprocess)[0]
        assert argv[-2] == "--"
        assert argv[-1] == "-l --literal"

    def test_no_submit_omits_enter(self, client, mock_subprocess):
        _send(client, "%263", "hello", submit=False)

        assert _write_argv(mock_subprocess) == [
            [TMUX, "send-keys", "-t", "%263", "-l", "--", "hello"]
        ]

    def test_empty_text_with_submit_sends_only_enter(self, client, mock_subprocess):
        _send(client, "%263", "", submit=True)

        assert _write_argv(mock_subprocess) == [[TMUX, "send-keys", "-t", "%263", "Enter"]]

    def test_long_text_is_chunked_into_exact_slices(self, client, mock_subprocess):
        text = "".join(chr(ord("a") + (index % 26)) for index in range(2500))
        _send(client, "%263", text, submit=True)

        argv = _write_argv(mock_subprocess)
        assert len(argv) == 4  # 1024 + 1024 + 452 + Enter
        assert argv[0][-1] == text[0:1024]
        assert argv[1][-1] == text[1024:2048]
        assert argv[2][-1] == text[2048:2500]
        assert argv[3] == [TMUX, "send-keys", "-t", "%263", "Enter"]
        assert "".join(item[-1] for item in argv[:3]) == text

    def test_target_is_always_a_pane_id(self, client, mock_subprocess):
        """A session:window target can resolve to a pane the caller never named."""
        _send(client, "%263", "/compact")

        for argv in _write_argv(mock_subprocess):
            target = argv[argv.index("-t") + 1]
            assert target == "%263"
            assert ":" not in target

    def test_uses_the_resolved_absolute_tmux_binary(self, client, mock_subprocess):
        _send(client, "%263", "/compact")

        assert all(argv[0] == TMUX for argv in _all_argv(mock_subprocess))


class TestSendLiteralLineChecksTheServer:
    """A pane id is a target only once its tmux server is named (§24.7).

    The same ``%263`` exists on every server on the host and names an
    unrelated pane on each, so the writer boundary refuses anything it
    cannot prove sits on the server the caller bound.  Every refusal here
    must also prove that *nothing* was written: a refusal that might have
    written is an ambiguous outcome, and the whole value of this one is
    that it is not.
    """

    def test_the_server_is_proven_before_the_first_byte(self, client, mock_subprocess):
        _send(client, "%263", "/compact")

        first = _all_argv(mock_subprocess)[0]
        assert first == [TMUX, "list-panes", "-a", "-F", SERVER_FORMAT]

    def test_the_probe_never_targets_a_pane(self, client, mock_subprocess):
        """``display-message -t`` answers for a different pane with status 0.

        Asking through a ``-t`` target is how a lookup ends up reporting
        the socket of a server the pane is not on — which would make this
        check confirm the very error it exists to catch.
        """
        _send(client, "%263", "/compact")

        probe = _all_argv(mock_subprocess)[0]
        assert "display-message" not in probe
        assert "-t" not in probe

    def test_an_unbound_caller_is_refused(self, client, mock_subprocess):
        with pytest.raises(TmuxServerIdentityError) as excinfo:
            client.send_literal_line("%263", "/compact", expected_server_identity=None)

        assert excinfo.value.reason_code == REASON_SERVER_IDENTITY_UNBOUND
        assert excinfo.value.chunks_sent == 0
        assert excinfo.value.enter_attempted is False
        assert _write_argv(mock_subprocess) == []

    def test_omitting_the_binding_is_not_possible(self, client, mock_subprocess):
        """No default, so the omission that caused the incident cannot compile.

        The guarded failure was a helper that inherited a default and
        wrote into live composers on a server it never named.  Refusing at
        runtime would still leave that call site writable; refusing at the
        signature means it cannot be written at all.
        """
        with pytest.raises(TypeError, match="expected_server_identity"):
            client.send_literal_line("%263", "/compact")  # type: ignore[call-arg]

        assert mock_subprocess.run.call_count == 0

    def test_a_pane_on_another_server_is_refused(self, client, mock_subprocess, answers):
        """The colliding-pane-id case, at the boundary that would write."""
        answers.probe = _ok(_server_line("%263", OTHER_SOCKET))

        with pytest.raises(TmuxServerIdentityError) as excinfo:
            _send(client, "%263", "/compact")

        assert excinfo.value.reason_code == REASON_SERVER_IDENTITY_MISMATCH
        assert excinfo.value.bound == SOCKET
        assert excinfo.value.observed == OTHER_SOCKET
        assert excinfo.value.chunks_sent == 0
        assert _write_argv(mock_subprocess) == []

    @pytest.mark.parametrize(
        "probe",
        [
            # The pane is not on the server this process reaches at all.
            _ok(_server_line("%999", SOCKET)),
            # The server could not be read.
            _fail("no server running"),
            # A tmux too old to know #{socket_path} expands it to nothing.
            _ok("%263\t"),
            # tmux itself is gone.
            OSError("tmux vanished"),
        ],
    )
    def test_an_unprovable_server_is_refused(self, client, mock_subprocess, answers, probe):
        """Absent, unreadable and unexpanded are one answer: not proven.

        They differ in why the identity could not be read, and in none of
        them can the pane be shown to be on the bound server — which is
        the only question the boundary asks.
        """
        answers.probe = probe

        with pytest.raises(TmuxServerIdentityError) as excinfo:
            _send(client, "%263", "/compact")

        assert excinfo.value.reason_code == REASON_SERVER_IDENTITY_UNREADABLE
        assert excinfo.value.observed is None
        assert excinfo.value.chunks_sent == 0
        assert _write_argv(mock_subprocess) == []

    def test_the_refusal_is_not_a_partial_write(self, client, mock_subprocess, answers):
        """Callers read ``chunks_sent`` off whichever failure they caught.

        :class:`TmuxServerIdentityError` is deliberately not a
        :class:`TmuxLiteralSendError` — that one means bytes may have
        landed, this one means they provably did not — so a caller that
        collapsed the two would report an ambiguous outcome for the one
        failure that is unambiguous.
        """
        answers.probe = _ok(_server_line("%263", OTHER_SOCKET))

        with pytest.raises(TmuxServerIdentityError) as excinfo:
            _send(client, "%263", "/compact")

        assert not isinstance(excinfo.value, TmuxLiteralSendError)

    def test_a_differently_spelled_path_is_the_same_server(
        self, client, mock_subprocess, answers, aliased_socket
    ):
        """A symlinked directory in the path does not make it another server.

        The write is bound through the alias and the pane answers with
        the canonical spelling.  Both name one socket, so comparing them
        as strings would refuse a write to the very server it was bound
        to.
        """
        canonical, aliased = aliased_socket
        answers.probe = _ok(_server_line("%263", canonical))

        _send(client, "%263", "/compact", expected_server_identity=aliased)

        assert len(_write_argv(mock_subprocess)) == 2

    def test_the_server_is_read_exactly_once(self, client, mock_subprocess):
        """A second reading could answer differently.

        An error reporting a reading other than the one it refused on
        would be evidence of nothing, and a write permitted by a reading
        it then re-took would be gated on neither.
        """
        _send(client, "%263", "x" * 2500, submit=True)

        probes = [argv for argv in _all_argv(mock_subprocess) if "list-panes" in argv]
        assert len(probes) == 1


class TestSendLiteralLineEmitsNoSentinels:
    """No pane may receive bracketed-paste bytes on the control path."""

    @pytest.mark.parametrize(
        "text",
        [
            "/compact",
            "plain text",
            "-leading-dash",
            "x" * 3000,
            "unicode: café — ✓",
            "",
        ],
    )
    def test_never_pastes_and_never_brackets(self, client, mock_subprocess, text):
        _send(client, "%263", text, submit=True)

        for invocation in mock_subprocess.run.call_args_list:
            argv = invocation[0][0]
            assert "load-buffer" not in argv
            assert "paste-buffer" not in argv
            assert "set-buffer" not in argv
            assert not any("\x1b[200~" in item or "\x1b[201~" in item for item in argv)
            # No payload is ever handed to tmux over stdin, so there is no
            # buffer for a sentinel to be wrapped around.
            assert "input" not in invocation[1]

    @pytest.mark.parametrize(
        "sentinel",
        [
            "\x1b[200~",
            "\x1b[201~",
            # The single-byte C1 spelling of the same two sequences.  A
            # terminal in 8-bit mode reads them identically, so screening
            # only the ESC form leaves a working way to smuggle the
            # framing through.
            "\x9b200~",
            "\x9b201~",
        ],
    )
    def test_sentinel_bearing_text_is_rejected_before_any_write(
        self, client, mock_subprocess, sentinel
    ):
        with pytest.raises(ValueError, match="bracketed-paste"):
            _send(client, "%263", f"before{sentinel}after")

        assert mock_subprocess.run.call_count == 0


class TestSendLiteralLineRejects:
    """Every refusal happens before the first write, so nothing is emitted."""

    @pytest.mark.parametrize("char", ["\n", "\r", "\x1b", "\x9b"])
    def test_rejects_control_characters(self, client, mock_subprocess, char):
        with pytest.raises(ValueError, match="must not contain"):
            _send(client, "%263", f"line one{char}line two")

        assert mock_subprocess.run.call_count == 0

    @pytest.mark.parametrize(
        "pane_id",
        [
            "sess:win",
            "cao-1a2b:claude-9f8e",
            "%",
            "%abc",
            "@261",
            "-t",
            "%263;kill-server",
            "%263 %264",
            "%263\n",
            "",
            "%12345678901",
        ],
    )
    def test_rejects_non_pane_id_targets(self, client, mock_subprocess, pane_id):
        with pytest.raises(ValueError, match="Invalid pane_id"):
            _send(client, pane_id, "/compact")

        assert mock_subprocess.run.call_count == 0

    def test_rejects_a_write_that_would_emit_nothing(self, client, mock_subprocess):
        with pytest.raises(ValueError, match="emit nothing"):
            _send(client, "%263", "", submit=False)

        assert mock_subprocess.run.call_count == 0

    def test_payload_screening_precedes_the_server_probe(self, client, mock_subprocess):
        """Whether these bytes may exist is settled before where they go.

        The two questions are independent, and asking tmux anything about
        a payload that may never be written would make a rejected control
        observable on the server it was aimed at.
        """
        with pytest.raises(ValueError):
            _send(client, "%263", "line\nbreak")

        assert _all_argv(mock_subprocess) == []


class TestSendLiteralLineFailures:
    """A failed write reports how much of it may already have landed."""

    def test_first_write_failure_reports_zero_chunks(self, client, mock_subprocess, answers):
        answers.writes = [_fail()]

        with pytest.raises(TmuxLiteralSendError) as excinfo:
            _send(client, "%263", "/compact")

        assert excinfo.value.chunks_sent == 0
        assert excinfo.value.enter_attempted is False
        assert "can't find pane" in str(excinfo.value)

    def test_later_chunk_failure_reports_completed_chunks(self, client, mock_subprocess, answers):
        answers.writes = [_ok(), _fail("server exited")]

        with pytest.raises(TmuxLiteralSendError) as excinfo:
            _send(client, "%263", "y" * 2000, submit=True)

        assert excinfo.value.chunks_sent == 1
        assert excinfo.value.enter_attempted is False

    def test_enter_failure_is_flagged_as_possibly_submitted(self, client, mock_subprocess, answers):
        answers.writes = [_ok(), _fail()]

        with pytest.raises(TmuxLiteralSendError) as excinfo:
            _send(client, "%263", "/compact", submit=True)

        assert excinfo.value.chunks_sent == 1
        assert excinfo.value.enter_attempted is True

    def test_os_error_is_wrapped_not_leaked(self, client, mock_subprocess, answers):
        answers.writes = [OSError("tmux vanished")]

        with pytest.raises(TmuxLiteralSendError) as excinfo:
            _send(client, "%263", "/compact")

        assert excinfo.value.chunks_sent == 0


class TestSendLiteralLineLogRedaction:
    """Control text is caller-supplied and stays out of INFO logs."""

    def test_info_log_omits_payload(self, client, mock_subprocess, caplog):
        secret = "/model sk-do-not-log-this"
        with caplog.at_level(logging.INFO, logger="cli_agent_orchestrator.clients.tmux"):
            _send(client, "%263", secret)

        info_text = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.INFO)
        assert "sk-do-not-log-this" not in info_text
        assert "%263" in info_text
        assert "text length" in info_text

    def test_debug_log_retains_payload(self, client, mock_subprocess, caplog):
        with caplog.at_level(logging.DEBUG, logger="cli_agent_orchestrator.clients.tmux"):
            _send(client, "%263", "visible-at-debug")

        debug_text = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG)
        assert "visible-at-debug" in debug_text


class TestObservePaneServerIdentity:
    """The one question the writer boundary asks, answered on its own."""

    def test_argv_is_exact_and_narrow(self, client, mock_subprocess):
        client.observe_pane_server_identity("%263")

        assert _all_argv(mock_subprocess) == [[TMUX, "list-panes", "-a", "-F", SERVER_FORMAT]]

    def test_it_reports_the_socket_of_the_named_pane(self, client, mock_subprocess, answers):
        answers.probe = _ok(
            "\n".join(
                [
                    _server_line("%100", OTHER_SOCKET),
                    _server_line("%263", SOCKET),
                    _server_line("%400", OTHER_SOCKET),
                ]
            )
        )

        assert client.observe_pane_server_identity("%263") == SOCKET

    def test_a_pane_that_is_not_listed_is_unknown(self, client, mock_subprocess, answers):
        answers.probe = _ok(_server_line("%100"))

        assert client.observe_pane_server_identity("%263") is None

    def test_a_prefix_is_not_a_match(self, client, mock_subprocess, answers):
        """``%26`` and ``%263`` are different panes, not a near miss."""
        answers.probe = _ok(_server_line("%2630"))

        assert client.observe_pane_server_identity("%263") is None

    def test_a_malformed_pane_id_is_never_asked_about(self, client, mock_subprocess):
        assert client.observe_pane_server_identity("%263;kill-server") is None
        assert mock_subprocess.run.call_count == 0

    @pytest.mark.parametrize(
        "probe",
        [_fail("no server running"), OSError("tmux vanished"), _ok("%263\t"), _ok("")],
    )
    def test_an_unreadable_server_is_unknown_not_empty(
        self, client, mock_subprocess, answers, probe
    ):
        answers.probe = probe

        assert client.observe_pane_server_identity("%263") is None

    def test_the_reported_socket_is_canonical(
        self, client, mock_subprocess, answers, aliased_socket
    ):
        """Reported in the form the comparison uses, so both sides agree."""
        canonical, aliased = aliased_socket
        answers.probe = _ok(_server_line("%263", aliased))

        assert client.observe_pane_server_identity("%263") == canonical


class TestPaneControlIdentityLookup:
    """Identity comes from an enumeration filtered in Python, never a -t target."""

    def test_enumeration_argv_is_exact(self, client, mock_subprocess):
        client.pane_control_identity(pane_id="%263")

        assert _all_argv(mock_subprocess) == [[TMUX, "list-panes", "-a", "-F", PANE_FORMAT]]

    def test_never_targets_a_pane_and_never_uses_display_message(self, client, mock_subprocess):
        client.pane_control_identity(session_name="cao-1a2b3c4d", window_name="claude-9f8e")

        for argv in _all_argv(mock_subprocess):
            assert "display-message" not in argv
            assert "-t" not in argv

    def test_resolves_by_pane_id(self, client, mock_subprocess, answers):
        answers.panes = _ok(
            "\n".join([_pane_line(pane_id="%100"), _pane_line(), _pane_line(pane_id="%400")])
        )

        identity = client.pane_control_identity(pane_id="%263")

        assert identity is not None
        assert identity.pane_id == "%263"
        assert identity.window_id == "@261"
        assert identity.session_id == "$7"
        assert identity.pane_pid == 74654
        assert identity.session_name == "cao-1a2b3c4d"
        assert identity.window_name == "claude-9f8e"
        assert identity.bracketed_paste_proven is True
        assert identity.dead is False
        assert identity.server_socket_path == SOCKET

    def test_resolves_by_session_and_window(self, client, mock_subprocess, answers):
        answers.panes = _ok("\n".join([_pane_line(pane_id="%100", window="other"), _pane_line()]))

        identity = client.pane_control_identity(
            session_name="cao-1a2b3c4d", window_name="claude-9f8e"
        )

        assert identity is not None
        assert identity.pane_id == "%263"

    def test_deadline_bound_window_identity_uses_direct_bounded_enumeration(
        self, client, mock_subprocess, answers
    ):
        answers.panes = _ok(_pane_line())
        deadline = time.monotonic() + 1.0

        identity = client.window_identity(
            "cao-1a2b3c4d",
            "claude-9f8e",
            deadline_monotonic=deadline,
        )

        assert identity == {
            "pane_id": "%263",
            "window_id": "@261",
            "session_id": "$7",
            "pane_pid": "74654",
            "server_socket_path": SOCKET,
        }
        assert _all_argv(mock_subprocess) == [[TMUX, "list-panes", "-a", "-F", PANE_FORMAT]]
        timeout = mock_subprocess.run.call_args.kwargs["timeout"]
        assert 0 < timeout <= 1.0
        client.server.sessions.get.assert_not_called()

    def test_deadline_bound_window_existence_preserves_unreadable_server(
        self, client, mock_subprocess, answers
    ):
        answers.panes = _fail("server unavailable")

        with pytest.raises(RuntimeError, match="unavailable or unreadable"):
            client.window_exists(
                "cao-1a2b3c4d",
                "claude-9f8e",
                deadline_monotonic=time.monotonic() + 1.0,
            )

    def test_unknown_pane_is_absent_not_guessed(self, client, mock_subprocess, answers):
        answers.panes = _ok(_pane_line(pane_id="%100"))

        assert client.pane_control_identity(pane_id="%263") is None

    def test_multi_pane_window_is_ambiguous(self, client, mock_subprocess, answers):
        """A window with two panes has no single control target."""
        answers.panes = _ok(
            "\n".join([_pane_line(pane_id="%263"), _pane_line(pane_id="%264", window_id="@261")])
        )

        assert (
            client.pane_control_identity(session_name="cao-1a2b3c4d", window_name="claude-9f8e")
            is None
        )

    def test_failed_enumeration_is_unknown_not_empty(self, client, mock_subprocess, answers):
        answers.panes = _fail("no server running")

        assert client.list_pane_control_identities() is None
        assert client.pane_control_identity(pane_id="%263") is None

    def test_os_error_is_unknown_not_empty(self, client, mock_subprocess, answers):
        answers.panes = OSError("tmux vanished")

        assert client.list_pane_control_identities() is None

    def test_unresolvable_binary_is_unknown_not_empty(self, client):
        with patch("cli_agent_orchestrator.clients.tmux.tmux_binary") as binary:
            binary.side_effect = RuntimeError("tmux executable is not resolvable")

            assert client.list_pane_control_identities() is None

    @pytest.mark.parametrize(
        "line",
        [
            "%263\t@261\t74654",
            "%263 @261 74654 1 0 sock sess win",
            "not-a-pane\t@261\t74654\t1\t0\tsock\tsess\twin",
            "%263\tnot-a-window\t74654\t1\t0\tsock\tsess\twin",
            "%263\t@261\tnot-a-pid\t1\t0\tsock\tsess\twin",
            "%263\t@261\t0\t1\t0\tsock\tsess\twin",
            # Seven fields: a record from a tmux that does not know
            # #{socket_path} is short, not merely missing one value, and a
            # short record must not be repaired into a longer one.
            "%263\t@261\t74654\t1\t0\tsess\twin",
            "",
        ],
    )
    def test_unparseable_lines_are_dropped(self, client, mock_subprocess, answers, line):
        answers.panes = _ok(line)

        assert client.list_pane_control_identities() == []

    def test_good_lines_survive_a_malformed_neighbour(self, client, mock_subprocess, answers):
        answers.panes = _ok("\n".join(["garbage line", _pane_line()]))

        records = client.list_pane_control_identities()

        assert records is not None
        assert [record.pane_id for record in records] == ["%263"]

    @pytest.mark.parametrize(
        "flag,expected",
        [("1", True), ("0", False), ("", False), ("#{bracket_paste_flag}", False)],
    )
    def test_bracketed_paste_is_proven_only_by_an_explicit_one(
        self, client, mock_subprocess, answers, flag, expected
    ):
        """An older tmux expands an unknown format to nothing; that is not support."""
        answers.panes = _ok(_pane_line(bracket=flag))

        identity = client.pane_control_identity(pane_id="%263")

        assert identity is not None
        assert identity.bracketed_paste_proven is expected

    @pytest.mark.parametrize("socket", ["", "#{socket_path}", "relative/path"])
    def test_an_unusable_socket_is_absent_not_a_value(
        self, client, mock_subprocess, answers, socket
    ):
        """The same reasoning as the paste flag, where it matters more.

        An older tmux expands ``#{socket_path}`` to nothing.  Recording
        that as the pane's server would give the writer boundary something
        to compare against, and a check that passes on an unproven value
        is worse than no check.
        """
        answers.panes = _ok(_pane_line(socket=socket))

        identity = client.pane_control_identity(pane_id="%263")

        assert identity is not None
        assert identity.server_socket_path is None

    def test_dead_pane_is_reported_not_hidden(self, client, mock_subprocess, answers):
        answers.panes = _ok(_pane_line(dead="1"))

        identity = client.pane_control_identity(pane_id="%263")

        assert identity is not None
        assert identity.dead is True

    def test_tab_in_a_window_name_cannot_corrupt_identity(self, client, mock_subprocess, answers):
        """Variable-content fields are last, so a tab shifts only itself."""
        answers.panes = _ok(_pane_line(window="odd\tname"))

        identity = client.pane_control_identity(pane_id="%263")

        assert identity is not None
        assert identity.pane_id == "%263"
        assert identity.window_id == "@261"
        assert identity.session_id == "$7"
        assert identity.pane_pid == 74654
        # The field that decides which server a write may reach sits ahead
        # of both name fields, so a tab in a foreign window's name cannot
        # shift it.
        assert identity.server_socket_path == SOCKET
        assert identity.window_name == "odd\tname"

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"pane_id": "%263", "session_name": "cao-1a2b3c4d", "window_name": "claude-9f8e"},
            {"pane_id": "%263", "session_name": "cao-1a2b3c4d"},
        ],
    )
    def test_requires_exactly_one_selector(self, client, mock_subprocess, kwargs):
        with pytest.raises(ValueError, match="not both"):
            client.pane_control_identity(**kwargs)

        assert mock_subprocess.run.call_count == 0

    @pytest.mark.parametrize(
        "kwargs", [{"session_name": "cao-1a2b3c4d"}, {"window_name": "claude-9f8e"}]
    )
    def test_name_selector_must_be_complete(self, client, mock_subprocess, kwargs):
        with pytest.raises(ValueError, match="together"):
            client.pane_control_identity(**kwargs)

        assert mock_subprocess.run.call_count == 0


class TestSendSteerChord:
    """The v2 steer-chord primitive: a named chord, distinct from the
    composer control keys, proven against the bound server before it lands."""

    def test_presses_the_named_chord_after_proving_the_server(self, client, mock_subprocess):
        client.send_steer_chord("%263", "C-s", expected_server_identity=SOCKET)

        argv = _write_argv(mock_subprocess)
        assert argv == [[TMUX, "send-keys", "-t", "%263", "C-s"]]
        # The server is proven before the chord, never trusted from the caller.
        assert SERVER_FORMAT in " ".join(_all_argv(mock_subprocess)[0])

    def test_a_non_chord_name_is_refused_with_no_write(self, client, mock_subprocess):
        for bad in ["C-foo", "Cs", "Enter", "C-", "C-s ", "M-s", ""]:
            with pytest.raises(ValueError):
                client.send_steer_chord("%263", bad, expected_server_identity=SOCKET)
            assert _write_argv(mock_subprocess) == []

    def test_an_invalid_pane_id_is_refused_before_any_call(self, client, mock_subprocess):
        with pytest.raises(ValueError):
            client.send_steer_chord("263", "C-s", expected_server_identity=SOCKET)
        assert mock_subprocess.run.call_count == 0

    def test_an_unbound_caller_is_refused(self, client, mock_subprocess):
        with pytest.raises(TmuxServerIdentityError):
            client.send_steer_chord("%263", "C-s", expected_server_identity=None)
        assert _write_argv(mock_subprocess) == []

    def test_a_pane_on_another_server_is_refused(self, client, mock_subprocess, answers):
        answers.probe = _ok(_server_line(socket=OTHER_SOCKET))
        with pytest.raises(TmuxServerIdentityError):
            client.send_steer_chord("%263", "C-s", expected_server_identity=SOCKET)
        assert _write_argv(mock_subprocess) == []

    def test_a_tmux_rejection_is_a_bounded_send_error(self, client, mock_subprocess, answers):
        answers.writes = [_fail(stderr="can't find pane: %263")]
        with pytest.raises(TmuxLiteralSendError):
            client.send_steer_chord("%263", "C-s", expected_server_identity=SOCKET)


class TestSendSequenceKey:
    """The v3 sequence-key primitive: a wire key name, translated to tmux's
    own name at the argv, proven against the bound server before it lands.

    The translation is load-bearing, not cosmetic: ``send-keys`` without
    ``-l`` never errors on a name it does not know — it sends the argument
    as literal bytes, so the wire's ``Backspace`` would type the nine
    characters "Backspace" into the composer.  tmux's name for the erase
    key is ``BSpace``.
    """

    def test_backspace_reaches_tmux_as_bspace(self, client, mock_subprocess):
        client.send_sequence_key("%263", "Backspace", expected_server_identity=SOCKET)

        argv = _write_argv(mock_subprocess)
        assert argv == [[TMUX, "send-keys", "-t", "%263", "BSpace"]]
        # The wire name itself is never an argv element: it is the
        # contract's name, not tmux's, and tmux would type it literally.
        assert all("Backspace" not in invocation for invocation in argv)
        # The server is proven before the keystroke, never trusted from the caller.
        assert SERVER_FORMAT in " ".join(_all_argv(mock_subprocess)[0])

    def test_exact_wire_names_pass_through_unchanged(self, client, mock_subprocess):
        names = ["Escape", "C-c", "C-s", "Enter"]
        for name in names:
            client.send_sequence_key("%263", name, expected_server_identity=SOCKET)
        assert _write_argv(mock_subprocess) == [
            [TMUX, "send-keys", "-t", "%263", name] for name in names
        ]

    @pytest.mark.parametrize(
        "name,tmux_arg",
        [
            ("Escape", "Escape"),
            ("C-c", "C-c"),
            ("C-s", "C-s"),
            ("Enter", "Enter"),
            ("Backspace", "BSpace"),
            ("Up", "Up"),
            ("Down", "Down"),
            ("Left", "Left"),
            ("Right", "Right"),
            ("Home", "Home"),
            ("End", "End"),
            ("PageUp", "PageUp"),
            ("PageDown", "PageDown"),
            ("Delete", "Delete"),
            ("Insert", "Insert"),
            ("Tab", "Tab"),
        ],
    )
    def test_every_wire_name_reaches_tmux_as_its_table_entry(
        self, client, mock_subprocess, name, tmux_arg
    ):
        client.send_sequence_key("%263", name, expected_server_identity=SOCKET)

        # The tmux argument is exactly the table entry: the canonical
        # primary names PageUp/PageDown/Delete/Insert pass as themselves,
        # never the tmux aliases PPage/NPage/DC/IC, and only Backspace is
        # renamed (to BSpace).  This is pinned per name, not sampled,
        # because send-keys without -l never errors on a name it does not
        # know — it sends the argument as literal bytes — so a near-miss
        # spelling would type itself into the composer.
        assert _write_argv(mock_subprocess) == [[TMUX, "send-keys", "-t", "%263", tmux_arg]]

    def test_the_translation_table_covers_exactly_the_contract_set(self):
        # Totality is already enforced by an assert at module import; this
        # test makes the intent explicit, because an import-time assert
        # reads like a build detail when it is in fact the guard against
        # the sink property: send-keys without -l turns an unvetted name
        # into literal bytes, so the contract set and the sink table may
        # never drift apart in either direction — a contract name without
        # a table entry would fail at the argv build, and a stale entry
        # would admit a name the contract no longer permits.
        assert set(tmux_module._TMUX_SEQUENCE_KEY_NAMES) == set(
            control_input_contract.SEQUENCE_KEY_NAMES
        )
        assert len(control_input_contract.SEQUENCE_KEY_NAMES) == 16
        for name in control_input_contract.SEQUENCE_KEY_NAMES:
            assert tmux_module._TMUX_SEQUENCE_KEY_NAMES[name]

    def test_a_name_outside_the_set_is_refused_with_no_write(self, client, mock_subprocess):
        # "BSpace" is tmux's name, not the wire's — the sink takes wire
        # names only and translation is its own internal step.  The rest
        # are names still outside the pinned 16-name set: "Tab" joined
        # the set with the v3 navigation keys, while the shifted-tab
        # chord, function keys, and modified arrows stay the §10.1
        # unsupported-key class.  Each is refused before a subprocess
        # exists, because tmux would type an unrecognized name as
        # literal bytes.
        for bad in ["BSpace", "backspace", "BTab", "F1", "C-Up", "C-foo", "Backspace ", ""]:
            with pytest.raises(ValueError):
                client.send_sequence_key("%263", bad, expected_server_identity=SOCKET)
            assert _write_argv(mock_subprocess) == []

    def test_an_invalid_pane_id_is_refused_before_any_call(self, client, mock_subprocess):
        with pytest.raises(ValueError):
            client.send_sequence_key("263", "Backspace", expected_server_identity=SOCKET)
        assert mock_subprocess.run.call_count == 0

    def test_an_unbound_caller_is_refused(self, client, mock_subprocess):
        with pytest.raises(TmuxServerIdentityError):
            client.send_sequence_key("%263", "Backspace", expected_server_identity=None)
        assert _write_argv(mock_subprocess) == []

    def test_a_pane_on_another_server_is_refused(self, client, mock_subprocess, answers):
        answers.probe = _ok(_server_line(socket=OTHER_SOCKET))
        with pytest.raises(TmuxServerIdentityError):
            client.send_sequence_key("%263", "Backspace", expected_server_identity=SOCKET)
        assert _write_argv(mock_subprocess) == []

    def test_a_tmux_rejection_is_a_bounded_send_error(self, client, mock_subprocess, answers):
        answers.writes = [_fail(stderr="can't find pane: %263")]
        with pytest.raises(TmuxLiteralSendError):
            client.send_sequence_key("%263", "Backspace", expected_server_identity=SOCKET)

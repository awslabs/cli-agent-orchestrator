"""Tests for the literal, bracket-free control-input primitives.

Two properties are asserted structurally rather than by sampling: the
control write path emits no paste buffer and no bracketed-paste sentinel
for *any* input, and identity is never resolved through a tmux ``-t``
target (which silently falls back to a different pane).
"""

import logging
from subprocess import CompletedProcess
from unittest.mock import call, patch

import pytest

from cli_agent_orchestrator.clients.tmux import TmuxClient, TmuxLiteralSendError

TMUX = "/usr/local/bin/tmux"

PANE_FORMAT = (
    "#{pane_id}\t#{window_id}\t#{pane_pid}\t"
    "#{bracket_paste_flag}\t#{pane_dead}\t#{session_name}\t#{window_name}"
)


def _pane_line(
    pane_id: str = "%263",
    window_id: str = "@261",
    pane_pid: str = "74654",
    bracket: str = "1",
    dead: str = "0",
    session: str = "cao-1a2b3c4d",
    window: str = "claude-9f8e",
) -> str:
    return "\t".join([pane_id, window_id, pane_pid, bracket, dead, session, window])


def _ok(stdout: str = "") -> CompletedProcess:
    return CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _fail(stderr: str = "can't find pane: %999999") -> CompletedProcess:
    return CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)


@pytest.fixture
def client():
    with patch("cli_agent_orchestrator.clients.tmux.libtmux"):
        yield TmuxClient()


@pytest.fixture
def mock_subprocess():
    with (
        patch("cli_agent_orchestrator.clients.tmux.subprocess") as mock,
        patch("cli_agent_orchestrator.clients.tmux.tmux_binary", return_value=TMUX),
    ):
        mock.run.return_value = _ok()
        yield mock


def _all_argv(mock_subprocess) -> list[list[str]]:
    return [invocation[0][0] for invocation in mock_subprocess.run.call_args_list]


class TestSendLiteralLineArgv:
    """The exact argv is the contract: text as literal bytes, Enter as a key."""

    def test_text_then_explicit_enter(self, client, mock_subprocess):
        client.send_literal_line("%263", "/compact")

        assert mock_subprocess.run.call_args_list == [
            call(
                [TMUX, "send-keys", "-t", "%263", "-l", "--", "/compact"],
                capture_output=True,
                text=True,
                check=False,
            ),
            call(
                [TMUX, "send-keys", "-t", "%263", "Enter"],
                capture_output=True,
                text=True,
                check=False,
            ),
        ]

    def test_printable_text_is_sent_verbatim(self, client, mock_subprocess):
        message = """He said "hello" and ran `cmd` with $VAR and a \\n backslash"""
        client.send_literal_line("%263", message, submit=False)

        assert _all_argv(mock_subprocess) == [
            [TMUX, "send-keys", "-t", "%263", "-l", "--", message]
        ]

    def test_dash_leading_text_stays_text(self, client, mock_subprocess):
        """'--' must precede the payload or tmux parses '-l' as an option."""
        client.send_literal_line("%263", "-l --literal", submit=False)

        argv = _all_argv(mock_subprocess)[0]
        assert argv[-2] == "--"
        assert argv[-1] == "-l --literal"

    def test_no_submit_omits_enter(self, client, mock_subprocess):
        client.send_literal_line("%263", "hello", submit=False)

        assert _all_argv(mock_subprocess) == [
            [TMUX, "send-keys", "-t", "%263", "-l", "--", "hello"]
        ]

    def test_empty_text_with_submit_sends_only_enter(self, client, mock_subprocess):
        client.send_literal_line("%263", "", submit=True)

        assert _all_argv(mock_subprocess) == [[TMUX, "send-keys", "-t", "%263", "Enter"]]

    def test_long_text_is_chunked_into_exact_slices(self, client, mock_subprocess):
        text = "".join(chr(ord("a") + (index % 26)) for index in range(2500))
        client.send_literal_line("%263", text, submit=True)

        argv = _all_argv(mock_subprocess)
        assert len(argv) == 4  # 1024 + 1024 + 452 + Enter
        assert argv[0][-1] == text[0:1024]
        assert argv[1][-1] == text[1024:2048]
        assert argv[2][-1] == text[2048:2500]
        assert argv[3] == [TMUX, "send-keys", "-t", "%263", "Enter"]
        assert "".join(item[-1] for item in argv[:3]) == text

    def test_target_is_always_a_pane_id(self, client, mock_subprocess):
        """A session:window target can resolve to a pane the caller never named."""
        client.send_literal_line("%263", "/compact")

        for argv in _all_argv(mock_subprocess):
            target = argv[argv.index("-t") + 1]
            assert target == "%263"
            assert ":" not in target

    def test_uses_the_resolved_absolute_tmux_binary(self, client, mock_subprocess):
        client.send_literal_line("%263", "/compact")

        assert all(argv[0] == TMUX for argv in _all_argv(mock_subprocess))


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
        client.send_literal_line("%263", text, submit=True)

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
            client.send_literal_line("%263", f"before{sentinel}after")

        assert mock_subprocess.run.call_count == 0


class TestSendLiteralLineRejects:
    """Every refusal happens before the first write, so nothing is emitted."""

    @pytest.mark.parametrize("char", ["\n", "\r", "\x1b", "\x9b"])
    def test_rejects_control_characters(self, client, mock_subprocess, char):
        with pytest.raises(ValueError, match="must not contain"):
            client.send_literal_line("%263", f"line one{char}line two")

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
            client.send_literal_line(pane_id, "/compact")

        assert mock_subprocess.run.call_count == 0

    def test_rejects_a_write_that_would_emit_nothing(self, client, mock_subprocess):
        with pytest.raises(ValueError, match="emit nothing"):
            client.send_literal_line("%263", "", submit=False)

        assert mock_subprocess.run.call_count == 0


class TestSendLiteralLineFailures:
    """A failed write reports how much of it may already have landed."""

    def test_first_write_failure_reports_zero_chunks(self, client, mock_subprocess):
        mock_subprocess.run.return_value = _fail()

        with pytest.raises(TmuxLiteralSendError) as excinfo:
            client.send_literal_line("%263", "/compact")

        assert excinfo.value.chunks_sent == 0
        assert excinfo.value.enter_attempted is False
        assert "can't find pane" in str(excinfo.value)

    def test_later_chunk_failure_reports_completed_chunks(self, client, mock_subprocess):
        mock_subprocess.run.side_effect = [_ok(), _fail("server exited")]

        with pytest.raises(TmuxLiteralSendError) as excinfo:
            client.send_literal_line("%263", "y" * 2000, submit=True)

        assert excinfo.value.chunks_sent == 1
        assert excinfo.value.enter_attempted is False

    def test_enter_failure_is_flagged_as_possibly_submitted(self, client, mock_subprocess):
        mock_subprocess.run.side_effect = [_ok(), _fail()]

        with pytest.raises(TmuxLiteralSendError) as excinfo:
            client.send_literal_line("%263", "/compact", submit=True)

        assert excinfo.value.chunks_sent == 1
        assert excinfo.value.enter_attempted is True

    def test_os_error_is_wrapped_not_leaked(self, client, mock_subprocess):
        mock_subprocess.run.side_effect = OSError("tmux vanished")

        with pytest.raises(TmuxLiteralSendError) as excinfo:
            client.send_literal_line("%263", "/compact")

        assert excinfo.value.chunks_sent == 0


class TestSendLiteralLineLogRedaction:
    """Control text is caller-supplied and stays out of INFO logs."""

    def test_info_log_omits_payload(self, client, mock_subprocess, caplog):
        secret = "/model sk-do-not-log-this"
        with caplog.at_level(logging.INFO, logger="cli_agent_orchestrator.clients.tmux"):
            client.send_literal_line("%263", secret)

        info_text = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.INFO)
        assert "sk-do-not-log-this" not in info_text
        assert "%263" in info_text
        assert "text length" in info_text

    def test_debug_log_retains_payload(self, client, mock_subprocess, caplog):
        with caplog.at_level(logging.DEBUG, logger="cli_agent_orchestrator.clients.tmux"):
            client.send_literal_line("%263", "visible-at-debug")

        debug_text = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG)
        assert "visible-at-debug" in debug_text


class TestPaneControlIdentityLookup:
    """Identity comes from an enumeration filtered in Python, never a -t target."""

    def test_enumeration_argv_is_exact(self, client, mock_subprocess):
        mock_subprocess.run.return_value = _ok(_pane_line())

        client.pane_control_identity(pane_id="%263")

        assert _all_argv(mock_subprocess) == [[TMUX, "list-panes", "-a", "-F", PANE_FORMAT]]

    def test_never_targets_a_pane_and_never_uses_display_message(self, client, mock_subprocess):
        mock_subprocess.run.return_value = _ok(_pane_line())

        client.pane_control_identity(session_name="cao-1a2b3c4d", window_name="claude-9f8e")

        for argv in _all_argv(mock_subprocess):
            assert "display-message" not in argv
            assert "-t" not in argv

    def test_resolves_by_pane_id(self, client, mock_subprocess):
        mock_subprocess.run.return_value = _ok(
            "\n".join([_pane_line(pane_id="%100"), _pane_line(), _pane_line(pane_id="%400")])
        )

        identity = client.pane_control_identity(pane_id="%263")

        assert identity is not None
        assert identity.pane_id == "%263"
        assert identity.window_id == "@261"
        assert identity.pane_pid == 74654
        assert identity.session_name == "cao-1a2b3c4d"
        assert identity.window_name == "claude-9f8e"
        assert identity.bracketed_paste_proven is True
        assert identity.dead is False

    def test_resolves_by_session_and_window(self, client, mock_subprocess):
        mock_subprocess.run.return_value = _ok(
            "\n".join([_pane_line(pane_id="%100", window="other"), _pane_line()])
        )

        identity = client.pane_control_identity(
            session_name="cao-1a2b3c4d", window_name="claude-9f8e"
        )

        assert identity is not None
        assert identity.pane_id == "%263"

    def test_unknown_pane_is_absent_not_guessed(self, client, mock_subprocess):
        mock_subprocess.run.return_value = _ok(_pane_line(pane_id="%100"))

        assert client.pane_control_identity(pane_id="%263") is None

    def test_multi_pane_window_is_ambiguous(self, client, mock_subprocess):
        """A window with two panes has no single control target."""
        mock_subprocess.run.return_value = _ok(
            "\n".join([_pane_line(pane_id="%263"), _pane_line(pane_id="%264", window_id="@261")])
        )

        assert (
            client.pane_control_identity(session_name="cao-1a2b3c4d", window_name="claude-9f8e")
            is None
        )

    def test_failed_enumeration_is_unknown_not_empty(self, client, mock_subprocess):
        mock_subprocess.run.return_value = _fail("no server running")

        assert client.list_pane_control_identities() is None
        assert client.pane_control_identity(pane_id="%263") is None

    def test_os_error_is_unknown_not_empty(self, client, mock_subprocess):
        mock_subprocess.run.side_effect = OSError("tmux vanished")

        assert client.list_pane_control_identities() is None

    def test_unresolvable_binary_is_unknown_not_empty(self, client):
        with patch("cli_agent_orchestrator.clients.tmux.tmux_binary") as binary:
            binary.side_effect = RuntimeError("tmux executable is not resolvable")

            assert client.list_pane_control_identities() is None

    @pytest.mark.parametrize(
        "line",
        [
            "%263\t@261\t74654",
            "%263 @261 74654 1 0 sess win",
            "not-a-pane\t@261\t74654\t1\t0\tsess\twin",
            "%263\tnot-a-window\t74654\t1\t0\tsess\twin",
            "%263\t@261\tnot-a-pid\t1\t0\tsess\twin",
            "%263\t@261\t0\t1\t0\tsess\twin",
            "",
        ],
    )
    def test_unparseable_lines_are_dropped(self, client, mock_subprocess, line):
        mock_subprocess.run.return_value = _ok(line)

        assert client.list_pane_control_identities() == []

    def test_good_lines_survive_a_malformed_neighbour(self, client, mock_subprocess):
        mock_subprocess.run.return_value = _ok("\n".join(["garbage line", _pane_line()]))

        records = client.list_pane_control_identities()

        assert records is not None
        assert [record.pane_id for record in records] == ["%263"]

    @pytest.mark.parametrize(
        "flag,expected",
        [("1", True), ("0", False), ("", False), ("#{bracket_paste_flag}", False)],
    )
    def test_bracketed_paste_is_proven_only_by_an_explicit_one(
        self, client, mock_subprocess, flag, expected
    ):
        """An older tmux expands an unknown format to nothing; that is not support."""
        mock_subprocess.run.return_value = _ok(_pane_line(bracket=flag))

        identity = client.pane_control_identity(pane_id="%263")

        assert identity is not None
        assert identity.bracketed_paste_proven is expected

    def test_dead_pane_is_reported_not_hidden(self, client, mock_subprocess):
        mock_subprocess.run.return_value = _ok(_pane_line(dead="1"))

        identity = client.pane_control_identity(pane_id="%263")

        assert identity is not None
        assert identity.dead is True

    def test_tab_in_a_window_name_cannot_corrupt_identity(self, client, mock_subprocess):
        """Variable-content fields are last, so a tab shifts only itself."""
        mock_subprocess.run.return_value = _ok(_pane_line(window="odd\tname"))

        identity = client.pane_control_identity(pane_id="%263")

        assert identity is not None
        assert identity.pane_id == "%263"
        assert identity.window_id == "@261"
        assert identity.pane_pid == 74654
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

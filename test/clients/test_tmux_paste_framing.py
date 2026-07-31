"""Who is allowed to write the bracketed-paste markers: tmux, never us.

Ordinary message delivery (``send_input`` →
``TmuxClient.send_keys(force_bracketed_paste=True)``) once wrote the
paste markers into the buffer itself and pasted with ``-r``.  tmux
sanitizes control bytes on their way out of a paste buffer, so those
ESCs never reached the pane as escapes: they arrived as the seven
printable characters ``^[[200~``, which a composer types out as visible
text and then submits with the message.

These tests are the CI-runnable half of that fix.  They assert the argv
and the buffer, which is all a machine without tmux can see, and they are
deliberately not the whole story: the previous implementation's argv was
self-consistent and passed every test it had.  What it actually put on
the wire is measured against a real pane in
``test/e2e/test_ordinary_input_live.py``.  The two halves are load-
bearing together — this file catches a code change, that one catches a
tmux change.

The invariant both enforce: **the payload handed to ``load-buffer`` is
the caller's text and nothing else**, and any framing is requested from
tmux with ``-p`` rather than typed into the payload.
"""

import subprocess
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.clients.tmux import TmuxClient
from cli_agent_orchestrator.models.provider import ProviderType

# The markers, as real escapes.  If either appears in an argv or in the
# bytes we hand to load-buffer, this code is trying to do tmux's job.
FRAMED_START = "\x1b[200~"
FRAMED_END = "\x1b[201~"

TARGET = "sess:win"
BUFFER = "cao_abcd1234"


@pytest.fixture
def client():
    with (
        patch("cli_agent_orchestrator.clients.tmux.libtmux"),
        # History reads are bounded tmux subprocesses since COND-0242 and
        # claude_code re-reads the pane mid-delivery, so the binary is pinned
        # here rather than resolved from a machine that may not have tmux.
        patch("cli_agent_orchestrator.clients.tmux.tmux_binary", return_value="/usr/bin/tmux"),
    ):
        yield TmuxClient()


@pytest.fixture
def tmux_calls():
    """Every ``subprocess.run`` the client makes, in order.

    Read-only observations get a real answer, because history reads run as
    bounded tmux subprocesses too since COND-0242 and a provider (claude_code)
    re-reads the pane mid-delivery. Write calls keep returning ``None`` — this
    suite asserts on their argv, not on their result.
    """
    with patch("cli_agent_orchestrator.clients.tmux.subprocess") as mock:

        def run(cmd, *args, **kwargs):
            argv = list(cmd)
            if "list-panes" in argv:
                window = argv[argv.index("-t") + 1].split(":=", 1)[1]
                return subprocess.CompletedProcess(argv, 0, stdout=f"%0\t{window}\n", stderr="")
            if "capture-pane" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout="pane tail\n", stderr="")
            return None

        mock.run.side_effect = run
        yield mock.run.call_args_list


@pytest.fixture(autouse=True)
def _fixed_buffer_name():
    with patch("cli_agent_orchestrator.clients.tmux.uuid") as mock:
        mock.uuid4.return_value.hex = "abcd1234efgh"
        yield


@pytest.fixture(autouse=True)
def _no_real_sleeping():
    """The submit delay is real seconds; this suite does not need to wait."""
    with patch("cli_agent_orchestrator.clients.tmux.time.sleep"):
        yield


def _argv(call) -> list:
    return call[0][0]


def _find(calls, verb: str) -> list:
    """The argv of the single call carrying ``verb``."""
    matches = [_argv(call) for call in calls if len(_argv(call)) > 1 and _argv(call)[1] == verb]
    assert len(matches) == 1, f"expected exactly one {verb}: {matches}"
    return matches[0]


def _loaded_bytes(calls) -> bytes:
    loads = [call for call in calls if _argv(call)[1] == "load-buffer"]
    assert len(loads) == 1, loads
    return loads[0][1]["input"]


class TestForcedPasteAsksTmuxForTheFraming:
    """The fix, stated as the argv it must produce."""

    def test_paste_buffer_carries_both_p_and_r(self, client, tmux_calls):
        """``-p`` requests the framing; ``-r`` keeps newlines as newlines.

        Both flags, asserted as one exact argv rather than as two
        containment checks, because the pair is the contract: ``-p``
        alone silently rewrites every LF to CR and turns a multi-line
        message into one submission per line, and ``-r`` alone is the old
        broken behaviour with the markers simply missing.
        """
        client.send_keys("sess", "win", "hello", force_bracketed_paste=True)

        assert _find(tmux_calls, "paste-buffer") == [
            "tmux",
            "paste-buffer",
            "-p",
            "-r",
            "-b",
            BUFFER,
            "-t",
            TARGET,
        ]

    def test_the_buffer_holds_the_message_and_nothing_else(self, client, tmux_calls):
        """No marker is added to the payload — that is tmux's job now."""
        client.send_keys("sess", "win", "hello", force_bracketed_paste=True)

        assert _loaded_bytes(tmux_calls) == b"hello"

    def test_a_multiline_message_is_loaded_verbatim(self, client, tmux_calls):
        message = "line one\nline two\nline three"
        client.send_keys("sess", "win", message, force_bracketed_paste=True)

        assert _loaded_bytes(tmux_calls) == message.encode()


class TestNoMarkerIsEverManufactured:
    """The regression gate, over payloads that have tripped this code before."""

    @pytest.mark.parametrize(
        "message",
        [
            pytest.param("hello", id="single-line"),
            pytest.param("line one\nline two", id="multi-line"),
            pytest.param("", id="empty"),
            pytest.param("X" * 50_000, id="large"),
            pytest.param("naïve — ünicode ✓", id="non-ascii"),
            pytest.param('say "hi" && run `cmd` $VAR', id="shell-metacharacters"),
            pytest.param("/compact", id="slash-command"),
        ],
    )
    def test_no_escape_byte_is_added_to_any_argv_or_to_the_buffer(
        self, client, tmux_calls, message
    ):
        """Stronger than searching for the two markers.

        None of these payloads contains an ESC, so *any* escape byte in
        the argv or the buffer was manufactured by the delivery path —
        whether or not it happens to spell a paste marker.
        """
        client.send_keys("sess", "win", message, force_bracketed_paste=True)

        assert _loaded_bytes(tmux_calls) == message.encode()
        assert b"\x1b" not in _loaded_bytes(tmux_calls)
        for call in tmux_calls:
            for part in _argv(call):
                assert "\x1b" not in part, (part, _argv(call))

    def test_a_message_that_itself_contains_markers_is_not_re_wrapped(self, client, tmux_calls):
        """A hostile or careless payload is passed through, not amplified.

        Delivery is not the layer that decides whether a message may
        contain these bytes — it is the layer that must not *add* them.
        The count is asserted to be exactly the caller's own, so a future
        re-wrap would be caught even for a payload where a containment
        check could not tell whose markers it found.  (tmux neutralizes
        these on the way to the pane, which is precisely the behaviour
        this fix stopped fighting.)
        """
        message = f"{FRAMED_START}already framed{FRAMED_END}"
        client.send_keys("sess", "win", message, force_bracketed_paste=True)

        loaded = _loaded_bytes(tmux_calls)
        assert loaded == message.encode()
        assert loaded.count(FRAMED_START.encode()) == 1
        assert loaded.count(FRAMED_END.encode()) == 1


class TestTheShellCommandPathIsUnchanged:
    """``force_bracketed_paste=False`` must keep converting LF to CR.

    Initialization sends shell commands to bash this way, where each
    newline *should* become the Enter that runs the line.  Adding ``-r``
    here — the symmetric-looking change — would leave those commands
    sitting unexecuted on the prompt.
    """

    def test_paste_buffer_carries_p_alone(self, client, tmux_calls):
        client.send_keys("sess", "win", "echo hi", force_bracketed_paste=False)

        assert _find(tmux_calls, "paste-buffer") == [
            "tmux",
            "paste-buffer",
            "-p",
            "-b",
            BUFFER,
            "-t",
            TARGET,
        ]

    def test_r_is_absent(self, client, tmux_calls):
        client.send_keys("sess", "win", "echo hi", force_bracketed_paste=False)

        assert "-r" not in _find(tmux_calls, "paste-buffer")

    def test_the_default_is_the_shell_command_path(self, client, tmux_calls):
        """Omitting the flag must not silently opt into paste framing."""
        client.send_keys("sess", "win", "echo hi")

        assert "-r" not in _find(tmux_calls, "paste-buffer")


class TestEveryProviderDeliversTheSameBytes:
    """The sweep the defect's blast radius demands.

    A provider cannot change the framing directly, but it does choose
    ``paste_enter_count`` and ``paste_submit_delay``, and those are the
    only provider-controlled inputs to delivery.  The claim under test is
    that nothing a provider may vary can reintroduce a marker — so the
    parameters are read from the real provider classes rather than
    invented, and a provider added later shows up here automatically.
    """

    @pytest.fixture
    def provider(self, request):
        from cli_agent_orchestrator.providers.manager import ProviderManager

        return ProviderManager().create_provider(
            request.param, "tid", "sess", "win", agent_profile="default"
        )

    @staticmethod
    def _deliver(client, provider, message: str) -> bool:
        """Run one ``send_input`` down the real service → backend → client route.

        The backend is given ``client`` explicitly rather than allowed to
        fall back to the module singleton: that singleton holds a live
        ``libtmux.Server``, and a provider whose ``mark_input_received``
        reads pane history (Claude Code does) would reach for a real tmux
        the moment delivery finished.

        Everything patched here is a dependency of ``send_input`` that has
        nothing to do with which bytes reach tmux — metadata lookup, the
        status monitor, activity bookkeeping, memory injection.  The
        delivery path itself is not stubbed.
        """
        from cli_agent_orchestrator.backends.tmux_backend import TmuxBackend
        from cli_agent_orchestrator.services.terminal_service import send_input

        with (
            patch(
                "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
                return_value={"tmux_session": "sess", "tmux_window": "win"},
            ),
            patch("cli_agent_orchestrator.services.terminal_service.provider_manager") as pm,
            patch("cli_agent_orchestrator.backends.registry._backend", TmuxBackend(client=client)),
            patch("cli_agent_orchestrator.services.terminal_service.status_monitor"),
            patch("cli_agent_orchestrator.services.terminal_service.update_last_active"),
            patch(
                "cli_agent_orchestrator.services.terminal_service.inject_memory_context",
                side_effect=lambda text, _tid: text,
            ),
        ):
            pm.get_provider.return_value = provider
            return send_input("tid", message)

    @pytest.mark.parametrize(
        "provider",
        [pytest.param(kind.value, id=kind.value) for kind in ProviderType],
        indirect=True,
    )
    def test_delivery_through_send_input_manufactures_no_marker(
        self, client, provider, tmux_calls, isolated_memory_db
    ):
        """The whole ordinary path, per provider, ending at the tmux argv.

        Driven through ``send_input`` rather than through the client so
        the provider's real tunables travel the real route.
        """
        message = "review this\nand this"

        assert self._deliver(client, provider, message) is True

        assert _loaded_bytes(tmux_calls) == message.encode()
        assert _find(tmux_calls, "paste-buffer")[2:4] == ["-p", "-r"]
        for call in tmux_calls:
            for part in _argv(call):
                assert "\x1b" not in part, (part, _argv(call))

    @pytest.mark.parametrize(
        "provider",
        [pytest.param(kind.value, id=kind.value) for kind in ProviderType],
        indirect=True,
    )
    def test_the_providers_own_enter_count_is_honoured(
        self, client, provider, tmux_calls, isolated_memory_db
    ):
        """The sweep must not pass by delivering nothing.

        Without this, a ``send_input`` that silently returned early would
        satisfy every "no marker present" assertion above.
        """
        self._deliver(client, provider, "hello")

        enters = [
            _argv(call)
            for call in tmux_calls
            if _argv(call)[1] == "send-keys" and _argv(call)[-1] == "Enter"
        ]
        assert len(enters) == provider.paste_enter_count
        assert provider.paste_enter_count >= 1


class TestTheClientStillDoesTheRestOfItsJob:
    """Guard rails around the edited block, so the fix stayed narrow."""

    def test_the_buffer_is_deleted_even_when_the_paste_fails(self, client, tmux_calls):
        with patch("cli_agent_orchestrator.clients.tmux.subprocess") as mock:
            mock.run.side_effect = [None, RuntimeError("paste failed"), None]
            with pytest.raises(RuntimeError, match="paste failed"):
                client.send_keys("sess", "win", "msg", force_bracketed_paste=True)
            assert mock.run.call_args_list[-1][0][0] == [
                "tmux",
                "delete-buffer",
                "-b",
                BUFFER,
            ]

    def test_the_target_is_still_validated_at_the_sink(self, client):
        """A name that could pivot the write to another pane is refused."""
        with pytest.raises(ValueError, match="session_name"):
            client.send_keys("other:window", "win", "msg", force_bracketed_paste=True)

    def test_the_payload_is_not_logged_at_info(self, client, tmux_calls, caplog):
        import logging

        secret = "API_TOKEN=super-secret-value"
        with caplog.at_level(logging.INFO, logger="cli_agent_orchestrator.clients.tmux"):
            client.send_keys("sess", "win", secret, force_bracketed_paste=True)

        info = "\n".join(r.getMessage() for r in caplog.records if r.levelno == logging.INFO)
        assert "super-secret-value" not in info
        assert "keys length" in info


def test_send_keys_contains_no_hand_written_marker():
    """The defect class, forbidden at the source.

    Every test above measures one call.  This one measures the function:
    if the literal marker bytes appear in ``send_keys`` again, some path
    is manufacturing framing whether or not a test happens to cover it.

    Scoped to ``send_keys`` rather than to the whole module on purpose.
    Elsewhere in this file the literals are legitimate — a neighbouring
    docstring names them to explain what ``paste-buffer -p`` makes tmux
    emit — and a module-wide ban would have to be relaxed the first time
    someone wrote an accurate comment.
    """
    import inspect

    source = inspect.getsource(TmuxClient.send_keys)
    # Both spellings: the escape as typed into source, and the byte it
    # would produce if it were assembled some other way.
    assert "\\x1b[200~" not in source
    assert "\\x1b[201~" not in source
    assert FRAMED_START not in source
    assert FRAMED_END not in source

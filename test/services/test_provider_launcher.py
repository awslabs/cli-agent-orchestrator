"""Focused unit coverage for the managed provider-launcher boundary."""

from __future__ import annotations

import io
import json
import signal
from types import SimpleNamespace

import pytest

from cli_agent_orchestrator.services import managed_provider_bridge as bridge
from cli_agent_orchestrator.services import provider_launcher


class _NonClosingBytesIO(io.BytesIO):
    closed_by_launcher = False

    def close(self) -> None:
        self.closed_by_launcher = True


class _SocketDouble:
    def __init__(self, *, connect_error: Exception | None = None):
        self.connect_error = connect_error
        self.connected_to = None
        self.sent: list[bytes] = []
        self.closed_by_launcher = False

    def connect(self, path: str) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.connected_to = path

    def sendall(self, payload: bytes) -> None:
        self.sent.append(payload)

    def close(self) -> None:
        self.closed_by_launcher = True


def test_stdio_pumps_preserve_short_reads_and_close_child_stdin(monkeypatch):
    class _ReadOne:
        def read1(self, size):
            assert size == 7
            return bytearray(b"short")

    class _ReadFallback:
        def read(self, size):
            assert size == 64
            return b"fallback"

    assert provider_launcher._read_available(_ReadOne(), 7) == b"short"
    assert provider_launcher._read_available(_ReadFallback(), 64) == b"fallback"

    child_stdin = _NonClosingBytesIO()
    child = SimpleNamespace(stdin=child_stdin, stdout=io.BytesIO(b"provider-out"))
    monkeypatch.setattr(
        provider_launcher.sys,
        "stdin",
        SimpleNamespace(buffer=io.BytesIO(b"provider-in")),
    )
    output = _NonClosingBytesIO()
    monkeypatch.setattr(provider_launcher.sys, "stdout", SimpleNamespace(buffer=output))

    provider_launcher._pump_stdin(child)
    provider_launcher._pump_stdout(child)

    assert child_stdin.getvalue() == b"provider-in"
    assert child_stdin.closed_by_launcher is True
    assert output.getvalue() == b"provider-out"


def test_stdio_pumps_tolerate_broken_streams(monkeypatch):
    class _BrokenReader:
        def read1(self, _size):
            raise BrokenPipeError

    class _BrokenClose:
        def close(self):
            raise ValueError

    child = SimpleNamespace(stdin=_BrokenClose(), stdout=_BrokenReader())
    monkeypatch.setattr(
        provider_launcher.sys,
        "stdin",
        SimpleNamespace(buffer=_BrokenReader()),
    )

    provider_launcher._pump_stdin(child)
    provider_launcher._pump_stdout(child)


def test_channel_loop_acknowledges_only_valid_issue_requests():
    class _Channel:
        chunks = [
            b"{bad-json}\n"
            b'{"op":"status"}\n'
            b'{"op":"issue-request","request_id":"request-1"}\n',
            b"",
        ]

        def __init__(self):
            self.sent: list[bytes] = []

        def recv(self, _size):
            return self.chunks.pop(0)

        def sendall(self, payload):
            self.sent.append(payload)

    channel = _Channel()
    provider_launcher._channel_loop(channel)

    assert [json.loads(payload) for payload in channel.sent] == [
        {"op": "issue-ack", "request_id": "request-1"}
    ]


def test_channel_loop_fails_closed_on_transport_and_oversize_input():
    class _ReceiveError:
        def recv(self, _size):
            raise OSError("closed")

    class _Oversize:
        def recv(self, _size):
            return b"x" * (4 * 1024 * 1024 + 1)

    class _SendError:
        sent = False

        def recv(self, _size):
            if self.sent:
                return b""
            self.sent = True
            return b'{"op":"issue-request","request_id":"request-2"}\n'

        def sendall(self, _payload):
            raise OSError("closed")

    provider_launcher._channel_loop(_ReceiveError())
    provider_launcher._channel_loop(_Oversize())
    provider_launcher._channel_loop(_SendError())


def test_open_channel_pins_the_same_claim_before_and_after_handshake(monkeypatch):
    identity = {"terminal_id": "terminal-1"}
    verification = object()
    observed = []
    client = _SocketDouble()

    def _verify(path, actual_identity, *, expected=None):
        observed.append((path, actual_identity, expected))
        return verification

    monkeypatch.setattr(bridge, "verify_rendezvous_binding", _verify)
    monkeypatch.setattr(provider_launcher.socket, "socket", lambda *_: client)

    assert provider_launcher._open_channel("/tmp/bridge.sock", identity) == (
        client,
        verification,
    )
    assert client.connected_to == "/tmp/bridge.sock"
    assert json.loads(client.sent[0]) == {
        "rendezvous_identity": identity,
        "request": {"op": "provider-channel", "pid": provider_launcher.os.getpid()},
    }
    assert observed == [
        ("/tmp/bridge.sock", identity, None),
        ("/tmp/bridge.sock", identity, verification),
        ("/tmp/bridge.sock", identity, verification),
    ]


def test_open_channel_retries_connect_errors_then_succeeds(monkeypatch):
    identity = {"terminal_id": "terminal-1"}
    verification = object()
    clients = [_SocketDouble(connect_error=OSError("not ready")), _SocketDouble()]

    monkeypatch.setattr(
        bridge,
        "verify_rendezvous_binding",
        lambda *_args, **_kwargs: verification,
    )
    monkeypatch.setattr(provider_launcher.socket, "socket", lambda *_: clients.pop(0))
    monkeypatch.setattr(provider_launcher.time, "sleep", lambda _delay: None)

    opened = provider_launcher._open_channel("/tmp/bridge.sock", identity)

    assert opened is not None
    assert opened[0].connected_to == "/tmp/bridge.sock"


def test_open_channel_refuses_binding_errors_and_post_send_replacement(monkeypatch):
    identity = {"terminal_id": "terminal-1"}

    def _binding_error(*_args, **_kwargs):
        raise bridge.BridgeError("socket-identity-collision")

    monkeypatch.setattr(bridge, "verify_rendezvous_binding", _binding_error)
    assert provider_launcher._open_channel("/tmp/bridge.sock", identity) is None

    client = _SocketDouble()
    verification = object()
    calls = 0

    def _replaced_after_send(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise bridge.BridgeError("socket-rendezvous-replaced")
        return verification

    monkeypatch.setattr(bridge, "verify_rendezvous_binding", _replaced_after_send)
    monkeypatch.setattr(provider_launcher.socket, "socket", lambda *_: client)

    assert provider_launcher._open_channel("/tmp/bridge.sock", identity) is None
    assert client.sent
    assert client.closed_by_launcher is True


def test_open_channel_closes_every_ambiguous_transport_state(monkeypatch):
    identity = {"terminal_id": "terminal-1"}
    verification = object()
    after_connect = _SocketDouble()
    calls = 0

    def _replaced_during_connect(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise bridge.BridgeError("socket-rendezvous-replaced")
        return verification

    monkeypatch.setattr(bridge, "verify_rendezvous_binding", _replaced_during_connect)
    monkeypatch.setattr(provider_launcher.socket, "socket", lambda *_: after_connect)
    assert provider_launcher._open_channel("/tmp/bridge.sock", identity) is None
    assert after_connect.closed_by_launcher is True

    failed_clients = []

    def _never_connect(*_args):
        client = _SocketDouble(connect_error=OSError("not ready"))
        failed_clients.append(client)
        return client

    monkeypatch.setattr(
        bridge,
        "verify_rendezvous_binding",
        lambda *_args, **_kwargs: verification,
    )
    monkeypatch.setattr(provider_launcher.socket, "socket", _never_connect)
    monkeypatch.setattr(provider_launcher.time, "sleep", lambda _delay: None)
    assert provider_launcher._open_channel("/tmp/bridge.sock", identity) is None
    assert len(failed_clients) == 200
    assert all(client.closed_by_launcher for client in failed_clients)

    no_verification = _SocketDouble()
    monkeypatch.setattr(
        bridge,
        "verify_rendezvous_binding",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(provider_launcher.socket, "socket", lambda *_: no_verification)
    assert provider_launcher._open_channel("/tmp/bridge.sock", identity) is None
    assert no_verification.closed_by_launcher is True

    send_failure = _SocketDouble()
    send_failure.sendall = lambda _payload: (_ for _ in ()).throw(OSError("closed"))
    monkeypatch.setattr(
        bridge,
        "verify_rendezvous_binding",
        lambda *_args, **_kwargs: verification,
    )
    monkeypatch.setattr(provider_launcher.socket, "socket", lambda *_: send_failure)
    assert provider_launcher._open_channel("/tmp/bridge.sock", identity) is None
    assert send_failure.closed_by_launcher is True


@pytest.mark.parametrize(
    "identity_json,provider_argv,error",
    [
        ("{bad-json}", ["--", "/bin/true"], "--identity-json must be valid JSON"),
        ("[]", ["--", "/bin/true"], "--identity-json must name an object"),
        ("{}", [], "the real provider argv is required after --"),
    ],
)
def test_main_rejects_malformed_launch_envelopes(identity_json, provider_argv, error, capsys):
    with pytest.raises(SystemExit):
        provider_launcher.main(
            ["--socket", "/tmp/bridge.sock", "--identity-json", identity_json, *provider_argv]
        )

    assert error in capsys.readouterr().err


def test_main_reverifies_before_spawn_and_runs_the_proxy(monkeypatch):
    identity = {"terminal_id": "terminal-1"}
    verification = object()
    channel = _SocketDouble()
    child_stdin = _NonClosingBytesIO()
    child = SimpleNamespace(
        stdin=child_stdin,
        stdout=io.BytesIO(b""),
        terminated=False,
    )
    child.poll = lambda: None if not child.terminated else 0
    child.terminate = lambda: setattr(child, "terminated", True)
    child.wait = lambda: 23
    verifies = []
    popens = []
    threads = []
    pumped = []

    monkeypatch.setattr(
        provider_launcher,
        "_open_channel",
        lambda path, actual_identity: (channel, verification),
    )
    monkeypatch.setattr(
        bridge,
        "verify_launch_binding_identity",
        lambda actual_identity: verifies.append(("launch", actual_identity)),
    )
    monkeypatch.setattr(
        bridge,
        "verify_rendezvous_binding",
        lambda path, actual_identity, *, expected=None: verifies.append(
            ("socket", path, actual_identity, expected)
        ),
    )

    def _popen(argv, **kwargs):
        popens.append((argv, kwargs))
        return child

    monkeypatch.setattr(provider_launcher.subprocess, "Popen", _popen)

    class _Thread:
        def __init__(self, *, target, args, daemon):
            threads.append((target, args, daemon))

        def start(self):
            return None

    monkeypatch.setattr(provider_launcher.threading, "Thread", _Thread)
    monkeypatch.setattr(
        provider_launcher,
        "_pump_stdout",
        lambda actual_child: pumped.append(actual_child),
    )

    def _install_signal(signum, handler):
        if signum == signal.SIGTERM:
            handler(signum, None)

    monkeypatch.setattr(provider_launcher.signal, "signal", _install_signal)

    result = provider_launcher.main(
        [
            "--socket",
            "/tmp/bridge.sock",
            "--identity-json",
            json.dumps(identity),
            "--",
            "/bin/provider",
            "--flag",
        ]
    )

    assert result == 23
    assert verifies == [
        ("launch", identity),
        ("socket", "/tmp/bridge.sock", identity, verification),
    ]
    assert popens[0][0] == ["/bin/provider", "--flag"]
    assert popens[0][1]["stdin"] == provider_launcher.subprocess.PIPE
    assert popens[0][1]["stdout"] == provider_launcher.subprocess.PIPE
    assert popens[0][1]["stderr"] is None
    assert child.terminated is True
    assert [target for target, _args, _daemon in threads] == [
        provider_launcher._channel_loop,
        provider_launcher._pump_stdin,
    ]
    assert pumped == [child]
    assert channel.closed_by_launcher is True


def test_main_closes_channel_when_provider_spawn_raises(monkeypatch):
    identity = {"terminal_id": "terminal-1"}
    channel = _SocketDouble()
    monkeypatch.setattr(
        provider_launcher,
        "_open_channel",
        lambda *_args: (channel, object()),
    )
    monkeypatch.setattr(bridge, "verify_launch_binding_identity", lambda _identity: None)
    monkeypatch.setattr(bridge, "verify_rendezvous_binding", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        provider_launcher.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("spawn failed")),
    )

    with pytest.raises(RuntimeError, match="spawn failed"):
        provider_launcher.main(
            [
                "--socket",
                "/tmp/bridge.sock",
                "--identity-json",
                json.dumps(identity),
                "--",
                "/bin/provider",
            ]
        )

    assert channel.closed_by_launcher is True


def test_main_refuses_absent_channel_and_effect_boundary_mismatch(monkeypatch):
    identity = {"terminal_id": "terminal-1"}
    argv = [
        "--socket",
        "/tmp/bridge.sock",
        "--identity-json",
        json.dumps(identity),
        "--",
        "/bin/provider",
    ]
    monkeypatch.setattr(provider_launcher, "_open_channel", lambda *_args: None)
    assert provider_launcher.main(argv) == 1

    channel = _SocketDouble()
    monkeypatch.setattr(
        provider_launcher,
        "_open_channel",
        lambda *_args: (channel, object()),
    )
    monkeypatch.setattr(
        bridge,
        "verify_launch_binding_identity",
        lambda _identity: (_ for _ in ()).throw(bridge.BridgeError("head drift")),
    )
    assert provider_launcher.main(argv) == 1
    assert channel.closed_by_launcher is True

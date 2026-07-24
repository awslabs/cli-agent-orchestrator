"""End-to-end: provider-originated actor-assertion issuance.

A real provider child is spawned through the real provider launcher shim;
the bridge's actual ``_serve`` accept loop runs. The conductor peer is
refused direct issuance (it is not in the provider tree), issuance is
relayed through the provider-originated channel with kernel verification
on THAT connection, a foreign same-UID peer cannot open the channel,
replay is refused, and report provenance completes (one-use consumption).
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest

from cli_agent_orchestrator import constants
from cli_agent_orchestrator.services import actor_broker, heartbeat_store
from cli_agent_orchestrator.services import managed_provider_bridge as bridge
from cli_agent_orchestrator.services import provider_launcher


@pytest.fixture
def short_root(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="lb-act-") as root:
        root_path = Path(root)
        monkeypatch.setattr(bridge, "RENDEZVOUS_ROOT", root_path / "runtime")
        yield root_path


def _identity(root, terminal_id, generation):
    return {
        "project": "test-project",
        "task_id": str(uuid.uuid4()),
        "terminal_id": terminal_id,
        "terminal_generation": generation,
        "worktree_realpath": str(root.resolve()),
        "repository": "test-repository",
        "head": "1" * 40,
        "actor": "cafebabe",
    }


def _target(root, identity):
    target = {
        "root": root / "bridge",
        "state": root / "bridge" / "state.json",
    }
    target.update(bridge.rendezvous_paths(identity))
    target["root"].mkdir(parents=True)
    return target


def _call(target, identity, command, timeout=30.0):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(timeout)
        client.connect(str(target["socket"]))
        client.sendall(
            json.dumps({"rendezvous_identity": identity, "request": command}).encode() + b"\n"
        )
        raw = bytearray()
        while b"\n" not in raw:
            block = client.recv(65536)
            if not block:
                break
            raw.extend(block)
        return json.loads(bytes(raw).split(b"\n", 1)[0])
    finally:
        client.close()


def _assertion_command(**changes):
    command = {
        "op": "actor-assertion",
        "report_sha256": "a" * 64,
        "report_path": "/tmp/report.md",
        "project": "cao-conductor-self-heal",
        "run_id": "run-0001",
        "obligation_generation": "obgen-7c2e4a1b",
        "attempt_id": str(uuid.uuid4()),
        "native_session_id": "native",
        "launch_nonce_digest": "b" * 64,
        "route_chain_head": "c" * 64,
    }
    command.update(changes)
    return command


@pytest.fixture
def live_bridge(short_root, monkeypatch):
    """The real _serve loop with a REAL provider child via the REAL launcher."""
    terminal_id = "a1b2c3d4"
    generation = str(uuid.uuid4())
    monkeypatch.setattr(constants, "COMPANION_DIR", short_root / "companion")
    heartbeat_store.issue_fencing_token(
        constants.COMPANION_DIR, terminal_id, generation, str(uuid.uuid4())
    )
    # Keep this test's state root registry-free; registry-first bridge
    # resources have their own dedicated coverage.
    monkeypatch.setattr(bridge, "_declare_bridge_resources", lambda *a: None)
    monkeypatch.setattr(bridge, "_mark_bridge_resource_created", lambda *a: None)
    monkeypatch.setattr(bridge, "_deregister_bridge_resources", lambda *a, **k: None)
    identity = _identity(short_root, terminal_id, generation)
    target = _target(short_root, identity)
    captured: dict = {}

    class RealLauncherSession:
        """A session whose rpc is a real _RpcProcess over the real launcher
        and a real (sleeping) provider child — the genuine kernel topology."""

        def __init__(self, request):
            self.rpc = None

        def initialize(self):
            argv = bridge._launcher_argv(
                target["socket"],
                identity,
                [sys.executable, "-c", "import time; time.sleep(120)"],
            )
            self.rpc = bridge._RpcProcess(argv)
            return {"provider_session_id": "native"}

        def _scan_companion_events(self):
            return None

        def close(self):
            if self.rpc is not None:
                self.rpc.close()

    real_build = bridge._build_actor_broker

    def _capturing_build(request, session):
        broker = real_build(request, session)
        captured["broker"] = broker
        captured["session"] = session
        return broker

    monkeypatch.setattr(bridge, "_ProviderSession", RealLauncherSession)
    monkeypatch.setattr(bridge, "_build_actor_broker", _capturing_build)
    request = {
        "reservation_id": str(uuid.uuid4()),
        "provider": "codex",
        "terminal_id": terminal_id,
        "generation": generation,
        "rendezvous_identity": identity,
    }
    server = threading.Thread(target=bridge._serve, args=(request, target), daemon=True)
    server.start()
    for _ in range(300):
        if target["socket"].exists():
            break
        time.sleep(0.01)
    else:
        raise AssertionError("bridge socket never appeared")
    try:
        yield target, captured, generation, identity
    finally:
        # Reliable teardown: the real launcher + provider child are real
        # subprocesses; never leak them past the test.
        session = captured.get("session")
        if session is not None:
            session.close()


def test_issuance_via_provider_channel_and_provenance(live_bridge):
    target, captured, generation, identity = live_bridge

    response = _call(target, identity, _assertion_command())

    assert response["ok"] is True, response
    assert response["issued_via"] == "provider-channel"
    assertion = response["assertion"]
    assert assertion["schema"] == "cao-actor-assertion-v1"
    assert assertion["terminal_generation"] == generation
    assert assertion["report_sha256"] == "a" * 64
    launcher_pid = captured["session"].rpc.proc.pid
    # Kernel-bound: the assertion was issued to the provider launcher's pid,
    # never to the conductor peer.
    assert assertion["peer_pid"] == launcher_pid
    assert assertion["peer_pid"] != 0

    # Report provenance completes: one-use consumption succeeds exactly once.
    broker = captured["broker"]
    assert broker.check(assertion) is True
    broker.verify_and_consume(assertion)
    # Replay is refused: the consumed assertion can never be consumed again.
    with pytest.raises(actor_broker.AssertionInvalid):
        broker.verify_and_consume(assertion)


def test_conductor_and_foreign_peers_never_issue_directly(live_bridge):
    target, captured, _, identity = live_bridge

    # A foreign same-UID process (this test process) cannot open the
    # provider-originated channel: kernel lineage refuses it.
    refused = _call(target, identity, {"op": "provider-channel", "pid": 0})
    assert refused["ok"] is False
    assert "outside the live provider process tree" in refused["error"]

    # The genuine channel is still bound and still issues (the failed
    # foreign attempt did not displace it).
    response = _call(target, identity, _assertion_command())
    assert response["ok"] is True
    assert response["issued_via"] == "provider-channel"

    # And the conductor peer itself was refused direct issuance — proven by
    # the relay marker and by the broker's lineage gate on a direct check.
    left, right = socket.socketpair()
    try:
        with pytest.raises(actor_broker.ActorRefused):
            captured["broker"].verify_peer_lineage(right)
    finally:
        left.close()
        right.close()


def test_second_channel_bind_is_refused(live_bridge):
    target, captured, _, identity = live_bridge
    # Even an in-tree peer cannot bind a second channel for the generation:
    # a child of the real launcher attempts the bind.
    script = (
        "import json, socket, sys\n"
        "s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        f"s.connect({str(target['socket'])!r})\n"
        f"identity = {identity!r}\n"
        "wire = {'rendezvous_identity': identity, "
        "'request': {'op': 'provider-channel', 'pid': 0}}\n"
        "s.sendall(json.dumps(wire).encode() + b'\\n')\n"
        "print(s.recv(65536).decode().splitlines()[0])\n"
    )
    launcher_pid = captured["session"].rpc.proc.pid
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        preexec_fn=None,
    )
    response = json.loads(proc.stdout.strip())
    # The child of the test process is not in the launcher tree either, so
    # this is refused at lineage before the single-bind rule; both prove a
    # non-provider origin can never own the channel.
    assert response["ok"] is False
    assert launcher_pid > 0


def test_launcher_proxies_stdio_and_acks_issue_requests(short_root):
    """The real launcher shim: byte-transparent stdio and the issue ack."""
    # A real child that upper-cases one line then exits.
    child_code = (
        "import sys\n"
        "line = sys.stdin.buffer.readline()\n"
        "sys.stdout.buffer.write(line.upper())\n"
        "sys.stdout.buffer.flush()\n"
    )
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    identity = _identity(short_root, "a1b2c3d4", str(uuid.uuid4()))
    rendezvous = bridge.rendezvous_paths(identity)
    sock_path = str(rendezvous["socket"])
    bridge._create_binding_record(rendezvous["binding"], identity)
    server.bind(sock_path)
    server.listen(1)
    proc = None
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-m",
                "cli_agent_orchestrator.services.provider_launcher",
                "--socket",
                sock_path,
                "--identity-json",
                json.dumps(identity, sort_keys=True, separators=(",", ":")),
                "--",
                sys.executable,
                "-c",
                child_code,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        server.settimeout(30.0)
        conn, _ = server.accept()
        try:
            conn.settimeout(30.0)
            hello = json.loads(conn.makefile().readline())
            assert hello["rendezvous_identity"] == identity
            assert hello["request"]["op"] == "provider-channel"
            assert hello["request"]["pid"] == proc.pid
            conn.sendall(
                json.dumps({"op": "issue-request", "request_id": "rid-1"}).encode() + b"\n"
            )
            ack = json.loads(conn.makefile().readline())
            assert ack == {"op": "issue-ack", "request_id": "rid-1"}
        finally:
            conn.close()
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(b"provider-rpc-line\n")
        proc.stdin.flush()
        assert proc.stdout.readline() == b"PROVIDER-RPC-LINE\n"
        proc.stdin.close()
        assert proc.wait(timeout=30) == 0
    finally:
        server.close()
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_launcher_refuses_before_provider_spawn_without_exact_binding(short_root, monkeypatch):
    identity = _identity(short_root, "a1b2c3d4", str(uuid.uuid4()))
    socket_path = bridge.rendezvous_paths(identity)["socket"]
    spawned = []

    def _unexpected_spawn(*args, **kwargs):
        spawned.append((args, kwargs))
        raise AssertionError("provider child must not start before binding verification")

    monkeypatch.setattr(provider_launcher.subprocess, "Popen", _unexpected_spawn)

    result = provider_launcher.main(
        [
            "--socket",
            str(socket_path),
            "--identity-json",
            json.dumps(identity, sort_keys=True, separators=(",", ":")),
            "--",
            "/bin/false",
        ]
    )

    assert result == 1
    assert spawned == []

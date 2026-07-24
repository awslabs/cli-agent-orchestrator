"""cond-0082: bounded, full-identity managed bridge rendezvous."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest

from cli_agent_orchestrator.services import managed_provider_bridge as bridge
from cli_agent_orchestrator.services import resource_registry as rr


@pytest.fixture
def rendezvous_env(tmp_path, monkeypatch):
    with tempfile.TemporaryDirectory(prefix="cao-rv-", dir="/tmp") as runtime:
        runtime_root = Path(runtime) / "owner"
        monkeypatch.setattr(bridge, "RENDEZVOUS_ROOT", runtime_root)
        monkeypatch.setattr(bridge, "BRIDGE_ROOT", tmp_path / "state")
        rr.reset_resource_registry()
        registry = rr.get_resource_registry(tmp_path / "registry.sqlite")
        try:
            yield runtime_root, registry
        finally:
            rr.reset_resource_registry()


def _identity(worktree: Path, **changes):
    value = {
        "project": "cao-conductor-self-heal",
        "task_id": "self-heal-control-plane-recovery-fix-cond0081-activation-observation",
        "terminal_id": "a1b2c3d4",
        "terminal_generation": "22222222-2222-4222-8222-222222222222",
        "worktree_realpath": str(worktree.resolve()),
        "repository": "cli-agent-orchestrator",
        "head": "1" * 40,
        "actor": "deadbeef",
    }
    value.update(changes)
    return value


def _request(worktree: Path, **identity_changes):
    identity = _identity(worktree, **identity_changes)
    return {
        "bridge_version": bridge.BRIDGE_VERSION,
        "reservation_id": str(uuid.uuid4()),
        "terminal_id": identity["terminal_id"],
        "generation": identity["terminal_generation"],
        "delivery_id": str(uuid.uuid4()),
        "provider": "codex",
        "rendezvous_identity": identity,
    }


def _target(tmp_path: Path, request: dict):
    root = tmp_path / request["reservation_id"]
    target = {
        "root": root,
        "request": root / "request.json",
        "state": root / "state.json",
    }
    target["root"].mkdir(parents=True, exist_ok=True)
    target.update(bridge.rendezvous_paths(request["rendezvous_identity"]))
    return target


def _bind_path(path: Path) -> socket.socket:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    path.chmod(0o600)
    return server


def _declare_and_claim(request: dict, target: dict) -> int:
    bridge._declare_bridge_resources(target, request)
    identity, descriptor = bridge._claim_rendezvous(request, target)
    assert identity == request["rendezvous_identity"]
    return descriptor


def _publish_claim(request: dict, target: dict, descriptor: int) -> None:
    bridge._publish_socket_claim(
        descriptor,
        target["binding"],
        target["socket"],
        request["rendezvous_identity"],
    )


def test_rendezvous_digest_uses_domain_fixed_order_and_trailing_newline(rendezvous_env, tmp_path):
    identity = _identity(tmp_path)
    canonical = bridge._rendezvous_canonical_bytes(identity)

    assert canonical.endswith(b"\n") and not canonical.endswith(b"\n\n")
    payload = json.loads(canonical)
    assert list(payload) == ["domain", *bridge.RENDEZVOUS_IDENTITY_FIELDS]
    assert payload["domain"] == bridge.RENDEZVOUS_DIGEST_DOMAIN
    assert {field: payload[field] for field in bridge.RENDEZVOUS_IDENTITY_FIELDS} == identity
    digest = hashlib.sha256(canonical).hexdigest()
    assert bridge._rendezvous_digest(identity) == digest
    assert bridge._rendezvous_key(identity) == f"sk-{digest[:16]}"


@pytest.mark.parametrize(
    "changes,error",
    [
        ({"task_id": ""}, "empty fields"),
        ({"worktree_realpath": "/tmp/../tmp"}, "not canonical"),
        ({"head": "NOT-A-FULL-LOWERCASE-OID"}, "not a full lowercase hex OID"),
    ],
)
def test_binding_identity_rejects_ambiguous_full_tuple_fields(tmp_path, changes, error):
    with pytest.raises(bridge.BridgeError, match=error):
        bridge._validate_binding_identity(_identity(tmp_path, **changes))

    with pytest.raises(bridge.BridgeError, match="incomplete or malformed"):
        bridge._validate_binding_identity({"terminal_id": "a1b2c3d4"})


def test_launch_binding_refuses_a_missing_canonical_worktree(tmp_path):
    identity = _identity(tmp_path, worktree_realpath=str(tmp_path / "missing"))

    with pytest.raises(bridge.BridgeError, match="worktree identity drifted"):
        bridge.verify_launch_binding_identity(identity)


def test_rendezvous_root_and_path_bounds_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "RENDEZVOUS_ROOT", tmp_path / "missing" / "runtime")
    with pytest.raises(bridge.BridgeError, match="runtime directory is unavailable"):
        bridge._secure_rendezvous_root()

    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(mode=0o755)
    runtime_root.chmod(0o755)
    monkeypatch.setattr(bridge, "RENDEZVOUS_ROOT", runtime_root)
    with pytest.raises(bridge.BridgeError, match="not owner-only"):
        bridge._secure_rendezvous_root()

    runtime_root.chmod(0o700)
    monkeypatch.setattr(bridge, "_AF_UNIX_SAFE_PATH_BYTES", 1)
    with pytest.raises(bridge.BridgeError, match="exceeds the safe AF_UNIX bound"):
        bridge.rendezvous_paths(_identity(tmp_path))


def test_socket_identity_record_rejects_malformed_or_unsafe_shapes():
    with pytest.raises(bridge.BridgeError, match="socket-binding-record-malformed"):
        bridge._validate_socket_identity({})

    wrong_type = {
        "st_dev": 1,
        "st_ino": 1,
        "st_mode": 0,
        "st_uid": 1,
        "st_size": 0,
        "st_mtime_ns": 1,
        "st_ctime_ns": True,
    }
    with pytest.raises(bridge.BridgeError, match="socket-binding-record-malformed"):
        bridge._validate_socket_identity(wrong_type)

    wrong_type["st_ctime_ns"] = 1
    with pytest.raises(bridge.BridgeError, match="socket-binding-record-malformed"):
        bridge._validate_socket_identity(wrong_type)


def test_long_cond0081_worktree_kept_exact_while_socket_is_bounded(
    rendezvous_env, tmp_path, monkeypatch
):
    runtime_root, _ = rendezvous_env
    worktree = tmp_path
    for index in range(8):
        worktree = worktree / (
            f"unchanged-cond0081-control-plane-recovery-worktree-segment-{index}"
        )
    worktree.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.test"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=worktree, check=True)
    (worktree / "proof.txt").write_text("unchanged", encoding="utf-8")
    subprocess.run(["git", "add", "proof.txt"], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-qm", "proof"], cwd=worktree, check=True)

    identity = bridge.launch_binding_identity(
        project="cao-conductor-self-heal",
        task_id="self-heal-control-plane-recovery-fix-cond0081-activation-observation",
        terminal_id="a1b2c3d4",
        terminal_generation="22222222-2222-4222-8222-222222222222",
        working_directory=str(worktree.resolve()),
        actor="deadbeef",
    )
    request = _request(worktree)
    request["rendezvous_identity"] = identity
    target = bridge.write_request(request["reservation_id"], request)

    assert len(os.fsencode(identity["worktree_realpath"])) > bridge._AF_UNIX_SAFE_PATH_BYTES
    assert identity["worktree_realpath"] == str(worktree.resolve())
    assert target["socket"].parent == runtime_root
    assert len(os.fsencode(target["socket"])) <= bridge._AF_UNIX_SAFE_PATH_BYTES
    assert re.fullmatch(r"sk-[a-f0-9]{16}\.sock", target["socket"].name)
    assert identity["worktree_realpath"] not in str(target["socket"])

    class _AdmittingSession:
        def __init__(self, _request):
            self.rpc = None
            self._turn_sequence = 1
            self.provider_session_id = "native-cond0081"
            self.readiness = {"provider_version": "test"}

        def initialize(self):
            return {
                "provider_session_id": self.provider_session_id,
                "provider_version": "test",
            }

        def _scan_companion_events(self):
            return None

        def admit(self, command):
            assert command["message"] == identity["task_id"]
            return {
                "delivery_id": command["delivery_id"],
                "provider_turn_id": "turn-cond0081",
            }

        def close(self):
            return None

    monkeypatch.setattr(bridge, "_ProviderSession", _AdmittingSession)
    monkeypatch.setattr(bridge, "_build_actor_broker", lambda *_: None)
    thread = threading.Thread(target=bridge._serve, args=(request, target), daemon=True)
    thread.start()
    for _ in range(200):
        if target["socket"].exists():
            break
        time.sleep(0.01)
    else:
        raise AssertionError("unchanged cond0081 bridge socket never appeared")

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(str(target["socket"]))
        client.sendall(
            json.dumps(
                {
                    "rendezvous_identity": identity,
                    "request": {
                        "op": "admit",
                        "delivery_id": str(uuid.uuid4()),
                        "message": identity["task_id"],
                    },
                }
            ).encode()
            + b"\n"
        )
        response = json.loads(client.makefile().readline())
        assert response["ok"] is True
        assert response["receipt"]["provider_turn_id"] == "turn-cond0081"
        bridge.verify_rendezvous_binding(target["socket"], identity)
        row = rr.get_resource_registry().resolve(target["socket"].name)
        assert row["binding_identity"] == identity
    finally:
        client.close()


def test_exact_duplicate_is_refused_without_unlink(rendezvous_env, tmp_path):
    request = _request(tmp_path)
    target = _target(tmp_path, request)
    descriptor = _declare_and_claim(request, target)
    server = _bind_path(target["socket"])
    _publish_claim(request, target, descriptor)
    before = target["binding"].read_bytes()
    try:
        with pytest.raises(bridge.BridgeError, match="duplicate-live"):
            bridge._claim_rendezvous(request, target)
        assert target["socket"].exists()
        assert target["binding"].read_bytes() == before
    finally:
        server.close()
        os.close(descriptor)


def test_duplicate_startup_does_not_clobber_live_bridge_state(rendezvous_env, tmp_path):
    request = _request(tmp_path)
    target = bridge.write_request(request["reservation_id"], request)
    descriptor = _declare_and_claim(request, target)
    server = _bind_path(target["socket"])
    _publish_claim(request, target, descriptor)
    live_state = b'{"bridge_version":"live","state":"ready"}\n'
    target["state"].write_bytes(live_state)
    try:
        assert bridge._serve(request, target) == 1
        assert target["state"].read_bytes() == live_state
        assert target["socket"].exists()
        assert target["binding"].exists()
    finally:
        server.close()
        os.close(descriptor)


def test_forced_digest_collision_refuses_with_zero_foreign_unlink(
    rendezvous_env, tmp_path, monkeypatch
):
    monkeypatch.setattr(bridge, "_rendezvous_key", lambda _identity: "sk-0000000000000000")
    first = _request(tmp_path, task_id="foreign-task")
    second = _request(tmp_path, task_id="intended-task")
    first_target = _target(tmp_path, first)
    second_target = _target(tmp_path, second)
    descriptor = _declare_and_claim(first, first_target)
    server = _bind_path(first_target["socket"])
    _publish_claim(first, first_target, descriptor)
    before = first_target["binding"].read_bytes()
    try:
        with pytest.raises(bridge.BridgeError, match="socket-identity-collision"):
            bridge._claim_rendezvous(second, second_target)
        assert first_target["socket"].exists()
        assert first_target["binding"].read_bytes() == before
    finally:
        server.close()
        os.close(descriptor)


@pytest.mark.parametrize("record_kind", ["absent", "malformed"])
def test_existing_socket_with_absent_or_malformed_record_never_unlinks(
    rendezvous_env, tmp_path, record_kind
):
    request = _request(tmp_path)
    target = _target(tmp_path, request)
    bridge._declare_bridge_resources(target, request)
    if record_kind == "malformed":
        target["binding"].write_text("{not-json", encoding="utf-8")
        target["binding"].chmod(0o600)
    server = _bind_path(target["socket"])
    before = target["binding"].read_bytes() if target["binding"].exists() else None
    try:
        with pytest.raises(bridge.BridgeError, match=f"record-{record_kind}"):
            bridge._claim_rendezvous(request, target)
        assert target["socket"].exists()
        assert (target["binding"].read_bytes() if target["binding"].exists() else None) == before
    finally:
        server.close()


class _ReadySession:
    def __init__(self, request):
        self.rpc = None

    def initialize(self):
        return {"provider_session_id": "native"}

    def _scan_companion_events(self):
        return None

    def close(self):
        return None


def test_handshake_mismatch_is_journaled_and_keeps_rendezvous(
    rendezvous_env, tmp_path, monkeypatch
):
    request = _request(tmp_path)
    target = _target(tmp_path, request)
    monkeypatch.setattr(bridge, "_ProviderSession", _ReadySession)
    monkeypatch.setattr(bridge, "_deregister_bridge_resources", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bridge, "_build_actor_broker", lambda *_: None)
    monkeypatch.setattr(bridge, "verify_launch_binding_identity", lambda *_: None)
    thread = threading.Thread(target=bridge._serve, args=(request, target), daemon=True)
    thread.start()
    for _ in range(200):
        if target["socket"].exists():
            break
        time.sleep(0.01)
    else:
        raise AssertionError("bridge socket never appeared")

    foreign = {**request["rendezvous_identity"], "actor": "cafebabe"}
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(str(target["socket"]))
        client.sendall(
            json.dumps({"rendezvous_identity": foreign, "request": {"op": "status"}}).encode()
            + b"\n"
        )
        response = json.loads(client.makefile().readline())
    finally:
        client.close()
    assert response == {
        "ok": False,
        "error": "connection-handshake-identity-mismatch",
    }
    state = json.loads(target["state"].read_text(encoding="utf-8"))
    assert state["handshake_refusals"][-1]["reason"] == ("connection-handshake-identity-mismatch")
    assert target["socket"].exists()
    assert target["binding"].exists()


def test_stale_cleanup_requires_closed_exact_registry_tuple(rendezvous_env, tmp_path):
    _, registry = rendezvous_env
    request = _request(tmp_path)
    identity = request["rendezvous_identity"]
    target = _target(tmp_path, request)
    descriptor = _declare_and_claim(request, target)
    server = _bind_path(target["socket"])
    _publish_claim(request, target, descriptor)
    server.close()
    entry_id = target["socket"].name
    socket_identity = bridge.verify_rendezvous_binding(target["socket"], identity).record[
        "socket_identity"
    ]
    registry.register_created(
        entry_id,
        actor_id="managed_provider_bridge._serve",
        observed={
            "observed_fs_path": str(target["socket"]),
            "observed_fs_identity": socket_identity,
        },
        existence_receipt_digest="1" * 64,
    )
    live = registry.resolve(entry_id)
    with pytest.raises(bridge.BridgeError, match="proven-dead"):
        bridge.cleanup_stale_rendezvous(
            live,
            terminal_id=request["terminal_id"],
            generation=request["generation"],
        )
    assert target["socket"].exists() and target["binding"].exists()

    registry.drain(entry_id, actor_id="terminal_service.delete_terminal")
    registry.close(entry_id, actor_id="terminal_service.delete_terminal")
    closed = registry.resolve(entry_id)
    bridge.cleanup_stale_rendezvous(
        closed,
        terminal_id=request["terminal_id"],
        generation=request["generation"],
    )
    assert not target["socket"].exists()
    assert not target["binding"].exists()
    os.close(descriptor)


def test_declared_pre_bind_crash_compare_deletes_only_its_exact_sidecar(rendezvous_env, tmp_path):
    request = _request(tmp_path)
    target = bridge.write_request(request["reservation_id"], request)
    bridge._declare_bridge_resources(target, request)
    _, descriptor = bridge._claim_rendezvous(request, target)

    bridge._deregister_bridge_resources(target, request)
    os.close(descriptor)

    assert not target["socket"].exists()
    assert not target["binding"].exists()
    socket_row = rr.get_resource_registry().resolve(target["socket"].name)
    assert socket_row["lifecycle_state"] == "aborted"


def test_production_order_recovers_crash_after_o_excl_sidecar(
    rendezvous_env, tmp_path, monkeypatch
):
    _, registry = rendezvous_env
    request = _request(tmp_path)
    target = bridge.write_request(request["reservation_id"], request)
    real_claim = bridge._claim_rendezvous

    class _SimulatedHardCrash(BaseException):
        pass

    def _crash_after_claim(actual_request, actual_target):
        identity, descriptor = real_claim(actual_request, actual_target)
        row = registry.resolve(actual_target["socket"].name)
        assert row["lifecycle_state"] == "declared"
        assert row["binding_identity"] == identity
        assert actual_target["binding"].exists()
        assert not actual_target["socket"].exists()
        os.close(descriptor)  # process death releases the pinned claim lock
        raise _SimulatedHardCrash

    monkeypatch.setattr(bridge, "_claim_rendezvous", _crash_after_claim)
    with pytest.raises(_SimulatedHardCrash):
        bridge._serve(request, target)

    assert target["binding"].exists()
    assert not target["socket"].exists()
    assert registry.resolve(target["socket"].name)["lifecycle_state"] == "declared"

    monkeypatch.setattr(bridge, "_claim_rendezvous", real_claim)
    bridge._declare_bridge_resources(target, request)
    identity, descriptor = bridge._claim_rendezvous(request, target)
    try:
        assert identity == request["rendezvous_identity"]
        assert bridge._read_binding_record(target["binding"])["socket_identity"] is None
    finally:
        bridge._deregister_bridge_resources(target, request)
        os.close(descriptor)


def test_stale_cleanup_refuses_foreign_socket_path_takeover(rendezvous_env, tmp_path):
    _, registry = rendezvous_env
    request = _request(tmp_path)
    identity = request["rendezvous_identity"]
    target = _target(tmp_path, request)
    descriptor = _declare_and_claim(request, target)
    owned_server = _bind_path(target["socket"])
    _publish_claim(request, target, descriptor)
    socket_identity = bridge.verify_rendezvous_binding(target["socket"], identity).record[
        "socket_identity"
    ]
    registry.register_created(
        target["socket"].name,
        actor_id="managed_provider_bridge._serve",
        observed={
            "observed_fs_path": str(target["socket"]),
            "observed_fs_identity": socket_identity,
        },
        existence_receipt_digest="2" * 64,
    )
    registry.drain(
        target["socket"].name,
        actor_id="terminal_service.delete_terminal",
    )
    registry.close(
        target["socket"].name,
        actor_id="terminal_service.delete_terminal",
    )
    closed = registry.resolve(target["socket"].name)

    owned_server.close()
    target["socket"].unlink()
    foreign_server = _bind_path(target["socket"])
    foreign_info = target["socket"].lstat()
    foreign_inode = foreign_info.st_ino
    assert bridge._socket_identity_record(foreign_info) != socket_identity
    try:
        with pytest.raises(bridge.BridgeError, match="socket-identity-collision"):
            bridge.cleanup_stale_rendezvous(
                closed,
                terminal_id=request["terminal_id"],
                generation=request["generation"],
            )
        assert target["socket"].lstat().st_ino == foreign_inode
        assert target["binding"].exists()
        assert bridge._read_binding_record(target["binding"])["binding_identity"] == identity
    finally:
        foreign_server.close()
        os.close(descriptor)


def test_request_connect_swap_sends_zero_bytes_to_foreign_socket(
    rendezvous_env, tmp_path, monkeypatch
):
    request = _request(tmp_path)
    target = bridge.write_request(request["reservation_id"], request)
    descriptor = _declare_and_claim(request, target)
    owned_server = _bind_path(target["socket"])
    owned_server.listen(1)
    _publish_claim(request, target, descriptor)

    real_socket = socket.socket
    foreign: dict[str, socket.socket] = {}

    class _SwappingClient:
        def __init__(self, *args, **kwargs):
            self._socket = real_socket(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._socket, name)

        def connect(self, path):
            target["socket"].unlink()
            foreign_server = real_socket(socket.AF_UNIX, socket.SOCK_STREAM)
            foreign_server.bind(path)
            Path(path).chmod(0o600)
            foreign_server.listen(1)
            foreign["server"] = foreign_server
            self._socket.connect(path)

        def close(self):
            self._socket.close()

    monkeypatch.setattr(bridge.socket, "socket", _SwappingClient)
    with pytest.raises(bridge.BridgeError, match="socket-identity-collision"):
        bridge.request_bridge(request["reservation_id"], {"op": "status"}, timeout=0.2)

    foreign_server = foreign["server"]
    foreign_server.settimeout(2)
    connection, _ = foreign_server.accept()
    try:
        connection.settimeout(2)
        assert connection.recv(65536) == b""
    finally:
        connection.close()
        foreign_server.close()
        owned_server.close()
        os.close(descriptor)
    assert target["binding"].exists()


def test_bridge_rechecks_head_immediately_before_socket_bind(rendezvous_env, tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "base.txt"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=bridge-test",
            "-c",
            "user.email=bridge@example.test",
            "commit",
            "-qm",
            "base",
        ],
        cwd=tmp_path,
        check=True,
    )
    identity = bridge.launch_binding_identity(
        project="cao-conductor-self-heal",
        task_id="cond0081",
        terminal_id="a1b2c3d4",
        terminal_generation="22222222-2222-4222-8222-222222222222",
        working_directory=str(tmp_path.resolve()),
        actor="deadbeef",
    )
    request = _request(tmp_path)
    request["rendezvous_identity"] = identity
    target = bridge.write_request(request["reservation_id"], request)

    (tmp_path / "drift.txt").write_text("drift\n", encoding="utf-8")
    subprocess.run(["git", "add", "drift.txt"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=bridge-test",
            "-c",
            "user.email=bridge@example.test",
            "commit",
            "-qm",
            "drift",
        ],
        cwd=tmp_path,
        check=True,
    )
    initialized = []

    class _MustNotInitialize:
        def __init__(self, _request):
            self.rpc = None

        def initialize(self):
            initialized.append(True)
            raise AssertionError("provider initialization must not follow HEAD drift")

        def close(self):
            return None

    monkeypatch.setattr(bridge, "_ProviderSession", _MustNotInitialize)

    assert bridge._serve(request, target) == 1
    assert initialized == []
    assert not target["socket"].exists()
    state = json.loads(target["state"].read_text(encoding="utf-8"))
    assert state["state"] == "launch-failed-bridge"
    assert "repository/head identity drifted" in state["error"]

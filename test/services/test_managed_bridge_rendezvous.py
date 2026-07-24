"""cond-0082: bounded, full-identity managed bridge rendezvous."""

from __future__ import annotations

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
        "provider": "codex",
        "rendezvous_identity": identity,
    }


def _target(tmp_path: Path, request: dict):
    target = {
        "root": tmp_path / "bridge-state",
        "request": tmp_path / "bridge-state" / "request.json",
        "state": tmp_path / "bridge-state" / "state.json",
    }
    target["root"].mkdir(parents=True, exist_ok=True)
    target.update(bridge.rendezvous_paths(request["rendezvous_identity"]))
    return target


def _bind_path(path: Path) -> socket.socket:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    return server


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
    bridge._claim_rendezvous(request, target)
    server = _bind_path(target["socket"])
    before = target["binding"].read_bytes()
    try:
        with pytest.raises(bridge.BridgeError, match="duplicate-live"):
            bridge._claim_rendezvous(request, target)
        assert target["socket"].exists()
        assert target["binding"].read_bytes() == before
    finally:
        server.close()


def test_duplicate_startup_does_not_clobber_live_bridge_state(rendezvous_env, tmp_path):
    request = _request(tmp_path)
    target = bridge.write_request(request["reservation_id"], request)
    bridge._claim_rendezvous(request, target)
    server = _bind_path(target["socket"])
    live_state = b'{"bridge_version":"live","state":"ready"}\n'
    target["state"].write_bytes(live_state)
    try:
        assert bridge._serve(request, target) == 1
        assert target["state"].read_bytes() == live_state
        assert target["socket"].exists()
        assert target["binding"].exists()
    finally:
        server.close()


def test_forced_digest_collision_refuses_with_zero_foreign_unlink(
    rendezvous_env, tmp_path, monkeypatch
):
    monkeypatch.setattr(bridge, "_rendezvous_key", lambda _identity: "sk-0000000000000000")
    first = _request(tmp_path, task_id="foreign-task")
    second = _request(tmp_path, task_id="intended-task")
    first_target = _target(tmp_path, first)
    second_target = _target(tmp_path, second)
    bridge._claim_rendezvous(first, first_target)
    server = _bind_path(first_target["socket"])
    before = first_target["binding"].read_bytes()
    try:
        with pytest.raises(bridge.BridgeError, match="socket-identity-collision"):
            bridge._claim_rendezvous(second, second_target)
        assert first_target["socket"].exists()
        assert first_target["binding"].read_bytes() == before
    finally:
        server.close()


@pytest.mark.parametrize("record_kind", ["absent", "malformed"])
def test_existing_socket_with_absent_or_malformed_record_never_unlinks(
    rendezvous_env, tmp_path, record_kind
):
    request = _request(tmp_path)
    target = _target(tmp_path, request)
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
    monkeypatch.setattr(bridge, "_declare_bridge_resources", lambda *_: None)
    monkeypatch.setattr(bridge, "_mark_bridge_resource_created", lambda *_: None)
    monkeypatch.setattr(bridge, "_deregister_bridge_resources", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bridge, "_build_actor_broker", lambda *_: None)
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
    bridge._create_binding_record(target["binding"], identity)
    server = _bind_path(target["socket"])
    server.close()
    entry_id = target["socket"].name
    registry.declare(
        entry_id=entry_id,
        kind="socket",
        protocol_vintage="v2",
        terminal_id=request["terminal_id"],
        generation=request["generation"],
        owner="fork",
        ownership="owned",
        constructor_id="managed_provider_bridge._serve",
        deleter_id="terminal_service.delete_terminal",
        rollback_rule="generation-isolated",
        actor_id="managed_provider_bridge._serve",
        desired_fs_path=str(target["socket"]),
        binding_identity=identity,
    )
    registry.register_created(
        entry_id,
        actor_id="managed_provider_bridge._serve",
        observed={"observed_fs_path": str(target["socket"])},
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


def test_declared_pre_bind_crash_compare_deletes_only_its_exact_sidecar(rendezvous_env, tmp_path):
    request = _request(tmp_path)
    identity = request["rendezvous_identity"]
    target = bridge.write_request(request["reservation_id"], request)
    bridge._declare_bridge_resources(target, request)
    bridge._create_binding_record(target["binding"], identity)

    bridge._deregister_bridge_resources(target, request)

    assert not target["socket"].exists()
    assert not target["binding"].exists()
    socket_row = rr.get_resource_registry().resolve(target["socket"].name)
    assert socket_row["lifecycle_state"] == "aborted"

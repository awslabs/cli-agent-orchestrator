from __future__ import annotations

import hashlib
import json
import os
import pathlib

import pytest

from cli_agent_orchestrator.services import managed_provider_bridge as bridge


def _request(tmp_path, *, provider="codex", model="gpt-5.6-sol", effort="xhigh"):
    executable = tmp_path / provider
    executable.write_text("provider")
    executable.chmod(0o755)
    request = {
        "bridge_version": bridge.BRIDGE_VERSION,
        "reservation_id": "11111111-1111-4111-8111-111111111111",
        "terminal_id": "deadbeef",
        "generation": "22222222-2222-4222-8222-222222222222",
        "delivery_id": "33333333-3333-4333-8333-333333333333",
        "provider": provider,
        "agent_profile": "reviewer",
        "profile_sha256": "a" * 64,
        "model": model,
        "effort": effort,
        "working_directory": str(tmp_path),
        "provider_executable": str(executable),
        "provider_executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
    }
    request["rendezvous_identity"] = {
        "project": "test-project",
        "task_id": "test-task",
        "terminal_id": request["terminal_id"],
        "terminal_generation": request["generation"],
        "worktree_realpath": str(tmp_path),
        "repository": "test-repository",
        "head": "1" * 40,
        "actor": "cafebabe",
    }
    return request


def _admission(request):
    message = "review exact head"
    return {
        "op": "admit",
        "reservation_id": request["reservation_id"],
        "terminal_id": request["terminal_id"],
        "generation": request["generation"],
        "delivery_id": request["delivery_id"],
        "message": message,
        "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
        "sender_id": "cafebabe",
        "orchestration_type": "assign",
        "context": {"task_sha256": "b" * 64, "dossier_sha256": "c" * 64},
    }


def _material():
    return {
        "profile": object(),
        "profile_sha256": "a" * 64,
        "allowed_tools": ["*"],
        "system_prompt": "review carefully",
        "mcp_servers": [],
    }


class _CodexRpc:
    def __init__(self, argv, *, env=None, companion_identity=None):
        self.argv = argv
        self.calls = []
        self._notifications = []

    def notifications_since(self, index):
        return list(self._notifications[index:]), len(self._notifications)

    def request(self, method, params, timeout=30.0):
        self.calls.append((method, params))
        if method == "initialize":
            return {"protocolVersion": 1}
        if method == "config/read":
            return {
                "config": {"projects": {params["cwd"]: {"trust_level": "trusted"}}},
                "origins": ["sessionFlags"],
            }
        if method == "thread/start":
            return {
                "thread": {"id": "thread_provider_opaque"},
                "model": "gpt-5.6-sol",
                "reasoningEffort": "xhigh",
                "cwd": params["cwd"],
            }
        if method == "turn/start":
            return {"turn": {"id": "turn_provider_opaque"}}
        raise AssertionError(method)

    def notify(self, method, params):
        self.calls.append((method, params))

    def close(self):
        pass


def test_codex_readiness_and_submission_share_exact_provider_process(tmp_path, monkeypatch):
    request = _request(tmp_path)
    clients = []

    def fake_rpc(*args, **kwargs):
        client = _CodexRpc(*args, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(bridge, "_profile_material", lambda *_: _material())
    monkeypatch.setattr(bridge, "_RpcProcess", fake_rpc)
    monkeypatch.setattr(bridge, "_contains_session_flags", lambda _: True)
    monkeypatch.setattr(bridge._ProviderSession, "_version", lambda *_: "codex-cli 0.145.0")
    monkeypatch.setattr(bridge, "_file_digest_or_absent", lambda _: "d" * 64)

    session = bridge._ProviderSession(request)
    readiness = session.initialize()
    submission = session.admit(_admission(request))

    assert len(clients) == 1
    assert readiness["receipt_id"] == "thread_provider_opaque"
    assert readiness["provider_session_id"] == "thread_provider_opaque"
    assert submission["receipt_id"] == "turn_provider_opaque"
    assert submission["provider_turn_id"] == "turn_provider_opaque"
    assert submission["provider_session_id"] == readiness["provider_session_id"]
    assert [method for method, _ in clients[0].calls] == [
        "initialize",
        "initialized",
        "config/read",
        "thread/start",
        "turn/start",
    ]


def _fake_codex_executable(tmp_path, request, banner: str):
    # Rewrite the request's own executable path as an executable script.
    executable = pathlib.Path(request["provider_executable"])
    executable.write_text(f"#!/bin/sh\necho '{banner}'\n")
    executable.chmod(0o755)
    request["provider_executable_sha256"] = hashlib.sha256(executable.read_bytes()).hexdigest()
    return executable


def test_codex_version_gate_accepts_exact_0145_0(tmp_path, monkeypatch):
    # The real fail-closed gate (no _version stub) accepts the pinned
    # codex-cli 0.145.0 banner exactly.
    request = _request(tmp_path)
    executable = _fake_codex_executable(tmp_path, request, "codex-cli 0.145.0")
    monkeypatch.setattr(bridge, "_profile_material", lambda *_: _material())
    session = bridge._ProviderSession(request)
    assert session._version(str(executable), bridge.SUPPORTED_CODEX_VERSION) == "codex-cli 0.145.0"


@pytest.mark.parametrize("banner", ["codex-cli 0.144.6", "codex-cli 0.145.1", "codex 0.145.0"])
def test_codex_version_gate_fails_closed_off_pin(tmp_path, monkeypatch, banner):
    # The retired pin, an adjacent patch, and a renamed banner all fail
    # closed — the gate is exact, never a range, minimum, or prefix match.
    request = _request(tmp_path)
    executable = _fake_codex_executable(tmp_path, request, banner)
    monkeypatch.setattr(bridge, "_profile_material", lambda *_: _material())
    session = bridge._ProviderSession(request)
    with pytest.raises(bridge.BridgeError, match="unsupported provider version"):
        session._version(str(executable), bridge.SUPPORTED_CODEX_VERSION)


class _KimiRpc:
    def __init__(self, argv, *, env=None, companion_identity=None):
        self.argv = argv
        self.env = env
        self.calls = []

    def notifications_since(self, index):
        return [], 0

    def request(self, method, params, timeout=30.0):
        self.calls.append((method, params))
        if method == "initialize":
            return {"protocolVersion": 1}
        if method == "session/new":
            return {
                "sessionId": "session_provider_opaque",
                "configOptions": [
                    {"id": "model", "category": "model", "currentValue": "kimi-code/k3"},
                    {"id": "thinking", "category": "thought_level", "currentValue": "max"},
                ],
            }
        raise AssertionError(method)

    def notification_count(self):
        return 0

    def start_request(self, method, params):
        self.calls.append((method, params))
        return 91

    def wait_notification(self, predicate, *, start_index, timeout):
        update = {
            "method": "session/update",
            "params": {
                "sessionId": "session_provider_opaque",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Starting review."},
                },
            },
        }
        assert predicate(update)
        return update

    def close(self):
        pass


def test_kimi_receipt_never_promotes_client_rpc_id_to_provider_identity(tmp_path, monkeypatch):
    request = _request(tmp_path, provider="kimi_cli", model="kimi-code/k3", effort="max")
    wire = tmp_path / "wire.jsonl"
    wire.write_text("")
    monkeypatch.setattr(bridge, "_profile_material", lambda *_: _material())
    monkeypatch.setattr(bridge, "_RpcProcess", _KimiRpc)
    monkeypatch.setattr(bridge._ProviderSession, "_version", lambda *_: "0.29.0")
    monkeypatch.setattr(bridge, "_kimi_wire_path", lambda *_: wire)
    monkeypatch.setattr(
        bridge,
        "_wait_kimi_turn_start",
        lambda *_args, **_kwargs: {
            "type": "step.begin",
            "uuid": "provider-step-opaque",
            "turnId": "0",
            "step": 1,
        },
    )

    session = bridge._ProviderSession(request)
    readiness = session.initialize()
    submission = session.admit(_admission(request))

    assert readiness["receipt_id"] == "session_provider_opaque"
    assert submission["receipt_id"] == "provider-step-opaque"
    assert submission["provider_turn_id"] == "provider-step-opaque"
    assert ":rpc:" not in submission["receipt_id"]
    assert submission["provider_accepted"] is True


def test_kimi_inventory_names_match_final_provider_child_environment(tmp_path, monkeypatch):
    request = _request(tmp_path, provider="kimi_cli", model="kimi-code/k3", effort="max")
    wire = tmp_path / "wire.jsonl"
    wire.write_text("")
    isolated_environment = {
        "HOME": "/home/kimi",
        "PATH": "/ambient/bin",
        "KIMI_CODE_HOME": "/provider/kimi",
        "KIMI_MODEL_THINKING_EFFORT": "low",
        "CODEX_HOME": "/foreign/codex",
    }
    monkeypatch.setattr(os, "environ", isolated_environment)
    monkeypatch.setattr(bridge, "_BOUND_PROVIDER_ENV", None)
    monkeypatch.setattr(bridge, "_profile_material", lambda *_: _material())
    monkeypatch.setattr(bridge, "_RpcProcess", _KimiRpc)
    monkeypatch.setattr(bridge._ProviderSession, "_version", lambda *_: "0.29.0")
    monkeypatch.setattr(bridge, "_kimi_wire_path", lambda *_: wire)

    inventory = bridge._bind_bridge_environment(request)
    session = bridge._ProviderSession(request)
    session.initialize()
    child_environment = session.rpc.env

    assert inventory["names"] == sorted(child_environment)
    assert (
        inventory["names_sha256"]
        == bridge._environment_inventory("kimi_cli", list(child_environment))["names_sha256"]
    )
    assert child_environment["KIMI_MODEL_THINKING_EFFORT"] == "max"
    assert "CODEX_HOME" not in child_environment
    serialized = json.dumps(inventory, sort_keys=True)
    assert "max" not in serialized
    assert "/provider/kimi" not in serialized


def test_kimi_turn_receipt_comes_from_structured_provider_journal(tmp_path):
    wire = tmp_path / "wire.jsonl"
    wire.write_text(
        '{"type":"turn.prompt","input":"review"}\n'
        '{"type":"context.append_loop_event","event":'
        '{"type":"step.begin","uuid":"provider-step-opaque","turnId":"7","step":1}}\n'
    )

    event = bridge._wait_kimi_turn_start(wire, start_offset=0, timeout=0.1)

    assert event["uuid"] == "provider-step-opaque"
    assert event["turnId"] == "7"


# -- P1-7/P1-10: companion producers (final conformance §20.2f) ---------------


def _codex_session(tmp_path, monkeypatch):
    request = _request(tmp_path)
    monkeypatch.setattr(bridge, "_profile_material", lambda *_: _material())
    monkeypatch.setattr(bridge, "_RpcProcess", _CodexRpc)
    monkeypatch.setattr(bridge, "_contains_session_flags", lambda _: True)
    monkeypatch.setattr(bridge._ProviderSession, "_version", lambda *_: "codex-cli 0.145.0")
    monkeypatch.setattr(bridge, "_file_digest_or_absent", lambda _: "d" * 64)
    session = bridge._ProviderSession(request)
    session.initialize()
    return request, session


def test_deliver_inbox_records_exact_provider_turn_ack(tmp_path, monkeypatch):
    # P1-7: the bridge submits one exact inbox message to the provider turn
    # and records the generation-bound acknowledgement — digest-bound, no
    # message body, exactly once.
    from cli_agent_orchestrator.services import companion_receipts

    monkeypatch.setattr(companion_receipts, "COMPANION_DIR", tmp_path / "companion")
    request, session = _codex_session(tmp_path, monkeypatch)
    command = {
        "op": "deliver",
        "reservation_id": request["reservation_id"],
        "terminal_id": request["terminal_id"],
        "generation": request["generation"],
        "message_id": "msg-1",
        "message": "ping",
        "message_sha256": hashlib.sha256(b"ping").hexdigest(),
        "sender_id": "cafebabe",
    }
    receipt = session.deliver_inbox(command)
    assert receipt["provider_turn_id"] == "turn_provider_opaque"
    assert receipt["provider_receipt_kind"] == "codex-turn-start"
    assert receipt["receiver_id"] == "deadbeef"

    ack = companion_receipts.get_message_ack("deadbeef", request["generation"], "msg-1")
    assert ack["kind"] == "submitted"
    assert ack["message_sha256"] == command["message_sha256"]
    assert ack["receiver_generation"] == request["generation"]
    assert ack["provider_session_id"] == "thread_provider_opaque"
    assert ack["provider_turn_id"] == "turn_provider_opaque"
    assert "message" not in ack
    # the per-turn route identity moved to the exact provider turn
    route = companion_receipts.get_route("deadbeef", request["generation"])
    assert route["turn_id"] == "turn_provider_opaque"
    # a wrong-generation reader is never served
    assert companion_receipts.get_message_ack("deadbeef", "gen-X", "msg-1") is None

    # digest mismatch and identity drift refuse BEFORE any provider I/O
    import pytest

    with pytest.raises(bridge.BridgeError):
        session.deliver_inbox({**command, "message_id": "msg-2", "message_sha256": "0" * 64})
    with pytest.raises(bridge.BridgeError):
        session.deliver_inbox({**command, "message_id": "msg-3", "reservation_id": "gen-X"})
    # the refused messages recorded no ack
    assert companion_receipts.get_message_ack("deadbeef", request["generation"], "msg-2") is None
    assert companion_receipts.get_message_ack("deadbeef", request["generation"], "msg-3") is None


def test_reverse_request_prompt_lifecycle_is_observation_only(tmp_path, monkeypatch):
    # P1-10: a provider-native reverse request is recorded as a pending
    # structured prompt and closed when answered — observation only.
    from cli_agent_orchestrator.services import companion_receipts

    monkeypatch.setattr(companion_receipts, "COMPANION_DIR", tmp_path / "companion")
    events = []
    real_record = companion_receipts.record_prompt
    real_clear = companion_receipts.clear_prompt
    monkeypatch.setattr(
        companion_receipts,
        "record_prompt",
        lambda *a, **k: events.append(("record", a, k)) or real_record(*a, **k),
    )
    monkeypatch.setattr(
        companion_receipts,
        "clear_prompt",
        lambda *a, **k: events.append(("clear", a, k)) or real_clear(*a, **k),
    )
    rpc = object.__new__(bridge._RpcProcess)
    rpc._companion_identity = ("deadbeef", "gen-1")
    sent = []
    rpc._send = sent.append
    rpc._answer_reverse_request(
        {
            "id": 7,
            "method": "session/request_permission",
            "params": {
                "title": "Allow tool call?",
                "options": [
                    {"optionId": "allow", "kind": "allow_once", "name": "Allow once"},
                    {"optionId": "deny", "kind": "reject_once", "name": "Deny"},
                ],
            },
        }
    )
    kinds = [kind for kind, _a, _k in events]
    assert kinds == ["record", "clear"]
    _, args, kwargs = events[0]
    assert args[0] == "deadbeef" and args[1] == "gen-1"
    assert kwargs["text"] == "Allow tool call?"
    assert kwargs["choices"] == ["Allow once", "Deny"]
    # the bridge's existing managed answer policy is unchanged
    assert sent[0]["result"]["outcome"] == {
        "outcome": "selected",
        "optionId": "allow",
    }


def test_provider_error_items_become_generation_bound_refusal_receipts(tmp_path, monkeypatch):
    # P1-10: the provider's own structured error items are recorded as
    # refusal receipts bound to the exact generation and current turn.
    from cli_agent_orchestrator.services import companion_receipts

    monkeypatch.setattr(companion_receipts, "COMPANION_DIR", tmp_path / "companion")
    request, session = _codex_session(tmp_path, monkeypatch)
    session._current_turn_id = "turn-7"
    rpc = session.rpc
    rpc._notifications.append(
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "id": "item-9",
                    "type": "error",
                    "message": "This content cannot be shown",
                }
            },
        }
    )
    session._scan_companion_events()
    refusal = companion_receipts.get_refusal("deadbeef", request["generation"])
    assert refusal["refusal_id"] == "item-9"
    assert refusal["identity"] == "This content cannot be shown"
    assert refusal["turn_id"] == "turn-7"
    assert companion_receipts.get_refusal("deadbeef", "gen-X") is None
    # a rescan of the same notifications is idempotent (index advanced)
    session._scan_companion_events()
    assert (
        companion_receipts.get_refusal("deadbeef", request["generation"])["refusal_id"] == "item-9"
    )


def test_bridge_environment_is_pruned_to_minimal_allowlist(monkeypatch):
    # The bridge and provider child are both composed from bounded inputs.
    # Foreign provider controls are removed before the fail-closed guard,
    # while the target provider retains only its own non-route controls.
    import json
    import os

    ambient = {
        "HOME": "/home/test",
        "PATH": "/hostile/bin:/usr/bin",
        "CAO_TERMINAL_ID": "deadbeef",
        "CAO_CONDUCTOR_SHIM_DIR": "/pinned/shim",
        "CAO_WORKFLOW_RUN_ID": "run-1",
        "CODEX_CI": "1",
        "CODEX_MANAGED_BY_NPM": "1",
        "CODEX_MANAGED_PACKAGE_ROOT": "/secret/codex",
        "CODEX_THREAD_ID": "thread-secret",
        "KIMI_CODE_HOME": "/provider/kimi",
        "KIMI_PROVIDER_TOKEN": "provider-secret",
        "CONDUCT_DEV_ALLOW_ABSENT_DEPLOY_RECEIPT": "1",
        "UNRELATED_AMBIENT_VARIABLE": "x",
    }
    bridge_env, provider_env, inventory = bridge._provider_bound_environments("kimi_cli", ambient)

    assert bridge_env == {
        "HOME": "/home/test",
        "PATH": f"/pinned/shim:{bridge._MINIMAL_PATH}",
        "CAO_TERMINAL_ID": "deadbeef",
        "CAO_CONDUCTOR_SHIM_DIR": "/pinned/shim",
        "CAO_WORKFLOW_RUN_ID": "run-1",
    }
    assert provider_env == {
        **bridge_env,
        "KIMI_CODE_HOME": "/provider/kimi",
        "KIMI_PROVIDER_TOKEN": "provider-secret",
    }
    for name in (
        "CODEX_CI",
        "CODEX_MANAGED_BY_NPM",
        "CODEX_MANAGED_PACKAGE_ROOT",
        "CODEX_THREAD_ID",
        "CONDUCT_DEV_ALLOW_ABSENT_DEPLOY_RECEIPT",
        "UNRELATED_AMBIENT_VARIABLE",
    ):
        assert name not in bridge_env
        assert name not in provider_env
        assert name not in inventory["names"]
    serialized_inventory = json.dumps(inventory, sort_keys=True)
    assert "provider-secret" not in serialized_inventory
    assert "/provider/kimi" not in serialized_inventory

    # Exercise the destructive launch-boundary scrub against an isolated
    # environment mapping so this test never alters the pytest process.
    isolated_environment = dict(ambient)
    monkeypatch.setattr(os, "environ", isolated_environment)
    monkeypatch.setattr(bridge, "_BOUND_PROVIDER_ENV", None)
    assert bridge._prune_bridge_environment("kimi_cli") == inventory
    assert dict(os.environ) == bridge_env
    bridge._assert_bridge_environment()


def test_bridge_guard_refuses_controls_injected_after_scrub(monkeypatch):
    import os

    ambient = {
        "HOME": "/home/test",
        "CODEX_CI": "1",
        "CODEX_THREAD_ID": "thread-secret",
    }
    monkeypatch.setattr(os, "environ", ambient)
    monkeypatch.setattr(bridge, "_BOUND_PROVIDER_ENV", None)
    bridge._prune_bridge_environment("kimi_cli")
    os.environ["CODEX_THREAD_ID"] = "injected-after-scrub"

    with pytest.raises(bridge.BridgeError, match="CODEX_THREAD_ID"):
        bridge._assert_bridge_environment()


def test_write_request_refuses_missing_or_changed_delivery_before_disk_io(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "BRIDGE_ROOT", tmp_path / "bridge-root")
    monkeypatch.setattr(bridge, "_secure_rendezvous_root", lambda: bridge.pathlib.Path("/tmp"))
    request = _request(tmp_path)
    missing = dict(request)
    missing.pop("delivery_id")

    with pytest.raises(bridge.BridgeError, match="canonical delivery_id"):
        bridge.write_request(request["reservation_id"], missing)
    assert not (bridge.BRIDGE_ROOT / request["reservation_id"]).exists()

    bridge.write_request(request["reservation_id"], request)
    changed = {
        **request,
        "delivery_id": "44444444-4444-4444-8444-444444444444",
    }
    with pytest.raises(bridge.BridgeError, match="identity changed"):
        bridge.write_request(request["reservation_id"], changed)


# -- P1 bridge wiring regressions (fence atomicity, heartbeat producer) ------


def _v2_session(tmp_path, monkeypatch):
    """A minimal v2-identified session over a patched companion dir."""
    from cli_agent_orchestrator import constants

    companion = tmp_path / "companion"
    monkeypatch.setattr(constants, "COMPANION_DIR", companion)
    request = _request(tmp_path)
    request.update(
        {
            "project": "cao-conductor-self-heal",
            "task_id": "self-heal-demo-task",
            "run_id": "run-0001",
            "obligation_generation": "obgen-7c2e4a1b",
            "assigned_policy_sha256": "7" * 64,
        }
    )
    session = bridge._ProviderSession.__new__(bridge._ProviderSession)
    session.request = request
    session.provider = request["provider"]
    session.rpc = object()
    session.provider_session_id = "thread_provider_opaque"
    session.readiness = {"provider_version": "0.145.0"}
    session.current_model = request["model"]
    session.current_effort = request["effort"]
    session._current_turn_id = None
    session._heartbeat_producer = None
    session.kimi_wire_path = None
    session._companion_scan_index = 0
    return session, companion, request


def _bound_generation(companion, request):
    from cli_agent_orchestrator.services import heartbeat_store as hb
    from cli_agent_orchestrator.services.destructive_endpoint import write_binding_record

    token = hb.issue_fencing_token(
        companion, request["terminal_id"], request["generation"], "attempt-1"
    )
    write_binding_record(
        companion,
        terminal_id=request["terminal_id"],
        generation=request["generation"],
        reservation_id=request["reservation_id"],
        attempt_id="attempt-1",
        launch_nonce_digest="a" * 64,
        fencing_token_id=token.id,
        provider=request["provider"],
        native_session_id="thread_provider_opaque",
        assigned_policy_sha256=request["assigned_policy_sha256"],
        route_payload_sha256="c" * 64,
    )
    return token


def test_emit_beat_retains_producer_and_rehydrates_across_sessions(tmp_path, monkeypatch):
    # HB-1 bridge-wiring durable regression: the bridge keeps ONE producer
    # for its lifetime, and a reconstructed bridge (fresh session object)
    # rehydrates the durable epoch/sequence instead of restarting at zero
    # (the per-beat construction made every second beat a refused
    # regression and silently killed liveness).
    import json as _json

    from cli_agent_orchestrator.services import heartbeat_store as hb

    monkeypatch.setattr(hb, "COALESCE_SECONDS", 0)  # every beat writes
    session, companion, request = _v2_session(tmp_path, monkeypatch)
    _bound_generation(companion, request)
    session._emit_beat("turn-1", "codex-turn-start:turn-1")
    first_producer = session._heartbeat_producer
    assert first_producer is not None
    session._emit_beat("turn-2", "codex-turn-start:turn-2")
    assert session._heartbeat_producer is first_producer  # retained, not rebuilt
    record = _json.loads(
        hb.heartbeat_path(companion, request["terminal_id"], request["generation"]).read_bytes()
    )
    assert record["seq"] == 2
    # A reconstructed bridge (new session, fresh producer) continues the
    # sequence — before the fix this beat regressed to seq 0/1 and was
    # refused by the fencing compare step.
    restarted, _, _ = _v2_session(tmp_path, monkeypatch)
    restarted._emit_beat("turn-3", "codex-turn-start:turn-3")
    record = _json.loads(
        hb.heartbeat_path(companion, request["terminal_id"], request["generation"]).read_bytes()
    )
    assert record["seq"] == 3
    assert record["epoch"] == 1


def test_admission_holds_fence_lock_across_provider_io(tmp_path, monkeypatch):
    # FENCE-1 bridge-wiring durable regression: the generation fence lock is
    # held across the final fence recheck AND the provider/model/tool-entry
    # I/O, so a fence installed concurrent with an admission cannot land
    # between the check and the submission — it waits, and every later
    # admission is refused.
    import threading

    from cli_agent_orchestrator import constants
    from cli_agent_orchestrator.services import generation_fence as gf

    session, companion, request = _v2_session(tmp_path, monkeypatch)
    submitted: list = []

    def fake_submit(message, **_kwargs):
        submitted.append(message)
        return "turn-race", "codex-turn-start", {"source": "test"}

    session._submit_provider_turn = fake_submit
    session._scan_companion_events = lambda: None
    session._emit_beat = lambda *_args: None

    rechecked = threading.Event()
    finish_io = threading.Event()
    real_check = gf.assert_admission_open

    def check_then_pause(companion_dir, terminal_id, generation):
        real_check(companion_dir, terminal_id, generation)
        rechecked.set()
        assert finish_io.wait(timeout=10)

    monkeypatch.setattr(gf, "assert_admission_open", check_then_pause)
    admission = _admission(request)
    outcome: list = []

    def admit():
        try:
            outcome.append(session.admit(admission))
        except Exception as exc:  # noqa: BLE001 - the test records the outcome
            outcome.append(exc)

    worker = threading.Thread(target=admit)
    worker.start()
    assert rechecked.wait(timeout=10)
    installed: list = []

    def install():
        installed.append(
            gf.install_fence(
                constants.COMPANION_DIR,
                terminal_id=request["terminal_id"],
                generation=request["generation"],
                vintage="v2",
                request={
                    "schema": gf.FENCE_REQUEST_SCHEMA,
                    "terminal_generation": request["generation"],
                    "obligation_generation": request["obligation_generation"],
                    "attempt_id": "attempt-1",
                    "intent_id": "3d813cbb-47fb-42ba-91df-831e1593ac29",
                    "report_sha256": "a" * 64,
                },
                fencing_token_id="token-1",
            )
        )

    installer = threading.Thread(target=install)
    installer.start()
    installer.join(timeout=2)
    # The fence cannot interleave with the in-flight admission's provider I/O.
    assert installer.is_alive()
    finish_io.set()
    worker.join(timeout=10)
    installer.join(timeout=10)
    assert not worker.is_alive() and not installer.is_alive()
    assert submitted == [admission["message"]]
    assert outcome[0]["provider_turn_id"] == "turn-race"
    assert installed[0]["outcome"] == gf.OUTCOME_FENCED
    # Every admission after the fence is refused before any provider I/O.
    monkeypatch.setattr(gf, "assert_admission_open", real_check)
    import pytest

    with pytest.raises(bridge.BridgeError, match="sealed"):
        session.admit(admission)
    assert submitted == [admission["message"]]


def test_actor_broker_built_for_generation_private_uds(tmp_path, monkeypatch):
    # ACTOR durable regression: the production broker construction exists —
    # bound to the exact generation-private state dir and refusing once the
    # fencing registry names a superseding generation.
    from cli_agent_orchestrator.services import heartbeat_store as hb
    from cli_agent_orchestrator.services.actor_broker import (
        AssertionInvalid,
        PeerCredentials,
        platform_supported,
    )

    session, companion, request = _v2_session(tmp_path, monkeypatch)
    session.rpc = None  # no live provider process in this unit test
    _bound_generation(companion, request)
    broker = bridge._build_actor_broker(request, session)
    if not platform_supported():
        assert broker is None  # unwired capability is never advertised
        return
    assert broker is not None
    assert broker._dir == companion / request["terminal_id"] / request["generation"]
    issue_kwargs = dict(
        report_sha256="a" * 64,
        report_path="/abs/report.md",
        project="p",
        task_id="t",
        run_id="r",
        obligation_generation="o",
        attempt_id="attempt-1",
        native_session_id="n",
        launch_nonce_digest="b" * 64,
        route_chain_head="c" * 64,
        peer=PeerCredentials(pid=999999, uid=501),
    )
    # After supersession the broker's generation gate closes first.
    hb.issue_fencing_token(companion, request["terminal_id"], "gen-superseding", "attempt-2")
    with pytest.raises(AssertionInvalid, match="superseded"):
        broker.issue(None, **issue_kwargs)

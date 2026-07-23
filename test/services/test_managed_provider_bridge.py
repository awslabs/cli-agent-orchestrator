from __future__ import annotations

import hashlib

from cli_agent_orchestrator.services import managed_provider_bridge as bridge


def _request(tmp_path, *, provider="codex", model="gpt-5.6-sol", effort="xhigh"):
    executable = tmp_path / provider
    executable.write_text("provider")
    executable.chmod(0o755)
    return {
        "bridge_version": bridge.BRIDGE_VERSION,
        "reservation_id": "11111111-1111-4111-8111-111111111111",
        "terminal_id": "deadbeef",
        "generation": "22222222-2222-4222-8222-222222222222",
        "provider": provider,
        "agent_profile": "reviewer",
        "profile_sha256": "a" * 64,
        "model": model,
        "effort": effort,
        "working_directory": str(tmp_path),
        "provider_executable": str(executable),
        "provider_executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
    }


def _admission(request):
    message = "review exact head"
    return {
        "op": "admit",
        "reservation_id": request["reservation_id"],
        "terminal_id": request["terminal_id"],
        "generation": request["generation"],
        "delivery_id": "33333333-3333-4333-8333-333333333333",
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
    monkeypatch.setattr(bridge._ProviderSession, "_version", lambda *_: "codex-cli 0.144.6")
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
    monkeypatch.setattr(bridge._ProviderSession, "_version", lambda *_: "codex-cli 0.144.6")
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
    # P1-9 (final conformance §20.2f): the bridge's OWN environment becomes
    # the fresh minimal allowlist — unrelated ambient variables (incl.
    # protected conductor/route variables) never leak through it, and PATH is
    # the fixed minimal value, never inherited.
    import os

    monkeypatch.setenv("HOME", "/home/test")
    monkeypatch.setenv("UNRELATED_AMBIENT_VARIABLE", "x")
    monkeypatch.setenv("CONDUCT_DEV_ALLOW_ABSENT_DEPLOY_RECEIPT", "1")
    monkeypatch.setenv("PATH", "/hostile/bin:/usr/bin")
    bridge._prune_bridge_environment()
    assert os.environ.get("UNRELATED_AMBIENT_VARIABLE") is None
    assert os.environ.get("CONDUCT_DEV_ALLOW_ABSENT_DEPLOY_RECEIPT") is None
    assert os.environ.get("HOME") == "/home/test"
    assert os.environ.get("PATH") == bridge._MINIMAL_PATH

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
    def __init__(self, argv, *, env=None):
        self.argv = argv
        self.calls = []

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
    def __init__(self, argv, *, env=None):
        self.argv = argv
        self.env = env
        self.calls = []

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

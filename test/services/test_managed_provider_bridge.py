from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import uuid
from types import SimpleNamespace

import pytest

from cli_agent_orchestrator.services import managed_provider_bridge as bridge
from cli_agent_orchestrator.services import provider_contracts


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
    monkeypatch.setattr(bridge._ProviderSession, "_version", lambda *_: "codex-cli 0.146.0")
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


def test_codex_version_gate_accepts_exact_0146_0(tmp_path, monkeypatch):
    # The real fail-closed gate (no _version stub) accepts the pinned
    # codex-cli 0.146.0 banner exactly.
    request = _request(tmp_path)
    executable = _fake_codex_executable(tmp_path, request, "codex-cli 0.146.0")
    monkeypatch.setattr(bridge, "_profile_material", lambda *_: _material())
    session = bridge._ProviderSession(request)
    assert (
        session._version(str(executable), provider_contracts.PROVIDER_CODEX) == "codex-cli 0.146.0"
    )


@pytest.mark.parametrize(
    "banner",
    ["codex-cli 0.145.0", "codex-cli 0.146.1", "codex 0.145.0"],
)
def test_codex_version_gate_accepts_semver_updates(tmp_path, monkeypatch, banner):
    # Open enforcement accepts semver-shaped updates at launch.  Exact
    # feature authority remains build-specific in the capability tables.
    request = _request(tmp_path)
    executable = _fake_codex_executable(tmp_path, request, banner)
    monkeypatch.setattr(bridge, "_profile_material", lambda *_: _material())
    session = bridge._ProviderSession(request)
    assert session._version(str(executable), provider_contracts.PROVIDER_CODEX) == banner


def test_codex_version_gate_accepts_banner_with_pinned_semver(tmp_path, monkeypatch):
    # The bridge now normalizes the banner the same way check_pinned_version
    # does, so any banner containing the pinned semver is accepted.
    request = _request(tmp_path)
    executable = _fake_codex_executable(tmp_path, request, "codex 0.146.0")
    monkeypatch.setattr(bridge, "_profile_material", lambda *_: _material())
    session = bridge._ProviderSession(request)
    assert session._version(str(executable), provider_contracts.PROVIDER_CODEX) == "codex 0.146.0"


def _version_probe_run(observed, *, banner, slow_seconds=None):
    """A fake ``subprocess.run`` for the provider ``--version`` probe.

    ``slow_seconds`` models a provider that needs that long to answer: any
    observation deadline below it times out, exactly as the healthy pinned
    Kimi binary did under startup load against the fixed 5 s bound
    (cond-0313). No test sleeps; the slowness is simulated by what the
    fake accepts, not by wall clock.
    """

    def fake_run(argv, **kwargs):
        observed.update(kwargs)
        timeout = kwargs.get("timeout", 0)
        if slow_seconds is not None and timeout < slow_seconds:
            raise subprocess.TimeoutExpired(argv, timeout)
        return SimpleNamespace(returncode=0, stdout=banner, stderr="")

    return fake_run


def test_kimi_version_banner_observes_under_the_provider_bounded_deadline(tmp_path, monkeypatch):
    """COND-0313: a slow-but-valid Kimi ``--version`` is admitted, once.

    One bounded observation under the provider-appropriate deadline (20 s —
    the bound the native-TUI acceptance harness already allows for this
    exact probe), not a replayed launch and not the generic 5 s that
    failed a healthy pinned binary under startup load.
    """
    request = _request(tmp_path, provider="kimi_cli")
    observed = {}
    monkeypatch.setattr(
        "subprocess.run",
        _version_probe_run(observed, banner="kimi 0.31.0\n", slow_seconds=12.0),
    )
    banner = bridge.provider_version_banner(request, environment={"PATH": "/usr/bin"})
    assert banner == "kimi 0.31.0"
    assert observed["timeout"] == 20.0
    # The probe still runs in the exact child environment it was handed.
    assert observed["env"] == {"PATH": "/usr/bin"}


@pytest.mark.parametrize("provider", ["codex", "claude_code"])
def test_other_providers_keep_the_generic_version_deadline(tmp_path, monkeypatch, provider):
    # Only Kimi's runway is widened; every other provider observes under
    # the unchanged generic bound.
    request = _request(tmp_path, provider=provider)
    observed = {}
    monkeypatch.setattr("subprocess.run", _version_probe_run(observed, banner="x 1.0\n"))
    bridge.provider_version_banner(request, environment={"PATH": "/usr/bin"})
    assert observed["timeout"] == 5.0


def test_an_explicit_version_deadline_is_honored(tmp_path, monkeypatch):
    request = _request(tmp_path, provider="kimi_cli")
    observed = {}
    monkeypatch.setattr("subprocess.run", _version_probe_run(observed, banner="kimi 0.31.0\n"))
    bridge.provider_version_banner(request, timeout=7.5, environment={"PATH": "/usr/bin"})
    assert observed["timeout"] == 7.5


def test_kimi_version_banner_beyond_the_provider_bound_fails_closed(tmp_path, monkeypatch):
    # The deadline stays finite, and the error keeps the exact failing
    # command and the deadline that fired.
    request = _request(tmp_path, provider="kimi_cli")

    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr("subprocess.run", fake_run)
    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        bridge.provider_version_banner(request, environment={"PATH": "/usr/bin"})
    assert "'--version'" in str(excinfo.value)
    assert "timed out after 20.0 seconds" in str(excinfo.value)


def test_version_banner_nonzero_exit_fails_closed(tmp_path, monkeypatch):
    request = _request(tmp_path, provider="kimi_cli")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    with pytest.raises(bridge.BridgeError, match="provider --version exited 1"):
        bridge.provider_version_banner(request, environment={"PATH": "/usr/bin"})


def test_kimi_bridge_version_gate_observes_under_the_provider_bounded_deadline(
    tmp_path, monkeypatch
):
    # The ACP bridge's own admission gate observes under the same
    # provider-appropriate bound as the native preflight banner.
    request = _request(tmp_path, provider="kimi_cli")
    monkeypatch.setattr(bridge, "_profile_material", lambda *_: _material())
    session = bridge._ProviderSession(request)
    observed = {}
    monkeypatch.setattr(
        "subprocess.run",
        _version_probe_run(observed, banner="0.31.0\n", slow_seconds=12.0),
    )
    version = session._version(request["provider_executable"], provider_contracts.PROVIDER_KIMI)
    assert version == "0.31.0"
    assert observed["timeout"] == 20.0


def test_codex_bridge_version_gate_keeps_the_generic_deadline(tmp_path, monkeypatch):
    request = _request(tmp_path, provider="codex")
    monkeypatch.setattr(bridge, "_profile_material", lambda *_: _material())
    session = bridge._ProviderSession(request)
    observed = {}
    monkeypatch.setattr(
        "subprocess.run", _version_probe_run(observed, banner="codex-cli 0.146.0\n")
    )
    version = session._version(request["provider_executable"], provider_contracts.PROVIDER_CODEX)
    assert version == "codex-cli 0.146.0"
    assert observed["timeout"] == 5.0


def test_bridge_version_gate_fails_closed_on_digest_drift_before_any_probe(tmp_path, monkeypatch):
    # Digest drift is detected before the probe runs at all: no provider
    # process may be started against an unpinned binary.
    request = _request(tmp_path, provider="kimi_cli")
    request["provider_executable_sha256"] = "0" * 64
    monkeypatch.setattr(bridge, "_profile_material", lambda *_: _material())
    session = bridge._ProviderSession(request)

    def forbidden_run(*args, **kwargs):
        raise AssertionError("the version probe must not run after digest drift")

    monkeypatch.setattr("subprocess.run", forbidden_run)
    with pytest.raises(bridge.BridgeError, match="digest changed after reservation"):
        session._version(request["provider_executable"], provider_contracts.PROVIDER_KIMI)


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
    monkeypatch.setattr(bridge._ProviderSession, "_version", lambda *_: "codex-cli 0.146.0")
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
        "sender_generation": "supervisor-generation",
        "message_created_at": "2026-07-30T12:00:00.000000Z",
        "expected_provider": "codex",
        "expected_provider_session_id": "thread_provider_opaque",
        "expected_execution_mode": "acp",
    }
    receipt = session.deliver_inbox(command)
    assert receipt["provider_turn_id"] == "turn_provider_opaque"
    assert receipt["provider_receipt_kind"] == "codex-turn-start"
    assert receipt["receiver_id"] == "deadbeef"

    ack = companion_receipts.get_message_ack("deadbeef", request["generation"], "msg-1")
    assert ack["kind"] == "submitted"
    assert ack["schema"] == "cao-model-turn-receipt-v1"
    assert ack["source"] == "provider-adapter"
    assert ack["sender_generation"] == "supervisor-generation"
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
    provider_submissions = []
    monkeypatch.setattr(
        session,
        "_submit_provider_turn",
        lambda *args, **kwargs: provider_submissions.append((args, kwargs)),
    )
    with pytest.raises(bridge.BridgeError, match="provider_session_id changed"):
        session.deliver_inbox(
            {
                **command,
                "message_id": "msg-4",
                "expected_provider_session_id": "replacement-session",
            }
        )
    assert provider_submissions == []
    # the refused messages recorded no ack
    assert companion_receipts.get_message_ack("deadbeef", request["generation"], "msg-2") is None
    assert companion_receipts.get_message_ack("deadbeef", request["generation"], "msg-3") is None
    assert companion_receipts.get_message_ack("deadbeef", request["generation"], "msg-4") is None


def test_parked_acp_inbox_refuses_before_provider_ack_or_receipt(tmp_path, monkeypatch):
    """A real M3/W13 receipt fences ACP inbox bytes, not just a mocked helper."""
    from cli_agent_orchestrator.services import companion_receipts
    from cli_agent_orchestrator.services import generation_fence as gf

    session, companion, request = _v2_session(tmp_path, monkeypatch)
    token = _bound_generation(companion, request)
    submitted = []
    acknowledgements = []
    route_receipts = []
    monkeypatch.setattr(
        session,
        "_submit_provider_turn",
        lambda *args, **kwargs: submitted.append((args, kwargs)),
    )
    monkeypatch.setattr(
        companion_receipts,
        "record_message_ack",
        lambda *args, **kwargs: acknowledgements.append((args, kwargs)),
    )
    monkeypatch.setattr(
        companion_receipts,
        "record_route_receipt",
        lambda *args, **kwargs: route_receipts.append((args, kwargs)),
    )
    gf.install_fence(
        companion,
        terminal_id=request["terminal_id"],
        generation=request["generation"],
        vintage="v2",
        fencing_token_id=token.id,
        request={
            "schema": gf.FENCE_REQUEST_SCHEMA,
            "terminal_generation": request["generation"],
            "obligation_generation": request["obligation_generation"],
            "attempt_id": request["attempt_id"],
            "intent_id": str(uuid.uuid4()),
            "report_sha256": "a" * 64,
        },
    )
    command = {
        "op": "deliver",
        "reservation_id": request["reservation_id"],
        "terminal_id": request["terminal_id"],
        "generation": request["generation"],
        "message_id": "parked-msg-1",
        "message": "do not submit",
        "message_sha256": hashlib.sha256(b"do not submit").hexdigest(),
        "sender_id": "supervisor",
        "expected_provider": request["provider"],
        "expected_provider_session_id": session.provider_session_id,
        "expected_execution_mode": "acp",
    }

    with pytest.raises(bridge.BridgeError, match="w13-fenced-before-provider-io"):
        session.deliver_inbox(command)

    assert submitted == []
    assert acknowledgements == []
    assert route_receipts == []


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


# -- COND-0315: the updater kill-switch is a managed-launch invariant ------


def test_the_route_environment_pins_the_kimi_updater_kill_switch(tmp_path):
    # The provider's supported background updater replaced the installed
    # binary mid-campaign four times running (0.30/0.31/0.32/0.33); the
    # deterministic kill-switch is pinned by the reservation, not by the
    # operator's ambient shell.
    route_env = bridge._provider_route_environment(
        _request(tmp_path, provider="kimi_cli", model="kimi-code/k3", effort="max")
    )
    assert route_env["KIMI_CODE_NO_AUTO_UPDATE"] == "1"
    assert route_env["KIMI_MODEL_THINKING_EFFORT"] == "max"


def test_the_kill_switch_never_reaches_a_non_kimi_route(tmp_path):
    codex_env = bridge._provider_route_environment(_request(tmp_path, provider="codex"))
    claude_env = bridge._provider_route_environment(
        _request(tmp_path, provider="claude_code", model="claude-x", effort="high")
    )
    assert "KIMI_CODE_NO_AUTO_UPDATE" not in codex_env
    assert "KIMI_CODE_NO_AUTO_UPDATE" not in claude_env


def test_an_ambient_conflicting_value_cannot_reenable_updates(tmp_path, monkeypatch):
    # The reservation-owned value wins over the ambient passthrough: an
    # operator shell carrying KIMI_CODE_NO_AUTO_UPDATE=0 (or any other
    # value) must not re-enable the updater for a CAO-managed Kimi launch.
    ambient = {
        "HOME": "/home/test",
        "KIMI_CODE_NO_AUTO_UPDATE": "0",
    }
    monkeypatch.setattr(bridge, "_BOUND_PROVIDER_ENV", None)
    bridge_env, provider_env, _ = bridge._provider_bound_environments("kimi_cli", ambient)
    assert provider_env["KIMI_CODE_NO_AUTO_UPDATE"] == "0"  # passthrough, pre-override
    monkeypatch.setattr(bridge, "_BOUND_PROVIDER_ENV", provider_env)
    child_env = bridge._provider_child_environment(
        _request(tmp_path, provider="kimi_cli", model="kimi-code/k3", effort="max")
    )
    assert child_env["KIMI_CODE_NO_AUTO_UPDATE"] == "1"


def test_the_version_banner_probe_observes_under_the_kill_switch(tmp_path, monkeypatch):
    # The preflight ``--version`` probe runs the update preflight too
    # (bundle-read: every CLI entry runs it), so it must observe under the
    # same suppression.
    request = _request(tmp_path, provider="kimi_cli", model="kimi-code/k3", effort="max")
    observed = {}

    def _run(argv, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="0.33.0\n", stderr="")

    monkeypatch.setattr("subprocess.run", _run)
    monkeypatch.setattr(bridge, "_BOUND_PROVIDER_ENV", {"HOME": "/home/test"})

    assert bridge.provider_version_banner(request) == "0.33.0"
    assert observed["env"]["KIMI_CODE_NO_AUTO_UPDATE"] == "1"


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
            "attempt_id": "attempt-1",
            "obligation_generation": "obgen-7c2e4a1b",
            "assigned_policy_sha256": "7" * 64,
        }
    )
    session = bridge._ProviderSession.__new__(bridge._ProviderSession)
    session.request = request
    session.provider = request["provider"]
    session.rpc = object()
    session.provider_session_id = "thread_provider_opaque"
    session.readiness = {"provider_version": "0.146.0"}
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
    token = _bound_generation(companion, request)
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
    _bound_generation(companion, request)
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


def test_successor_fencing_cannot_interleave_with_provider_io(tmp_path, monkeypatch):
    import threading

    from cli_agent_orchestrator.services import heartbeat_store as hb

    session, companion, request = _v2_session(tmp_path, monkeypatch)
    _bound_generation(companion, request)
    entered_io = threading.Event()
    finish_io = threading.Event()
    submitted = []

    def fake_submit(message, **_kwargs):
        entered_io.set()
        assert finish_io.wait(timeout=10)
        submitted.append(message)
        return "turn-successor-race", "codex-turn-start", {"source": "test"}

    session._submit_provider_turn = fake_submit
    session._scan_companion_events = lambda: None
    session._emit_beat = lambda *_args: None
    admission = _admission(request)
    outcome = []
    worker = threading.Thread(target=lambda: outcome.append(session.admit(admission)))
    worker.start()
    assert entered_io.wait(timeout=10)

    successor = []
    issuer = threading.Thread(
        target=lambda: successor.append(
            hb.issue_fencing_token(
                companion,
                request["terminal_id"],
                "successor-generation",
                "attempt-2",
            )
        )
    )
    issuer.start()
    issuer.join(timeout=0.1)
    assert issuer.is_alive()
    finish_io.set()
    worker.join(timeout=10)
    issuer.join(timeout=10)

    assert submitted == [admission["message"]]
    assert outcome[0]["provider_turn_id"] == "turn-successor-race"
    assert (
        hb.current_fencing_record(companion, request["terminal_id"])["generation"]
        == "successor-generation"
    )
    with pytest.raises(bridge.BridgeError, match="successor-fenced-before-provider-io"):
        session.admit(admission)
    assert submitted == [admission["message"]]


def test_parked_follow_up_is_journaled_as_generation_fenced_without_provider_call(
    tmp_path, monkeypatch
):
    """A /terminals/{id}/input follow-up is a byte lane, never ambiguous."""
    from cli_agent_orchestrator.services import generation_fence as gf

    session, companion, request = _v2_session(tmp_path, monkeypatch)
    token = _bound_generation(companion, request)
    session._config_options = []
    journal = bridge.SessionControlJournal(tmp_path / "session-control.sqlite")
    operation_id = "55555555-5555-4555-8555-555555555555"
    command = {
        "operation_id": operation_id,
        "action": "follow-up",
        "reservation_id": request["reservation_id"],
        "terminal_id": request["terminal_id"],
        "generation": request["generation"],
        "message": "do not send",
    }
    journal.begin(
        operation_id=operation_id,
        terminal_id=request["terminal_id"],
        generation=request["generation"],
        action="follow-up",
        request_sha256="d" * 64,
        provider=request["provider"],
        provider_session_id=session.provider_session_id,
    )
    calls = []
    session._submit_provider_turn = lambda *a, **kw: calls.append((a, kw))
    gf.install_fence(
        companion,
        terminal_id=request["terminal_id"],
        generation=request["generation"],
        vintage="v2",
        fencing_token_id=token.id,
        request={
            "schema": gf.FENCE_REQUEST_SCHEMA,
            "terminal_generation": request["generation"],
            "obligation_generation": request["obligation_generation"],
            "attempt_id": request["attempt_id"],
            "intent_id": str(uuid.uuid4()),
            "report_sha256": "a" * 64,
        },
    )

    receipt = session.session_operation(command, journal)

    assert receipt["state"] == "refused"
    assert receipt["reason_code"] == "generation_fenced"
    assert calls == []


def test_stale_follow_up_token_is_typed_generation_fenced_without_provider_call(
    tmp_path, monkeypatch
):
    """Successor drift uses the same typed follow-up boundary as a park."""
    from cli_agent_orchestrator.services import heartbeat_store

    session, companion, request = _v2_session(tmp_path, monkeypatch)
    _bound_generation(companion, request)
    heartbeat_store.issue_fencing_token(
        companion, request["terminal_id"], "successor-generation", "attempt-2"
    )
    session._config_options = []
    journal = bridge.SessionControlJournal(tmp_path / "stale-session-control.sqlite")
    operation_id = "66666666-6666-4666-8666-666666666666"
    command = {
        "operation_id": operation_id,
        "action": "follow-up",
        "reservation_id": request["reservation_id"],
        "terminal_id": request["terminal_id"],
        "generation": request["generation"],
        "message": "stale follow-up",
    }
    journal.begin(
        operation_id=operation_id,
        terminal_id=request["terminal_id"],
        generation=request["generation"],
        action="follow-up",
        request_sha256="e" * 64,
        provider=request["provider"],
        provider_session_id=session.provider_session_id,
    )
    calls = []
    session._submit_provider_turn = lambda *a, **kw: calls.append((a, kw))

    receipt = session.session_operation(command, journal)

    assert receipt["state"] == "refused"
    assert receipt["reason_code"] == "generation_fenced"
    assert calls == []


def test_parked_compact_is_typed_generation_fenced_before_start_request(tmp_path, monkeypatch):
    """Compact is provider input, so a park refuses it before ACP submission."""
    import threading

    from cli_agent_orchestrator.services import generation_fence as gf

    session, companion, request = _v2_session(tmp_path, monkeypatch)
    request["provider"] = "kimi_cli"
    session.provider = "kimi_cli"
    token = _bound_generation(companion, request)

    class Rpc:
        def __init__(self):
            self.calls = []

        def notifications_since(self, _index):
            return (
                [
                    {
                        "method": "session/update",
                        "params": {
                            "update": {
                                "sessionUpdate": "available_commands_update",
                                "availableCommands": [{"name": "compact"}],
                            }
                        },
                    }
                ],
                1,
            )

        def notification_count(self):
            return 1

        def start_request(self, method, params):
            self.calls.append((method, params))
            return "must-not-start"

    rpc = Rpc()
    session.rpc = rpc
    session._active_prompt_lock = threading.Lock()
    session._active_prompt_request_id = None
    journal = bridge.SessionControlJournal(tmp_path / "compact-session-control.sqlite")
    operation_id = "77777777-7777-4777-8777-777777777777"
    command = {
        "operation_id": operation_id,
        "action": "compact",
        "reservation_id": request["reservation_id"],
        "terminal_id": request["terminal_id"],
        "generation": request["generation"],
    }
    journal.begin(
        operation_id=operation_id,
        terminal_id=request["terminal_id"],
        generation=request["generation"],
        action="compact",
        request_sha256="f" * 64,
        provider=request["provider"],
        provider_session_id=session.provider_session_id,
    )
    gf.install_fence(
        companion,
        terminal_id=request["terminal_id"],
        generation=request["generation"],
        vintage="v2",
        fencing_token_id=token.id,
        request={
            "schema": gf.FENCE_REQUEST_SCHEMA,
            "terminal_generation": request["generation"],
            "obligation_generation": request["obligation_generation"],
            "attempt_id": request["attempt_id"],
            "intent_id": str(uuid.uuid4()),
            "report_sha256": "a" * 64,
        },
    )

    receipt = session.session_operation(command, journal)

    assert receipt["state"] == "refused"
    assert receipt["reason_code"] == "generation_fenced"
    assert rpc.calls == []


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

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import signal
import threading
import time
import uuid
from typing import Any, Optional

import pytest

from cli_agent_orchestrator.services import managed_launch
from cli_agent_orchestrator.services import managed_provider_bridge as bridge
from cli_agent_orchestrator.services import provider_contracts
from cli_agent_orchestrator.services.managed_event_renderer import ManagedEventRenderer


def _claude_request(
    tmp_path: pathlib.Path,
    *,
    model: str = "deepseek-v4-flash",
    effort: str = "high",
    provider_route: str = "deepseek",
) -> dict[str, Any]:
    executable = tmp_path / "claude"
    executable.write_text("#!/bin/sh\necho 'claude 2.1.233'\n")
    executable.chmod(0o755)
    request = {
        "bridge_version": bridge.BRIDGE_VERSION,
        "reservation_id": "11111111-1111-4111-8111-111111111111",
        "terminal_id": "deadbeef",
        "generation": "22222222-2222-4222-8222-222222222222",
        "delivery_id": "33333333-3333-4333-8333-333333333333",
        "provider": "claude_code",
        "agent_profile": "implementer",
        "profile_sha256": "a" * 64,
        "model": model,
        "effort": effort,
        "provider_route": provider_route,
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


def _admission(
    request: dict[str, Any], message: str = "implement COND-0415 repair"
) -> dict[str, Any]:
    return {
        "op": "admit",
        "reservation_id": request["reservation_id"],
        "terminal_id": request["terminal_id"],
        "generation": request["generation"],
        "delivery_id": request["delivery_id"],
        "message": message,
        "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
        "sender_id": "cafebabe",
        "orchestration_type": "assign",
        "context": {"task_sha256": "b" * 64, "dossier_sha256": "c" * 64},
    }


def _material() -> dict[str, Any]:
    return {
        "profile": object(),
        "profile_sha256": "a" * 64,
        "allowed_tools": ["*"],
        "system_prompt": "implement bounded repairs accurately",
        "mcp_servers": [],
    }


class _FakeClaudeProcess:
    def __init__(
        self,
        argv: list[str],
        *,
        env: Optional[dict[str, str]] = None,
        companion_identity: Any = None,
        provider: str = "claude_code",
    ):
        self.argv = argv
        self.env = env or {}
        self.provider = provider
        self.sent_messages: list[dict[str, Any]] = []
        self._notifications: list[dict[str, Any]] = []
        self.closed = False
        self.proc = self
        self.auto_complete = True
        self._condition = threading.Condition()

        # Extract session_id from argv
        self.session_id = "test-session-uuid"
        for i, arg in enumerate(argv):
            if arg == "--session-id" and i + 1 < len(argv):
                self.session_id = argv[i + 1]

    def poll(self) -> Optional[int]:
        return None if not self.closed else 0

    def send_signal(self, sig: int) -> None:
        if sig == signal.SIGINT:
            with self._condition:
                self._notifications.append(
                    {
                        "type": "result",
                        "session_id": self.session_id,
                        "uuid": "cancelled-turn",
                        "result": "Turn interrupted by operator.",
                        "stop_reason": "cancelled",
                    }
                )
                self._condition.notify_all()

    def finish_turn(self, turn_uuid: str = "turn-uuid") -> None:
        with self._condition:
            self._notifications.append(
                {
                    "type": "result",
                    "session_id": self.session_id,
                    "uuid": turn_uuid,
                    "result": "Completed task.",
                    "stop_reason": "end_turn",
                }
            )
            self._condition.notify_all()

    def _send(self, message: dict[str, Any]) -> None:
        self.sent_messages.append(message)
        if message.get("type") == "user":
            turn_uuid = f"turn-{uuid.uuid4()}"
            with self._condition:
                self._notifications.append(
                    {
                        "type": "user",
                        "message": message.get("message"),
                        "session_id": self.session_id,
                        "uuid": turn_uuid,
                    }
                )
                self._notifications.append(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Starting implementation."}],
                        },
                        "session_id": self.session_id,
                        "uuid": turn_uuid,
                    }
                )
                if self.auto_complete:
                    self._notifications.append(
                        {
                            "type": "result",
                            "session_id": self.session_id,
                            "uuid": turn_uuid,
                            "result": "Completed task.",
                            "stop_reason": "end_turn",
                        }
                    )
                self._condition.notify_all()

    def notification_count(self) -> int:
        with self._condition:
            return len(self._notifications)

    def notifications_since(self, index: int) -> tuple[list[dict[str, Any]], int]:
        with self._condition:
            return list(self._notifications[index:]), len(self._notifications)

    def wait_notification(
        self, predicate: Any, *, start_index: int, timeout: float
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        with self._condition:
            index = start_index
            while True:
                while index < len(self._notifications):
                    item = self._notifications[index]
                    index += 1
                    if predicate(item):
                        return item
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise bridge.BridgeError("notification predicate timed out")
                self._condition.wait(remaining)

    def close(self) -> None:
        self.closed = True


def test_claude_acp_readiness_and_task_submission(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _claude_request(tmp_path)
    procs: list[_FakeClaudeProcess] = []

    def fake_proc(*args: Any, **kwargs: Any) -> _FakeClaudeProcess:
        proc = _FakeClaudeProcess(*args, **kwargs)
        procs.append(proc)
        return proc

    isolated_env = {
        "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
        "ANTHROPIC_AUTH_TOKEN": "test-deepseek-token",
    }
    monkeypatch.setattr(os, "environ", isolated_env)
    monkeypatch.setattr(bridge, "_BOUND_PROVIDER_ENV", None)
    bridge._prune_bridge_environment("claude_code")
    monkeypatch.setattr(bridge, "_profile_material", lambda *_: _material())
    monkeypatch.setattr(bridge, "_RpcProcess", fake_proc)
    monkeypatch.setattr(bridge._ProviderSession, "_version", lambda *_: "claude 2.1.233")
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.claude_native_readiness.await_session_start",
        lambda _path, session_id, **_kw: {
            "session_id": session_id,
            "hook_event_name": "SessionStart",
            "source": "startup",
            "cwd": str(tmp_path),
        },
    )

    session = bridge._ProviderSession(request)
    readiness = session.initialize()
    submission = session.admit(_admission(request))

    assert len(procs) == 1
    assert readiness["provider_receipt_kind"] == "claude-session-start"
    assert readiness["provider_session_id"] == session.provider_session_id
    assert readiness["receipt_id"] == session.provider_session_id
    assert readiness["model_input_ready"] is True

    assert submission["provider_receipt_kind"] == "claude-turn-start"
    assert submission["provider_session_id"] == readiness["provider_session_id"]
    assert submission["provider_turn_id"] == submission["receipt_id"]
    assert submission["provider_accepted"] is True
    assert len(procs[0].sent_messages) == 1
    assert procs[0].sent_messages[0]["type"] == "user"


def test_deepseek_route_fails_closed_when_gateway_missing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _claude_request(tmp_path, model="deepseek-v4-flash")
    # Empty env with no ANTHROPIC_BASE_URL
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr(bridge, "_BOUND_PROVIDER_ENV", None)
    bridge._prune_bridge_environment("claude_code")
    monkeypatch.setattr(bridge, "_profile_material", lambda *_: _material())
    monkeypatch.setattr(bridge._ProviderSession, "_version", lambda *_: "claude 2.1.233")

    session = bridge._ProviderSession(request)
    with pytest.raises(bridge.BridgeError, match="DeepSeek .* ANTHROPIC_BASE_URL"):
        session.initialize()


def test_deepseek_route_fails_closed_on_conflicting_ambient_cloud_keys(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _claude_request(tmp_path, model="deepseek-v4-flash")
    isolated_env = {
        "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
        "ANTHROPIC_AUTH_TOKEN": "test-deepseek-token",
        "CLAUDE_CODE_USE_BEDROCK": "1",
    }
    monkeypatch.setattr(os, "environ", isolated_env)
    monkeypatch.setattr(bridge, "_BOUND_PROVIDER_ENV", None)
    bridge._prune_bridge_environment("claude_code")
    monkeypatch.setattr(bridge, "_profile_material", lambda *_: _material())
    monkeypatch.setattr(bridge._ProviderSession, "_version", lambda *_: "claude 2.1.233")

    session = bridge._ProviderSession(request)
    with pytest.raises(bridge.BridgeError, match="conflicting cloud controls"):
        session.initialize()


def test_claude_acp_renderer_formats_stream_json_events() -> None:
    renderer = ManagedEventRenderer(provider="claude_code")

    assistant_text_event = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "Drafting code."}],
        },
    }
    assert renderer.render(assistant_text_event) == "Drafting code."

    tool_event = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}],
        },
    }
    assert "[tool] Bash — started" in (renderer.render(tool_event) or "")

    result_event = {
        "type": "result",
        "stop_reason": "end_turn",
    }
    assert "[turn completed] end_turn" in (renderer.render(result_event) or "")


def test_claude_acp_session_operation_route_query(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _claude_request(tmp_path)
    isolated_env = {
        "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
        "ANTHROPIC_AUTH_TOKEN": "test-deepseek-token",
    }
    monkeypatch.setattr(os, "environ", isolated_env)
    monkeypatch.setattr(bridge, "_BOUND_PROVIDER_ENV", None)
    bridge._prune_bridge_environment("claude_code")
    monkeypatch.setattr(bridge, "_profile_material", lambda *_: _material())
    monkeypatch.setattr(bridge, "_RpcProcess", _FakeClaudeProcess)
    monkeypatch.setattr(bridge._ProviderSession, "_version", lambda *_: "claude 2.1.233")
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.claude_native_readiness.await_session_start",
        lambda _path, session_id, **_kw: {
            "session_id": session_id,
            "hook_event_name": "SessionStart",
            "source": "startup",
            "cwd": str(tmp_path),
        },
    )

    session = bridge._ProviderSession(request)
    session.initialize()

    journal_path = tmp_path / "control-journal.json"
    journal = bridge.SessionControlJournal(journal_path)
    op_id = str(uuid.uuid4())
    op = journal.begin(
        operation_id=op_id,
        terminal_id=request["terminal_id"],
        generation=request["generation"],
        action="route-query",
        request_sha256="0" * 64,
        provider="claude_code",
        provider_session_id=session.provider_session_id or "session-id",
    )
    command = {
        "reservation_id": request["reservation_id"],
        "terminal_id": request["terminal_id"],
        "generation": request["generation"],
        "operation_id": op_id,
        "action": "route-query",
    }
    receipt = session.session_operation(command, journal)
    assert receipt["state"] == bridge.CONTROL_COMPLETED
    assert receipt["result"]["model"] == "deepseek-v4-flash"
    assert receipt["result"]["capabilities"]["follow_up"] is True
    assert receipt["result"]["capabilities"]["cancel"] is True
    assert receipt["result"]["capabilities"]["route_query"] is True


def test_claude_acp_session_operation_follow_up_and_cancel(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _claude_request(tmp_path)
    procs: list[_FakeClaudeProcess] = []

    def fake_proc(*args: Any, **kwargs: Any) -> _FakeClaudeProcess:
        proc = _FakeClaudeProcess(*args, **kwargs)
        proc.auto_complete = False  # Keep turn active until cancel/signal
        procs.append(proc)
        return proc

    isolated_env = {
        "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
        "ANTHROPIC_AUTH_TOKEN": "test-deepseek-token",
    }
    monkeypatch.setattr(os, "environ", isolated_env)
    monkeypatch.setattr(bridge, "_BOUND_PROVIDER_ENV", None)
    bridge._prune_bridge_environment("claude_code")
    monkeypatch.setattr(bridge, "_profile_material", lambda *_: _material())
    monkeypatch.setattr(bridge, "_RpcProcess", fake_proc)
    monkeypatch.setattr(bridge._ProviderSession, "_version", lambda *_: "claude 2.1.233")
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.claude_native_readiness.await_session_start",
        lambda _path, session_id, **_kw: {
            "session_id": session_id,
            "hook_event_name": "SessionStart",
            "source": "startup",
            "cwd": str(tmp_path),
        },
    )

    session = bridge._ProviderSession(request)
    session.initialize()

    journal_path = tmp_path / "control-journal.json"
    journal = bridge.SessionControlJournal(journal_path)

    # Follow-up
    op_id = str(uuid.uuid4())
    op = journal.begin(
        operation_id=op_id,
        terminal_id=request["terminal_id"],
        generation=request["generation"],
        action="follow-up",
        request_sha256="1" * 64,
        provider="claude_code",
        provider_session_id=session.provider_session_id or "session-id",
    )
    command = {
        "reservation_id": request["reservation_id"],
        "terminal_id": request["terminal_id"],
        "generation": request["generation"],
        "operation_id": op_id,
        "action": "follow-up",
        "message": "check next step",
    }
    receipt = session.session_operation(command, journal)
    assert receipt["state"] == bridge.CONTROL_ACCEPTED
    assert receipt["provider_turn_id"] is not None

    # Refuse second concurrent follow-up while turn is active
    second_op_id = str(uuid.uuid4())
    second_op = journal.begin(
        operation_id=second_op_id,
        terminal_id=request["terminal_id"],
        generation=request["generation"],
        action="follow-up",
        request_sha256="3" * 64,
        provider="claude_code",
        provider_session_id=session.provider_session_id or "session-id",
    )
    second_command = {
        "reservation_id": request["reservation_id"],
        "terminal_id": request["terminal_id"],
        "generation": request["generation"],
        "operation_id": second_op_id,
        "action": "follow-up",
        "message": "concurrent prompt",
    }
    second_receipt = session.session_operation(second_command, journal)
    assert second_receipt["state"] == bridge.CONTROL_REFUSED
    assert second_receipt["reason_code"] == "turn_busy"

    # Cancel while turn is active
    cancel_op_id = str(uuid.uuid4())
    cancel_op = journal.begin(
        operation_id=cancel_op_id,
        terminal_id=request["terminal_id"],
        generation=request["generation"],
        action="cancel",
        request_sha256="2" * 64,
        provider="claude_code",
        provider_session_id=session.provider_session_id or "session-id",
    )
    cancel_command = {
        "reservation_id": request["reservation_id"],
        "terminal_id": request["terminal_id"],
        "generation": request["generation"],
        "operation_id": cancel_op_id,
        "action": "cancel",
    }
    cancel_receipt = session.session_operation(cancel_command, journal)
    assert cancel_receipt["state"] == bridge.CONTROL_ACCEPTED

    # Reconcile after cancel completes (clear active prompt lock)
    session._active_prompt_request_id = None
    reconciled = session.reconcile_session_operation(journal, cancel_op_id)
    assert reconciled["state"] == bridge.CONTROL_COMPLETED


def test_claude_acp_standard_claude_model_without_gateway(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _claude_request(
        tmp_path, model="claude-3-7-sonnet-20250219", provider_route="anthropic"
    )
    # Standard Claude model with standard environment (no ANTHROPIC_BASE_URL required)
    isolated_env = {
        "ANTHROPIC_API_KEY": "sk-ant-test-key",
    }
    monkeypatch.setattr(os, "environ", isolated_env)
    monkeypatch.setattr(bridge, "_BOUND_PROVIDER_ENV", None)
    bridge._prune_bridge_environment("claude_code")
    monkeypatch.setattr(bridge, "_profile_material", lambda *_: _material())
    monkeypatch.setattr(bridge, "_RpcProcess", _FakeClaudeProcess)
    monkeypatch.setattr(bridge._ProviderSession, "_version", lambda *_: "claude 2.1.233")
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.claude_native_readiness.await_session_start",
        lambda _path, session_id, **_kw: {
            "session_id": session_id,
            "hook_event_name": "SessionStart",
            "source": "startup",
            "cwd": str(tmp_path),
        },
    )

    session = bridge._ProviderSession(request)
    readiness = session.initialize()
    assert readiness["provider_receipt_kind"] == "claude-session-start"
    assert readiness["model"] == "claude-3-7-sonnet-20250219"


def test_claude_code_is_in_authoritative_readiness_and_submission_maps() -> None:
    # Verify exact readiness receipt and submission receipt kinds
    assert managed_launch._READINESS_RECEIPT_KINDS["claude_code"] == "claude-session-start"
    assert managed_launch._SUBMISSION_RECEIPT_KINDS["claude_code"] == "claude-turn-start"
    assert "claude_code" in managed_launch.READINESS_PROVIDERS

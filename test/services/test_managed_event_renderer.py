from __future__ import annotations

import json
import os
import sys
import threading
from io import StringIO
from unittest.mock import MagicMock

import pytest

from cli_agent_orchestrator.services.managed_event_renderer import ManagedEventRenderer
from cli_agent_orchestrator.services.managed_provider_bridge import (
    BridgeError,
    _authorize_operator_peer,
    _operator_command,
    _operator_console,
    _render_provider_diagnostic,
    _RpcProcess,
    _send_socket_response,
)


def _update(kind, **values):
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": "session-1", "update": {"sessionUpdate": kind, **values}},
    }


def test_renderer_projects_message_and_never_raw_rpc():
    renderer = ManagedEventRenderer(provider="kimi_cli")
    item = _update(
        "agent_message_chunk",
        content={"type": "text", "text": "Readable answer"},
    )

    rendered = renderer.render(item)

    assert rendered == "Readable answer"
    assert "jsonrpc" not in rendered
    assert "session/update" not in rendered


def test_renderer_coalesces_repeated_tool_state():
    renderer = ManagedEventRenderer(provider="kimi_cli")
    item = _update(
        "tool_call_update",
        toolCallId="tool-1",
        title="Reading README.md",
        status="in_progress",
        rawInput={"secret": "must-not-render"},
    )

    first = renderer.render(item)
    second = renderer.render(item)

    assert first == "\n[tool] Reading README.md — in_progress\n"
    assert second is None
    assert "must-not-render" not in first


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        (
            _update(
                "agent_thought_chunk",
                content=[
                    {"content": {"text": "first"}},
                    {"text": " second"},
                ],
            ),
            "first second",
        ),
        (
            {"method": "session/update", "params": None},
            "[provider event] session update\n",
        ),
        (
            _update(
                "plan",
                entries=[
                    {"status": "in progress", "content": "Inspect code"},
                    "ignored",
                    {"title": "Run tests"},
                ],
            ),
            "[plan]\n  [in progress] Inspect code\n  [pending] Run tests\n",
        ),
        (_update("plan_update", entries=None), "[plan updated]\n"),
        (_update("plan_removed"), "[plan cleared]\n"),
        (
            _update(
                "available_commands_update",
                availableCommands=[{"name": "compact"}, {"name": " route   query "}],
            ),
            "[commands] compact, route query\n",
        ),
        (
            _update("available_commands_update", availableCommands=None),
            "[commands] none advertised\n",
        ),
        (
            _update("config_option_update", configId="model", currentValue="kimi-code/k3"),
            "[route] model=kimi-code/k3\n",
        ),
        (_update("current_mode_update", currentModeId="agent"), "[mode] agent\n"),
        (_update("session_info_update"), "[session metadata updated]\n"),
        (_update("usage_update"), "[usage updated]\n"),
        (_update("future_event"), "[provider event] future_event\n"),
        ({"method": "item/output/delta", "params": {"delta": "hello"}}, "hello"),
        (
            {"method": "item/output/delta", "params": {"delta": {"text": "world"}}},
            "world",
        ),
        ({"method": "item/output/delta", "params": {"delta": {}}}, None),
        ({"method": "turn/started"}, "\n[turn started]\n"),
        ({"method": "tool/progress"}, "[provider event] tool/progress\n"),
        ({"method": "future/notification"}, "[provider event] future/notification\n"),
        ({"not_method": True}, "[provider event]\n"),
    ],
)
def test_renderer_projects_all_supported_event_families(item, expected):
    assert ManagedEventRenderer(provider="kimi_cli").render(item) == expected


def test_rpc_process_pane_output_is_rendered_not_json(capsys):
    item = _update(
        "agent_message_chunk",
        content={"type": "text", "text": "Human output"},
    )
    script = "import json,time;" f"print(json.dumps({item!r}), flush=True);" "time.sleep(1)"
    rpc = _RpcProcess([sys.executable, "-c", script])
    try:
        rpc.wait_notification(lambda value: value == item, start_index=0, timeout=2)
    finally:
        rpc.close()

    output = capsys.readouterr().out
    assert "Human output" in output
    assert json.dumps(item, sort_keys=True) not in output
    assert '"jsonrpc"' not in output


def _rpc_process_without_child():
    rpc = object.__new__(_RpcProcess)
    rpc._condition = threading.Condition()
    rpc._next_id = 1
    rpc._responses = {}
    rpc._notifications = []
    rpc._closed_error = None
    rpc._send = MagicMock()
    return rpc


def test_rpc_process_request_response_and_notification_edges():
    rpc = _rpc_process_without_child()

    request_id = rpc.start_request("session/prompt", {"message": "continue"})
    assert request_id == 1
    rpc._send.assert_called_once_with(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session/prompt",
            "params": {"message": "continue"},
        }
    )
    rpc.notify("session/cancel", {"sessionId": "session-1"})
    assert rpc._send.call_count == 2

    rpc._responses[2] = {"result": {"ok": True}}
    assert rpc.wait_response(2, 0.1) == {"ok": True}
    rpc._responses[3] = {"error": {"code": -1}}
    with pytest.raises(BridgeError, match="provider request failed"):
        rpc.wait_response(3, 0.1)
    rpc._responses[4] = {"unexpected": True}
    with pytest.raises(BridgeError, match="omitted result"):
        rpc.wait_response(4, 0.1)
    rpc._responses[5] = {"result": ["not", "an", "object"]}
    with pytest.raises(BridgeError, match="not an object"):
        rpc.wait_response(5, 0.1)

    rpc._notifications.extend([{"method": "ignored"}, {"method": "target"}])
    assert (
        rpc.wait_notification(
            lambda item: item.get("method") == "target",
            start_index=0,
            timeout=0.1,
        )["method"]
        == "target"
    )
    assert rpc.notification_count() == 2
    assert rpc.notifications_since(1) == ([{"method": "target"}], 2)

    rpc._closed_error = "provider exited"
    with pytest.raises(BridgeError, match="provider exited"):
        rpc.wait_response(99, 0.1)
    with pytest.raises(BridgeError, match="provider exited"):
        rpc.wait_notification(lambda _item: False, start_index=2, timeout=0.1)
    rpc._closed_error = None
    with pytest.raises(BridgeError, match="timed out awaiting response"):
        rpc.wait_response(100, 0)
    with pytest.raises(BridgeError, match="no model-turn acceptance"):
        rpc.wait_notification(lambda _item: False, start_index=2, timeout=0)


def test_operator_console_translates_text_and_semantic_commands():
    assert _operator_command("please continue\n") == (
        "follow-up",
        {"message": "please continue"},
    )
    assert _operator_command("/cancel\n") == ("cancel", {})
    assert _operator_command("/compact retain the decisions\n") == (
        "compact",
        {"instruction": "retain the decisions"},
    )
    assert _operator_command("/model kimi-k2.7\n") == (
        "route-set",
        {"config_id": "model", "value": "kimi-k2.7"},
    )
    assert _operator_command("/effort high\n") == (
        "route-set",
        {"config_id": "thinking", "value": "high"},
    )
    assert _operator_command("/exit\n") == (
        "invalid-command",
        {"command": "/exit"},
    )
    assert _operator_command("/send /exit\n") == (
        "follow-up",
        {"message": "/exit"},
    )
    assert _operator_command("/operation terminal-op-1\n") == (
        "operation-query",
        {"operation_id": "terminal-op-1"},
    )


def test_operator_console_reconciles_same_operation_after_response_loss(monkeypatch, capsys):
    calls = []

    def fake_request_bridge(reservation_id, command, *, timeout):
        calls.append((reservation_id, command, timeout))
        if command["op"] == "session.op.begin":
            raise TimeoutError("response lost")
        return {"ok": True, "receipt": {"state": "accepted"}}

    monkeypatch.setattr(sys, "stdin", StringIO("continue the work\n"))
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.managed_provider_bridge.request_bridge",
        fake_request_bridge,
    )
    _operator_console(
        {
            "reservation_id": "reservation-1",
            "terminal_id": "terminal-1",
            "generation": "generation-1",
        }
    )

    assert [command["op"] for _, command, _ in calls] == [
        "session.op.begin",
        "session.op.query",
    ]
    assert calls[0][1]["operation_id"] == calls[1][1]["operation_id"]
    output = capsys.readouterr().out
    assert "response was lost" in output
    assert "is accepted; do not resend it" in output


def test_operator_console_help_query_and_success_paths(monkeypatch, capsys):
    calls = []

    def fake_request_bridge(reservation_id, command, *, timeout):
        calls.append((reservation_id, command, timeout))
        if command["op"] == "session.op.query" and command["operation_id"] == "op-missing":
            raise BridgeError("unknown operation")
        if command["op"] == "session.op.query":
            return {"ok": True, "receipt": {"state": "completed"}}
        return {
            "ok": True,
            "receipt": {"state": "refused", "reason_detail": "turn is busy"},
        }

    monkeypatch.setattr(
        sys,
        "stdin",
        StringIO("\n/help\n/exit\n/operation op-done\n/op op-missing\ncontinue\n"),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.managed_provider_bridge.request_bridge",
        fake_request_bridge,
    )
    _operator_console(
        {
            "reservation_id": "reservation-1",
            "terminal_id": "terminal-1",
            "generation": "generation-1",
        }
    )

    output = capsys.readouterr().out
    assert "messages create provider-native follow-up turns" in output
    assert "unknown local command: /exit" in output
    assert "operation op-done is completed" in output
    assert "operation op-missing could not be reconciled" in output
    assert "follow-up refused" in output
    assert "turn is busy" in output
    assert [command["op"] for _, command, _ in calls] == [
        "session.op.query",
        "session.op.query",
        "session.op.begin",
    ]


def test_operator_console_preserves_id_when_begin_and_query_both_fail(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", StringIO("continue\n"))
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.managed_provider_bridge.request_bridge",
        MagicMock(side_effect=[TimeoutError("begin lost"), TimeoutError("query lost")]),
    )

    _operator_console(
        {
            "reservation_id": "reservation-1",
            "terminal_id": "terminal-1",
            "generation": "generation-1",
        }
    )

    output = capsys.readouterr().out
    assert "outcome is unresolved" in output
    assert "Do not resend; use /operation terminal-" in output


def test_structured_stderr_is_not_rendered_as_raw_json():
    diagnostic = _render_provider_diagnostic(
        '{"jsonrpc":"2.0","params":{"secret":"must-not-render"}}'
    )

    assert diagnostic == "structured detail suppressed"
    assert "must-not-render" not in diagnostic


def test_operator_peer_is_pinned_to_bridge_or_controller(monkeypatch):
    from cli_agent_orchestrator.services.actor_broker import PeerCredentials

    connection = MagicMock()
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.managed_provider_bridge.actor_broker.peer_credentials",
        lambda _: PeerCredentials(pid=4321, uid=os.getuid()),
    )
    assert _authorize_operator_peer(connection, {"controller_pid": 4321}).pid == 4321

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.managed_provider_bridge.actor_broker.peer_credentials",
        lambda _: PeerCredentials(pid=9999, uid=os.getuid()),
    )
    with pytest.raises(BridgeError, match="not the pinned conductor"):
        _authorize_operator_peer(connection, {"controller_pid": 4321})


def test_disconnected_operator_response_does_not_escape_or_kill_bridge():
    disconnected = MagicMock()
    disconnected.sendall.side_effect = BrokenPipeError()

    assert _send_socket_response(disconnected, {"ok": False, "error": "turn busy"}) is False

    next_connection = MagicMock()
    assert _send_socket_response(next_connection, {"ok": True}) is True
    next_connection.sendall.assert_called_once()


def test_unserializable_operator_response_is_connection_local():
    connection = MagicMock()

    assert _send_socket_response(connection, {"bad": object()}) is False
    connection.sendall.assert_not_called()

from __future__ import annotations

import json
import os
import sys
from io import StringIO
from unittest.mock import MagicMock

import pytest

from cli_agent_orchestrator.services.managed_event_renderer import ManagedEventRenderer
from cli_agent_orchestrator.services.managed_provider_bridge import (
    BridgeError,
    _RpcProcess,
    _authorize_operator_peer,
    _operator_command,
    _operator_console,
    _render_provider_diagnostic,
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

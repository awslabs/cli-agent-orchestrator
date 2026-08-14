"""Contract tests for the Pi MCP JSONL bridge."""

import json
import select
import subprocess
import sys
from io import StringIO

import pytest

from cli_agent_orchestrator.providers.pi_mcp_proxy import (
    ProxyConfigError,
    ProxyProtocolError,
    _write_response,
    flatten_tools,
    load_proxy_config,
    response_error,
    response_ok,
)


def _tool(name: str) -> dict[str, object]:
    return {"name": name, "description": f"{name} tool", "inputSchema": {"type": "object"}}


def _send_proxy_request(process, request: dict[str, object]) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(request) + "\n")
    process.stdin.flush()


def _read_proxy_response(process, timeout: float = 2) -> dict[str, object]:
    assert process.stdout is not None
    ready, _, _ = select.select([process.stdout], [], [], timeout)
    assert ready, "timed out waiting for Pi MCP proxy response"
    return json.loads(process.stdout.readline())


def test_load_config_rejects_http_server(tmp_path):
    """A URL-only server cannot be launched over the V1 stdio bridge."""
    config = tmp_path / "mcp.json"
    config.write_text('{"terminalId":"t1","servers":{"remote":{"url":"https://x"}}}')

    with pytest.raises(ProxyConfigError, match="stdio"):
        load_proxy_config(config)


@pytest.mark.parametrize(
    ("server", "message"),
    [
        ({}, "command"),
        ({"command": "fixture", "env": {"TOKEN": 1}}, "environment"),
        ({"command": "fixture", "requestTimeoutMs": 0}, "timeout"),
        ({"command": "fixture", "requestTimeoutMs": 1_200_001}, "timeout"),
    ],
)
def test_load_config_rejects_invalid_stdio_server(tmp_path, server, message):
    """Malformed process configs fail before a command can be launched."""
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"terminalId": "t1", "servers": {"fixture": server}}))

    with pytest.raises(ProxyConfigError, match=message):
        load_proxy_config(config)


def test_duplicate_tool_names_fail_closed():
    """A duplicate exposed name must never be routed to an arbitrary server."""
    with pytest.raises(ProxyProtocolError, match="duplicate tool name"):
        flatten_tools({"one": [_tool("handoff")], "two": [_tool("handoff")]})


@pytest.mark.parametrize("name", ["bash", "read", "edit", "write", "grep", "find", "ls"])
def test_pi_builtin_tool_names_fail_closed_before_exposure(name):
    """An MCP server cannot replace any Pi native tool with a same-named tool."""
    with pytest.raises(ProxyProtocolError, match=rf"reserved Pi tool name: {name}"):
        flatten_tools({"fixture": [_tool(name)]})


def test_flatten_tools_preserves_server_routing_and_schema():
    """Pi receives enough metadata to register a tool and route it back safely."""
    result = flatten_tools({"fixture": [_tool("echo")]})

    assert result == [
        {
            "server": "fixture",
            "name": "echo",
            "description": "echo tool",
            "inputSchema": {"type": "object"},
        }
    ]


def test_flatten_tools_keeps_cao_routing_metadata_authoritative():
    """A server cannot redirect its discovered tool to another MCP server."""
    tool = _tool("echo")
    tool["server"] = "attacker-controlled"

    assert flatten_tools({"fixture": [tool]})[0]["server"] == "fixture"


def test_response_helpers_correlate_ids_and_keep_errors_safe():
    """Protocol responses retain the request identifier without config disclosure."""
    assert response_ok("1", {"tools": []}) == {"id": "1", "ok": True, "result": {"tools": []}}
    assert response_error("2", ValueError("bad request")) == {
        "id": "2",
        "ok": False,
        "error": "internal proxy error",
    }


def test_response_error_hides_unexpected_exception_details_from_protocol_output():
    """Transport failures cannot disclose secrets through the Pi JSONL stream."""
    secret = "api-token=super-secret-value"
    writer = StringIO()

    _write_response(writer, response_error("3", RuntimeError(secret)))

    assert secret not in writer.getvalue()
    assert json.loads(writer.getvalue()) == {
        "id": "3",
        "ok": False,
        "error": "internal proxy error",
    }


def test_response_error_keeps_intentional_protocol_errors_descriptive():
    """Caller-fixable malformed requests retain their safe protocol detail."""
    assert response_error("4", ProxyProtocolError("unknown request type")) == {
        "id": "4",
        "ok": False,
        "error": "unknown request type",
    }


def test_proxy_lists_and_calls_a_stdio_mcp_server(tmp_path):
    """The bridge initializes, lists, and calls a real SDK stdio server."""
    server = tmp_path / "fixture_server.py"
    server.write_text(
        "from fastmcp import FastMCP\n"
        "mcp = FastMCP('fixture')\n"
        "@mcp.tool()\n"
        "def echo(text: str) -> str:\n"
        "    return text\n"
        "mcp.run(transport='stdio', show_banner=False)\n"
    )
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "terminalId": "term-1",
                "servers": {
                    "fixture": {
                        "command": sys.executable,
                        "args": [str(server)],
                    }
                },
            }
        )
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cli_agent_orchestrator.providers.pi_mcp_proxy",
            "--config",
            str(config),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _send_proxy_request(process, {"id": "1", "type": "list_tools"})
        listed = _read_proxy_response(process)
        assert listed["result"]["tools"][0]["name"] == "echo"

        _send_proxy_request(
            process,
            {
                "id": "2",
                "type": "call_tool",
                "server": "fixture",
                "name": "echo",
                "arguments": {"text": "hi"},
            },
        )
        called = _read_proxy_response(process)
        assert called["result"]["content"] == [{"type": "text", "text": "hi"}]

        _send_proxy_request(process, {"id": "3", "type": "shutdown"})
        assert _read_proxy_response(process) == {"id": "3", "ok": True, "result": {}}
        assert process.wait(timeout=2) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)


def test_proxy_cancels_hanging_call_and_remains_usable(tmp_path):
    """ID-targeted cancellation releases a real hanging SDK call without blocking input."""
    server = tmp_path / "hanging_server.py"
    server.write_text(
        "import asyncio\n"
        "from fastmcp import FastMCP\n"
        "mcp = FastMCP('fixture')\n"
        "@mcp.tool()\n"
        "async def echo(text: str) -> str:\n"
        "    if text == 'hang':\n"
        "        await asyncio.Event().wait()\n"
        "    return text\n"
        "mcp.run(transport='stdio', show_banner=False)\n"
    )
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "terminalId": "term-1",
                "servers": {
                    "fixture": {
                        "command": sys.executable,
                        "args": [str(server)],
                    }
                },
            }
        )
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cli_agent_orchestrator.providers.pi_mcp_proxy",
            "--config",
            str(config),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _send_proxy_request(process, {"id": "1", "type": "list_tools"})
        assert _read_proxy_response(process)["id"] == "1"

        _send_proxy_request(
            process,
            {
                "id": "2",
                "type": "call_tool",
                "server": "fixture",
                "name": "echo",
                "arguments": {"text": "hang"},
            },
        )
        _send_proxy_request(process, {"id": "3", "type": "cancel", "targetId": "2"})
        assert _read_proxy_response(process) == {
            "id": "3",
            "ok": True,
            "result": {"cancelled": True},
        }

        _send_proxy_request(
            process,
            {
                "id": "4",
                "type": "call_tool",
                "server": "fixture",
                "name": "echo",
                "arguments": {"text": "after abort"},
            },
        )
        response = _read_proxy_response(process)
        assert response["id"] == "4"
        assert response["result"]["content"] == [{"type": "text", "text": "after abort"}]

        _send_proxy_request(process, {"id": "5", "type": "shutdown"})
        assert _read_proxy_response(process) == {"id": "5", "ok": True, "result": {}}
        assert process.wait(timeout=3) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)

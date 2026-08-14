"""Contract tests for the Pi MCP JSONL bridge."""

import asyncio
import json
import os
import queue
import select
import signal
import subprocess
import sys
import time
from io import StringIO
from pathlib import Path

import pytest

import cli_agent_orchestrator.providers.pi_mcp_proxy as pi_mcp_proxy
from cli_agent_orchestrator.providers.pi_mcp_proxy import (
    ProxyConfigError,
    ProxyProtocolError,
    _request_id,
    _run_proxy,
    _write_response,
    flatten_tools,
    load_proxy_config,
    main,
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


class _QueuedJSONLReader:
    """Blocking text reader that lets an in-process proxy receive real JSONL incrementally."""

    def __init__(self) -> None:
        self._lines: queue.Queue[str] = queue.Queue()

    def send(self, request: dict[str, object]) -> None:
        self._lines.put(json.dumps(request) + "\n")

    def close(self) -> None:
        self._lines.put("")

    def readline(self) -> str:
        return self._lines.get()


class _ToolResult:
    """Small real-protocol-shaped result for exercising the proxy loop."""

    def __init__(self, text: str) -> None:
        self._payload = {"content": [{"type": "text", "text": text}], "isError": False}

    def model_dump(self, **_kwargs: object) -> dict[str, object]:
        return self._payload


class _NotificationFailureSession:
    """A controlled MCP boundary for proxy-level cancellation behavior tests."""

    def __init__(self, notification_behavior: str) -> None:
        self.notification_behavior = notification_behavior
        self.call_started = asyncio.Event()
        self.call_cancelled = asyncio.Event()
        self.notification_attempted = asyncio.Event()
        self.notification_cancelled = asyncio.Event()
        self.release_notification = asyncio.Event()

    async def call_tool(
        self, _name: str, arguments: dict[str, object], **_kwargs: object
    ) -> _ToolResult:
        if arguments["text"] != "hang":
            return _ToolResult(str(arguments["text"]))
        self.call_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.call_cancelled.set()
            raise
        raise AssertionError("unreachable")

    async def notify_call_cancelled(self, _task: asyncio.Task[object]) -> None:
        self.notification_attempted.set()
        if self.notification_behavior == "raises":
            raise RuntimeError("stdio writer is closed")
        try:
            await self.release_notification.wait()
        except asyncio.CancelledError:
            self.notification_cancelled.set()
            raise


async def _response_with_id(writer: StringIO, request_id: str, timeout: float = 2) -> dict:
    """Wait for an asynchronously written protocol response with the requested ID."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        for line in writer.getvalue().splitlines():
            response = json.loads(line)
            if response["id"] == request_id:
                return response
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for Pi MCP proxy response {request_id!r}")


async def _path_exists(path, timeout: float = 2) -> None:
    """Wait for a real stdio tool to record that its invocation is active."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if path.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path}")


def _fixture_pid_from_file(pid_file) -> int | None:
    """Read a fixture PID whenever its test-owned file was published."""
    if not pid_file.exists():
        return None
    try:
        pid = int(pid_file.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise AssertionError(f"fixture PID file {pid_file} is invalid") from exc
    if pid <= 0:
        raise AssertionError(f"fixture PID file {pid_file} is invalid")
    return pid


def _fixture_group_is_owned(fixture_pid: int, fixture) -> bool:
    """Check that a live PID is the exact test fixture's session leader."""
    try:
        if os.getpgid(fixture_pid) != fixture_pid:
            raise AssertionError(f"fixture PID {fixture_pid} is not its group leader")
    except ProcessLookupError:
        return False

    result = subprocess.run(
        ["ps", "-ww", "-p", str(fixture_pid), "-o", "command="],
        check=False,
        capture_output=True,
        text=True,
    )
    command = result.stdout.strip()
    if result.returncode != 0 or not command or command == "<defunct>":
        return False
    if str(fixture) not in command:
        raise AssertionError(
            f"fixture PID {fixture_pid} is not the expected test process: {command}"
        )
    return True


def _fixture_pid_is_present(fixture_pid: int) -> bool:
    """Return whether the fixture PID still has a process-table entry."""
    result = subprocess.run(
        ["ps", "-ww", "-p", str(fixture_pid), "-o", "pid="],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _wait_for_fixture_exit(fixture_pid: int, fixture, timeout: float = 3) -> None:
    """Require the fixture PID to be absent from the process table after reaping."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _fixture_pid_is_present(fixture_pid):
            return
        _fixture_group_is_owned(fixture_pid, fixture)
        time.sleep(0.01)
    raise AssertionError(f"stdio fixture PID {fixture_pid} survived proxy shutdown")


def test_wait_for_fixture_exit_rejects_an_unreaped_zombie(monkeypatch, tmp_path):
    """A zombie still has a process-table entry and must not count as cleaned up."""
    fixture_pid = 12345
    fixture = tmp_path / "fixture_server.py"

    monkeypatch.setattr(os, "getpgid", lambda _pid: fixture_pid)

    def defunct_fixture_ps(command, **_kwargs):
        assert command[:3] == ["ps", "-ww", "-p"]
        stdout = f"{fixture_pid}\n" if command[-1] == "pid=" else "<defunct>\n"
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", defunct_fixture_ps)

    with pytest.raises(AssertionError, match="survived proxy shutdown"):
        _wait_for_fixture_exit(fixture_pid, fixture, timeout=0.02)


def _terminate_proxy_group(process: subprocess.Popen) -> None:
    """Terminate only the test-owned proxy process group and reap its leader."""
    group_id = process.pid
    try:
        assert os.getpgid(group_id) == group_id
        os.killpg(group_id, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)


def _terminate_fixture_group(fixture_pid: int, fixture) -> None:
    """Terminate only a validated, separately owned FastMCP child group."""
    if not _fixture_group_is_owned(fixture_pid, fixture):
        return
    os.killpg(fixture_pid, signal.SIGTERM)
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        if not _fixture_group_is_owned(fixture_pid, fixture):
            return
        time.sleep(0.01)
    os.killpg(fixture_pid, signal.SIGKILL)


def _cleanup_fixture_from_pid_file(
    pid_file, fixture, owner: subprocess.Popen | None = None
) -> int | None:
    """Discover, validate, terminate, and wait for any published fixture PID."""
    fixture_pid = _fixture_pid_from_file(pid_file)
    if fixture_pid is None:
        return None
    _terminate_fixture_group(fixture_pid, fixture)
    if owner is not None:
        owner.wait(timeout=2)
    _wait_for_fixture_exit(fixture_pid, fixture)
    return fixture_pid


def test_fixture_cleanup_discovers_pid_file_after_failure(tmp_path):
    """A failed assertion after child start still terminates the recorded fixture."""
    pid_file = tmp_path / "fixture.pid"
    fixture = tmp_path / "fixture_server.py"
    fixture.write_text(
        "import os\n"
        "import time\n"
        "from pathlib import Path\n"
        "Path(os.environ['CAO_TEST_PID_FILE']).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    process = subprocess.Popen(
        [sys.executable, str(fixture)],
        env={**os.environ, "CAO_TEST_PID_FILE": str(pid_file)},
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 2
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pid_file.exists(), "fixture did not record its PID"

        with pytest.raises(AssertionError, match="deliberate failure"):
            try:
                raise AssertionError("deliberate failure after fixture start")
            finally:
                _cleanup_fixture_from_pid_file(pid_file, fixture, process)

        fixture_pid = int(pid_file.read_text(encoding="utf-8"))
        assert process.returncode == 0 - signal.SIGTERM
        _wait_for_fixture_exit(fixture_pid, fixture)
    finally:
        _cleanup_fixture_from_pid_file(pid_file, fixture, process)


def test_load_config_rejects_http_server(tmp_path):
    """A URL-only server cannot be launched over the V1 stdio bridge."""
    config = tmp_path / "mcp.json"
    config.write_text('{"terminalId":"t1","servers":{"remote":{"url":"https://x"}}}')

    with pytest.raises(ProxyConfigError, match="stdio"):
        load_proxy_config(config)


def test_mcp_dependency_caps_the_private_request_id_contract():
    """Fresh installs cannot select an MCP minor the private tracker has not exercised."""
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"

    assert '"mcp>=1.28.1,<1.29.0",' in pyproject.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("{", "unable to read"),
        (json.dumps([]), "must be an object"),
        (json.dumps({"servers": {}}), "terminalId"),
        (json.dumps({"terminalId": "t1", "servers": []}), "servers"),
        (
            json.dumps({"terminalId": "t1", "servers": {"": {"command": "node"}}}),
            "server names",
        ),
        (
            json.dumps({"terminalId": "t1", "servers": {"fixture": []}}),
            "must be an object",
        ),
        (
            json.dumps(
                {
                    "terminalId": "t1",
                    "servers": {"fixture": {"command": "node", "args": ["ok", 1]}},
                }
            ),
            "args",
        ),
    ],
)
def test_load_config_rejects_malformed_bridge_boundaries(tmp_path, contents, message):
    """Invalid bridge JSON is rejected before an MCP child can be launched."""
    config = tmp_path / "mcp.json"
    config.write_text(contents, encoding="utf-8")

    with pytest.raises(ProxyConfigError, match=message):
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


def test_discovered_tool_without_a_name_fails_before_pi_exposure():
    """A malformed SDK tool cannot become a nameless Pi command."""
    with pytest.raises(ProxyProtocolError, match="without a name"):
        flatten_tools({"fixture": [{}]})


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


@pytest.mark.parametrize("payload", [[], {"id": 1}])
def test_jsonl_request_requires_a_string_correlation_id(payload):
    """Malformed JSONL cannot create an uncorrelated bridge response."""
    with pytest.raises(ProxyProtocolError, match="request id must be a string"):
        _request_id(payload)


def test_cli_reports_invalid_config_without_echoing_its_contents(tmp_path, capsys):
    """The bridge CLI exits predictably without reflecting malformed configuration data."""
    config = tmp_path / "mcp.json"
    malformed = '{"terminalId":"sensitive-but-invalid"'
    config.write_text(malformed, encoding="utf-8")

    with pytest.raises(SystemExit) as exit_info:
        main(["--config", str(config)])

    captured = capsys.readouterr()
    assert exit_info.value.code == 2
    assert "pi MCP proxy configuration error: unable to read proxy configuration" in captured.err
    assert malformed not in captured.err


def test_in_process_proxy_rejects_bad_request_then_routes_real_stdio_tool(tmp_path):
    """A malformed JSONL request cannot prevent later calls to a real MCP process."""
    server = tmp_path / "fixture_server.py"
    server.write_text(
        "from fastmcp import FastMCP\n"
        "mcp = FastMCP('fixture')\n"
        "@mcp.tool()\n"
        "def echo(text: str) -> str:\n"
        "    return text\n"
        "mcp.run(transport='stdio', show_banner=False)\n"
    )
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
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
    config = load_proxy_config(config_path)

    async def exercise() -> None:
        reader = _QueuedJSONLReader()
        writer = StringIO()
        proxy_task = asyncio.create_task(_run_proxy(config, reader, writer))
        try:
            reader.send(
                {
                    "id": "bad",
                    "type": "call_tool",
                    "server": "missing",
                    "name": "echo",
                    "arguments": {"text": "ignored"},
                }
            )
            assert await _response_with_id(writer, "bad") == {
                "id": "bad",
                "ok": False,
                "error": "unknown server",
            }

            reader.send({"id": "list", "type": "list_tools"})
            listed = await _response_with_id(writer, "list")
            [tool] = listed["result"]["tools"]
            assert tool["name"] == "echo"
            assert tool["server"] == "fixture"
            assert tool["inputSchema"]["properties"]["text"] == {"type": "string"}

            reader.send(
                {
                    "id": "call",
                    "type": "call_tool",
                    "server": "fixture",
                    "name": "echo",
                    "arguments": {"text": "after bad request"},
                }
            )
            called = await _response_with_id(writer, "call")
            assert called["result"]["content"] == [{"type": "text", "text": "after bad request"}]

            reader.send({"id": "stop", "type": "shutdown"})
            assert await _response_with_id(writer, "stop") == {
                "id": "stop",
                "ok": True,
                "result": {},
            }
            await asyncio.wait_for(proxy_task, timeout=2)
        finally:
            if not proxy_task.done():
                reader.close()
                await asyncio.wait_for(proxy_task, timeout=2)

    asyncio.run(exercise())


@pytest.mark.parametrize("notification_behavior", ["blocks", "raises"])
def test_proxy_cancellation_acknowledges_local_cancel_when_notification_is_unavailable(
    monkeypatch, notification_behavior
):
    """A broken MCP writer cannot delay Pi's local abort, recovery, or shutdown.

    ``cancelled`` means only that the active bridge task was locally cancelled;
    MCP notifications are one-way best effort and never confirm remote tool state.
    """
    session = _NotificationFailureSession(notification_behavior)
    config = pi_mcp_proxy.ProxyConfig(
        terminal_id="term-1",
        servers={
            "fixture": pi_mcp_proxy.ProxyServerConfig(
                name="fixture",
                command="unused",
                args=[],
                env={},
                request_timeout_ms=1_000,
            )
        },
    )

    async def load_controlled_server(_config, _stack):
        return {"fixture": session}, {"fixture": [_tool("echo")]}

    monkeypatch.setattr(pi_mcp_proxy, "_load_server_tools", load_controlled_server)

    async def exercise() -> None:
        reader = _QueuedJSONLReader()
        writer = StringIO()
        proxy_task = asyncio.create_task(_run_proxy(config, reader, writer))
        try:
            reader.send({"id": "list", "type": "list_tools"})
            await _response_with_id(writer, "list")

            reader.send(
                {
                    "id": "hang",
                    "type": "call_tool",
                    "server": "fixture",
                    "name": "echo",
                    "arguments": {"text": "hang"},
                }
            )
            await asyncio.wait_for(session.call_started.wait(), timeout=0.5)

            cancellation_started = asyncio.get_running_loop().time()
            reader.send({"id": "cancel", "type": "cancel", "targetId": "hang"})
            assert await _response_with_id(writer, "cancel", timeout=0.75) == {
                "id": "cancel",
                "ok": True,
                "result": {"cancelled": True},
            }
            assert asyncio.get_running_loop().time() - cancellation_started < 0.75
            assert session.notification_attempted.is_set()
            assert session.call_cancelled.is_set()
            if notification_behavior == "blocks":
                assert session.notification_cancelled.is_set()

            reader.send(
                {
                    "id": "next",
                    "type": "call_tool",
                    "server": "fixture",
                    "name": "echo",
                    "arguments": {"text": "recovered"},
                }
            )
            assert (await _response_with_id(writer, "next"))["result"]["content"] == [
                {"type": "text", "text": "recovered"}
            ]

            reader.send({"id": "stop", "type": "shutdown"})
            assert await _response_with_id(writer, "stop") == {
                "id": "stop",
                "ok": True,
                "result": {},
            }
            await asyncio.wait_for(proxy_task, timeout=1)
        finally:
            session.release_notification.set()
            reader.close()
            if not proxy_task.done():
                await asyncio.wait_for(proxy_task, timeout=1)

    asyncio.run(exercise())


def test_in_process_proxy_rejects_invalid_tool_requests_and_remains_usable(tmp_path):
    """Invalid JSONL tool requests are correlated errors that do not poison later calls."""
    server = tmp_path / "fixture_server.py"
    server.write_text(
        "from fastmcp import FastMCP\n"
        "mcp = FastMCP('fixture')\n"
        "@mcp.tool()\n"
        "def echo(text: str) -> str:\n"
        "    return text\n"
        "mcp.run(transport='stdio', show_banner=False)\n"
    )
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
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
    config = load_proxy_config(config_path)

    async def exercise() -> None:
        reader = _QueuedJSONLReader()
        writer = StringIO()
        proxy_task = asyncio.create_task(_run_proxy(config, reader, writer))
        try:
            reader.send({"id": "list", "type": "list_tools"})
            await _response_with_id(writer, "list")

            invalid_requests = [
                (
                    "tool",
                    {
                        "type": "call_tool",
                        "server": "fixture",
                        "name": "missing",
                        "arguments": {},
                    },
                    "unknown tool",
                ),
                (
                    "arguments",
                    {
                        "type": "call_tool",
                        "server": "fixture",
                        "name": "echo",
                        "arguments": [],
                    },
                    "tool arguments must be an object",
                ),
                (
                    "cancel",
                    {"type": "cancel", "targetId": 1},
                    "cancel targetId must be a string",
                ),
                ("type", {"type": "unknown"}, "unknown request type"),
            ]
            for request_id, request, error in invalid_requests:
                reader.send({"id": request_id, **request})
                assert await _response_with_id(writer, request_id) == {
                    "id": request_id,
                    "ok": False,
                    "error": error,
                }

            reader.send(
                {
                    "id": "next",
                    "type": "call_tool",
                    "server": "fixture",
                    "name": "echo",
                    "arguments": {"text": "still usable"},
                }
            )
            assert (await _response_with_id(writer, "next"))["result"]["content"] == [
                {"type": "text", "text": "still usable"}
            ]

            reader.send({"id": "stop", "type": "shutdown"})
            await _response_with_id(writer, "stop")
            await asyncio.wait_for(proxy_task, timeout=2)
        finally:
            if not proxy_task.done():
                reader.close()
                await asyncio.wait_for(proxy_task, timeout=2)

    asyncio.run(exercise())


def test_in_process_proxy_sanitizes_crashed_mcp_server_and_routes_healthy_server(tmp_path):
    """A broken MCP transport cannot disclose errors or block another configured server."""
    crashed_server = tmp_path / "crashed_server.py"
    crashed_server.write_text(
        "import os\n"
        "from fastmcp import FastMCP\n"
        "mcp = FastMCP('crashed')\n"
        "@mcp.tool()\n"
        "def crash() -> str:\n"
        "    os._exit(17)\n"
        "mcp.run(transport='stdio', show_banner=False)\n"
    )
    healthy_server = tmp_path / "healthy_server.py"
    healthy_server.write_text(
        "from fastmcp import FastMCP\n"
        "mcp = FastMCP('healthy')\n"
        "@mcp.tool()\n"
        "def echo(text: str) -> str:\n"
        "    return text\n"
        "mcp.run(transport='stdio', show_banner=False)\n"
    )
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "terminalId": "term-1",
                "servers": {
                    "crashed": {"command": sys.executable, "args": [str(crashed_server)]},
                    "healthy": {"command": sys.executable, "args": [str(healthy_server)]},
                },
            }
        )
    )
    config = load_proxy_config(config_path)

    async def exercise() -> None:
        reader = _QueuedJSONLReader()
        writer = StringIO()
        proxy_task = asyncio.create_task(_run_proxy(config, reader, writer))
        try:
            reader.send({"id": "list", "type": "list_tools"})
            await _response_with_id(writer, "list")

            reader.send(
                {
                    "id": "crash",
                    "type": "call_tool",
                    "server": "crashed",
                    "name": "crash",
                    "arguments": {},
                }
            )
            assert await _response_with_id(writer, "crash") == {
                "id": "crash",
                "ok": False,
                "error": "internal proxy error",
            }

            reader.send(
                {
                    "id": "healthy",
                    "type": "call_tool",
                    "server": "healthy",
                    "name": "echo",
                    "arguments": {"text": "recovered"},
                }
            )
            assert (await _response_with_id(writer, "healthy"))["result"]["content"] == [
                {"type": "text", "text": "recovered"}
            ]

            reader.send({"id": "stop", "type": "shutdown"})
            await _response_with_id(writer, "stop")
            await asyncio.wait_for(proxy_task, timeout=2)
        finally:
            if not proxy_task.done():
                reader.close()
                await asyncio.wait_for(proxy_task, timeout=2)

    asyncio.run(exercise())


def test_in_process_proxy_cancels_live_stdio_call_then_routes_next(tmp_path):
    """Cancelling an active real MCP call frees the bridge for the next Pi tool call."""
    started = tmp_path / "started"
    stopped = tmp_path / "stopped"
    pid_file = tmp_path / "fixture.pid"
    server = tmp_path / "hanging_server.py"
    server.write_text(
        "import asyncio\n"
        "import os\n"
        "from pathlib import Path\n"
        "from fastmcp import FastMCP\n"
        "mcp = FastMCP('fixture')\n"
        "Path(os.environ['CAO_TEST_PID_FILE']).write_text(str(os.getpid()))\n"
        "@mcp.tool()\n"
        "async def echo(text: str) -> str:\n"
        "    if text == 'hang':\n"
        "        Path(os.environ['CAO_TEST_STARTED_FILE']).write_text('started')\n"
        "        try:\n"
        "            await asyncio.Event().wait()\n"
        "        except asyncio.CancelledError:\n"
        "            Path(os.environ['CAO_TEST_STOPPED_FILE']).write_text('stopped')\n"
        "            raise\n"
        "    return text\n"
        "mcp.run(transport='stdio', show_banner=False)\n"
    )
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "terminalId": "term-1",
                "servers": {
                    "fixture": {
                        "command": sys.executable,
                        "args": [str(server)],
                        "env": {
                            "CAO_TEST_STARTED_FILE": str(started),
                            "CAO_TEST_STOPPED_FILE": str(stopped),
                            "CAO_TEST_PID_FILE": str(pid_file),
                        },
                    }
                },
            }
        )
    )
    config = load_proxy_config(config_path)

    async def exercise() -> None:
        reader = _QueuedJSONLReader()
        writer = StringIO()
        proxy_task = asyncio.create_task(_run_proxy(config, reader, writer))
        try:
            reader.send({"id": "list", "type": "list_tools"})
            await _response_with_id(writer, "list")

            reader.send(
                {
                    "id": "hang",
                    "type": "call_tool",
                    "server": "fixture",
                    "name": "echo",
                    "arguments": {"text": "hang"},
                }
            )
            await _path_exists(started)
            assert _fixture_pid_from_file(pid_file) is not None

            reader.send(
                {
                    "id": "hang",
                    "type": "call_tool",
                    "server": "fixture",
                    "name": "echo",
                    "arguments": {"text": "duplicate"},
                }
            )
            assert await _response_with_id(writer, "hang") == {
                "id": "hang",
                "ok": False,
                "error": "request id is already active",
            }

            reader.send({"id": "cancel", "type": "cancel", "targetId": "hang"})
            assert await _response_with_id(writer, "cancel") == {
                "id": "cancel",
                "ok": True,
                "result": {"cancelled": True},
            }
            await _path_exists(stopped)

            reader.send(
                {
                    "id": "next",
                    "type": "call_tool",
                    "server": "fixture",
                    "name": "echo",
                    "arguments": {"text": "after cancel"},
                }
            )
            called = await _response_with_id(writer, "next")
            assert called["result"]["content"] == [{"type": "text", "text": "after cancel"}]

            reader.send({"id": "stop", "type": "shutdown"})
            await _response_with_id(writer, "stop")
            await asyncio.wait_for(proxy_task, timeout=2)
        finally:
            if not proxy_task.done():
                reader.close()
                try:
                    await asyncio.wait_for(asyncio.shield(proxy_task), timeout=0.5)
                except TimeoutError:
                    await asyncio.to_thread(_cleanup_fixture_from_pid_file, pid_file, server)
                    await asyncio.wait_for(proxy_task, timeout=2)
            await asyncio.to_thread(_cleanup_fixture_from_pid_file, pid_file, server)

    asyncio.run(exercise())


def test_proxy_lists_and_calls_a_stdio_mcp_server(tmp_path):
    """The bridge initializes, lists, and calls a real SDK stdio server."""
    pid_file = tmp_path / "fixture.pid"
    server = tmp_path / "fixture_server.py"
    server.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "from fastmcp import FastMCP\n"
        "mcp = FastMCP('fixture')\n"
        "Path(os.environ['CAO_TEST_PID_FILE']).write_text(str(os.getpid()))\n"
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
                        "env": {"CAO_TEST_PID_FILE": str(pid_file)},
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
        start_new_session=True,
    )
    try:
        _send_proxy_request(process, {"id": "1", "type": "list_tools"})
        listed = _read_proxy_response(process)
        assert listed["result"]["tools"][0]["name"] == "echo"
        assert _fixture_pid_from_file(pid_file) is not None

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
        _terminate_proxy_group(process)
        _cleanup_fixture_from_pid_file(pid_file, server)


def test_proxy_cancels_hanging_call_and_remains_usable(tmp_path):
    """ID-targeted cancellation releases a real hanging SDK call without blocking input."""
    started = tmp_path / "started"
    stopped = tmp_path / "stopped"
    pid_file = tmp_path / "fixture.pid"
    server = tmp_path / "hanging_server.py"
    server.write_text(
        "import asyncio\n"
        "import os\n"
        "from pathlib import Path\n"
        "from fastmcp import FastMCP\n"
        "mcp = FastMCP('fixture')\n"
        "Path(os.environ['CAO_TEST_PID_FILE']).write_text(str(os.getpid()))\n"
        "@mcp.tool()\n"
        "async def echo(text: str) -> str:\n"
        "    if text == 'hang':\n"
        "        Path(os.environ['CAO_TEST_STARTED_FILE']).write_text('started')\n"
        "        try:\n"
        "            await asyncio.Event().wait()\n"
        "        except asyncio.CancelledError:\n"
        "            Path(os.environ['CAO_TEST_STOPPED_FILE']).write_text('stopped')\n"
        "            raise\n"
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
                        "env": {
                            "CAO_TEST_PID_FILE": str(pid_file),
                            "CAO_TEST_STARTED_FILE": str(started),
                            "CAO_TEST_STOPPED_FILE": str(stopped),
                        },
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
        start_new_session=True,
    )
    try:
        assert os.getpgid(process.pid) == process.pid
        _send_proxy_request(process, {"id": "1", "type": "list_tools"})
        assert _read_proxy_response(process)["id"] == "1"
        fixture_pid = _fixture_pid_from_file(pid_file)
        assert fixture_pid is not None

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
        deadline = time.monotonic() + 2
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started.exists(), "hanging fixture never received the MCP request"
        _send_proxy_request(process, {"id": "3", "type": "cancel", "targetId": "2"})
        assert _read_proxy_response(process) == {
            "id": "3",
            "ok": True,
            "result": {"cancelled": True},
        }
        deadline = time.monotonic() + 2
        while not stopped.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert stopped.exists(), "MCP server did not receive the cancellation notification"

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
        _wait_for_fixture_exit(fixture_pid, server)
    finally:
        _terminate_proxy_group(process)
        _cleanup_fixture_from_pid_file(pid_file, server)

"""JSONL bridge between Pi extensions and stdio MCP servers."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Any, Sequence, TextIO

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CancelledNotification, CancelledNotificationParams, ClientNotification

DEFAULT_REQUEST_TIMEOUT_MS = 1_200_000
CANCEL_NOTIFICATION_TIMEOUT_SECONDS = 0.25
RESERVED_PI_TOOL_NAMES = frozenset({"bash", "read", "edit", "write", "grep", "find", "ls"})


class ProxyConfigError(ValueError):
    """Raised when a bridge configuration cannot be used safely."""


class ProxyProtocolError(ValueError):
    """Raised when a bridge protocol request or response is invalid."""


class _CancellableClientSession(ClientSession):
    """Expose MCP cancellation for the bridge task that issued a tool request."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._tool_request_ids: dict[asyncio.Task[Any], int | str] = {}

    async def call_tool(self, *args: Any, **kwargs: Any) -> Any:
        task = asyncio.current_task()
        if task is None:
            return await super().call_tool(*args, **kwargs)

        # CAO's locked MCP 1.28.1 has no public request-ID accessor. Its public
        # call_tool() immediately enters send_request(), which assigns this value
        # before its first await, so the mapping is exact and remains task-local
        # for calls concurrently issued to the same MCP server. Re-run the real
        # stdio cancellation regression when upgrading that locked dependency.
        request_id = self._request_id
        self._tool_request_ids[task] = request_id
        try:
            return await super().call_tool(*args, **kwargs)
        finally:
            self._tool_request_ids.pop(task, None)

    async def notify_call_cancelled(self, task: asyncio.Task[Any]) -> None:
        """Send the MCP cancellation notification for this bridge call, if active."""
        request_id = self._tool_request_ids.get(task)
        if request_id is None:
            return
        await self.send_notification(
            ClientNotification(
                root=CancelledNotification(
                    params=CancelledNotificationParams(
                        requestId=request_id,
                        reason="cancelled by Pi",
                    )
                )
            )
        )


@dataclass(frozen=True)
class ProxyServerConfig:
    """The supported stdio subset of an MCP server declaration."""

    name: str
    command: str
    args: list[str]
    env: dict[str, str]
    request_timeout_ms: int


@dataclass(frozen=True)
class ProxyConfig:
    """Validated bridge configuration."""

    terminal_id: str
    servers: dict[str, ProxyServerConfig]


def load_proxy_config(path: Path) -> ProxyConfig:
    """Load the command-only MCP configuration accepted by the bridge."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProxyConfigError("unable to read proxy configuration") from exc

    if not isinstance(raw, dict):
        raise ProxyConfigError("proxy configuration must be an object")
    terminal_id = raw.get("terminalId")
    if not isinstance(terminal_id, str) or not terminal_id:
        raise ProxyConfigError("terminalId must be a non-empty string")
    servers_raw = raw.get("servers")
    if not isinstance(servers_raw, dict):
        raise ProxyConfigError("servers must be an object")

    servers: dict[str, ProxyServerConfig] = {}
    for name, server_raw in servers_raw.items():
        if not isinstance(name, str) or not name:
            raise ProxyConfigError("server names must be non-empty strings")
        if not isinstance(server_raw, dict):
            raise ProxyConfigError(f"server {name!r} must be an object")
        if server_raw.get("type", "stdio") != "stdio" or "url" in server_raw:
            raise ProxyConfigError(f"server {name!r} must use stdio transport")

        command = server_raw.get("command")
        if not isinstance(command, str) or not command:
            raise ProxyConfigError(f"server {name!r} requires a non-empty command")
        args = server_raw.get("args", [])
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise ProxyConfigError(f"server {name!r} args must be a list of strings")
        env = server_raw.get("env", {})
        if not isinstance(env, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in env.items()
        ):
            raise ProxyConfigError(f"server {name!r} environment must map strings to strings")
        timeout = server_raw.get("requestTimeoutMs", DEFAULT_REQUEST_TIMEOUT_MS)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int)
            or not 1 <= timeout <= DEFAULT_REQUEST_TIMEOUT_MS
        ):
            raise ProxyConfigError(
                f"server {name!r} timeout must be between 1 and {DEFAULT_REQUEST_TIMEOUT_MS} ms"
            )
        servers[name] = ProxyServerConfig(name, command, args, env, timeout)

    return ProxyConfig(terminal_id, servers)


def flatten_tools(tools_by_server: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Attach server names to MCP tools, rejecting ambiguous exposed names."""
    flattened: list[dict[str, Any]] = []
    names: set[str] = set()
    for server, tools in tools_by_server.items():
        for tool in tools:
            name = tool.get("name")
            if not isinstance(name, str) or not name:
                raise ProxyProtocolError(f"server {server!r} returned a tool without a name")
            if name in RESERVED_PI_TOOL_NAMES:
                raise ProxyProtocolError(f"reserved Pi tool name: {name}")
            if name in names:
                raise ProxyProtocolError(f"duplicate tool name: {name}")
            names.add(name)
            flattened.append({**tool, "server": server})
    return flattened


def response_ok(request_id: str, result: Any) -> dict[str, Any]:
    """Build an ID-correlated successful JSONL response."""
    return {"id": request_id, "ok": True, "result": result}


def response_error(request_id: str, error: Exception) -> dict[str, Any]:
    """Build an ID-correlated error response without serializing exception state."""
    if isinstance(error, (ProxyConfigError, ProxyProtocolError)):
        message = str(error)
    else:
        message = "internal proxy error"
    return {"id": request_id, "ok": False, "error": message}


def _write_response(writer: TextIO, response: dict[str, Any]) -> None:
    """Write exactly one protocol response without adding diagnostics to stdout."""
    writer.write(json.dumps(response, separators=(",", ":")) + "\n")
    writer.flush()


def _request_id(request: object) -> str:
    if not isinstance(request, dict):
        raise ProxyProtocolError("request id must be a string")
    request_id = request.get("id")
    if not isinstance(request_id, str):
        raise ProxyProtocolError("request id must be a string")
    return request_id


def _tool_names(tools_by_server: dict[str, list[dict[str, Any]]]) -> dict[str, set[str]]:
    """Return the SDK-discovered tool names after enforcing global uniqueness."""
    flatten_tools(tools_by_server)
    return {server: {tool["name"] for tool in tools} for server, tools in tools_by_server.items()}


async def _load_server_tools(
    config: ProxyConfig, stack: AsyncExitStack
) -> tuple[dict[str, _CancellableClientSession], dict[str, list[dict[str, Any]]]]:
    """Start every configured stdio server and retrieve its advertised tools."""
    sessions: dict[str, _CancellableClientSession] = {}
    tools_by_server: dict[str, list[dict[str, Any]]] = {}

    for server in config.servers.values():
        env = {**os.environ, **server.env, "CAO_TERMINAL_ID": config.terminal_id}
        read_stream, write_stream = await stack.enter_async_context(
            stdio_client(
                StdioServerParameters(command=server.command, args=server.args, env=env),
                errlog=sys.stderr,
            )
        )
        session = await stack.enter_async_context(
            _CancellableClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timedelta(milliseconds=server.request_timeout_ms),
            )
        )
        await session.initialize()
        sessions[server.name] = session

        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            result = await session.list_tools(cursor=cursor)
            tools.extend(tool.model_dump(mode="json") for tool in result.tools)
            cursor = result.nextCursor
            if cursor is None:
                break
        tools_by_server[server.name] = tools

    _tool_names(tools_by_server)
    return sessions, tools_by_server


async def _run_proxy(config: ProxyConfig, reader: TextIO, writer: TextIO) -> None:
    """Serve the Pi bridge protocol until an explicit shutdown request or EOF."""
    async with AsyncExitStack() as stack:
        sessions, tools_by_server = await _load_server_tools(config, stack)
        exposed_tools = flatten_tools(tools_by_server)
        names_by_server = _tool_names(tools_by_server)
        write_lock = asyncio.Lock()
        call_tasks: dict[str, asyncio.Task[None]] = {}
        call_sessions: dict[asyncio.Task[Any], _CancellableClientSession] = {}

        async def write_response(response: dict[str, Any]) -> None:
            async with write_lock:
                _write_response(writer, response)

        async def call_tool(
            request_id: str,
            server_name: str,
            tool_name: str,
            arguments: dict[str, Any],
        ) -> None:
            task = asyncio.current_task()
            session = sessions[server_name]
            if task is not None:
                call_sessions[task] = session
            try:
                result = await session.call_tool(
                    tool_name,
                    arguments,
                    read_timeout_seconds=timedelta(
                        milliseconds=config.servers[server_name].request_timeout_ms
                    ),
                )
                await write_response(
                    response_ok(
                        request_id,
                        result.model_dump(mode="json", exclude_none=True),
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(
                    f"Unexpected Pi MCP proxy request failure: {type(exc).__name__}",
                    file=sys.stderr,
                )
                await write_response(response_error(request_id, exc))
            finally:
                if task is not None:
                    call_sessions.pop(task, None)

        def forget_call(request_id: str, task: asyncio.Future[None]) -> None:
            if call_tasks.get(request_id) is task:
                call_tasks.pop(request_id, None)

        async def cancel_calls(tasks: list[asyncio.Task[None]]) -> None:
            for task in tasks:
                session = call_sessions.get(task)
                if session is not None:
                    try:
                        # A successful notification only means the local MCP transport
                        # accepted the one-way message; Pi's ``cancelled`` response
                        # guarantees local task cancellation, never remote tool state.
                        await asyncio.wait_for(
                            session.notify_call_cancelled(task),
                            timeout=CANCEL_NOTIFICATION_TIMEOUT_SECONDS,
                        )
                    except Exception as exc:
                        print(
                            f"Unexpected Pi MCP proxy cancellation failure: {type(exc).__name__}",
                            file=sys.stderr,
                        )
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        try:
            while True:
                line = await asyncio.to_thread(reader.readline)
                if line == "":
                    break
                if not line.strip():
                    continue
                request_id = ""
                try:
                    request = json.loads(line)
                    request_id = _request_id(request)
                    request_type = request.get("type")
                    if request_type == "list_tools":
                        await write_response(response_ok(request_id, {"tools": exposed_tools}))
                    elif request_type == "call_tool":
                        server_name = request.get("server")
                        tool_name = request.get("name")
                        arguments = request.get("arguments", {})
                        if not isinstance(server_name, str) or server_name not in sessions:
                            raise ProxyProtocolError("unknown server")
                        if (
                            not isinstance(tool_name, str)
                            or tool_name not in names_by_server[server_name]
                        ):
                            raise ProxyProtocolError("unknown tool")
                        if not isinstance(arguments, dict):
                            raise ProxyProtocolError("tool arguments must be an object")
                        if request_id in call_tasks:
                            raise ProxyProtocolError("request id is already active")
                        call_task = asyncio.create_task(
                            call_tool(request_id, server_name, tool_name, arguments)
                        )
                        call_tasks[request_id] = call_task
                        call_task.add_done_callback(partial(forget_call, request_id))
                    elif request_type == "cancel":
                        target_id = request.get("targetId")
                        if not isinstance(target_id, str):
                            raise ProxyProtocolError("cancel targetId must be a string")
                        target_task = call_tasks.get(target_id)
                        cancelled = target_task is not None and not target_task.done()
                        if target_task is not None:
                            await cancel_calls([target_task])
                        await write_response(response_ok(request_id, {"cancelled": cancelled}))
                    elif request_type == "shutdown":
                        await cancel_calls(list(call_tasks.values()))
                        await write_response(response_ok(request_id, {}))
                        return
                    else:
                        raise ProxyProtocolError("unknown request type")
                except Exception as exc:
                    if not isinstance(exc, (ProxyConfigError, ProxyProtocolError)):
                        print(
                            f"Unexpected Pi MCP proxy request failure: {type(exc).__name__}",
                            file=sys.stderr,
                        )
                    await write_response(response_error(request_id, exc))
        finally:
            await cancel_calls(list(call_tasks.values()))


def run_proxy(config_path: Path, reader: TextIO, writer: TextIO) -> None:
    """Load bridge configuration and synchronously run its JSONL protocol loop."""
    asyncio.run(_run_proxy(load_proxy_config(config_path), reader, writer))


def main(argv: Sequence[str] | None = None) -> None:
    """Run the proxy as ``python -m ...pi_mcp_proxy --config <path>``."""
    parser = argparse.ArgumentParser(description="Pi stdio MCP JSONL proxy")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        run_proxy(args.config, sys.stdin, sys.stdout)
    except ProxyConfigError as exc:
        print(f"pi MCP proxy configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":  # pragma: no cover - exercised by the Pi extension process.
    main()

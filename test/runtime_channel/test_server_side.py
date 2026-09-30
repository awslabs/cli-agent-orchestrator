"""cao-server's side of the runtime channel (#745).

The handshake cases use the TestClient's WebSocket session. The rest run the
real app under uvicorn with a real ``Bridge`` whose command execution is
scripted: a launch or an input only completes when the HTTP request and the
runtime's channel share the server's event loop, as they do in production, and
the TestClient gives each request its own loop. The server's own terminal
backend fails the test if anything touches it.
"""

import asyncio
import socket
import threading
import time

import httpx
import pytest
import uvicorn
import websockets
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect

import cli_agent_orchestrator.clients.database as database
import cli_agent_orchestrator.runtime_channel.registry as registry_mod
import cli_agent_orchestrator.runtime_channel.server as server_mod
from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.backends import registry as backend_registry
from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.plugins import PluginRegistry
from cli_agent_orchestrator.runtime_channel import token as token_mod
from cli_agent_orchestrator.runtime_channel.bridge import Bridge
from cli_agent_orchestrator.runtime_channel.protocol import (
    PROTOCOL_VERSION,
    Command,
    CommandType,
    Hello,
    Result,
    Status,
    decode,
    encode,
)
from cli_agent_orchestrator.runtime_channel.registry import RuntimeRegistry
from cli_agent_orchestrator.services import terminal_service

TOKEN = "test-runtime-token"
WS_HEADERS = {"x-cao-runtime-token": TOKEN, "host": "localhost"}
DEADLINE = 10.0

LAUNCHED = {
    "id": "beef0001",
    "name": "developer-beef",
    "provider": "mock_cli",
    "session_name": "cao-beef",
    "agent_profile": "developer",
    "allowed_tools": None,
    "status": "idle",
}


def _wait_for(predicate, what, timeout=DEADLINE):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


class _NoLocalTmux:
    """The server's own terminal backend: any use fails the test."""

    def __getattr__(self, name):
        raise AssertionError(f"cao-server used its local tmux ({name}) for a remote terminal")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'server.db'}", connect_args={"check_same_thread": False}
    )
    database.Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    registry = RuntimeRegistry()
    monkeypatch.setattr(registry_mod, "runtime_registry", registry)
    monkeypatch.setattr(server_mod, "runtime_registry", registry)
    monkeypatch.setattr(app.state, "plugin_registry", PluginRegistry(), raising=False)
    monkeypatch.setenv(token_mod.TOKEN_ENV, TOKEN)
    monkeypatch.delenv(token_mod.TOKEN_FILE_ENV, raising=False)
    token_mod._reset_for_tests()
    # Through monkeypatch, so later test modules get their backend back.
    monkeypatch.setattr(backend_registry, "_backend", _NoLocalTmux())
    yield registry
    token_mod._reset_for_tests()


def _remote_row(terminal_id="abcd1234", runtime_id="rt-1", session="cao-remote1"):
    database.create_terminal(
        terminal_id, session, f"developer-{terminal_id[:4]}", "mock_cli", "developer",
        runtime_id=runtime_id,
    )  # fmt: skip


# --- handshake (TestClient) ---


@pytest.fixture
def client():
    return TestClient(app, base_url="http://localhost")


def _hello(runtime_id="rt-1", statuses=None, version=PROTOCOL_VERSION):
    return encode(Hello(protocol_version=version, runtime_id=runtime_id, statuses=statuses or {}))


class TestHandshake:
    def test_a_server_without_a_token_refuses_every_runtime(self, client, monkeypatch):
        monkeypatch.delenv(token_mod.TOKEN_ENV, raising=False)
        token_mod._reset_for_tests()
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/runtime/channel", headers=WS_HEADERS):
                pass
        assert exc.value.code == 1008

    def test_a_wrong_token_is_refused(self, client):
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(
                "/runtime/channel", headers={**WS_HEADERS, "x-cao-runtime-token": "wrong"}
            ):
                pass
        assert exc.value.code == 1008

    def test_another_protocol_version_is_refused_after_the_server_names_its_own(self, client):
        with client.websocket_connect("/runtime/channel", headers=WS_HEADERS) as ws:
            ws.send_text(_hello(version=PROTOCOL_VERSION + 1))
            reply = decode(ws.receive_text())
            assert isinstance(reply, Hello) and reply.protocol_version == PROTOCOL_VERSION
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
            assert exc.value.code == 1002

    def test_hello_places_the_runtimes_terminals_and_restores_their_status(self, client):
        _remote_row("abcd1234", "rt-1")
        with client.websocket_connect("/runtime/channel", headers=WS_HEADERS) as ws:
            ws.send_text(_hello(statuses={"abcd1234": "completed"}))
            assert isinstance(decode(ws.receive_text()), Hello)
            runtimes = client.get("/runtimes").json()["runtimes"]
            assert runtimes["rt-1"]["terminals"] == ["abcd1234"]
            assert client.get("/terminals/abcd1234").json()["status"] == "completed"
        # Disconnected: the server cannot know, and says so.
        assert client.get("/terminals/abcd1234").json()["status"] == "unknown"

    def test_a_terminal_the_reconnected_runtime_does_not_report_is_unknown(self, client):
        # E.g. the runtime pod was replaced: its panes are gone and its hello
        # no longer mentions them. The old connection's report must not linger.
        _remote_row("abcd1234", "rt-1")
        with client.websocket_connect("/runtime/channel", headers=WS_HEADERS) as ws:
            ws.send_text(_hello(statuses={"abcd1234": "completed"}))
            ws.receive_text()
        with client.websocket_connect("/runtime/channel", headers=WS_HEADERS) as ws:
            ws.send_text(_hello(statuses={}))
            ws.receive_text()
            assert client.get("/runtimes").json()["runtimes"]["rt-1"]["terminals"] == ["abcd1234"]
            assert client.get("/terminals/abcd1234").json()["status"] == "unknown"


class TestRuntimeNotConnected:
    def test_a_launch_is_503(self, client):
        response = client.post("/runtimes/rt-9/terminals", json={"agent_profile": "developer"})
        assert response.status_code == 503

    @pytest.mark.parametrize(
        "method,path",
        [
            ("post", "/terminals/abcd1234/input?message=hi"),
            ("post", "/terminals/abcd1234/key?key=Enter"),
            ("get", "/terminals/abcd1234/output"),
            ("post", "/terminals/abcd1234/exit"),
            ("get", "/terminals/abcd1234/working-directory"),
            ("delete", "/terminals/abcd1234"),
            ("delete", "/sessions/cao-remote1"),
        ],
    )
    def test_an_operation_is_503_and_changes_nothing(self, client, method, path):
        _remote_row("abcd1234", "rt-1")
        response = getattr(client, method)(path)
        assert response.status_code == 503, response.text
        assert "not connected" in response.json()["detail"]
        assert database.get_terminal_metadata("abcd1234") is not None


# --- a live server and a real runtime ---


class ScriptedRuntime(Bridge):
    """The real channel client; each command is answered by ``script``."""

    def __init__(self, server, runtime_id, script, statuses, ready_file):
        super().__init__(f"ws://{server}/runtime/channel", runtime_id, TOKEN, ready_file)
        self.script = script
        self.statuses = statuses
        self.received = []
        self.loop = None

    async def execute(self, command):
        self.received.append(command)
        return self.script(command)

    def _current_statuses(self):
        return dict(self.statuses)

    def _status_of(self, terminal_id):
        return self.statuses.get(terminal_id, TerminalStatus.UNKNOWN)

    def push(self, frame):
        asyncio.run_coroutine_threadsafe(self._send(frame), self.loop).result(timeout=DEADLINE)


@pytest.fixture
def server():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    config = uvicorn.Config(app, lifespan="off", log_level="warning", timeout_graceful_shutdown=2)
    uv = uvicorn.Server(config)
    thread = threading.Thread(target=uv.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    _wait_for(lambda: uv.started, "uvicorn to start")
    yield f"127.0.0.1:{sock.getsockname()[1]}"
    uv.should_exit = True
    thread.join(timeout=DEADLINE)
    sock.close()


@pytest.fixture
def http(server):
    with httpx.Client(base_url=f"http://{server}", timeout=DEADLINE) as client:
        yield client


@pytest.fixture
def start_runtime(server, tmp_path):
    running = []

    def start(runtime_id="rt-1", script=lambda command: {}, statuses=None):
        ready = tmp_path / f"{runtime_id}.ready"
        runtime = ScriptedRuntime(server, runtime_id, script, statuses or {}, ready)
        runtime.loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=runtime.loop.run_until_complete, args=(runtime.run(),), daemon=True
        )
        thread.start()
        running.append((runtime, thread))
        # The runtime marks itself ready only after the server's hello, which
        # the server sends once the runtime's terminals are placed.
        _wait_for(ready.exists, f"{runtime_id} to connect")
        return runtime

    yield start
    for runtime, thread in running:
        runtime.loop.call_soon_threadsafe(runtime.stop)
        thread.join(timeout=DEADLINE)
        if not thread.is_alive():
            runtime.loop.close()


def _answer(command):
    return {
        CommandType.LAUNCH: {"terminal": dict(LAUNCHED)},
        CommandType.INPUT: {"success": True},
        CommandType.KEY: {"success": True},
        CommandType.OUTPUT: {"output": "agent says hi"},
        CommandType.EXIT: {},
        CommandType.DELETE: {"deleted": True},
    }[command.type]


class TestRemoteTerminal:
    def test_every_operation_runs_in_the_runtime(self, http, start_runtime):
        runtime = start_runtime(script=_answer)

        launched = http.post(
            "/runtimes/rt-1/terminals", json={"agent_profile": "developer", "provider": "mock_cli"}
        )
        assert launched.status_code == 201, launched.text
        assert launched.json()["status"] == "idle"
        assert database.get_terminal_metadata("beef0001")["runtime_id"] == "rt-1"

        assert http.post("/terminals/beef0001/input", params={"message": "hi"}).json() == {
            "success": True
        }
        assert http.post("/terminals/beef0001/key", params={"key": "Enter"}).json() == {
            "success": True
        }
        output = http.get("/terminals/beef0001/output", params={"mode": "last"})
        assert output.json()["output"] == "agent says hi"
        assert http.post("/terminals/beef0001/exit").status_code == 200
        assert http.delete("/terminals/beef0001").status_code == 200
        assert http.get("/terminals/beef0001").status_code == 404

        assert [(c.type, c.terminal_id) for c in runtime.received] == [
            (CommandType.LAUNCH, None),
            (CommandType.INPUT, "beef0001"),
            (CommandType.KEY, "beef0001"),
            (CommandType.OUTPUT, "beef0001"),
            (CommandType.EXIT, "beef0001"),
            (CommandType.DELETE, "beef0001"),
        ]
        assert runtime.received[0].payload == {"agent_profile": "developer", "provider": "mock_cli"}
        assert runtime.received[1].payload["message"] == "hi"
        assert runtime.received[3].payload == {"mode": "last"}

    def test_the_working_directory_comes_from_the_runtime(self, http, start_runtime):
        _remote_row("abcd1234", "rt-1")
        runtime = start_runtime(script=lambda command: {"working_directory": "/work/repo"})
        response = http.get("/terminals/abcd1234/working-directory")
        assert response.status_code == 200, response.text
        assert response.json() == {"working_directory": "/work/repo"}
        assert [(c.type.value, c.terminal_id) for c in runtime.received] == [
            ("working_directory", "abcd1234")
        ]

    def test_a_reconnect_during_a_delete_leaves_no_placement_behind(
        self, http, start_runtime, monkeypatch
    ):
        _remote_row("abcd1234", "rt-1")
        start_runtime(script=_answer)
        real = terminal_service.delete_terminal_row

        def reconnect_meanwhile(terminal_id, metadata, registry=None):
            # A reconnect's hello still finds the row, and places the terminal.
            registry_mod.runtime_registry.place(terminal_id, "rt-1")
            return real(terminal_id, metadata, registry=registry)

        monkeypatch.setattr(terminal_service, "delete_terminal_row", reconnect_meanwhile)
        assert http.delete("/terminals/abcd1234").status_code == 200
        assert runtimes_of(http)["rt-1"]["terminals"] == []

    def test_an_orchestrated_message_keeps_its_sender_and_type(self, start_runtime):
        _remote_row("abcd1234", "rt-1")
        runtime = start_runtime(script=lambda command: {"success": True})
        sent = terminal_service.send_input(
            "abcd1234", "do it", sender_id="sup00001", orchestration_type=OrchestrationType.ASSIGN
        )
        assert sent is True
        assert runtime.received[0].payload == {
            "message": "do it",
            "sender_id": "sup00001",
            "orchestration_type": "assign",
            "frozen_memory": None,
        }

    def test_the_status_the_runtime_pushes_is_the_terminals_status(self, http, start_runtime):
        _remote_row("abcd1234", "rt-1")
        runtime = start_runtime(statuses={"abcd1234": TerminalStatus.IDLE})
        assert http.get("/terminals/abcd1234").json()["status"] == "idle"
        runtime.push(Status(terminal_id="abcd1234", status=TerminalStatus.PROCESSING))
        _wait_for(
            lambda: http.get("/terminals/abcd1234").json()["status"] == "processing",
            "the pushed status",
        )

    def test_a_failure_in_the_runtime_is_502_with_its_reason(self, http, start_runtime):
        _remote_row("abcd1234", "rt-1")

        def fail(command):
            raise RuntimeError("pane is gone")

        start_runtime(script=fail)
        response = http.get("/terminals/abcd1234/output")
        assert response.status_code == 502
        assert "pane is gone" in response.json()["detail"]

    @pytest.mark.parametrize("cleaned", [True, False])
    def test_a_launch_the_server_cannot_record_is_torn_down(
        self, http, start_runtime, monkeypatch, cleaned
    ):
        def refuse(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(server_mod, "db_create_terminal", refuse)
        answers = {
            CommandType.LAUNCH: {"terminal": dict(LAUNCHED)},
            CommandType.DELETE: {"deleted": cleaned},
        }
        runtime = start_runtime(script=lambda command: answers[command.type])

        response = http.post("/runtimes/rt-1/terminals", json={"agent_profile": "developer"})
        assert response.status_code == 500
        assert ("may still be running" in response.json()["detail"]) is (not cleaned)
        assert runtimes_of(http)["rt-1"]["terminals"] == []
        assert [(c.type, c.terminal_id) for c in runtime.received] == [
            (CommandType.LAUNCH, None),
            (CommandType.DELETE, "beef0001"),
        ]
        assert database.get_terminal_metadata("beef0001") is None


def runtimes_of(http):
    return http.get("/runtimes").json()["runtimes"]


def _dial(server):
    """A raw channel connection, standing in for a runtime."""
    return connect(
        f"ws://{server}/runtime/channel", additional_headers={"x-cao-runtime-token": TOKEN}
    )


async def _next_command(ws, timeout=5):
    frame = decode(await asyncio.wait_for(ws.recv(), timeout))
    assert isinstance(frame, Command), frame
    return frame


class TestReplacedConnection:
    def test_a_replaced_connection_is_closed_and_its_reports_ignored(self, http, server):
        _remote_row("abcd1234", "rt-1")

        async def scenario():
            async with _dial(server) as old:
                await old.send(_hello(statuses={"abcd1234": "idle"}))
                await old.recv()
                async with _dial(server) as new:
                    await new.send(_hello(statuses={"abcd1234": "processing"}))
                    await new.recv()
                    try:
                        # The server may already have closed the old socket.
                        await old.send(encode(Status(terminal_id="abcd1234", status="completed")))
                    except websockets.exceptions.ConnectionClosed:
                        pass
                    with pytest.raises(websockets.exceptions.ConnectionClosed):
                        await asyncio.wait_for(old.recv(), 5)
                    return http.get("/terminals/abcd1234").json()["status"]

        assert asyncio.run(scenario()) == "processing"


class TestUnrecordedTerminals:
    def test_a_terminal_the_server_never_recorded_is_deleted_at_hello(self, http, server):
        _remote_row("abcd1234", "rt-1")

        async def scenario():
            async with _dial(server) as ws:
                await ws.send(_hello(statuses={"abcd1234": "idle", "beef0002": "unknown"}))
                await ws.recv()
                command = await _next_command(ws)
                await ws.send(
                    encode(Result(op_id=command.op_id, ok=True, payload={"deleted": True}))
                )
                # The recorded terminal is left alone.
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(ws.recv(), 0.5)
                return command

        command = asyncio.run(scenario())
        assert (command.type, command.terminal_id) == (CommandType.DELETE, "beef0002")
        assert database.get_terminal_metadata("abcd1234") is not None

    def test_a_launch_that_finishes_after_the_server_gave_up_is_deleted(
        self, http, server, monkeypatch
    ):
        monkeypatch.setattr(server_mod, "LAUNCH_TIMEOUT", 0.5)

        async def scenario():
            async with _dial(server) as ws:
                await ws.send(_hello())
                await ws.recv()
                loop = asyncio.get_running_loop()
                request = loop.run_in_executor(
                    None,
                    lambda: http.post(
                        "/runtimes/rt-1/terminals", json={"agent_profile": "developer"}
                    ),
                )
                launch = await _next_command(ws)
                response = await asyncio.wait_for(request, 10)
                # The agent started after all; its result arrives too late.
                late = Result(op_id=launch.op_id, ok=True, payload={"terminal": dict(LAUNCHED)})
                await ws.send(encode(late))
                delete = await _next_command(ws)
                await ws.send(
                    encode(Result(op_id=delete.op_id, ok=True, payload={"deleted": True}))
                )
                return response, delete

        response, delete = asyncio.run(scenario())
        assert response.status_code == 504
        assert (delete.type, delete.terminal_id) == (CommandType.DELETE, "beef0001")
        assert database.get_terminal_metadata("beef0001") is None


class TestHandshakeOrdering:
    def test_a_terminal_deleted_during_the_hello_is_not_placed_again(
        self, http, start_runtime, monkeypatch
    ):
        _remote_row("abcd1234", "rt-1")
        real = server_mod.list_terminal_ids_on_runtime
        calls = []

        def racing(runtime_id):
            calls.append(runtime_id)
            ids = real(runtime_id)
            if len(calls) == 1:
                # A delete completes right after this snapshot: row gone, and
                # its forget() already ran (a no-op, nothing was placed yet).
                database.delete_terminal("abcd1234")
                registry_mod.runtime_registry.forget("abcd1234")
            return ids

        monkeypatch.setattr(server_mod, "list_terminal_ids_on_runtime", racing)
        start_runtime(script=_answer)
        assert runtimes_of(http)["rt-1"]["terminals"] == [], "a ghost placement came back"

    def test_no_command_reaches_a_runtime_before_the_server_hello(self, http, server, monkeypatch):
        stalled, release = threading.Event(), threading.Event()
        real = server_mod.list_terminal_ids_on_runtime

        def slow(runtime_id):
            stalled.set()
            release.wait(5)
            return real(runtime_id)

        monkeypatch.setattr(server_mod, "list_terminal_ids_on_runtime", slow)
        monkeypatch.setattr(server_mod, "LAUNCH_TIMEOUT", 0.5)

        async def scenario():
            async with _dial(server) as ws:
                await ws.send(_hello())
                await asyncio.to_thread(stalled.wait, 5)
                # A launch while the server is still handling the runtime's hello.
                response = await asyncio.to_thread(
                    http.post, "/runtimes/rt-1/terminals", json={"agent_profile": "developer"}
                )
                release.set()
                return response, decode(await asyncio.wait_for(ws.recv(), 5))

        response, first = asyncio.run(scenario())
        assert response.status_code == 503, response.text
        assert isinstance(first, Hello), first


class TestAttach:
    @pytest.mark.asyncio
    async def test_attaching_to_a_remote_terminal_is_refused_without_local_tmux(self):
        from unittest.mock import AsyncMock, MagicMock

        from cli_agent_orchestrator.api.main import terminal_ws

        _remote_row("abcd1234", "rt-1")
        ws = MagicMock()
        ws.client = MagicMock(host="127.0.0.1")
        ws.headers = {}
        ws.accept = AsyncMock()
        ws.close = AsyncMock()
        await terminal_ws(ws, "abcd1234")
        ws.close.assert_awaited_once()
        assert ws.close.call_args.kwargs.get("code") == 4004
        assert "execution runtime" in ws.close.call_args.kwargs.get("reason", "")


class TestLaunchAcrossAReconnect:
    def test_a_status_reported_while_the_launch_is_recorded_is_kept(
        self, http, server, monkeypatch
    ):
        recording, release = threading.Event(), threading.Event()
        real = server_mod._record_launch

        def slow_record(*args, **kwargs):
            recording.set()
            release.wait(5)
            return real(*args, **kwargs)

        monkeypatch.setattr(server_mod, "_record_launch", slow_record)

        async def scenario():
            async with _dial(server) as old:
                await old.send(_hello())
                await old.recv()
                loop = asyncio.get_running_loop()
                request = loop.run_in_executor(
                    None,
                    lambda: http.post(
                        "/runtimes/rt-1/terminals", json={"agent_profile": "developer"}
                    ),
                )
                launch = await _next_command(old)
                await old.send(
                    encode(
                        Result(op_id=launch.op_id, ok=True, payload={"terminal": dict(LAUNCHED)})
                    )
                )
                await asyncio.to_thread(recording.wait, 5)
                # Reconnected before the row is written; the hello carries the
                # terminal's current status, the only report it will get.
                async with _dial(server) as new:
                    await new.send(_hello(statuses={"beef0001": "processing"}))
                    await new.recv()
                    release.set()
                    response = await asyncio.wait_for(request, 10)
                    return response, http.get("/terminals/beef0001").json()["status"]

        response, status_now = asyncio.run(scenario())
        assert response.status_code == 201, response.text
        assert status_now == "processing"

    def test_a_reconnect_while_a_launch_is_being_recorded_keeps_its_terminal(
        self, http, server, monkeypatch
    ):
        recording, release = threading.Event(), threading.Event()
        real = server_mod._record_launch

        def slow_record(*args, **kwargs):
            recording.set()
            release.wait(5)
            return real(*args, **kwargs)

        monkeypatch.setattr(server_mod, "_record_launch", slow_record)

        async def scenario():
            async with _dial(server) as old:
                await old.send(_hello())
                await old.recv()
                loop = asyncio.get_running_loop()
                request = loop.run_in_executor(
                    None,
                    lambda: http.post(
                        "/runtimes/rt-1/terminals", json={"agent_profile": "developer"}
                    ),
                )
                launch = await _next_command(old)
                await old.send(
                    encode(
                        Result(op_id=launch.op_id, ok=True, payload={"terminal": dict(LAUNCHED)})
                    )
                )
                await asyncio.to_thread(recording.wait, 5)
                # The runtime reconnects before the server has placed the terminal;
                # its hello lists the terminal it just started.
                async with _dial(server) as new:
                    await new.send(_hello(statuses={"beef0001": "idle"}))
                    await new.recv()
                    try:
                        stray = decode(await asyncio.wait_for(new.recv(), 0.5))
                    except asyncio.TimeoutError:
                        stray = None
                    release.set()
                    return await asyncio.wait_for(request, 10), stray

        response, stray = asyncio.run(scenario())
        assert stray is None, f"the server sent {stray} for a launch it was still recording"
        assert response.status_code == 201, response.text
        assert database.get_terminal_metadata("beef0001") is not None

    def test_a_launch_recorded_during_a_reconnect_keeps_the_new_connections_status(
        self, http, server, monkeypatch
    ):
        writing, release = threading.Event(), threading.Event()
        real_create = database.create_terminal

        def slow_create(*args, **kwargs):
            writing.set()
            release.wait(5)
            return real_create(*args, **kwargs)

        monkeypatch.setattr(server_mod, "db_create_terminal", slow_create)

        async def scenario():
            async with _dial(server) as old:
                await old.send(_hello())
                await old.recv()
                loop = asyncio.get_running_loop()
                request = loop.run_in_executor(
                    None,
                    lambda: http.post(
                        "/runtimes/rt-1/terminals", json={"agent_profile": "developer"}
                    ),
                )
                launch = await _next_command(old)
                await old.send(
                    encode(
                        Result(op_id=launch.op_id, ok=True, payload={"terminal": dict(LAUNCHED)})
                    )
                )
                await asyncio.to_thread(writing.wait, 5)
                # The runtime reconnects while the row is being written.
                async with _dial(server) as new:
                    await new.send(_hello())
                    await new.recv()
                    release.set()
                    response = await asyncio.wait_for(request, 10)
                    return response, http.get("/terminals/beef0001").json()["status"]

        response, status_now = asyncio.run(scenario())
        assert response.status_code == 201, response.text
        assert status_now == "unknown", "the old connection's launch status must not stand"


class TestServerThatRunsNoAgents:
    @pytest.mark.parametrize(
        "method,path,kwargs",
        [
            (
                "post",
                "/sessions",
                {"params": {"agent_profile": "developer", "provider": "mock_cli"}},
            ),
            (
                "post",
                "/sessions/cao-local1/terminals",
                {"params": {"agent_profile": "developer", "provider": "mock_cli"}},
            ),
            (
                "post",
                "/terminals/run-step",
                {"json": {"provider": "mock_cli", "agent": "developer", "prompt": "hi"}},
            ),
        ],
    )
    def test_local_launches_are_refused(self, client, monkeypatch, method, path, kwargs):
        monkeypatch.setenv("CAO_LOCAL_EXECUTION", "0")
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code == 409, response.text
        assert "POST /runtimes/{runtime_id}/terminals" in str(response.json()["detail"])


class TestRemoteSessionTeardown:
    def test_a_remote_session_replaced_while_waiting_for_the_lock_is_left_alone(self, monkeypatch):
        from cli_agent_orchestrator.services import session_service

        _remote_row("abcd0001", "rt-1", session="cao-swap1")
        real = session_service.session_is_remote
        calls = []

        def racing(name):
            calls.append(name)
            answer = real(name)
            if len(calls) == 1:
                # Before this delete gets the lock, another delete removes the
                # last remote row and a local session takes the name.
                database.delete_terminal("abcd0001")
                database.create_terminal("beef0002", "cao-swap1", "dev-b", "mock_cli", "developer")
            return answer

        monkeypatch.setattr(session_service, "session_is_remote", racing)
        deleted = []
        monkeypatch.setattr(
            terminal_service,
            "delete_terminal",
            lambda terminal_id, registry=None: deleted.append(terminal_id) or True,
        )
        result = session_service.delete_session("cao-swap1")
        assert deleted == [], "the replacement local terminal is not this delete's"
        assert result["deleted"] == ["cao-swap1"]
        assert database.get_terminal_metadata("beef0002") is not None

    def test_a_session_recorded_remotely_mid_delete_is_torn_down_in_its_runtime(self, monkeypatch):
        from cli_agent_orchestrator.services import session_service

        real = session_service.session_is_remote
        calls = []

        def racing(name):
            calls.append(name)
            if len(calls) == 1:
                # Routed as local; a runtime records this session right after,
                # before the local teardown takes the session lock.
                _remote_row("abcd0001", "rt-1", session="cao-race1")
                return False
            return real(name)

        monkeypatch.setattr(session_service, "session_is_remote", racing)
        deleted = []

        def delete(terminal_id, registry=None):
            deleted.append(terminal_id)
            return database.delete_terminal(terminal_id)

        monkeypatch.setattr(terminal_service, "delete_terminal", delete)
        result = session_service.delete_session("cao-race1")
        assert deleted == ["abcd0001"], "its terminal must be deleted in its runtime"
        assert result["deleted"] == ["cao-race1"]

    def test_concurrent_deletes_of_one_remote_session_tear_it_down_once(self, monkeypatch):
        from cli_agent_orchestrator.services import session_lock, session_service

        _remote_row("abcd0001", "rt-1", session="cao-remote1")
        started, release = threading.Event(), threading.Event()
        calls = []

        def delete(terminal_id, registry=None):
            calls.append(terminal_id)
            started.set()
            release.wait(5)
            return database.delete_terminal(terminal_id)

        monkeypatch.setattr(terminal_service, "delete_terminal", delete)
        results = []

        def run():
            results.append(session_service.delete_session("cao-remote1"))

        first, second = threading.Thread(target=run), threading.Thread(target=run)
        first.start()
        assert started.wait(5)
        second.start()

        def contended():
            with session_lock._registry_guard:
                entry = session_lock._session_locks.get("cao-remote1")
            return entry is not None and entry[1] >= 2

        try:
            _wait_for(contended, "the second delete to wait on the session lock", timeout=5)
        finally:
            release.set()
            first.join(5)
            second.join(5)
        assert calls == ["abcd0001"]
        assert [r["deleted"] for r in results] == [["cao-remote1"], ["cao-remote1"]]

    def test_the_session_event_waits_for_every_terminal_to_be_gone(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        from cli_agent_orchestrator.services import session_service

        _remote_row("abcd0001", "rt-1", session="cao-remote1")
        _remote_row("abcd0002", "rt-1", session="cao-remote1")

        def delete(terminal_id, registry=None):
            if terminal_id == "abcd0002":
                return False  # its runtime deferred the cleanup
            return database.delete_terminal(terminal_id)

        monkeypatch.setattr(terminal_service, "delete_terminal", delete)
        registry = MagicMock()
        registry.dispatch = AsyncMock()
        result = session_service.delete_session("cao-remote1", registry=registry)
        assert result["deleted"] == []
        assert [e["terminal_id"] for e in result["errors"]] == ["abcd0002"]
        events = [c.args[0] for c in registry.dispatch.await_args_list]
        assert events == ["post_kill_terminal"]
        assert registry.dispatch.await_args_list[0].args[1].terminal_id == "abcd0001"


class TestRemoteSession:
    def test_deleting_a_remote_session_deletes_each_terminal_in_its_runtime(
        self, http, start_runtime
    ):
        _remote_row("abcd0001", "rt-1", session="cao-remote1")
        _remote_row("abcd0002", "rt-1", session="cao-remote1")
        runtime = start_runtime(script=lambda command: {"deleted": True})

        response = http.delete("/sessions/cao-remote1")
        assert response.status_code == 200, response.text
        assert response.json()["deleted"] == ["cao-remote1"]
        assert response.json()["errors"] == []
        assert sorted(c.terminal_id for c in runtime.received) == ["abcd0001", "abcd0002"]
        assert {c.type for c in runtime.received} == {CommandType.DELETE}
        assert database.list_terminals_by_session("cao-remote1") == []

    def test_a_terminal_the_runtime_could_not_delete_is_reported_and_kept(
        self, http, start_runtime
    ):
        _remote_row("abcd0001", "rt-1", session="cao-remote1")
        start_runtime(script=lambda command: {"deleted": False})

        # The runtime deferred the cleanup: the same retryable 409 as locally.
        response = http.delete("/sessions/cao-remote1")
        assert response.status_code == 409
        assert database.get_terminal_metadata("abcd0001") is not None


class _RecordingPlugins:
    """Stands in for the server's plugin registry and records what it is sent."""

    def __init__(self):
        self.events = []

    async def dispatch(self, event_type, event):
        self.events.append((event_type, event))


class TestLaunchEvents:
    def test_a_remote_launch_emits_the_central_lifecycle_events(
        self, http, start_runtime, monkeypatch
    ):
        plugins = _RecordingPlugins()
        monkeypatch.setattr(app.state, "plugin_registry", plugins)
        start_runtime(script=_answer)

        response = http.post("/runtimes/rt-1/terminals", json={"agent_profile": "developer"})
        assert response.status_code == 201, response.text
        _wait_for(lambda: len(plugins.events) >= 2, "the launch's plugin events")
        by_type = dict(plugins.events)
        assert sorted(by_type) == ["post_create_session", "post_create_terminal"]
        created = by_type["post_create_terminal"]
        assert (created.terminal_id, created.provider, created.agent_name) == (
            "beef0001",
            "mock_cli",
            "developer",
        )
        assert created.session_id == "cao-beef"
        assert by_type["post_create_session"].session_name == "cao-beef"

    def test_a_launch_the_server_could_not_record_emits_nothing(
        self, http, start_runtime, monkeypatch
    ):
        plugins = _RecordingPlugins()
        monkeypatch.setattr(app.state, "plugin_registry", plugins)

        def fail(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(server_mod, "db_create_terminal", fail)
        start_runtime(script=_answer)
        response = http.post("/runtimes/rt-1/terminals", json={"agent_profile": "developer"})
        assert response.status_code == 500
        time.sleep(0.2)
        assert plugins.events == []


class TestWithApiAuthentication:
    """The remote-runtime example turns on the API token; the channel keeps its own."""

    API_TOKEN = "test-api-token"

    def test_the_channel_works_and_the_api_requires_the_token(
        self, http, start_runtime, monkeypatch
    ):
        monkeypatch.setenv("CAO_AUTH_LOCAL_TOKEN", self.API_TOKEN)
        start_runtime(script=_answer)  # dials with the runtime token only
        bearer = {"Authorization": f"Bearer {self.API_TOKEN}"}

        assert http.get("/runtimes").status_code == 401
        launch = {"agent_profile": "developer"}
        assert http.post("/runtimes/rt-1/terminals", json=launch).status_code == 401
        assert http.get("/runtimes", headers=bearer).json()["runtimes"]["rt-1"]
        response = http.post("/runtimes/rt-1/terminals", json=launch, headers=bearer)
        assert response.status_code == 201, response.text
        assert http.get("/terminals/beef0001").status_code == 401
        assert http.get("/terminals/beef0001", headers=bearer).status_code == 200


class TestInvalidLaunchResults:
    @pytest.mark.parametrize(
        "terminal",
        [
            {"id": "beef0001"},  # fields missing
            {**LAUNCHED, "provider": "not-a-provider"},
            {**LAUNCHED, "session_name": 7},
        ],
    )
    def test_an_invalid_result_naming_a_terminal_is_undone(self, http, start_runtime, terminal):
        def script(command):
            if command.type == CommandType.LAUNCH:
                return {"terminal": terminal}
            return {"deleted": True}

        runtime = start_runtime(script=script)
        response = http.post("/runtimes/rt-1/terminals", json={"agent_profile": "developer"})
        assert response.status_code == 502, response.text
        assert "invalid launch result" in response.json()["detail"]
        assert [(c.type, c.terminal_id) for c in runtime.received][1:] == [
            (CommandType.DELETE, "beef0001")
        ]
        assert database.get_terminal_metadata("beef0001") is None
        assert runtimes_of(http)["rt-1"]["terminals"] == []

    @pytest.mark.parametrize("payload", [{}, {"terminal": "beef0001"}, {"terminal": {"id": 7}}])
    def test_an_invalid_result_naming_nothing_is_a_502(self, http, start_runtime, payload):
        runtime = start_runtime(script=lambda command: payload)
        response = http.post("/runtimes/rt-1/terminals", json={"agent_profile": "developer"})
        assert response.status_code == 502, response.text
        assert "may still be running" in response.json()["detail"]
        assert [c.type for c in runtime.received] == [CommandType.LAUNCH]


class TestSessionNameCollisions:
    @pytest.mark.parametrize("existing_runtime", ["rt-2", None])
    def test_a_launch_into_a_session_name_already_in_use_is_undone(
        self, http, start_runtime, existing_runtime
    ):
        # Another runtime's session, or a local one, already has this name.
        database.create_terminal(
            "aaaa0001", "cao-beef", "developer-aaaa", "mock_cli", "developer",
            runtime_id=existing_runtime,
        )  # fmt: skip
        runtime = start_runtime(script=_answer)  # launches beef0001 in cao-beef
        response = http.post("/runtimes/rt-1/terminals", json={"agent_profile": "developer"})
        assert response.status_code == 409, response.text
        assert "cao-beef" in response.json()["detail"]
        assert [(c.type, c.terminal_id) for c in runtime.received][1:] == [
            (CommandType.DELETE, "beef0001")
        ]
        assert [t["id"] for t in database.list_terminals_by_session("cao-beef")] == ["aaaa0001"]
        assert runtimes_of(http)["rt-1"]["terminals"] == []

    def test_a_launch_reusing_a_terminal_id_leaves_the_other_terminal_placed(
        self, http, start_runtime
    ):
        _remote_row("beef0001", "rt-2", session="cao-other")
        registry_mod.runtime_registry.place("beef0001", "rt-2")
        runtime = start_runtime(script=_answer)
        response = http.post("/runtimes/rt-1/terminals", json={"agent_profile": "developer"})
        assert response.status_code == 409, response.text
        assert [(c.type, c.terminal_id) for c in runtime.received][1:] == [
            (CommandType.DELETE, "beef0001")
        ]
        assert database.get_terminal_metadata("beef0001")["tmux_session"] == "cao-other"
        assert registry_mod.runtime_registry.is_placed("beef0001", "rt-2")

    def test_a_local_session_cannot_take_a_remote_sessions_name(self, client):
        _remote_row("abcd0001", "rt-1", session="cao-remote1")
        response = client.post(
            "/sessions",
            params={
                "agent_profile": "developer",
                "provider": "mock_cli",
                "session_name": "cao-remote1",
            },
        )
        assert response.status_code == 400, response.text
        assert "already exists" in response.json()["detail"]
        assert [t["id"] for t in database.list_terminals_by_session("cao-remote1")] == ["abcd0001"]


class TestSiblings:
    def test_a_remote_siblings_status_is_unknown_before_its_runtime_reconnects(self, http):
        # As after a server restart: remote rows, no runtime connected yet.
        _remote_row("abcd0001", "rt-1", session="cao-grp1")
        _remote_row("abcd0002", "rt-1", session="cao-grp1")
        for terminal_id in ("abcd0001", "abcd0002"):
            database.update_terminal_group(terminal_id, ["team"])
        response = http.get("/terminals/abcd0001/siblings")
        assert response.status_code == 200, response.text
        body = response.json()
        siblings = body["siblings"] if isinstance(body, dict) else body
        assert [(s["id"], s["status"]) for s in siblings] == [("abcd0002", "unknown")]
        assert "runtime_id" not in siblings[0], "the response shape is unchanged"

    def test_a_remote_siblings_status_is_the_one_its_runtime_reported(self, http, start_runtime):
        _remote_row("abcd0001", "rt-1", session="cao-grp1")
        _remote_row("abcd0002", "rt-1", session="cao-grp1")
        for terminal_id in ("abcd0001", "abcd0002"):
            database.update_terminal_group(terminal_id, ["team"])
        start_runtime(
            script=_answer,
            statuses={"abcd0001": TerminalStatus.IDLE, "abcd0002": TerminalStatus.PROCESSING},
        )
        response = http.get("/terminals/abcd0001/siblings")
        assert response.status_code == 200, response.text
        body = response.json()
        siblings = body["siblings"] if isinstance(body, dict) else body
        assert [(s["id"], s["status"]) for s in siblings] == [("abcd0002", "processing")]


class TestLaunchCancellation:
    @pytest.mark.asyncio
    async def test_a_launch_whose_caller_went_away_is_still_settled(self, monkeypatch):
        from types import SimpleNamespace

        registry = server_mod.runtime_registry
        sent = []

        async def send_text(text):
            command = decode(text)
            sent.append(command)
            payload = (
                {"terminal": dict(LAUNCHED)}
                if command.type == CommandType.LAUNCH
                else {"deleted": True}
            )
            reply = Result(op_id=command.op_id, ok=True, payload=payload)
            if command.type == CommandType.LAUNCH:
                # What the channel's receive loop does for an awaited launch.
                registry.reserve("beef0001", "rt-1")
            asyncio.get_running_loop().call_soon(conn.resolve, reply)

        conn = registry.register("rt-1", send_text)
        registry.activate(conn)
        entered, release = threading.Event(), threading.Event()

        def failing_record(*args, **kwargs):
            entered.set()
            release.wait(5)
            raise RuntimeError("disk full")

        monkeypatch.setattr(server_mod, "_record_launch", failing_record)
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(plugin_registry=None)))
        body = server_mod.LaunchRequest(agent_profile="developer")
        task = asyncio.ensure_future(server_mod.launch_on_runtime("rt-1", body, request))
        await asyncio.to_thread(entered.wait, 5)
        task.cancel()  # the client went away while the row was being written
        await asyncio.sleep(0.05)
        assert registry.is_known("beef0001", "rt-1"), "reserved until the launch settles"
        release.set()
        for _ in range(250):
            if any(c.type == CommandType.DELETE for c in sent):
                break
            await asyncio.sleep(0.02)
        assert [(c.type, c.terminal_id) for c in sent] == [
            (CommandType.LAUNCH, None),
            (CommandType.DELETE, "beef0001"),
        ]
        for _ in range(50):
            if not registry.is_known("beef0001", "rt-1"):
                break
            await asyncio.sleep(0.02)
        assert not registry.is_known("beef0001", "rt-1")
        with pytest.raises(asyncio.CancelledError):
            await task


class TestReplacedSocket:
    def test_a_replaced_connection_is_closed_even_if_it_never_speaks(self, server):
        async def scenario():
            async with _dial(server) as old:
                await old.send(_hello())
                await old.recv()
                async with _dial(server) as new:
                    await new.send(_hello())
                    await new.recv()
                    try:
                        await asyncio.wait_for(old.recv(), 5)
                    except websockets.exceptions.ConnectionClosed:
                        return "closed"
                    except asyncio.TimeoutError:
                        return "still open"
                    return "got a frame"

        assert asyncio.run(scenario()) == "closed"

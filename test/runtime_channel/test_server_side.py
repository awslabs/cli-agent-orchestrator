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
from cli_agent_orchestrator.models.terminal import LocalExecutionDisabledError, TerminalStatus
from cli_agent_orchestrator.plugins import PluginRegistry
from cli_agent_orchestrator.runtime_channel import token as token_mod
from cli_agent_orchestrator.runtime_channel.bridge import Bridge, UnreportedLaunch
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
from cli_agent_orchestrator.utils import agent_profiles

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


def _no_installed_profile(name):
    # The host's profile stores must not decide a test: an installed or an
    # unreadable profile there turned local launches here into 500s.
    raise FileNotFoundError(name)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'server.db'}", connect_args={"check_same_thread": False}
    )
    database.Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    # The loader create_terminal calls (#866 routed it through load_launch_profile).
    monkeypatch.setattr(agent_profiles, "load_launch_profile", _no_installed_profile)
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

        def reconnect_meanwhile(terminal_id, metadata, registry=None, **kwargs):
            # A reconnect's hello still finds the row, and places the terminal.
            registry_mod.runtime_registry.place(terminal_id, "rt-1")
            return real(terminal_id, metadata, registry=registry, **kwargs)

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

    def test_a_delivered_orchestrated_message_emits_the_callers_text(
        self, http, start_runtime, monkeypatch
    ):
        # The runtime injects memory beside the pane; the server's event carries
        # what the caller sent, as the local path's original_message does.
        _remote_row("abcd1234", "rt-1")
        plugins = _RecordingPlugins()
        monkeypatch.setattr(app.state, "plugin_registry", plugins)
        start_runtime(script=lambda command: {"success": True})
        response = http.post(
            "/terminals/abcd1234/input",
            params={"message": "hi", "sender_id": "abcd9999", "orchestration_type": "send_message"},
        )
        assert response.status_code == 200 and response.json()["success"] is True
        _wait_for(lambda: plugins.events, "the post_send_message event")
        [(event_type, event)] = plugins.events
        assert event_type == "post_send_message"
        assert (event.session_id, event.sender, event.receiver, event.message) == (
            "cao-remote1",
            "abcd9999",
            "abcd1234",
            "hi",
        )
        assert event.orchestration_type == OrchestrationType.SEND_MESSAGE

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
        received = [(c.type, c.terminal_id) for c in runtime.received]
        assert received[:2] == [
            (CommandType.LAUNCH, None),
            (CommandType.DELETE, "beef0001"),
        ]
        # A deferred delete is retried (TestUndoRetry); never anything else.
        assert set(received[2:]) <= {(CommandType.DELETE, "beef0001")}
        assert cleaned is False or len(received) == 2
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

    def test_an_undo_is_not_confirmed_by_another_instance_of_the_runtime(
        self, http, server, monkeypatch
    ):
        # The new connection's hello does not list the terminal: another
        # instance took over rt-1. A real cao-bridge answers a delete of a
        # terminal it never ran with deleted/absent, which proves nothing.
        response, received = _undo_across_a_reconnect(
            http, server, monkeypatch, {}, {"deleted": True, "absent": True}
        )
        assert response.status_code == 409, response.text
        assert "may still be running there" in response.json()["detail"]
        assert received == [], "no delete goes to an instance that never ran the terminal"

        # The instance that runs it lists it when it reconnects: deleted then.
        async def original_reconnects():
            async with _dial(server) as again:
                await again.send(_hello(statuses={"beef0001": "idle"}))
                await again.recv()
                return await _next_command(again)

        command = asyncio.run(original_reconnects())
        assert (command.type, command.terminal_id) == (CommandType.DELETE, "beef0001")

    def test_an_undo_reaches_the_runtime_through_its_new_connection(
        self, http, server, monkeypatch
    ):
        # The new connection's hello lists the terminal: the same runtime,
        # reconnected while the launch was being recorded (so that hello did
        # not delete it). The undo is sent there, and its answer counts.
        response, received = _undo_across_a_reconnect(
            http, server, monkeypatch, {"beef0001": "idle"}, {"deleted": True}
        )
        assert response.status_code == 409, response.text
        assert "it was deleted" in response.json()["detail"]
        assert received == [(CommandType.DELETE, "beef0001")]

    def test_a_reconnect_during_an_undo_still_gets_the_terminal_deleted(
        self, http, server, monkeypatch
    ):
        monkeypatch.setattr(
            server_mod, "_record_launch", lambda *a, **k: "session cao-beef already exists"
        )

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
                undo = await _next_command(old)  # left unanswered: the connection drops
                # The same runtime reconnects mid-undo; its hello lists the terminal.
                async with _dial(server) as new:
                    await new.send(_hello(statuses={"beef0001": "idle"}))
                    await new.recv()
                    retry = await _next_command(new)
                    await new.send(
                        encode(Result(op_id=retry.op_id, ok=True, payload={"deleted": True}))
                    )
                    return undo, retry, await asyncio.wait_for(request, 10)

        undo, retry, response = asyncio.run(scenario())
        assert (undo.type, undo.terminal_id) == (CommandType.DELETE, "beef0001")
        assert (retry.type, retry.terminal_id) == (CommandType.DELETE, "beef0001")
        assert response.status_code == 409, response.text

    def test_an_undo_releases_the_launch_before_it_deletes(self, http, start_runtime, monkeypatch):
        # Still reserved, the terminal would be skipped by a hello that arrives
        # during the undo, as a launch being recorded: never deleted.
        monkeypatch.setattr(
            server_mod, "_record_launch", lambda *a, **k: "session cao-beef already exists"
        )
        registry = registry_mod.runtime_registry
        known_during_delete = []

        def script(command):
            if command.type == CommandType.DELETE:
                known_during_delete.append(registry.is_known("beef0001", "rt-1"))
                return {"deleted": True}
            return {"terminal": dict(LAUNCHED)}

        start_runtime(script=script)
        response = http.post("/runtimes/rt-1/terminals", json={"agent_profile": "developer"})
        assert response.status_code == 409, response.text
        assert known_during_delete == [False]


def _undo_across_a_reconnect(http, server, monkeypatch, hello_statuses, answer):
    """Launch over one connection. While the launch is being recorded, rt-1
    reconnects with ``hello_statuses``; then the record step refuses the launch.

    Returns the response, and the (type, terminal id) of each command the new
    connection received, each answered with ``answer``.
    """
    recording, release = threading.Event(), threading.Event()

    def refusing_record(*args, **kwargs):
        recording.set()
        release.wait(5)
        return "session cao-beef already exists"

    monkeypatch.setattr(server_mod, "_record_launch", refusing_record)
    received = []

    async def answer_commands(ws):
        try:
            while True:
                frame = decode(await ws.recv())
                if isinstance(frame, Command):
                    received.append((frame.type, frame.terminal_id))
                    await ws.send(encode(Result(op_id=frame.op_id, ok=True, payload=answer)))
        except websockets.exceptions.ConnectionClosed:
            return

    async def scenario():
        async with _dial(server) as old:
            await old.send(_hello())
            await old.recv()
            loop = asyncio.get_running_loop()
            request = loop.run_in_executor(
                None,
                lambda: http.post("/runtimes/rt-1/terminals", json={"agent_profile": "developer"}),
            )
            launch = await _next_command(old)
            await old.send(
                encode(Result(op_id=launch.op_id, ok=True, payload={"terminal": dict(LAUNCHED)}))
            )
            await asyncio.to_thread(recording.wait, 5)
            async with _dial(server) as new:
                await new.send(_hello(statuses=hello_statuses))
                await new.recv()
                responder = asyncio.create_task(answer_commands(new))
                release.set()
                response = await asyncio.wait_for(request, 10)
                await asyncio.sleep(0.3)  # room for a background retry, if any
                responder.cancel()
                await asyncio.gather(responder, return_exceptions=True)
                return response

    return asyncio.run(scenario()), received


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
            "delete_remote_terminal",
            lambda terminal_id: deleted.append(terminal_id) or (True, True),
        )
        result = session_service.delete_session("cao-swap1")
        assert deleted == [], "the replacement local terminal is not this delete's"
        # The name is taken again, so it is not reported as freed.
        assert result["deleted"] == []
        assert "another session" in result["errors"][0]["error"]
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

        def delete(terminal_id):
            deleted.append(terminal_id)
            dropped = database.delete_terminal(terminal_id)
            return dropped, dropped

        monkeypatch.setattr(terminal_service, "delete_remote_terminal", delete)
        result = session_service.delete_session("cao-race1")
        assert deleted == ["abcd0001"], "its terminal must be deleted in its runtime"
        assert result["deleted"] == ["cao-race1"]

    def test_concurrent_deletes_of_one_remote_session_tear_it_down_once(self, monkeypatch):
        from cli_agent_orchestrator.services import session_lock, session_service

        _remote_row("abcd0001", "rt-1", session="cao-remote1")
        started, release = threading.Event(), threading.Event()
        calls = []

        def delete(terminal_id):
            calls.append(terminal_id)
            started.set()
            release.wait(5)
            dropped = database.delete_terminal(terminal_id)
            return dropped, dropped

        monkeypatch.setattr(terminal_service, "delete_remote_terminal", delete)
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

        def delete(terminal_id):
            if terminal_id == "abcd0002":
                return False, False  # its runtime deferred the cleanup
            dropped = database.delete_terminal(terminal_id)
            return dropped, dropped

        monkeypatch.setattr(terminal_service, "delete_remote_terminal", delete)
        registry = MagicMock()
        registry.dispatch = AsyncMock()
        result = session_service.delete_session("cao-remote1", registry=registry)
        assert result["deleted"] == []
        assert [e["terminal_id"] for e in result["errors"]] == ["abcd0002"]
        events = [c.args[0] for c in registry.dispatch.await_args_list]
        assert events == ["post_kill_terminal"]
        assert registry.dispatch.await_args_list[0].args[1].terminal_id == "abcd0001"

    def test_a_terminal_a_concurrent_delete_removed_counts_as_deleted_once(
        self, http, start_runtime, monkeypatch
    ):
        from unittest.mock import AsyncMock, MagicMock

        from cli_agent_orchestrator.services import session_service

        # A DELETE /terminals of the session's terminal drops its row (and
        # dispatches its event) while this session delete waits on the runtime.
        _remote_row("abcd0001", "rt-1", session="cao-remote1")
        start_runtime(script=_answer)
        real_delete_row = terminal_service.delete_terminal_row

        def the_other_request_won(terminal_id, *args, **kwargs):
            assert real_delete_row(terminal_id, *args, **kwargs)
            return False

        monkeypatch.setattr(terminal_service, "delete_terminal_row", the_other_request_won)
        registry = MagicMock()
        registry.dispatch = AsyncMock()
        result = session_service.delete_session("cao-remote1", registry=registry)
        assert result == {"deleted": ["cao-remote1"], "errors": []}
        assert registry.dispatch.await_args_list == [], "the other delete owns its event"


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
            # Names local creation refuses: the session APIs could not address them.
            {**LAUNCHED, "session_name": "cao:bad"},
            {**LAUNCHED, "session_name": "-cao-beef"},
            {**LAUNCHED, "name": "developer.beef"},
            {**LAUNCHED, "name": "-developer"},
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
    async def test_a_launch_cancelled_before_it_is_sent_never_starts(self):
        from types import SimpleNamespace

        registry = server_mod.runtime_registry
        written, release = [], asyncio.Event()

        async def send_text(text):
            written.append(decode(text))
            await release.wait()  # the first send holds the channel

        conn = registry.register("rt-1", send_text)
        registry.activate(conn)
        holder = asyncio.ensure_future(conn.call(CommandType.KEY, {"key": "x"}, "t1", timeout=5))
        await asyncio.sleep(0.05)
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(plugin_registry=None)))
        body = server_mod.LaunchRequest(agent_profile="developer")
        task = asyncio.ensure_future(server_mod.launch_on_runtime("rt-1", body, request))
        await asyncio.sleep(0.05)  # queued behind the held channel
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await asyncio.sleep(0.2)
        holder.cancel()
        assert [c.type for c in written] == [CommandType.KEY], "the launch must never be sent"
        assert not server_mod._settlements, "nothing is left running for it"

    @pytest.mark.asyncio
    async def test_a_cancel_landing_with_the_launch_result_still_settles_it(self):
        from types import SimpleNamespace

        registry = server_mod.runtime_registry
        sent = []

        async def send_text(text):
            sent.append(decode(text))

        conn = registry.register("rt-1", send_text)
        registry.activate(conn)
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(plugin_registry=None)))
        body = server_mod.LaunchRequest(agent_profile="developer")
        task = asyncio.ensure_future(server_mod.launch_on_runtime("rt-1", body, request))
        for _ in range(100):
            if sent:
                break
            await asyncio.sleep(0.01)
        # The client goes away while the runtime is still starting the agent.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Then the result arrives, as the receive loop delivers it.
        (launch,) = sent
        if conn.awaits(launch.op_id):
            registry.reserve("beef0001", "rt-1")
        conn.resolve(Result(op_id=launch.op_id, ok=True, payload={"terminal": dict(LAUNCHED)}))
        for _ in range(200):
            if database.get_terminal_metadata("beef0001") and not server_mod._settlements:
                break
            await asyncio.sleep(0.01)
        assert database.get_terminal_metadata("beef0001") is not None, "recorded anyway"
        assert ("beef0001", "rt-1") not in registry._reserved, "the reservation was released"

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


class TestFailedRowDelete:
    def test_a_row_the_delete_did_not_drop_keeps_its_placement(
        self, http, start_runtime, monkeypatch
    ):
        _remote_row("abcd1234", "rt-1")
        start_runtime(script=_answer, statuses={"abcd1234": TerminalStatus.IDLE})
        monkeypatch.setattr(terminal_service, "delete_terminal_row", lambda *args, **kwargs: False)
        http.delete("/terminals/abcd1234")
        assert database.get_terminal_metadata("abcd1234") is not None
        assert runtimes_of(http)["rt-1"]["terminals"] == ["abcd1234"]

    def test_a_row_that_could_not_be_dropped_keeps_its_placement(
        self, http, start_runtime, monkeypatch
    ):
        _remote_row("abcd1234", "rt-1")
        start_runtime(script=_answer, statuses={"abcd1234": TerminalStatus.IDLE})

        def locked(*args, **kwargs):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(terminal_service, "delete_terminal_row", locked)
        response = http.delete("/terminals/abcd1234")
        assert response.status_code >= 500, response.text
        # The row survives for a retry, and so does its routing.
        assert database.get_terminal_metadata("abcd1234") is not None
        assert runtimes_of(http)["rt-1"]["terminals"] == ["abcd1234"]

    def test_a_delete_whose_row_a_concurrent_delete_dropped_succeeds(
        self, http, start_runtime, monkeypatch
    ):
        # Two DELETEs of one terminal: the runtime confirms both (the second as
        # absent), and the other request drops the central row first.
        _remote_row("abcd1234", "rt-1")
        start_runtime(script=_answer, statuses={"abcd1234": TerminalStatus.IDLE})
        real_delete_row = terminal_service.delete_terminal_row

        def the_other_request_won(terminal_id, *args, **kwargs):
            assert real_delete_row(terminal_id, *args, **kwargs)
            return False  # this request's own drop then finds no row

        monkeypatch.setattr(terminal_service, "delete_terminal_row", the_other_request_won)
        response = http.delete("/terminals/abcd1234")
        assert response.status_code == 200, response.text
        assert database.get_terminal_metadata("abcd1234") is None
        assert runtimes_of(http)["rt-1"]["terminals"] == []

    def test_a_delete_leaves_a_terminal_launched_since_under_the_same_id(
        self, http, start_runtime, monkeypatch
    ):
        # While this delete waits on the runtime, another drops the row, and a
        # new launch reuses the 8-hex id (another session) before this one
        # drops "its" row.
        _remote_row("abcd1234", "rt-1", session="cao-old1")
        start_runtime(script=_answer, statuses={"abcd1234": TerminalStatus.IDLE})
        real_call = terminal_service._call_runtime

        def replaced_meanwhile(row, command_type, payload, **kwargs):
            answer = real_call(row, command_type, payload, **kwargs)
            if command_type == CommandType.DELETE:
                database.delete_terminal("abcd1234")
                _remote_row("abcd1234", "rt-1", session="cao-new1")
            return answer

        monkeypatch.setattr(terminal_service, "_call_runtime", replaced_meanwhile)
        response = http.delete("/terminals/abcd1234")
        assert response.status_code == 200, response.text  # the terminal it meant is gone
        row = database.get_terminal_metadata("abcd1234")
        assert row is not None and row["tmux_session"] == "cao-new1", "the new terminal is kept"
        assert runtimes_of(http)["rt-1"]["terminals"] == ["abcd1234"], "and so is its routing"


class TestLaunchIdRace:
    def test_a_launch_losing_an_id_race_leaves_the_winner_alone(self):
        registry = registry_mod.runtime_registry
        # rt-2's launch of beef0001 has claimed the id and is writing its row.
        registry.place("beef0001", "rt-2")
        launched = server_mod._Launched.model_validate({**LAUNCHED, "session_name": "cao-other"})
        conflict = server_mod._record_launch("rt-1", launched, None)
        assert conflict == "terminal id beef0001 is already in use"
        assert registry.is_placed("beef0001", "rt-2")
        assert database.get_terminal_metadata("beef0001") is None

    def test_reservations_of_one_id_on_two_runtimes_do_not_overwrite_each_other(self):
        registry = registry_mod.runtime_registry
        registry.reserve("beef0001", "rt-1")
        registry.reserve("beef0001", "rt-2")
        registry.release("beef0001", "rt-2")
        assert registry.is_known("beef0001", "rt-1")
        assert not registry.is_known("beef0001", "rt-2")

    def test_a_deleted_terminals_placement_is_kept_for_a_launch_of_its_id(self):
        # Between a launch's claim and its row write, a delete of the old
        # terminal with that id finds no row: the claim is the launch's.
        registry = registry_mod.runtime_registry
        registry.reserve("beef0001", "rt-1")
        registry.claim("beef0001", "rt-1")
        registry.forget_unless_launching("beef0001")
        assert registry.is_placed("beef0001", "rt-1")

    def test_a_stale_placement_does_not_block_another_runtimes_launch_of_its_id(self):
        registry = registry_mod.runtime_registry
        registry.place("beef0001", "rt-1")
        registry.reserve("beef0001", "rt-2")
        registry.forget_unless_launching("beef0001")
        assert not registry.is_placed("beef0001", "rt-1")
        assert registry.claim("beef0001", "rt-2")

    def test_a_launch_losing_a_row_race_on_its_runtime_keeps_the_winners_placement(
        self, monkeypatch
    ):
        # Two launches on rt-1 reported beef0001 in different sessions, so under
        # different locks: the other wrote its row after this one's checks.
        registry = registry_mod.runtime_registry
        real_create = server_mod.db_create_terminal

        def create_after_the_winner(terminal_id, *args, **kwargs):
            real_create(terminal_id, "cao-winner", "developer-beef", "mock_cli", runtime_id="rt-1")
            return real_create(terminal_id, *args, **kwargs)  # its primary key is taken

        monkeypatch.setattr(server_mod, "db_create_terminal", create_after_the_winner)
        launched = server_mod._Launched.model_validate(LAUNCHED)
        with pytest.raises(Exception):
            server_mod._record_launch("rt-1", launched, None)
        assert database.get_terminal_metadata("beef0001")["tmux_session"] == "cao-winner"
        assert registry.is_placed("beef0001", "rt-1"), "the winner's row still places it"

    def test_a_launch_whose_row_write_fails_is_unplaced(self, monkeypatch):
        def refuse(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(server_mod, "db_create_terminal", refuse)
        launched = server_mod._Launched.model_validate(LAUNCHED)
        with pytest.raises(RuntimeError):
            server_mod._record_launch("rt-1", launched, None)
        assert not registry_mod.runtime_registry.is_placed("beef0001", "rt-1")


class TestUnrecordedCleanupPerId:
    @pytest.mark.asyncio
    async def test_a_repeated_unrecorded_id_gets_one_cleanup(self, monkeypatch):
        registry = registry_mod.runtime_registry
        deletes = []

        async def send_text(text):
            command = decode(text)
            deletes.append(command.terminal_id)
            reply = Result(op_id=command.op_id, ok=True, payload={"deleted": False})
            asyncio.get_running_loop().call_soon(conn.resolve, reply)

        conn = registry.register("rt-1", send_text)
        registry.activate(conn)
        # Deferred by the runtime: each cleanup then waits this long to retry.
        monkeypatch.setattr(server_mod, "UNRECORDED_RETRY_DELAY", 60.0)
        try:
            for _ in range(3):  # a runtime repeating an id, in results it made up
                server_mod._delete_unrecorded(conn, "beef0009")
            server_mod._delete_unrecorded(conn, "beef0010")  # another id gets its own
            for _ in range(100):
                if len(deletes) >= 2:
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            assert sorted(deletes) == ["beef0009", "beef0010"]
            assert len(server_mod._teardowns) == 2
        finally:
            tasks = list(server_mod._teardowns)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        assert not server_mod._unrecorded, "finished cleanups are forgotten"


class TestUnreportedLaunch:
    @staticmethod
    def _fail_launches_naming(runtime, terminal_id):
        # What cao-bridge does for an agent it could neither report nor stop.
        async def execute(command):
            runtime.received.append(command)
            if command.type == CommandType.LAUNCH:
                raise UnreportedLaunch(
                    f"launched terminal {terminal_id} but could not report it", terminal_id
                )
            return {"deleted": True}

        runtime.execute = execute

    def test_a_failed_launch_naming_its_terminal_gets_it_deleted(self, http, start_runtime):
        runtime = start_runtime()
        self._fail_launches_naming(runtime, "beef0009")
        response = http.post("/runtimes/rt-1/terminals", json={"agent_profile": "developer"})
        assert response.status_code == 502, response.text
        _wait_for(
            lambda: (CommandType.DELETE, "beef0009")
            in [(c.type, c.terminal_id) for c in runtime.received],
            "the server to delete the unreported terminal",
        )

    def test_a_failed_launch_cannot_get_a_recorded_terminal_deleted(self, http, start_runtime):
        _remote_row("abcd1234", "rt-1")
        runtime = start_runtime(script=_answer)
        self._fail_launches_naming(runtime, "abcd1234")
        response = http.post("/runtimes/rt-1/terminals", json={"agent_profile": "developer"})
        assert response.status_code == 502, response.text
        time.sleep(0.3)
        assert [c.type for c in runtime.received] == [CommandType.LAUNCH]
        assert database.get_terminal_metadata("abcd1234") is not None


class TestStatusOnReconnect:
    def test_a_reconnected_runtime_republishes_its_statuses(self, start_runtime, monkeypatch):
        # Status consumers (approval prompts, inbox delivery) react to status
        # events only. After a hello, cao-bridge sends each terminal's status
        # as a status frame, which the server publishes: a terminal already
        # waiting for its user when the server restarted gets its prompt back.
        from cli_agent_orchestrator.services.event_bus import bus

        _remote_row("abcd1234", "rt-1")
        events, loop = [], asyncio.new_event_loop()
        monkeypatch.setattr(bus, "_loop", loop)
        queue = loop.run_until_complete(_subscribe(bus))
        try:
            start_runtime(statuses={"abcd1234": TerminalStatus.WAITING_USER_ANSWER})
            event = loop.run_until_complete(asyncio.wait_for(queue.get(), 5))
            events.append((event["topic"], event["data"]))
        finally:
            bus.unsubscribe("terminal.*.status", queue)
            loop.close()
        assert events == [("terminal.abcd1234.status", {"status": "waiting_user_answer"})]


async def _subscribe(bus):
    return bus.subscribe("terminal.*.status")


class TestUnrecordedCleanupRetry:
    @pytest.mark.asyncio
    async def test_a_deferred_delete_of_an_unrecorded_terminal_is_retried(self, monkeypatch):
        registry = RuntimeRegistry()
        deletes = []

        async def send_text(text):
            command = decode(text)
            deletes.append(command)
            done = len(deletes) >= 3  # the runtime defers twice, then succeeds
            reply = Result(op_id=command.op_id, ok=True, payload={"deleted": done})
            asyncio.get_running_loop().call_soon(conn.resolve, reply)

        conn = registry.register("rt-1", send_text)
        registry.activate(conn)
        monkeypatch.setattr(server_mod, "UNRECORDED_RETRY_DELAY", 0.01)
        server_mod._delete_unrecorded(conn, "beef0009")
        for _ in range(200):
            if len(deletes) >= 3 and not server_mod._teardowns:
                break
            await asyncio.sleep(0.01)
        assert [(c.type, c.terminal_id) for c in deletes] == [(CommandType.DELETE, "beef0009")] * 3
        assert not server_mod._teardowns, "the retries stop once it is deleted"


class TestLaunchMetadata:
    def test_a_remote_launch_keeps_its_kiro_engine(self, http, start_runtime):
        kiro = {**LAUNCHED, "provider": "kiro_cli", "engine": "kas"}
        start_runtime(script=lambda command: {"terminal": dict(kiro)})
        response = http.post("/runtimes/rt-1/terminals", json={"agent_profile": "developer"})
        assert response.status_code == 201, response.text
        assert database.get_terminal_metadata("beef0001")["engine"] == "kas"

    def test_a_remote_launch_keeps_the_model_its_runtime_launched_with(self, http, start_runtime):
        from cli_agent_orchestrator.runtime_channel.bridge import _jsonable

        # What the runtime's get_terminal returns, through cao-bridge's own
        # serializer: a scripted result alone would bypass what it drops.
        local = {
            **LAUNCHED,
            "provider": "claude_code",
            "status": TerminalStatus.IDLE,
            "model": "model-x",
            "model_honored": True,
        }
        start_runtime(script=lambda command: {"terminal": _jsonable(local)})
        response = http.post("/runtimes/rt-1/terminals", json={"agent_profile": "developer"})
        assert response.status_code == 201, response.text
        row = database.get_terminal_metadata("beef0001")
        assert (row["model"], row["model_honored"]) == ("model-x", True)
        assert (response.json()["model"], response.json()["model_honored"]) == ("model-x", True)

    def test_a_runtime_that_reports_no_model_leaves_it_unknown(self, http, start_runtime):
        # A cao-bridge from before #856 sends neither field.
        start_runtime(script=lambda command: {"terminal": dict(LAUNCHED)})
        response = http.post("/runtimes/rt-1/terminals", json={"agent_profile": "developer"})
        assert response.status_code == 201, response.text
        row = database.get_terminal_metadata("beef0001")
        assert (row["model"], row["model_honored"]) == (None, None)

    def test_the_bridge_reports_the_engine_it_launched_with(self):
        from cli_agent_orchestrator.models.kiro_engine import KiroEngine
        from cli_agent_orchestrator.runtime_channel.bridge import _jsonable

        reported = _jsonable({**LAUNCHED, "provider": "kiro_cli", "engine": KiroEngine.KAS})
        assert reported["engine"] == "kas"


class TestLocalCapacity:
    def test_remote_terminals_do_not_count_against_the_local_cap(self, monkeypatch):
        from cli_agent_orchestrator.services import terminal_service as ts

        _remote_row("abcd0001", "rt-1")
        monkeypatch.setenv("CAO_MAX_TERMINALS", "1")
        # The cap check runs before anything touches tmux; stop right after it.
        sentinel = RuntimeError("past the cap check")

        def stop(*args, **kwargs):
            raise sentinel

        monkeypatch.setattr(ts, "generate_terminal_id", stop)
        with pytest.raises(RuntimeError) as exc:
            asyncio.run(ts.create_terminal(provider="mock_cli", agent_profile="developer"))
        assert exc.value is sentinel, f"refused by the cap: {exc.value}"


class TestUnrecordedCleanupPersists:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("failures", ["deferred", "failed"])
    async def test_cleanup_keeps_retrying_while_the_connection_is_up(self, monkeypatch, failures):
        registry = RuntimeRegistry()
        deletes = []

        async def send_text(text):
            command = decode(text)
            deletes.append(command)
            done = len(deletes) >= 9  # more attempts than any fixed cap
            if failures == "failed" and not done:
                reply = Result(op_id=command.op_id, ok=False, error="pane busy")
            else:
                reply = Result(op_id=command.op_id, ok=True, payload={"deleted": done})
            asyncio.get_running_loop().call_soon(conn.resolve, reply)

        conn = registry.register("rt-1", send_text)
        registry.activate(conn)
        monkeypatch.setattr(server_mod, "UNRECORDED_RETRY_DELAY", 0.001)
        server_mod._delete_unrecorded(conn, "beef0009")
        for _ in range(500):
            if len(deletes) >= 9 and not server_mod._teardowns:
                break
            await asyncio.sleep(0.01)
        assert len(deletes) == 9, "retried until the runtime confirmed the delete"
        assert not server_mod._teardowns

    @pytest.mark.asyncio
    async def test_cleanup_ends_with_its_connection(self, monkeypatch):
        registry = RuntimeRegistry()
        deletes = []

        async def send_text(text):
            command = decode(text)
            deletes.append(command)
            reply = Result(op_id=command.op_id, ok=True, payload={"deleted": False})
            asyncio.get_running_loop().call_soon(conn.resolve, reply)

        conn = registry.register("rt-1", send_text)
        registry.activate(conn)
        monkeypatch.setattr(server_mod, "UNRECORDED_RETRY_DELAY", 0.01)
        server_mod._delete_unrecorded(conn, "beef0009")
        await asyncio.sleep(0.05)
        registry.unregister("rt-1", conn)  # the next hello lists it again
        for _ in range(100):
            if not server_mod._teardowns:
                break
            await asyncio.sleep(0.01)
        assert not server_mod._teardowns, "the retries stop with the connection"


class TestRemoteApproval:
    @pytest.mark.asyncio
    async def test_the_approval_prompt_of_a_remote_terminal_is_read_from_its_runtime(
        self, monkeypatch
    ):
        from cli_agent_orchestrator.services.agui.approval_bridge import ApprovalBridge

        calls = []

        def get_output(terminal_id, *args, **kwargs):
            # The real remote path blocks on the channel loop: refuse to run there.
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                calls.append(terminal_id)
                return "Allow this command? (y/n)"
            raise RuntimeError("call_blocking used on the channel loop; await call() instead")

        monkeypatch.setattr(terminal_service, "get_output", get_output)
        from unittest.mock import MagicMock

        construct = MagicMock()
        bridge = ApprovalBridge(construct, get_provider_fn=lambda tid: "claude_code")
        await bridge._on_waiting("abcd1234")
        assert calls == ["abcd1234"], "the prompt must be read off the event loop"


class TestUndoRetry:
    def test_a_deferred_undo_is_retried_until_the_runtime_deletes(
        self, http, start_runtime, monkeypatch
    ):
        monkeypatch.setattr(server_mod, "UNRECORDED_RETRY_DELAY", 0.01)

        def fail(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(server_mod, "db_create_terminal", fail)
        deletes = []

        def script(command):
            if command.type == CommandType.LAUNCH:
                return {"terminal": dict(LAUNCHED)}
            deletes.append(command.terminal_id)
            return {"deleted": len(deletes) >= 3}  # deferred twice

        start_runtime(script=script)
        response = http.post("/runtimes/rt-1/terminals", json={"agent_profile": "developer"})
        assert response.status_code == 500
        assert "may still be running" in response.json()["detail"]
        _wait_for(lambda: len(deletes) >= 3, "the undo to be retried")
        assert deletes == ["beef0001"] * 3


class TestLaunchTimeout:
    def test_the_launch_deadline_is_configurable(self, http, server, monkeypatch):
        monkeypatch.setenv("CAO_RUNTIME_LAUNCH_TIMEOUT", "0.3")

        async def scenario():
            async with _dial(server) as ws:
                await ws.send(_hello())
                await ws.recv()
                started = time.monotonic()
                response = await asyncio.to_thread(
                    http.post, "/runtimes/rt-1/terminals", json={"agent_profile": "developer"}
                )
                return response, time.monotonic() - started

        response, elapsed = asyncio.run(scenario())
        assert response.status_code == 504, response.text
        assert elapsed < 5, f"took {elapsed:.1f}s: the configured deadline was not used"


class TestLaunchReadBack:
    def test_a_recorded_launch_is_reported_even_if_reading_it_back_fails(
        self, http, start_runtime, monkeypatch
    ):
        start_runtime(script=_answer)

        def flaky(terminal_id):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(terminal_service, "get_terminal", flaky)
        response = http.post("/runtimes/rt-1/terminals", json={"agent_profile": "developer"})
        assert response.status_code == 201, response.text
        body = response.json()
        assert (body["id"], body["session_name"], body["provider"]) == (
            "beef0001",
            "cao-beef",
            "mock_cli",
        )
        assert database.get_terminal_metadata("beef0001") is not None

    def test_the_fallback_answer_keeps_the_launch_model(self, http, start_runtime, monkeypatch):
        reported = {**LAUNCHED, "model": "model-x", "model_honored": False}
        start_runtime(script=lambda command: {"terminal": dict(reported)})

        def flaky(terminal_id):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(terminal_service, "get_terminal", flaky)
        response = http.post("/runtimes/rt-1/terminals", json={"agent_profile": "developer"})
        assert response.status_code == 201, response.text
        assert (response.json()["model"], response.json()["model_honored"]) == ("model-x", False)


class TestRunStepOnARemoteTerminal:
    def test_reusing_a_remote_terminal_in_run_step_is_refused(self, http, start_runtime):
        _remote_row("abcd1234", "rt-1")
        runtime = start_runtime(script=_answer)
        response = http.post(
            "/terminals/run-step",
            json={
                "provider": "mock_cli",
                "agent": "developer",
                "prompt": "hi",
                "reuse_terminal_id": "abcd1234",
                "timeout": 5,
            },
        )
        assert response.status_code == 409, response.text
        assert "execution runtime" in response.json()["detail"]
        assert runtime.received == [], "nothing is sent to the runtime"


class TestLocalOnlyRoutes:
    """Routes served from this server's own panes or logs refuse a remote terminal."""

    def test_an_inbox_message_to_a_remote_terminal_is_refused(self, http, start_runtime):
        _remote_row("abcd1234", "rt-1")
        runtime = start_runtime(script=_answer)
        response = http.post(
            "/terminals/abcd1234/inbox/messages",
            params={"sender_id": "abcd9999", "message": "hello"},
        )
        assert response.status_code == 409, response.text
        assert "execution runtime" in response.json()["detail"]
        assert database.get_inbox_messages("abcd1234") == [], "nothing is queued"
        assert runtime.received == [], "nothing is sent to the runtime"

    def test_no_caller_can_queue_a_message_for_a_remote_terminal(self):
        # The deferred-init failure notice queues straight into the database.
        _remote_row("abcd1234", "rt-1")
        with pytest.raises(LocalExecutionDisabledError):
            database.create_inbox_message("abcd9999", "abcd1234", "worker failed")
        assert database.get_inbox_messages("abcd1234") == []

    def test_a_local_terminal_still_gets_its_messages(self):
        database.create_terminal(
            "abcd5678", "cao-local1", "developer-abcd", "mock_cli", "developer"
        )
        queued = database.create_inbox_message("abcd9999", "abcd5678", "hello")
        assert (queued.receiver_id, queued.status.value) == ("abcd5678", "pending")

    def test_an_output_range_of_a_remote_terminal_is_refused(self, http, start_runtime):
        _remote_row("abcd1234", "rt-1")
        runtime = start_runtime(script=_answer)
        response = http.get("/terminals/abcd1234/output/range", params={"offset": 0, "length": 64})
        assert response.status_code == 409, response.text
        assert "execution runtime" in response.json()["detail"]
        assert runtime.received == [], "nothing is sent to the runtime"


class TestLaunchStatusOrder:
    def test_a_status_reported_after_the_launch_result_wins(self, http, server, monkeypatch):
        entered, release = threading.Event(), threading.Event()
        real = server_mod._record_launch

        def slow_record(*args, **kwargs):
            entered.set()
            release.wait(5)
            return real(*args, **kwargs)

        monkeypatch.setattr(server_mod, "_record_launch", slow_record)

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
                # The result says idle; the terminal is processing by the time
                # the bridge follows it with the current status.
                await ws.send(
                    encode(
                        Result(op_id=launch.op_id, ok=True, payload={"terminal": dict(LAUNCHED)})
                    )
                )
                await ws.send(encode(Status(terminal_id="beef0001", status="processing")))
                await asyncio.to_thread(entered.wait, 5)
                await asyncio.sleep(0.2)  # the status frame is handled meanwhile
                release.set()
                response = await asyncio.wait_for(request, 10)
                return response, http.get("/terminals/beef0001").json()["status"]

        response, status_now = asyncio.run(scenario())
        assert response.status_code == 201, response.text
        assert status_now == "processing", "the older status in the result must not win"


class TestUnsuccessfulSends:
    def _last_active(self, terminal_id):
        with database.SessionLocal() as db:
            return db.get(database.TerminalModel, terminal_id).last_active

    def test_an_input_the_runtime_did_not_deliver_changes_nothing(
        self, http, start_runtime, monkeypatch
    ):
        _remote_row("abcd1234", "rt-1")
        plugins = _RecordingPlugins()
        monkeypatch.setattr(app.state, "plugin_registry", plugins)
        before = self._last_active("abcd1234")
        start_runtime(script=lambda command: {"success": False})
        response = http.post(
            "/terminals/abcd1234/input",
            params={"message": "hi", "sender_id": "abcd9999", "orchestration_type": "send_message"},
        )
        assert response.status_code == 200 and response.json()["success"] is False
        time.sleep(0.2)
        assert self._last_active("abcd1234") == before
        assert [t for t, _ in plugins.events] == []

    def test_dispatch_input_refuses_a_remote_terminal(self, start_runtime):
        # The local dispatch path (#566) types into this server's tmux and
        # returns an output boundary of this server's monitor: neither applies
        # to a remote pane. Only send_input routes one, to its runtime.
        _remote_row("abcd1234", "rt-1")
        runtime = start_runtime(script=lambda command: {"success": True})
        with pytest.raises(LocalExecutionDisabledError):
            terminal_service.dispatch_input("abcd1234", "hi")
        assert runtime.received == [], "nothing is sent to the runtime"
        assert terminal_service.send_input("abcd1234", "hi") is True
        assert [c.type for c in runtime.received] == [CommandType.INPUT]

    def test_a_key_the_runtime_did_not_send_does_not_mark_the_terminal_active(
        self, http, start_runtime
    ):
        _remote_row("abcd1234", "rt-1")
        before = self._last_active("abcd1234")
        start_runtime(script=lambda command: {"success": False})
        response = http.post("/terminals/abcd1234/key", params={"key": "Enter"})
        assert response.status_code == 200 and response.json()["success"] is False
        assert self._last_active("abcd1234") == before


class TestUnrecordedCleanupErrors:
    @pytest.mark.asyncio
    async def test_an_unexpected_error_is_logged_not_left_on_the_task(self, caplog):
        registry = RuntimeRegistry()

        async def send_text(text):
            raise AssertionError("never reached")

        conn = registry.register("rt-1", send_text)
        registry.activate(conn)

        async def broken_call(*args, **kwargs):
            raise KeyError("unexpected")

        conn.call = broken_call
        server_mod._delete_unrecorded(conn, "beef0009")
        (task,) = list(server_mod._teardowns)
        await asyncio.wait_for(asyncio.shield(task), 5)
        assert task.exception() is None, "nothing is left for 'never retrieved'"
        assert "beef0009" in caplog.text

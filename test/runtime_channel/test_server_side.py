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
    backend_registry.set_backend(_NoLocalTmux())
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
                    await old.send(encode(Status(terminal_id="abcd1234", status="completed")))
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

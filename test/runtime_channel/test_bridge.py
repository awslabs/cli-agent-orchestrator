"""cao-bridge, the execution runtime's side of the channel (#745)."""

import asyncio
from types import SimpleNamespace

import pytest
import websockets
from websockets.datastructures import Headers
from websockets.http11 import Response

import cli_agent_orchestrator.runtime_channel.bridge as bridge_mod
from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.runtime_channel.bridge import Bridge, ChannelRefused
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
from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.event_bus import bus


class FakeServer:
    """One accepted channel, from the runtime's side."""

    def __init__(self, frames=(), version=PROTOCOL_VERSION):
        self.sent = []
        self._frames = [encode(f) for f in frames]
        self._hello = encode(Hello(protocol_version=version, runtime_id="server"))
        self.closed = asyncio.Event()

    async def send(self, text):
        self.sent.append(decode(text))

    async def recv(self):
        return self._hello

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for frame in self._frames:
            yield frame
        await self.closed.wait()

    async def close(self):
        self.closed.set()


def _bridge(tmp_path=None):
    ready = tmp_path / "ready" if tmp_path else None
    bridge = Bridge("ws://server/runtime/channel", "rt-1", "token", ready_file=ready)
    bridge._current_statuses = lambda: {"abcd1234": TerminalStatus.IDLE}
    return bridge


def _command(type_, terminal_id="abcd1234", **payload):
    return Command(op_id=f"op-{type_.value}", type=type_, terminal_id=terminal_id, payload=payload)


class TestExecute:
    @pytest.mark.asyncio
    async def test_launch_starts_a_local_terminal_and_reports_plain_fields(self, monkeypatch):
        calls = {}

        async def create_terminal(**kwargs):
            calls.update(kwargs)
            return SimpleNamespace(id="beef0001")

        monkeypatch.setattr(terminal_service, "create_terminal", create_terminal)
        monkeypatch.setattr(
            terminal_service,
            "get_terminal",
            lambda tid: {
                "id": tid,
                "name": "developer-beef",
                "provider": SimpleNamespace(value="mock_cli"),
                "session_name": "cao-beef",
                "agent_profile": "developer",
                "allowed_tools": ["@builtin"],
                "status": TerminalStatus.IDLE,
                "model": "model-x",
                "model_honored": True,
                "last_active": "not sent",
            },
        )
        result = await _bridge().execute(
            _command(CommandType.LAUNCH, None, agent_profile="developer", provider="mock_cli")
        )
        assert calls == {
            "provider": "mock_cli",
            "agent_profile": "developer",
            "new_session": True,
            "working_directory": None,
        }
        assert result == {
            "terminal": {
                "id": "beef0001",
                "name": "developer-beef",
                "provider": "mock_cli",
                "session_name": "cao-beef",
                "agent_profile": "developer",
                "allowed_tools": ["@builtin"],
                "status": "idle",
                "engine": None,  # set for kiro_cli launches (v2 or kas)
                # The launch model and whether the provider applies it (#856).
                "model": "model-x",
                "model_honored": True,
            }
        }

    @pytest.mark.asyncio
    async def test_launch_without_a_provider_uses_the_profiles_provider(self, monkeypatch):
        import cli_agent_orchestrator.utils.agent_profiles as agent_profiles

        seen = {}

        async def create_terminal(**kwargs):
            seen.update(kwargs)
            return SimpleNamespace(id="beef0001")

        monkeypatch.setattr(
            agent_profiles, "resolve_provider", lambda profile, default: "claude_code"
        )
        monkeypatch.setattr(terminal_service, "create_terminal", create_terminal)
        monkeypatch.setattr(terminal_service, "get_terminal", lambda tid: {"id": tid})
        await _bridge().execute(_command(CommandType.LAUNCH, None, agent_profile="developer"))
        assert seen["provider"] == "claude_code"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stopped", [True, False])
    async def test_a_launch_that_cannot_be_reported_stops_its_agent(self, monkeypatch, stopped):
        deleted = []

        async def create_terminal(**kwargs):
            return SimpleNamespace(id="beef0001")

        def get_terminal(tid):
            raise RuntimeError("database is locked")

        def delete_terminal(tid):
            deleted.append(tid)
            return stopped

        monkeypatch.setattr(terminal_service, "create_terminal", create_terminal)
        monkeypatch.setattr(terminal_service, "get_terminal", get_terminal)
        monkeypatch.setattr(terminal_service, "delete_terminal", delete_terminal)
        with pytest.raises(RuntimeError) as exc:
            await _bridge().execute(
                _command(CommandType.LAUNCH, None, agent_profile="developer", provider="mock_cli")
            )
        assert deleted == ["beef0001"]
        assert "beef0001" in str(exc.value)
        assert ("may still be running" in str(exc.value)) is (not stopped)

    @pytest.mark.asyncio
    async def test_input_keeps_the_sender_and_orchestration_type(self, monkeypatch):
        seen = {}

        def send_input(terminal_id, message, **kwargs):
            seen.update(terminal_id=terminal_id, message=message, **kwargs)
            return True

        monkeypatch.setattr(terminal_service, "send_input", send_input)
        result = await _bridge().execute(
            _command(
                CommandType.INPUT,
                message="do it",
                sender_id="sup00001",
                orchestration_type="assign",
                frozen_memory=None,
            )
        )
        assert result == {"success": True}
        assert seen == {
            "terminal_id": "abcd1234",
            "message": "do it",
            "sender_id": "sup00001",
            "orchestration_type": OrchestrationType.ASSIGN,
            "frozen_memory": None,
        }

    @pytest.mark.asyncio
    async def test_the_working_directory_is_read_beside_the_pane(self, monkeypatch):
        monkeypatch.setattr(terminal_service, "get_working_directory", lambda tid: f"/w/{tid}")
        command = Command(op_id="op-wd", type="working_directory", terminal_id="abcd1234")
        assert await _bridge().execute(command) == {"working_directory": "/w/abcd1234"}

    @pytest.mark.asyncio
    async def test_output_uses_the_requested_mode(self, monkeypatch):
        monkeypatch.setattr(terminal_service, "get_output", lambda tid, mode: f"{tid}:{mode.value}")
        result = await _bridge().execute(_command(CommandType.OUTPUT, mode="last"))
        assert result == {"output": "abcd1234:last"}

    @pytest.mark.asyncio
    async def test_deleting_a_terminal_this_runtime_does_not_have_succeeds(self, monkeypatch):
        import cli_agent_orchestrator.clients.database as database

        monkeypatch.setattr(database, "get_terminal_metadata", lambda tid: None)
        monkeypatch.setattr(
            terminal_service, "delete_terminal", lambda tid: pytest.fail("nothing to delete")
        )
        result = await _bridge().execute(_command(CommandType.DELETE))
        assert result == {"deleted": True, "absent": True}

    @pytest.mark.asyncio
    async def test_delete_reports_what_the_local_teardown_did(self, monkeypatch):
        import cli_agent_orchestrator.clients.database as database

        monkeypatch.setattr(database, "get_terminal_metadata", lambda tid: {"id": tid})
        monkeypatch.setattr(terminal_service, "delete_terminal", lambda tid: False)
        assert await _bridge().execute(_command(CommandType.DELETE)) == {"deleted": False}

    @pytest.mark.asyncio
    async def test_a_command_for_a_terminal_needs_its_id(self):
        with pytest.raises(ValueError):
            await _bridge().execute(_command(CommandType.KEY, None, key="Enter"))


class TestHandle:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("stopped", [True, False])
    async def test_a_launch_it_could_not_stop_is_named_in_its_failed_result(
        self, monkeypatch, stopped
    ):
        # The server learns of a terminal only from successful results: a
        # failure must name the one still running, or nothing would stop it.
        bridge = _bridge()
        server = FakeServer()
        bridge._ws = server

        async def create_terminal(**kwargs):
            return SimpleNamespace(id="beef0001")

        def get_terminal(tid):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(terminal_service, "create_terminal", create_terminal)
        monkeypatch.setattr(terminal_service, "get_terminal", get_terminal)
        monkeypatch.setattr(terminal_service, "delete_terminal", lambda tid: stopped)
        bridge._existing_terminal_ids = lambda: set()
        await bridge.handle(
            _command(CommandType.LAUNCH, None, agent_profile="developer", provider="mock_cli")
        )
        (result,) = server.sent
        assert result.ok is False and "beef0001" in result.error
        assert result.payload == ({} if stopped else {"terminal_id": "beef0001"})

    @pytest.mark.asyncio
    async def test_a_failure_is_reported_as_a_failed_result(self):
        bridge = _bridge()
        server = FakeServer()
        bridge._ws = server

        async def boom(command):
            raise RuntimeError("pane is gone")

        bridge.execute = boom
        await bridge.handle(_command(CommandType.OUTPUT))
        assert server.sent == [Result(op_id="op-output", ok=False, error="pane is gone")]

    @pytest.mark.asyncio
    async def test_one_terminals_commands_run_in_order_others_do_not_wait(self):
        bridge = _bridge()
        bridge._ws = FakeServer()
        release = asyncio.Event()
        order = []

        async def execute(command):
            order.append(("start", command.op_id))
            if command.op_id == "first":
                await release.wait()
            order.append(("end", command.op_id))
            return {}

        bridge.execute = execute
        first = Command(op_id="first", type=CommandType.INPUT, terminal_id="t1", payload={})
        second = Command(op_id="second", type=CommandType.KEY, terminal_id="t1", payload={})
        other = Command(op_id="other", type=CommandType.KEY, terminal_id="t2", payload={})
        tasks = [asyncio.ensure_future(bridge.handle(c)) for c in (first, second, other)]
        await asyncio.sleep(0.05)
        assert ("end", "other") in order, "another terminal must not wait"
        assert ("start", "second") not in order, "the same terminal must wait"
        release.set()
        await asyncio.gather(*tasks)
        assert order.index(("end", "first")) < order.index(("start", "second"))


class TestOrdering:
    @pytest.mark.asyncio
    async def test_a_launch_result_is_followed_by_the_terminals_current_status(self, monkeypatch):
        bridge = _bridge()
        server = FakeServer()
        bridge._ws = server

        async def create_terminal(**kwargs):
            return SimpleNamespace(id="beef0001")

        launched = {
            "id": "beef0001",
            "name": "developer-beef",
            "provider": "mock_cli",
            "session_name": "cao-beef",
            "agent_profile": "developer",
            "allowed_tools": None,
            "status": TerminalStatus.PROCESSING,  # when the result was built
        }
        monkeypatch.setattr(terminal_service, "create_terminal", create_terminal)
        monkeypatch.setattr(terminal_service, "get_terminal", lambda tid: dict(launched))
        # It finished meanwhile; a status sent before the result was refused.
        bridge._status_of = lambda tid: TerminalStatus.COMPLETED
        await bridge.handle(_command(CommandType.LAUNCH, None, agent_profile="developer"))
        assert isinstance(server.sent[0], Result) and server.sent[0].ok
        assert server.sent[1:] == [Status(terminal_id="beef0001", status=TerminalStatus.COMPLETED)]

    @pytest.mark.asyncio
    async def test_a_delete_of_an_older_terminal_does_not_wait_for_a_launch(self, monkeypatch):
        import cli_agent_orchestrator.clients.database as database

        bridge = _bridge()
        bridge._ws = FakeServer()
        bridge._local_terminal_ids = lambda: {"abcd0001"}  # existed before the launch
        initializing = asyncio.Event()
        events = []
        monkeypatch.setattr(database, "get_terminal_metadata", lambda tid: {"id": tid})

        async def create_terminal(**kwargs):
            events.append("launch started")
            await initializing.wait()
            events.append("launch done")
            return SimpleNamespace(id="beef0001")

        monkeypatch.setattr(terminal_service, "create_terminal", create_terminal)
        monkeypatch.setattr(terminal_service, "get_terminal", lambda tid: {"id": tid})
        monkeypatch.setattr(
            terminal_service, "delete_terminal", lambda tid: events.append(f"delete {tid}") or True
        )
        launch = asyncio.ensure_future(
            bridge.handle(_command(CommandType.LAUNCH, None, agent_profile="developer"))
        )
        await asyncio.sleep(0.05)
        await asyncio.wait_for(bridge.handle(_command(CommandType.DELETE, "abcd0001")), 2)
        assert events == ["launch started", "delete abcd0001"], "not held up by the launch"
        initializing.set()
        await asyncio.wait_for(launch, 5)

    @pytest.mark.asyncio
    async def test_a_delete_waits_for_a_launch_still_in_progress(self, monkeypatch):
        import cli_agent_orchestrator.clients.database as database

        bridge = _bridge()
        bridge._ws = FakeServer()
        initializing = asyncio.Event()
        events = []
        monkeypatch.setattr(database, "get_terminal_metadata", lambda tid: {"id": tid})

        async def create_terminal(**kwargs):
            events.append("launch started")
            await initializing.wait()  # the provider is still starting
            events.append("launch done")
            return SimpleNamespace(id="beef0001")

        def delete_terminal(terminal_id):
            events.append(f"delete {terminal_id}")
            return True

        monkeypatch.setattr(terminal_service, "create_terminal", create_terminal)
        monkeypatch.setattr(terminal_service, "get_terminal", lambda tid: {"id": tid})
        monkeypatch.setattr(terminal_service, "delete_terminal", delete_terminal)
        launch = asyncio.ensure_future(
            bridge.handle(_command(CommandType.LAUNCH, None, agent_profile="developer"))
        )
        await asyncio.sleep(0.05)
        # After a reconnect the server deletes the terminal it has no record of.
        delete = asyncio.ensure_future(bridge.handle(_command(CommandType.DELETE, "beef0001")))
        await asyncio.sleep(0.1)
        assert events == ["launch started"], "the delete must not run under the launch"
        initializing.set()
        await asyncio.wait_for(asyncio.gather(launch, delete), 5)
        assert events == ["launch started", "launch done", "delete beef0001"]

    @pytest.mark.asyncio
    async def test_a_command_after_a_deferred_delete_still_waits_its_turn(self, monkeypatch):
        import threading

        import cli_agent_orchestrator.clients.database as database

        bridge = _bridge()
        bridge._ws = FakeServer()
        input_started, release_input = threading.Event(), threading.Event()
        keys = []

        def send_input(terminal_id, message, **kwargs):
            input_started.set()
            release_input.wait(5)
            return True

        monkeypatch.setattr(database, "get_terminal_metadata", lambda tid: {"id": tid})
        # The runtime could not finish the teardown, so the terminal stays live.
        monkeypatch.setattr(terminal_service, "delete_terminal", lambda tid: False)
        monkeypatch.setattr(terminal_service, "send_input", send_input)
        monkeypatch.setattr(
            terminal_service, "send_special_key", lambda tid, key: keys.append(key) or True
        )

        delete = asyncio.ensure_future(bridge.handle(_command(CommandType.DELETE)))
        typed = asyncio.ensure_future(bridge.handle(_command(CommandType.INPUT, message="hi")))
        await asyncio.wait_for(asyncio.to_thread(input_started.wait, 5), 6)
        key = asyncio.ensure_future(bridge.handle(_command(CommandType.KEY, key="Enter")))
        await asyncio.sleep(0.1)
        assert keys == [], "the key must wait for the input queued ahead of it"
        release_input.set()
        await asyncio.wait_for(asyncio.gather(delete, typed, key), 5)
        assert keys == ["Enter"]
        assert bridge._terminal_locks == {}, "a lock nobody holds or awaits is dropped"


class TestConnection:
    @pytest.mark.asyncio
    async def test_one_failed_status_push_does_not_stop_the_forwarder(self):
        bridge = _bridge()
        server = FakeServer()
        bridge._ws = server
        calls = []

        def status_of(terminal_id):
            calls.append(terminal_id)
            if len(calls) == 1:
                raise RuntimeError("status monitor hiccup")
            return TerminalStatus.COMPLETED

        bridge._status_of = status_of
        previous = bus._loop
        bus.set_loop(asyncio.get_running_loop())
        forwarding = asyncio.ensure_future(bridge.forward_status())
        try:
            await asyncio.sleep(0)
            bus.publish("terminal.abcd1234.status", {"status": "processing"})
            bus.publish("terminal.abcd1234.status", {"status": "completed"})
            for _ in range(100):
                if server.sent:
                    break
                await asyncio.sleep(0.01)
            assert not forwarding.done(), "the forwarder died on one bad status"
            assert server.sent == [Status(terminal_id="abcd1234", status=TerminalStatus.COMPLETED)]
        finally:
            forwarding.cancel()
            bus.set_loop(previous)

    @pytest.mark.asyncio
    async def test_a_slow_status_read_does_not_stall_the_event_loop(self):
        import time

        bridge = _bridge()
        bridge._ws = FakeServer()

        def slow(terminal_id):
            time.sleep(0.3)  # e.g. a capture-pane behind the status monitor
            return TerminalStatus.IDLE

        bridge._status_of = slow
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.01)

        ticking = asyncio.ensure_future(ticker())
        await bridge._push_status("abcd1234")
        ticking.cancel()
        assert ticks >= 10, f"the loop ran {ticks} times in 0.3 s: the read blocked it"
        assert bridge._ws.sent == [Status(terminal_id="abcd1234", status=TerminalStatus.IDLE)]

    @pytest.mark.asyncio
    async def test_hello_reports_status_then_commands_are_answered(self, tmp_path):
        bridge = _bridge(tmp_path)

        async def execute(command):
            return {"output": "hi"}

        bridge.execute = execute
        server = FakeServer(frames=[_command(CommandType.OUTPUT, mode="full")])
        serving = asyncio.ensure_future(bridge.serve(server))
        for _ in range(100):
            if any(isinstance(frame, Result) for frame in server.sent):
                break
            await asyncio.sleep(0.01)
        hello = server.sent[0]
        (result,) = [frame for frame in server.sent if isinstance(frame, Result)]
        assert hello == Hello(
            protocol_version=PROTOCOL_VERSION,
            runtime_id="rt-1",
            statuses={"abcd1234": TerminalStatus.IDLE},
        )
        assert result == Result(op_id="op-output", ok=True, payload={"output": "hi"})
        assert (tmp_path / "ready").exists(), "ready while the channel is up"
        await server.close()
        await serving
        assert not (tmp_path / "ready").exists()

    def test_hello_lists_every_local_terminal_even_before_its_status_is_known(self, monkeypatch):
        import cli_agent_orchestrator.clients.database as database
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        monkeypatch.setattr(
            database, "list_all_terminals", lambda: [{"id": "aaaa0001"}, {"id": "aaaa0002"}]
        )
        monkeypatch.setattr(
            status_monitor,
            "get_status",
            lambda tid: TerminalStatus.IDLE if tid == "aaaa0001" else TerminalStatus.UNKNOWN,
        )
        bridge = Bridge("ws://server/runtime/channel", "rt-1", "token")
        assert bridge._current_statuses() == {
            "aaaa0001": TerminalStatus.IDLE,
            "aaaa0002": TerminalStatus.UNKNOWN,
        }

    @pytest.mark.asyncio
    async def test_a_status_that_changes_during_the_hello_is_sent_once_connected(self, monkeypatch):
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        bridge = _bridge()  # its hello reports abcd1234 as idle
        # By the time the server's hello arrives, the terminal is processing.
        monkeypatch.setattr(status_monitor, "get_status", lambda tid: TerminalStatus.PROCESSING)
        server = FakeServer()
        serving = asyncio.ensure_future(bridge.serve(server))
        for _ in range(100):
            if len(server.sent) >= 2:
                break
            await asyncio.sleep(0.01)
        await server.close()
        await serving
        assert server.sent[0].statuses == {"abcd1234": TerminalStatus.IDLE}
        assert server.sent[-1] == Status(terminal_id="abcd1234", status=TerminalStatus.PROCESSING)

    @pytest.mark.asyncio
    async def test_a_result_that_could_not_be_sent_is_delivered_after_the_next_hello(self):
        bridge = _bridge()
        # A launch finished while the channel was down: its result had nowhere to go.
        late = Result(op_id="op-launch", ok=True, payload={"terminal": {"id": "beef0001"}})
        await bridge._send(late)
        server = FakeServer()
        serving = asyncio.ensure_future(bridge.serve(server))
        for _ in range(100):
            if late in server.sent:
                break
            await asyncio.sleep(0.01)
        await server.close()
        await serving
        assert isinstance(server.sent[0], Hello)
        assert late in server.sent

    @pytest.mark.asyncio
    async def test_a_failure_right_after_the_hello_leaves_the_runtime_not_ready(self, tmp_path):
        bridge = _bridge(tmp_path)

        def broken(terminal_id):
            raise RuntimeError("status monitor unavailable")

        # The status replay after the hello fails with something other than a
        # closed connection.
        bridge._status_of = broken
        with pytest.raises(RuntimeError):
            await bridge.serve(FakeServer())
        assert bridge._ws is None
        assert not (tmp_path / "ready").exists(), "Ready must not outlive the connection"

    @pytest.mark.asyncio
    async def test_a_server_that_drops_right_after_the_hello_gets_growing_backoff(
        self, monkeypatch
    ):
        attempts = []

        class DropsAfterHello(FakeServer):
            async def __aenter__(self):
                attempts.append(asyncio.get_running_loop().time())
                return self

            async def __aexit__(self, *exc):
                return False

            async def _iterate(self):
                raise websockets.exceptions.ConnectionClosedError(None, None)
                yield  # pragma: no cover - makes this an async generator

        monkeypatch.setattr(bridge_mod, "connect", lambda *args, **kwargs: DropsAfterHello())
        monkeypatch.setattr(bridge_mod, "BACKOFF_INITIAL", 0.05)
        bridge = _bridge()
        running = asyncio.ensure_future(bridge.run())
        await asyncio.sleep(0.7)
        bridge.stop()
        await asyncio.wait_for(running, 5)
        assert len(attempts) <= 5, f"{len(attempts)} attempts: a hello alone reset the backoff"

    @pytest.mark.asyncio
    async def test_a_server_that_drops_every_hello_gets_growing_backoff(self, monkeypatch):
        attempts = []

        class DropsBeforeHello:
            async def __aenter__(self):
                attempts.append(asyncio.get_running_loop().time())
                return self

            async def __aexit__(self, *exc):
                return False

            async def send(self, text):
                pass

            async def recv(self):
                raise websockets.exceptions.ConnectionClosedError(None, None)

        monkeypatch.setattr(bridge_mod, "connect", lambda *args, **kwargs: DropsBeforeHello())
        monkeypatch.setattr(bridge_mod, "BACKOFF_INITIAL", 0.05)
        bridge = _bridge()
        running = asyncio.ensure_future(bridge.run())
        await asyncio.sleep(0.7)
        bridge.stop()
        await asyncio.wait_for(running, 5)
        # Doubling from 0.05 s allows ~4 attempts in 0.7 s; a reset after every
        # accepted upgrade would retry every 0.05 s (~14 attempts).
        assert len(attempts) <= 5, f"{len(attempts)} attempts: the backoff never grew"

    @pytest.mark.asyncio
    async def test_a_server_speaking_another_version_is_fatal(self, tmp_path):
        with pytest.raises(ChannelRefused):
            await _bridge(tmp_path).serve(FakeServer(version=PROTOCOL_VERSION + 1))
        assert not (tmp_path / "ready").exists()

    @pytest.mark.asyncio
    async def test_a_refused_token_is_fatal_not_retried(self, monkeypatch):
        attempts = []

        def connect(url, **kwargs):
            attempts.append(kwargs["additional_headers"])
            raise websockets.exceptions.InvalidStatus(Response(403, "Forbidden", Headers()))

        monkeypatch.setattr(bridge_mod, "connect", connect)
        with pytest.raises(ChannelRefused):
            await asyncio.wait_for(_bridge().run(), timeout=5)
        assert attempts == [{"x-cao-runtime-token": "token"}]

    @pytest.mark.asyncio
    async def test_a_network_error_is_retried_with_backoff(self, monkeypatch):
        monkeypatch.setattr(bridge_mod, "BACKOFF_INITIAL", 0.01)
        bridge = _bridge()
        attempts = []

        def connect(url, **kwargs):
            attempts.append(url)
            if len(attempts) == 3:
                bridge.stop()
            raise OSError("connection refused")

        monkeypatch.setattr(bridge_mod, "connect", connect)
        await asyncio.wait_for(bridge.run(), timeout=5)
        assert len(attempts) == 3


class TestStatusForwarding:
    @pytest.mark.asyncio
    async def test_every_local_status_change_is_pushed(self, monkeypatch):
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        monkeypatch.setattr(status_monitor, "get_status", lambda tid: TerminalStatus.COMPLETED)
        bridge = _bridge()
        server = FakeServer()
        bridge._ws = server
        previous = bus._loop
        bus.set_loop(asyncio.get_running_loop())
        forwarding = asyncio.ensure_future(bridge.forward_status())
        try:
            await asyncio.sleep(0)
            bus.publish("terminal.abcd1234.status", {"status": "completed"})
            bus.publish("terminal.not-a-terminal.output", {"status": "idle"})
            for _ in range(100):
                if server.sent:
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            assert server.sent == [Status(terminal_id="abcd1234", status=TerminalStatus.COMPLETED)]
        finally:
            forwarding.cancel()
            await asyncio.gather(forwarding, return_exceptions=True)
            bus.set_loop(previous)

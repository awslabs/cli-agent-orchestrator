"""Runtime channel building blocks: frames, the shared token, and the registry."""

import asyncio
import threading

import pytest
from pydantic import ValidationError

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.runtime_channel import token as token_mod
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
from cli_agent_orchestrator.runtime_channel.registry import (
    RemoteCommandError,
    RemoteOutcomeUnknownError,
    RuntimeRegistry,
    RuntimeUnavailableError,
)


class TestFrames:
    @pytest.mark.parametrize(
        "frame",
        [
            Hello(
                protocol_version=PROTOCOL_VERSION, runtime_id="rt-1", statuses={"abcd1234": "idle"}
            ),
            Command(op_id="op1", type=CommandType.INPUT, terminal_id="abcd1234", payload={"m": 1}),
            Result(op_id="op1", ok=False, error="boom"),
            Status(terminal_id="abcd1234", status=TerminalStatus.COMPLETED),
        ],
    )
    def test_every_frame_round_trips(self, frame):
        assert decode(encode(frame)) == frame

    def test_an_unknown_frame_is_rejected(self):
        with pytest.raises(ValidationError):
            decode('{"kind": "shell", "cmd": "rm -rf /"}')


class TestToken:
    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch):
        monkeypatch.delenv(token_mod.TOKEN_ENV, raising=False)
        monkeypatch.delenv(token_mod.TOKEN_FILE_ENV, raising=False)
        token_mod._reset_for_tests()
        yield
        token_mod._reset_for_tests()

    def test_the_file_wins_and_the_env_value_is_removed(self, monkeypatch, tmp_path):
        path = tmp_path / "token"
        path.write_text("from-file\n")
        monkeypatch.setenv(token_mod.TOKEN_FILE_ENV, str(path))
        monkeypatch.setenv(token_mod.TOKEN_ENV, "from-env")
        assert token_mod.runtime_token() == "from-file"
        import os

        assert token_mod.TOKEN_ENV not in os.environ
        # Nor does a later child learn where the token file is.
        assert token_mod.TOKEN_FILE_ENV not in os.environ

    def test_the_env_value_alone_works_and_is_removed(self, monkeypatch):
        monkeypatch.setenv(token_mod.TOKEN_ENV, "from-env")
        assert token_mod.runtime_token() == "from-env"
        import os

        assert token_mod.TOKEN_ENV not in os.environ

    def test_nothing_configured_is_none(self):
        assert token_mod.runtime_token() is None

    def test_an_unreadable_file_is_none(self, monkeypatch, tmp_path):
        monkeypatch.setenv(token_mod.TOKEN_FILE_ENV, str(tmp_path / "missing"))
        assert token_mod.runtime_token() is None

    @pytest.mark.parametrize("content", [None, "\n"])
    def test_a_configured_file_that_gives_no_token_does_not_fall_back(
        self, monkeypatch, tmp_path, content
    ):
        # Fail closed, as docs/execution-runtimes.md states: the env value is
        # not a fallback for a token file that is missing or empty.
        path = tmp_path / "token"
        if content is not None:
            path.write_text(content)
        monkeypatch.setenv(token_mod.TOKEN_FILE_ENV, str(path))
        monkeypatch.setenv(token_mod.TOKEN_ENV, "from-env")
        assert token_mod.runtime_token() is None


class _FakeRuntime:
    """Answers every command it is sent, the way a bridge would."""

    def __init__(self, registry, runtime_id="rt-1", reply=None):
        self.sent = []
        self.reply = reply
        self.conn = registry.register(runtime_id, self.send_text)
        registry.activate(self.conn)  # as the server does once its hello is sent

    async def send_text(self, text):
        command = decode(text)
        self.sent.append(command)
        if self.reply is not None:
            result = self.reply(command)
            asyncio.get_running_loop().call_soon(self.conn.resolve, result)


@pytest.mark.asyncio
async def test_a_result_is_matched_to_its_command_by_op_id():
    registry = RuntimeRegistry()
    runtime = _FakeRuntime(
        registry,
        reply=lambda c: Result(op_id=c.op_id, ok=True, payload={"echo": c.payload["message"]}),
    )
    first, second = await asyncio.gather(
        registry.call("rt-1", CommandType.INPUT, {"message": "a"}, terminal_id="t1"),
        registry.call("rt-1", CommandType.INPUT, {"message": "b"}, terminal_id="t2"),
    )
    assert (first, second) == ({"echo": "a"}, {"echo": "b"})
    assert len({c.op_id for c in runtime.sent}) == 2


@pytest.mark.asyncio
async def test_a_runtime_that_is_not_connected_is_503_and_nothing_is_sent():
    registry = RuntimeRegistry()
    with pytest.raises(RuntimeUnavailableError) as exc:
        await registry.call("missing", CommandType.KEY, {"key": "C-c"}, terminal_id="t1")
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_a_missing_result_is_an_unknown_outcome_not_a_retry():
    registry = RuntimeRegistry()
    runtime = _FakeRuntime(registry, reply=None)
    with pytest.raises(RemoteOutcomeUnknownError) as exc:
        await registry.call("rt-1", CommandType.INPUT, {"message": "a"}, timeout=0.05)
    assert exc.value.status_code == 504
    assert len(runtime.sent) == 1, "the command must not be resent"


@pytest.mark.asyncio
async def test_a_disconnect_fails_waiting_calls_as_unknown():
    registry = RuntimeRegistry()
    runtime = _FakeRuntime(registry, reply=None)
    call = asyncio.ensure_future(registry.call("rt-1", CommandType.OUTPUT, {}, terminal_id="t1"))
    for _ in range(100):  # until the command has been written and awaits its result
        if runtime.sent:
            break
        await asyncio.sleep(0)
    registry.unregister("rt-1", runtime.conn)
    with pytest.raises(RemoteOutcomeUnknownError):
        await call


@pytest.mark.asyncio
async def test_a_call_that_cannot_get_the_channel_in_time_is_503_and_never_written():
    registry = RuntimeRegistry()
    written = []

    async def stalled(text):
        written.append(decode(text))
        await asyncio.Event().wait()  # backpressure: the runtime stopped reading

    conn = registry.register("rt-1", stalled)
    registry.activate(conn)
    first = asyncio.ensure_future(conn.call(CommandType.INPUT, {"message": "a"}, "t1", timeout=2))
    await asyncio.sleep(0.05)
    started = asyncio.get_running_loop().time()
    with pytest.raises(RuntimeUnavailableError) as exc:
        await conn.call(CommandType.KEY, {"key": "Enter"}, "t1", timeout=0.2)
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 1.0, f"the timeout covers the wait for the channel ({elapsed:.2f}s)"
    assert exc.value.status_code == 503, "never written, so safe to retry"
    assert [c.type for c in written] == [CommandType.INPUT]
    first.cancel()


@pytest.mark.asyncio
async def test_a_send_the_runtime_never_reads_times_out_and_frees_the_channel():
    registry = RuntimeRegistry()

    async def stalled(text):
        await asyncio.Event().wait()  # backpressure: the runtime stopped reading

    conn = registry.register("rt-1", stalled)
    registry.activate(conn)
    first = conn.call(CommandType.INPUT, {"message": "a"}, "t1", timeout=0.2)
    with pytest.raises(RemoteOutcomeUnknownError):
        await asyncio.wait_for(first, 2)
    # The send lock is free again: the next command gets its own bounded try.
    second = conn.call(CommandType.KEY, {"key": "Enter"}, "t1", timeout=0.2)
    with pytest.raises(RemoteOutcomeUnknownError):
        await asyncio.wait_for(second, 2)


@pytest.mark.asyncio
async def test_a_call_queued_behind_a_send_is_never_written_once_replaced():
    registry = RuntimeRegistry()
    written, release = [], asyncio.Event()

    async def slow_send(text):
        written.append(decode(text))
        await release.wait()  # the first send holds the send lock

    old = registry.register("rt-1", slow_send)
    registry.activate(old)
    first = asyncio.ensure_future(old.call(CommandType.INPUT, {"message": "a"}, "t1", timeout=5))
    queued = asyncio.ensure_future(old.call(CommandType.KEY, {"key": "Enter"}, "t1", timeout=5))
    await asyncio.sleep(0.05)
    # A newer connection from the same runtime replaces (and closes) this one
    # while the second call still waits for the send lock.
    _FakeRuntime(registry)
    release.set()
    with pytest.raises(RemoteOutcomeUnknownError):
        await first  # it was written, so its outcome is unknown
    with pytest.raises(RuntimeUnavailableError) as exc:
        await queued
    assert exc.value.status_code == 503, "never written, so safe to retry"
    assert [c.type for c in written] == [CommandType.INPUT]


@pytest.mark.asyncio
async def test_a_result_for_a_call_that_already_gave_up_is_unclaimed():
    registry = RuntimeRegistry()
    runtime = _FakeRuntime(registry, reply=None)
    call = asyncio.ensure_future(registry.call("rt-1", CommandType.LAUNCH, {}, timeout=10))
    for _ in range(100):  # until the command has been written
        if runtime.sent:
            break
        await asyncio.sleep(0)
    conn = registry.connection("rt-1")
    (waiting,) = conn._pending.values()
    # The caller gave up (timeout or cancellation) but has not cleaned up yet.
    waiting.cancel()
    late = Result(op_id=runtime.sent[0].op_id, ok=True, payload={"terminal": {"id": "beef0001"}})
    assert conn.resolve(late) is False, "nobody will act on it, so the caller must"
    with pytest.raises(asyncio.CancelledError):
        await call


@pytest.mark.asyncio
async def test_a_reported_failure_is_502():
    registry = RuntimeRegistry()
    _FakeRuntime(registry, reply=lambda c: Result(op_id=c.op_id, ok=False, error="no pane"))
    with pytest.raises(RemoteCommandError) as exc:
        await registry.call("rt-1", CommandType.OUTPUT, {}, terminal_id="t1")
    assert exc.value.status_code == 502
    assert "no pane" in str(exc.value)


@pytest.mark.asyncio
async def test_only_the_runtime_a_terminal_is_placed_on_may_report_its_status():
    registry = RuntimeRegistry()
    _FakeRuntime(registry, "rt-1")
    _FakeRuntime(registry, "rt-2")
    registry.place("t1", "rt-1")

    assert registry.set_status("t1", "rt-2", TerminalStatus.COMPLETED) is False
    assert registry.get_status("t1", "rt-1") == TerminalStatus.UNKNOWN
    assert registry.set_status("t1", "rt-1", TerminalStatus.PROCESSING) is True
    assert registry.get_status("t1", "rt-1") == TerminalStatus.PROCESSING


@pytest.mark.asyncio
async def test_status_is_unknown_until_the_hello_exchange_is_over():
    registry = RuntimeRegistry()
    conn = registry.register("rt-1", lambda text: asyncio.sleep(0))
    registry.place("t1", "rt-1")
    # The server records the runtime's hello statuses before sending its own hello.
    assert registry.set_status("t1", "rt-1", TerminalStatus.PROCESSING, conn=conn) is True
    assert registry.get_status("t1", "rt-1") == TerminalStatus.UNKNOWN
    registry.activate(conn)
    assert registry.get_status("t1", "rt-1") == TerminalStatus.PROCESSING


@pytest.mark.asyncio
async def test_status_is_unknown_while_the_runtime_is_disconnected():
    registry = RuntimeRegistry()
    runtime = _FakeRuntime(registry)
    registry.place("t1", "rt-1")
    registry.set_status("t1", "rt-1", TerminalStatus.COMPLETED)
    registry.unregister("rt-1", runtime.conn)
    assert registry.get_status("t1", "rt-1") == TerminalStatus.UNKNOWN


@pytest.mark.asyncio
async def test_a_new_connection_starts_with_no_status_from_the_old_one():
    registry = RuntimeRegistry()
    first = _FakeRuntime(registry)
    registry.place("t1", "rt-1")
    registry.set_status("t1", "rt-1", TerminalStatus.COMPLETED)
    registry.unregister("rt-1", first.conn)
    _FakeRuntime(registry)
    assert registry.get_status("t1", "rt-1") == TerminalStatus.UNKNOWN


@pytest.mark.asyncio
async def test_a_worker_thread_can_call_through_the_loop():
    registry = RuntimeRegistry()
    _FakeRuntime(registry, reply=lambda c: Result(op_id=c.op_id, ok=True, payload={"ok": 1}))
    out = {}

    def worker():
        out["result"] = registry.call_blocking("rt-1", CommandType.KEY, {"key": "C-c"}, "t1")

    thread = threading.Thread(target=worker)
    thread.start()
    while thread.is_alive():
        await asyncio.sleep(0.01)
    assert out["result"] == {"ok": 1}


@pytest.mark.asyncio
async def test_a_blocking_call_on_the_loop_itself_is_refused():
    registry = RuntimeRegistry()
    _FakeRuntime(registry)
    with pytest.raises(RuntimeError):
        registry.call_blocking("rt-1", CommandType.KEY, {"key": "C-c"}, "t1")


@pytest.mark.asyncio
async def test_a_losing_launchs_status_cannot_touch_the_winners():
    registry = RuntimeRegistry()
    winner, loser = _FakeRuntime(registry, "rt-2"), _FakeRuntime(registry, "rt-1")
    registry.place("beef0001", "rt-2")
    assert registry.set_status("beef0001", "rt-2", TerminalStatus.IDLE, conn=winner.conn)
    # rt-1 launched the same id; its launch is being recorded (and will lose).
    registry.reserve("beef0001", "rt-1")
    registry.set_status("beef0001", "rt-1", TerminalStatus.PROCESSING, conn=loser.conn)
    registry.release("beef0001", "rt-1")
    assert registry.get_status("beef0001", "rt-2") == TerminalStatus.IDLE


@pytest.mark.parametrize("runtime_id", ["", "a/b", "rt 1", "../x", "x" * 200])
def test_a_runtime_id_must_be_one_url_path_segment(runtime_id):
    with pytest.raises(ValidationError):
        Hello(protocol_version=PROTOCOL_VERSION, runtime_id=runtime_id)


@pytest.mark.parametrize("runtime_id", ["cao-runtime-0", "rt-1", "server", "a.b_c-9"])
def test_pod_names_are_valid_runtime_ids(runtime_id):
    assert Hello(protocol_version=PROTOCOL_VERSION, runtime_id=runtime_id).runtime_id == runtime_id


def test_cao_bridge_refuses_an_unaddressable_runtime_id(monkeypatch):
    from cli_agent_orchestrator.runtime_channel import bridge as bridge_mod

    monkeypatch.setenv("CAO_BRIDGE_SERVER_URL", "ws://server/runtime/channel")
    monkeypatch.setenv("CAO_BRIDGE_RUNTIME_ID", "a/b")
    monkeypatch.setenv(token_mod.TOKEN_ENV, "token")
    token_mod._reset_for_tests()
    try:
        with pytest.raises(SystemExit) as exc:
            asyncio.run(bridge_mod._amain())
        assert "CAO_BRIDGE_RUNTIME_ID" in str(exc.value)
    finally:
        token_mod._reset_for_tests()


@pytest.mark.parametrize("url", ["http://cao-server:9889/runtime/channel", "cao-server:9889"])
def test_cao_bridge_refuses_a_server_url_it_could_never_dial(monkeypatch, url):
    # Retrying could not help, as with a refused token: exit at once, not loop.
    import cli_agent_orchestrator.clients.database as database
    from cli_agent_orchestrator.runtime_channel import bridge as bridge_mod

    def must_not_start(*args, **kwargs):
        raise AssertionError("cao-bridge started with a URL it can never dial")

    monkeypatch.setattr(database, "init_db", must_not_start)
    monkeypatch.setattr(bridge_mod.Bridge, "run", must_not_start)
    monkeypatch.setenv("CAO_BRIDGE_SERVER_URL", url)
    monkeypatch.setenv("CAO_BRIDGE_RUNTIME_ID", "rt-1")
    monkeypatch.setenv(token_mod.TOKEN_ENV, "token")
    token_mod._reset_for_tests()
    try:
        with pytest.raises(SystemExit) as exc:
            asyncio.run(bridge_mod._amain())
        assert "CAO_BRIDGE_SERVER_URL" in str(exc.value)
        assert "ws://" in str(exc.value)
    finally:
        token_mod._reset_for_tests()

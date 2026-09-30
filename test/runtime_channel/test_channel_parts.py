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
    await asyncio.sleep(0)
    registry.unregister("rt-1", runtime.conn)
    with pytest.raises(RemoteOutcomeUnknownError):
        await call


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

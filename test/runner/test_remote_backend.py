"""RemoteBackend + RunnerHub + Runner over a real WebSocket (#745).

The hub and the runner are the real implementations talking over a loopback
socket. Only the runner's local terminal layer is faked (an in-memory stand-in
for TmuxBackend), so routing, placement, staging, output forwarding and the
failure semantics are exercised end to end without tmux.
"""

import asyncio
import os
import select
import threading
import time
from typing import Dict, List, Optional

import pytest

from cli_agent_orchestrator.backends.base import (
    TerminalBackendError,
    TerminalCleanupOutcome,
    TerminalCleanupResult,
)
from cli_agent_orchestrator.backends.remote_backend import RemoteBackend
from cli_agent_orchestrator.runner.agent import Runner
from cli_agent_orchestrator.runner.hub import RunnerHub
from cli_agent_orchestrator.runner.protocol import RunnerOutcomeUnknown, RunnerUnavailable

TOKEN = "t" * 48


class FakeLocalBackend:
    """In-memory stand-in for the runner's TmuxBackend."""

    def __init__(self) -> None:
        self.windows: Dict[str, Dict[str, str]] = {}  # session -> {window: terminal_id}
        self.keys: List[tuple] = []
        self.piped: Dict[tuple, str] = {}
        self.block_send_keys = threading.Event()

    def create_session(self, session, window, terminal_id, working_directory=None, extra_env=None):
        if session in self.windows:
            raise ValueError(f"Session '{session}' already exists")
        self.windows[session] = {window: terminal_id}
        return window

    def create_window(
        self,
        session,
        window,
        terminal_id,
        working_directory=None,
        window_shell=None,
        extra_env=None,
    ):
        if session not in self.windows:
            raise ValueError(f"Session '{session}' not found")
        self.windows[session][window] = terminal_id
        return window

    def session_exists(self, session):
        return session in self.windows

    def session_exists_strict(self, session):
        return session in self.windows

    def list_sessions(self):
        return [{"id": s, "name": s, "status": "active"} for s in self.windows]

    def kill_session(self, session):
        return self.windows.pop(session, None) is not None

    def kill_window(self, session, window):
        removed = self.windows.get(session, {}).pop(window, None) is not None
        if session in self.windows and not self.windows[session]:
            del self.windows[session]
        return removed

    def cleanup_terminal_exact(self, terminal_id, session=None, window=None, *, close=True):
        for s, wins in list(self.windows.items()):
            for w, tid in list(wins.items()):
                if tid == terminal_id:
                    if not close:
                        return TerminalCleanupResult(TerminalCleanupOutcome.STILL_PRESENT)
                    self.kill_window(s, w)
                    return TerminalCleanupResult(TerminalCleanupOutcome.DELETED)
        return TerminalCleanupResult(TerminalCleanupOutcome.ABSENT)

    def send_keys(self, session, window, keys, **kwargs):
        if window not in self.windows.get(session, {}):
            raise ValueError(f"Window '{window}' not found")
        if self.block_send_keys.is_set():
            time.sleep(5)
        self.keys.append((session, window, keys))

    def send_special_key(self, session, window, key):
        self.keys.append((session, window, f"<{key}>"))

    def get_history(self, session, window, **kwargs):
        return f"history of {session}:{window}"

    def get_pane_working_directory(self, session, window):
        return "/workspace"

    def get_pane_current_command(self, session, window):
        return "bash"

    def pipe_pane(self, session, window, path):
        self.piped[(session, window)] = path

    def stop_pipe_pane(self, session, window):
        self.piped.pop((session, window), None)


class RunnerThread:
    """A real Runner on its own event loop thread."""

    def __init__(
        self,
        url: str,
        runner_id: str,
        tmp_dir: str,
        token: str = TOKEN,
        hosting: Optional[Dict[tuple, str]] = None,
        **kwargs,
    ) -> None:
        self.local = FakeLocalBackend()
        self.runner = Runner(url, token, runner_id, backend=self.local, tmp_dir=tmp_dir, **kwargs)
        # Terminals already running when the runner starts: in its first hello.
        for (session, window), terminal_id in (hosting or {}).items():
            self.runner._record(session, window, terminal_id)
            self.local.windows.setdefault(session, {})[window] = terminal_id
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.task = self.loop.create_task(self.runner.run_forever())
        try:
            self.loop.run_until_complete(self.task)
        except BaseException:
            pass

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.task.cancel)
        self.thread.join(timeout=5)


def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


@pytest.fixture
def hub():
    h = RunnerHub(token=TOKEN, host="127.0.0.1", port=0, call_timeout=5, placement_timeout=1)
    h.start()
    yield h
    h.stop()


@pytest.fixture
def server_tmp(tmp_path):
    d = tmp_path / "server-tmp"
    d.mkdir()
    return d


@pytest.fixture
def backend(hub, server_tmp):
    b = RemoteBackend(hub=hub)
    b._tmp_dir = str(server_tmp)
    return b


@pytest.fixture
def start_runner(hub, tmp_path):
    runners: List[RunnerThread] = []

    def _start(runner_id: str, **kwargs) -> RunnerThread:
        tmp_dir = tmp_path / f"{runner_id}-tmp"
        r = RunnerThread(f"ws://127.0.0.1:{hub.port}", runner_id, str(tmp_dir), **kwargs)
        runners.append(r)
        wait_for(lambda: runner_id in hub.connected_runners())
        return r

    yield _start
    for r in runners:
        r.stop()


class TestPlacement:
    def test_claimed_session_goes_to_its_claimant(self, backend, start_runner):
        pool = start_runner("pool-1")
        sup = start_runner("sup", claims=["cao-supervisor"], accepts_unclaimed=False)
        assert backend.create_session("cao-supervisor", "w", "aaaaaaaa") == "w"
        assert "cao-supervisor" in sup.local.windows
        assert "cao-supervisor" not in pool.local.windows

    def test_window_spills_to_a_pool_runner_when_the_hosting_runner_is_full(
        self, backend, start_runner
    ):
        sup = start_runner("sup", claims=["cao-supervisor"], accepts_unclaimed=False)
        w1 = start_runner("worker-1")
        w2 = start_runner("worker-2")
        backend.create_session("cao-supervisor", "supervisor", "aaaaaaaa")
        backend.create_window("cao-supervisor", "dev-1", "bbbbbbbb")
        backend.create_window("cao-supervisor", "dev-2", "cccccccc")
        # One terminal per runner: the supervisor's runner is full, so each
        # assigned window lands on a different worker runner.
        hosts = {
            rid: list(r.local.windows.get("cao-supervisor", {}))
            for rid, r in (("sup", sup), ("worker-1", w1), ("worker-2", w2))
        }
        assert hosts["sup"] == ["supervisor"]
        assert sorted(hosts["worker-1"] + hosts["worker-2"]) == ["dev-1", "dev-2"]
        assert backend.session_exists("cao-supervisor")

    def test_no_room_fails_after_the_placement_wait_and_releases_nothing(
        self, hub, backend, start_runner
    ):
        start_runner("only")
        backend.create_session("s1", "w", "aaaaaaaa")
        with pytest.raises(RunnerUnavailable):
            backend.create_session("s2", "w", "bbbbbbbb")
        assert hub.describe()[0]["reserved"] == 0

    def test_a_failed_create_releases_its_reservation(self, hub, backend, start_runner):
        r = start_runner("only")
        r.local.windows["s1"] = {"x": "zzzzzzzz"}  # runner-side collision
        with pytest.raises(ValueError):
            backend.create_session("s1", "w", "aaaaaaaa")
        info = hub.describe()[0]
        assert info["reserved"] == 0 and info["terminals"] == []

    def test_relative_working_directory_is_refused(self, backend, start_runner):
        start_runner("only")
        with pytest.raises(ValueError, match="absolute"):
            backend.create_session("s1", "w", "aaaaaaaa", working_directory="rel/path")


class TestRouting:
    def test_calls_reach_the_runner_hosting_the_window(self, backend, start_runner):
        a = start_runner("a")
        b = start_runner("b")
        backend.create_session("s1", "w", "aaaaaaaa")
        backend.create_session("s2", "w", "bbbbbbbb")
        owner = a if "s1" in a.local.windows else b
        backend.send_keys("s1", "w", "hello")
        assert owner.local.keys == [("s1", "w", "hello")]
        assert backend.get_history("s1", "w") == "history of s1:w"
        assert backend.get_pane_current_command("s1", "w") == "bash"

    def test_unrouted_window_raises_value_error_like_tmux(self, backend, start_runner):
        start_runner("a")
        with pytest.raises(ValueError):
            backend.send_keys("nope", "w", "x")
        assert backend.kill_window("nope", "w") is False
        assert backend.get_pane_working_directory("nope", "w") is None

    def test_runner_errors_keep_their_type(self, backend, start_runner):
        r = start_runner("a")
        backend.create_session("s1", "w", "aaaaaaaa")
        del r.local.windows["s1"]  # gone on the runner behind the server's back
        with pytest.raises(ValueError, match="runner a"):
            backend.send_keys("s1", "w", "x")

    def test_kill_and_exact_cleanup_drop_routes(self, hub, backend, start_runner):
        start_runner("a", max_terminals=2)
        backend.create_session("s1", "w1", "aaaaaaaa")
        backend.create_window("s1", "w2", "bbbbbbbb")
        assert backend.kill_window("s1", "w1") is True
        assert hub.route("s1", "w1") is None
        result = backend.cleanup_terminal_exact("bbbbbbbb", "s1", "w2")
        assert result.outcome is TerminalCleanupOutcome.DELETED and result.reclaimed
        assert hub.route("s1", "w2") is None
        assert backend.session_exists_strict("s1") is False

    def test_reconnecting_runner_restores_routes_from_its_own_report(
        self, hub, backend, start_runner, tmp_path
    ):
        r = start_runner("a")
        backend.create_session("s1", "w", "aaaaaaaa")
        r.stop()
        wait_for(lambda: "a" not in hub.connected_runners())
        # Restart the same runner state on a new connection.
        replacement = RunnerThread(f"ws://127.0.0.1:{hub.port}", "a", str(tmp_path / "a2"))
        replacement.runner._terminals = dict(r.runner._terminals)
        replacement.local.windows = r.local.windows
        try:
            wait_for(lambda: "a" in hub.connected_runners())
            assert hub.route("s1", "w") == ("a", "aaaaaaaa")
            backend.send_keys("s1", "w", "after reconnect")
            assert replacement.local.keys[-1] == ("s1", "w", "after reconnect")
        finally:
            replacement.stop()


class TestFailureSemantics:
    def test_disconnected_runner_is_unavailable_not_absent(self, hub, backend, start_runner):
        r = start_runner("a")
        backend.create_session("s1", "w", "aaaaaaaa")
        r.stop()
        wait_for(lambda: "a" not in hub.connected_runners())
        with pytest.raises(RunnerUnavailable):
            backend.send_keys("s1", "w", "x")
        with pytest.raises(TerminalBackendError):
            backend.session_exists_strict("s1")
        # A session no runner ever reported is provably absent: routes are the
        # runners' own reports, and the disconnected runner's are on record.
        assert backend.session_exists_strict("never-seen") is False
        # Its routed terminal cannot be proven gone while it is out of reach.
        assert backend.cleanup_terminal_exact("aaaaaaaa").outcome is TerminalCleanupOutcome.UNKNOWN

    def test_a_replaced_pool_runner_does_not_block_absence_proofs_forever(
        self, hub, backend, start_runner
    ):
        old = start_runner("pool-old")
        old.stop()
        wait_for(lambda: "pool-old" not in hub.connected_runners())
        start_runner("pool-new")
        assert backend.session_exists_strict("unrelated") is False

    def test_a_restarted_server_proves_nothing_until_every_member_reports(self, tmp_path):
        state = tmp_path / "runner-hub.json"
        first = RunnerHub(token=TOKEN, host="127.0.0.1", port=0, state_path=state)
        first.start()
        a = RunnerThread(f"ws://127.0.0.1:{first.port}", "a", str(tmp_path / "a"))
        b = RunnerThread(f"ws://127.0.0.1:{first.port}", "b", str(tmp_path / "b"))
        wait_for(lambda: first.connected_runners() == ["a", "b"])
        a.stop()
        b.stop()
        first.stop()

        # Restart: membership survives on disk, routes do not.
        second = RunnerHub(token=TOKEN, host="127.0.0.1", port=0, state_path=state)
        second.start()
        backend = RemoteBackend(hub=second)
        a2 = RunnerThread(f"ws://127.0.0.1:{second.port}", "a", str(tmp_path / "a"))
        try:
            wait_for(lambda: "a" in second.connected_runners())
            # "b" may be partitioned and hosting live panes: nothing is provable.
            with pytest.raises(TerminalBackendError):
                backend.session_exists_strict("s1")
            assert (
                backend.cleanup_terminal_exact("aaaaaaaa").outcome is TerminalCleanupOutcome.UNKNOWN
            )
            b2 = RunnerThread(f"ws://127.0.0.1:{second.port}", "b", str(tmp_path / "b"))
            try:
                wait_for(lambda: "b" in second.connected_runners())
                assert backend.session_exists_strict("s1") is False
            finally:
                b2.stop()
        finally:
            a2.stop()
            second.stop()

    def test_an_expired_member_stops_blocking_proofs_and_its_terminals_are_reaped(self, tmp_path):
        h = RunnerHub(token=TOKEN, host="127.0.0.1", port=0, expiry=0.2)
        h.start()
        try:
            backend = RemoteBackend(hub=h)
            r = RunnerThread(f"ws://127.0.0.1:{h.port}", "a", str(tmp_path / "a"))
            wait_for(lambda: "a" in h.connected_runners())
            backend.create_session("s1", "w", "aaaaaaaa")
            r.stop()
            wait_for(lambda: "a" not in h.connected_runners())
            time.sleep(0.3)
            assert h.can_prove_unrouted_absence() is False  # no member left at all
            assert h.route("s1", "w") is None
            assert h.is_reaping("aaaaaaaa")
            # It comes back still running the pane: the pane is killed on sight.
            back = RunnerThread(f"ws://127.0.0.1:{h.port}", "a", str(tmp_path / "a2"))
            back.runner._terminals = dict(r.runner._terminals)
            back.local.windows = r.local.windows
            try:
                wait_for(lambda: not h.is_reaping("aaaaaaaa"))
                assert back.local.windows == {}
            finally:
                back.stop()
        finally:
            h.stop()

    def test_a_call_lost_mid_flight_is_outcome_unknown(self, hub, backend, start_runner):
        r = start_runner("a")
        backend.create_session("s1", "w", "aaaaaaaa")
        r.local.block_send_keys.set()
        errors: List[BaseException] = []

        def _send() -> None:
            try:
                backend.send_keys("s1", "w", "x")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t = threading.Thread(target=_send)
        t.start()
        time.sleep(0.3)
        r.stop()
        t.join(timeout=10)
        assert errors and isinstance(errors[0], RunnerOutcomeUnknown)

    def test_hub_with_no_runner_cannot_prove_absence(self, backend):
        with pytest.raises(TerminalBackendError):
            backend.session_exists_strict("s1")
        assert backend.session_exists("s1") is False
        assert backend.cleanup_terminal_exact("aaaaaaaa").outcome is TerminalCleanupOutcome.UNKNOWN

    def test_bad_token_is_refused_at_the_handshake(self, hub, tmp_path):
        r = RunnerThread(
            f"ws://127.0.0.1:{hub.port}", "intruder", str(tmp_path / "x"), token="wrong"
        )
        try:
            time.sleep(0.5)
            assert hub.connected_runners() == []
        finally:
            r.stop()

    def test_call_from_the_hub_thread_is_refused(self, hub):
        out: List[BaseException] = []
        hub._loop.call_soon_threadsafe(
            lambda: out.append(_try(lambda: hub.call("a", "list_sessions", {})))
        )
        wait_for(lambda: out)
        assert isinstance(out[0], RuntimeError)

    def test_attach_is_explicitly_unsupported(self, backend):
        with pytest.raises(TerminalBackendError, match="not supported"):
            backend.prepare_web_attach("s", "w")
        with pytest.raises(TerminalBackendError, match="not supported"):
            backend.attach_session("s")


def _try(fn):
    try:
        fn()
    except BaseException as exc:  # noqa: BLE001
        return exc
    return None


class TestProviderFiles:
    def test_terminal_files_are_staged_and_paths_rewritten(self, backend, server_tmp, start_runner):
        r = start_runner("a")
        backend.create_session("s1", "w", "aaaaaaaa")
        (server_tmp / "aaaaaaaa.prompt").write_text("be helpful")
        (server_tmp / "aaaaaaaa.mcp.json").write_text("{}")
        (server_tmp / "bbbbbbbb.prompt").write_text("someone else's")
        backend.send_keys(
            "s1", "w", f"claude --append-system-prompt-file {server_tmp}/aaaaaaaa.prompt"
        )
        runner_tmp = r.runner.tmp_dir
        assert sorted(os.listdir(runner_tmp)) == ["aaaaaaaa.mcp.json", "aaaaaaaa.prompt"]
        with open(os.path.join(runner_tmp, "aaaaaaaa.prompt")) as fh:
            assert fh.read() == "be helpful"
        assert (
            r.local.keys[-1][2]
            == f"claude --append-system-prompt-file {runner_tmp}/aaaaaaaa.prompt"
        )
        # Removed with the terminal.
        backend.kill_window("s1", "w")
        assert os.listdir(runner_tmp) == []

    def test_runner_refuses_names_outside_its_tmp_dir(self, tmp_path):
        runner = Runner(
            "ws://unused", TOKEN, "a", backend=FakeLocalBackend(), tmp_dir=str(tmp_path)
        )
        for name in ("../escape", "aaaaaaaa/../../x", "not-a-terminal.prompt", ""):
            with pytest.raises(ValueError):
                runner.dispatch("stage_files", {"files": [{"name": name, "data": ""}]})

    def test_runner_refuses_unknown_ops(self, tmp_path):
        runner = Runner(
            "ws://unused", TOKEN, "a", backend=FakeLocalBackend(), tmp_dir=str(tmp_path)
        )
        with pytest.raises(ValueError):
            runner.dispatch("rm_rf", {})


class TestOutputStreaming:
    def test_runner_output_lands_in_the_server_fifo_in_order(self, backend, start_runner, tmp_path):
        from cli_agent_orchestrator.services.fifo_reader import _ensure_fifo, _open_fifo

        r = start_runner("a")
        backend.create_session("s1", "w", "aaaaaaaa")
        fifo = str(tmp_path / "server.fifo")
        _ensure_fifo(fifo)
        read_fd = _open_fifo(fifo, os.O_RDONLY | os.O_NONBLOCK)
        keepalive = _open_fifo(fifo, os.O_WRONLY | os.O_NONBLOCK)
        try:
            backend.pipe_pane("s1", "w", fifo)
            assert ("s1", "w") in r.local.piped
            pipe_id = r.runner._pipes[("s1", "w")].pipe_id
            for part in (b"one ", b"two ", b"three"):
                r.runner._emit(pipe_id, part)
            received = b""
            deadline = time.monotonic() + 5
            while received != b"one two three" and time.monotonic() < deadline:
                ready, _, _ = select.select([read_fd], [], [], 0.2)
                if ready:
                    received += os.read(read_fd, 4096)
            assert received == b"one two three"
            backend.stop_pipe_pane("s1", "w")
            assert ("s1", "w") not in r.local.piped
        finally:
            os.close(read_fd)
            os.close(keepalive)

    def test_output_buffered_while_disconnected_is_replayed(self, hub, tmp_path):
        r = RunnerThread("ws://127.0.0.1:1", "a", str(tmp_path / "a"))  # nothing listening
        try:
            r.runner._emit("p1", b"held")
            assert len(r.runner._outbox) == 1
        finally:
            r.stop()


class TestServerEnvForwarding:
    def test_remote_backend_forwards_the_agent_facing_address_not_its_own(self, monkeypatch):
        from cli_agent_orchestrator.backends import registry
        from cli_agent_orchestrator.utils.mcp_resolution import cao_server_env

        monkeypatch.setenv("CAO_API_HOST", "127.0.0.1")
        monkeypatch.setenv("CAO_HOME_DIR", "/srv/cao")
        monkeypatch.setenv("CAO_RUNNER_AGENT_API_HOST", "cao-server.cao.svc")
        monkeypatch.setenv("CAO_RUNNER_AGENT_API_PORT", "9889")
        monkeypatch.setattr(registry, "_backend", RemoteBackend())
        assert cao_server_env() == {"CAO_API_HOST": "cao-server.cao.svc", "CAO_API_PORT": "9889"}

    def test_local_backends_are_unchanged(self, monkeypatch):
        from cli_agent_orchestrator.backends import registry
        from cli_agent_orchestrator.utils.mcp_resolution import cao_server_env

        monkeypatch.setenv("CAO_API_HOST", "0.0.0.0")
        monkeypatch.delenv("CAO_HOME_DIR", raising=False)
        monkeypatch.setenv("CAO_API_PORT", "9889")
        monkeypatch.setattr(registry, "_backend", None)
        assert cao_server_env() == {"CAO_API_HOST": "127.0.0.1", "CAO_API_PORT": "9889"}


class TestTerminalServiceHooks:
    def test_working_directory_passes_through_for_the_runner_to_validate(self, monkeypatch):
        from cli_agent_orchestrator.backends import registry
        from cli_agent_orchestrator.services import terminal_service

        monkeypatch.setattr(registry, "_backend", RemoteBackend())
        resolve = terminal_service._resolve_launch_working_directory
        assert resolve("/does/not/exist/here") == "/does/not/exist/here"
        assert resolve(None) is None
        with pytest.raises(ValueError):
            resolve("relative")

    def test_unsupported_provider_reason(self):
        b = RemoteBackend()
        assert b.unsupported_provider_reason("claude_code") is None
        assert "kiro_cli" in (b.unsupported_provider_reason("kiro_cli") or "")


def test_factory_builds_remote_with_configured_providers(monkeypatch):
    from cli_agent_orchestrator.backends.factory import BackendFactory
    from cli_agent_orchestrator.services.config_service import ConfigService

    real_get = ConfigService.get

    def fake_get(key, default=None, override=None):
        if key == "terminal.remote_providers":
            return "claude_code, codex"
        return real_get(key, default=default, override=override)

    monkeypatch.setattr(ConfigService, "get", staticmethod(fake_get))
    b = BackendFactory.create(backend_override="remote")
    assert isinstance(b, RemoteBackend)
    assert b.supported_providers == frozenset({"claude_code", "codex"})
    with pytest.raises(TerminalBackendError, match="no runner hub"):
        b.create_session("s", "w", "aaaaaaaa")


class TestReviewRegressions:
    """Each one pins a failure mode found in review of the first slice."""

    def test_a_create_whose_reply_is_lost_is_reaped_not_orphaned(self, hub, backend, start_runner):
        r = start_runner("a")
        real_create = r.local.create_session

        def slow_create(*args, **kwargs):
            name = real_create(*args, **kwargs)
            time.sleep(1.0)  # the pane exists; the reply will miss the deadline
            return name

        r.local.create_session = slow_create
        hub.call_timeout = 0.3
        with pytest.raises(RunnerOutcomeUnknown):
            backend.create_session("s1", "w", "aaaaaaaa")
        hub.call_timeout = 5
        # The runner is still connected, so the hub reaps it right away; no
        # route or registry row was ever written for it.
        wait_for(lambda: "s1" not in r.local.windows, timeout=10)
        wait_for(lambda: not hub.is_reaping("aaaaaaaa"))
        assert hub.describe()[0]["terminals"] == []

    def test_a_kill_the_runner_never_confirmed_is_reaped_when_it_returns(
        self, hub, backend, start_runner, tmp_path
    ):
        r = start_runner("a")
        backend.create_session("s1", "w", "aaaaaaaa")
        r.stop()
        wait_for(lambda: "a" not in hub.connected_runners())
        assert backend.kill_window("s1", "w") is False  # unconfirmed, never "gone"
        assert hub.is_reaping("aaaaaaaa")
        back = RunnerThread(f"ws://127.0.0.1:{hub.port}", "a", str(tmp_path / "a2"))
        back.runner._terminals = dict(r.runner._terminals)
        back.local.windows = r.local.windows
        try:
            wait_for(lambda: not hub.is_reaping("aaaaaaaa"))
            assert back.local.windows == {}
        finally:
            back.stop()

    def test_a_reserved_session_waits_for_its_claimant_instead_of_the_pool(self, tmp_path):
        h = RunnerHub(
            token=TOKEN,
            host="127.0.0.1",
            port=0,
            placement_timeout=0.5,
            reserved_sessions=["cao-supervisor"],
        )
        h.start()
        try:
            backend = RemoteBackend(hub=h)
            pool = RunnerThread(f"ws://127.0.0.1:{h.port}", "pool", str(tmp_path / "p"))
            wait_for(lambda: "pool" in h.connected_runners())
            with pytest.raises(RunnerUnavailable):
                backend.create_session("cao-supervisor", "w", "aaaaaaaa")
            assert pool.local.windows == {}
            pool.stop()
        finally:
            h.stop()

    def test_a_disconnected_claimant_still_owns_its_claim(self, hub, backend, start_runner):
        start_runner("pool")
        sup = start_runner("sup", claims=["cao-supervisor"], accepts_unclaimed=False)
        sup.stop()
        wait_for(lambda: "sup" not in hub.connected_runners())
        with pytest.raises(RunnerUnavailable):
            backend.create_session("cao-supervisor", "w", "aaaaaaaa")

    def test_a_live_runner_cannot_be_displaced_by_a_newcomer_with_its_id(
        self, hub, backend, start_runner, tmp_path
    ):
        genuine = start_runner("a")
        backend.create_session("s1", "w", "aaaaaaaa")
        impostor = RunnerThread(f"ws://127.0.0.1:{hub.port}", "a", str(tmp_path / "imp"))
        try:
            time.sleep(0.5)
            backend.send_keys("s1", "w", "for the genuine runner")
            assert genuine.local.keys[-1][2] == "for the genuine runner"
            assert impostor.local.keys == []
        finally:
            impostor.stop()

    def test_output_from_a_previous_pipe_never_reaches_the_new_one(
        self, hub, backend, start_runner, tmp_path
    ):
        from cli_agent_orchestrator.services.fifo_reader import _ensure_fifo, _open_fifo

        r = start_runner("a")
        backend.create_session("s1", "w", "aaaaaaaa")
        fifo = str(tmp_path / "server.fifo")
        _ensure_fifo(fifo)
        read_fd = _open_fifo(fifo, os.O_RDONLY | os.O_NONBLOCK)
        keepalive = _open_fifo(fifo, os.O_WRONLY | os.O_NONBLOCK)
        try:
            backend.pipe_pane("s1", "w", fifo)
            old_pipe = r.runner._pipes[("s1", "w")].pipe_id
            backend.stop_pipe_pane("s1", "w")
            backend.pipe_pane("s1", "w", fifo)
            new_pipe = r.runner._pipes[("s1", "w")].pipe_id
            assert new_pipe != old_pipe
            r.runner._emit(old_pipe, b"STALE")
            r.runner._emit(new_pipe, b"fresh")
            received = b""
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and b"fresh" not in received:
                ready, _, _ = select.select([read_fd], [], [], 0.2)
                if ready:
                    received += os.read(read_fd, 4096)
            assert received == b"fresh"
        finally:
            os.close(read_fd)
            os.close(keepalive)

    def test_stopping_a_pipe_purges_its_undelivered_frames(self, tmp_path):
        runner = Runner(
            "ws://unused", TOKEN, "a", backend=FakeLocalBackend(), tmp_dir=str(tmp_path)
        )

        class _Stub:
            pipe_id = "old"

            def stop(self):
                pass

        runner._emit("old", b"x")
        runner._emit("keep", b"y")
        runner._stop_pipe(_Stub())
        assert [e[0] for e in runner._outbox] == ["keep"]

    def test_inventory_does_not_forget_a_terminal_created_during_its_snapshot(
        self, tmp_path, monkeypatch
    ):
        runner = Runner(
            "ws://unused", TOKEN, "a", backend=FakeLocalBackend(), tmp_dir=str(tmp_path)
        )

        def listing_then_create():
            # tmux was listed BEFORE this create landed.
            runner.dispatch(
                "create_session",
                {"session_name": "s1", "window_name": "w", "terminal_id": "aaaaaaaa"},
            )
            return set()

        monkeypatch.setattr(runner, "_live_windows", listing_then_create)
        assert runner.inventory() == [{"session": "s1", "window": "w", "terminal_id": "aaaaaaaa"}]

    def test_a_bad_runner_working_directory_is_a_value_error(self, tmp_path):
        runner = Runner(
            "ws://unused", TOKEN, "a", backend=FakeLocalBackend(), tmp_dir=str(tmp_path)
        )
        with pytest.raises(ValueError):
            runner.dispatch(
                "create_session",
                {
                    "session_name": "s1",
                    "window_name": "w",
                    "terminal_id": "aaaaaaaa",
                    "working_directory": str(tmp_path / "missing"),
                },
            )

    def test_a_closed_sink_with_a_full_backlog_exits(self, tmp_path):
        from cli_agent_orchestrator.runner import hub as hub_module

        sink = hub_module._PipeSink("a", "s:w", str(tmp_path / "no-such.fifo"))
        for _ in range(hub_module._PIPE_QUEUE_CHUNKS + 10):
            sink.feed(b"x")
        sink.close()
        sink.join(timeout=3)
        assert not sink._thread.is_alive()

    def test_bundled_mcp_command_stays_bare_for_remote_terminals(self, monkeypatch):
        from cli_agent_orchestrator.backends import registry
        from cli_agent_orchestrator.utils.mcp_resolution import resolve_mcp_server_config

        monkeypatch.setattr(registry, "_backend", RemoteBackend())
        resolved = resolve_mcp_server_config({"command": "cao-mcp-server"}, name="cao-mcp-server")
        assert resolved["command"] == "cao-mcp-server"


class TestSecondReviewRegressions:
    def test_a_reap_racing_a_create_that_has_not_made_its_pane_yet_waits_for_it(
        self, hub, backend, start_runner, monkeypatch
    ):
        from cli_agent_orchestrator.runner import agent as agent_module

        monkeypatch.setattr(agent_module, "_INVENTORY_INTERVAL", 0.2)
        r = start_runner("a")
        real_create = r.local.create_session

        def blocked_then_create(*args, **kwargs):
            time.sleep(1.0)  # the reply deadline passes BEFORE the pane exists
            return real_create(*args, **kwargs)

        r.local.create_session = blocked_then_create
        hub.call_timeout = 0.3
        with pytest.raises(RunnerOutcomeUnknown):
            backend.create_session("s1", "w", "aaaaaaaa")
        hub.call_timeout = 5
        # The immediate reap saw the create in flight and did not call it absent.
        assert hub.is_reaping("aaaaaaaa")
        # Once the pane exists the next inventory reports it and it is killed.
        wait_for(lambda: not hub.is_reaping("aaaaaaaa"), timeout=10)
        assert r.local.windows == {}

    def test_a_routed_session_on_an_unreachable_runner_still_exists(
        self, hub, backend, start_runner
    ):
        r = start_runner("a")
        backend.create_session("s1", "w", "aaaaaaaa")
        r.stop()
        wait_for(lambda: "a" not in hub.connected_runners())
        # "False" here would let a create of the same name land elsewhere.
        assert backend.session_exists("s1") is True
        assert backend.session_exists("never-routed") is False

    def test_a_replayed_output_frame_is_written_once(self, hub, backend, start_runner, tmp_path):
        from cli_agent_orchestrator.services.fifo_reader import _ensure_fifo, _open_fifo

        r = start_runner("a")
        backend.create_session("s1", "w", "aaaaaaaa")
        fifo = str(tmp_path / "server.fifo")
        _ensure_fifo(fifo)
        read_fd = _open_fifo(fifo, os.O_RDONLY | os.O_NONBLOCK)
        keepalive = _open_fifo(fifo, os.O_WRONLY | os.O_NONBLOCK)
        try:
            backend.pipe_pane("s1", "w", fifo)
            pipe_id = r.runner._pipes[("s1", "w")].pipe_id
            frame = {"type": "output", "pipe_id": pipe_id, "seq": 1, "data": "b25jZQ=="}  # "once"
            hub._on_output("a", dict(frame))
            hub._on_output("a", dict(frame))  # the replay after a reconnect
            hub._on_output("a", {**frame, "seq": 2, "data": "IQ=="})  # "!"
            received = b""
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and received != b"once!":
                ready, _, _ = select.select([read_fd], [], [], 0.2)
                if ready:
                    received += os.read(read_fd, 4096)
            time.sleep(0.3)
            ready, _, _ = select.select([read_fd], [], [], 0.1)
            if ready:
                received += os.read(read_fd, 4096)
            assert received == b"once!"
        finally:
            os.close(read_fd)
            os.close(keepalive)

    def test_a_missing_first_frame_is_reported_as_a_gap(self, hub, backend, start_runner, caplog):
        r = start_runner("a")
        backend.create_session("s1", "w", "aaaaaaaa")
        pipe_id = hub.open_pipe("a", "s1", "w", "/nonexistent.fifo")
        with caplog.at_level("WARNING"):
            hub._on_output("a", {"type": "output", "pipe_id": pipe_id, "seq": 7, "data": ""})
        assert "expected seq 1, got 7" in caplog.text
        hub.close_pipe("a", "s1", "w")
        assert r is not None

    def test_a_restarted_server_restores_routes_and_reaps_an_expired_members_terminals(
        self, tmp_path
    ):
        state = tmp_path / "runner-hub.json"
        first = RunnerHub(token=TOKEN, host="127.0.0.1", port=0, state_path=state)
        first.start()
        r = RunnerThread(f"ws://127.0.0.1:{first.port}", "a", str(tmp_path / "a"))
        wait_for(lambda: "a" in first.connected_runners())
        RemoteBackend(hub=first).create_session("s1", "w", "aaaaaaaa")
        r.stop()
        first.stop()

        # Placement comes back from the terminal's registry row, not the file.
        second = RunnerHub(token=TOKEN, host="127.0.0.1", port=0, state_path=state, expiry=3600)
        second.seed_placements(
            [{"id": "aaaaaaaa", "tmux_session": "s1", "tmux_window": "w", "runner_id": "a"}]
        )
        assert second.route("s1", "w") == ("a", "aaaaaaaa")  # owned, not absent
        second.expiry = 0
        assert second.can_prove_unrouted_absence() is False
        assert second.is_reaping("aaaaaaaa")  # declared lost, killed if "a" returns

    def test_forget_only_removes_the_incarnation_it_was_asked_to(self, tmp_path):
        runner = Runner(
            "ws://unused", TOKEN, "a", backend=FakeLocalBackend(), tmp_dir=str(tmp_path)
        )
        runner._record("s1", "w", "bbbbbbbb")  # a replacement took the name
        runner._forget(("s1", "w"), expected_terminal_id="aaaaaaaa")
        assert runner._terminals == {("s1", "w"): "bbbbbbbb"}


class TestThirdReviewRegressions:
    def test_a_create_reports_its_placement_the_moment_it_succeeds(
        self, hub, backend, start_runner
    ):
        start_runner("a")
        backend.create_session("s1", "w", "aaaaaaaa")
        # terminal_service records this in the row it writes right after the
        # create returns (terminals.runner_id); no heartbeat needs to run first.
        assert backend.placement_of("aaaaaaaa") == "a"
        assert backend.placement_of("bbbbbbbb") is None

    def test_an_unconfirmed_create_still_occupies_its_runner(self, hub, backend, start_runner):
        r = start_runner("a")  # max_terminals=1
        real_create = r.local.create_session

        def blocked_then_create(*args, **kwargs):
            time.sleep(1.5)
            return real_create(*args, **kwargs)

        r.local.create_session = blocked_then_create
        hub.call_timeout = 0.3
        with pytest.raises(RunnerOutcomeUnknown):
            backend.create_session("s1", "w", "aaaaaaaa")
        hub.call_timeout = 5
        # The first pane may still appear there: a second must not be placed.
        with pytest.raises(RunnerUnavailable):
            backend.create_session("s2", "w", "bbbbbbbb")

    def test_close_pipe_waits_for_its_writer(self, hub, tmp_path):
        hub.add_route("s1", "w", "a", "aaaaaaaa")
        pipe_id = hub.open_pipe("a", "s1", "w", str(tmp_path / "no.fifo"))
        sink = hub._sinks[pipe_id]
        hub.close_pipe("a", "s1", "w")
        assert not sink.is_alive()

    def test_a_routed_session_on_an_unreachable_runner_is_still_listed(
        self, hub, backend, start_runner
    ):
        r = start_runner("a")
        backend.create_session("s1", "w", "aaaaaaaa")
        r.stop()
        wait_for(lambda: "a" not in hub.connected_runners())
        assert [s["name"] for s in backend.list_sessions()] == ["s1"]

"""Durable placement, terminal guards, reachability and channel config (#745).

Real hub, real runner, real WebSocket, as in ``test_remote_backend.py``; the
registry is a real SQLite file and the status detector a real StatusMonitor.
"""

import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from test.runner.test_remote_backend import TOKEN, FakeLocalBackend, RunnerThread, _try, wait_for

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.backends.base import TerminalBackendError
from cli_agent_orchestrator.backends.remote_backend import RemoteBackend
from cli_agent_orchestrator.clients import database as db_mod
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.runner import agent as agent_mod
from cli_agent_orchestrator.runner import hub as hub_mod
from cli_agent_orchestrator.runner import token as token_mod
from cli_agent_orchestrator.runner.agent import RUNNER_ID_OPTION, Runner
from cli_agent_orchestrator.runner.hub import RunnerHub
from cli_agent_orchestrator.services import status_monitor as status_mod

# --- Fixtures ---------------------------------------------------------------


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """A real terminals table in a private SQLite file."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'registry.db'}", connect_args={"check_same_thread": False}
    )
    db_mod.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(
        db_mod, "SessionLocal", sessionmaker(autocommit=False, autoflush=False, bind=engine)
    )
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def monitor(monkeypatch):
    """A fresh StatusMonitor standing in for the process-global one."""
    fresh = status_mod.StatusMonitor()
    monkeypatch.setattr(status_mod, "status_monitor", fresh)
    return fresh


@pytest.fixture
def make_hub():
    hubs = []

    def _make(**kwargs) -> RunnerHub:
        kwargs.setdefault("call_timeout", 5)
        kwargs.setdefault("placement_timeout", 1)
        h = RunnerHub(token=TOKEN, host="127.0.0.1", port=0, **kwargs)
        hubs.append(h)
        return h

    yield _make
    for h in hubs:
        h.stop()


@pytest.fixture
def runners():
    started = []

    def _start(hub: RunnerHub, runner_id: str, tmp_dir: Path, **kwargs) -> RunnerThread:
        r = RunnerThread(f"ws://127.0.0.1:{hub.port}", runner_id, str(tmp_dir), **kwargs)
        started.append(r)
        return r

    yield _start
    for r in started:
        r.stop()


def _row(terminal_id, session="s1", window="w", runner_id=None):
    return db_mod.create_terminal(
        terminal_id, session, window, "mock_cli", **({"runner_id": runner_id} if runner_id else {})
    )


# --- A1: durable placement --------------------------------------------------


class TestRunnerIdColumn:
    def _legacy_db(self, path):
        table = db_mod.Base.metadata.tables["terminals"]
        from sqlalchemy.dialects import sqlite
        from sqlalchemy.schema import CreateTable

        ddl = str(CreateTable(table).compile(dialect=sqlite.dialect()))
        legacy = ddl.replace("\trunner_id VARCHAR, \n", "")
        assert legacy != ddl and "runner_id" not in legacy
        engine = create_engine(f"sqlite:///{path}")
        try:
            db_mod.Base.metadata.create_all(
                engine, tables=[t for t in db_mod.Base.metadata.sorted_tables if t is not table]
            )
            with engine.begin() as conn:
                conn.exec_driver_sql(legacy)
        finally:
            engine.dispose()
        with sqlite3.connect(str(path)) as conn:
            conn.execute(
                "INSERT INTO terminals (id, tmux_session, tmux_window, provider) "
                "VALUES ('aaaa1111', 'cao-s', 'w-0', 'claude_code')"
            )

    def test_an_existing_database_gains_the_column_idempotently(self, tmp_path, monkeypatch):
        path = tmp_path / "legacy.db"
        self._legacy_db(path)
        engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
        monkeypatch.setattr(db_mod, "engine", engine)
        monkeypatch.setattr(db_mod, "SessionLocal", sessionmaker(bind=engine))
        monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", path, raising=False)
        try:
            db_mod.init_db()
            db_mod.init_db()  # a restart
            with sqlite3.connect(str(path)) as conn:
                cols = [
                    r for r in conn.execute("PRAGMA table_info(terminals)") if r[1] == "runner_id"
                ]
            [(_, _, column_type, notnull, default, _)] = cols
            assert column_type == "TEXT" and notnull == 0 and default is None
            assert db_mod.get_terminal_runner("aaaa1111") is None  # a local row
            assert db_mod.list_runner_placements() == []
        finally:
            engine.dispose()

    def test_rows_round_trip_their_runner(self, registry):
        _row("aaaaaaaa", runner_id="a")
        _row("bbbbbbbb", window="w2")  # local terminal: no runner
        assert db_mod.get_terminal_runner("aaaaaaaa") == "a"
        assert db_mod.list_runner_placements() == [
            {"id": "aaaaaaaa", "tmux_session": "s1", "tmux_window": "w", "runner_id": "a"}
        ]
        assert db_mod.update_terminal_runner("aaaaaaaa", "b") is True
        assert db_mod.update_terminal_runner("cccccccc", "b") is False
        assert db_mod.get_terminal_runner("aaaaaaaa") == "b"
        assert db_mod.terminal_exists("bbbbbbbb") and not db_mod.terminal_exists("cccccccc")

    def test_terminal_service_records_the_backend_placement(self, monkeypatch):
        from cli_agent_orchestrator.services import terminal_service

        class Placed:
            def placement_of(self, terminal_id):
                return "runner-a" if terminal_id == "aaaaaaaa" else None

        monkeypatch.setattr(terminal_service, "get_backend", lambda: Placed())
        assert terminal_service._backend_placement("aaaaaaaa") == "runner-a"
        assert terminal_service._backend_placement("bbbbbbbb") is None

        class Local:  # tmux/herdr, a MagicMock: never a placement
            def placement_of(self, terminal_id):
                return object()

        monkeypatch.setattr(terminal_service, "get_backend", lambda: Local())
        assert terminal_service._backend_placement("aaaaaaaa") is None


class TestPlacementReconciliation:
    """DB row vs. runner report. Rules (also in docs/remote-runners.md):

    1. At startup the rows are seeded as routes; seeded terminals read UNKNOWN
       until their runner reports.
    2. A runner's report wins over the row (it holds the pane); the row is
       corrected, or filled in when it names no runner.
    3. A row whose runner reports WITHOUT that terminal loses its route; its
       status stays UNKNOWN until teardown removes the row.
    4. A reported terminal with NO row is reaped after two consecutive reports,
       never within the placement grace, and never when the registry cannot be
       read.
    """

    def test_rows_seed_routes_before_any_runner_reconnects(self, registry, monitor, make_hub):
        _row("aaaaaaaa", runner_id="a")
        h = make_hub()
        RemoteBackend().attach_hub(h)
        assert h.route("s1", "w") == ("a", "aaaaaaaa")
        assert h.can_prove_unrouted_absence() is False  # "a" has not reported
        assert monitor.get_status("aaaaaaaa") is TerminalStatus.UNKNOWN

    def test_the_reporting_runner_wins_and_the_row_follows(
        self, registry, monitor, make_hub, runners, tmp_path
    ):
        _row("aaaaaaaa", runner_id="a")
        h = make_hub()
        RemoteBackend().attach_hub(h)
        h.start()
        runners(h, "b", tmp_path / "b", hosting={("s1", "w"): "aaaaaaaa"})
        wait_for(lambda: "b" in h.connected_runners())
        assert h.route("s1", "w") == ("b", "aaaaaaaa")
        wait_for(lambda: db_mod.get_terminal_runner("aaaaaaaa") == "b")
        assert "aaaaaaaa" not in monitor._unreachable

    def test_a_row_its_runner_no_longer_hosts_loses_its_route(
        self, registry, monitor, make_hub, runners, tmp_path
    ):
        _row("aaaaaaaa", runner_id="a")
        h = make_hub()
        RemoteBackend().attach_hub(h)
        h.start()
        runners(h, "a", tmp_path / "a")  # reports nothing
        wait_for(lambda: "a" in h.connected_runners())
        wait_for(lambda: h.route("s1", "w") is None)
        assert db_mod.terminal_exists("aaaaaaaa")  # teardown owns the row
        assert monitor.get_status("aaaaaaaa") is TerminalStatus.UNKNOWN

    def test_an_unowned_terminal_is_reaped_after_two_reports(
        self, registry, monitor, make_hub, runners, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(agent_mod, "_INVENTORY_INTERVAL", 0.2)
        h = make_hub()
        RemoteBackend().attach_hub(h)
        h.start()
        a = runners(h, "a", tmp_path / "a", hosting={("s1", "w"): "dddddddd"})  # no row
        wait_for(lambda: a.local.windows == {}, timeout=10)
        wait_for(lambda: not h.is_reaping("dddddddd"))

    def test_a_row_without_a_runner_is_backfilled_from_the_report(
        self, registry, monitor, make_hub, runners, tmp_path
    ):
        _row("aaaaaaaa")  # written before the column existed: runner_id NULL
        h = make_hub()
        RemoteBackend().attach_hub(h)
        h.start()
        runners(h, "a", tmp_path / "a", hosting={("s1", "w"): "aaaaaaaa"})
        wait_for(lambda: db_mod.get_terminal_runner("aaaaaaaa") == "a")
        # And a restarted server now seeds it.
        h2 = make_hub()
        RemoteBackend().attach_hub(h2)
        assert h2.route("s1", "w") == ("a", "aaaaaaaa")

    def test_a_terminal_being_placed_is_not_reaped(
        self, registry, monitor, make_hub, runners, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(agent_mod, "_INVENTORY_INTERVAL", 0.2)
        h = make_hub()
        RemoteBackend().attach_hub(h)
        h.start()
        h.note_placing("eeeeeeee")  # its row is about to be written
        a = runners(h, "a", tmp_path / "a", hosting={("s1", "w"): "eeeeeeee"})
        time.sleep(1.0)  # several inventories
        assert a.local.windows == {"s1": {"w": "eeeeeeee"}}

    def test_an_unreadable_registry_never_reaps(
        self, monitor, make_hub, runners, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(agent_mod, "_INVENTORY_INTERVAL", 0.2)

        def broken(_terminal_id):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(db_mod, "terminal_exists", broken)
        h = make_hub()
        RemoteBackend().attach_hub(h, restore=False)
        h.start()
        a = runners(h, "a", tmp_path / "a", hosting={("s1", "w"): "ffffffff"})
        time.sleep(1.0)
        assert a.local.windows == {"s1": {"w": "ffffffff"}}

    def test_a_create_is_recorded_once_written(
        self, registry, monitor, make_hub, runners, tmp_path
    ):
        h = make_hub()
        backend = RemoteBackend()
        backend.attach_hub(h)
        h.start()
        runners(h, "a", tmp_path / "a")
        wait_for(lambda: "a" in h.connected_runners())
        backend.create_session("s1", "w", "aaaaaaaa")
        _row("aaaaaaaa", runner_id=backend.placement_of("aaaaaaaa"))
        assert db_mod.list_runner_placements()[0]["runner_id"] == "a"

        # A restarted server knows where it is before "a" says anything.
        h2 = make_hub()
        RemoteBackend().attach_hub(h2)
        assert h2.route("s1", "w") == ("a", "aaaaaaaa")


# --- A6: every window op is guarded by its terminal --------------------------


class TestTerminalGuard:
    def test_the_runner_refuses_an_op_for_a_terminal_that_lost_the_window(self, tmp_path):
        local = FakeLocalBackend()
        local.windows = {"s1": {"w": "bbbbbbbb"}}
        runner = Runner("ws://unused", TOKEN, "a", backend=local, tmp_dir=str(tmp_path))
        runner._record("s1", "w", "bbbbbbbb")  # a newer terminal reused the name
        for op, extra in [
            ("send_keys", {"keys": "rm -rf"}),
            ("send_special_key", {"key": "C-c"}),
            ("get_history", {}),
            ("kill_window", {}),
            ("pipe_pane", {"pipe_id": "p1"}),
            ("stop_pipe_pane", {}),
            ("get_pane_working_directory", {}),
            ("get_pane_current_command", {}),
        ]:
            args = {"session_name": "s1", "window_name": "w", "expected_terminal_id": "aaaaaaaa"}
            # "Window not found" for that terminal: 404 on every route, as tmux.
            with pytest.raises(ValueError, match="not found"):
                runner.dispatch(op, {**args, **extra})
        assert local.keys == [] and local.windows == {"s1": {"w": "bbbbbbbb"}}
        # The current owner is served.
        runner.dispatch(
            "send_keys",
            {
                "session_name": "s1",
                "window_name": "w",
                "expected_terminal_id": "bbbbbbbb",
                "keys": "ls",
            },
        )
        assert local.keys == [("s1", "w", "ls")]

    def test_every_window_op_carries_the_routed_terminal(self, make_hub, runners, tmp_path):
        h = make_hub()
        backend = RemoteBackend(hub=h)
        backend._tmp_dir = str(tmp_path / "srv")
        os.makedirs(backend._tmp_dir)
        h.start()
        runners(h, "a", tmp_path / "a")
        wait_for(lambda: "a" in h.connected_runners())
        backend.create_session("s1", "w", "aaaaaaaa")
        seen = {}
        real_call = h.call

        def spy(runner_id, op, args):
            seen[op] = dict(args)
            return real_call(runner_id, op, args)

        h.call = spy
        backend.send_keys("s1", "w", "hi")
        backend.send_special_key("s1", "w", "Enter")
        backend.get_history("s1", "w")
        backend.get_pane_working_directory("s1", "w")
        backend.get_pane_current_command("s1", "w")
        backend.pipe_pane("s1", "w", str(tmp_path / "fifo"))
        backend.stop_pipe_pane("s1", "w")
        backend.kill_window("s1", "w")
        window_ops = {op: a for op, a in seen.items() if op != "stage_files"}
        assert len(window_ops) == 8
        assert {a.get("expected_terminal_id") for a in window_ops.values()} == {"aaaaaaaa"}

    def test_a_reused_window_name_rejects_the_stale_route(self, make_hub, runners, tmp_path):
        h = make_hub()
        backend = RemoteBackend(hub=h)
        backend._tmp_dir = str(tmp_path / "srv")
        os.makedirs(backend._tmp_dir)
        h.start()
        a = runners(h, "a", tmp_path / "a")
        wait_for(lambda: "a" in h.connected_runners())
        backend.create_session("s1", "w", "aaaaaaaa")
        # The runner's window now belongs to someone else (e.g. a late create).
        a.runner._record("s1", "w", "bbbbbbbb")
        with pytest.raises(ValueError, match="not found"):
            backend.send_keys("s1", "w", "meant for aaaaaaaa")
        assert a.local.keys == []
        # Killing the stale route kills nothing and forgets the route.
        assert backend.kill_window("s1", "w") is False
        assert a.local.windows == {"s1": {"w": "aaaaaaaa"}}
        assert h.route("s1", "w") is None

    def test_concurrent_re_arms_leave_the_runner_streaming_the_live_sink(
        self, make_hub, runners, tmp_path
    ):
        h = make_hub()
        backend = RemoteBackend(hub=h)
        backend._tmp_dir = str(tmp_path / "srv")
        os.makedirs(backend._tmp_dir)
        h.start()
        a = runners(h, "a", tmp_path / "a")
        wait_for(lambda: "a" in h.connected_runners())
        backend.create_session("s1", "w", "aaaaaaaa")
        real_call = h.call
        inside, peak = [0], [0]

        def slow_arm(runner_id, op, args):
            if op != "pipe_pane":
                return real_call(runner_id, op, args)
            inside[0] += 1
            peak[0] = max(peak[0], inside[0])
            time.sleep(0.3)
            try:
                return real_call(runner_id, op, args)
            finally:
                inside[0] -= 1

        h.call = slow_arm
        fifo = str(tmp_path / "server.fifo")
        threads = [
            threading.Thread(target=backend.pipe_pane, args=("s1", "w", fifo)) for _ in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert peak[0] == 1  # sink-then-arm is one step per window
        assert a.runner._pipes[("s1", "w")].pipe_id == h._pipe_of[("a", "s1", "w")]

    def test_a_window_op_never_overlaps_a_kill_of_the_same_window(self, tmp_path):
        events = []

        class Slow(FakeLocalBackend):
            def send_keys(self, session, window, keys, **kwargs):
                events.append("send-start")
                time.sleep(0.4)
                super().send_keys(session, window, keys, **kwargs)
                events.append("send-end")

            def kill_window(self, session, window):
                events.append("kill")
                return super().kill_window(session, window)

        local = Slow()
        local.windows = {"s1": {"w": "aaaaaaaa"}}
        runner = Runner("ws://unused", TOKEN, "a", backend=local, tmp_dir=str(tmp_path))
        runner._record("s1", "w", "aaaaaaaa")
        guard = {"session_name": "s1", "window_name": "w", "expected_terminal_id": "aaaaaaaa"}
        sender = threading.Thread(
            target=runner.dispatch, args=("send_keys", {**guard, "keys": "x"})
        )
        sender.start()
        time.sleep(0.1)  # the send is inside the backend call
        runner.dispatch("kill_window", dict(guard))
        sender.join(5)
        assert events == ["send-start", "send-end", "kill"]

    def test_a_slow_window_does_not_starve_the_runner(self, make_hub, runners, tmp_path):
        h = make_hub(call_timeout=10)
        backend = RemoteBackend(hub=h)
        backend._tmp_dir = str(tmp_path / "srv")
        os.makedirs(backend._tmp_dir)
        h.start()
        a = runners(h, "a", tmp_path / "a", max_terminals=2)
        wait_for(lambda: "a" in h.connected_runners())
        backend.create_session("s1", "slow", "aaaaaaaa")
        backend.create_window("s1", "fast", "bbbbbbbb")
        a.local.block_send_keys.set()  # each send on "slow" takes 5s
        stuck = [
            threading.Thread(target=_try, args=(lambda: backend.send_keys("s1", "slow", "x"),))
            for _ in range(40)  # more than the default executor's threads
        ]
        for t in stuck:
            t.start()
        time.sleep(0.5)
        started = time.monotonic()
        assert backend.get_history("s1", "fast") == "history of s1:fast"
        assert time.monotonic() - started < 3

    def test_a_create_queued_behind_its_window_is_already_in_flight(self, tmp_path):
        import asyncio

        local = FakeLocalBackend()
        runner = Runner("ws://unused", TOKEN, "a", backend=local, tmp_dir=str(tmp_path))
        seen = {}

        async def scenario():
            sent = []

            class WS:
                async def send(self, data):
                    sent.append(data)

            lock = runner._async_window_lock("s1", "w")
            await lock.acquire()  # another call holds the window
            frame = (
                '{"type": "call", "id": "1", "op": "create_session", "args": '
                '{"session_name": "s1", "window_name": "w", "terminal_id": "aaaaaaaa"}}'
            )
            task = asyncio.ensure_future(runner._serve_call(WS(), frame))
            await asyncio.sleep(0.05)
            seen["queued"] = runner.dispatch("cleanup_terminal_exact", {"terminal_id": "aaaaaaaa"})
            lock.release()
            await task
            seen["after"] = sorted(runner._creating)

        asyncio.run(scenario())
        assert seen["queued"]["outcome"] == "unknown"  # never "absent" while queued
        assert seen["after"] == []
        assert local.windows == {"s1": {"w": "aaaaaaaa"}}

    def test_a_create_whose_window_was_re_reported_is_unconfirmed(
        self, make_hub, runners, tmp_path
    ):
        from cli_agent_orchestrator.runner.protocol import RunnerOutcomeUnknown

        h = make_hub()
        backend = RemoteBackend(hub=h)
        h.start()
        runners(h, "a", tmp_path / "a")
        wait_for(lambda: "a" in h.connected_runners())
        real_call = h.call

        def racing(runner_id, op, args):
            value = real_call(runner_id, op, args)
            if op == "create_session":  # a report lands before add_route
                with h._lock:
                    h._routes[("s1", "w")] = hub_mod._Route("a", "bbbbbbbb")
            return value

        h.call = racing
        with pytest.raises(RunnerOutcomeUnknown):
            backend.create_session("s1", "w", "aaaaaaaa")
        assert h.route("s1", "w") == ("a", "bbbbbbbb")  # the report wins
        assert h.is_reaping("aaaaaaaa")

    def test_a_pipe_is_never_opened_for_a_displaced_route(self, make_hub):
        h = make_hub()
        h.add_route("s1", "w", "b", "aaaaaaaa")  # runner b won the window
        with pytest.raises(ValueError):
            h.open_pipe("a", "s1", "w", "/tmp/x.fifo", "aaaaaaaa")
        with pytest.raises(ValueError):
            h.open_pipe("b", "s1", "w", "/tmp/x.fifo", "cccccccc")
        assert h._pipe_of == {}

    def test_a_stale_reconciliation_never_writes_back_a_lost_placement(self, make_hub):
        h = make_hub()
        written = []
        h.bind(
            ownership=lambda t, r: written.append((t, r)) or True,
            relocate=lambda t, r: written.append(("relocate", t, r)),
        )
        h.add_route("s1", "w", "b", "aaaaaaaa")  # b now holds it
        h._reconcile_registry("a", ["aaaaaaaa"], ["aaaaaaaa"])  # a's late pass
        assert written == []
        h._reconcile_registry("b", ["aaaaaaaa"], [])
        assert written == [("aaaaaaaa", "b")]

    def test_a_dropped_connection_never_clears_a_create_still_running(self, tmp_path):
        import asyncio

        gate = threading.Event()

        class Slow(FakeLocalBackend):
            def create_session(self, *args, **kwargs):
                gate.wait(5)
                return super().create_session(*args, **kwargs)

        runner = Runner("ws://unused", TOKEN, "a", backend=Slow(), tmp_dir=str(tmp_path))
        seen = {}

        async def scenario():
            class WS:
                async def send(self, data):
                    pass

            frame = (
                '{"type": "call", "id": "1", "op": "create_session", "args": '
                '{"session_name": "s1", "window_name": "w", "terminal_id": "aaaaaaaa"}}'
            )
            task = asyncio.ensure_future(runner._serve_call(WS(), frame))
            await asyncio.sleep(0.1)  # the worker thread is inside the create
            task.cancel()  # the connection dropped
            await asyncio.sleep(0.05)
            with runner._lock:
                seen["during"] = sorted(runner._creating)
            gate.set()
            for _ in range(100):
                await asyncio.sleep(0.02)
                with runner._lock:
                    if not runner._creating:
                        break
            seen["after"] = sorted(runner._creating)

        asyncio.run(scenario())
        assert seen["during"] == ["aaaaaaaa"]  # still reported as in flight
        assert seen["after"] == []

    def test_a_row_written_after_its_route_moved_follows_the_route(self, make_hub):
        h = make_hub()
        rows = {}

        def ownership(terminal_id, runner_id):
            # The route moves to b while this (a's) write is in progress.
            with h._lock:
                h._routes[("s1", "w")] = hub_mod._Route("b", terminal_id)
            rows[terminal_id] = runner_id
            return True

        h.bind(ownership=ownership, relocate=lambda t, r: rows.__setitem__(t, r))
        h.add_route("s1", "w", "a", "aaaaaaaa")
        h._reconcile_registry("a", ["aaaaaaaa"], [])
        assert rows == {"aaaaaaaa": "b"}

    def test_reports_are_reconciled_on_one_worker(self, make_hub):
        h = make_hub()
        release = threading.Event()
        calls = []

        def ownership(terminal_id, runner_id):
            calls.append(terminal_id)
            release.wait(5)  # a stalled database
            return True

        h.bind(ownership=ownership)
        h.add_route("s1", "w", "a", "aaaaaaaa")
        before = threading.active_count()
        for _ in range(20):
            h._after_report("a", ["aaaaaaaa"], [], [])
        time.sleep(0.2)
        assert threading.active_count() - before <= 1
        release.set()
        wait_for(lambda: not h._reconcile_running)
        assert len(calls) <= 2  # coalesced, not one pass per report

    def test_coalesced_reports_still_count_toward_reaping(self, make_hub):
        h = make_hub()
        reaped = []
        release = threading.Event()
        h.add_route("s0", "w", "z", "zzzzzzzz")
        h.add_route("s1", "w", "a", "dddddddd")

        def ownership(terminal_id, runner_id):
            if terminal_id == "zzzzzzzz":
                release.wait(5)  # the worker is busy with another runner
                return True
            return False  # no row

        h.bind(ownership=ownership)
        h.mark_for_reaping = lambda r, t: reaped.append((r, t))
        h._after_report("z", ["zzzzzzzz"], [], [])
        time.sleep(0.1)
        h._after_report("a", ["dddddddd"], [], [])  # hello
        h._after_report("a", ["dddddddd"], [], [])  # inventory, merged with it
        release.set()
        wait_for(lambda: not h._reconcile_running)
        assert reaped == [("a", "dddddddd")]  # two reports, two strikes

    def test_the_reconcile_queue_is_served_oldest_first(self, make_hub):
        h = make_hub()
        order = []
        release = threading.Event()
        for runner in ("x", "a", "b"):
            h.add_route(f"s-{runner}", "w", runner, f"{runner * 8}")

        def ownership(terminal_id, runner_id):
            order.append(runner_id)
            if runner_id == "x":
                release.wait(5)
            return True

        h.bind(ownership=ownership)
        h._after_report("x", ["xxxxxxxx"], [], [])
        time.sleep(0.1)
        h._after_report("b", ["bbbbbbbb"], [], [])
        h._after_report("a", ["aaaaaaaa"], [], [])
        h._after_report("b", ["bbbbbbbb"], [], [])  # b re-reports: keeps its place
        release.set()
        wait_for(lambda: not h._reconcile_running)
        assert order == ["x", "b", "a"]

    def test_a_route_displaced_while_the_old_sink_drains_gets_no_sink(self, make_hub, tmp_path):
        h = make_hub()
        h.add_route("s1", "w", "a", "aaaaaaaa")
        h.open_pipe("a", "s1", "w", str(tmp_path / "x.fifo"), "aaaaaaaa")
        old = h._sinks[h._pipe_of[("a", "s1", "w")]]
        real_join = old.join

        def displace_then_join(*args, **kwargs):
            with h._lock:
                h._routes[("s1", "w")] = hub_mod._Route("b", "aaaaaaaa")
            return real_join(*args, **kwargs)

        old.join = displace_then_join
        with pytest.raises(ValueError):
            h.open_pipe("a", "s1", "w", str(tmp_path / "x.fifo"), "aaaaaaaa")
        assert ("a", "s1", "w") not in h._pipe_of

    def test_a_kill_reply_never_drops_a_newer_route(self, make_hub):
        h = make_hub()
        with h._lock:
            h._routes[("s1", "w")] = hub_mod._Route(runner_id="a", terminal_id="bbbbbbbb")
        h.drop_route("s1", "w", "a", "aaaaaaaa")  # the reply for the old incarnation
        assert h.route("s1", "w") == ("a", "bbbbbbbb")
        h.drop_route("s1", "w", "b", "bbbbbbbb")  # another runner's reply
        assert h.route("s1", "w") == ("a", "bbbbbbbb")
        h.drop_route("s1", "w", "a", "bbbbbbbb")
        assert h.route("s1", "w") is None

    def test_a_failed_tag_with_no_stderr_still_fails_and_an_unconfirmed_undo_stays_tracked(
        self, tmp_path, monkeypatch
    ):
        class Stuck(FakeLocalBackend):
            def kill_session(self, session):
                return False  # kill not confirmed

        local = Stuck()
        runner = Runner("ws://unused", TOKEN, "a", backend=local, tmp_dir=str(tmp_path))
        runner._owns_tmux = True

        class Failed:
            returncode, stdout, stderr = 1, "", ""

        monkeypatch.setattr(agent_mod.subprocess, "run", lambda *a, **k: Failed())
        with pytest.raises(TerminalBackendError, match="exit status 1"):
            runner.dispatch(
                "create_session",
                {"session_name": "s1", "window_name": "w", "terminal_id": "aaaaaaaa"},
            )
        # Still reported, so the hub reaps it as a terminal with no row.
        assert runner._terminals == {("s1", "w"): "aaaaaaaa"}

    def test_a_failed_owner_tag_undoes_the_create(self, tmp_path, monkeypatch):
        local = FakeLocalBackend()
        runner = Runner("ws://unused", TOKEN, "a", backend=local, tmp_dir=str(tmp_path))
        runner._owns_tmux = True  # as if driving its own tmux

        class Failed:
            returncode, stdout, stderr = 1, "", "no server running"

        monkeypatch.setattr(agent_mod.subprocess, "run", lambda *a, **k: Failed())
        with pytest.raises(TerminalBackendError, match="could not tag"):
            runner.dispatch(
                "create_session",
                {"session_name": "s1", "window_name": "w", "terminal_id": "aaaaaaaa"},
            )
        assert local.windows == {} and runner._terminals == {}


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
class TestRunnerRestartRecovery:
    def test_a_restarted_runner_re_adopts_only_its_own_windows(self, tmp_path, monkeypatch):
        # Short: a unix socket path under pytest's tmp_path overflows on macOS.
        sock_dir = tempfile.mkdtemp(prefix="cao-rt-", dir="/tmp")
        monkeypatch.setenv("TMUX_TMPDIR", sock_dir)
        monkeypatch.delenv("TMUX", raising=False)

        def tmux(*args):
            return subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=10)

        try:
            tmux("new-session", "-d", "-s", "s1", "-n", "mine")
            tmux("set-option", "-w", "-t", "=s1:=mine", "@cao_terminal_id", "aaaaaaaa")
            tmux("set-option", "-w", "-t", "=s1:=mine", RUNNER_ID_OPTION, "a")
            tmux("new-window", "-d", "-t", "=s1", "-n", "local")  # a local CAO's window
            tmux("set-option", "-w", "-t", "=s1:=local", "@cao_terminal_id", "bbbbbbbb")
            tmux("new-window", "-d", "-t", "=s1", "-n", "other")
            tmux("set-option", "-w", "-t", "=s1:=other", "@cao_terminal_id", "cccccccc")
            tmux("set-option", "-w", "-t", "=s1:=other", RUNNER_ID_OPTION, "b")

            runner = Runner("ws://unused", TOKEN, "a", tmp_dir=str(tmp_path / "rt"))
            assert runner.recover_terminals() == 1
            assert runner._terminals == {("s1", "mine"): "aaaaaaaa"}
            runner._tag_owner("s1", "local", created_session=False)
            out = tmux("show-options", "-w", "-v", "-t", "=s1:=local", RUNNER_ID_OPTION)
            assert out.stdout.strip() == "a"
        finally:
            tmux("kill-server")
            shutil.rmtree(sock_dir, ignore_errors=True)

    def test_a_runner_with_an_injected_backend_never_reads_tmux(self, tmp_path):
        runner = Runner(
            "ws://unused", TOKEN, "a", backend=FakeLocalBackend(), tmp_dir=str(tmp_path)
        )
        assert runner.recover_terminals() == 0


# --- A7: unreachable runners read UNKNOWN ------------------------------------


class TestReachability:
    def test_disconnect_reads_unknown_and_reconnect_restores(
        self, monitor, make_hub, runners, tmp_path
    ):
        h = make_hub()
        backend = RemoteBackend()
        backend.attach_hub(h, restore=False)
        h.start()
        a = runners(h, "a", tmp_path / "a")
        wait_for(lambda: "a" in h.connected_runners())
        backend.create_session("s1", "w", "aaaaaaaa")
        with monitor._lock:
            monitor._last_status["aaaaaaaa"] = TerminalStatus.PROCESSING  # last parsed
        assert "aaaaaaaa" not in monitor._unreachable

        a.stop()
        wait_for(lambda: "aaaaaaaa" in monitor._unreachable)
        # Never the stale PROCESSING (or a stale COMPLETED) while out of reach.
        assert monitor.get_status("aaaaaaaa") is TerminalStatus.UNKNOWN
        assert monitor.status_observation("aaaaaaaa").status is TerminalStatus.UNKNOWN

        runners(h, "a", tmp_path / "a2", hosting=dict(a.runner._terminals))
        wait_for(lambda: "aaaaaaaa" not in monitor._unreachable)
        with monitor._lock:
            assert monitor._last_status["aaaaaaaa"] is TerminalStatus.PROCESSING

    def test_an_expired_member_stays_unknown(self, monitor, make_hub, runners, tmp_path):
        h = make_hub(expiry=0.2)
        backend = RemoteBackend()
        backend.attach_hub(h, restore=False)
        h.start()
        a = runners(h, "a", tmp_path / "a")
        wait_for(lambda: "a" in h.connected_runners())
        backend.create_session("s1", "w", "aaaaaaaa")
        a.stop()
        wait_for(lambda: "aaaaaaaa" in monitor._unreachable)
        time.sleep(0.3)
        assert h.can_prove_unrouted_absence() is False  # triggers expiry
        assert h.is_reaping("aaaaaaaa")
        assert monitor.get_status("aaaaaaaa") is TerminalStatus.UNKNOWN

    def test_a_new_runner_process_gets_its_lost_pipes_re_armed(
        self, monitor, make_hub, runners, tmp_path
    ):
        h = make_hub()
        backend = RemoteBackend()
        backend._tmp_dir = str(tmp_path / "srv")
        os.makedirs(backend._tmp_dir)
        backend.attach_hub(h, restore=False)
        h.start()
        a = runners(h, "a", tmp_path / "a")
        wait_for(lambda: "a" in h.connected_runners())
        backend.create_session("s1", "w", "aaaaaaaa")
        backend.pipe_pane("s1", "w", str(tmp_path / "server.fifo"))
        first_pipe = h._pipe_of[("a", "s1", "w")]
        a.stop()
        wait_for(lambda: "a" not in h.connected_runners())

        # A new process: same panes (re-adopted), but none of the pipes.
        b = runners(h, "a", tmp_path / "a2", hosting={("s1", "w"): "aaaaaaaa"})
        wait_for(lambda: ("s1", "w") in b.local.piped)
        wait_for(lambda: h._pipe_of.get(("a", "s1", "w")) not in (None, first_pipe))
        assert b.runner._pipes[("s1", "w")].pipe_id == h._pipe_of[("a", "s1", "w")]

    def test_a_reconnect_of_the_same_process_keeps_its_pipes(
        self, monitor, make_hub, runners, tmp_path
    ):
        h = make_hub()
        backend = RemoteBackend()
        backend._tmp_dir = str(tmp_path / "srv")
        os.makedirs(backend._tmp_dir)
        backend.attach_hub(h, restore=False)
        h.start()
        runners(h, "a", tmp_path / "a")
        wait_for(lambda: "a" in h.connected_runners())
        backend.create_session("s1", "w", "aaaaaaaa")
        backend.pipe_pane("s1", "w", str(tmp_path / "server.fifo"))
        pipe_id = h._pipe_of[("a", "s1", "w")]
        with h._lock:
            assert h._lost_pipes_locked("a", [pipe_id]) == []
            assert h._lost_pipes_locked("a", None) == []  # not listed: assume kept
            assert h._lost_pipes_locked("a", []) == [("s1", "w", str(tmp_path / "server.fifo"))]

    def test_a_pane_its_runner_stops_reporting_reads_unknown(
        self, monitor, make_hub, runners, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(agent_mod, "_INVENTORY_INTERVAL", 0.2)
        h = make_hub()
        RemoteBackend().attach_hub(h, restore=False)
        h.start()
        a = runners(h, "a", tmp_path / "a", hosting={("s1", "w"): "aaaaaaaa"})
        wait_for(lambda: h.route("s1", "w") is not None)
        with monitor._lock:
            monitor._last_status["aaaaaaaa"] = TerminalStatus.PROCESSING
        a.runner._forget(("s1", "w"))  # the window vanished on the runner
        wait_for(lambda: h.route("s1", "w") is None)
        wait_for(lambda: "aaaaaaaa" in monitor._unreachable)
        assert monitor.get_status("aaaaaaaa") is TerminalStatus.UNKNOWN

    def test_working_directory_surfaces_an_unreachable_runner(
        self, monitor, make_hub, runners, tmp_path
    ):
        from cli_agent_orchestrator.runner.protocol import RunnerUnavailable

        h = make_hub()
        backend = RemoteBackend()
        backend.attach_hub(h, restore=False)
        h.start()
        a = runners(h, "a", tmp_path / "a")
        wait_for(lambda: "a" in h.connected_runners())
        backend.create_session("s1", "w", "aaaaaaaa")
        assert backend.get_pane_working_directory("s1", "w") == "/workspace"
        a.stop()
        wait_for(lambda: "a" not in h.connected_runners())
        with pytest.raises(RunnerUnavailable):  # 503, not a 200 with null
            backend.get_pane_working_directory("s1", "w")

    def test_a_displaced_incarnation_loses_its_sink_and_reads_unknown(
        self, monitor, make_hub, runners, tmp_path
    ):
        h = make_hub()
        backend = RemoteBackend()
        backend._tmp_dir = str(tmp_path / "srv")
        os.makedirs(backend._tmp_dir)
        backend.attach_hub(h, restore=False)
        h.start()
        runners(h, "a", tmp_path / "a")
        wait_for(lambda: "a" in h.connected_runners())
        backend.create_session("s1", "w", "aaaaaaaa")
        backend.pipe_pane("s1", "w", str(tmp_path / "server.fifo"))
        old_pipe = h._pipe_of[("a", "s1", "w")]
        # A report now names a different terminal in that window.
        h._on_frame(
            h._conns["a"],
            '{"type": "inventory", "terminals": '
            '[{"session": "s1", "window": "w", "terminal_id": "bbbbbbbb"}], "creating": []}',
        )
        assert h.route("s1", "w") == ("a", "bbbbbbbb")
        assert old_pipe not in h._sinks  # no longer fed from the old incarnation
        wait_for(lambda: "aaaaaaaa" in monitor._unreachable)
        assert "bbbbbbbb" not in monitor._unreachable

    def test_a_failed_re_arm_is_retried(self, monkeypatch):
        from cli_agent_orchestrator.backends import remote_backend as rb_mod

        monkeypatch.setattr(rb_mod, "_REARM_RETRY_DELAY", 0.0)
        backend = RemoteBackend()
        calls = []

        def flaky(session, window, path):
            calls.append(session)
            if len(calls) == 1:
                raise rb_mod.RunnerOutcomeUnknown("lost")

        monkeypatch.setattr(backend, "pipe_pane", flaky)
        backend._rearm_pipes([("s1", "w", "/tmp/f")])
        assert calls == ["s1", "s1"]

    def test_an_expired_runner_stops_keeping_its_session_alive(self, make_hub, runners, tmp_path):
        h = make_hub(expiry=0.3)
        backend = RemoteBackend(hub=h)
        backend._tmp_dir = str(tmp_path / "srv")
        os.makedirs(backend._tmp_dir)
        h.start()
        a = runners(h, "a", tmp_path / "a")
        wait_for(lambda: "a" in h.connected_runners())
        backend.create_session("s1", "w", "aaaaaaaa")
        a.stop()
        wait_for(lambda: "a" not in h.connected_runners())
        assert backend.session_exists("s1") is True  # unreachable, not absent
        time.sleep(0.4)
        # No placement ran in between; the route read itself expires "a".
        assert h.session_runners("s1") == []
        assert h.is_reaping("aaaaaaaa")

    def test_teardown_clears_the_mark(self, monitor):
        monitor.set_unreachable("aaaaaaaa", True)
        monitor.clear_terminal("aaaaaaaa")
        assert "aaaaaaaa" not in monitor._unreachable


# --- A3: token from a file ------------------------------------------------------


class TestToken:
    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch):
        token_mod._reset_for_tests()
        monkeypatch.delenv(token_mod.TOKEN_ENV, raising=False)
        monkeypatch.delenv(token_mod.TOKEN_FILE_ENV, raising=False)
        yield
        token_mod._reset_for_tests()

    def test_the_file_wins_and_both_variables_are_removed(self, tmp_path, monkeypatch):
        f = tmp_path / "token"
        f.write_text("from-file\n")
        monkeypatch.setenv(token_mod.TOKEN_ENV, "from-env")
        monkeypatch.setenv(token_mod.TOKEN_FILE_ENV, str(f))
        assert token_mod.runner_token() == "from-file"
        assert token_mod.TOKEN_ENV not in os.environ
        assert token_mod.TOKEN_FILE_ENV not in os.environ
        assert token_mod.runner_token() == "from-file"  # read once

    def test_the_environment_alone_still_works(self, monkeypatch):
        monkeypatch.setenv(token_mod.TOKEN_ENV, "from-env")
        assert token_mod.runner_token() == "from-env"
        assert token_mod.TOKEN_ENV not in os.environ

    def test_an_unreadable_file_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.setenv(token_mod.TOKEN_ENV, "from-env")
        monkeypatch.setenv(token_mod.TOKEN_FILE_ENV, str(tmp_path / "missing"))
        assert token_mod.runner_token() is None  # never the env fallback

    def test_the_runner_refuses_to_start_without_one(self, monkeypatch):
        with pytest.raises(SystemExit):
            agent_mod.main(["--server-url", "ws://127.0.0.1:1"])

    def test_the_hub_refuses_an_empty_token(self):
        with pytest.raises(ValueError):
            RunnerHub(token="")


# --- A4: readiness file ------------------------------------------------------


class TestReadyFile:
    def test_present_only_while_connected(self, make_hub, runners, tmp_path):
        ready = tmp_path / "ready"
        ready.write_text("stale")  # left by a previous process
        h = make_hub()
        r = RunnerThread("ws://127.0.0.1:1", "a", str(tmp_path / "a"), ready_file=str(ready))
        try:
            wait_for(lambda: not ready.exists())  # unreachable hub: not ready
        finally:
            r.stop()
        h.start()
        r = runners(h, "a", tmp_path / "a", ready_file=str(ready))
        wait_for(lambda: ready.exists())
        assert ready.read_text().strip() == "a"
        h.stop()
        wait_for(lambda: not ready.exists())


# --- A5: wss:// -------------------------------------------------------------


def _self_signed(tmp_path):
    if shutil.which("openssl") is None:
        pytest.skip("openssl not installed")
    cert, key = tmp_path / "hub.crt", tmp_path / "hub.key"
    out = subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        capture_output=True,
        timeout=60,
    )
    if out.returncode != 0:
        pytest.skip(f"openssl could not make a certificate: {out.stderr!r}")
    return cert, key


class TestTls:
    def test_a_runner_connects_over_wss_with_the_configured_ca(
        self, make_hub, tmp_path, monkeypatch
    ):
        cert, key = _self_signed(tmp_path)
        monkeypatch.setenv(token_mod.TLS_CERT_ENV, str(cert))
        monkeypatch.setenv(token_mod.TLS_KEY_ENV, str(key))
        h = make_hub(ssl_context=token_mod.runner_hub_ssl_context())
        h.start()
        trusted = RunnerThread(
            f"wss://127.0.0.1:{h.port}", "a", str(tmp_path / "a"), ca_file=str(cert)
        )
        untrusted = RunnerThread(f"wss://127.0.0.1:{h.port}", "b", str(tmp_path / "b"))
        plain = RunnerThread(f"ws://127.0.0.1:{h.port}", "c", str(tmp_path / "c"))
        try:
            wait_for(lambda: "a" in h.connected_runners())
            time.sleep(1.0)
            assert h.connected_runners() == ["a"]  # unverified and plaintext refused
        finally:
            for r in (trusted, untrusted, plain):
                r.stop()

    def test_a_key_without_a_certificate_is_an_error(self, tmp_path, monkeypatch):
        monkeypatch.delenv(token_mod.TLS_CERT_ENV, raising=False)
        monkeypatch.setenv(token_mod.TLS_KEY_ENV, str(tmp_path / "k"))
        with pytest.raises(ValueError):
            token_mod.runner_hub_ssl_context()

    def test_no_tls_configured_means_plain_ws(self, monkeypatch):
        monkeypatch.delenv(token_mod.TLS_CERT_ENV, raising=False)
        monkeypatch.delenv(token_mod.TLS_KEY_ENV, raising=False)
        assert token_mod.runner_hub_ssl_context() is None


def test_module_constants_used_above_exist():
    assert hub_mod._UNOWNED_REPORTS_BEFORE_REAP == 2

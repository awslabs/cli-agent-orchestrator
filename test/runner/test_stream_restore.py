"""Remote terminals get their output streams back after a restart (#745).

A cao-server restart leaves every surviving remote terminal with no FIFO
reader and no runner pipe; a runner restart leaves the pipes the hub knows
unserved. Either way the status derived before the outage is stale, so the
terminal is latched UNKNOWN (a stream gap) before it is made reachable, and a
resync recreates the reader, re-arms the pipe and processes a fresh snapshot.
A teardown and a restore never interleave.
"""

import threading
import time

import pytest

from cli_agent_orchestrator.backends import remote_backend as rb_mod
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.runner import hub as hub_mod
from cli_agent_orchestrator.runner.hub import RunnerHub
from cli_agent_orchestrator.services import status_monitor as status_mod
from cli_agent_orchestrator.services import terminal_service

TOKEN = "t" * 32


class _Ready:
    def get_status(self, buffer):
        return TerminalStatus.IDLE


class _PipeBackend:
    def supports_event_inbox(self):
        return False


@pytest.fixture
def monitor(monkeypatch):
    fresh = status_mod.StatusMonitor()
    monkeypatch.setattr(status_mod, "status_monitor", fresh)
    monkeypatch.setattr(status_mod.provider_manager, "get_provider", lambda tid: _Ready())
    monkeypatch.setattr(
        # The singleton, not get_backend: a module first imported while the
        # function is patched keeps the patched function for the session.
        "cli_agent_orchestrator.backends.registry._backend",
        _PipeBackend(),
    )
    return fresh


def _hello_report(h, runner_id, terminals, pipes):
    """What _handle does with a hello, minus the socket."""
    with h._changed:
        h._conns[runner_id] = hub_mod._Connection(
            info=hub_mod.RunnerInfo(runner_id, frozenset(), True, 4, "/tmp"), ws=None
        )
        report = h._replace_runner_routes(runner_id, terminals, [])
        restore = h._streams_to_restore_locked(runner_id, pipes, report)
    for terminal_id in restore:
        h._report_stream_gap(terminal_id)
    h._after_report(runner_id, report)
    return restore


class TestTheHello:
    def test_a_seeded_terminal_is_latched_before_it_is_made_reachable(self):
        h = RunnerHub(token=TOKEN, host="127.0.0.1", port=0)
        events = []
        h.bind(
            stream_gap=lambda tid: events.append(("gap", tid)),
            reachability=lambda ids, ok: events.append(("reachable" if ok else "out", *ids)),
        )
        # A restarted server: the row seeds the route; there is no pipe at all.
        h.seed_placements(
            [{"id": "aaaaaaaa", "tmux_session": "s1", "tmux_window": "w", "runner_id": "a"}]
        )
        events.clear()
        restore = _hello_report(
            h, "a", [{"session": "s1", "window": "w", "terminal_id": "aaaaaaaa"}], []
        )
        assert restore == ["aaaaaaaa"]
        assert events == [("gap", "aaaaaaaa"), ("reachable", "aaaaaaaa")]

    def test_a_terminal_whose_pipe_survived_is_not_resynced(self, tmp_path):
        h = RunnerHub(token=TOKEN, host="127.0.0.1", port=0)
        gaps = []
        h.bind(stream_gap=gaps.append)
        h.add_route("s1", "w", "a", "aaaaaaaa")
        pipe_id = h.open_pipe("a", "s1", "w", str(tmp_path / "x.fifo"), "aaaaaaaa")
        try:
            restore = _hello_report(
                h, "a", [{"session": "s1", "window": "w", "terminal_id": "aaaaaaaa"}], [pipe_id]
            )
        finally:
            h.close_pipe("a", "s1", "w")
        assert restore == [] and gaps == []

    def test_a_terminal_still_being_created_is_left_to_its_create(self):
        h = RunnerHub(token=TOKEN, host="127.0.0.1", port=0)
        gaps = []
        h.bind(stream_gap=gaps.append)
        h.note_placing("aaaaaaaa", "a")  # created, its pipe not armed yet
        restore = _hello_report(
            h, "a", [{"session": "s1", "window": "w", "terminal_id": "aaaaaaaa"}], []
        )
        assert restore == [] and gaps == []

    def test_a_restarted_runner_never_exposes_a_stale_idle(self, monitor, monkeypatch):
        # The terminal was IDLE when its runner died; output produced since is lost.
        monitor._process_chunk("aaaaaaaa", "ready")
        monitor._apply_detection("aaaaaaaa", TerminalStatus.IDLE)
        monitor.set_unreachable("aaaaaaaa", True)
        monkeypatch.setattr(rb_mod, "_request_resync", lambda tid: None)  # resync not done yet
        h = RunnerHub(token=TOKEN, host="127.0.0.1", port=0)
        rb_mod.RemoteBackend(hub=h)
        h.bind(stream_gap=rb_mod._mark_stream_gap, reachability=rb_mod._mark_reachability)
        h.add_route("s1", "w", "a", "aaaaaaaa")
        h._pipe_of[("a", "s1", "w")] = "old-pipe"  # the dead process's pipe
        _hello_report(h, "a", [{"session": "s1", "window": "w", "terminal_id": "aaaaaaaa"}], [])
        assert "aaaaaaaa" not in monitor._unreachable  # reachable again...
        assert monitor.get_status("aaaaaaaa") is TerminalStatus.UNKNOWN  # ...but not trusted


class TestReaderRestore:
    @pytest.fixture
    def fifo(self, monkeypatch):
        created = []

        class Manager:
            readers = set()

            def has_reader(self, terminal_id):
                return terminal_id in self.readers

            def create_reader(self, terminal_id, pane_probe=None, rearm=None):
                assert callable(pane_probe) and callable(rearm)
                self.readers.add(terminal_id)
                created.append(terminal_id)

        manager = Manager()
        monkeypatch.setattr(terminal_service, "fifo_manager", manager)
        monkeypatch.setattr(
            terminal_service,
            "get_terminal_metadata",
            lambda tid: (
                {"id": tid, "tmux_session": "s1", "tmux_window": "w"} if tid != "gone0000" else None
            ),
        )
        yield created
        terminal_service.forget_stream_lifecycle("aaaaaaaa")

    def test_a_missing_reader_is_recreated_once(self, fifo):
        assert terminal_service.ensure_stream_reader("aaaaaaaa") is True
        assert terminal_service.ensure_stream_reader("aaaaaaaa") is True
        assert fifo == ["aaaaaaaa"]

    def test_no_reader_for_a_terminal_without_a_row(self, fifo):
        assert terminal_service.ensure_stream_reader("gone0000") is False
        assert fifo == []

    def test_no_reader_once_a_teardown_has_begun(self, fifo):
        terminal_service.mark_stream_closing("aaaaaaaa")
        assert terminal_service.ensure_stream_reader("aaaaaaaa") is False
        assert fifo == []

    def test_a_teardown_waits_for_a_restore_in_progress_and_none_follows(self, fifo, monkeypatch):
        order = []
        monkeypatch.setattr(terminal_service, "get_backend", lambda: _Teardown(order))
        monkeypatch.setattr(
            terminal_service.status_monitor, "clear_terminal", lambda tid: order.append("clear")
        )
        restoring = threading.Event()
        release = threading.Event()

        def restore():
            with terminal_service.stream_lifecycle("aaaaaaaa"):
                assert terminal_service.ensure_stream_reader("aaaaaaaa")
                order.append("restored")
                restoring.set()
                release.wait(5)

        worker = threading.Thread(target=restore)
        worker.start()
        restoring.wait(5)
        teardown = threading.Thread(
            target=terminal_service.dismantle_terminal_runtime,
            args=("aaaaaaaa", {"tmux_session": "s1", "tmux_window": "w"}),
        )
        teardown.start()
        time.sleep(0.2)
        assert "stop_pipe" not in order  # blocked behind the restore
        release.set()
        worker.join(5)
        teardown.join(5)
        assert order.index("restored") < order.index("stop_pipe")
        # And a restore arriving after the teardown began creates nothing.
        fifo.clear()
        with terminal_service.stream_lifecycle("aaaaaaaa"):
            assert terminal_service.ensure_stream_reader("aaaaaaaa") is False
        assert fifo == []


class _Teardown:
    def __init__(self, order):
        self.order = order

    def supports_event_inbox(self):
        return False

    def stop_pipe_pane(self, session, window):
        self.order.append("stop_pipe")

    def kill_window(self, session, window):
        self.order.append("kill")

    def __getattr__(self, name):
        return lambda *a, **k: None


class TestNoLeftovers:
    def test_a_restore_refused_for_a_closing_terminal_frees_its_worker_slot(
        self, monitor, monkeypatch
    ):
        monkeypatch.setattr(rb_mod, "_resyncing", set())
        monkeypatch.setattr(rb_mod, "_resync_again", set())
        monkeypatch.setattr(terminal_service, "ensure_stream_reader", lambda tid: False)
        monitor.mark_stream_gap("cccccccc")
        rb_mod._request_resync("cccccccc")
        for _ in range(200):
            with rb_mod._resync_lock:
                if not rb_mod._resyncing:
                    break
            time.sleep(0.01)
        assert rb_mod._resyncing == set() and rb_mod._resync_again == set()

    def test_deleting_a_terminal_that_has_no_row_leaves_no_lifecycle_state(self, monkeypatch):
        monkeypatch.setattr(terminal_service, "db_delete_terminal", lambda tid, **kw: False)
        monkeypatch.setattr(terminal_service, "terminal_exists", lambda tid: False)
        with terminal_service.stream_lifecycle("dddddddd"):
            terminal_service.mark_stream_closing("dddddddd")
        terminal_service.delete_terminal_row("dddddddd", None, end_reason="deleted")
        assert "dddddddd" not in terminal_service._STREAMS_CLOSING
        assert "dddddddd" not in terminal_service._STREAM_LIFECYCLE_LOCKS

    def test_a_retained_row_keeps_its_closing_marker(self, monkeypatch):
        # A deferred teardown keeps the row as its retry handle: still closing.
        monkeypatch.setattr(terminal_service, "db_delete_terminal", lambda tid, **kw: False)
        monkeypatch.setattr(terminal_service, "terminal_exists", lambda tid: True)
        terminal_service.mark_stream_closing("eeeeeeee")
        try:
            terminal_service.delete_terminal_row("eeeeeeee", None, end_reason="deleted")
            assert "eeeeeeee" in terminal_service._STREAMS_CLOSING
        finally:
            terminal_service.forget_stream_lifecycle("eeeeeeee")

    def test_a_flow_recycle_sweep_forgets_the_swept_terminals(self, monkeypatch):
        from cli_agent_orchestrator.services import flow_service

        monkeypatch.setattr(flow_service, "delete_terminals_by_ids", lambda ids, **kw: len(ids))
        monkeypatch.setattr(flow_service.fifo_manager, "stop_reader", lambda tid: True)
        for terminal_id in ("ffff0001", "ffff0002"):
            flow_service._stop_recycled_stream(terminal_id)  # as the recycle does
            assert terminal_id in terminal_service._STREAMS_CLOSING
        flow_service._delete_recycled_terminal_rows(["ffff0001", "ffff0002"])
        assert not {"ffff0001", "ffff0002"} & terminal_service._STREAMS_CLOSING

    def test_a_flow_recycle_waits_for_a_restore_in_progress_and_none_follows(self, monkeypatch):
        from cli_agent_orchestrator.services import flow_service

        order = []
        monkeypatch.setattr(
            flow_service.fifo_manager, "stop_reader", lambda tid: order.append("stop") or True
        )
        monkeypatch.setattr(
            terminal_service,
            "get_terminal_metadata",
            lambda tid: {"id": tid, "tmux_session": "s1", "tmux_window": "w"},
        )
        monkeypatch.setattr(terminal_service.fifo_manager, "has_reader", lambda tid: True)
        restoring, release = threading.Event(), threading.Event()

        def restore():
            with terminal_service.stream_lifecycle("ffff0003"):
                assert terminal_service.ensure_stream_reader("ffff0003")
                order.append("restored")
                restoring.set()
                release.wait(5)

        worker = threading.Thread(target=restore)
        worker.start()
        restoring.wait(5)
        recycle = threading.Thread(target=flow_service._stop_recycled_stream, args=("ffff0003",))
        recycle.start()
        time.sleep(0.2)
        assert order == ["restored"]  # the recycle waits
        release.set()
        worker.join(5)
        recycle.join(5)
        assert order == ["restored", "stop"]
        with terminal_service.stream_lifecycle("ffff0003"):
            assert terminal_service.ensure_stream_reader("ffff0003") is False
        terminal_service.forget_stream_lifecycle("ffff0003")

    def test_a_failing_restore_frees_both_worker_registries(self, monitor, monkeypatch):
        monkeypatch.setattr(rb_mod, "_resyncing", set())
        monkeypatch.setattr(rb_mod, "_resync_again", set())
        calls = []

        def boom(tid):
            calls.append(tid)
            rb_mod._request_resync(tid)  # another gap arrives meanwhile
            raise RuntimeError("database is locked")

        monkeypatch.setattr(terminal_service, "ensure_stream_reader", boom)
        monitor.mark_stream_gap("gggggggg")
        with pytest.raises(RuntimeError):
            with rb_mod._resync_lock:
                rb_mod._resyncing.add("gggggggg")
            rb_mod._resync_worker("gggggggg")
        assert calls == ["gggggggg"]
        assert rb_mod._resyncing == set() and rb_mod._resync_again == set()

    @pytest.mark.parametrize("failure", ["false", "raises"])
    def test_a_reader_that_does_not_stop_is_reported(self, monkeypatch, failure):
        from cli_agent_orchestrator.services import flow_service

        def stop(tid):
            if failure == "raises":
                raise RuntimeError("join timed out")
            return False

        monkeypatch.setattr(flow_service.fifo_manager, "stop_reader", stop)
        assert flow_service._stop_recycled_stream("ffff0004") is False
        assert "ffff0004" in terminal_service._STREAMS_CLOSING  # still guarded
        terminal_service.forget_stream_lifecycle("ffff0004")

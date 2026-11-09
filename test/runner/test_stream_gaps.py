"""A gap in a remote terminal's output stream is explicit (#745).

A runner pipe that overflows or skips a sequence number, or an output event the
status monitor's queue drops, means the status derived from the stream may be
built on incomplete output. The terminal then reads UNKNOWN, latched, until a
resync (a fresh pane snapshot, processed directly) clears it. Reachability is a
separate state: a runner reporting the terminal again does not clear the latch.
"""

import asyncio
import base64
import json
import os
import queue

import pytest

from cli_agent_orchestrator.backends import remote_backend as rb_mod
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.runner import hub as hub_mod
from cli_agent_orchestrator.runner.hub import RunnerHub
from cli_agent_orchestrator.services import event_bus as bus_mod
from cli_agent_orchestrator.services import fifo_reader as fifo_mod
from cli_agent_orchestrator.services import status_monitor as status_mod

TOKEN = "t" * 32


class _ReadyWhenSeen:
    """A provider whose screen reads IDLE while it ends at the ``ready>`` prompt."""

    def get_status(self, buffer):
        if buffer.rstrip().endswith("ready>"):
            return TerminalStatus.IDLE
        return TerminalStatus.PROCESSING


class _PipeBackend:
    def supports_event_inbox(self):
        return False


@pytest.fixture
def monitor(monkeypatch):
    fresh = status_mod.StatusMonitor()
    monkeypatch.setattr(status_mod, "status_monitor", fresh)
    monkeypatch.setattr(status_mod.provider_manager, "get_provider", lambda tid: _ReadyWhenSeen())
    monkeypatch.setattr(
        "cli_agent_orchestrator.backends.registry.get_backend", lambda: _PipeBackend()
    )
    return fresh


def _idle(monitor, terminal_id="aaaaaaaa"):
    monitor._process_chunk(terminal_id, "ready>")
    monitor._apply_detection(terminal_id, TerminalStatus.IDLE)
    assert monitor.get_status(terminal_id) is TerminalStatus.IDLE


# --- the latch -----------------------------------------------------------------


class TestTheLatch:
    def test_a_gap_reads_unknown_until_a_resync_clears_it(self, monitor):
        _idle(monitor)
        generation = monitor.mark_stream_gap("aaaaaaaa")
        assert monitor.get_status("aaaaaaaa") is TerminalStatus.UNKNOWN
        assert monitor.status_observation("aaaaaaaa").status is TerminalStatus.UNKNOWN
        assert monitor.resync_from_snapshot("aaaaaaaa", "ready>", generation) is True
        assert monitor.get_status("aaaaaaaa") is TerminalStatus.IDLE

    def test_a_report_of_the_terminal_does_not_clear_the_latch(self, monitor):
        _idle(monitor)
        monitor.mark_stream_gap("aaaaaaaa")
        monitor.set_unreachable("aaaaaaaa", True)  # its runner went away...
        monitor.set_unreachable("aaaaaaaa", False)  # ...and reported it again
        assert monitor.get_status("aaaaaaaa") is TerminalStatus.UNKNOWN

    def test_a_gap_during_a_resync_keeps_the_latch(self, monitor):
        _idle(monitor)
        stale = monitor.mark_stream_gap("aaaaaaaa")
        monitor.mark_stream_gap("aaaaaaaa")  # another one, while the snapshot was taken
        assert monitor.resync_from_snapshot("aaaaaaaa", "ready>", stale) is False
        assert monitor.get_status("aaaaaaaa") is TerminalStatus.UNKNOWN

    def test_the_resync_status_is_detected_from_the_snapshot_not_the_old_stream(self, monitor):
        _idle(monitor)
        generation = monitor.mark_stream_gap("aaaaaaaa")
        # The agent went to work during the gap; the snapshot shows it.
        monitor.notify_input_sent("aaaaaaaa")
        monitor.resync_from_snapshot("aaaaaaaa", "\r\nworking...", generation)
        assert monitor.get_status("aaaaaaaa") is TerminalStatus.PROCESSING

    def test_the_resync_publishes_the_snapshots_status_not_the_stale_one(
        self, monitor, monkeypatch
    ):
        # A screen-detecting provider (the default for one that opts in, such
        # as the default remote provider) is detected on the event loop. A loop that never runs
        # stands in for that wait: only the resync's own detection can replace
        # the PROCESSING derived before the gap, and the status it publishes is
        # what a waiter on the status topic acts on.
        class Screen(_ReadyWhenSeen):
            supports_screen_detection = True

            def get_status_from_screen(self, lines):
                return TerminalStatus.PROCESSING  # the screen pass has not settled

        published = []
        monkeypatch.setattr(status_mod, "CAO_PYTE_STATUS", True)
        monkeypatch.setattr(status_mod.provider_manager, "get_provider", lambda tid: Screen())
        monkeypatch.setattr(
            status_mod.bus, "publish", lambda topic, data: published.append((topic, data))
        )
        loop = asyncio.new_event_loop()
        try:
            monitor._loop = loop
            monitor._process_chunk("aaaaaaaa", "working...")
            monitor._apply_detection("aaaaaaaa", TerminalStatus.PROCESSING)
            generation = monitor.mark_stream_gap("aaaaaaaa")
            # The agent finished during the gap; the snapshot shows its prompt.
            assert monitor.resync_from_snapshot("aaaaaaaa", "\r\nready>", generation)
            assert monitor._last_status["aaaaaaaa"] is TerminalStatus.IDLE
            assert published[-1] == ("terminal.aaaaaaaa.status", {"status": "idle"})
        finally:
            monitor._loop = None
            loop.close()

    def test_clearing_a_terminal_forgets_its_gap(self, monitor):
        monitor.mark_stream_gap("aaaaaaaa")
        monitor.clear_terminal("aaaaaaaa")
        assert monitor.stream_gap_generation("aaaaaaaa") is None

    def test_a_latched_gap_asks_for_a_resync(self, monitor):
        asked = []
        monitor.on_stream_gap = asked.append
        monitor.mark_stream_gap("aaaaaaaa")
        assert asked == ["aaaaaaaa"]


# --- where gaps come from --------------------------------------------------------


class TestGapSources:
    def test_a_sink_overflow_reports_its_terminal(self, tmp_path):
        gaps = []
        sink = hub_mod._PipeSink(
            "a", "s1:w", str(tmp_path / "missing.fifo"), terminal_id="aaaaaaaa", on_gap=gaps.append
        )
        try:
            sink._queue = queue.Queue(maxsize=1)
            sink._queue.put_nowait(b"x")
            sink.feed(b"lost")
        finally:
            sink.close()
        assert sink.dropped_chunks == 1
        assert gaps == ["aaaaaaaa"]

    def test_a_skipped_sequence_number_reports_its_terminal(self, tmp_path):
        gaps = []
        h = RunnerHub(token=TOKEN, host="127.0.0.1", port=0)
        h.bind(stream_gap=gaps.append)
        h.add_route("s1", "w", "a", "aaaaaaaa")
        fed = []

        class Sink:
            runner_id = "a"
            terminal_id = "aaaaaaaa"

            def feed(self, data):
                fed.append(data)

            def report_gap(self):
                gaps.append(self.terminal_id)

        h._sinks["p1"] = Sink()
        payload = base64.b64encode(b"hi").decode()
        h._on_output("a", {"pipe_id": "p1", "seq": 1, "data": payload})
        assert gaps == []
        h._on_output("a", {"pipe_id": "p1", "seq": 3, "data": payload})  # seq 2 is gone
        assert gaps == ["aaaaaaaa"]
        assert fed == [b"hi", b"hi"]  # what did arrive is still written

    def test_a_pipe_sink_carries_its_routed_terminal(self, tmp_path):
        gaps = []
        h = RunnerHub(token=TOKEN, host="127.0.0.1", port=0)
        h.bind(stream_gap=gaps.append)
        h.add_route("s1", "w", "a", "aaaaaaaa")
        pipe_id = h.open_pipe("a", "s1", "w", str(tmp_path / "x.fifo"))
        try:
            sink = h._sinks[pipe_id]
            assert sink.terminal_id == "aaaaaaaa"
            sink.report_gap()
            assert gaps == ["aaaaaaaa"]
        finally:
            h.close_pipe("a", "s1", "w")

    def test_a_dropped_bus_event_latches_only_a_tracked_terminal(self, monitor, monkeypatch):
        monkeypatch.setattr(bus_mod, "get_server_settings", lambda: {"event_bus_max_queue_size": 1})
        bus = bus_mod.EventBus()
        q = bus.subscribe("terminal.*.output", on_drop=monitor._on_output_dropped)
        monitor.track_stream_gaps("aaaaaaaa")
        for terminal_id in ("aaaaaaaa", "bbbbbbbb"):
            bus._dispatch(f"terminal.{terminal_id}.output", {"data": "1"})
            bus._dispatch(f"terminal.{terminal_id}.output", {"data": "2"})  # queue full
            while not q.empty():
                q.get_nowait()
        assert monitor.stream_gap_generation("aaaaaaaa") is not None
        assert monitor.stream_gap_generation("bbbbbbbb") is None  # e.g. a local tmux pane
        bus.unsubscribe("terminal.*.output", q)
        assert bus._on_drop == {}

    def test_the_monitor_subscribes_with_a_drop_callback(self, monitor, monkeypatch):
        seen = {}

        def subscribe(pattern, on_drop=None):
            seen["on_drop"] = on_drop
            raise asyncio.CancelledError  # stop the loop at once

        monkeypatch.setattr(status_mod.bus, "subscribe", subscribe)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(monitor.run())
        assert seen["on_drop"] == monitor._on_output_dropped


# --- the resync --------------------------------------------------------------------


class TestResync:
    @pytest.fixture
    def manager(self, monkeypatch):
        manager = fifo_mod.FifoManager()
        calls = []
        manager._pane_probe["aaaaaaaa"] = lambda: calls.append("probe") or "ready>"
        manager._rearm["aaaaaaaa"] = lambda: calls.append("rearm")
        manager.calls = calls
        return manager

    def test_it_re_arms_captures_and_clears(self, monitor, manager):
        _idle(monitor)
        monitor.mark_stream_gap("aaaaaaaa")
        assert manager.resync("aaaaaaaa") is True
        assert manager.calls == ["rearm", "probe"]  # re-armed before the snapshot
        assert monitor.get_status("aaaaaaaa") is TerminalStatus.IDLE

    def test_a_gap_while_capturing_keeps_the_latch(self, monitor, manager):
        _idle(monitor)
        monitor.mark_stream_gap("aaaaaaaa")

        def probe():
            monitor.mark_stream_gap("aaaaaaaa")
            return "ready>"

        manager._pane_probe["aaaaaaaa"] = probe
        assert manager.resync("aaaaaaaa") is False
        assert monitor.get_status("aaaaaaaa") is TerminalStatus.UNKNOWN

    def test_a_failed_capture_keeps_the_latch(self, monitor, manager):
        monitor.mark_stream_gap("aaaaaaaa")

        def broken():
            raise RuntimeError("runner unavailable")

        manager._pane_probe["aaaaaaaa"] = broken
        assert manager.resync("aaaaaaaa") is False
        assert monitor.get_status("aaaaaaaa") is TerminalStatus.UNKNOWN

    def test_nothing_to_resync_without_a_gap_or_a_probe(self, monitor, manager):
        assert manager.resync("aaaaaaaa") is False  # no gap
        monitor.mark_stream_gap("bbbbbbbb")
        assert manager.resync("bbbbbbbb") is False  # no probe
        assert manager.calls == []

    def test_the_backend_retries_until_the_resync_lands(self, monitor, manager, monkeypatch):
        monkeypatch.setattr(fifo_mod, "fifo_manager", manager)
        monkeypatch.setattr(rb_mod, "_RESYNC_RETRY_DELAY", 0.0)
        attempts = []
        real = manager.resync

        def flaky(terminal_id):
            attempts.append(terminal_id)
            return real(terminal_id) if len(attempts) > 2 else False

        manager.resync = flaky
        _idle(monitor)
        monitor.on_stream_gap = rb_mod._request_resync
        monitor.mark_stream_gap("aaaaaaaa")
        for _ in range(200):
            if monitor.stream_gap_generation("aaaaaaaa") is None:
                break
            import time

            time.sleep(0.01)
        assert monitor.get_status("aaaaaaaa") is TerminalStatus.IDLE
        assert attempts == ["aaaaaaaa"] * 3
        assert "aaaaaaaa" not in rb_mod._resyncing


def test_a_gap_frame_reaches_the_status_monitor_through_the_backend(monitor, tmp_path):
    """End to end inside the server: a sequence gap on a routed pipe latches UNKNOWN."""
    h = RunnerHub(token=TOKEN, host="127.0.0.1", port=0)
    backend = rb_mod.RemoteBackend()
    backend.attach_hub(h, restore=False)
    monitor.on_stream_gap = None  # no resync here: only the latch is under test
    h.add_route("s1", "w", "a", "aaaaaaaa")
    pipe_id = h.open_pipe("a", "s1", "w", str(tmp_path / "x.fifo"), "aaaaaaaa")
    try:
        _idle(monitor)
        for seq in (1, 3):
            frame = {
                "type": "output",
                "pipe_id": pipe_id,
                "seq": seq,
                "data": base64.b64encode(b"x").decode(),
            }
            h._on_output("a", json.loads(json.dumps(frame)))
        assert monitor.get_status("aaaaaaaa") is TerminalStatus.UNKNOWN
    finally:
        h.close_pipe("a", "s1", "w")


class TestMoreLossPaths:
    def test_an_undecodable_frame_is_a_gap(self):
        gaps = []
        h = RunnerHub(token=TOKEN, host="127.0.0.1", port=0)

        class Sink:
            runner_id = "a"
            terminal_id = "aaaaaaaa"

            def feed(self, data):
                pass

            def report_gap(self):
                gaps.append(self.terminal_id)

        h._sinks["p1"] = Sink()
        h._on_output("a", {"pipe_id": "p1", "seq": 1, "data": "not base64!"})
        assert gaps == ["aaaaaaaa"]

    def test_a_sink_that_cannot_open_its_fifo_is_a_gap(self, tmp_path, monkeypatch):
        gaps = []
        sink = hub_mod._PipeSink(
            "a", "s1:w", str(tmp_path / "x.fifo"), terminal_id="aaaaaaaa", on_gap=gaps.append
        )
        monkeypatch.setattr(sink, "_open", lambda: None)
        sink.feed(b"x")
        sink.join(5)
        assert gaps == ["aaaaaaaa"]

    def test_a_sink_whose_reader_went_away_is_a_gap(self, tmp_path, monkeypatch):
        gaps = []
        sink = hub_mod._PipeSink(
            "a", "s1:w", str(tmp_path / "x.fifo"), terminal_id="aaaaaaaa", on_gap=gaps.append
        )
        monkeypatch.setattr(sink, "_open", lambda: os.open(os.devnull, os.O_WRONLY))
        monkeypatch.setattr(sink, "_write_all", lambda fd, chunk: False)
        sink.feed(b"x")
        sink.join(5)
        assert gaps == ["aaaaaaaa"]

    def test_a_writer_that_raises_is_a_gap(self, tmp_path, monkeypatch):
        gaps = []
        sink = hub_mod._PipeSink(
            "a", "s1:w", str(tmp_path / "x.fifo"), terminal_id="aaaaaaaa", on_gap=gaps.append
        )
        monkeypatch.setattr(sink, "_open", lambda: os.open(os.devnull, os.O_WRONLY))

        def broken(fd, chunk):
            raise OSError("select failed")

        monkeypatch.setattr(sink, "_write_all", broken)
        sink.feed(b"x")
        sink.join(5)
        assert gaps == ["aaaaaaaa"]

    def test_closing_a_sink_on_purpose_is_not_a_gap(self, tmp_path, monkeypatch):
        gaps = []
        sink = hub_mod._PipeSink(
            "a", "s1:w", str(tmp_path / "x.fifo"), terminal_id="aaaaaaaa", on_gap=gaps.append
        )
        sink.close()
        sink._lost_stream()
        sink.join(5)
        assert gaps == []


def test_a_gapped_terminal_whose_runner_reports_it_again_is_resynced(monitor, monkeypatch):
    asked = []
    monkeypatch.setattr(rb_mod, "_request_resync", asked.append)
    monitor.mark_stream_gap("aaaaaaaa")
    rb_mod._mark_reachability(["aaaaaaaa", "bbbbbbbb"], True)
    assert asked == ["aaaaaaaa"]  # bbbbbbbb has no gap
    rb_mod._mark_reachability(["aaaaaaaa"], False)
    assert asked == ["aaaaaaaa"]  # going out of reach starts nothing

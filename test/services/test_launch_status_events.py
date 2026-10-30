"""Issue #1082 busy time: a status transition is persisted for BOTH backends --
tmux via StatusMonitor._apply_detection's sticky-latch pipeline, and herdr via
the agent_status herdr reports for a managed pane on its event stream
(HerdrInboxService), never by sampling whoever happens to poll get_status().
A recording failure must never affect the status either path delivers."""

import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import herdr_inbox_service, status_monitor
from cli_agent_orchestrator.services.herdr_inbox_service import HerdrInboxService
from cli_agent_orchestrator.services.status_monitor import StatusMonitor

pytestmark = pytest.mark.usefixtures("isolated_memory_db")

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)


def _backend(event_inbox):
    backend = MagicMock()
    backend.supports_event_inbox.return_value = event_inbox
    return backend


def _rows(terminal_id):
    from cli_agent_orchestrator.clients import database

    with database.SessionLocal() as db:
        return (
            db.query(database.LaunchStatusEventModel)
            .filter_by(terminal_id=terminal_id)
            .order_by(database.LaunchStatusEventModel.id.asc())
            .all()
        )


def _drain_tmux_status_writes():
    """Wait for every queued tmux status write: the writer is one FIFO thread."""
    status_monitor._status_event_executor().submit(lambda: None).result(timeout=10)


def _on_loop(fn):
    """Run ``fn`` on a running event loop's own thread, as the quiescence
    handlers run ``_apply_detection``. Returns (fn's result, loop thread id)."""

    async def run():
        return fn(), threading.get_ident()

    return asyncio.run(run())


def _pane_event(pane_id, agent_status):
    frame = {"event": "pane_updated", "data": {"pane": {"pane_id": pane_id}}}
    if agent_status is not None:
        frame["data"]["pane"]["agent_status"] = agent_status
    return (json.dumps(frame) + "\n").encode()


def _feed(service, frames, at_seconds=None, monkeypatch=None):
    """Run ``_event_loop`` over ``frames``; event i is observed at T0 + at_seconds[i]."""
    if at_seconds is not None:
        times = iter(T0 + timedelta(seconds=s) for s in at_seconds)
        monkeypatch.setattr(herdr_inbox_service, "_utcnow", lambda: next(times))

    async def run():
        reader = asyncio.StreamReader()
        service._reader = reader
        reader.feed_data(b"".join(frames))
        reader.feed_eof()
        with pytest.raises(ConnectionError):
            await service._event_loop()

    asyncio.run(run())


def _service(callback=None):
    service = HerdrInboxService(socket_path="/tmp/test.sock", delivery_callback=callback)
    service.register_terminal("h1", "pane-1")
    return service


class TestTmuxBusyTimeRecording:
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_status_change_persists_a_transition(self, mock_get_backend):
        mock_get_backend.return_value = _backend(event_inbox=False)
        sm = StatusMonitor()

        assert sm._apply_detection("t1", TerminalStatus.PROCESSING) is True

        assert [r.status for r in _rows("t1")] == ["processing"]

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_unchanged_status_records_nothing_new(self, mock_get_backend):
        """Mutation guard: recording must be gated on `changed`, not fire on
        every detection call regardless of whether the latch actually moved."""
        mock_get_backend.return_value = _backend(event_inbox=False)
        sm = StatusMonitor()
        sm._apply_detection("t2", TerminalStatus.PROCESSING)
        sm._apply_detection("t2", TerminalStatus.PROCESSING)  # same status again

        assert len(_rows("t2")) == 1

    @patch("cli_agent_orchestrator.services.status_monitor.bus")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_recording_failure_never_changes_the_detection_result(self, mock_get_backend, mock_bus):
        """AC: a failure writing the record must never fail or alter a launch
        (here: the status-change detection result the caller observes)."""
        mock_get_backend.return_value = _backend(event_inbox=False)
        sm = StatusMonitor()

        with patch(
            "cli_agent_orchestrator.clients.database.record_launch_status_event",
            side_effect=RuntimeError("db is down"),
        ):
            changed = sm._apply_detection("t3", TerminalStatus.PROCESSING)

        assert changed is True
        mock_bus.publish.assert_called_once()  # the real side effect still ran

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_status_write_never_runs_on_the_event_loop(self, mock_get_backend, monkeypatch):
        """A transition detected on the loop is written by another thread:
        the commit can wait on the SQLite lock, and the loop must not."""
        from cli_agent_orchestrator.clients import database

        mock_get_backend.return_value = _backend(event_inbox=False)
        real_write = database.record_launch_status_event
        writer_threads = []

        def spy(*args, **kwargs):
            writer_threads.append(threading.get_ident())
            real_write(*args, **kwargs)

        monkeypatch.setattr(database, "record_launch_status_event", spy)
        sm = StatusMonitor()

        changed, loop_thread = _on_loop(
            lambda: sm._apply_detection("t5", TerminalStatus.PROCESSING)
        )
        _drain_tmux_status_writes()

        assert changed is True
        assert writer_threads, "no write observed"
        assert loop_thread not in writer_threads
        assert [r.status for r in _rows("t5")] == ["processing"]

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_status_event_is_stamped_at_the_transition_not_the_write(
        self, mock_get_backend, monkeypatch
    ):
        """The writer is held busy while the clock moves on 60 s: the row
        still carries the moment the transition was detected."""
        from cli_agent_orchestrator.clients import database

        mock_get_backend.return_value = _backend(event_inbox=False)
        clock = {"now": T0}
        monkeypatch.setattr(status_monitor, "_utcnow", lambda: clock["now"])
        monkeypatch.setattr(database, "_utcnow", lambda: clock["now"])
        gate = threading.Event()
        status_monitor._status_event_executor().submit(gate.wait, 10)
        sm = StatusMonitor()

        try:
            _on_loop(lambda: sm._apply_detection("t6", TerminalStatus.PROCESSING))
            clock["now"] = T0 + timedelta(seconds=60)
        finally:
            gate.set()
        _drain_tmux_status_writes()

        assert [(r.status, r.created_at) for r in _rows("t6")] == [("processing", T0)]

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_status_events_keep_the_transition_order(self, mock_get_backend, monkeypatch):
        """Transitions from the loop and from a worker thread are written in
        the order they happened, even when the first write is slow; busy
        time reads the rows by id."""
        from cli_agent_orchestrator.clients import database

        mock_get_backend.return_value = _backend(event_inbox=False)
        real_write = database.record_launch_status_event
        first_write = threading.Event()

        def slow_first_write(*args, **kwargs):
            if not first_write.is_set():
                first_write.set()
                time.sleep(0.3)
            real_write(*args, **kwargs)

        monkeypatch.setattr(database, "record_launch_status_event", slow_first_write)
        sm = StatusMonitor()

        _on_loop(lambda: sm._apply_detection("t7", TerminalStatus.PROCESSING))
        _on_loop(lambda: sm._apply_detection("t7", TerminalStatus.COMPLETED))
        sm.notify_input_sent("t7")
        worker = threading.Thread(
            target=sm._apply_detection, args=("t7", TerminalStatus.PROCESSING)
        )
        worker.start()
        worker.join(timeout=10)
        _drain_tmux_status_writes()

        rows = _rows("t7")
        assert [r.status for r in rows] == ["processing", "completed", "processing"]
        assert [r.created_at for r in rows] == sorted(r.created_at for r in rows)

    @patch("cli_agent_orchestrator.services.status_monitor.bus")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_failed_status_write_on_the_loop_is_logged_and_swallowed(
        self, mock_get_backend, mock_bus, caplog
    ):
        """The failure happens on the writer thread: the loop caller still
        sees its change, it is logged, and the writer keeps writing."""
        mock_get_backend.return_value = _backend(event_inbox=False)
        sm = StatusMonitor()

        with caplog.at_level(logging.DEBUG, logger=status_monitor.__name__):
            with patch(
                "cli_agent_orchestrator.clients.database.record_launch_status_event",
                side_effect=RuntimeError("db is down"),
            ):
                changed, _ = _on_loop(lambda: sm._apply_detection("t8", TerminalStatus.PROCESSING))
                _drain_tmux_status_writes()

        assert changed is True
        mock_bus.publish.assert_called_once()
        assert any(
            "Launch status event recording failed" in r.getMessage() and r.exc_info
            for r in caplog.records
        )
        assert _rows("t8") == []
        sm._apply_detection("t8", TerminalStatus.COMPLETED)
        assert [r.status for r in _rows("t8")] == ["completed"]


# The follow-up issue "Busy time misses two tmux status transitions that
# StatusMonitor publishes". When it lands, the strict xfails below start
# passing and fail the run, so drop their markers in that change.
_UNRECORDED_GET_STATUS_TRANSITION = (
    "Known gap: StatusMonitor.get_status publishes this transition without "
    "recording it, so busy time is off until the next recorded change "
    "(follow-up: 'Busy time misses two tmux status transitions that "
    "StatusMonitor publishes')"
)


class TestUnrecordedGetStatusTransitions:
    """Two latch changes in ``get_status`` are published on the bus but not
    written to ``launch_status_events``. The plain tests pin that each one is
    published today; the strict xfails say it is not recorded yet."""

    @staticmethod
    def _swarm_revoke(mock_pm, terminal_id):
        """A native swarm starts another batch while the latch reads COMPLETED:
        get_status revokes the ready status to PROCESSING."""
        provider = MagicMock()
        provider.has_pending_native_swarm = True
        mock_pm.get_provider.return_value = provider
        sm = StatusMonitor()
        assert sm._apply_detection(terminal_id, TerminalStatus.COMPLETED) is True
        status = sm.get_status(terminal_id)
        _drain_tmux_status_writes()
        return status

    @staticmethod
    def _stale_capture_recovery(mock_pm, mock_get_backend, terminal_id):
        """The buffer stopped changing while the latch reads PROCESSING, and
        two matching pane captures confirm IDLE: get_status heals to IDLE."""
        provider = MagicMock()
        provider.session_name = "s1"
        provider.window_name = "w1"
        provider.supports_screen_detection = False
        provider.supports_direct_status_probe = True
        provider.has_pending_native_swarm = False
        provider.get_status.return_value = TerminalStatus.IDLE
        mock_pm.get_provider.return_value = provider
        mock_get_backend.return_value.get_history.return_value = "the real pane, idle"
        sm = StatusMonitor()
        assert sm._apply_detection(terminal_id, TerminalStatus.PROCESSING) is True
        sm._buffers[terminal_id] = ""
        sm._buffer_changed_at[terminal_id] = -1000.0  # quiet for good
        assert sm.get_status(terminal_id) == TerminalStatus.PROCESSING  # first read
        sm._last_stale_capture_check[terminal_id] = None  # skip the rate limit
        status = sm.get_status(terminal_id)  # the confirming read
        _drain_tmux_status_writes()
        return status

    @staticmethod
    def _published(mock_bus):
        return [c.args[1]["status"] for c in mock_bus.publish.call_args_list]

    @patch("cli_agent_orchestrator.services.status_monitor.bus")
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_native_swarm_revoke_is_published(self, mock_get_backend, mock_pm, mock_bus):
        mock_get_backend.return_value = _backend(event_inbox=False)
        assert self._swarm_revoke(mock_pm, "t10") == TerminalStatus.PROCESSING
        assert self._published(mock_bus) == ["completed", "processing"]

    @pytest.mark.xfail(strict=True, reason=_UNRECORDED_GET_STATUS_TRANSITION)
    @patch("cli_agent_orchestrator.services.status_monitor.bus")
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_native_swarm_revoke_is_recorded(self, mock_get_backend, mock_pm, mock_bus):
        mock_get_backend.return_value = _backend(event_inbox=False)
        self._swarm_revoke(mock_pm, "t11")
        assert [r.status for r in _rows("t11")] == ["completed", "processing"]

    @patch("cli_agent_orchestrator.services.status_monitor.bus")
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_stale_capture_recovery_is_published(self, mock_get_backend, mock_pm, mock_bus):
        mock_get_backend.return_value = _backend(event_inbox=False)
        status = self._stale_capture_recovery(mock_pm, mock_get_backend, "t12")
        assert status == TerminalStatus.IDLE
        assert self._published(mock_bus) == ["processing", "idle"]

    @pytest.mark.xfail(strict=True, reason=_UNRECORDED_GET_STATUS_TRANSITION)
    @patch("cli_agent_orchestrator.services.status_monitor.bus")
    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_stale_capture_recovery_is_recorded(self, mock_get_backend, mock_pm, mock_bus):
        mock_get_backend.return_value = _backend(event_inbox=False)
        self._stale_capture_recovery(mock_pm, mock_get_backend, "t13")
        assert [r.status for r in _rows("t13")] == ["processing", "idle"]


class TestHerdrBusyTimeFromEvents:
    def test_agent_status_events_record_transitions_at_event_time(self, monkeypatch):
        _feed(
            _service(),
            [_pane_event("pane-1", "working"), _pane_event("pane-1", "done")],
            at_seconds=[5, 65],
            monkeypatch=monkeypatch,
        )
        rows = _rows("h1")
        assert [r.status for r in rows] == ["processing", "completed"]
        assert [r.created_at for r in rows] == [
            T0 + timedelta(seconds=5),
            T0 + timedelta(seconds=65),
        ]

    def test_repeated_same_status_records_once(self):
        """Mutation guard: herdr repeats pane.updated for an unchanged
        agent_status, so dedup is against the last status recorded."""
        _feed(_service(), [_pane_event("pane-1", "working")] * 3 + [_pane_event("pane-1", "idle")])
        assert [r.status for r in _rows("h1")] == ["processing", "idle"]

    def test_unmanaged_pane_and_unresolvable_status_record_nothing(self):
        service = _service()
        _feed(
            service,
            [
                _pane_event("pane-other", "working"),
                _pane_event("pane-1", "unknown"),
                _pane_event("pane-1", None),
            ],
        )
        assert _rows("h1") == []
        assert service._last_recorded_status == {}

    def test_unregister_forgets_the_dedup_entry(self):
        """A relaunch on a reused id is not deduped against the old status."""
        service = _service()
        _feed(service, [_pane_event("pane-1", "idle")])
        service.unregister_terminal("h1")
        assert "h1" not in service._last_recorded_status
        service.register_terminal("h1", "pane-1")
        _feed(service, [_pane_event("pane-1", "idle")])
        assert [r.status for r in _rows("h1")] == ["idle", "idle"]

    def test_recording_failure_never_breaks_delivery(self):
        """AC: a recording failure must never alter what the event loop does
        for the pane: delivery still fires and later events still process."""
        callback = MagicMock()
        with patch(
            "cli_agent_orchestrator.clients.database.record_launch_status_event",
            side_effect=RuntimeError("db is down"),
        ):
            _feed(
                _service(callback),
                [_pane_event("pane-1", "working"), _pane_event("pane-1", "idle")],
            )
        callback.assert_called_once_with("h1")

    def test_connect_time_snapshot_records_the_current_status(self, monkeypatch):
        """A transition missed while disconnected surfaces at reconnect."""
        service = _service()
        monkeypatch.setattr(
            service,
            "_fetch_snapshot",
            lambda: {"panes": [{"pane_id": "pane-1", "agent_status": "working"}]},
        )
        asyncio.run(service._deliver_settled_panes())
        assert [r.status for r in _rows("h1")] == ["processing"]

    def test_status_write_runs_off_the_event_loop(self):
        """The write goes through ``asyncio.to_thread``, so it never shares
        the event-loop thread every herdr caller runs on. Moving it back
        onto the loop (calling ``record_launch_status_event`` directly,
        synchronously) would leave every other test in this file green,
        since none of them inspects which thread wrote the row."""
        import threading

        main_thread = threading.current_thread()
        seen_threads = []

        def _spy(terminal_id, status, observed_at=None):
            seen_threads.append(threading.current_thread())

        with patch(
            "cli_agent_orchestrator.clients.database.record_launch_status_event",
            side_effect=_spy,
        ):
            _feed(_service(), [_pane_event("pane-1", "working")])

        assert seen_threads, "no write observed"
        assert all(t is not main_thread for t in seen_threads)

    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_get_status_poll_records_nothing_on_herdr(self, mock_get_backend, mock_pm):
        """One source per backend: polling get_status() on herdr writes no
        transition, so a poll sample can never reopen a finished turn."""
        mock_get_backend.return_value = _backend(event_inbox=True)
        provider = MagicMock()
        provider.get_status.return_value = TerminalStatus.PROCESSING
        mock_pm.get_provider.return_value = provider

        assert StatusMonitor().get_status("h9") == TerminalStatus.PROCESSING
        assert _rows("h9") == []

    @patch("cli_agent_orchestrator.services.status_monitor.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    def test_finished_turn_is_not_counted_until_teardown(
        self, mock_get_backend, mock_pm, monkeypatch
    ):
        """The inflation case: a dispatch wait polls once while the worker is
        processing, the worker finishes 60 s later with nobody polling it, and
        it is torn down an hour after that. Busy time is the 60 s turn, not
        3600 s (poll-sampled), and not 0 (no poll during the work)."""
        from cli_agent_orchestrator.clients import database

        monkeypatch.setattr(database, "_utcnow", lambda: T0)
        database.create_launch_record(
            terminal_id="h1",
            session_name="s",
            provider="claude_code",
            target_name="developer",
            target_kind="installed",
            launched_model=None,
            model_honored=None,
        )
        mock_get_backend.return_value = _backend(event_inbox=True)
        provider = MagicMock()
        provider.get_status.return_value = TerminalStatus.PROCESSING
        mock_pm.get_provider.return_value = provider
        StatusMonitor().get_status("h1")  # the one dispatch-wait poll

        _feed(
            _service(),
            [_pane_event("pane-1", "working"), _pane_event("pane-1", "idle")],
            at_seconds=[1, 61],
            monkeypatch=monkeypatch,
        )
        monkeypatch.setattr(database, "_utcnow", lambda: T0 + timedelta(seconds=3600))
        database.finalize_launch_records(["h1"], "deleted")

        record_id = database.get_launch_record("h1")["id"]
        assert database.compute_busy_seconds(record_id) == 60.0

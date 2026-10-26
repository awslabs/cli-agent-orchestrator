"""Issue #1082 busy time: a status transition is persisted for BOTH backends --
tmux via StatusMonitor._apply_detection's sticky-latch pipeline, and herdr via
the agent_status herdr reports for a managed pane on its event stream
(HerdrInboxService), never by sampling whoever happens to poll get_status().
A recording failure must never affect the status either path delivers."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import herdr_inbox_service
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

        assert database.compute_busy_seconds("h1") == 60.0

"""Tests for re-attaching surviving terminal output pipelines after server restart (#1438)."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import logging
import os
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from test.api.test_lifespan_inbox import _patched_lifespan
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.api import main
from cli_agent_orchestrator.api.main import app, lifespan
from cli_agent_orchestrator.backends.base import TerminalCleanupOutcome, TerminalCleanupResult
from cli_agent_orchestrator.clients import database as db
from cli_agent_orchestrator.constants import (
    PIPE_LIVENESS_MAX_COLD_START_ATTEMPTS,
    PIPE_LIVENESS_STALL_CHECKS,
    PIPE_LIVENESS_TAIL_LINES,
)
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import ephemeral_service, fifo_reader
from cli_agent_orchestrator.services import terminal_service as ts
from cli_agent_orchestrator.services.event_bus import bus
from cli_agent_orchestrator.services.fifo_reader import FifoManager
from cli_agent_orchestrator.services.status_monitor import StatusMonitor


def _make_pipe_backend(*, live_ids=None, viewport=""):
    live_set = set(live_ids or ())
    backend = MagicMock()
    backend.supports_event_inbox.return_value = False
    backend.get_native_status.return_value = None
    backend.get_history.return_value = viewport
    backend.cleanup_terminal_exact.side_effect = (
        lambda tid, session=None, window=None, close=True: TerminalCleanupResult(
            (
                TerminalCleanupOutcome.STILL_PRESENT
                if tid in live_set
                else TerminalCleanupOutcome.ABSENT
            ),
            "test probe",
        )
    )
    return backend


def _insert_ephemeral_row(
    name: str,
    terminal_id: str,
    *,
    state: str,
    session_name: str = "cao-sess",
) -> None:
    now = datetime.now(timezone.utc)
    with db.SessionLocal() as session:
        session.add(
            db.EphemeralAgentModel(
                name=name,
                owner_kind="terminal",
                owner_id="owner-1",
                session_name=session_name,
                state=state,
                launched_terminal_id=terminal_id,
                bound_at=now,
                provider="kiro_cli",
                effective_tools="[]",
                created_at=now,
                expires_at=now + timedelta(minutes=30),
                spec_sha256="0" * 64,
                profile_sha256="0" * 64,
                audit_path="/tmp/audit.json",
            )
        )
        session.commit()


def _open_writer(fifo_path, timeout: float = 5.0) -> int:
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
            break
        except OSError as error:
            if error.errno != errno.ENXIO:
                raise
            if time.monotonic() >= deadline:
                pytest.fail(f"no reader opened {fifo_path} within {timeout} s")
            time.sleep(0.01)
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
    return fd


class TestAttachOutputPipeline:
    """Tests for terminal_service.attach_output_pipeline()."""

    def test_replace_existing_order_is_reader_stop_pipe(self, monkeypatch, tmp_path):
        events: list[str] = []
        fifo_dir = tmp_path / "fifos"
        fifo_dir.mkdir()
        monkeypatch.setattr(ts, "FIFO_DIR", fifo_dir)

        fake_fifo = MagicMock()
        fake_fifo.create_reader.side_effect = lambda *a, **k: events.append("create_reader")
        monkeypatch.setattr(ts, "fifo_manager", fake_fifo)

        backend = MagicMock()
        backend.stop_pipe_pane.side_effect = lambda s, w: events.append(f"stop:{s}:{w}")
        backend.pipe_pane.side_effect = lambda s, w, p: events.append(f"pipe:{s}:{w}:{p}")
        monkeypatch.setattr(ts, "get_backend", lambda: backend)

        ts.attach_output_pipeline("term-1", "cao-s", "win-1", replace_existing=True)

        assert events == [
            "create_reader",
            "stop:cao-s:win-1",
            f"pipe:cao-s:win-1:{fifo_dir / 'term-1.fifo'}",
        ]
        # Verify watchdog callbacks use the same probe and stop-then-start re-arm.
        kwargs = fake_fifo.create_reader.call_args.kwargs
        kwargs["pane_probe"]()
        backend.get_history.assert_called_once_with(
            "cao-s", "win-1", tail_lines=PIPE_LIVENESS_TAIL_LINES
        )
        backend.stop_pipe_pane.reset_mock()
        backend.pipe_pane.reset_mock()
        kwargs["rearm"]()
        backend.stop_pipe_pane.assert_called_once_with("cao-s", "win-1")
        backend.pipe_pane.assert_called_once_with("cao-s", "win-1", str(fifo_dir / "term-1.fifo"))

    def test_create_path_skips_stop_pipe_pane(self, monkeypatch, tmp_path):
        fifo_dir = tmp_path / "fifos"
        fifo_dir.mkdir()
        monkeypatch.setattr(ts, "FIFO_DIR", fifo_dir)

        fake_fifo = MagicMock()
        monkeypatch.setattr(ts, "fifo_manager", fake_fifo)

        backend = MagicMock()
        monkeypatch.setattr(ts, "get_backend", lambda: backend)

        ts.attach_output_pipeline("term-2", "cao-s", "win-2", replace_existing=False)

        fake_fifo.create_reader.assert_called_once()
        backend.stop_pipe_pane.assert_not_called()
        backend.pipe_pane.assert_called_once_with("cao-s", "win-2", str(fifo_dir / "term-2.fifo"))

    def test_failure_during_pipe_pane_stops_reader_and_reraises(self, monkeypatch, tmp_path):
        fifo_dir = tmp_path / "fifos"
        fifo_dir.mkdir()
        monkeypatch.setattr(ts, "FIFO_DIR", fifo_dir)

        fake_fifo = MagicMock()
        monkeypatch.setattr(ts, "fifo_manager", fake_fifo)

        backend = MagicMock()
        backend.pipe_pane.side_effect = RuntimeError("pane gone")
        monkeypatch.setattr(ts, "get_backend", lambda: backend)

        with pytest.raises(RuntimeError, match="pane gone"):
            ts.attach_output_pipeline("term-3", "cao-s", "win-3", replace_existing=True)

        fake_fifo.create_reader.assert_called_once()
        assert fake_fifo.create_reader.call_args.kwargs["cold_start_check"] is False
        backend.stop_pipe_pane.assert_called_once_with("cao-s", "win-3")
        fake_fifo.stop_reader.assert_called_once_with("term-3")

    def test_replace_existing_disables_cold_start_check_and_create_enables_it(
        self, monkeypatch, tmp_path
    ):
        fifo_dir = tmp_path / "fifos"
        fifo_dir.mkdir()
        monkeypatch.setattr(ts, "FIFO_DIR", fifo_dir)

        fake_fifo = MagicMock()
        monkeypatch.setattr(ts, "fifo_manager", fake_fifo)
        monkeypatch.setattr(ts, "get_backend", lambda: MagicMock())

        ts.attach_output_pipeline("term-new", "cao-s", "win-1", replace_existing=False)
        assert fake_fifo.create_reader.call_args.kwargs["cold_start_check"] is True

        fake_fifo.create_reader.reset_mock()
        ts.attach_output_pipeline("term-surv", "cao-s", "win-2", replace_existing=True)
        assert fake_fifo.create_reader.call_args.kwargs["cold_start_check"] is False

    def test_redundant_attach_does_not_toggle_pipe_or_stop_existing_reader(
        self, monkeypatch, tmp_path
    ):
        """P3-1: when create_reader finds an existing reader, skip pipe_pane and stop_reader."""
        fifo_dir = tmp_path / "fifos"
        fifo_dir.mkdir()
        monkeypatch.setattr(fifo_reader, "FIFO_DIR", fifo_dir)
        monkeypatch.setattr(ts, "FIFO_DIR", fifo_dir)

        fresh_fifo = FifoManager()
        monkeypatch.setattr(ts, "fifo_manager", fresh_fifo)

        backend = MagicMock()
        monkeypatch.setattr(ts, "get_backend", lambda: backend)

        try:
            # First attach creates the reader and opens pipe-pane once.
            ts.attach_output_pipeline("term-dup", "cao-s", "win-dup", replace_existing=False)
            assert backend.pipe_pane.call_count == 1
            assert "term-dup" in fresh_fifo._readers

            # Redundant attach (even if pipe_pane would fail) must be a no-op:
            # no pipe_pane toggle, no stop_pipe_pane, and no stop_reader.
            backend.pipe_pane.reset_mock()
            backend.stop_pipe_pane.reset_mock()
            backend.pipe_pane.side_effect = RuntimeError("must not be called")

            ts.attach_output_pipeline("term-dup", "cao-s", "win-dup", replace_existing=False)
            ts.attach_output_pipeline("term-dup", "cao-s", "win-dup", replace_existing=True)

            backend.pipe_pane.assert_not_called()
            backend.stop_pipe_pane.assert_not_called()
            assert "term-dup" in fresh_fifo._readers
        finally:
            fresh_fifo.stop_reader("term-dup")
            fresh_fifo.stop_watchdog()

    def test_failure_during_pipe_pane_preserves_error_if_stop_reader_raises(
        self, monkeypatch, tmp_path
    ):
        fifo_dir = tmp_path / "fifos"
        fifo_dir.mkdir()
        monkeypatch.setattr(ts, "FIFO_DIR", fifo_dir)

        fake_fifo = MagicMock()
        fake_fifo.create_reader.return_value = True
        fake_fifo.stop_reader.side_effect = OSError("cleanup failed")
        monkeypatch.setattr(ts, "fifo_manager", fake_fifo)

        backend = MagicMock()
        backend.pipe_pane.side_effect = RuntimeError("original attach error")
        monkeypatch.setattr(ts, "get_backend", lambda: backend)

        with pytest.raises(RuntimeError, match="original attach error"):
            ts.attach_output_pipeline("term-err", "cao-s", "win-err")


class TestReattachSurvivingTerminals:
    """Tests for terminal_service.reattach_surviving_terminals()."""

    @pytest.fixture(autouse=True)
    def isolated_reattach(self, isolated_memory_db, monkeypatch, tmp_path):
        monkeypatch.setattr(ts, "TERMINAL_LOG_DIR", tmp_path / "logs")
        monkeypatch.setattr(ts, "FIFO_DIR", tmp_path / "fifos")
        (tmp_path / "fifos").mkdir()

    def test_skips_event_inbox_backend(self, monkeypatch):
        db.create_terminal("term-live", "cao-s", "win-live", "kiro_cli")
        backend = MagicMock()
        backend.supports_event_inbox.return_value = True
        monkeypatch.setattr(ts, "get_backend", lambda: backend)

        attach = MagicMock()
        monkeypatch.setattr(ts, "attach_output_pipeline", attach)

        assert ts.reattach_surviving_terminals() == 0
        attach.assert_not_called()
        backend.cleanup_terminal_exact.assert_not_called()

    def test_skips_error_kept_ephemeral_residue_and_dead_but_reattaches_live(self, monkeypatch):
        # 1. Live surviving non-ephemeral terminal -> RE-ATTACH
        db.create_terminal("term-live", "cao-s", "win-live", "kiro_cli")
        # 2. Live ephemeral helper (state == "launched") -> RE-ATTACH
        db.create_terminal("term-eph-live", "cao-s", "win-eph-live", "kiro_cli")
        _insert_ephemeral_row("ACDC-live-1111", "term-eph-live", state="launched")
        # 3. Ephemeral residue (state == "gc") -> SKIP
        db.create_terminal("term-eph-gc", "cao-s", "win-eph-gc", "kiro_cli")
        _insert_ephemeral_row("Queen-gc-2222", "term-eph-gc", state="gc")
        # 4. ERROR row (deferred_init_failure) -> SKIP
        db.create_terminal("term-err-fail", "cao-s", "win-err-fail", "kiro_cli")
        db.update_terminal_deferred_init_failure(
            "term-err-fail", {"kind": "interrupted_init", "message": "failed"}
        )
        # 5. Deferred-init external owner still pending retry -> SKIP
        db.create_terminal(
            "term-err-ext",
            "cao-s",
            "win-err-ext",
            "kiro_cli",
            deferred_init_external_owner=True,
        )
        # 6. Runtime already reclaimed -> SKIP
        db.create_terminal("term-err-reclaimed", "cao-s", "win-err-reclaimed", "kiro_cli")
        db.update_terminal_deferred_init_runtime_reclaimed("term-err-reclaimed", True)
        # 7. Failure fallback sidecar on disk -> SKIP
        db.create_terminal("term-err-sidecar", "cao-s", "win-err-sidecar", "kiro_cli")
        assert ts._publish_deferred_failure_fallback(
            "term-err-sidecar", {"kind": "interrupted_init", "message": "sidecar"}
        )
        # 8. Kept child -> SKIP
        db.create_terminal("term-kept", "cao-s", "win-kept", "kiro_cli", caller_id="term-live")
        db.mark_terminal_kept(
            "term-kept",
            "cao-s",
            kept_until=datetime.now(timezone.utc) + timedelta(minutes=15),
            reason="handoff",
        )
        # 9. Dead pane (not STILL_PRESENT in backend) -> SKIP
        db.create_terminal("term-dead", "cao-s", "win-dead", "kiro_cli")

        # Mark all of them as "present" in backend EXCEPT term-dead, so only our
        # policy filters decide which ones are skipped vs attached.
        live_ids = {
            "term-live",
            "term-eph-live",
            "term-eph-gc",
            "term-err-fail",
            "term-err-ext",
            "term-err-reclaimed",
            "term-err-sidecar",
            "term-kept",
        }
        backend = _make_pipe_backend(live_ids=live_ids)
        monkeypatch.setattr(ts, "get_backend", lambda: backend)

        attached: list[tuple[str, str, str, bool]] = []
        monkeypatch.setattr(
            ts,
            "attach_output_pipeline",
            lambda tid, sess, win, replace_existing=False: attached.append(
                (tid, sess, win, replace_existing)
            ),
        )
        seeded: list[str] = []
        fake_sm = MagicMock()
        fake_sm.seed_from_viewport.side_effect = lambda tid, sess, win: seeded.append(tid)
        monkeypatch.setattr(ts, "status_monitor", fake_sm)

        count = ts.reattach_surviving_terminals()

        assert count == 2
        assert attached == [
            ("term-live", "cao-s", "win-live", True),
            ("term-eph-live", "cao-s", "win-eph-live", True),
        ]
        assert seeded == ["term-live", "term-eph-live"]
        backend.cleanup_terminal_exact.assert_any_call(
            "term-dead", "cao-s", "win-dead", close=False
        )

    def test_reattach_failure_is_isolated_and_logged(self, monkeypatch, caplog):
        db.create_terminal("term-bad", "cao-s", "win-bad", "kiro_cli")
        db.create_terminal("term-good", "cao-s", "win-good", "kiro_cli")

        backend = _make_pipe_backend(live_ids={"term-bad", "term-good"})
        monkeypatch.setattr(ts, "get_backend", lambda: backend)

        attached: list[str] = []

        def fake_attach(tid, sess, win, replace_existing=False):
            if tid == "term-bad":
                raise RuntimeError("pipe-pane boom")
            attached.append(tid)

        monkeypatch.setattr(ts, "attach_output_pipeline", fake_attach)
        fake_sm = MagicMock()
        monkeypatch.setattr(ts, "status_monitor", fake_sm)

        with caplog.at_level(logging.WARNING, logger=ts.__name__):
            count = ts.reattach_surviving_terminals()

        assert count == 1
        assert attached == ["term-good"]
        fake_sm.seed_from_viewport.assert_called_once_with("term-good", "cao-s", "win-good")
        assert "term-bad" in caplog.text
        assert "pipe-pane boom" in caplog.text

    def test_list_terminals_error_does_not_raise(self, monkeypatch, caplog):
        backend = _make_pipe_backend()
        monkeypatch.setattr(ts, "get_backend", lambda: backend)
        monkeypatch.setattr(
            ts, "list_all_terminals", MagicMock(side_effect=RuntimeError("db locked"))
        )

        with caplog.at_level(logging.WARNING, logger=ts.__name__):
            assert ts.reattach_surviving_terminals() == 0
        assert "db locked" in caplog.text

    def test_backend_unavailable_skips_reattach(self, monkeypatch, caplog):
        monkeypatch.setattr(ts, "get_backend", MagicMock(side_effect=RuntimeError("no backend")))

        with caplog.at_level(logging.WARNING, logger=ts.__name__):
            assert ts.reattach_surviving_terminals() == 0
        assert "backend unavailable" in caplog.text

    def test_skips_rows_missing_identity_or_tmux_coords(self, monkeypatch):
        backend = _make_pipe_backend(live_ids={"term-ok"})
        monkeypatch.setattr(ts, "get_backend", lambda: backend)
        monkeypatch.setattr(
            ts,
            "list_all_terminals",
            lambda: [
                {"id": "", "tmux_session": "s", "tmux_window": "w"},
                {"id": "term-no-sess", "tmux_session": "", "tmux_window": "w"},
                {"id": "term-no-win", "tmux_session": "s", "tmux_window": ""},
            ],
        )

        assert ts.reattach_surviving_terminals() == 0
        backend.cleanup_terminal_exact.assert_not_called()


class TestViewportSeeding:
    """Tests for StatusMonitor.seed_from_viewport() and subsequent output transitions."""

    def test_open_dialog_seeds_waiting_user_answer_via_screen_detector_and_later_output_moves_status(
        self, monkeypatch
    ):
        sm = StatusMonitor()
        backend = _make_pipe_backend(
            viewport=(
                "────────────────────────\n"
                " shell requires approval\n"
                " ❯ Yes, single permission\n"
                "────────────────────────\n"
                " esc to close"
            )
        )
        provider = MagicMock()
        provider.supports_screen_detection = True
        provider.get_status_from_screen.return_value = TerminalStatus.WAITING_USER_ANSWER

        with (
            patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
            patch("cli_agent_orchestrator.services.status_monitor.provider_manager") as mock_pm,
        ):
            mock_pm.get_provider.return_value = provider
            seeded = sm.seed_from_viewport("t1", "cao-s", "win-1")

        assert seeded == TerminalStatus.WAITING_USER_ANSWER
        assert sm.get_status("t1") == TerminalStatus.WAITING_USER_ANSWER
        backend.get_history.assert_called_once_with(
            "cao-s", "win-1", strip_escapes=True, visible_only=True
        )
        provider.get_status_from_screen.assert_called_once()
        provider.get_status.assert_not_called()

        # Later output after user answers moves status normally.
        provider.get_status_from_screen.return_value = TerminalStatus.PROCESSING
        provider.get_status.return_value = TerminalStatus.PROCESSING
        sm.notify_input_sent("t1")
        with patch("cli_agent_orchestrator.services.status_monitor.provider_manager") as mock_pm:
            mock_pm.get_provider.return_value = provider
            sm._process_chunk("t1", "working...\n")
            with patch(
                "cli_agent_orchestrator.backends.registry.get_backend", return_value=backend
            ):
                assert sm.get_status("t1") == TerminalStatus.PROCESSING

            provider.get_status_from_screen.return_value = TerminalStatus.COMPLETED
            provider.get_status.return_value = TerminalStatus.COMPLETED
            sm._process_chunk("t1", "done\n")
            sm._apply_detection("t1", TerminalStatus.COMPLETED)
            with patch(
                "cli_agent_orchestrator.backends.registry.get_backend", return_value=backend
            ):
                assert sm.get_status("t1") == TerminalStatus.COMPLETED

    def test_open_kiro_dialog_seeds_waiting_user_answer_for_raw_stream_provider(self):
        from cli_agent_orchestrator.models.kiro_engine import KiroEngine
        from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider

        sm = StatusMonitor()
        kiro = KiroCliProvider("t-kiro", "cao-s", "win-kiro", "developer", engine=KiroEngine.V2)
        viewport = (
            "↓ Shell make test SUITE=unit\n"
            "    ╰ working_dir=/work/repo\n\n"
            "────────────────────────────────────────\n"
            " shell requires approval\n"
            " ❯ Yes, single permission\n"
            "   Trust, always allow in this session\n"
            "   No (Tab to edit)\n"
            "────────────────────────────────────────\n"
            " esc to close · ↑↓ to navigate · ↵ to select · Tab to edit\n"
        )
        backend = _make_pipe_backend(viewport=viewport)

        with (
            patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
            patch("cli_agent_orchestrator.services.status_monitor.provider_manager") as mock_pm,
        ):
            mock_pm.get_provider.return_value = kiro
            seeded = sm.seed_from_viewport("t-kiro", "cao-s", "win-kiro")

        assert seeded == TerminalStatus.WAITING_USER_ANSWER
        assert sm.get_status("t-kiro") == TerminalStatus.WAITING_USER_ANSWER

    @pytest.mark.parametrize(
        "verdict",
        [
            TerminalStatus.IDLE,
            TerminalStatus.COMPLETED,
            TerminalStatus.PROCESSING,
            TerminalStatus.UNKNOWN,
            TerminalStatus.ERROR,
        ],
    )
    def test_non_waiting_verdict_leaves_status_unknown(self, verdict):
        sm = StatusMonitor()
        backend = _make_pipe_backend(viewport="> ready")
        provider = MagicMock()
        provider.supports_screen_detection = False
        provider.probe_stale_processing_capture = None
        provider.get_status.return_value = verdict

        with (
            patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
            patch("cli_agent_orchestrator.services.status_monitor.provider_manager") as mock_pm,
        ):
            mock_pm.get_provider.return_value = provider
            seeded = sm.seed_from_viewport("t2", "cao-s", "win-2")
            assert seeded is None
            assert sm.get_status("t2") == TerminalStatus.UNKNOWN

    def test_viewport_read_failure_seeds_nothing(self):
        sm = StatusMonitor()
        backend = _make_pipe_backend()
        backend.get_history.side_effect = RuntimeError("tmux capture-pane failed")
        provider = MagicMock()

        with (
            patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
            patch("cli_agent_orchestrator.services.status_monitor.provider_manager") as mock_pm,
        ):
            mock_pm.get_provider.return_value = provider
            assert sm.seed_from_viewport("t3", "cao-s", "win-3") is None
            assert sm.get_status("t3") == TerminalStatus.UNKNOWN

    def test_concurrent_stream_output_is_not_overwritten_by_stale_viewport_seed(self):
        sm = StatusMonitor()
        backend = _make_pipe_backend(viewport="approval dialog")
        provider = MagicMock()
        provider.supports_screen_detection = False
        provider.probe_stale_processing_capture = None

        def get_history_while_stream_advances(*args, **kwargs):
            # Simulate a live FIFO chunk arriving while capture-pane is in flight.
            provider.get_status.return_value = TerminalStatus.COMPLETED
            sm._process_chunk("t4", "> done\n")
            sm._apply_detection("t4", TerminalStatus.COMPLETED)
            provider.get_status.return_value = TerminalStatus.WAITING_USER_ANSWER
            return "approval dialog"

        backend.get_history.side_effect = get_history_while_stream_advances

        with (
            patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
            patch("cli_agent_orchestrator.services.status_monitor.provider_manager") as mock_pm,
        ):
            mock_pm.get_provider.return_value = provider
            assert sm.seed_from_viewport("t4", "cao-s", "win-4") is None
            assert sm.get_status("t4") == TerminalStatus.COMPLETED

    def test_seed_from_viewport_skips_when_status_already_known(self):
        sm = StatusMonitor()
        sm._last_status["t-known"] = TerminalStatus.IDLE
        with patch("cli_agent_orchestrator.services.status_monitor.provider_manager") as mock_pm:
            assert sm.seed_from_viewport("t-known", "cao-s", "win") is None
            mock_pm.get_provider.assert_not_called()

    def test_seed_from_viewport_handles_missing_or_failing_provider_and_empty_viewport(self):
        sm = StatusMonitor()
        backend = _make_pipe_backend(viewport="")

        with patch("cli_agent_orchestrator.services.status_monitor.provider_manager") as mock_pm:
            mock_pm.get_provider.side_effect = RuntimeError("no provider")
            assert sm.seed_from_viewport("t-p1", "cao-s", "win") is None

            mock_pm.get_provider.side_effect = None
            mock_pm.get_provider.return_value = None
            assert sm.seed_from_viewport("t-p2", "cao-s", "win") is None

            provider = MagicMock()
            mock_pm.get_provider.return_value = provider
            with patch(
                "cli_agent_orchestrator.backends.registry.get_backend", return_value=backend
            ):
                assert sm.seed_from_viewport("t-empty", "cao-s", "win") is None
                provider.get_status.assert_not_called()

    def test_seed_from_viewport_uses_probe_and_handles_detection_error(self):
        sm = StatusMonitor()
        backend = _make_pipe_backend(viewport="approval prompt")

        provider = MagicMock()
        provider.supports_screen_detection = False
        provider.probe_stale_processing_capture = MagicMock(
            return_value=TerminalStatus.WAITING_USER_ANSWER
        )
        with (
            patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
            patch("cli_agent_orchestrator.services.status_monitor.provider_manager") as mock_pm,
        ):
            mock_pm.get_provider.return_value = provider
            assert (
                sm.seed_from_viewport("t-probe", "cao-s", "win")
                == TerminalStatus.WAITING_USER_ANSWER
            )
            provider.probe_stale_processing_capture.assert_called_once_with("approval prompt")
            provider.get_status.assert_not_called()

        provider_err = MagicMock()
        provider_err.supports_screen_detection = False
        provider_err.probe_stale_processing_capture = None
        provider_err.get_status.side_effect = ValueError("parse boom")
        with (
            patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
            patch("cli_agent_orchestrator.services.status_monitor.provider_manager") as mock_pm,
        ):
            mock_pm.get_provider.return_value = provider_err
            assert sm.seed_from_viewport("t-det-err", "cao-s", "win") is None
            assert sm.get_status("t-det-err") == TerminalStatus.UNKNOWN


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs require a POSIX platform")
class TestRestartPipelineReproduction:
    """Reproduce stale UNKNOWN after restart and verify live FIFO re-attach restores updates."""

    @pytest.mark.asyncio
    async def test_surviving_terminal_stays_unknown_until_reattached_then_updates_via_fifo(
        self, isolated_memory_db, monkeypatch, tmp_path
    ):
        fifo_dir = tmp_path / "fifos"
        fifo_dir.mkdir()
        monkeypatch.setattr(fifo_reader, "FIFO_DIR", fifo_dir)
        monkeypatch.setattr(ts, "FIFO_DIR", fifo_dir)
        monkeypatch.setattr(ts, "TERMINAL_LOG_DIR", tmp_path / "logs")

        db.create_terminal("term-e2e", "cao-s", "win-e2e", "mock_cli")
        backend = _make_pipe_backend(live_ids={"term-e2e"}, viewport="> idle\n")

        # Fresh post-restart managers (simulating a brand new cao-server process).
        fresh_fifo = FifoManager()
        fresh_sm = StatusMonitor()
        monkeypatch.setattr(ts, "fifo_manager", fresh_fifo)
        monkeypatch.setattr(ts, "status_monitor", fresh_sm)
        monkeypatch.setattr(ts, "get_backend", lambda: backend)

        provider = MagicMock()
        provider.supports_screen_detection = False
        provider.probe_stale_processing_capture = None
        provider.get_status.side_effect = lambda buf: (
            TerminalStatus.COMPLETED if "DONE_MARKER" in buf else TerminalStatus.IDLE
        )

        loop = asyncio.get_running_loop()
        bus.set_loop(loop)
        sm_task = asyncio.create_task(fresh_sm.run())
        await asyncio.sleep(0)

        writer_fd = None
        try:
            with (
                patch(
                    "cli_agent_orchestrator.backends.registry.get_backend",
                    return_value=backend,
                ),
                patch("cli_agent_orchestrator.services.status_monitor.provider_manager") as mock_pm,
            ):
                mock_pm.get_provider.return_value = provider

                # Before re-attach: StatusMonitor has no reader and stays UNKNOWN.
                assert fresh_sm.get_status("term-e2e") == TerminalStatus.UNKNOWN
                assert not (fifo_dir / "term-e2e.fifo").exists()

                # Re-attach surviving terminals (as cao-server startup now does).
                count = await asyncio.to_thread(ts.reattach_surviving_terminals)
                assert count == 1
                assert (fifo_dir / "term-e2e.fifo").exists()
                backend.stop_pipe_pane.assert_called_once_with("cao-s", "win-e2e")
                backend.pipe_pane.assert_called_once_with(
                    "cao-s", "win-e2e", str(fifo_dir / "term-e2e.fifo")
                )

                # Write completion output through the FIFO as pipe-pane's fifo_writer does.
                writer_fd = _open_writer(fifo_dir / "term-e2e.fifo")
                os.write(writer_fd, b"working output\nDONE_MARKER\n")

                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    if fresh_sm.get_status("term-e2e") == TerminalStatus.COMPLETED:
                        break
                    await asyncio.sleep(0.05)

                assert fresh_sm.get_status("term-e2e") == TerminalStatus.COMPLETED
                assert "DONE_MARKER" in ts.get_output("term-e2e")
        finally:
            if writer_fd is not None:
                os.close(writer_fd)
            fresh_fifo.stop_reader("term-e2e")
            sm_task.cancel()
            try:
                await sm_task
            except asyncio.CancelledError:
                pass
            bus.set_loop(None)


class TestLifespanReattachWiring:
    """Startup order and resilience in api/main.py lifespan."""

    @pytest.mark.asyncio
    async def test_reattach_runs_after_sweeps_and_before_yield(self) -> None:
        tasks: list = []
        order: list[str] = []

        async def rec_ext():
            order.append("deferred_ext")
            return True

        async def rec_init():
            order.append("initial_delivery")
            return True

        def fake_sweep(startup):
            order.append(f"ephemeral:{startup}")
            return {}

        def fake_reattach():
            order.append("reattach")
            return 0

        with (
            _patched_lifespan(MagicMock(), tasks),
            patch.object(
                main.terminal_service,
                "recover_interrupted_deferred_init_external_owners",
                side_effect=rec_ext,
            ),
            patch.object(
                main.terminal_service,
                "recover_interrupted_initial_deliveries",
                side_effect=rec_init,
            ),
            patch.object(ephemeral_service, "sweep", side_effect=fake_sweep),
            patch.object(
                main.terminal_service,
                "reattach_surviving_terminals",
                side_effect=fake_reattach,
            ),
        ):
            async with lifespan(app):
                order.append("serving")

        assert order == [
            "deferred_ext",
            "initial_delivery",
            "ephemeral:True",
            "reattach",
            "serving",
        ]

    @pytest.mark.asyncio
    async def test_failing_reattach_does_not_block_startup(self, caplog) -> None:
        tasks: list = []
        served = False
        with (
            _patched_lifespan(MagicMock(), tasks),
            patch.object(ephemeral_service, "sweep", return_value={}),
            patch.object(
                main.terminal_service,
                "reattach_surviving_terminals",
                side_effect=RuntimeError("reattach boom"),
            ),
            caplog.at_level(logging.WARNING, logger=main.__name__),
        ):
            async with lifespan(app):
                served = True

        assert served
        assert "surviving terminal re-attach failed" in caplog.text


class TestIdleSurvivorWatchdog:
    """P2-1: re-attached idle survivors must not trigger cold-start 5x replay or watchdog drop."""

    def test_idle_survivor_no_cold_start_replay_stays_watched_and_catches_real_stall(
        self, monkeypatch, tmp_path, caplog
    ):
        fifo_dir = tmp_path / "fifos"
        fifo_dir.mkdir()
        monkeypatch.setattr(fifo_reader, "FIFO_DIR", fifo_dir)
        monkeypatch.setattr(ts, "FIFO_DIR", fifo_dir)
        monkeypatch.setattr(fifo_reader, "PIPE_LIVENESS_COLD_START_GRACE_S", 0.0)

        fresh_fifo = FifoManager()
        monkeypatch.setattr(ts, "fifo_manager", fresh_fifo)
        # Prevent background watchdog thread from racing with deterministic ticks.
        monkeypatch.setattr(fresh_fifo, "_ensure_watchdog", lambda: None)

        pane_content = "> ready at prompt\n"
        backend = _make_pipe_backend(live_ids={"term-idle"})
        backend.get_history.side_effect = lambda *a, **k: pane_content
        monkeypatch.setattr(ts, "get_backend", lambda: backend)

        published: list[dict] = []
        monkeypatch.setattr(
            fifo_reader.bus, "publish", lambda topic, payload: published.append(payload)
        )

        try:
            with caplog.at_level(logging.WARNING, logger=fifo_reader.__name__):
                ts.attach_output_pipeline("term-idle", "cao-s", "win-idle", replace_existing=True)

                # Reset mocks after the initial replace_existing stop+pipe setup.
                backend.stop_pipe_pane.reset_mock()
                backend.pipe_pane.reset_mock()

                # Run more ticks than PIPE_LIVENESS_MAX_COLD_START_ATTEMPTS while pane is idle.
                for _ in range(PIPE_LIVENESS_MAX_COLD_START_ATTEMPTS + 3):
                    fresh_fifo._check_pipe_liveness("term-idle")

                assert published == []
                backend.stop_pipe_pane.assert_not_called()
                backend.pipe_pane.assert_not_called()
                assert caplog.records == []
                assert "term-idle" in fresh_fifo._pane_probe

                # Now simulate a real stall: pane changes while FIFO delivers nothing.
                pane_content = "> working on new task\n"
                for _ in range(PIPE_LIVENESS_STALL_CHECKS):
                    fresh_fifo._check_pipe_liveness("term-idle")

                backend.stop_pipe_pane.assert_called_once_with("cao-s", "win-idle")
                backend.pipe_pane.assert_called_once_with(
                    "cao-s", "win-idle", str(fifo_dir / "term-idle.fifo")
                )
                assert published == [{"data": "> working on new task\r\n"}]
                assert "term-idle" in fresh_fifo._pane_probe
        finally:
            fresh_fifo.stop_reader("term-idle")


@pytest.mark.integration
class TestRealTmuxReattach:
    """Real tmux + FIFO tests for P2-1 (idle survivor) and P3-1 (duplicate attach)."""

    def test_real_tmux_idle_survivor_and_duplicate_attach_non_toggle(
        self, monkeypatch, tmp_path, caplog
    ):
        if not shutil.which("tmux"):
            pytest.skip("tmux not installed")

        import libtmux

        from cli_agent_orchestrator.backends.tmux_backend import TmuxBackend
        from cli_agent_orchestrator.clients.tmux import TmuxClient

        socket_name = f"cao-test-reattach-{uuid.uuid4().hex[:12]}"
        subprocess.run(["tmux", "-L", socket_name, "kill-server"], capture_output=True)

        fifo_dir = tmp_path / "fifos"
        fifo_dir.mkdir()
        monkeypatch.setattr(fifo_reader, "FIFO_DIR", fifo_dir)
        monkeypatch.setattr(ts, "FIFO_DIR", fifo_dir)
        monkeypatch.setattr(fifo_reader, "PIPE_LIVENESS_COLD_START_GRACE_S", 0.0)

        client = TmuxClient()
        client.server = libtmux.Server(socket_name=socket_name)
        backend = TmuxBackend(client=client)
        monkeypatch.setattr(ts, "get_backend", lambda: backend)

        fresh_fifo = FifoManager()
        monkeypatch.setattr(ts, "fifo_manager", fresh_fifo)
        monkeypatch.setattr(fresh_fifo, "_ensure_watchdog", lambda: None)

        published: list[str] = []
        monkeypatch.setattr(
            fifo_reader.bus,
            "publish",
            lambda topic, payload: published.append(payload.get("data", "")),
        )

        tid = "term-real-reattach"
        sess = "cao-reattach-test"
        try:
            win = client.create_session(sess, "win1", tid, str(tmp_path))
            # Produce pre-restart content in the pane and wait for it to settle idle.
            subprocess.run(
                [
                    "tmux",
                    "-L",
                    socket_name,
                    "send-keys",
                    "-t",
                    f"{sess}:{win}",
                    "echo SURVIVOR_PRE_RESTART",
                    "Enter",
                ],
                check=True,
            )
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if "SURVIVOR_PRE_RESTART" in backend.get_history(sess, win):
                    break
                time.sleep(0.05)
            time.sleep(0.2)

            # Re-attach survivor pipeline (as cao-server startup does).
            with caplog.at_level(logging.WARNING, logger=fifo_reader.__name__):
                ts.attach_output_pipeline(tid, sess, win, replace_existing=True)

                def _pane_pipe_flag() -> str:
                    res = subprocess.run(
                        [
                            "tmux",
                            "-L",
                            socket_name,
                            "display-message",
                            "-t",
                            f"{sess}:{win}",
                            "-p",
                            "#{pane_pipe}",
                        ],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    return res.stdout.strip()

                assert _pane_pipe_flag() == "1"

                # P3-1: redundant attach must NOT toggle #{pane_pipe} off to "0".
                ts.attach_output_pipeline(tid, sess, win, replace_existing=False)
                assert _pane_pipe_flag() == "1"

                # P2-1: idle survivor must NOT trigger cold-start replays or drop from watchdog.
                published.clear()
                for _ in range(PIPE_LIVENESS_MAX_COLD_START_ATTEMPTS + 2):
                    fresh_fifo._check_pipe_liveness(tid)

                assert published == []
                assert caplog.records == []
                assert tid in fresh_fifo._pane_probe

            # Real post-reattach output still flows through the real tmux pipe + FIFO.
            subprocess.run(
                [
                    "tmux",
                    "-L",
                    socket_name,
                    "send-keys",
                    "-t",
                    f"{sess}:{win}",
                    "echo POST_REATTACH_LIVE",
                    "Enter",
                ],
                check=True,
            )
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if "POST_REATTACH_LIVE" in "".join(published):
                    break
                time.sleep(0.05)
            assert "POST_REATTACH_LIVE" in "".join(published)
        finally:
            fresh_fifo.stop_reader(tid)
            subprocess.run(["tmux", "-L", socket_name, "kill-server"], capture_output=True)

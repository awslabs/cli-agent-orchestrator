"""Tests for the deferred-init submit-verification guard.

The deferred-init delivery (send_input: paste -> fixed sleep -> Enter) can drop
the Enter (message left in the box) or the whole paste (TUI not input-ready).
Nothing blocks on completion in that path, so a dropped submit would leave the
worker idle forever. These cover the confirm + re-submit logic that closes it.
"""

import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import terminal_service as ts


class TestMessageVisibleInBox:
    @staticmethod
    def _visible_in_current_composer(composer, message):
        with patch.object(ts, "_capture_current_composer_region", return_value=composer):
            return ts._message_visible_in_box("t1", message)

    def test_true_when_current_composer_adds_message(self):
        assert self._visible_in_current_composer("❯ Analyze the logs now", "Analyze the logs")

    def test_false_when_current_viewport_does_not_add_message(self):
        assert not self._visible_in_current_composer("❯ (empty prompt)", "Analyze the logs")

    def test_current_composer_capture_delegates_plain_viewport_to_provider(self):
        backend = MagicMock()
        backend.get_history.return_value = "old history\n› Analyze the logs carefully"
        provider = MagicMock()
        provider.extract_current_composer.return_value = "› Analyze the logs carefully"
        with (
            patch.object(
                ts,
                "get_terminal_metadata",
                return_value={"tmux_session": "session", "tmux_window": "window"},
            ),
            patch.object(ts, "get_backend", return_value=backend),
            patch.object(ts, "provider_manager") as manager,
        ):
            manager.get_provider.return_value = provider
            assert ts._capture_current_composer_region("t1") == "› Analyze the logs carefully"
        backend.get_history.assert_called_once_with(
            "session", "window", strip_escapes=True, visible_only=True
        )
        provider.extract_current_composer.assert_called_once_with(
            "old history\n› Analyze the logs carefully"
        )

    def test_send_input_does_not_retain_a_viewport_before_paste(self):
        events = []
        backend = MagicMock()
        backend.send_keys.side_effect = lambda *args, **kwargs: events.append("paste")
        with (
            patch.object(
                ts,
                "get_terminal_metadata",
                return_value={"tmux_session": "session", "tmux_window": "window"},
            ),
            patch.object(ts, "provider_manager") as manager,
            patch.object(ts, "inject_memory_context", return_value="Analyze the logs"),
            patch.object(ts.status_monitor, "notify_input_sent"),
            patch.object(ts.status_monitor, "clear_rolling_buffer"),
            patch.object(ts, "get_backend", return_value=backend),
            patch.object(ts, "update_last_active"),
        ):
            manager.get_provider.return_value = None
            assert ts.send_input("t1", "Analyze the logs")
        assert events == ["paste"]

    def test_false_when_message_too_short(self):
        with patch.object(ts, "_capture_current_composer_region") as capture:
            assert ts._message_visible_in_box("t1", "go") is False
            capture.assert_not_called()

    def test_false_when_output_fetch_raises(self):
        assert not self._visible_in_current_composer(None, "Analyze the logs")

    def test_current_composer_capture_rejects_provider_without_composer_contract(self):
        backend = MagicMock()
        backend.get_history.return_value = "› Analyze the logs"
        provider = MagicMock()
        provider.extract_current_composer.return_value = None
        with (
            patch.object(
                ts,
                "get_terminal_metadata",
                return_value={"tmux_session": "session", "tmux_window": "window"},
            ),
            patch.object(ts, "get_backend", return_value=backend),
            patch.object(ts, "provider_manager") as manager,
        ):
            manager.get_provider.return_value = provider
            assert ts._capture_current_composer_region("t1") is None

    def test_match_survives_wrapping_and_whitespace(self):
        assert self._visible_in_current_composer(
            "❯ Analyze the\n  logs carefully", "Analyze the logs"
        )

    def test_current_composer_tail_handles_wrapping_and_unicode(self):
        message = "Analyze the logs carefully and preserve the current composer message tail"
        tail = ts._normalized_box_text(message)[-ts._CURRENT_COMPOSER_PROBE_MAX_CHARS :]
        with patch.object(
            ts, "_capture_current_composer_region", return_value=f"› {tail[:24]}\n{tail[24:]}"
        ):
            assert ts._message_visible_in_box("t1", message)


class TestRedeliverDroppedMessageHelper:
    """The shared one-attempt helper: a caller without a provider instance
    (the synchronous step path, #562) gets it resolved from the registry,
    best-effort — a resolution failure means no probe, never a lost
    redelivery."""

    def test_resolves_provider_from_registry_for_direct_probe(self):
        # Provider without explicit pass + direct probe True → started, no send.
        provider = MagicMock(supports_direct_status_probe=True)
        with (
            patch.object(ts, "provider_manager") as mgr,
            patch.object(ts, "_worker_is_started_direct", return_value=True) as probe,
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            mgr.get_provider.return_value = provider
            started = ts.redeliver_dropped_message("t1", "Analyze the logs", 1)
        assert started is True
        mgr.get_provider.assert_called_once_with("t1")
        probe.assert_called_once_with("t1", provider)
        key.assert_not_called()
        send.assert_not_called()

    def test_provider_resolution_failure_falls_through_to_box_check(self):
        # Registry blowup must not lose the redelivery — box check still runs.
        with (
            patch.object(ts, "provider_manager") as mgr,
            patch.object(ts, "_message_visible_in_box", return_value=True) as box,
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            mgr.get_provider.side_effect = ValueError("Terminal t1 not found")
            started = ts.redeliver_dropped_message("t1", "Analyze the logs", 1)
        assert started is False
        box.assert_called_once_with("t1", "Analyze the logs")
        key.assert_called_once_with("t1", "Enter")
        send.assert_not_called()

    def test_gate_on_probe_capable_still_full_resends_when_box_empty(self):
        # Gated step path: probe ran and said not-started, text absent → the
        # probe ruled out a working worker, so the full re-send is safe.
        provider = MagicMock(supports_direct_status_probe=True)
        with (
            patch.object(ts, "_worker_is_started_direct", return_value=False),
            patch.object(ts, "_message_visible_in_box", return_value=False),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            started = ts.redeliver_dropped_message(
                "t1", "Analyze the logs", 1, provider, full_resend_requires_probe=True
            )
        assert started is False
        key.assert_not_called()
        send.assert_called_once()

    def test_gate_on_skips_full_resend_without_probe(self):
        # Gated step path + non-probe provider + text absent: cannot tell
        # "paste dropped" from "worker running, prompt scrolled off" — the
        # full re-send would risk a duplicate task, so nothing is sent.
        provider = MagicMock(supports_direct_status_probe=False)
        with (
            patch.object(ts, "_worker_is_started_direct") as probe,
            patch.object(ts, "_message_visible_in_box", return_value=False),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            started = ts.redeliver_dropped_message(
                "t1", "Analyze the logs", 1, provider, full_resend_requires_probe=True
            )
        assert started is False
        probe.assert_not_called()
        key.assert_not_called()
        send.assert_not_called()

    def test_gate_on_still_sends_bare_enter_without_probe(self):
        # Gated step path + non-probe provider + text VISIBLE: a bare Enter
        # cannot duplicate a task, so the Enter-swallowed recovery survives
        # the gate.
        provider = MagicMock(supports_direct_status_probe=False)
        with (
            patch.object(ts, "_message_visible_in_box", return_value=True),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            started = ts.redeliver_dropped_message(
                "t1", "Analyze the logs", 1, provider, full_resend_requires_probe=True
            )
        assert started is False
        key.assert_called_once_with("t1", "Enter")
        send.assert_not_called()

    def test_gate_off_default_keeps_deferred_init_behavior(self):
        # Deferred-init path (default): non-probe provider + text absent →
        # full re-send, exactly as before the helper was extracted.
        provider = MagicMock(supports_direct_status_probe=False)
        with (
            patch.object(ts, "_message_visible_in_box", return_value=False),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            started = ts.redeliver_dropped_message("t1", "Analyze the logs", 1, provider)
        assert started is False
        key.assert_not_called()
        send.assert_called_once()


@pytest.mark.asyncio
class TestConfirmWorkerStartedOrResubmit:
    async def test_started_on_first_confirm_no_resubmit(self):
        with (
            patch.object(ts, "wait_until_status", new=AsyncMock(return_value=True)),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            ok = await ts._confirm_worker_started_or_resubmit(
                "t1", "Analyze the logs", None, "sup", None
            )
        assert ok is True
        key.assert_not_called()
        send.assert_not_called()

    async def test_enter_resubmit_when_message_in_box(self):
        # First confirm fails, box shows our text (Enter swallowed) → bare Enter,
        # second confirm succeeds.
        with (
            patch.object(ts, "wait_until_status", new=AsyncMock(side_effect=[False, True])),
            patch.object(ts, "_message_visible_in_box", return_value=True),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            ok = await ts._confirm_worker_started_or_resubmit(
                "t1", "Analyze the logs", None, "sup", None
            )
        assert ok is True
        key.assert_called_once_with("t1", "Enter")
        send.assert_not_called()

    async def test_full_redeliver_when_box_empty(self):
        # First confirm fails, box empty (paste dropped) → re-deliver full msg.
        with (
            patch.object(ts, "wait_until_status", new=AsyncMock(side_effect=[False, True])),
            patch.object(ts, "_message_visible_in_box", return_value=False),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            ok = await ts._confirm_worker_started_or_resubmit(
                "t1", "Analyze the logs", "reg", "sup", None
            )
        assert ok is True
        key.assert_not_called()
        send.assert_called_once()
        assert send.call_args.args[0] == "t1"
        assert send.call_args.args[1] == "Analyze the logs"

    async def test_returns_false_when_worker_never_starts(self):
        # Every confirm fails through all resubmit attempts.
        with (
            patch.object(ts, "wait_until_status", new=AsyncMock(return_value=False)),
            patch.object(ts, "_message_visible_in_box", return_value=True),
            patch.object(ts, "send_special_key"),
            patch.object(ts, "send_input"),
        ):
            ok = await ts._confirm_worker_started_or_resubmit(
                "t1", "Analyze the logs", None, "sup", None
            )
        assert ok is False

    async def test_direct_probe_short_circuits_when_worker_started(self):
        # Provider with supports_direct_status_probe=True + direct probe True →
        # returns True without calling send_input or send_special_key.
        provider = MagicMock(supports_direct_status_probe=True)
        with (
            patch.object(ts, "wait_until_status", new=AsyncMock(return_value=False)),
            patch.object(ts, "_worker_is_started_direct", return_value=True),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            ok = await ts._confirm_worker_started_or_resubmit(
                "t1",
                "Analyze the logs",
                None,
                "sup",
                None,
                provider=provider,
            )
        assert ok is True
        key.assert_not_called()
        send.assert_not_called()

    async def test_direct_probe_falls_through_when_worker_not_started(self):
        # Direct probe returns False → continues to existing resubmit logic.
        provider = MagicMock(supports_direct_status_probe=True)
        with (
            patch.object(ts, "wait_until_status", new=AsyncMock(side_effect=[False, True])),
            patch.object(ts, "_worker_is_started_direct", return_value=False),
            patch.object(ts, "_message_visible_in_box", return_value=True),
            patch.object(ts, "send_special_key") as key,
            patch.object(ts, "send_input") as send,
        ):
            ok = await ts._confirm_worker_started_or_resubmit(
                "t1",
                "Analyze the logs",
                None,
                "sup",
                None,
                provider=provider,
            )
        assert ok is True
        key.assert_called_once()
        send.assert_not_called()

    async def test_direct_probe_skipped_when_provider_not_opted_in(self):
        # Provider without supports_direct_status_probe → direct probe never
        # invoked; falls through to existing resubmit logic.
        provider = MagicMock(supports_direct_status_probe=False)
        with (
            patch.object(ts, "wait_until_status", new=AsyncMock(side_effect=[False, True])),
            patch.object(ts, "_worker_is_started_direct") as probe,
            patch.object(ts, "_message_visible_in_box", return_value=True),
            patch.object(ts, "send_special_key"),
            patch.object(ts, "send_input"),
        ):
            ok = await ts._confirm_worker_started_or_resubmit(
                "t1",
                "Analyze the logs",
                None,
                "sup",
                None,
                provider=provider,
            )
        assert ok is True
        probe.assert_not_called()

    async def test_provider_none_skips_direct_probe(self):
        # The existing None-provider path still works unchanged.
        with (
            patch.object(ts, "wait_until_status", new=AsyncMock(side_effect=[False, True])),
            patch.object(ts, "_worker_is_started_direct") as probe,
            patch.object(ts, "_message_visible_in_box", return_value=True),
            patch.object(ts, "send_special_key"),
            patch.object(ts, "send_input"),
        ):
            ok = await ts._confirm_worker_started_or_resubmit(
                "t1",
                "Analyze the logs",
                None,
                "sup",
                None,
                provider=None,
            )
        assert ok is True
        probe.assert_not_called()


class TestWorkerIsStartedDirect:
    """Unit tests for the capture-pane direct status probe."""

    def test_execution_evidence_provider_error_stops_redelivery(self):
        provider = MagicMock(
            supports_direct_status_probe=True,
            requires_execution_evidence=True,
        )
        with (
            patch.object(
                ts,
                "get_terminal_metadata",
                return_value={"tmux_session": "s1", "tmux_window": "w1"},
            ),
            patch.object(ts.status_monitor, "probe_execution_evidence", return_value=False),
            patch.object(ts.status_monitor, "get_status", return_value=TerminalStatus.ERROR),
        ):
            assert ts._worker_is_started_direct("t1", provider) is True

    def test_returns_false_when_metadata_is_none(self):
        with patch.object(ts, "get_terminal_metadata", return_value=None):
            assert ts._worker_is_started_direct("t1", MagicMock()) is False

    def test_returns_false_when_session_key_missing(self):
        with patch.object(ts, "get_terminal_metadata", return_value={"tmux_window": "w1"}):
            assert ts._worker_is_started_direct("t1", MagicMock()) is False

    def test_returns_false_when_window_key_missing(self):
        with patch.object(ts, "get_terminal_metadata", return_value={"tmux_session": "s1"}):
            assert ts._worker_is_started_direct("t1", MagicMock()) is False

    def test_returns_false_when_get_history_raises(self):
        with (
            patch.object(
                ts,
                "get_terminal_metadata",
                return_value={
                    "tmux_session": "s1",
                    "tmux_window": "w1",
                },
            ),
            patch.object(ts, "get_backend") as mock_be,
        ):
            mock_be.return_value.get_history.side_effect = Exception("capture failed")
            assert ts._worker_is_started_direct("t1", MagicMock()) is False

    def test_returns_false_when_get_status_raises(self):
        provider = MagicMock()
        provider.get_status.side_effect = Exception("parse failure")
        with (
            patch.object(
                ts,
                "get_terminal_metadata",
                return_value={
                    "tmux_session": "s1",
                    "tmux_window": "w1",
                },
            ),
            patch.object(ts, "get_backend") as mock_be,
        ):
            assert ts._worker_is_started_direct("t1", provider) is False

    def test_returns_true_when_status_is_processing(self):
        from cli_agent_orchestrator.models.terminal import TerminalStatus

        provider = MagicMock()
        provider.get_status.return_value = TerminalStatus.PROCESSING
        with (
            patch.object(
                ts,
                "get_terminal_metadata",
                return_value={
                    "tmux_session": "s1",
                    "tmux_window": "w1",
                },
            ),
            patch.object(ts, "get_backend") as mock_be,
        ):
            assert ts._worker_is_started_direct("t1", provider) is True

    def test_returns_false_when_status_is_idle(self):
        from cli_agent_orchestrator.models.terminal import TerminalStatus

        provider = MagicMock()
        provider.get_status.return_value = TerminalStatus.IDLE
        with (
            patch.object(
                ts,
                "get_terminal_metadata",
                return_value={
                    "tmux_session": "s1",
                    "tmux_window": "w1",
                },
            ),
            patch.object(ts, "get_backend") as mock_be,
        ):
            assert ts._worker_is_started_direct("t1", provider) is False

    def test_kimi_code_current_buffer_proves_initial_task_started(self):
        """Regression: a fast Kimi turn may never update the cached status edge.

        Deferred init must use current-dispatch bytes before deciding the paste
        was dropped; otherwise it re-sends the already-executed initial task and
        eventually tears the terminal down.
        """

        import time

        from cli_agent_orchestrator.models.terminal import TerminalStatus
        from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider, KimiDialect
        from cli_agent_orchestrator.services import status_monitor as sm
        from cli_agent_orchestrator.services.status_monitor import StatusMonitor

        ready = (
            "● Finished the requested task.\n"
            "── input ─────────────────────────────────\n"
            "──────────────────────────────────────────\n"
            "auto  agent (DeepSeek V4.1 Flash ●)  /tmp/project  ctrl-o: editor\n"
            "context: 1.0% (2.6k/262.1k)\n"
        )
        provider = KimiCliProvider("t1", "s1", "w1")
        provider._dialect = KimiDialect.CODE
        monitor = StatusMonitor()
        with patch.object(sm.provider_manager, "get_provider", return_value=provider):
            monitor.clear_rolling_buffer(provider.terminal_id, provider)
            provider.mark_input_received()
            with (
                patch.object(monitor, "_schedule_raw_detection"),
                patch.object(monitor, "_schedule_screen_detection"),
            ):
                monitor._process_chunk(provider.terminal_id, "⠙ Thinking… 1s · 4 tokens\n" + ready)
        provider._last_dispatch_time = time.time() - 9.0

        with (
            patch.object(
                ts,
                "get_terminal_metadata",
                return_value={"tmux_session": "s1", "tmux_window": "w1"},
            ),
            patch.object(ts, "get_backend") as service_backend,
            patch("cli_agent_orchestrator.providers.kimi_cli.get_backend") as kimi_backend,
            patch.object(ts, "status_monitor", monitor),
        ):
            service_backend.return_value.get_history.return_value = ready
            kimi_backend.return_value.get_history.return_value = ready
            assert ts._worker_is_started_direct("t1", provider) is True
            assert provider.get_status(ready) is TerminalStatus.COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [False, True])
async def test_kimi_confirmation_requires_execution_even_when_cached_status_says_started(accepted):
    from pathlib import Path

    from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider, KimiDialect

    fixtures = Path(__file__).parents[1] / "providers" / "fixtures"
    idle = (fixtures / "kimi_code_0431_01_fresh_startup_idle.txt").read_text()
    completed = (fixtures / "kimi_code_0431_04_post_answer_idle.txt").read_text()
    provider = KimiCliProvider("t1", "s1", "w1")
    provider._dialect = KimiDialect.CODE
    provider.mark_input_received()
    backend = MagicMock()
    backend.get_history.return_value = completed  # Previous turn remains in scrollback.
    execution_probe = (
        MagicMock(side_effect=[False, True]) if accepted else MagicMock(return_value=False)
    )
    with (
        patch.object(
            ts, "get_terminal_metadata", return_value={"tmux_session": "s1", "tmux_window": "w1"}
        ),
        patch.object(ts, "get_backend", return_value=backend),
        patch.object(ts.status_monitor, "probe_execution_evidence", execution_probe),
        patch.object(ts, "wait_until_status", new=AsyncMock(return_value=True)),
        patch.object(ts, "_DEFERRED_SUBMIT_CONFIRM_TIMEOUT", 8.0 if accepted else 0.0),
        patch.object(ts.asyncio, "sleep", new=AsyncMock()) as sleep,
        patch.object(ts, "_message_visible_in_box", return_value=False),
        patch.object(ts, "send_input") as send,
    ):
        assert (
            await ts._confirm_worker_started_or_resubmit(
                "t1", "Run the task", None, None, None, provider
            )
            is accepted
        )
    if accepted:
        sleep.assert_awaited_once_with(0.5)
        send.assert_not_called()
    else:
        assert send.call_count == ts._DEFERRED_SUBMIT_MAX_RESUBMITS


@pytest.mark.parametrize(
    "current",
    ["", "⠙ Thinking… 1s · 4 tokens\n", "⠙ Thinking… 1s · 4 tokens\n● Finished the new task."],
)
def test_kimi_send_input_bounds_execution_evidence_to_new_bytes(monkeypatch, current):
    from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider, KimiDialect
    from cli_agent_orchestrator.services.status_monitor import StatusMonitor

    monitor = StatusMonitor()
    provider = KimiCliProvider("review-dispatch-boundary", "s", "w")
    provider._dialect = KimiDialect.CODE
    old = "● Finished the previous task."
    monitor._buffers[provider.terminal_id] = old
    provider.mark_input_received()
    assert provider.has_execution_evidence("⠙ Thinking… 1s · 4 tokens\n" + old) is True
    backend = MagicMock()
    backend.get_history.return_value = old

    def accept_paste(*args, **kwargs):
        assert monitor.get_buffer(provider.terminal_id) == ""
        assert provider._execution_observed is False
        monitor._buffers[provider.terminal_id] = current

    backend.send_keys.side_effect = accept_paste
    monkeypatch.setattr(ts, "status_monitor", monitor)
    monkeypatch.setattr(ts, "get_backend", lambda: backend)
    monkeypatch.setattr(
        ts, "get_terminal_metadata", lambda _: {"tmux_session": "s", "tmux_window": "w"}
    )
    monkeypatch.setattr(ts.provider_manager, "get_provider", lambda _: provider)
    monkeypatch.setattr(ts, "inject_memory_context", lambda message, *_: message)
    monkeypatch.setattr(ts, "update_last_active", lambda _: None)
    assert ts.send_input(provider.terminal_id, "Run the new task") is True
    assert ts._worker_is_started_direct(provider.terminal_id, provider) is bool(current)
    backend.get_history.assert_not_called()


def test_kimi_direct_probe_cannot_import_old_execution_across_buffer_epoch(monkeypatch):
    """Reviewer round 4: an in-flight old probe cannot certify a new dispatch.

    The first probe deliberately pauses *inside* the stateful evidence parser.
    A concurrent generation reset is then queued.  The StatusMonitor lock must
    serialize them so the reset happens after the old probe's latch mutation and
    clears it before the new generation can be queried.
    """

    from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider, KimiDialect
    from cli_agent_orchestrator.services.status_monitor import StatusMonitor

    monitor = StatusMonitor()
    provider = KimiCliProvider("review-generation-race", "s", "w")
    provider._dialect = KimiDialect.CODE
    monitor.clear_rolling_buffer(provider.terminal_id, provider)
    provider.mark_input_received()
    monitor._buffers[provider.terminal_id] = "⠙ Thinking… old turn\n"

    entered_parser = threading.Event()
    release_parser = threading.Event()
    reset_started = threading.Event()
    original = provider.has_execution_evidence
    first_call = True

    def paused_checker(output):
        nonlocal first_call
        if first_call:
            first_call = False
            entered_parser.set()
            assert release_parser.wait(timeout=2.0)
        return original(output)

    provider.has_execution_evidence = paused_checker
    monkeypatch.setattr(ts, "status_monitor", monitor)
    monkeypatch.setattr(
        ts, "get_terminal_metadata", lambda _: {"tmux_session": "s", "tmux_window": "w"}
    )

    old_probe_result = []

    def old_probe():
        old_probe_result.append(ts._worker_is_started_direct(provider.terminal_id, provider))

    def reset_generation():
        reset_started.set()
        monitor.clear_rolling_buffer(provider.terminal_id, provider)
        provider.mark_input_received()

    probe_thread = threading.Thread(target=old_probe)
    reset_thread = threading.Thread(target=reset_generation)
    probe_thread.start()
    assert entered_parser.wait(timeout=2.0)
    reset_thread.start()
    assert reset_started.wait(timeout=2.0)
    release_parser.set()
    probe_thread.join(timeout=2.0)
    reset_thread.join(timeout=2.0)
    assert not probe_thread.is_alive()
    assert not reset_thread.is_alive()

    # The old generation really contained execution, so its own probe may
    # return True.  The reset that follows it must erase that latch for the new
    # empty generation; no stale bytes can be imported across the boundary.
    assert old_probe_result == [True]
    assert monitor.get_buffer(provider.terminal_id) == ""
    assert ts._worker_is_started_direct(provider.terminal_id, provider) is False
    assert provider._execution_observed is False


def test_kimi_paused_old_probe_cannot_certify_real_new_send_input(monkeypatch):
    """Match the maintainer's two-thread send_input interleaving end to end."""

    from cli_agent_orchestrator.models.terminal import TerminalStatus
    from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider, KimiDialect
    from cli_agent_orchestrator.services.status_monitor import StatusMonitor

    monitor = StatusMonitor()
    provider = KimiCliProvider("review-real-send-race", "s", "w")
    provider._dialect = KimiDialect.CODE
    monitor.clear_rolling_buffer(provider.terminal_id, provider)
    provider.mark_input_received()
    monitor._buffers[provider.terminal_id] = "⠙ Thinking… old turn\n"

    entered_parser = threading.Event()
    release_parser = threading.Event()
    send_entered_notify = threading.Event()
    send_keys_seen = threading.Event()
    original_checker = provider.has_execution_evidence
    original_notify = monitor.notify_input_sent
    first_call = True

    def paused_checker(output):
        nonlocal first_call
        if first_call:
            first_call = False
            entered_parser.set()
            assert release_parser.wait(timeout=2.0)
        return original_checker(output)

    def notify_input_sent(*args, **kwargs):
        send_entered_notify.set()
        return original_notify(*args, **kwargs)

    provider.has_execution_evidence = paused_checker
    monitor.notify_input_sent = notify_input_sent
    monitor.get_status = lambda _: TerminalStatus.IDLE
    backend = MagicMock()
    backend.send_keys.side_effect = lambda *args, **kwargs: send_keys_seen.set()
    monkeypatch.setattr(ts, "status_monitor", monitor)
    monkeypatch.setattr(ts, "get_backend", lambda: backend)
    monkeypatch.setattr(
        ts, "get_terminal_metadata", lambda _: {"tmux_session": "s", "tmux_window": "w"}
    )
    monkeypatch.setattr(ts.provider_manager, "get_provider", lambda _: provider)
    monkeypatch.setattr(ts, "inject_memory_context", lambda message, *_: message)
    monkeypatch.setattr(ts, "update_last_active", lambda _: None)

    old_probe_result = []
    send_result = []
    probe_thread = threading.Thread(
        target=lambda: old_probe_result.append(
            ts._worker_is_started_direct(provider.terminal_id, provider)
        )
    )
    send_thread = threading.Thread(
        target=lambda: send_result.append(ts.send_input(provider.terminal_id, "new task"))
    )
    probe_thread.start()
    assert entered_parser.wait(timeout=2.0)
    send_thread.start()
    assert send_entered_notify.wait(timeout=2.0)

    # On the buggy implementation get_buffer() had already released the monitor
    # lock, so the new send reaches send_keys before the old parser resumes.  On
    # the fixed implementation the send blocks at the generation lock instead.
    send_keys_seen.wait(timeout=0.1)
    release_parser.set()
    probe_thread.join(timeout=2.0)
    send_thread.join(timeout=2.0)
    assert not probe_thread.is_alive()
    assert not send_thread.is_alive()
    assert old_probe_result == [True]
    assert send_result == [True]
    assert send_keys_seen.is_set()

    # The new paste deliberately emits no bytes.  The reset performed by the
    # real send_input path therefore leaves the new generation unconfirmed;
    # stale execution from the old probe cannot suppress recovery.
    assert monitor.get_buffer(provider.terminal_id) == ""
    assert provider._execution_observed is False
    assert ts._worker_is_started_direct(provider.terminal_id, provider) is False

"""The dispatch boundary is sampled at the last point before the pane write (#709).

``dispatch_input`` used to take its boundary before the provider bookkeeping and
before the backend's own preparation (on tmux: cancel, foreground-command probe,
``load-buffer``). Output landing in that window pushed the output generation past
the boundary, so a status earned from that OLD output confirmed a send whose
Enter was then swallowed, and the recovery was skipped.

Every test here drives the real ``dispatch_input``, a real ``StatusMonitor`` and
the real confirmation waits; only the pane, the provider and the DB are stubbed.
"""

from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, Optional
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.backends.tmux_backend import TmuxBackend
from cli_agent_orchestrator.clients.tmux import TmuxClient
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import inbox_service as inbox_mod
from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.inbox_service import InboxService
from cli_agent_orchestrator.services.status_monitor import StatusMonitor

TID = "test1234"
# The output generation before the dispatch starts: the old sampling point.
BASELINE = 3
STALE_STATUSES = [TerminalStatus.PROCESSING, TerminalStatus.COMPLETED]


def _monitor() -> StatusMonitor:
    sm = StatusMonitor()
    with sm._lock:
        sm._last_status[TID] = TerminalStatus.IDLE
        sm._output_generation[TID] = BASELINE
        sm._capture_generation[TID] = BASELINE
        sm._status_generation[TID] = 1
    return sm


def _chunk(sm: StatusMonitor, detected: TerminalStatus) -> None:
    """One chunk of terminal output: what ``_process_chunk`` and its detection do."""
    with sm._lock:
        sm._output_generation[TID] += 1
        sm._capture_generation[TID] += 1
    sm._apply_detection(TID, detected)


def _provider() -> MagicMock:
    provider = MagicMock()
    provider.paste_enter_count = 1
    provider.paste_submit_delay = 0.0
    provider.assume_processing_on_dispatch = False
    provider.blocks_orchestrated_input_while_waiting_user_answer = False
    provider.has_pending_native_swarm = False
    return provider


class _Backend:
    """A pane that records what reached it and can emit at each step of a send.

    ``before_hook`` runs where a backend prepares (tmux ``load-buffer``),
    ``after_write`` once the text is in the pane. ``calls_hook=False`` is a
    backend that predates ``pre_write_hook`` and ignores it.
    """

    def __init__(
        self,
        *,
        before_hook: Optional[Callable[[], None]] = None,
        after_write: Optional[Callable[[], None]] = None,
        calls_hook: bool = True,
    ) -> None:
        self.before_hook = before_hook
        self.after_write = after_write
        self.calls_hook = calls_hook
        self.written: list[str] = []

    def supports_event_inbox(self) -> bool:
        return False

    def send_keys(
        self,
        session_name: str,
        window_name: str,
        keys: str,
        enter_count: int = 1,
        force_bracketed_paste: bool = False,
        submit_delay: float = 0.3,
        pre_write_hook: Optional[Callable[[], None]] = None,
    ) -> None:
        if self.before_hook is not None:
            self.before_hook()
        if self.calls_hook and pre_write_hook is not None:
            pre_write_hook()
        self.written.append(keys)
        if self.after_write is not None:
            self.after_write()


@contextmanager
def _wired(sm: Any, provider: MagicMock, backend: Any) -> Iterator[None]:
    pm = MagicMock()
    pm.get_provider.return_value = provider
    memory = MagicMock()
    memory.return_value.get_curated_memory_context.return_value = ""
    metadata: Dict[str, str] = {"tmux_session": "cao-s", "tmux_window": "w-abcd"}
    with (
        patch.object(terminal_service, "status_monitor", sm),
        patch.object(inbox_mod, "status_monitor", sm),
        patch.object(terminal_service, "provider_manager", pm),
        patch("cli_agent_orchestrator.services.status_monitor.provider_manager", pm),
        patch.object(terminal_service, "MemoryService", memory),
        patch.object(terminal_service, "update_last_active", MagicMock()),
        patch.object(terminal_service, "get_terminal_metadata", MagicMock(return_value=metadata)),
        patch("cli_agent_orchestrator.backends.registry._backend", backend),
        patch.object(inbox_mod, "INBOX_SUBMIT_CONFIRM_TIMEOUT", 0.3),
    ):
        yield


@pytest.mark.parametrize("stale", STALE_STATUSES)
class TestOutputBeforeThePaneWriteIsNotPickup:
    """Output between the old sampling point and the write must not confirm a send."""

    def test_output_during_provider_bookkeeping_does_not_confirm(self, isolated_memory_db, stale):
        """The probe from the #709 review: it passed on main with ``confirmed is True``."""
        sm = _monitor()
        provider = _provider()
        backend = _Backend()  # the paste lands, the Enter is swallowed: no output after it
        # record_dispatched_message runs after the old sample and before send_keys.
        provider.record_dispatched_message.side_effect = lambda _message: _chunk(sm, stale)

        with _wired(sm, provider, backend):
            boundary = terminal_service.dispatch_input(TID, "hello")
            observation = sm.status_observation(TID)
            confirmed = InboxService()._wait_for_pickup(TID, boundary)

        assert backend.written == ["hello"]
        assert (observation.status, observation.output_generation) == (stale, BASELINE + 1)
        assert confirmed is False
        assert boundary == BASELINE + 1

    def test_output_during_backend_preparation_does_not_confirm(self, isolated_memory_db, stale):
        sm = _monitor()
        backend = _Backend(before_hook=lambda: _chunk(sm, stale))

        with _wired(sm, _provider(), backend):
            boundary = terminal_service.dispatch_input(TID, "hello")
            confirmed = InboxService()._wait_for_pickup(TID, boundary)

        assert backend.written == ["hello"]
        assert confirmed is False
        assert boundary == BASELINE + 1

    def test_output_during_tmux_load_buffer_does_not_confirm(self, isolated_memory_db, stale):
        """The real tmux client: the hook must sit after ``load-buffer``, not before it."""
        sm = _monitor()
        with patch("cli_agent_orchestrator.clients.tmux.libtmux"):
            backend = TmuxBackend(TmuxClient())
        ran: list[list[str]] = []

        def run(argv: list[str], **_kwargs: Any) -> MagicMock:
            ran.append(argv)
            if argv[:2] == ["tmux", "load-buffer"]:
                _chunk(sm, stale)
            return MagicMock(returncode=0, stdout="")

        with (
            patch("cli_agent_orchestrator.clients.tmux.subprocess") as subprocess_mock,
            patch.object(TmuxClient, "_paste_buffer_sanitizes", True),
            patch.object(TmuxClient, "get_pane_current_command", return_value="node"),
            _wired(sm, _provider(), backend),
        ):
            subprocess_mock.run.side_effect = run
            boundary = terminal_service.dispatch_input(TID, "hello")
            confirmed = InboxService()._wait_for_pickup(TID, boundary)

        assert [argv[1] for argv in ran if argv[1] in ("load-buffer", "paste-buffer")] == [
            "load-buffer",
            "paste-buffer",
        ]
        assert confirmed is False
        assert boundary == BASELINE + 1

    @pytest.mark.asyncio
    async def test_first_task_confirmation_sees_the_later_boundary(self, isolated_memory_db, stale):
        """``_wait_for_post_dispatch_start`` gates on the same returned value."""
        sm = _monitor()
        backend = _Backend(before_hook=lambda: _chunk(sm, stale))

        with _wired(sm, _provider(), backend):
            boundary = terminal_service.dispatch_input(TID, "hello")
            started = await terminal_service._wait_for_post_dispatch_start(
                TID, boundary, timeout=0.3, polling_interval=0.05
            )

        assert started is False
        assert boundary == BASELINE + 1


class TestOutputAfterThePaneWriteStillConfirms:
    """The later boundary must not swallow this send's own output."""

    def test_pickup_after_the_write_confirms(self, isolated_memory_db):
        sm = _monitor()
        backend = _Backend(after_write=lambda: _chunk(sm, TerminalStatus.PROCESSING))

        with _wired(sm, _provider(), backend):
            boundary = terminal_service.dispatch_input(TID, "hello")
            confirmed = InboxService()._wait_for_pickup(TID, boundary)

        assert boundary == BASELINE
        assert confirmed is True

    @pytest.mark.parametrize("stale", STALE_STATUSES)
    def test_pickup_after_stale_output_in_the_window_confirms(self, isolated_memory_db, stale):
        """Old output moved the boundary; this send's PROCESSING is earned above it."""
        sm = _monitor()
        backend = _Backend(
            before_hook=lambda: _chunk(sm, stale),
            after_write=lambda: _chunk(sm, TerminalStatus.PROCESSING),
        )

        with _wired(sm, _provider(), backend):
            boundary = terminal_service.dispatch_input(TID, "hello")
            observation = sm.status_observation(TID)
            confirmed = InboxService()._wait_for_pickup(TID, boundary)

        assert boundary == BASELINE + 1
        assert (observation.status, observation.output_generation) == (
            TerminalStatus.PROCESSING,
            BASELINE + 2,
        )
        assert confirmed is True


class TestBoundaryFallback:
    """Without a usable hook sample the boundary is the one taken before send_keys."""

    def test_backend_that_never_calls_the_hook_keeps_the_earlier_boundary(self, isolated_memory_db):
        sm = _monitor()
        backend = _Backend(
            before_hook=lambda: _chunk(sm, TerminalStatus.PROCESSING), calls_hook=False
        )

        with _wired(sm, _provider(), backend):
            boundary = terminal_service.dispatch_input(TID, "hello")

        assert backend.written == ["hello"]
        assert boundary == BASELINE

    def test_failing_sample_in_the_hook_neither_blocks_nor_repeats_the_write(
        self, isolated_memory_db, caplog
    ):
        """The hook swallows its own failure: one write, the earlier boundary, a warning."""
        sm = MagicMock()
        sm.get_status.return_value = TerminalStatus.IDLE
        sm.output_generation.side_effect = [BASELINE, RuntimeError("monitor lock poisoned")]
        backend = _Backend()

        with _wired(sm, _provider(), backend), caplog.at_level("WARNING"):
            boundary = terminal_service.dispatch_input(TID, "hello")

        assert backend.written == ["hello"]
        assert sm.output_generation.call_count == 2
        assert boundary == BASELINE
        assert "Could not re-sample the dispatch boundary" in caplog.text

    def test_send_that_fails_before_the_write_never_runs_the_hook(self, isolated_memory_db):
        sm = _monitor()
        backend = MagicMock()
        backend.supports_event_inbox.return_value = False
        backend.send_keys.side_effect = RuntimeError("load-buffer failed")

        with _wired(sm, _provider(), backend), pytest.raises(RuntimeError, match="load-buffer"):
            terminal_service.dispatch_input(TID, "hello")

        assert backend.send_keys.call_count == 1
        assert callable(backend.send_keys.call_args.kwargs["pre_write_hook"])

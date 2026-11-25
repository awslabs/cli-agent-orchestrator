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


# ---------------------------------------------------------------------------
# The fast-turn case (#1498 review, haofeif and Kiro Crew, P2-1)
# ---------------------------------------------------------------------------

TASK = "write the quarterly report now"
I, P, C = TerminalStatus.IDLE, TerminalStatus.PROCESSING, TerminalStatus.COMPLETED

# The table from the #1498 review, one row per case. ``before`` is the status
# before the send, ``stale`` the old output that lands while the send is being
# prepared (first send only), ``turn`` what the worker prints after each write
# of the task. The last four columns are (writes of the task, outcome) on the
# first-task path and on the inbox path. What main (606c50a2) and the first
# version of this branch (580b97e7) did on the first-task path is in the id.
FAST_TURN_TABLE = [
    # main: 1 write, started.  580b97e7: 4 writes, not started.
    pytest.param(I, C, [C], 1, True, 1, False, id="stale-completed_turn-completed"),
    # main: 1 write, started.  580b97e7: 4 writes, not started.
    pytest.param(I, C, [I, C], 1, True, 1, False, id="stale-completed_turn-idle-completed"),
    # main: 1 write, started.  580b97e7: 1 write, started.
    pytest.param(I, C, [P, C], 1, True, 1, True, id="stale-completed_turn-processing-completed"),
    # main: 1 write, started.  580b97e7: 1 write, started.
    pytest.param(I, P, [C], 1, True, 1, True, id="stale-processing_turn-completed"),
    # The control. main: 4 writes, not started.  580b97e7: the same.  Not this PR's.
    pytest.param(C, None, [C], 4, False, 1, False, id="completed-before-send_turn-completed"),
    # main: 1 write, started.  580b97e7: 1 write, started.
    pytest.param(I, None, [C], 1, True, 1, True, id="no-stale-output_turn-completed"),
]


def _fast_turn_rig(before, stale, turn):
    """A monitor at ``before`` and a pane whose worker prints ``turn`` after each write."""
    sm = _monitor()
    with sm._lock:
        sm._last_status[TID] = before
    first = {"pending": stale is not None}

    def before_hook() -> None:
        if first["pending"]:
            first["pending"] = False
            _chunk(sm, stale)

    def after_write() -> None:
        for status in turn:
            _chunk(sm, status)

    provider = _provider()
    provider.supports_direct_status_probe = False
    provider.requires_execution_evidence = False
    provider.execution_evidence_ambiguous = False
    return sm, provider, _Backend(before_hook=before_hook, after_write=after_write)


@pytest.mark.parametrize("composer", [False, None], ids=["composer-empty", "composer-unreadable"])
@pytest.mark.parametrize(
    "before, stale, turn, first_writes, first_started, inbox_writes, inbox_confirmed",
    FAST_TURN_TABLE,
)
class TestFastTurnAfterStaleOutput:
    """A worker that finishes with no PROCESSING frame is not sent its task again.

    The provider has no direct probe and the composer does not show the task,
    which is every provider but opencode, minimax and kimi after an accepted
    send. Real ``dispatch_input``, ``StatusMonitor`` and confirmation loops.
    """

    @pytest.mark.asyncio
    async def test_first_task_path(
        self,
        isolated_memory_db,
        composer,
        before,
        stale,
        turn,
        first_writes,
        first_started,
        inbox_writes,
        inbox_confirmed,
    ):
        sm, provider, backend = _fast_turn_rig(before, stale, turn)
        samples = terminal_service.DispatchBoundarySamples()

        with (
            _wired(sm, provider, backend),
            patch.object(terminal_service, "_DEFERRED_SUBMIT_CONFIRM_TIMEOUT", 0.2),
            patch.object(terminal_service, "_message_in_composer", return_value=composer),
        ):
            boundary = terminal_service.dispatch_input(TID, TASK, boundary_samples=samples)
            started = await terminal_service._confirm_worker_started_or_resubmit(
                TID,
                TASK,
                None,
                None,
                None,
                provider=provider,
                dispatch_generation=boundary,
                earlier_dispatch_generation=samples.earlier_if_resampled(),
            )

        assert (len(backend.written), started) == (first_writes, first_started)
        assert set(backend.written) == {TASK}

    def test_inbox_path(
        self,
        isolated_memory_db,
        composer,
        before,
        stale,
        turn,
        first_writes,
        first_started,
        inbox_writes,
        inbox_confirmed,
    ):
        """Unchanged by the first-task fallback: the inbox keeps the later boundary alone."""
        sm, provider, backend = _fast_turn_rig(before, stale, turn)

        with (
            _wired(sm, provider, backend),
            patch.object(terminal_service, "_message_in_composer", return_value=composer),
        ):
            boundary = terminal_service.dispatch_input(TID, TASK)
            confirmed = InboxService()._confirm_submitted(
                TID, TASK, boundary, registry=None, sender_id=None
            )

        assert (len(backend.written), confirmed) == (inbox_writes, inbox_confirmed)


class TestSwallowedEnterAfterStaleOutputIsStillRecovered:
    """The #709 fix survives the fallback: the composer shows the task, so Enter is sent."""

    @pytest.mark.parametrize("stale", STALE_STATUSES)
    @pytest.mark.asyncio
    async def test_task_left_in_the_composer_gets_a_bare_enter(self, isolated_memory_db, stale):
        sm = _monitor()
        first = {"pending": True}

        def before_hook() -> None:
            if first["pending"]:
                first["pending"] = False
                _chunk(sm, stale)

        provider = _provider()
        provider.supports_direct_status_probe = False
        provider.requires_execution_evidence = False
        provider.execution_evidence_ambiguous = False
        backend = _Backend(before_hook=before_hook)  # the Enter is swallowed: no output
        samples = terminal_service.DispatchBoundarySamples()
        enters: list[str] = []

        def enter(_terminal_id: str, key: str) -> bool:
            enters.append(key)
            _chunk(sm, TerminalStatus.PROCESSING)  # the worker picks the task up
            return True

        with (
            _wired(sm, provider, backend),
            patch.object(terminal_service, "_DEFERRED_SUBMIT_CONFIRM_TIMEOUT", 0.2),
            patch.object(terminal_service, "_message_in_composer", return_value=True),
            patch.object(terminal_service, "send_special_key", side_effect=enter),
        ):
            boundary = terminal_service.dispatch_input(TID, TASK, boundary_samples=samples)
            started = await terminal_service._confirm_worker_started_or_resubmit(
                TID,
                TASK,
                None,
                None,
                None,
                provider=provider,
                dispatch_generation=boundary,
                earlier_dispatch_generation=samples.earlier_if_resampled(),
            )

        assert (samples.before_preparation, samples.before_write) == (BASELINE, BASELINE + 1)
        assert backend.written == [TASK]
        assert enters == ["Enter"]
        assert started is True


class TestBoundarySamples:
    """What ``dispatch_input`` reports through ``DispatchBoundarySamples``."""

    def test_both_samples_are_reported(self, isolated_memory_db):
        sm = _monitor()
        backend = _Backend(before_hook=lambda: _chunk(sm, TerminalStatus.COMPLETED))
        samples = terminal_service.DispatchBoundarySamples()

        with _wired(sm, _provider(), backend):
            boundary = terminal_service.dispatch_input(TID, "hello", boundary_samples=samples)

        assert (samples.before_preparation, samples.before_write) == (BASELINE, BASELINE + 1)
        assert boundary == samples.before_write
        assert samples.earlier_if_resampled() == BASELINE

    def test_backend_that_never_calls_the_hook_reports_no_later_sample(self, isolated_memory_db):
        """No window to judge, so the first-task fallback must stay off."""
        sm = _monitor()
        backend = _Backend(
            before_hook=lambda: _chunk(sm, TerminalStatus.COMPLETED), calls_hook=False
        )
        samples = terminal_service.DispatchBoundarySamples()

        with _wired(sm, _provider(), backend):
            boundary = terminal_service.dispatch_input(TID, "hello", boundary_samples=samples)

        assert (samples.before_preparation, samples.before_write) == (BASELINE, None)
        assert boundary == BASELINE
        assert samples.earlier_if_resampled() is None

    @pytest.mark.asyncio
    async def test_first_task_caller_hands_the_earlier_sample_to_the_confirmation(self):
        """``_schedule_deferred_init`` is the caller that uses it; pin the wiring."""
        from unittest.mock import AsyncMock

        provider_instance = AsyncMock()
        provider_instance.initialize.return_value = True
        provider_instance.shell_baseline = None
        provider_instance.runtime_variant = None
        provider_instance.requires_execution_evidence = False

        def dispatch(_terminal_id, _message, *, boundary_samples, **_kwargs):
            boundary_samples.before_preparation = 7
            boundary_samples.before_write = 9
            return 9

        backend = MagicMock()
        backend.supports_event_inbox.return_value = False
        monitor = MagicMock()
        monitor.get_status.return_value = TerminalStatus.PROCESSING
        confirm = AsyncMock(return_value=True)
        with (
            patch.object(terminal_service, "status_monitor", monitor),
            patch.object(terminal_service, "get_terminal_metadata", return_value={}),
            patch.object(terminal_service, "dispatch_input", side_effect=dispatch),
            patch.object(terminal_service, "_confirm_worker_started_or_resubmit", confirm),
            patch.object(terminal_service, "get_backend", return_value=backend),
        ):
            before = set(terminal_service._deferred_init_tasks)
            terminal_service._schedule_deferred_init(
                provider_instance, "worker99", "do the task", None, None
            )
            (task,) = set(terminal_service._deferred_init_tasks) - before
            await task

        confirm.assert_awaited_once()
        assert confirm.call_args.kwargs["dispatch_generation"] == 9
        assert confirm.call_args.kwargs["earlier_dispatch_generation"] == 7

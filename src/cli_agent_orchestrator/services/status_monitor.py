"""Monitors terminal status by accumulating output and detecting changes.

Consumer: terminal.{id}.output
Publisher: terminal.{id}.status
"""

import asyncio
import logging
import threading
from typing import Dict, List, Optional, Tuple

from cli_agent_orchestrator.constants import (
    CAO_PYTE_STATUS,
    PYTE_QUIESCENCE_DELAY_S,
    PYTE_SCREEN_COLS,
    PYTE_SCREEN_ROWS,
    STATE_BUFFER_MAX,
)
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.services.event_bus import bus
from cli_agent_orchestrator.utils.event import terminal_id_from_topic

logger = logging.getLogger(__name__)

# Statuses that represent a stable "ready" state — the agent has finished
# producing output and is waiting for further input. Once latched, the
# StatusMonitor will not regress to PROCESSING until ``notify_input_sent``
# is called (signalling that a new processing cycle is starting).
#
# Why: the event-driven pipeline derives status from a rolling 8KB buffer,
# and TUI redraws (cursor positioning, status-bar refreshes) routinely
# evict the idle/response markers that the per-provider get_status() relies
# on. That makes status flap rapidly between IDLE/COMPLETED and PROCESSING
# in the seconds following completion. Without stickiness, both
# wait_until_status (server-side) and the e2e tests' HTTP polling miss the
# brief "ready" windows and time out (PR #273 codex 60s init timeouts,
# completion-timeout failures).
_STICKY_READY_STATUSES = frozenset(
    {
        TerminalStatus.IDLE,
        TerminalStatus.COMPLETED,
        TerminalStatus.WAITING_USER_ANSWER,
        TerminalStatus.ERROR,
    }
)


class StatusMonitor:
    """Accumulates terminal output into rolling buffers and detects status changes."""

    def __init__(self):
        # Guards _buffers/_last_status/_allow_processing_revert. State is
        # touched from the asyncio consumer (_process_chunk), FastAPI's
        # threadpool (send_input → notify_input_sent, get_status), inbox
        # delivery worker threads, and cleanup_old_data's thread. Individual
        # dict ops are GIL-atomic, but the latch logic is a read-modify-write
        # sequence (read armed → decide transition → consume arm) that must
        # not interleave with notify_input_sent, or a freshly-armed gate can
        # be consumed by a decision taken against stale state.
        self._lock = threading.RLock()
        self._buffers: Dict[str, str] = {}
        self._last_status: Dict[str, TerminalStatus] = {}
        # Per-terminal flag: when True, the next provider-detected PROCESSING
        # is honored and stickiness reset. Set by notify_input_sent() whenever
        # external input is sent to the terminal (paste-bombed by send_input
        # or backend.send_keys via provider init). Without this, latched
        # IDLE/COMPLETED would freeze the terminal forever even when the
        # agent is genuinely processing new work.
        self._allow_processing_revert: Dict[str, bool] = {}
        # --- pyte rendered-screen detection state (only used when CAO_PYTE_STATUS
        # is on AND the provider opts in via supports_screen_detection) ---
        # Per-terminal pyte Screen+Stream that composites the raw byte stream
        # into a rendered viewport. Detection runs against the composited screen
        # on two edges only — rising (output resumed) and quiescence (output
        # stopped for PYTE_QUIESCENCE_DELAY_S) — never mid-burst, which is what
        # keeps status flap-free.
        self._screens: Dict[str, Tuple[object, object]] = {}
        self._bursting: Dict[str, bool] = {}
        # Pending quiescence-detect timer handle per terminal (loop.call_later).
        self._quiesce_handle: Dict[str, asyncio.TimerHandle] = {}
        # The event loop that owns the quiescence timers. Captured when the
        # first timer is scheduled (on the loop thread). clear_terminal /
        # reset_buffer can run OFF that thread (cleanup_old_data is dispatched
        # via asyncio.to_thread), and TimerHandle.cancel() is not thread-safe,
        # so the cancel is marshaled back onto this loop. See
        # _cancel_quiesce_handle.
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Strong references to in-flight quiescence-detection tasks. asyncio only
        # keeps a WEAK reference to tasks created via loop.create_task, so without
        # this a detection task can be garbage-collected mid-run and silently drop
        # a status transition. Tasks remove themselves on completion.
        self._detect_tasks: set = set()

    async def run(self) -> None:
        """Subscribe to output events and detect status changes.

        ``_process_chunk`` runs provider status detection which, for tmux-backed
        providers, shells out to the ``tmux`` binary via libtmux (a blocking
        ``subprocess`` fork/exec — e.g. kiro's ``get_pane_current_command`` in
        Check 3). Running that inline on the event loop meant every output chunk
        from every worker forked tmux ON the loop; with a few concurrent workers
        streaming, that fork storm froze the whole server (no /health, assign
        POSTs stranded until the MCP client's ~120s timeout). Offload
        ``_process_chunk`` to a worker thread so the loop stays free.

        Chunks are processed one at a time (each ``to_thread`` is awaited before
        the next ``queue.get()``), so per-terminal ordering and the latch's
        read-modify-write sequence are preserved exactly as before.
        """
        # Capture the loop up front, on the loop thread, so the debounce timers
        # scheduled from the worker thread can be marshaled back onto it.
        self._loop = asyncio.get_running_loop()
        queue = bus.subscribe("terminal.*.output")
        logger.info("StatusMonitor started")

        while True:
            try:
                event = await queue.get()
                terminal_id = terminal_id_from_topic(event["topic"])
                await asyncio.to_thread(self._process_chunk, terminal_id, event["data"]["data"])
            except Exception as e:
                logger.exception(f"Error in StatusMonitor: {e}")

    def _process_chunk(self, terminal_id: str, chunk: str) -> None:
        """Append chunk to the rolling buffer and (re)detect status.

        Two detection paths share one latch/publish backend (_apply_detection):
        - RAW (default, every provider): regex over the rolling 8KB byte
          buffer, run on every chunk. Unchanged legacy behavior.
        - SCREEN (pyte): when CAO_PYTE_STATUS is on AND the provider opts in
          via supports_screen_detection, the chunk is fed to a per-terminal
          pyte screen and detection runs only on the rising edge (output
          resumed) and at quiescence (output stopped) — see
          _schedule_screen_detection.
        """
        provider = provider_manager.get_provider(terminal_id)
        use_screen = (
            CAO_PYTE_STATUS
            and provider is not None
            and getattr(provider, "supports_screen_detection", False)
        )

        with self._lock:
            buffer = self._buffers.get(terminal_id, "") + chunk
            if len(buffer) > STATE_BUFFER_MAX:
                buffer = buffer[-STATE_BUFFER_MAX:]
            self._buffers[terminal_id] = buffer
            if use_screen:
                self._feed_screen_locked(terminal_id, chunk)

        if not use_screen:
            # Debounced raw detection: same rising-edge + quiescence pattern as
            # the pyte path.  Detects immediately on the first chunk after quiet
            # (catches PROCESSING transition), then waits for output to settle
            # before re-detecting (catches IDLE/COMPLETED without running costly
            # regex on every single chunk during bursts).
            self._schedule_raw_detection(terminal_id, buffer)
            return

        self._schedule_screen_detection(terminal_id, provider)

    def _apply_detection(self, terminal_id: str, detected: TerminalStatus) -> None:
        """Apply the sticky-latch rules to a freshly detected status and publish
        on change. Shared by the raw and pyte detection paths.

        Stickiness: once a ready status is latched, refuse downgrades unless
        notify_input_sent() armed a revert. Two kinds of downgrade are blocked:
        1. ready → PROCESSING/UNKNOWN — buffer-eviction / mid-redraw flap.
        2. COMPLETED → IDLE — the response marker evicts before the user marker.
        The arm is consumed only by a genuine PROCESSING transition or an
        init-style non-ready → ready upgrade, never by a ready → ready flap
        (which would block the input's real PROCESSING and let InboxService
        paste into a busy agent).
        """
        with self._lock:
            last = self._last_status.get(terminal_id)

            # UNKNOWN is "no signal", not a state: never let it overwrite a known
            # status. Mid-turn the screen can momentarily show neither a spinner
            # nor the prompt (e.g. while a tool runs), which the detector reports
            # as UNKNOWN; downgrading a known PROCESSING to UNKNOWN there is a
            # spurious transition (observed live as processing->unknown->completed).
            #
            # Do NOT narrow this to "suppress only when not armed" (to let an
            # armed new turn clear a stale ready status). It does not actually
            # close that window — the rising-edge frame right after a paste still
            # composites the PREVIOUS turn's COMPLETED box, so get_status() reports
            # ready whether or not UNKNOWN is let through — and it opens a worse
            # one: an armed ready->UNKNOWN->ready re-render (torn paste frame, then
            # the prior turn repainted before the new spinner draws) makes the
            # bounce back to COMPLETED a non-ready->ready upgrade that CONSUMES the
            # revert arm. The genuine PROCESSING that follows is then latch-blocked
            # and the terminal reads ready for the entire busy turn — exactly what
            # InboxService must never paste into. See
            # test_armed_unknown_then_ready_rerender_keeps_processing. The initial
            # UNKNOWN (last is None, nothing detected yet) is still allowed through.
            if detected == TerminalStatus.UNKNOWN and last is not None:
                return

            armed = self._allow_processing_revert.get(terminal_id, False)
            if not armed:
                if last in _STICKY_READY_STATUSES and detected in (
                    TerminalStatus.PROCESSING,
                    TerminalStatus.UNKNOWN,
                ):
                    return
                if last == TerminalStatus.COMPLETED and detected == TerminalStatus.IDLE:
                    return

            if detected == last:
                return

            self._last_status[terminal_id] = detected
            if detected == TerminalStatus.PROCESSING:
                self._allow_processing_revert[terminal_id] = False
            elif detected in _STICKY_READY_STATUSES and last not in _STICKY_READY_STATUSES:
                self._allow_processing_revert[terminal_id] = False

        # Publish outside the lock — subscribers must never be able to
        # re-enter StatusMonitor while the latch state is mid-update.
        bus.publish(f"terminal.{terminal_id}.status", {"status": detected.value})
        logger.info(f"Terminal {terminal_id} status changed: {detected.value}")

    # ----- pyte rendered-screen detection (edge-debounced) -------------------

    def _feed_screen_locked(self, terminal_id: str, chunk: str) -> None:
        """Feed a chunk into the terminal's pyte screen. Caller holds the lock.

        Lazily creates the Screen+Stream so pyte is only imported/used when the
        screen path is active for this terminal.
        """
        scr = self._screens.get(terminal_id)
        if scr is None:
            import pyte

            screen = pyte.Screen(PYTE_SCREEN_COLS, PYTE_SCREEN_ROWS)
            stream = pyte.Stream(screen)
            scr = (screen, stream)
            self._screens[terminal_id] = scr
        scr[1].feed(chunk)

    def _detect_screen(self, terminal_id: str, provider) -> TerminalStatus:
        """Detect status from the terminal's composited pyte screen."""
        fallback_buffer: Optional[str] = None
        with self._lock:
            scr = self._screens.get(terminal_id)
            buffer = self._buffers.get(terminal_id, "")
            try:
                lines: List[str] = list(scr[0].display) if scr is not None else []
            except Exception:
                # pyte can transiently hold zero-length cell data while rendering
                # complex TUI redraws. Fall back to raw-buffer detection instead of
                # letting the quiescence callback tear down status monitoring.
                logger.exception(
                    "Error rendering screen status for %s; falling back to raw buffer",
                    terminal_id,
                )
                fallback_buffer = buffer
                lines = []
        if fallback_buffer is not None:
            if provider is None:
                return TerminalStatus.UNKNOWN
            try:
                return provider.get_status(fallback_buffer)
            except Exception:
                logger.exception("Error detecting fallback status for %s", terminal_id)
                return TerminalStatus.UNKNOWN
        if not lines or provider is None:
            return TerminalStatus.UNKNOWN
        try:
            return provider.get_status_from_screen(lines)
        except Exception:
            # Full traceback: screen detectors are new and can trip on
            # unexpected TUI frames; the stack makes such regressions debuggable.
            logger.exception(f"Error detecting screen status for {terminal_id}")
            return TerminalStatus.UNKNOWN

    def _schedule_screen_detection(self, terminal_id: str, provider) -> None:
        """Edge-debounce detection on the pyte screen.

        Rising edge (first chunk after quiet) → detect immediately (catches the
        PROCESSING transition the instant work resumes). Quiescence (no new
        chunk for PYTE_QUIESCENCE_DELAY_S) → detect again (the TUI repaint has
        settled, so the screen shows the true end state). Detection NEVER runs
        mid-burst, which is what eliminates the flaps naive per-chunk rendered
        detection produces.
        """
        loop = self._loop or self._running_loop()
        if loop is None:
            # No event loop (unit tests / offline replay): detect immediately
            # on the current screen — deterministic, no timing.
            self._apply_detection(terminal_id, self._detect_screen(terminal_id, provider))
            return

        with self._lock:
            was_bursting = self._bursting.get(terminal_id, False)
            self._bursting[terminal_id] = True
            handle = self._quiesce_handle.pop(terminal_id, None)
        self._cancel_quiesce_handle(handle)

        if not was_bursting:
            self._apply_detection(terminal_id, self._detect_screen(terminal_id, provider))

        self._arm_quiesce_timer(loop, terminal_id, self._on_screen_quiescent, provider)

    def _on_screen_quiescent(self, terminal_id: str, provider) -> None:
        """Quiescence timer fired: output stopped, so the screen has settled.

        Fires on the loop; offload the (potentially blocking) screen detection
        to a worker thread so the loop stays free.
        """
        with self._lock:
            self._bursting[terminal_id] = False
            self._quiesce_handle.pop(terminal_id, None)

        async def _detect_and_apply() -> None:
            detected = await asyncio.to_thread(self._detect_screen, terminal_id, provider)
            self._apply_detection(terminal_id, detected)

        loop = self._loop or self._running_loop()
        if loop is None:
            self._apply_detection(terminal_id, self._detect_screen(terminal_id, provider))
        else:
            self._spawn_tracked(loop, _detect_and_apply())

    def _schedule_raw_detection(self, terminal_id: str, buffer: str) -> None:
        """Edge-debounce detection on the raw rolling buffer.

        Detects on every chunk while the terminal is in a ready/armed state
        (to catch the IDLE→PROCESSING transition immediately). Once PROCESSING
        is observed, switches to quiescence-only detection (the busy→ready
        transition only matters after output settles). This prevents queue
        overflow during sustained output while ensuring InboxService never
        pastes into a busy terminal.

        Runs on a StatusMonitor worker thread (``run`` dispatches
        ``_process_chunk`` via ``asyncio.to_thread``), so the blocking
        ``_detect_status`` (which shells out to tmux) executes off the event
        loop. The quiescence timer is loop-affine, so it is armed on the
        captured loop via ``call_soon_threadsafe`` rather than the current
        thread's (nonexistent) loop.
        """
        loop = self._loop or self._running_loop()
        if loop is None:
            # No loop ever captured (unit tests / offline replay): detect
            # inline and skip the debounce timer.
            self._apply_detection(terminal_id, self._detect_status(terminal_id, buffer))
            return

        with self._lock:
            was_bursting = self._bursting.get(terminal_id, False)
            self._bursting[terminal_id] = True
            handle = self._quiesce_handle.pop(terminal_id, None)
            last_status = self._last_status.get(terminal_id)
        self._cancel_quiesce_handle(handle)

        # While terminal is ready/armed, detect on every chunk so the
        # IDLE→PROCESSING transition is never missed (prevents stale-IDLE
        # delivery by InboxService). Once PROCESSING is observed, debounce.
        if not was_bursting or last_status in _STICKY_READY_STATUSES or last_status is None:
            detected = self._detect_status(terminal_id, buffer)
            self._apply_detection(terminal_id, detected)

        self._arm_quiesce_timer(loop, terminal_id, self._on_raw_quiescent)

    def _arm_quiesce_timer(self, loop, terminal_id: str, callback, *cb_args) -> None:
        """Schedule the quiescence timer on ``loop`` from any thread.

        ``loop.call_later`` is not thread-safe and this may run on a worker
        thread, so marshal the scheduling onto the loop with
        ``call_soon_threadsafe``. The resulting TimerHandle is stored from
        inside the marshaled closure (still on the loop thread) so cancel
        marshaling in ``_cancel_quiesce_handle`` stays correct. ``cb_args``
        are extra positional args passed to ``callback`` after ``terminal_id``.
        """

        def _arm() -> None:
            # Runs on the loop thread (via call_soon_threadsafe), so it is safe
            # to cancel a prior TimerHandle directly here. Cancel any existing
            # timer for this terminal BEFORE arming the new one: if several
            # chunks arrive in quick succession their _arm closures are queued
            # together, and without this the later closure would overwrite
            # _quiesce_handle while leaving the earlier timer live — two timers
            # then fire, and a stale one firing mid-burst causes early/duplicate
            # quiescence detections and status flaps. One outstanding timer per
            # terminal, always the latest.
            with self._lock:
                prior = self._quiesce_handle.get(terminal_id)
                if prior is not None:
                    prior.cancel()
                handle = loop.call_later(PYTE_QUIESCENCE_DELAY_S, callback, terminal_id, *cb_args)
                self._quiesce_handle[terminal_id] = handle

        try:
            loop.call_soon_threadsafe(_arm)
        except RuntimeError:
            # Loop closed during shutdown — quiescence re-detect is moot.
            pass

    def _on_raw_quiescent(self, terminal_id: str) -> None:
        """Quiescence timer fired for raw path: re-detect from current buffer.

        Fires on the event loop (via call_later), so the blocking
        ``_detect_status`` is offloaded to a worker thread to keep the loop
        free — a tmux ``get_pane_current_command`` here would otherwise fork
        on the loop.
        """
        with self._lock:
            self._bursting[terminal_id] = False
            self._quiesce_handle.pop(terminal_id, None)
            buffer = self._buffers.get(terminal_id, "")

        async def _detect_and_apply() -> None:
            detected = await asyncio.to_thread(self._detect_status, terminal_id, buffer)
            self._apply_detection(terminal_id, detected)

        loop = self._loop or self._running_loop()
        if loop is None:
            self._apply_detection(terminal_id, self._detect_status(terminal_id, buffer))
        else:
            self._spawn_tracked(loop, _detect_and_apply())

    def _spawn_tracked(self, loop, coro) -> None:
        """Create a task on ``loop`` and hold a strong reference until it
        finishes, so asyncio's weak task references can't GC it mid-run."""
        task = loop.create_task(coro)
        self._detect_tasks.add(task)
        task.add_done_callback(self._detect_tasks.discard)

    @staticmethod
    def _running_loop() -> Optional[asyncio.AbstractEventLoop]:
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    def _cancel_quiesce_handle(self, handle: Optional[asyncio.TimerHandle]) -> None:
        """Cancel a quiescence timer safely from any thread.

        The timer is an asyncio.TimerHandle owned by ``self._loop``.
        TimerHandle.cancel() mutates loop-internal scheduling state and is NOT
        thread-safe, yet clear_terminal/reset_buffer can run off the loop thread
        (cleanup_old_data is dispatched via asyncio.to_thread). Marshal the
        cancel onto the owning loop with call_soon_threadsafe unless we are
        already on it.
        """
        if handle is None:
            return
        loop = self._loop
        if loop is None:
            handle.cancel()  # no loop ever captured (unit/offline path) — safe
            return
        try:
            on_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            handle.cancel()
        else:
            try:
                loop.call_soon_threadsafe(handle.cancel)
            except RuntimeError:
                pass  # loop already closed during shutdown — the timer is moot

    def notify_input_sent(self, terminal_id: str) -> None:
        """Arm the next PROCESSING transition.

        Call before any send_keys / paste that initiates a new processing
        cycle (terminal_service.send_input, provider.initialize warm-up
        and CLI-launch keystrokes). Without this, a previously-latched
        IDLE/COMPLETED would block the genuine PROCESSING transition.
        """
        with self._lock:
            self._allow_processing_revert[terminal_id] = True

    def clear_rolling_buffer(self, terminal_id: str) -> None:
        """Clear ONLY the rolling byte buffer for a terminal — preserves
        ``_last_status`` and ``_allow_processing_revert``.

        Used by send_input to drop stale pre-task content (e.g. kiro-cli 2.11's
        "ask a question" idle placeholder) so it can't combine with the
        input_received flag to trigger a false COMPLETED before the agent has
        rendered its processing indicator. Unlike ``reset_buffer``, this does
        NOT wipe the sticky-latch state, so the arm set by ``notify_input_sent``
        survives and the subsequent IDLE→PROCESSING transition is honored.
        """
        with self._lock:
            self._buffers[terminal_id] = ""

    def _detect_status(self, terminal_id: str, buffer: str) -> TerminalStatus:
        """Detect status: provider-specific patterns or UNKNOWN if no provider."""
        provider = provider_manager.get_provider(terminal_id)
        if provider is None:
            return TerminalStatus.UNKNOWN

        try:
            return provider.get_status(buffer)
        except Exception as e:
            logger.error(f"Error detecting status for {terminal_id}: {e}")
            return TerminalStatus.UNKNOWN

    def clear_terminal(self, terminal_id: str) -> None:
        """Free buffer and status for a deleted terminal."""
        with self._lock:
            self._buffers.pop(terminal_id, None)
            self._last_status.pop(terminal_id, None)
            self._allow_processing_revert.pop(terminal_id, None)
            self._screens.pop(terminal_id, None)
            self._bursting.pop(terminal_id, None)
            handle = self._quiesce_handle.pop(terminal_id, None)
        self._cancel_quiesce_handle(handle)

    def reset_buffer(self, terminal_id: str) -> None:
        """Clear the rolling buffer + last-known status WITHOUT forgetting the
        terminal.

        Used when a provider relaunches a different CLI mode on the SAME
        ``terminal_id`` (e.g. Kiro's TUI -> ``--legacy-ui`` fallback). Without
        this, the retry re-derives status from a buffer still full of stale bytes
        from the failed first attempt and can spuriously time out.
        """
        with self._lock:
            self._buffers[terminal_id] = ""
            self._last_status.pop(terminal_id, None)
            self._allow_processing_revert.pop(terminal_id, None)
            # Drop the rendered screen too so the relaunched CLI mode is
            # detected against a fresh viewport, not the failed attempt's.
            self._screens.pop(terminal_id, None)
            self._bursting.pop(terminal_id, None)
            handle = self._quiesce_handle.pop(terminal_id, None)
        self._cancel_quiesce_handle(handle)

    def get_status(self, terminal_id: str) -> TerminalStatus:
        """Get current terminal status — the single source of truth for both backends.

        Pipe-pane backends (tmux) return the last status pushed by the FIFO →
        EventBus → _process_chunk pipeline. Event-inbox backends (herdr) don't
        feed that pipeline (no FIFO reader is started for them), so _last_status
        would stay UNKNOWN forever; for those we derive status on demand from the
        provider, whose get_status() consults backend.get_native_status(). Doing
        it here means every caller (API status, init waits, busy checks, curator
        liveness) works on herdr without each having to special-case the backend.
        """
        from cli_agent_orchestrator.backends.registry import get_backend

        if get_backend().supports_event_inbox():
            try:
                provider = provider_manager.get_provider(terminal_id)
            except Exception:
                provider = None
            if provider is not None:
                with self._lock:
                    buffer = self._buffers.get(terminal_id, "")
                try:
                    # The native (herdr) path ignores the buffer arg; pass the
                    # rolling buffer (empty for herdr) so the rare
                    # get_native_status()==None fallback still gets what we have.
                    # provider.get_status may shell out to the herdr CLI — call
                    # it outside the lock.
                    return provider.get_status(buffer)
                except Exception as e:
                    logger.error(f"Error deriving native status for {terminal_id}: {e}")
                    return TerminalStatus.UNKNOWN

        with self._lock:
            cached = self._last_status.get(terminal_id, TerminalStatus.UNKNOWN)
            # When cached status is PROCESSING, the debounced detection may be
            # stuck: TUI providers (kiro-cli) can send escape sequences
            # continuously after becoming idle, preventing the 200ms quiescence
            # timer from ever firing. Do a fresh detection from the current
            # buffer so poll-based callers (wait_until_status) catch the
            # PROCESSING→ready transition without waiting for stream silence.
            if cached == TerminalStatus.PROCESSING:
                buffer = self._buffers.get(terminal_id, "")
            else:
                buffer = ""

        if cached == TerminalStatus.PROCESSING and buffer:
            fresh = self._detect_status(terminal_id, buffer)
            logger.debug(
                f"get_status [{terminal_id}]: cached=PROCESSING, "
                f"fresh={fresh.value}, buffer_len={len(buffer)}"
            )
            if fresh != TerminalStatus.PROCESSING and fresh != TerminalStatus.UNKNOWN:
                self._apply_detection(terminal_id, fresh)
                return fresh
        return cached

    def get_buffer(self, terminal_id: str) -> str:
        """Get accumulated output buffer for a terminal."""
        with self._lock:
            return self._buffers.get(terminal_id, "")


# Module-level singleton
status_monitor = StatusMonitor()

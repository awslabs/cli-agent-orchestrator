"""Monitors terminal status by accumulating output and detecting changes.

Consumer: terminal.{id}.output
Publisher: terminal.{id}.status
"""

import asyncio
import logging
import threading
import time
from typing import Dict, List, Optional, Tuple

from cli_agent_orchestrator.constants import (
    CAO_PYTE_STATUS,
    PYTE_QUIESCENCE_DELAY_S,
    PYTE_SCREEN_COLS,
    PYTE_SCREEN_ROWS,
)
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.services.event_bus import bus
from cli_agent_orchestrator.services.settings_service import get_server_settings
from cli_agent_orchestrator.utils.event import terminal_id_from_topic

logger = logging.getLogger(__name__)

# Statuses that represent a stable "ready" state — the agent has finished
# producing output and is waiting for further input. Once latched, the
# StatusMonitor will not regress to PROCESSING until ``notify_input_sent``
# is called (signalling that a new processing cycle is starting).
#
# Why: the event-driven pipeline derives status from a rolling state buffer,
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

# Stale-PROCESSING self-heal (#558). get_status()'s cheap re-check re-derives from the SAME
# rolling buffer the FIFO pipeline feeds — and the moment a process goes genuinely idle it also
# stops emitting output, so that buffer stops changing. If its final content never happened to
# parse as a ready state (the idle marker rotated out of the bounded window, or a truncated
# escape corrupted the tail), re-running detection on the unchanging buffer returns
# PROCESSING/UNKNOWN forever: the pane already shows the finished response while every poller
# sees PROCESSING. The #397 pipe-liveness watchdog cannot see this either — the FIFO delivered
# its bytes, so the pipe looks healthy. Observed live: a queued message sat undelivered for
# ~10 minutes until a manual tmux resize forced a redraw. The self-heal reads the pane directly
# via get_backend().get_history() (a real ``tmux capture-pane``, NOT the FIFO-fed buffer — tmux
# always holds the current rendered state regardless of output volume) and re-detects from that.
# This constant rate-limits those reads: get_status() is a hot path (every wait_until_status
# poll, every UI refresh, fleet-wide) and a capture-pane is a real subprocess fork — unbounded,
# it would recreate the fork-storm class run()'s own docstring documents.
STALE_PROCESSING_CAPTURE_INTERVAL_S = 3.0

# The interval above bounds how OFTEN the fallback re-runs; this gates WHETHER it runs at all.
# Without it, every genuinely busy terminal would eat a capture-pane subprocess every ~3s for
# the entire duration of every ordinary turn, on top of the fifo watchdog's own probing. The
# wedge's actual signature is "the rolling buffer stopped changing", so require exactly that:
# only attempt a capture once no new chunk has been appended for this long. A terminal
# mid-burst never reaches the fallback at all.
STALE_PROCESSING_BUFFER_QUIET_S = 3.0

# A ready verdict from a single capture is never honored on its own — it must repeat on the
# next eligible read (see _fresh_capture_pane_status). This bounds how far apart those two
# reads may be: a candidate older than this is dropped and confirmation starts over. Without
# the bound, a candidate recorded during one turn could sit in the map indefinitely and be
# "confirmed" by one lone mid-repaint frame much later — exactly the single-frame latch the
# two-read confirm exists to prevent.
STALE_PROCESSING_CONFIRM_TTL_S = 2 * STALE_PROCESSING_CAPTURE_INTERVAL_S


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
        # Monotonic per-terminal byte-buffer generation.  A provider that
        # remembers positions across get_status() calls needs an explicit reset
        # boundary when send_input discards the old rolling buffer; content
        # overlap alone cannot distinguish a fresh, byte-identical turn from a
        # stale screen redraw.
        self._buffer_epochs: Dict[str, int] = {}
        self._last_status: Dict[str, TerminalStatus] = {}
        # Per-terminal flag: when True, the next provider-detected PROCESSING
        # is honored and stickiness reset. Set by notify_input_sent() whenever
        # external input is sent to the terminal (paste-bombed by send_input
        # or backend.send_keys via provider init). Without this, latched
        # IDLE/COMPLETED would freeze the terminal forever even when the
        # agent is genuinely processing new work.
        self._allow_processing_revert: Dict[str, bool] = {}
        # Per-terminal monotonic timestamp of the last stale-PROCESSING capture-pane
        # attempt — the STALE_PROCESSING_CAPTURE_INTERVAL_S rate limit. Absence is None,
        # deliberately NOT 0.0: time.monotonic()'s reference point is arbitrary, so a 0.0
        # sentinel is indistinguishable from a genuine reading and could rate-limit the
        # very first check before it ever runs.
        self._last_stale_capture_check: Dict[str, Optional[float]] = {}
        # Per-terminal monotonic timestamp of the last time _process_chunk actually
        # appended a chunk (i.e. the buffer changed) — the STALE_PROCESSING_BUFFER_QUIET_S
        # quiet gate reads this. Same None-vs-0.0 sentinel rule as above.
        self._buffer_changed_at: Dict[str, Optional[float]] = {}
        # Per-terminal (status, monotonic, generation) candidate from a
        # stale-PROCESSING capture, awaiting a second confirming read before being
        # honored (see _fresh_capture_pane_status). Cleared on confirm, on an
        # intervening PROCESSING/UNKNOWN read, on expiry past
        # STALE_PROCESSING_CONFIRM_TTL_S, by a real chunk landing in
        # _process_chunk, and by notify_input_sent — new input or new output means
        # whatever the pane showed before no longer describes the terminal. The
        # third element is the generation sampled BEFORE the candidate's pane
        # read: every mutation of this map is rejected unless that generation is
        # still current, so a read that straddled a turn/output boundary can never
        # seed (or confirm) a candidate — see _fresh_capture_pane_status.
        self._pending_stale_capture: Dict[str, Tuple[TerminalStatus, float, int]] = {}
        # Per-terminal turn/output generation. Bumped under the lock by
        # notify_input_sent (a new turn began) and by _process_chunk (real output
        # arrived). A capture-pane verdict is only applied if the generation it was
        # sampled under is still current at apply time: checking _last_status alone
        # cannot see a new turn, because notify_input_sent deliberately leaves
        # _last_status == PROCESSING while arming the revert — a stale ready verdict
        # applied across that boundary would consume the arm and latch-block the new
        # turn's genuine PROCESSING.
        self._capture_generation: Dict[str, int] = {}
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
        - RAW (default, every provider): regex over the rolling state buffer
          (``state_buffer_max`` bytes, server setting), run on every chunk.
          Unchanged legacy behavior.
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
        state_buffer_max = get_server_settings()["state_buffer_max"]

        with self._lock:
            buffer = self._buffers.get(terminal_id, "") + chunk
            if len(buffer) > state_buffer_max:
                buffer = buffer[-state_buffer_max:]
            self._buffers[terminal_id] = buffer
            # Real new output just landed — the stale-PROCESSING quiet gate keys off this
            # (see STALE_PROCESSING_BUFFER_QUIET_S). It also advances the capture
            # generation and kills any pending capture candidate: output arriving
            # means the terminal is demonstrably alive, so a ready verdict sampled
            # before this chunk no longer describes it and must not be confirmable
            # by a later read.
            self._buffer_changed_at[terminal_id] = time.monotonic()
            self._capture_generation[terminal_id] = self._capture_generation.get(terminal_id, 0) + 1
            self._pending_stale_capture.pop(terminal_id, None)
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
            changed = self._apply_detection_locked(terminal_id, detected)
        if changed:
            # Publish outside the lock — subscribers must never be able to
            # re-enter StatusMonitor while the latch state is mid-update.
            bus.publish(f"terminal.{terminal_id}.status", {"status": detected.value})
            logger.info(f"Terminal {terminal_id} status changed: {detected.value}")

    def _apply_detection_locked(self, terminal_id: str, detected: TerminalStatus) -> bool:
        """Sticky-latch core of _apply_detection. Caller MUST hold self._lock.

        Split out so callers that need to validate a precondition and apply in
        ONE critical section (the stale-PROCESSING capture path revalidating its
        generation) can do so without a check-then-apply gap for
        notify_input_sent to slip through. Returns True when the status changed;
        the caller must then publish the change on the bus AFTER releasing the
        lock (see _apply_detection for why).
        """
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
            return False

        armed = self._allow_processing_revert.get(terminal_id, False)
        if not armed:
            if last in _STICKY_READY_STATUSES and detected in (
                TerminalStatus.PROCESSING,
                TerminalStatus.UNKNOWN,
            ):
                return False
            if last == TerminalStatus.COMPLETED and detected == TerminalStatus.IDLE:
                return False

        if detected == last:
            return False

        self._last_status[terminal_id] = detected
        if detected == TerminalStatus.PROCESSING:
            self._allow_processing_revert[terminal_id] = False
        elif detected in _STICKY_READY_STATUSES and last not in _STICKY_READY_STATUSES:
            self._allow_processing_revert[terminal_id] = False

        return True

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

    def notify_input_sent(self, terminal_id: str, *, assume_processing: bool = False) -> None:
        """Arm the next PROCESSING transition.

        Call before any send_keys / paste that initiates a new processing
        cycle (terminal_service.send_input, provider.initialize warm-up
        and CLI-launch keystrokes). Without this, a previously-latched
        IDLE/COMPLETED would block the genuine PROCESSING transition.
        """
        with self._lock:
            self._allow_processing_revert[terminal_id] = True
            # A new turn is starting: whatever ready state a stale-PROCESSING capture saw
            # before this input no longer describes the terminal. Left armed, that candidate
            # could be "confirmed" by a single post-input read and latch ready against the
            # new turn's genuine PROCESSING. The generation bump additionally invalidates
            # any capture verdict already CONFIRMED but not yet applied — an in-flight
            # get_status() that sampled the pane before this input must not stamp its
            # stale verdict over the new turn (and consume the revert arm just set).
            self._pending_stale_capture.pop(terminal_id, None)
            self._capture_generation[terminal_id] = self._capture_generation.get(terminal_id, 0) + 1
        if assume_processing:
            self._apply_detection(terminal_id, TerminalStatus.PROCESSING)

    def clear_rolling_buffer(self, terminal_id: str, provider=None) -> None:
        """Clear ONLY the rolling byte buffer for a terminal — preserves
        ``_last_status`` and ``_allow_processing_revert``.

        Used by send_input to drop stale pre-task content (e.g. kiro-cli 2.11's
        "ask a question" idle placeholder) so it can't combine with the
        input_received flag to trigger a false COMPLETED before the agent has
        rendered its processing indicator. Unlike ``reset_buffer``, this does
        NOT wipe the sticky-latch state, so the arm set by ``notify_input_sent``
        survives and the subsequent IDLE→PROCESSING transition is honored.

        When the active provider is supplied, it is synchronously notified of
        the new monotonically increasing byte-buffer epoch while this monitor's
        lock is held.  That makes the boundary atomic with respect to the
        output-consumer thread, which otherwise could parse the fresh first
        chunk against state from the discarded buffer.
        """
        with self._lock:
            self._buffers[terminal_id] = ""
            epoch = self._buffer_epochs.get(terminal_id, 0) + 1
            self._buffer_epochs[terminal_id] = epoch
            if provider is not None:
                provider.notify_status_buffer_reset(epoch)

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
            self._buffer_epochs.pop(terminal_id, None)
            self._last_status.pop(terminal_id, None)
            self._allow_processing_revert.pop(terminal_id, None)
            self._screens.pop(terminal_id, None)
            self._bursting.pop(terminal_id, None)
            self._last_stale_capture_check.pop(terminal_id, None)
            self._buffer_changed_at.pop(terminal_id, None)
            self._pending_stale_capture.pop(terminal_id, None)
            self._capture_generation.pop(terminal_id, None)
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
            self._last_stale_capture_check.pop(terminal_id, None)
            self._buffer_changed_at.pop(terminal_id, None)
            self._pending_stale_capture.pop(terminal_id, None)
            self._capture_generation.pop(terminal_id, None)
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

        if cached == TerminalStatus.PROCESSING:
            # The cheap re-check above re-derives from the SAME rolling buffer the FIFO
            # pipeline feeds — once the process stops emitting output, that buffer stops
            # changing too, so the re-check can return PROCESSING/UNKNOWN forever while
            # the real pane already shows the finished response (#558; the module
            # constants above carry the full incident rationale). Consult the quiet gate
            # BEFORE anything that could fork: a terminal still streaming chunks is busy,
            # not wedged, and must not cost a subprocess call.
            with self._lock:
                changed_at = self._buffer_changed_at.get(terminal_id)
                # Pin the turn/output generation BEFORE the capture read. The pane
                # is sampled outside the lock; only a verdict whose generation is
                # still current at apply time may be applied (see below).
                generation = self._capture_generation.get(terminal_id, 0)
            buffer_is_quiet = (
                changed_at is not None
                and time.monotonic() - changed_at >= STALE_PROCESSING_BUFFER_QUIET_S
            )
            if buffer_is_quiet:
                fresh_capture = self._fresh_capture_pane_status(terminal_id, generation)
                if fresh_capture is not None:
                    logger.debug(
                        f"get_status [{terminal_id}]: cached=PROCESSING stale-buffer re-check "
                        f"still PROCESSING/UNKNOWN, fresh capture-pane={fresh_capture.value}"
                    )
                    if (
                        fresh_capture != TerminalStatus.PROCESSING
                        and fresh_capture != TerminalStatus.UNKNOWN
                    ):
                        # The capture-pane read above ran OUTSIDE the lock — a real
                        # subprocess call, tens of milliseconds rather than microseconds —
                        # so the world can have moved: real new output can have resumed
                        # PROCESSING, or notify_input_sent can have started a whole new
                        # turn. The latter is invisible to a _last_status check (a new
                        # turn deliberately KEEPS _last_status == PROCESSING while arming
                        # the revert), which is why the generation pinned before the read
                        # is the authority here. Validate and apply in ONE critical
                        # section — a check-then-apply gap would let notify_input_sent
                        # slip between them, and the stale verdict would consume the arm
                        # it just set, latch-blocking the new turn's genuine PROCESSING.
                        with self._lock:
                            current_last_status = self._last_status.get(terminal_id)
                            generation_current = (
                                self._capture_generation.get(terminal_id, 0) == generation
                            )
                            apply_ok = (
                                generation_current
                                and current_last_status == TerminalStatus.PROCESSING
                            )
                            if apply_ok:
                                changed = self._apply_detection_locked(terminal_id, fresh_capture)
                        if apply_ok:
                            if changed:
                                bus.publish(
                                    f"terminal.{terminal_id}.status",
                                    {"status": fresh_capture.value},
                                )
                                logger.info(
                                    f"Terminal {terminal_id} status changed: "
                                    f"{fresh_capture.value}"
                                )
                            return fresh_capture
                        logger.debug(
                            f"get_status [{terminal_id}]: fresh capture-pane result "
                            "discarded — the terminal moved on (new input or new "
                            "output) while the capture-pane read was in flight"
                        )
                        # The pipeline got there first: hand back ITS status rather than
                        # the `cached` value snapshotted at entry, which is now one step
                        # stale.
                        if current_last_status is not None:
                            return current_last_status
        return cached

    def _fresh_capture_pane_status(
        self, terminal_id: str, generation: int
    ) -> Optional[TerminalStatus]:
        """Re-detect a stuck-PROCESSING terminal from a fresh pane capture (#558).

        ``generation`` is the turn/output generation the caller pinned BEFORE
        deciding to capture. The pane read below runs unlocked, so
        notify_input_sent or _process_chunk can bump the generation (and clear the
        candidate map) while it is in flight; a verdict from such a read describes
        the pane from BEFORE that boundary. The caller's apply-time check cannot
        see this alone: it only rejects CONFIRMED verdicts, while a straddling
        first read would re-seed the candidate map AFTER the boundary cleared it,
        and the next poll — pinning the new, by-then-stable generation — would
        treat that pre-boundary entry as its matching first read and confirm it.
        So every candidate-map mutation here (seed, confirm-pop, busy-pop) is
        performed only if ``generation`` is still current under the lock.

        Reads the pane directly via ``get_backend().get_history()`` (a real ``tmux
        capture-pane``, not the FIFO-fed rolling buffer) and re-runs provider detection
        against it. tmux always holds the correct, current rendered pane state regardless
        of output volume, so this can see the ready state a stale buffer cannot.

        Detector routing: a capture-pane snapshot is RENDERED content — cursor moves
        resolved, lines in on-screen order — a different input shape from the raw byte
        stream most ``get_status()`` detectors are tuned against (see BaseProvider.
        get_status's input contract). Feeding a rendered frame to a raw-stream detector
        produces systematic misreads, not noise: in a rendered busy kiro frame the
        always-drawn composer placeholder sits physically BELOW the working line and the
        credits line, which its raw-stream ordering checks parse as COMPLETED — and a
        deterministic misread sails straight through the two-read confirm below, because
        both reads see the same bytes. Applied, that false ready sticky-latches, disarms
        _allow_processing_revert, and blocks the agent's genuine PROCESSING for the rest
        of the turn. So the capture is routed through the two existing opt-in predicates:
        ``supports_screen_detection`` providers get their purpose-built
        ``get_status_from_screen()`` (calibrated for exactly this composited-viewport
        shape), ``supports_direct_status_probe`` providers get ``get_status()`` (declared
        safe on rendered snapshots — the same contract terminal_service's deferred-init
        direct probe relies on), and providers with neither flag fail CLOSED: no capture,
        no verdict, the terminal stays PROCESSING until the pipeline resolves it.
        The read is viewport-only (``visible_only=True`` — capture-pane ``-S 0``): a
        ``tail_lines`` read would include scrollback ABOVE the viewport, and detectors
        that match anywhere in their input (kimi/kiro ERROR indicators) would resurrect
        text from finished turns. Only the currently rendered screen is evidence.

        A single capture is still not trusted even on a routed detector: Ink-style TUIs
        repaint by clear-then-rewrite, and a sample caught between those two steps can
        miss the spinner while the previous response box parses ready. The screen path
        never samples mid-burst for exactly this reason (_schedule_screen_detection);
        since this read fires at an arbitrary moment instead, it requires the SAME ready
        status on two consecutive reads — the confirming read arriving within
        STALE_PROCESSING_CONFIRM_TTL_S — before honoring it. The same confirm-don't-trust
        pattern claude_code's wait_until_input_ready uses. The confirm still earns its
        keep despite the quiet gate: the gate watches the FIFO-fed buffer, so a pane that
        repaints without reaching the wedged FIFO can differ between reads.

        Returns ``None`` when skipped (rate-limited, no provider, unroutable detector,
        unconfirmed candidate, or any read/detection failure) — the caller treats that
        identically to "still PROCESSING", never as a signal to change status. Only ever
        called when cached status is already PROCESSING, so every failure path degrades
        to today's behavior, never past it.
        """
        now = time.monotonic()
        with self._lock:
            last_check = self._last_stale_capture_check.get(terminal_id)
            if last_check is not None and now - last_check < STALE_PROCESSING_CAPTURE_INTERVAL_S:
                return None
            self._last_stale_capture_check[terminal_id] = now

        try:
            provider = provider_manager.get_provider(terminal_id)
        except Exception as e:
            # get_provider() raises (not returns None) for a terminal it doesn't
            # recognize (not yet / no longer in the DB) — same defensive shape as
            # get_status()'s own event-inbox branch.
            logger.debug(f"_fresh_capture_pane_status [{terminal_id}]: get_provider failed: {e}")
            return None
        if provider is None:
            return None

        use_screen = getattr(provider, "supports_screen_detection", False)
        if not use_screen and not getattr(provider, "supports_direct_status_probe", False):
            # Raw-stream-tuned detector with no snapshot-safe alternative (kiro_cli,
            # cursor_cli): a rendered frame cannot be trusted as its input — see the
            # docstring — so don't capture at all. Self-heal is opt-in via either flag,
            # never a guess; these providers stay PROCESSING until the pipeline resolves
            # them.
            return None

        try:
            from cli_agent_orchestrator.backends.registry import get_backend

            # visible_only, NOT tail_lines: capture-pane's -S -N means "N history
            # lines ABOVE the viewport, plus the viewport" — a tail_lines read
            # includes scrollback, and detectors that match anywhere in their input
            # (kiro/kimi ERROR indicators) would resurrect text from finished turns.
            # Only the currently rendered screen is evidence about the current turn.
            fresh_output = get_backend().get_history(
                provider.session_name,
                provider.window_name,
                strip_escapes=True,
                visible_only=True,
            )
        except Exception as e:
            logger.debug(
                f"_fresh_capture_pane_status [{terminal_id}]: capture-pane read failed: {e}"
            )
            return None
        if not fresh_output:
            return None

        try:
            if use_screen:
                detected = provider.get_status_from_screen(fresh_output.splitlines())
            else:
                detected = provider.get_status(fresh_output)
        except Exception as e:
            logger.debug(f"_fresh_capture_pane_status [{terminal_id}]: detection failed: {e}")
            return None

        if detected == TerminalStatus.PROCESSING or detected == TerminalStatus.UNKNOWN:
            # Not a ready candidate — nothing to confirm. Clear any pending one: a busy
            # read BETWEEN two ready reads means the terminal is genuinely still working,
            # so the earlier candidate no longer describes a settled pane. Only if this
            # read didn't straddle a boundary, though — a busy verdict from BEFORE a
            # notify_input_sent/_process_chunk bump says nothing about a candidate
            # legitimately seeded after it.
            with self._lock:
                if self._capture_generation.get(terminal_id, 0) == generation:
                    self._pending_stale_capture.pop(terminal_id, None)
            return detected

        now = time.monotonic()
        with self._lock:
            if self._capture_generation.get(terminal_id, 0) != generation:
                # The pane read straddled a turn/output boundary: the boundary already
                # cleared the candidate map, and this verdict describes the pane from
                # before it. Seeding it anyway would hand the NEXT poll (which pins the
                # new generation before its read) a pre-boundary first read to "confirm"
                # against — the exact single-frame latch the two-read confirm exists to
                # prevent. No mutation, no verdict.
                logger.debug(
                    f"_fresh_capture_pane_status [{terminal_id}]: candidate "
                    f"{detected.value} discarded — the terminal moved on (new input or "
                    "new output) while the capture-pane read was in flight"
                )
                return None
            pending = self._pending_stale_capture.get(terminal_id)
            if (
                pending is not None
                and pending[0] == detected
                and pending[2] == generation
                and now - pending[1] <= STALE_PROCESSING_CONFIRM_TTL_S
            ):
                # Second consecutive matching read, in time and within the same
                # turn/output generation — honor it.
                self._pending_stale_capture.pop(terminal_id, None)
                confirmed = True
            else:
                # First sighting, a different candidate than the pending one, or a
                # candidate that aged out — (re)record with a fresh timestamp and wait
                # for the next read to agree.
                self._pending_stale_capture[terminal_id] = (detected, now, generation)
                confirmed = False
        if not confirmed:
            logger.debug(
                f"_fresh_capture_pane_status [{terminal_id}]: candidate {detected.value} "
                "recorded, awaiting a second confirming read before honoring it"
            )
            return None
        return detected

    def get_buffer(self, terminal_id: str) -> str:
        """Get accumulated output buffer for a terminal."""
        with self._lock:
            return self._buffers.get(terminal_id, "")


# Module-level singleton
status_monitor = StatusMonitor()

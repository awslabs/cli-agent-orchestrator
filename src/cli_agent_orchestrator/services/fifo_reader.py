"""FIFO reader for streaming terminal output from tmux pipe-pane.

Publisher: terminal.{id}.output
"""

import logging
import os
import select
import threading
import time
from typing import Callable, Dict, Optional, Tuple

from cli_agent_orchestrator.constants import (
    FIFO_DIR,
    PIPE_LIVENESS_CHECK_INTERVAL_S,
    PIPE_LIVENESS_MAX_REARM_FAILURES,
    PIPE_LIVENESS_STALL_CHECKS,
)
from cli_agent_orchestrator.services.event_bus import bus

logger = logging.getLogger(__name__)

CHUNK_SIZE = 4096

# How often a parked reader re-checks its stop flag. Bounds both shutdown
# latency and the cost of an idle terminal (one select wakeup per interval).
_POLL_INTERVAL = 0.5

# Coalesce rapid-fire chunks into one publish per window. TUI providers (kiro-cli)
# animate a spinner at ~10 fps; every frame is a separate FIFO write, and each
# write would otherwise publish an event. With two subscribers (StatusMonitor,
# LogWriter) sharing a bounded async queue (1024 slots), that fills the queue in
# seconds and drops events wholesale — including the worker's real state
# transitions that assign/handoff rely on. Batching every 50ms of chunks into
# one event drops the publish rate ~20x during bursts while staying well under
# the status monitor's 200ms quiescence debounce, so status detection is
# unaffected. Downstream consumers concatenate the batched bytes as before.
_COALESCE_WINDOW = 0.05

# Hard cap on how much data accumulates before an early flush. Prevents a single
# publish from growing unboundedly during a heavy sustained burst (e.g. a big
# response streaming from an LLM). 64KB is 16x CHUNK_SIZE — one flush per burst
# of ~16 back-to-back reads is fine.
_COALESCE_MAX_BYTES = 64 * 1024

# Type of the per-terminal callbacks the pipe-pane liveness watchdog needs.
# Kept as injected callables so FifoManager stays backend-agnostic (it knows
# nothing about tmux sessions/windows or the backend) and unit-testable with
# fakes. terminal_service wires the real backend calls at create_reader time.
PaneProbe = Callable[[], str]  # returns the live pane content (tmux capture-pane tail)
RearmPipe = Callable[[], None]  # re-attaches pipe-pane (stop then start, NOT a bare toggle)


class FifoManager:
    """Manages FIFO lifecycle: create named pipe, start reader thread, stop and cleanup.

    Also runs a pipe-pane liveness watchdog (issue #388): tmux can silently
    stop forwarding a pane's output to the FIFO after a burst of alternate-screen
    redraws — the pane keeps rendering but the piped copy freezes, and nothing
    errors (``pane_pipe`` still reports 1, the reader thread is healthy, there is
    simply no data to read). From inside the FIFO reader a stalled forwarder is
    indistinguishable from a genuinely idle terminal, so the watchdog compares
    tmux's *live* pane content against whether the FIFO delivered any bytes: pane
    advanced + FIFO silent = a stall, which it self-heals by re-arming the pipe.
    """

    def __init__(self):
        self._readers: Dict[str, threading.Event] = {}  # terminal_id -> stop flag
        self._threads: Dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

        # ---- pipe-pane liveness watchdog state (issue #388) ----
        # Monotonic timestamp of the last byte the FIFO reader delivered for a
        # terminal. Updated by the reader thread; read by the watchdog to tell a
        # stalled pipe (pane moving, FIFO silent) from a genuinely idle one.
        self._last_data_at: Dict[str, float] = {}
        # Per-terminal probes/re-arm callbacks (only tmux/pipe-pane terminals
        # register these; herdr and callers that pass none are never watched).
        self._pane_probe: Dict[str, PaneProbe] = {}
        self._rearm: Dict[str, RearmPipe] = {}
        # Per-terminal watchdog bookkeeping: (last_pane_content, last_check_monotonic,
        # consecutive_diverging_checks). The full tail string (not a hash) is
        # stored so an accidental hash collision can never mask a real stall.
        self._liveness: Dict[str, Tuple[str, float, int]] = {}
        # Consecutive re-arm *failures* per terminal (rearm() raised). Reset on
        # any successful re-arm; once it hits PIPE_LIVENESS_MAX_REARM_FAILURES
        # the terminal is dropped from the watchdog instead of retrying forever.
        self._rearm_failures: Dict[str, int] = {}
        self._watchdog_stop = threading.Event()
        self._watchdog_thread: Optional[threading.Thread] = None

        FIFO_DIR.mkdir(parents=True, exist_ok=True)

    def create_reader(
        self,
        terminal_id: str,
        pane_probe: Optional[PaneProbe] = None,
        rearm: Optional[RearmPipe] = None,
    ) -> None:
        """Create FIFO and start reader thread.

        ``pane_probe``/``rearm`` are optional and only supplied by pipe-pane
        (tmux) callers. When both are given, the terminal is enrolled in the
        liveness watchdog (issue #388). Callers that omit them (or backends
        without pipe-pane) get exactly the old behavior — no watchdog.
        """
        fifo_path = FIFO_DIR / f"{terminal_id}.fifo"

        enroll = pane_probe is not None and rearm is not None

        with self._lock:
            if terminal_id in self._readers:
                return

            if not fifo_path.exists():
                os.mkfifo(fifo_path)

            stop_flag = threading.Event()
            thread = threading.Thread(
                target=self._reader_loop,
                args=(terminal_id, fifo_path, stop_flag),
                daemon=True,
                name=f"fifo-{terminal_id}",
            )
            self._readers[terminal_id] = stop_flag
            self._threads[terminal_id] = thread
            # Seed the liveness clock BEFORE pipe-pane starts so the first
            # watchdog check has a baseline; the reader bumps it on real data.
            self._last_data_at[terminal_id] = time.monotonic()
            if enroll:
                self._pane_probe[terminal_id] = pane_probe
                self._rearm[terminal_id] = rearm
            thread.start()

        if enroll:
            self._ensure_watchdog()

        logger.info("Started FIFO reader for terminal %s", terminal_id)

    def stop_reader(self, terminal_id: str) -> None:
        """Stop the reader thread (if running) and delete the FIFO file.

        The unlink is best-effort and runs even when no in-memory reader is
        tracked for ``terminal_id`` — e.g. retention cleanup iterating DB
        terminals after a server restart, where ``_readers`` is empty but stale
        ``*.fifo`` files may still be on disk. Without it those files would
        accumulate unbounded.
        """
        with self._lock:
            stop_flag = self._readers.pop(terminal_id, None)
            thread = self._threads.pop(terminal_id, None)
            # Drop watchdog bookkeeping so a re-created terminal starts clean and
            # the watchdog stops probing a gone pane.
            self._pane_probe.pop(terminal_id, None)
            self._rearm.pop(terminal_id, None)
            self._liveness.pop(terminal_id, None)
            self._last_data_at.pop(terminal_id, None)
            self._rearm_failures.pop(terminal_id, None)

        # Deliberately NOT stopping the watchdog thread here even when this was
        # the last enrolled terminal: doing it under a "now idle" check raced
        # against a concurrent create_reader() enrolling a new terminal between
        # this method releasing the lock and calling stop_watchdog() — the
        # watchdog thread that create_reader's _ensure_watchdog() decided was
        # still alive and reusable could be torn down out from under the newly
        # enrolled terminal, leaving it silently unwatched. A single lingering
        # thread waking every PIPE_LIVENESS_CHECK_INTERVAL_S to iterate an empty
        # dict is a cheap, correctness-preserving tradeoff instead; it is
        # actually torn down at process shutdown (api/main.py's lifespan).
        fifo_path = FIFO_DIR / f"{terminal_id}.fifo"

        if stop_flag and thread:
            # The reader never blocks in open()/read() (non-blocking fd +
            # select with a timeout), so setting the flag is sufficient — it is
            # observed within one poll interval. No write-side "wakeup" open is
            # needed; the old wakeup raced with the reader's reopen cycle and
            # could strand the thread forever in a blocking FIFO open on an
            # unlinked inode (issue #382).
            stop_flag.set()
            thread.join(timeout=2.0)
            if thread.is_alive():
                # Never silent: a leaked reader thread was how #382's wedge
                # built up. With the non-blocking loop this should not happen.
                logger.warning(
                    "FIFO reader thread for terminal %s did not exit "
                    "within 2s; leaking a daemon thread",
                    terminal_id,
                )
            else:
                logger.info("Stopped FIFO reader for terminal %s", terminal_id)

        # Best-effort unlink regardless of whether a reader was tracked — when
        # none is tracked there is no active reader holding the FIFO, so removing
        # a stale file on disk is safe.
        try:
            fifo_path.unlink()
        except OSError:
            pass

    def _reader_loop(self, terminal_id: str, fifo_path, stop_flag: threading.Event) -> None:
        """Read chunks from FIFO and publish to the event bus.

        Never blocks in a FIFO ``open()`` (issue #382): the previous design
        opened the pipe with a plain blocking ``O_RDONLY`` and reopened on
        every EOF, which parked the thread in the kernel's ``wait_for_partner``
        whenever no writer was attached. ``stop_reader``'s write-side wakeup
        only worked if the thread happened to be inside ``open()`` at that
        instant — miss the window (post-EOF reopen, error sleep) and the
        thread was stranded forever on an inode whose name had been unlinked.
        Accumulated leaks eventually wedged the whole server.

        Instead:
        - the read end is opened ``O_RDONLY | O_NONBLOCK``, which succeeds
          immediately for a FIFO even with no writer;
        - a keepalive write end is held by this process, so the pipe never
          reaches writer-count zero — ``select`` therefore only reports the fd
          readable when actual data arrives (avoiding the busy EOF spin a
          writer-less non-blocking FIFO would otherwise produce), and tmux
          detaching its ``pipe-pane`` writer produces no EOF churn at all;
        - ``select`` uses a timeout so the stop flag is observed within
          ``_POLL_INTERVAL`` seconds regardless of traffic.

        Chunks are also coalesced (``_COALESCE_WINDOW``) before publishing.
        Kiro's TUI animates a spinner at ~10 fps and each frame is a separate
        FIFO write — publishing one event per raw read floods the shared
        async queue (1024 slots, drop-on-full), and the dropped events wiped
        out worker state transitions that assign/handoff rely on. Batching
        every 50ms of chunks into one event drops the publish rate ~20x
        during bursts while staying well under the status monitor's 200ms
        quiescence debounce, so detection is unaffected and consumers see
        the same bytes in the same order.

        Liveness bookkeeping for the pipe-pane watchdog (issue #388) is
        recorded independent of coalescing, right when bytes are pulled off
        the FIFO — the watchdog only cares whether the FIFO delivered data
        in a window, not whether/when that data was published.
        """
        topic = f"terminal.{terminal_id}.output"
        read_fd = -1
        keepalive_fd = -1
        pending = bytearray()
        # Time at which the currently-accumulating batch started.
        batch_start = 0.0
        try:
            # Non-blocking read open of a FIFO succeeds immediately (POSIX),
            # writer attached or not.
            read_fd = os.open(str(fifo_path), os.O_RDONLY | os.O_NONBLOCK)
            # With our read end open, a non-blocking write open cannot ENXIO.
            keepalive_fd = os.open(str(fifo_path), os.O_WRONLY | os.O_NONBLOCK)

            while not stop_flag.is_set():
                # Wait at most _COALESCE_WINDOW so we always flush pending data
                # within one window even when the writer went silent mid-burst
                # (e.g. kiro's TUI paused between spinner frames). The
                # _POLL_INTERVAL upper bound is still honored when nothing has
                # been received yet (pending is empty).
                timeout = _COALESCE_WINDOW if pending else _POLL_INTERVAL
                readable, _, _ = select.select([read_fd], [], [], timeout)
                if readable:
                    try:
                        raw = os.read(read_fd, CHUNK_SIZE)
                    except BlockingIOError:
                        raw = b""
                    if raw:
                        # Record liveness for the pipe-pane watchdog (issue
                        # #388): the watchdog treats "pane advanced but no
                        # byte delivered since the last check" as a stall.
                        # This must be recorded the instant bytes are pulled
                        # off the FIFO, independent of the coalescing/publish
                        # schedule below — the watchdog cares whether the FIFO
                        # delivered data, not whether/when a batch flushed.
                        #
                        # Guarded by membership rather than unconditional: if
                        # stop_reader already popped this terminal (torn down
                        # while this thread was mid-read, before it noticed
                        # stop_flag), writing here would resurrect a dict entry
                        # nothing will ever clean up again — a slow leak across
                        # create/stop churn. The check is unlocked (consistent
                        # with the watchdog loop's own lock-free dict reads
                        # elsewhere in this class); it only needs to be right
                        # often enough to close the common case, not atomic.
                        if terminal_id in self._readers:
                            self._last_data_at[terminal_id] = time.monotonic()
                        if not pending:
                            batch_start = time.monotonic()
                        pending.extend(raw)

                # Flush conditions: window elapsed, size cap hit, or select
                # returned nothing (writer went idle). "Writer went idle"
                # matters because kiro's TUI can stop emitting bytes mid-turn
                # (waiting on an LLM response) — we must publish what we have
                # so status detection can see the current buffer state.
                if pending and (
                    time.monotonic() - batch_start >= _COALESCE_WINDOW
                    or len(pending) >= _COALESCE_MAX_BYTES
                    or not readable
                ):
                    bus.publish(topic, {"data": pending.decode("utf-8", errors="replace")})
                    pending.clear()
        except Exception as e:
            if not stop_flag.is_set():
                logger.error("FIFO reader for terminal %s exiting on error: %s", terminal_id, e)
        finally:
            # Flush any unpublished bytes so the last frame of a torn-down
            # terminal isn't lost — status/log consumers may need it.
            if pending:
                try:
                    bus.publish(topic, {"data": pending.decode("utf-8", errors="replace")})
                except Exception:
                    pass
            for fd in (read_fd, keepalive_fd):
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    # ---- pipe-pane liveness watchdog (issue #388) ---------------------------

    def _ensure_watchdog(self) -> None:
        """Start the single background watchdog thread on first enrolled reader."""
        with self._lock:
            if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
                return
            self._watchdog_stop.clear()
            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop,
                daemon=True,
                name="fifo-pipe-watchdog",
            )
            self._watchdog_thread.start()

    def stop_watchdog(self) -> None:
        """Stop the watchdog thread (shutdown / tests)."""
        self._watchdog_stop.set()
        thread = self._watchdog_thread
        if thread is not None:
            thread.join(timeout=2.0)

    def _watchdog_loop(self) -> None:
        while not self._watchdog_stop.wait(PIPE_LIVENESS_CHECK_INTERVAL_S):
            for terminal_id in list(self._pane_probe.keys()):
                try:
                    self._check_pipe_liveness(terminal_id)
                except Exception:
                    logger.exception("pipe-pane liveness check failed for terminal %s", terminal_id)

    def _check_pipe_liveness(self, terminal_id: str) -> None:
        """One liveness check for a terminal: re-arm a stalled pipe-pane forwarder.

        A stalled forwarder is invisible from inside the FIFO reader (no bytes to
        read, exactly like an idle terminal). The only ground truth is tmux's own
        live pane content, which keeps rendering through the stall. So:

        - pane content advanced since the last check AND the FIFO delivered no
          bytes in that same window  -> the pipe is stalled: re-arm it.
        - pane content unchanged                                   -> idle: do nothing.
        - FIFO delivered bytes                                     -> healthy: do nothing.

        Requiring BOTH "pane advanced" and "FIFO silent" is what stops a
        legitimately idle terminal (pane unchanged, FIFO silent) from triggering
        a needless re-pipe. Re-arm only after ``PIPE_LIVENESS_STALL_CHECKS``
        consecutive diverging checks (default 2 — a single diverging check can be
        a false positive on a healthy-but-bursty pipe).
        """
        probe = self._pane_probe.get(terminal_id)
        rearm = self._rearm.get(terminal_id)
        if probe is None or rearm is None:
            return

        # probe() is a slow tmux `capture-pane` call — deliberately made
        # without holding self._lock so it never blocks stop_reader() (or
        # other terminals' housekeeping) for its duration.
        content = probe()
        now = time.monotonic()

        do_rearm = False
        with self._lock:
            # stop_reader() may have unenrolled this terminal while probe()
            # was in flight above. Re-check membership before touching any
            # per-terminal state: writing back unconditionally (the previous
            # behavior) would resurrect dict entries for a terminal that is
            # gone — _watchdog_loop only ever iterates _pane_probe, so a
            # resurrected entry in _liveness/_last_data_at is never revisited
            # and never cleaned up, leaking slowly across create/stop churn.
            if terminal_id not in self._pane_probe:
                return
            last_data_at = self._last_data_at.get(terminal_id, 0.0)

            prev = self._liveness.get(terminal_id)
            if prev is None:
                # First observation: establish a baseline, never act on it.
                self._liveness[terminal_id] = (content, now, 0)
                return
            prev_content, last_check_at, strikes = prev

            # Full tail string compared, not a hash: a hash collision would
            # make pane_advanced False and mask a real stall (negligible
            # probability, but the string is just as cheap to compare and
            # collision-free).
            pane_advanced = content != prev_content
            # Did the reader deliver anything since the previous check?
            fifo_advanced = last_data_at >= last_check_at

            if pane_advanced and not fifo_advanced:
                strikes += 1
                if strikes >= PIPE_LIVENESS_STALL_CHECKS:
                    do_rearm = True
                    strikes = 0
            else:
                strikes = 0

            self._liveness[terminal_id] = (content, now, strikes)

        if not do_rearm:
            return

        logger.warning(
            "pipe-pane forwarder for terminal %s appears stalled "
            "(pane advanced, no FIFO data) — re-arming",
            terminal_id,
        )
        try:
            rearm()
        except Exception:
            logger.exception("failed to re-arm pipe-pane for terminal %s", terminal_id)
            with self._lock:
                if terminal_id not in self._pane_probe:
                    return
                failures = self._rearm_failures.get(terminal_id, 0) + 1
                self._rearm_failures[terminal_id] = failures
                give_up = failures >= PIPE_LIVENESS_MAX_REARM_FAILURES
                if give_up:
                    self._pane_probe.pop(terminal_id, None)
                    self._rearm.pop(terminal_id, None)
                    self._liveness.pop(terminal_id, None)
                    self._rearm_failures.pop(terminal_id, None)
            if give_up:
                # Not a silent retry-forever: a re-arm that keeps failing
                # (e.g. the tmux pane is gone) previously re-struck and
                # re-attempted every ~PIPE_LIVENESS_STALL_CHECKS intervals
                # indefinitely, each logging at WARNING/exception — bounded
                # but noisy forever. Give up after N consecutive failures and
                # say so loudly instead.
                logger.error(
                    "pipe-pane forwarder for terminal %s failed to re-arm %d "
                    "consecutive times — giving up and dropping it from the "
                    "liveness watchdog",
                    terminal_id,
                    failures,
                )
            return

        # Bytes lost during the stall are gone (tmux never buffered them), but
        # the pane's *current* content is not — replay it into the pipeline so
        # the StatusMonitor buffer / GET output immediately reflect the live
        # screen instead of staying frozen until the agent happens to emit
        # something new.
        #
        # capture-pane output is joined with a bare "\n" (clients/tmux.py's
        # get_history), which is linefeed-without-carriage-return. pyte's
        # screen (fed by StatusMonitor for CAO_PYTE_STATUS providers — on by
        # default, and opted into by claude_code, exactly the provider #388
        # was filed against) defaults LNM (line-feed/new-line mode) off, so a
        # bare "\n" advances the row without returning to column 0: each
        # replayed line renders indented past the previous one
        # ("staircasing"), and the composited screen no longer matches the
        # real pane until a later cursor-addressed repaint happens to paper
        # over it — meaning status detection can stay broken after the very
        # re-arm meant to fix it. Replaying with "\r\n" makes pyte treat it as
        # a real newline, matching what a real terminal does with tmux's own
        # capture-pane output.
        replay = content.replace("\n", "\r\n")
        with self._lock:
            if terminal_id not in self._pane_probe:
                return
            self._rearm_failures.pop(terminal_id, None)
            self._last_data_at[terminal_id] = time.monotonic()

        bus.publish(f"terminal.{terminal_id}.output", {"data": replay})


# Module-level singleton
fifo_manager = FifoManager()

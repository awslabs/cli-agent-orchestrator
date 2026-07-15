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
    PIPE_LIVENESS_COLD_START_GRACE_S,
    PIPE_LIVENESS_MAX_COLD_START_ATTEMPTS,
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
    diverged from its last known-healthy baseline + FIFO silent = a stall, which
    it self-heals by re-arming the pipe. The baseline is pinned across checks
    (not just the previous one) so a stall that settles into a new static frame
    right after the burst — instead of staying visibly in flux — is still caught
    (harness-control#148).

    Also runs a separate cold-start check (harness-control#93): the
    divergence logic above can only ever catch a pipe that WAS delivering
    and then stalled — it needs a baseline to diverge from. A pipe that has
    been dead since the terminal was created never gets one: an already-idle
    shell prompt renders once and then never changes, so every check sees
    identical content and "diverged" is permanently False. This is the
    common, live-reproduced shape of #93 — a shell that IS genuinely ready,
    sitting on a stable prompt, whose FIFO simply never forwarded anything
    (tmux's ``pipe-pane -o`` only captures output produced after it attaches;
    a fast shell can draw its prompt in the same instant, and the guaranteed
    "nudge a fresh prompt through the pipe" keystroke terminal_service.py
    already sends is not 100% reliable under load). The cold-start check is
    independent of any baseline: has the FIFO delivered ANYTHING since
    registration, within a short, fixed grace period? If not — and the live
    pane already shows real content — re-arm immediately instead of waiting
    on a divergence that structurally cannot happen.
    """

    def __init__(self):
        self._readers: Dict[str, threading.Event] = {}  # terminal_id -> stop flag
        self._threads: Dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

        # ---- pipe-pane liveness watchdog state (issue #388) ----
        # Monotonic timestamp of the last time this terminal is considered to
        # have delivered real, current output. Read by the watchdog to tell a
        # stalled pipe (pane moving, FIFO silent) from a genuinely idle one.
        # Updated from two places, not just the reader thread (round-3
        # Copilot review on #397, flagging this comment as stale): the reader
        # thread bumps it on every real byte pulled off the FIFO, AND
        # _rearm_stalled_pipe() also bumps it after a successful re-arm +
        # replay — deliberately, so the check immediately following a re-arm
        # sees "FIFO advanced" (via the replay, which bypasses the FIFO/
        # reader thread entirely) and doesn't misread the just-fixed pipe as
        # still stalled on the very next tick.
        self._last_data_at: Dict[str, float] = {}
        # Monotonic timestamp of when this reader was registered, and whether
        # the FIFO has EVER delivered a real byte since — the cold-start check
        # (harness-control#93) is "has anything come through, within this much
        # time of registration", entirely independent of the baseline/diverge
        # bookkeeping below (which presupposes at least one healthy delivery
        # to establish a baseline against). Tracked for every reader (like
        # _last_data_at above), not just enrolled ones, so the bookkeeping
        # stays uniform; only enrolled terminals ever have it consulted.
        self._registered_at: Dict[str, float] = {}
        self._ever_delivered: Dict[str, bool] = {}
        # Cold-start re-arm attempts (self-ROAST finding: a rearm() call that
        # succeeds does NOT mean the FIFO actually started delivering — only
        # the reader thread pulling a real byte off it flips _ever_delivered.
        # A genuinely, permanently dead pipe would otherwise re-trigger the
        # cold-start check every grace period forever. Bounded the same way
        # the rearm()-exception path already is, via a dedicated counter so
        # the two failure classes — "rearm() raised" vs. "rearm() succeeded
        # but the pipe still never delivered" — don't get conflated.
        self._cold_start_attempts: Dict[str, int] = {}
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
            now = time.monotonic()
            self._last_data_at[terminal_id] = now
            self._registered_at[terminal_id] = now
            self._ever_delivered[terminal_id] = False
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
            self._registered_at.pop(terminal_id, None)
            self._ever_delivered.pop(terminal_id, None)
            self._cold_start_attempts.pop(terminal_id, None)

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
                        # create/stop churn. The check-then-write must happen
                        # under _lock as one critical section: a stop_reader()
                        # pop between an unlocked check and the assignment
                        # could still resurrect the entry (round-3 Copilot
                        # review on #397). Cheap and non-blocking either way —
                        # this is a plain dict write, not the slow tmux probe.
                        with self._lock:
                            if terminal_id in self._readers:
                                self._last_data_at[terminal_id] = time.monotonic()
                                self._ever_delivered[terminal_id] = True
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
            # Snapshot under _lock: create_reader()/stop_reader() mutate
            # _pane_probe concurrently (enroll/unenroll), and iterating a dict
            # while it's being resized raises RuntimeError — which would kill
            # this thread and permanently disable the watchdog (round-3
            # Copilot review on #397). The lock is released before calling
            # _check_pipe_liveness(), which takes it again itself per-terminal
            # — so no lock is held across the slow probe()/rearm() calls.
            with self._lock:
                terminal_ids = list(self._pane_probe.keys())
            for terminal_id in terminal_ids:
                try:
                    self._check_pipe_liveness(terminal_id)
                except Exception:
                    logger.exception("pipe-pane liveness check failed for terminal %s", terminal_id)

    def _check_pipe_liveness(self, terminal_id: str) -> None:
        """One liveness check for a terminal: re-arm a stalled pipe-pane forwarder.

        A stalled forwarder is invisible from inside the FIFO reader (no bytes to
        read, exactly like an idle terminal). The only ground truth is tmux's own
        live pane content, which keeps rendering through the stall. So:

        - pane content has diverged from the last known-healthy baseline AND the
          FIFO has delivered no bytes since        -> the pipe is stalled: re-arm.
        - pane content matches the baseline                        -> idle: do nothing.
        - FIFO delivered bytes since the last check       -> healthy: re-baseline.

        The baseline is pinned to the pane content last observed while the FIFO
        was confirmed delivering data (or the first-ever observation) — NOT to
        the immediately-previous check's content. This matters for a "burst-then-
        settle" stall: tmux can silently stop forwarding mid-burst and the pane
        then settles into a new, different, static frame before the next poll
        observes it (e.g. an AskUserQuestion menu finishing its render and
        sitting idle awaiting input — harness-control#148's live repro). Comparing
        only against the immediately-previous check's content would see exactly
        one diverging check (baseline -> new frame) followed by unchanging checks
        thereafter (new frame -> same new frame), resetting the strike counter to
        0 forever — even though the FIFO-fed buffer is permanently stuck on stale
        mid-burst content. Pinning the baseline until the FIFO actually delivers
        data again means every check keeps comparing against the ORIGINAL
        pre-stall content, so a settle-and-freeze keeps accumulating strikes
        exactly like an ongoing divergence would.

        Requiring BOTH "diverged from baseline" and "FIFO silent" is what stops a
        legitimately idle terminal (pane unchanged, FIFO silent) from triggering
        a needless re-pipe. Re-arm only after ``PIPE_LIVENESS_STALL_CHECKS``
        consecutive checks confirm the divergence persists (default 2 — a single
        diverging check can be a false positive on a healthy-but-bursty pipe).

        Before any of that, a separate cold-start check (harness-control#93)
        runs first: has the FIFO delivered ANYTHING since the terminal was
        registered, within ``PIPE_LIVENESS_COLD_START_GRACE_S``? This is not
        a variant of the divergence logic — it is checked instead of it,
        because divergence requires a healthy baseline to diverge FROM, and a
        pipe that's been dead since t=0 never gets one. See module docstring.
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
        cold_start = False
        cold_start_give_up = False
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

            # ---- cold-start check (harness-control#93) ----
            # `.get(..., True)` / `registered_at is None` default to "not a
            # cold start": entries created via create_reader() always have
            # both set, but tests that poke _pane_probe/_rearm/_last_data_at
            # directly (bypassing create_reader) leave them absent, and must
            # keep exercising only the pre-existing divergence path.
            ever_delivered = self._ever_delivered.get(terminal_id, True)
            registered_at = self._registered_at.get(terminal_id)
            if (
                not ever_delivered
                and registered_at is not None
                and now - registered_at >= PIPE_LIVENESS_COLD_START_GRACE_S
                and content.strip()
            ):
                # The FIFO has never delivered a single byte in the grace
                # period since registration, yet the live pane already has
                # real content — the forwarder never started, full stop.
                #
                # self-ROAST finding: a rearm() call SUCCEEDING does not mean
                # the pipe actually started delivering — only the reader
                # thread pulling a real byte off it flips `ever_delivered`
                # (the replay below publishes straight to the event bus,
                # bypassing the FIFO entirely, so it never touches that
                # flag). Without a bound, a genuinely, permanently dead pipe
                # would re-trigger this exact branch every grace period
                # forever: an unbounded stop/start + replay loop, live-
                # reproduced (5/5 checks all re-armed with a never-delivering
                # fake FIFO). Bounded the same way the rearm()-exception path
                # already is, via a dedicated counter/constant so the two
                # failure classes don't get conflated.
                attempts = self._cold_start_attempts.get(terminal_id, 0) + 1
                if attempts > PIPE_LIVENESS_MAX_COLD_START_ATTEMPTS:
                    cold_start_give_up = True
                    self._pane_probe.pop(terminal_id, None)
                    self._rearm.pop(terminal_id, None)
                    self._liveness.pop(terminal_id, None)
                    self._rearm_failures.pop(terminal_id, None)
                    self._registered_at.pop(terminal_id, None)
                    self._ever_delivered.pop(terminal_id, None)
                    self._cold_start_attempts.pop(terminal_id, None)
                else:
                    self._cold_start_attempts[terminal_id] = attempts
                    # Reset the grace-period clock so the NEXT evaluation is
                    # a fresh grace period after THIS attempt, not literally
                    # the very next watchdog tick (~PIPE_LIVENESS_CHECK_
                    # INTERVAL_S later) — gives each rearm a real chance to
                    # start delivering before being judged again.
                    self._registered_at[terminal_id] = now
                    cold_start = True
                    do_rearm = True
                    # Reset liveness bookkeeping to the post-rearm state so
                    # the divergence check starts clean on the next tick
                    # instead of possibly re-triggering off a baseline
                    # captured before rearm.
                    self._liveness[terminal_id] = (content, now, 0)
            else:
                prev = self._liveness.get(terminal_id)
                if prev is None:
                    # First observation: establish a baseline, never act on it.
                    self._liveness[terminal_id] = (content, now, 0)
                else:
                    baseline_content, last_check_at, strikes = prev

                    # Did the reader deliver anything since the previous check?
                    fifo_advanced = last_data_at >= last_check_at

                    if fifo_advanced:
                        # Healthy: the pipe is confirmed delivering. Re-baseline
                        # to the current pane content and clear strikes.
                        self._liveness[terminal_id] = (content, now, 0)
                    else:
                        # FIFO silent since the last check. Compare against the
                        # STICKY baseline (last known-healthy content), not the
                        # previous check's content — see docstring for why this
                        # matters for a burst-then-settle stall. Full tail
                        # string compared, not a hash: a hash collision would
                        # make this False and mask a real stall (negligible
                        # probability, but the string is just as cheap to
                        # compare and collision-free).
                        #
                        # Tradeoff (round-3 review, call-me-ram): pinning the
                        # baseline means ANY one-shot pane divergence seen
                        # while the FIFO happens to be silent — not just a
                        # genuine stall — now accumulates strikes toward a
                        # re-arm, where the old previous-check comparison
                        # would have reset on the very next unchanging check.
                        # E.g. an attached client resizing the pane, causing
                        # tmux to rewrap the captured tail once, then nothing
                        # further changing. The cost of a false-positive
                        # re-arm here is mild (a stop/start on an otherwise-
                        # idle pipe plus one snapshot replay), and it's the
                        # unavoidable price of catching a stall that settles
                        # into a new static frame — accepted deliberately.
                        diverged_from_baseline = content != baseline_content

                        if diverged_from_baseline:
                            strikes += 1
                            if strikes >= PIPE_LIVENESS_STALL_CHECKS:
                                do_rearm = True
                                strikes = 0
                        else:
                            strikes = 0

                        # Baseline is intentionally left unchanged (not reset
                        # to `content`) so a burst-then-settle stall keeps
                        # accumulating strikes against the original pre-stall
                        # baseline across checks where the now-static content
                        # no longer changes.
                        self._liveness[terminal_id] = (baseline_content, now, strikes)

        if cold_start_give_up:
            logger.error(
                "pipe-pane forwarder for terminal %s never started delivering after "
                "%d cold-start re-arm attempts — giving up and dropping it from the "
                "liveness watchdog",
                terminal_id,
                PIPE_LIVENESS_MAX_COLD_START_ATTEMPTS,
            )
            return

        if not do_rearm:
            return

        self._rearm_stalled_pipe(terminal_id, content, rearm, cold_start=cold_start)

    def _rearm_stalled_pipe(
        self, terminal_id: str, content: str, rearm: RearmPipe, *, cold_start: bool
    ) -> None:
        """Re-arm a confirmed-stalled pipe-pane forwarder and replay the live
        pane snapshot into the pipeline, shared by both stall checks above —
        they differ only in HOW a stall is detected, not in what happens once
        one is confirmed.

        Bytes lost during the stall are gone (tmux never buffered them), but
        the pane's *current* content is not — replay it into the pipeline so
        the StatusMonitor buffer / GET output immediately reflect the live
        screen instead of staying frozen until the agent happens to emit
        something new. This is also what makes the cold-start case actually
        resolve: the replay publishes straight to the event bus, bypassing
        the FIFO reader entirely, so wait_for_shell()'s buffer read is
        satisfied even though the FIFO itself never delivered a byte.

        capture-pane output is joined with a bare "\\n" (clients/tmux.py's
        get_history), which is linefeed-without-carriage-return. pyte's
        screen (fed by StatusMonitor for CAO_PYTE_STATUS providers — on by
        default, and opted into by claude_code, exactly the provider #388
        was filed against) defaults LNM (line-feed/new-line mode) off, so a
        bare "\\n" advances the row without returning to column 0: each
        replayed line renders indented past the previous one
        ("staircasing"), and the composited screen no longer matches the
        real pane until a later cursor-addressed repaint happens to paper
        over it — meaning status detection can stay broken after the very
        re-arm meant to fix it. Replaying with "\\r\\n" makes pyte treat it as
        a real newline, matching what a real terminal does with tmux's own
        capture-pane output.
        """
        logger.warning(
            "pipe-pane forwarder for terminal %s appears %s — re-arming",
            terminal_id,
            (
                "never started forwarding (cold-start, harness-control#93)"
                if cold_start
                else "stalled (pane advanced, no FIFO data)"
            ),
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
                    self._registered_at.pop(terminal_id, None)
                    self._ever_delivered.pop(terminal_id, None)
                    self._cold_start_attempts.pop(terminal_id, None)
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

        replay = content.replace("\n", "\r\n")
        with self._lock:
            if terminal_id not in self._pane_probe:
                return
            self._rearm_failures.pop(terminal_id, None)
            self._last_data_at[terminal_id] = time.monotonic()

        bus.publish(f"terminal.{terminal_id}.output", {"data": replay})


# Module-level singleton
fifo_manager = FifoManager()

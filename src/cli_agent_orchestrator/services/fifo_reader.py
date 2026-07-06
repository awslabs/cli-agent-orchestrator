"""FIFO reader for streaming terminal output from tmux pipe-pane.

Publisher: terminal.{id}.output
"""

import logging
import os
import select
import threading
from typing import Dict

from cli_agent_orchestrator.constants import FIFO_DIR
from cli_agent_orchestrator.services.event_bus import bus

logger = logging.getLogger(__name__)

CHUNK_SIZE = 4096

# How often a parked reader re-checks its stop flag. Bounds both shutdown
# latency and the cost of an idle terminal (one select wakeup per interval).
_POLL_INTERVAL = 0.5


class FifoManager:
    """Manages FIFO lifecycle: create named pipe, start reader thread, stop and cleanup."""

    def __init__(self):
        self._readers: Dict[str, threading.Event] = {}  # terminal_id -> stop flag
        self._threads: Dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        FIFO_DIR.mkdir(parents=True, exist_ok=True)

    def create_reader(self, terminal_id: str) -> None:
        """Create FIFO and start reader thread."""
        fifo_path = FIFO_DIR / f"{terminal_id}.fifo"

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
            thread.start()

        logger.info(f"Started FIFO reader for terminal {terminal_id}")

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
                    f"FIFO reader thread for terminal {terminal_id} did not exit "
                    "within 2s; leaking a daemon thread"
                )
            else:
                logger.info(f"Stopped FIFO reader for terminal {terminal_id}")

        # Best-effort unlink regardless of whether a reader was tracked — when
        # none is tracked there is no active reader holding the FIFO, so removing
        # a stale file on disk is safe.
        try:
            fifo_path.unlink()
        except OSError:
            pass

    @staticmethod
    def _reader_loop(terminal_id: str, fifo_path, stop_flag: threading.Event) -> None:
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
        """
        read_fd = -1
        keepalive_fd = -1
        try:
            # Non-blocking read open of a FIFO succeeds immediately (POSIX),
            # writer attached or not.
            read_fd = os.open(str(fifo_path), os.O_RDONLY | os.O_NONBLOCK)
            # With our read end open, a non-blocking write open cannot ENXIO.
            keepalive_fd = os.open(str(fifo_path), os.O_WRONLY | os.O_NONBLOCK)

            while not stop_flag.is_set():
                readable, _, _ = select.select([read_fd], [], [], _POLL_INTERVAL)
                if not readable:
                    continue
                try:
                    raw = os.read(read_fd, CHUNK_SIZE)
                except BlockingIOError:
                    continue
                if raw:
                    chunk = raw.decode("utf-8", errors="replace")
                    bus.publish(f"terminal.{terminal_id}.output", {"data": chunk})
        except Exception as e:
            if not stop_flag.is_set():
                logger.error(f"FIFO reader for terminal {terminal_id} exiting on error: {e}")
        finally:
            for fd in (read_fd, keepalive_fd):
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass


# Module-level singleton
fifo_manager = FifoManager()

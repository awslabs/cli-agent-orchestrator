"""FIFO reader for streaming terminal output from tmux pipe-pane.

Publisher: terminal.{id}.output
"""

import logging
import os
import threading
import time
from typing import Dict

from cli_agent_orchestrator.constants import FIFO_DIR
from cli_agent_orchestrator.services.event_bus import bus

logger = logging.getLogger(__name__)

CHUNK_SIZE = 4096


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
        """Stop reader thread and delete FIFO file."""
        with self._lock:
            stop_flag = self._readers.pop(terminal_id, None)
            thread = self._threads.pop(terminal_id, None)

        if not stop_flag or not thread:
            return

        stop_flag.set()

        # Unblock thread if stuck on open() by briefly opening write side
        fifo_path = FIFO_DIR / f"{terminal_id}.fifo"
        try:
            fd = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
            os.close(fd)
        except OSError:
            pass

        thread.join(timeout=2.0)

        try:
            fifo_path.unlink()
        except OSError:
            pass

        logger.info(f"Stopped FIFO reader for terminal {terminal_id}")

    @staticmethod
    def _reader_loop(terminal_id: str, fifo_path, stop_flag: threading.Event) -> None:
        """Read chunks from FIFO and publish to event bus. Reopens on EOF."""
        while not stop_flag.is_set():
            try:
                with open(fifo_path, "r") as fifo:
                    while not stop_flag.is_set():
                        chunk = fifo.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        bus.publish(f"terminal.{terminal_id}.output", {"data": chunk})
            except Exception as e:
                if not stop_flag.is_set():
                    logger.error(f"FIFO read error for terminal {terminal_id}: {e}")
                    time.sleep(1.0)


# Module-level singleton
fifo_manager = FifoManager()

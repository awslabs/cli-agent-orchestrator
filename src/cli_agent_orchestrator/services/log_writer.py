"""Writes terminal output to per-terminal log files for debugging.

Consumer: terminal.{id}.output

Batching rationale
------------------
Under a heavy output burst (two workers each streaming multi-KB frames at
20+ Hz), the naive "one asyncio.to_thread(write) per event" pattern caps
LogWriter throughput at ~one file-write per event-loop tick. That's slower
than the FIFO reader's publish rate, so the shared event-bus queue fills
and starts dropping events. Symptoms: 40k+ ``event_bus queue full`` log
lines in ~20 minutes and stalled per-terminal log files.

The writer now drains up to ``_MAX_BATCH`` events per group before
scheduling a single ``asyncio.to_thread(write)``. Same-terminal chunks are
concatenated into one file-open, keeping ordering and cutting file-open
overhead. Different terminals' chunks are grouped by path so a burst on
terminal A doesn't block writes for terminal B.
"""

import asyncio
import logging
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

from cli_agent_orchestrator.constants import TERMINAL_LOG_DIR
from cli_agent_orchestrator.services.event_bus import bus
from cli_agent_orchestrator.utils.event import terminal_id_from_topic

logger = logging.getLogger(__name__)

# Cap on events drained before flushing. Higher = better throughput under
# burst, at the cost of larger latency between an output chunk landing and
# it being visible in the log file. 256 gives ~1 second of latency at the
# 200-events/sec rate observed under two concurrent evaluators.
_MAX_BATCH = 256


class LogWriter:
    """Appends terminal output chunks to log files.

    Runs one coroutine that drains the shared event-bus queue in batches
    and delegates the actual file I/O to a threadpool via
    ``asyncio.to_thread``. Ordering per terminal is preserved because we
    concatenate all pending chunks for a given path before scheduling the
    write.
    """

    async def run(self) -> None:
        queue = bus.subscribe("terminal.*.output")
        logger.info("LogWriter started")

        while True:
            try:
                # Block until at least one event is available.
                first_event = await queue.get()
                events = [first_event]

                # Opportunistically drain more without blocking. We stop at
                # _MAX_BATCH to bound the per-flush latency and avoid holding
                # the loop for the entire tick under a sustained burst.
                while len(events) < _MAX_BATCH:
                    try:
                        events.append(queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                # Group same-file writes so we open each file at most once
                # per batch. Preserves order because dict-of-lists append
                # order matches drain order (Python 3.7+ dicts are ordered).
                grouped: Dict[Path, List[str]] = defaultdict(list)
                for event in events:
                    terminal_id = terminal_id_from_topic(event["topic"])
                    log_path = TERMINAL_LOG_DIR / f"{terminal_id}.log"
                    grouped[log_path].append(event["data"]["data"])

                # One thread hop per unique file, not per event. Schedule the
                # per-file writes concurrently (gather) so a large batch for
                # terminal A doesn't serialize ahead of terminal B's write —
                # each is an independent append to a distinct path.
                await asyncio.gather(
                    *(
                        asyncio.to_thread(self._write, path, "".join(chunks))
                        for path, chunks in grouped.items()
                    )
                )
            except Exception as e:
                # Log-writer failure must never crash the whole loop:
                # continue draining so back-pressure recovers.
                logger.error(f"Failed to write log: {e}")

    @staticmethod
    def _write(path: Path, data: str) -> None:
        # Explicit UTF-8: the platform default encoding can be non-UTF-8
        # (e.g. POSIX/C locale), and a single unencodable chunk would raise
        # UnicodeEncodeError and stop log persistence for the terminal.
        with open(path, "a", encoding="utf-8", errors="replace") as f:
            f.write(data)


log_writer = LogWriter()

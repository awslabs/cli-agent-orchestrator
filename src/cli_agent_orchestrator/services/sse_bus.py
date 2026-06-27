"""SSE fan-out bus for relaying live normalized fleet events to iframes.

Each connected MCP App iframe subscribes and receives its own bounded
``asyncio.Queue``. ``publish`` is **non-blocking and drop-on-slow**: if a
subscriber's queue is full (a stalled or slow iframe), its event is dropped
rather than blocking the producer — the durable record is always the ring
buffer (``event_log_service``), which the iframe backfills from via
``cao_fetch_history``. One slow consumer can therefore never apply
back-pressure to the orchestration core.
"""

import asyncio
import logging
import threading
from typing import AsyncGenerator, Dict, List, Optional

logger = logging.getLogger(__name__)

# Per-subscriber queue capacity. Mirrors the design's SSE_MAX_QUEUE_SIZE.
SSE_MAX_QUEUE_SIZE = 256


class SseBus:
    """Per-subscriber bounded-queue fan-out; drop-on-slow, never blocks producers."""

    def __init__(self) -> None:
        """Create a bus with no subscribers."""

        self._subs: List["asyncio.Queue[Dict]"] = []
        # A threading.Lock (not asyncio.Lock) guards the subscriber list so
        # publish() is safe to call from any thread — lifecycle hooks may run
        # off the event loop, and the producer must never await.
        self._lock = threading.Lock()

    def publish(self, event: Dict) -> None:
        """Deliver an event to every subscriber with available capacity.

        Non-blocking: a full subscriber queue causes the event to be dropped
        for that subscriber only; all other subscribers are still served and
        the caller returns immediately regardless of queue state.
        """

        with self._lock:
            subscribers = list(self._subs)
        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Drop for this slow subscriber; history backfills via
                # cao_fetch_history. Debug-level to avoid log spam under load.
                logger.debug("SSE subscriber queue full; dropping event %s", event.get("id"))

    async def subscribe(self) -> AsyncGenerator[Dict, None]:
        """Register a new subscriber queue and yield events until cancelled.

        The queue is removed from the active set when the generator is closed
        (subscriber disconnect / iframe teardown / cancellation).
        """

        queue: "asyncio.Queue[Dict]" = asyncio.Queue(maxsize=SSE_MAX_QUEUE_SIZE)
        with self._lock:
            self._subs.append(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            with self._lock:
                try:
                    self._subs.remove(queue)
                except ValueError:
                    pass

    @property
    def subscriber_count(self) -> int:
        """Return the number of currently active subscribers."""

        with self._lock:
            return len(self._subs)


_bus: Optional[SseBus] = None
_bus_lock = threading.Lock()


def get_bus() -> SseBus:
    """Return the process-wide singleton ``SseBus`` (lazily created)."""

    global _bus
    if _bus is None:
        with _bus_lock:
            if _bus is None:
                _bus = SseBus()
    return _bus

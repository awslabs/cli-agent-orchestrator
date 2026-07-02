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
from typing import AsyncGenerator, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Per-subscriber queue capacity. Mirrors the design's SSE_MAX_QUEUE_SIZE.
SSE_MAX_QUEUE_SIZE = 256


class SseBus:
    """Per-subscriber bounded-queue fan-out; drop-on-slow, never blocks producers."""

    def __init__(self) -> None:
        """Create a bus with no subscribers."""

        # Pair each subscriber queue with the event loop it was created on, so
        # publish() can feed it thread-safely (asyncio.Queue is not thread-safe).
        self._subs: List[Tuple["asyncio.Queue[Dict]", asyncio.AbstractEventLoop]] = []
        # A threading.Lock (not asyncio.Lock) guards the subscriber list so
        # publish() is safe to call from any thread — lifecycle hooks may run
        # off the event loop, and the producer must never await.
        self._lock = threading.Lock()

    def publish(self, event: Dict) -> None:
        """Deliver an event to every subscriber with available capacity.

        Thread-safe and non-blocking. ``asyncio.Queue`` is not thread-safe, and
        ``publish`` may be invoked off the event loop (e.g. a plugin lifecycle
        hook running under ``asyncio.run``), so each subscriber's queue is fed
        on the loop it was created on via ``call_soon_threadsafe``. A full
        subscriber queue drops the event for that subscriber only — the durable
        record is the ring buffer, which the iframe backfills via
        ``cao_fetch_history``. The caller never blocks.
        """

        with self._lock:
            subscribers = list(self._subs)

        def _deliver(queue: "asyncio.Queue[Dict]") -> None:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("Subscriber queue full. Dropping event.")

        dead: List[Tuple["asyncio.Queue[Dict]", asyncio.AbstractEventLoop]] = []
        for entry in subscribers:
            queue, loop = entry
            try:
                loop.call_soon_threadsafe(_deliver, queue)
            except RuntimeError:
                # Subscriber's loop is closed/closing — treat as disconnected and
                # unregister it so it is neither retried nor logged on every
                # subsequent publish. ``subscribe()``'s ``finally`` also removes
                # it on normal teardown; this covers the case where the loop dies
                # before that runs (otherwise the entry would leak).
                dead.append(entry)
                logger.debug("SSE subscriber loop unavailable; dropping subscriber")

        if dead:
            with self._lock:
                for entry in dead:
                    try:
                        self._subs.remove(entry)
                    except ValueError:
                        pass

    async def subscribe(self) -> AsyncGenerator[Dict, None]:
        """Register a new subscriber queue and yield events until cancelled.

        The queue is removed from the active set when the generator is closed
        (subscriber disconnect / iframe teardown / cancellation).
        """

        loop = asyncio.get_running_loop()
        queue: "asyncio.Queue[Dict]" = asyncio.Queue(maxsize=SSE_MAX_QUEUE_SIZE)
        entry = (queue, loop)
        with self._lock:
            self._subs.append(entry)
        try:
            while True:
                yield await queue.get()
        finally:
            with self._lock:
                try:
                    self._subs.remove(entry)
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

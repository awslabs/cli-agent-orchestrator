"""SSE fan-out bus for relaying live normalized fleet events to iframes.

Each connected MCP App iframe subscribes and receives its own bounded
``asyncio.Queue``. ``publish`` is **non-blocking**: it never lets one slow
consumer apply back-pressure to the orchestration core.

Two overflow policies exist for a full queue, chosen per subscriber at
``register`` time:

* **drop-on-slow** (default, ``overflow_close=False``): the event is dropped for
  that subscriber only — the durable record is the ring buffer
  (``event_log_service``), which the iframe backfills via ``cao_fetch_history``.
  This is the legacy ``/events`` behaviour and is preserved verbatim.
* **overflow-close** (``overflow_close=True``, used by the AG-UI stream): a full
  queue is turned into an explicit *gap signal* — the subscriber is marked
  ``overflowed`` and an ``OVERFLOW_SENTINEL`` is enqueued so the drain loop
  terminates the HTTP stream. The client then reconnects (native ``EventSource``
  resends ``Last-Event-ID``) and the endpoint replays the dropped records
  exactly once. This closes the "silent gap" hole (PR #436, F2): overflow is no
  longer a silent drop on an open connection.
"""

import asyncio
import logging
import threading
from typing import AsyncGenerator, Dict, List, Optional

logger = logging.getLogger(__name__)

# Per-subscriber queue capacity. Mirrors the design's SSE_MAX_QUEUE_SIZE.
SSE_MAX_QUEUE_SIZE = 256

# Sentinel enqueued to an ``overflow_close`` subscriber whose bounded queue
# fills. It is a unique object compared by identity (``is``), never by value, so
# it can never collide with a real event; typed as ``Dict`` so the per-subscriber
# queue stays ``Queue[Dict]``. The drain loop returns when it sees this, closing
# the stream so the client reconnects and backfills the gap.
OVERFLOW_SENTINEL: Dict = {"__cao_sse_overflow__": True}


class _Subscriber:
    """A single SSE subscription: its queue, the loop it lives on, and its
    overflow policy/state.

    ``overflowed`` is only ever written from ``_deliver`` (which runs on
    ``loop`` via ``call_soon_threadsafe``) and read by ``drain`` on that same
    loop, so it needs no extra locking — a single-threaded event loop serializes
    both.
    """

    def __init__(
        self,
        queue: "asyncio.Queue[Dict]",
        loop: asyncio.AbstractEventLoop,
        overflow_close: bool,
    ) -> None:
        self.queue = queue
        self.loop = loop
        self.overflow_close = overflow_close
        self.overflowed = False


class SseBus:
    """Per-subscriber bounded-queue fan-out; drop-on-slow or overflow-close."""

    def __init__(self) -> None:
        """Create a bus with no subscribers."""

        # A threading.Lock (not asyncio.Lock) guards the subscriber list so
        # publish() is safe to call from any thread — lifecycle hooks may run
        # off the event loop, and the producer must never await.
        self._subs: List[_Subscriber] = []
        self._lock = threading.Lock()

    def publish(self, event: Dict) -> None:
        """Deliver an event to every subscriber with available capacity.

        Thread-safe and non-blocking. ``asyncio.Queue`` is not thread-safe, and
        ``publish`` may be invoked off the event loop (e.g. a plugin lifecycle
        hook running under ``asyncio.run``), so each subscriber's queue is fed on
        the loop it was created on via ``call_soon_threadsafe``. A full queue
        either drops the event (drop-on-slow subscribers) or is turned into a gap
        signal (overflow-close subscribers); see the module docstring. The caller
        never blocks.
        """

        with self._lock:
            subscribers = list(self._subs)

        def _deliver(sub: _Subscriber) -> None:
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                if not sub.overflow_close:
                    # Legacy drop-on-slow: the ring buffer is the durable record.
                    logger.warning("Subscriber queue full. Dropping event.")
                    return
                if sub.overflowed:
                    # Gap already signalled; the sentinel is queued and the drain
                    # loop will close the stream. Extra events are dropped (they
                    # are replayed on reconnect via Last-Event-ID).
                    return
                # First overflow: mark the gap and enqueue the sentinel. We must
                # free a slot for it, and *which* event we drop is load-bearing
                # for lossless recovery. The client replays on reconnect from the
                # last id it actually received (``Last-Event-ID``), so the events
                # it sees before the gap MUST form an unbroken prefix of the
                # stream — otherwise a hole *before* that id can never be filled
                # (the no-loss bug fanhongy reported on PR #436: dropping the
                # OLDEST buffered event left e0 permanently missing). So drop the
                # NEWEST buffered event instead: the prefix stays contiguous, and
                # the dropped event — plus every later one — is durably in the
                # event log and replayed via ``after_id(last_delivered)``.
                #
                # asyncio.Queue has no public tail-drop, so drain the queue and
                # re-enqueue all but the newest, then the sentinel. Safe without
                # guards: this branch is only reached on a *full* queue and
                # ``_deliver`` runs synchronously on the queue's own loop, so no
                # other coroutine drains it in between.
                sub.overflowed = True
                logger.warning("Subscriber queue overflowed; signalling reconnect.")
                buffered: List[Dict] = []
                while not sub.queue.empty():
                    buffered.append(sub.queue.get_nowait())
                for item in buffered[:-1]:  # keep the contiguous prefix; drop newest
                    sub.queue.put_nowait(item)
                sub.queue.put_nowait(OVERFLOW_SENTINEL)

        dead: List[_Subscriber] = []
        for sub in subscribers:
            try:
                sub.loop.call_soon_threadsafe(_deliver, sub)
            except RuntimeError:
                # Subscriber's loop is closed/closing — treat as disconnected and
                # unregister it so it is neither retried nor logged on every
                # subsequent publish. ``subscribe()``'s ``finally`` also removes
                # it on normal teardown; this covers the case where the loop dies
                # before that runs (otherwise the entry would leak).
                dead.append(sub)
                logger.debug("SSE subscriber loop unavailable; dropping subscriber")

        if dead:
            with self._lock:
                self._subs = [s for s in self._subs if s not in dead]

    def register(self, overflow_close: bool = False) -> _Subscriber:
        """Register a subscriber immediately and return its handle.

        Unlike :meth:`subscribe` — whose queue is registered lazily on the first
        iteration — this registers synchronously, so a caller can start buffering
        live events *before* it takes a history snapshot. That closes the
        replay/live-subscription gap: an event published during the replay→live
        handoff lands in this queue instead of being lost. Pair with
        :meth:`unregister` in a ``finally``.

        ``overflow_close=True`` opts the subscriber into the overflow-as-gap-
        signal behaviour (see the module docstring); the default preserves the
        legacy drop-on-slow policy used by ``/events``.
        """

        loop = asyncio.get_running_loop()
        queue: "asyncio.Queue[Dict]" = asyncio.Queue(maxsize=SSE_MAX_QUEUE_SIZE)
        sub = _Subscriber(queue, loop, overflow_close)
        with self._lock:
            self._subs.append(sub)
        return sub

    def unregister(self, sub: _Subscriber) -> None:
        """Remove a subscriber registered via :meth:`register` (idempotent)."""

        with self._lock:
            self._subs = [s for s in self._subs if s is not sub]

    async def drain(self, sub: _Subscriber) -> AsyncGenerator[Dict, None]:
        """Yield events from a subscriber obtained via :meth:`register`.

        Yields until cancelled (client disconnect) or, for an overflow-close
        subscriber, until the ``OVERFLOW_SENTINEL`` is reached — at which point
        the generator returns, closing the stream so the client reconnects and
        backfills the gap. Split out from :meth:`subscribe` so a caller can
        ``register`` the queue *before* taking a snapshot (closing the
        replay/live gap) and then drain it here.
        """

        while True:
            item = await sub.queue.get()
            if item is OVERFLOW_SENTINEL:
                # Overflow gap signal: close the stream. The caller's ``finally``
                # unregisters the subscriber; the client reconnects and replays.
                return
            yield item

    async def subscribe(self) -> AsyncGenerator[Dict, None]:
        """Register a new subscriber and yield events until cancelled.

        Uses the default drop-on-slow policy (``overflow_close=False``), so the
        legacy ``/events`` behaviour is preserved: a full queue drops events and
        the stream stays open. The subscriber is removed when the generator is
        closed (disconnect / teardown / cancellation).
        """

        sub = self.register()
        try:
            async for event in self.drain(sub):
                yield event
        finally:
            self.unregister(sub)

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


# Backward-compatible alias. Some consumers (e.g. services.zellij_bridge) refer
# to this class as ``SSEBus``; keep the old name importable so they work.
SSEBus = SseBus


def reset_bus() -> None:
    """Drop the singleton SSE bus (used by tests to start with a clean slate)."""

    global _bus
    with _bus_lock:
        _bus = None

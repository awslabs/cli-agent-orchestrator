"""In-process pub/sub event bus with wildcard topic matching.

Event Topics:
- terminal.{id}.output  → raw output chunks (from FIFO readers)
- terminal.{id}.status  → status changes (from StatusMonitor)
"""

import asyncio
import logging
import re
import threading
import time
from typing import Dict, List, Optional, Tuple

from cli_agent_orchestrator.services.settings_service import get_server_settings

logger = logging.getLogger(__name__)

# Minimum seconds between "Queue full" summaries per topic. Under a real
# output burst _dispatch can be called thousands of times per second; logging
# every drop as ERROR fills the log file and, when the logging handler is
# synchronous, contributes to event-loop starvation. Instead we accumulate
# per-topic drop counts and emit one summary line per topic at most once per
# second — the operator still sees the signal, but the loop stays free.
_DROP_LOG_INTERVAL_SECS = 1.0

# Bound the per-topic drop-state maps. Topics embed terminal IDs
# (``terminal.<id>.output``), so a long-running server that churns through many
# terminals would otherwise accumulate a dead entry per terminal forever. When
# the maps exceed _DROP_STATE_MAX_TOPICS we evict entries not touched within
# _DROP_STATE_TTL_SECS — a topic that stopped dropping that long ago is stale,
# and if it drops again it simply re-registers as a fresh first-drop.
_DROP_STATE_MAX_TOPICS = 1024
_DROP_STATE_TTL_SECS = 300.0


class EventBus:
    """Thread-safe publishing, async consumption via asyncio.Queue."""

    def __init__(self):
        self._exact: Dict[str, List[asyncio.Queue]] = {}
        self._wildcard: Dict[str, Tuple[re.Pattern, List[asyncio.Queue]]] = {}
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Per-topic rate-limit state for queue-full drop reporting.
        # {topic: (dropped_since_last_log, last_log_monotonic)}
        self._drop_counts: Dict[str, int] = {}
        self._drop_last_logged: Dict[str, float] = {}

    def set_loop(self, loop: Optional[asyncio.AbstractEventLoop]) -> None:
        """Register the asyncio event loop (required for thread-safe publishing).

        Pass ``None`` to detach the bus from a loop (used by test fixtures and
        at shutdown) — publish() becomes a no-op until a loop is set again.
        """
        self._loop = loop

    def publish(self, topic: str, data: dict) -> None:
        """Publish event to all matching subscribers. Safe to call from any thread."""
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._dispatch, topic, data)
        except RuntimeError:
            # Loop already closed (server shutting down while a FIFO reader
            # thread drains its last chunks) — drop the event instead of
            # crashing the publisher thread.
            logger.debug(f"Event bus loop closed; dropping event: {topic}")

    def subscribe(self, pattern: str) -> asyncio.Queue:
        """Subscribe to a topic pattern (e.g., 'terminal.*.output'). Returns async queue."""
        queue: asyncio.Queue = asyncio.Queue(
            maxsize=get_server_settings()["event_bus_max_queue_size"]
        )

        with self._lock:
            if "*" in pattern:
                regex = pattern.replace(".", r"\.").replace("*", "[^.]+")
                if regex not in self._wildcard:
                    self._wildcard[regex] = (re.compile(f"^{regex}$"), [])
                self._wildcard[regex][1].append(queue)
            else:
                if pattern not in self._exact:
                    self._exact[pattern] = []
                self._exact[pattern].append(queue)

        return queue

    def unsubscribe(self, pattern: str, queue: asyncio.Queue) -> None:
        """Remove a queue from a subscription pattern."""
        with self._lock:
            if "*" in pattern:
                regex = pattern.replace(".", r"\.").replace("*", "[^.]+")
                if regex in self._wildcard:
                    queues = self._wildcard[regex][1]
                    try:
                        queues.remove(queue)
                    except ValueError:
                        pass
                    if not queues:
                        del self._wildcard[regex]
            else:
                if pattern in self._exact:
                    try:
                        self._exact[pattern].remove(queue)
                    except ValueError:
                        pass
                    if not self._exact[pattern]:
                        del self._exact[pattern]

    def _prune_drop_state(self, now: float) -> None:
        """Drop rate-limit entries for topics idle longer than the TTL.

        Runs on the loop thread (via ``_record_drop`` → ``_dispatch``), so the
        drop-state dicts are single-threaded and need no lock. If every entry is
        still fresh (a pathological burst across >1024 live topics), nothing is
        evicted — the maps are allowed to exceed the cap rather than drop live
        state; the cap is a floor for eviction, not a hard ceiling.
        """
        stale = [
            t for t, last in self._drop_last_logged.items() if now - last >= _DROP_STATE_TTL_SECS
        ]
        for t in stale:
            self._drop_last_logged.pop(t, None)
            self._drop_counts.pop(t, None)

    def _record_drop(self, topic: str) -> None:
        """Track a drop for ``topic`` and log a rate-limited summary.

        Called from ``_dispatch`` (loop thread) so no locking needed for
        the drop-count dicts — they are not touched from other threads.
        The first drop for a topic emits a WARNING immediately so the signal
        is not silently swallowed; subsequent drops within
        ``_DROP_LOG_INTERVAL_SECS`` are counted and rolled up.
        """
        now = time.monotonic()

        # Evict stale topics before inserting a new one so the maps stay bounded
        # even on a server that churns through thousands of short-lived
        # terminals. Only runs when the map has grown past the cap, so it adds no
        # per-drop overhead in the common (steady-state) case.
        if (
            topic not in self._drop_last_logged
            and len(self._drop_last_logged) >= _DROP_STATE_MAX_TOPICS
        ):
            self._prune_drop_state(now)

        last = self._drop_last_logged.get(topic, 0.0)
        count = self._drop_counts.get(topic, 0) + 1

        if count == 1 and last == 0.0:
            # First-ever drop for this topic: log immediately so operators
            # notice back-pressure the moment it starts.
            logger.warning("event_bus queue full — dropping events for %s (first drop)", topic)
            self._drop_counts[topic] = 0
            self._drop_last_logged[topic] = now
            return

        if now - last >= _DROP_LOG_INTERVAL_SECS:
            logger.warning(
                "event_bus queue full — dropped %d events for %s in the last %.1fs",
                count,
                topic,
                now - last,
            )
            self._drop_counts[topic] = 0
            self._drop_last_logged[topic] = now
        else:
            self._drop_counts[topic] = count

    def _dispatch(self, topic: str, data: dict) -> None:
        """Route event to matching subscriber queues.

        Runs on the asyncio loop thread (via ``call_soon_threadsafe``), so
        drop-count bookkeeping in ``_record_drop`` is single-threaded and
        does not need its own lock.
        """
        event = {"topic": topic, "data": data}
        with self._lock:
            # O(1) exact match lookup
            for q in self._exact.get(topic, []):
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    self._record_drop(topic)

            # Wildcard pattern matching
            for compiled, queues in self._wildcard.values():
                if compiled.match(topic):
                    for q in queues:
                        try:
                            q.put_nowait(event)
                        except asyncio.QueueFull:
                            self._record_drop(topic)


bus = EventBus()

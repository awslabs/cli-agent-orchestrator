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

# Bound on undelivered loss markers (per queue, per topic).
_OWED_LOSS_MAX = 2048
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
        # Loss ranges owed to specific subscriber queues that refused a payload.
        # {id(queue): (queue, {"from_pos": int, "to_pos": int})} -- one widening
        # range per queue, delivered on the next put that queue accepts, so the
        # subscriber that actually lost bytes is the one told about it.
        self._owed_loss: Dict[int, Tuple[asyncio.Queue, dict]] = {}

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
            # Drop any loss marker owed to this queue. Two reasons, both real:
            # the owed entry holds a reference to the queue, so leaving it behind
            # pins a dead subscriber's queue forever; and the map is keyed by
            # ``id(queue)``, which CPython reuses once an object is collected, so a
            # stale entry could hand a future queue at the same address a gap it
            # never suffered.
            abandoned = [k for k in self._owed_loss if k[0] == id(queue)]
            for k in abandoned:
                _, rng = self._owed_loss.pop(k)
                logger.warning(
                    "subscriber for %s unsubscribed still owed a gap marker %s; "
                    "that loss is now unreported",
                    k[1],
                    rng,
                )
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

    def flush_owed(self, topic: str) -> int:
        """Try to hand over every loss marker still owed for ``topic``.

        "Delivered on the next put that queue accepts" is not a guarantee at the
        END of a stream: if the dropped chunk is the last output a terminal ever
        produces, no further dispatch for that topic occurs and the marker would sit
        owed forever while the watermark had already advanced past the bytes
        (Copilot review on #802). Callers that know a stream has finished — the
        status transition on the runtime channel — call this so the loss is reported
        while there is still a consumer to tell.

        Returns how many markers are still outstanding afterwards.
        """
        with self._lock:
            pending = [(q, k) for k, (q, _) in self._owed_loss.items() if k[1] == topic]
            for q, _ in pending:
                self._flush_owed_loss(q, topic)
            return sum(1 for k in self._owed_loss if k[1] == topic)

    def deliver_with_loss_markers(self, topic: str, data: dict, *, lost: dict) -> int:
        """Deliver ``data``, and owe a loss marker to every queue that refuses it.

        The per-subscriber half of loss reporting. Two earlier attempts failed for
        the same structural reason — the bus is a shared, bounded, fire-and-forget
        fanout, so anything derived from an AGGREGATE drop count is wrong for
        somebody:

        * holding the stream watermark back replays the range to EVERY subscriber
          on reconnect, duplicating output for the ones that accepted it;
        * broadcasting a gap marker tells subscribers that received the bytes that
          they lost them, and can itself be dropped by the very queue that is full
          — so the one subscriber that needed the marker is the one least likely to
          get it.

        So the marker is owed to a SPECIFIC queue and delivered to that queue only,
        on the next put that queue accepts. ``lost`` is the range to report
        (``{"from_pos": int, "to_pos": int}``). A queue that keeps refusing
        accumulates one widening range rather than a backlog of markers, so the
        memory is bounded by subscriber count, not by dropped bytes (Copilot
        reviews on #802).

        Returns the number of queues that refused this payload.
        """
        return self._dispatch(topic, data, lost=lost)

    def deliver_now(self, topic: str, data: dict) -> int:
        """Dispatch on the CALLING thread and report how many subscribers dropped it.

        For a publisher that owns a durable watermark and must not advance it past
        bytes nobody received. ``publish`` cannot answer that: it hands the event
        to the loop with ``call_soon_threadsafe`` and returns before any queue is
        touched, so a full queue became a log line and nothing else. The runtime
        channel handler advanced its resume position on that basis, which told the
        runtime those bytes had landed and made them unreplayable — a bounded,
        recoverable overflow turned into permanent loss (Copilot review on #802).

        Only safe from the loop thread, which is where the channel handler already
        runs; ``_record_drop``'s bookkeeping assumes single-threaded access.
        Returns 0 when every subscriber took the event.
        """
        return self._dispatch(topic, data)

    def _owe_loss(self, q, topic: str, lost: dict) -> None:
        """Record (or widen) the loss range this queue has not been told about.

        Keyed by (queue, TOPIC). Keying on the queue alone was wrong in the way
        that matters here: every real output consumer subscribes once to
        ``terminal.*.output`` (LogWriter, StatusMonitor, the bridge) and routes by
        ``event["topic"]``, so a marker owed for terminal A and flushed on
        terminal B's next chunk arrived stamped as B's loss — misreporting B and
        never reporting A at all (Copilot review on #802).

        A range is only widened within one ``generation``. Positions restart at 0
        when a stream is re-armed, so widening across that boundary would produce a
        nonsense ``[0, old_to)``; a new generation replaces the pending range
        instead.
        """
        key = (id(q), topic)
        owed = self._owed_loss.get(key)
        if owed is None:
            if len(self._owed_loss) >= _OWED_LOSS_MAX:
                # Bounded like the drop-state maps: per queue AND per topic, so a
                # server churning through terminals would otherwise accumulate one
                # entry per terminal forever. Dropping the oldest is reported,
                # because it means a loss nobody will now hear about.
                stale_key, (stale_q, stale_rng) = next(iter(self._owed_loss.items()))
                self._owed_loss.pop(stale_key, None)
                logger.warning(
                    "event_bus owed-loss map full; discarding an undelivered gap "
                    "marker for %s %s",
                    stale_key[1],
                    stale_rng,
                )
            self._owed_loss[key] = (q, dict(lost))
            return
        _, rng = owed
        if rng.get("generation") != lost.get("generation"):
            self._owed_loss[key] = (q, dict(lost))
            return
        rng["from_pos"] = min(rng["from_pos"], lost["from_pos"])
        rng["to_pos"] = max(rng["to_pos"], lost["to_pos"])

    def _flush_owed_loss(self, q, topic: str) -> None:
        """Hand a queue its outstanding loss marker for ``topic``, if it can take one.

        Only the entry recorded for this exact topic is eligible, so a marker is
        always delivered under the terminal it belongs to.
        """
        key = (id(q), topic)
        owed = self._owed_loss.get(key)
        if owed is None:
            return
        _, rng = owed
        try:
            q.put_nowait({"topic": topic, "data": {"data": "", "gap": dict(rng)}})
        except asyncio.QueueFull:
            # Still full. The range stays owed and widens with any further loss;
            # it is delivered whenever this queue drains.
            return
        self._owed_loss.pop(key, None)

    def _dispatch(self, topic: str, data: dict, lost: Optional[dict] = None) -> int:
        """Route event to matching subscriber queues; return the number of drops.

        Runs on the asyncio loop thread (via ``call_soon_threadsafe``), so
        drop-count bookkeeping in ``_record_drop`` is single-threaded and
        does not need its own lock.

        The return value is ignored by ``publish`` (a fire-and-forget callback has
        nowhere to put it) and used by ``deliver_now``.
        """
        event = {"topic": topic, "data": data}
        dropped = 0
        with self._lock:
            # O(1) exact match lookup
            for q in self._exact.get(topic, []):
                self._flush_owed_loss(q, topic)
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    dropped += 1
                    self._record_drop(topic)
                    if lost is not None:
                        self._owe_loss(q, topic, lost)

            # Wildcard pattern matching
            for compiled, queues in self._wildcard.values():
                if compiled.match(topic):
                    for q in queues:
                        self._flush_owed_loss(q, topic)
                        try:
                            q.put_nowait(event)
                        except asyncio.QueueFull:
                            dropped += 1
                            self._record_drop(topic)
                            if lost is not None:
                                self._owe_loss(q, topic, lost)
        return dropped


bus = EventBus()

"""In-process pub/sub event bus with wildcard topic matching.

Event Topics:
- terminal.{id}.output  → raw output chunks (from FIFO readers)
- terminal.{id}.status  → status changes (from StatusMonitor)
"""

import asyncio
import logging
import re
import threading
from typing import Dict, List, Optional, Tuple

from cli_agent_orchestrator.services.settings_service import get_server_settings

logger = logging.getLogger(__name__)


class EventBus:
    """Thread-safe publishing, async consumption via asyncio.Queue."""

    def __init__(self):
        self._exact: Dict[str, List[asyncio.Queue]] = {}
        self._wildcard: Dict[str, Tuple[re.Pattern, List[asyncio.Queue]]] = {}
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

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

    def _dispatch(self, topic: str, data: dict) -> None:
        """Route event to matching subscriber queues."""
        event = {"topic": topic, "data": data}
        with self._lock:
            # O(1) exact match lookup
            for q in self._exact.get(topic, []):
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    logger.error(f"Queue full, dropping event: {topic}")

            # Wildcard pattern matching
            for compiled, queues in self._wildcard.values():
                if compiled.match(topic):
                    for q in queues:
                        try:
                            q.put_nowait(event)
                        except asyncio.QueueFull:
                            logger.error(f"Queue full, dropping event: {topic}")


bus = EventBus()

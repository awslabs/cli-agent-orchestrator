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

logger = logging.getLogger(__name__)


class EventBus:
    """Thread-safe publishing, async consumption via asyncio.Queue."""

    def __init__(self):
        self._subscriptions: Dict[str, Tuple[re.Pattern, List[asyncio.Queue]]] = {}
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Register the asyncio event loop (required for thread-safe publishing)."""
        self._loop = loop

    def publish(self, topic: str, data: dict) -> None:
        """Publish event to all matching subscribers. Safe to call from any thread."""
        if self._loop:
            self._loop.call_soon_threadsafe(self._dispatch, topic, data)

    def subscribe(self, pattern: str) -> asyncio.Queue:
        """Subscribe to a topic pattern (e.g., 'terminal.*.output'). Returns async queue."""
        regex = pattern.replace(".", r"\.").replace("*", "[^.]+")
        queue: asyncio.Queue = asyncio.Queue()

        with self._lock:
            if regex not in self._subscriptions:
                self._subscriptions[regex] = (re.compile(f"^{regex}$"), [])
            self._subscriptions[regex][1].append(queue)

        return queue

    def _dispatch(self, topic: str, data: dict) -> None:
        """Route event to matching subscriber queues."""
        event = {"topic": topic, "data": data}
        with self._lock:
            for compiled, queues in self._subscriptions.values():
                if compiled.match(topic):
                    for q in queues:
                        try:
                            q.put_nowait(event)
                        except asyncio.QueueFull:
                            logger.error(f"Queue full, dropping event: {topic}")


bus = EventBus()

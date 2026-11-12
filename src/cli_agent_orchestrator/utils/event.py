"""Event bus utilities."""

import asyncio
from typing import Callable, TypeVar

T = TypeVar("T")


def terminal_id_from_topic(topic: str) -> str:
    """Extract terminal ID from event topic (e.g., 'terminal.abc123.output' → 'abc123')."""
    return topic.split(".")[1]


async def snapshot_off_loop(snapshot_fn: Callable[[], T]) -> T:
    """Take a fleet snapshot in a worker thread and return it (#745).

    A snapshot lists sessions through the terminal backend; under the remote
    backend that is a round trip to each runner, which must not hold the event
    loop. The name keeps ``snapshot`` in the call, so
    ``test/test_subscribe_before_snapshot.py`` still sees where a stream takes
    its snapshot relative to its subscription.
    """
    return await asyncio.to_thread(snapshot_fn)

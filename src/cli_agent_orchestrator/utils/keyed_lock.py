"""Per-key locks whose map holds only the keys in use (#745).

A long-lived ``cao-server`` or ``cao-runner`` serialises work per terminal
window. A plain ``dict.setdefault(key, Lock())`` keeps one entry for every
window it ever saw, so the map grows with every terminal created. Evicting an
entry when its window is torn down is not safe either: a caller still waiting
on the old lock and a new caller given a fresh one would both get in.

Here each entry counts its holders and waiters, and is dropped when the last
one leaves. A key with nobody using it has no entry, and every caller using a
key at the same time shares one lock.
"""

import asyncio
import threading
from contextlib import asynccontextmanager, contextmanager
from typing import AsyncIterator, Dict, Hashable, Iterator, List, Tuple


class KeyedLock:
    """A ``threading.Lock`` per key, kept only while it is held or awaited."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._entries: Dict[Hashable, Tuple[threading.Lock, List[int]]] = {}

    @contextmanager
    def hold(self, key: Hashable) -> Iterator[None]:
        with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                entry = self._entries[key] = (threading.Lock(), [0])
            entry[1][0] += 1
        try:
            with entry[0]:
                yield
        finally:
            with self._guard:
                entry[1][0] -= 1
                if entry[1][0] == 0:
                    del self._entries[key]

    def __len__(self) -> int:
        with self._guard:
            return len(self._entries)


class AsyncKeyedLock:
    """An ``asyncio.Lock`` per key, kept only while it is held or awaited.

    Used from one event loop only, so the map needs no guard of its own.
    """

    def __init__(self) -> None:
        self._entries: Dict[Hashable, Tuple[asyncio.Lock, List[int]]] = {}

    @asynccontextmanager
    async def hold(self, key: Hashable) -> AsyncIterator[None]:
        entry = self._entries.get(key)
        if entry is None:
            entry = self._entries[key] = (asyncio.Lock(), [0])
        entry[1][0] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1][0] -= 1
            if entry[1][0] == 0:
                del self._entries[key]

    def __len__(self) -> int:
        return len(self._entries)

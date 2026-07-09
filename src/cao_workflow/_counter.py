"""Lock-guarded call-order counter (E2, BR-3).

Process-local, monotonically increasing from 1. Never reset, never
persisted, never visible outside the process — discarded with the script
process at exit.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_counter = 0


def _next_call_key() -> str:
    """Return the next ``call-{n}`` key, race-free under concurrent callers.

    The lock only guarantees no two calls get the same key — it does NOT make
    unmarked concurrent fan-out deterministic across runs (BR-13); an explicit
    ``step_id`` is required for that.
    """
    global _counter
    with _lock:
        _counter += 1
        n = _counter
    return f"call-{n}"

"""GraphView cache keyed by provider-defined immutable tuples.

Issue #348, perf follow-up.

DELIBERATE ADR REVERSAL. The original graph-layer design record specified
"lint-on-demand, no caching machinery" (ADR-7): every ``/graph/{provider}``
request re-ran ``wiki_lint.run_lint`` in-request. Profiling the shipped
``memory`` provider on ``scope=global`` measured that projection at ~30s
typical and up to ~148s under load — worse than the frontend's 120s timeout,
so the UI aborted before the server answered. The dominant cost is NOT the LLM
contradiction detector (only ~8.5s / 3 pairs / 0 findings on global) but the
ripgrep-based ``stale_claim`` detector (~20s: ~95 ``rg`` subprocess spawns over
the whole repo). Caching the *projected* GraphView sidesteps the entire run_lint
cost on repeat views regardless of which detector dominates.

This module lives in the graph layer ONLY — it does not touch the shipped
``wiki_lint`` / ``memory_service`` modules (their pure-read / no-cache contracts
are preserved). A ``memory`` GraphProvider opts in by wrapping its build in
``get_or_build``.

Staleness tradeoff (chosen: SHORT TTL, not write-invalidation): wiring
invalidation into the memory write path (``memory_service.store`` / ``forget`` /
``consolidate``) would mean editing a shipped module and reaching across the
graph-layer boundary into it — invasive, and it couples the graph cache to the
memory service's internals. Instead we use a short TTL (``DEFAULT_TTL_S``, 5
min): the graph can be up to TTL seconds stale after a memory edit, which for a
human-viewed knowledge graph is an acceptable price for a self-contained,
boundary-respecting cache. ``invalidate`` is still exposed so a future write-path
hook can wire proactive invalidation without changing this module's shape.

Fingerprint-bearing providers can produce a new key after each configuration
change. Expired entries are therefore swept on every lookup, and per-key locks
are reference-counted and reclaimed once no caller or live entry needs them.
The retained key set is bounded by one TTL window plus in-flight builds.
"""

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from cli_agent_orchestrator.graph.models import GraphView

# 5 minutes. First request within a window pays the full projection cost;
# repeats return the cached GraphView instantly. Also the maximum staleness a
# viewer can observe after a memory write, given we chose TTL over
# write-invalidation (see module docstring).
DEFAULT_TTL_S = 300.0

# Base cache key: (provider name, scope, scope_id, lint_enabled). scope_id is
# normalized to a string-or-None so ``("memory","global",None,True)`` and a
# project projection never collide, a global request never serves a
# project-scope entry, and lint-enabled/disabled graph projections stay isolated.
# The memory provider appends its current binding fingerprint. The four-element
# form remains supported for every other existing consumer.
CacheKey = tuple[str, str, Optional[str], bool] | tuple[str, str, Optional[str], bool, str]


@dataclass
class _Entry:
    view: GraphView
    created_monotonic: float
    as_of: str  # ISO-8601 UTC wall-clock of the build, surfaced as meta.as_of


class GraphViewCache:
    """Async-safe TTL cache with single-flight (no thundering herd).

    The FastAPI app is async on a single event loop, so an ``asyncio.Lock``
    per key is the right guard. Single-flight matters here specifically because
    the work being cached can take ~148s: without it, N concurrent cold
    requests for the same key would each launch the full projection. With it,
    the first request builds while the rest await the same result.
    """

    def __init__(
        self,
        ttl_s: float = DEFAULT_TTL_S,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_s
        self._clock = clock
        self._entries: dict[CacheKey, _Entry] = {}
        self._locks: dict[CacheKey, asyncio.Lock] = {}
        self._lock_users: dict[CacheKey, int] = {}
        # Guards mutation of the ``_locks`` map itself so two coroutines racing
        # to create/reference the per-key lock can't each use a different one.
        self._locks_guard = asyncio.Lock()

    async def _lock_for(self, key: CacheKey) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            self._lock_users[key] = self._lock_users.get(key, 0) + 1
            return lock

    def _release_lock_user(self, key: CacheKey, lock: asyncio.Lock) -> None:
        """Release one caller's lock reference without yielding the event loop."""
        users = self._lock_users.get(key)
        if users is None:
            return
        if users > 1:
            self._lock_users[key] = users - 1
            return
        self._lock_users.pop(key, None)
        if key not in self._entries and self._locks.get(key) is lock:
            self._locks.pop(key, None)

    def _prune_unused_lock(self, key: CacheKey) -> None:
        if self._lock_users.get(key, 0) == 0 and key not in self._entries:
            self._locks.pop(key, None)
            self._lock_users.pop(key, None)

    def _sweep(self) -> None:
        """Reclaim expired entries and every unreferenced orphan lock."""
        now = self._clock()
        expired = [
            key
            for key, entry in self._entries.items()
            if now - entry.created_monotonic >= self._ttl
        ]
        for key in expired:
            self._entries.pop(key, None)
        for key in tuple(self._locks):
            self._prune_unused_lock(key)
        for key, users in tuple(self._lock_users.items()):
            if users == 0 and key not in self._locks:
                self._lock_users.pop(key, None)

    def _fresh(self, key: CacheKey) -> Optional[_Entry]:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if self._clock() - entry.created_monotonic >= self._ttl:
            # Evict the expired entry and its lock only when no caller holds a
            # reference. Referenced locks survive until the final caller exits.
            del self._entries[key]
            self._prune_unused_lock(key)
            return None
        return entry

    async def get_or_build(
        self, key: CacheKey, builder: Callable[[], Awaitable[GraphView]]
    ) -> tuple[GraphView, bool, str]:
        """Return ``(view, cached, as_of)`` for ``key``.

        ``cached`` is True when a fresh entry was served without calling
        ``builder``. ``builder`` is invoked at most once per (key, window) even
        under concurrent callers.
        """
        self._sweep()
        entry = self._fresh(key)
        if entry is not None:
            return entry.view, True, entry.as_of

        lock: Optional[asyncio.Lock] = None
        try:
            lock = await self._lock_for(key)
            async with lock:
                # Re-check under the lock: a concurrent caller may have built it
                # while we waited (single-flight — this is where the herd collapses
                # onto one build).
                entry = self._fresh(key)
                if entry is not None:
                    return entry.view, True, entry.as_of

                view = await builder()
                as_of = datetime.now(timezone.utc).isoformat()
                self._entries[key] = _Entry(
                    view=view,
                    created_monotonic=self._clock(),
                    as_of=as_of,
                )
                return view, False, as_of
        finally:
            if lock is not None:
                self._release_lock_user(key, lock)

    def invalidate(self, key: CacheKey) -> None:
        """Drop a single key's entry (no-op if absent).

        Exposed for a future write-path hook; unused today (we chose short-TTL
        over write-invalidation — see module docstring).
        """
        self._entries.pop(key, None)
        self._prune_unused_lock(key)

    def clear(self) -> None:
        """Drop cached entries and unreferenced locks.

        In-flight callers retain their shared lock and may repopulate the cache,
        preserving the established clear-during-build serialization behavior.
        """
        self._entries.clear()
        for key in tuple(self._locks):
            self._prune_unused_lock(key)
        for key, users in tuple(self._lock_users.items()):
            if users == 0:
                self._lock_users.pop(key, None)


def make_meta(base: dict[str, Any], *, cached: bool, as_of: str) -> dict[str, Any]:
    """Return a copy of ``base`` meta annotated with cache provenance.

    Never mutates ``base`` (the cached GraphView's own meta must stay
    untouched, since the same instance is served to every hit).
    """
    return {**base, "cached": cached, "as_of": as_of}

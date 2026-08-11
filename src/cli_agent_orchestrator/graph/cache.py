"""Per-(provider, scope, scope_id, lint_enabled) GraphView cache.

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
``wiki_lint`` / ``memory_service`` modules. A ``memory`` GraphProvider opts in
by wrapping its build in ``get_or_build``.

Staleness tradeoff (chosen: SHORT TTL, not write-invalidation): wiring
invalidation into the memory write path (``memory_service.store`` / ``forget`` /
``consolidate``) would mean editing a shipped module and reaching across the
graph-layer boundary into it — invasive, and it couples the graph cache to the
memory service's internals. Instead we use a short TTL (``DEFAULT_TTL_S``, 5
min): the graph can be up to TTL seconds stale after a memory edit, which for a
human-viewed knowledge graph is an acceptable price for a self-contained,
boundary-respecting cache. ``invalidate`` is still exposed so a future write-path
hook can wire proactive invalidation without changing this module's shape.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from typing import Any, Awaitable, Callable, Literal, Optional

from cli_agent_orchestrator.graph.models import GraphView

logger = logging.getLogger(__name__)

# 5 minutes. First request within a window pays the full projection cost;
# repeats return the cached GraphView instantly. Also the maximum staleness a
# viewer can observe after a memory write, given we chose TTL over
# write-invalidation (see module docstring).
DEFAULT_TTL_S = 300.0

# Bounds how long a detached build may hold its per-key lock. Cancellation
# cannot stop work already running in ``asyncio.to_thread``; it only releases
# the asyncio lock and makes the key recoverable.
GRAPH_BUILD_MAX_S = 600.0

# Detached graph builds use the process-wide default thread pool indirectly.
# Keep their concurrency bounded so caller-supplied scope_ids cannot starve
# unrelated executor users.
GRAPH_BUILD_CONCURRENCY = 2

# At most two additional keys may wait behind the running builds. Once this
# queue is full, callers receive rejected_queue_full status without allocating
# a detached task, so caller-controlled scope_ids cannot grow the task registry
# without bound.
GRAPH_BUILD_QUEUE_MAX = 2

# Cache key: (provider name, scope, scope_id, lint_enabled). scope_id is
# normalized to a string-or-None so ``("memory","global",None,True)`` and a
# project projection never collide, a global request never serves a
# project-scope entry, and lint-enabled/disabled graph projections stay isolated.
CacheKey = tuple[str, str, Optional[str], bool]


@dataclass
class _Entry:
    view: GraphView
    created_monotonic: float
    as_of: str  # ISO-8601 UTC wall-clock of the build, surfaced as meta.as_of


BuildState = Literal[
    "in_progress",
    "started",
    "queued",
    "rejected_queue_full",
    "failed_deadline",
]


@dataclass
class BuildStatus:
    """Observable state for one detached projection build."""

    state: BuildState
    started_monotonic: float
    started_at: str


class GraphBuildDeadlineError(asyncio.TimeoutError):
    """A graph build exceeded ``GRAPH_BUILD_MAX_S`` and released its key lock."""


class GraphBuildQueueFullError(Exception):
    """No detached task was created because the bounded build queue is full."""

    def __init__(self, build_status: dict[str, Any]) -> None:
        super().__init__("graph build queue is full")
        self.build_status = build_status


class GraphViewCache:
    """Async-safe TTL cache whose tasks own single-flight projection work.

    Each cold key has exactly one strongly-referenced task. Request deadlines
    may stop waiting for that task without cancelling it; retries join it. A
    hard build deadline bounds how long the per-key lock can remain held, but
    cannot stop blocking worker threads already launched by ``to_thread``.
    """

    def __init__(
        self,
        ttl_s: float = DEFAULT_TTL_S,
        *,
        clock: Callable[[], float] = time.monotonic,
        build_max_s: float = GRAPH_BUILD_MAX_S,
        max_concurrent_builds: int = GRAPH_BUILD_CONCURRENCY,
        max_pending_builds: int = GRAPH_BUILD_QUEUE_MAX,
    ) -> None:
        if max_concurrent_builds < 1:
            raise ValueError("max_concurrent_builds must be at least 1")
        if max_pending_builds < 0:
            raise ValueError("max_pending_builds must not be negative")
        self._ttl = ttl_s
        self._clock = clock
        self._build_max_s = build_max_s
        self._entries: dict[CacheKey, _Entry] = {}
        self._locks: dict[CacheKey, asyncio.Lock] = {}
        self._inflight: dict[CacheKey, asyncio.Task[GraphView]] = {}
        self._statuses: dict[CacheKey, BuildStatus] = {}
        self._failed_deadlines: dict[CacheKey, BuildStatus] = {}
        self._build_slots = asyncio.Semaphore(max_concurrent_builds)
        self._active_builds = 0
        self._max_concurrent_builds = max_concurrent_builds
        self._max_pending_builds = max_pending_builds
        # Guards mutation of the ``_locks`` map itself so two coroutines racing
        # to create the per-key lock can't each make a different one.
        self._locks_guard = asyncio.Lock()

    async def _lock_for(self, key: CacheKey) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    def _fresh(self, key: CacheKey) -> Optional[_Entry]:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if self._clock() - entry.created_monotonic >= self._ttl:
            # Evict the expired entry so ``_entries`` doesn't retain a stale
            # GraphView for every key ever queried. ``_locks`` is intentionally
            # NOT pruned here: it holds one tiny ``asyncio.Lock`` per key
            # (bounded by the number of distinct keys, a small finite set), and
            # a concurrent coroutine may be awaiting that very lock — dropping
            # it mid-flight would let a second builder run for the same key.
            del self._entries[key]
            return None
        return entry

    def get_or_build_task(
        self, key: CacheKey, builder: Callable[[], Awaitable[GraphView]]
    ) -> asyncio.Future[GraphView]:
        """Return the cache-owned future for ``key``, starting it if needed.

        Calling this method is atomic on the event-loop thread: there is no
        await between checking and inserting ``_inflight``. Consequently every
        concurrent or retrying caller receives the same task for a cold key.
        Fresh hits use an already-resolved Future and do not enter the detached
        task registry.
        """
        entry = self._fresh(key)
        if entry is not None:
            future = asyncio.get_running_loop().create_future()
            future.set_result(self._with_provenance(entry.view, cached=True, as_of=entry.as_of))
            return future

        task = self._inflight.get(key)
        if task is not None:
            return task

        started_at = datetime.now(timezone.utc).isoformat()
        started_monotonic = self._clock()
        if len(self._inflight) >= self._max_concurrent_builds + self._max_pending_builds:
            raise GraphBuildQueueFullError({"build_state": "rejected_queue_full"})

        initial_state: BuildState = (
            "started" if len(self._inflight) < self._max_concurrent_builds else "queued"
        )
        self._statuses[key] = BuildStatus(
            state=initial_state,
            started_monotonic=started_monotonic,
            started_at=started_at,
        )
        self._failed_deadlines.pop(key, None)
        task = asyncio.create_task(self._run_build(key, builder), name=f"graph-build:{key!r}")
        self._inflight[key] = task
        task.add_done_callback(partial(self._build_done, key))
        return task

    async def get_or_build(
        self, key: CacheKey, builder: Callable[[], Awaitable[GraphView]]
    ) -> GraphView:
        """Await the cache-owned task for callers outside the API route."""
        return await self.get_or_build_task(key, builder)

    async def _run_build(
        self, key: CacheKey, builder: Callable[[], Awaitable[GraphView]]
    ) -> GraphView:
        lock = await self._lock_for(key)
        async with lock:
            entry = self._fresh(key)
            if entry is not None:
                return self._with_provenance(entry.view, cached=True, as_of=entry.as_of)

            async with self._build_slots:
                self._active_builds += 1
                status = self._statuses[key]
                status.state = "in_progress"
                try:
                    view = await asyncio.wait_for(builder(), timeout=self._build_max_s)
                except asyncio.TimeoutError as exc:
                    status.state = "failed_deadline"
                    self._failed_deadlines[key] = status
                    raise GraphBuildDeadlineError(
                        f"graph build exceeded {self._build_max_s:g} seconds"
                    ) from exc
                finally:
                    self._active_builds -= 1

            as_of = datetime.now(timezone.utc).isoformat()
            self._entries[key] = _Entry(
                view=view,
                created_monotonic=self._clock(),
                as_of=as_of,
            )
            return self._with_provenance(view, cached=False, as_of=as_of)

    def _build_done(self, key: CacheKey, task: asyncio.Task[GraphView]) -> None:
        """Retrieve failures and release the cache's strong task reference."""
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            exc = None
        if exc is not None:
            logger.error(
                "detached graph build failed for %r: %r",
                key,
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        if self._inflight.get(key) is task:
            self._inflight.pop(key, None)
            self._statuses.pop(key, None)

    def build_status(self, key: CacheKey) -> Optional[dict[str, Any]]:
        """Return additive timeout metadata for an active or deadline-failed key."""
        status = self._statuses.get(key) or self._failed_deadlines.get(key)
        if status is None:
            return None
        return self._status_dict(status)

    def _status_dict(self, status: BuildStatus) -> dict[str, Any]:
        return {
            "build_state": status.state,
            "build_elapsed_s": max(0.0, self._clock() - status.started_monotonic),
            "build_started_at": status.started_at,
        }

    def inflight_task(self, key: CacheKey) -> Optional[asyncio.Task[GraphView]]:
        """Return the strongly-referenced task for shutdown tracking."""
        return self._inflight.get(key)

    @staticmethod
    def _with_provenance(view: GraphView, *, cached: bool, as_of: str) -> GraphView:
        return GraphView(
            nodes=view.nodes,
            edges=view.edges,
            meta=make_meta(view.meta, cached=cached, as_of=as_of),
        )

    def invalidate(self, key: CacheKey) -> None:
        """Drop a single key's entry (no-op if absent).

        Exposed for a future write-path hook; unused today (we chose short-TTL
        over write-invalidation — see module docstring).
        """
        self._entries.pop(key, None)

    def clear(self) -> None:
        """Drop entries and cancel detached work (used by tests/global flush)."""
        tasks = list(self._inflight.values())
        self._inflight.clear()
        self._statuses.clear()
        self._failed_deadlines.clear()
        for task in tasks:
            task.cancel()
        self._entries.clear()


def make_meta(base: dict[str, Any], *, cached: bool, as_of: str) -> dict[str, Any]:
    """Return a copy of ``base`` meta annotated with cache provenance.

    Never mutates ``base`` (the cached GraphView's own meta must stay
    untouched, since the same instance is served to every hit).
    """
    return {**base, "cached": cached, "as_of": as_of}

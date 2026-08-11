"""Tests for the graph-layer GraphView cache (Issue #348 perf follow-up).

Covers the cache contract directly (TTL freshness, single-flight, per-key
isolation, invalidate) and its integration through MemoryGraphProvider
(a 2nd project() call within TTL does NOT re-run run_lint).
"""

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base, MemoryMetadataModel
from cli_agent_orchestrator.graph.cache import (
    DEFAULT_TTL_S,
    GRAPH_CACHE_MAX_ENTRIES,
    GraphBuildDeadlineError,
    GraphBuildQueueFullError,
    GraphViewCache,
    make_meta,
)
from cli_agent_orchestrator.graph.models import GraphView, Node
from cli_agent_orchestrator.graph.providers import memory as memory_provider
from cli_agent_orchestrator.graph.providers.memory import MemoryGraphProvider
from cli_agent_orchestrator.services import settings_service, wiki_lint
from cli_agent_orchestrator.services.memory_service import MemoryService

BODY = "A reasonably long article body so contradiction pairing engages." + " filler" * 10


# ---------------------------------------------------------------------------
# Unit tests: GraphViewCache in isolation (deterministic fake clock)
# ---------------------------------------------------------------------------


class _FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _view(node_id: str) -> GraphView:
    return GraphView(nodes=[Node(id=node_id, kind="topic", label=node_id)], edges=[], meta={})


class TestGraphViewCache:
    @pytest.mark.asyncio
    async def test_second_call_within_ttl_is_cached_and_skips_builder(self):
        cache = GraphViewCache(ttl_s=300.0)
        calls = {"n": 0}

        async def builder():
            calls["n"] += 1
            return _view("a")

        key = ("memory", "global", None, True)
        view1 = await cache.get_or_build(key, builder)
        view2 = await cache.get_or_build(key, builder)

        assert calls["n"] == 1  # builder ran ONCE across two calls
        assert view1.meta["cached"] is False
        assert view2.meta["cached"] is True

    @pytest.mark.asyncio
    async def test_ttl_expiry_reruns_builder(self):
        clock = _FakeClock()
        cache = GraphViewCache(ttl_s=300.0, clock=clock)
        calls = {"n": 0}

        async def builder():
            calls["n"] += 1
            return _view("a")

        key = ("memory", "global", None, True)
        view1 = await cache.get_or_build(key, builder)
        clock.advance(300.1)  # past TTL
        view2 = await cache.get_or_build(key, builder)

        assert calls["n"] == 2
        assert view1.meta["cached"] is False and view2.meta["cached"] is False

    @pytest.mark.asyncio
    async def test_per_key_isolation(self):
        """A project-scope entry must not serve a global request."""
        cache = GraphViewCache(ttl_s=300.0)

        async def build_global():
            return _view("g")

        async def build_project():
            return _view("p")

        vg = await cache.get_or_build(("memory", "global", None, True), build_global)
        vp = await cache.get_or_build(("memory", "project", "proj1", True), build_project)

        assert vp.meta["cached"] is False  # different key ⇒ built fresh, not a global hit
        assert {n.id for n in vg.nodes} == {"g"}
        assert {n.id for n in vp.nodes} == {"p"}

    @pytest.mark.asyncio
    async def test_single_flight_collapses_concurrent_cold_requests(self):
        """N concurrent cold requests for one key run the builder ONCE."""
        cache = GraphViewCache(ttl_s=300.0)
        calls = {"n": 0}
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_builder():
            calls["n"] += 1
            started.set()
            await release.wait()  # hold all concurrent callers on the lock
            return _view("a")

        key = ("memory", "global", None, True)
        tasks = [asyncio.create_task(cache.get_or_build(key, slow_builder)) for _ in range(5)]
        await started.wait()
        release.set()
        results = await asyncio.gather(*tasks)

        assert calls["n"] == 1
        assert all(view.meta["cached"] is False for view in results)

    @pytest.mark.asyncio
    async def test_invalidate_forces_rebuild(self):
        cache = GraphViewCache(ttl_s=300.0)
        calls = {"n": 0}

        async def builder():
            calls["n"] += 1
            return _view("a")

        key = ("memory", "global", None, True)
        await cache.get_or_build(key, builder)
        cache.invalidate(key)
        view = await cache.get_or_build(key, builder)

        assert calls["n"] == 2 and view.meta["cached"] is False

    @pytest.mark.asyncio
    async def test_expired_entry_is_evicted_not_just_missed(self):
        """An expired entry is REMOVED from ``_entries`` on access, so the map
        does not retain a stale GraphView for every key ever queried.
        """
        clock = _FakeClock()
        cache = GraphViewCache(ttl_s=300.0, clock=clock)

        async def builder():
            return _view("a")

        key = ("memory", "global", None, True)
        await cache.get_or_build(key, builder)
        assert key in cache._entries  # cached while fresh

        clock.advance(300.1)  # past TTL
        assert cache._fresh(key) is None  # treated as a miss...
        assert key not in cache._entries  # ...AND evicted, not merely skipped

    @pytest.mark.asyncio
    async def test_distinct_keys_do_not_grow_entries_beyond_lru_bound(self):
        """Request-controlled distinct keys must not create permanent rows."""
        cache = GraphViewCache(ttl_s=300.0)

        async def builder():
            return _view("a")

        keys = [("memory", "project", f"k{i}", True) for i in range(80)]
        for key in keys:
            await cache.get_or_build(key, builder)

        assert len(cache._entries) <= GRAPH_CACHE_MAX_ENTRIES

    @pytest.mark.asyncio
    async def test_entry_cap_uses_lru_order(self):
        cache = GraphViewCache(max_entries=2)

        async def builder():
            return _view("a")

        key1 = ("memory", "project", "one", True)
        key2 = ("memory", "project", "two", True)
        key3 = ("memory", "project", "three", True)
        await cache.get_or_build(key1, builder)
        await cache.get_or_build(key2, builder)
        await cache.get_or_build(key1, builder)  # refresh key1's recency
        await cache.get_or_build(key3, builder)

        assert list(cache._entries) == [key1, key3]

    @pytest.mark.asyncio
    async def test_entry_cap_rechecks_after_inflight_keys_complete(self):
        cache = GraphViewCache(max_concurrent_builds=2, max_entries=1)
        release = asyncio.Event()

        async def builder():
            await release.wait()
            return _view("a")

        tasks = [
            cache.get_or_build_task(("memory", "project", f"k{i}", True), builder) for i in range(2)
        ]
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(*tasks)
        await asyncio.sleep(0)

        assert len(cache._entries) == 1

    @pytest.mark.asyncio
    async def test_failed_deadline_diagnostics_are_bounded(self):
        cache = GraphViewCache(build_max_s=0, max_entries=3)

        async def builder():
            await asyncio.sleep(0)
            return _view("never")

        for i in range(5):
            key = ("memory", "project", f"deadline-{i}", True)
            with pytest.raises(GraphBuildDeadlineError):
                await cache.get_or_build(key, builder)

        assert len(cache._failed_deadlines) == 3

    def test_make_meta_does_not_mutate_base(self):
        base = {"provider": "memory", "scope": "global"}
        out = make_meta(base, cached=True, as_of="2026-07-14T00:00:00+00:00")
        assert out["cached"] is True and out["as_of"] == "2026-07-14T00:00:00+00:00"
        assert "cached" not in base  # original untouched

    def test_default_ttl_is_five_minutes(self):
        assert DEFAULT_TTL_S == 300.0

    @pytest.mark.asyncio
    async def test_request_timeout_does_not_cancel_build_and_retry_converges(self):
        from cli_agent_orchestrator.api.main import _project_graph_with_timeout

        cache = GraphViewCache()
        key = ("memory", "global", None, True)
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def builder():
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return _view("completed")

        class Provider:
            def project_inflight(self, **filters):
                return cache.get_or_build_task(key, builder)

            def projection_status(self, **filters):
                return cache.build_status(key)

        first = asyncio.create_task(
            _project_graph_with_timeout(Provider(), {}, provider="memory", timeout_s=0.01)
        )
        await started.wait()
        with pytest.raises(HTTPException) as exc_info:
            await first
        assert exc_info.value.status_code == 504
        assert exc_info.value.detail["kind"] == "graph_projection_timeout"
        assert key in cache._inflight

        retry = asyncio.create_task(
            _project_graph_with_timeout(Provider(), {}, provider="memory", timeout_s=0.2)
        )
        await asyncio.sleep(0)
        release.set()
        view = await retry

        assert calls == 1
        assert {node.id for node in view.nodes} == {"completed"}
        assert key in cache._entries

    @pytest.mark.asyncio
    async def test_fixed_retry_cadence_joins_completed_build_before_ttl(self):
        """The ordinary timeout hint and header agree, and a retry at that
        fixed cadence hits the completed single-flight build before its TTL.
        """
        from cli_agent_orchestrator.api import main as api_main

        clock = _FakeClock()
        cache = GraphViewCache(ttl_s=DEFAULT_TTL_S, clock=clock)
        key = ("memory", "global", None, True)
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def builder():
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return _view("completed")

        class Provider:
            def project_inflight(self, **filters):
                return cache.get_or_build_task(key, builder)

            def projection_status(self, **filters):
                return cache.build_status(key)

        build = cache.get_or_build_task(key, builder)
        await started.wait()
        with pytest.raises(HTTPException) as exc_info:
            await api_main._project_graph_with_timeout(
                Provider(),
                {},
                provider="memory",
                timeout_s=0,
            )

        retry_after_s = exc_info.value.detail["retry_after_s"]
        assert retry_after_s == int(exc_info.value.headers["Retry-After"])
        assert retry_after_s < DEFAULT_TTL_S

        release.set()
        await build
        clock.advance(retry_after_s)
        view = await api_main._project_graph_with_timeout(
            Provider(),
            {},
            provider="memory",
            timeout_s=0.1,
        )

        assert calls == 1
        assert view.meta["cached"] is True

    @pytest.mark.asyncio
    async def test_build_deadline_makes_key_recoverable_without_per_key_locks(self):
        cache = GraphViewCache(build_max_s=0.01)
        key = ("memory", "global", None, True)

        async def hangs():
            await asyncio.Event().wait()
            return _view("never")

        with pytest.raises(GraphBuildDeadlineError):
            await cache.get_or_build(key, hangs)
        assert cache.build_status(key)["build_state"] == "failed_deadline"
        assert not hasattr(cache, "_locks")
        assert not hasattr(cache, "_locks_guard")
        assert not hasattr(cache, "_lock_for")

        recovered = await cache.get_or_build(key, lambda: asyncio.sleep(0, result=_view("ok")))
        assert {node.id for node in recovered.nodes} == {"ok"}

    @pytest.mark.asyncio
    async def test_global_cap_marks_additional_key_queued(self):
        cache = GraphViewCache(max_concurrent_builds=1)
        release = asyncio.Event()
        key1 = ("memory", "project", "one", True)
        key2 = ("memory", "project", "two", True)

        async def blocked():
            await release.wait()
            return _view("done")

        task1 = cache.get_or_build_task(key1, blocked)
        await asyncio.sleep(0)
        task2 = cache.get_or_build_task(key2, blocked)

        assert cache.build_status(key1)["build_state"] == "in_progress"
        assert cache.build_status(key2)["build_state"] == "queued"
        release.set()
        await asyncio.gather(task1, task2)

    @pytest.mark.asyncio
    async def test_full_pending_queue_rejects_key_without_creating_task(self):
        cache = GraphViewCache(max_concurrent_builds=1, max_pending_builds=1)
        release = asyncio.Event()
        active_key = ("memory", "project", "active", True)
        pending_key = ("memory", "project", "pending", True)
        rejected_key = ("memory", "project", "rejected", True)

        async def blocked():
            await release.wait()
            return _view("done")

        active = cache.get_or_build_task(active_key, blocked)
        await asyncio.sleep(0)
        pending = cache.get_or_build_task(pending_key, blocked)

        with pytest.raises(GraphBuildQueueFullError) as exc_info:
            cache.get_or_build_task(rejected_key, blocked)

        assert cache.build_status(pending_key)["build_state"] == "queued"
        assert exc_info.value.build_status == {"build_state": "rejected_queue_full"}
        assert (
            exc_info.value.build_status["build_state"]
            != cache.build_status(pending_key)["build_state"]
        )
        assert cache.inflight_task(rejected_key) is None
        assert rejected_key not in cache._statuses
        assert len(cache._inflight) == 2

        release.set()
        await asyncio.gather(active, pending)

    @pytest.mark.asyncio
    async def test_inflight_registry_drops_strong_reference_after_completion(self):
        cache = GraphViewCache()
        key = ("memory", "global", None, True)

        task = cache.get_or_build_task(key, lambda: asyncio.sleep(0, result=_view("done")))
        assert cache.inflight_task(key) is task
        await task
        await asyncio.sleep(0)

        assert cache.inflight_task(key) is None
        assert cache._inflight == {}


# ---------------------------------------------------------------------------
# Integration: cache through MemoryGraphProvider.project()
# ---------------------------------------------------------------------------


@pytest.fixture
def db_engine(tmp_path):
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return engine


@pytest.fixture
def svc(tmp_path, db_engine):
    return MemoryService(base_dir=tmp_path, db_engine=db_engine)


def _write_topic(svc: MemoryService, key: str) -> str:
    path = svc.get_wiki_path("global", None, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(BODY, encoding="utf-8")
    return str(path)


def _write_index(svc: MemoryService, keys: list) -> None:
    index_path = svc.get_index_path("global", None)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Memory Index", "", "## global", ""]
    for key in keys:
        lines.append(
            f"- [{key}](global/{key}.md) — type:project tags:t ~10tok "
            f"updated:2026-01-01T00:00:00Z"
        )
    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _insert_row(db_engine, key: str, file_path: str):
    Session = sessionmaker(bind=db_engine)
    session = Session()
    try:
        session.add(
            MemoryMetadataModel(
                key=key,
                memory_type="project",
                scope="global",
                scope_id=None,
                file_path=file_path,
                tags="t",
                related_keys=None,
            )
        )
        session.commit()
    finally:
        session.close()


def _patch_lint_env(monkeypatch, db_engine, svc) -> None:
    from cli_agent_orchestrator.clients import database as db_mod
    from cli_agent_orchestrator.services import memory_service as ms_mod

    monkeypatch.setattr(db_mod, "SessionLocal", sessionmaker(bind=db_engine))
    monkeypatch.setattr(ms_mod, "MEMORY_BASE_DIR", svc.base_dir)
    monkeypatch.setattr(settings_service, "is_memory_enabled", lambda: True)


class TestProviderCacheIntegration:
    @pytest.mark.asyncio
    async def test_lint_mode_isolates_cache_entries(self, monkeypatch):
        """The returned artifact must match its lint mode, not a cached view
        built for the other mode.
        """
        cache = GraphViewCache()
        monkeypatch.setattr(memory_provider, "_CACHE", cache)
        lint_state = {"enabled": False}
        provider = MemoryGraphProvider(lint_enabled=lambda: lint_state["enabled"])
        calls = 0

        async def _build(scope, scope_id, lint_enabled):
            nonlocal calls
            calls += 1
            return GraphView(
                nodes=[],
                edges=[],
                meta={"lint_enabled": lint_enabled},
            )

        monkeypatch.setattr(provider, "_build", _build)

        lint_disabled_view = await provider.project(scope="global")
        lint_state["enabled"] = True
        lint_enabled_view = await provider.project(scope="global")

        assert calls == 2
        assert len(cache._entries) == 2
        assert lint_disabled_view.meta["lint_enabled"] is False
        assert lint_enabled_view.meta["lint_enabled"] is True

    @pytest.mark.asyncio
    async def test_request_scope_ids_do_not_create_unbounded_entries(self, monkeypatch):
        cache = GraphViewCache()
        monkeypatch.setattr(memory_provider, "_CACHE", cache)
        provider = MemoryGraphProvider(lint_enabled=lambda: False)

        async def _build(scope, scope_id, lint_enabled):
            return GraphView(nodes=[], edges=[], meta={})

        monkeypatch.setattr(provider, "_build", _build)
        for i in range(80):
            await provider.project(scope="project", scope_id=f"project-{i}")

        map_bounds = {
            "_entries": cache._max_entries,
            "_inflight": cache._max_concurrent_builds + cache._max_pending_builds,
            "_statuses": cache._max_concurrent_builds + cache._max_pending_builds,
            "_failed_deadlines": cache._max_entries,
        }
        cache_maps = {name: value for name, value in vars(cache).items() if isinstance(value, dict)}

        # A newly-added retention map must declare and test its own bound.
        assert set(cache_maps) == set(map_bounds)
        for name, cache_map in cache_maps.items():
            assert len(cache_map) <= map_bounds[name], name

    @pytest.mark.asyncio
    async def test_global_scope_ids_collapse_to_one_build(self, monkeypatch):
        """Ignored global scope_ids must not consume every admission slot."""
        cache = GraphViewCache()
        monkeypatch.setattr(memory_provider, "_CACHE", cache)
        provider = MemoryGraphProvider(lint_enabled=lambda: False)
        release = asyncio.Event()
        calls = 0

        async def _blocked_build(scope, scope_id, lint_enabled):
            nonlocal calls
            calls += 1
            await release.wait()
            return GraphView(
                nodes=[],
                edges=[],
                meta={"scope": scope, "scope_id": scope_id},
            )

        monkeypatch.setattr(provider, "_build", _blocked_build)
        tasks = [
            provider.project_inflight(scope="global", scope_id=f"ignored-{i}") for i in range(4)
        ]
        legitimate = provider.project_inflight(scope="global")
        assert legitimate is tasks[0]
        await asyncio.sleep(0)
        release.set()
        views = await asyncio.gather(*tasks, legitimate)

        assert calls == 1
        assert len(cache._entries) == 1
        assert all(view.meta["scope_id"] is None for view in views)

    @pytest.mark.asyncio
    async def test_second_project_call_does_not_rerun_lint(self, svc, db_engine, monkeypatch):
        """The money shot: run_lint is called ONCE across two project() calls
        within TTL, and the 2nd view reports meta.cached=True.
        """
        path_a = _write_topic(svc, "a")
        _write_index(svc, ["a"])
        _insert_row(db_engine, "a", path_a)
        _patch_lint_env(monkeypatch, db_engine, svc)

        lint_calls = {"n": 0}
        real_run_lint = wiki_lint.run_lint

        async def _spy_run_lint(*args, **kwargs):
            lint_calls["n"] += 1
            return await real_run_lint(*args, **kwargs)

        monkeypatch.setattr(wiki_lint, "run_lint", _spy_run_lint)
        # Disable the LLM so the (real) run_lint stays cheap in this test.
        monkeypatch.setattr(wiki_lint, "_build_llm_client", lambda: None)

        provider = MemoryGraphProvider(memory_service=svc)
        view1 = await provider.project(scope="global")
        view2 = await provider.project(scope="global")

        assert lint_calls["n"] == 1  # expensive step ran once, not twice
        assert view1.meta["cached"] is False
        assert view2.meta["cached"] is True
        assert view1.meta["as_of"] == view2.meta["as_of"]  # same build timestamp
        assert {n.id for n in view2.nodes} >= {"a"}

    @pytest.mark.asyncio
    async def test_ttl_expiry_reruns_lint_through_provider(self, svc, db_engine, monkeypatch):
        """After TTL expiry a fresh project() re-runs the expensive step."""
        # Swap the module cache for one with a fake clock we control.
        clock = _FakeClock()
        monkeypatch.setattr(
            memory_provider, "_CACHE", GraphViewCache(ttl_s=DEFAULT_TTL_S, clock=clock)
        )

        path_a = _write_topic(svc, "a")
        _write_index(svc, ["a"])
        _insert_row(db_engine, "a", path_a)
        _patch_lint_env(monkeypatch, db_engine, svc)

        calls = {"n": 0}

        async def _fake_run_lint(*args, **kwargs):
            calls["n"] += 1
            return []

        monkeypatch.setattr(wiki_lint, "run_lint", _fake_run_lint)

        provider = MemoryGraphProvider(memory_service=svc)
        await provider.project(scope="global")
        clock.advance(DEFAULT_TTL_S + 1.0)
        view2 = await provider.project(scope="global")

        assert calls["n"] == 2
        assert view2.meta["cached"] is False

    @pytest.mark.asyncio
    async def test_project_scope_does_not_serve_global(self, svc, db_engine, monkeypatch):
        """Per-key isolation through the provider: a global projection cached
        first must NOT be returned for a project-scope request.
        """
        path_a = _write_topic(svc, "a")
        _write_index(svc, ["a"])
        _insert_row(db_engine, "a", path_a)
        _patch_lint_env(monkeypatch, db_engine, svc)
        monkeypatch.setattr(wiki_lint, "_build_llm_client", lambda: None)

        provider = MemoryGraphProvider(memory_service=svc)
        global_view = await provider.project(scope="global")
        assert {n.id for n in global_view.nodes} >= {"a"}

        # A project scope with no wiki on disk → empty view, NOT the cached global.
        project_view = await provider.project(scope="project", scope_id="nonexistent")
        assert project_view.nodes == []
        assert project_view.meta["cached"] is False

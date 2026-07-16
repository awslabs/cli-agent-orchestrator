"""Tests for the graph-layer GraphView cache (Issue #348 perf follow-up).

Covers the cache contract directly (TTL freshness, single-flight, per-key
isolation, invalidate) and its integration through MemoryGraphProvider
(a 2nd project() call within TTL does NOT re-run run_lint).
"""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base, MemoryMetadataModel
from cli_agent_orchestrator.graph.cache import DEFAULT_TTL_S, GraphViewCache, make_meta
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

        key = ("memory", "global", None)
        view1, cached1, _ = await cache.get_or_build(key, builder)
        view2, cached2, _ = await cache.get_or_build(key, builder)

        assert calls["n"] == 1  # builder ran ONCE across two calls
        assert cached1 is False and cached2 is True
        assert view1 is view2  # same cached instance served

    @pytest.mark.asyncio
    async def test_ttl_expiry_reruns_builder(self):
        clock = _FakeClock()
        cache = GraphViewCache(ttl_s=300.0, clock=clock)
        calls = {"n": 0}

        async def builder():
            calls["n"] += 1
            return _view("a")

        key = ("memory", "global", None)
        _, cached1, _ = await cache.get_or_build(key, builder)
        clock.advance(300.1)  # past TTL
        _, cached2, _ = await cache.get_or_build(key, builder)

        assert calls["n"] == 2
        assert cached1 is False and cached2 is False

    @pytest.mark.asyncio
    async def test_per_key_isolation(self):
        """A project-scope entry must not serve a global request."""
        cache = GraphViewCache(ttl_s=300.0)

        async def build_global():
            return _view("g")

        async def build_project():
            return _view("p")

        vg, _, _ = await cache.get_or_build(("memory", "global", None), build_global)
        vp, cached, _ = await cache.get_or_build(("memory", "project", "proj1"), build_project)

        assert cached is False  # different key ⇒ built fresh, not a global hit
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

        key = ("memory", "global", None)
        tasks = [asyncio.create_task(cache.get_or_build(key, slow_builder)) for _ in range(5)]
        await started.wait()
        release.set()
        results = await asyncio.gather(*tasks)

        assert calls["n"] == 1
        # Exactly one caller saw cached=False (the builder), the rest hit cache.
        assert sum(1 for _, cached, _ in results if cached is False) == 1

    @pytest.mark.asyncio
    async def test_invalidate_forces_rebuild(self):
        cache = GraphViewCache(ttl_s=300.0)
        calls = {"n": 0}

        async def builder():
            calls["n"] += 1
            return _view("a")

        key = ("memory", "global", None)
        await cache.get_or_build(key, builder)
        cache.invalidate(key)
        _, cached, _ = await cache.get_or_build(key, builder)

        assert calls["n"] == 2 and cached is False

    @pytest.mark.asyncio
    async def test_expired_entry_is_evicted_not_just_missed(self):
        """An expired entry is REMOVED from ``_entries`` on access, so the map
        does not retain a stale GraphView for every key ever queried.
        """
        clock = _FakeClock()
        cache = GraphViewCache(ttl_s=300.0, clock=clock)

        async def builder():
            return _view("a")

        key = ("memory", "global", None)
        await cache.get_or_build(key, builder)
        assert key in cache._entries  # cached while fresh

        clock.advance(300.1)  # past TTL
        assert cache._fresh(key) is None  # treated as a miss...
        assert key not in cache._entries  # ...AND evicted, not merely skipped

    @pytest.mark.asyncio
    async def test_repeated_expired_lookups_do_not_grow_entries(self):
        """Across many distinct keys whose entries have all expired, a second
        round of lookups must not leave ``_entries`` growing without bound —
        each expired access evicts the entry it read.
        """
        clock = _FakeClock()
        cache = GraphViewCache(ttl_s=300.0, clock=clock)

        async def builder():
            return _view("a")

        keys = [("memory", "global", f"k{i}") for i in range(50)]
        for key in keys:
            await cache.get_or_build(key, builder)
        assert len(cache._entries) == 50

        clock.advance(300.1)  # expire every entry
        # Read each key once (a miss): eviction should drain _entries.
        for key in keys:
            assert cache._fresh(key) is None
        assert len(cache._entries) == 0

    def test_make_meta_does_not_mutate_base(self):
        base = {"provider": "memory", "scope": "global"}
        out = make_meta(base, cached=True, as_of="2026-07-14T00:00:00+00:00")
        assert out["cached"] is True and out["as_of"] == "2026-07-14T00:00:00+00:00"
        assert "cached" not in base  # original untouched

    def test_default_ttl_is_five_minutes(self):
        assert DEFAULT_TTL_S == 300.0


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

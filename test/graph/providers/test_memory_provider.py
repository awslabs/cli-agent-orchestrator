"""Tests for MemoryGraphProvider (U2, Issue #348).

Covers AC 1-6: topic nodes from the index, related_keys + contradiction
edges, cross-scope edge filtering, empty-scope behaviour, and a SOFT
timing assertion for NFR-1. Fixtures use a real tmp_path wiki + SQLite
engine (no mocking of the memory internals); the lint LLM is stubbed.
"""

import asyncio
import json
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base, MemoryMetadataModel
from cli_agent_orchestrator.graph.models import EdgeType, GraphView
from cli_agent_orchestrator.graph.providers import get_provider
from cli_agent_orchestrator.graph.providers.memory import MemoryGraphProvider
from cli_agent_orchestrator.services import settings_service, wiki_lint
from cli_agent_orchestrator.services.memory_service import MemoryService
from cli_agent_orchestrator.services.wiki_lint import LintIssue

BODY = "A reasonably long article body so contradiction pairing engages." + " filler" * 10


@pytest.fixture
def db_engine(tmp_path):
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return engine


@pytest.fixture
def svc(tmp_path, db_engine):
    return MemoryService(base_dir=tmp_path, db_engine=db_engine)


def _write_topic(svc: MemoryService, key: str, *, content: str = BODY) -> str:
    path = svc.get_wiki_path("global", None, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
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


def _insert_row(db_engine, key: str, file_path: str, *, tags: str = "t", related_keys=None):
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
                tags=tags,
                related_keys=related_keys,
            )
        )
        session.commit()
    finally:
        session.close()


@pytest.fixture
def populated_scope(svc, db_engine, monkeypatch):
    """Three global topics: a→b via related_keys, a/b share a tag (pairable)."""
    paths = {key: _write_topic(svc, key) for key in ("a", "b", "c")}
    _write_index(svc, ["a", "b", "c"])
    _insert_row(db_engine, "a", paths["a"], tags="t", related_keys="b")
    _insert_row(db_engine, "b", paths["b"], tags="t")
    _insert_row(db_engine, "c", paths["c"], tags="other")

    # run_lint constructs MemoryService() and SessionLocal() internally —
    # point both at the test fixtures.
    _patch_lint_env(monkeypatch, db_engine, svc)
    return svc


def _patch_lint_env(monkeypatch, db_engine, svc) -> None:
    from cli_agent_orchestrator.clients import database as db_mod
    from cli_agent_orchestrator.services import memory_service as ms_mod

    monkeypatch.setattr(db_mod, "SessionLocal", sessionmaker(bind=db_engine))
    monkeypatch.setattr(ms_mod, "MEMORY_BASE_DIR", svc.base_dir)
    # Hermeticity: run_lint short-circuits on is_memory_enabled() — pin it so
    # a CAO_MEMORY_ENABLED=0 environment can't fail these tests (N3).
    monkeypatch.setattr(settings_service, "is_memory_enabled", lambda: True)


def _stub_llm_contradicts(monkeypatch) -> None:
    """Make the contradiction detector report every pair as contradictory."""
    monkeypatch.setattr(wiki_lint, "_build_llm_client", lambda: object())

    async def _fake(_client, system, user, *, timeout_s):
        return json.dumps({"contradicts": True, "summary": "a and b disagree."})

    monkeypatch.setattr(wiki_lint, "_llm_call", _fake)


def _disable_llm(monkeypatch) -> None:
    monkeypatch.setattr(wiki_lint, "_build_llm_client", lambda: None)


class TestMemoryProviderHappyPath:
    @pytest.mark.asyncio
    async def test_nodes_edges_from_populated_scope(self, populated_scope, monkeypatch):
        """AC 1-4: topic nodes for every index key; related_keys edge with
        source attr; contradiction edge from the lint finding; no edge
        outside the scope's node set.
        """
        _stub_llm_contradicts(monkeypatch)
        provider = MemoryGraphProvider(memory_service=populated_scope)

        view = await provider.project(scope="global")

        assert {n.id for n in view.nodes} >= {"a", "b", "c"}
        assert all(n.kind == "topic" for n in view.nodes)
        assert all(n.status.value == "active" for n in view.nodes)
        related = [e for e in view.edges if e.type == EdgeType.RELATES_TO]
        assert [(e.source, e.target) for e in related] == [("a", "b")]
        assert related[0].attrs["source"] == "related_keys"
        # related_keys carries no relevance score — none must be invented.
        assert "score" not in related[0].attrs and "relevance" not in related[0].attrs

        contradictions = [e for e in view.edges if e.type == EdgeType.CONTRADICTION]
        assert len(contradictions) == 1
        assert {contradictions[0].source, contradictions[0].target} == {"a", "b"}

        node_ids = {n.id for n in view.nodes}
        for edge in view.edges:
            assert edge.source in node_ids and edge.target in node_ids

    @pytest.mark.asyncio
    async def test_resolvable_from_registry(self):
        assert isinstance(get_provider("memory"), MemoryGraphProvider)


class TestMemoryProviderEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_scope_returns_empty_view(self, svc):
        """AC 5: a scope with no wiki on disk → empty GraphView, not an error."""
        provider = MemoryGraphProvider(memory_service=svc)

        view = await provider.project(scope="project", scope_id="nonexistent123")

        assert isinstance(view, GraphView)
        assert view.nodes == [] and view.edges == []

    @pytest.mark.asyncio
    async def test_invalid_scope_id_returns_empty_view(self, svc):
        """get_index_path raises ValueError for a scope_id containing a path
        separator (validate_path_component rejects it before any join); the
        ValueError branch must degrade to an empty GraphView, not raise.
        """
        provider = MemoryGraphProvider(memory_service=svc)

        view = await provider.project(scope="project", scope_id="../evil")

        assert isinstance(view, GraphView)
        assert view.nodes == [] and view.edges == []

    @pytest.mark.asyncio
    async def test_cross_scope_related_key_does_not_leak(self, svc, db_engine, monkeypatch):
        """AC 4: a related_keys row pointing outside (scope, scope_id) must
        not produce a cross-scope edge.
        """
        path_a = _write_topic(svc, "a")
        _write_index(svc, ["a"])
        # "other-scope-key" exists only in a different scope's index — from
        # this scope's perspective it is not a node, so no edge may appear.
        _insert_row(db_engine, "a", path_a, related_keys="other-scope-key")
        _disable_llm(monkeypatch)
        _patch_lint_env(monkeypatch, db_engine, svc)
        provider = MemoryGraphProvider(memory_service=svc)

        view = await provider.project(scope="global")

        assert {n.id for n in view.nodes} >= {"a"}
        assert view.edges == []

    @pytest.mark.asyncio
    async def test_session_scope_id_filters_shared_index(self, svc, db_engine, monkeypatch):
        """FR-9(c): session/agent entries live in the shared global index
        with scope_id embedded in the entry path; projecting one session
        must not surface another session's keys.
        """
        index_path = svc.get_index_path("session", "term-a")
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(
            "\n".join(
                [
                    "# Memory Index",
                    "",
                    "## session",
                    "",
                    "- [k1](session/term-a/k1.md) — type:project tags:t ~10tok "
                    "updated:2026-01-01T00:00:00Z",
                    "- [k2](session/term-b/k2.md) — type:project tags:t ~10tok "
                    "updated:2026-01-01T00:00:00Z",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        _disable_llm(monkeypatch)
        _patch_lint_env(monkeypatch, db_engine, svc)
        provider = MemoryGraphProvider(memory_service=svc)

        view = await provider.project(scope="session", scope_id="term-a")

        # term-b's k2 must not leak into term-a's graph.
        assert {n.id for n in view.nodes} == {"k1"}

    @pytest.mark.asyncio
    async def test_lint_finding_mapping(self, populated_scope, monkeypatch):
        """orphan_page/graph_density become node attrs (not edges);
        stale_claim/poison_frequency/lint_error are dropped (ADR-2);
        contradiction findings from another container are filtered (FR-9).
        """

        async def _fake_run_lint(project_hash, *, scope=None, **kw):
            return [
                LintIssue(issue_type="orphan_page", key="lonely", scope_id=None),
                LintIssue(issue_type="graph_density", key="a", description="hub"),
                LintIssue(issue_type="stale_claim", key="a", description="drop me"),
                LintIssue(issue_type="poison_frequency", key="b", severity="error"),
                LintIssue(issue_type="lint_error", key="run_lint", severity="info"),
                # Same-key contradiction from ANOTHER container — must not leak.
                LintIssue(
                    issue_type="contradiction",
                    key="a",
                    related_key="b",
                    severity="error",
                    scope_id="other-container",
                ),
            ]

        monkeypatch.setattr(wiki_lint, "run_lint", _fake_run_lint)
        provider = MemoryGraphProvider(memory_service=populated_scope)

        view = await provider.project(scope="global")

        by_id = {n.id: n for n in view.nodes}
        assert by_id["lonely"].attrs["is_orphan"] is True
        assert by_id["lonely"].kind == "topic"
        assert by_id["a"].attrs["is_hub"] is True
        # Cross-container contradiction filtered; only the related_keys edge remains.
        assert [e.type for e in view.edges] == [EdgeType.RELATES_TO]
        # EdgeType has no orphan/hub/stale/poison members.
        assert {t.value for t in EdgeType} == {"relates_to", "contradiction", "supersedes"}

    @pytest.mark.asyncio
    async def test_project_completes_under_two_seconds(self, populated_scope, monkeypatch):
        """AC 6 / NFR-1: SOFT timing assertion (not a hard CI gate) — a small
        scope with the LLM detector disabled projects well under 2s.
        """
        _disable_llm(monkeypatch)
        provider = MemoryGraphProvider(memory_service=populated_scope)

        start = time.monotonic()
        await provider.project(scope="global")
        elapsed = time.monotonic() - start

        # Soft NFR-1 bound; loosen rather than gate CI on it if it flakes.
        assert elapsed < 2.0, f"project() took {elapsed:.2f}s (soft NFR-1 bound)"

    @pytest.mark.asyncio
    async def test_run_lint_failure_degrades_to_lint_free_graph(self, populated_scope, monkeypatch):
        """A run_lint failure must degrade to a lint-free graph (topic nodes
        still returned) with meta["lint_error"] set, rather than raising.
        """

        async def _raise_runtime_error(project_hash, *, scope=None, **kw):
            raise RuntimeError("lint backend unavailable")

        monkeypatch.setattr(wiki_lint, "run_lint", _raise_runtime_error)
        provider = MemoryGraphProvider(memory_service=populated_scope)

        view = await provider.project(scope="global")

        assert {n.id for n in view.nodes} >= {"a", "b", "c"}
        assert view.meta["lint_error"] == "RuntimeError"

    @pytest.mark.asyncio
    async def test_run_lint_cancelled_error_propagates(self, populated_scope, monkeypatch):
        """asyncio.CancelledError from run_lint must propagate, not be
        swallowed by the general lint-failure handler.
        """

        async def _raise_cancelled(project_hash, *, scope=None, **kw):
            raise asyncio.CancelledError()

        monkeypatch.setattr(wiki_lint, "run_lint", _raise_cancelled)
        provider = MemoryGraphProvider(memory_service=populated_scope)

        with pytest.raises(asyncio.CancelledError):
            await provider.project(scope="global")

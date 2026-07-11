"""Tests for StubGraphProvider (U3, Issue #348).

Covers AC 1-2: registry resolution to a valid GraphView with no memory
dependency, and kind heterogeneity (never "topic").
"""

import ast
import inspect

import pytest

from cli_agent_orchestrator.graph.models import GraphView
from cli_agent_orchestrator.graph.providers import get_provider, list_providers
from cli_agent_orchestrator.graph.providers import stub as stub_module
from cli_agent_orchestrator.graph.providers.stub import StubGraphProvider


class TestStubProvider:
    @pytest.mark.asyncio
    async def test_resolvable_and_returns_valid_graph_view(self):
        """AC 1: resolvable from the registry by name, returns a valid view."""
        assert "stub" in list_providers()
        provider = get_provider("stub")
        assert isinstance(provider, StubGraphProvider)

        view = await provider.project()

        assert isinstance(view, GraphView)
        assert len(view.nodes) >= 1
        node_ids = {n.id for n in view.nodes}
        for edge in view.edges:
            assert edge.source in node_ids and edge.target in node_ids

    @pytest.mark.asyncio
    async def test_every_node_kind_differs_from_topic(self):
        """AC 2: kind heterogeneity — every node is kind="stub", never "topic"."""
        view = await get_provider("stub").project()

        assert all(n.kind == "stub" for n in view.nodes)
        assert all(n.kind != "topic" for n in view.nodes)

    @pytest.mark.asyncio
    async def test_arbitrary_filters_are_ignored(self):
        """Edge 1: arbitrary **filters pass through without error or effect."""
        bare = await get_provider("stub").project()
        filtered = await get_provider("stub").project(
            scope="global", scope_id="abc", bogus=object(), depth=99
        )

        assert filtered.model_dump() == bare.model_dump()

    def test_stub_module_imports_no_memory_internals(self):
        """Edge 2: static assertion — stub.py's import statements reference
        neither memory_service nor wiki_lint nor sqlite.
        """
        tree = ast.parse(inspect.getsource(stub_module))
        imported: list = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
                imported.extend(alias.name for alias in node.names)
        forbidden = ("memory_service", "wiki_lint", "sqlite")
        for name in imported:
            assert not any(f in name.lower() for f in forbidden), name

    def test_project_body_contains_no_await(self):
        """ADR-7: project() is async but awaits nothing (uniform-async tax
        at zero cost).
        """
        source = inspect.getsource(StubGraphProvider.project)
        func = ast.parse(source.strip()).body[0]
        awaits = [n for n in ast.walk(func) if isinstance(n, ast.Await)]
        assert awaits == []

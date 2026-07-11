"""Stub GraphProvider (U3, Issue #348).

A minimal dependency-free provider proving the provider seam is
heterogeneous: every node kind is "stub" (never "topic"), and nothing
here imports memory_service, wiki_lint, or SQLite. ``project()`` is
declared async per the uniform-async contract (ADR-7) but awaits
nothing — the async tax at zero cost.
"""

from typing import Any

from cli_agent_orchestrator.graph.models import Edge, EdgeType, GraphView, Node
from cli_agent_orchestrator.graph.providers.base import GraphProvider, register_provider


@register_provider("stub")
class StubGraphProvider(GraphProvider):
    """Projects a small fixed graph; ignores all filters."""

    async def project(self, **filters: Any) -> GraphView:
        nodes = [
            Node(id="stub-a", kind="stub", label="Stub A"),
            Node(id="stub-b", kind="stub", label="Stub B"),
            Node(id="stub-c", kind="stub", label="Stub C"),
        ]
        edges = [Edge(source="stub-a", target="stub-b", type=EdgeType.RELATES_TO)]
        return GraphView(nodes=nodes, edges=edges, meta={"provider": "stub"})

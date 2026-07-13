"""Memory wiki GraphProvider (U2, Issue #348).

Projects one memory scope's wiki into a GraphView by calling the
memory-service internals directly (ADR-1: no facade, no MemoryBackend
ABC, no edits to memory_service/wiki_lint) and awaiting
``wiki_lint.run_lint`` in-request (ADR-7).
"""

import asyncio
import logging
from typing import Any, Optional

from cli_agent_orchestrator.graph.models import Edge, EdgeType, GraphView, Node
from cli_agent_orchestrator.graph.providers.base import GraphProvider, register_provider
from cli_agent_orchestrator.services import wiki_lint
from cli_agent_orchestrator.services.memory_service import MemoryService

logger = logging.getLogger(__name__)


@register_provider("memory")
class MemoryGraphProvider(GraphProvider):
    """Projects a (scope, scope_id) memory wiki into nodes and edges.

    Nodes: one kind="topic" node per key in the scope's index (FR-6),
    plus one per orphan_page lint finding — orphans are by definition
    absent from the index, so without an added node the is_orphan
    attribute (FR-8) could never land anywhere. graph_density findings
    map to an existing node's is_hub attribute. Edges: related_keys rows
    (FR-7a) and contradiction lint findings; stale_claim /
    poison_frequency / lint_error findings are dropped (ADR-2). Edges
    never cross the (scope, scope_id) boundary (FR-9).
    """

    def __init__(self, memory_service: Optional[MemoryService] = None) -> None:
        self._svc = memory_service or MemoryService()

    async def project(self, **filters: Any) -> GraphView:
        scope = str(filters.get("scope", "global"))
        raw_scope_id = filters.get("scope_id")
        scope_id: Optional[str] = None if raw_scope_id is None else str(raw_scope_id)
        meta: dict[str, Any] = {"provider": "memory", "scope": scope, "scope_id": scope_id}

        # Resolve + parse the scope's index. A scope with no wiki on disk
        # (or an unresolvable scope/scope_id) is an empty graph, not an error.
        try:
            index_path = self._svc.get_index_path(scope, scope_id)
        except ValueError:
            return GraphView(nodes=[], edges=[], meta=meta)
        if not index_path.exists():
            return GraphView(nodes=[], edges=[], meta=meta)
        try:
            entries = self._svc._parse_index(index_path)
        except OSError:
            return GraphView(nodes=[], edges=[], meta=meta)

        # session/agent indexes are shared per container with scope_id
        # encoded in each entry's path; project/global indexes are already
        # per-container, so their entries carry no scope_id.
        keys: list[str] = []
        seen: set[str] = set()
        for entry in entries:
            if entry["scope"] != scope:
                continue
            if scope in ("session", "agent") and entry["scope_id"] != scope_id:
                continue
            if entry["key"] not in seen:
                seen.add(entry["key"])
                keys.append(entry["key"])

        nodes: dict[str, Node] = {key: Node(id=key, kind="topic", label=key) for key in keys}
        edges: list[Edge] = []

        # related_keys edges (FR-7a). related_keys carries no relevance
        # score, so edge attrs stay score-free. A target outside this
        # scope's key set is dropped — never a cross-scope edge (FR-9).
        related_raw = self._svc._related_keys_lookup(keys, scope, scope_id)
        for key in keys:
            for target in MemoryService._parse_related_keys(related_raw.get(key), scope):
                if target not in nodes or target == key:
                    continue
                edges.append(
                    Edge(
                        source=key,
                        target=target,
                        type=EdgeType.RELATES_TO,
                        attrs={"source": "related_keys"},
                    )
                )

        # Lint findings — awaited directly in-request (ADR-7); no SQL or LLM
        # calls beyond what run_lint itself performs (FR-7, C-1). A lint
        # failure degrades to a lint-free graph rather than a 500.
        try:
            # project_hash arg is only used for run_lint's audit log, not for
            # lookup — `project()` has no cwd/terminal_context to resolve the
            # real project id (resolve_project_id), so this is a placeholder.
            issues = await wiki_lint.run_lint(scope_id or scope, scope=scope)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("memory graph provider: run_lint failed: %r", e, exc_info=True)
            meta["lint_error"] = type(e).__name__
            issues = []

        for issue in issues:
            if issue.issue_type == "orphan_page":
                # run_lint reports findings across every container of the
                # scope; keep only this container's (FR-9).
                if issue.scope_id != scope_id:
                    continue
                node = nodes.get(issue.key)
                if node is None:
                    node = Node(id=issue.key, kind="topic", label=issue.key)
                    nodes[issue.key] = node
                node.attrs["is_orphan"] = True
            elif issue.issue_type == "graph_density":
                # graph_density findings carry no scope_id; membership in
                # this container's key set is the only available guard, so
                # a same-named hub in another container of this scope can
                # mis-mark this one. Fixing it needs wiki_lint to emit
                # scope_id, which ADR-1 forbids editing — tracked follow-up.
                node = nodes.get(issue.key)
                if node is not None:
                    node.attrs["is_hub"] = True
            elif issue.issue_type == "contradiction":
                if issue.scope_id != scope_id or issue.related_key is None:
                    continue
                if issue.key not in nodes or issue.related_key not in nodes:
                    continue
                edges.append(
                    Edge(
                        source=issue.key,
                        target=issue.related_key,
                        type=EdgeType.CONTRADICTION,
                        attrs={"source": "wiki_lint", "summary": issue.description},
                    )
                )
            # stale_claim / poison_frequency / lint_error → dropped (ADR-2).

        return GraphView(nodes=list(nodes.values()), edges=edges, meta=meta)

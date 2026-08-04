"""Memory wiki GraphProvider (U2, Issue #348).

Projects one memory scope's wiki into a GraphView by calling the
memory-service internals directly (ADR-1: no facade, no MemoryBackend
ABC, no edits to memory_service/wiki_lint) and awaiting
``wiki_lint.run_lint`` in-request (ADR-7).
"""

import asyncio
import logging
from typing import Any, Callable, Optional

from cli_agent_orchestrator.graph.cache import GraphViewCache, make_meta
from cli_agent_orchestrator.graph.models import Edge, EdgeType, GraphView, Node
from cli_agent_orchestrator.graph.providers.base import GraphProvider, register_provider
from cli_agent_orchestrator.services import settings_service, wiki_lint
from cli_agent_orchestrator.services.memory_service import MemoryService

logger = logging.getLogger(__name__)

# Module-level cache shared across every MemoryGraphProvider instance (the U4
# route instantiates a fresh provider per request via get_provider, so a
# per-instance cache would never hit). DELIBERATE reversal of the original
# "lint-on-demand, no caching" ADR — see graph/cache.py for the perf finding
# (ripgrep stale_claim ~20s + LLM ~8.5s ⇒ ~30s typical, up to ~148s under
# load, past the frontend's 120s timeout). Keyed by (provider, scope, scope_id).
_CACHE = GraphViewCache()


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

    def __init__(
        self,
        memory_service: Optional[MemoryService] = None,
        lint_enabled: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._svc = memory_service or MemoryService()
        self._lint_enabled = lint_enabled or settings_service.is_memory_lint_enabled

    async def project(self, **filters: Any) -> GraphView:
        """Return this scope's GraphView, served from cache when fresh.

        The expensive build (``_build`` — which awaits ``wiki_lint.run_lint``)
        runs at most once per (scope, scope_id) per TTL window; concurrent cold
        requests for the same key collapse onto a single build (single-flight,
        see GraphViewCache). ``meta.cached`` / ``meta.as_of`` tell the frontend
        whether it got a hit and when the underlying data was projected.
        """
        scope = str(filters.get("scope", "global"))
        raw_scope_id = filters.get("scope_id")
        scope_id: Optional[str] = None if raw_scope_id is None else str(raw_scope_id)

        lint_enabled = self._lint_enabled()
        key = ("memory", scope, scope_id, lint_enabled)
        view, cached, as_of = await _CACHE.get_or_build(
            key,
            lambda: self._build(scope, scope_id, lint_enabled),
        )
        # Re-wrap with fresh cache provenance without mutating the cached
        # instance's own meta (the same GraphView object is served to every hit).
        return GraphView(
            nodes=view.nodes,
            edges=view.edges,
            meta=make_meta(view.meta, cached=cached, as_of=as_of),
        )

    async def _build(self, scope: str, scope_id: Optional[str], lint_enabled: bool) -> GraphView:
        """Project the scope's wiki into a GraphView (the uncached, ~148s path)."""
        meta: dict[str, Any] = {"provider": "memory", "scope": scope, "scope_id": scope_id}
        if not lint_enabled:
            meta.update(
                {
                    "lint_enabled": False,
                    "lint_enrichment": "disabled",
                    "disabled_enrichments": [
                        "orphan_page",
                        "contradiction",
                        "stale_claim",
                        "poison_frequency",
                        "graph_density",
                    ],
                }
            )

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

        # Typed relationship edges from the STORE (issue #511, FR-4.3). The
        # provider projects ACTIVE relationships (relates_to + contradiction +
        # supersedes) read through the single service — NOT from related_keys and
        # NOT from projection-time lint. Edge attrs carry provenance (the row's
        # origin) and NO invented score/relevance. A target outside this scope's
        # node set is dropped (never a cross-scope edge, FR-9). Multiple edge
        # types between one pair remain distinct Edges (FR-4.4).
        _TYPE_MAP = {
            "relates_to": EdgeType.RELATES_TO,
            "contradiction": EdgeType.CONTRADICTION,
            "supersedes": EdgeType.SUPERSEDES,
        }

        issues = []
        if lint_enabled:
            # Lint findings may run expensive detectors. A failure degrades to
            # a lint-free graph rather than a 500.
            try:
                # project_hash arg is only used for run_lint's audit log, not
                # lookup — `project()` has no cwd/terminal_context to resolve
                # the real project id (resolve_project_id), so this is a
                # placeholder.
                issues = await wiki_lint.run_lint(scope_id or scope, scope=scope)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("memory graph provider: run_lint failed: %r", e, exc_info=True)
                meta["lint_error"] = type(e).__name__

        # ORDERING (human review, PR #524): the relationship read MUST come after
        # run_lint. run_lint persists its contradiction findings into this same
        # store (wiki_lint._persist_contradictions), so reading first meant a
        # contradiction detected during THIS projection was absent from the graph
        # it produced — invisible for a full cache window, and reappearing later
        # with no apparent cause. Reading after makes the projection reflect the
        # lint run it just performed.
        try:
            from cli_agent_orchestrator.services.memory_relationship_service import (
                MemoryRelationshipService,
            )

            # Bound the query to THIS projection's node set. Without
            # source_keys the read loads every active relationship in the
            # (scope, scope_id) and discards the out-of-set rows in Python,
            # where the pre-#511 related_keys read was naturally bounded to
            # the current keys.
            #
            # This bounds the SOURCE side only. The both-endpoints check below
            # is still required and must NOT be removed: a source inside the
            # node set may legitimately point at a target outside it, and
            # dropping that edge is what enforces FR-9 (never a cross-scope
            # edge) and keeps GraphView's endpoint validation satisfied.
            active = MemoryRelationshipService().list_relationships(
                scope, scope_id, status="active", source_keys=keys
            )
        except Exception as e:  # degrade to a relationship-free graph, never 500
            logger.warning("memory graph provider: relationship read failed: %r", e)
            meta["relationship_error"] = type(e).__name__
            active = []
        for rel in active:
            edge_type = _TYPE_MAP.get(rel.type)
            if edge_type is None:
                continue
            if rel.source_key not in nodes or rel.target_key not in nodes:
                continue
            if rel.source_key == rel.target_key:
                continue
            edges.append(
                Edge(
                    source=rel.source_key,
                    target=rel.target_key,
                    type=edge_type,
                    attrs={"source": rel.origin},
                )
            )

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
            # contradiction: NO LONGER projected from live lint here (issue #511).
            # Contradiction edges now come from the durable store above (persisted
            # by the wiki_lint producer with origin=wiki_lint), so projecting them
            # again from the in-request lint run would double-source them. Lint
            # still runs for the orphan_page/graph_density NODE attrs above.
            # stale_claim / poison_frequency / lint_error → dropped (ADR-2).

        return GraphView(nodes=list(nodes.values()), edges=edges, meta=meta)

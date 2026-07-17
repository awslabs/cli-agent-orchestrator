// Shared graphology/Sigma graph construction used by the web Memory graph view.
//
// This helper is intentionally package-local to the web/ build; the MCP-apps
// GraphView uses an equivalent implementation because the two packages do not
// currently share a common TypeScript module path. Keep the visual semantics
// (hub size, orphan color, contradiction edge color, circular layout) in sync
// with cao_mcp_apps/src/graph/GraphView.tsx.

import Graph from "graphology";
import { circular } from "graphology-layout";
import { GraphView } from "../api";

export const HUB_SIZE = 12;
export const DEFAULT_SIZE = 6;
export const ORPHAN_COLOR = "#9ca3af";
export const DEFAULT_NODE_COLOR = "#2563eb";
export const CONTRADICTION_COLOR = "#dc2626";
export const DEFAULT_EDGE_COLOR = "#94a3b8";

export function buildGraph(view: GraphView): Graph {
  const graph = new Graph();

  for (const node of view.nodes) {
    const attrs = node.attrs || {};
    graph.addNode(node.id, {
      label: node.label,
      size: attrs.is_hub ? HUB_SIZE : DEFAULT_SIZE,
      color: attrs.is_orphan ? ORPHAN_COLOR : DEFAULT_NODE_COLOR,
    });
  }

  for (const edge of view.edges) {
    if (!graph.hasNode(edge.source) || !graph.hasNode(edge.target)) continue;
    if (graph.hasEdge(edge.source, edge.target)) continue;
    graph.addEdge(edge.source, edge.target, {
      color:
        edge.type === "contradiction"
          ? CONTRADICTION_COLOR
          : DEFAULT_EDGE_COLOR,
    });
  }

  circular.assign(graph);
  return graph;
}

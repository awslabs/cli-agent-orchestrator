// Graph view (ui://cao/graph).
//
// Hydrates from the initial tool result (or `initialSnapshot`), then polls
// `render_graph_view` on an interval and mounts a Sigma.js canvas over a
// graphology graph rebuilt from scratch on every snapshot (ADR-6 LOCKED:
// tool-wrapper data flow, no direct fetch; graphology owns the update path so
// no RFC-6902 patching here — mirrors AgentView's minimal snapshot+error state,
// NOT Dashboard's clientDiff/applyPatch delta-patching).

import Graph from "graphology";
import { circular } from "graphology-layout";
import React, { useEffect, useRef, useState } from "react";
import Sigma from "sigma";
import { HeaderBar } from "../shared/HeaderBar";
import { McpApp } from "../shared/mcpApp";
import type { GraphEdgeData, GraphNodeData, GraphViewData } from "./types";

const POLL_INTERVAL_MS = 30_000;

const HUB_SIZE = 12;
const DEFAULT_SIZE = 6;
const ORPHAN_COLOR = "#9ca3af";
const DEFAULT_NODE_COLOR = "#2563eb";
const CONTRADICTION_COLOR = "#dc2626";
const DEFAULT_EDGE_COLOR = "#94a3b8";

export interface GraphViewProps {
  app?: McpApp;
  initialSnapshot?: GraphViewData;
  provider?: string;
  scope?: string;
  scopeId?: string;
  onOpenTopic?: (nodeId: string, attrs: Record<string, unknown>) => void;
}

function buildGraph(snapshot: GraphViewData): Graph {
  const graph = new Graph();
  for (const node of snapshot.nodes as GraphNodeData[]) {
    graph.addNode(node.id, {
      label: node.label,
      size: Boolean(node.attrs.is_hub) ? HUB_SIZE : DEFAULT_SIZE,
      color: Boolean(node.attrs.is_orphan) ? ORPHAN_COLOR : DEFAULT_NODE_COLOR,
    });
  }
  for (const edge of snapshot.edges as GraphEdgeData[]) {
    // graphology's simple Graph throws if an endpoint isn't a node or if an
    // edge already exists between the pair. The memory provider can emit BOTH
    // a relates_to and a contradiction edge for the same topic pair, so guard
    // both cases (mirrors web/ MemoryGraphView.tsx buildGraph()).
    if (!graph.hasNode(edge.source) || !graph.hasNode(edge.target)) continue;
    if (graph.hasEdge(edge.source, edge.target)) continue;
    const isContradiction = edge.type === "contradiction";
    graph.addEdge(edge.source, edge.target, {
      color: isContradiction ? CONTRADICTION_COLOR : DEFAULT_EDGE_COLOR,
    });
  }
  // Sigma throws at construction if any node lacks a position — assign one
  // deterministically before mounting (no layout step existed previously).
  circular.assign(graph);
  return graph;
}

export function GraphView({
  app,
  initialSnapshot,
  provider = "memory",
  scope,
  scopeId,
  onOpenTopic,
}: GraphViewProps): JSX.Element {
  const [snapshot, setSnapshot] = useState<GraphViewData | null>(
    initialSnapshot ?? null,
  );
  const [unreachable, setUnreachable] = useState(false);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const sigmaRef = useRef<Sigma | null>(null);

  useEffect(() => {
    if (!app) return;
    let stop: (() => void) | undefined;

    // Register handlers BEFORE connect (lifecycle invariant).
    app.onToolResult((result) => {
      const snap = (result?.structuredContent ?? result) as
        GraphViewData | undefined;
      if (snap && Array.isArray(snap.nodes)) {
        setUnreachable(false);
        setSnapshot(snap);
      }
    });

    void app.connect().then(() => {
      stop = app.startPolling(
        "render_graph_view",
        POLL_INTERVAL_MS,
        (snap) => {
          if (snap && Array.isArray((snap as GraphViewData).nodes)) {
            setUnreachable(false);
            setSnapshot(snap as GraphViewData);
          }
        },
        { provider, scope, scope_id: scopeId },
        () => setUnreachable(true),
      );
    });

    return () => {
      if (stop) stop();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [app, provider, scope, scopeId]);

  // Mount/rebuild the Sigma canvas whenever the container or snapshot changes.
  // Never mount against a zero-node snapshot.
  useEffect(() => {
    if (sigmaRef.current) {
      sigmaRef.current.kill();
      sigmaRef.current = null;
    }
    if (!containerRef.current || !snapshot || snapshot.nodes.length === 0) {
      return;
    }
    const graph = buildGraph(snapshot);
    const sigma = new Sigma(graph, containerRef.current);
    sigma.on("clickNode", ({ node }) => {
      onOpenTopic?.(node, graph.getNodeAttributes(node));
    });
    sigmaRef.current = sigma;

    return () => {
      sigma.kill();
      sigmaRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [containerRef, snapshot]);

  function retry(): void {
    if (!app) return;
    void app
      .callServerTool("render_graph_view", {
        provider,
        scope,
        scope_id: scopeId,
      })
      .then((snap) => {
        setUnreachable(false);
        setSnapshot(snap as GraphViewData);
      })
      .catch(() => setUnreachable(true));
  }

  return (
    <div className="cao-root">
      <HeaderBar title="CAO Graph" />
      {unreachable ? (
        <div
          className="cao-taskcontrol-error"
          role="alert"
          data-testid="retry-banner"
        >
          Backplane unreachable.{" "}
          <button
            type="button"
            className="cao-btn"
            data-testid="retry-button"
            onClick={retry}
          >
            Retry
          </button>
        </div>
      ) : !snapshot ? (
        <div className="cao-events-empty" data-testid="graph-loading">
          Loading graph…
        </div>
      ) : snapshot.nodes.length === 0 ? (
        <div className="cao-events-empty" data-testid="empty-placeholder">
          No graph data for this provider
        </div>
      ) : (
        <div
          ref={containerRef}
          className="cao-graph-canvas"
          data-testid="graph-canvas"
        />
      )}
    </div>
  );
}

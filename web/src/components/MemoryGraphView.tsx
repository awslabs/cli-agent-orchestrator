// Knowledge-graph sub-view for the Memory panel (Issue #348).
//
// Renders the memory graph with Sigma over a graphology graph in the web/ React
// stack: lets you click a node to READ that topic's content (plain text —
// memory bodies are untrusted agent output), and export the loaded scope to an
// Obsidian vault. All I/O goes through api.ts; this component never fetch()es.

import { useCallback, useEffect, useRef, useState } from "react";
import Graph from "graphology";
import Sigma from "sigma";
import { Brain, Download, RefreshCw, X } from "lucide-react";
import {
  api,
  ApiError,
  GraphExportResult,
  GraphView,
  MemoryDetail,
} from "../api";
import { useStore } from "../store";
import {
  buildGraph,
  CONTRADICTION_COLOR,
  DEFAULT_NODE_COLOR,
  ORPHAN_COLOR,
} from "../graph/buildGraph";

// The graph endpoint requires a concrete, non-private provider scope. session /
// agent are refused server-side (400, private tier), and '' (all scopes) can't
// project a single graph — so only these two are fetchable.
const GRAPHABLE_SCOPES = new Set(["global", "project"]);

interface MemoryGraphViewProps {
  scope: string;
  scopeId: string;
}

function formatGraphError(err: ApiError): string {
  if (err.status === 400) {
    return err.detail || "This scope cannot be viewed as a graph.";
  }
  if (err.status === 404) {
    return err.detail || "Graph provider not found (is memory enabled?).";
  }
  if (err.name === "AbortError") {
    return (
      "Graph fetch timed out (waited 120s). The wiki-lint projection is ~30s typical, " +
      "up to ~148s under load, so a full timeout usually means the CAO server is stuck or down. " +
      "In dev the UI proxies to cao-server on :9889 — check it’s running (uv run cao-server), then Refresh."
    );
  }
  if (err.status === undefined) {
    return (
      "Couldn’t reach the CAO server. In dev the UI proxies to cao-server on :9889 — " +
      "make sure it’s running (uv run cao-server). On the bundled UI, the CAO server serves " +
      "this page directly, so it should already be up."
    );
  }
  return err.detail || err.message || "The CAO server returned an error.";
}

function formatExportError(err: ApiError): string {
  if (err.status === 401 || err.status === 403) {
    return "Export not authorized (needs cao:write). With auth off this should not happen.";
  }
  if (err.status === 422) {
    return `Export blocked by the secret gate: ${err.detail || "a secret pattern matched"}. Nothing was written.`;
  }
  if (err.status === 400) {
    return err.detail || "Bad export destination or private scope.";
  }
  return err.detail || err.message || "Export failed.";
}

function formatExportMessage(res: GraphExportResult): string {
  const n = res.written_files.length;
  const first = n ? ` (${res.written_files[0]})` : "";
  return `Exported ${n} note${n === 1 ? "" : "s"} to vault "${res.dest}"${first}`;
}

function effectiveScopeId(scope: string, scopeId: string): string | undefined {
  // scope_id only belongs to the `project` tier. `global` has no scope_id, so a
  // stale value left in state from a prior project selection must NOT ride along
  // — it produces a 404 (global + a project scope_id names nothing).
  return scope === "project" ? scopeId || undefined : undefined;
}

function useGraphData(
  scope: string,
  scopeId: string,
  graphable: boolean,
  sid: string | undefined,
) {
  const [view, setView] = useState<GraphView | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fetchSeqRef = useRef(0);

  const refresh = useCallback(async () => {
    if (!graphable) return;
    const seq = ++fetchSeqRef.current;
    const isStale = () => fetchSeqRef.current !== seq;
    setLoading(true);
    setError(null);
    try {
      const data = await api.getGraph("memory", scope, sid);
      if (isStale()) return;
      setView(data);
    } catch (e) {
      if (isStale()) return;
      setView(null);
      setError(formatGraphError(e as ApiError));
    } finally {
      if (!isStale()) setLoading(false);
    }
  }, [graphable, scope, sid]);

  useEffect(() => {
    setView(null);
    setError(null);
    refresh();
  }, [refresh]);

  return { view, loading, error, refresh };
}

function useNodeTopic() {
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const [detail, setDetail] = useState<{
    id: string;
    data: MemoryDetail;
  } | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);

  const openTopic = useCallback(
    async (nodeId: string, scope: string, scopeId: string) => {
      const sid = scope === "project" ? scopeId || undefined : undefined;
      setSelectedNode(nodeId);
      setDetail(null);
      setDetailError(null);
      try {
        const data = await api.getMemory(nodeId, scope || undefined, sid);
        setSelectedNode((current) => {
          if (current === nodeId) setDetail({ id: nodeId, data });
          return current;
        });
      } catch (e) {
        const err = e as ApiError;
        setSelectedNode((current) => {
          if (current === nodeId)
            setDetailError(
              err.detail || err.message || "Failed to load memory",
            );
          return current;
        });
      }
    },
    [],
  );

  const reset = useCallback(() => {
    setSelectedNode(null);
    setDetail(null);
    setDetailError(null);
  }, []);

  return { selectedNode, detail, detailError, openTopic, reset };
}

function bindSigmaEvents(
  sigma: Sigma,
  graph: Graph,
  container: HTMLDivElement,
  dragRef: React.MutableRefObject<{ node: string | null; moved: boolean }>,
  openTopic: (nodeId: string) => void,
) {
  sigma.on("downNode", ({ node }) => {
    dragRef.current = { node, moved: false };
    sigma.getCamera().disable();
  });

  sigma.on("moveBody", ({ event }) => {
    const drag = dragRef.current;
    if (!drag.node) return;
    drag.moved = true;
    const pos = sigma.viewportToGraph({ x: event.x, y: event.y });
    graph.setNodeAttribute(drag.node, "x", pos.x);
    graph.setNodeAttribute(drag.node, "y", pos.y);
    event.preventSigmaDefault();
    event.original.preventDefault();
    event.original.stopPropagation();
  });

  const endDrag = () => {
    if (dragRef.current.node) {
      sigma.getCamera().enable();
      dragRef.current.node = null;
    }
  };
  sigma.on("upNode", endDrag);
  sigma.on("upStage", endDrag);

  sigma.on("clickNode", ({ node }) => {
    if (dragRef.current.moved) {
      dragRef.current.moved = false;
      return;
    }
    void openTopic(node);
  });

  sigma.on("enterNode", () => {
    if (!dragRef.current.node) container.style.cursor = "grab";
  });
  sigma.on("leaveNode", () => {
    if (!dragRef.current.node) container.style.cursor = "";
  });
  sigma.on("downNode", () => {
    container.style.cursor = "grabbing";
  });
  sigma.on("upStage", () => {
    container.style.cursor = "";
  });
  sigma.on("upNode", () => {
    container.style.cursor = "grab";
  });
}

function useSigma(
  containerRef: React.RefObject<HTMLDivElement>,
  view: GraphView | null,
  openTopic: (nodeId: string) => void,
) {
  const sigmaRef = useRef<Sigma | null>(null);
  const dragRef = useRef<{ node: string | null; moved: boolean }>({
    node: null,
    moved: false,
  });

  useEffect(() => {
    if (sigmaRef.current) {
      sigmaRef.current.kill();
      sigmaRef.current = null;
    }
    if (!containerRef.current || !view || view.nodes.length === 0) return;

    const graph = buildGraph(view);
    const sigma = new Sigma(graph, containerRef.current, {
      renderLabels: true,
      labelRenderedSizeThreshold: 0,
    });
    const container = containerRef.current;

    bindSigmaEvents(sigma, graph, container, dragRef, openTopic);

    sigmaRef.current = sigma;

    return () => {
      sigma.kill();
      sigmaRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, openTopic]);
}

function useGraphExport(
  scope: string,
  sid: string | undefined,
  hasGraph: boolean,
) {
  const { showSnackbar } = useStore();
  const [exporting, setExporting] = useState(false);

  const exportGraph = useCallback(async () => {
    if (!hasGraph) return;
    setExporting(true);
    try {
      const dest = `${scope}-vault`;
      const res = await api.exportGraph(
        "memory",
        { sink: "obsidian", dest },
        scope,
        sid,
      );
      showSnackbar({ type: "success", message: formatExportMessage(res) });
    } catch (e) {
      showSnackbar({
        type: "error",
        message: formatExportError(e as ApiError),
      });
    } finally {
      setExporting(false);
    }
  }, [hasGraph, scope, sid, showSnackbar]);

  return { exporting, exportGraph };
}

function GraphScopeGuard() {
  return (
    <div className="bg-gray-800/60 border border-gray-700/50 rounded-xl p-8 text-center">
      <Brain size={32} className="mx-auto text-gray-600 mb-3" />
      <p className="text-gray-400 text-sm">
        Pick <span className="text-emerald-400">global</span> or{" "}
        <span className="text-emerald-400">project</span> to view the graph.
      </p>
      <p className="text-gray-600 text-xs mt-1">
        The <span className="text-gray-400">All scopes</span>,{" "}
        <span className="text-gray-400">session</span> and{" "}
        <span className="text-gray-400">agent</span> tiers are private and
        cannot be projected as a graph.
      </p>
    </div>
  );
}

function GraphToolbar({
  view,
  loading,
  exporting,
  hasGraph,
  onRefresh,
  onExport,
}: {
  view: GraphView | null;
  loading: boolean;
  exporting: boolean;
  hasGraph: boolean;
  onRefresh: () => void;
  onExport: () => void;
}) {
  return (
    <div className="flex items-center justify-between mb-4">
      <h3 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
        Knowledge Graph
        {view
          ? ` (${view.nodes.length} node${view.nodes.length === 1 ? "" : "s"})`
          : ""}
      </h3>
      <div className="flex items-center gap-2">
        <button
          onClick={onRefresh}
          disabled={loading}
          className="flex items-center gap-2 bg-gray-700 hover:bg-gray-600 disabled:opacity-40 text-gray-200 text-sm font-medium px-3 py-2 rounded-lg transition-colors"
          title="Rebuild the graph"
        >
          <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
          Refresh
        </button>
        <button
          onClick={onExport}
          disabled={!hasGraph || exporting}
          className="flex items-center gap-2 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 text-white text-sm font-medium px-3 py-2 rounded-lg transition-colors"
          title={
            hasGraph
              ? "Export this graph to an Obsidian vault"
              : "Load a graph first"
          }
        >
          <Download size={14} />
          {exporting ? "Exporting…" : "Export to Obsidian"}
        </button>
      </div>
    </div>
  );
}

function GraphCanvas({
  loading,
  error,
  hasGraph,
  scope,
  scopeId,
  containerRef,
  onRetry,
}: {
  loading: boolean;
  error: string | null;
  hasGraph: boolean;
  scope: string;
  scopeId: string;
  containerRef: React.RefObject<HTMLDivElement>;
  onRetry: () => void;
}) {
  return (
    <div className="relative flex-1 min-w-0 bg-gray-950/60 border border-gray-700/30 rounded-lg overflow-hidden">
      {loading ? (
        <div
          className="absolute inset-0 flex flex-col items-center justify-center text-center px-6"
          data-testid="graph-loading"
        >
          <RefreshCw size={26} className="text-emerald-500 animate-spin mb-3" />
          <p className="text-gray-300 text-sm">Building graph…</p>
          <p className="text-gray-500 text-xs mt-1">
            This can take ~30s (up to ~148s under load) — the server runs
            wiki-lint detectors.
          </p>
        </div>
      ) : error ? (
        <div
          className="absolute inset-0 flex flex-col items-center justify-center text-center px-6"
          data-testid="graph-error"
        >
          <X size={28} className="text-red-500 mb-3" />
          <p className="text-red-400 text-sm">{error}</p>
          <button
            onClick={onRetry}
            className="mt-3 text-emerald-400 text-xs hover:underline"
          >
            Retry
          </button>
        </div>
      ) : !hasGraph ? (
        <div
          className="absolute inset-0 flex flex-col items-center justify-center text-center px-6"
          data-testid="graph-empty"
        >
          <Brain size={32} className="text-gray-600 mb-3" />
          <p className="text-gray-500 text-sm">No graph for this scope.</p>
          <p className="text-gray-600 text-xs mt-1">
            Scope <code className="text-emerald-400">{scope}</code>
            {scopeId ? (
              <>
                {" "}
                / <code className="text-emerald-400">{scopeId}</code>
              </>
            ) : null}{" "}
            has no topics yet.
          </p>
        </div>
      ) : null}
      <div
        ref={containerRef}
        data-testid="graph-canvas"
        className="absolute inset-0"
      />
    </div>
  );
}

function GraphSidePanel({
  selectedNode,
  detail,
  detailError,
}: {
  selectedNode: string | null;
  detail: { id: string; data: MemoryDetail } | null;
  detailError: string | null;
}) {
  return (
    <aside className="w-80 shrink-0 flex flex-col bg-gray-950/60 border border-gray-700/30 rounded-lg overflow-hidden">
      {selectedNode ? (
        <>
          <div className="px-4 py-3 border-b border-gray-700/30">
            <div className="text-sm font-semibold text-gray-200 break-all">
              {selectedNode}
            </div>
            {detail && detail.id === selectedNode && (
              <div className="text-xs text-gray-500 mt-1">
                {detail.data.memory_type}
                {detail.data.updated_at
                  ? ` · updated ${new Date(detail.data.updated_at).toLocaleString()}`
                  : ""}
              </div>
            )}
          </div>
          <div className="flex-1 overflow-y-auto p-4">
            {detailError ? (
              <div className="text-red-400 text-sm">{detailError}</div>
            ) : detail && detail.id === selectedNode ? (
              <div className="text-sm text-gray-300 font-mono whitespace-pre-wrap leading-relaxed">
                {detail.data.content}
              </div>
            ) : (
              <div className="text-gray-500 text-sm">
                Loading “{selectedNode}”…
              </div>
            )}
          </div>
        </>
      ) : (
        <div className="flex-1 flex items-center justify-center text-center px-6">
          <p className="text-gray-500 text-sm">
            Click a node in the graph to read that memory.
          </p>
        </div>
      )}
    </aside>
  );
}

function GraphLegend() {
  return (
    <div className="flex flex-wrap items-center gap-4 mt-3 text-xs text-gray-500">
      <span className="flex items-center gap-1.5">
        <span
          className="w-2.5 h-2.5 rounded-full"
          style={{ background: DEFAULT_NODE_COLOR }}
        />{" "}
        topic
      </span>
      <span className="flex items-center gap-1.5">
        <span
          className="w-2.5 h-2.5 rounded-full"
          style={{ background: ORPHAN_COLOR }}
        />{" "}
        orphan
      </span>
      <span className="flex items-center gap-1.5">
        <span
          className="w-3.5 h-3.5 rounded-full"
          style={{ background: DEFAULT_NODE_COLOR }}
        />{" "}
        larger = hub
      </span>
      <span className="flex items-center gap-1.5">
        <span
          className="inline-block w-3.5 h-0.5"
          style={{ background: CONTRADICTION_COLOR }}
        />{" "}
        contradiction edge
      </span>
    </div>
  );
}

export function MemoryGraphView({ scope, scopeId }: MemoryGraphViewProps) {
  const graphable = GRAPHABLE_SCOPES.has(scope);
  const sid = effectiveScopeId(scope, scopeId);
  const containerRef = useRef<HTMLDivElement>(null);

  const { view, loading, error, refresh } = useGraphData(
    scope,
    scopeId,
    graphable,
    sid,
  );
  const { selectedNode, detail, detailError, openTopic, reset } =
    useNodeTopic();
  const { exporting, exportGraph } = useGraphExport(
    scope,
    sid,
    !!view && view.nodes.length > 0,
  );

  const hasGraph = !!view && view.nodes.length > 0;

  const handleNodeClick = useCallback(
    (nodeId: string) => openTopic(nodeId, scope, scopeId),
    [openTopic, scope, scopeId],
  );

  useEffect(() => {
    reset();
  }, [scope, scopeId, reset]);

  useSigma(containerRef, view, handleNodeClick);

  if (!graphable) {
    return <GraphScopeGuard />;
  }

  return (
    <div className="bg-gray-800/60 border border-gray-700/50 rounded-xl p-5">
      <GraphToolbar
        view={view}
        loading={loading}
        exporting={exporting}
        hasGraph={hasGraph}
        onRefresh={refresh}
        onExport={exportGraph}
      />

      <div className="flex gap-4 h-[600px]">
        <GraphCanvas
          loading={loading}
          error={error}
          hasGraph={hasGraph}
          scope={scope}
          scopeId={scopeId}
          containerRef={containerRef}
          onRetry={refresh}
        />
        <GraphSidePanel
          selectedNode={selectedNode}
          detail={detail}
          detailError={detailError}
        />
      </div>

      <GraphLegend />
    </div>
  );
}

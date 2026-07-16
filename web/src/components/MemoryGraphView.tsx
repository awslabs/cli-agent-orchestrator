// Knowledge-graph sub-view for the Memory panel (Issue #348).
//
// Renders the memory graph with Sigma over a graphology graph in the web/ React
// stack: lets you click a node to READ that topic's content (plain text —
// memory bodies are untrusted agent output), and export the loaded scope to an
// Obsidian vault. All I/O goes through api.ts; this component never fetch()es.
//
// Visual constants mirror cao_mcp_apps/src/graph/GraphView.tsx exactly.

import { useEffect, useRef, useState } from 'react'
import Graph from 'graphology'
import { circular } from 'graphology-layout'
import Sigma from 'sigma'
import { Brain, Download, RefreshCw, X } from 'lucide-react'
import { api, ApiError, GraphView, MemoryDetail } from '../api'
import { useStore } from '../store'

const HUB_SIZE = 12
const DEFAULT_SIZE = 6
const ORPHAN_COLOR = '#9ca3af'
const DEFAULT_NODE_COLOR = '#2563eb'
const CONTRADICTION_COLOR = '#dc2626'
const DEFAULT_EDGE_COLOR = '#94a3b8'

// The graph endpoint requires a concrete, non-private provider scope. session /
// agent are refused server-side (400, private tier), and '' (all scopes) can't
// project a single graph — so only these two are fetchable.
const GRAPHABLE_SCOPES = new Set(['global', 'project'])

interface MemoryGraphViewProps {
  scope: string
  scopeId: string
}

/**
 * Build a graphology graph from the GraphView wire shape, mirroring
 * GraphView.tsx buildGraph(). circular.assign gives every node an x/y — Sigma
 * throws at construction otherwise. Edges referencing unknown nodes (or
 * duplicates) are skipped rather than throwing.
 */
export function buildGraph(view: GraphView): Graph {
  const graph = new Graph()
  for (const node of view.nodes) {
    const attrs = node.attrs || {}
    graph.addNode(node.id, {
      label: node.label,
      size: attrs.is_hub ? HUB_SIZE : DEFAULT_SIZE,
      color: attrs.is_orphan ? ORPHAN_COLOR : DEFAULT_NODE_COLOR,
    })
  }
  for (const edge of view.edges) {
    if (!graph.hasNode(edge.source) || !graph.hasNode(edge.target)) continue
    if (graph.hasEdge(edge.source, edge.target)) continue
    graph.addEdge(edge.source, edge.target, {
      color: edge.type === 'contradiction' ? CONTRADICTION_COLOR : DEFAULT_EDGE_COLOR,
    })
  }
  circular.assign(graph)
  return graph
}

export function MemoryGraphView({ scope, scopeId }: MemoryGraphViewProps) {
  const { showSnackbar } = useStore()

  const [view, setView] = useState<GraphView | null>(null)
  const [loading, setLoading] = useState(false)
  // Inline error message shown in the canvas area (unreachable / timeout / bad
  // scope), distinct from the friendly scope-guard below.
  const [error, setError] = useState<string | null>(null)
  const [exporting, setExporting] = useState(false)

  // Selected-topic side panel state. Keyed by node id so a slow fetch for a
  // previously-clicked node can't land under a later selection.
  const [selectedNode, setSelectedNode] = useState<string | null>(null)
  const [detail, setDetail] = useState<{ id: string; data: MemoryDetail } | null>(null)
  const [detailError, setDetailError] = useState<string | null>(null)

  const containerRef = useRef<HTMLDivElement | null>(null)
  const sigmaRef = useRef<Sigma | null>(null)
  // Latest scope/scopeId, so the clickNode handler (bound once per mount) reads
  // current values without being torn down and rebuilt on every scope change.
  const scopeRef = useRef({ scope, scopeId })
  scopeRef.current = { scope, scopeId }
  // Drag state for node dragging. `node` is the node under the pointer between
  // downNode and up; `moved` records whether the pointer actually moved so a
  // drag isn't mistaken for a click-to-read (Sigma can still fire clickNode on
  // mouse-up). Reset on every downNode.
  const dragRef = useRef<{ node: string | null; moved: boolean }>({ node: null, moved: false })
  // Monotonic id for the in-flight graph fetch. Each fetchGraph() call claims
  // the next id; only the latest may touch view/error/loading. Guards against a
  // stale request landing after the user switched scope/scopeId — mirrors the
  // latest-wins pattern openTopic() uses for the side panel.
  const fetchSeqRef = useRef(0)

  const graphable = GRAPHABLE_SCOPES.has(scope)

  // scope_id only belongs to the `project` tier. `global` has no scope_id, so a
  // stale value left in state from a prior project selection must NOT ride along
  // — it produces a 404 (global + a project scope_id names nothing). Compute the
  // effective scope_id from the scope so global always sends none, regardless of
  // what's in `scopeId`.
  const effectiveScopeId = scope === 'project' ? scopeId || undefined : undefined

  const openTopic = async (nodeId: string) => {
    const { scope: s } = scopeRef.current
    // Recompute from the current scope rather than trusting a captured scopeId,
    // so a global topic read never carries a stale project scope_id.
    const sid = s === 'project' ? scopeRef.current.scopeId || undefined : undefined
    setSelectedNode(nodeId)
    setDetail(null)
    setDetailError(null)
    try {
      const data = await api.getMemory(nodeId, s || undefined, sid)
      // Guard against a stale fetch clobbering a later selection.
      setSelectedNode(current => {
        if (current === nodeId) setDetail({ id: nodeId, data })
        return current
      })
    } catch (e) {
      const err = e as ApiError
      setSelectedNode(current => {
        if (current === nodeId) setDetailError(err.detail || err.message || 'Failed to load memory')
        return current
      })
    }
  }

  const fetchGraph = async () => {
    if (!graphable) return
    // Claim this fetch's id; a later fetchGraph() (scope switch) bumps it, so
    // any state update below is skipped once we're no longer the latest.
    const seq = ++fetchSeqRef.current
    const isStale = () => fetchSeqRef.current !== seq
    setLoading(true)
    setError(null)
    try {
      const data = await api.getGraph('memory', scope, effectiveScopeId)
      if (isStale()) return
      setView(data)
    } catch (e) {
      if (isStale()) return
      const err = e as ApiError
      setView(null)
      if (err.status === 400) {
        setError(err.detail || 'This scope cannot be viewed as a graph.')
      } else if (err.status === 404) {
        setError(err.detail || 'Graph provider not found (is memory enabled?).')
      } else if (err.name === 'AbortError') {
        // The AbortController in api.ts fired after the 120s graph budget. The
        // wiki-lint projection is ~30s typical / up to ~148s under load, so a
        // full timeout usually means the CAO server is stuck or down rather
        // than merely slow.
        setError(
          'Graph fetch timed out (waited 120s). The wiki-lint projection is ~30s typical, up to ~148s under load, so a full timeout usually means the CAO server is stuck or down. In dev the UI proxies to cao-server on :9889 — check it’s running (uv run cao-server), then Refresh.',
        )
      } else if (err.status === undefined) {
        // No HTTP status = the fetch never reached a server (connection
        // refused / proxy target down). The web UI is same-origin: in dev Vite
        // proxies /graph + /memory to cao-server on :9889; the bundled UI is
        // served by that same server. Either way the target isn’t answering.
        setError(
          'Couldn’t reach the CAO server. In dev the UI proxies to cao-server on :9889 — make sure it’s running (uv run cao-server). On the bundled UI, the CAO server serves this page directly, so it should already be up.',
        )
      } else {
        setError(err.detail || err.message || 'The CAO server returned an error.')
      }
    } finally {
      // Only the latest request may flip the spinner off — a stale finally
      // must not mask the current request's loading state.
      if (!isStale()) setLoading(false)
    }
  }

  // Refetch whenever the shared scope selector changes. Clears any open topic
  // so the side panel doesn't show a memory from the previous scope.
  useEffect(() => {
    setSelectedNode(null)
    setDetail(null)
    setDetailError(null)
    if (graphable) {
      fetchGraph()
    } else {
      setView(null)
      setError(null)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope, scopeId])

  // Mount / rebuild the Sigma canvas whenever the snapshot changes. Never mount
  // against a zero-node snapshot. kill() before re-mount and on unmount so no
  // WebGL context leaks.
  useEffect(() => {
    if (sigmaRef.current) {
      sigmaRef.current.kill()
      sigmaRef.current = null
    }
    if (!containerRef.current || !view || view.nodes.length === 0) return

    const graph = buildGraph(view)
    const sigma = new Sigma(graph, containerRef.current, {
      renderLabels: true,
      labelRenderedSizeThreshold: 0,
    })
    const container = containerRef.current

    // ── Node dragging (Sigma v3 canonical pattern) ──────────────────────
    // Sigma v3 does not move nodes on its own. On downNode we remember the
    // node and DISABLE the camera so the pan gesture doesn't fight the drag;
    // on moveBody we translate the pointer to graph coords and write x/y; on
    // mouse-up we clear state and RE-ENABLE the camera. `dragRef.moved`
    // distinguishes a drag from a click (see clickNode below).
    sigma.on('downNode', ({ node }) => {
      dragRef.current = { node, moved: false }
      sigma.getCamera().disable()
    })

    sigma.on('moveBody', ({ event }) => {
      const drag = dragRef.current
      if (!drag.node) return
      drag.moved = true
      const pos = sigma.viewportToGraph({ x: event.x, y: event.y })
      graph.setNodeAttribute(drag.node, 'x', pos.x)
      graph.setNodeAttribute(drag.node, 'y', pos.y)
      // Keep the camera from also panning during the drag.
      event.preventSigmaDefault()
      event.original.preventDefault()
      event.original.stopPropagation()
    })

    // Mouse-up may land on the node (upNode) or on empty canvas after the
    // pointer slid off (upStage) — end the drag on either and re-enable the
    // camera. Defer clearing the node so the trailing clickNode (below) can
    // still read `moved` to tell a drag from a click.
    const endDrag = () => {
      if (dragRef.current.node) {
        sigma.getCamera().enable()
        // Keep `moved` so the clickNode that fires right after a drag is
        // suppressed; only null the node so a fresh downNode starts clean.
        dragRef.current.node = null
      }
    }
    sigma.on('upNode', endDrag)
    sigma.on('upStage', endDrag)

    // Click-to-read: only when the pointer did NOT move between down and up.
    // A drag leaves `moved === true`, so it never opens the side panel.
    sigma.on('clickNode', ({ node }) => {
      if (dragRef.current.moved) {
        dragRef.current.moved = false
        return
      }
      void openTopic(node)
    })

    // Cursor affordance: grab on hover, grabbing while dragging.
    sigma.on('enterNode', () => {
      if (!dragRef.current.node) container.style.cursor = 'grab'
    })
    sigma.on('leaveNode', () => {
      if (!dragRef.current.node) container.style.cursor = ''
    })
    sigma.on('downNode', () => {
      container.style.cursor = 'grabbing'
    })
    sigma.on('upStage', () => {
      container.style.cursor = ''
    })
    sigma.on('upNode', () => {
      container.style.cursor = 'grab'
    })

    sigmaRef.current = sigma

    return () => {
      sigma.kill()
      sigmaRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view])

  const handleExport = async () => {
    setExporting(true)
    try {
      // dest is a RELATIVE vault name; the server confines it under
      // CAO_GRAPH_EXPORT_ROOT. Never send an absolute path.
      const dest = `${scope}-vault`
      const res = await api.exportGraph('memory', { sink: 'obsidian', dest }, scope, effectiveScopeId)
      const n = res.written_files.length
      const first = n ? ` (${res.written_files[0]})` : ''
      showSnackbar({
        type: 'success',
        message: `Exported ${n} note${n === 1 ? '' : 's'} to vault "${res.dest}"${first}`,
      })
    } catch (e) {
      const err = e as ApiError
      let message: string
      if (err.status === 401 || err.status === 403) {
        message = 'Export not authorized (needs cao:write). With auth off this should not happen.'
      } else if (err.status === 422) {
        // Secret gate: err.detail names only the matched PATTERN, never the
        // content. Surface it verbatim; nothing was written.
        message = `Export blocked by the secret gate: ${err.detail || 'a secret pattern matched'}. Nothing was written.`
      } else if (err.status === 400) {
        message = err.detail || 'Bad export destination or private scope.'
      } else {
        message = err.detail || err.message || 'Export failed.'
      }
      showSnackbar({ type: 'error', message })
    } finally {
      setExporting(false)
    }
  }

  const hasGraph = !!view && view.nodes.length > 0

  // Friendly guard: don't fire a doomed request for '' / session / agent.
  if (!graphable) {
    return (
      <div className="bg-gray-800/60 border border-gray-700/50 rounded-xl p-8 text-center">
        <Brain size={32} className="mx-auto text-gray-600 mb-3" />
        <p className="text-gray-400 text-sm">Pick <span className="text-emerald-400">global</span> or <span className="text-emerald-400">project</span> to view the graph.</p>
        <p className="text-gray-600 text-xs mt-1">
          The <span className="text-gray-400">All scopes</span>, <span className="text-gray-400">session</span> and <span className="text-gray-400">agent</span> tiers are private and cannot be projected as a graph.
        </p>
      </div>
    )
  }

  return (
    <div className="bg-gray-800/60 border border-gray-700/50 rounded-xl p-5">
      {/* Toolbar */}
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
          Knowledge Graph{view ? ` (${view.nodes.length} node${view.nodes.length === 1 ? '' : 's'})` : ''}
        </h3>
        <div className="flex items-center gap-2">
          <button
            onClick={fetchGraph}
            disabled={loading}
            className="flex items-center gap-2 bg-gray-700 hover:bg-gray-600 disabled:opacity-40 text-gray-200 text-sm font-medium px-3 py-2 rounded-lg transition-colors"
            title="Rebuild the graph"
          >
            <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
            Refresh
          </button>
          <button
            onClick={handleExport}
            disabled={!hasGraph || exporting}
            className="flex items-center gap-2 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 text-white text-sm font-medium px-3 py-2 rounded-lg transition-colors"
            title={hasGraph ? 'Export this graph to an Obsidian vault' : 'Load a graph first'}
          >
            <Download size={14} />
            {exporting ? 'Exporting…' : 'Export to Obsidian'}
          </button>
        </div>
      </div>

      {/* Graph + side panel */}
      <div className="flex gap-4 h-[600px]">
        {/* Canvas area */}
        <div className="relative flex-1 min-w-0 bg-gray-950/60 border border-gray-700/30 rounded-lg overflow-hidden">
          {loading ? (
            <div className="absolute inset-0 flex flex-col items-center justify-center text-center px-6" data-testid="graph-loading">
              <RefreshCw size={26} className="text-emerald-500 animate-spin mb-3" />
              <p className="text-gray-300 text-sm">Building graph…</p>
              <p className="text-gray-500 text-xs mt-1">This can take ~30s (up to ~148s under load) — the server runs wiki-lint detectors.</p>
            </div>
          ) : error ? (
            <div className="absolute inset-0 flex flex-col items-center justify-center text-center px-6" data-testid="graph-error">
              <X size={28} className="text-red-500 mb-3" />
              <p className="text-red-400 text-sm">{error}</p>
              <button onClick={fetchGraph} className="mt-3 text-emerald-400 text-xs hover:underline">Retry</button>
            </div>
          ) : !hasGraph ? (
            <div className="absolute inset-0 flex flex-col items-center justify-center text-center px-6" data-testid="graph-empty">
              <Brain size={32} className="text-gray-600 mb-3" />
              <p className="text-gray-500 text-sm">No graph for this scope.</p>
              <p className="text-gray-600 text-xs mt-1">
                Scope <code className="text-emerald-400">{scope}</code>{scopeId ? <> / <code className="text-emerald-400">{scopeId}</code></> : null} has no topics yet.
              </p>
            </div>
          ) : null}
          {/* Canvas is always mounted (but empty until Sigma attaches) so the
              ref exists for the mount effect. Overlays above cover it. */}
          <div ref={containerRef} data-testid="graph-canvas" className="absolute inset-0" />
        </div>

        {/* Side panel: click-to-read. Content renders as PLAIN TEXT only —
            memory bodies are untrusted agent output (matches MemoryPanel). */}
        <aside className="w-80 shrink-0 flex flex-col bg-gray-950/60 border border-gray-700/30 rounded-lg overflow-hidden">
          {selectedNode ? (
            <>
              <div className="px-4 py-3 border-b border-gray-700/30">
                <div className="text-sm font-semibold text-gray-200 break-all">{selectedNode}</div>
                {detail && detail.id === selectedNode && (
                  <div className="text-xs text-gray-500 mt-1">
                    {detail.data.memory_type}
                    {detail.data.updated_at ? ` · updated ${new Date(detail.data.updated_at).toLocaleString()}` : ''}
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
                  <div className="text-gray-500 text-sm">Loading “{selectedNode}”…</div>
                )}
              </div>
            </>
          ) : (
            <div className="flex-1 flex items-center justify-center text-center px-6">
              <p className="text-gray-500 text-sm">Click a node in the graph to read that memory.</p>
            </div>
          )}
        </aside>
      </div>

      {/* Legend */}
      <div className="flex flex-wrap items-center gap-4 mt-3 text-xs text-gray-500">
        <span className="flex items-center gap-1.5"><span className="w-2.5 h-2.5 rounded-full" style={{ background: DEFAULT_NODE_COLOR }} /> topic</span>
        <span className="flex items-center gap-1.5"><span className="w-2.5 h-2.5 rounded-full" style={{ background: ORPHAN_COLOR }} /> orphan</span>
        <span className="flex items-center gap-1.5"><span className="w-3.5 h-3.5 rounded-full" style={{ background: DEFAULT_NODE_COLOR }} /> larger = hub</span>
        <span className="flex items-center gap-1.5"><span className="inline-block w-3.5 h-0.5" style={{ background: CONTRADICTION_COLOR }} /> contradiction edge</span>
      </div>
    </div>
  )
}

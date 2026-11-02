// FleetWorkers — the `cao worker list` / `cao worker status` view of a CAO
// cluster, collapsed by default at the bottom of Home. Read-only: releasing a
// worker or shutting the fleet down stays a CLI action. Fetches only while open
// (polling every 10s), and renders a 503 as "no fleet reachable" since most
// installs have no cluster at all.

import { useEffect, useState } from 'react'
import { ChevronDown, ChevronRight, Loader2, Server } from 'lucide-react'
import { api, FleetWorker, FleetWorkerDetail } from '../api'

const POLL_MS = 10000

// A worker's last output is a raw terminal capture: colour codes, cursor moves
// and title sequences (OSC). Strip them so the <pre> shows the text, not escapes.
export function stripAnsi(text: string): string {
  return text
    .replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g, '')
    .replace(/\x1b\[[0-9;?]*[ -/]*[@-~]/g, '')
    .replace(/\x1b[@-_]/g, '')
}

export function FleetWorkers() {
  const [open, setOpen] = useState(false)
  const [showAll, setShowAll] = useState(false)
  const [workers, setWorkers] = useState<FleetWorker[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [detail, setDetail] = useState<FleetWorkerDetail | null>(null)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [loadingId, setLoadingId] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    let cancelled = false
    const load = () =>
      api.listFleetWorkers(showAll)
        .then(r => { if (!cancelled) { setWorkers(r.workers); setError(null) } })
        .catch((e: any) => {
          if (cancelled) return
          setWorkers(null)
          setError(e?.status === 503
            ? `No fleet reachable: ${e?.detail || 'no broker configured'}`
            : e?.detail || e?.message || 'Failed to load fleet workers')
        })
    load()
    const id = setInterval(load, POLL_MS)
    return () => { cancelled = true; clearInterval(id) }
  }, [open, showAll])

  const showDetail = async (workerId: string) => {
    setLoadingId(workerId)
    setDetailError(null)
    try {
      setDetail(await api.getFleetWorker(workerId))
    } catch (e: any) {
      setDetail(null)
      setDetailError(e?.detail || e?.message || `Failed to load worker ${workerId}`)
    } finally {
      setLoadingId(null)
    }
  }

  return (
    <section className="rounded-xl border border-gray-800 bg-gray-900/40 p-3">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 text-sm font-semibold text-gray-300"
      >
        {open ? <ChevronDown size={16} aria-hidden="true" /> : <ChevronRight size={16} aria-hidden="true" />}
        <Server size={15} aria-hidden="true" /> Fleet workers
      </button>

      {open && (
        <div className="mt-3 space-y-3">
          <label className="inline-flex items-center gap-2 text-xs text-gray-400">
            <input type="checkbox" checked={showAll} onChange={e => setShowAll(e.target.checked)} />
            Include settled leases
          </label>

          {error ? (
            <p className="text-sm text-gray-500" role="status">{error}</p>
          ) : workers === null ? (
            <Loader2 size={16} className="animate-spin text-gray-500" aria-label="Loading" />
          ) : workers.length === 0 ? (
            <p className="text-sm text-gray-500">{showAll ? 'No workers.' : 'No live workers.'}</p>
          ) : (
            <table className="w-full text-left text-xs">
              <thead className="text-gray-500">
                <tr>
                  <th className="py-1 pr-3 font-medium">Worker</th>
                  <th className="py-1 pr-3 font-medium">State</th>
                  <th className="py-1 pr-3 font-medium">Agent</th>
                  <th className="py-1 pr-3 font-medium">Provider</th>
                  <th className="py-1 pr-3 font-medium">Age</th>
                  <th className="py-1 pr-3 font-medium">Reason</th>
                  <th className="py-1" />
                </tr>
              </thead>
              <tbody className="text-gray-300">
                {workers.map(w => (
                  <tr key={w.worker_id} className="border-t border-gray-800">
                    <td className="py-1 pr-3 font-mono">{w.worker_id}</td>
                    <td className="py-1 pr-3">{w.state}{w.cleanup_pending ? ' (cleanup pending)' : ''}</td>
                    <td className="py-1 pr-3">{w.agent_profile || 'N/A'}</td>
                    <td className="py-1 pr-3">{w.provider || 'N/A'}</td>
                    <td className="py-1 pr-3">{w.age_seconds != null ? `${w.age_seconds}s` : ''}</td>
                    <td className="py-1 pr-3 text-gray-500">{w.reason || ''}</td>
                    <td className="py-1 text-right">
                      <button
                        type="button"
                        onClick={() => showDetail(w.worker_id)}
                        disabled={loadingId !== null}
                        className="rounded border border-gray-700 px-2 py-0.5 text-gray-300 hover:bg-gray-800 disabled:opacity-40"
                      >
                        {loadingId === w.worker_id ? 'Loading…' : 'Details'}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {detailError && <p className="text-xs text-red-400" role="alert">{detailError}</p>}
          {detail && (
            <div className="rounded-lg border border-gray-800 bg-gray-950 p-3 text-xs text-gray-300 space-y-1">
              <p><span className="text-gray-500">Worker:</span> <span className="font-mono">{detail.worker_id}</span></p>
              <p>
                <span className="text-gray-500">Lease:</span>{' '}
                {detail.lease
                  ? `${detail.lease.state} (${detail.lease.age_seconds ?? '?'}s)${detail.lease.reason ? ` — ${detail.lease.reason}` : ''}`
                  : "not in the broker's ledger (it may have restarted)"}
              </p>
              {detail.terminal && (
                <p>
                  <span className="text-gray-500">Terminal:</span>{' '}
                  <span className="font-mono">{detail.terminal.id}</span>{' '}
                  {detail.terminal.agent_profile || ''} · {detail.terminal.status || 'unknown'}
                </p>
              )}
              {detail.terminal_error && <p className="text-amber-300">{detail.terminal_error}</p>}
              {detail.last_output && (
                <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-gray-900 p-2 text-gray-200">
                  {stripAnsi(detail.last_output)}
                </pre>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  )
}

// HealthCard — a small read-only card for the Home tab: GET /health — terminal
// backend, per-component probes and the server version (the `cao --version` /
// `curl /health` checks, without a shell).

import { useEffect, useState } from 'react'
import { Server } from 'lucide-react'
import { api, HealthInfo } from '../api'

export function HealthCard() {
  const [health, setHealth] = useState<HealthInfo | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    api.getHealth()
      .then(h => { if (!cancelled && h && typeof h === 'object') { setHealth(h); setError(null) } })
      .catch((e: any) => { if (!cancelled) setError(e?.detail || e?.message || 'Health check failed') })
    return () => { cancelled = true }
  }, [])

  return (
    <div className="rounded-xl border border-gray-700/50 bg-gray-900/40 p-4" aria-label="Server health">
      <div className="flex items-center gap-2 text-sm font-semibold text-gray-300">
        <Server size={15} aria-hidden="true" /> Server
        {health?.version && <span className="ml-auto font-mono text-xs text-gray-500">v{health.version}</span>}
      </div>
      {error ? (
        <p className="mt-2 text-xs text-red-400" role="alert">{error}</p>
      ) : !health ? (
        <p className="mt-2 text-xs text-gray-500">Checking…</p>
      ) : (
        <div className="mt-2 space-y-1 text-xs">
          {health.terminal_backend && (
            <p className="text-gray-400">
              Terminal backend: <span className="font-mono text-gray-200">{health.terminal_backend}</span>
            </p>
          )}
          <div className="flex flex-wrap gap-1.5">
            {Object.entries(health.components && typeof health.components === 'object' ? health.components : {}).map(([name, state]) => (
              <span
                key={name}
                className={`rounded px-1.5 py-0.5 font-mono ${state === 'ok' ? 'bg-emerald-900/40 text-emerald-300' : 'bg-red-900/40 text-red-300'}`}
              >
                {name}: {state}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

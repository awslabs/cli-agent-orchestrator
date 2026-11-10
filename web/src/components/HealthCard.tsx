// HealthCard — a small read-only card for the Home tab: GET /health — terminal
// backend, per-component probes and the server version (the `cao --version` /
// `curl /health` checks, without a shell).

import { useEffect, useState } from 'react'
import { Server } from 'lucide-react'
import { api, HealthInfo } from '../api'

// The server probes each binary with `which`; `unavailable` only means it isn't
// on PATH (herdr on a tmux host, a provider CLI nobody installed), which is not a
// fault. Shown neutral, so red stays for components that actually failed.
function componentChip(state: unknown): { className: string; label: string } {
  if (state === 'ok') return { className: 'bg-emerald-900/40 text-emerald-300', label: 'ok' }
  if (state === 'unavailable') return { className: 'bg-gray-800 text-gray-400', label: 'not installed' }
  return { className: 'bg-red-900/40 text-red-300', label: String(state) }
}

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
            {Object.entries(health.components && typeof health.components === 'object' ? health.components : {}).map(([name, state]) => {
              const chip = componentChip(state)
              return (
                <span key={name} className={`rounded px-1.5 py-0.5 font-mono ${chip.className}`}>
                  {name}: {chip.label}
                </span>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}

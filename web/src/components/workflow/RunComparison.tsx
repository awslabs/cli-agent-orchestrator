// RunComparison — side-by-side compare of the selected run against another
// (#504 / U8, FR-8). Consumes the U6 compare API. Added/removed steps are made
// EXPLICIT (never silently dropped, BR-1) with a non-color status cue and an SR
// label on each delta. The compare target is chosen from the other runs via the
// reused CustomSelect.

import { useState } from 'react'
import { GitCompare, Loader2, Plus, Minus, Equal } from 'lucide-react'
import { api, type RunComparison as RunComparisonData, type RunSummaryRow, type ApiError } from '../../api'
import { CustomSelect } from '../CustomSelect'

interface RunComparisonProps {
  runId: string
  otherRuns: RunSummaryRow[]
}

const STATUS_CUE: Record<string, { label: string; Icon: typeof Plus; color: string; glyph: string; sr: string }> = {
  aligned: { label: 'Aligned', Icon: Equal, color: 'text-gray-400', glyph: '=', sr: 'aligned in both runs' },
  added: { label: 'Added', Icon: Plus, color: 'text-emerald-400', glyph: '+', sr: 'added: present only in the compare run' },
  removed: { label: 'Removed', Icon: Minus, color: 'text-red-400', glyph: '−', sr: 'removed: present only in the baseline run' },
}

export function RunComparison({ runId, otherRuns }: RunComparisonProps) {
  const [against, setAgainst] = useState('')
  const [data, setData] = useState<RunComparisonData | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const options = otherRuns
    .filter(r => r.run_id !== runId)
    .map(r => ({ value: r.run_id, label: `${r.workflow_name} · ${r.run_id.slice(0, 8)}`, sublabel: r.state }))

  const runCompare = async (target: string) => {
    setAgainst(target)
    if (!target) {
      setData(null)
      return
    }
    setLoading(true)
    setError(null)
    try {
      const result = await api.compareWorkflowRuns(runId, target)
      setData(result)
    } catch (e) {
      const err = e as ApiError
      setError(err?.detail || err?.message || 'Comparison failed')
      setData(null)
    } finally {
      setLoading(false)
    }
  }

  return (
    <section aria-label="Run comparison" className="space-y-3">
      <div className="flex items-center gap-2">
        <GitCompare size={16} className="text-gray-400 shrink-0" aria-hidden="true" />
        <span className="text-sm text-gray-300">Compare against</span>
        <div className="min-w-[16rem]">
          <CustomSelect
            value={against}
            onChange={runCompare}
            options={options}
            placeholder={options.length ? 'Select a run…' : 'No other runs'}
          />
        </div>
      </div>

      {loading && (
        <div className="flex items-center gap-2 text-xs text-gray-500" role="status">
          <Loader2 size={14} className="animate-spin" aria-hidden="true" />
          Comparing…
        </div>
      )}
      {error && !loading && (
        <div className="text-xs text-red-400" role="alert">
          {error}
        </div>
      )}

      {data && !loading && (
        <table className="w-full text-xs" aria-label="Aligned step comparison">
          <thead>
            <tr className="text-gray-500 text-left">
              <th className="py-1 font-medium">Step</th>
              <th className="py-1 font-medium">Status</th>
              <th className="py-1 font-medium">Baseline</th>
              <th className="py-1 font-medium">Compare</th>
            </tr>
          </thead>
          <tbody>
            {data.steps.map(step => {
              const cue = STATUS_CUE[step.status] ?? STATUS_CUE.aligned
              return (
                <tr key={step.step_id} className="border-t border-gray-800/60">
                  <td className="py-1.5 font-mono text-gray-300">{step.step_id}</td>
                  <td className="py-1.5">
                    <span className={`inline-flex items-center gap-1 ${cue.color}`}>
                      <cue.Icon size={13} aria-hidden="true" />
                      <span aria-hidden="true">{cue.glyph}</span>
                      <span>{cue.label}</span>
                      <span className="sr-only">{cue.sr}</span>
                    </span>
                  </td>
                  <td className="py-1.5 text-gray-400">
                    {step.a ? `${step.a.state} · ${step.a.attempts}×` : <span className="text-gray-600">absent</span>}
                  </td>
                  <td className="py-1.5 text-gray-400">
                    {step.b ? `${step.b.state} · ${step.b.attempts}×` : <span className="text-gray-600">absent</span>}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </section>
  )
}

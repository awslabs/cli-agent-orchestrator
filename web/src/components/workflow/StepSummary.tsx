// StepSummary — the durable per-step projection of the selected run (#504 / U8,
// FR-5.1). Reads RunInspection.steps; each row carries the step's derived-cue
// state (icon + label + glyph, non-color) plus attempts and any structured
// error_kind. Read-only.

import type { RunInspection } from '../../api'
import { runCue } from './stateCues'

interface StepSummaryProps {
  run: RunInspection
}

export function StepSummary({ run }: StepSummaryProps) {
  if (run.steps.length === 0) {
    return <p className="text-xs text-gray-500">No steps recorded for this run.</p>
  }
  return (
    <table className="w-full text-xs" aria-label="Step summary">
      <thead>
        <tr className="text-gray-500 text-left">
          <th className="py-1 font-medium">Step</th>
          <th className="py-1 font-medium">State</th>
          <th className="py-1 font-medium">Attempts</th>
          <th className="py-1 font-medium">Error kind</th>
        </tr>
      </thead>
      <tbody>
        {run.steps.map(step => {
          const cue = runCue(step.state)
          return (
            <tr key={step.id} className="border-t border-gray-800/60">
              <td className="py-1.5 font-mono text-gray-300">{step.id}</td>
              <td className="py-1.5">
                <span className={`inline-flex items-center gap-1 ${cue.color}`}>
                  <cue.Icon size={13} aria-hidden="true" />
                  <span aria-hidden="true">{cue.glyph}</span>
                  <span>{cue.label}</span>
                  <span className="sr-only">{cue.sr}</span>
                </span>
              </td>
              <td className="py-1.5 text-gray-400 tabular-nums">{step.attempts}</td>
              <td className="py-1.5 text-gray-400 font-mono">{step.error_kind || '—'}</td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

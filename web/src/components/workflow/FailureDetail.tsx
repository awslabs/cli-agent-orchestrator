// FailureDetail — the forensic summary for a failed run (#504 / U8, FR-7.2).
// From the durable read (cold-read capable): the last step that changed state,
// its failing attempt count, the structured error_kind, and the owning terminal
// id. Renders nothing for a non-failed run.

import { AlertOctagon } from 'lucide-react'
import type { RunInspection } from '../../api'

interface FailureDetailProps {
  run: RunInspection
}

export function FailureDetail({ run }: FailureDetailProps) {
  if (run.state !== 'failed') return null

  // The failing step: prefer a step whose own state is `failed`; else the
  // current_step_id the run stopped on; else the last step in the projection.
  const failedStep =
    run.steps.find(s => s.state === 'failed') ??
    run.steps.find(s => s.id === run.current_step_id) ??
    run.steps[run.steps.length - 1]

  if (!failedStep) {
    return (
      <section
        aria-label="Failure detail"
        className="rounded-lg border border-red-700/40 bg-red-950/20 p-3 text-xs text-red-300"
      >
        <div className="flex items-center gap-2">
          <AlertOctagon size={14} aria-hidden="true" />
          Run failed — no per-step detail was recorded.
        </div>
      </section>
    )
  }

  return (
    <section
      aria-label="Failure detail"
      className="rounded-lg border border-red-700/40 bg-red-950/20 p-3 space-y-1.5"
    >
      <div className="flex items-center gap-2 text-sm font-medium text-red-300">
        <AlertOctagon size={16} aria-hidden="true" />
        Run failed
      </div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
        <dt className="text-gray-500">Last step</dt>
        <dd className="font-mono text-gray-300">{failedStep.id}</dd>
        <dt className="text-gray-500">Failing attempt</dt>
        <dd className="text-gray-300 tabular-nums">{failedStep.attempts}</dd>
        <dt className="text-gray-500">Error kind</dt>
        <dd className="font-mono text-red-300">{failedStep.error_kind || 'unknown'}</dd>
        <dt className="text-gray-500">Terminal</dt>
        <dd className="font-mono text-gray-300">{failedStep.terminal_id || '—'}</dd>
      </dl>
    </section>
  )
}

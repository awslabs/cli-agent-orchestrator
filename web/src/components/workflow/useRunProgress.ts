// useRunProgress: each listed run's step progress, such as "2 of 3", for the
// Workflows home. The runs list route (RunSummaryRow) carries no step counts,
// so this reads each run's inspection, at most MAX_IN_FLIGHT at a time. A run
// that has ended is read once and kept until its list row changes (a resume
// changes its state). A live run is read again on every list change and every
// PROGRESS_REFRESH_MS while any run is live. A failed read leaves the run
// without progress, and the next round tries again.

import { useEffect, useRef, useState } from 'react'
import { api, type RunSummaryRow } from '../../api'
import { stepProgress, type StepProgress } from './runPage'
import { TERMINAL_RUN_STATES } from './RunDetail'

/** How many inspections are read at the same time. */
export const MAX_IN_FLIGHT = 4
/** How often live runs are read again; the same cadence as the list poll. */
export const PROGRESS_REFRESH_MS = 10000

const rowKey = (row: RunSummaryRow) => `${row.state}|${row.finished_at ?? ''}`

export function useRunProgress(runs: RunSummaryRow[]): Record<string, StepProgress> {
  const [progress, setProgress] = useState<Record<string, StepProgress>>({})
  // The list row each run's progress was read for.
  const readFor = useRef(new Map<string, string>())
  const [round, setRound] = useState(0)

  const anyLive = runs.some(r => !TERMINAL_RUN_STATES.has(r.state))
  useEffect(() => {
    if (!anyLive) return
    const timer = setInterval(() => setRound(n => n + 1), PROGRESS_REFRESH_MS)
    return () => clearInterval(timer)
  }, [anyLive])

  useEffect(() => {
    let cancelled = false
    const todo = runs.filter(r => !TERMINAL_RUN_STATES.has(r.state) || readFor.current.get(r.run_id) !== rowKey(r))
    let next = 0
    const worker = async () => {
      while (!cancelled && next < todo.length) {
        const row = todo[next++]
        try {
          const inspection = await api.inspectWorkflowRun(row.run_id)
          if (cancelled) return
          readFor.current.set(row.run_id, rowKey(row))
          const value = stepProgress(inspection.steps ?? [])
          setProgress(prev => {
            const old = prev[row.run_id]
            return old && old.reached === value.reached && old.total === value.total ? prev : { ...prev, [row.run_id]: value }
          })
        } catch {
          // No progress for this run this round.
        }
      }
    }
    for (let i = 0; i < Math.min(MAX_IN_FLIGHT, todo.length); i++) void worker()
    return () => {
      cancelled = true
    }
  }, [runs, round])

  return progress
}

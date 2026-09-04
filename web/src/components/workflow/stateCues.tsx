// stateCues — non-color visual cues for run/step state and the declared gap
// marker (WCAG 2.1 AA, SC 1.4.1 Use of Color). Every state carries THREE
// independent, non-color cues so the surface never relies on color alone:
//   1. a distinct lucide ICON (shape),
//   2. a text LABEL, and
//   3. a screen-reader phrase (`sr`) / glyph token used in text lists + charts.
// Color is applied on top as a fourth, redundant channel — never the sole cue.

import {
  Circle,
  Loader2,
  CheckCircle2,
  RotateCcw,
  SkipForward,
  XCircle,
  Ban,
  AlertTriangle,
  type LucideIcon,
} from 'lucide-react'
import type { DerivedStepState } from './derivedStepState'

export interface StateCue {
  label: string
  Icon: LucideIcon
  /** Screen-reader phrase, also used as the text-only glyph in dense lists. */
  sr: string
  /** Redundant color classes (never the sole cue). */
  color: string
  /** ASCII glyph for text-only / chart contexts (a third non-color channel). */
  glyph: string
}

export const STEP_STATE_CUES: Record<DerivedStepState, StateCue> = {
  pending: { label: 'Pending', Icon: Circle, sr: 'pending', color: 'text-gray-400', glyph: '○' },
  running: { label: 'Running', Icon: Loader2, sr: 'running', color: 'text-blue-400', glyph: '▶' },
  completed: {
    label: 'Completed',
    Icon: CheckCircle2,
    sr: 'completed',
    color: 'text-emerald-400',
    glyph: '✓',
  },
  retried: {
    label: 'Retried',
    Icon: RotateCcw,
    sr: 'retried after a failed attempt',
    color: 'text-amber-400',
    glyph: '↻',
  },
  skipped: {
    label: 'Skipped',
    Icon: SkipForward,
    sr: 'skipped',
    color: 'text-gray-500',
    glyph: '»',
  },
  failed: { label: 'Failed', Icon: XCircle, sr: 'failed', color: 'text-red-400', glyph: '✗' },
  cancelled: { label: 'Cancelled', Icon: Ban, sr: 'cancelled', color: 'text-orange-400', glyph: '⊘' },
}

/** The declared-gap cue — a first-class state distinct from empty (BR-4). */
export const GAP_CUE: StateCue = {
  label: 'Declared gap',
  Icon: AlertTriangle,
  sr: 'declared gap in the event sequence — one or more events are missing',
  color: 'text-yellow-400',
  glyph: '⋯',
}

export function stepCue(state: DerivedStepState): StateCue {
  return STEP_STATE_CUES[state] ?? STEP_STATE_CUES.pending
}

/** Map a run-level state string to a step cue (shared taxonomy; safe fallback). */
export function runCue(state: string): StateCue {
  const key = (state || '').toLowerCase() as DerivedStepState
  return STEP_STATE_CUES[key] ?? STEP_STATE_CUES.pending
}

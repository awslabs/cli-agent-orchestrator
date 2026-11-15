// StepOutput: what a step answered, with the rules the run page's Output tab
// and the Result panel share.
//
// Output shows what the step answered: its structured output (output_json),
// pretty-printed, when it has one, and otherwise its saved final answer
// (result.text), with a badge when the server truncated the answer or redacted
// secrets from it. RunResultPanel shows the last step's output with the same
// rules, and the run page's console (StepConsole) shows it in its Output tab.

import type { StepInspection } from '../../api'
import { Badge } from './StepTerminal'
import { stepActivity } from './runView'

/** A step's structured output as pretty JSON, or as stored when it is not valid JSON. */
export function structuredOutputText(outputJson: string): string {
  try {
    return JSON.stringify(JSON.parse(outputJson), null, 2)
  } catch {
    return outputJson
  }
}

export interface StepOutputProps {
  /** The step's inspection row, or null when the inspection has no row for it. */
  step: StepInspection | null
  /** The step's live state, as the Flow shows it. */
  state: string
  /** True once the run has ended. */
  runEnded: boolean
}

/** What a step answered, or why there is nothing to show yet. */
export function StepOutput({ step, state, runEnded }: StepOutputProps) {
  const activity = stepActivity(state, runEnded)
  const structured = step?.output_json ? structuredOutputText(step.output_json) : null
  const result = step?.result ?? null
  const answer = result && typeof result.text === 'string' && result.text !== '' ? result.text : null

  if (structured != null) {
    return (
      <div className="space-y-1" data-testid="step-output">
        <p className="text-[11px] text-gray-500">Structured output</p>
        <pre
          tabIndex={0}
          aria-label="Structured output"
          className="max-h-80 overflow-auto rounded-sm bg-gray-950 p-2 font-mono text-[11px] text-gray-300"
        >
          {structured}
        </pre>
      </div>
    )
  }
  if (answer != null) {
    return (
      <div className="space-y-1" data-testid="step-output">
        <div className="flex flex-wrap items-center gap-2">
          <p className="text-[11px] text-gray-500">Final answer</p>
          {result?.truncated && <Badge>Truncated</Badge>}
          {result?.redacted && <Badge>Secrets redacted</Badge>}
        </div>
        <pre
          tabIndex={0}
          aria-label="Final answer"
          className="max-h-80 overflow-auto whitespace-pre-wrap wrap-break-word rounded-sm bg-gray-950 p-2 font-mono text-[11px] text-gray-300"
        >
          {answer}
        </pre>
      </div>
    )
  }
  const empty =
    activity === 'running' ? 'Still running' : activity === 'pending' ? 'This step has not started.' : 'No answer was recorded'
  return (
    <p className="text-xs text-gray-500" role="status" data-testid="step-output">
      {empty}
    </p>
  )
}

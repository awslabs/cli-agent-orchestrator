// Two notes in the workflow builder: the warning on a param value that starts
// or ends with a space (never trimmed, never blocking a save), and the note in
// Edit mode that changes apply to new runs only, shown when the workflow has
// runs.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useState } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { api } from '../api'
import type { RunSummaryRow, WorkflowSpec, WorkflowSpecStep } from '../api'
import { useStore } from '../store'
import { StepEditor, paramSpaceWarning } from '../components/workflow/StepEditor'
import { WorkflowBuilder, FUTURE_RUNS_NOTE, RUN_LIST_MAX_LIMIT } from '../components/workflow/WorkflowBuilder'
import { applyStepPatch, newStep, readDeclaredParams, type BuilderStep } from '../components/workflow/workflowYaml'

const SPEC_NULLS = { engine: null, output_schema: null, when: null, until: null, max_iterations: null, on_stall: null, on_no_progress: null }

function specStep(over: Partial<WorkflowSpecStep> = {}): WorkflowSpecStep {
  return {
    id: 'collect', provider: 'claude_code', agent: 'orders-reader', prompt: 'Count the rows.', call: null,
    params: null, retries: null, on_failure: null, ...SPEC_NULLS, ...over,
  }
}

function spec(step: Partial<WorkflowSpecStep> = {}): WorkflowSpec {
  return {
    name: 'nightly-check', description: 'Nightly check', mode: 'sequential', inputs: {},
    steps: [specStep(step)], path: '/wf/nightly-check.yaml', source: '"name": "nightly-check"\n', content_hash: 'hash-1',
  }
}

function runRow(workflow_name: string, run_id = `${workflow_name}-run`): RunSummaryRow {
  return { run_id, workflow_name, state: 'completed', tier: 'yaml', started_at: '2026-11-10T10:00:00Z', finished_at: '2026-11-10T10:01:00Z', current_step_id: null }
}

// A stateful host for StepEditor.
function StepHarness({ initial }: { initial: BuilderStep }) {
  const [step, setStep] = useState<BuilderStep>(initial)
  return (
    <>
      <StepEditor step={step} index={0} providers={[]} onChange={patch => setStep(s => applyStepPatch(s, patch))} />
      <pre data-testid="step-state">{JSON.stringify(step)}</pre>
    </>
  )
}
const current = (): BuilderStep => JSON.parse(screen.getByTestId('step-state').textContent as string)

/** Let pending mocked responses land inside act. */
async function settle() {
  await act(async () => {
    await new Promise(resolve => setTimeout(resolve, 0))
  })
}

beforeEach(() => {
  localStorage.clear()
  useStore.setState({ snackbar: null, navLockCount: 0 })
  // The builder's Save validates first.
  vi.spyOn(api, 'validateWorkflowSource').mockResolvedValue({ status: 'pass', errors: [] })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

// ── 5. Param spaces ───────────────────────────────────────────────────────

describe('the param whitespace warning', () => {
  it('names where the spaces are, and says nothing for a value without them', () => {
    expect(paramSpaceWarning(' orders')).toBe('This value starts with a space.')
    expect(paramSpaceWarning('orders ')).toBe('This value ends with a space.')
    expect(paramSpaceWarning(' orders ')).toBe('This value starts and ends with a space.')
    expect(paramSpaceWarning('   ')).toBe('This value starts and ends with a space.')
    // A tab or a non-breaking space pasted from a document is as hard to see.
    expect(paramSpaceWarning('\torders')).toBe('This value starts with a space.')
    expect(paramSpaceWarning('orders\u00a0')).toBe('This value ends with a space.')
    expect(paramSpaceWarning('order items')).toBeNull()
    expect(paramSpaceWarning('orders')).toBeNull()
    expect(paramSpaceWarning('')).toBeNull()
  })

  it('warns under a free-form value as it is typed, and keeps the value exactly as typed', () => {
    render(<StepHarness initial={{ ...newStep('step-1'), params: [{ key: 'table', value: 'orders' }] }} />)
    const input = screen.getByLabelText('Step 1 param 1 value')
    expect(screen.queryByTestId('param-space-warning')).toBeNull()
    expect(input).not.toHaveAttribute('aria-describedby')

    fireEvent.change(input, { target: { value: ' orders' } })
    expect(screen.getByTestId('param-space-warning')).toHaveTextContent('This value starts with a space.')
    fireEvent.change(input, { target: { value: 'orders ' } })
    expect(screen.getByTestId('param-space-warning')).toHaveTextContent('This value ends with a space.')
    fireEvent.change(input, { target: { value: ' orders ' } })
    const warning = screen.getByTestId('param-space-warning')
    expect(warning).toHaveTextContent('This value starts and ends with a space.')
    // Not trimmed: the field and the step keep the spaces.
    expect(input).toHaveValue(' orders ')
    expect(current().params[0].value).toBe(' orders ')
    // The warning is tied to the field for screen readers.
    expect(input).toHaveAttribute('aria-describedby', warning.id)

    fireEvent.change(input, { target: { value: 'orders' } })
    expect(screen.queryByTestId('param-space-warning')).toBeNull()
  })

  it('warns under a declared param too, next to its description', () => {
    const params = readDeclaredParams({ table_name: { type: 'string', required: true, description: 'Table to read' } })
    const initial: BuilderStep = {
      ...newStep('step-1'),
      agent: 'orders-reader',
      declared: { status: 'ready', params, values: ['example-table '] },
    }
    render(<StepHarness initial={initial} />)
    const input = screen.getByLabelText('Step 1 param table_name')
    const warning = screen.getByTestId('param-space-warning')
    expect(warning).toHaveTextContent('This value ends with a space.')
    expect(input.getAttribute('aria-describedby')?.split(' ')).toContain(warning.id)
    expect(input).toHaveValue('example-table ')
  })

  it('never blocks saving, and saves the value with its spaces', async () => {
    vi.spyOn(api, 'getWorkflowSpec').mockResolvedValue(spec({ params: { table: ' orders ' } }))
    vi.spyOn(api, 'getProfile').mockResolvedValue({ name: 'orders-reader', description: '', provider: 'claude_code' })
    vi.spyOn(api, 'listProviders').mockResolvedValue([])
    vi.spyOn(api, 'listWorkflowRuns').mockResolvedValue([])
    const update = vi.spyOn(api, 'updateWorkflow').mockResolvedValue({ name: 'nightly-check', path: '/wf/nightly-check.yaml', content_hash: 'hash-2', findings: [] })
    const onSaved = vi.fn()
    render(<WorkflowBuilder mode="edit" name="nightly-check" onClose={() => {}} onSaved={onSaved} />)
    expect(await screen.findByTestId('param-space-warning')).toHaveTextContent('This value starts and ends with a space.')
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('nightly-check', expect.objectContaining({ content_hash: 'hash-2' })))
    expect(update.mock.calls[0][1]).toContain('      "table": " orders "\n')
    expect(screen.queryByText(/Fix the .*marked in the form first/)).toBeNull()
  })
})

// ── 6. The future-runs note ───────────────────────────────────────────────

describe('the future-runs note', () => {
  beforeEach(() => {
    vi.spyOn(api, 'getWorkflowSpec').mockResolvedValue(spec())
    vi.spyOn(api, 'getProfile').mockResolvedValue({ name: 'orders-reader', description: '', provider: 'claude_code' })
    vi.spyOn(api, 'listProviders').mockResolvedValue([])
  })

  it('shows in Edit mode for a workflow with runs, from the widest page of the runs list', async () => {
    const list = vi.spyOn(api, 'listWorkflowRuns').mockResolvedValue([runRow('other-flow'), runRow('nightly-check')])
    render(<WorkflowBuilder mode="edit" name="nightly-check" onClose={() => {}} onSaved={() => {}} />)
    const note = await screen.findByRole('note', { name: 'Changes apply to new runs only' })
    expect(note).toHaveTextContent(FUTURE_RUNS_NOTE)
    expect(FUTURE_RUNS_NOTE).toBe(
      'Changes apply to new runs only. Runs that already started, including runs you resume, keep the version they started with.',
    )
    expect(list).toHaveBeenCalledWith(RUN_LIST_MAX_LIMIT)
    expect(RUN_LIST_MAX_LIMIT).toBe(500)
  })

  it('does not show for a workflow without runs', async () => {
    vi.spyOn(api, 'listWorkflowRuns').mockResolvedValue([runRow('other-flow')])
    render(<WorkflowBuilder mode="edit" name="nightly-check" onClose={() => {}} onSaved={() => {}} />)
    await screen.findByTestId('builder-fixed-name')
    await settle()
    expect(screen.queryByTestId('builder-future-runs-note')).toBeNull()
  })

  it('does not show when the runs list cannot be read', async () => {
    vi.spyOn(api, 'listWorkflowRuns').mockRejectedValue(new Error('500 Error'))
    render(<WorkflowBuilder mode="edit" name="nightly-check" onClose={() => {}} onSaved={() => {}} />)
    await screen.findByTestId('builder-fixed-name')
    await settle()
    expect(screen.queryByTestId('builder-future-runs-note')).toBeNull()
  })

  it('does not show in Create mode, which does not read the runs', async () => {
    const list = vi.spyOn(api, 'listWorkflowRuns').mockResolvedValue([runRow('nightly-check')])
    render(<WorkflowBuilder mode="create" onClose={() => {}} onSaved={() => {}} />)
    await settle()
    fireEvent.change(screen.getByLabelText('Workflow name'), { target: { value: 'nightly-check' } })
    await settle()
    expect(screen.queryByTestId('builder-future-runs-note')).toBeNull()
    expect(list).not.toHaveBeenCalled()
  })
})

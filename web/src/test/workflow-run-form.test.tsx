// Tests for the run-with-inputs surface (issue #789, PR A): the generated
// RunWorkflowForm (typed controls, submit spine, the four error stories) and
// the WorkflowCatalog (list, select, admin-gated delete).

import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { RunWorkflowForm } from '../components/workflow/RunWorkflowForm'
import { WorkflowCatalog } from '../components/workflow/WorkflowCatalog'
import { api } from '../api'
import type { ApiError, WorkflowDetail, WorkflowIndexRow } from '../api'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

/** Build the ApiError shape fetchJSON throws (message + status + detail). */
function apiError(status: number, detail?: string, detailMeta?: Record<string, unknown>): ApiError {
  const err: ApiError = new Error(`${status} error`)
  err.status = status
  err.detail = detail
  err.detailMeta = detailMeta
  return err
}

const specWithInputs: WorkflowDetail = {
  name: 'adsp-ecs-e2e',
  description: 'End-to-end beta smoke test',
  mode: 'sequential',
  inputs: {
    table_name: { type: 'string', required: true, default: null },
    lookback_min: { type: 'int', required: false, default: 30 },
    dry_run: { type: 'bool', required: false, default: null },
    log_dir: { type: 'path', required: false, default: null },
  },
  steps: [],
}

describe('RunWorkflowForm', () => {
  it('generates one typed control per declared input, with defaults pre-filled', async () => {
    vi.spyOn(api, 'getWorkflow').mockResolvedValue(specWithInputs)
    render(<RunWorkflowForm name="adsp-ecs-e2e" onSubmitted={() => {}} onClose={() => {}} />)

    const tableInput = await screen.findByLabelText(/table_name/)
    expect(tableInput).toHaveAttribute('type', 'text')
    // Required with no default -> marked required.
    expect(screen.getByTitle('required')).toBeInTheDocument()

    const intInput = screen.getByLabelText(/lookback_min/)
    expect(intInput).toHaveAttribute('type', 'number')
    expect(intInput).toHaveValue(30)

    expect(screen.getByLabelText(/dry_run/)).toHaveAttribute('type', 'checkbox')
    expect(screen.getByLabelText(/log_dir/)).toHaveAttribute('placeholder', '/path/to/target')

    // The plaintext-journal warning is always shown (cao-workflow skill R2).
    expect(screen.getByText(/do not enter secrets/i)).toBeInTheDocument()
  })

  it('submits typed inputs through runs:submit and reports the accepted run id', async () => {
    vi.spyOn(api, 'getWorkflow').mockResolvedValue(specWithInputs)
    const submit = vi.spyOn(api, 'submitWorkflowRun').mockResolvedValue({
      run_id: 'run-1234', state: 'running', links: {},
    })
    const onSubmitted = vi.fn()
    render(<RunWorkflowForm name="adsp-ecs-e2e" onSubmitted={onSubmitted} onClose={() => {}} />)

    fireEvent.change(await screen.findByLabelText(/table_name/), { target: { value: 'TradingGoals' } })
    fireEvent.click(screen.getByLabelText(/dry_run/))
    fireEvent.click(screen.getByRole('button', { name: /^run$/i }))

    await waitFor(() => expect(onSubmitted).toHaveBeenCalledWith('run-1234'))
    // int coerced to a number, bool a real boolean, empty optional path omitted
    // so the server fills its own default.
    expect(submit).toHaveBeenCalledWith('adsp-ecs-e2e', {
      table_name: 'TradingGoals',
      lookback_min: 30,
      dry_run: true,
    })
  })

  it('blocks a missing required input client-side, with no API call', async () => {
    vi.spyOn(api, 'getWorkflow').mockResolvedValue(specWithInputs)
    const submit = vi.spyOn(api, 'submitWorkflowRun')
    render(<RunWorkflowForm name="adsp-ecs-e2e" onSubmitted={() => {}} onClose={() => {}} />)

    fireEvent.click(await screen.findByRole('button', { name: /^run$/i }))
    expect(await screen.findByText('required')).toBeInTheDocument()
    expect(submit).not.toHaveBeenCalled()
  })

  it('anchors a 400 naming an input next to that field, not as a banner', async () => {
    vi.spyOn(api, 'getWorkflow').mockResolvedValue(specWithInputs)
    vi.spyOn(api, 'submitWorkflowRun').mockRejectedValue(
      apiError(400, "input 'table_name' must be a string"),
    )
    render(<RunWorkflowForm name="adsp-ecs-e2e" onSubmitted={() => {}} onClose={() => {}} />)

    fireEvent.change(await screen.findByLabelText(/table_name/), { target: { value: 'x' } })
    fireEvent.click(screen.getByRole('button', { name: /^run$/i }))

    const message = await screen.findByText("input 'table_name' must be a string")
    // Rendered inside the table_name control's block, not a generic alert.
    expect(message.previousElementSibling).toHaveAttribute('id', 'wf-input-table_name')
  })

  it('renders a scope 403 as a permission story and disables Run', async () => {
    vi.spyOn(api, 'getWorkflow').mockResolvedValue(specWithInputs)
    vi.spyOn(api, 'submitWorkflowRun').mockRejectedValue(
      apiError(403, "Forbidden: requires one of ['cao:write', 'cao:admin']"),
    )
    render(<RunWorkflowForm name="adsp-ecs-e2e" onSubmitted={() => {}} onClose={() => {}} />)

    fireEvent.change(await screen.findByLabelText(/table_name/), { target: { value: 'T' } })
    fireEvent.click(screen.getByRole('button', { name: /^run$/i }))

    expect(await screen.findByText(/lacks the cao:write scope/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^run$/i })).toBeDisabled()
  })

  it('renders an approval 403 with the plan id and approves-then-reruns', async () => {
    vi.spyOn(api, 'getWorkflow').mockResolvedValue(specWithInputs)
    const submit = vi.spyOn(api, 'submitWorkflowRun')
      .mockRejectedValueOnce(apiError(
        403,
        "Plan 'sha256:abc123' has not been approved. Approve this plan identifier and run again.",
      ))
      .mockResolvedValueOnce({ run_id: 'run-2', state: 'running', links: {} })
    const approve = vi.spyOn(api, 'approveWorkflowPlan').mockResolvedValue({
      plan_id: 'sha256:abc123', approved: true, changed: true,
      approved_at: '2026-09-22T00:00:00Z', approved_by: 'sujoydc',
    })
    const onSubmitted = vi.fn()
    render(<RunWorkflowForm name="adsp-ecs-e2e" onSubmitted={onSubmitted} onClose={() => {}} />)

    fireEvent.change(await screen.findByLabelText(/table_name/), { target: { value: 'T' } })
    fireEvent.click(screen.getByRole('button', { name: /^run$/i }))

    expect(await screen.findByText(/plan_id: sha256:abc123/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /approve plan and run again/i }))

    await waitFor(() => expect(onSubmitted).toHaveBeenCalledWith('run-2'))
    expect(approve).toHaveBeenCalledWith('sha256:abc123')
    expect(submit).toHaveBeenCalledTimes(2)
  })

  it('renders a 503 as retry, never as an approval prompt', async () => {
    vi.spyOn(api, 'getWorkflow').mockResolvedValue(specWithInputs)
    vi.spyOn(api, 'submitWorkflowRun').mockRejectedValue(
      apiError(503, "Could not read the approval store for plan 'sha256:abc'. The run was refused."),
    )
    render(<RunWorkflowForm name="adsp-ecs-e2e" onSubmitted={() => {}} onClose={() => {}} />)

    fireEvent.change(await screen.findByLabelText(/table_name/), { target: { value: 'T' } })
    fireEvent.click(screen.getByRole('button', { name: /^run$/i }))

    expect(await screen.findByRole('button', { name: /retry/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /approve plan/i })).not.toBeInTheDocument()
  })

  it('renders 422 script-lint findings with their line and rule id', async () => {
    vi.spyOn(api, 'getWorkflow').mockResolvedValue({ ...specWithInputs, steps: undefined, path: '/x.py' })
    vi.spyOn(api, 'submitWorkflowRun').mockRejectedValue(
      apiError(422, undefined, {
        findings: [
          { line: 7, rule_id: 'missing-recovery-policy', severity: 'error', message: 'step "draft" has no recovery policy' },
        ],
      }),
    )
    render(<RunWorkflowForm name="adsp-ecs-e2e" onSubmitted={() => {}} onClose={() => {}} />)

    fireEvent.change(await screen.findByLabelText(/table_name/), { target: { value: 'T' } })
    fireEvent.click(screen.getByRole('button', { name: /^run$/i }))

    expect(await screen.findByText(/script lint failed/i)).toBeInTheDocument()
    expect(screen.getByText(/line 7: \[missing-recovery-policy\]/)).toBeInTheDocument()
  })
})

const catalogRows: WorkflowIndexRow[] = [
  {
    name: 'adsp-ecs-e2e',
    source_path: '/home/u/.aws/cli-agent-orchestrator/workflows/adsp-ecs-e2e.yaml',
    mode: 'sequential',
    step_count: 5,
    description: 'End-to-end beta smoke test',
    indexed_at: '2026-09-22T00:00:00Z',
  },
  {
    name: 'blogdemo',
    source_path: '/home/u/.aws/cli-agent-orchestrator/workflows/blogdemo.py',
    mode: 'script',
    step_count: null,
    description: '',
    indexed_at: '2026-09-22T00:00:00Z',
  },
]

describe('WorkflowCatalog', () => {
  it('lists saved workflows and fires onSelect with the name', async () => {
    vi.spyOn(api, 'listWorkflows').mockResolvedValue(catalogRows)
    const onSelect = vi.fn()
    render(<WorkflowCatalog selectedName={null} onSelect={onSelect} onDeleted={() => {}} />)

    expect(await screen.findByText('adsp-ecs-e2e')).toBeInTheDocument()
    expect(screen.getByText('5 steps')).toBeInTheDocument()
    // A script workflow has no static step count.
    expect(screen.getByText('dynamic steps')).toBeInTheDocument()

    fireEvent.click(screen.getByText('adsp-ecs-e2e'))
    expect(onSelect).toHaveBeenCalledWith('adsp-ecs-e2e')
  })

  it('deletes only after the type-to-confirm gate, then reports onDeleted', async () => {
    vi.spyOn(api, 'listWorkflows').mockResolvedValue(catalogRows)
    const del = vi.spyOn(api, 'deleteWorkflow').mockResolvedValue({ success: true, name: 'blogdemo' })
    const onDeleted = vi.fn()
    render(<WorkflowCatalog selectedName={null} onSelect={() => {}} onDeleted={onDeleted} />)

    fireEvent.click(await screen.findByLabelText('Delete workflow blogdemo'))
    const confirmBtn = screen.getByRole('button', { name: /^delete$/i })
    expect(confirmBtn).toBeDisabled()

    fireEvent.change(screen.getByLabelText('Confirmation text'), { target: { value: 'blogdemo' } })
    fireEvent.click(screen.getByRole('button', { name: /^delete$/i }))

    await waitFor(() => expect(onDeleted).toHaveBeenCalledWith('blogdemo'))
    expect(del).toHaveBeenCalledWith('blogdemo')
  })

  it('renders a delete 403 as the cao:admin permission story', async () => {
    vi.spyOn(api, 'listWorkflows').mockResolvedValue(catalogRows)
    vi.spyOn(api, 'deleteWorkflow').mockRejectedValue(
      apiError(403, "Forbidden: requires one of ['cao:admin']"),
    )
    render(<WorkflowCatalog selectedName={null} onSelect={() => {}} onDeleted={() => {}} />)

    fireEvent.click(await screen.findByLabelText('Delete workflow blogdemo'))
    fireEvent.change(screen.getByLabelText('Confirmation text'), { target: { value: 'blogdemo' } })
    fireEvent.click(screen.getByRole('button', { name: /^delete$/i }))

    expect(await screen.findByText(/requires the cao:admin scope/i)).toBeInTheDocument()
  })
})

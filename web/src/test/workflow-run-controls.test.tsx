// Workflow run controls in the web UI: the run/validate/spec/approve/cancel/
// resume/result/artifact client methods, the store's approval outcomes, and the
// launcher, plan-approval form, result panel and report viewer components.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { api, WorkflowRunResult } from '../api'
import { useStore } from '../store'
import { PlanApprovalForm } from '../components/workflow/PlanApprovalForm'
import { RunResultPanel, findReportPaths } from '../components/workflow/RunResultPanel'
import { RunArtifacts } from '../components/workflow/RunArtifacts'
import {
  WorkflowLauncher, LAUNCHER_DRAFT_KEY, loadLauncherDraft, rowsFromDeclaredInputs,
} from '../components/workflow/WorkflowLauncher'

function ok(data: unknown) {
  return { ok: true, status: 200, statusText: 'OK', json: () => Promise.resolve(data), text: () => Promise.resolve(JSON.stringify(data)) }
}
function fail(status: number, detail: unknown) {
  return { ok: false, status, statusText: 'Error', json: () => Promise.resolve({ detail }) }
}

const DEF = { name: 'review_flow', source_path: '/wf/review_flow.py', mode: 'script', step_count: null, description: '', indexed_at: '' }

describe('workflow run-control client methods', () => {
  const mockFetch = vi.fn()
  beforeEach(() => vi.stubGlobal('fetch', mockFetch))
  afterEach(() => { vi.restoreAllMocks(); mockFetch.mockReset() })

  it('listWorkflows fetches /workflows (definitions, not runs)', async () => {
    mockFetch.mockResolvedValueOnce(ok([DEF]))
    expect(await api.listWorkflows()).toEqual([DEF])
    expect(mockFetch).toHaveBeenCalledWith('/workflows', expect.any(Object))
  })

  it('submitWorkflowRun POSTs the async :submit twin with a JSON body', async () => {
    mockFetch.mockResolvedValueOnce(ok({ run_id: 'run-1', state: 'running' }))
    const body = { name_or_path: 'review_flow', inputs: { topic: 't', report: '/tmp/report.md' }, run_id: 'run-1' }
    const ack = await api.submitWorkflowRun(body)
    expect(ack.run_id).toBe('run-1')
    const [url, opts] = mockFetch.mock.calls[0]
    expect(url).toBe('/workflows/runs:submit')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body)).toEqual(body)
    expect(opts.headers['Content-Type']).toBe('application/json')
  })

  it('cancelWorkflowRun POSTs the cancel route with no body; resume POSTs resume', async () => {
    mockFetch.mockResolvedValueOnce(ok({ success: true, run_id: 'run-1' }))
    await api.cancelWorkflowRun('run-1')
    expect(mockFetch.mock.calls[0][0]).toBe('/workflows/runs/run-1/cancel')
    expect(mockFetch.mock.calls[0][1].body).toBeUndefined()
    mockFetch.mockResolvedValueOnce(ok({ run_id: 'run-1', state: 'running' }))
    await api.resumeWorkflowRun('run-1')
    expect(mockFetch.mock.calls[1][0]).toBe('/workflows/runs/run-1/resume')
    expect(mockFetch.mock.calls[1][1].method).toBe('POST')
  })

  it('approveWorkflowPlan POSTs the plan id verbatim in the body', async () => {
    mockFetch.mockResolvedValueOnce(ok({ plan_id: 'plan-v1:75048c5db632', approved: true, changed: true }))
    expect((await api.approveWorkflowPlan('plan-v1:75048c5db632')).approved).toBe(true)
    const [url, opts] = mockFetch.mock.calls[0]
    expect(url).toBe('/workflows/plans/approve')
    expect(JSON.parse(opts.body)).toEqual({ plan_id: 'plan-v1:75048c5db632' })
  })

  it('validateWorkflow POSTs {path}', async () => {
    mockFetch.mockResolvedValueOnce(ok({ status: 'pass', errors: [], findings: [] }))
    expect((await api.validateWorkflow('/wf/review_flow.py')).status).toBe('pass')
    expect(mockFetch.mock.calls[0][0]).toBe('/workflows/validate')
    expect(JSON.parse(mockFetch.mock.calls[0][1].body)).toEqual({ path: '/wf/review_flow.py' })
  })

  it('getWorkflowRunResult and getWorkflowSpec encode their path segment', async () => {
    mockFetch.mockResolvedValueOnce(ok({ run_id: 'a b', state: 'completed', steps: [] }))
    await api.getWorkflowRunResult('a b')
    expect(mockFetch.mock.calls[0][0]).toBe('/workflows/runs/a%20b/result')
    mockFetch.mockResolvedValueOnce(ok({ name: 'wf', source: 'x' }))
    await api.getWorkflowSpec('a/b')
    expect(mockFetch.mock.calls[1][0]).toBe('/workflows/a%2Fb')
  })

  it('listWorkflowArtifacts / getWorkflowArtifact encode the run id and input', async () => {
    mockFetch.mockResolvedValueOnce(ok({ run_id: 'r 1', artifacts: [] }))
    await api.listWorkflowArtifacts('r 1')
    expect(mockFetch.mock.calls[0][0]).toBe('/workflows/runs/r%201/artifacts')
    mockFetch.mockResolvedValueOnce(ok({ run_id: 'r1', input: 'report', path: '/tmp/x.md', size: 1, content: 'x' }))
    await api.getWorkflowArtifact('r1', 'report')
    expect(mockFetch.mock.calls[1][0]).toBe('/workflows/runs/r1/artifacts/report')
  })

  it('surfaces ApiError detail on a bad-inputs 400 from submit', async () => {
    mockFetch.mockResolvedValueOnce(fail(400, "undeclared input 'topc'"))
    await expect(api.submitWorkflowRun({ name_or_path: 'wf', inputs: { topc: 'x' } }))
      .rejects.toMatchObject({ status: 400, detail: "undeclared input 'topc'" })
  })
})

describe('store approval outcomes', () => {
  const mockFetch = vi.fn()
  beforeEach(() => vi.stubGlobal('fetch', mockFetch))
  afterEach(() => { vi.restoreAllMocks(); mockFetch.mockReset() })

  it('startWorkflowRun hands back the plan id on an approval_required 403', async () => {
    mockFetch.mockResolvedValueOnce(fail(403, { kind: 'approval_required', plan_id: 'plan-v1:abc', message: 'needs approval' }))
    expect(await useStore.getState().startWorkflowRun('wf', { topic: 't' }, 'run-1'))
      .toEqual({ approvalPlanId: 'plan-v1:abc', message: 'needs approval' })
  })

  it('startWorkflowRun returns null (not a plan) on other refusals', async () => {
    mockFetch.mockResolvedValueOnce(fail(503, { kind: 'plan_identity_unavailable', plan_id: null, message: 'freeze failed' }))
    expect(await useStore.getState().startWorkflowRun('wf', {})).toBeNull()
  })

  it('resumeWorkflowRun also hands back the plan id on approval_required', async () => {
    mockFetch.mockResolvedValueOnce(fail(403, { kind: 'approval_required', plan_id: 'plan-v1:def', message: 'changed' }))
    expect(await useStore.getState().resumeWorkflowRun('run-1')).toEqual({ approvalPlanId: 'plan-v1:def', message: 'changed' })
  })
})

describe('WorkflowLauncher', () => {
  beforeEach(() => localStorage.clear())
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('builds input rows from the declared inputs, required first, defaults filled', () => {
    expect(rowsFromDeclaredInputs({
      tone: { type: 'string', required: false, default: 'plain' },
      topic: { type: 'string', required: true },
    })).toEqual([
      { key: 'topic', value: '', required: true, type: 'string' },
      { key: 'tone', value: 'plain', required: false, type: 'string' },
    ])
    expect(rowsFromDeclaredInputs({})).toEqual([{ key: '', value: '' }])
    expect(rowsFromDeclaredInputs(undefined)).toEqual([{ key: '', value: '' }])
  })

  it('prefills the selected workflow’s declared inputs when opened', async () => {
    vi.spyOn(api, 'listWorkflows').mockResolvedValue([DEF])
    vi.spyOn(api, 'getWorkflowSpec').mockResolvedValue({ name: 'review_flow', inputs: { topic: { type: 'string', required: true }, report: { type: 'string', default: '/tmp/report.md' } } })
    render(<WorkflowLauncher />)
    fireEvent.click(screen.getByRole('button', { name: /Run a workflow/ }))
    await waitFor(() => expect((screen.getByLabelText('Input 1 name') as HTMLInputElement).value).toBe('topic'))
    expect((screen.getByLabelText('Input 2 value') as HTMLInputElement).value).toBe('/tmp/report.md')
    expect(screen.getByLabelText('Input 1 value').getAttribute('placeholder')).toContain('required')
  })

  it('does not overwrite rows the user already edited', async () => {
    let resolveSpec: (v: any) => void = () => {}
    vi.spyOn(api, 'listWorkflows').mockResolvedValue([DEF])
    vi.spyOn(api, 'getWorkflowSpec').mockReturnValue(new Promise(r => { resolveSpec = r }))
    render(<WorkflowLauncher />)
    fireEvent.click(screen.getByRole('button', { name: /Run a workflow/ }))
    const key = await screen.findByLabelText('Input 1 name')
    fireEvent.change(key, { target: { value: 'mine' } })
    resolveSpec({ name: 'review_flow', inputs: { topic: { type: 'string' } } })
    await new Promise(r => setTimeout(r, 0))
    expect((screen.getByLabelText('Input 1 name') as HTMLInputElement).value).toBe('mine')
  })

  const inputs = [{ key: 'topic', value: 't' }, { key: 'report', value: '/tmp/report.md' }]
  const forDraft = JSON.stringify(['review_flow', inputs, 'run-1'])

  it('restores a pending approval after a reload and opens itself', async () => {
    localStorage.setItem(LAUNCHER_DRAFT_KEY, JSON.stringify({ selected: 'review_flow', runId: 'run-1', inputs, pendingApproval: { planId: 'plan-v2:abc', forDraft } }))
    vi.spyOn(api, 'listWorkflows').mockResolvedValue([DEF])
    const spec = vi.spyOn(api, 'getWorkflowSpec')
    render(<WorkflowLauncher />)
    expect(await screen.findByText('plan-v2:abc')).toBeTruthy()
    expect(screen.getByRole('button', { name: /Approve & run/ })).toBeTruthy()
    expect(spec).not.toHaveBeenCalled() // a restored draft wins over the prefill
  })

  it('drops a stored approval that no longer matches the draft', () => {
    localStorage.setItem(LAUNCHER_DRAFT_KEY, JSON.stringify({ selected: 'review_flow', runId: 'other', inputs, pendingApproval: { planId: 'plan-v2:abc', forDraft } }))
    vi.spyOn(api, 'listWorkflows').mockResolvedValue([])
    render(<WorkflowLauncher />)
    expect(screen.queryByText('plan-v2:abc')).toBeNull()
  })

  it('ignores malformed drafts', () => {
    localStorage.setItem(LAUNCHER_DRAFT_KEY, 'not json')
    expect(loadLauncherDraft()).toBeNull()
    localStorage.setItem(LAUNCHER_DRAFT_KEY, JSON.stringify({ selected: 1 }))
    expect(loadLauncherDraft()).toBeNull()
    localStorage.setItem(LAUNCHER_DRAFT_KEY, JSON.stringify({ selected: 'a', runId: '', inputs: [{ key: 'k', value: 'v' }, 7], pendingApproval: { planId: 3 } }))
    expect(loadLauncherDraft()).toEqual({ selected: 'a', runId: '', inputs: [{ key: 'k', value: 'v' }], pendingApproval: null })
  })
})

describe('PlanApprovalForm', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('approves the pasted id verbatim and reports the outcome', async () => {
    const spy = vi.spyOn(api, 'approveWorkflowPlan').mockResolvedValue({ plan_id: 'plan-v2:abc', approved: true, changed: true, approved_at: 't0' })
    render(<PlanApprovalForm />)
    fireEvent.click(screen.getByText('Approve a plan id'))
    fireEvent.change(screen.getByLabelText('Plan id'), { target: { value: ' plan-v2:abc ' } })
    fireEvent.click(screen.getByRole('button', { name: /Approve$/ }))
    expect(await screen.findByRole('status')).toBeTruthy()
    expect(spy).toHaveBeenCalledWith('plan-v2:abc')
    expect(screen.getByRole('status').textContent).toContain('Approved at t0')
  })

  it('says when the plan was already approved', async () => {
    vi.spyOn(api, 'approveWorkflowPlan').mockResolvedValue({ plan_id: 'p', approved: true, changed: false })
    render(<PlanApprovalForm />)
    fireEvent.click(screen.getByText('Approve a plan id'))
    fireEvent.change(screen.getByLabelText('Plan id'), { target: { value: 'p' } })
    fireEvent.click(screen.getByRole('button', { name: /Approve$/ }))
    expect((await screen.findByRole('status')).textContent).toContain('Already approved')
  })
})

describe('RunResultPanel', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks() })
  const base: WorkflowRunResult = { run_id: 'r', workflow_name: 'wf', state: 'completed', started_at: '', steps: [] }

  it('lifts report-like paths out of step outputs, once each', () => {
    const r = {
      ...base,
      steps: [
        { id: 'a', state: 'completed', attempts: 1, output: { findings: 'x', report_path: '/tmp/report.md' } },
        { id: 'b', state: 'completed', attempts: 1, output: { nested: { file: '/tmp/report.md' }, other: '/tmp/notes.md' } },
      ],
    }
    expect(findReportPaths(r)).toEqual(['/tmp/report.md', '/tmp/notes.md'])
  })

  it('ignores prose that merely mentions a report', () => {
    expect(findReportPaths({ ...base, steps: [{ id: 'a', state: 'completed', attempts: 1, output: { report: 'all good' } }] })).toEqual([])
  })

  it('does not fetch while the run is still going', () => {
    const spy = vi.spyOn(api, 'getWorkflowRunResult')
    const { container } = render(<RunResultPanel runId="r" terminal={false} state="running" />)
    expect(spy).not.toHaveBeenCalled()
    expect(container.textContent).toBe('')
  })

  it('shows the final state and the report path once terminal', async () => {
    vi.spyOn(api, 'getWorkflowRunResult').mockResolvedValue({
      ...base, finished_at: 't1',
      steps: [{ id: 'write-report', state: 'completed', attempts: 1, output: { report: '/tmp/report.md' } }],
    })
    render(<RunResultPanel runId="r" terminal state="completed" />)
    expect(await screen.findByText('/tmp/report.md')).toBeTruthy()
    expect(screen.getByText('completed')).toBeTruthy()
  })
})

describe('RunArtifacts', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('does not ask while the run is still going', () => {
    const list = vi.spyOn(api, 'listWorkflowArtifacts')
    const { container } = render(<RunArtifacts runId="r1" terminal={false} />)
    expect(list).not.toHaveBeenCalled()
    expect(container.textContent).toBe('')
  })

  it('shows a View button per artifact and renders the report as text, not HTML', async () => {
    vi.spyOn(api, 'listWorkflowArtifacts').mockResolvedValue({
      run_id: 'r1',
      artifacts: [
        { input: 'report', path: '/tmp/report.md', available: true, size: 30 },
        { input: 'draft', path: '/tmp/draft.md', available: false, size: null },
      ],
    })
    const get = vi.spyOn(api, 'getWorkflowArtifact').mockResolvedValue({
      run_id: 'r1', input: 'report', path: '/tmp/report.md', size: 30,
      content: '# Summary\n<img src=x onerror=alert(1)>',
    })
    const { container } = render(<RunArtifacts runId="r1" terminal={true} />)
    const view = await screen.findByRole('button', { name: /View report/ })
    expect(screen.getByRole('button', { name: /View draft/ })).toBeDisabled()
    fireEvent.click(view)
    await waitFor(() => expect(get).toHaveBeenCalledWith('r1', 'report'))
    const pre = await waitFor(() => {
      const el = container.querySelector('pre')
      expect(el).not.toBeNull()
      return el!
    })
    expect(pre.textContent).toContain('<img src=x onerror=alert(1)>')
    expect(container.querySelector('img')).toBeNull()
  })

  it('renders nothing when the run declared no report', async () => {
    vi.spyOn(api, 'listWorkflowArtifacts').mockResolvedValue({ run_id: 'r1', artifacts: [] })
    const { container } = render(<RunArtifacts runId="r1" terminal={true} />)
    await waitFor(() => expect(api.listWorkflowArtifacts).toHaveBeenCalled())
    expect(container.textContent).toBe('')
  })
})

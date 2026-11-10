// The workflow builder's UI: the authoring client methods, StepEditor,
// WorkflowBuilder, WorkflowFlow, the delete confirm in the launcher, the New
// workflow wiring in WorkflowsPanel, and the flow in RunDetail.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useState } from 'react'
import { act, render, screen, fireEvent, waitFor, within, cleanup } from '@testing-library/react'
import { api } from '../api'
import type {
  ApiError,
  ProfileSearchResult,
  ProviderInfo,
  RunInspection,
  WorkflowDefRow,
  WorkflowEvent,
  WorkflowSpec,
  WorkflowSpecStep,
} from '../api'
import { useStore } from '../store'
import { StepEditor } from '../components/workflow/StepEditor'
import { WorkflowBuilder, STALE_MESSAGE, describeSaveError } from '../components/workflow/WorkflowBuilder'
import { WorkflowFlow, runFlowSteps, runStepStates, flowStateView } from '../components/workflow/WorkflowFlow'
import { WorkflowLauncher, isYamlWorkflow } from '../components/workflow/WorkflowLauncher'
import { WorkflowsPanel } from '../components/WorkflowsPanel'
import { RunDetail } from '../components/workflow/RunDetail'
import {
  MAX_PARAMS,
  applyStepPatch,
  errorsUnder,
  newStep,
  validateBuilder,
  type BuilderStep,
} from '../components/workflow/workflowYaml'

const PROVIDERS: ProviderInfo[] = [
  { name: 'claude_code', binary: 'claude', installed: true },
  { name: 'kiro_cli', binary: 'kiro-cli', installed: false },
]

function result(name: string, description = ''): ProfileSearchResult {
  return { name, description, capabilities: [], tags: [], role: '', source: 'local', coverage: 1, score: 1 }
}

function apiError(status: number, detail?: string): ApiError {
  return Object.assign(new Error(`${status} Error`), { status, detail })
}

function ok(data: unknown) {
  return { ok: true, status: 200, statusText: 'OK', json: () => Promise.resolve(data), text: () => Promise.resolve(JSON.stringify(data)) }
}

beforeEach(() => {
  localStorage.clear()
  useStore.setState({ snackbar: null, navLockCount: 0 })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('workflow authoring client methods', () => {
  const mockFetch = vi.fn()
  beforeEach(() => vi.stubGlobal('fetch', mockFetch))
  afterEach(() => mockFetch.mockReset())

  it('createWorkflow POSTs name, source and tier to /workflows', async () => {
    mockFetch.mockResolvedValueOnce(ok({ name: 'wf', path: '/wf/wf.yaml', content_hash: 'h', findings: [], status: 'pass', reserved_notes: [] }))
    expect((await api.createWorkflow('wf', '"name": "wf"\n', 'yaml')).content_hash).toBe('h')
    const [url, opts] = mockFetch.mock.calls[0]
    expect(url).toBe('/workflows')
    expect(opts.method).toBe('POST')
    expect(opts.headers['Content-Type']).toBe('application/json')
    expect(JSON.parse(opts.body)).toEqual({ name: 'wf', source: '"name": "wf"\n', tier: 'yaml' })
  })

  it('updateWorkflow PUTs source, expected_hash and tier to the encoded name', async () => {
    mockFetch.mockResolvedValueOnce(ok({ name: 'a b', path: '/wf/a.yaml', content_hash: 'h2', findings: [] }))
    await api.updateWorkflow('a b', 'src', 'h1', 'yaml')
    const [url, opts] = mockFetch.mock.calls[0]
    expect(url).toBe('/workflows/a%20b')
    expect(opts.method).toBe('PUT')
    expect(JSON.parse(opts.body)).toEqual({ source: 'src', expected_hash: 'h1', tier: 'yaml' })
  })

  it('deleteWorkflow DELETEs /workflows/{name}', async () => {
    mockFetch.mockResolvedValueOnce(ok({ success: true, name: 'a/b' }))
    await api.deleteWorkflow('a/b')
    expect(mockFetch.mock.calls[0][0]).toBe('/workflows/a%2Fb')
    expect(mockFetch.mock.calls[0][1].method).toBe('DELETE')
  })

  it('validateWorkflowSource POSTs {source, tier} to the validate route', async () => {
    mockFetch.mockResolvedValueOnce(ok({ status: 'pass_reserved', errors: [], reserved_notes: ['n'] }))
    expect((await api.validateWorkflowSource('src', 'yaml')).status).toBe('pass_reserved')
    expect(mockFetch.mock.calls[0][0]).toBe('/workflows/validate')
    expect(JSON.parse(mockFetch.mock.calls[0][1].body)).toEqual({ source: 'src', tier: 'yaml' })
  })

  it('surfaces the server detail of a refused write', async () => {
    mockFetch.mockResolvedValueOnce({ ok: false, status: 409, statusText: 'Conflict', json: () => Promise.resolve({ detail: 'exists' }) })
    await expect(api.createWorkflow('wf', 'src', 'yaml')).rejects.toMatchObject({ status: 409, detail: 'exists' })
  })
})

// A stateful host for StepEditor, with errors from validateBuilder.
function StepHarness({ initial = newStep('step-1'), providers = PROVIDERS }: { initial?: BuilderStep; providers?: ProviderInfo[] }) {
  const [step, setStep] = useState<BuilderStep>(initial)
  const errors = errorsUnder(validateBuilder({ name: 'w', description: '', inputs: [], steps: [step] }), 'steps.0')
  return (
    <>
      <StepEditor step={step} index={0} errors={errors} providers={providers} onChange={patch => setStep(s => applyStepPatch(s, patch))} />
      <pre data-testid="step-state">{JSON.stringify(step)}</pre>
    </>
  )
}
const current = (): BuilderStep => JSON.parse(screen.getByTestId('step-state').textContent as string)

// Lets pending mock responses land inside act, so a test that ends right after
// a render does not leave a state update outside it.
const settle = () => act(async () => { await new Promise(r => setTimeout(r, 0)) })

async function searchAndPick(n: number, query: string, name = query) {
  fireEvent.change(screen.getByLabelText(`Search profiles for step ${n}`), { target: { value: query } })
  const pick = await screen.findByRole('button', { name: `Use profile ${name}` })
  // The profile read that follows the pick resolves inside act.
  await act(async () => { fireEvent.click(pick) })
}

describe('StepEditor', () => {
  it('search, then select, sets the agent and the provider the profile names', async () => {
    const search = vi.spyOn(api, 'searchProfiles').mockResolvedValue([result('sqs-sender-beta', 'Sends SQS messages')])
    const get = vi.spyOn(api, 'getProfile').mockResolvedValue({ name: 'sqs-sender-beta', description: '', provider: 'claude_code' })
    render(<StepHarness />)
    await searchAndPick(1, 'sqs', 'sqs-sender-beta')
    expect(search).toHaveBeenCalledWith('sqs', 8)
    await waitFor(() => expect(current().provider).toBe('claude_code'))
    expect(current().agent).toBe('sqs-sender-beta')
    expect(get).toHaveBeenCalledWith('sqs-sender-beta')
    expect(screen.getByTestId('step-1-agent')).toHaveTextContent('sqs-sender-beta')
    expect((screen.getByLabelText('Provider for step 1') as HTMLSelectElement).value).toBe('claude_code')
    expect(screen.getByText('From the profile.')).toBeInTheDocument()
    // The results close once a profile is picked.
    expect(screen.queryByRole('list', { name: 'Profile results for step 1' })).toBeNull()
  })

  it('leaves the provider to the user when the profile names none, from the listProviders options', async () => {
    vi.spyOn(api, 'searchProfiles').mockResolvedValue([result('reviewer')])
    vi.spyOn(api, 'getProfile').mockResolvedValue({ name: 'reviewer', description: '' })
    render(<StepHarness />)
    await searchAndPick(1, 'reviewer')
    expect(await screen.findByText('This profile names no provider. Pick one.')).toBeInTheDocument()
    const select = screen.getByLabelText('Provider for step 1') as HTMLSelectElement
    expect([...select.options].map(o => o.textContent)).toEqual(['Pick a provider', 'claude_code', 'kiro_cli (not installed)'])
    expect(screen.getByText('Pick a provider.')).toBeInTheDocument()
    fireEvent.change(select, { target: { value: 'claude_code' } })
    expect(current().provider).toBe('claude_code')
    expect(screen.queryByText('Pick a provider.')).toBeNull()
  })

  it('drops a late profile read once a newer profile is picked', async () => {
    let resolveFirst: (v: any) => void = () => {}
    vi.spyOn(api, 'searchProfiles').mockImplementation(async q => [result(q)])
    vi.spyOn(api, 'getProfile').mockImplementation(name =>
      name === 'first'
        ? new Promise(r => { resolveFirst = r })
        : Promise.resolve({ name, description: '', provider: 'kiro_cli' }))
    render(<StepHarness />)
    await searchAndPick(1, 'first')
    await searchAndPick(1, 'second')
    await waitFor(() => expect(current().provider).toBe('kiro_cli'))
    resolveFirst({ name: 'first', description: '', provider: 'claude_code' })
    await new Promise(r => setTimeout(r, 0))
    expect(current()).toMatchObject({ agent: 'second', provider: 'kiro_cli' })
  })

  it('offers three call types and marks assign and send_message as not built yet', () => {
    render(<StepHarness />)
    const call = screen.getByLabelText('Call type for step 1') as HTMLSelectElement
    expect([...call.options].map(o => o.textContent)).toEqual(['handoff', 'assign (not built yet)', 'send_message (not built yet)'])
    expect(call.value).toBe('handoff')
    expect(screen.queryByTestId('step-1-reserved')).toBeNull()
    fireEvent.change(call, { target: { value: 'send_message' } })
    expect(current().call).toBe('send_message')
    expect(screen.getByTestId('step-1-reserved')).toHaveTextContent('Not built yet')
    fireEvent.change(call, { target: { value: 'handoff' } })
    expect(screen.queryByTestId('step-1-reserved')).toBeNull()
  })

  it('edits params as rows, with inline errors', () => {
    render(<StepHarness />)
    fireEvent.click(screen.getByRole('button', { name: /Add param/ }))
    const key1 = screen.getByLabelText('Step 1 param 1 name')
    fireEvent.change(key1, { target: { value: '1bad' } })
    expect(screen.getByText(/Start with a letter/)).toBeInTheDocument()
    expect(key1).toHaveAttribute('aria-invalid', 'true')
    fireEvent.change(key1, { target: { value: 'aws-profile-name' } })
    expect(screen.queryByText(/Start with a letter/)).toBeNull()
    fireEvent.change(screen.getByLabelText('Step 1 param 1 value'), { target: { value: 'x'.repeat(513) } })
    expect(screen.getByText('A param value can have at most 512 characters.')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Step 1 param 1 value'), { target: { value: 'beta' } })

    fireEvent.click(screen.getByRole('button', { name: /Add param/ }))
    fireEvent.change(screen.getByLabelText('Step 1 param 2 name'), { target: { value: 'aws-profile-name' } })
    expect(screen.getByText('Another param has this name.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Remove step 1 param 2' }))
    expect(current().params).toEqual([{ key: 'aws-profile-name', value: 'beta' }])
    expect(screen.queryByText('Another param has this name.')).toBeNull()
  })

  it(`stops adding params at ${MAX_PARAMS}`, () => {
    const params = Array.from({ length: MAX_PARAMS }, (_, j) => ({ key: `k${j}`, value: 'v' }))
    render(<StepHarness initial={{ ...newStep('s'), params }} />)
    expect(screen.getByRole('button', { name: /Add param/ })).toBeDisabled()
  })

  it('Clone opens the profile editor in clone mode, and the saved copy becomes the step agent', async () => {
    vi.spyOn(api, 'getProfileSource').mockResolvedValue({ name: 'developer', content: '---\nname: developer\ndescription: Dev\nprovider: claude_code\n---\nYou write code.\n' })
    vi.spyOn(api, 'validateProfile').mockResolvedValue({ valid: true, messages: [] })
    const create = vi.spyOn(api, 'createProfile').mockResolvedValue({ name: 'developer-beta', warnings: [] })
    vi.spyOn(api, 'getProfile').mockResolvedValue({ name: 'developer-beta', description: '', provider: 'claude_code' })
    render(<StepHarness initial={{ ...newStep('s'), agent: 'developer', provider: 'claude_code' }} />)
    fireEvent.click(screen.getByRole('button', { name: 'Clone profile developer' }))
    const dialog = await screen.findByRole('dialog', { name: 'Clone profile' })
    await within(dialog).findByLabelText('Profile source')
    fireEvent.change(within(dialog).getByLabelText('New profile name'), { target: { value: 'developer-beta' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create copy' }))
    await waitFor(() => expect(current().agent).toBe('developer-beta'))
    expect(create.mock.calls[0][0]).toBe('developer-beta')
    expect(create.mock.calls[0][1]).toContain('name: "developer-beta"')
    expect(screen.queryByRole('dialog', { name: 'Clone profile' })).toBeNull()
    expect(useStore.getState().snackbar).toMatchObject({ type: 'success', message: 'Cloned to developer-beta' })
  })
})

const SPEC_NULLS = { engine: null, output_schema: null, when: null, until: null, max_iterations: null, on_stall: null, on_no_progress: null }

function specStep(id: string, call: WorkflowSpecStep['call'], over: Partial<WorkflowSpecStep> = {}): WorkflowSpecStep {
  return { id, provider: 'claude_code', agent: `${id}-agent`, prompt: `Do ${id}.`, call, params: null, retries: null, on_failure: null, ...SPEC_NULLS, ...over }
}

const SPEC: WorkflowSpec = {
  name: 'demo',
  description: 'Demo check',
  mode: 'sequential',
  inputs: { run_label: { type: 'string', required: true, default: null } },
  steps: [
    specStep('send', 'handoff', { agent: 'sqs-sender-beta', prompt: 'Send.', params: { 'aws-profile-name': 'beta' } }),
    specStep('check', 'assign', { agent: 'ddb-checker', prompt: 'Check.', retries: 1, on_failure: 'continue' }),
  ],
  path: '/wf/demo.yaml',
  source: '"name": "demo"\n',
  content_hash: 'hash-1',
}

describe('WorkflowBuilder', () => {
  beforeEach(() => {
    vi.spyOn(api, 'listProviders').mockResolvedValue(PROVIDERS)
    vi.spyOn(api, 'searchProfiles').mockImplementation(async q => [result(q)])
    vi.spyOn(api, 'getProfile').mockImplementation(async name => ({ name, description: '', provider: 'claude_code' }))
  })

  async function fillValidStep(n = 1, agent = 'sqs-sender-beta') {
    await searchAndPick(n, agent)
    await waitFor(() => expect((screen.getByLabelText(`Provider for step ${n}`) as HTMLSelectElement).value).toBe('claude_code'))
    fireEvent.change(screen.getByLabelText(`Prompt for step ${n}`), { target: { value: 'Send message 01.' } })
  }

  it('creates a YAML workflow from the form', async () => {
    const create = vi.spyOn(api, 'createWorkflow').mockResolvedValue({
      name: 'my-wf', path: '/wf/my-wf.yaml', content_hash: 'h1', findings: [], status: 'pass', reserved_notes: [],
    })
    const onSaved = vi.fn()
    render(<WorkflowBuilder mode="create" onClose={() => {}} onSaved={onSaved} />)
    fireEvent.change(screen.getByLabelText('Workflow name'), { target: { value: 'my-wf' } })
    await fillValidStep()
    fireEvent.click(screen.getByRole('button', { name: /Add param/ }))
    fireEvent.change(screen.getByLabelText('Step 1 param 1 name'), { target: { value: 'aws-profile-name' } })
    fireEvent.change(screen.getByLabelText('Step 1 param 1 value'), { target: { value: 'beta' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create workflow' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('my-wf', expect.objectContaining({ content_hash: 'h1' })))
    const expected = [
      '"name": "my-wf"',
      '"description": ""',
      '"mode": "sequential"',
      '"inputs": {}',
      '"steps":',
      '  - "id": "step-1"',
      '    "provider": "claude_code"',
      '    "agent": "sqs-sender-beta"',
      '    "call": "handoff"',
      '    "params":',
      '      "aws-profile-name": "beta"',
      '    "prompt": "Send message 01."',
      '',
    ].join('\n')
    expect(create).toHaveBeenCalledWith('my-wf', expected, 'yaml')
    expect(screen.getByLabelText('YAML preview').textContent).toBe(expected)
  })

  it('shows field errors after the first Save and sends nothing while any remain', async () => {
    const create = vi.spyOn(api, 'createWorkflow')
    render(<WorkflowBuilder mode="create" onClose={() => {}} onSaved={() => {}} />)
    await settle()
    expect(screen.queryByText('Enter a workflow name.')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Create workflow' }))
    expect(screen.getByText('Enter a workflow name.')).toBeInTheDocument()
    expect(screen.getByText('Pick an agent profile.')).toBeInTheDocument()
    expect(screen.getByText('Pick a provider.')).toBeInTheDocument()
    expect(screen.getByText('Write a prompt.')).toBeInTheDocument()
    expect(screen.getByText('Fix the 4 problems marked in the form first.')).toBeInTheDocument()
    expect(create).not.toHaveBeenCalled()
  })

  it('shows a 400 from the server inline', async () => {
    const detail = "step 'step-1': retries 99 out of range (0..10)"
    vi.spyOn(api, 'createWorkflow').mockRejectedValue(apiError(400, detail))
    const onSaved = vi.fn()
    render(<WorkflowBuilder mode="create" onClose={() => {}} onSaved={onSaved} />)
    fireEvent.change(screen.getByLabelText('Workflow name'), { target: { value: 'my-wf' } })
    await fillValidStep()
    fireEvent.click(screen.getByRole('button', { name: 'Create workflow' }))
    expect(await screen.findByText(detail)).toBeInTheDocument()
    expect(screen.getByText(detail).closest('[role="alert"]')).not.toBeNull()
    expect(onSaved).not.toHaveBeenCalled()
  })

  it('says so when a create hits an existing name (409)', async () => {
    vi.spyOn(api, 'createWorkflow').mockRejectedValue(apiError(409, "workflow 'my-wf' already exists"))
    render(<WorkflowBuilder mode="create" onClose={() => {}} onSaved={() => {}} />)
    fireEvent.change(screen.getByLabelText('Workflow name'), { target: { value: 'my-wf' } })
    await fillValidStep()
    fireEvent.click(screen.getByRole('button', { name: 'Create workflow' }))
    expect(await screen.findByText("A workflow named 'my-wf' already exists. Choose another name.")).toBeInTheDocument()
  })

  it('edits: loads the spec, keeps the name fixed, and saves with the content hash', async () => {
    vi.spyOn(api, 'getWorkflowSpec').mockResolvedValue(SPEC)
    const update = vi.spyOn(api, 'updateWorkflow').mockResolvedValue({ name: 'demo', path: '/wf/demo.yaml', content_hash: 'hash-2', findings: [] })
    const onSaved = vi.fn()
    render(<WorkflowBuilder mode="edit" name="demo" onClose={() => {}} onSaved={onSaved} />)
    expect(await screen.findByTestId('builder-fixed-name')).toHaveTextContent('demo')
    expect(screen.queryByLabelText('Workflow name')).toBeNull()
    expect((screen.getByLabelText('Workflow description') as HTMLInputElement).value).toBe('Demo check')
    expect((screen.getByLabelText('Workflow input 1 name') as HTMLInputElement).value).toBe('run_label')
    expect((screen.getByLabelText('Step 1 param 1 value') as HTMLInputElement).value).toBe('beta')
    expect((screen.getByLabelText('Call type for step 2') as HTMLSelectElement).value).toBe('assign')
    expect((screen.getByLabelText('Retries for step 2') as HTMLInputElement).value).toBe('1')
    fireEvent.change(screen.getByLabelText('Prompt for step 1'), { target: { value: 'Send it.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('demo', expect.objectContaining({ content_hash: 'hash-2' })))
    const [name, source, hash, tier] = update.mock.calls[0]
    expect([name, hash, tier]).toEqual(['demo', 'hash-1', 'yaml'])
    expect(source).toContain('    "prompt": "Send it."\n')
    expect(source).toContain('    "call": "assign"\n    "prompt": "Check."\n    "retries": 1\n    "on_failure": "continue"\n')
  })

  it('edits: a stale hash says the workflow changed, and Reload reads it again', async () => {
    const get = vi.spyOn(api, 'getWorkflowSpec').mockResolvedValue(SPEC)
    vi.spyOn(api, 'updateWorkflow').mockRejectedValue(apiError(409,
      "workflow 'demo' has changed on disk since it was read (expected source hash hash-1, found hash-9); re-read the spec and re-apply your changes"))
    render(<WorkflowBuilder mode="edit" name="demo" onClose={() => {}} onSaved={() => {}} />)
    await screen.findByTestId('builder-fixed-name')
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    expect(await screen.findByText(STALE_MESSAGE)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Reload/ }))
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByText(STALE_MESSAGE)).toBeNull())
  })

  it('maps save failures to the right message', () => {
    expect(describeSaveError(apiError(409, 'x'), 'create', 'wf').message).toBe("A workflow named 'wf' already exists. Choose another name.")
    const stale = "workflow 'wf' has changed on disk since it was read (expected source hash a, found b); re-read the spec and re-apply your changes"
    expect(describeSaveError(apiError(409, stale), 'edit', 'wf')).toEqual({ message: STALE_MESSAGE, stale: true })
    expect(describeSaveError(apiError(409), 'edit', 'wf').stale).toBe(true)
    // A name that contains "python" or "tier" must not turn a stale hash into a raw message.
    const staleNamed = "workflow 'python-tier-smoke' has changed on disk since it was read (expected source hash a, found b); re-read the spec and re-apply your changes"
    expect(describeSaveError(apiError(409, staleNamed), 'edit', 'python-tier-smoke')).toEqual({ message: STALE_MESSAGE, stale: true })
    const python = "workflow 'wf' is a Python workflow; update it with tier 'python'"
    expect(describeSaveError(apiError(409, python), 'edit', 'wf')).toEqual({ message: python })
    const collision = "workflow 'wf' exists in both the script and YAML tiers (same-stem collision across tiers)"
    expect(describeSaveError(apiError(409, collision), 'edit', 'wf')).toEqual({ message: collision })
    const both = "workflow 'wf' has both 'wf.yaml' and 'wf.yml'; remove one of them before updating"
    expect(describeSaveError(apiError(409, both), 'edit', 'wf')).toEqual({ message: both })
    expect(describeSaveError(apiError(400, 'bad yaml'), 'create', 'wf').message).toBe('bad yaml')
    const listed = Object.assign(apiError(400, 'grammar'), { detailMeta: { message: 'grammar', errors: ['e1', 'e2'] } })
    expect(describeSaveError(listed, 'create', 'wf').details).toEqual(['e1', 'e2'])
    expect(describeSaveError(apiError(500), 'create', 'wf').message).toBe('500 Error')
  })

  it('opens read-only, naming the blocking fields, when it cannot represent the workflow', async () => {
    vi.spyOn(api, 'getWorkflowSpec').mockResolvedValue({ ...SPEC, mode: 'parallel' })
    render(<WorkflowBuilder mode="edit" name="demo" onClose={() => {}} onSaved={() => {}} />)
    expect(await screen.findByText('mode: parallel')).toBeInTheDocument()
    expect(screen.getByText(/cannot edit this workflow without losing data/)).toBeInTheDocument()
    expect(screen.getByText('/wf/demo.yaml')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled()
  })

  it('Validate shows the reserved notes for a reserved call type', async () => {
    vi.spyOn(api, 'getWorkflowSpec').mockResolvedValue(SPEC)
    const note = "construct 'assign' is reserved (not built yet)"
    const validate = vi.spyOn(api, 'validateWorkflowSource').mockResolvedValue({ status: 'pass_reserved', errors: [], reserved_notes: [note] })
    render(<WorkflowBuilder mode="edit" name="demo" onClose={() => {}} onSaved={() => {}} />)
    await screen.findByTestId('builder-fixed-name')
    fireEvent.click(screen.getByRole('button', { name: 'Validate' }))
    expect(await screen.findByText(note)).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent(/not built yet/)
    expect(validate).toHaveBeenCalledWith(expect.stringContaining('"call": "assign"'), 'yaml')
    // The result describes the YAML it checked, so an edit clears it.
    fireEvent.change(screen.getByLabelText('Prompt for step 1'), { target: { value: 'Changed.' } })
    expect(screen.queryByText(note)).toBeNull()
  })

  it('Validate lists the server errors of a failing spec', async () => {
    vi.spyOn(api, 'getWorkflowSpec').mockResolvedValue(SPEC)
    vi.spyOn(api, 'validateWorkflowSource').mockResolvedValue({ status: 'fail', errors: ["step 'check': retries 11 out of range (0..10)"] })
    render(<WorkflowBuilder mode="edit" name="demo" onClose={() => {}} onSaved={() => {}} />)
    await screen.findByTestId('builder-fixed-name')
    fireEvent.click(screen.getByRole('button', { name: 'Validate' }))
    expect(await screen.findByText("step 'check': retries 11 out of range (0..10)")).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('Validation failed.')
  })

  it('adds, reorders and removes steps, and the YAML and the flow follow', async () => {
    render(<WorkflowBuilder mode="create" onClose={() => {}} onSaved={() => {}} />)
    await settle()
    fireEvent.click(screen.getByRole('button', { name: /Add step/ }))
    expect((screen.getByLabelText('Step 2 id') as HTMLInputElement).value).toBe('step-2')
    fireEvent.change(screen.getByLabelText('Step 2 id'), { target: { value: 'second' } })
    fireEvent.change(screen.getByLabelText('Call type for step 2'), { target: { value: 'assign' } })
    fireEvent.click(screen.getByRole('button', { name: 'Move step 2 up' }))
    const yaml = () => screen.getByLabelText('YAML preview').textContent as string
    expect(yaml().indexOf('"id": "second"')).toBeLessThan(yaml().indexOf('"id": "step-1"'))
    const flow = screen.getByRole('group', { name: 'Flow preview' })
    expect(within(flow).getByTestId('flow-step-1')).toHaveTextContent('second')
    expect(within(flow).getByTestId('flow-edge-1')).toHaveAttribute('data-reserved', 'true')
    expect((screen.getByLabelText('Call type for step 1') as HTMLSelectElement).value).toBe('assign')
    fireEvent.click(screen.getByRole('button', { name: 'Remove step 1' }))
    expect(yaml()).not.toContain('"second"')
    expect(within(flow).queryByTestId('flow-step-2')).toBeNull()
  })

  it('carries the secrets note and the note that saving drops comments', async () => {
    render(<WorkflowBuilder mode="create" onClose={() => {}} onSaved={() => {}} />)
    await settle()
    expect(screen.getByText(/journaled in plaintext with every run, so do not enter secrets/)).toBeInTheDocument()
    expect(screen.getByText(/Params are journaled in plaintext, so do not enter secrets/)).toBeInTheDocument()
    expect(screen.getByText('Saving rewrites the file from this form. Comments in the file are not kept.')).toBeInTheDocument()
  })

  it.each([
    ['create', () => <WorkflowBuilder mode="create" onClose={() => {}} onSaved={() => {}} />],
    ['edit', () => <WorkflowBuilder mode="edit" name="demo" onClose={() => {}} onSaved={() => {}} />],
  ])('says what a YAML workflow cannot do and links to the workflow docs (%s)', async (_mode, ui) => {
    vi.spyOn(api, 'getWorkflowSpec').mockResolvedValue(SPEC)
    render(ui())
    const note = await screen.findByRole('note', { name: 'What YAML workflows cannot do' })
    expect(note).toHaveTextContent('A YAML workflow runs a fixed list of steps, one at a time, in order.')
    expect(note).toHaveTextContent('branch on a condition, loop, or run steps in parallel')
    expect(note).toHaveTextContent("pass one step's answer to a later step")
    expect(note).toHaveTextContent('run assign or send_message steps, which are not built yet')
    expect(note).toHaveTextContent('For these, write a Python workflow.')
    const link = within(note).getByRole('link', { name: 'workflow docs' })
    expect(link).toHaveAttribute('href', 'https://github.com/awslabs/cli-agent-orchestrator/blob/main/docs/workflows.md')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
  })

  it('shows no limits note on a workflow it cannot edit', async () => {
    vi.spyOn(api, 'getWorkflowSpec').mockResolvedValue({ ...SPEC, mode: 'parallel' })
    render(<WorkflowBuilder mode="edit" name="demo" onClose={() => {}} onSaved={() => {}} />)
    await screen.findByText('mode: parallel')
    expect(screen.queryByRole('note', { name: 'What YAML workflows cannot do' })).toBeNull()
  })
})

describe('WorkflowFlow', () => {
  const STEPS = [
    { id: 'send', call: 'handoff' as const, agent: 'sqs-sender-beta', provider: 'claude_code' },
    { id: 'fan', call: 'assign' as const },
    { id: 'msg', call: 'send_message' as const },
    { id: 'known-later' },
  ]

  it('draws the orchestrator, then one node per step with an edge labelled by number and call type', () => {
    render(<WorkflowFlow steps={STEPS} />)
    expect(screen.getByTestId('flow-orchestrator')).toHaveTextContent('Orchestrator (CAO engine)')
    expect(screen.getByText('1 · handoff')).toBeInTheDocument()
    expect(screen.getByText('2 · assign')).toBeInTheDocument()
    expect(screen.getByText('3 · send_message')).toBeInTheDocument()
    // No call type known: the edge carries the step number only.
    expect(within(screen.getByTestId('flow-edge-4')).getByText('4')).toBeInTheDocument()
    expect(screen.getByTestId('flow-step-1')).toHaveTextContent('sqs-sender-beta on claude_code')
    const ids = screen.getAllByTestId(/^flow-step-\d$/).map(li => li.querySelector('span.font-mono')?.textContent)
    expect(ids).toEqual(['send', 'fan', 'msg', 'known-later'])
  })

  it('draws reserved call types dashed and labelled not built yet', () => {
    render(<WorkflowFlow steps={STEPS} />)
    expect(screen.getByTestId('flow-edge-line-1')).toHaveClass('border-solid')
    expect(screen.getByTestId('flow-edge-line-1')).not.toHaveClass('border-dashed')
    for (const n of [2, 3]) {
      expect(screen.getByTestId(`flow-edge-line-${n}`)).toHaveClass('border-dashed')
      expect(screen.getByTestId(`flow-edge-${n}`)).toHaveAttribute('data-reserved', 'true')
      expect(within(screen.getByTestId(`flow-edge-${n}`)).getByText('not built yet')).toBeInTheDocument()
    }
    expect(screen.getAllByText('not built yet')).toHaveLength(2)
    expect(screen.getByTestId('flow-edge-4')).toHaveAttribute('data-reserved', 'false')
  })

  it('shows each state as text with an icon and a glyph, never colour alone', () => {
    render(<WorkflowFlow steps={STEPS} states={{ send: 'completed_unvalidated', fan: 'running', msg: 'rerun_authorized' }} />)
    expect(screen.getByTestId('flow-state-1')).toHaveTextContent('Completed, output not validated')
    expect(screen.getByTestId('flow-state-2')).toHaveTextContent('Running')
    expect(screen.getByTestId('flow-state-3')).toHaveTextContent('Rerun authorized')
    expect(screen.getByTestId('flow-state-4')).toHaveTextContent('Pending')
    for (const n of [1, 2, 3, 4]) {
      const state = screen.getByTestId(`flow-state-${n}`)
      expect(state.querySelector('svg')).not.toBeNull()
      expect(state.querySelector('[aria-hidden="true"]:not(svg)')?.textContent).toBeTruthy()
    }
  })

  it('maps every journal state to text, and shows an unknown state as is', () => {
    const states = ['pending', 'running', 'completed', 'failed', 'skipped', 'completed_unvalidated', 'rerun_authorized', 'replay_authorized', 'retried', 'cancelled']
    expect(states.map(s => flowStateView(s).text)).toEqual([
      'Pending', 'Running', 'Completed', 'Failed', 'Skipped', 'Completed, output not validated',
      'Rerun authorized', 'Replay authorized', 'Retried', 'Cancelled',
    ])
    expect(flowStateView('mystery').text).toBe('mystery')
    expect(flowStateView(undefined).text).toBe('Pending')
  })

  it('shows no state without a states map, and an empty note without steps', () => {
    const { rerender } = render(<WorkflowFlow steps={STEPS} />)
    expect(screen.queryByTestId('flow-state-1')).toBeNull()
    rerender(<WorkflowFlow steps={[]} />)
    expect(screen.getByText('No steps yet.')).toBeInTheDocument()
    expect(screen.getByTestId('flow-orchestrator')).toBeInTheDocument()
  })
})

function inspection(state: string, steps: RunInspection['steps']): RunInspection {
  return { run_id: 'run-1', workflow_name: 'demo', state, current_step_id: null, started_at: '2026-11-08T00:00:00Z', finished_at: null, tier: 'yaml', steps }
}

function ev(seq: number, event_type: string, step_id: string, state?: string): WorkflowEvent {
  return { run_id: 'run-1', seq, event_type, event_schema_version: 1, ts: '', step_id, state }
}

describe('run flow helpers', () => {
  const run = inspection('running', [
    { id: 'a', state: 'running', attempts: 1 },
    { id: 'b', state: 'completed_unvalidated', attempts: 1 },
    { id: 'c', state: 'rerun_authorized', attempts: 1 },
    { id: 'd', state: 'pending', attempts: 0 },
    { id: 'e', state: 'running', attempts: 2 },
  ])

  it('lets the event fold win while a step is pending or running, and the inspection once it settles', () => {
    const events = [
      ev(1, 'step.started', 'a', 'running'), ev(2, 'step.completed', 'a', 'completed'),
      ev(3, 'step.completed', 'b', 'completed_unvalidated'),
      ev(4, 'step.failed', 'c'),
      ev(5, 'step.started', 'e', 'running'), ev(6, 'step.attempt.failed', 'e'),
    ]
    expect(runStepStates(run, events)).toEqual({ a: 'completed', b: 'completed_unvalidated', c: 'rerun_authorized', d: 'pending', e: 'retried' })
    expect(runStepStates(run, [])).toEqual({ a: 'running', b: 'completed_unvalidated', c: 'rerun_authorized', d: 'pending', e: 'running' })
  })

  it('takes the call type from the spec by step id, and none without a spec', () => {
    const spec = [specStep('a', 'handoff'), specStep('b', null), specStep('c', 'send_message')]
    expect(runFlowSteps(run, null).map(s => s.call)).toEqual([undefined, undefined, undefined, undefined, undefined])
    expect(runFlowSteps(run, spec).map(s => s.call)).toEqual(['handoff', 'handoff', 'send_message', undefined, undefined])
    expect(runFlowSteps(inspection('running', []), spec).map(s => s.id)).toEqual(['a', 'b', 'c'])
  })
})

describe('RunDetail shows the flow', () => {
  beforeEach(() => {
    useStore.setState({ workflowRuns: [], selectedRun: null, selectedRunId: null, wfEvents: [], wfGaps: [], snackbar: null })
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 500, statusText: 'Error', json: async () => ({}), text: async () => '' })))
  })

  it('draws the run with the spec call types and the step states', async () => {
    vi.spyOn(api, 'getWorkflowSpec').mockResolvedValue({ ...SPEC, steps: [specStep('send', 'handoff'), specStep('fan', 'assign')] })
    render(<RunDetail run={inspection('completed', [{ id: 'send', state: 'completed', attempts: 1 }, { id: 'fan', state: 'failed', attempts: 2 }])} allRuns={[]} />)
    const flow = screen.getByRole('group', { name: 'Run flow' })
    expect(within(flow).getByText('Orchestrator (CAO engine)')).toBeInTheDocument()
    expect(await within(flow).findByText('1 · handoff')).toBeInTheDocument()
    expect(within(flow).getByText('2 · assign')).toBeInTheDocument()
    expect(within(flow).getByText('not built yet')).toBeInTheDocument()
    expect(within(flow).getByTestId('flow-state-1')).toHaveTextContent('Completed')
    expect(within(flow).getByTestId('flow-state-2')).toHaveTextContent('Failed')
    expect(api.getWorkflowSpec).toHaveBeenCalledWith('demo')
  })

  it('follows the events for live states, and shows no call label when the spec read fails', async () => {
    const spec = vi.spyOn(api, 'getWorkflowSpec').mockRejectedValue(apiError(404, "unknown workflow 'demo'"))
    useStore.setState({ wfEvents: [ev(1, 'step.started', 'send', 'running'), ev(2, 'step.completed', 'send', 'completed'), ev(3, 'step.started', 'fan', 'running')] })
    render(<RunDetail run={inspection('running', [{ id: 'send', state: 'running', attempts: 1 }, { id: 'fan', state: 'pending', attempts: 0 }])} allRuns={[]} />)
    const flow = screen.getByRole('group', { name: 'Run flow' })
    expect(within(flow).getByTestId('flow-state-1')).toHaveTextContent('Completed')
    expect(within(flow).getByTestId('flow-state-2')).toHaveTextContent('Running')
    await waitFor(() => expect(spec).toHaveBeenCalled())
    await new Promise(r => setTimeout(r, 0))
    expect(within(flow).getByTestId('flow-edge-1')).toHaveAttribute('data-call', '')
    expect(within(flow).queryByText(/handoff/)).toBeNull()
  })
})

const YAML_DEF: WorkflowDefRow = { name: 'demo', source_path: '/wf/demo.yaml', mode: 'sequential', step_count: 2, description: '', indexed_at: '' }
const PY_DEF: WorkflowDefRow = { name: 'lint', source_path: '/wf/lint.py', mode: 'script', step_count: null, description: '', indexed_at: '' }

describe('WorkflowLauncher: Edit and Delete for YAML workflows', () => {
  beforeEach(() => {
    vi.spyOn(api, 'getWorkflowSpec').mockResolvedValue({ name: 'demo', inputs: {} })
  })

  it('tells a YAML workflow by its source path', () => {
    expect(isYamlWorkflow({ source_path: '/wf/a.yaml' })).toBe(true)
    expect(isYamlWorkflow({ source_path: '/wf/a.YML' })).toBe(true)
    expect(isYamlWorkflow({ source_path: '/wf/a.py' })).toBe(false)
    expect(isYamlWorkflow({ source_path: '/wf/a.yaml.py' })).toBe(false)
  })

  it('offers Edit and Delete only for a YAML workflow', async () => {
    vi.spyOn(api, 'listWorkflows').mockResolvedValue([YAML_DEF, PY_DEF])
    const onEdit = vi.fn()
    render(<WorkflowLauncher onEdit={onEdit} />)
    fireEvent.click(screen.getByRole('button', { name: /Run a workflow/ }))
    fireEvent.click(await screen.findByRole('button', { name: 'Edit workflow demo' }))
    expect(onEdit).toHaveBeenCalledWith('demo')
    expect(screen.getByRole('button', { name: 'Delete workflow demo' })).toBeInTheDocument()
    fireEvent.change(screen.getByDisplayValue('demo'), { target: { value: 'lint' } })
    await waitFor(() => expect(screen.queryByRole('button', { name: /Edit workflow/ })).toBeNull())
    expect(screen.queryByRole('button', { name: /Delete workflow/ })).toBeNull()
    // The run form is still there for the Python workflow.
    expect(screen.getByRole('button', { name: /Run workflow/ })).toBeEnabled()
  })

  it('enables the delete confirm only once the exact name is typed, then deletes and re-reads the list', async () => {
    let deleted = false
    const list = vi.spyOn(api, 'listWorkflows').mockImplementation(async () => (deleted ? [PY_DEF] : [YAML_DEF, PY_DEF]))
    const del = vi.spyOn(api, 'deleteWorkflow').mockImplementation(async () => {
      deleted = true
      return { success: true, name: 'demo' }
    })
    render(<WorkflowLauncher onEdit={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: /Run a workflow/ }))
    fireEvent.click(await screen.findByRole('button', { name: 'Delete workflow demo' }))
    const dialog = screen.getByRole('alertdialog')
    const confirm = within(dialog).getByRole('button', { name: 'Delete' })
    const typed = within(dialog).getByLabelText('Confirmation text')
    expect(confirm).toBeDisabled()
    for (const wrong of ['dem', 'DEMO', 'demo ', ' demo']) {
      fireEvent.change(typed, { target: { value: wrong } })
      expect(confirm, wrong).toBeDisabled()
    }
    fireEvent.click(confirm)
    expect(del).not.toHaveBeenCalled()
    fireEvent.change(typed, { target: { value: 'demo' } })
    expect(confirm).toBeEnabled()
    const readsBefore = list.mock.calls.length
    fireEvent.click(confirm)
    await waitFor(() => expect(del).toHaveBeenCalledWith('demo'))
    // The list is read again and the picker moves to the workflow that is left.
    await waitFor(() => expect(screen.getByDisplayValue('lint')).toBeInTheDocument())
    expect(list.mock.calls.length).toBeGreaterThan(readsBefore)
    expect(screen.queryByRole('option', { name: 'demo' })).toBeNull()
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(useStore.getState().snackbar).toMatchObject({ type: 'success', message: 'Deleted workflow demo' })
  })
})

describe('WorkflowsPanel: New workflow', () => {
  beforeEach(() => {
    useStore.setState({ workflowRuns: [], selectedRun: null, selectedRunId: null, wfEvents: [], wfGaps: [] })
    vi.spyOn(api, 'listWorkflowRuns').mockResolvedValue([])
    vi.spyOn(api, 'listProviders').mockResolvedValue(PROVIDERS)
    vi.spyOn(api, 'searchProfiles').mockImplementation(async q => [result(q)])
    vi.spyOn(api, 'getProfile').mockImplementation(async name => ({ name, description: '', provider: 'claude_code' }))
  })

  it('opens the builder, and after a save opens the launcher on the saved workflow', async () => {
    const saved: WorkflowDefRow = { ...YAML_DEF, name: 'my-wf', source_path: '/wf/my-wf.yaml' }
    const list = vi.spyOn(api, 'listWorkflows').mockResolvedValue([PY_DEF, saved])
    vi.spyOn(api, 'getWorkflowSpec').mockResolvedValue({ name: 'my-wf', inputs: {} })
    vi.spyOn(api, 'createWorkflow').mockResolvedValue({
      name: 'my-wf', path: '/wf/my-wf.yaml', content_hash: 'h', findings: [], status: 'pass_reserved',
      reserved_notes: ["construct 'assign' is reserved (not built yet)"],
    })
    render(<WorkflowsPanel />)
    fireEvent.click(screen.getByRole('button', { name: /New workflow/ }))
    const dialog = screen.getByRole('dialog', { name: 'New workflow' })
    fireEvent.change(within(dialog).getByLabelText('Workflow name'), { target: { value: 'my-wf' } })
    await searchAndPick(1, 'dev')
    await waitFor(() => expect((within(dialog).getByLabelText('Provider for step 1') as HTMLSelectElement).value).toBe('claude_code'))
    fireEvent.change(within(dialog).getByLabelText('Call type for step 1'), { target: { value: 'assign' } })
    fireEvent.change(within(dialog).getByLabelText('Prompt for step 1'), { target: { value: 'Go.' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create workflow' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'New workflow' })).toBeNull())
    expect(await screen.findByRole('button', { name: 'Edit workflow my-wf' })).toBeInTheDocument()
    expect(screen.getByDisplayValue('my-wf')).toBeInTheDocument()
    expect(list).toHaveBeenCalled()
    expect(useStore.getState().snackbar).toMatchObject({ type: 'info' })
    expect(useStore.getState().snackbar?.message).toMatch(/not built yet/)
  })

  it('Edit in the launcher opens the builder on that workflow', async () => {
    vi.spyOn(api, 'listWorkflows').mockResolvedValue([YAML_DEF])
    vi.spyOn(api, 'getWorkflowSpec').mockResolvedValue(SPEC)
    render(<WorkflowsPanel />)
    fireEvent.click(screen.getByRole('button', { name: /Run a workflow/ }))
    fireEvent.click(await screen.findByRole('button', { name: 'Edit workflow demo' }))
    const dialog = screen.getByRole('dialog', { name: 'Edit workflow demo' })
    expect(await within(dialog).findByTestId('builder-fixed-name')).toHaveTextContent('demo')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog', { name: 'Edit workflow demo' })).toBeNull()
  })
})

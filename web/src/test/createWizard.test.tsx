import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { CreateProfileWizard } from '../components/CreateProfileWizard'
import { ProfileForm, ProfileFormValues } from '../components/ProfileForm'
import { api } from '../api'
import { useState } from 'react'

// #510 U3: create wizard + shared ProfileForm. All scaffolding/validation/write
// is server-side; these tests assert the flow wires to the (mocked) endpoints
// and enforces the client-side gates (provider+model required, preview renders
// server output, save blocked on validation errors, F-1 keeps provider/model
// out of the render config).

const TEMPLATES = [
  { name: 'aws/sqs-monitor', description: 'Poll an SQS queue', path: '/x' },
  { name: 'aws/stepfunction', description: 'Trigger Step Functions', path: '/y' },
]

const SCHEMA = {
  type: 'object',
  properties: {
    region: { type: 'string', description: 'AWS region' },
    queue_url: { type: 'string', description: 'SQS queue URL' },
  },
  required: ['region', 'queue_url'],
  additionalProperties: false,
}

// Provider renders as a CustomSelect when the server returns providers; select
// claude_code through the dropdown (label is the underscore→space form).
function selectClaudeProvider() {
  fireEvent.click(screen.getByText('Select provider…'))
  fireEvent.click(screen.getByText('claude code'))
}

function fillModel(value = 'opus') {
  fireEvent.change(screen.getByPlaceholderText('e.g. claude-sonnet-4'), { target: { value } })
}

describe('CreateProfileWizard', () => {
  let listTemplatesSpy: any
  let getSchemaSpy: any
  let previewSpy: any
  let createSpy: any

  beforeEach(() => {
    listTemplatesSpy = vi.spyOn(api, 'listTemplates').mockResolvedValue(TEMPLATES as any)
    getSchemaSpy = vi.spyOn(api, 'getTemplateSchema').mockResolvedValue(SCHEMA as any)
    previewSpy = vi.spyOn(api, 'previewProfile').mockResolvedValue({
      text: '---\nname: sqs-monitor-agent\nprovider: claude_code\nmodel: opus\n---\nbody',
      valid: true, errors: [], warnings: [],
    })
    createSpy = vi.spyOn(api, 'createProfile').mockResolvedValue({
      name: 'sqs-monitor-agent', source: 'local', path: '/store/sqs-monitor-agent.md',
    })
    vi.spyOn(api, 'listProviders').mockResolvedValue([
      { name: 'claude_code', binary: 'claude', installed: true },
    ] as any)
  })

  afterEach(() => vi.restoreAllMocks())

  it('step 1 lists templates from the endpoint (no hardcoded list) [AC4.1]', async () => {
    render(<CreateProfileWizard onClose={() => {}} />)
    await screen.findByTestId('template-picker')
    expect(listTemplatesSpy).toHaveBeenCalled()
    expect(screen.getByText('aws/sqs-monitor')).toBeInTheDocument()
    expect(screen.getByText('aws/stepfunction')).toBeInTheDocument()
  })

  it('picking a template loads its schema and advances to the form', async () => {
    render(<CreateProfileWizard onClose={() => {}} />)
    await screen.findByTestId('template-picker')
    fireEvent.click(screen.getByText('aws/sqs-monitor'))
    await screen.findByTestId('profile-form')
    await waitFor(() => expect(getSchemaSpy).toHaveBeenCalledWith('aws/sqs-monitor'))
    // Schema-driven field rendered.
    expect(screen.getByLabelText(/Region/)).toBeInTheDocument()
  })

  it('Preview is disabled until provider AND model are filled [ADR-006/AC4.4]', async () => {
    render(<CreateProfileWizard onClose={() => {}} />)
    await screen.findByTestId('template-picker')
    fireEvent.click(screen.getByText('aws/sqs-monitor'))
    await screen.findByTestId('profile-form')

    const previewBtn = screen.getByText('Preview').closest('button')!
    expect(previewBtn).toBeDisabled()

    // Fill provider only → still disabled.
    selectClaudeProvider()
    expect(previewBtn).toBeDisabled()

    // Fill model too → enabled.
    fillModel()
    expect(previewBtn).not.toBeDisabled()
  })

  it('Preview calls the server render and shows its output [AC4.2]', async () => {
    render(<CreateProfileWizard onClose={() => {}} />)
    await screen.findByTestId('template-picker')
    fireEvent.click(screen.getByText('aws/sqs-monitor'))
    await screen.findByTestId('profile-form')
    selectClaudeProvider()
    fillModel()
    fireEvent.click(screen.getByText('Preview'))

    await screen.findByTestId('preview-panel')
    expect(previewSpy).toHaveBeenCalledWith(expect.objectContaining({
      template_name: 'aws/sqs-monitor', provider: 'claude_code', model: 'opus',
    }))
    // The preview panel shows the SERVER-rendered text.
    expect(screen.getByText(/name: sqs-monitor-agent/)).toBeInTheDocument()
  })

  it('preview F-1: provider/model are NOT put into the render config', async () => {
    render(<CreateProfileWizard onClose={() => {}} />)
    await screen.findByTestId('template-picker')
    fireEvent.click(screen.getByText('aws/sqs-monitor'))
    await screen.findByTestId('profile-form')
    fireEvent.change(screen.getByLabelText(/Region/), { target: { value: 'us-east-1' } })
    selectClaudeProvider()
    fillModel()
    fireEvent.click(screen.getByText('Preview'))
    await screen.findByTestId('preview-panel')

    const sentConfig = previewSpy.mock.calls[0][0].config
    expect(sentConfig).not.toHaveProperty('provider')
    expect(sentConfig).not.toHaveProperty('model')
    expect(sentConfig.region).toBe('us-east-1')
  })

  it('invalid config surfaces the preview error and stays on the form [AC4.2]', async () => {
    previewSpy.mockRejectedValueOnce(Object.assign(new Error('400'), { detail: 'region: required' }))
    render(<CreateProfileWizard onClose={() => {}} />)
    await screen.findByTestId('template-picker')
    fireEvent.click(screen.getByText('aws/sqs-monitor'))
    await screen.findByTestId('profile-form')
    selectClaudeProvider()
    fillModel()
    fireEvent.click(screen.getByText('Preview'))

    expect(await screen.findByText(/Preview failed: region: required/)).toBeInTheDocument()
    // Still on the form, not the preview panel.
    expect(screen.getByTestId('profile-form')).toBeInTheDocument()
    expect(screen.queryByTestId('preview-panel')).not.toBeInTheDocument()
  })

  it('Save is blocked while the preview reports validation errors [AC4.5]', async () => {
    previewSpy.mockResolvedValueOnce({
      text: '---\nname: bad name\n---\n', valid: false,
      errors: ['[error] name: does not match pattern'], warnings: [],
    })
    render(<CreateProfileWizard onClose={() => {}} />)
    await screen.findByTestId('template-picker')
    fireEvent.click(screen.getByText('aws/sqs-monitor'))
    await screen.findByTestId('profile-form')
    selectClaudeProvider()
    fillModel()
    fireEvent.click(screen.getByText('Preview'))

    await screen.findByTestId('preview-panel')
    expect(screen.getByText(/save blocked/i)).toBeInTheDocument()
    expect(screen.getByText('Save Profile').closest('button')).toBeDisabled()
    expect(createSpy).not.toHaveBeenCalled()
  })

  it('Save calls createProfile and reports the saved profile on success [AC4.5]', async () => {
    const onClose = vi.fn()
    render(<CreateProfileWizard onClose={onClose} />)
    await screen.findByTestId('template-picker')
    fireEvent.click(screen.getByText('aws/sqs-monitor'))
    await screen.findByTestId('profile-form')
    fireEvent.change(screen.getByLabelText(/Region/), { target: { value: 'us-east-1' } })
    selectClaudeProvider()
    fillModel()
    fireEvent.click(screen.getByText('Preview'))
    await screen.findByTestId('preview-panel')
    fireEvent.click(screen.getByText('Save Profile'))

    await waitFor(() => expect(createSpy).toHaveBeenCalledWith(expect.objectContaining({
      template_name: 'aws/sqs-monitor', provider: 'claude_code', model: 'opus',
    })))
    await waitFor(() => expect(onClose).toHaveBeenCalledWith(expect.objectContaining({ name: 'sqs-monitor-agent', source: 'local' })))
  })

  it('save F-1: provider/model are NOT put into the createProfile config either', async () => {
    // The preview F-1 test guards the /preview call; this pins the SAME rule on
    // the /agents/profiles (create) call, so a regression that leaked
    // provider/model into config only on the save path is still caught.
    render(<CreateProfileWizard onClose={() => {}} />)
    await screen.findByTestId('template-picker')
    fireEvent.click(screen.getByText('aws/sqs-monitor'))
    await screen.findByTestId('profile-form')
    fireEvent.change(screen.getByLabelText(/Region/), { target: { value: 'us-east-1' } })
    selectClaudeProvider()
    fillModel()
    fireEvent.click(screen.getByText('Preview'))
    await screen.findByTestId('preview-panel')
    fireEvent.click(screen.getByText('Save Profile'))

    await waitFor(() => expect(createSpy).toHaveBeenCalled())
    const sentConfig = createSpy.mock.calls[0][0].config
    expect(sentConfig).not.toHaveProperty('provider')
    expect(sentConfig).not.toHaveProperty('model')
    expect(sentConfig.region).toBe('us-east-1')
  })
})

// A tiny controlled harness to exercise ProfileForm directly. (ProfileForm is
// create-only as of #510 U4 — the schema-only 'edit' mode had no production
// consumer and was removed; U4's editor is EditCloneModal's content textarea.)
function FormHarness({ schema }: { schema?: any }) {
  const [values, setValues] = useState<ProfileFormValues>({ config: {}, provider: '', model: '' })
  return (
    <div>
      <ProfileForm schema={schema} values={values} onChange={setValues} />
      <div data-testid="values">{JSON.stringify(values)}</div>
    </div>
  )
}

describe('ProfileForm (create-from-template, U3-owned)', () => {
  beforeEach(() => {
    vi.spyOn(api, 'listProviders').mockResolvedValue([
      { name: 'claude_code', binary: 'claude', installed: true },
    ] as any)
  })
  afterEach(() => vi.restoreAllMocks())

  it('always renders explicit required provider and model fields', async () => {
    render(<FormHarness schema={SCHEMA} />)
    await screen.findByTestId('profile-form')
    expect(screen.getByText('Provider')).toBeInTheDocument()
    expect(screen.getByText('Model')).toBeInTheDocument()
    // model field is marked required for a11y.
    expect(screen.getByLabelText(/Model/)).toHaveAttribute('aria-required', 'true')
  })

  it('renders schema-driven config fields with required markers', async () => {
    render(<FormHarness schema={SCHEMA} />)
    await screen.findByTestId('profile-form')
    expect(screen.getByLabelText(/Region/)).toBeInTheDocument()
    expect(screen.getByLabelText(/Queue Url/)).toBeInTheDocument()
  })

  it('renders provider/model with no config fields when the schema is absent', async () => {
    // A template whose schema failed to load (pickTemplate catch → schema=null):
    // provider/model still render and are required; no config fields appear.
    render(<FormHarness schema={null} />)
    await screen.findByTestId('profile-form')
    expect(screen.getByText('Provider')).toBeInTheDocument()
    expect(screen.getByText('Model')).toBeInTheDocument()
    expect(screen.queryByLabelText(/Region/)).not.toBeInTheDocument()
  })

  it('config edits do not leak provider/model into config', async () => {
    render(<FormHarness schema={SCHEMA} />)
    await screen.findByTestId('profile-form')
    fireEvent.change(screen.getByLabelText(/Region/), { target: { value: 'us-west-2' } })
    fireEvent.change(screen.getByPlaceholderText('e.g. claude-sonnet-4'), { target: { value: 'opus' } })
    const values = JSON.parse(screen.getByTestId('values').textContent!)
    expect(values.config).toEqual({ region: 'us-west-2' })
    expect(values.config).not.toHaveProperty('provider')
    expect(values.config).not.toHaveProperty('model')
    expect(values.model).toBe('opus')
  })
})

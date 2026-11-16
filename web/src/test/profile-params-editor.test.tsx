// The Params section of Edit and Clone (ProfileEditorModal), the builder's
// Clone button that opens it, and the params list on the profile page
// (ProfilesPanel).
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { api } from '../api'
import type { AgentProfileDetail, ApiError, ProfileParamDecl, ProfileValidationResponse } from '../api'
import { useStore } from '../store'
import { PARAMS_READ_DEBOUNCE_MS, ProfileEditorModal } from '../components/ProfileEditorModal'
import { ProfilesPanel } from '../components/ProfilesPanel'
import { StepEditor } from '../components/workflow/StepEditor'
import { newStep } from '../components/workflow/workflowYaml'

const SOURCE = [
  '---',
  'name: query-agent',
  'description: Query a table',
  '# Install with cao install --env.',
  'params:',
  '  table_name:',
  '    description: "DynamoDB table name"   # the table to read',
  '    required: true',
  '  limit:',
  '    type: int',
  '    default: 1',
  '',
  '# Keep this comment.',
  'provider: claude_code',
  '---',
  '',
  'Query ${TABLE_NAME} in ${AWS_REGION}. Return ${LIMIT} items.',
  '',
].join('\n')

const PARAMS: Record<string, ProfileParamDecl> = {
  table_name: { type: 'string', required: true, description: 'DynamoDB table name' },
  limit: { type: 'int', required: false, default: 1 },
}

const BLOCK_START = SOURCE.indexOf('params:\n')
const BLOCK_END = SOURCE.indexOf('    default: 1\n') + '    default: 1\n'.length

interface Deferred<T> {
  promise: Promise<T>
  resolve: (value: T) => void
}

function deferred<T>(): Deferred<T> {
  let resolve: (value: T) => void = () => {}
  const promise = new Promise<T>(r => { resolve = r })
  return { promise, resolve }
}

function apiError(status: number): ApiError {
  return Object.assign(new Error(`${status} Error`), { status })
}

const flush = async () => {
  for (let i = 0; i < 4; i++) await act(async () => {})
}

function mockEditor({ source = SOURCE, validate }: {
  source?: string
  validate?: (content: string) => Promise<ProfileValidationResponse>
} = {}) {
  vi.spyOn(api, 'getProfileSource').mockImplementation(async name => ({ name, content: source }))
  return {
    validate: vi.spyOn(api, 'validateProfile').mockImplementation(validate ?? (async () => ({ valid: true, messages: [], params: PARAMS }))),
    put: vi.spyOn(api, 'replaceProfile').mockImplementation(async name => ({ name, warnings: [] })),
    post: vi.spyOn(api, 'createProfile').mockImplementation(async name => ({ name, warnings: [] })),
  }
}

async function openEditor(mode: 'edit' | 'clone' = 'edit') {
  render(<ProfileEditorModal open mode={mode} name="query-agent" onClose={() => {}} onSaved={() => {}} />)
  await flush()
}

const section = () => screen.getByRole('region', { name: 'Params a workflow step must fill' })
const status = () => within(section()).getByTestId('params-status')
const sourceText = () => (screen.getByRole('textbox', { name: 'Profile source' }) as HTMLTextAreaElement).value
const typeSource = (value: string) => fireEvent.change(screen.getByRole('textbox', { name: 'Profile source' }), { target: { value } })
const saveButton = () => screen.getByRole('button', { name: /save changes/i })
const offered = () => within(section()).queryAllByRole('button', { name: /^\+ / }).map(b => b.textContent)

beforeEach(() => {
  useStore.setState({ snackbar: null, navLockCount: 0 })
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('Edit: the Params section', () => {
  it('shows the rows the validate response gives for the loaded text, above the text', async () => {
    const { validate } = mockEditor()
    await openEditor()
    expect(validate).toHaveBeenCalledTimes(1)
    expect(validate).toHaveBeenCalledWith(SOURCE)
    expect(screen.getByLabelText('Param 1 name')).toHaveValue('table_name')
    expect(screen.getByLabelText('Param 1 required')).toBeChecked()
    expect(screen.getByLabelText('Param 1 default')).toBeDisabled()
    expect(screen.getByLabelText('Param 1 description')).toHaveValue('DynamoDB table name')
    expect(screen.getByLabelText('Param 2 name')).toHaveValue('limit')
    expect(screen.getByLabelText('Param 2 type')).toHaveValue('int')
    expect(screen.getByLabelText('Param 2 default')).toHaveValue('1')
    expect(status()).toHaveTextContent(
      'Changing a param here rewrites the params block in the text below. Comments inside that block are not kept.',
    )
    const box = screen.getByRole('textbox', { name: 'Profile source' })
    expect(section().compareDocumentPosition(box) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // From the body: table_name and limit cover two placeholders.
    expect(offered()).toEqual(['+ AWS_REGION'])
  })

  it('a row change rewrites only the params block and starts no read, and Save sends that text', async () => {
    const { validate, put } = mockEditor()
    await openEditor()
    fireEvent.change(screen.getByLabelText('Param 2 default'), { target: { value: '25' } })
    const text = sourceText()
    const tail = SOURCE.slice(BLOCK_END)
    expect(text.slice(0, BLOCK_START)).toBe(SOURCE.slice(0, BLOCK_START))
    expect(text.slice(text.length - tail.length)).toBe(tail)
    expect(text.slice(BLOCK_START, text.length - tail.length)).toBe([
      'params:',
      '  table_name:',
      '    description: "DynamoDB table name"',
      '    required: true',
      '  limit:',
      '    type: int',
      '    default: 25',
      '',
    ].join('\n'))
    expect(validate).toHaveBeenCalledTimes(1)
    expect(status()).toHaveTextContent('Changing a param here')

    fireEvent.click(saveButton())
    await flush()
    expect(put).toHaveBeenCalledWith('query-agent', text)
  })

  it('saving without touching the rows sends the loaded text unchanged', async () => {
    const { put } = mockEditor()
    await openEditor()
    fireEvent.click(saveButton())
    await flush()
    expect(put).toHaveBeenCalledWith('query-agent', SOURCE)
  })

  it('putting a row back as it was restores the loaded text, comments and all', async () => {
    mockEditor()
    await openEditor()
    fireEvent.change(screen.getByLabelText('Param 1 description'), { target: { value: 'Table to read' } })
    expect(sourceText()).not.toContain('# the table to read')
    fireEvent.change(screen.getByLabelText('Param 1 description'), { target: { value: 'DynamoDB table name' } })
    expect(sourceText()).toBe(SOURCE)
  })

  it('a row error blocks Save, and the text keeps its last good block', async () => {
    mockEditor()
    await openEditor()
    fireEvent.change(screen.getByLabelText('Param 2 default'), { target: { value: 'ten' } })
    expect(screen.getByLabelText('Param 2 default')).toHaveAccessibleDescription('Use a whole number, up to 18 digits.')
    expect(sourceText()).toBe(SOURCE)
    expect(saveButton()).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Param 2 default'), { target: { value: '10' } })
    expect(saveButton()).toBeEnabled()
    expect(sourceText()).toContain('  limit:\n    type: int\n    default: 10\n\n# Keep this comment.\n')
  })

  it('adds a block from a suggestion to a profile that has none, and removing the row restores the text', async () => {
    const plain = '---\nname: query-agent\ndescription: Query a table\n---\n\nQuery ${TABLE_NAME}.\n'
    mockEditor({ source: plain, validate: async () => ({ valid: true, messages: [], params: null }) })
    await openEditor()
    expect(within(section()).getByText('No params yet.')).toBeInTheDocument()
    fireEvent.click(within(section()).getByRole('button', { name: '+ TABLE_NAME' }))
    expect(sourceText()).toBe(
      '---\nname: query-agent\ndescription: Query a table\nparams:\n  table_name:\n    required: true\n---\n\nQuery ${TABLE_NAME}.\n',
    )
    fireEvent.click(screen.getByRole('button', { name: 'Remove param 1' }))
    expect(sourceText()).toBe(plain)
  })

  it('typing in the text reads it again after a pause, with the rows disabled meanwhile', async () => {
    vi.useFakeTimers()
    const { validate } = mockEditor({
      validate: async content => ({
        valid: true,
        messages: [],
        params: content.includes('  region:') ? { ...PARAMS, region: { type: 'string', required: true } } : PARAMS,
      }),
    })
    await openEditor()
    const typed = SOURCE.replace('    default: 1\n', '    default: 1\n  region:\n    required: true\n')
    typeSource(typed)
    expect(status()).toHaveTextContent('Reading the params from the text...')
    expect(screen.getByLabelText('Param 1 name')).toBeDisabled()
    expect(screen.getByLabelText('Param 2 default')).toBeDisabled()
    expect(within(section()).getByRole('button', { name: /Add param/ })).toBeDisabled()

    await act(async () => { vi.advanceTimersByTime(PARAMS_READ_DEBOUNCE_MS - 1) })
    expect(validate).toHaveBeenCalledTimes(1)
    await act(async () => { vi.advanceTimersByTime(1) })
    await flush()
    expect(validate).toHaveBeenCalledTimes(2)
    expect(validate).toHaveBeenLastCalledWith(typed)
    expect(screen.getByLabelText('Param 3 name')).toHaveValue('region')
    expect(screen.getByLabelText('Param 1 name')).toBeEnabled()
    expect(sourceText()).toBe(typed)
  })

  it('ignores a read that answers after the text changed', async () => {
    vi.useFakeTimers()
    const reads: Deferred<ProfileValidationResponse>[] = []
    mockEditor({
      validate: () => {
        const read = deferred<ProfileValidationResponse>()
        reads.push(read)
        return read.promise
      },
    })
    await openEditor()
    expect(reads).toHaveLength(1)
    expect(status()).toHaveTextContent('Reading')
    typeSource(SOURCE.replace('# Keep this comment.', '# Edited.'))
    await act(async () => { reads[0].resolve({ valid: true, messages: [], params: { stale_param: { type: 'string', required: true } } }) })
    await flush()
    expect(screen.queryByDisplayValue('stale_param')).toBeNull()
    expect(status()).toHaveTextContent('Reading')

    await act(async () => { vi.advanceTimersByTime(PARAMS_READ_DEBOUNCE_MS) })
    expect(reads).toHaveLength(2)
    await act(async () => { reads[1].resolve({ valid: true, messages: [], params: PARAMS }) })
    await flush()
    expect(screen.getByLabelText('Param 1 name')).toHaveValue('table_name')
    expect(status()).toHaveTextContent('Changing a param here')
  })

  it('is text only, with the reason, when the block is written in a form the editor can\'t replace', async () => {
    mockEditor({ source: SOURCE.replace('params:\n', '"params":\n') })
    await openEditor()
    expect(status()).toHaveTextContent('Text only. The params key is quoted. Edit the params in the text below.')
    expect(within(section()).queryAllByTestId('param-row')).toHaveLength(0)
    expect(within(section()).queryByRole('button', { name: /Add param/ })).toBeNull()
    expect(saveButton()).toBeEnabled()
  })

  it('is text only when the block has errors, and lists the findings', async () => {
    const finding = { severity: 'error' as const, message: "param 'limit': default must be a whole number, not a string", path: 'params.limit.default' }
    mockEditor({ validate: async () => ({ valid: false, messages: [finding], params: null }) })
    await openEditor()
    expect(status()).toHaveTextContent('Text only. The params block has errors. The findings below list them.')
    expect(within(section()).getByTestId('validation-findings')).toHaveTextContent("param 'limit': default must be a whole number, not a string")
    expect(within(section()).queryAllByTestId('param-row')).toHaveLength(0)
  })

  it.each([
    ['a 400', () => apiError(400)],
    ['a network failure', () => new TypeError('Failed to fetch')],
  ])("says it can't read the params when validate fails with %s", async (_label, failure) => {
    mockEditor({ validate: async () => { throw failure() } })
    await openEditor()
    expect(status()).toHaveTextContent('Fix the frontmatter to edit params here.')
    expect(within(section()).queryAllByTestId('param-row')).toHaveLength(0)
    expect(saveButton()).toBeEnabled()
  })
})

describe('Clone: the Params section', () => {
  it('shows the same rows, and the copy keeps the params block', async () => {
    const { post } = mockEditor()
    await openEditor('clone')
    expect(screen.getByLabelText('Param 1 name')).toHaveValue('table_name')
    expect(status()).toHaveTextContent('Changing a param here')
    fireEvent.click(screen.getByRole('button', { name: /create copy/i }))
    await flush()
    expect(post).toHaveBeenCalledTimes(1)
    const [name, content] = post.mock.calls[0]
    expect(name).toBe('query-agent-copy')
    expect(content).toContain('name: "query-agent-copy"')
    expect(content).toContain(SOURCE.slice(BLOCK_START, BLOCK_END))
  })

  it("the builder's Clone button opens the same editor, with the Params section", async () => {
    mockEditor()
    render(<StepEditor step={{ ...newStep('step-1'), agent: 'query-agent' }} index={0} providers={[]} onChange={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Clone profile query-agent' }))
    await flush()
    expect(screen.getByRole('dialog', { name: 'Clone profile' })).toBeInTheDocument()
    expect(screen.getByLabelText('Param 2 name')).toHaveValue('limit')
  })
})

describe('the profile page lists the params', () => {
  function mockPanel(detail: AgentProfileDetail, source: string) {
    vi.spyOn(api, 'listProfiles').mockResolvedValue([{ name: detail.name, description: '', source }])
    vi.spyOn(api, 'getProfile').mockResolvedValue(detail)
  }

  it('shows each param in declaration order: name, * when required, type, default, allowed values and description', async () => {
    mockPanel({
      name: 'query-agent',
      description: '',
      params: {
        limit: { type: 'int', required: false, default: 1, description: 'Max items to return' },
        table_name: { type: 'string', required: true, description: 'DynamoDB table name' },
        partition_key_type: { type: 'string', required: true, enum: ['S', 'N', 'B'] },
        dry_run: { type: 'bool', required: false, default: false },
      },
    }, 'local')
    render(<ProfilesPanel />)
    fireEvent.click(await screen.findByRole('option', { name: /query-agent/ }))
    const list = await screen.findByRole('list', { name: 'Params' })
    const items = within(list).getAllByTestId('profile-param')
    expect(items.map(item => item.dataset.name)).toEqual(['limit', 'table_name', 'partition_key_type', 'dry_run'])
    expect(items[0]).toHaveTextContent('limitintdefault: 1Max items to return')
    expect(items[1]).toHaveTextContent('table_name* (required)stringDynamoDB table name')
    expect(items[2]).toHaveTextContent('partition_key_type* (required)stringallowed: S, N, B')
    expect(items[3]).toHaveTextContent('dry_runbooldefault: false')
  })

  it.each([
    ['local', 'This profile declares no params. Edit it to add some.'],
    ['built-in', 'This profile declares no params. Clone it to add some.'],
  ])('says how to add params to a %s profile that declares none', async (source, line) => {
    mockPanel({ name: 'plain-agent', description: '' }, source)
    render(<ProfilesPanel />)
    fireEvent.click(await screen.findByRole('option', { name: /plain-agent/ }))
    const params = await screen.findByTestId('profile-params')
    expect(params).toHaveTextContent(line)
    expect(within(params).queryByRole('list')).toBeNull()
  })
})

// Params a profile declares, in the workflow builder: reading the declaration,
// the typed rows in StepEditor, the YAML they write, the checks validateBuilder
// adds, edit mode, the detail of a refused run, and the Profiles tab with a
// params block.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useState } from 'react'
import { act, render, screen, fireEvent, waitFor, within, cleanup } from '@testing-library/react'
import { api, errorDetailText } from '../api'
import type { AgentProfileDetail, ApiError, ProfileParamDecl, ProfileSearchResult, ProviderInfo, WorkflowDefRow, WorkflowSpec } from '../api'
import { useStore } from '../store'
import { StepEditor } from '../components/workflow/StepEditor'
import { WorkflowBuilder } from '../components/workflow/WorkflowBuilder'
import { WorkflowLauncher } from '../components/workflow/WorkflowLauncher'
import { ProfilesPanel } from '../components/ProfilesPanel'
import { ProfileCreateModal } from '../components/ProfileCreateModal'
import {
  MAX_PARAMS,
  PLACEHOLDER_MESSAGE,
  PLACEHOLDER_RE,
  applyDeclaredParams,
  applyStepPatch,
  dropDeclaredParams,
  errorsUnder,
  newStep,
  readDeclaredParams,
  toYaml,
  validateBuilder,
  writtenParams,
  type BuilderStep,
  type BuilderWorkflow,
} from '../components/workflow/workflowYaml'

const PROVIDERS: ProviderInfo[] = [{ name: 'claude_code', binary: 'claude', installed: true }]

// Declaration order mixes required and optional params on purpose.
const QUERY_PARAMS: Record<string, ProfileParamDecl> = {
  limit: { type: 'int', required: false, default: 1, description: 'Max items to return' },
  table_name: { type: 'string', required: true, description: 'DynamoDB table name' },
  consistent_read: { type: 'bool', required: false, default: false, description: 'Use a strongly consistent read' },
  region: { type: 'string', required: true, description: 'AWS region' },
  partition_key_type: { type: 'string', required: true, enum: ['S', 'N', 'B'], description: 'DynamoDB key type' },
  index_name: { type: 'string', required: false },
}

const PROFILES: Record<string, AgentProfileDetail> = {
  'dynamodb-query-agent': { name: 'dynamodb-query-agent', description: '', provider: 'claude_code', params: QUERY_PARAMS },
  'dynamodb-scan-agent': {
    name: 'dynamodb-scan-agent',
    description: '',
    provider: 'claude_code',
    params: {
      table_name: { type: 'string', required: true, description: 'DynamoDB table name' },
      segment_count: { type: 'int', required: false, default: 4 },
    },
  },
  // No type and no required: a string, and optional.
  'queue-check-agent': {
    name: 'queue-check-agent',
    description: '',
    provider: 'claude_code',
    params: {
      dry_run: { type: 'bool', required: true, description: 'Report only' },
      mode: { enum: ['fast', 'full'] },
      batch: { type: 'int', enum: [10, 20] },
    },
  },
  'plain-agent': { name: 'plain-agent', description: '', provider: 'claude_code' },
}

function result(name: string): ProfileSearchResult {
  return { name, description: '', capabilities: [], tags: [], role: '', source: 'local', coverage: 1, score: 1 }
}

function ok(data: unknown) {
  return { ok: true, status: 200, statusText: 'OK', json: () => Promise.resolve(data), text: () => Promise.resolve(JSON.stringify(data)) }
}

function fail(status: number, detail: unknown) {
  return { ok: false, status, statusText: 'Error', json: () => Promise.resolve({ detail }), text: () => Promise.resolve(JSON.stringify({ detail })) }
}

function apiError(status: number): ApiError {
  return Object.assign(new Error(`${status} Error`), { status })
}

/** Profile reads: the fixtures above, a 404 for broken-agent, and no params for any other name. */
function mockProfiles() {
  vi.spyOn(api, 'searchProfiles').mockImplementation(async q => [result(q)])
  return vi.spyOn(api, 'getProfile').mockImplementation(async name => {
    if (name === 'broken-agent') throw apiError(404)
    return PROFILES[name] ?? { name, description: '', provider: 'claude_code' }
  })
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

// A stateful host for StepEditor, with errors from validateBuilder.
function StepHarness({ initial = newStep('step-1') }: { initial?: BuilderStep }) {
  const [step, setStep] = useState<BuilderStep>(initial)
  const errors = errorsUnder(validateBuilder({ name: 'w', description: '', inputs: [], steps: [step] }), 'steps.0')
  return (
    <>
      <StepEditor
        step={step}
        index={0}
        errors={errors}
        providers={PROVIDERS}
        onChange={patch => setStep(s => applyStepPatch(s, patch))}
      />
      <pre data-testid="step-state">{JSON.stringify(step)}</pre>
    </>
  )
}
const current = (): BuilderStep => JSON.parse(screen.getByTestId('step-state').textContent as string)
const valueOf = (step: BuilderStep, name: string) =>
  step.declared.status === 'ready' ? step.declared.values[step.declared.params.findIndex(p => p.name === name)] : undefined

async function searchAndPick(n: number, name: string) {
  fireEvent.change(screen.getByLabelText(`Search profiles for step ${n}`), { target: { value: name } })
  const pick = await screen.findByRole('button', { name: `Use profile ${name}` })
  await act(async () => { fireEvent.click(pick) })
}

const declaredGroup = (n = 1) => screen.findByRole('group', { name: `Params from the profile for step ${n}` })
const rowNames = (group: HTMLElement) => within(group).getAllByTestId('declared-param').map(r => r.dataset.name)
const options = (select: HTMLElement) =>
  [...(select as HTMLSelectElement).options].map(o => ({ text: o.textContent, value: o.value, disabled: o.disabled }))

describe('readDeclaredParams', () => {
  it('keeps declaration order, reads a missing type as string and a missing required as false, and turns defaults into text', () => {
    expect(readDeclaredParams(QUERY_PARAMS)).toEqual([
      { name: 'limit', type: 'int', required: false, default: '1', description: 'Max items to return', enum: null },
      { name: 'table_name', type: 'string', required: true, default: null, description: 'DynamoDB table name', enum: null },
      { name: 'consistent_read', type: 'bool', required: false, default: 'false', description: 'Use a strongly consistent read', enum: null },
      { name: 'region', type: 'string', required: true, default: null, description: 'AWS region', enum: null },
      { name: 'partition_key_type', type: 'string', required: true, default: null, description: 'DynamoDB key type', enum: ['S', 'N', 'B'] },
      { name: 'index_name', type: 'string', required: false, default: null, description: '', enum: null },
    ])
    expect(readDeclaredParams({ mode: { enum: ['fast', 'full'] }, batch: { type: 'int', enum: [10, 20] } })).toEqual([
      { name: 'mode', type: 'string', required: false, default: null, description: '', enum: ['fast', 'full'] },
      { name: 'batch', type: 'int', required: false, default: null, description: '', enum: ['10', '20'] },
    ])
  })

  it('reads untrusted content defensively, with every size bounded', () => {
    expect(readDeclaredParams(undefined)).toEqual([])
    expect(readDeclaredParams(['a'])).toEqual([])
    expect(readDeclaredParams('params')).toEqual([])
    const many = Object.fromEntries(Array.from({ length: MAX_PARAMS + 8 }, (_, i) => [`p${i}`, {}]))
    expect(readDeclaredParams(many)).toHaveLength(MAX_PARAMS)
    expect(readDeclaredParams({ '1st': {}, 'has space': {}, list: [], none: null, text: 'x', kept: {} }).map(p => p.name)).toEqual(['kept'])
    const [odd] = readDeclaredParams({
      odd: { type: 'float', required: 'yes', description: 'd'.repeat(300), enum: [...Array.from({ length: 40 }, (_, i) => i), 1, '1', '', null] },
    })
    expect(odd).toMatchObject({ type: 'string', required: false })
    expect(odd.description).toHaveLength(200)
    // Duplicates and empty values go, and at most 32 values stay.
    expect(odd.enum).toEqual(Array.from({ length: 32 }, (_, i) => String(i)))
    // A required param keeps no default, a bool keeps no enum, and an empty default is no default.
    expect(readDeclaredParams({
      a: { required: true, default: 'x' },
      b: { type: 'bool', enum: ['true'] },
      c: { default: '' },
    }).map(p => [p.default, p.enum])).toEqual([[null, null], [null, null], [null, null]])
  })
})

describe('applying a declaration to a step', () => {
  const declared = readDeclaredParams(QUERY_PARAMS)
  const stepWith = (over: Partial<BuilderStep>) => ({ ...newStep('s'), agent: 'dynamodb-query-agent', ...over })

  it('pre-fills the defaults when picking, and leaves every value empty when not', () => {
    expect(applyDeclaredParams(stepWith({}), declared, true)).toEqual({
      params: [],
      declared: { status: 'ready', params: declared, values: ['1', '', 'false', '', '', ''] },
    })
    expect(applyDeclaredParams(stepWith({}), declared, false)).toEqual({
      params: [],
      declared: { status: 'ready', params: declared, values: ['', '', '', '', '', ''] },
    })
  })

  it('moves a free-form row the profile declares into its typed row, and keeps the others', () => {
    const step = stepWith({ params: [{ key: 'note', value: 'nightly' }, { key: 'limit', value: '7' }, { key: 'table_name', value: 'orders' }] })
    expect(applyDeclaredParams(step, declared, true)).toEqual({
      params: [{ key: 'note', value: 'nightly' }],
      declared: { status: 'ready', params: declared, values: ['7', 'orders', 'false', '', '', ''] },
    })
  })

  it('on a switch keeps the values of shared names, takes the new defaults and drops the rest', () => {
    const before = applyStepPatch(stepWith({ params: [{ key: 'note', value: 'nightly' }] }), s => applyDeclaredParams(s, declared, true))
    const filled = applyStepPatch(before, s => s.declared.status === 'ready'
      ? { declared: { ...s.declared, values: ['5', 'orders', 'true', 'us-east-1', 'S', ''] } }
      : {})
    const scan = readDeclaredParams(PROFILES['dynamodb-scan-agent'].params)
    expect(applyDeclaredParams(filled, scan, true)).toEqual({
      params: [{ key: 'note', value: 'nightly' }],
      declared: { status: 'ready', params: scan, values: ['orders', '4'] },
    })
  })

  it('when the profile cannot be read, turns every value into a free-form row', () => {
    const step: BuilderStep = stepWith({
      params: [{ key: 'note', value: 'nightly' }],
      declared: { status: 'ready', params: declared, values: ['5', 'orders', '', 'us-east-1', '', ''] },
    })
    expect(dropDeclaredParams(step)).toEqual({
      params: [
        { key: 'limit', value: '5' },
        { key: 'table_name', value: 'orders' },
        { key: 'region', value: 'us-east-1' },
        { key: 'note', value: 'nightly' },
      ],
      declared: { status: 'error' },
    })
  })
})

describe('toYaml with declared params', () => {
  const step = (over: Partial<BuilderStep>): BuilderStep => ({
    ...newStep('query'),
    provider: 'claude_code',
    agent: 'dynamodb-query-agent',
    prompt: 'Query the table.',
    ...over,
  })
  const wf = (s: BuilderStep): BuilderWorkflow => ({ name: 'wf', description: '', inputs: [], steps: [s] })

  it('writes the declared params that have a value, in declaration order, then the free-form rows', () => {
    const s = step({
      params: [{ key: 'note', value: 'nightly' }],
      declared: { status: 'ready', params: readDeclaredParams(QUERY_PARAMS), values: ['5', 'orders', '', 'us-east-1', 'S', ''] },
    })
    const yaml = toYaml(wf(s))
    expect(yaml).toContain([
      '    "params":',
      '      "limit": "5"',
      '      "table_name": "orders"',
      '      "region": "us-east-1"',
      '      "partition_key_type": "S"',
      '      "note": "nightly"',
      '    "prompt": "Query the table."',
    ].join('\n'))
    // Optional params left empty are left out.
    expect(yaml).not.toContain('consistent_read')
    expect(yaml).not.toContain('index_name')
    expect(writtenParams(s).map(p => p.key)).toEqual(['limit', 'table_name', 'region', 'partition_key_type', 'note'])
  })

  it('writes no params key when every declared value is empty and there are no free-form rows', () => {
    const s = step({ declared: { status: 'ready', params: readDeclaredParams(QUERY_PARAMS), values: ['', '', '', '', '', ''] } })
    expect(toYaml(wf(s))).not.toContain('"params"')
  })
})

describe('validateBuilder with declared params', () => {
  const declared = readDeclaredParams({
    ...QUERY_PARAMS,
    batch: { type: 'int', enum: [10, 20] },
    letters: { enum: 'abcdefghijkl'.split('') },
  })
  // Declaration order: limit 0, table_name 1, consistent_read 2, region 3, partition_key_type 4, index_name 5, batch 6, letters 7.
  const check = (values: Record<string, string>, params: BuilderStep['params'] = []) => {
    const step: BuilderStep = {
      ...newStep('query'),
      provider: 'claude_code',
      agent: 'dynamodb-query-agent',
      prompt: 'Query.',
      params,
      declared: { status: 'ready', params: declared, values: declared.map(p => values[p.name] ?? '') },
    }
    return errorsUnder(validateBuilder({ name: 'wf', description: '', inputs: [], steps: [step] }), 'steps.0')
  }
  const full = { table_name: 'orders', region: 'us-east-1', partition_key_type: 'S' }

  it('passes when every required param has a value and optional ones are empty', () => {
    expect(check(full)).toEqual({})
  })

  it('marks each empty required param Required., keyed by its place in the declaration', () => {
    expect(check({})).toEqual({ 'declared.1': 'Required.', 'declared.3': 'Required.', 'declared.4': 'Required.' })
  })

  it('checks an int as the server does: an optional minus and 1 to 18 digits', () => {
    for (const good of ['0', '-3', '007', '1'.repeat(18)]) expect(check({ ...full, limit: good }), good).toEqual({})
    for (const bad of ['1.5', '+1', ' 1', '1e3', 'ten', '1'.repeat(19)]) {
      expect(check({ ...full, limit: bad })['declared.0'], bad).toBe('Use a whole number, up to 18 digits.')
    }
  })

  it('checks a bool as exactly true or false', () => {
    expect(check({ ...full, consistent_read: 'true' })).toEqual({})
    expect(check({ ...full, consistent_read: 'false' })).toEqual({})
    for (const bad of ['True', 'yes', '1']) expect(check({ ...full, consistent_read: bad })['declared.2'], bad).toBe('Use true or false.')
  })

  it('checks an enum against its values as text, naming at most 10 of them', () => {
    expect(check({ ...full, partition_key_type: 'X' })['declared.4']).toBe('Use one of: S, N, B.')
    expect(check({ ...full, batch: '20' })).toEqual({})
    expect(check({ ...full, batch: '30' })['declared.6']).toBe('Use one of: 10, 20.')
    expect(check({ ...full, letters: 'z' })['declared.7']).toBe('Use one of: a, b, c, d, e, f, g, h, i, j, ...')
  })

  it('refuses a {{...}} placeholder in a declared value and in a free-form row, and lets single braces pass', () => {
    expect(check({ ...full, table_name: '{{workflow.inputs.table}}' })['declared.1']).toBe(PLACEHOLDER_MESSAGE)
    expect(check({ ...full, limit: '{{ n }}' })['declared.0']).toBe(PLACEHOLDER_MESSAGE)
    const free = check(full, [{ key: 'filter', value: 'id = {{workflow.inputs.id}}' }, { key: 'payload', value: '{"a": 1}' }])
    expect(free).toEqual({ 'params.0.value': PLACEHOLDER_MESSAGE })
    expect(PLACEHOLDER_MESSAGE).toBe('Param values are fixed when you build the workflow, so {{...}} placeholders are not allowed.')
    // The engine's own pattern, as the contract gives it.
    expect(PLACEHOLDER_RE.source).toBe(String.raw`\{\{\s*([^{}]+?)\s*\}\}`)
  })

  it('applies the line break and length rules to declared values too', () => {
    expect(check({ ...full, table_name: 'a\nb' })['declared.1']).toBe('A param value cannot contain a line break.')
    expect(check({ ...full, index_name: 'x'.repeat(513) })['declared.5']).toBe('A param value can have at most 512 characters.')
  })

  it('points a free-form row that repeats a declared name at the typed row', () => {
    expect(check(full, [{ key: 'region', value: 'us-west-2' }])['params.0.key']).toBe('The profile declares this param. Set it in its row above.')
  })

  it('counts declared values toward the 32-param limit', () => {
    const rows = Array.from({ length: MAX_PARAMS - 3 }, (_, j) => ({ key: `k${j}`, value: 'v' }))
    expect(check(full, rows).params).toBeUndefined()
    expect(check({ ...full, limit: '1' }, rows).params).toBe('A step can have at most 32 params.')
  })

  it('runs no declared checks until the declaration is known', () => {
    for (const status of ['unknown', 'loading', 'error'] as const) {
      const step: BuilderStep = { ...newStep('s'), provider: 'p', agent: 'a', prompt: 'x', declared: { status } }
      expect(validateBuilder({ name: 'wf', description: '', inputs: [], steps: [step] }), status).toEqual({})
    }
  })
})

describe('StepEditor: params from the profile', () => {
  beforeEach(() => { mockProfiles() })

  it('shows one row per declared param, required first and marked *, each group in declaration order, with descriptions', async () => {
    render(<StepHarness />)
    await searchAndPick(1, 'dynamodb-query-agent')
    const group = await declaredGroup()
    expect(within(group).getByText('Params from the profile')).toBeInTheDocument()
    expect(rowNames(group)).toEqual(['table_name', 'region', 'partition_key_type', 'limit', 'consistent_read', 'index_name'])
    const names = within(group).getAllByTestId('declared-param').map(r => r.firstElementChild?.textContent)
    expect(names).toEqual(['table_name*', 'region*', 'partition_key_type*', 'limit', 'consistent_read', 'index_name'])
    expect(screen.getByLabelText('Step 1 param table_name')).toHaveAttribute('aria-required', 'true')
    expect(screen.getByLabelText('Step 1 param limit')).not.toHaveAttribute('aria-required')
    expect(screen.getByLabelText('Step 1 param table_name')).toHaveAccessibleDescription('DynamoDB table name')
    expect(screen.getByLabelText('Step 1 param partition_key_type')).toHaveAccessibleDescription('DynamoDB key type')
    expect(screen.getByLabelText('Step 1 param index_name')).not.toHaveAttribute('aria-describedby')
  })

  it('wraps a long name only after _ . or -, never mid-word, and keeps its * with it', async () => {
    render(<StepHarness />)
    await searchAndPick(1, 'dynamodb-query-agent')
    const group = await declaredGroup()
    const row = within(group).getAllByTestId('declared-param').find(r => r.dataset.name === 'partition_key_type')
    const name = row?.firstElementChild as HTMLElement
    // A soft break after each underscore, and no break-anywhere class on the name.
    expect(name.querySelectorAll('wbr')).toHaveLength(2)
    expect(name.className).not.toMatch(/break-all/)
    expect(name.textContent).toBe('partition_key_type*')
    // The soft breaks do not split the name for a text query either.
    expect(within(group).getByText('partition_key_type')).toBe(name)
  })

  it('renders a text field for string and int, and a select for enum and bool, with an empty choice only when optional', async () => {
    render(<StepHarness />)
    await searchAndPick(1, 'dynamodb-query-agent')
    await declaredGroup()
    const table = screen.getByLabelText('Step 1 param table_name')
    expect(table.tagName).toBe('INPUT')
    expect(table).toHaveAttribute('placeholder', 'value')
    const limit = screen.getByLabelText('Step 1 param limit')
    expect(limit.tagName).toBe('INPUT')
    expect(limit).toHaveAttribute('inputmode', 'numeric')
    fireEvent.change(limit, { target: { value: '25' } })
    expect(valueOf(current(), 'limit')).toBe('25')

    // A required enum: an empty value shows a prompt that cannot be picked, and no empty choice once set.
    const keyType = screen.getByLabelText('Step 1 param partition_key_type')
    expect(keyType.tagName).toBe('SELECT')
    expect(options(keyType)).toEqual([
      { text: 'Pick a value', value: '', disabled: true },
      { text: 'S', value: 'S', disabled: false },
      { text: 'N', value: 'N', disabled: false },
      { text: 'B', value: 'B', disabled: false },
    ])
    fireEvent.change(keyType, { target: { value: 'N' } })
    expect(valueOf(current(), 'partition_key_type')).toBe('N')
    expect(options(keyType).map(o => o.value)).toEqual(['S', 'N', 'B'])

    // An optional bool: true, false and an empty choice that names the default.
    const consistent = screen.getByLabelText('Step 1 param consistent_read')
    expect(options(consistent)).toEqual([
      { text: 'not set (default: false)', value: '', disabled: false },
      { text: 'true', value: 'true', disabled: false },
      { text: 'false', value: 'false', disabled: false },
    ])
    fireEvent.change(consistent, { target: { value: '' } })
    expect(valueOf(current(), 'consistent_read')).toBe('')
  })

  it('a required bool has no empty choice, and an optional enum has one', async () => {
    render(<StepHarness />)
    await searchAndPick(1, 'queue-check-agent')
    await declaredGroup()
    const dryRun = screen.getByLabelText('Step 1 param dry_run')
    expect(options(dryRun).map(o => [o.value, o.disabled])).toEqual([['', true], ['true', false], ['false', false]])
    fireEvent.change(dryRun, { target: { value: 'true' } })
    expect(options(dryRun).map(o => o.value)).toEqual(['true', 'false'])
    expect(options(screen.getByLabelText('Step 1 param mode'))).toEqual([
      { text: 'not set', value: '', disabled: false },
      { text: 'fast', value: 'fast', disabled: false },
      { text: 'full', value: 'full', disabled: false },
    ])
    // An int enum is a select of its values as text.
    expect(options(screen.getByLabelText('Step 1 param batch')).map(o => o.value)).toEqual(['', '10', '20'])
  })

  it('picking a profile pre-fills each declared default as text', async () => {
    render(<StepHarness />)
    await searchAndPick(1, 'dynamodb-query-agent')
    await declaredGroup()
    expect(screen.getByLabelText('Step 1 param limit')).toHaveValue('1')
    expect(screen.getByLabelText('Step 1 param consistent_read')).toHaveValue('false')
    expect(screen.getByLabelText('Step 1 param table_name')).toHaveValue('')
    expect(current().declared).toEqual({ status: 'ready', params: readDeclaredParams(QUERY_PARAMS), values: ['1', '', 'false', '', '', ''] })
    expect(current().provider).toBe('claude_code')
  })

  it('shows each default as a placeholder only, for a step loaded from a file without the optional values', () => {
    const loaded: BuilderStep = {
      ...newStep('step-1'),
      agent: 'dynamodb-query-agent',
      provider: 'claude_code',
      ...applyDeclaredParams(newStep('step-1'), readDeclaredParams(QUERY_PARAMS), false),
    }
    render(<StepHarness initial={loaded} />)
    const limit = screen.getByLabelText('Step 1 param limit')
    expect(limit).toHaveValue('')
    expect(limit).toHaveAttribute('placeholder', 'default: 1')
    expect(screen.getByLabelText('Step 1 param consistent_read')).toHaveValue('')
    expect(options(screen.getByLabelText('Step 1 param consistent_read'))[0].text).toBe('not set (default: false)')
    expect(current().declared).toMatchObject({ status: 'ready', values: ['', '', '', '', '', ''] })
  })

  it('switching profiles keeps values for shared names and the free-form rows, and drops the other declared rows', async () => {
    render(<StepHarness />)
    await searchAndPick(1, 'dynamodb-query-agent')
    await declaredGroup()
    fireEvent.change(screen.getByLabelText('Step 1 param table_name'), { target: { value: 'orders' } })
    fireEvent.change(screen.getByLabelText('Step 1 param region'), { target: { value: 'us-east-1' } })
    fireEvent.click(screen.getByRole('button', { name: /Add param/ }))
    fireEvent.change(screen.getByLabelText('Step 1 param 1 name'), { target: { value: 'note' } })
    fireEvent.change(screen.getByLabelText('Step 1 param 1 value'), { target: { value: 'nightly' } })

    await searchAndPick(1, 'dynamodb-scan-agent')
    const group = await declaredGroup()
    await waitFor(() => expect(rowNames(group)).toEqual(['table_name', 'segment_count']))
    expect(screen.getByLabelText('Step 1 param table_name')).toHaveValue('orders')
    expect(screen.getByLabelText('Step 1 param segment_count')).toHaveValue('4')
    expect(screen.queryByLabelText('Step 1 param region')).toBeNull()
    expect(current().params).toEqual([{ key: 'note', value: 'nightly' }])
    expect(screen.getByLabelText('Step 1 param 1 name')).toHaveValue('note')
  })

  it('says so when the profile declares no params, and keeps the free-form rows', async () => {
    render(<StepHarness initial={{ ...newStep('step-1'), params: [{ key: 'note', value: 'nightly' }] }} />)
    await searchAndPick(1, 'plain-agent')
    const group = await declaredGroup()
    expect(await within(group).findByText(
      'This profile declares no params. Clone it above, or edit it in the Profiles tab, to add some.',
    )).toBeInTheDocument()
    expect(within(group).queryAllByTestId('declared-param')).toHaveLength(0)
    expect(screen.getByLabelText('Step 1 param 1 name')).toHaveValue('note')
    expect(screen.getByLabelText('Step 1 param 1 value')).toHaveValue('nightly')
  })

  it('when the profile fails to load, says its params cannot be checked and shows every param as a free-form row', async () => {
    render(<StepHarness initial={{ ...newStep('step-1'), params: [{ key: 'note', value: 'nightly' }] }} />)
    await searchAndPick(1, 'dynamodb-query-agent')
    await declaredGroup()
    fireEvent.change(screen.getByLabelText('Step 1 param table_name'), { target: { value: 'orders' } })
    await searchAndPick(1, 'broken-agent')
    const group = await declaredGroup()
    expect(await within(group).findByText("Couldn't load this profile, so its params can't be checked.")).toBeInTheDocument()
    expect(within(group).queryAllByTestId('declared-param')).toHaveLength(0)
    expect(current().params).toEqual([
      { key: 'limit', value: '1' },
      { key: 'table_name', value: 'orders' },
      { key: 'consistent_read', value: 'false' },
      { key: 'note', value: 'nightly' },
    ])
    expect(screen.getByLabelText('Step 1 param 2 name')).toHaveValue('table_name')
    expect(screen.getByText('Could not read the profile. Pick a provider.')).toBeInTheDocument()
  })

  it('still applies the params when the user picks a provider while the profile is being read', async () => {
    let resolve: (p: AgentProfileDetail) => void = () => {}
    vi.spyOn(api, 'getProfile').mockImplementation(() => new Promise(r => { resolve = r }))
    render(<StepHarness />)
    await searchAndPick(1, 'dynamodb-query-agent')
    expect(screen.getByText("Reading the profile's params...")).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Provider for step 1'), { target: { value: 'claude_code' } })
    await act(async () => { resolve({ ...PROFILES['dynamodb-query-agent'], provider: 'other_cli' }) })
    expect(current().provider).toBe('claude_code')
    expect(rowNames(await declaredGroup())).toHaveLength(6)
    expect(screen.queryByText("Reading the profile's params...")).toBeNull()
  })
})

const SPEC_NULLS = { engine: null, output_schema: null, when: null, until: null, max_iterations: null, on_stall: null, on_no_progress: null }

const EDIT_SPEC: WorkflowSpec = {
  name: 'query-orders',
  description: 'Query two tables',
  mode: 'sequential',
  inputs: {},
  steps: [
    {
      id: 'orders', provider: 'claude_code', agent: 'dynamodb-query-agent', prompt: 'Query orders.', call: 'handoff',
      params: { note: 'nightly', region: 'us-east-1', table_name: 'orders', partition_key_type: 'S' },
      retries: null, on_failure: null, ...SPEC_NULLS,
    },
    {
      id: 'customers', provider: 'claude_code', agent: 'dynamodb-query-agent', prompt: 'Query customers.', call: 'handoff',
      params: { table_name: 'customers' }, retries: null, on_failure: null, ...SPEC_NULLS,
    },
  ],
  path: '/wf/query-orders.yaml',
  source: '"name": "query-orders"\n',
  content_hash: 'hash-1',
}

describe('WorkflowBuilder with declared params', () => {
  beforeEach(() => {
    vi.spyOn(api, 'listProviders').mockResolvedValue(PROVIDERS)
    // Save validates first.
    vi.spyOn(api, 'validateWorkflowSource').mockResolvedValue({ status: 'pass', errors: [] })
  })

  it('blocks Save while a required param is empty, then writes the declared values first', async () => {
    mockProfiles()
    const create = vi.spyOn(api, 'createWorkflow').mockResolvedValue({
      name: 'query-orders', path: '/wf/query-orders.yaml', content_hash: 'h1', findings: [], status: 'pass', reserved_notes: [],
    })
    render(<WorkflowBuilder mode="create" onClose={() => {}} onSaved={() => {}} />)
    fireEvent.change(screen.getByLabelText('Workflow name'), { target: { value: 'query-orders' } })
    await searchAndPick(1, 'dynamodb-query-agent')
    await declaredGroup()
    await waitFor(() => expect(screen.getByLabelText('Provider for step 1')).toHaveValue('claude_code'))
    fireEvent.change(screen.getByLabelText('Prompt for step 1'), { target: { value: 'Query the table.' } })

    fireEvent.click(screen.getByRole('button', { name: 'Create workflow' }))
    expect(screen.getAllByText('Required.')).toHaveLength(3)
    expect(screen.getByText('Fix the 3 problems marked in the form first.')).toBeInTheDocument()
    expect(screen.getByLabelText('Step 1 param table_name')).toHaveAttribute('aria-invalid', 'true')
    expect(create).not.toHaveBeenCalled()

    fireEvent.change(screen.getByLabelText('Step 1 param table_name'), { target: { value: 'orders' } })
    fireEvent.change(screen.getByLabelText('Step 1 param region'), { target: { value: 'us-east-1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create workflow' }))
    expect(screen.getAllByText('Required.')).toHaveLength(1)
    expect(create).not.toHaveBeenCalled()

    fireEvent.change(screen.getByLabelText('Step 1 param partition_key_type'), { target: { value: 'S' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create workflow' }))
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1))
    expect(create.mock.calls[0][1]).toContain([
      '    "params":',
      '      "limit": "1"',
      '      "table_name": "orders"',
      '      "consistent_read": "false"',
      '      "region": "us-east-1"',
      '      "partition_key_type": "S"',
      '    "prompt": "Query the table."',
    ].join('\n'))
  })

  it('edit mode reads each distinct profile once, shows the file values without pre-filling, and saves in declared order', async () => {
    const getProfile = mockProfiles()
    vi.spyOn(api, 'getWorkflowSpec').mockResolvedValue(EDIT_SPEC)
    const update = vi.spyOn(api, 'updateWorkflow').mockResolvedValue({ name: 'query-orders', path: '/wf/query-orders.yaml', content_hash: 'hash-2', findings: [] })
    render(<WorkflowBuilder mode="edit" name="query-orders" onClose={() => {}} onSaved={() => {}} />)

    const first = await declaredGroup(1)
    await waitFor(() => expect(rowNames(first)).toHaveLength(6))
    expect(getProfile).toHaveBeenCalledTimes(1)
    expect(getProfile).toHaveBeenCalledWith('dynamodb-query-agent')
    expect(screen.getByLabelText('Step 1 param table_name')).toHaveValue('orders')
    expect(screen.getByLabelText('Step 1 param region')).toHaveValue('us-east-1')
    expect(screen.getByLabelText('Step 1 param partition_key_type')).toHaveValue('S')
    // Not in the file, so not set: the default is a placeholder only.
    expect(screen.getByLabelText('Step 1 param limit')).toHaveValue('')
    expect(screen.getByLabelText('Step 1 param limit')).toHaveAttribute('placeholder', 'default: 1')
    expect(screen.getByLabelText('Step 1 param consistent_read')).toHaveValue('')
    // A name the profile does not declare stays a free-form row.
    expect(screen.getByLabelText('Step 1 param 1 name')).toHaveValue('note')
    expect(screen.getByLabelText('Step 2 param table_name')).toHaveValue('customers')

    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    expect(screen.getAllByText('Required.')).toHaveLength(2)
    expect(update).not.toHaveBeenCalled()

    // Picking a profile in edit mode pre-fills its defaults, as in create mode.
    // Only the values loaded from the file are left as they are.
    await searchAndPick(2, 'dynamodb-scan-agent')
    const second = await declaredGroup(2)
    await waitFor(() => expect(rowNames(second)).toEqual(['table_name', 'segment_count']))
    expect(screen.getByLabelText('Step 2 param segment_count')).toHaveValue('4')
    expect(screen.getByLabelText('Step 2 param table_name')).toHaveValue('customers')
    expect(screen.getByLabelText('Step 1 param limit')).toHaveValue('')

    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(update).toHaveBeenCalledTimes(1))
    const source = update.mock.calls[0][1]
    expect(source).toContain('    "params":\n      "table_name": "orders"\n      "region": "us-east-1"\n      "partition_key_type": "S"\n      "note": "nightly"\n    "prompt": "Query orders."\n')
    expect(source).toContain('    "agent": "dynamodb-scan-agent"\n    "call": "handoff"\n    "params":\n      "table_name": "customers"\n      "segment_count": "4"\n    "prompt": "Query customers."\n')
  })

  it('edit mode with a profile that fails to load keeps every param as a free-form row, in file order', async () => {
    vi.spyOn(api, 'searchProfiles').mockResolvedValue([])
    vi.spyOn(api, 'getProfile').mockRejectedValue(apiError(404))
    vi.spyOn(api, 'getWorkflowSpec').mockResolvedValue(EDIT_SPEC)
    const update = vi.spyOn(api, 'updateWorkflow').mockResolvedValue({ name: 'query-orders', path: '/wf/query-orders.yaml', content_hash: 'hash-2', findings: [] })
    render(<WorkflowBuilder mode="edit" name="query-orders" onClose={() => {}} onSaved={() => {}} />)
    const first = await declaredGroup(1)
    expect(await within(first).findByText("Couldn't load this profile, so its params can't be checked.")).toBeInTheDocument()
    expect(within(await declaredGroup(2)).getByText("Couldn't load this profile, so its params can't be checked.")).toBeInTheDocument()
    expect(['note', 'region', 'table_name', 'partition_key_type'].map((_, j) =>
      (screen.getByLabelText(`Step 1 param ${j + 1} name`) as HTMLInputElement).value)).toEqual(['note', 'region', 'table_name', 'partition_key_type'])
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(update).toHaveBeenCalledTimes(1))
    expect(update.mock.calls[0][1]).toContain('    "params":\n      "note": "nightly"\n      "region": "us-east-1"\n      "table_name": "orders"\n      "partition_key_type": "S"\n')
  })

  it('keeps the workflow Inputs section as it was', async () => {
    mockProfiles()
    render(<WorkflowBuilder mode="create" onClose={() => {}} onSaved={() => {}} />)
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    fireEvent.click(screen.getByRole('button', { name: /Add input/ }))
    expect(screen.getByLabelText('Workflow input 1 name')).toBeInTheDocument()
    expect(screen.getByLabelText('Workflow input 1 type')).toBeInTheDocument()
    expect(screen.getByLabelText('Workflow input 1 required')).toBeInTheDocument()
    expect(screen.getByLabelText('Workflow input 1 default')).toBeInTheDocument()
    expect(screen.getByText(/Values a run asks for/)).toBeInTheDocument()
  })
})

describe('a refused run shows the server detail', () => {
  const DEF: WorkflowDefRow = { name: 'query-orders', source_path: '/wf/query-orders.yaml', mode: 'sequential', step_count: 1, description: '', indexed_at: '' }
  const messages = [
    "step 'orders': missing required param 'region' (declared by profile 'dynamodb-query-agent')",
    "step 'orders': param 'limit' must be a whole number",
  ]

  it.each([
    ['a list of messages', messages, messages.join('\n')],
    ['one string', messages.join('\n'), messages.join('\n')],
    ['an object with a message and errors', { message: 'Step params do not match their profiles.', errors: messages }, ['Step params do not match their profiles.', ...messages].join('\n')],
  ])('when the 422 detail is %s', async (_label, detail, expected) => {
    vi.spyOn(api, 'listWorkflows').mockResolvedValue([DEF])
    vi.spyOn(api, 'getWorkflowSpec').mockResolvedValue({ name: 'query-orders', inputs: {} })
    const fetchMock = vi.fn(async () => fail(422, detail))
    vi.stubGlobal('fetch', fetchMock)
    render(<WorkflowLauncher />)
    fireEvent.click(screen.getByRole('button', { name: /Run a workflow/ }))
    const run = await screen.findByRole('button', { name: /Run workflow/ })
    await waitFor(() => expect(run).toBeEnabled())
    await act(async () => { fireEvent.click(run) })
    expect(fetchMock).toHaveBeenCalledWith('/workflows/runs:submit', expect.anything())
    expect(useStore.getState().snackbar).toEqual({ type: 'error', message: expected })
  })

  it('reads a list detail as its messages, one per line', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => fail(422, [{ loc: ['body', 'name_or_path'], msg: 'Field required', type: 'missing' }, 'second', 7])))
    await expect(api.submitWorkflowRun({ name_or_path: '' })).rejects.toMatchObject({ status: 422, detail: 'Field required\nsecond' })
  })

  it('errorDetailText adds listed errors the detail does not already hold', () => {
    expect(errorDetailText({ detail: 'a' })).toBe('a')
    expect(errorDetailText({ detail: 'a', detailMeta: { message: 'a', errors: ['b', 'a', 3, ''] } })).toBe('a\nb')
    expect(errorDetailText({ detail: 'x\ny', detailMeta: { errors: ['x', 'y'] } })).toBe('x\ny')
    expect(errorDetailText({ detailMeta: { errors: ['only'] } })).toBe('only')
    expect(errorDetailText({})).toBeUndefined()
    expect(errorDetailText(undefined)).toBeUndefined()
  })
})

describe('the Profiles tab with a params block', () => {
  const SOURCE = [
    '---',
    'name: query-agent',
    'description: Query a table',
    'provider: claude_code',
    'params:',
    '  table_name:',
    '    description: DynamoDB table name',
    '    required: true',
    '  limit:',
    '    description: Max items to return',
    '    type: int',
    '    default: 1',
    '---',
    '',
    '# Query the table.',
    '',
  ].join('\n')
  const DETAIL: AgentProfileDetail = {
    name: 'query-agent',
    description: 'Query a table',
    provider: 'claude_code',
    params: {
      table_name: { type: 'string', required: true, description: 'DynamoDB table name' },
      limit: { type: 'int', required: false, default: 1, description: 'Max items to return' },
    },
  }

  it('opens, edits and saves a local profile, keeping the params block as written', async () => {
    const mock = vi.fn(async (url: string, opts?: any) => {
      if (url.includes('/agents/profiles/validate')) return ok({ valid: true, messages: [] })
      if (url.includes('/source')) return ok({ name: 'query-agent', content: SOURCE })
      if (opts?.method === 'PUT') return ok({ name: 'query-agent', warnings: [] })
      if (/\/agents\/profiles\/[^/?]+$/.test(url)) return ok(DETAIL)
      if (url.includes('/agents/profiles')) return ok([{ name: 'query-agent', description: 'Query a table', source: 'local' }])
      return ok([])
    })
    vi.stubGlobal('fetch', mock)
    render(<ProfilesPanel />)
    fireEvent.click(await screen.findByRole('option', { name: /query-agent/ }))
    const detail = await screen.findByTestId('profile-detail')
    expect(await within(detail).findByText('claude_code')).toBeInTheDocument()

    fireEvent.click(within(detail).getByRole('button', { name: /edit/i }))
    const editor = await screen.findByRole('textbox', { name: /profile source/i })
    expect(editor).toHaveValue(SOURCE)
    const edited = SOURCE.replace('# Query the table.', '# Query the orders table.')
    fireEvent.change(editor, { target: { value: edited } })
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }))
    await act(async () => {})
    const put = mock.mock.calls.find(([, o]) => o?.method === 'PUT')
    expect(put).toBeTruthy()
    expect(JSON.parse(put![1].body).content).toBe(edited)
  })

  describe('from scratch', () => {
    beforeEach(() => vi.useFakeTimers())
    afterEach(() => vi.useRealTimers())

    const PROFILE_SCHEMA = {
      type: 'object',
      required: ['name'],
      properties: {
        name: { type: 'string' },
        description: { type: 'string' },
        params: {
          type: 'object',
          description: 'Params a workflow step that uses this profile fills in.',
          maxProperties: 32,
          propertyNames: { pattern: '^[A-Za-z][A-Za-z0-9_.-]{0,63}$' },
          additionalProperties: {
            type: 'object',
            additionalProperties: false,
            properties: {
              type: { type: 'string', enum: ['string', 'int', 'bool'] },
              required: { type: 'boolean' },
              default: { type: ['string', 'integer', 'boolean'] },
              description: { type: 'string', maxLength: 200 },
              enum: { type: 'array', minItems: 1, maxItems: 32 },
            },
          },
        },
      },
      additionalProperties: false,
    }

    function scratchFetch() {
      return vi.fn(async (url: string, opts?: any) => {
        if (url.includes('/agents/providers')) return ok([])
        if (url.includes('/agents/profiles/validate')) return ok({ valid: true, messages: [] })
        if (url.includes('/agents/profiles/templates/preview')) return ok({ template: 'aws/sqs-monitor', content: '---\nname: x\n---\n' })
        if (url.includes('/agents/profiles/templates/')) return ok({ type: 'object', properties: {} })
        if (url.includes('/agents/profiles/templates')) return ok([{ name: 'aws/sqs-monitor', description: 'Poll an SQS queue' }])
        if (url.includes('/agents/profiles/schema')) return ok(PROFILE_SCHEMA)
        if (url.endsWith('/agents/profiles') && opts?.method === 'POST') return ok({ name: JSON.parse(opts.body).name, warnings: [] })
        return ok([])
      })
    }

    async function openScratch(mock: ReturnType<typeof scratchFetch>) {
      vi.stubGlobal('fetch', mock)
      render(<ProfileCreateModal open={true} onClose={() => {}} onCreated={() => {}} />)
      await act(async () => {})
      fireEvent.click(screen.getByRole('tab', { name: 'From scratch' }))
      await act(async () => {})
      fireEvent.change(screen.getByRole('textbox', { name: 'Profile name' }), { target: { value: 'query-agent' } })
    }

    const section = () => screen.getByRole('region', { name: 'Params a workflow step must fill' })
    const createButton = () => screen.getByRole('button', { name: /create profile/i })
    const postedContent = (mock: ReturnType<typeof scratchFetch>) => {
      const post = mock.mock.calls.find(([u, o]) => String(u).endsWith('/agents/profiles') && o?.method === 'POST')
      expect(post).toBeTruthy()
      return JSON.parse(post![1].body).content as string
    }

    it('shows the Params section in the main form after System prompt, and no params field under Advanced properties', async () => {
      await openScratch(scratchFetch())
      const prompt = screen.getByRole('textbox', { name: 'System prompt' })
      const advanced = screen.getByRole('button', { name: /advanced properties/i })
      expect(prompt.compareDocumentPosition(section()) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
      expect(section().compareDocumentPosition(advanced) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
      expect(within(section()).getByText('No params yet.')).toBeInTheDocument()
      // The schema's params field is the section, so Advanced lists no field.
      expect(advanced).toHaveTextContent('Advanced properties (0)')
      fireEvent.click(advanced)
      expect(screen.queryByRole('textbox', { name: 'params' })).toBeNull()
    })

    it('Create sends the params block just before the closing ---', async () => {
      const mock = scratchFetch()
      await openScratch(mock)
      fireEvent.change(screen.getByRole('textbox', { name: 'System prompt' }), { target: { value: 'Query ${TABLE_NAME}.' } })
      fireEvent.click(within(section()).getByRole('button', { name: '+ TABLE_NAME' }))
      fireEvent.click(within(section()).getByRole('button', { name: /Add param/ }))
      fireEvent.change(screen.getByLabelText('Param 2 name'), { target: { value: 'limit' } })
      fireEvent.change(screen.getByLabelText('Param 2 type'), { target: { value: 'int' } })
      fireEvent.change(screen.getByLabelText('Param 2 default'), { target: { value: '1' } })
      fireEvent.change(screen.getByLabelText('Param 2 description'), { target: { value: 'Max items to return' } })
      expect(createButton()).toBeEnabled()
      fireEvent.click(createButton())
      await act(async () => {})
      expect(postedContent(mock)).toBe([
        '---',
        'name: "query-agent"',
        'params:',
        '  table_name:',
        '    required: true',
        '  limit:',
        '    description: "Max items to return"',
        '    type: int',
        '    default: 1',
        '---',
        '',
        'Query ${TABLE_NAME}.',
        '',
      ].join('\n'))
    })

    it('a row error blocks Create until it is fixed', async () => {
      const mock = scratchFetch()
      await openScratch(mock)
      expect(createButton()).toBeEnabled()
      fireEvent.click(within(section()).getByRole('button', { name: /Add param/ }))
      expect(within(section()).getByText('Enter a name.')).toBeInTheDocument()
      expect(createButton()).toBeDisabled()
      fireEvent.change(screen.getByLabelText('Param 1 name'), { target: { value: 'limit' } })
      fireEvent.change(screen.getByLabelText('Param 1 type'), { target: { value: 'int' } })
      fireEvent.change(screen.getByLabelText('Param 1 default'), { target: { value: 'ten' } })
      expect(screen.getByLabelText('Param 1 default')).toHaveAccessibleDescription('Use a whole number, up to 18 digits.')
      expect(createButton()).toBeDisabled()
      fireEvent.change(screen.getByLabelText('Param 1 default'), { target: { value: '10' } })
      expect(createButton()).toBeEnabled()
      fireEvent.click(createButton())
      await act(async () => {})
      expect(postedContent(mock)).toContain('params:\n  limit:\n    type: int\n    default: 10\n---\n')
    })

    it('a bool row has a not set, true or false default and no allowed values, and required clears and disables the default', async () => {
      await openScratch(scratchFetch())
      fireEvent.click(within(section()).getByRole('button', { name: /Add param/ }))
      fireEvent.change(screen.getByLabelText('Param 1 name'), { target: { value: 'dry_run' } })
      fireEvent.change(screen.getByLabelText('Param 1 allowed values'), { target: { value: 'a, b' } })
      fireEvent.change(screen.getByLabelText('Param 1 type'), { target: { value: 'bool' } })
      const fallback = screen.getByLabelText('Param 1 default')
      expect(fallback.tagName).toBe('SELECT')
      expect([...(fallback as HTMLSelectElement).options].map(o => [o.value, o.textContent])).toEqual([['', 'not set'], ['true', 'true'], ['false', 'false']])
      expect(screen.getByLabelText('Param 1 allowed values')).toHaveValue('')
      expect(screen.getByLabelText('Param 1 allowed values')).toBeDisabled()
      fireEvent.change(fallback, { target: { value: 'true' } })
      expect(screen.getByLabelText('Param 1 default')).toHaveValue('true')
      fireEvent.click(screen.getByLabelText('Param 1 required'))
      expect(screen.getByLabelText('Param 1 default')).toHaveValue('')
      expect(screen.getByLabelText('Param 1 default')).toBeDisabled()
      expect(createButton()).toBeEnabled()
    })

    it('suggests params from the System prompt, leaving out covered names, shell variables and secret-looking names', async () => {
      await openScratch(scratchFetch())
      fireEvent.click(within(section()).getByRole('button', { name: /Add param/ }))
      fireEvent.change(screen.getByLabelText('Param 1 name'), { target: { value: 'region' } })
      fireEvent.change(screen.getByRole('textbox', { name: 'System prompt' }), {
        target: {
          value: 'Query ${TABLE_NAME} in ${AWS_REGION} with ${API_KEY} and ${GITHUB_TOKEN}.\nELAPSED=0\nWait ${ELAPSED} seconds. Read ${TABLE_NAME} again, then ${INDEX_NAME}.',
        },
      })
      const offered = () => within(section()).queryAllByRole('button', { name: /^\+ / }).map(b => b.textContent)
      expect(offered()).toEqual(['+ TABLE_NAME', '+ INDEX_NAME'])
      expect(within(section()).getByText('Skipped 2 placeholders that look like secrets. Keep secrets in .env, not in params.')).toBeInTheDocument()

      fireEvent.click(within(section()).getByRole('button', { name: '+ TABLE_NAME' }))
      expect(screen.getByLabelText('Param 2 name')).toHaveValue('table_name')
      expect(screen.getByLabelText('Param 2 type')).toHaveValue('string')
      expect(screen.getByLabelText('Param 2 required')).toBeChecked()
      expect(offered()).toEqual(['+ INDEX_NAME'])
    })
  })
})

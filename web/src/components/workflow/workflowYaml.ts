// workflowYaml: the workflow builder's data model, the YAML it writes, and its
// client-side checks.
//
// The browser never parses YAML. Edit loads `GET /workflows/{name}`, which is
// the spec as the server parsed it (JSON), and `fromSpec` maps that into
// builder state. Save regenerates the whole file with `toYaml`, so comments in
// the file are not kept.
//
// `toYaml` writes every key and every string as a JSON string literal. JSON
// strings are valid YAML double-quoted scalars, so no YAML library is needed
// and no value can be read back as a number, a bool, a null or a structure.
// Ints and bools are written bare.
//
// A step's params come in two parts. The params its agent profile declares
// (`GET /agents/profiles/{name}`) get one typed row each, and free-form rows
// hold every other name. Every param value is fixed here and saved in the
// file. Nothing about a param is asked at run time.

import type { ProfileParamDecl, WorkflowInputDecl, WorkflowSpec, WorkflowSpecStep, WorkflowStepCall } from '../../api'

export type StepCall = WorkflowStepCall

/** Call types in the order the builder offers them. */
export const STEP_CALLS: readonly StepCall[] = ['handoff', 'assign', 'send_message']

/** Call types that validate and save but are refused at run time (not built yet). */
export const RESERVED_CALLS: ReadonlySet<StepCall> = new Set<StepCall>(['assign', 'send_message'])

export function isReservedCall(call: StepCall | null | undefined): boolean {
  return !!call && RESERVED_CALLS.has(call)
}

export type InputType = WorkflowInputDecl['type']

export const INPUT_TYPES: readonly InputType[] = ['string', 'int', 'bool', 'path']

export type OnFailure = '' | 'halt' | 'continue'

export interface BuilderParam {
  key: string
  value: string
}

/** The kinds of value a profile param takes. */
export type ProfileParamType = 'string' | 'int' | 'bool'

/** A param the step's profile declares, as the builder uses it. */
export interface DeclaredParam {
  name: string
  type: ProfileParamType
  required: boolean
  /** The declared default as text (1 is "1", false is "false"), or null. */
  default: string | null
  /** Empty when the profile gives none. */
  description: string
  /** The allowed values as text, or null when the param has no enum. */
  enum: string[] | null
}

/**
 * What the builder knows about the params the step's profile declares.
 *
 * - `unknown`: no profile, or it has not been read.
 * - `loading`: the edit load is reading it.
 * - `error`: the read failed, so every param is a free-form row.
 * - `ready`: the declaration, which can be empty, with one value per declared
 *   param, in declaration order. An empty value means not set.
 */
export type DeclaredParams =
  | { status: 'unknown' }
  | { status: 'loading' }
  | { status: 'error' }
  | { status: 'ready'; params: DeclaredParam[]; values: string[] }

export interface BuilderInput {
  name: string
  type: InputType
  required: boolean
  /** The default as typed. null means no default. */
  default: string | null
}

export interface BuilderStep {
  id: string
  provider: string
  agent: string
  call: StepCall
  /**
   * Free-form params: names the profile does not declare, in the order they
   * were added. While the declaration is not known, every param is here.
   */
  params: BuilderParam[]
  /** The params the profile declares, and their values. */
  declared: DeclaredParams
  prompt: string
  /** As typed. Empty means not set, so the engine default applies. */
  retries: string
  /** Empty means not set. */
  onFailure: OnFailure
}

/** A change to one step: the new field values, or a function of the newest step that returns them. */
export type StepPatch = Partial<BuilderStep> | ((step: BuilderStep) => Partial<BuilderStep>)

export function applyStepPatch(step: BuilderStep, patch: StepPatch): BuilderStep {
  return { ...step, ...(typeof patch === 'function' ? patch(step) : patch) }
}

/** A builder workflow. `mode` is not stored: the builder only writes sequential workflows. */
export interface BuilderWorkflow {
  name: string
  description: string
  inputs: BuilderInput[]
  steps: BuilderStep[]
}

// The server's rules (constants.WORKFLOW_NAME_RE and the params rules in the
// builder contract). The server checks them again on every write.
export const NAME_RE = /^[A-Za-z0-9_-]{1,64}$/
export const PARAM_KEY_RE = /^[A-Za-z][A-Za-z0-9_.-]{0,63}$/
export const MAX_PARAMS = 32
export const MAX_PARAM_VALUE_CHARS = 512
/** The engine's placeholder pattern. Param values are fixed, so none may hold one. */
export const PLACEHOLDER_RE = /\{\{\s*([^{}]+?)\s*\}\}/
/** A declared int param, as the server checks it. */
const PARAM_INT_RE = /^-?[0-9]{1,18}$/
const MAX_ENUM_VALUES = 32
const MAX_DESCRIPTION_CHARS = 200
/** An enum error names at most this many values. */
const MAX_LISTED_VALUES = 10

export function newStep(id: string): BuilderStep {
  return {
    id,
    provider: '',
    agent: '',
    call: 'handoff',
    params: [],
    declared: { status: 'unknown' },
    prompt: '',
    retries: '',
    onFailure: '',
  }
}

export function newInput(): BuilderInput {
  return { name: '', type: 'string', required: false, default: null }
}

export function emptyWorkflow(): BuilderWorkflow {
  return { name: '', description: '', inputs: [], steps: [newStep('step-1')] }
}

/** The first `step-N` id that no step uses yet. */
export function nextStepId(steps: BuilderStep[]): string {
  const used = new Set(steps.map(s => s.id))
  let n = steps.length + 1
  while (used.has(`step-${n}`)) n++
  return `step-${n}`
}

// The params a profile declares.

const isScalar = (value: unknown): value is string | number | boolean =>
  typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean'

/**
 * The params a profile declares, from the `params` of
 * `GET /agents/profiles/{name}`, in declaration order.
 *
 * Profile content is untrusted, so this reads it defensively and keeps every
 * size bounded: at most MAX_PARAMS entries, only names that pass PARAM_KEY_RE,
 * and at most MAX_ENUM_VALUES enum values. A missing or unknown type reads as
 * string, a missing required as false. Defaults and enum values become text.
 * A required param keeps no default, and a bool keeps no enum.
 */
export function readDeclaredParams(raw: unknown): DeclaredParam[] {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return []
  const out: DeclaredParam[] = []
  for (const [name, entry] of Object.entries(raw as Record<string, unknown>)) {
    if (out.length >= MAX_PARAMS) break
    if (!PARAM_KEY_RE.test(name) || !entry || typeof entry !== 'object' || Array.isArray(entry)) continue
    const decl = entry as ProfileParamDecl
    const type: ProfileParamType = decl.type === 'int' || decl.type === 'bool' ? decl.type : 'string'
    const required = decl.required === true
    const listed = type !== 'bool' && Array.isArray(decl.enum) ? decl.enum : []
    const choices = [...new Set(listed.filter(isScalar).map(String))].filter(v => v !== '').slice(0, MAX_ENUM_VALUES)
    const fallback = !required && isScalar(decl.default) ? String(decl.default) : ''
    const description = typeof decl.description === 'string' ? decl.description.slice(0, MAX_DESCRIPTION_CHARS * 2) : ''
    out.push({
      name,
      type,
      required,
      default: fallback === '' ? null : fallback,
      // Counted in code points, so a cut never splits a character.
      description: [...description].slice(0, MAX_DESCRIPTION_CHARS).join(''),
      enum: choices.length > 0 ? choices : null,
    })
  }
  return out
}

/**
 * The params a step writes: the declared ones that have a value, in
 * declaration order, then the free-form rows in their order.
 */
export function writtenParams(step: BuilderStep): BuilderParam[] {
  const declared = step.declared.status === 'ready' ? step.declared : null
  const fromProfile = declared
    ? declared.params.flatMap((p, k) => (declared.values[k] ? [{ key: p.name, value: declared.values[k] }] : []))
    : []
  return [...fromProfile, ...step.params]
}

/**
 * The patch for a step whose profile declaration has arrived.
 *
 * Each declared name keeps the value it already has. Without one, it takes the
 * value of a free-form row with the same name, and that row leaves the
 * free-form rows. Still without one, it takes the declared default when
 * `prefill` is set. Values for names the profile does not declare are dropped.
 * The other free-form rows stay as they are.
 */
export function applyDeclaredParams(step: BuilderStep, params: DeclaredParam[], prefill: boolean): Partial<BuilderStep> {
  const before = step.declared.status === 'ready' ? step.declared : null
  const free = [...step.params]
  const values = params.map(param => {
    const k = before ? before.params.findIndex(p => p.name === param.name) : -1
    let value = before && k >= 0 ? (before.values[k] ?? '') : ''
    if (value === '') {
      const j = free.findIndex(p => p.key === param.name)
      if (j >= 0) value = free.splice(j, 1)[0].value
    }
    if (value === '' && prefill && param.default !== null) value = param.default
    return value
  })
  return { params: free, declared: { status: 'ready', params, values } }
}

/**
 * The patch for a step whose profile could not be read. Its params cannot be
 * checked, so every param becomes a free-form row, in the order the step
 * writes them.
 */
export function dropDeclaredParams(step: BuilderStep): Partial<BuilderStep> {
  return { params: writtenParams(step), declared: { status: 'error' } }
}

// The YAML writer.

// JSON leaves these characters raw, and a YAML parser does not read them back
// as written: DEL and the C1 controls are not allowed in a YAML stream, U+0085,
// U+2028 and U+2029 are line breaks in YAML 1.1 (PyYAML folds U+0085 into a
// space), and U+FEFF, U+FFFE and U+FFFF are special. A \u escape means the same
// thing in JSON and in YAML, so escaping them keeps the literal valid JSON.
const YAML_UNSAFE = /[\u007f-\u009f\u2028\u2029\ufeff\ufffe\uffff]/g

/** A string as a JSON string literal that YAML reads back unchanged. */
export function quote(value: string): string {
  return JSON.stringify(value).replace(
    YAML_UNSAFE,
    ch => `\\u${ch.charCodeAt(0).toString(16).padStart(4, '0')}`,
  )
}

const INT_RE = /^\s*([+-]?)(\d+)\s*$/

/** The decimal text of a whole number, or null when `text` is not one. */
export function intText(text: string): string | null {
  const m = INT_RE.exec(text)
  if (!m) return null
  const n = Number.parseInt(`${m[1]}${m[2]}`, 10)
  return Number.isSafeInteger(n) ? String(n) : null
}

/** A non-negative whole number, as the decimal text to write, or null. */
function retriesText(text: string): string | null {
  const t = intText(text)
  return t !== null && !t.startsWith('-') ? t : null
}

function defaultScalar(input: BuilderInput, value: string): string {
  if (input.type === 'int') return intText(value) ?? quote(value)
  if (input.type === 'bool' && (value === 'true' || value === 'false')) return value
  return quote(value)
}

/**
 * The YAML file for a builder workflow.
 *
 * Key order: the workflow writes name, description, mode, inputs, steps. An
 * input writes type, then required only when true, then default only when
 * set. A step writes id, provider, agent, call (always), params (only when
 * there are any), prompt, retries (only when set), on_failure (only when set).
 * Params are the declared ones that have a value, in declaration order, then
 * the free-form rows (writtenParams). An empty declared param is left out.
 *
 * A value that fails `validateBuilder` is still written, as a quoted string,
 * so the preview always shows something. Save is blocked until it is fixed.
 */
export function toYaml(wf: BuilderWorkflow): string {
  const lines: string[] = []
  const field = (indent: string, key: string, scalar: string) => lines.push(`${indent}${quote(key)}: ${scalar}`)

  field('', 'name', quote(wf.name))
  field('', 'description', quote(wf.description))
  field('', 'mode', quote('sequential'))

  if (wf.inputs.length === 0) {
    field('', 'inputs', '{}')
  } else {
    lines.push(`${quote('inputs')}:`)
    for (const input of wf.inputs) {
      lines.push(`  ${quote(input.name)}:`)
      field('    ', 'type', quote(input.type))
      if (input.required) field('    ', 'required', 'true')
      if (input.default !== null) field('    ', 'default', defaultScalar(input, input.default))
    }
  }

  if (wf.steps.length === 0) {
    field('', 'steps', '[]')
  } else {
    lines.push(`${quote('steps')}:`)
    for (const step of wf.steps) {
      field('  - ', 'id', quote(step.id))
      field('    ', 'provider', quote(step.provider))
      field('    ', 'agent', quote(step.agent))
      field('    ', 'call', quote(step.call))
      const params = writtenParams(step)
      if (params.length > 0) {
        lines.push(`    ${quote('params')}:`)
        for (const p of params) field('      ', p.key, quote(p.value))
      }
      field('    ', 'prompt', quote(step.prompt))
      if (step.retries.trim() !== '') field('    ', 'retries', retriesText(step.retries) ?? quote(step.retries))
      if (step.onFailure !== '') field('    ', 'on_failure', quote(step.onFailure))
    }
  }

  return `${lines.join('\n')}\n`
}

// GET /workflows/{name} to builder state.

/** Step fields the builder cannot show. A workflow that sets one is not editable here. */
export const BLOCKING_STEP_FIELDS = [
  'engine',
  'output_schema',
  'when',
  'until',
  'max_iterations',
  'on_stall',
  'on_no_progress',
] as const

const BUILDER_STEP_FIELDS = new Set(['id', 'provider', 'agent', 'prompt', 'call', 'params', 'retries', 'on_failure'])
const BLOCKING_FIELD_SET = new Set<string>(BLOCKING_STEP_FIELDS)
const INPUT_FIELDS = new Set(['type', 'required', 'default'])

export type FromSpecResult =
  | { ok: true; workflow: BuilderWorkflow }
  | { ok: false; blocking: string[] }

const isSet = (value: unknown) => value !== null && value !== undefined

/**
 * Builder state for a workflow, from the JSON `GET /workflows/{name}` returns.
 *
 * Regenerating the file must not lose data, so a workflow the builder cannot
 * represent comes back as `{ok: false, blocking}`, naming each field that
 * blocks editing: a Python workflow, a mode other than sequential, or a step
 * field the builder has no control for. A step or input field this client does
 * not know about blocks too, for the same reason.
 */
export function fromSpec(spec: WorkflowSpec): FromSpecResult {
  const path = typeof spec.path === 'string' ? spec.path : ''
  if (/\.py$/i.test(path)) return { ok: false, blocking: ['Python workflow (edit the .py file)'] }
  if (!Array.isArray(spec.steps)) return { ok: false, blocking: ['steps (not a list)'] }

  const blocking: string[] = []
  if (isSet(spec.mode) && spec.mode !== 'sequential') blocking.push(`mode: ${String(spec.mode)}`)

  const inputs: BuilderInput[] = []
  for (const [name, raw] of Object.entries(spec.inputs ?? {})) {
    const decl = (raw ?? {}) as WorkflowInputDecl & Record<string, unknown>
    for (const [key, value] of Object.entries(decl)) {
      if (!INPUT_FIELDS.has(key) && isSet(value)) blocking.push(`input '${name}': ${key}`)
    }
    if (!(INPUT_TYPES as readonly string[]).includes(decl.type)) {
      blocking.push(`input '${name}': type ${String(decl.type)}`)
      continue
    }
    inputs.push({
      name,
      type: decl.type,
      required: decl.required === true,
      default: isSet(decl.default) ? String(decl.default) : null,
    })
  }

  const steps: BuilderStep[] = []
  spec.steps.forEach((raw: WorkflowSpecStep, i) => {
    const label = `step '${typeof raw?.id === 'string' ? raw.id : i + 1}'`
    for (const [key, value] of Object.entries(raw ?? {})) {
      if (BLOCKING_FIELD_SET.has(key) || !BUILDER_STEP_FIELDS.has(key)) {
        if (isSet(value)) blocking.push(`${label}: ${key}`)
      }
    }
    const call = raw?.call ?? 'handoff'
    if (!STEP_CALLS.includes(call)) blocking.push(`${label}: call ${String(call)}`)
    const onFailure = raw?.on_failure ?? ''
    if (onFailure !== '' && onFailure !== 'halt' && onFailure !== 'continue') {
      blocking.push(`${label}: on_failure ${String(onFailure)}`)
    }
    steps.push({
      id: String(raw?.id ?? ''),
      provider: String(raw?.provider ?? ''),
      agent: String(raw?.agent ?? ''),
      call: STEP_CALLS.includes(call) ? call : 'handoff',
      // The declaration is read later, so every param starts as a free-form row.
      params: Object.entries(raw?.params ?? {}).map(([key, value]) => ({ key, value: String(value) })),
      declared: { status: 'unknown' },
      prompt: String(raw?.prompt ?? ''),
      retries: isSet(raw?.retries) ? String(raw.retries) : '',
      onFailure: onFailure === 'halt' || onFailure === 'continue' ? onFailure : '',
    })
  })

  if (blocking.length > 0) return { ok: false, blocking }
  return {
    ok: true,
    workflow: {
      name: String(spec.name ?? ''),
      description: typeof spec.description === 'string' ? spec.description : '',
      inputs,
      steps,
    },
  }
}

// Client-side checks.

/**
 * Errors keyed by field path: `name`, `steps`, `inputs.<i>.name`,
 * `inputs.<i>.default`, `steps.<i>.id`, `steps.<i>.provider`,
 * `steps.<i>.agent`, `steps.<i>.prompt`, `steps.<i>.retries`,
 * `steps.<i>.params`, `steps.<i>.params.<j>.key`,
 * `steps.<i>.params.<j>.value` and `steps.<i>.declared.<k>`. Indexes are
 * 0-based. `<k>` is the param's place in the profile's declaration.
 */
export type BuilderErrors = Record<string, string>

const NAME_HINT = 'Use 1 to 64 letters, digits, "_" or "-".'

export const PLACEHOLDER_MESSAGE =
  'Param values are fixed when you build the workflow, so {{...}} placeholders are not allowed.'

function nameError(value: string, what: string): string | null {
  if (value === '') return `Enter ${what}.`
  return NAME_RE.test(value) ? null : NAME_HINT
}

/** The checks every param value gets, declared or free-form. */
function paramValueError(value: string): string | null {
  if (/[\r\n]/.test(value)) return 'A param value cannot contain a line break.'
  // Count code points, as the server does, not UTF-16 units.
  if ([...value].length > MAX_PARAM_VALUE_CHARS) return `A param value can have at most ${MAX_PARAM_VALUE_CHARS} characters.`
  if (PLACEHOLDER_RE.test(value)) return PLACEHOLDER_MESSAGE
  return null
}

function listValues(values: string[]): string {
  return values.length > MAX_LISTED_VALUES
    ? `${values.slice(0, MAX_LISTED_VALUES).join(', ')}, ...`
    : `${values.join(', ')}.`
}

/** A declared param's value against its declaration. An empty value is missing. */
function declaredValueError(param: DeclaredParam, value: string): string | null {
  if (value === '') return param.required ? 'Required.' : null
  const general = paramValueError(value)
  if (general) return general
  if (param.enum) return param.enum.includes(value) ? null : `Use one of: ${listValues(param.enum)}`
  if (param.type === 'int' && !PARAM_INT_RE.test(value)) return 'Use a whole number, up to 18 digits.'
  if (param.type === 'bool' && value !== 'true' && value !== 'false') return 'Use true or false.'
  return null
}

/** Indexes of entries whose key repeats an earlier entry's key. */
function repeats(keys: string[]): Set<number> {
  const seen = new Set<string>()
  const out = new Set<number>()
  keys.forEach((key, i) => {
    if (key !== '' && seen.has(key)) out.add(i)
    seen.add(key)
  })
  return out
}

/**
 * The checks the builder runs before it validates or saves. They mirror the
 * server's rules for names, step ids, params and retries, so an obvious mistake
 * shows next to its field instead of coming back as a 400. A step whose profile
 * declaration is known also gets the server's run-time profile check: each
 * required param has a value, and int, bool and enum values have the declared
 * form. The server stays the authority: it checks everything again.
 */
export function validateBuilder(wf: BuilderWorkflow): BuilderErrors {
  const errors: BuilderErrors = {}
  const put = (key: string, message: string | null) => {
    if (message) errors[key] = message
  }

  put('name', nameError(wf.name, 'a workflow name'))

  const repeatedInputs = repeats(wf.inputs.map(i => i.name))
  wf.inputs.forEach((input, i) => {
    put(`inputs.${i}.name`, nameError(input.name, 'an input name') ?? (repeatedInputs.has(i) ? 'Another input has this name.' : null))
    if (input.default === null) return
    if (input.type === 'int' && intText(input.default) === null) put(`inputs.${i}.default`, 'The default must be a whole number.')
    if (input.type === 'bool' && input.default !== 'true' && input.default !== 'false') put(`inputs.${i}.default`, 'The default must be true or false.')
  })

  if (wf.steps.length === 0) put('steps', 'Add at least one step.')
  const repeatedSteps = repeats(wf.steps.map(s => s.id))
  wf.steps.forEach((step, i) => {
    const at = `steps.${i}`
    put(`${at}.id`, nameError(step.id, 'a step id') ?? (repeatedSteps.has(i) ? 'Another step has this id.' : null))
    if (step.provider.trim() === '') put(`${at}.provider`, 'Pick a provider.')
    if (step.agent.trim() === '') put(`${at}.agent`, 'Pick an agent profile.')
    if (step.prompt.trim() === '') put(`${at}.prompt`, 'Write a prompt.')
    if (step.retries.trim() !== '' && retriesText(step.retries) === null) put(`${at}.retries`, 'Use a whole number, 0 or more.')

    if (writtenParams(step).length > MAX_PARAMS) put(`${at}.params`, `A step can have at most ${MAX_PARAMS} params.`)
    const declared = step.declared.status === 'ready' ? step.declared : null
    declared?.params.forEach((param, k) => put(`${at}.declared.${k}`, declaredValueError(param, declared.values[k] ?? '')))
    const declaredNames = new Set(declared?.params.map(p => p.name) ?? [])
    const repeatedParams = repeats(step.params.map(p => p.key))
    step.params.forEach((param, j) => {
      const key =
        param.key === ''
          ? 'Enter a param name, or remove this row.'
          : !PARAM_KEY_RE.test(param.key)
            ? 'Start with a letter, then use up to 63 letters, digits, "_", "." or "-".'
            : declaredNames.has(param.key)
              ? 'The profile declares this param. Set it in its row above.'
              : repeatedParams.has(j)
                ? 'Another param has this name.'
                : null
      put(`${at}.params.${j}.key`, key)
      put(`${at}.params.${j}.value`, paramValueError(param.value))
    })
  })

  return errors
}

/** The errors under `prefix` (for example `steps.2`), with the prefix and its dot removed. */
export function errorsUnder(errors: BuilderErrors, prefix: string): BuilderErrors {
  const out: BuilderErrors = {}
  const lead = `${prefix}.`
  for (const [key, message] of Object.entries(errors)) {
    if (key.startsWith(lead)) out[key.slice(lead.length)] = message
  }
  return out
}

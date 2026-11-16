// The workflow builder's data layer: the YAML it writes (toYaml), the mapping
// from GET /workflows/{name} back into builder state (fromSpec), and the
// client-side checks (validateBuilder).
import { describe, it, expect } from 'vitest'
import type { WorkflowSpec } from '../api'
import {
  BLOCKING_STEP_FIELDS,
  LINE_BREAK_RE,
  MAX_PARAMS,
  errorsUnder,
  fromSpec,
  newStep,
  nextStepId,
  quote,
  toYaml,
  validateBuilder,
  type BuilderStep,
  type BuilderWorkflow,
} from '../components/workflow/workflowYaml'

const step = (over: Partial<BuilderStep> = {}): BuilderStep => ({
  ...newStep('step-1'),
  provider: 'claude_code',
  agent: 'developer',
  prompt: 'Do the thing.',
  ...over,
})

const workflow = (over: Partial<BuilderWorkflow> = {}): BuilderWorkflow => ({
  name: 'wf',
  description: '',
  inputs: [],
  steps: [step()],
  ...over,
})

// Reads back the subset of YAML that toYaml writes: block mappings and
// sequences by indentation, keys and strings as JSON literals, bare ints and
// bools, and {} or []. Test-only: the browser never parses YAML. The real
// parser is the server's (PyYAML).
function readEmitted(text: string): any {
  const lines = text.split('\n').filter(l => l !== '')
  let i = 0
  const indentOf = (l: string) => l.length - l.trimStart().length
  const scalar = (src: string): unknown => (src === '{}' ? {} : src === '[]' ? [] : JSON.parse(src))
  const splitKey = (src: string): [string, string] => {
    expect(src.startsWith('"'), `key is not JSON-quoted: ${src}`).toBe(true)
    let j = 1
    while (j < src.length && src[j] !== '"') j += src[j] === '\\' ? 2 : 1
    const rest = src.slice(j + 1)
    expect(rest.startsWith(':'), `no ':' after the key in ${src}`).toBe(true)
    return [JSON.parse(src.slice(0, j + 1)), rest.slice(1).trim()]
  }
  const isItem = (l: string, indent: number) => indentOf(l) === indent && l.slice(indent).startsWith('- ')
  const block = (indent: number): unknown => {
    if (isItem(lines[i], indent)) {
      const out: unknown[] = []
      while (i < lines.length && isItem(lines[i], indent)) {
        lines[i] = ' '.repeat(indent + 2) + lines[i].slice(indent + 2)
        out.push(block(indent + 2))
      }
      return out
    }
    const out: Record<string, unknown> = {}
    while (i < lines.length && indentOf(lines[i]) === indent && !isItem(lines[i], indent)) {
      const [key, rest] = splitKey(lines[i].slice(indent))
      i++
      out[key] = rest !== '' ? scalar(rest) : block(indentOf(lines[i]))
    }
    return out
  }
  const doc = block(0)
  expect(i, 'unread lines').toBe(lines.length)
  return doc
}

// What GET /workflows/{name} returns for parsed YAML: WorkflowSpec.model_dump(),
// so every unset field is present as null, plus path, source and content_hash.
function asServerDump(parsed: any, source: string): WorkflowSpec {
  return {
    name: parsed.name,
    description: parsed.description ?? '',
    mode: parsed.mode ?? 'sequential',
    inputs: Object.fromEntries(
      Object.entries(parsed.inputs ?? {}).map(([k, d]: [string, any]) => [
        k,
        { type: d.type, required: d.required ?? false, default: d.default ?? null },
      ]),
    ),
    steps: parsed.steps.map((s: any) => ({
      id: s.id, provider: s.provider, agent: s.agent, prompt: s.prompt,
      engine: null, output_schema: null, when: null, until: null, max_iterations: null,
      on_stall: null, on_no_progress: null,
      on_failure: s.on_failure ?? null, retries: s.retries ?? null,
      call: s.call ?? null, params: s.params ?? null,
    })),
    path: `/home/u/.aws/cli-agent-orchestrator/workflows/${parsed.name}.yaml`,
    source,
    content_hash: 'c0ffee',
  }
}

const FULL: BuilderWorkflow = {
  name: 'nightly-smoke-check',
  description: 'Nightly end-to-end smoke check',
  inputs: [
    { name: 'run_label', type: 'string', required: true, default: null },
    { name: 'count', type: 'int', required: false, default: '3' },
    { name: 'dry', type: 'bool', required: false, default: 'false' },
    { name: 'report', type: 'path', required: false, default: '/tmp/report.md' },
  ],
  steps: [
    step({
      id: 'send-message-01',
      agent: 'sqs-sender-beta',
      params: [
        { key: 'aws-profile-name', value: 'beta' },
        { key: 'aws-account-number', value: '123456789012' },
      ],
      prompt: 'Send message 01 for {{workflow.inputs.run_label}} and report the MessageId.',
      retries: '0',
    }),
    step({ id: 'check-ddb-row', agent: 'ddb-checker', call: 'assign', prompt: 'Check the row.', onFailure: 'continue' }),
  ],
}

const FULL_YAML = `"name": "nightly-smoke-check"
"description": "Nightly end-to-end smoke check"
"mode": "sequential"
"inputs":
  "run_label":
    "type": "string"
    "required": true
  "count":
    "type": "int"
    "default": 3
  "dry":
    "type": "bool"
    "default": false
  "report":
    "type": "path"
    "default": "/tmp/report.md"
"steps":
  - "id": "send-message-01"
    "provider": "claude_code"
    "agent": "sqs-sender-beta"
    "call": "handoff"
    "params":
      "aws-profile-name": "beta"
      "aws-account-number": "123456789012"
    "prompt": "Send message 01 for {{workflow.inputs.run_label}} and report the MessageId."
    "retries": 0
  - "id": "check-ddb-row"
    "provider": "claude_code"
    "agent": "ddb-checker"
    "call": "assign"
    "prompt": "Check the row."
    "on_failure": "continue"
`

describe('toYaml', () => {
  it('writes keys in the contract order, with conditional keys only when set', () => {
    expect(toYaml(FULL)).toBe(FULL_YAML)
  })

  it('JSON-quotes quotes, colons, newlines and backslashes, one scalar per line', () => {
    const prompt = 'He said "go": now\nnext line\ttab \\ back\r\nend'
    const out = toYaml(workflow({ description: 'a: b # not a comment', steps: [step({ prompt })] }))
    const lines = out.split('\n')
    expect(lines).toContain(String.raw`"description": "a: b # not a comment"`)
    expect(lines).toContain(String.raw`    "prompt": "He said \"go\": now\nnext line\ttab \\ back\r\nend"`)
    // The newlines inside the value stay escaped, so the step is still 5 lines.
    expect(lines.filter(l => l.startsWith('    ')).length).toBe(4)
  })

  it('keeps unicode as is, and escapes the characters YAML reads differently', () => {
    const out = toYaml(workflow({ steps: [step({ params: [{ key: 'region', value: 'café 日本語 🚀' }] })] }))
    expect(out).toContain('      "region": "café 日本語 🚀"')
    // U+0085 and U+2028 are line breaks to a YAML 1.1 parser, DEL is not
    // allowed in a YAML stream, and U+FEFF is special.
    expect(quote('a\u0085b\u2028c\u2029d\u007fe\u0080f\ufeffg')).toBe(String.raw`"a\u0085b\u2028c\u2029d\u007fe\u0080f\ufeffg"`)
    expect(JSON.parse(quote('a\u0085b\u2028c\u007f'))).toBe('a\u0085b\u2028c\u007f')
  })

  it('quotes values YAML would otherwise read as numbers, bools or nulls', () => {
    const values = ['123456789012', 'true', 'no', 'null', '~', '0x1F', '1e3', '- item', '{a: 1}', '']
    const out = toYaml(workflow({ steps: [step({ params: values.map((value, i) => ({ key: `k${i}`, value })) })] }))
    values.forEach((value, i) => expect(out).toContain(`      "k${i}": ${JSON.stringify(value)}\n`))
  })

  it('writes ints and bools bare, and string defaults quoted', () => {
    const out = toYaml(workflow({
      inputs: [
        { name: 'n', type: 'int', required: true, default: '-007' },
        { name: 'b', type: 'bool', required: false, default: 'true' },
        { name: 's', type: 'string', required: false, default: '42' },
        { name: 'e', type: 'string', required: false, default: '' },
      ],
      steps: [step({ retries: '2' })],
    }))
    expect(out).toContain('    "required": true\n    "default": -7\n')
    expect(out).toContain('  "b":\n    "type": "bool"\n    "default": true\n')
    expect(out).toContain('  "s":\n    "type": "string"\n    "default": "42"\n')
    // An empty string is a default that is set.
    expect(out).toContain('  "e":\n    "type": "string"\n    "default": ""\n')
    expect(out).toContain('    "retries": 2\n')
    expect(out).not.toContain('"required": false')
  })

  it('always writes call, handoff by default, and leaves params out when empty', () => {
    const out = toYaml(workflow({ steps: [step({ id: 'a' }), step({ id: 'b', call: 'send_message' })] }))
    expect(out.match(/"call": /g)).toHaveLength(2)
    expect(out).toContain('    "call": "handoff"\n')
    expect(out).toContain('    "call": "send_message"\n')
    expect(out).not.toContain('"params"')
    expect(out).not.toContain('"retries"')
    expect(out).not.toContain('"on_failure"')
  })

  it('always writes description, mode and inputs, using {} and [] when empty', () => {
    expect(toYaml(workflow({ steps: [] }))).toBe('"name": "wf"\n"description": ""\n"mode": "sequential"\n"inputs": {}\n"steps": []\n')
  })

  it('writes YAML the subset reader reads back to the intended document', () => {
    expect(readEmitted(FULL_YAML)).toEqual({
      name: 'nightly-smoke-check',
      description: 'Nightly end-to-end smoke check',
      mode: 'sequential',
      inputs: {
        run_label: { type: 'string', required: true },
        count: { type: 'int', default: 3 },
        dry: { type: 'bool', default: false },
        report: { type: 'path', default: '/tmp/report.md' },
      },
      steps: [
        {
          id: 'send-message-01', provider: 'claude_code', agent: 'sqs-sender-beta', call: 'handoff',
          params: { 'aws-profile-name': 'beta', 'aws-account-number': '123456789012' },
          prompt: 'Send message 01 for {{workflow.inputs.run_label}} and report the MessageId.', retries: 0,
        },
        { id: 'check-ddb-row', provider: 'claude_code', agent: 'ddb-checker', call: 'assign', prompt: 'Check the row.', on_failure: 'continue' },
      ],
    })
  })
})

describe('fromSpec', () => {
  it('round-trips: builder state to YAML, parsed by the server, back to the same state', () => {
    const tricky: BuilderWorkflow = {
      ...FULL,
      description: 'quotes " and colons: and\nnewlines and 🚀',
      inputs: [...FULL.inputs, { name: 'empty', type: 'string', required: false, default: '' }],
      steps: [...FULL.steps, step({ id: 'third', call: 'send_message', params: [{ key: 'z.last', value: 'a: "b"' }, { key: 'a-first', value: '' }] })],
    }
    const yaml = toYaml(tricky)
    const result = fromSpec(asServerDump(readEmitted(yaml), yaml))
    expect(result).toEqual({ ok: true, workflow: tricky })
    // Param order is declaration order, which the engine keeps in the prompt.
    if (result.ok) expect(result.workflow.steps[2].params.map(p => p.key)).toEqual(['z.last', 'a-first'])
  })

  it('reads a null call as handoff and null params as none', () => {
    const result = fromSpec({
      name: 'old', description: 'written before call and params', mode: 'sequential', inputs: {},
      steps: [{ id: 'a', provider: 'p', agent: 'g', prompt: 'x', call: null, params: null, retries: null, on_failure: null }],
      path: '/wf/old.yaml', content_hash: 'h',
    })
    expect(result).toEqual({ ok: true, workflow: { name: 'old', description: 'written before call and params', inputs: [], steps: [step({ id: 'a', provider: 'p', agent: 'g', prompt: 'x' })] } })
  })

  it('refuses a Python workflow', () => {
    expect(fromSpec({ name: 'lint', path: '/wf/lint.py', source: 'def main(): ...', content_hash: 'h', findings: [], inputs: {} }))
      .toEqual({ ok: false, blocking: ['Python workflow (edit the .py file)'] })
  })

  it('refuses a mode other than sequential', () => {
    const result = fromSpec({ name: 'p', mode: 'parallel', inputs: {}, steps: [{ id: 'a', provider: 'p', agent: 'g', prompt: 'x' }], path: '/wf/p.yaml' })
    expect(result).toEqual({ ok: false, blocking: ['mode: parallel'] })
  })

  it.each(BLOCKING_STEP_FIELDS.map(f => [f]))('refuses a step that sets %s', field => {
    const value = field === 'output_schema' ? { type: 'object' } : field === 'max_iterations' ? 3 : 'x'
    const result = fromSpec({
      name: 'w', mode: 'sequential', inputs: {}, path: '/wf/w.yaml',
      steps: [
        { id: 'ok', provider: 'p', agent: 'g', prompt: 'x', [field]: null },
        { id: 'build', provider: 'p', agent: 'g', prompt: 'x', [field]: value },
      ],
    })
    expect(result).toEqual({ ok: false, blocking: [`step 'build': ${field}`] })
  })

  it('refuses a step or input field it does not know, rather than dropping it', () => {
    const result = fromSpec({
      name: 'w', mode: 'sequential', path: '/wf/w.yaml',
      inputs: { a: { type: 'string', required: false, default: null, secret: true } as any },
      steps: [{ id: 's', provider: 'p', agent: 'g', prompt: 'x', timeout_s: 30, when: 'x' }],
    })
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.blocking).toEqual(["input 'a': secret", "step 's': timeout_s", "step 's': when"])
  })
})

describe('validateBuilder', () => {
  it('passes a complete workflow', () => {
    expect(validateBuilder(FULL)).toEqual({})
  })

  it('checks the workflow name', () => {
    expect(validateBuilder(workflow({ name: '' })).name).toBe('Enter a workflow name.')
    for (const bad of ['has space', 'dot.ted', 'x'.repeat(65), 'naïve']) {
      expect(validateBuilder(workflow({ name: bad })).name, bad).toMatch(/1 to 64/)
    }
    expect(validateBuilder(workflow({ name: `A-b_9${'x'.repeat(59)}` })).name).toBeUndefined()
  })

  it('checks step ids and their uniqueness', () => {
    const errors = validateBuilder(workflow({ steps: [step({ id: 'a' }), step({ id: 'a' }), step({ id: 'b c' }), step({ id: '' })] }))
    expect(errors['steps.0.id']).toBeUndefined()
    expect(errors['steps.1.id']).toBe('Another step has this id.')
    expect(errors['steps.2.id']).toMatch(/1 to 64/)
    expect(errors['steps.3.id']).toBe('Enter a step id.')
  })

  it('checks input names, their uniqueness and typed defaults', () => {
    const errors = validateBuilder(workflow({
      inputs: [
        { name: 'x', type: 'string', required: false, default: null },
        { name: 'x', type: 'string', required: false, default: null },
        { name: 'bad name', type: 'string', required: false, default: null },
        { name: 'n', type: 'int', required: false, default: '1.5' },
        { name: 'm', type: 'int', required: false, default: '-12' },
        { name: 'b', type: 'bool', required: false, default: 'yes' },
      ],
    }))
    expect(errors['inputs.0.name']).toBeUndefined()
    expect(errors['inputs.1.name']).toBe('Another input has this name.')
    expect(errors['inputs.2.name']).toMatch(/1 to 64/)
    expect(errors['inputs.3.default']).toBe('The default must be a whole number.')
    expect(errors['inputs.4.default']).toBeUndefined()
    expect(errors['inputs.5.default']).toBe('The default must be true or false.')
  })

  it('requires a provider, an agent and a prompt', () => {
    const errors = validateBuilder(workflow({ steps: [step({ provider: '', agent: '', prompt: '  \n ' })] }))
    expect(errors['steps.0.provider']).toBe('Pick a provider.')
    expect(errors['steps.0.agent']).toBe('Pick an agent profile.')
    expect(errors['steps.0.prompt']).toBe('Write a prompt.')
  })

  it('requires at least one step', () => {
    expect(validateBuilder(workflow({ steps: [] })).steps).toBe('Add at least one step.')
  })

  it('accepts retries as a whole number of 0 or more, or unset', () => {
    for (const ok of ['', '0', '3', ' 10 ']) expect(validateBuilder(workflow({ steps: [step({ retries: ok })] }))['steps.0.retries'], ok).toBeUndefined()
    for (const bad of ['-1', '1.5', 'two', '1e3']) {
      expect(validateBuilder(workflow({ steps: [step({ retries: bad })] }))['steps.0.retries'], bad).toBe('Use a whole number, 0 or more.')
    }
  })

  it('checks param names: a leading letter, then up to 63 of [A-Za-z0-9_.-], and unique', () => {
    const keys = ['aws-profile-name', 'table.name_2', `a${'b'.repeat(63)}`, `a${'b'.repeat(64)}`, '1st', 'has space', '', 'aws-profile-name']
    const errors = validateBuilder(workflow({ steps: [step({ params: keys.map(key => ({ key, value: 'v' })) })] }))
    const at = (j: number) => errors[`steps.0.params.${j}.key`]
    expect([at(0), at(1), at(2)]).toEqual([undefined, undefined, undefined])
    expect(at(3)).toMatch(/Start with a letter/)
    expect(at(4)).toMatch(/Start with a letter/)
    expect(at(5)).toMatch(/Start with a letter/)
    expect(at(6)).toBe('Enter a param name, or remove this row.')
    expect(at(7)).toBe('Another param has this name.')
  })

  it('checks param values: no line breaks, at most 512 characters counted as the server counts them', () => {
    const values = ['x'.repeat(512), 'x'.repeat(513), 'a\nb', 'a\rb', '🚀'.repeat(512), '🚀'.repeat(513), '']
    const errors = validateBuilder(workflow({ steps: [step({ params: values.map((value, j) => ({ key: `k${j}`, value })) })] }))
    const at = (j: number) => errors[`steps.0.params.${j}.value`]
    expect(at(0)).toBeUndefined()
    expect(at(1)).toBe('A param value can have at most 512 characters.')
    expect(at(2)).toBe('A param value cannot contain a line break.')
    expect(at(3)).toBe('A param value cannot contain a line break.')
    // 512 emoji are 1024 UTF-16 units but 512 code points, which the server allows.
    expect(at(4)).toBeUndefined()
    expect(at(5)).toBe('A param value can have at most 512 characters.')
    expect(at(6)).toBeUndefined()
  })

  it('refuses every line break the server refuses, not only \\n and \\r', () => {
    const others = ['\v', '\f', '\u001c', '\u001d', '\u001e', '\u0085', '\u2028', '\u2029']
    const values = [...others.map(b => `true${b}- dry_run: false`), 'a\tb\u001fc\u0007d']
    const errors = validateBuilder(workflow({ steps: [step({ params: values.map((value, j) => ({ key: `k${j}`, value })) })] }))
    others.forEach((_, j) => expect(errors[`steps.0.params.${j}.value`]).toBe('A param value cannot contain a line break.'))
    // A tab and the other control characters are not line breaks.
    expect(errors[`steps.0.params.${others.length}.value`]).toBeUndefined()
  })

  it('LINE_BREAK_RE matches exactly the characters Python str.splitlines() splits on', () => {
    const matched: number[] = []
    for (let cp = 0; cp <= 0xffff; cp++) if (LINE_BREAK_RE.test(String.fromCharCode(cp))) matched.push(cp)
    expect(matched).toEqual([0x0a, 0x0b, 0x0c, 0x0d, 0x1c, 0x1d, 0x1e, 0x85, 0x2028, 0x2029])
  })

  it(`allows at most ${MAX_PARAMS} params on a step`, () => {
    const params = (n: number) => Array.from({ length: n }, (_, j) => ({ key: `k${j}`, value: 'v' }))
    expect(validateBuilder(workflow({ steps: [step({ params: params(MAX_PARAMS) })] }))['steps.0.params']).toBeUndefined()
    expect(validateBuilder(workflow({ steps: [step({ params: params(MAX_PARAMS + 1) })] }))['steps.0.params']).toBe('A step can have at most 32 params.')
  })

  it('groups errors by field path', () => {
    const errors = validateBuilder(workflow({ steps: [step(), step({ id: 'b', agent: '' })] }))
    expect(errorsUnder(errors, 'steps.1')).toEqual({ agent: 'Pick an agent profile.' })
    expect(errorsUnder(errors, 'steps.0')).toEqual({})
  })
})

describe('nextStepId', () => {
  it('picks the first step-N id that is free', () => {
    expect(nextStepId([])).toBe('step-1')
    expect(nextStepId([step({ id: 'step-1' })])).toBe('step-2')
    expect(nextStepId([step({ id: 'a' }), step({ id: 'step-3' })])).toBe('step-4')
  })
})

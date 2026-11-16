// profileParams: the Params editor's row rules, the YAML block it writes, the
// splice into a profile's frontmatter, the read of the validator's answer, and
// the suggestions from the prompt.
import { describe, it, expect } from 'vitest'
import {
  bodyAfterFrontmatter,
  hasParamRowErrors,
  leavesValueOpen,
  newParamRow,
  paramKey,
  paramRowErrors,
  parseAllowed,
  readParamsState,
  rowsFromParams,
  spliceParamsBlock,
  suggestParams,
  suggestedRow,
  updateParamRow,
  writeParamsBlock,
  type ParamRow,
} from '../components/profileParams'
import { quote } from '../components/workflow/workflowYaml'

const row = (patch: Partial<ParamRow>): ParamRow => ({ ...newParamRow(), ...patch })
const errorsOf = (...rows: ParamRow[]) => paramRowErrors(rows)

describe('writeParamsBlock', () => {
  it('writes the layout of the AWS examples', () => {
    const rows = [
      row({ name: 'table_name', required: true, description: 'DynamoDB table name' }),
      row({ name: 'partition_key_type', required: true, description: 'DynamoDB type', allowed: 'S, N, B' }),
      row({ name: 'limit', type: 'int', default: '1', description: 'Max items to return' }),
    ]
    expect(writeParamsBlock(rows)).toBe([
      'params:',
      '  table_name:',
      '    description: "DynamoDB table name"',
      '    required: true',
      '  partition_key_type:',
      '    description: "DynamoDB type"',
      '    required: true',
      '    enum: ["S", "N", "B"]',
      '  limit:',
      '    description: "Max items to return"',
      '    type: int',
      '    default: 1',
      '',
    ].join('\n'))
  })

  it('orders the keys of an entry description, type, required, enum, default', () => {
    expect(writeParamsBlock([
      row({ name: 'batch', type: 'int', default: '20', allowed: '10, 20, 30', description: 'Batch size' }),
      row({ name: 'mode', required: true, allowed: 'fast, full', description: 'Run mode' }),
      row({ name: 'dry_run', type: 'bool', default: 'false' }),
    ])).toBe([
      'params:',
      '  batch:',
      '    description: "Batch size"',
      '    type: int',
      '    enum: [10, 20, 30]',
      '    default: 20',
      '  mode:',
      '    description: "Run mode"',
      '    required: true',
      '    enum: ["fast", "full"]',
      '  dry_run:',
      '    type: bool',
      '    default: false',
      '',
    ].join('\n'))
  })

  it('leaves out an empty description, type string, required false, no allowed values and an empty default', () => {
    expect(writeParamsBlock([row({ name: 'note' }), row({ name: 'region', description: 'AWS region' })]))
      .toBe('params:\n  note: {}\n  region:\n    description: "AWS region"\n')
  })

  it('writes no block for no rows', () => {
    expect(writeParamsBlock([])).toBe('')
  })

  it('quotes a name YAML 1.1 reads as a bool or a null, in any case, and writes other names bare', () => {
    for (const name of ['y', 'N', 'yes', 'No', 'on', 'OFF', 'True', 'false', 'null', 'NULL']) {
      expect(paramKey(name)).toBe(`"${name}"`)
    }
    for (const name of ['only', 'nullable', 'yes_no', 'offset', 'table.name', 'key-type']) expect(paramKey(name)).toBe(name)
    expect(writeParamsBlock([row({ name: 'on', description: 'Run it' })])).toBe('params:\n  "on":\n    description: "Run it"\n')
  })

  it('writes strings with quote(), and ints and bools bare', () => {
    const hard = 'Say "hi": a # b \\ c 🚀 \u2028 end'
    const text = writeParamsBlock([
      row({ name: 'greeting', description: hard, default: 'a: "b" # c', allowed: 'a: "b" # c, it\'s' }),
      row({ name: 'offset', type: 'int', default: '-007', allowed: '-007, 0, 12' }),
      row({ name: 'zero', type: 'int', default: '-0' }),
    ])
    expect(text).toBe([
      'params:',
      '  greeting:',
      `    description: ${quote(hard)}`,
      `    enum: [${quote('a: "b" # c')}, ${quote("it's")}]`,
      `    default: ${quote('a: "b" # c')}`,
      '  offset:',
      '    type: int',
      '    enum: [-7, 0, 12]',
      '    default: -7',
      '  zero:',
      '    type: int',
      '    default: 0',
      '',
    ].join('\n'))
    expect(text).toContain('\\u2028')
    expect(text).not.toContain('\u2028')
  })

  it('ends every line with the line ending it is given', () => {
    const text = writeParamsBlock([row({ name: 'table_name', required: true }), row({ name: 'note' })], '\r\n')
    expect(text).toBe('params:\r\n  table_name:\r\n    required: true\r\n  note: {}\r\n')
    expect(text.replace(/\r\n/g, '')).not.toMatch(/[\r\n]/)
  })
})

describe('the row rules', () => {
  it('a name is required, follows the name rule, and no two rows share one', () => {
    expect(errorsOf(row({ name: '' }))[0].name).toBe('Enter a name.')
    for (const bad of ['1st', '_x', 'has space', 'a'.repeat(65), 'a:b']) {
      expect(errorsOf(row({ name: bad }))[0].name).toMatch(/^Start with a letter/)
    }
    for (const good of ['a', 'table_name', 'aws.region', 'key-type', 'A'.repeat(64)]) expect(errorsOf(row({ name: good }))[0]).toEqual({})
    expect(errorsOf(row({ name: 'limit' }), row({ name: 'limit' })).map(e => e.name)).toEqual([undefined, 'Another param has this name.'])
  })

  it('checking required clears the default, and a required row with a default is an error', () => {
    expect(updateParamRow(row({ name: 'x', default: 'orders' }), { required: true })).toMatchObject({ required: true, default: '' })
    expect(errorsOf(row({ name: 'x', required: true, default: 'orders' }))[0].default).toBe("A required param can't have a default.")
  })

  it('type bool clears the allowed values and keeps only a true or false default', () => {
    expect(updateParamRow(row({ name: 'x', allowed: 'a, b', default: 'a' }), { type: 'bool' })).toMatchObject({ allowed: '', default: '' })
    expect(updateParamRow(row({ name: 'x', default: 'true' }), { type: 'bool' })).toMatchObject({ default: 'true' })
    expect(errorsOf(row({ name: 'x', type: 'bool', default: 'yes' }))[0].default).toBe('Use true or false.')
    expect(errorsOf(row({ name: 'x', type: 'bool', allowed: 'true' }))[0].allowed).toBe("A bool param can't have allowed values.")
  })

  it('type int takes a whole number of up to 18 digits, as the default and as each allowed value', () => {
    for (const good of ['0', '-3', '007', '9'.repeat(18), `-${'9'.repeat(18)}`]) {
      expect(errorsOf(row({ name: 'n', type: 'int', default: good }))[0]).toEqual({})
    }
    for (const bad of ['1.5', '1e3', ' 1', '+1', 'ten', '9'.repeat(19)]) {
      expect(errorsOf(row({ name: 'n', type: 'int', default: bad }))[0].default).toBe('Use a whole number, up to 18 digits.')
    }
    expect(errorsOf(row({ name: 'n', type: 'int', allowed: '1, x' }))[0].allowed).toBe('Each allowed value must be a whole number, up to 18 digits.')
    // Compared by value, as the server compares ints.
    expect(errorsOf(row({ name: 'n', type: 'int', allowed: '1, 01' }))[0].allowed).toBe('Each allowed value must be different.')
    expect(errorsOf(row({ name: 'n', type: 'int', allowed: '1, 2', default: '02' }))[0]).toEqual({})
  })

  it('type string takes one line of at most 512 characters with no {{...}} placeholder', () => {
    const defaultError = (value: string) => errorsOf(row({ name: 's', default: value }))[0].default
    expect(defaultError('x'.repeat(512))).toBeUndefined()
    expect(defaultError('🚀'.repeat(512))).toBeUndefined()
    expect(defaultError('x'.repeat(513))).toBe('Use at most 512 characters.')
    expect(defaultError('a\nb')).toBe('Use one line.')
    expect(defaultError('{{ workflow.inputs.table }}')).toMatch(/^A default can't hold a \{\{\.\.\.\}\} placeholder/)
    expect(defaultError('{{\t }}')).toMatch(/placeholder/)
    expect(defaultError('{x} and {{}}')).toBeUndefined()
    const allowedError = (value: string) => errorsOf(row({ name: 's', allowed: value }))[0].allowed
    expect(allowedError(`a, ${'x'.repeat(513)}`)).toBe('Each allowed value can have at most 512 characters.')
    expect(allowedError('a, {{x}}')).toMatch(/^An allowed value can't hold a \{\{\.\.\.\}\} placeholder/)
  })

  it('allowed values are comma-separated and trimmed: none, or 1 to 32 distinct values, and a set default is one of them', () => {
    expect(parseAllowed(' S , N,,B , ')).toEqual(['S', 'N', 'B'])
    expect(parseAllowed('  ')).toEqual([])
    const values = Array.from({ length: 33 }, (_, i) => `v${i}`)
    expect(errorsOf(row({ name: 's', allowed: values.join(', ') }))[0].allowed).toBe('Use at most 32 allowed values.')
    expect(errorsOf(row({ name: 's', allowed: values.slice(0, 32).join(', ') }))[0]).toEqual({})
    expect(errorsOf(row({ name: 's', allowed: 'S, N, S' }))[0].allowed).toBe('Each allowed value must be different.')
    expect(errorsOf(row({ name: 's', allowed: 'S, N', default: 'B' }))[0].default).toBe('Use one of the allowed values.')
    expect(errorsOf(row({ name: 's', allowed: 'S, N', default: 'N' }))[0]).toEqual({})
  })

  it('a description is one line of at most 200 characters', () => {
    expect(errorsOf(row({ name: 'd', description: 'x'.repeat(200) }))[0]).toEqual({})
    expect(errorsOf(row({ name: 'd', description: 'x'.repeat(201) }))[0].description).toBe('Use at most 200 characters.')
    expect(errorsOf(row({ name: 'd', description: 'a\rb' }))[0].description).toBe('Use one line.')
  })

  it.each([
    ['VT', '\v'], ['FF', '\f'], ['FS', '\u001c'], ['GS', '\u001d'], ['RS', '\u001e'],
    ['NEL', '\u0085'], ['LS', '\u2028'], ['PS', '\u2029'],
  ])('a default, an allowed value and a description with %s are not one line', (_label, lineBreak) => {
    const value = `a${lineBreak}b`
    expect(errorsOf(row({ name: 'd', default: value }))[0].default).toBe('Use one line.')
    expect(errorsOf(row({ name: 'e', allowed: `ok, ${value}` }))[0].allowed).toBe('Use one line for each allowed value.')
    expect(errorsOf(row({ name: 't', description: value }))[0].description).toBe('Use one line.')
  })

  it('hasParamRowErrors is true for any row error and for more than 32 rows', () => {
    expect(hasParamRowErrors([])).toBe(false)
    expect(hasParamRowErrors([row({ name: 'ok' })])).toBe(false)
    expect(hasParamRowErrors([row({ name: 'ok' }), row({ name: '' })])).toBe(true)
    expect(hasParamRowErrors(Array.from({ length: 33 }, (_, i) => row({ name: `p${i}` })))).toBe(true)
  })
})

describe('spliceParamsBlock', () => {
  const ROWS = [row({ name: 'table_name', required: true }), row({ name: 'limit', type: 'int', default: '5' })]

  const DOC = [
    '---',
    'name: query-agent',
    '# a comment before the block',
    'params:',
    '  old_name:',
    '    description: "Old"  # an inline comment',
    '# a column-0 comment inside the block',
    '    required: true',
    '',
    '# a trailing comment',
    'provider: claude_code',
    'tags: [aws]',
    '---',
    '',
    'The body may say params: too.',
    'params:',
    '',
  ].join('\n')
  const blockOf = (doc: string, eol: string) => {
    const last = `    required: true${eol}`
    return { start: doc.indexOf(`params:${eol}`), end: doc.indexOf(last) + last.length }
  }

  it('replaces only the block, and keeps blank lines and column-0 comments at its end outside it', () => {
    const { start, end } = blockOf(DOC, '\n')
    const out = spliceParamsBlock(DOC, ROWS)
    expect(out).toEqual({ ok: true, text: DOC.slice(0, start) + writeParamsBlock(ROWS) + DOC.slice(end) })
    const text = out.ok ? out.text : ''
    expect(text).toContain('\n\n# a trailing comment\nprovider: claude_code\n')
    expect(text).not.toContain('inside the block')
    expect(text).not.toContain('an inline comment')
  })

  it('removes the block lines when there are no rows', () => {
    const { start, end } = blockOf(DOC, '\n')
    expect(spliceParamsBlock(DOC, [])).toEqual({ ok: true, text: DOC.slice(0, start) + DOC.slice(end) })
  })

  it('inserts the block at the end of the frontmatter, before the closing ---, when there is none', () => {
    const doc = '---\nname: query-agent\nprovider: claude_code\n# last line\n---\n\n# Body.\n'
    const at = doc.indexOf('---\n\n# Body')
    expect(spliceParamsBlock(doc, ROWS)).toEqual({ ok: true, text: doc.slice(0, at) + writeParamsBlock(ROWS) + doc.slice(at) })
    expect(spliceParamsBlock(doc, [])).toEqual({ ok: true, text: doc })
  })

  it('replaces a flow value that starts with {, a params: line with a comment, and a block that runs to the closing ---', () => {
    for (const first of ['params: {}', 'params: {a: {}}  # flow', 'params:   # note']) {
      const doc = `---\nname: x\n${first}\nmodel: m\n---\n`
      expect(spliceParamsBlock(doc, ROWS)).toEqual({ ok: true, text: `---\nname: x\n${writeParamsBlock(ROWS)}model: m\n---\n` })
    }
    expect(spliceParamsBlock('---\nname: x\nparams:\n  a: {}\n---\nbody\n', ROWS))
      .toEqual({ ok: true, text: `---\nname: x\n${writeParamsBlock(ROWS)}---\nbody\n` })
  })

  it('keeps CRLF line endings, and every byte outside the block', () => {
    const doc = DOC.replace(/\n/g, '\r\n')
    const { start, end } = blockOf(doc, '\r\n')
    const out = spliceParamsBlock(doc, ROWS)
    expect(out).toEqual({ ok: true, text: doc.slice(0, start) + writeParamsBlock(ROWS, '\r\n') + doc.slice(end) })
    expect((out.ok ? out.text : '').replace(/\r\n/g, '')).not.toMatch(/[\r\n]/)
    expect(spliceParamsBlock('---\r\nname: x\r\n---\r\n', ROWS))
      .toEqual({ ok: true, text: `---\r\nname: x\r\n${writeParamsBlock(ROWS, '\r\n')}---\r\n` })
  })

  it.each([
    ['there is no frontmatter', '# Just a body\nparams:\n  a: {}\n', 'no-frontmatter'],
    ['there are two params keys', '---\nparams:\n  a: {}\nname: x\nparams:\n  b: {}\n---\n', 'duplicate-key'],
    ['a quoted key repeats a bare one', '---\nparams:\n  a: {}\n"params": {}\n---\n', 'duplicate-key'],
    ['the key is double-quoted', '---\n"params":\n  a: {}\n---\n', 'quoted-key'],
    ['the key is single-quoted', "---\n'params':\n  a: {}\n---\n", 'quoted-key'],
    ['the value has an anchor', '---\nparams: &shared\n  a: {}\n---\n', 'anchor'],
    ['the value is an alias', '---\nbase: &p {a: {}}\nparams: *p\n---\n', 'anchor'],
    ['the value has a tag', '---\nparams: !!map\n  a: {}\n---\n', 'anchor'],
    ['an anchor in the block may be used outside it', '---\nparams:\n  a:\n    description: &d "x"\ndescription: *d\n---\n', 'anchor'],
    ['a scalar follows the colon', '---\nparams: none\n---\n', 'other-value'],
    ['a list follows the colon', '---\nparams: []\n---\n', 'other-value'],
    ['a block scalar follows the colon', '---\nparams: |\n  a\n---\n', 'other-value'],
    ['no space follows the colon', '---\nparams:{}\n---\n', 'other-value'],
    ['a space comes before the colon', '---\nparams :\n  a: {}\n---\n', 'key-form'],
    ['an explicit key could name params', '---\n? params\n: {a: {}}\n---\n', 'key-form'],
    ['a tagged key could name params', '---\n!!str params:\n  a: {}\n---\n', 'key-form'],
    ['a lone CR breaks a line', '---\nname: x\rparams: {}\n---\n', 'line-breaks'],
    ['U+2028 breaks a line', '---\nname: x\u2028params: {}\n---\n', 'line-breaks'],
    ['a flow value closes on a column-0 line', '---\nparams: {\n  a: {required: true}\n}\nname: x\n---\n', 'multi-line'],
    ['a flow value closes on an indented line', '---\nparams: {\n  a: {}\n  }\nname: x\n---\n', 'multi-line'],
    ['a flow list in an entry runs onto the next line', '---\nparams:\n  a:\n    enum: ["S",\n      "N"]\nname: x\n---\n', 'multi-line'],
    ['a double-quoted string runs onto a column-0 line', '---\nparams:\n  a:\n    description: "line one\ncontinued"\nname: x\n---\n', 'multi-line'],
    ['a single-quoted string runs onto the next line', "---\nparams:\n  a:\n    description: 'line one\n      continued'\nname: x\n---\n", 'multi-line'],
  ])('refuses when %s', (_label, doc, reason) => {
    expect(spliceParamsBlock(doc, ROWS)).toEqual({ ok: false, reason })
    expect(spliceParamsBlock(doc, [])).toEqual({ ok: false, reason })
  })
})

describe('leavesValueOpen', () => {
  it.each([
    ['a quoted string with an escaped quote, a "#" and a backslash', '    description: "Say \\"hi\\": a # b \\\\ c"'],
    ['a single-quoted string with a doubled quote', "    description: 'it''s fine'"],
    ['an apostrophe inside a plain value', "    description: don't stop"],
    ['braces inside a plain value', '    description: use {x} and [y]'],
    ['a flow list of quoted values', '    enum: ["S", "N", "B"]'],
    ['nested flow mappings and a comment', 'params: {a: {required: true}}  # flow'],
    ['a quote inside a comment', '    required: true  # "not a string'],
    ['a quoted key', '  "on":'],
  ])('lets a line pass when it closes what it opens: %s', (_label, line) => {
    expect(leavesValueOpen(line)).toBe(false)
  })

  it.each([
    ['an open double-quoted string', '    description: "line one'],
    ['an open single-quoted string', "    description: 'line one"],
    ['an open flow mapping', 'params: {'],
    ['an open flow list', '    enum: ["S",'],
    ['a quote that the escape keeps open', '    description: "ends with \\"'],
  ])('flags a line that leaves a value open: %s', (_label, line) => {
    expect(leavesValueOpen(line)).toBe(true)
  })

  it('lets every line the writer produces pass', () => {
    const block = writeParamsBlock([
      row({ name: 'on', description: 'Say "hi": a # b \\ c 🚀 \u2028 it\'s', default: '{x}, [y]', allowed: '{x}, [y], it\'s' }),
      row({ name: 'offset', type: 'int', default: '-7', allowed: '-7, 0, 12' }),
      row({ name: 'dry_run', type: 'bool', default: 'false' }),
      row({ name: 'note' }),
    ])
    for (const line of block.split('\n')) expect(leavesValueOpen(line)).toBe(false)
  })
})

describe('reading the params the validator returns', () => {
  it('makes one row per param, in declaration order', () => {
    expect(rowsFromParams({
      table_name: { type: 'string', required: true, description: 'DynamoDB table name' },
      partition_key_type: { type: 'string', required: true, enum: ['S', 'N', 'B'] },
      limit: { type: 'int', required: false, default: 1, description: 'Max items to return' },
      consistent: { type: 'bool', required: false, default: false },
    })).toEqual({
      ok: true,
      rows: [
        row({ name: 'table_name', required: true, description: 'DynamoDB table name' }),
        row({ name: 'partition_key_type', required: true, allowed: 'S, N, B' }),
        row({ name: 'limit', type: 'int', default: '1', description: 'Max items to return' }),
        row({ name: 'consistent', type: 'bool', default: 'false' }),
      ],
    })
    expect(rowsFromParams({})).toEqual({ ok: true, rows: [] })
  })

  it("is not ok for a value a row can't hold exactly, or a shape it does not know", () => {
    for (const params of [
      { big: { type: 'int', default: 2 ** 53 + 2 } },
      { pick: { type: 'string', enum: ['a,b', 'c'] } },
      { pick: { type: 'string', enum: [' a', 'b'] } },
      { pick: { type: 'string', enum: ['', 'b'] } },
      { odd: { type: 'string', pattern: '.*' } },
      { odd: { type: 'float' } },
      { 'bad name': { type: 'string' } },
      [],
    ]) expect(rowsFromParams(params)).toEqual({ ok: false })
  })

  const DOC = '---\nname: query-agent\nparams:\n  table_name:\n    required: true\n---\n'
  const NO_BLOCK = '---\nname: query-agent\n---\n'
  const error = (path: string | null, message = 'Something is wrong.') => ({ severity: 'error' as const, message, path })

  it('is editable when the server read the block the splice finds', () => {
    expect(readParamsState(DOC, { messages: [], params: { table_name: { type: 'string', required: true } } }))
      .toEqual({ kind: 'editable', rows: [row({ name: 'table_name', required: true })] })
  })

  it('is editable with no rows when the text has no params block, whatever else is wrong', () => {
    expect(readParamsState(NO_BLOCK, { messages: [error('(root)')], params: null })).toEqual({ kind: 'editable', rows: [] })
    expect(readParamsState(NO_BLOCK, { messages: [] })).toEqual({ kind: 'editable', rows: [] })
  })

  it('is text only when the block has errors, listing the findings on params', () => {
    const finding = error('params.limit.default', "param 'limit': default must be a whole number, not a string")
    expect(readParamsState(DOC, { messages: [error('model'), finding], params: null }))
      .toEqual({ kind: 'text-only', reason: 'block-errors', findings: [finding] })
  })

  it('is text only when an error stopped the checks before params', () => {
    const stop = error(null, 'The frontmatter is too large.')
    expect(readParamsState(DOC, { messages: [stop], params: null }))
      .toEqual({ kind: 'text-only', reason: 'frontmatter-errors', findings: [stop] })
  })

  it('is text only when the server and the splice disagree about the block', () => {
    expect(readParamsState(NO_BLOCK, { messages: [], params: { a: {} } })).toMatchObject({ kind: 'text-only', reason: 'key-form' })
    expect(readParamsState(DOC, { messages: [], params: null })).toMatchObject({ kind: 'text-only', reason: 'key-form' })
  })

  it("is text only when the splice refuses, or a row can't hold a value", () => {
    expect(readParamsState('---\n"params": {}\n---\n', { messages: [], params: {} })).toMatchObject({ kind: 'text-only', reason: 'quoted-key' })
    expect(readParamsState(DOC, { messages: [], params: { pick: { type: 'string', enum: ['a,b'] } } }))
      .toMatchObject({ kind: 'text-only', reason: 'values' })
  })
})

describe('suggestParams', () => {
  it('offers each distinct ${NAME} placeholder, in order of first appearance', () => {
    expect(suggestParams('Use ${TABLE_NAME} in ${REGION}, then ${TABLE_NAME}. Not $TABLE, ${1X} or {{x}}.', []))
      .toEqual({ names: ['TABLE_NAME', 'REGION'], skippedSecrets: 0 })
  })

  it("leaves out a name a row covers: its lower-case form is the row's name, or ends with _ and the row's name", () => {
    const rows = [row({ name: 'region' }), row({ name: 'Table_Name' }), row({ name: '' })]
    expect(suggestParams('${AWS_REGION} ${REGION} ${TABLE_NAME} ${QUEUE_URL} ${REGIONAL}', rows).names).toEqual(['QUEUE_URL', 'REGIONAL'])
  })

  it('leaves out a shell variable the text assigns', () => {
    const text = 'ELAPSED=0\nwhile true; do sleep 5; ELAPSED=$((ELAPSED+5)); echo ${ELAPSED}; done\n  export OUT=x;echo ${OUT} ${COUNT}'
    expect(suggestParams(text, []).names).toEqual(['COUNT'])
  })

  it('leaves out names that look like secrets, and counts them', () => {
    expect(suggestParams('${API_KEY} ${GitHub_Token} ${DB_PASSWORD} ${client_secret} ${API_KEY} ${MONKEY} ${TABLE}', []))
      .toEqual({ names: ['TABLE'], skippedSecrets: 5 })
  })

  it('leaves out a name whose lower-case form fails the name rule, and offers at most 32', () => {
    expect(suggestParams('${_PRIVATE} ${OK}', []).names).toEqual(['OK'])
    const many = Array.from({ length: 40 }, (_, i) => `\${P${i}}`).join(' ')
    expect(suggestParams(many, []).names).toHaveLength(32)
  })

  it('adds a suggestion as a required string row, named in lower case', () => {
    expect(suggestedRow('TABLE_NAME')).toEqual(row({ name: 'table_name', required: true }))
  })

  it('reads the body after the frontmatter', () => {
    expect(bodyAfterFrontmatter('---\nname: ${NOT_THIS}\n---\nUse ${THIS}.\n')).toBe('\nUse ${THIS}.\n')
    expect(bodyAfterFrontmatter('No frontmatter ${X}')).toBe('No frontmatter ${X}')
  })
})

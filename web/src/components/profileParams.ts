// profileParams: the data model of the Params editor in the Profiles tab, its
// checks, the YAML it writes, and the splice that puts that YAML into a
// profile's frontmatter.
//
// The browser never parses YAML. Edit and Clone read a profile's params through
// POST /agents/profiles/validate, which returns them as JSON. A row change
// writes the whole params block again and splices it into the text by lines,
// so every byte outside the block stays the same.
//
// Profile text is untrusted. The splice works on lines with fixed patterns. It
// runs no regex taken from the profile, and it changes only the params block.

import type { ProfileValidationMessage, ProfileValidationResponse } from '../api'
import { LINE_BREAK_RE, MAX_PARAMS, MAX_PARAM_VALUE_CHARS, PARAM_KEY_RE, PLACEHOLDER_RE, quote } from './workflow/workflowYaml'

export type ParamType = 'string' | 'int' | 'bool'

export const PARAM_TYPES: readonly ParamType[] = ['string', 'int', 'bool']

/** One row of the Params editor. Every value is text, as typed. */
export interface ParamRow {
  name: string
  type: ParamType
  required: boolean
  /** Empty means no default. A bool default is "true" or "false". */
  default: string
  /** Comma-separated. Empty means none. */
  allowed: string
  description: string
}

/** A row's errors, by field. A field with no error has no key. */
export interface ParamRowErrors {
  name?: string
  default?: string
  allowed?: string
  description?: string
}

// The server's rules for a profile's params (models/agent_profile.py). The
// server checks them again on every validate and write.
export const MAX_PARAM_ROWS = MAX_PARAMS
const MAX_ALLOWED_VALUES = 32
const MAX_DESCRIPTION_CHARS = 200
const INT_RE = /^-?[0-9]{1,18}$/

const NAME_HINT = 'Start with a letter, then use up to 63 letters, digits, "_", "." or "-".'

export function newParamRow(name = ''): ParamRow {
  return { name, type: 'string', required: false, default: '', allowed: '', description: '' }
}

/**
 * `row` with `patch` applied. A required row keeps no default. A bool row
 * keeps no allowed values, and only a true or false default.
 */
export function updateParamRow(row: ParamRow, patch: Partial<ParamRow>): ParamRow {
  const next = { ...row, ...patch }
  if (next.required) next.default = ''
  if (next.type === 'bool') {
    next.allowed = ''
    if (next.default !== 'true' && next.default !== 'false') next.default = ''
  }
  return next
}

/** The allowed values in `text`: split on commas and trimmed, with empty items left out. */
export function parseAllowed(text: string): string[] {
  return text.split(',').map(v => v.trim()).filter(v => v !== '')
}

/** A whole number in INT_RE form, as YAML 1.1 reads it back: no leading zeros and no "-0". */
function canonicalInt(text: string): string {
  const negative = text.startsWith('-')
  const digits = (negative ? text.slice(1) : text).replace(/^0+(?=[0-9])/, '')
  return negative && digits !== '0' ? `-${digits}` : digits
}

/** True when `value` has more than `limit` characters, counted in code points as the server counts them. */
function tooLong(value: string, limit: number): boolean {
  if (value.length <= limit) return false
  if (value.length > limit * 2) return true
  return [...value].length > limit
}

/**
 * True when the engine would find a {{...}} placeholder in `value`. Each run of
 * whitespace is cut to one space first, as the server does. That keeps the
 * answer and bounds the time.
 */
function hasPlaceholder(value: string): boolean {
  return PLACEHOLDER_RE.test(value.replace(/\s+/g, ' '))
}

function defaultError(type: ParamType, value: string): string | null {
  if (type === 'int') return INT_RE.test(value) ? null : 'Use a whole number, up to 18 digits.'
  if (type === 'bool') return value === 'true' || value === 'false' ? null : 'Use true or false.'
  if (LINE_BREAK_RE.test(value)) return 'Use one line.'
  if (tooLong(value, MAX_PARAM_VALUE_CHARS)) return `Use at most ${MAX_PARAM_VALUE_CHARS} characters.`
  if (hasPlaceholder(value)) {
    return "A default can't hold a {{...}} placeholder. It is copied into workflow steps, where values are fixed when the workflow is built."
  }
  return null
}

function allowedValueError(type: ParamType, value: string): string | null {
  if (type === 'int') return INT_RE.test(value) ? null : 'Each allowed value must be a whole number, up to 18 digits.'
  if (LINE_BREAK_RE.test(value)) return 'Use one line for each allowed value.'
  if (tooLong(value, MAX_PARAM_VALUE_CHARS)) return `Each allowed value can have at most ${MAX_PARAM_VALUE_CHARS} characters.`
  if (hasPlaceholder(value)) {
    return "An allowed value can't hold a {{...}} placeholder. Values are fixed when the workflow is built."
  }
  return null
}

/**
 * Each row's errors, in row order. The rules mirror the server's, so a
 * mistake shows next to its field instead of coming back from validate.
 */
export function paramRowErrors(rows: ParamRow[]): ParamRowErrors[] {
  const seen = new Set<string>()
  return rows.map(row => {
    const errors: ParamRowErrors = {}

    if (row.name === '') errors.name = 'Enter a name.'
    else if (!PARAM_KEY_RE.test(row.name)) errors.name = NAME_HINT
    else if (seen.has(row.name)) errors.name = 'Another param has this name.'
    seen.add(row.name)

    // The allowed values, compared the way the server compares them: an int by value.
    const key = (v: string) => (row.type === 'int' ? canonicalInt(v) : v)
    let allowed: Set<string> | null = null
    const values = parseAllowed(row.allowed)
    if (values.length > 0) {
      if (row.type === 'bool') errors.allowed = "A bool param can't have allowed values."
      else if (values.length > MAX_ALLOWED_VALUES) errors.allowed = `Use at most ${MAX_ALLOWED_VALUES} allowed values.`
      else {
        const problem = values.map(v => allowedValueError(row.type, v)).find(p => p !== null)
        if (problem) errors.allowed = problem
        else if (new Set(values.map(key)).size !== values.length) errors.allowed = 'Each allowed value must be different.'
        else allowed = new Set(values.map(key))
      }
    }

    if (row.default !== '') {
      const problem = row.required ? "A required param can't have a default." : defaultError(row.type, row.default)
      if (problem) errors.default = problem
      else if (allowed && !allowed.has(key(row.default))) errors.default = 'Use one of the allowed values.'
    }

    if (LINE_BREAK_RE.test(row.description)) errors.description = 'Use one line.'
    else if (tooLong(row.description, MAX_DESCRIPTION_CHARS)) errors.description = `Use at most ${MAX_DESCRIPTION_CHARS} characters.`

    return errors
  })
}

/** True when a row has an error, or there are more rows than a profile may declare. */
export function hasParamRowErrors(rows: ParamRow[]): boolean {
  return rows.length > MAX_PARAM_ROWS || paramRowErrors(rows).some(e => Object.keys(e).length > 0)
}

export function sameParamRows(a: ParamRow[], b: ParamRow[]): boolean {
  return a.length === b.length && a.every((r, i) => {
    const s = b[i]
    return r.name === s.name && r.type === s.type && r.required === s.required &&
      r.default === s.default && r.allowed === s.allowed && r.description === s.description
  })
}

// The writer.

/** Names YAML 1.1 reads as a bool or a null when they are written bare. */
const YAML11_WORDS = new Set(['y', 'n', 'yes', 'no', 'on', 'off', 'true', 'false', 'null'])

/** A param name as a mapping key: bare, or quoted when YAML 1.1 would not read it back as a string. */
export function paramKey(name: string): string {
  return !PARAM_KEY_RE.test(name) || YAML11_WORDS.has(name.toLowerCase()) ? quote(name) : name
}

/** A default or allowed value: an int or a bool bare, anything else quoted. */
function scalar(type: ParamType, value: string): string {
  if (type === 'int' && INT_RE.test(value)) return canonicalInt(value)
  if (type === 'bool' && (value === 'true' || value === 'false')) return value
  return quote(value)
}

/**
 * The params block for `rows`, in the layout of the AWS examples, with every
 * line ended by `eol`. No rows give no block.
 *
 * Entries are in row order. Inside an entry: description, type, required,
 * enum, default. An empty description, type string, required false, no allowed
 * values and an empty default are left out. An entry with nothing left is
 * written `name: {}`, because `name:` alone reads as null.
 */
export function writeParamsBlock(rows: ParamRow[], eol: '\n' | '\r\n' = '\n'): string {
  if (rows.length === 0) return ''
  const lines = ['params:']
  for (const row of rows) {
    const fields: string[] = []
    if (row.description !== '') fields.push(`description: ${quote(row.description)}`)
    if (row.type !== 'string') fields.push(`type: ${row.type}`)
    if (row.required) fields.push('required: true')
    const allowed = parseAllowed(row.allowed)
    if (allowed.length > 0) fields.push(`enum: [${allowed.map(v => scalar(row.type, v)).join(', ')}]`)
    if (row.default !== '') fields.push(`default: ${scalar(row.type, row.default)}`)
    if (fields.length === 0) lines.push(`  ${paramKey(row.name)}: {}`)
    else lines.push(`  ${paramKey(row.name)}:`, ...fields.map(f => `    ${f}`))
  }
  return lines.map(line => line + eol).join('')
}

// The splice.

/** The leading frontmatter: the pattern rewriteFrontmatterName uses. */
const FRONTMATTER_RE = /^---\r?\n([\s\S]*?)\r?\n---/

/** Why the editor leaves a params block to the text. */
export type TextOnlyReason =
  | 'no-frontmatter'
  | 'line-breaks'
  | 'key-form'
  | 'duplicate-key'
  | 'quoted-key'
  | 'anchor'
  | 'multi-line'
  | 'other-value'
  | 'values'
  | 'block-errors'
  | 'frontmatter-errors'

export const TEXT_ONLY_REASONS: Record<TextOnlyReason, string> = {
  'no-frontmatter': 'The text has no frontmatter.',
  'line-breaks': 'The frontmatter has a line break this editor does not read.',
  'key-form': "The frontmatter is written in a form this editor can't change safely.",
  'duplicate-key': 'The frontmatter has more than one params key.',
  'quoted-key': 'The params key is quoted.',
  anchor: 'The params block uses a YAML anchor, alias or tag.',
  'multi-line': 'A value in the params block runs onto another line.',
  'other-value': 'Something other than a comment follows "params:" on its line.',
  values: "The params block has a value this form can't show exactly.",
  'block-errors': 'The params block has errors. The findings below list them.',
  'frontmatter-errors': "The frontmatter has errors, so its params can't be read. The findings below list them.",
}

type Eol = '\n' | '\r\n'

interface Line {
  start: number
  /** Just after the line ending. */
  end: number
  /** Without the line ending. */
  text: string
  eol: Eol
}

/** The lines between the frontmatter's `---` lines, and where the closing one starts. */
function frontmatterLines(text: string): { lines: Line[]; close: number } | null {
  const m = FRONTMATTER_RE.exec(text)
  if (!m) return null
  // The match starts at 0. The closing --- is its last three characters, and
  // the line before it always ends with a line feed.
  const close = m[0].length - 3
  const lines: Line[] = []
  for (let pos = text.indexOf('\n') + 1; pos < close;) {
    const nl = text.indexOf('\n', pos)
    const cr = nl > pos && text[nl - 1] === '\r'
    lines.push({ start: pos, end: nl + 1, text: text.slice(pos, cr ? nl - 1 : nl), eol: cr ? '\r\n' : '\n' })
    pos = nl + 1
  }
  return { lines, close }
}

/** A column-0 params key, bare or quoted, then any spaces before the colon, then the rest of the line. */
const PARAMS_KEY_RE = /^(params|"params"|'params')([ \t]*):(.*)$/
/** A column-0 key form that can name params without the word showing: an explicit, anchored, aliased, tagged or escaped key. */
const HIDDEN_KEY_RE = /^[?&*!]|^"[^"]*\\/
/** An anchor. Rewriting the block would drop it, and a node outside the block may use it. */
const ANCHOR_RE = /(?:^|[\s[{,])&[^\s[\]{},]/

/**
 * True when `line` leaves a quoted string or a flow collection open, so its
 * value may go on past the line. YAML lets such a value continue on a
 * column-0 line, where the block scan would stop and cut the value in two.
 *
 * The check is conservative: when it can't tell, it says the value is open,
 * and the editor leaves the block to the text. A quote starts a string only
 * where a value can start: at the start of the line, after ":", "-", "?",
 * "[", "{" or ",", or after an anchor or a tag. A "#" after whitespace,
 * outside a string, starts a comment.
 */
export function leavesValueOpen(line: string): boolean {
  let depth = 0
  let canStart = true
  for (let i = 0; i < line.length; i++) {
    const ch = line[i]
    if (ch === ' ' || ch === '\t') continue
    if (ch === '#' && (i === 0 || line[i - 1] === ' ' || line[i - 1] === '\t')) break
    if (canStart && (ch === '"' || ch === "'")) {
      let j = i + 1
      for (;;) {
        if (j >= line.length) return true
        if (ch === '"' && line[j] === '\\') {
          j += 2
          continue
        }
        if (line[j] === ch) {
          if (ch === "'" && line[j + 1] === "'") {
            j += 2
            continue
          }
          break
        }
        j++
      }
      i = j
      canStart = false
      continue
    }
    if (canStart && (ch === '&' || ch === '!')) {
      // An anchor or a tag. A value can still start after it.
      while (i + 1 < line.length && line[i + 1] !== ' ' && line[i + 1] !== '\t') i++
      continue
    }
    if ((canStart || depth > 0) && (ch === '{' || ch === '[')) {
      depth++
      canStart = true
      continue
    }
    if (depth > 0 && (ch === '}' || ch === ']')) {
      depth--
      canStart = false
      continue
    }
    canStart = ch === ':' || ch === '-' || ch === '?' || (depth > 0 && ch === ',')
  }
  return depth > 0
}

type Located =
  | { kind: 'refused'; reason: TextOnlyReason }
  | { kind: 'none'; at: number; eol: Eol }
  | { kind: 'block'; start: number; end: number; eol: Eol }

/**
 * Where the params block is in `text`.
 *
 * The block starts at a column-0 `params:` line with nothing after the colon
 * but spaces or a comment, or a flow value that starts with `{`. It ends before
 * the next column-0 line that is not blank and not a comment. Blank lines and
 * column-0 comments at its end stay outside it. A form the splice can't replace
 * safely is refused.
 */
function locateParamsBlock(text: string): Located {
  const fm = frontmatterLines(text)
  if (!fm) return { kind: 'refused', reason: 'no-frontmatter' }
  const { lines, close } = fm

  // YAML 1.1 also breaks a line at a lone CR, U+0085, U+2028 and U+2029, which
  // this scan does not, so the two could disagree on where a line starts.
  if (lines.some(l => /[\r\u0085\u2028\u2029]/.test(l.text))) return { kind: 'refused', reason: 'line-breaks' }

  let found = -1
  for (let i = 0; i < lines.length; i++) {
    if (HIDDEN_KEY_RE.test(lines[i].text)) return { kind: 'refused', reason: 'key-form' }
    if (!PARAMS_KEY_RE.test(lines[i].text)) continue
    if (found >= 0) return { kind: 'refused', reason: 'duplicate-key' }
    found = i
  }
  if (found < 0) return { kind: 'none', at: close, eol: lines.length > 0 ? lines[lines.length - 1].eol : '\n' }

  const [, key, gap, rest] = PARAMS_KEY_RE.exec(lines[found].text) as RegExpExecArray
  if (key !== 'params') return { kind: 'refused', reason: 'quoted-key' }
  if (gap !== '') return { kind: 'refused', reason: 'key-form' }
  const value = rest.replace(/^[ \t]+/, '')
  if (/^[&*!]/.test(value)) return { kind: 'refused', reason: 'anchor' }
  // A value needs a space after the colon. Then: nothing, a comment, or a flow mapping.
  if (rest !== '' && value === rest) return { kind: 'refused', reason: 'other-value' }
  if (value !== '' && !value.startsWith('#') && !value.startsWith('{')) return { kind: 'refused', reason: 'other-value' }

  let end = found + 1
  while (end < lines.length && /^([ \t]|#|$)/.test(lines[end].text)) end++
  while (end > found + 1 && /^([ \t]*$|#)/.test(lines[end - 1].text)) end--

  for (let i = found; i < end; i++) {
    const line = lines[i].text
    if (/^[ \t]*#/.test(line)) continue
    if (ANCHOR_RE.test(line)) return { kind: 'refused', reason: 'anchor' }
    // Every line must close what it opens, or the block may end on a later line.
    if (leavesValueOpen(line)) return { kind: 'refused', reason: 'multi-line' }
  }
  return { kind: 'block', start: lines[found].start, end: lines[end - 1].end, eol: lines[found].eol }
}

export type SpliceResult = { ok: true; text: string } | { ok: false; reason: TextOnlyReason }

/**
 * `text` with its params block written for `rows`. With no block, the rows go
 * in at the end of the frontmatter, before the closing `---`. With no rows, the
 * block's lines go. Every byte outside the block stays the same, and the block
 * uses the line ending of the line it replaces or follows.
 */
export function spliceParamsBlock(text: string, rows: ParamRow[]): SpliceResult {
  const at = locateParamsBlock(text)
  if (at.kind === 'refused') return { ok: false, reason: at.reason }
  if (at.kind === 'none') {
    if (rows.length === 0) return { ok: true, text }
    return { ok: true, text: text.slice(0, at.at) + writeParamsBlock(rows, at.eol) + text.slice(at.at) }
  }
  return { ok: true, text: text.slice(0, at.start) + writeParamsBlock(rows, at.eol) + text.slice(at.end) }
}

/** The text after the leading frontmatter, or all of it when there is none. */
export function bodyAfterFrontmatter(text: string): string {
  const m = FRONTMATTER_RE.exec(text)
  return m ? text.slice(m[0].length) : text
}

// Reading the params the validator returns.

const DECL_KEYS = new Set(['type', 'required', 'default', 'description', 'enum'])

/** A default or allowed value as row text, or null when a row can't hold it exactly. */
function valueText(type: ParamType, value: unknown): string | null {
  if (type === 'int') return typeof value === 'number' && Number.isSafeInteger(value) ? String(value) : null
  if (type === 'bool') return typeof value === 'boolean' ? String(value) : null
  return typeof value === 'string' ? value : null
}

/**
 * Rows for the params the validator read, in declaration order. The result is
 * not ok when a row can't hold a value exactly, because a row change would
 * then change that value. That covers an int too large for a JavaScript
 * number, and an allowed value that is empty, has spaces at an end or holds a
 * comma.
 */
export function rowsFromParams(params: unknown): { ok: true; rows: ParamRow[] } | { ok: false } {
  if (!params || typeof params !== 'object' || Array.isArray(params)) return { ok: false }
  const entries = Object.entries(params as Record<string, unknown>)
  if (entries.length > MAX_PARAM_ROWS) return { ok: false }
  const rows: ParamRow[] = []
  for (const [name, raw] of entries) {
    if (!PARAM_KEY_RE.test(name) || !raw || typeof raw !== 'object' || Array.isArray(raw)) return { ok: false }
    const decl = raw as Record<string, unknown>
    if (Object.keys(decl).some(k => !DECL_KEYS.has(k))) return { ok: false }
    const type = decl.type ?? 'string'
    if (type !== 'string' && type !== 'int' && type !== 'bool') return { ok: false }
    const required = decl.required ?? false
    const description = decl.description ?? ''
    if (typeof required !== 'boolean' || typeof description !== 'string') return { ok: false }
    const fallback = decl.default === undefined || decl.default === null ? '' : valueText(type, decl.default)
    if (fallback === null || (required && fallback !== '')) return { ok: false }
    let allowed = ''
    if (decl.enum !== undefined && decl.enum !== null) {
      if (!Array.isArray(decl.enum) || type === 'bool' || decl.enum.length > MAX_ALLOWED_VALUES) return { ok: false }
      const values = decl.enum.map(v => valueText(type, v))
      if (values.some(v => v === null || v === '' || v !== v.trim() || v.includes(','))) return { ok: false }
      allowed = values.join(', ')
    }
    rows.push({ name, type, required, default: fallback, allowed, description })
  }
  return { ok: true, rows }
}

export type ParamsReadState =
  | { kind: 'editable'; rows: ParamRow[] }
  | { kind: 'text-only'; reason: TextOnlyReason; findings: ProfileValidationMessage[] }

const isParamsPath = (path: string | null | undefined) => !!path && (path === 'params' || path.startsWith('params.'))

const textOnly = (reason: TextOnlyReason, findings: ProfileValidationMessage[] = []): ParamsReadState =>
  ({ kind: 'text-only', reason, findings })

/**
 * What the Params editor can do with `text`, given the validator's answer for
 * that same text. The rows are editable only when the splice and the server
 * agree on the block: a block the server read but the splice can't find, or
 * the other way round, stays text only.
 */
export function readParamsState(
  text: string,
  response: Pick<ProfileValidationResponse, 'messages' | 'params'>,
): ParamsReadState {
  const located = locateParamsBlock(text)
  if (located.kind === 'refused') return textOnly(located.reason)
  const errors = (response.messages ?? []).filter(m => m.severity === 'error')
  if (response.params !== null && response.params !== undefined) {
    if (located.kind === 'none') return textOnly('key-form')
    const read = rowsFromParams(response.params)
    return read.ok ? { kind: 'editable', rows: read.rows } : textOnly('values')
  }
  const paramErrors = errors.filter(m => isParamsPath(m.path))
  if (paramErrors.length > 0) return textOnly('block-errors', paramErrors)
  if (located.kind === 'none') return { kind: 'editable', rows: [] }
  if (errors.length > 0) return textOnly('frontmatter-errors', errors)
  return textOnly('key-form')
}

// Suggestions from the prompt.

/** A placeholder CAO fills from .env. */
const ENV_PLACEHOLDER_RE = /\$\{([A-Za-z_][A-Za-z0-9_]*)\}/g
/** A shell variable assignment: NAME= at the start of a line, or after whitespace or ";". */
const SHELL_ASSIGNMENT_RE = /(?:^|[\s;])([A-Za-z_][A-Za-z0-9_]*)=/g
const SECRET_RE = /KEY|TOKEN|SECRET|PASSWORD/i

export interface ParamSuggestions {
  /** Placeholder names to offer, in order of first appearance. */
  names: string[]
  /** How many distinct names were left out because they look like secrets. */
  skippedSecrets: number
}

/**
 * The `${NAME}` placeholders in `text` to offer as params. A name is left out
 * when the text assigns it as a shell variable, when it looks like a secret,
 * when its lower-case form is not a valid param name, or when a row covers it:
 * its lower-case form equals the row's name or ends with "_" and the row's
 * name, so a `region` row covers AWS_REGION.
 */
export function suggestParams(text: string, rows: ParamRow[]): ParamSuggestions {
  const assigned = new Set<string>()
  for (const m of text.matchAll(SHELL_ASSIGNMENT_RE)) assigned.add(m[1])
  const covered = rows.map(r => r.name.toLowerCase()).filter(n => n !== '')
  const seen = new Set<string>()
  const names: string[] = []
  let skippedSecrets = 0
  for (const m of text.matchAll(ENV_PLACEHOLDER_RE)) {
    const name = m[1]
    if (seen.has(name)) continue
    seen.add(name)
    if (assigned.has(name)) continue
    if (SECRET_RE.test(name)) {
      skippedSecrets++
      continue
    }
    const lower = name.toLowerCase()
    if (!PARAM_KEY_RE.test(lower)) continue
    if (covered.some(c => lower === c || lower.endsWith(`_${c}`))) continue
    if (names.length < MAX_PARAM_ROWS) names.push(name)
  }
  return { names, skippedSecrets }
}

/** The row a suggestion adds: the placeholder in lower case, a string, required. */
export function suggestedRow(name: string): ParamRow {
  return { ...newParamRow(name.toLowerCase()), required: true }
}

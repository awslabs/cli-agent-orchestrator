/**
 * Operator-macro notation: the §5.3 editing-surface grammar (TS live preview).
 *
 * This is the client-side *preview* parser. The server
 * (`services/macro_notation.py`, provisional until Lane A's contract-co-located
 * parser lands — §9) is the authority: it decides what may be saved or sent,
 * and both parsers are pinned byte-for-byte by the shared golden vectors in
 * `web/src/test/fixtures/macroNotationVectors.json`, mirroring the digest
 * golden-vector precedent. Any grammar change lands in both parsers and the
 * vectors in one change — a needed-but-absent grammar item is a spec
 * amendment, never a frontend invention.
 *
 * Grammar (pinned, §5.3):
 *
 *   sequence := event (WS+ event)*
 *   event    := text | named | chord | repeat
 *   text     := '"' JSON-string '"'      — JSON escaping exactly
 *   named    := [a-z][a-z0-9-]*          — the fourteen names in NAMED_KEYS
 *   chord    := 'ctrl+' [a-z]            — ctrl+c → key C-c; others → chord
 *   repeat   := (named|chord) '*' [1-9][0-9]*   — expansion counts toward
 *                                                 the 32-event cap
 *
 * WS is pinned to the ASCII whitespace set so the two parsers agree exactly
 * (Python's str.isspace() and JS's \s diverge on edge characters).
 */

import type { SequenceEvent } from './sequenceRecorder'
import { MAX_SEQUENCE_EVENTS, MAX_SEQUENCE_TEXT_BYTES } from './sequenceRecorder'

/** The fourteen named keys of the §5.3 grammar, notation name → wire name. */
export const NAMED_KEYS: Record<string, string> = {
  enter: 'Enter',
  escape: 'Escape',
  up: 'Up',
  down: 'Down',
  left: 'Left',
  right: 'Right',
  home: 'Home',
  end: 'End',
  'page-up': 'PageUp',
  'page-down': 'PageDown',
  delete: 'Delete',
  insert: 'Insert',
  tab: 'Tab',
  backspace: 'Backspace',
}

/** Wire name → notation name, for the canonical renderer. */
const WIRE_TO_NOTATION: Record<string, string> = Object.fromEntries(
  Object.entries(NAMED_KEYS).map(([name, wire]) => [wire, name]),
)

const WHITESPACE = new Set([' ', '\t', '\n', '\r', '\v', '\f'])

const SYMBOL_RE = /[a-z0-9+\-*]+/y
const REPEAT_COUNT_RE = /^[1-9][0-9]*$/
const CHORD_LETTER_RE = /^C-([a-z])$/
const LONE_SURROGATE_RE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/

export interface NotationErrorInfo {
  offset: number
  message: string
}

/** One parse/render failure carrying the §5.3 (offset, message) pair. */
export class NotationParseError extends Error {
  readonly offset: number

  constructor(offset: number, message: string) {
    super(message)
    this.name = 'NotationParseError'
    this.offset = offset
  }

  asInfo(): NotationErrorInfo {
    return { offset: this.offset, message: this.message }
  }
}

const encoder = new TextEncoder()

function scanJsonString(notation: string, start: number): [string, number] {
  let k = start + 1
  let closed = false
  while (k < notation.length) {
    const ch = notation[k]
    if (ch === '\\') {
      k += 2
      continue
    }
    if (ch === '"') {
      closed = true
      break
    }
    k += 1
  }
  if (!closed) {
    throw new NotationParseError(start, 'unterminated text event')
  }
  const fragment = notation.slice(start, k + 1)
  let value: unknown
  try {
    value = JSON.parse(fragment)
  } catch {
    throw new NotationParseError(start, 'invalid JSON string in text event')
  }
  if (typeof value !== 'string') {
    throw new NotationParseError(start, 'invalid JSON string in text event')
  }
  return [value, k + 1]
}

/** A repeat token as embedded in an error message, bounded in length. */
function displayToken(token: string): string {
  return token.length <= 24 ? token : `${token.slice(0, 23)}…`
}

function splitRepeat(token: string, start: number): [string, number | null] {
  const star = token.indexOf('*')
  if (star === -1) return [token, null]
  const base = token.slice(0, star)
  const countText = token.slice(star + 1)
  if (!REPEAT_COUNT_RE.test(countText)) {
    throw new NotationParseError(
      start + base.length,
      `invalid repeat count '*${countText}': expected '*' followed by a positive integer`,
    )
  }
  // A count with more than two digits is ≥ 100, which can never fit the
  // 32-event budget even in an empty sequence — fail before the numeric
  // conversion (huge digit strings lose precision/overflow to Infinity;
  // the failure keeps the ordinary offset-bearing shape). Mirrors the
  // Python authority exactly.
  if (countText.length > 2) {
    throw new NotationParseError(
      start,
      `repeat '${displayToken(token)}' expands past the ${MAX_SEQUENCE_EVENTS}-event cap`,
    )
  }
  return [base, Number(countText)]
}

function mapSymbol(base: string, start: number): SequenceEvent {
  if (base.startsWith('ctrl+')) {
    const rest = base.slice('ctrl+'.length)
    if (rest.length === 1 && rest >= 'a' && rest <= 'z') {
      if (rest === 'c') return { type: 'key', key: 'C-c' }
      return { type: 'chord', chord: `C-${rest}` }
    }
    throw new NotationParseError(
      start,
      `unrepresentable chord '${base}': only ctrl+<letter> has a pinned terminal byte encoding`,
    )
  }
  const wire = NAMED_KEYS[base]
  if (wire !== undefined) return { type: 'key', key: wire }
  if (base.includes('+')) {
    throw new NotationParseError(
      start,
      `unrepresentable event '${base}': terminal byte streams cannot express ` +
        'modifier combinations other than ctrl+<letter>',
    )
  }
  throw new NotationParseError(start, `unknown key name '${base}'`)
}

/**
 * Parse §5.3 notation into a v3 event array. Throws NotationParseError with
 * an offset and message on any failure; caps are enforced as events
 * accumulate so the failing token's offset is always known.
 */
export function parseNotation(notation: string): SequenceEvent[] {
  const events: SequenceEvent[] = []
  let textBytes = 0
  let i = 0
  const n = notation.length
  for (;;) {
    while (i < n && WHITESPACE.has(notation[i])) i += 1
    if (i >= n) break
    const start = i
    const ch = notation[i]
    if (ch === '"') {
      const [value, end] = scanJsonString(notation, i)
      i = end
      if (i < n && !WHITESPACE.has(notation[i])) {
        throw new NotationParseError(i, 'expected whitespace between events')
      }
      if (LONE_SURROGATE_RE.test(value)) {
        throw new NotationParseError(
          start,
          'text event is not UTF-8-encodable (lone surrogate); it can never become a wire event',
        )
      }
      textBytes += encoder.encode(value).length
      if (textBytes > MAX_SEQUENCE_TEXT_BYTES) {
        throw new NotationParseError(
          start,
          `text event pushes the sequence past the ${MAX_SEQUENCE_TEXT_BYTES}-byte aggregate cap`,
        )
      }
      events.push({ type: 'text', text: value })
    } else if (ch >= 'a' && ch <= 'z') {
      SYMBOL_RE.lastIndex = i
      const match = SYMBOL_RE.exec(notation)
      if (match === null) throw new Error('unreachable: event-start char is in the symbol class')
      const token = match[0]
      i = SYMBOL_RE.lastIndex
      if (i < n && !WHITESPACE.has(notation[i])) {
        throw new NotationParseError(i, 'expected whitespace between events')
      }
      const [base, count] = splitRepeat(token, start)
      const event = mapSymbol(base, start)
      if (count === null) {
        if (events.length + 1 > MAX_SEQUENCE_EVENTS) {
          throw new NotationParseError(start, `sequence holds at most ${MAX_SEQUENCE_EVENTS} events`)
        }
        events.push(event)
      } else {
        if (events.length + count > MAX_SEQUENCE_EVENTS) {
          throw new NotationParseError(
            start,
            `repeat '${displayToken(token)}' expands past the ${MAX_SEQUENCE_EVENTS}-event cap`,
          )
        }
        for (let k = 0; k < count; k += 1) events.push({ ...event })
      }
    } else {
      throw new NotationParseError(
        start,
        'expected an event (a "quoted" text, a key name, or ctrl+<letter>)',
      )
    }
  }
  if (events.length === 0) {
    throw new NotationParseError(0, 'empty notation: name at least one event')
  }
  return events
}

/** Non-throwing parse for the live editor: events, or the §5.3 errors. */
export function tryParseNotation(
  notation: string,
): { ok: true; events: SequenceEvent[] } | { ok: false; errors: NotationErrorInfo[] } {
  try {
    return { ok: true, events: parseNotation(notation) }
  } catch (error) {
    if (error instanceof NotationParseError) {
      return { ok: false, errors: [error.asInfo()] }
    }
    throw error
  }
}

function eventNotation(event: SequenceEvent): string {
  if (event.type === 'text') {
    // Canonical text form: JSON escaping exactly, non-ASCII literal.
    return JSON.stringify(event.text ?? '')
  }
  if (event.type === 'key') {
    if (event.key === 'C-c') return 'ctrl+c'
    const name = event.key !== undefined ? WIRE_TO_NOTATION[event.key] : undefined
    if (name === undefined) {
      throw new Error(`key ${JSON.stringify(event.key)} has no notation name`)
    }
    return name
  }
  if (event.type === 'chord') {
    const match = CHORD_LETTER_RE.exec(event.chord ?? '')
    // ctrl+c parses to key C-c (D7), so a chord C-c has no faithful
    // notation form — rendering one would round-trip to a different event.
    if (match === null || match[1] === 'c') {
      throw new Error(`chord ${JSON.stringify(event.chord)} has no notation form`)
    }
    return `ctrl+${match[1]}`
  }
  throw new Error(`event type ${JSON.stringify(event.type)} has no notation form`)
}

function sameNonTextEvent(a: SequenceEvent, b: SequenceEvent): boolean {
  return (
    a.type !== 'text' &&
    b.type !== 'text' &&
    a.type === b.type &&
    a.key === b.key &&
    a.chord === b.chord
  )
}

/**
 * Render the canonical notation for a validated v3 event array. Runs of two
 * or more identical non-text events fold to `name*N`; text events never
 * fold. parseNotation(renderNotation(events)) deep-equals events.
 */
export function renderNotation(events: SequenceEvent[]): string {
  const tokens: string[] = []
  let i = 0
  while (i < events.length) {
    const event = events[i]
    const token = eventNotation(event)
    if (event.type === 'text') {
      tokens.push(token)
      i += 1
      continue
    }
    let runEnd = i
    while (runEnd < events.length && sameNonTextEvent(events[runEnd], event)) runEnd += 1
    const run = runEnd - i
    tokens.push(run >= 2 ? `${token}*${run}` : token)
    i = runEnd
  }
  return tokens.join(' ')
}

/** One event's preview token: `"text"`, `[Enter]`, `[Ctrl+S]`. */
function previewTokenOf(event: SequenceEvent): string {
  if (event.type === 'text') return JSON.stringify(event.text ?? '')
  if (event.type === 'chord') {
    const chord = event.chord ?? ''
    const letter = chord.startsWith('C-') ? chord.slice(2) : chord
    return `[Ctrl+${letter.toUpperCase()}]`
  }
  if (event.key === 'C-c') return '[Ctrl+C]'
  return `[${event.key ?? ''}]`
}

/** The §5.3 normalized preview: `"text" [Enter] [Up]×3 [Ctrl+S]`. */
export function renderPreview(events: SequenceEvent[]): string {
  const tokens: string[] = []
  let i = 0
  while (i < events.length) {
    const event = events[i]
    const token = previewTokenOf(event)
    if (event.type === 'text') {
      tokens.push(token)
      i += 1
      continue
    }
    let runEnd = i
    while (runEnd < events.length && sameNonTextEvent(events[runEnd], event)) runEnd += 1
    const run = runEnd - i
    tokens.push(run >= 2 ? `${token}×${run}` : token)
    i = runEnd
  }
  return tokens.join(' ')
}

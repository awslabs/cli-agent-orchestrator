/**
 * Operator-macro notation: the §5.3 editing-surface grammar (TS live preview).
 *
 * The server (`services/macro_notation.py`, Lane A's canonical parser) is the
 * authority: it decides what may be saved or sent. This TypeScript preview is
 * its mirror — same tokenization, same messages, same offsets — pinned
 * byte-for-byte by the shared golden vectors in
 * `test/fixtures/notation_vectors.json`, mirroring the digest golden-vector
 * precedent. Any grammar change lands in both parsers and the vectors in one
 * change — a needed-but-absent grammar item is a spec amendment, never a
 * frontend invention.
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
 * Known residuals (documented, never hit by realistic editor input):
 * Python's str.isspace() and JS's \s differ on U+0085 and U+FEFF; malformed
 * \uXXXX escapes report Python's "Invalid \escape" rather than CPython's
 * \u-specific wording. The server authority is the gate for both.
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

// The sorted known-names list rendered exactly as Python's str(sorted(...)).
const KNOWN_NAMES =
  "['backspace', 'delete', 'down', 'end', 'enter', 'escape', 'home', 'insert', " +
  "'left', 'page-down', 'page-up', 'right', 'tab', 'up']"

// Modifier words a combination may be built from (§3.3): a combination of
// them has no standard-mode byte encoding, so it is named and refused.
const MODIFIER_WORDS = new Set(['ctrl', 'alt', 'meta', 'cmd', 'shift', 'super'])

const NAMED_RE = /[a-z][a-z0-9-]*/y
const COMBINATION_RE = /[a-z0-9][a-z0-9+\-]*/y
const REPEAT_RE = /[1-9][0-9]*/y

// Chars that end the modifier check after a modifier word (Python's
// ``notation[word.end()] in "+ \t\n"``).
const MODIFIER_ENDERS = new Set(['+', ' ', '\t', '\n'])

// Text bytes the control path can never send honestly (ESC, C1 CSI, CR, LF).
const ILLEGAL_TEXT_CHARS = ['\x1b', '\x9b', '\r', '\n']

const LONE_SURROGATE_RE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/

const CAP_MESSAGE =
  `this event brings the sequence past the ${MAX_SEQUENCE_EVENTS}-event cap; ` +
  'a repeat expansion counts every event it stands for'

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

function isSpace(ch: string): boolean {
  return /\s/.test(ch)
}

function multiModifierFailure(notation: string, pos: number): NotationParseError {
  COMBINATION_RE.lastIndex = pos
  const match = COMBINATION_RE.exec(notation)
  const combination = match !== null ? match[0] : notation.slice(pos)
  return new NotationParseError(
    pos,
    `multi-modifier combination '${combination}' cannot be represented: no ` +
      'standard-mode terminal byte encoding exists for it (tmux would inject the ' +
      'base key or a wrong encoding), so it is refused, never approximated',
  )
}

/**
 * Scan and decode the JSON string opening at `start`, reproducing the Python
 * authority's error taxonomy (unterminated / invalid escape / invalid
 * control character) with Python-identical offsets and messages for the
 * pinned cases.
 */
function scanJsonString(notation: string, start: number): [string, number] {
  const n = notation.length
  let k = start + 1
  let closed = false
  while (k < n) {
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
    throw new NotationParseError(start, 'unterminated string: a text token is a JSON string')
  }
  const token = notation.slice(start, k + 1)
  // Validate escape sequences and raw control characters with CPython's
  // messages (the authority's ``json`` errors, offset = the offending index
  // inside the token, reported at event_pos + index).
  for (let i = 1; i < token.length - 1; i += 1) {
    const ch = token[i]
    if (ch === '\\') {
      const next = token[i + 1]
      if (next === undefined || !'"\\/bfnrtu'.includes(next)) {
        throw new NotationParseError(
          start + i,
          'invalid JSON string: Invalid \\escape; text uses JSON escaping exactly ' +
            '(comma, plus, slash and backslash are literal inside quotes)',
        )
      }
      i += 1
      continue
    }
    if (ch < ' ') {
      throw new NotationParseError(
        start + i,
        'invalid JSON string: Invalid control character at; text uses JSON escaping ' +
          'exactly (comma, plus, slash and backslash are literal inside quotes)',
      )
    }
  }
  const decoded = JSON.parse(token) as string
  return [decoded, k + 1]
}

/**
 * Parse §5.3 notation into a v3 event array. Throws NotationParseError with
 * an offset and message on any failure; at most one error is reported — the
 * parse fails fast at the first malformed token.
 */
export function parseNotation(notation: string): SequenceEvent[] {
  const length = notation.length
  let pos = 0
  while (pos < length && isSpace(notation[pos])) pos += 1
  if (pos === length) {
    throw new NotationParseError(pos, 'a macro names at least one event')
  }

  const events: SequenceEvent[] = []
  let textBytes = 0

  while (pos < length) {
    const eventPos = pos
    let event: SequenceEvent | null = null
    let repeatable = false
    const char = notation[pos]

    if (char === '"') {
      const [decoded, end] = scanJsonString(notation, pos)
      if (decoded === '') {
        throw new NotationParseError(eventPos, 'a text event must be a non-empty string')
      }
      for (const illegal of ILLEGAL_TEXT_CHARS) {
        if (decoded.includes(illegal)) {
          throw new NotationParseError(
            eventPos,
            'the text contains a control character (ESC, C1 CSI, CR, or LF) ' +
              'the control path can never send honestly; an unrepresentable ' +
              'macro is refused, never approximated',
          )
        }
      }
      if (LONE_SURROGATE_RE.test(decoded)) {
        throw new NotationParseError(
          eventPos,
          'the text contains a lone surrogate that is not UTF-8-encodable; ' +
            'an unrepresentable macro is refused, never approximated',
        )
      }
      textBytes += encoder.encode(decoded).length
      if (textBytes > MAX_SEQUENCE_TEXT_BYTES) {
        throw new NotationParseError(
          eventPos,
          `this text pushes the sequence past the ${MAX_SEQUENCE_TEXT_BYTES}-byte ` +
            'aggregate text cap; a macro is a short control burst, not a document',
        )
      }
      event = { type: 'text', text: decoded }
      pos = end
    } else if (notation.startsWith('ctrl+', pos)) {
      const letterAt = pos + 'ctrl+'.length
      if (letterAt < length) {
        NAMED_RE.lastIndex = letterAt
        const word = NAMED_RE.exec(notation)
        if (
          word !== null &&
          MODIFIER_WORDS.has(word[0]) &&
          (NAMED_RE.lastIndex >= length || MODIFIER_ENDERS.has(notation[NAMED_RE.lastIndex]))
        ) {
          // ``ctrl+shift+x`` and friends: a modifier where the chord's
          // single letter must be.
          throw multiModifierFailure(notation, pos)
        }
      }
      if (letterAt >= length || !(notation[letterAt] >= 'a' && notation[letterAt] <= 'z')) {
        throw new NotationParseError(
          letterAt,
          "a chord is 'ctrl+' followed by one letter a-z; multi-modifier and " +
            'non-letter chords are unrepresentable and refused, never approximated',
        )
      }
      const letter = notation[letterAt]
      event = letter === 'c' ? { type: 'key', key: 'C-c' } : { type: 'chord', chord: `C-${letter}` }
      repeatable = true
      pos = letterAt + 1
    } else {
      NAMED_RE.lastIndex = pos
      const match = NAMED_RE.exec(notation)
      if (match === null) {
        throw new NotationParseError(
          pos,
          `expected a quoted text, a named key, or a ctrl+<letter> chord, ` +
            `not '${char}'; known key names are ${KNOWN_NAMES}`,
        )
      }
      const name = match[0]
      if (MODIFIER_WORDS.has(name) && NAMED_RE.lastIndex < length && notation[NAMED_RE.lastIndex] === '+') {
        // ``alt+x``, ``meta+x`` and friends at event position.
        throw multiModifierFailure(notation, pos)
      }
      const wire = NAMED_KEYS[name]
      if (wire === undefined) {
        throw new NotationParseError(
          pos,
          `unknown named key '${name}'; known key names are ${KNOWN_NAMES} — ` +
            'unlisted keys (BTab, modified arrows, F-keys) are refused, never approximated',
        )
      }
      event = { type: 'key', key: wire }
      repeatable = true
      pos = NAMED_RE.lastIndex
    }

    let count = 1
    if (repeatable && pos < length && notation[pos] === '*') {
      const repeatPos = pos
      REPEAT_RE.lastIndex = pos + 1
      const digits = REPEAT_RE.exec(notation)
      if (digits === null) {
        throw new NotationParseError(
          repeatPos + 1,
          'a repeat count is a positive integer written [1-9][0-9]* ' +
            '(zero and empty counts are malformed, not no-ops)',
        )
      }
      if (digits[0].length > 2) {
        // A count of 100+ can never fit the 32-event budget, even in an
        // empty sequence — fail before the numeric conversion (huge digit
        // strings lose precision/overflow to Infinity). r11: the failure
        // keeps the ordinary offset-bearing shape; the message embeds no
        // token, so it is bounded by construction.
        throw new NotationParseError(eventPos, CAP_MESSAGE)
      }
      count = Number(digits[0])
      pos = REPEAT_RE.lastIndex
    }

    if (events.length + count > MAX_SEQUENCE_EVENTS) {
      throw new NotationParseError(eventPos, CAP_MESSAGE)
    }
    for (let k = 0; k < count; k += 1) events.push({ ...(event as SequenceEvent) })

    if (pos < length) {
      if (!isSpace(notation[pos])) {
        throw new NotationParseError(pos, `expected whitespace between events, not '${notation[pos]}'`)
      }
      while (pos < length && isSpace(notation[pos])) pos += 1
    }
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
    const match = /^C-([a-z])$/.exec(event.chord ?? '')
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
 * Render the canonical notation for a validated v3 event array (client-side:
 * the §7.4 recorder keeps its tokens and notation in sync through this and
 * the pinned parser; the server never renders notation). Runs of two or more
 * identical non-text events fold to `name*N`; text events never fold.
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

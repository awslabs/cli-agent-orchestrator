/**
 * The v3 structured key-sequence recorder (cond-0175).
 *
 * Pure recording logic for the dashboard's record/send/cancel control:
 * which browser key events become which wire events, the caps, and the
 * readable preview. Kept DOM-free so the rules are testable without
 * rendering the terminal view.
 *
 * The honesty rules, exactly as the wire contract states them:
 *
 * - Only the exact representable events are recorded: Escape, C-c, the
 *   provider-pinned C-s steer chord, Enter, Backspace, and printable
 *   text. Comma, plus, and backslash are ordinary printable text inside
 *   text events — no escaping exists here, and none is needed.
 * - Anything else — modifier combinations the terminal cannot express —
 *   is refused with a message, never approximated into a key that was
 *   not pressed. Terminal protocols cannot express arbitrary
 *   simultaneous physical-key combinations, and pretending otherwise
 *   would deliver a different control than the operator recorded.
 * - The caps are the server's: at most 32 events and at most 512 UTF-8
 *   bytes of text across the sequence. Exceeding either refuses the
 *   recording, client-side, before anything is sent.
 */

export interface SequenceEvent {
  type: 'text' | 'key' | 'chord'
  text?: string
  key?: string
  chord?: string
}

export const MAX_SEQUENCE_EVENTS = 32
export const MAX_SEQUENCE_TEXT_BYTES = 512

/** The key names the wire contract normalizes, by preview label. */
const KEY_EVENTS: Record<string, { key: string; label: string }> = {
  Escape: { key: 'Escape', label: 'Escape' },
  Enter: { key: 'Enter', label: 'Enter' },
  Backspace: { key: 'Backspace', label: 'Backspace' },
}

export interface RecordResult {
  events: SequenceEvent[]
  /** Set when the key was refused; the recording is unchanged. */
  refused?: string
}

export interface KeyLike {
  key: string
  ctrlKey: boolean
  metaKey: boolean
  altKey: boolean
}

function utf8Bytes(text: string): number {
  return new TextEncoder().encode(text).length
}

export function sequenceTextBytes(events: SequenceEvent[]): number {
  return events.reduce(
    (total, event) => total + (event.type === 'text' && event.text ? utf8Bytes(event.text) : 0),
    0,
  )
}

function appendText(events: SequenceEvent[], char: string): SequenceEvent[] {
  const last = events[events.length - 1]
  if (last && last.type === 'text') {
    return [...events.slice(0, -1), { type: 'text', text: (last.text ?? '') + char }]
  }
  return [...events, { type: 'text', text: char }]
}

function capCheck(events: SequenceEvent[]): string | undefined {
  if (events.length > MAX_SEQUENCE_EVENTS) {
    return `a sequence holds at most ${MAX_SEQUENCE_EVENTS} events`
  }
  const bytes = sequenceTextBytes(events)
  if (bytes > MAX_SEQUENCE_TEXT_BYTES) {
    return `a sequence carries at most ${MAX_SEQUENCE_TEXT_BYTES} bytes of text`
  }
  return undefined
}

/**
 * Fold one browser key event into the recording. Returns the next
 * recording, or an unchanged recording plus a refusal message.
 */
export function applyKeyToRecording(events: SequenceEvent[], event: KeyLike): RecordResult {
  const { key, ctrlKey, metaKey, altKey } = event

  // The named control keys, unmodified only: Escape/Enter/Backspace with
  // a modifier held is a different physical combination, which the
  // terminal cannot express — so it is refused below, not approximated.
  if (!ctrlKey && !metaKey && !altKey && key in KEY_EVENTS) {
    const next = [...events, { type: 'key' as const, key: KEY_EVENTS[key].key }]
    const over = capCheck(next)
    return over ? { events, refused: over } : { events: next }
  }

  // The two representable Ctrl chords. Both are ordinary single-modifier
  // chords; Ctrl+S travels as the provider-pinned steer chord, which the
  // server validates against the provider's table before any write.
  if (ctrlKey && !metaKey && !altKey && (key === 'c' || key === 'C')) {
    const next = [...events, { type: 'key' as const, key: 'C-c' }]
    const over = capCheck(next)
    return over ? { events, refused: over } : { events: next }
  }
  if (ctrlKey && !metaKey && !altKey && (key === 's' || key === 'S')) {
    const next = [...events, { type: 'chord' as const, chord: 'C-s' }]
    const over = capCheck(next)
    return over ? { events, refused: over } : { events: next }
  }

  // Ordinary printable text — including comma, plus, and backslash,
  // which are text, never escapes. Merges into the trailing text event
  // so consecutive typing is one event, exactly as it will be typed.
  if (!ctrlKey && !metaKey && !altKey && key.length === 1) {
    const next = appendText(events, key)
    const over = capCheck(next)
    return over ? { events, refused: over } : { events: next }
  }

  // Everything else is a combination this surface cannot represent
  // honestly. Named and refused — never silently dropped, never
  // approximated into something else.
  const modifiers = [ctrlKey && 'Ctrl', metaKey && 'Meta', altKey && 'Alt']
    .filter(Boolean)
    .join('+')
  const combo = modifiers ? `${modifiers}+${key}` : key
  return {
    events,
    refused:
      `${combo} cannot be represented: terminal protocols cannot express arbitrary ` +
      'simultaneous physical-key combinations, and an unrepresentable combination is ' +
      'refused rather than approximated',
  }
}

/** One event's readable preview token: [Escape], [Ctrl+S], or the text. */
export function previewToken(event: SequenceEvent): string {
  if (event.type === 'text') return `"${event.text ?? ''}"`
  if (event.type === 'chord') return `[Ctrl+S]`
  if (event.key === 'C-c') return `[Ctrl+C]`
  return `[${event.key}]`
}

/** The whole recording as one readable preview line. */
export function previewSequence(events: SequenceEvent[]): string {
  return events.map(previewToken).join(' ')
}

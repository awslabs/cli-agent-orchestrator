/**
 * The v3 structured key-sequence recorder (cond-0175, extended in Lane B).
 *
 * Pure recording logic for the dashboard's capture surfaces (streaming §6.2,
 * macro recorder §7.4): which browser key events become which wire events,
 * the caps, and the readable preview. Kept DOM-free so the rules are
 * testable without rendering the terminal view.
 *
 * The honesty rules, exactly as the wire contract states them:
 *
 * - Only the exact representable events are recorded: the §3.2 key set
 *   (Escape, Enter, Backspace, the eleven navigation/editing keys), the
 *   provider-agnostic C-c interrupt, ctrl chords from the *per-terminal
 *   advertised set only* (§6.2, D9), and printable text. Comma, plus, and
 *   backslash are ordinary printable text inside text events — no escaping
 *   exists here, and none is needed.
 * - Anything else — modifier combinations the terminal cannot express,
 *   chords the server did not advertise for this terminal's provider+build —
 *   is refused with a message, never approximated into a key that was not
 *   pressed. Unadvertised chords are refused locally with zero requests
 *   sent: capability discovery never happens by provoking a server refusal.
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
  // The §3.2 navigation/editing set: KeyboardEvent.key values map to the
  // wire names one-to-one (UI Events standard names on the left).
  ArrowUp: { key: 'Up', label: 'Up' },
  ArrowDown: { key: 'Down', label: 'Down' },
  ArrowLeft: { key: 'Left', label: 'Left' },
  ArrowRight: { key: 'Right', label: 'Right' },
  Home: { key: 'Home', label: 'Home' },
  End: { key: 'End', label: 'End' },
  PageUp: { key: 'PageUp', label: 'PageUp' },
  PageDown: { key: 'PageDown', label: 'PageDown' },
  Delete: { key: 'Delete', label: 'Delete' },
  Insert: { key: 'Insert', label: 'Insert' },
  Tab: { key: 'Tab', label: 'Tab' },
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
  shiftKey?: boolean
}

/** Display form for one ctrl chord name: C-s → Ctrl+S. */
function chordLabel(chord: string): string {
  const letter = chord.startsWith('C-') ? chord.slice(2) : chord
  return `Ctrl+${letter.toUpperCase()}`
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

/** Append one event, merging into a trailing text event when possible. */
export function appendEvent(events: SequenceEvent[], event: SequenceEvent): SequenceEvent[] {
  if (event.type === 'text') return appendText(events, event.text ?? '')
  return [...events, event]
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
 * Map one browser key event to its wire event, or refuse it with a message.
 *
 * This is the single capture-mapping authority for both the macro recorder
 * and the streaming engine (§6.2); batching and caps live in the callers.
 *
 * `advertisedChords` is the per-terminal advertised chord set held at
 * arm/compose time (§3.5, §6.2): a ctrl chord becomes a chord event only
 * when that exact chord is advertised for this terminal's provider+build.
 * Anything absent is **refused locally, with zero requests sent** — the
 * client never discovers capabilities by provoking a server refusal (D9).
 * The deployed recorder's unconditional `C-s` shape does not survive
 * (§10.4): even `Ctrl+S` gates on the advertised set.
 */
export function mapKeyToEvent(
  event: KeyLike,
  advertisedChords: ReadonlySet<string>,
): { event?: SequenceEvent; refused?: string } {
  const { key, ctrlKey, metaKey, altKey } = event
  const shiftKey = event.shiftKey ?? false

  // The named control keys, unmodified only: a named key with a modifier
  // held is a different physical combination, which the terminal cannot
  // express — so it is refused below, not approximated. (Shift+Tab in
  // particular is BTab, which no managed provider has evidence of
  // consuming; it stays refused, §3.2.)
  if (!ctrlKey && !metaKey && !altKey && !shiftKey && key in KEY_EVENTS) {
    return { event: { type: 'key', key: KEY_EVENTS[key].key } }
  }

  // The provider-agnostic interrupt: Ctrl+C travels as the key C-c.
  if (ctrlKey && !metaKey && !altKey && !shiftKey && (key === 'c' || key === 'C')) {
    return { event: { type: 'key', key: 'C-c' } }
  }

  // Case-distinct chords (Ctrl+Shift+C vs Ctrl+C) are distinguishable in
  // the DOM but identical in the byte stream; the capture layer must not
  // claim the distinction — refused, never folded (§3.3).
  if (ctrlKey && shiftKey && !metaKey && !altKey && key.length === 1 && /[a-z]/i.test(key)) {
    return {
      refused:
        `Ctrl+Shift+${key.toUpperCase()} cannot be represented: it is indistinguishable ` +
        `from Ctrl+${key.toUpperCase()} in the terminal byte stream, and the distinction ` +
        'is refused rather than folded',
    }
  }

  // Every other Ctrl+letter: a chord event only when the exact chord is in
  // the per-terminal advertised set; otherwise refused locally, zero POSTs.
  if (ctrlKey && !metaKey && !altKey && !shiftKey && key.length === 1 && /[a-z]/i.test(key)) {
    const chord = `C-${key.toLowerCase()}`
    if (advertisedChords.has(chord)) {
      return { event: { type: 'chord', chord } }
    }
    return {
      refused:
        `${chordLabel(chord)} is not admitted for this terminal's provider and build ` +
        '(the server did not advertise it); refused locally — no request was sent',
    }
  }

  // Ordinary printable text — including comma, plus, and backslash,
  // which are text, never escapes. Merges into the trailing text event
  // so consecutive typing is one event, exactly as it will be typed.
  if (!ctrlKey && !metaKey && !altKey && key.length === 1) {
    return { event: { type: 'text', text: key } }
  }

  // Everything else is a combination this surface cannot represent
  // honestly. Named and refused — never silently dropped, never
  // approximated into something else.
  const modifiers = [ctrlKey && 'Ctrl', metaKey && 'Meta', altKey && 'Alt', shiftKey && 'Shift']
    .filter(Boolean)
    .join('+')
  const combo = modifiers ? `${modifiers}+${key}` : key
  return {
    refused:
      `${combo} cannot be represented: terminal protocols cannot express arbitrary ` +
      'simultaneous physical-key combinations, and an unrepresentable combination is ' +
      'refused rather than approximated',
  }
}

/**
 * Fold one browser key event into the recording. Returns the next
 * recording, or an unchanged recording plus a refusal message.
 */
export function applyKeyToRecording(
  events: SequenceEvent[],
  event: KeyLike,
  advertisedChords: ReadonlySet<string>,
): RecordResult {
  const mapped = mapKeyToEvent(event, advertisedChords)
  if (mapped.refused || !mapped.event) return { events, refused: mapped.refused }
  const next = appendEvent(events, mapped.event)
  const over = capCheck(next)
  return over ? { events, refused: over } : { events: next }
}

/** One event's readable preview token: [Escape], [Ctrl+S], or the text. */
export function previewToken(event: SequenceEvent): string {
  if (event.type === 'text') return `"${event.text ?? ''}"`
  if (event.type === 'chord') return `[${chordLabel(event.chord ?? '')}]`
  if (event.key === 'C-c') return `[Ctrl+C]`
  return `[${event.key}]`
}

/** The whole recording as one readable preview line. */
export function previewSequence(events: SequenceEvent[]): string {
  return events.map(previewToken).join(' ')
}

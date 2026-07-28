import { describe, it, expect } from 'vitest'
import {
  applyKeyToRecording,
  previewSequence,
  previewToken,
  sequenceTextBytes,
  MAX_SEQUENCE_EVENTS,
  MAX_SEQUENCE_TEXT_BYTES,
  type SequenceEvent,
} from '../lib/sequenceRecorder'

const plain = (key: string) => ({ key, ctrlKey: false, metaKey: false, altKey: false })
const ctrl = (key: string) => ({ key, ctrlKey: true, metaKey: false, altKey: false })
const ctrlShift = (key: string) => ({ key, ctrlKey: true, metaKey: false, altKey: false, shiftKey: true })

// The per-terminal advertised chord set (§3.5) the capture surface holds at
// arm/compose time. Kimi's pinned build advertises the C-s steer chord;
// Claude's advertises none.
const KIMI_CHORDS: ReadonlySet<string> = new Set(['C-s'])
const NO_CHORDS: ReadonlySet<string> = new Set()

describe('sequenceRecorder key mapping', () => {
  it('records the exact representable keys', () => {
    let events: SequenceEvent[] = []
    for (const press of [plain('Escape'), ctrl('c'), ctrl('s'), plain('Enter'), plain('Backspace')]) {
      const result = applyKeyToRecording(events, press, KIMI_CHORDS)
      expect(result.refused).toBeUndefined()
      events = result.events
    }
    expect(events).toEqual([
      { type: 'key', key: 'Escape' },
      { type: 'key', key: 'C-c' },
      { type: 'chord', chord: 'C-s' },
      { type: 'key', key: 'Enter' },
      { type: 'key', key: 'Backspace' },
    ])
  })

  it('records the §3.2 navigation/editing set one-to-one', () => {
    const mapping: Array<[string, string]> = [
      ['ArrowUp', 'Up'],
      ['ArrowDown', 'Down'],
      ['ArrowLeft', 'Left'],
      ['ArrowRight', 'Right'],
      ['Home', 'Home'],
      ['End', 'End'],
      ['PageUp', 'PageUp'],
      ['PageDown', 'PageDown'],
      ['Delete', 'Delete'],
      ['Insert', 'Insert'],
      ['Tab', 'Tab'],
    ]
    for (const [domKey, wireKey] of mapping) {
      const result = applyKeyToRecording([], plain(domKey), KIMI_CHORDS)
      expect(result.refused).toBeUndefined()
      expect(result.events).toEqual([{ type: 'key', key: wireKey }])
    }
  })

  it('records comma, plus, and backslash as ordinary text, unescaped', () => {
    let events: SequenceEvent[] = []
    for (const char of [',', '+', '\\']) {
      const result = applyKeyToRecording(events, plain(char), KIMI_CHORDS)
      expect(result.refused).toBeUndefined()
      events = result.events
    }
    expect(events).toEqual([{ type: 'text', text: ',+\\' }])
  })

  it('merges consecutive printable input into one text event', () => {
    let events: SequenceEvent[] = []
    for (const char of 'hello') events = applyKeyToRecording(events, plain(char), KIMI_CHORDS).events
    expect(events).toEqual([{ type: 'text', text: 'hello' }])
    // A key breaks the run; the next character starts a new text event.
    events = applyKeyToRecording(events, plain('Escape'), KIMI_CHORDS).events
    events = applyKeyToRecording(events, plain('x'), KIMI_CHORDS).events
    expect(events).toEqual([
      { type: 'text', text: 'hello' },
      { type: 'key', key: 'Escape' },
      { type: 'text', text: 'x' },
    ])
  })

  it('keeps ordering across heterogeneous events', () => {
    let events: SequenceEvent[] = []
    events = applyKeyToRecording(events, plain('a'), KIMI_CHORDS).events
    events = applyKeyToRecording(events, plain('Enter'), KIMI_CHORDS).events
    events = applyKeyToRecording(events, plain('Escape'), KIMI_CHORDS).events
    expect(events.map((event) => event.type)).toEqual(['text', 'key', 'key'])
    expect(events[1]).toEqual({ type: 'key', key: 'Enter' })
    expect(events[2]).toEqual({ type: 'key', key: 'Escape' })
  })

  it('refuses unrepresentable modifier combinations with a message', () => {
    for (const press of [
      { key: 'x', ctrlKey: true, metaKey: false, altKey: true },
      { key: 'Tab', ctrlKey: false, metaKey: true, altKey: false },
      { key: 'Tab', ctrlKey: false, metaKey: false, altKey: false, shiftKey: true },
      { key: 'F5', ctrlKey: false, metaKey: false, altKey: false },
      { key: 'Escape', ctrlKey: true, metaKey: false, altKey: false },
      { key: 'Enter', ctrlKey: false, metaKey: false, altKey: true },
    ]) {
      const before: SequenceEvent[] = [{ type: 'text', text: 'keep' }]
      const result = applyKeyToRecording(before, press, KIMI_CHORDS)
      expect(result.refused).toMatch(/cannot be represented/)
      expect(result.events).toBe(before) // unchanged, never approximated
    }
  })

  // Sol P1-2 acceptance (§10.4): chord admission is the per-terminal
  // advertised set held at arm, never an unconditional C-s shape.
  it('refuses Ctrl+S locally with zero requests when the set is empty (Claude)', () => {
    const result = applyKeyToRecording([], ctrl('s'), NO_CHORDS)
    expect(result.refused).toMatch(/not admitted for this terminal's provider and build/)
    expect(result.events).toEqual([])
  })

  it('refuses Ctrl+S locally when the build is unpinned (no advertised chords)', () => {
    const result = applyKeyToRecording([], ctrl('s'), NO_CHORDS)
    expect(result.refused).toBeDefined()
    expect(result.events).toEqual([])
  })

  it('refuses any arbitrary Ctrl+letter absent from the advertised set', () => {
    for (const letter of ['x', 'q', 'z']) {
      const result = applyKeyToRecording([], ctrl(letter), KIMI_CHORDS)
      expect(result.refused).toMatch(/not admitted/)
      expect(result.events).toEqual([])
    }
  })

  it('admits exactly the advertised chords, never neighbors', () => {
    const result = applyKeyToRecording([], ctrl('s'), KIMI_CHORDS)
    expect(result.events).toEqual([{ type: 'chord', chord: 'C-s' }])
    const neighbor = applyKeyToRecording([], ctrl('r'), KIMI_CHORDS)
    expect(neighbor.refused).toBeDefined()
  })

  it('refuses case-distinct chords rather than folding (§3.3)', () => {
    const result = applyKeyToRecording([], ctrlShift('C'), KIMI_CHORDS)
    expect(result.refused).toMatch(/indistinguishable/)
    expect(result.events).toEqual([])
  })

  it('Ctrl+C remains the provider-agnostic interrupt on every terminal', () => {
    const result = applyKeyToRecording([], ctrl('c'), NO_CHORDS)
    expect(result.refused).toBeUndefined()
    expect(result.events).toEqual([{ type: 'key', key: 'C-c' }])
  })

  it('refuses caps overflow without recording', () => {
    let events: SequenceEvent[] = []
    for (let i = 0; i < MAX_SEQUENCE_EVENTS; i++) {
      events = applyKeyToRecording(events, plain('Escape'), KIMI_CHORDS).events
    }
    expect(events).toHaveLength(MAX_SEQUENCE_EVENTS)
    const over = applyKeyToRecording(events, plain('Escape'), KIMI_CHORDS)
    expect(over.refused).toMatch(/at most 32 events/)
    expect(over.events).toBe(events)

    const textEvents: SequenceEvent[] = [{ type: 'text', text: 'a'.repeat(MAX_SEQUENCE_TEXT_BYTES) }]
    const overText = applyKeyToRecording(textEvents, plain('b'), KIMI_CHORDS)
    expect(overText.refused).toMatch(/at most 512 bytes/)
    expect(overText.events).toBe(textEvents)
  })

  it('measures text in UTF-8 bytes', () => {
    const events: SequenceEvent[] = [{ type: 'text', text: 'éé' }] // 4 bytes
    expect(sequenceTextBytes(events)).toBe(4)
  })
})

describe('sequenceRecorder preview', () => {
  it('renders readable tokens', () => {
    const events: SequenceEvent[] = [
      { type: 'chord', chord: 'C-s' },
      { type: 'key', key: 'Escape' },
      { type: 'key', key: 'C-c' },
      { type: 'key', key: 'Enter' },
      { type: 'key', key: 'Backspace' },
      { type: 'key', key: 'Up' },
      { type: 'text', text: 'a, b + c\\d' },
    ]
    expect(events.map(previewToken)).toEqual([
      '[Ctrl+S]',
      '[Escape]',
      '[Ctrl+C]',
      '[Enter]',
      '[Backspace]',
      '[Up]',
      '"a, b + c\\d"',
    ])
    expect(previewSequence(events)).toBe(
      '[Ctrl+S] [Escape] [Ctrl+C] [Enter] [Backspace] [Up] "a, b + c\\d"',
    )
  })
})

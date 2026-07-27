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

describe('sequenceRecorder key mapping', () => {
  it('records the exact representable keys', () => {
    let events: SequenceEvent[] = []
    for (const press of [plain('Escape'), ctrl('c'), ctrl('s'), plain('Enter'), plain('Backspace')]) {
      const result = applyKeyToRecording(events, press)
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

  it('records comma, plus, and backslash as ordinary text, unescaped', () => {
    let events: SequenceEvent[] = []
    for (const char of [',', '+', '\\']) {
      const result = applyKeyToRecording(events, plain(char))
      expect(result.refused).toBeUndefined()
      events = result.events
    }
    expect(events).toEqual([{ type: 'text', text: ',+\\' }])
  })

  it('merges consecutive printable input into one text event', () => {
    let events: SequenceEvent[] = []
    for (const char of 'hello') events = applyKeyToRecording(events, plain(char)).events
    expect(events).toEqual([{ type: 'text', text: 'hello' }])
    // A key breaks the run; the next character starts a new text event.
    events = applyKeyToRecording(events, plain('Escape')).events
    events = applyKeyToRecording(events, plain('x')).events
    expect(events).toEqual([
      { type: 'text', text: 'hello' },
      { type: 'key', key: 'Escape' },
      { type: 'text', text: 'x' },
    ])
  })

  it('keeps ordering across heterogeneous events', () => {
    let events: SequenceEvent[] = []
    events = applyKeyToRecording(events, plain('a')).events
    events = applyKeyToRecording(events, plain('Enter')).events
    events = applyKeyToRecording(events, plain('Escape')).events
    expect(events.map((event) => event.type)).toEqual(['text', 'key', 'key'])
    expect(events[1]).toEqual({ type: 'key', key: 'Enter' })
    expect(events[2]).toEqual({ type: 'key', key: 'Escape' })
  })

  it('refuses unrepresentable modifier combinations with a message', () => {
    for (const press of [
      { key: 'x', ctrlKey: true, metaKey: false, altKey: true },
      { key: 'Tab', ctrlKey: false, metaKey: true, altKey: false },
      { key: 'Tab', ctrlKey: false, metaKey: false, altKey: false },
      { key: 'F5', ctrlKey: false, metaKey: false, altKey: false },
      { key: 'Escape', ctrlKey: true, metaKey: false, altKey: false },
      { key: 'Enter', ctrlKey: false, metaKey: false, altKey: true },
    ]) {
      const before: SequenceEvent[] = [{ type: 'text', text: 'keep' }]
      const result = applyKeyToRecording(before, press)
      expect(result.refused).toMatch(/cannot be represented/)
      expect(result.events).toBe(before) // unchanged, never approximated
    }
  })

  it('refuses caps overflow without recording', () => {
    let events: SequenceEvent[] = []
    for (let i = 0; i < MAX_SEQUENCE_EVENTS; i++) {
      events = applyKeyToRecording(events, plain('Escape')).events
    }
    expect(events).toHaveLength(MAX_SEQUENCE_EVENTS)
    const over = applyKeyToRecording(events, plain('Escape'))
    expect(over.refused).toMatch(/at most 32 events/)
    expect(over.events).toBe(events)

    let textEvents: SequenceEvent[] = [{ type: 'text', text: 'a'.repeat(MAX_SEQUENCE_TEXT_BYTES) }]
    const overText = applyKeyToRecording(textEvents, plain('b'))
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
      { type: 'text', text: 'a, b + c\\d' },
    ]
    expect(events.map(previewToken)).toEqual([
      '[Ctrl+S]',
      '[Escape]',
      '[Ctrl+C]',
      '[Enter]',
      '[Backspace]',
      '"a, b + c\\d"',
    ])
    expect(previewSequence(events)).toBe(
      '[Ctrl+S] [Escape] [Ctrl+C] [Enter] [Backspace] "a, b + c\\d"',
    )
  })
})

import { describe, it, expect } from 'vitest'
import vectors from '../../../test/fixtures/notation_vectors.json'
import {
  NotationParseError,
  parseNotation,
  renderNotation,
  renderPreview,
  tryParseNotation,
} from '../lib/macroNotation'
import type { SequenceEvent } from '../lib/sequenceRecorder'

interface ValidVector {
  notation: string
  events: SequenceEvent[]
  preview: string
}

interface InvalidVector {
  notation: string
  errors: Array<{ offset: number; message: string }>
}

// The shared golden vectors pin this preview parser and the canonical
// Python authority (services/macro_notation.py) to identical behavior —
// same events, same preview, same (offset, message). Mirrors the digest
// golden-vector precedent: the two sides cannot drift into spelling the
// same macro two ways.
const validVectors = vectors.valid as ValidVector[]
const invalidVectors = vectors.invalid as InvalidVector[]

describe('macroNotation golden vectors', () => {
  it('parses every valid vector to the pinned events and preview', () => {
    for (const vector of validVectors) {
      const events = parseNotation(vector.notation)
      expect(events, vector.notation).toEqual(vector.events)
      expect(renderPreview(events), vector.notation).toBe(vector.preview)
    }
  })

  it('rejects every invalid vector with the pinned offset and message', () => {
    for (const vector of invalidVectors) {
      let caught: unknown
      try {
        parseNotation(vector.notation)
      } catch (error) {
        caught = error
      }
      expect(caught, vector.notation).toBeInstanceOf(NotationParseError)
      const failure = caught as NotationParseError
      expect(
        [{ offset: failure.offset, message: failure.message }],
        vector.notation,
      ).toEqual(vector.errors)
    }
  })

  it('tryParseNotation reports the same errors without throwing', () => {
    for (const vector of invalidVectors) {
      const result = tryParseNotation(vector.notation)
      expect(result.ok, vector.notation).toBe(false)
      if (!result.ok) {
        expect(result.errors, vector.notation).toEqual(vector.errors)
      }
    }
    for (const vector of validVectors) {
      expect(tryParseNotation(vector.notation).ok, vector.notation).toBe(true)
    }
  })
})

describe('macroNotation round-trip (client-side recorder sync, §7.4)', () => {
  it('renderNotation re-parses to the same events for every valid vector', () => {
    for (const vector of validVectors) {
      const notation = renderNotation(vector.events)
      expect(parseNotation(notation), `${vector.notation} → ${notation}`).toEqual(vector.events)
    }
  })

  it('refuses forms the notation cannot represent', () => {
    // ctrl+s parses to a chord; a wire *key* C-s has no notation name.
    expect(() => renderNotation([{ type: 'key', key: 'C-s' }])).toThrow(/no notation name/)
    // ctrl+c parses to key C-c, so a chord C-c would not round-trip.
    expect(() => renderNotation([{ type: 'chord', chord: 'C-c' }])).toThrow(/no notation form/)
    expect(() => renderNotation([{ type: 'chord', chord: 'C-Up' }])).toThrow(/no notation form/)
    expect(() => renderNotation([{ type: 'key', key: 'F1' }])).toThrow(/no notation name/)
  })

  it('the r11 guard: absurd repeat counts fail before conversion, bounded message', () => {
    const result = tryParseNotation(`up*${'9'.repeat(5000)}`)
    expect(result.ok).toBe(false)
    if (!result.ok) {
      expect(result.errors[0].offset).toBe(0)
      expect(result.errors[0].message).toBe(
        'this event brings the sequence past the 32-event cap; a repeat ' +
          'expansion counts every event it stands for',
      )
      expect(result.errors[0].message.length).toBeLessThan(120)
    }
  })
})

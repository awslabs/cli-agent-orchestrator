import { describe, it, expect } from 'vitest'
import vectors from './fixtures/macroNotationVectors.json'
import {
  NotationParseError,
  parseNotation,
  renderNotation,
  renderPreview,
  tryParseNotation,
} from '../lib/macroNotation'
import type { SequenceEvent } from '../lib/sequenceRecorder'

interface OkVector {
  name: string
  notation: string
  events: SequenceEvent[]
  canonical: string
  preview: string
}

interface ErrorVector {
  name: string
  notation: string
  offset: number
  message: string
}

// The shared golden vectors pin this preview parser and the Python
// authority parser (services/macro_notation.py) to identical behavior —
// same events, same canonical form, same preview, same (offset, message).
const okVectors = vectors.ok as OkVector[]
const errorVectors = vectors.errors as ErrorVector[]

describe('macroNotation golden vectors', () => {
  it('parses every ok vector to the pinned events', () => {
    for (const vector of okVectors) {
      expect(parseNotation(vector.notation), vector.name).toEqual(vector.events)
    }
  })

  it('renders the pinned canonical notation and preview', () => {
    for (const vector of okVectors) {
      expect(renderNotation(vector.events), vector.name).toBe(vector.canonical)
      expect(renderPreview(vector.events), vector.name).toBe(vector.preview)
    }
  })

  it('round-trips canonical notation back to the pinned events', () => {
    for (const vector of okVectors) {
      expect(parseNotation(vector.canonical), vector.name).toEqual(vector.events)
    }
  })

  it('rejects every error vector with the pinned offset and message', () => {
    for (const vector of errorVectors) {
      let caught: unknown
      try {
        parseNotation(vector.notation)
      } catch (error) {
        caught = error
      }
      expect(caught, vector.name).toBeInstanceOf(NotationParseError)
      const failure = caught as NotationParseError
      expect(failure.offset, vector.name).toBe(vector.offset)
      expect(failure.message, vector.name).toBe(vector.message)
    }
  })

  it('tryParseNotation reports the same errors without throwing', () => {
    for (const vector of errorVectors) {
      const result = tryParseNotation(vector.notation)
      expect(result.ok, vector.name).toBe(false)
      if (!result.ok) {
        expect(result.errors, vector.name).toEqual([{ offset: vector.offset, message: vector.message }])
      }
    }
    for (const vector of okVectors) {
      const result = tryParseNotation(vector.notation)
      expect(result.ok, vector.name).toBe(true)
    }
  })
})

describe('macroNotation renderer refusals', () => {
  it('refuses forms the notation cannot represent', () => {
    // ctrl+s parses to a chord; a wire *key* C-s has no notation name.
    expect(() => renderNotation([{ type: 'key', key: 'C-s' }])).toThrow(/no notation name/)
    // ctrl+c parses to key C-c, so a chord C-c would not round-trip.
    expect(() => renderNotation([{ type: 'chord', chord: 'C-c' }])).toThrow(/no notation form/)
    expect(() => renderNotation([{ type: 'chord', chord: 'C-Up' }])).toThrow(/no notation form/)
    expect(() => renderNotation([{ type: 'key', key: 'F1' }])).toThrow(/no notation name/)
  })
})

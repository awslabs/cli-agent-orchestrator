import { describe, it, expect, vi } from 'vitest'
import { parseSseFrame, dispatchFrame } from '../components/workflow/useEventFollow'
import type { WorkflowEvent, GapMarker } from '../api'

describe('parseSseFrame', () => {
  it('parses an event frame with event/data/id fields', () => {
    const block = 'event: step.completed\ndata: {"seq":5,"event_type":"step.completed"}\nid: 5'
    const frame = parseSseFrame(block)
    expect(frame).not.toBeNull()
    expect(frame!.event).toBe('step.completed')
    expect(frame!.id).toBe('5')
    expect(JSON.parse(frame!.data).seq).toBe(5)
  })

  it('parses a declared gap frame (event: gap, no id)', () => {
    const block = 'event: gap\ndata: {"after_seq":3,"before_seq":7,"missing_count":3,"reason":"append_swallowed"}'
    const frame = parseSseFrame(block)
    expect(frame!.event).toBe('gap')
    expect(frame!.id).toBeUndefined()
    expect(JSON.parse(frame!.data).missing_count).toBe(3)
  })

  it('joins multiple data lines and strips a single leading space', () => {
    const block = 'event: x\ndata: line1\ndata: line2'
    const frame = parseSseFrame(block)
    expect(frame!.data).toBe('line1\nline2')
  })

  it('ignores comment lines and returns null for a comment-only block', () => {
    expect(parseSseFrame(': keep-alive')).toBeNull()
  })

  it('tolerates CRLF line endings', () => {
    const block = 'event: run.started\r\ndata: {"seq":1}\r\nid: 1'
    const frame = parseSseFrame(block)
    expect(frame!.event).toBe('run.started')
    expect(frame!.id).toBe('1')
  })
})

describe('dispatchFrame', () => {
  function handlers() {
    return {
      onEvent: vi.fn() as (e: WorkflowEvent) => void,
      onGap: vi.fn() as (g: GapMarker) => void,
    }
  }

  it('routes an event frame to onEvent and returns its seq as the reconnect cursor', () => {
    const h = handlers()
    const seq = dispatchFrame(
      { event: 'step.completed', data: '{"seq":5,"event_type":"step.completed"}', id: '5' },
      h,
    )
    expect((h.onEvent as any).mock.calls.length).toBe(1)
    expect((h.onGap as any).mock.calls.length).toBe(0)
    expect(seq).toBe(5)
  })

  it('routes a declared gap frame to onGap and does NOT advance the cursor', () => {
    const h = handlers()
    const seq = dispatchFrame(
      {
        event: 'gap',
        data: '{"after_seq":3,"before_seq":7,"missing_count":3,"reason":"append_swallowed"}',
      },
      h,
    )
    expect((h.onGap as any).mock.calls.length).toBe(1)
    expect((h.onEvent as any).mock.calls.length).toBe(0)
    // A gap owns no seq — it must never advance the reconnect cursor.
    expect(seq).toBeNull()
  })

  it('never crashes on a non-JSON frame (e.g. a keep-alive)', () => {
    const h = handlers()
    expect(() => dispatchFrame({ event: 'message', data: 'not-json' }, h)).not.toThrow()
    expect((h.onEvent as any).mock.calls.length).toBe(0)
  })

  it('falls back to the payload seq when the id field is absent', () => {
    const h = handlers()
    const seq = dispatchFrame({ event: 'step.started', data: '{"seq":9}' }, h)
    expect(seq).toBe(9)
  })
})

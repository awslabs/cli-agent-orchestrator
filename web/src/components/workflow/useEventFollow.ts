// useEventFollow — live SSE follow of a run's event timeline (#504 / U8, FR-6).
//
// Opens the content-negotiated SSE arm of GET /workflows/runs/{id}/events with
// `Accept: text/event-stream` and parses the raw `event:`/`data:`/`id:` frame
// grammar with a fetch-stream reader (NOT EventSource):
//   - EventSource cannot set the Accept header or carry a reconnect cursor as a
//     query param, and cannot address the DYNAMIC per-event `event:` names the
//     server emits (`step.started`, `step.completed`, ...). A manual parser
//     handles every frame uniformly.
//   - The declared `event: gap` frame ({after_seq, before_seq, missing_count,
//     reason}) is routed to `onGap` as a first-class DECLARED gap — the client
//     renders exactly what the API declares and NEVER infers a gap from event
//     numbering (BR-4).
//   - Reconnect is cursor-based: each event frame carries `id: <seq>` (the sole
//     ordering authority); on stream end/error we reconnect with
//     `?after_seq=<lastSeq>` so replay resumes strictly after the last delivered
//     event, dedupe-free. `after_seq` is the same cursor a native EventSource
//     would send as `Last-Event-ID` — we pass it explicitly.

import { useEffect, useRef } from 'react'
import type { WorkflowEvent, GapMarker } from '../../api'

export interface EventFollowHandlers {
  onEvent: (event: WorkflowEvent) => void
  onGap: (gap: GapMarker) => void
  onConnectedChange?: (connected: boolean) => void
  // The server declared the run absent (never existed, or deleted / swept).
  // Terminal: the follow loop STOPS reconnecting when this fires, because no
  // amount of retrying can make a deleted run reappear.
  onAbsent?: (runId: string) => void
}

interface ParsedFrame {
  event: string
  data: string
  id?: string
}

/**
 * Parse a single SSE frame (a block of lines already split on the blank-line
 * delimiter) into its `event` / `data` / `id` fields. Multiple `data:` lines
 * are joined with `\n` per the SSE spec. Returns null for a comment-only /
 * empty frame.
 */
export function parseSseFrame(block: string): ParsedFrame | null {
  let event = 'message'
  const dataLines: string[] = []
  let id: string | undefined
  for (const rawLine of block.split('\n')) {
    const line = rawLine.replace(/\r$/, '')
    if (line === '' || line.startsWith(':')) continue // blank or comment
    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    // A single leading space after the colon is stripped (SSE spec).
    let value = colon === -1 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'event') event = value
    else if (field === 'data') dataLines.push(value)
    else if (field === 'id') id = value
  }
  if (dataLines.length === 0 && id === undefined) return null
  return { event, data: dataLines.join('\n'), id }
}

/** Sentinel returned by dispatchFrame for the terminal `run_absent` frame. */
export const RUN_ABSENT = Symbol('run_absent')

/**
 * Route one parsed frame to the appropriate handler. Exported for direct unit
 * testing of the gap-vs-event dispatch without a live stream. Returns the seq
 * to advance the reconnect cursor to, `null` for a frame that owns no seq, or
 * `RUN_ABSENT` to tell the follow loop to stop permanently.
 */
export function dispatchFrame(
  frame: ParsedFrame,
  handlers: EventFollowHandlers,
): number | null | typeof RUN_ABSENT {
  let jsonData: unknown
  try {
    jsonData = JSON.parse(frame.data)
  } catch {
    return null // a non-JSON frame (e.g. a keep-alive) is ignored, never crashes
  }
  if (frame.event === 'run_absent') {
    handlers.onAbsent?.((jsonData as { run_id?: string })?.run_id ?? '')
    return RUN_ABSENT
  }
  if (frame.event === 'gap') {
    handlers.onGap(jsonData as GapMarker)
    return null // a gap owns no seq — it never advances the reconnect cursor
  }
  const event = jsonData as WorkflowEvent
  handlers.onEvent(event)
  // The frame's `id:` is the seq; fall back to the payload seq if absent.
  const seq = frame.id != null ? Number(frame.id) : event.seq
  return Number.isFinite(seq) ? seq : null
}

/**
 * Follow a run's live event stream. `runId === null` (or `enabled === false`)
 * leaves the stream closed. Reconnects with backoff on transient failure,
 * always resuming from the last delivered seq. Handlers are read through a ref
 * so a caller can pass inline closures without forcing a reconnect.
 */
export function useEventFollow(
  runId: string | null,
  handlers: EventFollowHandlers,
  opts: { enabled?: boolean; initialAfterSeq?: number } = {},
): void {
  const { enabled = true, initialAfterSeq } = opts
  const handlersRef = useRef(handlers)
  handlersRef.current = handlers

  useEffect(() => {
    if (!runId || !enabled) return

    let closed = false
    let controller: AbortController | null = null
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined
    let lastSeq: number | undefined = initialAfterSeq

    const setConnected = (c: boolean) => {
      if (!closed) handlersRef.current.onConnectedChange?.(c)
    }

    const connect = async () => {
      if (closed) return
      controller = new AbortController()
      const q = lastSeq != null ? `?after_seq=${lastSeq}` : ''
      const url = `/workflows/runs/${encodeURIComponent(runId)}/events${q}`
      try {
        const res = await fetch(url, {
          headers: { Accept: 'text/event-stream' },
          signal: controller.signal,
        })
        if (!res.ok || !res.body) throw new Error(`SSE ${res.status}`)
        setConnected(true)

        const reader = res.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''
        // Read the stream, splitting on the SSE frame delimiter (a blank line).
        // Each iteration consumes one complete frame (everything up to the
        // delimiter) and advances the buffer past that delimiter.
        for (;;) {
          const { value, done } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true })
          let match: RegExpExecArray | null
          const delimiter = /\r?\n\r?\n/
          while ((match = delimiter.exec(buffer)) !== null) {
            const block = buffer.slice(0, match.index)
            buffer = buffer.slice(match.index + match[0].length)
            const frame = parseSseFrame(block)
            if (frame && !closed) {
              const seq = dispatchFrame(frame, handlersRef.current)
              if (seq === RUN_ABSENT) {
                // The run is gone for good. Suppress the reconnect (the
                // `finally` below checks `closed`) so we do not re-open the
                // stream every 1.5s against an id that can never come back.
                closed = true
                controller?.abort()
                return
              }
              if (seq != null) lastSeq = seq
            }
          }
        }
      } catch {
        // Transient network / server error — fall through to reconnect.
      } finally {
        setConnected(false)
        if (!closed) {
          // Reconnect after a short delay, resuming from lastSeq.
          reconnectTimer = setTimeout(connect, 1500)
        }
      }
    }

    connect()

    return () => {
      closed = true
      controller?.abort()
      if (reconnectTimer) clearTimeout(reconnectTimer)
      setConnected(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, enabled, initialAfterSeq])
}

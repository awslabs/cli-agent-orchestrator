/**
 * Streaming mode engine (§6): focused keystrokes become bounded, serialized
 * v3 batches on the ordinary control-input route (D2/D3 — streaming is a
 * client mode, never a second transport and never a websocket input frame).
 *
 * The engine is deliberately DOM-free. The component owns the fetch wiring
 * (`onSendBatch`), the capture surface, and the rendering; this class owns:
 *
 * - **Batching (§6.3 step 2):** trailing text merges; a non-text event other
 *   than Enter after text is a flush boundary; a trailing Enter fuses with
 *   pending text into one batch (caps permitting — otherwise the text
 *   flushes first and Enter rides the next batch); an Enter with no pending
 *   text is its own batch immediately; the quiet timer flushes after
 *   `coalesceWindowMs`; a pending text event flushes at 48 chars. Hard caps
 *   (32 events / 512 UTF-8 bytes) always seal the open batch first.
 * - **Serialization (§3.4):** at most one batch in flight; input arriving
 *   during a flight accumulates, in order, into later batches. A batch is
 *   never re-sent with the same `control_id`; the only re-attempt is the
 *   §6.4 single scheduled retry with a fresh id after a zero-byte refusal.
 * - **Grace pacing (§6.3 step 3):** after an accepted Enter-carrying batch,
 *   composer-class batches are withheld while the advertised
 *   `dispatchGraceMs` window runs. Pacing prevents the refusal; the §6.4
 *   pause rule is the backstop for clock skew.
 * - **Pause vs disarm (§6.4):** routed on the pinned reason-detail
 *   discriminators — never guessed; a `pane-busy` matching none of them is
 *   disarm (fail closed). `ambiguous` is never retried and always disarms.
 * - **Atomic disarm:** timers cancelled, all unsent pending events
 *   discarded (nothing typed after the disarm point is ever sent), only
 *   trace metadata retained; the in-flight batch's exact-id reconciliation
 *   still runs to completion.
 */

import {
  appendEvent,
  mapKeyToEvent,
  previewSequence,
  sequenceTextBytes,
  MAX_SEQUENCE_EVENTS,
  MAX_SEQUENCE_TEXT_BYTES,
  type KeyLike,
  type SequenceEvent,
} from './sequenceRecorder'

// §6.4 pinned pause/disarm discriminators: the three pane-busy flavors share
// one deployed reason code and are told apart by reason *detail* substrings.
// §10.1 contract-tests these against the deployed server strings so a
// wording change fails loudly. No typed sub-reason is added to the wire.
export const DISPATCH_GRACE_DISCRIMINATOR = 'inside its dispatch grace'
export const TURN_GATE_DISCRIMINATOR = 'not idle'
export const LEASE_CONTENTION_DISCRIMINATOR = 'input lease is held by'

// §6.4: the readiness-gate retry interval is a client-pinned streaming
// constant — deliberately NOT a server capability; no protocol growth.
export const STREAMING_GATE_RETRY_MS = 1000

// §6.3 step 2: a pending text event flushes when it reaches this length.
export const TEXT_FLUSH_CHARS = 48

// §6.5: the trace is bounded to the last 50 batches.
export const TRACE_LIMIT = 50

// Interrupt-class events (§3.2): Escape / C-c / C-s travel without the
// readiness gate, so a batch made only of them is not grace-withheld.
const INTERRUPT_KEYS = new Set(['Escape', 'C-c', 'C-s'])

function isComposerClass(events: SequenceEvent[]): boolean {
  return events.some(event => {
    if (event.type === 'text') return true
    if (event.type === 'key') return !INTERRUPT_KEYS.has(event.key ?? '')
    return !INTERRUPT_KEYS.has(event.chord ?? '')
  })
}

export interface BatchOutcome {
  outcome: string
  reasonCode?: string
  reasonDetail?: string
}

/**
 * The component's send-and-reconcile result. `resolved` carries the final
 * typed outcome — the POST's typed body when there is one, otherwise the
 * journaled record from the single exact-id GET (§3.4: a batch is never
 * re-sent). `reconcile-failed` means even that query failed — a §6.4
 * environment disarm.
 */
export type SendResult =
  | { kind: 'resolved'; result: BatchOutcome }
  | { kind: 'reconcile-failed' }

export interface TraceEntry {
  /** Short control id, or '—' for local refusals (no request was minted). */
  controlIdShort: string
  preview: string
  outcome: string
  reasonCode?: string
  events: number
  bytes: number
  note?: string
}

export interface StreamingConfig {
  /** Advertised coalesce window (§3.5); the quiet timer before a flush. */
  coalesceWindowMs: number
  /** Advertised kimi dispatch grace (§3.5); absent → no grace pacing. */
  dispatchGraceMs?: number
  /** §6.7 (r15): batches declare `payload_class: "interactive"` through the
   * armed surface, so the server no longer refuses them on the dispatch
   * grace — the legacy client grace withhold is skipped for this engine.
   * Absent/false (the old-server fallback) keeps the §6.3 pacing exactly. */
  declareInteractive?: boolean
  /** The per-terminal advertised chord set held at arm time (§3.5, D9). */
  advertisedChords: ReadonlySet<string>
  now?: () => number
  mintId?: () => string
  setTimeoutFn?: typeof setTimeout
  clearTimeoutFn?: typeof clearTimeout
}

export interface StreamingHooks {
  /** POST one batch + at most one exact-id reconcile; never re-sends. */
  onSendBatch: (controlId: string, events: SequenceEvent[]) => Promise<SendResult>
  /** Called with the full trace copy whenever it changes. */
  onTrace: (trace: TraceEntry[]) => void
  /** The atomic-disarm notification; the engine is inert afterwards. */
  onDisarm: (reason: string, reasonCode?: string) => void
  /** Any state change (pending counts, pause notes) for the surface. */
  onChange?: () => void
}

interface Flight {
  controlId: string
  events: SequenceEvent[]
}

export class StreamingEngine {
  private readonly config: Required<Omit<StreamingConfig, 'dispatchGraceMs'>> &
    Pick<StreamingConfig, 'dispatchGraceMs'>
  private readonly hooks: StreamingHooks

  /** The open batch being accumulated (text fusion target). */
  private open: SequenceEvent[] = []
  /** Sealed batches waiting for the flight slot, oldest first. */
  private queue: SequenceEvent[][] = []
  private inFlight: Flight | null = null
  /** Batches already given their single §6.4 re-attempt (by reference). */
  private readonly retriedOnce = new Set<SequenceEvent[]>()
  private graceUntil = 0
  private disarmed = false
  private readonly trace: TraceEntry[] = []

  private quietTimer: ReturnType<typeof setTimeout> | null = null
  private withholdTimer: ReturnType<typeof setTimeout> | null = null

  constructor(config: StreamingConfig, hooks: StreamingHooks) {
    this.config = {
      coalesceWindowMs: config.coalesceWindowMs,
      dispatchGraceMs: config.dispatchGraceMs,
      declareInteractive: config.declareInteractive ?? false,
      advertisedChords: config.advertisedChords,
      now: config.now ?? (() => Date.now()),
      mintId: config.mintId ?? (() => crypto.randomUUID()),
      // Browser timer intrinsics must be invoked with the global as their
      // receiver: calling a bare `setTimeout` reference as a method of this
      // config object (`this.config.setTimeoutFn(...)`) throws
      // `TypeError: Illegal invocation` in browsers, which killed the quiet
      // timer on the first captured key. Bind the defaults to globalThis.
      setTimeoutFn: config.setTimeoutFn ?? setTimeout.bind(globalThis),
      clearTimeoutFn: config.clearTimeoutFn ?? clearTimeout.bind(globalThis),
    }
    this.hooks = hooks
  }

  get isDisarmed(): boolean {
    return this.disarmed
  }

  get pendingEventCount(): number {
    return this.open.length + this.queue.reduce((n, batch) => n + batch.length, 0)
  }

  get hasInFlight(): boolean {
    return this.inFlight !== null
  }

  traceSnapshot(): TraceEntry[] {
    return [...this.trace]
  }

  // ── Capture ──────────────────────────────────────────────────────────

  /**
   * One keydown on the capture surface. The mapping refusal (unadvertised
   * chord, unrepresentable combination) is returned for inline display AND
   * recorded in the trace (§6.2: the refusal is trace-visible); no request
   * is ever minted for it.
   */
  handleKey(key: KeyLike): { refused?: string } {
    if (this.disarmed) return { refused: 'streaming is disarmed' }
    const mapped = mapKeyToEvent(key, this.config.advertisedChords)
    if (mapped.refused || !mapped.event) {
      const label = key.key.length === 1 && !key.ctrlKey && !key.metaKey && !key.altKey
        ? JSON.stringify(key.key)
        : key.key
      this.appendTrace({
        controlIdShort: '—',
        preview: label,
        outcome: 'refused-locally',
        events: 0,
        bytes: 0,
        note: mapped.refused,
      })
      return { refused: mapped.refused }
    }
    this.accept(mapped.event)
    this.hooks.onChange?.()
    return {}
  }

  /**
   * Pasted clipboard text becomes one text event, screened against the
   * remaining byte budget; over-budget is refused with a message (§6.2) and
   * never partially applied.
   */
  handlePaste(text: string): { refused?: string } {
    if (this.disarmed) return { refused: 'streaming is disarmed' }
    if (!text) return {}
    const remaining = MAX_SEQUENCE_TEXT_BYTES - sequenceTextBytes(this.open)
    const bytes = new TextEncoder().encode(text).length
    if (bytes > remaining) {
      const refused =
        `paste carries ${bytes} bytes but this batch has ${remaining} of ` +
        `${MAX_SEQUENCE_TEXT_BYTES} remaining; refused rather than truncated`
      this.appendTrace({
        controlIdShort: '—',
        preview: `"${text.slice(0, 24)}${text.length > 24 ? '…' : ''}"`,
        outcome: 'refused-locally',
        events: 0,
        bytes: 0,
        note: refused,
      })
      return { refused }
    }
    this.accept({ type: 'text', text })
    this.hooks.onChange?.()
    return {}
  }

  // ── Batching (§6.3 step 2) ───────────────────────────────────────────

  private accept(event: SequenceEvent): void {
    if (event.type === 'text') {
      // Caps seal the open batch first; the char then starts a fresh one.
      if (this.wouldExceedCaps(event)) this.sealOpen()
      this.open = appendEvent(this.open, event)
      const trailing = this.open[this.open.length - 1]
      if (trailing?.type === 'text' && (trailing.text?.length ?? 0) >= TEXT_FLUSH_CHARS) {
        this.sealOpen()
      } else {
        this.armQuietTimer()
      }
      this.trySend()
      return
    }
    if (event.type === 'key' && event.key === 'Enter') {
      const hasPendingText = this.open[this.open.length - 1]?.type === 'text'
      if (hasPendingText && this.open.length + 1 <= MAX_SEQUENCE_EVENTS) {
        // Enter-after-text fusion: the utterance and its submitting Enter
        // travel in one request (the interleave hazard §6.3 names).
        this.open = appendEvent(this.open, event)
      } else if (this.open.length > 0) {
        // Fusion would exceed the event cap: the text flushes first and
        // the Enter rides the next batch.
        this.sealOpen()
        this.open = [event]
      } else {
        this.open = [event]
      }
      // A completed utterance flushes immediately — no quiet timer.
      this.sealOpen()
      this.trySend()
      return
    }
    // Any other non-text event: a boundary after pending text; otherwise it
    // joins the open batch (caps sealing first).
    if (this.wouldExceedCaps(event)) this.sealOpen()
    const boundaryAfterText = this.open[this.open.length - 1]?.type === 'text'
    if (boundaryAfterText) this.sealOpen()
    this.open = appendEvent(this.open, event)
    this.armQuietTimer()
    this.trySend()
  }

  private wouldExceedCaps(event: SequenceEvent): boolean {
    if (this.open.length === 0) return false
    // A text event merges into a trailing text event (no new event);
    // everything else — and text after a non-text event — adds one.
    const mergesWithTrailing =
      event.type === 'text' && this.open[this.open.length - 1]?.type === 'text'
    const nextEvents = mergesWithTrailing ? this.open.length : this.open.length + 1
    if (nextEvents > MAX_SEQUENCE_EVENTS) return true
    // Hard caps always (§6.3 step 2). The byte branch is defensive: the
    // 48-char trailing-text flush and the §6.2 paste budget screen dominate,
    // so a single open batch can never actually approach 512 bytes — but if
    // a future flush rule ever relaxes those, the cap still seals first.
    const addedBytes = event.type === 'text' ? new TextEncoder().encode(event.text ?? '').length : 0
    return sequenceTextBytes(this.open) + addedBytes > MAX_SEQUENCE_TEXT_BYTES
  }

  private sealOpen(): void {
    this.cancelQuietTimer()
    if (this.open.length === 0) return
    this.queue.push(this.open)
    this.open = []
  }

  private armQuietTimer(): void {
    this.cancelQuietTimer()
    this.quietTimer = this.config.setTimeoutFn(() => {
      this.quietTimer = null
      this.sealOpen()
      this.trySend()
    }, this.config.coalesceWindowMs)
  }

  private cancelQuietTimer(): void {
    if (this.quietTimer !== null) {
      this.config.clearTimeoutFn(this.quietTimer)
      this.quietTimer = null
    }
  }

  // ── Dispatch (one flight at a time; grace pacing) ────────────────────

  private trySend(): void {
    if (this.disarmed || this.inFlight !== null || this.queue.length === 0) return
    const now = this.config.now()
    const head = this.queue[0]
    if (
      this.config.dispatchGraceMs !== undefined &&
      !this.config.declareInteractive &&
      now < this.graceUntil &&
      isComposerClass(head)
    ) {
      // §6.3 step 3: withhold composer-class batches while the advertised
      // dispatch grace runs; pacing prevents the server refusal.  Skipped
      // for declared interactive batches (§6.7 — no server grace refusal
      // exists for them, so graceUntil is never armed either).
      this.scheduleWithhold(this.graceUntil - now)
      return
    }
    this.cancelWithhold()
    const events = this.queue.shift() as SequenceEvent[]
    const flight: Flight = { controlId: this.config.mintId(), events }
    this.inFlight = flight
    this.hooks.onChange?.()
    void this.hooks.onSendBatch(flight.controlId, flight.events).then(result => {
      this.settle(flight, result)
    })
  }

  private scheduleWithhold(delayMs: number): void {
    if (this.withholdTimer !== null) return
    this.withholdTimer = this.config.setTimeoutFn(() => {
      this.withholdTimer = null
      this.trySend()
    }, Math.max(0, delayMs))
  }

  private cancelWithhold(): void {
    if (this.withholdTimer !== null) {
      this.config.clearTimeoutFn(this.withholdTimer)
      this.withholdTimer = null
    }
  }

  private settle(flight: Flight, result: SendResult): void {
    if (this.inFlight !== flight) return // superseded; engine was disarmed
    this.inFlight = null
    if (this.disarmed) {
      // Atomic disarm already happened; the exact-id reconciliation still
      // ran to completion, and its outcome is trace metadata (§6.4).
      if (result.kind === 'resolved') {
        this.appendTrace(this.traceEntry(flight, result.result))
      }
      return
    }
    if (result.kind === 'reconcile-failed') {
      this.appendTrace(this.traceEntry(flight, { outcome: 'unknown' }, 'reconciliation query failed'))
      this.disarm('reconciliation query failed; the batch outcome is unknowable')
      return
    }
    const outcome = result.result
    this.appendTrace(this.traceEntry(flight, outcome))
    if (outcome.outcome === 'accepted') {
      if (
        this.config.dispatchGraceMs !== undefined &&
        !this.config.declareInteractive &&
        flight.events.some(event => event.type === 'key' && event.key === 'Enter')
      ) {
        // §6.3 grace pacing applies to undeclared batches only (§6.7): the
        // server no longer issues the grace refusal to declared interactive
        // batches, so withholding them here would be pure delay.
        this.graceUntil = this.config.now() + this.config.dispatchGraceMs
      }
      this.trySend()
      return
    }
    this.routeNonAccepted(flight, outcome)
  }

  // ── §6.4 pause / disarm routing ──────────────────────────────────────

  private routeNonAccepted(flight: Flight, outcome: BatchOutcome): void {
    const { outcome: name, reasonCode, reasonDetail } = outcome
    if (name === 'refused' && reasonCode === 'pane-busy') {
      const detail = reasonDetail ?? ''
      if (detail.includes(LEASE_CONTENTION_DISCRIMINATOR)) {
        this.disarm(
          'concurrent input: another writer holds the pane input lease; streaming disarmed',
          reasonCode,
        )
        return
      }
      if (detail.includes(DISPATCH_GRACE_DISCRIMINATOR)) {
        this.pause(flight, 'paused (dispatch grace)', this.graceRetryDelay(), reasonCode)
        return
      }
      if (detail.includes(TURN_GATE_DISCRIMINATOR)) {
        this.pause(flight, 'provider busy', STREAMING_GATE_RETRY_MS, reasonCode)
        return
      }
      // A pane-busy matching none of the pinned discriminators is never
      // guessed into a pause (fail closed).
      this.disarm(`pane-busy with an unrecognized detail: ${detail || '(none)'}`, reasonCode)
      return
    }
    if (name === 'refused') {
      this.disarm(`refused: ${reasonCode ?? 'unknown'}${reasonDetail ? ` — ${reasonDetail}` : ''}`, reasonCode)
      return
    }
    if (name === 'ambiguous') {
      // Ambiguous is terminal for automation: reconciled by the journal
      // (already done by the sender), never by re-sending.
      this.disarm(
        `ambiguous: ${reasonCode ?? 'unknown'}${reasonDetail ? ` — ${reasonDetail}` : ''}`,
        reasonCode,
      )
      return
    }
    if (name === 'unsupported') {
      this.disarm('unsupported by this server', reasonCode)
      return
    }
    // Any unknown typed outcome fails closed.
    this.disarm(`unknown outcome ${name}`, reasonCode)
  }

  private graceRetryDelay(): number {
    const now = this.config.now()
    if (this.graceUntil > now) return this.graceUntil - now
    // Clock skew backstop (§6.3 step 3): the server's grace is still
    // running even though the local estimate expired.
    return this.config.dispatchGraceMs ?? STREAMING_GATE_RETRY_MS
  }

  /**
   * Pause: exactly one scheduled re-attempt with a FRESH control_id
   * (licensed because `refused` proves zero bytes, §3.4). A paused batch
   * keeps its queue position; input keeps accumulating behind it. If the
   * re-attempt is also refused, §6.4 disarms with the reason.
   */
  private pause(flight: Flight, note: string, delayMs: number, reasonCode?: string): void {
    if (this.retriedOnce.has(flight.events)) {
      this.disarm(`${note} — the re-attempt was also refused (${reasonCode ?? 'pane-busy'})`, reasonCode)
      return
    }
    this.retriedOnce.add(flight.events)
    this.appendTrace({
      controlIdShort: flight.controlId.slice(0, 8),
      preview: previewSequence(flight.events),
      outcome: 'paused',
      reasonCode,
      events: flight.events.length,
      bytes: sequenceTextBytes(flight.events),
      note: `${note}; one automatic re-attempt scheduled`,
    })
    this.queue.unshift(flight.events)
    this.scheduleWithhold(delayMs)
    this.hooks.onChange?.()
  }

  /**
   * Atomic disarm (§6.4): timers cancelled, all unsent pending events
   * discarded — nothing typed after this point is ever sent — and only
   * trace metadata retained. The in-flight batch's reconciliation still
   * completes (see settle).
   */
  disarm(reason: string, reasonCode?: string): void {
    if (this.disarmed) return
    this.disarmed = true
    this.cancelQuietTimer()
    this.cancelWithhold()
    this.open = []
    this.queue = []
    this.retriedOnce.clear()
    this.appendTrace({
      controlIdShort: '—',
      preview: '',
      outcome: 'disarmed',
      reasonCode,
      events: 0,
      bytes: 0,
      note: reason,
    })
    this.hooks.onDisarm(reason, reasonCode)
    this.hooks.onChange?.()
  }

  // ── Trace ────────────────────────────────────────────────────────────

  private traceEntry(flight: Flight, outcome: BatchOutcome, note?: string): TraceEntry {
    return {
      controlIdShort: flight.controlId.slice(0, 8),
      preview: previewSequence(flight.events),
      outcome: outcome.outcome,
      reasonCode: outcome.reasonCode,
      events: flight.events.length,
      bytes: sequenceTextBytes(flight.events),
      note,
    }
  }

  private appendTrace(entry: TraceEntry): void {
    this.trace.push(entry)
    if (this.trace.length > TRACE_LIMIT) {
      this.trace.splice(0, this.trace.length - TRACE_LIMIT)
    }
    this.hooks.onTrace([...this.trace])
  }

  clearTrace(): void {
    this.trace.length = 0
    this.hooks.onTrace([])
  }
}

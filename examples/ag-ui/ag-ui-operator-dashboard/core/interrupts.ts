/**
 * Interrupt cards: a pure reducer from run-plane outcomes to what the
 * operator sees.
 *
 * Cards come only from the `interrupts[]` of a `RUN_FINISHED(interrupt)` on
 * the run plane. An ambient `approval_card` frame never creates, resolves or
 * changes a card: any agent can emit one. It can only attach an unverified
 * hint and ask for a fresh run.
 */

import { controlsFor, payloadFor, type Control, type Decision } from "./controls.ts";
import type { ResponseSchema, ResumeEntry, RunOutcome, SkippedResume, WireInterrupt } from "./types.ts";
import { RECONCILE_KINDS } from "./types.ts";

export type CardState =
  | { s: "pending"; note?: string }
  | { s: "held"; decision: Decision; entry: ResumeEntry }
  | { s: "submitting"; decision: Decision }
  | { s: "delivered"; unconfirmed: boolean }
  | { s: "refused"; reason: string }
  | { s: "skipped"; reason: string }
  | { s: "unknown"; error: string };

export interface Hint {
  resolved: boolean;
  outcome?: string;
}

export interface Card {
  id: string;
  reason: string;
  message: string;
  provider: string;
  terminalId: string;
  subagentRunId: string;
  responseSchema: ResponseSchema | undefined;
  controls: Control[];
  state: CardState;
  hint?: Hint;
}

export interface Board {
  cards: Card[];
  /** The last failure shown verbatim, or `undefined`. */
  lastError?: string;
  /** A run that carried decisions failed; the next run must be a reconcile run. */
  needsReconcile: boolean;
}

/** Text the UI shows for a delivered card: the run plane does not report which decision won. */
export const DELIVERED_TEXT = "delivered (decision applied: not reported by the run plane)";
export const UNCONFIRMED_TEXT = "delivered (unconfirmed)";
export const GONE_REASON = "no longer pending on the run plane: answered elsewhere or expired";

export function emptyBoard(): Board {
  return { cards: [], needsReconcile: false };
}

function cardFrom(wire: WireInterrupt): Card {
  return {
    id: wire.id,
    reason: wire.reason ?? "",
    message: wire.message ?? "",
    provider: wire.metadata?.provider ?? "",
    terminalId: wire.metadata?.terminalId ?? "",
    subagentRunId: wire.subagentRunId ?? "",
    responseSchema: wire.responseSchema,
    controls: controlsFor(wire.responseSchema),
    state: { s: "pending" },
  };
}

function refreshed(card: Card, wire: WireInterrupt): Card {
  return { ...cardFrom(wire), state: card.state, hint: card.hint };
}

/** Cards the operator can still decide. */
export function isOpen(card: Card): boolean {
  return card.state.s === "pending" || card.state.s === "held" || card.state.s === "refused";
}

/**
 * Apply the interrupt list of a run that carried no decisions: a refresh, or
 * the reconcile run after a failure.
 */
export function applyListing(board: Board, interrupts: WireInterrupt[]): Board {
  const listed = new Map(interrupts.map((w) => [w.id, w]));
  const cards = board.cards.map((card): Card => {
    const wire = listed.get(card.id);
    if (wire) {
      const next = refreshed(card, wire);
      if (card.state.s === "unknown" || card.state.s === "submitting") {
        return { ...next, state: { s: "pending", note: "still open after the failed run" } };
      }
      if (card.state.s === "delivered" || card.state.s === "skipped") {
        return { ...next, state: { s: "pending", note: "listed as open again" } };
      }
      return next;
    }
    if (card.state.s === "unknown" || card.state.s === "submitting") {
      return { ...card, state: { s: "delivered", unconfirmed: true } };
    }
    if (isOpen(card)) return { ...card, state: { s: "skipped", reason: GONE_REASON } };
    return card;
  });
  const known = new Set(board.cards.map((c) => c.id));
  for (const wire of interrupts) {
    if (!known.has(wire.id)) cards.push(cardFrom(wire));
  }
  return { cards, needsReconcile: false, lastError: board.lastError };
}

/** Record an operator decision. An unsendable one becomes `refused` and nothing is held. */
export function decide(board: Board, id: string, decision: Decision): Board {
  return mapCard(board, id, (card) => {
    if (!isOpen(card)) return card;
    const result = payloadFor(card.id, decision, card.responseSchema);
    if (!result.ok) return { ...card, state: { s: "refused", reason: result.reason } };
    return { ...card, state: { s: "held", decision, entry: result.entry } };
  });
}

/** Withdraw a held decision before it is sent. */
export function undecide(board: Board, id: string): Board {
  return mapCard(board, id, (card) => (card.state.s === "held" ? { ...card, state: { s: "pending" } } : card));
}

export interface Batch {
  ready: boolean;
  resume: ResumeEntry[];
  /** Open cards that still need a decision. */
  waitingOn: string[];
}

/** One `resume[]` covering every open interrupt, or the ids still undecided. */
export function batch(board: Board): Batch {
  const open = board.cards.filter(isOpen);
  const waitingOn = open.filter((c) => c.state.s !== "held").map((c) => c.id);
  const resume = open.flatMap((c) => (c.state.s === "held" ? [c.state.entry] : []));
  const busy = board.cards.some((c) => c.state.s === "submitting");
  return { ready: open.length > 0 && waitingOn.length === 0 && !busy && !board.needsReconcile, resume, waitingOn };
}

export function markSubmitting(board: Board, ids: string[]): Board {
  const sent = new Set(ids);
  return {
    ...board,
    lastError: undefined,
    cards: board.cards.map((card) =>
      sent.has(card.id) && card.state.s === "held"
        ? { ...card, state: { s: "submitting", decision: card.state.decision } }
        : card,
    ),
  };
}

/** Describe a failed run for the operator, verbatim where the server said something. */
export function describeFailure(outcome: RunOutcome): string {
  switch (outcome.kind) {
    case "RUN_ERROR":
      return `RUN_ERROR: ${outcome.message}`;
    case "OTHER_END":
      return `the run finished with outcome "${outcome.outcomeType}", which reports no delivery`;
    case "HTTP_ERROR":
      // An unlabelled code's label is already "HTTP <status>"; don't say it twice.
      return outcome.label === `HTTP ${outcome.status}`
        ? `${outcome.label}: ${outcome.detail}`
        : `${outcome.label} (HTTP ${outcome.status}): ${outcome.detail}`;
    case "NETWORK_ERROR":
      return `network error (check the server is up and this origin is allowed): ${outcome.message}`;
    case "TIMEOUT":
      return "the run timed out";
    case "NO_END":
      return "the run ended with no RUN_FINISHED or RUN_ERROR";
    default:
      return "";
  }
}

/**
 * Apply the outcome of a run that carried `resume[]` for `sentIds`.
 *
 * Per id: listed in `skippedResume` → skipped; listed again in the outcome's
 * interrupts → still open (pending); otherwise delivered. A failed run marks
 * every sent id `unknown` and asks for a reconcile run, because `RUN_ERROR`
 * stops at the first failing entry and loses `skippedResume`.
 */
export function applyResumeOutcome(board: Board, sentIds: string[], outcome: RunOutcome): Board {
  const sent = new Set(sentIds);
  if (outcome.kind !== "SUCCESS" && outcome.kind !== "INTERRUPT") {
    const error = describeFailure(outcome);
    return {
      lastError: error,
      needsReconcile: RECONCILE_KINDS.has(outcome.kind),
      cards: board.cards.map((card) =>
        sent.has(card.id) && card.state.s === "submitting" ? { ...card, state: { s: "unknown", error } } : card,
      ),
    };
  }
  const interrupts = outcome.kind === "INTERRUPT" ? outcome.interrupts : [];
  const skipped = new Map(outcome.skipped.map((s: SkippedResume) => [s.interruptId, s.reason]));
  const stillOpen = new Set(interrupts.map((w) => w.id));
  const settled: Board = {
    ...board,
    cards: board.cards.map((card): Card => {
      if (!sent.has(card.id) || card.state.s !== "submitting") return card;
      const why = skipped.get(card.id);
      if (why !== undefined) return { ...card, state: { s: "skipped", reason: why } };
      if (stillOpen.has(card.id)) {
        return { ...card, state: { s: "pending", note: "still open after the answer was sent" } };
      }
      return { ...card, state: { s: "delivered", unconfirmed: false } };
    }),
  };
  // Every card that was not sent is reconciled against the same listing.
  const others = applyListing(
    { ...settled, cards: settled.cards.filter((c) => !sent.has(c.id)) },
    interrupts.filter((w) => !sent.has(w.id)),
  );
  const byId = new Map(others.cards.map((c) => [c.id, c]));
  const merged = settled.cards.map((c) => byId.get(c.id) ?? c);
  const present = new Set(merged.map((c) => c.id));
  return {
    cards: [...merged, ...others.cards.filter((c) => !present.has(c.id))],
    needsReconcile: false,
    lastError: undefined,
  };
}

/**
 * Attach an ambient `approval_card` hint to a matching card. The card's state
 * never changes. Returns whether a read-only run should confirm the state:
 * - a card this board knows says resolved or expired;
 * - an id this board does not know says it is open (a new prompt, perhaps).
 * The refresh carries no `resume[]`, so a forged frame can cost a run but can
 * never answer anything.
 */
export function applyAmbientHint(board: Board, props: Record<string, unknown>): { board: Board; refresh: boolean } {
  const id = typeof props.interrupt_id === "string" ? props.interrupt_id : "";
  if (!id) return { board, refresh: false };
  const resolved = props.resolved === true;
  const outcome = typeof props.outcome === "string" ? props.outcome : undefined;
  if (!board.cards.some((c) => c.id === id)) return { board, refresh: !resolved && outcome === undefined };
  const next = mapCard(board, id, (card) => ({ ...card, hint: { resolved, outcome } }));
  return { board: next, refresh: resolved || outcome === "expired" };
}

/** Plain-text status line for a card. */
export function stateText(card: Card): string {
  const st = card.state;
  switch (st.s) {
    case "pending":
      return st.note ? `pending (${st.note})` : "pending";
    case "held":
      return `held: ${st.decision.kind}, waiting until every open approval is decided`;
    case "submitting":
      return `sending: ${st.decision.kind}`;
    case "delivered":
      return st.unconfirmed ? UNCONFIRMED_TEXT : DELIVERED_TEXT;
    case "refused":
      return `refused: ${st.reason}`;
    case "skipped":
      return `skipped: ${st.reason}`;
    case "unknown":
      return `unknown, reconciling: ${st.error}`;
  }
}

function mapCard(board: Board, id: string, fn: (card: Card) => Card): Board {
  return { ...board, cards: board.cards.map((card) => (card.id === id ? fn(card) : card)) };
}

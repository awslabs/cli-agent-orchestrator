// Which agents need the operator's attention.
//
// An agent the operator is not looking at gets flagged when:
//   - it is waiting for an answer (WAITING_USER_ANSWER), for as long as it waits;
//   - it finished a turn (PROCESSING -> IDLE/COMPLETED) — "unread" until viewed;
//   - it moved into ERROR.
// "Looking at" means the terminal is in a visible pane while the browser tab is
// visible. Viewing clears the flag. The first status ever observed for a
// terminal never raises "done"/"error", so opening the page does not flag every
// agent that finished earlier; a waiting agent is flagged right away because it
// is blocked now.

export type Attention = 'waiting' | 'done' | 'error'

export const ATTENTION_LABEL: Record<Attention, string> = {
  waiting: 'Waiting for your answer',
  done: 'Finished while you were away',
  error: 'Hit an error',
}

export function normalizeStatus(status: string | null | undefined): string {
  return (status || 'UNKNOWN').toUpperCase()
}

/**
 * Next attention flag for one terminal.
 * @param prev    previously observed status, or undefined on first sight
 * @param next    newly observed status
 * @param current the flag the terminal has now
 * @param viewed  whether the operator is looking at it
 */
export function nextAttention(
  prev: string | undefined,
  next: string,
  current: Attention | undefined,
  viewed: boolean,
): Attention | undefined {
  if (viewed) return undefined
  const n = normalizeStatus(next)
  if (n === 'WAITING_USER_ANSWER') return 'waiting'
  // A waiting flag is about the present state; once it stops waiting it is
  // either processing the answer or finished, which the rules below cover.
  const kept = current === 'waiting' ? undefined : current
  if (prev === undefined) return kept
  const p = normalizeStatus(prev)
  if (n === 'ERROR' && p !== 'ERROR') return 'error'
  if ((n === 'IDLE' || n === 'COMPLETED') && p === 'PROCESSING') return 'done'
  return kept
}

/** Severity order for summarising several flags (higher wins). */
const RANK: Record<Attention, number> = { done: 1, error: 2, waiting: 3 }

export function strongest(flags: Iterable<Attention | undefined>): Attention | undefined {
  let best: Attention | undefined
  for (const f of flags) {
    if (f && (!best || RANK[f] > RANK[best])) best = f
  }
  return best
}

/** Severity order for summarising several statuses into one session dot. */
const STATUS_RANK: Record<string, number> = {
  WAITING_USER_ANSWER: 6,
  ERROR: 5,
  PROCESSING: 4,
  COMPLETED: 3,
  IDLE: 2,
  STOPPED: 1,
}

export function summaryStatus(statuses: Iterable<string | null | undefined>): string | null {
  let best: string | null = null
  for (const s of statuses) {
    const n = normalizeStatus(s)
    if (best === null || (STATUS_RANK[n] ?? 0) > (STATUS_RANK[best] ?? 0)) best = n
  }
  return best
}

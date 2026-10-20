import { useRef } from 'react'

/** Handle bound to one unit of async work. See {@link useGeneration}. */
export interface GenerationToken {
  /**
   * True while no newer generation has been started (`begin`) or declared
   * (`invalidate`) since this token was issued. A response, continuation, or
   * settled value whose token is no longer current is stale and must be
   * discarded.
   */
  isCurrent: () => boolean
}

export interface Generation {
  /**
   * Start a unit of async work bound to the state of the world RIGHT NOW.
   * Call at the moment the triggering state changes -- not when a debounced
   * request is eventually issued -- so that the state change itself, not the
   * request, is what defines staleness.
   */
  begin: () => GenerationToken
  /**
   * Declare every outstanding token stale without starting new work. Call at
   * every site where the REASON for outstanding work disappears (a clear, a
   * deselect, a close, a user navigation) even though nothing new is being
   * requested.
   */
  invalidate: () => void
}

/**
 * Monotonic generation counter for async staleness control.
 *
 * This is the single shared implementation of the invariant that five review
 * rounds of #692 re-derived one call site at a time: an async result may only
 * be applied if the state that motivated it is still the current state.
 * The failure modes it closes, each observed in review:
 *
 * - a late response landing after its request was superseded (round 2)
 * - a "clear" path that issues no new request leaving old tokens valid
 *   (round 3)
 * - a token advanced at request-issue time instead of state-change time,
 *   leaving the debounce window unprotected (round 4)
 * - a continuation racing user navigation performed after the mutation
 *   (round 5)
 *
 * Usage:
 * ```ts
 * const searchGen = useGeneration()
 * // state changed -> bind work to this moment:
 * const token = searchGen.begin()
 * const rows = await api.search(q)
 * if (!token.isCurrent()) return   // superseded meanwhile -> drop
 * setResults(rows)
 * // reason for outstanding work disappeared -> declare it stale:
 * searchGen.invalidate()
 * ```
 *
 * The returned object is referentially stable across renders, so it is safe
 * to use inside effects without listing it as a dependency.
 */
export function useGeneration(): Generation {
  const gen = useRef(0)
  const api = useRef<Generation>({
    begin: () => {
      const mine = ++gen.current
      return { isCurrent: () => mine === gen.current }
    },
    invalidate: () => {
      ++gen.current
    },
  })
  return api.current
}

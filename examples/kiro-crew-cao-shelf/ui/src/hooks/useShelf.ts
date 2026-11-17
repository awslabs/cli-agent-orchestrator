import { useAppInfo } from '@kirocrew/app-sdk'
import { useCallback, useEffect, useRef, useState } from 'react'
import { type ShelfError, toShelfError, useShelfApi } from '../api'
import type { ShelfResponse, Unchanged } from '../types'

export const POLL_MS = 5000
const MAX_BACKOFF_MS = 60000

export interface ShelfState {
  data: ShelfResponse | null
  /** Date.now() when `data` arrived; with `data.generated_at` it gives the browser-to-backend clock skew. */
  receivedAt: number
  /** Set when the last request to the app backend failed; `data` keeps the last good shelf. */
  error: ShelfError | null
  loading: boolean
  /** Fetch now (after an action). */
  refresh: () => void
}

const isUnchanged = (r: ShelfResponse | Unchanged): r is Unchanged => (r as Unchanged).unchanged === true

export function useShelf(): ShelfState {
  const api = useShelfApi()
  const { active } = useAppInfo()
  const enabled = active !== false // undefined means true
  const [data, setData] = useState<ShelfResponse | null>(null)
  const [receivedAt, setReceivedAt] = useState(0)
  const [error, setError] = useState<ShelfError | null>(null)
  const [loading, setLoading] = useState(true)
  const revision = useRef<number | null>(null)
  const failures = useRef(0)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const ctrl = useRef<AbortController | null>(null)
  const loop = useRef<(() => void) | null>(null)

  useEffect(() => {
    if (!enabled) return
    let dead = false
    const visible = () => document.visibilityState !== 'hidden'

    const schedule = () => {
      if (dead || !visible()) return
      const wait = failures.current === 0 ? POLL_MS : Math.min(MAX_BACKOFF_MS, POLL_MS * 2 ** failures.current)
      timer.current = setTimeout(run, wait)
    }
    const run = async () => {
      if (dead || !visible()) return
      if (timer.current) clearTimeout(timer.current)
      ctrl.current?.abort()
      const c = new AbortController()
      ctrl.current = c
      try {
        const r = await api.shelf(revision.current, c.signal)
        if (dead || c.signal.aborted) return
        failures.current = 0
        setError(null)
        if (!isUnchanged(r)) {
          revision.current = r.revision
          setData(r)
          setReceivedAt(Date.now())
        }
      } catch (e) {
        if (dead || c.signal.aborted) return
        failures.current += 1
        setError(toShelfError(e))
      } finally {
        if (!dead && !c.signal.aborted) setLoading(false)
      }
      schedule()
    }
    loop.current = run
    const onVisibility = () => {
      if (visible()) run()
      else {
        if (timer.current) clearTimeout(timer.current)
        ctrl.current?.abort()
      }
    }
    document.addEventListener('visibilitychange', onVisibility)
    if (visible()) run()
    return () => {
      dead = true
      loop.current = null
      document.removeEventListener('visibilitychange', onVisibility)
      if (timer.current) clearTimeout(timer.current)
      ctrl.current?.abort()
    }
  }, [api, enabled])

  const refresh = useCallback(() => {
    // Force a full read: an action may have changed things without a revision bump we have seen.
    revision.current = null
    loop.current?.()
  }, [])

  return { data, receivedAt, error, loading, refresh }
}

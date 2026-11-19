import { createContext, useContext } from 'react'
import type { ShelfApi } from './api'

export interface ShelfCtx {
  api: ShelfApi
  /** Why write actions are off, or null when they are available. */
  disabledReason: string | null
  /** The id of the banner on screen that says why, for `aria-describedby` on the controls it turns off. */
  reasonId: string | undefined
  /** Tell the polite live region. */
  announce: (msg: string) => void
  /** Re-read the shelf now. */
  refresh: () => void
  /** The backend's clock for the shown snapshot (ISO), used for "N min ago". */
  now: string
}

export const ShelfContext = createContext<ShelfCtx | null>(null)

export function useShelfCtx(): ShelfCtx {
  const c = useContext(ShelfContext)
  if (!c) throw new Error('ShelfContext missing')
  return c
}

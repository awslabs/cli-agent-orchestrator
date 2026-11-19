import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { vi } from 'vitest'
import type { ShelfResponse } from '../src/types'

// vitest runs from ui/, so fixtures are one level up.
const fx = (p: string) => resolve(process.cwd(), '../fixtures/expected', p)
export const load = <T = any,>(p: string): T => JSON.parse(readFileSync(fx(p), 'utf8')) as T
export const quiet = () => load<ShelfResponse>('quiet.shelf.json')
export const busy = () => load<ShelfResponse>('busy.shelf.json')
export const history = (day: 'quiet' | 'busy', name: string) => load<any>(`${day}/history.${name}.json`)
/** A shelf the backend served for a failed read; tests/test_cao_shelf_poller.py writes and pins it. */
export const backendPayload = (name: string): ShelfResponse =>
  JSON.parse(readFileSync(resolve(process.cwd(), '../tests/payloads', `${name}.shelf.json`), 'utf8')) as ShelfResponse

export interface Call {
  method: string
  path: string
  body: any
}
export type Handler = (call: Call) => { status?: number; body?: unknown } | Promise<{ status?: number; body?: unknown }>

/** Replaces globalThis.fetch with a router keyed "METHOD /path" (query string ignored). */
export function mockBackend(routes: Record<string, Handler | unknown>) {
  const calls: Call[] = []
  const table = { ...routes }
  const fetchMock = vi.fn(async (input: any, init?: RequestInit) => {
    const url = String(input)
    const path = url.split('?')[0].replace('/apps/cao-shelf/api', '')
    const method = (init?.method ?? 'GET').toUpperCase()
    const body = init?.body ? JSON.parse(String(init.body)) : undefined
    calls.push({ method, path: url.replace('/apps/cao-shelf/api', ''), body })
    const h = table[`${method} ${path}`]
    if (h === undefined) return new Response(JSON.stringify({ error: { code: 'not_found', message: `no route ${method} ${path}`, retryable: false } }), { status: 404 })
    const r = typeof h === 'function' ? await (h as Handler)({ method, path, body }) : { body: h }
    return new Response(JSON.stringify(r.body ?? {}), { status: r.status ?? 200 })
  })
  globalThis.fetch = fetchMock as unknown as typeof fetch
  return { calls, table, fetchMock, writes: () => calls.filter((c) => c.method !== 'GET') }
}

export const err = (status: number, code: string, message = 'x', detail?: unknown) => ({
  status,
  body: { error: { code, message, retryable: status >= 500, ...(detail ? { detail } : {}) } },
})

export const flush = async () => {
  for (let i = 0; i < 6; i++) await Promise.resolve()
}

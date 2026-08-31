// Guards the /workflows dev-proxy contract for the content-negotiated SSE
// events route (#504 / U8). The reviewer on PR #526 flagged that the comment
// claimed to disable "proxy buffering + Nagle" while the config only sets
// buffering/caching HEADERS — Nagle (TCP_NODELAY) is not reachable from here.
// These tests pin what the config actually does, so the corrected comment and
// the code cannot drift apart again: the header hints must be set on BOTH legs,
// and no socket-level/Nagle option may be introduced without updating this
// test (and the comment) deliberately.

import { describe, it, expect, vi } from 'vitest'
import config from '../../vite.config'

type ProxyOpts = {
  target?: string
  ws?: boolean
  configure?: (proxy: any, options: any) => void
}

function workflowsProxy(): ProxyOpts {
  const resolved: any = typeof config === 'function' ? (config as any)({ mode: 'development', command: 'serve' }) : config
  const entry = resolved?.server?.proxy?.['/workflows']
  expect(entry, 'vite.config.ts must proxy /workflows in dev').toBeDefined()
  return entry as ProxyOpts
}

describe('/workflows dev proxy — SSE no-buffering contract (#526)', () => {
  it('proxies to the API server', () => {
    expect(workflowsProxy().target).toBe('http://localhost:9889')
  })

  it('sets no-buffer/no-cache HEADERS on the request leg', () => {
    const entry = workflowsProxy()
    expect(typeof entry.configure).toBe('function')

    const handlers: Record<string, Function[]> = {}
    const proxy = { on: (ev: string, fn: Function) => ((handlers[ev] ||= []).push(fn), proxy) }
    entry.configure!(proxy, {})

    expect(handlers.proxyReq?.length).toBeGreaterThan(0)
    const setHeader = vi.fn()
    // A request-leg handler must ONLY touch headers — never a socket option.
    const proxyReq: any = { setHeader, socket: { setNoDelay: vi.fn() } }
    for (const fn of handlers.proxyReq) fn(proxyReq, {}, {}, {})

    expect(setHeader).toHaveBeenCalledWith('X-Accel-Buffering', 'no')
    expect(setHeader).toHaveBeenCalledWith('Cache-Control', 'no-cache')
    // Nagle is NOT controlled here — the corrected comment says so.
    expect(proxyReq.socket.setNoDelay).not.toHaveBeenCalled()
  })

  it('sets no-buffer/no-cache headers on the response leg too', () => {
    const entry = workflowsProxy()
    const handlers: Record<string, Function[]> = {}
    const proxy = { on: (ev: string, fn: Function) => ((handlers[ev] ||= []).push(fn), proxy) }
    entry.configure!(proxy, {})

    expect(handlers.proxyRes?.length).toBeGreaterThan(0)
    const proxyRes: any = { headers: {} }
    for (const fn of handlers.proxyRes) fn(proxyRes, {}, {})

    expect(proxyRes.headers['x-accel-buffering']).toBe('no')
    expect(proxyRes.headers['cache-control']).toBe('no-cache')
  })
})

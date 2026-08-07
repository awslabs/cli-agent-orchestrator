/// <reference types="vitest" />
// `defineConfig` comes from vitest/config (a superset of vite's) so the `test`
// block below type-checks. Under vitest 4 the bare `/// <reference>` above no
// longer widens vite's own UserConfig to accept `test`.
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// Minimal structural shapes for the two proxy hooks we use. Declared locally
// rather than imported from node:http / http-proxy: this package ships no
// @types/node, and http-proxy's own `ProxyServer` type omits the `on` overloads
// for these events, so the untyped callbacks were implicit `any`.
type ProxyReqHook = { setHeader(name: string, value: string): void }
type ProxyResHook = { headers: Record<string, string | string[] | undefined> }
type ConfigurableProxy = {
  on(event: 'proxyReq', cb: (proxyReq: ProxyReqHook) => void): void
  on(event: 'proxyRes', cb: (proxyRes: ProxyResHook) => void): void
}

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../src/cli_agent_orchestrator/web_ui',
    emptyOutDir: true,
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: './src/test/setup.ts',
  },
  server: {
    host: 'localhost',
    port: 5173,
    proxy: {
      '/sessions': { target: 'http://localhost:9889', changeOrigin: true },
      '/terminals': { target: 'http://localhost:9889', changeOrigin: true, ws: true },
      '/health': { target: 'http://localhost:9889', changeOrigin: true },
      '/agents': { target: 'http://localhost:9889', changeOrigin: true },
      '/settings': { target: 'http://localhost:9889', changeOrigin: true },
      '/flows': { target: 'http://localhost:9889', changeOrigin: true },
      '/memory': { target: 'http://localhost:9889', changeOrigin: true },
      '/graph': { target: 'http://localhost:9889', changeOrigin: true },
      // Workflow run journal (#504 / U8). The events route is content-negotiated
      // SSE, so we signal "do not buffer or cache this response" to any
      // intermediary via X-Accel-Buffering: no + Cache-Control: no-cache on
      // both legs. That keeps `event:`/`data:`/`id:` frames (including the
      // declared `event: gap` frame) reaching the client as they are emitted
      // rather than being coalesced by a buffering proxy. Note these are
      // HEADER hints only — TCP-level coalescing (Nagle/TCP_NODELAY) is not
      // controllable from this config.
      '/workflows': {
        target: 'http://localhost:9889',
        changeOrigin: true,
        configure: (proxy: unknown) => {
          const p = proxy as ConfigurableProxy
          p.on('proxyReq', (proxyReq) => {
            proxyReq.setHeader('X-Accel-Buffering', 'no')
            proxyReq.setHeader('Cache-Control', 'no-cache')
          })
          p.on('proxyRes', (proxyRes) => {
            // Ensure no intermediary caches or buffers the event stream.
            proxyRes.headers['x-accel-buffering'] = 'no'
            proxyRes.headers['cache-control'] = 'no-cache'
          })
        },
      },
    },
  },
})

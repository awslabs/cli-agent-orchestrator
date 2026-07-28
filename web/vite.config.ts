/// <reference types="vitest" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

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
      // SSE: disable proxy buffering + Nagle so `event:`/`data:`/`id:` frames
      // (including the declared `event: gap` frame) reach the client as they are
      // emitted rather than being coalesced by an intermediary buffer.
      '/workflows': {
        target: 'http://localhost:9889',
        changeOrigin: true,
        configure: (proxy) => {
          proxy.on('proxyReq', (proxyReq) => {
            proxyReq.setHeader('X-Accel-Buffering', 'no')
            proxyReq.setHeader('Cache-Control', 'no-cache')
          })
          proxy.on('proxyRes', (proxyRes) => {
            // Ensure no intermediary caches or buffers the event stream.
            proxyRes.headers['x-accel-buffering'] = 'no'
            proxyRes.headers['cache-control'] = 'no-cache'
          })
        },
      },
    },
  },
})

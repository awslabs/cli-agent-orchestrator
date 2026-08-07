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
    // The §10.5 Playwright suite lives in e2e/ and runs under
    // @playwright/test, never vitest.
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
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
      '/macros': { target: 'http://localhost:9889', changeOrigin: true },
      '/control-input': { target: 'http://localhost:9889', changeOrigin: true },
      '/operator-message': { target: 'http://localhost:9889', changeOrigin: true },
      '/annotations': { target: 'http://localhost:9889', changeOrigin: true },
      // Without this the Projects tab works when cao-server serves the built
      // bundle (same origin) and is entirely dead under `npm run dev`, where
      // every tracker call 404s against the Vite dev server instead.
      '/tracker': { target: 'http://localhost:9889', changeOrigin: true },
    },
  },
})

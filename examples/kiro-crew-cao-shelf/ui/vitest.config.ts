import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vitest/config'

const stub = (f: string) => fileURLToPath(new URL(`./test/sdk-stub/${f}`, import.meta.url))

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: [
      { find: '@kirocrew/app-sdk/ui', replacement: stub('ui.tsx') },
      { find: '@kirocrew/app-sdk', replacement: stub('index.tsx') },
    ],
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./test/setup.ts'],
    include: ['test/**/*.test.{ts,tsx}'],
    css: false,
  },
})

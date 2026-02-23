import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: { 
    host: '0.0.0.0',
    port: 5173,
    proxy: { 
      '/api': {
        target: 'http://localhost:8000',
        ws: true
      },
      '/terminals': {
        target: 'http://localhost:8000',
        ws: true
      }
    } 
  },
  test: {
    globals: true,
    environment: 'happy-dom',
    setupFiles: './src/test/setup.ts',
    include: ['src/**/*.test.{ts,tsx}']
  }
})

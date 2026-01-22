import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: { 
    proxy: { 
      '/api': 'http://localhost:8000', 
      '/sessions': 'http://localhost:9889', 
      '/terminals': 'http://localhost:9889', 
      '/ws': { target: 'ws://localhost:8000', ws: true }
    } 
  }
})

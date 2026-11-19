import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Library build: one unminified ES module, host-provided packages stay external
// (the Kiro Crew dashboard resolves them through its import map at runtime).
export default defineConfig({
  plugins: [react()],
  define: { 'process.env.NODE_ENV': JSON.stringify('production') },
  build: {
    lib: { entry: 'src/App.tsx', formats: ['es'], fileName: () => 'index.mjs' },
    outDir: 'bundle',
    emptyOutDir: true,
    minify: false,
    rollupOptions: {
      external: [
        'react',
        'react-dom',
        'react/jsx-runtime',
        '@kirocrew/app-sdk',
        '@kirocrew/app-sdk/ui',
        'lucide-react',
        '@tanstack/react-query',
      ],
    },
  },
})

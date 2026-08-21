import '@testing-library/jest-dom'

// Sigma references WebGL2RenderingContext at module-load time to pick a
// renderer, but jsdom provides no WebGL. Any test that transitively imports a
// component pulling in `sigma` (e.g. MemoryPanel → MemoryGraphView) would crash
// at import otherwise. A no-op class stub lets the module load; tests that
// actually mount the graph mock `sigma` outright (see memory-graph.test.tsx).
for (const name of ['WebGLRenderingContext', 'WebGL2RenderingContext']) {
  if (typeof (globalThis as any)[name] === 'undefined') {
    ;(globalThis as any)[name] = class {}
  }
}

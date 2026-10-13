import '@testing-library/jest-dom'
import type { TestingLibraryMatchers } from '@testing-library/jest-dom/matchers'

// Vitest 5 reads custom matcher types only from its own `Matchers<R, T>`
// interface. jest-dom still declares the Vitest 4 shape, so its matcher types
// are lost under Vitest 5 (testing-library/jest-dom#738). Remove this block
// when jest-dom ships Vitest 5 types. The type parameter names must stay the
// same as in Vitest's declaration, or TypeScript does not merge the interface.
declare module 'vitest' {
  interface Matchers<R, T> extends TestingLibraryMatchers<unknown, R> {}
}

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

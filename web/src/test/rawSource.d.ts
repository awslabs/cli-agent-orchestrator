// Vite's `?raw` suffix imports a module's own text. Used by the anti-vocabulary
// guard in annotations.test.tsx, which has to read the renderer's SOURCE rather
// than its exports — the thing being asserted is what the file does not say.
//
// Declared here rather than pulling in @types/node so the guard needs no new
// dependency and the same import works under both `tsc` and vitest.
declare module '*?raw' {
  const source: string
  export default source
}

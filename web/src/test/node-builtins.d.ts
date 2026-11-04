/**
 * Minimal ambient declarations for the Node builtins the tests in this
 * directory use.
 *
 * This package ships no `@types/node` (the reason `vite.config.ts` declares its
 * own structural shapes for the proxy hooks), and `create-budget.test.ts` has
 * to read source files that live OUTSIDE `web/` — the Python helper whose
 * arithmetic it mirrors. Vite's `?raw` import cannot reach those: it refuses
 * anything outside `server.fs.allow`, and widening that list to make a test
 * convenient would widen what the dev server will serve. Declaring the three
 * functions actually called is the smaller change.
 */

declare module 'node:fs' {
  export function readFileSync(path: string, encoding: 'utf-8'): string
  export function existsSync(path: string): boolean
}

declare module 'node:path' {
  export function dirname(path: string): string
  export function join(...parts: string[]): string
}

declare const process: { cwd(): string }

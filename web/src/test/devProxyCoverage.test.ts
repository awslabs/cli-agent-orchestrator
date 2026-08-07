import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

/**
 * Every route prefix `api.ts` calls must be proxied in dev.
 *
 * This gap is invisible in production and in every test: cao-server serves the
 * built bundle from its own origin, so a missing proxy entry breaks nothing
 * there, and unit tests stub `fetch` outright. It only appears under
 * `npm run dev`, where the unproxied call 404s against the Vite dev server and
 * the feature is simply dead — which is how the Projects tab shipped with
 * `/tracker` missing from the list.
 */

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')

function proxiedPrefixes(): Set<string> {
  const config = readFileSync(resolve(root, 'vite.config.ts'), 'utf-8')
  const block = config.slice(config.indexOf('proxy:'))
  return new Set(Array.from(block.matchAll(/'(\/[a-z-]+)':\s*\{/g), m => m[1]))
}

function calledPrefixes(): Set<string> {
  const api = readFileSync(resolve(root, 'src/api.ts'), 'utf-8')
  const prefixes = new Set<string>()
  // Template literals and plain strings alike: `/tracker/issues/${key}` and
  // '/agents/profiles' both contribute their first path segment.
  for (const [, path] of api.matchAll(/fetchJSON<[^>]*>\(\s*[`'"](\/[a-zA-Z0-9-]+)/g)) {
    prefixes.add(path)
  }
  return prefixes
}

describe('dev server proxy coverage', () => {
  it('proxies every prefix the api client calls', () => {
    const proxied = proxiedPrefixes()
    const missing = [...calledPrefixes()].filter(p => !proxied.has(p)).sort()
    expect(missing).toEqual([])
  })

  it('finds the prefixes at all, so an empty comparison cannot pass vacuously', () => {
    expect(proxiedPrefixes().size).toBeGreaterThan(5)
    expect(calledPrefixes().size).toBeGreaterThan(5)
  })

  it('includes the tracker routes the Projects tab depends on', () => {
    expect(proxiedPrefixes().has('/tracker')).toBe(true)
  })
})

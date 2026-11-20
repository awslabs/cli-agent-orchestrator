// The page may use only what src/sdk.d.ts declares from '@kirocrew/app-sdk/ui', the stub must
// export each of those names (or `make dev` fails at link time), and the dev host must serve this
// same stub rather than a second hand-written one.
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { NAMED, NAMESPACE_ONLY } from './kit-names'
import * as stub from './sdk-stub/ui'

const root = resolve(process.cwd(), '..')
const read = (rel: string) => readFileSync(resolve(root, rel), 'utf8')

function declaredUiNames(): string[] {
  const dts = read('ui/src/sdk.d.ts')
  const start = dts.indexOf("declare module '@kirocrew/app-sdk/ui'")
  expect(start).toBeGreaterThan(-1)
  const block = dts.slice(start)
  return [...block.matchAll(/export\s+(?:declare\s+)?(?:function|const|let)\s+(\w+)/g)].map((m) => m[1])
}

describe('the SDK UI surface', () => {
  it('the stub exports every name sdk.d.ts declares for @kirocrew/app-sdk/ui', () => {
    const names = declaredUiNames()
    expect(names.length).toBeGreaterThan(0)
    for (const n of names) expect(stub, n).toHaveProperty(n)
  })

  it('the stub exports the whole allowlist and both namespace-only names', () => {
    for (const n of [...NAMED, ...NAMESPACE_ONLY]) expect(stub, n).toHaveProperty(n)
  })

  it('sdk.d.ts declares the allowlist and the two namespace-only names', () => {
    expect(declaredUiNames().sort()).toEqual([...NAMED, ...NAMESPACE_ONLY].sort())
  })

  it('the dev host serves this stub, not a copy of its own', () => {
    const dev = read('e2e/rig/dev_host/app-sdk-ui.mjs')
    expect(dev).toMatch(/export \* from ["'][./]*ui\/test\/sdk-stub\/ui\.tsx["']/)
    expect(dev).not.toMatch(/\bfunction\b|createElement/)
  })
})

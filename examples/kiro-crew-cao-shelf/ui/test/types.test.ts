import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
// @ts-expect-error plain ESM script without types
import { generate } from '../scripts/gen-types.mjs'

describe('generated types', () => {
  it('src/types.ts matches the schemas (run npm run gen:types after a contract change)', () => {
    expect(readFileSync(resolve(process.cwd(), 'src/types.ts'), 'utf8')).toBe(generate())
  })
})

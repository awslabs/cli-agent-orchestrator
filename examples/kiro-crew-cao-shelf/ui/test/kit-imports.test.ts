// Static checks over ui/src: which host-kit names the page imports and how, and that the page
// defines no colours of its own (design sections 0, 2 and 5).
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative, resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { NAMED, NAMESPACE_ONLY } from './kit-names'

const SRC = resolve(process.cwd(), 'src')

function sources(dir = SRC): string[] {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n)
    if (statSync(p).isDirectory()) return sources(p)
    return /\.(ts|tsx)$/.test(n) && !n.endsWith('.d.ts') ? [p] : []
  })
}
const files = () => sources().map((p) => ({ rel: relative(SRC, p), text: readFileSync(p, 'utf8') }))

/** Colour literals the design keeps in ui/src: none. Add `file: literal` entries here only with a
 *  reason the design states. */
const ALLOWED_COLOURS: Record<string, string[]> = {}

describe('imports from @kirocrew/app-sdk/ui', () => {
  it('named imports stay within the allowlist', () => {
    const named = files().flatMap(({ rel, text }) =>
      [...text.matchAll(/import\s*(?:type\s*)?\{([^}]*)\}\s*from\s*['"]@kirocrew\/app-sdk\/ui['"]/g)].flatMap((m) =>
        m[1].split(',').map((s) => ({ rel, name: s.trim().split(/\s+as\s+/)[0] })).filter((x) => x.name),
      ),
    )
    expect(named.filter((x) => !NAMED.includes(x.name))).toEqual([])
  })

  it('Select and guideCaution are read through one namespace import, never by name', () => {
    const ns = files().filter(({ text }) => /import\s*\*\s*as\s+\w+\s+from\s*['"]@kirocrew\/app-sdk\/ui['"]/.test(text))
    expect(ns.map((f) => f.rel)).toHaveLength(1)
    for (const n of NAMESPACE_ONLY) expect(ns[0]?.text ?? '', n).toMatch(new RegExp(`\\b\\w+\\.${n}\\b`))
  })

  it('the page uses the host kit, not only PageHeader', () => {
    const imported = new Set(
      files().flatMap(({ text }) =>
        [...text.matchAll(/import\s*\{([^}]*)\}\s*from\s*['"]@kirocrew\/app-sdk\/ui['"]/g)].flatMap((m) => m[1].split(',').map((s) => s.trim())),
      ),
    )
    for (const n of ['Btn', 'Badge', 'Card', 'EmptyState', 'ErrorNotice', 'Input', 'Modal', 'Skeleton', 'Toggle']) expect(imported, n).toContain(n)
  })

  it('the local copies of host components are gone', () => {
    const names = files().map((f) => f.rel.replace(/\\/g, '/'))
    for (const gone of ['components/Modal.tsx', 'components/Banner.tsx', 'components/EmptyState.tsx']) expect(names).not.toContain(gone)
  })
})

describe('no colours of its own', () => {
  it('has no hex, rgb() or hsl() colour literal in any ui/src file', () => {
    const hits = files().flatMap(({ rel, text }) =>
      [...text.matchAll(/#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})\b|\b(?:rgba?|hsla?)\(/g)]
        .map((m) => m[0])
        .filter((lit) => !(ALLOWED_COLOURS[rel] ?? []).includes(lit))
        .map((lit) => `${rel}: ${lit}`),
    )
    expect(hits).toEqual([])
  })

  it('sets no inline style on a DOM element (it would bypass the token rules)', () => {
    const hits = files().flatMap(({ rel, text }) => [...text.matchAll(/style=\{\{/g)].map(() => rel))
    expect(hits).toEqual([])
  })
})

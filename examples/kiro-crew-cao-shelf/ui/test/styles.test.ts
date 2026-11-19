// The page's own CSS reads host tokens only, and its narrow-width layout rules (design sections 2 and
// 4). jsdom lays nothing out, so the layout is pinned as CSS text here and measured in e2e.
import { describe, expect, it } from 'vitest'
import { CSS } from '../src/styles'

/** The CSS with the whitespace around punctuation removed and quotes unified, so a rule can be
 *  matched however it is written. */
const css = CSS.replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/\s*([{};:,>()])\s*/g, '$1')
  .replace(/'/g, '"')
  .replace(/\[([\w-]+)=([\w-]+)\]/g, '[$1="$2"]')

/** The declarations of every rule whose selector list contains `selector`, joined. */
function decls(selector: string): string {
  const out: string[] = []
  for (const m of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (m[1].split(',').map((s) => s.trim()).includes(selector)) out.push(m[2])
  }
  return out.join(';')
}

/** The body of the first `@media` or `@container` block whose condition contains `cond`. */
function atBlock(kind: '@media' | '@container', cond: string): string {
  const i = css.indexOf(`${kind}${cond}`) >= 0 ? css.indexOf(`${kind}${cond}`) : css.indexOf(`${kind} ${cond}`)
  if (i < 0) return ''
  let depth = 0
  const start = css.indexOf('{', i)
  for (let j = start; j < css.length; j++) {
    if (css[j] === '{') depth++
    else if (css[j] === '}' && --depth === 0) return css.slice(start + 1, j)
  }
  return ''
}

const TOKENS = [
  '--bg', '--bg-elevated', '--bg-hover', '--card', '--border', '--border-strong', '--text', '--text-strong', '--muted',
  '--accent', '--accent-subtle', '--ok', '--ok-text', '--ok-subtle', '--warn', '--warn-text', '--warn-subtle',
  '--danger', '--danger-text', '--danger-subtle', '--info', '--shadow-sm', '--radius-md', '--radius-lg', '--mono',
]

describe('host tokens only', () => {
  it('has no colour literal anywhere', () => {
    expect(css).not.toMatch(/#[0-9a-fA-F]{3,8}\b/)
    expect(css).not.toMatch(/\b(?:rgba?|hsla?)\(/)
  })

  it('defines no variables and reads none with a fallback', () => {
    expect(css).not.toMatch(/(?:^|[{;])--[\w-]+:/)
    expect(css).not.toMatch(/var\(--[\w-]+,/)
    expect(css).not.toContain('--cs-')
  })

  it('reads only the allowlisted host tokens', () => {
    const used = new Set([...css.matchAll(/var\((--[\w-]+)\)/g)].map((m) => m[1]))
    expect([...used].filter((v) => !TOKENS.includes(v))).toEqual([])
    for (const v of ['--card', '--border', '--text', '--muted', '--warn', '--danger']) expect(used).toContain(v)
  })

  it('never uses a raw status hue as text colour', () => {
    expect(css).not.toMatch(/(?:^|[{;])color:var\(--(?:ok|warn|danger|info)\)/)
  })

  it('has no dark-mode branch of its own', () => {
    expect(css).not.toMatch(/prefers-color-scheme|\[data-theme|\[data-mode/)
  })

  it('does not scope a dialog class under .cs-root (the host portals dialogs outside it)', () => {
    expect(css).not.toMatch(/\.cs-root[^{]*\.cs-(?:field|check|lead|list|risk|disc|actions)\b/)
  })

  it('prefixes every class it styles with cs-', () => {
    const classes = [...css.matchAll(/\.([a-zA-Z][\w-]*)/g)].map((m) => m[1])
    expect(classes.filter((c) => !c.startsWith('cs-'))).toEqual([])
  })

  it('leaves the focus ring and the old button, dialog and notice classes to the host', () => {
    expect(css).not.toMatch(/outline:3px/)
    for (const gone of ['.cs-b', '.cs-g', '.cs-rd', '.cs-rd-solid', '.cs-ov', '.cs-dlg', '.cs-sticky', '.cs-banner', '.cs-alert', '.cs-tg', '.cs-panel', '.cs-relay-warn'])
      expect(css, gone).not.toMatch(new RegExp(`\\${gone}(?![\\w-])`))
  })
})

describe('text-safe colour', () => {
  it('status sentences are --text with a status-hue dot, and text on a tint is never muted (ruling 7)', () => {
    expect(decls('.cs-hof')).toContain('color:var(--text)')
    expect(decls('.cs-note-orphan')).toContain('color:var(--text)')
    expect(decls('.cs-hof::before')).toContain('background:var(--ok)')
    expect(decls('.cs-note-orphan::before')).toContain('background:var(--danger)')
    expect(decls('.cs-need em')).toContain('color:var(--text)')
    expect(decls('.cs-need')).toContain('var(--warn-subtle)')
  })

  it('the pending-request notice is an amber notice, not an error', () => {
    const n = decls('.cs-notice[data-tone="warn"]')
    expect(n).toContain('var(--warn-subtle)')
    expect(n).toMatch(/border-left:3px solid var\(--warn\)/)
  })

  it('dialog titles wrap instead of the host clipping them, and relay buttons keep the host hover fill', () => {
    expect(decls('.cs-dlg-title')).toContain('white-space:normal')
    expect(decls('.cs-dlg-title')).toContain('overflow-wrap:anywhere')
    expect(decls('.cs-relay-on .cs-btn:not(:hover)')).toContain('background:var(--card)')
    expect(decls('.cs-relay-on .cs-btn')).toBe('')
    expect(decls('.cs-need .cs-row .cs-btn:not(:hover)')).toContain('background:var(--card)')
  })

  it('open toggles and pressed tiers are visible, quiet buttons have no border', () => {
    expect(decls('.cs-btn[aria-expanded="true"]')).toContain('var(--bg-hover)')
    expect(decls('.cs-btn[aria-pressed="true"]')).toContain('var(--accent-subtle)')
    expect(decls('.cs-quiet')).toContain('border-color:transparent')
    expect(decls('.cs-check')).toContain('accent-color:var(--accent)')
  })
})

describe('layout at 320, 360 and 800 px', () => {
  it('the grid has a 300px minimum that never overflows, and one column at 640px and below', () => {
    expect(decls('.cs-grid')).toContain('grid-template-columns:repeat(auto-fill,minmax(min(100%,300px),1fr))')
    expect(decls('.cs-grid')).not.toContain('245px')
    expect(atBlock('@media', '(max-width:640px)')).toContain('.cs-grid{grid-template-columns:minmax(0,1fr)}')
  })

  it('the card is a column container and the action row is 2 by 2, 4 across from 460px', () => {
    const card = decls('.cs-card')
    expect(card).toContain('container-type:inline-size')
    expect(card).toContain('min-width:0')
    const acts = decls('.cs-acts')
    expect(acts).toContain('display:grid')
    expect(acts).toContain('grid-template-columns:repeat(2,minmax(0,1fr))')
    expect(atBlock('@container', '(min-width:460px)')).toMatch(/\.cs-acts\{[^}]*grid-template-columns:repeat\(4,auto\)/)
    expect(css).not.toMatch(/margin-left:auto/)
  })

  it('the card header wraps, the title can shrink, and the badge keeps its size', () => {
    expect(decls('.cs-head')).toMatch(/display:flex.*flex-wrap:wrap|flex-wrap:wrap.*display:flex/)
    expect(decls('.cs-ttl')).toContain('min-width:0')
    expect(decls('.cs-ttl')).toContain('overflow-wrap:anywhere')
  })

  it('chips wrap inside the card and their label can break', () => {
    expect(decls('.cs-chips')).toContain('flex-wrap:wrap')
    const chip = decls('.cs-chip')
    expect(chip).toContain('flex-wrap:wrap')
    expect(chip).toContain('max-width:100%')
  })

  it('the dialog footer wraps, and its buttons share the row at 640px and below', () => {
    const a = decls('.cs-actions')
    expect(a).toContain('display:flex')
    expect(a).toContain('flex-wrap:wrap')
    expect(a).toContain('justify-content:flex-end')
    expect(atBlock('@media', '(max-width:640px)')).toMatch(/\.cs-actions>\*\{flex:1 1 auto\}/)
  })

  it('the tier row stays one scrolling row at 640px and below', () => {
    expect(atBlock('@media', '(max-width:640px)')).toMatch(/\.cs-tiers\{[^}]*flex-wrap:nowrap;overflow-x:auto/)
  })

  it('touch targets are 44px on narrow or coarse-pointer screens', () => {
    const touch = atBlock('@media', '(max-width:640px),(pointer:coarse)')
    expect(touch).toMatch(/\.cs-btn,\.cs-disc summary,\.cs-check\{min-height:44px\}/)
  })

  it('respects reduced motion and dims stale cards, restoring colour on hover and focus', () => {
    expect(css).toMatch(/prefers-reduced-motion:reduce\)\{\.cs-dot-processing\{animation:none\}/)
    expect(decls('.cs-k-stale')).toMatch(/opacity:\.66.*saturate\(\.5\)/)
    expect(css).toContain('.cs-card:hover,.cs-card:focus-within{opacity:1;filter:none}')
  })
})

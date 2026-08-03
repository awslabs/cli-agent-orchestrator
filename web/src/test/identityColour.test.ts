// The identity palette, asserted rather than reviewed.
//
// Three properties, and every one of them is measured from the artifacts that
// actually decide it rather than restated from a comment:
//
//   * DISJOINTNESS from the six semantic roles — the role hues are PARSED OUT
//     OF the generated Tailwind preset, so re-hueing a role in
//     design-tokens/tokens.json and regenerating moves this test's inputs. A
//     copied-in list of role hexes would keep passing after the roles changed,
//     which is the one failure a disjointness test exists to catch.
//   * CONTRAST on both composited backdrops, against the 4.5 AA floor.
//   * DETERMINISM — same token, same colour, across reloads and across module
//     instances. The whole grouping claim rests on it.

import { describe, it, expect, vi } from 'vitest'
import presetSource from '../../tailwind.preset.cjs?raw'
import { IDENTITY_PALETTE, identityStyle, paletteIndex, rgba } from '../lib/identityColour'
import { SEMANTIC_ROLES } from '../lib/annotations'

type RGB = [number, number, number]

function hexToRgb(hex: string): RGB {
  return [
    parseInt(hex.slice(1, 3), 16),
    parseInt(hex.slice(3, 5), 16),
    parseInt(hex.slice(5, 7), 16),
  ]
}

function hsl(hex: string): { h: number; s: number; l: number } {
  const [r, g, b] = hexToRgb(hex).map(v => v / 255)
  const max = Math.max(r, g, b)
  const min = Math.min(r, g, b)
  const d = max - min
  let h = 0
  if (d > 0) {
    if (max === r) h = 60 * (((g - b) / d) % 6)
    else if (max === g) h = 60 * ((b - r) / d + 2)
    else h = 60 * ((r - g) / d + 4)
  }
  if (h < 0) h += 360
  const l = (max + min) / 2
  const s = d === 0 ? 0 : d / (1 - Math.abs(2 * l - 1))
  return { h, s, l }
}

/** Shortest way round the wheel, which is the only distance that means anything. */
function hueDistance(a: number, b: number): number {
  const d = Math.abs(a - b) % 360
  return Math.min(d, 360 - d)
}

function over(top: RGB, alpha: number, bottom: RGB): RGB {
  return [
    top[0] * alpha + bottom[0] * (1 - alpha),
    top[1] * alpha + bottom[1] * (1 - alpha),
    top[2] * alpha + bottom[2] * (1 - alpha),
  ]
}

function luminance(c: RGB): number {
  const f = c.map(v => {
    const s = v / 255
    return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4)
  })
  return 0.2126 * f[0] + 0.7152 * f[1] + 0.0722 * f[2]
}

function contrast(a: RGB, b: RGB): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

/**
 * CIE L*a*b* (D65, 2°), for the perceptual-distance check.
 *
 * Hue distance in HSL is the wrong ruler for "can an operator tell these two
 * apart": HSL degrees are not perceptually uniform, so 14° of hue is a wide gap
 * in the greens and nearly invisible in the pinks — which is exactly where this
 * palette's closest pairs sit.
 */
function rgbToLab(c: RGB): [number, number, number] {
  const f = c.map(v => {
    const s = v / 255
    return s <= 0.04045 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4)
  })
  const x = (0.4124 * f[0] + 0.3576 * f[1] + 0.1805 * f[2]) / 0.95047
  const y = 0.2126 * f[0] + 0.7152 * f[1] + 0.0722 * f[2]
  const z = (0.0193 * f[0] + 0.1192 * f[1] + 0.9505 * f[2]) / 1.08883
  const g = (t: number) => (t > 0.008856 ? Math.cbrt(t) : 7.787 * t + 16 / 116)
  const [fx, fy, fz] = [g(x), g(y), g(z)]
  return [116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)]
}

/** ΔE2000. The standard perceptual difference; ~2.3 is the just-noticeable difference. */
function ciede2000(l1: [number, number, number], l2: [number, number, number]): number {
  const rad = Math.PI / 180
  const [L1, a1, b1] = l1
  const [L2, a2, b2] = l2
  const C1 = Math.hypot(a1, b1)
  const C2 = Math.hypot(a2, b2)
  const Cb = (C1 + C2) / 2
  const G = 0.5 * (1 - Math.sqrt(Math.pow(Cb, 7) / (Math.pow(Cb, 7) + Math.pow(25, 7))))
  const a1p = (1 + G) * a1
  const a2p = (1 + G) * a2
  const C1p = Math.hypot(a1p, b1)
  const C2p = Math.hypot(a2p, b2)
  const h1p = (Math.atan2(b1, a1p) / rad + 360) % 360
  const h2p = (Math.atan2(b2, a2p) / rad + 360) % 360
  const dLp = L2 - L1
  const dCp = C2p - C1p
  let dhp = 0
  if (C1p * C2p !== 0) {
    dhp = h2p - h1p
    if (dhp > 180) dhp -= 360
    else if (dhp < -180) dhp += 360
  }
  const dHp = 2 * Math.sqrt(C1p * C2p) * Math.sin((dhp * rad) / 2)
  const Lbp = (L1 + L2) / 2
  const Cbp = (C1p + C2p) / 2
  let hbp = h1p + h2p
  if (C1p * C2p !== 0) {
    if (Math.abs(h1p - h2p) <= 180) hbp = (h1p + h2p) / 2
    else hbp = h1p + h2p < 360 ? (h1p + h2p + 360) / 2 : (h1p + h2p - 360) / 2
  }
  const T =
    1 -
    0.17 * Math.cos((hbp - 30) * rad) +
    0.24 * Math.cos(2 * hbp * rad) +
    0.32 * Math.cos((3 * hbp + 6) * rad) -
    0.2 * Math.cos((4 * hbp - 63) * rad)
  const dTh = 30 * Math.exp(-Math.pow((hbp - 275) / 25, 2))
  const Rc = 2 * Math.sqrt(Math.pow(Cbp, 7) / (Math.pow(Cbp, 7) + Math.pow(25, 7)))
  const Sl = 1 + (0.015 * Math.pow(Lbp - 50, 2)) / Math.sqrt(20 + Math.pow(Lbp - 50, 2))
  const Sc = 1 + 0.045 * Cbp
  const Sh = 1 + 0.015 * Cbp * T
  const Rt = -Math.sin(2 * dTh * rad) * Rc
  return Math.sqrt(
    Math.pow(dLp / Sl, 2) +
      Math.pow(dCp / Sc, 2) +
      Math.pow(dHp / Sh, 2) +
      Rt * (dCp / Sc) * (dHp / Sh),
  )
}

/**
 * The role colours, READ FROM THE GENERATED PRESET.
 *
 * The role names come from the renderer's own exported list, so a role added to
 * the design tokens has to be added there too before this test can ignore it —
 * and a role REMOVED from the preset fails here rather than silently dropping
 * out of the disjointness check.
 */
const ROLE_HEX: Record<string, string> = Object.fromEntries(
  SEMANTIC_ROLES.map(role => {
    const match = new RegExp(`"${role}":\\s*"(#[0-9a-fA-F]{6})"`).exec(presetSource)
    if (!match) throw new Error(`the generated preset no longer declares the "${role}" role`)
    return [role, match[1]]
  }),
)

/** Roles with a hue at all. `neutral` is achromatic and cannot be "near" anything. */
const CHROMATIC_ROLES = Object.entries(ROLE_HEX).filter(([, hex]) => hsl(hex).s > 0.05)

// The composited stack a chip is actually read against, innermost last:
//   app background → campaign panel (bg-gray-800/60) → row card (bg-gray-900/50)
// The chip then tints its own colour over that at 10%, exactly as a role chip
// does. Both surfaces are checked because a chip that cannot be fenced onto a
// row lands on the panel, which is the LIGHTER and therefore harder backdrop.
const APP: RGB = [15, 15, 20]
const GRAY_800: RGB = [31, 41, 55]
const GRAY_900: RGB = [17, 24, 39]
const PANEL = over(GRAY_800, 0.6, APP)
const ROW = over(GRAY_900, 0.5, PANEL)
const TINT = 0.1

function chipContrast(hex: string, backdrop: RGB): number {
  const rgb = hexToRgb(hex)
  return contrast(rgb, over(rgb, TINT, backdrop))
}

describe('the identity palette is disjoint from the semantic roles', () => {
  it('parses all six role colours out of the generated preset', () => {
    // If this drifts, every measurement below is against the wrong inputs and
    // the disjointness claim is worthless — so it is asserted first and loudly.
    expect(Object.keys(ROLE_HEX).sort()).toEqual([...SEMANTIC_ROLES].sort())
    expect(CHROMATIC_ROLES).toHaveLength(5)
  })

  it('keeps every entry at least 20° of hue from every chromatic role', () => {
    const measured = IDENTITY_PALETTE.map(hex => {
      const { h } = hsl(hex)
      const nearest = CHROMATIC_ROLES.map(([role, roleHex]) => ({
        role,
        distance: hueDistance(h, hsl(roleHex).h),
      })).sort((a, b) => a.distance - b.distance)[0]
      return { hex, hue: Math.round(h * 10) / 10, ...nearest }
    })
    const tooClose = measured.filter(m => m.distance < 20)
    expect(tooClose, 'identity hues colliding with a role hue').toEqual([])
  })

  it('keeps the red and amber arc clear, which is the confusion that matters', () => {
    // `danger` and `warning` are adjacent hues and are the pair an operator
    // must never conflate. An identity chip landing in either band would be
    // read as an alarm, and — worse — would make a real alarm ambiguous.
    const danger = hsl(ROLE_HEX.danger).h
    const warning = hsl(ROLE_HEX.warning).h
    for (const hex of IDENTITY_PALETTE) {
      const { h } = hsl(hex)
      expect(hueDistance(h, danger), `${hex} sits in the danger band`).toBeGreaterThanOrEqual(25)
      expect(hueDistance(h, warning), `${hex} sits in the warning band`).toBeGreaterThanOrEqual(25)
    }
  })

  it('pins the entry that comes closest to the reserved arc, by hex and by hue', () => {
    // THE RATIONALE COMMENT IN identityColour.ts IS A MEASURED CLAIM, and a
    // measured claim that nothing measures is a comment. Its previous wording
    // said the [330,360] band was empty; `#cc88aa` sits at hue EXACTLY 330.0,
    // so the claim was false by one endpoint while every test stayed green —
    // the rule the palette actually satisfies is the ≥25° one above, not a
    // band. This pins the margin so the two can never drift apart again.
    const danger = hsl(ROLE_HEX.danger).h
    const warning = hsl(ROLE_HEX.warning).h
    const nearest = IDENTITY_PALETTE.map(hex => {
      const { h } = hsl(hex)
      return { hex, hue: h, margin: Math.min(hueDistance(h, danger), hueDistance(h, warning)) }
    }).sort((a, b) => a.margin - b.margin)[0]
    expect(nearest.hex).toBe('#cc88aa')
    expect(Math.round(nearest.hue * 10) / 10).toBe(330)
    expect(Math.round(nearest.margin * 10) / 10).toBe(30)
  })

  it('is desaturated well below every role, so it can never out-shout an alarm', () => {
    // SATURATION IS THE SALIENCE CHANNEL. Hue distance alone would still allow
    // a fully saturated green identity chip to dominate a `danger` beside it.
    for (const hex of IDENTITY_PALETTE) {
      const { s } = hsl(hex)
      expect(s, `${hex} saturation`).toBeGreaterThanOrEqual(0.3)
      expect(s, `${hex} saturation`).toBeLessThanOrEqual(0.42)
    }
    for (const [role, hex] of CHROMATIC_ROLES) {
      expect(hsl(hex).s, `${role} is expected to stay loud`).toBeGreaterThanOrEqual(0.64)
    }
  })

  it('holds twelve distinct colours', () => {
    expect(IDENTITY_PALETTE).toHaveLength(12)
    expect(new Set(IDENTITY_PALETTE).size).toBe(12)
    for (const hex of IDENTITY_PALETTE) expect(hex).toMatch(/^#[0-9a-f]{6}$/)
  })

  it('keeps every pair perceptibly apart, not merely unequal as strings', () => {
    // WHAT THIS CLOSES. `new Set(...).size === 12` asserts STRING uniqueness,
    // and the three pinned-hex assertions are change-detectors that fire on any
    // palette edit including a good one. A QA pass proved the gap by replacing
    // the eight unpinned slots with an 8°-wide band — twelve entries that are
    // mutually ΔE76 0.38–3.04, i.e. literally one colour — and the whole suite
    // stayed green, axe gate included. A palette that means nothing was
    // shippable. This measures the thing the palette exists to provide.
    //
    // THE FLOOR IS A REGRESSION BAR, NOT A QUALITY BAR, and saying so is the
    // honest framing. The shipped palette's closest pair is `#c388cc`/`#cb85c4`
    // at ΔE2000 2.96, which is barely above the ~2.3 JND for two patches placed
    // side by side and well under it for 10px labels on separate rows. 6 of its
    // 66 pairs sit under 5. That is a known weakness recorded in the cutover
    // follow-ups, not something this test endorses: all twelve entries sit at
    // one lightness and one saturation by design, so hue is the only channel
    // carrying separation and twelve hues do not fit in the arc left over after
    // the reserved danger/amber band. Widening it means trading against the
    // stated "identity chips stay quiet" rule — an unconstrained search reaches
    // ΔE2000 12.0, but only by pushing contrast to 9.3:1, louder than every
    // semantic role including `warning` at 8.1. That trade is the reader's to
    // make; this test only stops it silently getting worse.
    const labs = IDENTITY_PALETTE.map(hex => rgbToLab(hexToRgb(hex)))
    const pairs: Array<{ a: string; b: string; dE: number }> = []
    for (let i = 0; i < labs.length; i += 1) {
      for (let j = i + 1; j < labs.length; j += 1) {
        pairs.push({ a: IDENTITY_PALETTE[i], b: IDENTITY_PALETTE[j], dE: ciede2000(labs[i], labs[j]) })
      }
    }
    pairs.sort((x, y) => x.dE - y.dE)
    expect(pairs).toHaveLength(66)
    // No pair may fall to or below the just-noticeable difference. Two entries
    // under it are one colour wearing two hex codes, and the grouping the chip
    // asserts would be invisible.
    expect(
      pairs.filter(p => p.dE <= 2.3),
      'palette entries indistinguishable even side by side',
    ).toEqual([])
    // And the closest pair may not get closer than the one being shipped.
    expect(pairs[0].dE, `closest pair ${pairs[0].a}/${pairs[0].b}`).toBeGreaterThanOrEqual(2.9)
    // The comment above quotes a count; pin it so the two cannot drift.
    expect(pairs.filter(p => p.dE < 5)).toHaveLength(6)
  })
})

describe('the identity palette is legible on both backdrops it is drawn on', () => {
  it('reproduces the six RECORDED role ratios, which is what makes the model trustworthy', () => {
    // The compositing model below is only worth anything if it agrees with
    // numbers this repo measured in a real browser. These six are the ones
    // written down in AnnotationChips.tsx and re-measured by the e2e gate every
    // run; if the model could not reproduce them it would be checking itself.
    const recorded: Record<string, number> = {
      success: 7.1,
      info: 5.5,
      accent: 5.4,
      warning: 8.1,
      danger: 5.2,
      neutral: 5.6,
    }
    for (const [role, hex] of Object.entries(ROLE_HEX)) {
      expect(chipContrast(hex, PANEL), `${role} against the recorded value`).toBeCloseTo(
        recorded[role],
        1,
      )
    }
  })

  it('clears the AA floor for every entry on the panel and on the row card', () => {
    const measured = IDENTITY_PALETTE.map(hex => ({
      hex,
      panel: Math.round(chipContrast(hex, PANEL) * 100) / 100,
      row: Math.round(chipContrast(hex, ROW) * 100) / 100,
    }))
    // Recorded so a regression reads as a number, not as a boolean.
    console.log('IDENTITY CONTRAST', JSON.stringify(measured))
    for (const m of measured) {
      expect(m.panel, `${m.hex} on the campaign panel`).toBeGreaterThanOrEqual(4.5)
      expect(m.row, `${m.hex} on the row card`).toBeGreaterThanOrEqual(4.5)
    }
  })

  it('clears the floors for the uncoloured variant too', () => {
    // The uncoloured chip is text (`gray-400`) inside a solid outline
    // (`gray-500`). Text takes the 4.5 AA floor; a non-text boundary takes 3.0.
    const gray400 = hexToRgb('#9ca3af')
    const gray500 = hexToRgb('#6b7280')
    for (const [name, backdrop] of [
      ['panel', PANEL],
      ['row', ROW],
    ] as Array<[string, RGB]>) {
      expect(contrast(gray400, backdrop), `label on the ${name}`).toBeGreaterThanOrEqual(4.5)
      expect(contrast(gray500, backdrop), `outline on the ${name}`).toBeGreaterThanOrEqual(3)
    }
  })
})

describe('the identity colour is deterministic', () => {
  it('resolves three pinned tokens to three pinned colours', () => {
    // PINNED, so reordering the palette or dropping the hash finalizer is a
    // failure rather than a silent repaint of every chip on the fleet.
    expect(identityStyle('9f2c41a7be03d5e8')).toEqual({
      index: 7,
      colour: '#9e96d2',
      tint: 'rgba(158, 150, 210, 0.1)',
    })
    expect(identityStyle('c31d7ba09e64f2a1').colour).toBe('#4cb14f')
    expect(identityStyle('5d62b4a76036ecd8').colour).toBe('#6bad4a')
  })

  it('gives the same token the same colour every time it is asked', () => {
    for (const key of ['a7f3', 'a-much-longer-opaque-token', '0', '💥', 'x'.repeat(64)]) {
      expect(identityStyle(key)).toEqual(identityStyle(key))
      expect(paletteIndex(key)).toBe(paletteIndex(key))
    }
  })

  it('survives a module reload, which is what "across reloads" actually means', async () => {
    const before = ['pr04', 'cond-0241', 'a7f3'].map(k => identityStyle(k))
    vi.resetModules()
    const fresh = await import('../lib/identityColour')
    const after = ['pr04', 'cond-0241', 'a7f3'].map(k => fresh.identityStyle(k))
    expect(after).toEqual(before)
  })

  it('reads no clock, no locale and no randomness', () => {
    // The three ambient inputs that would make a colour drift without any code
    // changing. `Math.random` is the obvious one; `Date.now` is the one a
    // caching layer would plausibly introduce.
    const random = vi.spyOn(Math, 'random')
    const now = vi.spyOn(Date, 'now')
    const locale = vi.spyOn(String.prototype, 'toLocaleLowerCase')
    identityStyle('9f2c41a7be03d5e8')
    paletteIndex('cond-0241')
    expect(random).not.toHaveBeenCalled()
    expect(now).not.toHaveBeenCalled()
    expect(locale).not.toHaveBeenCalled()
    random.mockRestore()
    now.mockRestore()
    locale.mockRestore()
  })

  it('answers an empty token with no colour at all', () => {
    // The third state. Not an error, not a missing value: "an identity chip
    // that has no colour", which the renderer draws outlined and grey.
    expect(identityStyle('')).toEqual({ index: null, colour: null, tint: null })
  })

  it('spreads tokens evenly enough that colour means something', () => {
    // Not a quality bar the hash was CHOSEN on — both candidate finalizers pass
    // this — but a floor: a hash that funnelled every token into two slots
    // would make the colour actively misleading rather than merely coarse.
    const counts = new Array(IDENTITY_PALETTE.length).fill(0)
    const n = 12000
    for (let i = 0; i < n; i += 1) counts[paletteIndex(`identity-${i}`)] += 1
    const expected = n / IDENTITY_PALETTE.length
    const chi = counts.reduce((sum, c) => sum + (c - expected) ** 2 / expected, 0)
    // 31.26 is the 0.001 critical value at 11 degrees of freedom.
    expect(chi, `bucket counts ${counts.join(',')}`).toBeLessThan(31.26)
  })

  it('renders a tint that is the same colour at ten percent', () => {
    expect(rgba('#4cb14f', 0.1)).toBe('rgba(76, 177, 79, 0.1)')
  })
})

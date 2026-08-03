// Deterministic colour for an OPAQUE identity token.
//
// The conductor publishes `colour_key` and nothing else about grouping: what
// identity the token expresses is entirely its business, and this module's only
// job is to turn a string into a stable palette slot. That is the whole of the
// colour contract, and it is why a change of grouping policy on the producer
// side needs no change on this one.
//
// THE PALETTE IS DISJOINT FROM THE SIX SEMANTIC ROLES, AND THAT IS ASSERTED,
// NOT REVIEWED. The roles mean SEVERITY. A red identity chip would read as
// danger and an amber one as warning, and — worse — a genuine `danger` chip
// would become indistinguishable from an identity that happened to hash red.
// identityColour.test.ts parses the six role hues out of the generated Tailwind
// preset and measures the minimum circular hue distance from every entry here;
// nothing about the separation depends on this comment staying true.
//
// Measured, with the roles parsed from the preset:
//
//   * minimum circular hue distance from a chromatic role: 21.1°
//     (the roles sit at 0.0 / 43.3 / 158.1 / 213.1 / 270.0; neutral is
//     achromatic and has no hue to be near)
//   * the reserved arc — everything within 25° of `danger` (0.0) or `warning`
//     (43.3), i.e. [335,360]∪[0,68.3] — is empty. The entry that comes closest
//     to it is `#cc88aa` at hue exactly 330.0: 5.0° clear of the arc and 30.0°
//     from `danger`. (An earlier version of this comment claimed the band
//     started at 330 and was empty, which `#cc88aa` contradicted by exactly one
//     endpoint. The arc is now stated as the ≥25° rule the test actually
//     enforces, and the nearest entry is asserted by hex and by hue so a
//     recited number cannot drift from the palette again.)
//   * saturation 0.397–0.404, against every chromatic role's 0.644–0.964
//
// SATURATION IS THE CHANNEL THAT MATTERS, not hue. Hue distance alone would
// still let a fully saturated green identity chip out-shout a real alarm
// sitting beside it; the 24-point saturation drop is what keeps an identity
// chip quiet. Both are asserted.
//
// CONTRAST IS MEASURED ON BOTH BACKDROPS a chip actually sits on. The identity
// chip tints its own colour to 10% over the composited backdrop, exactly as the
// role chips do:
//
//   app #0f0f14 → campaign panel (gray-800/60) → row card (gray-900/50)
//
// giving 5.20–5.24:1 on the panel and 5.40–5.44:1 on the row card, against the
// 4.5 AA floor. The compositing model is trustworthy because it reproduces all
// six RECORDED role ratios exactly — success 7.14 (recorded 7.1), info 5.54
// (5.5), accent 5.37 (5.4), warning 8.09 (8.1), danger 5.23 (5.2), neutral 5.57
// (5.6) — so it is being checked against numbers this repo already measured in
// a real browser rather than against itself.
//
// NOTHING IS ADDED TO design-tokens/tokens.json. These are not theme tokens:
// they are a closed, generated-from-nothing ramp used by one component through
// inline styles, and adding them to the token source would make
// `gen.mjs --check` — a drift gate about the THEME — fire on a palette that no
// other surface consumes.
//
// COLOUR IS ALWAYS REDUNDANT. The chip draws its label as text regardless, so
// a colour-blind operator loses grouping-at-a-glance and loses nothing else.
// That is what makes a 12-colour ramp acceptable for a fleet with far more than
// 12 identities: the colour is a scan filter ("these two MIGHT be the same —
// read the text"), never an identifier.

/**
 * Twelve hues at a common lightness and a common low saturation.
 *
 * Ordered around the wheel so that adjacent slots are adjacent hues; the hash
 * below is what makes the assignment unpredictable, so ordering here costs
 * nothing and makes the disjointness argument readable.
 */
export const IDENTITY_PALETTE: readonly string[] = [
  '#8aa747',
  '#6bad4a',
  '#4cb14f',
  '#4bb066',
  '#49abab',
  '#5da7b9',
  '#9399d1',
  '#9e96d2',
  '#c388cc',
  '#cb85c4',
  '#cc87b7',
  '#cc88aa',
]

/** The background tint alpha, matching the role chips' `/10`. */
const TINT_ALPHA = 0.1

/**
 * FNV-1a, 32-bit, over UTF-16 code units.
 *
 * `Math.imul` rather than `*` because the multiply must wrap at 32 bits;
 * JavaScript's `*` would silently go through a double and lose the low bits
 * that carry the avalanche.
 */
function fnv1a(text: string): number {
  let h = 0x811c9dc5
  for (let i = 0; i < text.length; i += 1) {
    h ^= text.charCodeAt(i)
    h = Math.imul(h, 0x01000193)
  }
  return h >>> 0
}

/**
 * murmur3's `fmix32` finalizer.
 *
 * HERE FOR A STRUCTURAL REASON, NOT AN EMPIRICAL ONE. `% 12` reads the LOW
 * bits, and FNV-1a's low bits are its weakest — the last multiply barely
 * diffuses them. The finalizer's shift/multiply rounds push high-bit entropy
 * down. It was NOT chosen because it scored better on today's identity tokens:
 * both variants are uniform by chi-square over 120k keys, and picking on that
 * basis would be fitting a hash to a fleet that changes hourly.
 */
function fmix32(input: number): number {
  let h = input
  h ^= h >>> 16
  h = Math.imul(h, 0x85ebca6b)
  h ^= h >>> 13
  h = Math.imul(h, 0xc2b2ae35)
  h ^= h >>> 16
  return h >>> 0
}

/** Which palette slot a token lands in. Pure: no clock, no locale, no random. */
export function paletteIndex(key: string): number {
  return fmix32(fnv1a(key)) % IDENTITY_PALETTE.length
}

export interface IdentityStyle {
  /** The palette slot, or null when the token asks for no colour. */
  index: number | null
  /** The chip's foreground, or null when the token asks for no colour. */
  colour: string | null
  /** That colour at the chips' shared 10% tint, for the background. */
  tint: string | null
}

/**
 * The colour an identity token resolves to.
 *
 * An EMPTY token is not an error and not a missing value — it is the producer
 * saying "this is an identity chip and it has no colour", which the renderer
 * draws outlined and grey. Returning nulls rather than a palette entry is what
 * keeps that distinction all the way to the DOM.
 */
export function identityStyle(key: string): IdentityStyle {
  if (!key) return { index: null, colour: null, tint: null }
  const index = paletteIndex(key)
  const colour = IDENTITY_PALETTE[index]
  return { index, colour, tint: rgba(colour, TINT_ALPHA) }
}

/** `#rrggbb` at an alpha, as a CSS colour. */
export function rgba(hex: string, alpha: number): string {
  const r = parseInt(hex.slice(1, 3), 16)
  const g = parseInt(hex.slice(3, 5), 16)
  const b = parseInt(hex.slice(5, 7), 16)
  return `rgba(${r}, ${g}, ${b}, ${alpha})`
}

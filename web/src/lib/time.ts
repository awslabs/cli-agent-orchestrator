// The dashboard's two timestamp formatters, lifted out of DashboardHome so the
// annotation renderer uses the SAME ones rather than a second implementation
// that would drift. Byte-for-byte the functions that were there; nothing about
// their behaviour changed in the move.

export function fmtRel(dateStr: string | null | undefined): string | null {
  if (!dateStr) return null
  const d = new Date(dateStr)
  if (isNaN(d.getTime())) return null
  const diff = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000))
  if (diff < 60) return 'just now'
  const m = Math.floor(diff / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  const rm = m % 60
  if (h < 24) return rm ? `${h}h ${rm}m ago` : `${h}h ago`
  const days = Math.floor(h / 24)
  const rh = h % 24
  return rh ? `${days}d ${rh}h ago` : `${days}d ago`
}

export function fmtAbs(dateStr: string | null | undefined): string | null {
  if (!dateStr) return null
  const d = new Date(dateStr)
  if (isNaN(d.getTime())) return null
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

/**
 * The same elapsed time as `fmtRel`, without the trailing " ago" — for a chip
 * that already reads as a duration ("waiting · 2d 4h"). Derived from `fmtRel`
 * rather than recomputed, so the chip and its hover can never disagree about
 * how old something is.
 */
export function fmtAge(dateStr: string | null | undefined): string | null {
  const rel = fmtRel(dateStr)
  if (rel === null) return null
  if (rel === 'just now') return 'now'
  return rel.replace(/ ago$/, '')
}

/**
 * An ISO-8601 date or date-time, and nothing else.
 *
 * `new Date(x)` is far too willing: `new Date('12')` is a valid Date in 2001,
 * so "does it parse?" would relativise a round number or a version string into
 * a confidently wrong age. Requiring the ISO SHAPE is a structural test with no
 * vocabulary in it — which is the point, because the alternative is a list of
 * key names the conductor is then not allowed to rename.
 */
const ISO_SHAPE = /^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?)?$/

/** The epoch for an ISO-8601 timestamp, or null if this is not one. */
export function parseTimestamp(value: string | null | undefined): number | null {
  if (!value) return null
  const text = value.trim()
  if (!ISO_SHAPE.test(text)) return null
  const at = new Date(text).getTime()
  return isNaN(at) ? null : at
}

/** "<1 min", "12 min", "5 h", "2 d". Ages in whole seconds; never negative. */
export function fmtAge(seconds: number): string {
  const s = Math.max(0, seconds)
  if (s < 60) return '<1 min'
  if (s < 3600) return `${Math.round(s / 60)} min`
  if (s < 48 * 3600) return `${Math.round(s / 3600)} h`
  return `${Math.round(s / 86400)} d`
}

/** Seconds between an ISO time and `now` (an ISO time or ms). Unparseable input gives 0. */
export function ageSince(iso: string, now: string | number): number {
  const a = Date.parse(iso)
  const b = typeof now === 'number' ? now : Date.parse(now)
  if (Number.isNaN(a) || Number.isNaN(b)) return 0
  return Math.max(0, Math.floor((b - a) / 1000))
}

/** "age unknown" when CAO sent no readable time, else the age in words. */
export function ageWords(iso: string, now: string | number): string {
  return Number.isNaN(Date.parse(iso)) ? 'age unknown' : fmtAge(ageSince(iso, now))
}

export const hours = (h: number): string => (Number.isInteger(h) ? String(h) : String(Math.round(h * 100) / 100))

/** Display name for a closed helper: CAO keeps no name, so use the first four id characters. */
export function helperName(label: string | null, id: string): string {
  return label ?? `helper ${id.slice(0, 4)}`
}

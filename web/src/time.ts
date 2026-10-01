const HAS_ZONE = /(?:[zZ]|[+-]\d\d:?\d\d)$/

export function parseServerTime(value: string): Date {
  return new Date(HAS_ZONE.test(value) ? value : value + 'Z')
}

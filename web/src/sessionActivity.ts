/** Read freshness is independent of the last state transition or user input. */
export const SESSION_ACTIVITY_TTL_MS = 120_000
export type ActivityStatus = 'IDLE' | 'PROCESSING' | 'COMPLETED' | 'WAITING_USER_ANSWER' | 'ERROR' | 'UNKNOWN'
export interface ReadingTime { wall: number; mono: number }
export interface StatusObservation extends ReadingTime { status: ActivityStatus }
export interface ActivityMembership extends ReadingTime { ids: string[] }

export function readingTime(): ReadingTime {
  return { wall: Date.now(), mono: performance.now() }
}

export function isFresh(reading: ReadingTime | null | undefined, now: ReadingTime): boolean {
  // Wall time catches suspension; monotonic time prevents a clock rollback
  // from extending a reading's life. Exact TTL is already expired.
  return !!reading && Math.max(now.wall - reading.wall, now.mono - reading.mono) < SESSION_ACTIVITY_TTL_MS
}

export function normalizeActivityStatus(status: unknown): ActivityStatus {
  const normalized = typeof status === 'string' ? status.toUpperCase() : ''
  return ['IDLE', 'PROCESSING', 'COMPLETED', 'WAITING_USER_ANSWER', 'ERROR'].includes(normalized)
    ? normalized as ActivityStatus : 'UNKNOWN'
}

export function effectiveStatus(observation: StatusObservation | undefined, now: ReadingTime): ActivityStatus {
  return isFresh(observation, now) ? observation!.status : 'UNKNOWN'
}

export function aggregateSessionStatus(membership: ActivityMembership | null, observations: Map<string, StatusObservation>, now: ReadingTime): ActivityStatus {
  if (!isFresh(membership, now)) return 'UNKNOWN'
  const statuses = membership!.ids.map(id => effectiveStatus(observations.get(id), now))
  if (statuses.includes('WAITING_USER_ANSWER')) return 'WAITING_USER_ANSWER'
  if (statuses.includes('PROCESSING')) return 'PROCESSING'
  if (statuses.includes('UNKNOWN')) return 'UNKNOWN'
  if (statuses.includes('ERROR')) return 'ERROR'
  if (statuses.length > 0 && statuses.every(s => s === 'COMPLETED')) return 'COMPLETED'
  return 'IDLE'
}

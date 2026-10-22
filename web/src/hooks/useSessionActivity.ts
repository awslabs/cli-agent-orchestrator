import { useEffect, useRef, useState } from 'react'
import { api, ApiError, Session } from '../api'
import { ActivityMembership, ActivityStatus, StatusObservation, aggregateSessionStatus, effectiveStatus, isFresh, normalizeActivityStatus, readingTime, SESSION_ACTIVITY_TTL_MS } from '../sessionActivity'

interface Member {
  observation?: StatusObservation
  busy: boolean
}
interface Entry {
  name: string
  incarnation: string
  membership: ActivityMembership | null
  members: Map<string, Member>
  busy: boolean
}

/** Agents-only reader. Object identity is the generation token for each session
 * and member: removal/recreation invalidates pending callbacks, including ABA.
 * Failures never update timestamps. Each independent key has one request in flight.
 */
export function useSessionActivity(sessions: Session[], fetchSessions: () => Promise<void>) {
  const entries = useRef(new Map<string, Entry>())
  const controller = useRef<{ reconcile: () => void; invalidate: (name: string) => void } | null>(null)
  const latest = useRef({ sessions, fetchSessions })
  latest.current = { sessions, fetchSessions }
  const [, render] = useState(0)

  useEffect(() => {
    let mounted = true
    // Keep transport locks across local invalidations too. A confirmed mutation
    // queues a fresh read after the obsolete request settles, without overlap.
    const memberFlights = new Set<string>()
    const statusFlights = new Set<string>()
    let expiry: ReturnType<typeof setTimeout> | undefined
    const current = (entry: Entry) => mounted && entries.current.get(entry.name) === entry
      && latest.current.sessions.some(s => s.id === entry.name && s.session_incarnation_id === entry.incarnation)
    const changed = () => {
      if (!mounted) return
      render(n => n + 1)
      clearTimeout(expiry)
      const now = readingTime()
      let next = Infinity
      const consider = (time: ActivityMembership | StatusObservation | null | undefined) => {
        if (!time || !isFresh(time, now)) return
        next = Math.min(next, SESSION_ACTIVITY_TTL_MS - Math.max(now.wall - time.wall, now.mono - time.mono))
      }
      for (const entry of entries.current.values()) {
        consider(entry.membership)
        entry.members.forEach(member => consider(member.observation))
      }
      if (Number.isFinite(next)) expiry = setTimeout(changed, Math.max(1, next))
    }
    const contradict = (entry: Entry) => {
      if (!current(entry)) return
      entry.membership = null
      entry.members.clear()
      changed()
      void latest.current.fetchSessions()
    }
    const readStatus = async (entry: Entry, id: string, member: Member) => {
      const key = JSON.stringify([entry.name, entry.incarnation, id])
      if (!current(entry) || member.busy || statusFlights.has(key) || !isFresh(entry.membership, readingTime())) return
      statusFlights.add(key)
      member.busy = true
      const started = readingTime()
      const valid = () => current(entry) && entry.members.get(id) === member && isFresh(started, readingTime())
      try {
        const result = await api.getTerminalStatusReading(id)
        if (!valid()) return
        if (result.id !== id || result.session_name !== entry.name || result.session_incarnation_id !== entry.incarnation) {
          contradict(entry)
          return
        }
        member.observation = { ...readingTime(), status: normalizeActivityStatus(result.status) }
        changed()
      } catch (error) {
        if (valid() && (error as ApiError)?.status === 404) void readMembers(entry)
      } finally {
        statusFlights.delete(key)
        member.busy = false
        const next = entries.current.get(entry.name)
        const nextMember = next?.members.get(id)
        if (mounted && next && next.incarnation === entry.incarnation && nextMember && nextMember !== member) void readStatus(next, id, nextMember)
      }
    }
    const readMembers = async (entry: Entry) => {
      const key = JSON.stringify([entry.name, entry.incarnation])
      if (!current(entry) || entry.busy || memberFlights.has(key)) return
      memberFlights.add(key)
      entry.busy = true
      const started = readingTime()
      try {
        const result = await api.getSessionMembership(entry.name, entry.incarnation)
        if (!current(entry) || !isFresh(started, readingTime())) return
        if (result.session.id !== entry.name || result.session.name !== entry.name || result.session.session_incarnation_id !== entry.incarnation
          || result.terminals.some(t => t.session_incarnation_id !== entry.incarnation)) {
          contradict(entry)
          return
        }
        const ids = result.terminals.map(t => t.id)
        for (const id of entry.members.keys()) if (!ids.includes(id)) entry.members.delete(id)
        entry.membership = { ...readingTime(), ids }
        for (const id of ids) {
          if (!entry.members.has(id)) {
            const member: Member = { busy: false }
            entry.members.set(id, member)
            void readStatus(entry, id, member)
          }
        }
        changed()
      } catch (error) {
        if (!current(entry) || !isFresh(started, readingTime())) return
        const code = (error as ApiError)?.status
        if (code === 409 || code === 404) contradict(entry)
      } finally {
        memberFlights.delete(key)
        entry.busy = false
        const next = entries.current.get(entry.name)
        if (mounted && next && next !== entry && next.incarnation === entry.incarnation) void readMembers(next)
      }
    }
    const reconcile = () => {
      const sessions = latest.current.sessions
      for (const [name, entry] of entries.current) {
        if (!sessions.some(s => s.id === name && s.session_incarnation_id === entry.incarnation)) entries.current.delete(name)
      }
      for (const session of sessions) {
        if (!session.session_incarnation_id || entries.current.has(session.id)) continue
        const entry: Entry = { name: session.id, incarnation: session.session_incarnation_id, membership: null, members: new Map(), busy: false }
        entries.current.set(session.id, entry)
        void readMembers(entry)
      }
      changed()
    }
    controller.current = {
      reconcile,
      invalidate: name => {
        // Confirmed mutations invalidate even a membership request already in flight.
        entries.current.delete(name)
        reconcile()
      },
    }
    reconcile()
    void latest.current.fetchSessions()
    const membersTimer = setInterval(() => entries.current.forEach(entry => { void readMembers(entry) }), 5000)
    const statesTimer = setInterval(() => entries.current.forEach(entry => entry.members.forEach((member, id) => { void readStatus(entry, id, member) })), 3000)
    const resume = () => {
      if (document.visibilityState === 'hidden') return
      changed()
      entries.current.forEach(entry => {
        void readMembers(entry)
        entry.members.forEach((member, id) => { void readStatus(entry, id, member) })
      })
    }
    window.addEventListener('focus', resume)
    document.addEventListener('visibilitychange', resume)
    return () => {
      mounted = false
      clearTimeout(expiry)
      clearInterval(membersTimer)
      clearInterval(statesTimer)
      window.removeEventListener('focus', resume)
      document.removeEventListener('visibilitychange', resume)
      controller.current = null
      entries.current.clear()
    }
  }, [])

  const sessionKeys = JSON.stringify(sessions.map(s => [s.id, s.session_incarnation_id]))
  useEffect(() => { controller.current?.reconcile() }, [sessionKeys])
  const now = readingTime()
  const sessionStatuses: Record<string, ActivityStatus> = {}
  const terminalStatuses: Record<string, ActivityStatus> = {}
  for (const session of sessions) {
    const entry = entries.current.get(session.id)
    sessionStatuses[session.id] = entry && entry.incarnation === session.session_incarnation_id
      ? aggregateSessionStatus(entry.membership, new Map([...entry.members].flatMap(([id, m]) => m.observation ? [[id, m.observation] as const] : [])), now)
      : 'UNKNOWN'
    entry?.members.forEach((m, id) => { terminalStatuses[id] = effectiveStatus(m.observation, now) })
  }
  return { sessionStatuses, terminalStatuses, invalidate: (name: string) => controller.current?.invalidate(name) }
}

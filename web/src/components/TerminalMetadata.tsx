// Shared presentation of the evidence behind a terminal's status chip.
//
// The hover card and the row-level Work state panel deliberately consume the
// SAME sections. A short hover may select fewer sections, but it may not
// reinterpret a field: an operator should never get two explanations for one
// status depending on which surface they opened.

import type { TerminalMeta, TerminalStatusSignal } from '../api'
import { fmtAbs, fmtRel } from '../lib/time'
import { STATUS_CONFIG, UNKNOWN_CONFIG } from '../status.generated'

export interface MetadataEntry {
  label: string
  value: string
}

export interface MetadataSection {
  id: 'reachability' | 'identity' | 'succession'
  label: string
  entries: MetadataEntry[]
}

const STATUS_MEANINGS: Record<string, string> = {
  IDLE: 'A provider detector currently reads the terminal as ready or idle. This does not prove that the worker has no assigned work.',
  PROCESSING: 'A provider detector currently reads the terminal as working. This is operational evidence, not a durable task or completion record.',
  COMPLETED: 'A provider detector reports completion. Durable reporting, collection, and task acceptance remain separate.',
  WAITING_USER_ANSWER: 'The provider appears to be waiting for external input before it can continue.',
  ERROR: 'The provider detector found an error state. The derivation reason and evidence below explain what was observed.',
  STOPPED: 'The terminal lifecycle says this worker was stopped; it is not currently running.',
  DEAD: 'The recorded pane is absent, so no provider turn state can be observed.',
  SUPERSEDED: 'A newer terminal generation replaced this one. Delivery and lifecycle actions should target the successor.',
  NOT_FIFO_MONITORED: 'The pane is managed and live, but the available classifiers cannot determine the agent\'s current turn state.',
  UNKNOWN: 'Available evidence cannot classify the terminal, is missing, or is contradictory.',
}

function human(value: string): string {
  return value.replace(/[_.-]+/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
}

function duration(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds))
  if (whole < 60) return `${whole}s`
  const minutes = Math.floor(whole / 60)
  if (minutes < 60) return `${minutes}m ${whole % 60}s`
  const hours = Math.floor(minutes / 60)
  const remainingMinutes = minutes % 60
  if (hours < 24) return `${hours}h${remainingMinutes ? ` ${remainingMinutes}m` : ''}`
  const days = Math.floor(hours / 24)
  const remainingHours = hours % 24
  return `${days}d${remainingHours ? ` ${remainingHours}h` : ''}`
}

function scalar(value: TerminalStatusSignal['value']): string | null {
  if (value === undefined || value === null) return null
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  return String(value)
}

/** Human-readable without closing the signal vocabulary to future producers. */
export function statusSignalText(signal: TerminalStatusSignal): string {
  let observed = scalar(signal.value)
  if (typeof signal.value === 'number' && signal.name === 'liveness') {
    observed = signal.value === 0
      ? 'pane changed since the previous sample'
      : `no pane rendering change for ${duration(signal.value)}`
  } else if (typeof signal.value === 'number' && signal.name === 'activity') {
    observed = `${duration(signal.value)} since CAO last sent input`
  } else if (observed && (signal.name === 'fifo' || signal.name === 'screen')) {
    observed = human(observed)
  }

  return [human(signal.state), observed, signal.detail || null].filter(Boolean).join(' · ')
}

export function effectiveStatus(terminal: TerminalMeta, status?: string | null): string {
  return (status ?? terminal.status ?? 'unknown').toUpperCase()
}

export function terminalMetadataSections(
  terminal: TerminalMeta,
  status?: string | null,
): MetadataSection[] {
  const normalized = effectiveStatus(terminal, status)
  const projectedStatus = effectiveStatus(terminal, terminal.status)
  const statusConfig = STATUS_CONFIG[normalized] || UNKNOWN_CONFIG
  const evidencePending = normalized !== projectedStatus
  const lastInput = terminal.last_active
    ? [fmtRel(terminal.last_active), fmtAbs(terminal.last_active)].filter(Boolean).join(' · ')
    : 'Not recorded'

  const reachability: MetadataEntry[] = [
    { label: 'Status', value: statusConfig.label },
    { label: 'Status key', value: normalized.toLowerCase() },
    { label: 'Meaning', value: STATUS_MEANINGS[normalized] ?? 'A server-projected terminal state. See the reason and evidence for its exact meaning.' },
    ...(evidencePending ? [{ label: 'Evidence snapshot status', value: projectedStatus.toLowerCase() }] : []),
    {
      label: 'Confidence',
      value: evidencePending ? 'Pending detailed-evidence refresh' : human(terminal.status_confidence || 'unknown'),
    },
    {
      label: 'Reason',
      value: evidencePending
        ? `The lightweight status poll advanced from ${projectedStatus.toLowerCase()}; the detailed signals below belong to that prior projection and will refresh with the session row.`
        : terminal.status_reason || 'No derivation reason was published',
    },
    { label: 'Lifecycle', value: human(terminal.lifecycle_state || 'unknown') },
    ...(terminal.lifecycle_reason ? [{ label: 'Lifecycle reason', value: terminal.lifecycle_reason }] : []),
    { label: 'Wedged', value: terminal.wedged ? 'Yes — independent quiet clocks contradict a working claim' : 'No' },
    { label: 'FIFO classifier', value: terminal.fifo_monitored ? 'Monitored' : 'Not monitored' },
    { label: 'Last CAO input', value: lastInput || terminal.last_active || 'Not recorded' },
    ...(terminal.status_signals || []).map(signal => ({
      label: `${human(signal.name)} signal`,
      value: statusSignalText(signal),
    })),
  ]

  const identity: MetadataEntry[] = [
    { label: 'Terminal', value: terminal.terminal_id || terminal.id },
    { label: 'Profile', value: terminal.agent_profile || 'Not declared' },
    { label: 'Provider', value: terminal.provider || 'Unknown' },
    { label: 'Protocol', value: terminal.protocol_vintage || 'Unknown' },
    { label: 'Generation', value: terminal.generation || 'Not recorded' },
    { label: 'Callback generation', value: terminal.callback_target_generation || 'Not recorded' },
    { label: 'Caller', value: terminal.caller_id || 'Not recorded' },
  ]

  const succession: MetadataEntry[] = []
  if (terminal.superseded_by_terminal_id) {
    succession.push({ label: 'Successor terminal', value: terminal.superseded_by_terminal_id })
  }
  if (terminal.superseded_by_generation) {
    succession.push({ label: 'Successor generation', value: terminal.superseded_by_generation })
  }

  return [
    { id: 'reachability', label: 'Status & liveness evidence', entries: reachability },
    { id: 'identity', label: 'Worker identity & provenance', entries: identity },
    ...(succession.length > 0
      ? [{ id: 'succession' as const, label: 'Succession', entries: succession }]
      : []),
  ]
}

export function MetadataRows({ entries, dense = false }: { entries: MetadataEntry[]; dense?: boolean }) {
  return (
    <dl className={`grid grid-cols-[auto,minmax(0,1fr)] gap-x-3 ${dense ? 'gap-y-0.5' : 'gap-y-1'}`}>
      {entries.map(({ label, value }, index) => (
        <div key={`${label}:${index}`} className="contents">
          <dt className="text-[10px] uppercase tracking-wide text-gray-400 whitespace-nowrap">{label}</dt>
          <dd className="text-[11px] text-gray-200 break-words">{value}</dd>
        </div>
      ))}
    </dl>
  )
}

export function terminalMetadataToText(
  terminal: TerminalMeta,
  status?: string | null,
): string[] {
  const lines: string[] = []
  for (const section of terminalMetadataSections(terminal, status)) {
    lines.push(section.label)
    for (const entry of section.entries) lines.push(`  ${entry.label}: ${entry.value}`)
    lines.push('')
  }
  return lines
}

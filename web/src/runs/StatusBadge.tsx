type TerminalStatus = 'IDLE' | 'PROCESSING' | 'COMPLETED' | 'WAITING_USER_ANSWER' | 'ERROR' | string | null

interface StatusStyle {
  label: string        // technical label (raw enum, Agents/Flows surfaces)
  plain: string        // plain-English label for the Runs surface
  hex: string          // the sacred status color (§1.3)
  dotClass: string
  bgClass: string
  textClass: string
  pulse?: boolean
}

/**
 * The six sacred status mappings (Build Spec §1.3). One enum drives every
 * badge, dot, node, and pulse in the app. Color is meaning — never reuse
 * these hues decoratively.
 */
export const STATUS_CONFIG: Record<string, StatusStyle> = {
  IDLE: {
    label: 'IDLE',
    plain: 'Ready',
    hex: '#34d399',
    dotClass: 'bg-emerald-400',
    bgClass: 'bg-emerald-400/10',
    textClass: 'text-emerald-400',
  },
  PROCESSING: {
    label: 'PROCESSING',
    plain: 'Working',
    hex: '#60a5fa',
    dotClass: 'bg-blue-400',
    bgClass: 'bg-blue-400/10',
    textClass: 'text-blue-400',
    pulse: true,
  },
  COMPLETED: {
    label: 'COMPLETED',
    plain: 'Done',
    hex: '#c084fc',
    dotClass: 'bg-purple-400',
    bgClass: 'bg-purple-400/10',
    textClass: 'text-purple-400',
  },
  WAITING_USER_ANSWER: {
    label: 'WAITING_USER_ANSWER',
    plain: 'Needs you',
    hex: '#fbbf24',
    dotClass: 'bg-amber-400',
    bgClass: 'bg-amber-400/[0.12]',
    textClass: 'text-amber-400',
    pulse: true,
  },
  ERROR: {
    label: 'ERROR',
    plain: 'Broken',
    hex: '#f87171',
    dotClass: 'bg-red-400',
    bgClass: 'bg-red-400/10',
    textClass: 'text-red-400',
  },
  UNKNOWN: {
    label: 'UNKNOWN',
    plain: 'Starting',
    hex: '#6b7280',
    dotClass: 'bg-gray-500',
    bgClass: 'bg-gray-500/10',
    textClass: 'text-gray-500',
  },
}

export function statusStyle(status: TerminalStatus): StatusStyle {
  const normalized = status ? status.toUpperCase() : 'UNKNOWN'
  return STATUS_CONFIG[normalized] || STATUS_CONFIG.UNKNOWN
}

/**
 * Status pill: 7px status dot + label (Build Spec §1.3).
 * - technical={true} (default — Agents/Flows): raw enum, mono, 11px.
 * - technical={false} (Runs board): plain English, sans, 12px.
 */
export function StatusBadge({ status, technical = true }: {
  status: TerminalStatus
  technical?: boolean
}) {
  const config = statusStyle(status)
  return (
    <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full ${config.bgClass}`}>
      <span
        className={`rounded-full ${config.dotClass} ${config.pulse ? 'pulse-dot' : ''}`}
        style={{ width: 7, height: 7 }}
      />
      <span
        className={`${config.textClass} font-medium ${technical ? 'font-mono' : ''}`}
        style={{ fontSize: technical ? 11 : 12 }}
      >
        {technical ? config.label : config.plain}
      </span>
    </span>
  )
}

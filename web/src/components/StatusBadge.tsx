type TerminalStatus = 'IDLE' | 'PROCESSING' | 'COMPLETED' | 'WAITING_USER_ANSWER' | 'ERROR' | string | null

interface StatusStyle {
  label: string
  dotClass: string
  bgClass: string
  textClass: string
  pulse?: boolean
}

const STATUS_CONFIG: Record<string, StatusStyle> = {
  IDLE: {
    label: 'Idle',
    dotClass: 'bg-emerald-400',
    bgClass: 'bg-emerald-400/10',
    textClass: 'text-emerald-400',
  },
  PROCESSING: {
    label: 'Processing',
    dotClass: 'bg-blue-400',
    bgClass: 'bg-blue-400/10',
    textClass: 'text-blue-400',
    pulse: true,
  },
  COMPLETED: {
    label: 'Completed',
    dotClass: 'bg-purple-400',
    bgClass: 'bg-purple-400/10',
    textClass: 'text-purple-400',
  },
  WAITING_USER_ANSWER: {
    label: 'Awaiting Input',
    dotClass: 'bg-amber-400',
    bgClass: 'bg-amber-400/10',
    textClass: 'text-amber-400',
  },
  ERROR: {
    label: 'Error',
    dotClass: 'bg-red-400',
    bgClass: 'bg-red-400/10',
    textClass: 'text-red-400',
  },
}

const UNKNOWN_CONFIG: StatusStyle = {
  label: 'Unknown',
  dotClass: 'bg-gray-500',
  bgClass: 'bg-gray-500/10',
  textClass: 'text-gray-500',
}

export function StatusBadge({ status }: { status: TerminalStatus }) {
  const normalized = status ? status.toUpperCase() : null
  const config = (normalized && STATUS_CONFIG[normalized]) || UNKNOWN_CONFIG

  return (
    <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full ${config.bgClass}`}>
      <span className={`w-2 h-2 rounded-full ${config.dotClass} ${config.pulse ? 'animate-pulse' : ''}`} />
      <span className={`text-xs font-medium ${config.textClass}`}>{config.label}</span>
    </span>
  )
}

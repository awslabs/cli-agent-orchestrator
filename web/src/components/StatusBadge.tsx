// StatusBadge — terminal status pill for the web dashboard.
//
// STATUS_CONFIG / UNKNOWN_CONFIG are generated from the shared design-token SSOT
// (design-tokens/status.json + tokens.json) via `node design-tokens/gen.mjs`.
// Do not hand-edit the status taxonomy here — edit the JSON and regenerate.
import { STATUS_CONFIG, UNKNOWN_CONFIG } from '../status.generated'

export { STATUS_CONFIG }

type TerminalStatus = 'IDLE' | 'PROCESSING' | 'COMPLETED' | 'WAITING_USER_ANSWER' | 'ERROR' | string | null

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

import { ProfileValidationMessage } from '../api'
import { AlertTriangle, CircleAlert, MoreHorizontal } from 'lucide-react'

/**
 * The backend's omission marker, verbatim from
 * services/profile_validator.py::_OMISSION_MESSAGE. The truncation contract:
 * at most 100 findings are returned INCLUDING this marker; when present it
 * appears exactly once, last, and its severity matches the omitted producer
 * (error when error-severity findings were dropped, warning when only
 * advisory findings were). The UI must render it as a truncation notice, not
 * as the 100th finding.
 */
export const OMISSION_MESSAGE = 'Additional validation findings omitted.'

function isOmissionMarker(f: ProfileValidationMessage, index: number, all: ProfileValidationMessage[]): boolean {
  return f.message === OMISSION_MESSAGE && index === all.length - 1
}

/**
 * Bounded findings list shared by the create and editor modals. Errors render
 * red, warnings amber; a trailing omission marker renders as a dimmed
 * truncation row whose colour still reflects its severity, so a truncated
 * error tail is not mistaken for a merely-advisory one.
 */
export function ValidationFindings({ findings }: { findings: ProfileValidationMessage[] }) {
  if (findings.length === 0) return null

  const marker = findings.find((f, i) => isOmissionMarker(f, i, findings))
  const regular = marker ? findings.slice(0, -1) : findings
  const errorCount = regular.filter(f => f.severity === 'error').length
  const warningCount = regular.length - errorCount

  return (
    <div className="border border-gray-700/60 rounded-lg overflow-hidden" data-testid="validation-findings">
      <div className="px-3 py-2 bg-gray-800/60 text-xs text-gray-400 flex items-center gap-3">
        <span className="font-medium text-gray-300">Validation findings</span>
        {errorCount > 0 && <span className="text-red-400">{errorCount} error{errorCount !== 1 ? 's' : ''}</span>}
        {warningCount > 0 && <span className="text-amber-400">{warningCount} warning{warningCount !== 1 ? 's' : ''}</span>}
        {marker && <span className="text-gray-500 italic">list truncated</span>}
      </div>
      <ul className="py-1 max-h-52 overflow-y-auto">
        {regular.map((f, i) => (
          <li key={i} className="px-3 py-1.5 flex items-start gap-2 text-xs">
            <span aria-hidden="true" className={`mt-px shrink-0 ${f.severity === 'error' ? 'text-red-400' : 'text-amber-400'}`}>•</span>
            {f.severity === 'error' ? (
              <CircleAlert size={13} className="text-red-400 mt-0.5 shrink-0" aria-label="error" />
            ) : (
              <AlertTriangle size={13} className="text-amber-400 mt-0.5 shrink-0" aria-label="warning" />
            )}
            <div className="min-w-0">
              {f.path && <span className="font-mono text-gray-500 mr-1.5">{f.path}</span>}
              <span className={f.severity === 'error' ? 'text-red-300' : 'text-amber-300'}>{f.message}</span>
            </div>
          </li>
        ))}
        {marker && (
          <li
            data-testid="omission-marker"
            className={`px-3 py-2 flex items-center gap-2 text-xs italic ${
              marker.severity === 'error' ? 'text-red-400/80 bg-red-950/20' : 'text-amber-400/80 bg-amber-950/10'
            }`}
          >
            <MoreHorizontal size={13} className="shrink-0" />
            {marker.message}
            {marker.severity === 'error' && <span className="not-italic text-[10px] uppercase tracking-wide ml-1">(errors among omitted)</span>}
          </li>
        )}
      </ul>
    </div>
  )
}

import { describe, it, expect, vi, afterAll, afterEach } from 'vitest'
import { cleanup, render } from '@testing-library/react'
// vi.mock is hoisted above this import, so DashboardHome loads against the
// trimmed STATUS_CONFIG below.
import { DashboardHome } from '../components/DashboardHome'

// Pins the constraint that keeps STATUS_ORDER honest: every entry must have a
// counterpart in the generated STATUS_CONFIG (or be the hand-added 'UNKNOWN').
// STATUS_META is built only from STATUS_CONFIG, and DashboardHome dereferences
// `STATUS_META[s].dot` unguarded in both StatusSummary and the filter pill row,
// so an entry with no counterpart does not degrade — it throws, and the whole
// dashboard goes blank.
//
// This exists so nobody adds a status to STATUS_ORDER without first adding it
// to the generated token source. That change typechecks and reads as obvious;
// it is a hard render error. Proven DEAD and SUPERSEDED dispositions therefore
// live in both sources, while unproven `unknown-liveness` still folds through
// the residual UNKNOWN path.
//
// The simulation: keep STATUS_ORDER exactly as shipped and take
// NOT_FIFO_MONITORED *out* of the generated config, which is the same shape as
// adding an entry the generator never produced.
vi.mock('../components/TerminalView', () => ({ TerminalView: () => null }))
vi.mock('../status.generated', () => ({
  STATUS_CONFIG: {
    IDLE: { label: 'Idle', dotClass: 'bg-cao-success', bgClass: 'bg-cao-success/10', textClass: 'text-cao-success' },
    PROCESSING: { label: 'Processing', dotClass: 'bg-cao-info', bgClass: 'bg-cao-info/10', textClass: 'text-cao-info', pulse: true },
    COMPLETED: { label: 'Completed', dotClass: 'bg-cao-accent', bgClass: 'bg-cao-accent/10', textClass: 'text-cao-accent' },
    WAITING_USER_ANSWER: { label: 'Awaiting Input', dotClass: 'bg-cao-warning', bgClass: 'bg-cao-warning/10', textClass: 'text-cao-warning' },
    ERROR: { label: 'Error', dotClass: 'bg-cao-danger', bgClass: 'bg-cao-danger/10', textClass: 'text-cao-danger' },
    STOPPED: { label: 'Stopped', dotClass: 'bg-cao-neutral', bgClass: 'bg-cao-neutral/10', textClass: 'text-cao-neutral' },
    DEAD: { label: 'Dead', dotClass: 'bg-cao-danger', bgClass: 'bg-cao-danger/10', textClass: 'text-cao-danger' },
    SUPERSEDED: { label: 'Superseded', dotClass: 'bg-cao-neutral', bgClass: 'bg-cao-neutral/10', textClass: 'text-cao-neutral' },
  },
  UNKNOWN_CONFIG: { label: 'Unknown', dotClass: 'bg-cao-neutral', bgClass: 'bg-cao-neutral/10', textClass: 'text-cao-neutral' },
}))

describe('STATUS_ORDER entries without a STATUS_CONFIG counterpart', () => {
  // React logs the caught render error; the throw is the assertion.
  const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {})

  afterEach(() => cleanup())
  afterAll(() => consoleSpy.mockRestore())

  it('cannot render: the unguarded STATUS_META[s].dot dereference throws', () => {
    // The status filter row renders unconditionally, with no session or
    // terminal data, so the throw needs nothing staged.
    expect(() => render(<DashboardHome onNavigate={() => {}} />)).toThrow(TypeError)
  })

  it('names the missing dot lookup, not some unrelated failure', () => {
    // V8 phrasing (`Cannot read properties of undefined (reading 'dot')`);
    // vitest runs on node here, so the property name is stable enough to pin.
    expect(() => render(<DashboardHome onNavigate={() => {}} />)).toThrow(/dot/)
  })
})

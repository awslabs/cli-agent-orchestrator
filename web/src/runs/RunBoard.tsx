import { useCallback, useEffect, useRef, useState } from 'react'
import { api, TerminalMeta, serverNow } from '../api'
import { useStore, FlowPulse } from '../store'
import { deriveRun, Run, RunMember, RunPhase } from '../orchestration'
import { StartRunWizard } from './StartRunWizard'
import { OutputViewer, stripAnsi } from './OutputViewer'
import { ConfirmModal } from './ConfirmModal'
import { SessionName } from './SessionName'
import { AgentAvatar } from './AgentAvatar'
import { StatusBadge } from './StatusBadge'
import { FlowGraph } from './FlowGraph'
import { Play, Trash2, MessageSquare, Eye, Send, RotateCcw } from 'lucide-react'

interface SessionTerminals {
  name: string
  terminals: TerminalMeta[]
}

/* ── plain-English helpers (Runs altitude: zero jargon — §1.4) ── */

const ACTIVITY: Record<string, string> = {
  PROCESSING: 'working on its task…',
  WAITING_USER_ANSWER: 'needs your answer',
  IDLE: 'ready for instructions',
  COMPLETED: 'finished its last task',
  ERROR: 'hit a problem',
  UNKNOWN: 'starting up…',
}

const PHASE_TO_STATUS: Record<RunPhase, string> = {
  starting: 'UNKNOWN',
  working: 'PROCESSING',
  needs_you: 'WAITING_USER_ANSWER',
  ready: 'IDLE',
  done: 'COMPLETED',
  problem: 'ERROR',
}

function relTime(iso: string | null | undefined): string | null {
  if (!iso) return null
  const t = new Date(iso).getTime()
  if (isNaN(t)) return null
  const diff = Math.max(0, Math.floor((serverNow() - t) / 60_000))
  if (diff < 1) return 'just now'
  if (diff < 60) return `${diff} min ago`
  const h = Math.floor(diff / 60)
  return h < 24 ? `${h} h ago` : `${Math.floor(h / 24)} d ago`
}

function runStartedAt(detail: SessionTerminals | undefined): string | null {
  if (!detail?.terminals.length) return null
  const times = detail.terminals.map(t => t.created_at).filter(Boolean) as string[]
  return times.sort()[0] || null
}

const ghostBtn =
  'flex items-center gap-1 text-xs px-2 py-1 rounded-lg text-[var(--t2)] hover:bg-[var(--hover)] transition-colors'

/* ── member row (§2.2 right column) ── */

function MemberRow({ member, onShow }: { member: RunMember; onShow: (m: RunMember) => void }) {
  return (
    <div className="flex items-center gap-3" style={{ minHeight: 40 }}>
      <AgentAvatar role={member.isPlanner ? 'planner' : member.profile} status={member.status} size={36} />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--t1)' }}>
            {member.isPlanner ? 'Planner' : member.profile}
          </span>
          <span style={{ fontSize: 11, color: 'var(--t4)' }}>{member.model || ''}</span>
        </div>
        <div className="truncate" style={{ fontSize: 12.5, color: 'var(--t2)' }}>
          {member.isPlanner && member.status === 'WAITING_USER_ANSWER'
            ? 'Paused — waiting for your answer'
            : ACTIVITY[member.status] || ACTIVITY.UNKNOWN}
        </div>
      </div>
      <StatusBadge status={member.status} technical={false} />
      <button onClick={() => onShow(member)} className={ghostBtn} title="Show what this agent said"
        data-testid={`show-${member.terminalId}`}>
        <Eye size={13} />
      </button>
    </div>
  )
}

/* ── inline question panel (§2.3) — the agent's question, answered in place ── */

function QuestionPanel({ member }: { member: RunMember }) {
  const { showSnackbar } = useStore()
  const [question, setQuestion] = useState('')
  const [answer, setAnswer] = useState('')
  const [sending, setSending] = useState(false)

  useEffect(() => {
    api.getTerminalOutput(member.terminalId, 'last')
      .then(r => {
        const text = stripAnsi(r.output || '').trim()
        setQuestion(text.slice(-600) || 'The agent is waiting for your input.')
      })
      .catch(() => setQuestion('The agent is waiting for your input.'))
  }, [member.terminalId])

  const send = async () => {
    if (!answer.trim()) return
    setSending(true)
    try {
      await api.sendInput(member.terminalId, answer.trim())
      showSnackbar({ type: 'success', message: 'Answer sent' })
      setAnswer('')
    } catch (e: any) {
      showSnackbar({ type: 'error', message: e.message || 'Could not send the answer' })
    } finally {
      setSending(false)
    }
  }

  return (
    <div className="rounded-lg p-3 mb-3"
      style={{ background: 'var(--page)', border: '1px solid rgba(251,191,36,.35)' }}>
      <div className="flex items-start gap-2 mb-2">
        <MessageSquare size={14} style={{ color: '#fbbf24', marginTop: 2 }} className="shrink-0" />
        <div style={{ fontSize: 12.5 }}>
          <span style={{ color: 'var(--t2)' }}>
            {member.isPlanner ? 'The Planner' : member.profile} asks:{' '}
          </span>
          <span style={{ color: '#fbbf24', whiteSpace: 'pre-wrap' }}>{question || 'Loading the question…'}</span>
        </div>
      </div>
      <div className="flex gap-2">
        <input
          value={answer}
          onChange={e => setAnswer(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && send()}
          placeholder="Type your answer…"
          className="flex-1 rounded-lg px-3 py-2 outline-none"
          style={{ background: 'var(--card)', border: '1px solid var(--border)', fontSize: 13, color: 'var(--t1)' }}
        />
        <button
          onClick={send}
          disabled={!answer.trim() || sending}
          className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-black font-medium disabled:opacity-40"
          style={{ background: '#fbbf24', fontSize: 12.5 }}
          data-testid={`answer-${member.terminalId}`}
        >
          <Send size={13} /> Send answer
        </button>
      </div>
    </div>
  )
}

/* ── run cards (§2.2–2.5) ── */

function RunCard({ run, label, startedAt, pulses, onDelete, onShow, onInstruct, onRetry }: {
  run: Run
  label?: string | null
  startedAt: string | null
  pulses: FlowPulse[]
  onDelete: (runId: string) => void
  onShow: (m: RunMember) => void
  onInstruct: (run: Run) => void
  onRetry: (run: Run) => void
}) {
  const borderColor =
    run.phase === 'needs_you' ? 'rgba(251,191,36,.35)'
    : run.phase === 'problem' ? 'rgba(248,113,113,.3)'
    : 'var(--border)'
  const started = relTime(startedAt)
  const members = [run.planner, ...run.workers].filter(Boolean) as RunMember[]

  /* finished → compact row (§2.5) */
  if (run.phase === 'done') {
    return (
      <div className="flex items-center gap-3 rounded-xl"
        style={{ background: 'var(--card)', border: '1px solid var(--border)', padding: 14 }}
        data-testid={`run-${run.runId}`}>
        <SessionName name={run.runId} label={label} className="font-medium text-sm" />
        <span data-testid={`phase-${run.runId}`}><StatusBadge status="COMPLETED" technical={false} /></span>
        <span style={{ fontSize: 12, color: 'var(--t3)' }}>{started ? `finished ${started}` : ''}</span>
        <span className="flex-1" />
        {run.planner && (
          <button onClick={() => onShow(run.planner!)} className={ghostBtn}>
            <Eye size={13} /> Show summary
          </button>
        )}
        <button onClick={() => onDelete(run.runId)} className={`${ghostBtn} hover:!text-red-400`}
          title="End this run" data-testid={`delete-${run.runId}`}>
          <Trash2 size={13} />
        </button>
      </div>
    )
  }

  const failed = members.find(m => m.status === 'ERROR')
  const waiting = run.needsYou[0]

  return (
    <div className="rounded-xl" style={{ background: 'var(--card)', border: `1px solid ${borderColor}`, padding: 20 }}
      data-testid={`run-${run.runId}`}>
      {/* header row */}
      <div className="flex items-center gap-3 mb-4 flex-wrap">
        <SessionName name={run.runId} label={label} className="font-semibold" />
        <span data-testid={`phase-${run.runId}`}>
          <StatusBadge status={PHASE_TO_STATUS[run.phase]} technical={false} />
        </span>
        {started && <span style={{ fontSize: 12, color: 'var(--t3)' }}>started {started}</span>}
        <span className="flex-1" />
        {run.planner && (
          <button onClick={() => onInstruct(run)} className={ghostBtn} title="Tell the planner something">
            <MessageSquare size={13} /> Instruct
          </button>
        )}
        <button onClick={() => onDelete(run.runId)} className={`${ghostBtn} hover:!text-red-400`}
          title="End this run" data-testid={`delete-${run.runId}`}>
          <Trash2 size={13} /> End
        </button>
      </div>

      {/* needs-you: the question, answerable right here (§2.3) */}
      {waiting && <QuestionPanel member={waiting} />}

      {/* broken: plain-language failure + recovery (§2.4) */}
      {run.phase === 'problem' && failed && (
        <div className="flex items-center gap-3 rounded-lg p-3 mb-3"
          style={{ background: 'var(--page)', border: '1px solid rgba(248,113,113,.3)' }}>
          <span style={{ fontSize: 12.5, color: '#f87171' }}>
            {(failed.isPlanner ? 'The Planner' : failed.profile)} hit a problem.
          </span>
          <span className="flex-1" />
          <button onClick={() => onShow(failed)} className={ghostBtn}>
            <Eye size={13} /> Show what happened
          </button>
          <button onClick={() => onRetry(run)} className={ghostBtn}>
            <RotateCcw size={13} /> Retry
          </button>
        </div>
      )}

      {/* body: mini graph + member list (§2.2) */}
      <div className="flex flex-wrap gap-5">
        <FlowGraph run={run} pulses={pulses} onShow={onShow} mini />
        <div className="flex-1 min-w-[260px] flex flex-col justify-center" style={{ gap: 14 }}>
          {members.map(m => <MemberRow key={m.terminalId} member={m} onShow={onShow} />)}
        </div>
      </div>
    </div>
  )
}

/* ── the board (§2) ── */

const PHASE_ORDER: Record<RunPhase, number> = {
  needs_you: 0, problem: 1, working: 2, starting: 3, ready: 4, done: 5,
}

export function RunBoard() {
  const { sessions, terminalStatuses, deleteSession, showSnackbar, setTerminalStatus, flowPulses } = useStore()
  const [details, setDetails] = useState<SessionTerminals[]>([])
  const [wizardOpen, setWizardOpen] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const [viewing, setViewing] = useState<RunMember | null>(null)
  const [instructing, setInstructing] = useState<Run | null>(null)
  const [instructText, setInstructText] = useState('')
  const fetchingRef = useRef(false)
  const seededRef = useRef<Set<string>>(new Set())

  const fetchAll = useCallback(async () => {
    if (fetchingRef.current) return
    fetchingRef.current = true
    try {
      const all = await Promise.all(
        sessions.map(async s => {
          try {
            const detail = await api.getSession(s.name)
            return { name: s.name, terminals: detail.terminals || [] }
          } catch {
            return { name: s.name, terminals: [] }
          }
        })
      )
      setDetails(all)
      all.flatMap(d => d.terminals).forEach(t => {
        if (seededRef.current.has(t.id)) return
        seededRef.current.add(t.id)
        api.getTerminalStatus(t.id)
          .then(status => { if (status) setTerminalStatus(t.id, status) })
          .catch(() => {})
      })
    } finally {
      fetchingRef.current = false
    }
  }, [sessions.map(s => s.name).join(',')])

  useEffect(() => {
    fetchAll()
    const interval = setInterval(fetchAll, 10000)
    return () => clearInterval(interval)
  }, [fetchAll])

  // A flow event = a worker just joined an existing session; refresh NOW so
  // the node (and the pulse heading to it) appears immediately.
  useEffect(() => {
    if (flowPulses.length) fetchAll()
  }, [flowPulses.length])

  const runs = details
    .map(d => deriveRun(d, terminalStatuses))
    .sort((a, b) => PHASE_ORDER[a.phase] - PHASE_ORDER[b.phase])
  const active = runs.filter(r => r.phase !== 'done')
  const finished = runs.filter(r => r.phase === 'done')

  const working = runs.filter(r => r.phase === 'working' || r.phase === 'starting').length
  const needsYou = runs.filter(r => r.phase === 'needs_you').length
  const broken = runs.filter(r => r.phase === 'problem').length
  const sceneParts = [
    working ? `${working} in motion` : null,
    needsYou ? `${needsYou} needs you` : null,
    broken ? `${broken} needs a look` : null,
    finished.length ? `${finished.length} finished` : null,
  ].filter(Boolean)

  const labelOf = (runId: string) => sessions.find(s => s.name === runId)?.label
  const startedOf = (runId: string) => runStartedAt(details.find(d => d.name === runId))

  const sendInstruction = async () => {
    if (!instructing?.planner || !instructText.trim()) return
    try {
      await api.sendInput(instructing.planner.terminalId, instructText.trim())
      showSnackbar({ type: 'success', message: 'Instruction sent to the planner' })
      setInstructing(null)
      setInstructText('')
    } catch (e: any) {
      showSnackbar({ type: 'error', message: e.message || 'Could not send the instruction' })
    }
  }

  const retry = async (run: Run) => {
    const target = run.planner || run.workers.find(w => w.status === 'ERROR')
    if (!target) return
    try {
      await api.sendInput(
        target.terminalId,
        'Something went wrong with the last step. Please diagnose what happened and retry the task.'
      )
      showSnackbar({ type: 'success', message: 'Retry requested' })
    } catch (e: any) {
      showSnackbar({ type: 'error', message: e.message || 'Could not request a retry' })
    }
  }

  return (
    <div className="space-y-5" data-testid="run-board">
      {/* page header (§2.1) */}
      <div className="flex items-center justify-between">
        <div>
          <h1 style={{ fontSize: 20, fontWeight: 600, color: 'var(--t1)' }}>Runs</h1>
          <p style={{ fontSize: 13, color: 'var(--t3)' }}>
            {sceneParts.length ? sceneParts.join(' · ') : 'Each run is a team of AI agents working on one goal. Watch them live.'}
          </p>
        </div>
        <button
          onClick={() => setWizardOpen(true)}
          className="flex items-center gap-2 px-4 py-2 rounded-lg text-white font-medium"
          style={{ background: 'var(--brand-deep)', fontSize: 13.5 }}
          data-testid="start-run"
        >
          <Play size={14} /> Start a run
        </button>
      </div>

      {runs.length === 0 && (
        <div className="text-center py-16 rounded-xl" style={{ border: '1px dashed var(--border)' }}>
          <p style={{ color: 'var(--t2)' }}>No runs yet.</p>
          <p style={{ fontSize: 13, color: 'var(--t4)', marginTop: 4 }}>
            Click <span style={{ color: 'var(--t2)' }}>Start a run</span>, describe what you want done,
            and a planner agent will organize the work.
          </p>
        </div>
      )}

      {active.length > 0 && (
        <div className="space-y-5">
          {active.map(run => (
            <RunCard key={run.runId} run={run} label={labelOf(run.runId)} startedAt={startedOf(run.runId)}
              pulses={flowPulses} onDelete={id => setConfirmDelete(id)} onShow={setViewing}
              onInstruct={setInstructing} onRetry={retry} />
          ))}
        </div>
      )}

      {finished.length > 0 && (
        <div>
          <h2 className="microlabel" style={{ marginBottom: 8 }}>Finished</h2>
          <div className="space-y-3">
            {finished.map(run => (
              <RunCard key={run.runId} run={run} label={labelOf(run.runId)} startedAt={startedOf(run.runId)}
                pulses={flowPulses} onDelete={id => setConfirmDelete(id)} onShow={setViewing}
                onInstruct={setInstructing} onRetry={retry} />
            ))}
          </div>
        </div>
      )}

      {wizardOpen && <StartRunWizard onClose={() => setWizardOpen(false)} />}

      <ConfirmModal
        open={confirmDelete !== null}
        title="End this run?"
        message={`This stops every agent in "${confirmDelete ?? ''}" and closes the run. Finished work on disk is kept.`}
        confirmLabel="End run"
        onConfirm={async () => {
          if (confirmDelete) await deleteSession(confirmDelete)
          setConfirmDelete(null)
        }}
        onCancel={() => setConfirmDelete(null)}
      />

      {viewing && <OutputViewer terminalId={viewing.terminalId} onClose={() => setViewing(null)} />}

      {instructing && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50">
          <div className="rounded-xl w-full max-w-lg p-5"
            style={{ background: 'var(--card)', border: '1px solid var(--border)' }}>
            <h3 style={{ fontSize: 15, fontWeight: 600, color: 'var(--t1)', marginBottom: 4 }}>
              Tell the planner what to do next
            </h3>
            <p style={{ fontSize: 12, color: 'var(--t3)', marginBottom: 12 }}>
              Run {instructing.runId} — your instruction goes straight to the planner agent, which
              will organize any follow-up work.
            </p>
            <textarea
              autoFocus
              value={instructText}
              onChange={e => setInstructText(e.target.value)}
              placeholder="e.g. Also add a dark-mode toggle, then have it reviewed"
              className="w-full h-24 rounded-lg p-2.5 outline-none resize-none"
              style={{ background: 'var(--page)', border: '1px solid var(--border)', fontSize: 13, color: 'var(--t1)' }}
              data-testid="instruct-text"
            />
            <div className="flex justify-end gap-2 mt-3">
              <button onClick={() => { setInstructing(null); setInstructText('') }}
                className="px-3 py-2 rounded-lg" style={{ fontSize: 13, color: 'var(--t2)' }}>
                Cancel
              </button>
              <button onClick={sendInstruction} disabled={!instructText.trim()}
                className="flex items-center gap-1.5 px-4 py-2 rounded-lg text-white disabled:opacity-40"
                style={{ background: 'var(--brand-deep)', fontSize: 13 }} data-testid="instruct-send">
                <Send size={13} /> Send instruction
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

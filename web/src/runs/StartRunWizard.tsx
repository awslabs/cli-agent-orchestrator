import { useEffect, useState } from 'react'
import { api, AgentProfileInfo } from '../api'
import { useStore } from '../store'
import { isPlannerProfile } from '../orchestration'
import { X, ArrowRight, ArrowLeft, Rocket, FolderSearch } from 'lucide-react'
import { FolderBrowser } from './FolderBrowser'

/**
 * Three small steps for someone who has never used a terminal:
 *   1. What do you want done?  (free text + optional folder)
 *   2. Who should lead it?     (pick a planner profile; sensible default)
 *   3. Confirm and launch.
 * Launch = existing POST /sessions with the planner profile, then the goal is
 * sent as the planner's first message. The planner spawns its own workers.
 */
export function StartRunWizard({ onClose }: { onClose: () => void }) {
  const { showSnackbar, fetchSessions } = useStore()
  const [step, setStep] = useState(0)
  const [goal, setGoal] = useState('')
  const [folder, setFolder] = useState('')
  const [profiles, setProfiles] = useState<AgentProfileInfo[]>([])
  const [planner, setPlanner] = useState('')
  const [launching, setLaunching] = useState(false)
  const [browsing, setBrowsing] = useState(false)

  useEffect(() => {
    api.listProfiles().then(all => {
      const planners = all.filter(p => isPlannerProfile(p.name))
      setProfiles(planners)
      if (planners.length > 0 && !planner) {
        const preferred = planners.find(p => p.name === 'code_supervisor') || planners[0]
        setPlanner(preferred.name)
      }
    }).catch(() => {})
  }, [])

  const launch = async () => {
    setLaunching(true)
    // Agent CLIs occasionally exceed the server's init timeout on a cold
    // start; one transparent retry absorbs that flake for the user.
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const terminal = await api.createSession('claude_code', planner, undefined, folder || undefined)
        await api.sendInput(terminal.id, goal)
        showSnackbar({ type: 'success', message: 'Run started — the planner is on it' })
        await fetchSessions()
        onClose()
        return
      } catch (e: any) {
        if (attempt === 0) {
          showSnackbar({ type: 'info', message: 'Slow start — retrying…' })
          continue
        }
        showSnackbar({ type: 'error', message: e.message || 'Could not start the run' })
        setLaunching(false)
      }
    }
  }

  const steps = ['Your goal', 'The team', 'Launch']

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50" data-testid="start-run-wizard">
      <div className="bg-[#16161e] border border-gray-800 rounded-xl w-full max-w-lg p-6 shadow-2xl">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-gray-100">Start a run</h2>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-300" aria-label="Close">
            <X size={18} />
          </button>
        </div>

        <div className="flex items-center gap-2 mb-6">
          {steps.map((label, i) => (
            <div key={label} className="flex items-center gap-2">
              <span
                className={`text-xs px-2 py-1 rounded-full ${
                  i === step ? 'bg-blue-500/20 text-blue-300' : 'bg-gray-800 text-gray-500'
                }`}
              >
                {i + 1}. {label}
              </span>
              {i < steps.length - 1 && <span className="text-gray-700">—</span>}
            </div>
          ))}
        </div>

        {step === 0 && (
          <div className="space-y-4">
            <label className="block">
              <span className="text-sm text-gray-300">What do you want done?</span>
              <textarea
                autoFocus
                value={goal}
                onChange={e => setGoal(e.target.value)}
                placeholder="e.g. Add a CSV export button to the invoices page, with tests"
                className="mt-1 w-full h-28 bg-[#0f0f14] border border-gray-700 rounded-lg p-3 text-sm text-gray-200 focus:border-blue-500 outline-none resize-none"
                data-testid="wizard-goal"
              />
            </label>
            <label className="block">
              <span className="text-sm text-gray-300">Project folder <span className="text-gray-500">(optional)</span></span>
              <div className="mt-1 flex gap-2">
                <input
                  value={folder}
                  onChange={e => setFolder(e.target.value)}
                  placeholder={'C:\\Users\\you\\project  or  /home/you/project'}
                  className="flex-1 bg-[#0f0f14] border border-gray-700 rounded-lg p-2.5 text-sm text-gray-200 focus:border-blue-500 outline-none"
                  data-testid="wizard-folder"
                />
                <button
                  type="button"
                  onClick={() => setBrowsing(true)}
                  className="flex items-center gap-1.5 px-3 py-2 text-sm bg-gray-800 hover:bg-gray-700 text-gray-300 rounded-lg shrink-0"
                  title="Browse the server's folders"
                >
                  <FolderSearch size={14} /> Browse…
                </button>
              </div>
            </label>
          </div>
        )}

        {step === 1 && (
          <div className="space-y-3">
            <p className="text-sm text-gray-400">
              A <span className="text-gray-200">planner</span> leads the run: it splits your goal into
              tasks, hands them to worker agents, and collects the results.
            </p>
            {profiles.length === 0 && (
              <p className="text-sm text-amber-400">
                No planner profiles installed — install one (e.g. <code>code_supervisor</code>) first.
              </p>
            )}
            {profiles.map(p => (
              <button
                key={p.name}
                onClick={() => setPlanner(p.name)}
                className={`w-full text-left p-3 rounded-lg border ${
                  planner === p.name
                    ? 'border-blue-500 bg-blue-500/10'
                    : 'border-gray-700 hover:border-gray-500'
                }`}
                data-testid={`wizard-planner-${p.name}`}
              >
                <div className="text-sm font-medium text-gray-200">{p.name}</div>
                <div className="text-xs text-gray-500 mt-0.5">{p.description}</div>
              </button>
            ))}
          </div>
        )}

        {step === 2 && (
          <div className="space-y-3 text-sm">
            <p className="text-gray-400">Ready to launch:</p>
            <div className="bg-[#0f0f14] border border-gray-800 rounded-lg p-3 space-y-2">
              <div><span className="text-gray-500">Goal: </span><span className="text-gray-200">{goal}</span></div>
              <div><span className="text-gray-500">Planner: </span><span className="text-gray-200">{planner}</span></div>
              <div><span className="text-gray-500">Folder: </span><span className="text-gray-200">{folder || 'server default'}</span></div>
            </div>
            <p className="text-xs text-gray-500">
              Launching takes ~30 seconds while the planner starts up. You can watch every step live on the board.
            </p>
          </div>
        )}

        <div className="flex justify-between mt-6">
          <button
            onClick={() => setStep(s => Math.max(0, s - 1))}
            disabled={step === 0 || launching}
            className="flex items-center gap-1.5 px-3 py-2 text-sm text-gray-400 hover:text-gray-200 disabled:opacity-30"
          >
            <ArrowLeft size={14} /> Back
          </button>
          {step < 2 ? (
            <button
              onClick={() => setStep(s => s + 1)}
              disabled={(step === 0 && !goal.trim()) || (step === 1 && !planner)}
              className="flex items-center gap-1.5 px-4 py-2 text-sm bg-blue-600 hover:bg-blue-500 text-white rounded-lg disabled:opacity-40"
              data-testid="wizard-next"
            >
              Next <ArrowRight size={14} />
            </button>
          ) : (
            <button
              onClick={launch}
              disabled={launching}
              className="flex items-center gap-1.5 px-4 py-2 text-sm bg-emerald-600 hover:bg-emerald-500 text-white rounded-lg disabled:opacity-50"
              data-testid="wizard-launch"
            >
              <Rocket size={14} /> {launching ? 'Launching…' : 'Launch run'}
            </button>
          )}
        </div>

        {browsing && (
          <FolderBrowser
            title="Choose the project folder"
            onSelect={setFolder}
            onClose={() => setBrowsing(false)}
          />
        )}
      </div>
    </div>
  )
}

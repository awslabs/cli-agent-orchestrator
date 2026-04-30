import { useState, useEffect } from 'react'
import { api, AgentProfileInfo } from '../api'
import { Package, ChevronDown, ChevronRight } from 'lucide-react'

export function ProfilesPanel() {
  const [profiles, setProfiles] = useState<AgentProfileInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [expanded, setExpanded] = useState<string | null>(null)

  useEffect(() => {
    api.listProfiles()
      .then(all => setProfiles(all.filter(p => !p.description.includes('managed by AIM'))))
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  if (loading) {
    return <div className="text-gray-500 text-sm py-8 text-center">Loading profiles...</div>
  }

  return (
    <div className="space-y-6">
      <div className="bg-gray-800/60 border border-gray-700/50 rounded-xl p-5">
        <h3 className="text-sm font-semibold text-gray-300 uppercase tracking-wide mb-4">
          Agent Profiles ({profiles.length})
        </h3>

        {profiles.length === 0 ? (
          <div className="text-center py-8">
            <Package size={32} className="mx-auto text-gray-600 mb-3" />
            <p className="text-gray-500 text-sm">No profiles found.</p>
            <p className="text-gray-600 text-xs mt-1">
              Install profiles via CLI: <code className="text-emerald-400">cao install &lt;profile_name&gt;</code>
            </p>
          </div>
        ) : (
          <div className="space-y-2">
            {profiles.map(p => (
              <div key={p.name} className="bg-gray-900/50 border border-gray-700/30 rounded-lg">
                <div
                  className="flex items-center justify-between p-3 cursor-pointer hover:bg-gray-800/50 transition-colors"
                  onClick={() => setExpanded(expanded === p.name ? null : p.name)}
                >
                  <div className="flex items-center gap-3 min-w-0">
                    {expanded === p.name ? (
                      <ChevronDown size={14} className="text-gray-400 shrink-0" />
                    ) : (
                      <ChevronRight size={14} className="text-gray-400 shrink-0" />
                    )}
                    <Package size={14} className="text-blue-400 shrink-0" />
                    <span className="text-sm text-gray-200 font-medium truncate">{p.name}</span>
                    <span className={`text-xs px-2 py-0.5 rounded-full shrink-0 ${
                      p.source === 'built-in' ? 'bg-blue-900/50 text-blue-400' : 'bg-emerald-900/50 text-emerald-400'
                    }`}>
                      {p.source}
                    </span>
                  </div>
                </div>

                {expanded === p.name && (
                  <div className="px-4 pb-4 border-t border-gray-700/30 pt-3">
                    <div className="space-y-2 text-sm">
                      <div>
                        <span className="text-gray-500">Name: </span>
                        <span className="text-gray-200">{p.name}</span>
                      </div>
                      <div>
                        <span className="text-gray-500">Description: </span>
                        <span className="text-gray-200">{p.description || '—'}</span>
                      </div>
                      <div>
                        <span className="text-gray-500">Source: </span>
                        <span className="text-gray-200">{p.source}</span>
                      </div>
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

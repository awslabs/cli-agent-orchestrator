import { useEffect } from 'react'

interface Props { onClose: () => void }

const HOTKEYS = [
  { key: 'Space', action: 'Jump to stuck agent' },
  { key: 'Escape', action: 'Close panel / Deselect' },
  { key: 'N', action: 'New Bead modal' },
  { key: 'R', action: 'New Ralph Loop modal' },
  { key: 'Delete', action: 'Delete selected' },
  { key: '+/=', action: 'Zoom in 10%' },
  { key: '-', action: 'Zoom out 10%' },
  { key: '0', action: 'Reset zoom to 100%' },
  { key: 'Arrow keys', action: 'Pan map 50px' },
  { key: 'Tab', action: 'Cycle through agents' },
  { key: 'Shift+Tab', action: 'Cycle backwards' },
  { key: 'Enter', action: 'Open terminal for selected' },
  { key: '1-9', action: 'Select agent by index' },
  { key: '? / F1', action: 'Show this help' }
]

export function HotkeyHelp({ onClose }: Props) {
  useEffect(() => {
    const handler = (e: KeyboardEvent) => { e.preventDefault(); onClose() }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose])

  return (
    <div className="fixed inset-0 flex items-center justify-center z-50" style={{ background: 'rgba(0,0,0,0.8)' }} onClick={onClose}>
      <div className="rounded-lg p-6" style={{ background: '#1a1a2e', border: '1px solid #333', maxWidth: 400 }} onClick={e => e.stopPropagation()}>
        <h2 className="text-lg font-bold mb-4" style={{ color: '#00ff88' }}>⌨️ Keyboard Shortcuts</h2>
        <div className="grid grid-cols-2 gap-2 text-sm">
          {HOTKEYS.map(h => (
            <div key={h.key} className="contents">
              <span className="px-2 py-1 rounded" style={{ background: '#0a0a0f', color: '#00d4ff', fontFamily: 'monospace' }}>{h.key}</span>
              <span style={{ color: '#e0e0e0' }}>{h.action}</span>
            </div>
          ))}
        </div>
        <p className="mt-4 text-xs text-center" style={{ color: '#666' }}>Press any key to close</p>
      </div>
    </div>
  )
}

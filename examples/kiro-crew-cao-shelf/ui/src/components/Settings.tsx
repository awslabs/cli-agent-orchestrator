import { Card, CardTitle, ErrorNotice, Input, Toggle } from '@kirocrew/app-sdk/ui'
import { useEffect, useId, useState } from 'react'
import { toShelfError } from '../api'
import { useShelfCtx } from '../context'
import type { Config } from '../types'
import { Button } from './Button'

const REMEMBER = 'Remember closed helpers (ids and times only, no text)'

export function Settings({ onClose }: { onClose: () => void }) {
  const { api, announce, refresh } = useShelfCtx()
  const id = useId()
  const [cfg, setCfg] = useState<Config | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    let dead = false
    api
      .getConfig()
      .then((c) => !dead && setCfg(c))
      .catch((e) => !dead && setError(toShelfError(e).message))
    return () => {
      dead = true
    }
  }, [api])

  const set = <K extends keyof Config>(k: K, v: Config[K]) => cfg && setCfg({ ...cfg, [k]: v })
  const num = (s: string) => (s === '' || Number.isNaN(Number(s)) ? 0 : Number(s))

  const save = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!cfg) return
    setError(null)
    setSaved(false)
    try {
      const c = await api.putConfig(cfg)
      setCfg(c)
      setSaved(true)
      announce('Settings saved')
      refresh()
    } catch (err) {
      setError(toShelfError(err).message || 'Settings were not saved.')
    }
  }

  return (
    <Card className="cs-box">
      <form onSubmit={save} aria-label="Settings" onKeyDown={(e) => e.key === 'Escape' && onClose()}>
        <CardTitle className="cs-box-title">Settings</CardTitle>
        {!cfg && !error ? <p className="cs-muted">Loading...</p> : null}
        {cfg ? (
          <>
            <label htmlFor={`${id}-u`}>CAO address</label>
            <Input id={`${id}-u`} className="cs-input" value={cfg.cao_url} onChange={(e) => set('cao_url', e.target.value)} aria-describedby={`${id}-uh`} />
            <small id={`${id}-uh`} className="cs-help">
              Must be http and loopback only: 127.0.0.1, localhost or ::1. Anything else is refused.
            </small>
            <label htmlFor={`${id}-s`}>Refresh every (seconds, 3 to 60)</label>
            <Input id={`${id}-s`} type="number" className="cs-input" min={3} max={60} value={cfg.poll_seconds} onChange={(e) => set('poll_seconds', num(e.target.value))} />
            <label htmlFor={`${id}-w`}>Warm below (hours, 0.25 to 24)</label>
            <Input id={`${id}-w`} type="number" step="any" className="cs-input" min={0.25} max={24} value={cfg.warm_hours} onChange={(e) => set('warm_hours', num(e.target.value))} />
            <label htmlFor={`${id}-t`}>Stale from (hours, 2 to 720)</label>
            <Input id={`${id}-t`} type="number" step="any" className="cs-input" min={2} max={720} value={cfg.stale_hours} onChange={(e) => set('stale_hours', num(e.target.value))} />
            {/* a persisted preference is a switch; its name is the visible sentence */}
            <div className="cs-switch">
              <Toggle checked={cfg.persist_history} onChange={(v) => set('persist_history', v)} label={REMEMBER} />
              <span aria-hidden="true" onClick={() => set('persist_history', !cfg.persist_history)}>
                {REMEMBER}
              </span>
            </div>
          </>
        ) : null}
        <ErrorNotice className="cs-gap" message={error} />
        {saved ? <p role="status">Saved.</p> : null}
        <div className="cs-foot">
          <Button type="submit" primary disabled={!cfg}>
            Save
          </Button>
          <Button onClick={onClose}>Close</Button>
        </div>
      </form>
    </Card>
  )
}

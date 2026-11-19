import { ErrorNotice, Input } from '@kirocrew/app-sdk/ui'
import { useEffect, useId, useRef, useState } from 'react'
import { toShelfError } from '../api'
import { useShelfCtx } from '../context'
import type { Session } from '../types'
import { Button } from './Button'

const DELIVERY: Record<string, string> = {
  delivered: 'Message delivered to the conductor of {name}.',
  pending: 'Message queued: the conductor of {name} is busy and CAO will deliver it when it is idle.',
  failed: 'CAO could not deliver the message to the conductor of {name}.',
  unknown: 'CAO accepted the message for {name}; its delivery could not be confirmed.',
}

export function AskForm({ session, onClose }: { session: Session; onClose: () => void }) {
  const { api, disabledReason, announce, refresh } = useShelfCtx()
  const id = useId()
  const input = useRef<HTMLInputElement>(null)
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [sent, setSent] = useState(false)
  const [outcome, setOutcome] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [changed, setChanged] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    input.current?.focus()
    return () => {
      if (timer.current) clearTimeout(timer.current)
    }
  }, [])

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    const message = text.trim()
    if (!message || busy || disabledReason) return
    setBusy(true)
    setError(null)
    setChanged(false)
    setOutcome(null)
    try {
      const r = await api.ask(session.name, { message, conductor_id: session.conductor.id })
      setText('')
      // say what CAO reported, not what was hoped for
      const said = DELIVERY[r.delivery] ?? DELIVERY.unknown
      setSent(r.delivery === 'delivered')
      setOutcome(said.replace('{name}', session.name))
      announce(said.replace('{name}', session.name))
      timer.current = setTimeout(() => setSent(false), 2000)
      refresh()
    } catch (err) {
      const se = toShelfError(err)
      if (se.code === 'conflict_changed') setChanged(true)
      else setError(se.message || 'The message was not sent.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <form className="cs-form" onSubmit={submit} onKeyDown={(e) => e.key === 'Escape' && (e.stopPropagation(), onClose())}>
        <label className="cs-sr" htmlFor={`${id}-q`}>
          Message to the conductor of {session.name}
        </label>
        <Input id={`${id}-q`} ref={input} className="cs-input" placeholder="Ask the conductor..." value={text} onChange={(e) => setText(e.target.value)} maxLength={8000} disabled={!!disabledReason} />
        <Button type="submit" primary disabled={!!disabledReason || !text.trim()} busy={busy} title={disabledReason ?? undefined}>
          {sent ? 'Sent' : 'Send'}
        </Button>
      </form>
      {outcome ? (
        <p className="cs-note" role="status">
          {outcome}
        </p>
      ) : null}
      {changed ? <ErrorNotice message="This session changed, reload. Nothing was sent." footer={<Button onClick={refresh}>Reload</Button>} /> : null}
      <ErrorNotice message={error} />
    </>
  )
}

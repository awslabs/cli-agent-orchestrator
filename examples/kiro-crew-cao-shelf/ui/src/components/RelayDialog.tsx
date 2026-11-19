import { ErrorNotice, Modal } from '@kirocrew/app-sdk/ui'
import { useEffect, useRef, useState } from 'react'
import { toShelfError } from '../api'
import { RELAY_RISK, RELAY_RISK_DETAIL, relayErrorText, useOpenChat } from '../chat'
import { useShelfCtx } from '../context'
import type { Session } from '../types'
import { Button } from './Button'

/** The stated risk: the short text, then the per-backend detail behind a disclosure. `shortId` lets a
 *  checkbox point its description at the short text. */
export function RiskText({ shortId }: { shortId?: string }) {
  return (
    <div className="cs-risk">
      <p id={shortId}>{RELAY_RISK}</p>
      <details className="cs-disc">
        <summary>What the chat can reach</summary>
        {RELAY_RISK_DETAIL.map((p) => (
          <p key={p.slice(0, 24)}>{p}</p>
        ))}
      </details>
    </div>
  )
}

interface Existing {
  relay_id: string
  label: string
  slot_key: string | null
  title: string | null
}

/** Remote control one session: the risk is stated here and nothing is sent until the owner accepts it
 *  (Accept and remote control, or Take over when the session already has an active relay). Cancel and
 *  Escape send nothing; while a request is in flight the dialog stays (every dismissal is refused), so
 *  its outcome shows here. A dismissal is always the safe direction: it declines. */
export function RelayDialog({ s, onClose }: { s: Session; onClose: () => void }) {
  const { api, announce, refresh } = useShelfCtx()
  const openChat = useOpenChat()
  const [existing, setExisting] = useState<Existing | null>(() =>
    s.relay && s.relay.state === 'active' ? { relay_id: s.relay.relay_id, label: s.relay.label, slot_key: s.relay.slot_key, title: s.relay.title } : null,
  )
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [fallback, setFallback] = useState<string | null>(null)
  const inFlight = useRef(false)
  const closeBtn = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    if (fallback) closeBtn.current?.focus()
  }, [fallback])

  const opened = (slotKey: string, title: string) => {
    if (openChat) {
      openChat(slotKey)
      announce(`Remote control chat for ${s.name} is open`)
      onClose()
    } else {
      setFallback(title)
      announce(`Open the chat "${title}" from the Kiro Crew sidebar.`)
    }
  }

  // one request at a time: a second press while one is in flight sends nothing
  const run = async (work: () => Promise<void>) => {
    if (inFlight.current) return
    inFlight.current = true
    setBusy(true)
    setError(null)
    try {
      await work()
    } catch (e) {
      setError(relayErrorText(toShelfError(e)))
    } finally {
      inFlight.current = false
      setBusy(false)
    }
  }

  const findSlot = async (relayId: string): Promise<Existing | null> => {
    try {
      const all = await api.relays()
      const r = all.relays.find((x) => x.relay_id === relayId)
      return r ? { relay_id: r.relay_id, label: r.label, slot_key: r.slot_key, title: r.title } : null
    } catch {
      return null
    }
  }

  const accept = () =>
    run(async () => {
      try {
        const r = await api.relayCreate({ session: s.name, acknowledge_risk: true })
        refresh()
        opened(r.slot_key, r.title)
      } catch (e) {
        const err = toShelfError(e)
        const id = err.detail?.relay_id
        if (err.code === 'relay_exists' && typeof id === 'string') {
          // another chat attached meanwhile: offer Open chat and Take over instead
          const label = typeof err.detail?.label === 'string' ? err.detail.label : ''
          setExisting((await findSlot(id)) ?? { relay_id: id, label, slot_key: null, title: null })
          refresh()
          return
        }
        throw e
      }
    })

  const takeOver = (id: string) =>
    run(async () => {
      const r = await api.relayTakeover(id, { acknowledge_risk: true })
      refresh()
      opened(r.slot_key, r.title)
    })

  const footer = fallback ? (
    <div className="cs-actions">
      <Button primary ref={closeBtn} onClick={onClose}>
        Done
      </Button>
    </div>
  ) : (
    <div className="cs-actions">
      <Button onClick={onClose} disabled={busy}>
        Cancel
      </Button>
      {existing?.slot_key ? (
        <Button onClick={() => opened(existing.slot_key as string, existing.title ?? `Remote control: ${existing.label}`)} disabled={busy}>
          Open chat
        </Button>
      ) : null}
      {existing ? (
        <Button primary onClick={() => takeOver(existing.relay_id)} busy={busy}>
          {busy ? 'Taking over...' : 'Take over'}
        </Button>
      ) : (
        <Button primary onClick={accept} busy={busy}>
          {busy ? 'Setting up...' : 'Accept and remote control'}
        </Button>
      )}
    </div>
  )

  return (
    <Modal open onClose={onClose} title={<span className="cs-dlg-title">Remote control {s.name}?</span>} maxWidth={520} dismissDisabled={busy} footer={footer}>
      {fallback ? (
        <p className="cs-lead" role="status">
          Open the chat &quot;{fallback}&quot; from the Kiro Crew sidebar.
        </p>
      ) : (
        <>
          {existing ? (
            <p className="cs-lead">
              This session is already remote controlled from chat <b>{existing.label}</b>. Take over ends that relay and makes a new relay chat.
            </p>
          ) : null}
          <RiskText />
          <ErrorNotice message={error} />
        </>
      )}
    </Modal>
  )
}

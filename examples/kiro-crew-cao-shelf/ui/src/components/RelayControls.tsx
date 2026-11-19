import { ErrorNotice } from '@kirocrew/app-sdk/ui'
import { useState } from 'react'
import { toShelfError } from '../api'
import { RELAY_EXPLAIN, RELAY_EXPLAIN_DETAIL, RELAY_EXPLAIN_SUMMARY, relayErrorText, useOpenChat } from '../chat'
import { useShelfCtx } from '../context'
import type { Session } from '../types'
import { Button } from './Button'

export const NO_CONDUCTOR = 'This session has no conductor, so there is nothing to remote control.'
const NOT_TOLD =
  'Kiro Crew did not answer, so the chat has not been told the relay ended yet: it keeps relaying until it is. CAO Shelf tells it as soon as Kiro Crew answers.'
const PENDING_SHORT = 'A tool request is waiting in the relay chat. Deny it unless you expect it.'
const PENDING_NOTICE =
  'A tool request is waiting in the relay chat. The relay\'s four CAO tools and its wait tool, which only pauses the chat, never ask on the kas backend, nor on claude with the allow rule from the README section "Remote control", so deny this request unless you are on claude, have not added that rule yet, and the chat shows one of those five tools. The name a request shows can be chosen by the model.'

/** What remote control means, once per page near its top: the sentence, then the detail behind a
 *  disclosure that starts closed. The texts live in chat.ts. */
export function RemoteControlIntro() {
  return (
    <div className="cs-rc-intro" role="note" aria-label="About remote control">
      <p>{RELAY_EXPLAIN}</p>
      <details className="cs-disc cs-disc--bare">
        <summary>{RELAY_EXPLAIN_SUMMARY}</summary>
        {RELAY_EXPLAIN_DETAIL.map((p) => (
          <p key={p.slice(0, 24)}>{p}</p>
        ))}
      </details>
    </div>
  )
}

/** The relay part of a card: the badge with Open chat and End relay, and the relay notices. The
 *  Remote control button sits in the card's action row and opens RelayDialog. Every sentence is
 *  fixed; the label and the chat title are shown as plain text. */
export function RelayControls({ s }: { s: Session }) {
  const { api, announce, refresh } = useShelfCtx()
  const openChat = useOpenChat()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [fallback, setFallback] = useState<string | null>(null)
  const relay = s.relay

  const open = (slotKey: string, title: string) => {
    setFallback(null)
    if (openChat) {
      openChat(slotKey)
      announce(`Remote control chat for ${s.name} is open`)
    } else {
      setFallback(title)
      announce(`Open the chat "${title}" from the Kiro Crew sidebar.`)
    }
  }

  const end = async (id: string) => {
    setBusy(true)
    setError(null)
    setFallback(null)
    try {
      const r = await api.relayEnd(id, {})
      announce(r.chat === 'unreachable' ? `Relay ended. The CAO session keeps working. ${NOT_TOLD}` : 'Relay ended. The CAO session keeps working.')
      refresh()
    } catch (e) {
      setError(relayErrorText(toShelfError(e)))
    } finally {
      setBusy(false)
    }
  }

  const active = relay && relay.state === 'active' ? relay : null
  if (!relay && !error && !fallback) return null

  return (
    <div className="cs-relay">
      {active ? (
        <div className="cs-relay-on" role="group" aria-label={`relay of ${s.name}`}>
          <span className="cs-relay-ttl">
            Remote controlled from chat <b>{active.label}</b>
          </span>
          {active.not_relayed > 0 ? (
            <small className="cs-muted" title="Messages the relay agent answered without sending them to the session or checking it">
              {active.not_relayed} not relayed
            </small>
          ) : null}
          <div className="cs-row">
            <Button onClick={() => open(active.slot_key, active.title)}>Open chat</Button>
            <Button danger onClick={() => end(active.relay_id)} busy={busy}>
              End relay
            </Button>
          </div>
        </div>
      ) : null}
      {relay && relay.state === 'needs_reattach' ? (
        <div className="cs-relay-on cs-relay-off">
          <span>Remote control stopped: this session&apos;s conductor changed.</span>
          <div className="cs-row">
            <Button onClick={() => end(relay.relay_id)} busy={busy}>
              Dismiss
            </Button>
          </div>
        </div>
      ) : null}
      {active && active.permission_pending ? (
        // a caution to act on, not an error: amber, with the reason behind a disclosure
        <div className="cs-notice" data-tone="warn">
          <span>{PENDING_SHORT}</span>
          <details className="cs-disc cs-disc--bare">
            <summary>Why</summary>
            <p>{PENDING_NOTICE}</p>
          </details>
        </div>
      ) : null}
      <ErrorNotice message={error} />
      {relay?.ended_notice_pending ? (
        <p className="cs-note" role="status">
          {NOT_TOLD}
        </p>
      ) : null}
      {fallback ? (
        <p className="cs-note" role="status">
          Open the chat &quot;{fallback}&quot; from the Kiro Crew sidebar.
        </p>
      ) : null}
    </div>
  )
}

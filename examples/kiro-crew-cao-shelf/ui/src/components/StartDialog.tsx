import { ErrorNotice, Input, Modal } from '@kirocrew/app-sdk/ui'
import { useEffect, useId, useState } from 'react'
import { toShelfError } from '../api'
import { relayErrorText, useOpenChat } from '../chat'
import { useShelfCtx } from '../context'
import type { Profiles } from '../types'
import { Button } from './Button'
import { Select } from './kit'
import { RiskText } from './RelayDialog'

const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,58}$/

/** One key per opened dialog: a retry of the same Start sends it again, so CAO and the backend can
 *  tell the retry from a second launch. A new dialog gets a new key. */
export function newStartKey(): string {
  const c = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto
  if (c?.randomUUID) return c.randomUUID()
  return `k${Math.random().toString(36).slice(2)}${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`.padEnd(20, '0')
}

export function StartDialog({ onClose }: { onClose: () => void }) {
  const { api, announce, refresh } = useShelfCtx()
  const id = useId()
  const [profiles, setProfiles] = useState<Profiles['profiles'] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [profile, setProfile] = useState('')
  const [provider, setProvider] = useState('')
  const [cwd, setCwd] = useState('')
  const [message, setMessage] = useState('')
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [remote, setRemote] = useState(false)
  const [accepted, setAccepted] = useState(false)
  const [key] = useState(newStartKey)
  const openChat = useOpenChat()

  useEffect(() => {
    let dead = false
    api
      .profiles()
      .then((r) => {
        if (dead) return
        setProfiles(r.profiles)
        if (r.profiles[0]) setProfile((p) => p || r.profiles[0].name)
      })
      .catch((e) => !dead && setError(toShelfError(e).message || 'Profiles could not be read.'))
    return () => {
      dead = true
    }
  }, [api])

  const nameBad = name !== '' && !NAME_RE.test(name)
  // the host Select has no `required`: Start stays off until a profile is chosen
  const complete = profile !== '' && cwd.trim() !== '' && !nameBad && (!remote || accepted)
  const ready = complete && !busy
  // a part-filled form ignores Escape and a backdrop click; Cancel and the X still close it
  const dirty = provider !== '' || cwd !== '' || message !== '' || name !== '' || remote
  const chosen = profiles?.find((p) => p.name === profile)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!ready) return
    setBusy(true)
    setError(null)
    const fields = {
      idempotency_key: key,
      agent_profile: profile,
      working_directory: cwd.trim(),
      ...(provider.trim() ? { provider: provider.trim() } : {}),
      ...(name ? { name } : {}),
    }
    if (remote) {
      // the handshake is the session's first message; the owner's first message goes through the chat
      try {
        const r = await api.relayCreate({ start: fields, acknowledge_risk: true })
        if (openChat) {
          openChat(r.slot_key)
          announce(`Session ${r.session} started; its remote control chat is open`)
        } else {
          announce(`Session ${r.session} started. Open the chat "${r.title}" from the Kiro Crew sidebar.`)
        }
        refresh()
        onClose()
      } catch (err) {
        setError(relayErrorText(toShelfError(err)))
        setBusy(false)
      }
      return
    }
    try {
      const r = await api.start({ ...fields, ...(message.trim() ? { message: message.trim() } : {}) })
      announce(`Session ${r.session} started`)
      refresh()
      onClose()
    } catch (err) {
      setError(toShelfError(err).message || 'The session was not started.')
      setBusy(false)
    }
  }

  const formId = `${id}-f`
  const footer = (
    <div className="cs-actions">
      <Button onClick={onClose}>Cancel</Button>
      <Button type="submit" form={formId} primary disabled={!complete} busy={busy}>
        {busy ? 'Starting...' : 'Start'}
      </Button>
    </div>
  )

  return (
    <Modal open onClose={onClose} title="Start a session" maxWidth={520} guardAccidentalDismiss={dirty} dismissDisabled={busy} footer={footer}>
      <small className="cs-small">Nothing is sent until you press Start.</small>
      <form id={formId} onSubmit={submit}>
        <span className="cs-field">Profile</span>
        <Select
          aria-label="Profile"
          className="cs-select"
          options={profiles?.map((p) => p.name) ?? []}
          value={profile}
          // '' is never a choice here (the clear row shows only while the list is off). The host's Radix
          // select reports '' once while its options register, which would undo the preselected profile.
          onChange={(v) => v && setProfile(v)}
          clearLabel={profiles === null ? 'Loading profiles...' : profiles.length === 0 ? 'No profiles found' : undefined}
          disabled={!profiles || profiles.length === 0}
        />
        {chosen?.description ? <small className="cs-help">{chosen.description}</small> : null}
        <label className="cs-field" htmlFor={`${id}-v`}>
          Provider (optional)
        </label>
        <Input id={`${id}-v`} className="cs-input" value={provider} placeholder={chosen?.provider ?? 'profile default'} onChange={(e) => setProvider(e.target.value)} />
        <label className="cs-field" htmlFor={`${id}-d`}>
          Working directory (required)
        </label>
        <Input id={`${id}-d`} className="cs-input" value={cwd} onChange={(e) => setCwd(e.target.value)} required />
        {remote ? (
          <small className="cs-help">Your first message goes through the relay chat. The session&apos;s first message is the remote-control handshake.</small>
        ) : (
          <>
            <label className="cs-field" htmlFor={`${id}-m`}>
              First message (optional)
            </label>
            <textarea id={`${id}-m`} className="cs-textarea" rows={3} maxLength={8000} value={message} onChange={(e) => setMessage(e.target.value)} />
          </>
        )}
        <label className="cs-field" htmlFor={`${id}-n`}>
          Session name (optional)
        </label>
        <Input id={`${id}-n`} className="cs-input" value={name} onChange={(e) => setName(e.target.value)} aria-invalid={nameBad} aria-describedby={nameBad ? `${id}-ne` : undefined} />
        {nameBad ? (
          <small id={`${id}-ne`} className="cs-bad" role="alert">
            Letters, digits, dash and underscore only, starting with a letter or digit, up to 59 characters.
          </small>
        ) : null}
        <label className="cs-checkrow">
          <input type="checkbox" className="cs-check" checked={remote} onChange={(e) => setRemote(e.target.checked)} />
          and remote control it
        </label>
        {remote ? (
          <>
            <RiskText shortId={`${id}-risk`} />
            <label className="cs-checkrow">
              <input type="checkbox" className="cs-check" checked={accepted} onChange={(e) => setAccepted(e.target.checked)} aria-describedby={`${id}-risk`} />I accept this risk
            </label>
          </>
        ) : null}
        <ErrorNotice className="cs-gap" message={error} />
      </form>
    </Modal>
  )
}

import { ErrorNotice, Input } from '@kirocrew/app-sdk/ui'
import { useEffect, useId, useRef, useState } from 'react'
import { toShelfError } from '../api'
import { useShelfCtx } from '../context'
import { ageWords } from '../format'
import type { PromptResponse, Session } from '../types'
import { Button } from './Button'
import { Dot } from './Chip'

type Ask = Session['asks'][number]

/** The question lives in the terminal tail, never in the shelf; the tail is shown as text only. */
export function AnswerBox({ ask }: { ask: Ask }) {
  const { api, disabledReason, announce, refresh, now } = useShelfCtx()
  const id = useId()
  const [prompt, setPrompt] = useState<PromptResponse | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [show, setShow] = useState(false)
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [sent, setSent] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [gone, setGone] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    let dead = false
    setLoadError(null)
    api
      .prompt(ask.terminal_id)
      .then((p) => !dead && setPrompt(p))
      .catch((e) => !dead && setLoadError(toShelfError(e).message || 'The prompt could not be read.'))
    return () => {
      dead = true
    }
  }, [api, ask.terminal_id, ask.since])

  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current)
    },
    [],
  )

  const send = async (answer: string) => {
    if (!answer || busy || disabledReason) return
    setBusy(true)
    setError(null)
    try {
      await api.answer(ask.terminal_id, { answer, expect_status: 'waiting_user_answer' })
      setSent(true)
      setText('')
      announce(`Answer sent to ${ask.label}`)
      timer.current = setTimeout(() => setSent(false), 2000)
      refresh()
    } catch (e) {
      const se = toShelfError(e)
      if (se.code === 'not_waiting') {
        setGone(true)
        refresh()
      } else setError(se.message || 'The answer was not sent.')
    } finally {
      setBusy(false)
    }
  }

  const tail = prompt?.tail ?? []
  const question = [...tail].reverse().find((l) => l.trim() !== '')
  const since = ageWords(ask.since, now)

  return (
    <div className="cs-need">
      <div>
        <b>{ask.label}</b> asks{question ? ': ' : ' '}
        {question ? <span className="cs-q">&ldquo;{question.length > 140 ? `${question.slice(0, 140)}...` : question}&rdquo;</span> : null}{' '}
        <em>{since === 'age unknown' ? since : `about ${since}`}</em>
      </div>
      {gone ? (
        <ErrorNotice message={`${ask.label} is no longer waiting. Nothing was sent.`} />
      ) : (
        <>
          {tail.length > 0 ? (
            <>
              <Button quiet aria-expanded={show} aria-controls={`${id}-tail`} onClick={() => setShow((s) => !s)}>
                {show ? 'Hide prompt' : 'Show prompt'}
              </Button>
              {show ? (
                <pre id={`${id}-tail`} className="cs-pre" tabIndex={0} aria-label={`Last lines from ${ask.label}`}>
                  {tail.join('\n')}
                </pre>
              ) : null}
            </>
          ) : null}
          {loadError ? <p className="cs-note">The prompt could not be loaded ({loadError}). You can still type an answer.</p> : null}
          {prompt && prompt.suggested.length > 0 ? (
            <div className="cs-row">
              {prompt.suggested.map((s, i) => (
                // every suggested answer is secondary: the page cannot tell a safe answer from a risky one
                <Button key={`${s.send}-${i}`} disabled={!!disabledReason} busy={busy} title={disabledReason ?? undefined} onClick={() => send(s.send)}>
                  {s.label}
                </Button>
              ))}
            </div>
          ) : prompt || loadError ? (
            <form
              className="cs-form"
              onSubmit={(e) => {
                e.preventDefault()
                send(text.trim())
              }}
            >
              <label className="cs-sr" htmlFor={`${id}-a`}>
                Answer for {ask.label}
              </label>
              <Input id={`${id}-a`} className="cs-input" value={text} onChange={(e) => setText(e.target.value)} maxLength={2000} placeholder="Type an answer..." disabled={!!disabledReason} />
              <Button type="submit" primary disabled={!!disabledReason || !text.trim()} busy={busy} title={disabledReason ?? undefined}>
                {sent ? 'Sent' : 'Send answer'}
              </Button>
            </form>
          ) : null}
          {sent && prompt && prompt.suggested.length > 0 ? <p className="cs-note" role="status">Answer sent.</p> : null}
          <ErrorNotice message={error} />
        </>
      )}
    </div>
  )
}

export function ErrorNeed({ ask, now }: { ask: Ask; now: string }) {
  return (
    <div className="cs-need cs-need-error">
      <div>
        <Dot status="error" /> <b>{ask.label}</b> reported an error <em>{ageWords(ask.since, now) === 'age unknown' ? 'age unknown' : `about ${ageWords(ask.since, now)}`}</em>
      </div>
      <p className="cs-note">The shelf cannot answer an error. Look at that terminal in CAO.</p>
    </div>
  )
}

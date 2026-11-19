// What remote control means, on the page itself: one sentence and a closed disclosure near the top,
// once per page, in every state, with the texts from chat.ts (the api-contract.md "explain:" blocks).
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { beforeEach, describe, expect, it } from 'vitest'
import App from '../src/App'
import { RELAY_EXPLAIN, RELAY_EXPLAIN_DETAIL, RELAY_EXPLAIN_SUMMARY } from '../src/chat'
import { err, mockBackend, quiet } from './helpers'
import { resetStub } from './sdk-stub'

beforeEach(() => resetStub())

const intro = () => screen.getByRole('note', { name: 'About remote control' })

async function boot(shelf: unknown = quiet()) {
  mockBackend({ 'GET /shelf': shelf })
  render(<App />)
  await screen.findAllByRole('article')
}

describe('the remote control explainer', () => {
  it('is the contract text word for word, sentence and detail', () => {
    const doc = readFileSync(resolve(process.cwd(), '../api-contract.md'), 'utf8')
    const m = /<!-- explain:relay -->\n```text\n([\s\S]*?)\n```/.exec(doc)
    expect(m?.[1]).toBe(RELAY_EXPLAIN)
    const d = /<!-- explain:relay-detail -->\n```text\n([\s\S]*?)\n```/.exec(doc)
    expect(d?.[1].split('\n\n')).toEqual([...RELAY_EXPLAIN_DETAIL])
  })

  it('opens the detail without repeating the sentence, and never points at a card that may be missing', () => {
    expect(RELAY_EXPLAIN).not.toMatch(/card/)
    expect(RELAY_EXPLAIN_DETAIL[0].startsWith('Remote control')).toBe(false)
  })

  it('says what the owner asked for: the tag, idle is not done, the catch-up brief and End relay', () => {
    const all = RELAY_EXPLAIN_DETAIL.join(' ')
    expect(all).toContain('tagged [REMOTE-CONTROL]')
    expect(all).toContain('Idle does not mean done')
    expect(all).toContain('helpers are still working')
    expect(all).toContain('pauses')
    expect(all).toContain('[REMOTE-CONTROL catch-up]')
    expect(all).toContain('a brief of what happened in between')
    expect(all).toContain('starts with / goes to the session untagged')
    expect(all).toContain('End relay stops the chat, never the CAO session')
  })

  it('shows the sentence near the top of the page, before the tier toggles and the cards', async () => {
    await boot()
    const note = intro()
    expect(note).toHaveTextContent(RELAY_EXPLAIN)
    const first = screen.getAllByRole('article')[0]
    expect(note.compareDocumentPosition(first) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    const tiers = screen.getByRole('button', { name: /^Needs you/ })
    expect(note.compareDocumentPosition(tiers) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('keeps the detail behind a disclosure that starts closed and opens on its summary', async () => {
    const user = userEvent.setup()
    await boot()
    const details = intro().querySelector('details')!
    expect(details.open).toBe(false)
    const summary = within(intro()).getByText(RELAY_EXPLAIN_SUMMARY)
    expect(summary.tagName).toBe('SUMMARY')
    for (const p of RELAY_EXPLAIN_DETAIL) expect(details).toHaveTextContent(p)
    await user.click(summary)
    expect(details.open).toBe(true)
  })

  it('appears once per page, not on every card, and adds no button', async () => {
    await boot()
    expect(screen.getAllByRole('note', { name: 'About remote control' })).toHaveLength(1)
    expect(screen.getAllByText(RELAY_EXPLAIN_SUMMARY)).toHaveLength(1)
    expect(screen.getAllByText(RELAY_EXPLAIN)).toHaveLength(1)
    for (const c of screen.getAllByRole('article')) expect(c).not.toHaveTextContent(RELAY_EXPLAIN_SUMMARY)
    expect(within(intro()).queryAllByRole('button')).toEqual([])
  })

  it('is there with no sessions and when the backend cannot be reached', async () => {
    const d = quiet()
    d.sessions = []
    mockBackend({ 'GET /shelf': d })
    const { unmount } = render(<App />)
    await screen.findByText('No CAO sessions yet.')
    expect(intro()).toHaveTextContent(RELAY_EXPLAIN)
    unmount()
    mockBackend({ 'GET /shelf': () => err(502, 'backend_unreachable') })
    render(<App />)
    await screen.findByText('Cannot reach the CAO Shelf backend.')
    expect(intro()).toHaveTextContent(RELAY_EXPLAIN)
  })
})

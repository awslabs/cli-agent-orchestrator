import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import App from '../src/App'
import { err, mockBackend, quiet } from './helpers'
import { resetStub } from './sdk-stub'

const cfg = { cao_url: 'http://127.0.0.1:9889', poll_seconds: 5, warm_hours: 1, stale_hours: 48, persist_history: true }
beforeEach(() => resetStub())

describe('settings (B6)', () => {
  it('reads /config, shows the loopback rule and saves what the user changed', async () => {
    const user = userEvent.setup()
    const b = mockBackend({ 'GET /shelf': quiet(), 'GET /config': cfg, 'PUT /config': ({ body }: any) => ({ body }) })
    render(<App />)
    await user.click(await screen.findByRole('button', { name: 'Settings' }))
    const form = await screen.findByRole('form', { name: 'Settings' })
    expect(form).toHaveTextContent('loopback only: 127.0.0.1, localhost or ::1')
    const url = await within(form).findByLabelText('CAO address')
    expect(url).toHaveValue('http://127.0.0.1:9889')
    await user.clear(url)
    await user.type(url, 'http://localhost:9990')
    await user.click(within(form).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(b.writes()).toHaveLength(1))
    expect(b.writes()[0]).toMatchObject({ method: 'PUT', path: '/config', body: { ...cfg, cao_url: 'http://localhost:9990' } })
    expect(await within(form).findByText('Saved.')).toBeInTheDocument()
  })

  it('shows the refusal for a non-loopback address', async () => {
    const user = userEvent.setup()
    mockBackend({ 'GET /shelf': quiet(), 'GET /config': cfg, 'PUT /config': () => err(400, 'forbidden_host', 'cao_url must be http on a loopback host') })
    render(<App />)
    await user.click(await screen.findByRole('button', { name: 'Settings' }))
    const form = await screen.findByRole('form', { name: 'Settings' })
    await within(form).findByLabelText('CAO address')
    await user.click(within(form).getByRole('button', { name: 'Save' }))
    expect(await within(form).findByRole('alert')).toHaveTextContent('cao_url must be http on a loopback host')
  })

  it('offers no setting that approves tool requests for you', async () => {
    const user = userEvent.setup()
    mockBackend({ 'GET /shelf': quiet(), 'GET /config': cfg })
    render(<App />)
    await user.click(await screen.findByRole('button', { name: 'Settings' }))
    const form = await screen.findByRole('form', { name: 'Settings' })
    await within(form).findByLabelText('CAO address')
    expect(within(form).queryByRole('checkbox', { name: /Approve/ })).toBeNull()
    expect(within(form).queryByRole('switch', { name: /Approve/ })).toBeNull()
    expect(within(form).queryAllByRole('checkbox')).toHaveLength(0)
    expect(within(form).getAllByRole('switch')).toHaveLength(1) // persist_history only
  })

  it('is reachable from the "cannot reach CAO" empty state', async () => {
    const user = userEvent.setup()
    const q = quiet()
    mockBackend({ 'GET /shelf': { ...q, sessions: [], cao: { ...q.cao, reachable: false, error: { code: 'cao_unreachable', message: 'x', retryable: true } } }, 'GET /config': cfg })
    render(<App />)
    await user.click(await screen.findByRole('button', { name: 'Open Settings' }))
    expect(await screen.findByLabelText('CAO address')).toHaveValue('http://127.0.0.1:9889')
  })
})

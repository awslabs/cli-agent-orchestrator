// On the real host the Profile picker is a Radix select, which reports '' once while its options
// register after the list loads (seen in the isolated harness on Kiro Crew 0.12.2). That report
// must not undo the preselected first profile, or Start stays off until the owner picks again.
import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it } from 'vitest'
import App from '../src/App'
import { mockBackend, quiet } from './helpers'
import { resetStub } from './sdk-stub'

beforeEach(() => resetStub())

it('an empty value reported by the Select keeps the preselected profile', async () => {
  const user = userEvent.setup()
  const b = mockBackend({
    'GET /shelf': quiet(),
    'GET /profiles': { profiles: [{ name: 'developer' }, { name: 'supervisor' }] },
    'POST /sessions': { status: 201, body: { session: 'x', conductor_id: 'cafe0001' } },
  })
  render(<App />)
  await screen.findAllByRole('article')
  await user.click(screen.getByRole('button', { name: 'Start session' }))
  const dlg = await screen.findByRole('dialog', { name: 'Start a session' })
  await within(dlg).findByRole('option', { name: 'developer' })
  // what the host's select does once on its own, with no option for ''
  fireEvent.change(within(dlg).getByRole('combobox', { name: 'Profile' }), { target: { value: '' } })
  await user.type(within(dlg).getByLabelText('Working directory (required)'), '/work')
  const start = within(dlg).getByRole('button', { name: 'Start' })
  expect(start).toBeEnabled()
  await user.click(start)
  expect(b.writes()[0]).toMatchObject({ path: '/sessions', body: { agent_profile: 'developer' } })
})

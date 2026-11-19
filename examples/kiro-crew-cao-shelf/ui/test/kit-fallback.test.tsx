// A host older than the Level-1 additions has no Select and no guideCaution. The page reads both
// through a namespace import (src/components/kit.tsx), so on such a host each is undefined, as here,
// and the page falls back to a native <select> and to no caution marker.
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import App from '../src/App'
import { Button } from '../src/components/Button'
import { mockBackend, quiet } from './helpers'
import { resetStub } from './sdk-stub'

vi.mock('@kirocrew/app-sdk/ui', async (importOriginal) => ({ ...(await importOriginal<object>()), Select: undefined, guideCaution: undefined }))

beforeEach(() => resetStub())

it('without the host Select, Profile is a native select that still preselects and sends', async () => {
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
  const profile = within(dlg).getByRole('combobox', { name: 'Profile' })
  expect(profile.tagName).toBe('SELECT')
  expect(profile).not.toHaveAttribute('data-kit')
  expect(profile).toHaveValue('developer')
  await user.selectOptions(profile, 'supervisor')
  await user.type(within(dlg).getByLabelText('Working directory (required)'), '/work')
  await user.click(within(dlg).getByRole('button', { name: 'Start' }))
  expect(b.writes()[0]).toMatchObject({ path: '/sessions', body: { agent_profile: 'supervisor' } })
})

it('without guideCaution, a danger button carries no caution marker and still works', async () => {
  const user = userEvent.setup()
  const onClick = vi.fn()
  render(
    <Button danger onClick={onClick}>
      Stop...
    </Button>,
  )
  const b = screen.getByRole('button', { name: 'Stop...' })
  expect(b).not.toHaveAttribute('data-guide-caution')
  expect(b).toHaveClass('busy-cue-danger')
  await user.click(b)
  expect(onClick).toHaveBeenCalledOnce()
})

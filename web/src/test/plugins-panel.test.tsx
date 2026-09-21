import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { PluginsPanel } from '../components/PluginsPanel'
import { api } from '../api'

/**
 * The load-bearing test in this file is the confirm-before-DELETE group.
 *
 * `GET`/`DELETE /plugins` only *report* which live sessions reference a skill a
 * plugin provides. A panel that fired the DELETE straight from the trash icon
 * would be perfectly conformant to that and would gate nothing for a web user,
 * so the panel is a second, independent enforcement point for the same
 * warn-and-confirm behaviour the CLI's `--yes` flag guards.
 */

const WARNING = 'Installing an agent plugin runs untrusted code and content from that source.'

const PLUGIN_WITH_SESSIONS = {
  name: 'demo',
  version: '1.0.0',
  source: { kind: 'path', location: '/src/demo', ref: null, subdir: null },
  resolved_ref: null,
  installed_at: '2026-08-08T12:00:00Z',
  schema_id: 'https://agent-plugins.org/schemas/1.0.0/plugin.schema.json',
  skill_names: ['alpha', 'beta'],
  projected_skill_names: ['alpha'],
  findings: [
    {
      severity: 'skipped' as const,
      code: 'projection.preexisting_collision',
      spec_ref: 'CAO policy',
      message: "Skill 'beta' was not projected: a built-in skill of that name already exists",
      path: 'beta',
    },
  ],
  affected_sessions: [
    {
      terminal_id: 'abcd1234',
      session_name: 'cao-live',
      profile_name: 'worker',
      skill_names: ['alpha'],
    },
  ],
}

const PLUGIN_NO_SESSIONS = { ...PLUGIN_WITH_SESSIONS, affected_sessions: [] }

describe('PluginsPanel', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  function stubList(plugins: unknown[]) {
    return vi
      .spyOn(api, 'listPlugins')
      .mockResolvedValue({ plugins, untrusted_content_warning: WARNING } as never)
  }

  describe('rendering the installed set', () => {
    it('lists each plugin with its projected skills', async () => {
      stubList([PLUGIN_WITH_SESSIONS])
      render(<PluginsPanel />)

      expect(await screen.findByText('demo')).toBeInTheDocument()
      expect(screen.getByText(/skills: alpha/)).toBeInTheDocument()
    })

    it('states the untrusted-content warning', async () => {
      stubList([PLUGIN_WITH_SESSIONS])
      render(<PluginsPanel />)

      expect(await screen.findByRole('alert')).toHaveTextContent(/untrusted code and content/i)
    })

    it('renders non-fatal findings, not only fatal ones', async () => {
      stubList([PLUGIN_WITH_SESSIONS])
      render(<PluginsPanel />)

      fireEvent.click(await screen.findByRole('button', { expanded: false }))

      expect(await screen.findByText(/projection.preexisting_collision/)).toBeInTheDocument()
      expect(screen.getByText('skipped')).toBeInTheDocument()
      expect(screen.getByText(/not projected: beta/)).toBeInTheDocument()
    })

    it('shows an empty state when nothing is installed', async () => {
      stubList([])
      render(<PluginsPanel />)

      expect(await screen.findByText('No agent plugins installed')).toBeInTheDocument()
    })
  })

  describe('installing from a GitHub URL', () => {
    it('posts the URL as the source', async () => {
      stubList([])
      const install = vi
        .spyOn(api, 'installPlugin')
        .mockResolvedValue({ installed: true, record: { name: 'demo' } } as never)

      render(<PluginsPanel />)
      await screen.findByText('No agent plugins installed')

      fireEvent.change(screen.getByLabelText('Plugin source'), {
        target: { value: 'https://github.com/agentplugins/agent-plugins-example' },
      })
      fireEvent.click(screen.getByRole('button', { name: /install/i }))

      await waitFor(() =>
        expect(install).toHaveBeenCalledWith(
          expect.objectContaining({
            source: 'https://github.com/agentplugins/agent-plugins-example',
          }),
        ),
      )
    })

    it('passes ref and subdir when given', async () => {
      stubList([])
      const install = vi
        .spyOn(api, 'installPlugin')
        .mockResolvedValue({ installed: true, record: { name: 'demo' } } as never)

      render(<PluginsPanel />)
      await screen.findByText('No agent plugins installed')

      fireEvent.change(screen.getByLabelText('Plugin source'), {
        target: { value: 'https://github.com/awslabs/cli-agent-orchestrator' },
      })
      fireEvent.change(screen.getByLabelText('Git ref'), { target: { value: 'main' } })
      fireEvent.change(screen.getByLabelText('Subdirectory'), {
        target: { value: 'agent-plugin/cao' },
      })
      fireEvent.click(screen.getByRole('button', { name: /install/i }))

      await waitFor(() =>
        expect(install).toHaveBeenCalledWith(
          expect.objectContaining({ ref: 'main', subdir: 'agent-plugin/cao' }),
        ),
      )
    })

    it('renders a validation preview without installing', async () => {
      stubList([])
      const install = vi.spyOn(api, 'installPlugin')
      vi.spyOn(api, 'validatePlugin').mockResolvedValue({
        root: '/src/demo',
        loadable: false,
        name: null,
        version: null,
        description: null,
        schema_id: null,
        skills: [],
        mcp_present: false,
        findings: [
          {
            severity: 'fatal',
            code: 'manifest.schema_unsupported',
            spec_ref: '§5.2',
            message: 'Unsupported plugin manifest schema',
            path: 'plugin.json',
          },
        ],
      } as never)

      render(<PluginsPanel />)
      await screen.findByText('No agent plugins installed')

      fireEvent.change(screen.getByLabelText('Plugin source'), { target: { value: '/src/demo' } })
      fireEvent.click(screen.getByRole('button', { name: /validate/i }))

      expect(await screen.findByText('NOT loadable')).toBeInTheDocument()
      expect(screen.getByText(/manifest.schema_unsupported/)).toBeInTheDocument()
      expect(install).not.toHaveBeenCalled()
    })
  })

  describe('confirm before DELETE', () => {
    it('does not issue the DELETE on the remove click alone', async () => {
      stubList([PLUGIN_WITH_SESSIONS])
      const uninstall = vi.spyOn(api, 'uninstallPlugin').mockResolvedValue({} as never)

      render(<PluginsPanel />)
      fireEvent.click(await screen.findByRole('button', { name: 'Remove demo' }))

      // The dialog is open and nothing has been deleted.
      expect(await screen.findByRole('dialog')).toBeInTheDocument()
      expect(uninstall).not.toHaveBeenCalled()
    })

    it('renders the affected sessions and skills in the confirmation', async () => {
      stubList([PLUGIN_WITH_SESSIONS])
      vi.spyOn(api, 'uninstallPlugin').mockResolvedValue({} as never)

      render(<PluginsPanel />)
      fireEvent.click(await screen.findByRole('button', { name: 'Remove demo' }))

      const dialog = await screen.findByRole('dialog')
      expect(dialog).toHaveTextContent('cao-live')
      expect(dialog).toHaveTextContent('abcd1234')
      expect(dialog).toHaveTextContent('worker')
      expect(dialog).toHaveTextContent('alpha')
    })

    it('issues the DELETE only after an explicit confirmation', async () => {
      stubList([PLUGIN_WITH_SESSIONS])
      const uninstall = vi.spyOn(api, 'uninstallPlugin').mockResolvedValue({} as never)

      render(<PluginsPanel />)
      fireEvent.click(await screen.findByRole('button', { name: 'Remove demo' }))
      fireEvent.click(await screen.findByRole('button', { name: 'Remove' }))

      await waitFor(() => expect(uninstall).toHaveBeenCalledWith('demo', false))
    })

    it('cancelling leaves the plugin installed', async () => {
      stubList([PLUGIN_WITH_SESSIONS])
      const uninstall = vi.spyOn(api, 'uninstallPlugin').mockResolvedValue({} as never)

      render(<PluginsPanel />)
      fireEvent.click(await screen.findByRole('button', { name: 'Remove demo' }))
      fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }))

      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
      expect(uninstall).not.toHaveBeenCalled()
    })

    it('still confirms when no session is affected', async () => {
      stubList([PLUGIN_NO_SESSIONS])
      const uninstall = vi.spyOn(api, 'uninstallPlugin').mockResolvedValue({} as never)

      render(<PluginsPanel />)
      fireEvent.click(await screen.findByRole('button', { name: 'Remove demo' }))

      const dialog = await screen.findByRole('dialog')
      expect(dialog).toHaveTextContent(/No live session references/i)
      expect(uninstall).not.toHaveBeenCalled()
    })

    it('purge-data is opt-in and forwarded', async () => {
      stubList([PLUGIN_NO_SESSIONS])
      const uninstall = vi.spyOn(api, 'uninstallPlugin').mockResolvedValue({} as never)

      render(<PluginsPanel />)
      fireEvent.click(await screen.findByRole('button', { name: 'Remove demo' }))
      fireEvent.click(await screen.findByRole('checkbox'))
      fireEvent.click(await screen.findByRole('button', { name: 'Remove' }))

      await waitFor(() => expect(uninstall).toHaveBeenCalledWith('demo', true))
    })
  })
})

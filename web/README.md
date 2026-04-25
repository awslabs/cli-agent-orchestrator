# CAO Web UI

A single-page dashboard for managing CLI Agent Orchestrator sessions, agents, flows, and settings from the browser.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Browser (localhost:5173 dev / localhost:9889 prod)  │
│                                                      │
│  ┌──────────┐  ┌──────────┐  ┌───────┐  ┌────────┐ │
│  │   Home   │  │  Agents  │  │ Flows │  │Settings│ │
│  └────┬─────┘  └────┬─────┘  └───┬───┘  └───┬────┘ │
│       └──────────────┴────────────┴──────────┘      │
│                       │                              │
│              ┌────────┴────────┐                     │
│              │   Zustand Store │                     │
│              └────────┬────────┘                     │
│                       │                              │
│              ┌────────┴────────┐                     │
│              │    api.ts       │                     │
│              │  (REST + WS)   │                     │
│              └────────┬────────┘                     │
└───────────────────────┼─────────────────────────────┘
                        │  HTTP / WebSocket
┌───────────────────────┼─────────────────────────────┐
│              cao-server (:9889)                       │
│  REST API: /sessions, /terminals, /agents, /flows    │
│  WebSocket: /terminals/{id}/ws (live PTY)            │
│  Settings: /settings/agent-dirs                      │
└──────────────────────────────────────────────────────┘
```

### Tech Stack

| Layer | Technology |
|-------|-----------|
| UI Framework | React 18 |
| State Management | Zustand |
| Styling | Tailwind CSS |
| Terminal Emulator | xterm.js |
| Icons | Lucide React |
| Build Tool | Vite |
| Testing | Vitest + React Testing Library |
| Language | TypeScript |

### Data Flow

1. **`api.ts`** — HTTP client wrapping all `cao-server` REST endpoints (sessions, terminals, agents, flows, settings). Handles timeouts and error responses.
2. **`store.ts`** — Zustand store that holds application state (sessions, active session, terminal statuses, snackbar notifications). Components subscribe to slices of state, re-rendering only when data changes.
3. **Components** — React components consume the store and call `api.ts` functions. No direct `fetch()` calls in components.

### Dev vs Production

| Mode | Frontend | Backend | URL |
|------|----------|---------|-----|
| **Development** | `npm run dev` (Vite dev server with hot-reload) | `cao-server` | `http://localhost:5173` |
| **Production** | `npm run build` (static files emitted into `src/cli_agent_orchestrator/web_ui/`) | `cao-server` serves the bundled UI from the installed package | `http://localhost:9889` |

In development mode, Vite proxies API requests (`/sessions`, `/terminals`, `/agents`, `/flows`, `/settings`, `/health`) to `cao-server` on port 9889 (configured in `vite.config.ts`).

## Pages and Components

### Home (`DashboardHome.tsx`)

The main dashboard showing all active sessions with their terminals. Provides session creation (provider + agent profile selection), session deletion, and real-time terminal status via polling. Clicking a terminal opens it in the Terminal View.

### Agents (`AgentPanel.tsx`)

Lists all discovered agent profiles from all configured directories (built-in, local store, provider-specific, custom). Shows profile name, description, and source label. Supports launching agents directly with provider and working directory selection.

### Flows (`FlowsPanel.tsx`)

Manages scheduled agent sessions (cron-based). Lists all flows with schedule, next run time, and enabled status. Supports adding flows from markdown files, enabling/disabling, manual triggering, and removal.

### Settings (`SettingsPanel.tsx`)

Configures agent profile directories per provider. Reads and writes to `~/.aws/cli-agent-orchestrator/settings.json` via the `/settings/agent-dirs` API endpoint. Default directories:

| Provider | Default Directory |
|----------|------------------|
| Kiro CLI | `~/.kiro/agents` |
| Q CLI | `~/.aws/amazonq/cli-agents` |
| Claude Code | `~/.aws/cli-agent-orchestrator/agent-store` |
| Codex | `~/.aws/cli-agent-orchestrator/agent-store` |
| CAO Installed | `~/.aws/cli-agent-orchestrator/agent-context` |

Users can also add extra custom directories that are scanned for agent profiles.

For details on the settings service backend, see [docs/settings.md](../docs/settings.md).

### Terminal View (`TerminalView.tsx`)

Full PTY terminal access via WebSocket (`/terminals/{id}/ws`). Uses xterm.js for terminal rendering with automatic fit-to-container sizing.

### Supporting Components

| Component | Purpose |
|-----------|---------|
| `InboxPanel.tsx` | Displays agent-to-agent messages queued in a terminal's inbox |
| `OutputViewer.tsx` | Extracts and displays the last assistant response from a terminal |
| `StatusBadge.tsx` | Color-coded terminal status indicator (idle, processing, completed, error) |
| `ConfirmModal.tsx` | Reusable confirmation dialog for destructive actions |
| `CustomSelect.tsx` | Styled dropdown select component |
| `ErrorBoundary.tsx` | React error boundary with fallback UI |

## Project Structure

```
web/
├── src/
│   ├── api.ts              # REST + WebSocket client for cao-server
│   ├── App.tsx             # Root component with tab navigation
│   ├── store.ts            # Zustand global state
│   ├── main.tsx            # React entry point
│   ├── index.css           # Tailwind CSS imports
│   ├── components/
│   │   ├── DashboardHome.tsx
│   │   ├── AgentPanel.tsx
│   │   ├── FlowsPanel.tsx
│   │   ├── SettingsPanel.tsx
│   │   ├── TerminalView.tsx
│   │   ├── InboxPanel.tsx
│   │   ├── OutputViewer.tsx
│   │   ├── StatusBadge.tsx
│   │   ├── ConfirmModal.tsx
│   │   ├── CustomSelect.tsx
│   │   └── ErrorBoundary.tsx
│   └── test/
│       ├── setup.ts
│       ├── api.test.ts
│       ├── store.test.ts
│       └── components.test.tsx
├── vite.config.ts
├── tailwind.config.js
├── tsconfig.json
├── package.json
└── README.md               # (this file)
```

## Development

```bash
# Install dependencies
cd web/
npm install

# Start dev server (requires cao-server running on :9889)
npm run dev

# Run tests
npm test

# Build for production
npm run build
```

## Testing

```bash
# Run frontend tests
cd web/
npm test

# Run from project root
uv run pytest test/ -k "web"  # Backend API tests
cd web/ && npm test            # Frontend unit tests
```
